from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from temporalio.api.common.v1 import Payload

from codex_orchestrator.contracts import (
    StartGoalCommand,
    UserMessageCommand,
    WorkflowConfig,
)
from codex_orchestrator.domain import (
    Budget,
    OperationKind,
    ResultDisposition,
    SupervisorState,
    SupervisorStatus,
)
from codex_orchestrator.payload_security import (
    AesGcmPayloadCodec,
    PayloadSecurityError,
    _windows_acl_is_trusted,
    encrypted_data_converter,
)


class AesGcmPayloadCodecTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.key = os.urandom(32)
        self.key_id = "test-current"
        self.codec = AesGcmPayloadCodec(key_id=self.key_id, key=self.key)

    async def test_round_trip_hides_complete_original_payload(self) -> None:
        objective = b"single-open APK evidence objective"
        original = Payload(
            metadata={
                "encoding": b"json/plain",
                "private-metadata": b"must also be encrypted",
            },
            data=b'{"objective":"' + objective + b'"}',
        )

        (encrypted,) = await self.codec.encode([original])
        wire = encrypted.SerializeToString(deterministic=True)

        self.assertNotIn(objective, wire)
        self.assertNotIn(b"json/plain", wire)
        self.assertNotIn(b"private-metadata", wire)
        self.assertEqual(b"binary/encrypted", encrypted.metadata["encoding"])
        self.assertEqual(b"aes-256-gcm", encrypted.metadata["encryption-algorithm"])
        self.assertEqual(b"1", encrypted.metadata["encryption-version"])
        self.assertEqual(b"test-current", encrypted.metadata["encryption-key-id"])
        self.assertEqual([original], await self.codec.decode([encrypted]))

    async def test_data_converter_hides_goal_and_user_message(self) -> None:
        objective = "Collect Java native ANR evidence on one launch"
        user_message = "Stop automation and answer only this new request"
        values = [
            StartGoalCommand(
                command_seq=1,
                objective=objective,
                budget=Budget(max_automatic_turns=2),
            ),
            UserMessageCommand(
                command_seq=2,
                message_id="message-2",
                text=user_message,
            ),
        ]
        converter = encrypted_data_converter(key_id=self.key_id, key=self.key)

        encrypted = await converter.encode(values)
        history_bytes = b"".join(
            payload.SerializeToString(deterministic=True) for payload in encrypted
        )

        self.assertNotIn(objective.encode("utf-8"), history_bytes)
        self.assertNotIn(user_message.encode("utf-8"), history_bytes)
        self.assertTrue(
            all(
                payload.metadata["encoding"] == b"binary/encrypted"
                for payload in encrypted
            )
        )
        restored = await converter.decode(
            encrypted,
            [StartGoalCommand, UserMessageCommand],
        )
        self.assertEqual(values, restored)

    async def test_encrypted_converter_preserves_nested_config_and_str_enums(
        self,
    ) -> None:
        supervisor = SupervisorState(
            workflow_key="encrypted-round-trip",
            project_root=r"C:\project",
        )
        supervisor.start_goal(
            command_seq=1,
            objective="nested encrypted objective",
            now_seconds=100,
        )
        supervisor.next_operation(now_seconds=101)
        config = WorkflowConfig(state=supervisor)
        values = [
            config,
            SupervisorStatus.ACTIVE,
            OperationKind.AUTOMATIC_TURN,
            ResultDisposition.CONTINUE,
        ]
        converter = encrypted_data_converter(key_id=self.key_id, key=self.key)

        encrypted = await converter.encode(values)
        restored = await converter.decode(
            encrypted,
            [
                WorkflowConfig,
                SupervisorStatus,
                OperationKind,
                ResultDisposition,
            ],
        )

        self.assertEqual(values, restored)
        self.assertIsInstance(restored[0].state.status, SupervisorStatus)
        self.assertIsInstance(restored[0].state.current_operation.kind, OperationKind)
        self.assertIsInstance(restored[1], SupervisorStatus)
        self.assertIsInstance(restored[2], OperationKind)
        self.assertIsInstance(restored[3], ResultDisposition)

    async def test_same_payload_uses_a_fresh_nonce(self) -> None:
        payload = Payload(metadata={"encoding": b"json/plain"}, data=b"same")

        (first,) = await self.codec.encode([payload])
        (second,) = await self.codec.encode([payload])

        self.assertNotEqual(first.data, second.data)
        self.assertNotEqual(first.data[:12], second.data[:12])

    async def test_wrong_key_fails_without_secret_details(self) -> None:
        plaintext = b"do not include this in an error"
        (encrypted,) = await self.codec.encode([Payload(data=plaintext)])
        wrong_key = os.urandom(32)
        wrong_codec = AesGcmPayloadCodec(key_id=self.key_id, key=wrong_key)

        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^encrypted payload authentication failed$",
        ) as caught:
            await wrong_codec.decode([encrypted])

        error = str(caught.exception)
        self.assertNotIn(plaintext.decode("ascii"), error)
        self.assertNotIn(self.key.hex(), error)
        self.assertNotIn(wrong_key.hex(), error)
        self.assertIsNone(caught.exception.__cause__)

    async def test_ciphertext_tampering_fails_closed(self) -> None:
        (encrypted,) = await self.codec.encode([Payload(data=b"protected")])
        tampered = Payload()
        tampered.CopyFrom(encrypted)
        changed = bytearray(tampered.data)
        changed[-1] ^= 1
        tampered.data = bytes(changed)

        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^encrypted payload authentication failed$",
        ):
            await self.codec.decode([tampered])

    async def test_plaintext_and_unknown_envelopes_are_rejected(self) -> None:
        plaintext = Payload(metadata={"encoding": b"json/plain"}, data=b"{}")
        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^unencrypted payload rejected$",
        ):
            await self.codec.decode([plaintext])

        (encrypted,) = await self.codec.encode([Payload(data=b"protected")])
        encrypted.metadata["encryption-version"] = b"2"
        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^encrypted payload envelope is unsupported$",
        ):
            await self.codec.decode([encrypted])

    async def test_key_identifier_mismatch_is_distinct_from_bad_ciphertext(self) -> None:
        (encrypted,) = await self.codec.encode([Payload(data=b"protected")])
        rotated = AesGcmPayloadCodec(key_id="rotated", key=os.urandom(32))

        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^encrypted payload key identifier mismatch$",
        ):
            await rotated.decode([encrypted])

    async def test_truncated_envelope_is_rejected_before_decrypt(self) -> None:
        truncated = Payload(
            metadata={
                "encoding": b"binary/encrypted",
                "encryption-algorithm": b"aes-256-gcm",
                "encryption-version": b"1",
                "encryption-key-id": b"test-current",
            },
            data=os.urandom(27),
        )

        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^encrypted payload is malformed$",
        ):
            await self.codec.decode([truncated])


