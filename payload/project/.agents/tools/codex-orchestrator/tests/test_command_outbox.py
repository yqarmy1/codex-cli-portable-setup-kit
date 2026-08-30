from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from codex_orchestrator.command_outbox import (
    CommandOutboxConflictError,
    CommandOutboxSecurityError,
    EncryptedCommandOutbox,
    OUTBOX_SCHEMA_VERSION,
    PreparedCommandRecord,
)
from codex_orchestrator.domain import Budget
from codex_orchestrator.local_runtime import RuntimePaths, workflow_id_for_project


class EncryptedCommandOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "project-a"
        self.project.mkdir()
        self.paths = RuntimePaths.from_project_root(
            self.project,
            workspace_root=self.workspace,
        )
        self.key = bytes(range(32))
        self.outbox = EncryptedCommandOutbox(
            paths=self.paths,
            key_id="test-key-v1",
            key=self.key,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(
        self,
        *,
        text: str = "OUTBOX-PLAINTEXT-SENTINEL",
        command_id: str = "command-1",
        command_seq: int = 5,
    ) -> PreparedCommandRecord:
        return PreparedCommandRecord(
            schema_version=OUTBOX_SCHEMA_VERSION,
            project_key=self.paths.project_key,
            workflow_id=workflow_id_for_project(self.project),
            kind="message",
            update_id="codex-message-stable-digest",
            command_id=command_id,
            command_seq=command_seq,
            message_id=f"{command_id}:message",
            text=text,
        )

    def test_prepare_round_trip_is_opaque_and_repr_is_redacted(self) -> None:
        record = self.record()

        self.outbox.prepare(record)

        ciphertext = self.outbox.path.read_bytes()
        self.assertEqual(record, self.outbox.load_pending())
        self.assertNotIn(record.text.encode("utf-8"), ciphertext)
        self.assertNotIn(record.command_id.encode("utf-8"), ciphertext)
        self.assertNotIn(b"test-key-v1", ciphertext)
        self.assertNotIn(record.text, repr(record))
        self.assertNotIn(record.command_id, repr(record))
        self.assertNotIn(str(self.outbox.path), repr(self.outbox))
        self.assertEqual([], list(self.outbox.path.parent.glob("*.tmp")))

    def test_same_exact_record_is_idempotent_but_different_record_conflicts(self) -> None:
        original = self.record()
        self.outbox.prepare(original)
        first_ciphertext = self.outbox.path.read_bytes()

        self.outbox.prepare(original)
        self.assertEqual(first_ciphertext, self.outbox.path.read_bytes())

        with self.assertRaises(CommandOutboxConflictError):
            self.outbox.prepare(self.record(text="different pending text"))
        self.assertEqual(original, self.outbox.load_pending())

    def test_resolve_is_a_durable_encrypted_tombstone(self) -> None:
        record = self.record()
        self.outbox.prepare(record)

        self.outbox.resolve(record.command_id)

        self.assertIsNone(self.outbox.load_pending())
        ciphertext = self.outbox.path.read_bytes()
        self.assertNotIn(record.command_id.encode("utf-8"), ciphertext)
        # Exact acknowledgement is idempotent; another ID is not.
        self.outbox.resolve(record.command_id)
        with self.assertRaises(CommandOutboxConflictError):
            self.outbox.resolve("another-command")

    def test_tamper_wrong_key_and_plaintext_all_fail_closed(self) -> None:
        self.outbox.prepare(self.record())
        ciphertext = bytearray(self.outbox.path.read_bytes())
        ciphertext[-1] ^= 0x01
        self.outbox.path.write_bytes(ciphertext)
        with self.assertRaises(CommandOutboxSecurityError):
            self.outbox.load_pending()

        self.outbox.path.unlink()
        self.outbox.prepare(self.record())
        wrong_key = EncryptedCommandOutbox(
            paths=self.paths,
            key_id="test-key-v1",
            key=os.urandom(32),
        )
        with self.assertRaises(CommandOutboxSecurityError):
            wrong_key.load_pending()

        self.outbox.path.write_bytes(b'{"record_type":"prepared","text":"plaintext"}')
        with self.assertRaises(CommandOutboxSecurityError):
            self.outbox.load_pending()

    def test_ciphertext_copied_to_another_project_is_rejected(self) -> None:
        self.outbox.prepare(self.record())
        other_project = self.workspace / "project-b"
        other_project.mkdir()
        other_paths = RuntimePaths.from_project_root(
            other_project,
            workspace_root=self.workspace,
        )
        other = EncryptedCommandOutbox(
            paths=other_paths,
            key_id="test-key-v1",
            key=self.key,
        )
        other.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.outbox.path, other.path)

        with self.assertRaises(CommandOutboxSecurityError):
            other.load_pending()

    def test_path_must_remain_inside_runtime_root(self) -> None:
        with self.assertRaises(CommandOutboxSecurityError):
            EncryptedCommandOutbox(
                paths=self.paths,
                key_id="test-key-v1",
                key=self.key,
                path=self.workspace / "escaped.bin",
            )

    def test_goal_budget_and_exact_objective_round_trip(self) -> None:
        objective = "  exact Goal bytes\n\u7b2c\u4e8c\u884c  "
        record = PreparedCommandRecord(
            schema_version=OUTBOX_SCHEMA_VERSION,
            project_key=self.paths.project_key,
            workflow_id=workflow_id_for_project(self.project),
            kind="start-goal",
            update_id="codex-start-goal-stable-digest",
            command_id="goal-command",
            command_seq=8,
            text=objective,
            budget=Budget(
                max_automatic_turns=7,
                max_tokens=54_321,
                max_elapsed_seconds=987,
                max_failures=2,
                max_rollovers=3,
            ),
        )

        self.outbox.prepare(record)
        restored = self.outbox.load_pending()

        self.assertEqual(record, restored)
        assert restored is not None
        payload = restored.typed_payload()
        self.assertEqual(objective, payload.objective)
        self.assertEqual(54_321, payload.budget.max_tokens)


if __name__ == "__main__":
    unittest.main()
