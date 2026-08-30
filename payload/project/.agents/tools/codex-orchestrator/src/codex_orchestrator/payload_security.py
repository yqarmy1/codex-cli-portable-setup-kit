"""Authenticated encryption for every Temporal payload.

The codec encrypts the complete serialized :class:`Payload`, including the
original converter metadata.  Only constant envelope metadata remains visible
in Temporal Event History.  Client and Worker processes must use a
``DataConverter`` built with the same runtime key.

There is deliberately no plaintext compatibility path.  A payload without the
exact authenticated-encryption envelope is rejected so a configuration error
cannot silently downgrade protected workflow data to plaintext.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.protobuf.message import DecodeError, EncodeError
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec


_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_AAD = b"codex-durable-orchestrator/temporal-payload/aes-256-gcm/v1"
_BASE_ENVELOPE_METADATA = {
    "encoding": b"binary/encrypted",
    "encryption-algorithm": b"aes-256-gcm",
    "encryption-version": b"1",
}
_ENVELOPE_METADATA_KEYS = frozenset(
    (*_BASE_ENVELOPE_METADATA, "encryption-key-id")
)
_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

# These masks let another principal read, replace, or change the policy of the
# key file. Metadata-only access such as READ_CONTROL and SYNCHRONIZE is not a
# key disclosure or replacement capability.
_WINDOWS_UNSAFE_FILE_ACCESS = (
    0x80000000  # GENERIC_READ
    | 0x40000000  # GENERIC_WRITE
    | 0x10000000  # GENERIC_ALL
    | 0x00000001  # FILE_READ_DATA
    | 0x00000002  # FILE_WRITE_DATA
    | 0x00000004  # FILE_APPEND_DATA
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)
_WINDOWS_TRUSTED_OS_SIDS = frozenset(
    {
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Builtin Administrators
    }
)


class PayloadSecurityError(RuntimeError):
    """Payload encryption configuration or authentication failed."""


def _windows_acl_is_trusted(
    *,
    owner_sid: str,
    current_user_sid: str,
    dacl_protected: bool,
    aces: Sequence[tuple[int, int, str]],
) -> bool:
    """Apply the conservative policy to an already parsed Windows DACL."""

    trusted_sids = _WINDOWS_TRUSTED_OS_SIDS | {current_user_sid}
    if not dacl_protected or owner_sid not in trusted_sids:
        return False

    for ace_type, access_mask, trustee_sid in aces:
        # A key file should need only ordinary allow/deny ACEs. Reject object,
        # callback, compound, or future ACE types instead of misparsing them.
        if ace_type not in {0, 1}:  # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE
            return False
        if (
            ace_type == 0
            and access_mask & _WINDOWS_UNSAFE_FILE_ACCESS
            and trustee_sid not in trusted_sids
        ):
            return False
    return True


def _validate_windows_key_file_permissions(key_file: BinaryIO) -> None:
    """Validate owner and DACL on the exact open Windows file handle."""

    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _AceHeader(ctypes.Structure):
            _fields_ = [
                ("ace_type", wintypes.BYTE),
                ("ace_flags", wintypes.BYTE),
                ("ace_size", wintypes.WORD),
            ]

        class _AccessAllowedAce(ctypes.Structure):
            _fields_ = [
                ("header", _AceHeader),
                ("mask", wintypes.DWORD),
                ("sid_start", wintypes.DWORD),
            ]

        class _AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("ace_count", wintypes.DWORD),
                ("acl_bytes_in_use", wintypes.DWORD),
                ("acl_bytes_free", wintypes.DWORD),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        get_security_info = advapi32.GetSecurityInfo
        get_security_info.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_security_info.restype = wintypes.DWORD

        get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
        get_security_descriptor_control.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_security_descriptor_control.restype = wintypes.BOOL

        get_acl_information = advapi32.GetAclInformation
        get_acl_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        get_acl_information.restype = wintypes.BOOL

        get_ace = advapi32.GetAce
        get_ace.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_ace.restype = wintypes.BOOL

        convert_sid_to_string = advapi32.ConvertSidToStringSidW
        convert_sid_to_string.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        convert_sid_to_string.restype = wintypes.BOOL

        open_process_token = advapi32.OpenProcessToken
        open_process_token.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        open_process_token.restype = wintypes.BOOL

        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_token_information.restype = wintypes.BOOL

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        def sid_to_string(sid: ctypes.c_void_p) -> str:
            if not sid:
                raise OSError("missing SID")
            sid_text = wintypes.LPWSTR()
            if not convert_sid_to_string(sid, ctypes.byref(sid_text)):
                raise OSError("SID conversion failed")
            try:
                if sid_text.value is None:
                    raise OSError("SID conversion returned no value")
                return sid_text.value
            finally:
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))

        token = wintypes.HANDLE()
        if not open_process_token(
            kernel32.GetCurrentProcess(),
            0x0008,  # TOKEN_QUERY
            ctypes.byref(token),
        ):
            raise OSError("process token could not be opened")
        try:
            token_size = wintypes.DWORD()
            get_token_information(
                token,
                1,  # TokenUser
                None,
                0,
                ctypes.byref(token_size),
            )
            if token_size.value == 0:
                raise OSError("process token user size was unavailable")
            token_buffer = ctypes.create_string_buffer(token_size.value)
            if not get_token_information(
                token,
                1,
                token_buffer,
                token_size,
                ctypes.byref(token_size),
            ):
                raise OSError("process token user was unavailable")
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is
            # the PSID pointer on both 32-bit and 64-bit Windows.
            current_sid_pointer = ctypes.cast(
                token_buffer,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents
            current_user_sid = sid_to_string(current_sid_pointer)
        finally:
            if token:
                kernel32.CloseHandle(token)

        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        try:
            status = get_security_info(
                wintypes.HANDLE(msvcrt.get_osfhandle(key_file.fileno())),
                1,  # SE_FILE_OBJECT
                0x00000001 | 0x00000004,  # OWNER + DACL_SECURITY_INFORMATION
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(security_descriptor),
            )
            if status != 0 or not security_descriptor:
                raise OSError("file security descriptor was unavailable")

            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not get_security_descriptor_control(
                security_descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                raise OSError("security descriptor control was unavailable")
            dacl_present = bool(control.value & 0x0004)  # SE_DACL_PRESENT
            dacl_protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
            if not dacl_present or not dacl:
                raise PayloadSecurityError(
                    "payload key file permissions are unsafe"
                )

            acl_info = _AclSizeInformation()
            if not get_acl_information(
                dacl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                2,  # AclSizeInformation
            ):
                raise OSError("file ACL information was unavailable")

            aces: list[tuple[int, int, str]] = []
            for index in range(acl_info.ace_count):
                ace_pointer = ctypes.c_void_p()
                if not get_ace(dacl, index, ctypes.byref(ace_pointer)):
                    raise OSError("file ACE was unavailable")
                header = ctypes.cast(
                    ace_pointer,
                    ctypes.POINTER(_AceHeader),
                ).contents
                if header.ace_type == 0:
                    if header.ace_size < ctypes.sizeof(_AccessAllowedAce):
                        raise OSError("file allow ACE was malformed")
                    allow_ace = ctypes.cast(
                        ace_pointer,
                        ctypes.POINTER(_AccessAllowedAce),
                    ).contents
                    sid_pointer = ctypes.c_void_p(
                        ace_pointer.value + _AccessAllowedAce.sid_start.offset
                    )
                    aces.append(
                        (
                            int(header.ace_type),
                            int(allow_ace.mask),
                            sid_to_string(sid_pointer),
                        )
                    )
                else:
                    aces.append((int(header.ace_type), 0, ""))

            if not _windows_acl_is_trusted(
                owner_sid=sid_to_string(owner),
                current_user_sid=current_user_sid,
                dacl_protected=dacl_protected,
                aces=aces,
            ):
                raise PayloadSecurityError(
                    "payload key file permissions are unsafe"
                )
        finally:
            if security_descriptor:
                kernel32.LocalFree(security_descriptor)
    except PayloadSecurityError:
        raise
    except (ImportError, OSError, TypeError, ValueError):
        raise PayloadSecurityError(
            "payload key file permissions could not be validated"
        ) from None


def _validate_key_file_permissions(key_file: BinaryIO) -> None:
    """Fail closed unless the open key file is private to a trusted owner."""

    try:
        file_stat = os.fstat(key_file.fileno())
    except (OSError, ValueError):
        raise PayloadSecurityError(
            "payload key file permissions could not be validated"
        ) from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise PayloadSecurityError("payload key file is not a regular file")

    if os.name == "nt":
        _validate_windows_key_file_permissions(key_file)
        return

    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise PayloadSecurityError(
            "payload key file permission validation is unsupported"
        )
    if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise PayloadSecurityError("payload key file permissions are unsafe")


class AesGcmPayloadCodec(PayloadCodec):
    """Strict AES-256-GCM Temporal payload codec.

    Exactly one key source is required. ``key`` must be 32 runtime-provided
    bytes. ``key_path`` must name an absolute regular file containing exactly
    32 raw bytes. ``key_id`` is an explicit nonsecret rotation label written to
    the envelope. The path and key material are not retained as public state and
    never appear in exception messages or ``repr`` output.
    """

    def __init__(
        self,
        *,
        key_id: str,
        key: bytes | None = None,
        key_path: str | os.PathLike[str] | None = None,
        expected_key_sha256: str | None = None,
    ) -> None:
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
            raise PayloadSecurityError("payload key ID is invalid")
        if (key is None) == (key_path is None):
            raise PayloadSecurityError("exactly one payload key source is required")

        if key_path is not None:
            key_material = self._read_key_file(key_path)
        else:
            if not isinstance(key, bytes):
                raise PayloadSecurityError("payload key must be bytes")
            key_material = key

        if len(key_material) != _KEY_BYTES:
            raise PayloadSecurityError(
                "AES-256-GCM key must contain exactly 32 raw bytes"
            )
        if expected_key_sha256 is not None:
            if (
                not isinstance(expected_key_sha256, str)
                or not _SHA256_PATTERN.fullmatch(expected_key_sha256)
            ):
                raise PayloadSecurityError("expected payload key hash is invalid")
            actual_key_sha256 = hashlib.sha256(key_material).hexdigest()
            if not hmac.compare_digest(actual_key_sha256, expected_key_sha256):
                raise PayloadSecurityError("payload key hash mismatch")

        try:
            self._aead = AESGCM(key_material)
        except (TypeError, ValueError):
            raise PayloadSecurityError("payload key is invalid") from None
        self._key_id = key_id.encode("ascii")
        self._aad = _AAD + b"/" + self._key_id
        self._envelope_metadata = {
            **_BASE_ENVELOPE_METADATA,
            "encryption-key-id": self._key_id,
        }

    @staticmethod
    def _read_key_file(key_path: str | os.PathLike[str]) -> bytes:
        try:
            path = Path(key_path)
        except (TypeError, ValueError):
            raise PayloadSecurityError("payload key path is invalid") from None
        if not path.is_absolute():
            raise PayloadSecurityError("payload key path must be absolute")

        try:
            resolved = path.resolve(strict=True)
            with resolved.open("rb") as key_file:
                _validate_key_file_permissions(key_file)
                # Read at most one byte beyond the accepted size.  This both
                # bounds the read and detects oversized/encoded key files.
                key_material = key_file.read(_KEY_BYTES + 1)
        except PayloadSecurityError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise PayloadSecurityError("payload key file could not be read") from None
        return key_material

    def __repr__(self) -> str:
        return "AesGcmPayloadCodec(key=<redacted>, strict=True)"

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Encrypt complete serialized payloads with independent random nonces."""

        encoded: list[Payload] = []
        for payload in payloads:
            try:
                cleartext = payload.SerializeToString(deterministic=True)
                nonce = os.urandom(_NONCE_BYTES)
                ciphertext = self._aead.encrypt(nonce, cleartext, self._aad)
            except (EncodeError, OSError, OverflowError, TypeError, ValueError):
                raise PayloadSecurityError("payload encryption failed") from None
            encoded.append(
                Payload(
                    metadata=self._envelope_metadata,
                    data=nonce + ciphertext,
                )
            )
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Authenticate and decrypt payloads, rejecting every downgrade path."""

        decoded: list[Payload] = []
        for payload in payloads:
            metadata = dict(payload.metadata)
            if metadata.get("encoding") != _BASE_ENVELOPE_METADATA["encoding"]:
                raise PayloadSecurityError("unencrypted payload rejected")
            if (
                frozenset(metadata) != _ENVELOPE_METADATA_KEYS
                or metadata.get("encryption-algorithm")
                != _BASE_ENVELOPE_METADATA["encryption-algorithm"]
                or metadata.get("encryption-version")
                != _BASE_ENVELOPE_METADATA["encryption-version"]
            ):
                raise PayloadSecurityError("encrypted payload envelope is unsupported")
            if metadata["encryption-key-id"] != self._key_id:
                raise PayloadSecurityError(
                    "encrypted payload key identifier mismatch"
                )

            if len(payload.data) < _NONCE_BYTES + _TAG_BYTES:
                raise PayloadSecurityError("encrypted payload is malformed")

            nonce = payload.data[:_NONCE_BYTES]
            ciphertext = payload.data[_NONCE_BYTES:]
            try:
                cleartext = self._aead.decrypt(nonce, ciphertext, self._aad)
            except (InvalidTag, OverflowError, TypeError, ValueError):
                raise PayloadSecurityError(
                    "encrypted payload authentication failed"
                ) from None

            try:
                decoded.append(Payload.FromString(cleartext))
            except DecodeError:
                raise PayloadSecurityError(
                    "decrypted payload could not be decoded"
                ) from None
        return decoded


def encrypted_data_converter(
    *,
    key_id: str,
    key: bytes | None = None,
    key_path: str | os.PathLike[str] | None = None,
    expected_key_sha256: str | None = None,
) -> DataConverter:
    """Build the Temporal converter that both Client and Worker must share."""

    codec = AesGcmPayloadCodec(
        key_id=key_id,
        key=key,
        key_path=key_path,
        expected_key_sha256=expected_key_sha256,
    )
    return dataclasses.replace(DataConverter.default, payload_codec=codec)


def encrypted_data_converter_for_runtime(paths: object) -> DataConverter:
    """Build a converter from the validated nonsecret runtime manifest."""

    # Local import keeps ``local_runtime`` dependency-free for bootstrap and
    # process-record validation.
    from .local_runtime import RuntimePaths, load_payload_encryption_config

    if not isinstance(paths, RuntimePaths):
        raise TypeError("paths must be RuntimePaths")
    config = load_payload_encryption_config(paths)
    return encrypted_data_converter(
        key_id=config.key_id,
        key_path=config.key_path,
        expected_key_sha256=config.key_sha256,
    )


__all__ = [
    "AesGcmPayloadCodec",
    "PayloadSecurityError",
    "encrypted_data_converter",
    "encrypted_data_converter_for_runtime",
]