class PayloadKeySourceTests(unittest.IsolatedAsyncioTestCase):
    def test_key_id_is_explicit_bounded_nonsecret_metadata(self) -> None:
        for invalid in ("", "contains spaces", "/path-like", "x" * 65):
            with self.subTest(key_id=invalid):
                with self.assertRaisesRegex(
                    PayloadSecurityError,
                    "^payload key ID is invalid$",
                ):
                    AesGcmPayloadCodec(key_id=invalid, key=os.urandom(32))

    def test_exactly_one_runtime_key_source_is_required(self) -> None:
        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^exactly one payload key source is required$",
        ):
            AesGcmPayloadCodec(key_id="current")

        with tempfile.TemporaryDirectory() as temporary:
            key_path = Path(temporary) / "payload.key"
            key_path.write_bytes(os.urandom(32))
            with self.assertRaisesRegex(
                PayloadSecurityError,
                "^exactly one payload key source is required$",
            ):
                AesGcmPayloadCodec(
                    key_id="current",
                    key=os.urandom(32),
                    key_path=key_path,
                )

    def test_key_must_be_exactly_32_immutable_bytes(self) -> None:
        for invalid in (b"", os.urandom(31), os.urandom(33)):
            with self.subTest(length=len(invalid)):
                with self.assertRaisesRegex(
                    PayloadSecurityError,
                    "^AES-256-GCM key must contain exactly 32 raw bytes$",
                ):
                    AesGcmPayloadCodec(key_id="current", key=invalid)

        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^payload key must be bytes$",
        ):
            AesGcmPayloadCodec(
                key_id="current",
                key=bytearray(os.urandom(32)),  # type: ignore[arg-type]
            )

    def test_optional_manifest_hash_is_validated_without_disclosure(self) -> None:
        key = os.urandom(32)
        expected = hashlib.sha256(key).hexdigest()
        AesGcmPayloadCodec(
            key_id="current",
            key=key,
            expected_key_sha256=expected,
        )

        for invalid_hash in ("", "A" * 64, "0" * 63):
            with self.subTest(invalid_hash=invalid_hash):
                with self.assertRaisesRegex(
                    PayloadSecurityError,
                    "^expected payload key hash is invalid$",
                ):
                    AesGcmPayloadCodec(
                        key_id="current",
                        key=key,
                        expected_key_sha256=invalid_hash,
                    )

        wrong_hash = hashlib.sha256(os.urandom(32)).hexdigest()
        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^payload key hash mismatch$",
        ) as caught:
            AesGcmPayloadCodec(
                key_id="current",
                key=key,
                expected_key_sha256=wrong_hash,
            )
        self.assertNotIn(expected, str(caught.exception))
        self.assertNotIn(wrong_hash, str(caught.exception))

    def test_key_path_is_absolute_existing_regular_raw_file(self) -> None:
        with self.assertRaisesRegex(
            PayloadSecurityError,
            "^payload key path must be absolute$",
        ):
            AesGcmPayloadCodec(key_id="current", key_path=Path("relative.key"))

        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.key"
            with self.assertRaisesRegex(
                PayloadSecurityError,
                "^payload key file could not be read$",
            ) as caught:
                AesGcmPayloadCodec(key_id="current", key_path=missing)
            self.assertNotIn(str(missing), str(caught.exception))

            with self.assertRaisesRegex(
                PayloadSecurityError,
                "^payload key file could not be read$",
            ):
                AesGcmPayloadCodec(key_id="current", key_path=Path(temporary))

            oversized = Path(temporary) / "oversized.key"
            oversized.write_bytes(os.urandom(33))
            with patch(
                "codex_orchestrator.payload_security._validate_key_file_permissions"
            ) as permission_check:
                with self.assertRaisesRegex(
                    PayloadSecurityError,
                    "^AES-256-GCM key must contain exactly 32 raw bytes$",
                ):
                    AesGcmPayloadCodec(key_id="current", key_path=oversized)
                permission_check.assert_called_once()

    def test_unsafe_or_unverifiable_file_permissions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key_path = Path(temporary) / "payload.key"
            key_path.write_bytes(os.urandom(32))
            with patch(
                "codex_orchestrator.payload_security._validate_key_file_permissions",
                side_effect=PayloadSecurityError(
                    "payload key file permissions are unsafe"
                ),
            ) as permission_check:
                with self.assertRaisesRegex(
                    PayloadSecurityError,
                    "^payload key file permissions are unsafe$",
                ) as caught:
                    AesGcmPayloadCodec(key_id="current", key_path=key_path)
                permission_check.assert_called_once()
                self.assertNotIn(str(key_path), str(caught.exception))

    async def test_key_file_round_trip_and_repr_do_not_expose_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key_path = Path(temporary) / "payload.key"
            key_material = os.urandom(32)
            key_path.write_bytes(key_material)
            with patch(
                "codex_orchestrator.payload_security._validate_key_file_permissions"
            ) as permission_check:
                codec = AesGcmPayloadCodec(
                    key_id="current",
                    key_path=key_path,
                )
                permission_check.assert_called_once()

            representation = repr(codec)
            self.assertNotIn(str(key_path), representation)
            self.assertNotIn(key_material.hex(), representation)
            self.assertIn("<redacted>", representation)

            original = Payload(data=b"runtime-key-file")
            encrypted = await codec.encode([original])
            self.assertEqual([original], await codec.decode(encrypted))


