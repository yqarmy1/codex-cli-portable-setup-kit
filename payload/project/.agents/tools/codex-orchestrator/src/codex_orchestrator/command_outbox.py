"""Crash-safe encrypted single-project interactive command outbox.

The outbox stores exactly one unresolved typed Temporal Update.  It is written
and made durable before the first ``execute_update`` attempt.  A process crash
therefore leaves either no command or a complete authenticated record that can
only be resent explicitly with the same Update ID and wire payload.

No plaintext is ever written to a temporary or final file.  Resolution is an
encrypted tombstone written through the same atomic path; deleting a file is
not used as the durability acknowledgement.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import ControlCommand, StartGoalCommand, UserMessageCommand
from .domain import Budget
from .local_runtime import RuntimePaths, workflow_id_for_project


OUTBOX_SCHEMA_VERSION = 1
OUTBOX_AAD = b"codex-durable-orchestrator/interactive-command-outbox/v1"
OUTBOX_MAGIC = b"CDXOBX01"
OUTBOX_NONCE_BYTES = 12
OUTBOX_TAG_BYTES = 16
MAX_OUTBOX_BYTES = 2 * 1024 * 1024
_KEY_BYTES = 32
_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_KINDS = frozenset({"start-goal", "message", "pause", "resume", "cancel"})


class CommandOutboxError(RuntimeError):
    """Base class for categorical outbox failures with no sensitive detail."""


class CommandOutboxSecurityError(CommandOutboxError):
    """Ciphertext, key, envelope, project binding, or record validation failed."""


class CommandOutboxConflictError(CommandOutboxError):
    """A different unresolved command already owns the single-project slot."""


@dataclass(frozen=True, slots=True)
class PreparedCommandRecord:
    """Exact typed Update data; every field is excluded from ``repr``."""

    schema_version: int = field(repr=False)
    project_key: str = field(repr=False)
    workflow_id: str = field(repr=False)
    kind: str = field(repr=False)
    update_id: str = field(repr=False)
    command_id: str = field(repr=False)
    command_seq: int = field(repr=False)
    message_id: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)
    budget: Budget | None = field(default=None, repr=False)

    def validate(self) -> None:
        if self.schema_version != OUTBOX_SCHEMA_VERSION:
            raise CommandOutboxSecurityError("unsupported command outbox schema")
        for value in (
            self.project_key,
            self.workflow_id,
            self.kind,
            self.update_id,
            self.command_id,
        ):
            if not isinstance(value, str) or not value.strip():
                raise CommandOutboxSecurityError("command outbox record is invalid")
        if self.kind not in _KINDS:
            raise CommandOutboxSecurityError("command outbox record is invalid")
        if type(self.command_seq) is not int or self.command_seq <= 0:
            raise CommandOutboxSecurityError("command outbox record is invalid")
        if self.kind == "start-goal":
            if (
                not isinstance(self.text, str)
                or not self.text.strip()
                or self.message_id is not None
                or self.budget is None
            ):
                raise CommandOutboxSecurityError("command outbox record is invalid")
            self.budget.validate()
        elif self.kind == "message":
            if (
                not isinstance(self.text, str)
                or not self.text.strip()
                or not isinstance(self.message_id, str)
                or not self.message_id.strip()
                or self.budget is not None
            ):
                raise CommandOutboxSecurityError("command outbox record is invalid")
        elif (
            self.text is not None
            or self.message_id is not None
            or self.budget is not None
        ):
            raise CommandOutboxSecurityError("command outbox record is invalid")

    def typed_payload(self) -> StartGoalCommand | UserMessageCommand | ControlCommand:
        self.validate()
        if self.kind == "start-goal":
            assert self.text is not None and self.budget is not None
            return StartGoalCommand(
                command_seq=self.command_seq,
                objective=self.text,
                budget=self.budget,
            )
        if self.kind == "message":
            assert self.text is not None and self.message_id is not None
            return UserMessageCommand(
                command_seq=self.command_seq,
                message_id=self.message_id,
                text=self.text,
            )
        return ControlCommand(command_seq=self.command_seq)

    def _json_object(self) -> dict[str, Any]:
        self.validate()
        return {
            "budget": asdict(self.budget) if self.budget is not None else None,
            "command_id": self.command_id,
            "command_seq": self.command_seq,
            "kind": self.kind,
            "message_id": self.message_id,
            "project_key": self.project_key,
            "record_type": "prepared",
            "schema_version": self.schema_version,
            "text": self.text,
            "update_id": self.update_id,
            "workflow_id": self.workflow_id,
        }

    @classmethod
    def _from_json_object(cls, value: dict[str, Any]) -> "PreparedCommandRecord":
        expected = {
            "budget",
            "command_id",
            "command_seq",
            "kind",
            "message_id",
            "project_key",
            "record_type",
            "schema_version",
            "text",
            "update_id",
            "workflow_id",
        }
        if set(value) != expected or value.get("record_type") != "prepared":
            raise CommandOutboxSecurityError("command outbox record is invalid")
        raw_budget = value.get("budget")
        budget: Budget | None
        if raw_budget is None:
            budget = None
        elif isinstance(raw_budget, dict) and set(raw_budget) == {
            "max_automatic_turns",
            "max_tokens",
            "max_elapsed_seconds",
            "max_failures",
            "max_rollovers",
        }:
            try:
                budget = Budget(**raw_budget)
            except (TypeError, ValueError) as exc:
                raise CommandOutboxSecurityError(
                    "command outbox record is invalid"
                ) from exc
        else:
            raise CommandOutboxSecurityError("command outbox record is invalid")
        record = cls(
            schema_version=value.get("schema_version"),
            project_key=value.get("project_key"),
            workflow_id=value.get("workflow_id"),
            kind=value.get("kind"),
            update_id=value.get("update_id"),
            command_id=value.get("command_id"),
            command_seq=value.get("command_seq"),
            message_id=value.get("message_id"),
            text=value.get("text"),
            budget=budget,
        )
        record.validate()
        return record


@dataclass(frozen=True, slots=True)
class _ResolvedMarker:
    schema_version: int = field(repr=False)
    project_key: str = field(repr=False)
    workflow_id: str = field(repr=False)
    command_id: str = field(repr=False)

    def _json_object(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "project_key": self.project_key,
            "record_type": "resolved",
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
        }


_StoredState = PreparedCommandRecord | _ResolvedMarker


class EncryptedCommandOutbox:
    """Authenticated single-slot outbox bound to one RuntimePaths project."""

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        key_id: str,
        key: bytes,
        path: Path | None = None,
    ) -> None:
        paths.validate()
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise CommandOutboxSecurityError("outbox key must be 32 immutable bytes")
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
            raise CommandOutboxSecurityError("outbox key ID is invalid")
        runtime_root = paths.runtime_root.resolve()
        selected = (
            path
            if path is not None
            else runtime_root / "outbox" / f"command-{paths.project_key}.bin"
        )
        if not selected.is_absolute():
            raise CommandOutboxSecurityError("outbox path must be absolute")
        resolved_path = selected.resolve(strict=False)
        try:
            resolved_path.relative_to(runtime_root)
        except ValueError as exc:
            raise CommandOutboxSecurityError(
                "outbox path must remain inside the runtime root"
            ) from exc
        if resolved_path == runtime_root:
            raise CommandOutboxSecurityError("outbox path must be a file")

        self._paths = paths
        self._path = resolved_path
        self._key_id = key_id
        self._cipher = AESGCM(key)

    def __repr__(self) -> str:
        return "EncryptedCommandOutbox(path=<redacted>, key=<redacted>, strict=True)"

    @property
    def path(self) -> Path:
        return self._path

    def load_pending(self) -> PreparedCommandRecord | None:
        state = self._load_state()
        if state is None or isinstance(state, _ResolvedMarker):
            return None
        return state

    def prepare(self, record: PreparedCommandRecord) -> None:
        self._validate_project_binding(record)
        current = self._load_state()
        if isinstance(current, PreparedCommandRecord):
            if current == record:
                return
            raise CommandOutboxConflictError(
                "a different unresolved command owns the outbox"
            )
        self._write_state(record)
        if self._load_state() != record:
            raise CommandOutboxError("durable command outbox verification failed")

    def resolve(self, command_id: str) -> None:
        if not isinstance(command_id, str) or not command_id.strip():
            raise CommandOutboxError("command ID must not be blank")
        current = self._load_state()
        if isinstance(current, _ResolvedMarker):
            if current.command_id == command_id:
                return
            raise CommandOutboxConflictError("outbox resolved marker belongs elsewhere")
        if current is None or current.command_id != command_id:
            raise CommandOutboxConflictError("outbox does not contain this command")
        marker = _ResolvedMarker(
            schema_version=OUTBOX_SCHEMA_VERSION,
            project_key=self._paths.project_key,
            workflow_id=current.workflow_id,
            command_id=command_id,
        )
        self._write_state(marker)
        verified = self._load_state()
        if not isinstance(verified, _ResolvedMarker) or verified != marker:
            raise CommandOutboxError("durable command resolution verification failed")

    def _validate_project_binding(self, record: PreparedCommandRecord) -> None:
        record.validate()
        if (
            record.project_key != self._paths.project_key
            or record.workflow_id
            != workflow_id_for_project(self._paths.project_root)
        ):
            raise CommandOutboxSecurityError("outbox record belongs to another project")

    def _load_state(self) -> _StoredState | None:
        try:
            file_stat = self._path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(file_stat.st_mode) or self._path.is_symlink():
            raise CommandOutboxSecurityError("command outbox is not a regular file")
        if not 0 < file_stat.st_size <= MAX_OUTBOX_BYTES:
            raise CommandOutboxSecurityError("encrypted command outbox size is invalid")
        try:
            envelope = self._path.read_bytes()
        except OSError as exc:
            raise CommandOutboxError("encrypted command outbox could not be read") from exc
        return self._decode(envelope)

    def _write_state(self, state: _StoredState) -> None:
        envelope = self._encode(state)
        if len(envelope) > MAX_OUTBOX_BYTES:
            raise CommandOutboxError("encrypted command outbox exceeds its hard limit")
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        resolved_directory = directory.resolve(strict=True)
        try:
            resolved_directory.relative_to(self._paths.runtime_root.resolve())
        except ValueError as exc:
            raise CommandOutboxSecurityError(
                "outbox directory escaped the runtime root"
            ) from exc
        temporary = directory / (
            f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            self._atomic_replace_write_through(temporary, self._path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_replace_write_through(source: Path, destination: Path) -> None:
        if sys.platform == "win32":
            move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            move_file_ex.restype = ctypes.c_int
            # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            if not move_file_ex(str(source), str(destination), 0x1 | 0x8):
                error = ctypes.get_last_error()
                raise OSError(error, "atomic write-through replace failed")
            return
        os.replace(source, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _encode(self, state: _StoredState) -> bytes:
        if isinstance(state, PreparedCommandRecord):
            self._validate_project_binding(state)
            value = state._json_object()
        else:
            value = state._json_object()
        value["key_id"] = self._key_id
        plaintext = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(OUTBOX_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, OUTBOX_AAD)
        return OUTBOX_MAGIC + nonce + ciphertext

    def _decode(self, envelope: bytes) -> _StoredState:
        minimum = len(OUTBOX_MAGIC) + OUTBOX_NONCE_BYTES + OUTBOX_TAG_BYTES
        if len(envelope) < minimum or not envelope.startswith(OUTBOX_MAGIC):
            raise CommandOutboxSecurityError("encrypted command outbox is invalid")
        nonce_start = len(OUTBOX_MAGIC)
        nonce_end = nonce_start + OUTBOX_NONCE_BYTES
        try:
            plaintext = self._cipher.decrypt(
                envelope[nonce_start:nonce_end],
                envelope[nonce_end:],
                OUTBOX_AAD,
            )
        except (InvalidTag, ValueError):
            raise CommandOutboxSecurityError(
                "encrypted command outbox authentication failed"
            ) from None
        try:
            value = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CommandOutboxSecurityError("command outbox record is invalid") from None
        if not isinstance(value, dict):
            raise CommandOutboxSecurityError("command outbox record is invalid")
        if value.pop("key_id", None) != self._key_id:
            raise CommandOutboxSecurityError("command outbox key ID does not match")
        record_type = value.get("record_type")
        if record_type == "prepared":
            record = PreparedCommandRecord._from_json_object(value)
            self._validate_project_binding(record)
            return record
        if record_type == "resolved":
            expected = {
                "command_id",
                "project_key",
                "record_type",
                "schema_version",
                "workflow_id",
            }
            if set(value) != expected:
                raise CommandOutboxSecurityError("command outbox record is invalid")
            marker = _ResolvedMarker(
                schema_version=value.get("schema_version"),
                project_key=value.get("project_key"),
                workflow_id=value.get("workflow_id"),
                command_id=value.get("command_id"),
            )
            if (
                marker.schema_version != OUTBOX_SCHEMA_VERSION
                or marker.project_key != self._paths.project_key
                or marker.workflow_id
                != workflow_id_for_project(self._paths.project_root)
                or not isinstance(marker.workflow_id, str)
                or not marker.workflow_id.strip()
                or not isinstance(marker.command_id, str)
                or not marker.command_id.strip()
            ):
                raise CommandOutboxSecurityError("command outbox record is invalid")
            return marker
        raise CommandOutboxSecurityError("command outbox record is invalid")


__all__ = [
    "CommandOutboxConflictError",
    "CommandOutboxError",
    "CommandOutboxSecurityError",
    "EncryptedCommandOutbox",
    "OUTBOX_AAD",
    "OUTBOX_SCHEMA_VERSION",
    "PreparedCommandRecord",
]