class WindowsAclPolicyTests(unittest.TestCase):
    current_sid = "S-1-5-21-1000"

    def trusted(self, **overrides: object) -> bool:
        values = {
            "owner_sid": self.current_sid,
            "current_user_sid": self.current_sid,
            "dacl_protected": True,
            "aces": [
                (0, 0x001F01FF, self.current_sid),
                (0, 0x001F01FF, "S-1-5-18"),
                (0, 0x001F01FF, "S-1-5-32-544"),
            ],
        }
        values.update(overrides)
        return _windows_acl_is_trusted(**values)  # type: ignore[arg-type]

    def test_private_current_user_system_admin_acl_is_trusted(self) -> None:
        self.assertTrue(self.trusted())

    def test_inherited_or_untrusted_owner_acl_is_rejected(self) -> None:
        self.assertFalse(self.trusted(dacl_protected=False))
        self.assertFalse(self.trusted(owner_sid="S-1-5-21-2000"))

    def test_other_principal_read_write_or_policy_access_is_rejected(self) -> None:
        other = "S-1-5-21-2000"
        for access_mask in (
            0x80000000,  # GENERIC_READ
            0x40000000,  # GENERIC_WRITE
            0x00000001,  # FILE_READ_DATA
            0x00000002,  # FILE_WRITE_DATA
            0x00010000,  # DELETE
            0x00040000,  # WRITE_DAC
        ):
            with self.subTest(access_mask=access_mask):
                self.assertFalse(self.trusted(aces=[(0, access_mask, other)]))

    def test_metadata_only_access_does_not_create_key_access(self) -> None:
        other = "S-1-5-21-2000"
        self.assertTrue(
            self.trusted(
                aces=[
                    (0, 0x00020000 | 0x00100000, other),  # READ_CONTROL + SYNCHRONIZE
                    (0, 0x00000001, self.current_sid),
                ]
            )
        )

    def test_complex_or_unknown_ace_types_fail_closed(self) -> None:
        self.assertFalse(self.trusted(aces=[(5, 0, "")]))


class PayloadDependencyPinTests(unittest.TestCase):
    def test_encryption_runtime_graph_is_exactly_pinned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        lock = (root / "requirements.lock").read_text(encoding="utf-8")

        self.assertIn('"cryptography==49.0.0"', pyproject)
        for requirement in (
            "cffi==2.1.1",
            "cryptography==49.0.0",
            "pycparser==3.0",
        ):
            self.assertIn(requirement, lock.splitlines())


if __name__ == "__main__":
    unittest.main()
