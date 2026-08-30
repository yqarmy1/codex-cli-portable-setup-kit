from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from continuous_cli import (
    CONTINUOUS_DEVELOPER_INSTRUCTIONS,
    CONTINUOUS_OBJECTIVE_COMPLETE_MARKER,
    RECOVERED_ROLLOVER_DISPATCH_PROMPT,
    RECOVERED_ROLLOVER_PROMPT,
    STARTUP_RESUME_PROMPT,
    ContinuousCodex,
    GuardianFinishCandidate,
    GuardianTarget,
    RolloverJournal,
    RolloverJournalLease,
    REWIND_TOKEN,
    RenderCapabilities,
    RenderLevel,
    TerminalRenderer,
    ToolPresentation,
    TurnOutcome,
    active_state_snapshot,
    assistant_defers_unfinished_work,
    assistant_reports_rollover_status_only,
    assistant_response_allows_terminal_settlement,
    assistant_response_contradicts_guardian_completion,
    assistant_requires_user_input,
    assistant_requests_manual_fresh_thread,
    auto_input_grace_ms,
    build_handoff,
    contextctl_subcommands,
    contextctl_command_binds_task,
    display_width,
    discover_guardian_target,
    finish_guardian_session,
    guardian_runtime_active,
    is_generic_content_block,
    item_may_have_side_effect,
    outcome_has_generic_block,
    outcome_has_policy_boundary,
    outcome_allows_terminal_settlement,
    prepare_guardian_source_for_replacement,
    prepare_guardian_user_objective,
    read_rollover_journal,
    reopen_completed_guardian_session,
    remove_rollover_journal,
    rollover_journal_location,
    rollover_reason,
    should_retry_generic,
    trim_text,
    turn_error_details,
    usage_input_and_window,
    validated_guardian_bundle,
    validate_guardian_lifecycle_receipt,
    validate_rollover_journal,
    validate_sdk_cli_pair,
    write_rollover_journal,
)


PREMATURE_FINISH_RESPONSE = (
    "\u6700\u5f8c\u7684 artifact\uff0f\u654f\u611f\u8cc7\u6599\u908a\u754c\u7a3d\u6838\u5c1a\u672a\u5b8c\u6210\uff0c"
    "\u5c07\u5f9e\u5df2\u9a57\u8b49\u7684 106/106 \u6e2c\u8a66\u72c0\u614b\u81ea\u52d5\u7e8c\u63a5\u3002"
)


def guardian_work_evidence(task_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "baseline_offset": 100,
        "observed_offset": 200,
        "rollout_file_id": f"rollout-{task_id}",
        "call_type": "custom_tool_call",
        "output_type": "custom_tool_call_output",
        "call_id_sha256": "1" * 64,
        "call_record_sha256": "2" * 64,
        "output_record_sha256": "3" * 64,
        "observed_at": "2026-08-13T00:00:00+00:00",
    }


def guardian_lifecycle_receipt(
    task_id: str,
    *,
    project_id: str = "demo",
    kind: str = "completed",
    state_sha256: str = "a" * 64,
    started_state_sha256: str | None = None,
    started_rules_fingerprint_sha256: str = "b" * 64,
    source_task_id: str | None = None,
    source_audited_state_sha256: str | None = None,
    source_audit_rules_fingerprint_sha256: str | None = None,
    source_audit_fingerprint_sha256: str | None = None,
    replacement_task_id: str | None = None,
) -> dict[str, Any]:
    if source_task_id is not None:
        source_audited_state_sha256 = (
            source_audited_state_sha256 or state_sha256
        )
        source_audit_rules_fingerprint_sha256 = (
            source_audit_rules_fingerprint_sha256
            or started_rules_fingerprint_sha256
        )
        source_audit_fingerprint_sha256 = (
            source_audit_fingerprint_sha256 or "c" * 64
        )
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "project_id": project_id,
        "task_id": task_id,
        "kind": kind,
        "state_sha256": state_sha256,
        "started_state_sha256": (
            started_state_sha256
            or source_audited_state_sha256
            or state_sha256
        ),
        "started_rules_fingerprint_sha256": started_rules_fingerprint_sha256,
        "audited_state_sha256": state_sha256,
        "audit_rules_fingerprint_sha256": "d" * 64,
        "audit_fingerprint_sha256": "e" * 64,
        "source_task_id": source_task_id,
        "source_audited_state_sha256": source_audited_state_sha256,
        "source_audit_rules_fingerprint_sha256": (
            source_audit_rules_fingerprint_sha256
        ),
        "source_audit_fingerprint_sha256": source_audit_fingerprint_sha256,
        "replacement_task_id": replacement_task_id,
        "work_evidence": (
            guardian_work_evidence(task_id) if source_task_id is not None else None
        ),
        "replacement_work_evidence": (
            guardian_work_evidence(replacement_task_id)
            if kind == "retired" and replacement_task_id is not None
            else None
        ),
        "recorded_at": "2026-08-13T00:00:00+00:00",
    }
    return receipt


def concrete_rollover_journal(
    source_task_id: str,
    target_task_id: str,
    handoff_sha256: str,
    *,
    project_root: Path = Path("C:/project"),
) -> RolloverJournal:
    return RolloverJournal(
        transaction_id="transaction-1",
        project_id="demo",
        workspace_root=str(project_root),
        project_root=str(project_root),
        session_cwd=str(project_root),
        generation=1,
        source_task_id=source_task_id,
        source_state_sha256="a" * 64,
        source_rules_sha256="b" * 64,
        source_audit_sha256="c" * 64,
        phase="concrete_started",
        target_task_id=target_task_id,
        target_state_sha256="a" * 64,
        target_rules_sha256="b" * 64,
        handoff_sha256=handoff_sha256,
        created_at="2026-08-13T00:00:00+00:00",
        updated_at="2026-08-13T00:00:01+00:00",
    )


def install_memory_rollover_transitions(client: ContinuousCodex) -> None:
    def begin(dispatched_handoff: str) -> None:
        journal = client._active_rollover_journal
        candidate = client._pending_guardian_finish_candidate
        if (
            journal.phase != "target_created"
            or candidate.old_task_id != journal.source_task_id
            or candidate.target_task_id != journal.target_task_id
            or candidate.handoff_sha256
            != hashlib.sha256(dispatched_handoff.encode("utf-8")).hexdigest()
        ):
            raise AssertionError("test rollover binding is inconsistent")
        client._active_rollover_journal = replace(
            journal,
            phase="dispatch_started",
        )

    def record_concrete() -> RolloverJournal:
        journal = client._active_rollover_journal
        if journal.phase != "dispatch_started":
            raise AssertionError("test rollover dispatch did not start")
        client._active_rollover_journal = replace(
            journal,
            phase="concrete_started",
        )
        return client._active_rollover_journal

    client._begin_concrete_rollover_dispatch = begin
    client._record_concrete_rollover_dispatch = record_concrete
    client._finish_active_rollover_journal = lambda: None


def usage(input_tokens: int, window: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(
        last=SimpleNamespace(input_tokens=input_tokens),
        model_context_window=window,
    )


class GuardianDiscoveryTests(unittest.TestCase):
    def test_exact_target_runtime_file_is_completion_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contextctl = (
                root
                / ".agents"
                / "skills"
                / "context-guardian"
                / "scripts"
                / "contextctl.py"
            )
            contextctl.parent.mkdir(parents=True)
            contextctl.write_text("# fixture\n", encoding="utf-8")
            registry = root / ".context" / "registry.json"
            registry.parent.mkdir()
            registry.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "demo",
                                "path": "demo",
                                "state": ".context/state.json",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            project = root / "demo"
            runtime = project / ".context" / "runtime" / "target-task.json"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                json.dumps(
                    {"project_id": "demo", "task_id": "target-task"}
                ),
                encoding="utf-8",
            )

            self.assertTrue(guardian_runtime_active(project, "target-task"))
            self.assertTrue(guardian_runtime_active(project, "other-task"))
            runtime.unlink()
            self.assertTrue(guardian_runtime_active(project, "target-task"))
            receipts = runtime.parent / "receipts"
            receipts.mkdir()
            receipts.joinpath("target-task.json").write_text(
                json.dumps(guardian_lifecycle_receipt("target-task")),
                encoding="utf-8",
            )
            self.assertFalse(guardian_runtime_active(project, "target-task"))

            receipt_path = receipts / "target-task.json"
            malformed = json.loads(receipt_path.read_text(encoding="utf-8"))
            malformed.pop("state_sha256")
            receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "state_sha256"):
                guardian_runtime_active(project, "target-task")

    def test_lifecycle_receipt_rejects_malformed_source_lineage(self) -> None:
        base = guardian_lifecycle_receipt(
            "target-task",
            started_state_sha256="b" * 64,
            started_rules_fingerprint_sha256="c" * 64,
            source_task_id="source-task",
            source_audited_state_sha256="b" * 64,
            source_audit_rules_fingerprint_sha256="c" * 64,
            source_audit_fingerprint_sha256="d" * 64,
        )
        validate_guardian_lifecycle_receipt(base, "demo", "target-task")
        malformed = dict(base)
        malformed["source_audit_fingerprint_sha256"] = None
        with self.assertRaisesRegex(RuntimeError, "source_audit_fingerprint"):
            validate_guardian_lifecycle_receipt(
                malformed,
                "demo",
                "target-task",
            )
        mismatched = dict(base)
        mismatched["started_rules_fingerprint_sha256"] = "e" * 64
        with self.assertRaisesRegex(RuntimeError, "start rules"):
            validate_guardian_lifecycle_receipt(
                mismatched,
                "demo",
                "target-task",
            )

        legacy = dict(base, schema_version=1)
        with self.assertRaisesRegex(RuntimeError, "schema"):
            validate_guardian_lifecycle_receipt(legacy, "demo", "target-task")

        missing_work = dict(base, work_evidence=None)
        with self.assertRaisesRegex(RuntimeError, "work evidence"):
            validate_guardian_lifecycle_receipt(
                missing_work,
                "demo",
                "target-task",
            )
        mismatched_protocol = json.loads(json.dumps(base))
        mismatched_protocol["work_evidence"]["output_type"] = (
            "function_call_output"
        )
        with self.assertRaisesRegex(RuntimeError, "protocol"):
            validate_guardian_lifecycle_receipt(
                mismatched_protocol,
                "demo",
                "target-task",
            )

        malformed_replacement = guardian_lifecycle_receipt(
            "source-task",
            kind="retired",
            replacement_task_id="../target",
        )
        with self.assertRaisesRegex(RuntimeError, "replacement"):
            validate_guardian_lifecycle_receipt(
                malformed_replacement,
                "demo",
                "source-task",
            )

    def test_reopen_completed_receipt_preserves_original_start_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime_dir = root / ".context" / "runtime"
            receipt_dir = runtime_dir / "receipts"
            receipt_dir.mkdir(parents=True)
            session_path = runtime_dir / "target-task.json"
            receipt_path = receipt_dir / "target-task.json"
            receipt = guardian_lifecycle_receipt(
                "target-task",
                started_state_sha256="f" * 64,
                started_rules_fingerprint_sha256="c" * 64,
            )
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            target = GuardianTarget(root, "demo", root / "contextctl.py")

            def preflight(*_args, **_kwargs) -> str:
                session_path.write_text(
                    json.dumps(
                        {
                            "project_id": "demo",
                            "task_id": "target-task",
                            "started_state_sha256": receipt[
                                "started_state_sha256"
                            ],
                            "started_rules_fingerprint_sha256": receipt[
                                "started_rules_fingerprint_sha256"
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                receipt_path.unlink()
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            with (
                patch(
                    "continuous_cli.guardian_receipt",
                    return_value=(target, receipt),
                ),
                patch(
                    "continuous_cli.validated_guardian_bundle",
                    side_effect=preflight,
                ),
                patch(
                    "continuous_cli.guardian_runtime_location",
                    return_value=(target, session_path),
                ),
            ):
                bundle = reopen_completed_guardian_session(
                    root,
                    "target-task",
                    receipt,
                )

            self.assertIn("BOUNDED CONTEXT BUNDLE", bundle)
            self.assertTrue(session_path.is_file())
            self.assertFalse(receipt_path.exists())

    def test_unregistered_nested_git_repo_does_not_inherit_control_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            contextctl = (
                root
                / ".agents"
                / "skills"
                / "context-guardian"
                / "scripts"
                / "contextctl.py"
            )
            contextctl.parent.mkdir(parents=True)
            contextctl.write_text("# fixture\n", encoding="utf-8")
            registry_path = root / ".context" / "registry.json"
            registry_path.parent.mkdir()
            registry_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {"id": "control", "path": "."},
                            {"id": "registered", "path": "registered"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            unmanaged = root / "unmanaged"
            (unmanaged / ".git").mkdir(parents=True)
            (unmanaged / "src").mkdir()
            registered = root / "registered"
            (registered / ".git").mkdir(parents=True)
            (registered / "src").mkdir()

            self.assertIsNone(discover_guardian_target(unmanaged / "src"))
            target = discover_guardian_target(registered / "src")
            self.assertIsNotNone(target)
            self.assertEqual(target.project_id, "registered")

    def test_new_guardian_bundle_is_bound_to_new_task_without_old_sentinel(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="control",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        good = SimpleNamespace(
            returncode=0,
            stdout="# BOUNDED CONTEXT BUNDLE\n\n- Task ID: `new-task`\n",
            stderr="",
        )
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch("continuous_cli.subprocess.run", return_value=good) as run,
        ):
            bundle = validated_guardian_bundle(
                Path("C:/project"),
                "new-task",
                reject_task_id="old-task",
            )

        self.assertIn("new-task", bundle or "")
        self.assertNotIn("old-task", bundle or "")
        self.assertNotIn("CONTEXT_ROLLOVER_REQUIRED", bundle or "")
        command = run.call_args.args[0]
        self.assertIn("new-task", command)
        self.assertIn("--resume", command)
        self.assertEqual(
            command[command.index("--replaces-task") + 1],
            "old-task",
        )

        stale_identity = SimpleNamespace(
            returncode=0,
            stdout=(
                "# BOUNDED CONTEXT BUNDLE\n\n"
                "Prior runtime: old-task\n"
                "- Task ID: `new-task`\n"
            ),
            stderr="",
        )
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch("continuous_cli.subprocess.run", return_value=stale_identity),
        ):
            sanitized = validated_guardian_bundle(
                Path("C:/project"),
                "new-task",
                reject_task_id="old-task",
            )
        self.assertIn("new-task", sanitized or "")
        self.assertNotIn("old-task", sanitized or "")
        self.assertIn("previous task id omitted", sanitized or "")

        stale_sentinel = SimpleNamespace(
            returncode=0,
            stdout=(
                "CONTEXT_ROLLOVER_REQUIRED project=control task=old-task\n\n"
                "- Task ID: `new-task`\n"
            ),
            stderr="",
        )
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch("continuous_cli.subprocess.run", return_value=stale_sentinel),
            self.assertRaisesRegex(RuntimeError, "rollover sentinel"),
        ):
            validated_guardian_bundle(
                Path("C:/project"),
                "new-task",
                reject_task_id="old-task",
            )


class RolloverJournalTests(unittest.TestCase):
    @staticmethod
    def fixture(raw: str) -> tuple[Path, Path]:
        root = Path(raw)
        contextctl = (
            root
            / ".agents"
            / "skills"
            / "context-guardian"
            / "scripts"
            / "contextctl.py"
        )
        contextctl.parent.mkdir(parents=True)
        contextctl.write_text("# fixture\n", encoding="utf-8")
        registry = root / ".context" / "registry.json"
        registry.parent.mkdir()
        registry.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "demo",
                            "path": "demo",
                            "state": ".context/state.json",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        project = root / "demo"
        (project / ".context" / "runtime").mkdir(parents=True)
        return root, project

    @staticmethod
    def prepared(root: Path, project: Path) -> RolloverJournal:
        return RolloverJournal(
            transaction_id="11111111-2222-4333-8444-555555555555",
            project_id="demo",
            workspace_root=os.path.normcase(str(root.resolve())),
            project_root=os.path.normcase(str(project.resolve())),
            session_cwd=os.path.normcase(str(project.resolve())),
            generation=1,
            source_task_id="source-task",
            source_state_sha256="a" * 64,
            source_rules_sha256="b" * 64,
            source_audit_sha256="c" * 64,
            phase="prepared",
            created_at="2026-08-13T00:00:00+00:00",
            updated_at="2026-08-13T00:00:00+00:00",
        )

    def test_schema_is_sha_only_and_requires_canonical_transaction_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            journal = self.prepared(root, project)
            write_rollover_journal(project, journal)
            _target, path = rollover_journal_location(project) or (None, None)
            self.assertIsNotNone(path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("handoff", persisted)
            self.assertNotIn("latest_user", persisted)
            self.assertNotIn("latest_assistant", persisted)
            self.assertNotIn("raw_prompt", persisted)
            self.assertEqual(read_rollover_journal(project), journal)

            malformed = journal.as_dict()
            malformed["transaction_id"] = "{11111111-2222-4333-8444-555555555555}"
            with self.assertRaisesRegex(RuntimeError, "not canonical"):
                validate_rollover_journal(
                    malformed,
                    expected_project_id="demo",
                    expected_workspace_root=root,
                    expected_project_root=project,
                )
            injected = journal.as_dict()
            injected["raw_prompt"] = "must never be accepted"
            with self.assertRaisesRegex(RuntimeError, "fields"):
                validate_rollover_journal(
                    injected,
                    expected_project_id="demo",
                    expected_workspace_root=root,
                    expected_project_root=project,
                )

    def test_phase_transition_is_monotonic_immutable_and_cleanup_is_gated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            target = replace(
                prepared,
                phase="target_created",
                target_task_id="target-task",
                target_state_sha256="a" * 64,
                target_rules_sha256="b" * 64,
                handoff_sha256="d" * 64,
                updated_at="2026-08-13T00:00:01+00:00",
            )
            write_rollover_journal(project, target)
            with self.assertRaisesRegex(RuntimeError, "before final publication"):
                remove_rollover_journal(project, target.transaction_id)
            dispatch = replace(
                target,
                phase="dispatch_started",
                updated_at="2026-08-13T00:00:02+00:00",
            )
            write_rollover_journal(project, dispatch)
            with self.assertRaisesRegex(RuntimeError, "immutable source"):
                write_rollover_journal(
                    project,
                    replace(
                        dispatch,
                        phase="concrete_started",
                        source_state_sha256="e" * 64,
                        updated_at="2026-08-13T00:00:03+00:00",
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "immutable target"):
                write_rollover_journal(
                    project,
                    replace(
                        dispatch,
                        phase="concrete_started",
                        handoff_sha256="f" * 64,
                        updated_at="2026-08-13T00:00:03+00:00",
                    ),
                )
            concrete = replace(
                dispatch,
                phase="concrete_started",
                updated_at="2026-08-13T00:00:03+00:00",
            )
            write_rollover_journal(project, concrete)
            retired = replace(
                concrete,
                phase="source_retired",
                updated_at="2026-08-13T00:00:04+00:00",
            )
            write_rollover_journal(project, retired)
            with self.assertRaisesRegex(RuntimeError, "before final publication"):
                remove_rollover_journal(project, retired.transaction_id)
            completion = replace(
                retired,
                phase="completion_ready",
                final_response_sha256="e" * 64,
                updated_at="2026-08-13T00:00:05+00:00",
            )
            write_rollover_journal(project, completion)
            remove_rollover_journal(project, completion.transaction_id)
            self.assertIsNone(read_rollover_journal(project))

    def test_second_controller_cannot_acquire_project_journal_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _root, project = self.fixture(raw)
            _target, journal_path = rollover_journal_location(project) or (None, None)
            self.assertIsNotNone(journal_path)
            first = RolloverJournalLease(journal_path.with_suffix(".lock"))
            try:
                with self.assertRaisesRegex(RuntimeError, "another codex-continuous"):
                    RolloverJournalLease(journal_path.with_suffix(".lock"))
            finally:
                first.close()

    def test_registered_project_subdirectories_share_one_journal_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _root, project = self.fixture(raw)
            subdir = project / "src" / "nested"
            subdir.mkdir(parents=True)
            _target, from_root = rollover_journal_location(project) or (None, None)
            _target, from_subdir = rollover_journal_location(subdir) or (None, None)
            self.assertEqual(from_root, from_subdir)
            journal = self.prepared(_root, project)
            write_rollover_journal(project, journal)
            self.assertEqual(read_rollover_journal(subdir), journal)
            first = RolloverJournalLease(from_root.with_suffix(".lock"))
            try:
                with self.assertRaisesRegex(RuntimeError, "another codex-continuous"):
                    RolloverJournalLease(from_subdir.with_suffix(".lock"))
            finally:
                first.close()

    def test_source_retired_worker_rotates_atomically_into_next_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            target_runtime = self.write_target_runtime(project)
            target_payload = json.loads(target_runtime.read_text(encoding="utf-8"))
            target_payload.update(
                {
                    "audited_state_sha256": "a" * 64,
                    "audit_rules_fingerprint_sha256": "b" * 64,
                    "audit_fingerprint_sha256": "e" * 64,
                }
            )
            target_runtime.write_text(json.dumps(target_payload), encoding="utf-8")

            prepared = self.prepared(root, project)
            target = self.target_journal(prepared)
            dispatch = replace(
                target,
                phase="dispatch_started",
                updated_at="2026-08-13T00:00:02+00:00",
            )
            concrete = replace(
                dispatch,
                phase="concrete_started",
                updated_at="2026-08-13T00:00:03+00:00",
            )
            retired = replace(
                concrete,
                phase="source_retired",
                updated_at="2026-08-13T00:00:04+00:00",
            )
            for value in (prepared, target, dispatch, concrete, retired):
                write_rollover_journal(project, value)
            self.retire_source(project)

            client = ContinuousCodex.__new__(ContinuousCodex)
            client.project_root = project
            client._rollover_journal_lease = None
            client._active_rollover_journal = retired
            try:
                successor = client._prepare_rollover_journal("target-task")
                self.assertEqual(successor.phase, "prepared")
                self.assertEqual(successor.generation, 2)
                self.assertEqual(successor.source_task_id, "target-task")
                self.assertIsNone(successor.target_task_id)
                self.assertNotEqual(successor.transaction_id, retired.transaction_id)
                self.assertEqual(
                    read_rollover_journal(
                        project,
                        lease=client._rollover_journal_lease,
                    ),
                    successor,
                )
            finally:
                self.release_controller(client)

    @staticmethod
    def persist_source_retired(
        project: Path,
        target: RolloverJournal,
    ) -> RolloverJournal:
        write_rollover_journal(project, target)
        dispatch = replace(
            target,
            phase="dispatch_started",
            updated_at="2026-08-13T00:00:02+00:00",
        )
        write_rollover_journal(project, dispatch)
        concrete = replace(
            dispatch,
            phase="concrete_started",
            updated_at="2026-08-13T00:00:03+00:00",
        )
        write_rollover_journal(project, concrete)
        retired = replace(
            concrete,
            phase="source_retired",
            updated_at="2026-08-13T00:00:04+00:00",
        )
        write_rollover_journal(project, retired)
        return retired

    @staticmethod
    def write_source_runtime(project: Path) -> Path:
        path = project / ".context" / "runtime" / "source-task.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "task_id": "source-task",
                    "started_state_sha256": "0" * 64,
                    "started_rules_fingerprint_sha256": "1" * 64,
                    "audited_state_sha256": "a" * 64,
                    "audit_rules_fingerprint_sha256": "b" * 64,
                    "audit_fingerprint_sha256": "c" * 64,
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def write_target_runtime(project: Path) -> Path:
        path = project / ".context" / "runtime" / "target-task.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "task_id": "target-task",
                    "started_state_sha256": "a" * 64,
                    "started_rules_fingerprint_sha256": "b" * 64,
                    "source_task_id": "source-task",
                    "source_audited_state_sha256": "a" * 64,
                    "source_audit_rules_fingerprint_sha256": "b" * 64,
                    "source_audit_fingerprint_sha256": "c" * 64,
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def retire_source(project: Path) -> bool:
        runtime = project / ".context" / "runtime" / "source-task.json"
        runtime.unlink(missing_ok=True)
        receipts = runtime.parent / "receipts"
        receipts.mkdir(exist_ok=True)
        receipts.joinpath("source-task.json").write_text(
            json.dumps(
                guardian_lifecycle_receipt(
                    "source-task",
                    kind="retired",
                    started_state_sha256="0" * 64,
                    started_rules_fingerprint_sha256="1" * 64,
                    replacement_task_id="target-task",
                )
            ),
            encoding="utf-8",
        )
        return True

    @staticmethod
    def complete_target(project: Path) -> None:
        runtime = project / ".context" / "runtime" / "target-task.json"
        runtime.unlink(missing_ok=True)
        receipts = runtime.parent / "receipts"
        receipts.mkdir(exist_ok=True)
        receipts.joinpath("target-task.json").write_text(
            json.dumps(
                guardian_lifecycle_receipt(
                    "target-task",
                    source_task_id="source-task",
                    source_audited_state_sha256="a" * 64,
                    source_audit_rules_fingerprint_sha256="b" * 64,
                    source_audit_fingerprint_sha256="c" * 64,
                )
            ),
            encoding="utf-8",
        )

    @staticmethod
    def target_journal(prepared: RolloverJournal) -> RolloverJournal:
        return replace(
            prepared,
            phase="target_created",
            target_task_id="target-task",
            target_state_sha256="a" * 64,
            target_rules_sha256="b" * 64,
            handoff_sha256="d" * 64,
            updated_at="2026-08-13T00:00:01+00:00",
        )

    @staticmethod
    def restarted_controller(
        project: Path,
        journal: RolloverJournal,
        *,
        turns: list[Any] | None = None,
    ) -> tuple[ContinuousCodex, dict[str, Any]]:
        calls: dict[str, Any] = {
            "start": 0,
            "resume": [],
            "read": 0,
            "list": 0,
            "list_params": [],
        }
        thread = SimpleNamespace(
            id="target-task",
            cwd=str(project),
            thread_source=journal.thread_source,
            turns=list(turns or []),
        )
        calls["thread"] = thread

        def forbidden_start(_params: dict[str, Any]) -> Any:
            calls["start"] += 1
            raise AssertionError("restart created a duplicate target")

        def resume(task_id: str, _params: dict[str, Any]) -> Any:
            calls["resume"].append(task_id)
            return SimpleNamespace(thread=SimpleNamespace(id=task_id))

        def read(task_id: str, *, include_turns: bool = False) -> Any:
            self_value = include_turns
            del self_value
            calls["read"] += 1
            if task_id != "target-task":
                raise AssertionError("restart read the wrong target")
            return SimpleNamespace(thread=thread)

        def listed(_params: dict[str, Any]) -> Any:
            calls["list"] += 1
            calls["list_params"].append(_params)
            return SimpleNamespace(data=[thread], next_cursor=None)

        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = project
        client.client = SimpleNamespace(
            thread_start=forbidden_start,
            thread_resume=resume,
            thread_read=read,
            thread_list=listed,
        )
        client._Thread = lambda _client, task_id: SimpleNamespace(id=task_id)
        client._adopt_runtime_settings = lambda _response: None
        client.thread = None
        client.pending_handoff = None
        client._pending_guardian_finish_candidate = None
        client._pending_guardian_finishes = []
        client._active_rollover_journal = None
        client._rollover_journal_lease = None
        client.active_permission_profile = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        return client, calls

    @staticmethod
    def release_controller(client: ContinuousCodex) -> None:
        lease = getattr(client, "_rollover_journal_lease", None)
        if lease is not None:
            lease.close()
            client._rollover_journal_lease = None

    def test_restart_after_target_create_attaches_exact_thread_and_never_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)

            def preflight(*_args: Any, **_kwargs: Any) -> str:
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.validated_guardian_bundle",
                    side_effect=preflight,
                ):
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(calls["start"], 0)
                self.assertEqual(
                    calls["list_params"][0]["sourceKinds"],
                    ["appServer", "vscode"],
                )
                self.assertIs(calls["list_params"][0]["useStateDbOnly"], False)
                self.assertEqual(client.thread.id, "target-task")
                self.assertIsNotNone(client.pending_handoff)
                client._begin_concrete_rollover_dispatch(str(client.pending_handoff))
                with patch(
                    "continuous_cli.finish_guardian_session",
                    side_effect=lambda *_args, **_kwargs: self.retire_source(project),
                ):
                    client._complete_handoff_dispatch(
                        TurnOutcome("real target work", None, tool_activity=True),
                        dispatched_handoff=str(client.pending_handoff),
                    )
                self.assertFalse(
                    (project / ".context" / "runtime" / "source-task.json").exists()
                )
                retained = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(retained.phase, "source_retired")
            finally:
                self.release_controller(client)

    def test_restart_after_prepared_write_creates_exactly_one_transaction_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)
            started_params: list[dict[str, Any]] = []
            client.client.thread_list = lambda _params: SimpleNamespace(
                data=[],
                next_cursor=None,
            )

            def start(params: dict[str, Any]) -> Any:
                calls["start"] += 1
                started_params.append(params)
                return SimpleNamespace(thread=SimpleNamespace(id="target-task"))

            client.client.thread_start = start

            def preflight(*_args: Any, **_kwargs: Any) -> str:
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.validated_guardian_bundle",
                    side_effect=preflight,
                ):
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(calls["start"], 1)
                self.assertEqual(
                    started_params[0]["threadSource"],
                    prepared.thread_source,
                )
                self.assertEqual(client.thread.id, "target-task")
                recovered = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(recovered.phase, "target_created")
                self.assertEqual(recovered.target_task_id, "target-task")
            finally:
                self.release_controller(client)

    def test_prepared_restart_fails_closed_on_duplicate_transaction_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)
            duplicate = SimpleNamespace(
                id="other-target",
                cwd=str(project),
                thread_source=prepared.thread_source,
                turns=[],
            )
            original = SimpleNamespace(
                id="target-task",
                cwd=str(project),
                thread_source=prepared.thread_source,
                turns=[],
            )
            client.client.thread_list = lambda _params: SimpleNamespace(
                data=[original, duplicate],
                next_cursor=None,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "multiple target tasks"):
                    client._reconcile_rollover_journal()
                self.assertEqual(calls["start"], 0)
            finally:
                self.release_controller(client)

    def test_prepared_restart_scans_every_page_without_creating_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)
            pages: list[str | None] = []
            target = SimpleNamespace(
                id="target-task",
                cwd=str(project),
                thread_source=prepared.thread_source,
                turns=[],
            )

            def listed(params: dict[str, Any]) -> Any:
                cursor = params.get("cursor")
                pages.append(cursor)
                page = 0 if cursor is None else int(str(cursor).removeprefix("page-"))
                if page == 24:
                    return SimpleNamespace(data=[target], next_cursor=None)
                return SimpleNamespace(data=[], next_cursor=f"page-{page + 1}")

            client.client.thread_list = listed

            def preflight(*_args: Any, **_kwargs: Any) -> str:
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.validated_guardian_bundle",
                    side_effect=preflight,
                ):
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(calls["start"], 0)
                self.assertGreater(len(pages), 20)
                self.assertEqual(client.thread.id, "target-task")
            finally:
                self.release_controller(client)

    def test_prepared_restart_rejects_a_repeated_list_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)
            client.client.thread_list = lambda _params: SimpleNamespace(
                data=[],
                next_cursor="same-cursor",
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "repeated thread/list cursor"):
                    client._reconcile_rollover_journal()
                self.assertEqual(calls["start"], 0)
            finally:
                self.release_controller(client)

    def test_zero_turn_target_is_recovered_from_exact_persisted_session_meta(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            prepared = self.prepared(root, project)
            codex_home = Path(raw) / "codex-home"
            rollout = codex_home / "sessions" / "2026" / "08" / "13" / (
                "rollout-target-task.jsonl"
            )
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "target-task",
                            "session_id": "target-task",
                            "cwd": str(project),
                            "thread_source": prepared.thread_source,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            client, calls = self.restarted_controller(project, prepared)
            client.client.thread_list = lambda _params: SimpleNamespace(
                data=[],
                next_cursor=None,
            )
            try:
                with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                    self.assertEqual(
                        client._list_exact_rollover_target(prepared),
                        "target-task",
                    )
                self.assertEqual(calls["start"], 0)
            finally:
                self.release_controller(client)

    def test_thread_identity_unwraps_real_sdk_absolute_path_buf(self) -> None:
        try:
            from openai_codex.generated.v2_all import AbsolutePathBuf
        except ImportError:
            self.skipTest("openai-codex test runtime is unavailable")
        value = AbsolutePathBuf(root="C:/workspace/project")
        thread = SimpleNamespace(cwd=value)
        self.assertEqual(
            ContinuousCodex._thread_cwd_value(thread),
            "C:/workspace/project",
        )

    def test_restart_after_concrete_dispatch_retires_source_without_replaying_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            write_rollover_journal(project, target)
            concrete_turn = {
                "status": "completed",
                "itemsView": "full",
                "items": [
                    {
                        "type": "commandExecution",
                        "id": "tool-1",
                        "status": "completed",
                    },
                    {"type": "agentMessage", "text": "real target work"},
                ]
            }
            client, calls = self.restarted_controller(
                project,
                target,
                turns=[concrete_turn],
            )
            try:
                with patch(
                    "continuous_cli.finish_guardian_session",
                    side_effect=lambda *_args, **_kwargs: self.retire_source(project),
                ) as finish:
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(calls["start"], 0)
                self.assertEqual(calls["resume"], ["target-task"])
                self.assertIsNone(client.pending_handoff)
                finish.assert_called_once()
                self.assertFalse(
                    (project / ".context" / "runtime" / "source-task.json").exists()
                )
                retained = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(retained.phase, "source_retired")
            finally:
                self.release_controller(client)

    def test_dispatch_started_restart_interrupts_orphan_turn_before_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = replace(
                self.target_journal(self.prepared(root, project)),
                phase="dispatch_started",
                updated_at="2026-08-13T00:00:02+00:00",
            )
            write_rollover_journal(project, self.target_journal(self.prepared(root, project)))
            write_rollover_journal(project, target)
            in_progress = {
                "id": "turn-running",
                "status": "inProgress",
                "itemsView": "full",
                "items": [
                    {"type": "commandExecution", "status": "inProgress"},
                ],
            }
            client, calls = self.restarted_controller(
                project,
                target,
                turns=[in_progress],
            )
            interrupted: list[tuple[str, str]] = []

            def interrupt(thread_id: str, turn_id: str) -> None:
                interrupted.append((thread_id, turn_id))
                calls["thread"].turns[0]["status"] = "interrupted"

            client.client.turn_interrupt = interrupt
            try:
                self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(interrupted, [("target-task", "turn-running")])
                self.assertEqual(calls["start"], 0)
                self.assertIsNotNone(client.pending_handoff)
                durable = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(durable.phase, "dispatch_started")
            finally:
                self.release_controller(client)

    def test_restart_after_target_completed_retires_source_without_new_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            write_rollover_journal(project, target)
            self.complete_target(project)
            final_turn = {
                "status": "completed",
                "itemsView": "full",
                "items": [
                    {"type": "agentMessage", "text": "objective truly complete"},
                ],
            }
            client, calls = self.restarted_controller(
                project,
                target,
                turns=[final_turn],
            )
            try:
                with patch(
                    "continuous_cli.finish_guardian_session",
                    side_effect=lambda *_args, **_kwargs: self.retire_source(project),
                ) as finish:
                    self.assertEqual(client._reconcile_rollover_journal(), "complete")
                self.assertEqual(calls["start"], 0)
                self.assertEqual(calls["resume"], [])
                finish.assert_called_once()
                self.assertFalse(
                    (project / ".context" / "runtime" / "source-task.json").exists()
                )
                retained = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(retained.phase, "completion_ready")
                self.assertEqual(
                    retained.final_response_sha256,
                    hashlib.sha256(b"objective truly complete").hexdigest(),
                )
            finally:
                self.release_controller(client)

    def test_restart_reopens_completed_receipt_with_explicit_unfinished_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            retired = self.persist_source_retired(project, target)
            self.retire_source(project)
            self.complete_target(project)
            client, calls = self.restarted_controller(
                project,
                retired,
                turns=[
                    {
                        "status": "completed",
                        "itemsView": "full",
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": PREMATURE_FINISH_RESPONSE,
                            }
                        ],
                    }
                ],
            )
            receipt_path = (
                project
                / ".context"
                / "runtime"
                / "receipts"
                / "target-task.json"
            )

            def reopen(
                project_root: Path,
                task_id: str,
                receipt: dict[str, Any],
            ) -> str:
                self.assertEqual(project_root, project)
                self.assertEqual(task_id, "target-task")
                self.assertEqual(receipt["source_task_id"], "source-task")
                receipt_path.unlink()
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.reopen_completed_guardian_session",
                    side_effect=reopen,
                ) as reopened:
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                reopened.assert_called_once()
                self.assertEqual(calls["start"], 0)
                self.assertEqual(calls["resume"], ["target-task"])
                self.assertTrue(
                    (project / ".context" / "runtime" / "target-task.json").is_file()
                )
                self.assertFalse(receipt_path.exists())
                self.assertFalse(
                    hasattr(client, "_reconciled_terminal_outcome")
                )
                self.assertEqual(
                    read_rollover_journal(
                        project,
                        lease=client._rollover_journal_lease,
                    ).phase,
                    "source_retired",
                )
            finally:
                self.release_controller(client)

    def test_restart_reopens_completed_receipt_with_plaintext_user_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            retired = self.persist_source_retired(project, target)
            self.retire_source(project)
            self.complete_target(project)
            marker = (
                "[CONTINUOUS_USER_INPUT_REQUIRED] "
                "Which deployment region should I use?"
            )
            client, calls = self.restarted_controller(
                project,
                retired,
                turns=[
                    {
                        "status": "completed",
                        "itemsView": "full",
                        "items": [{"type": "agentMessage", "text": marker}],
                    }
                ],
            )
            receipt_path = (
                project
                / ".context"
                / "runtime"
                / "receipts"
                / "target-task.json"
            )

            def reopen(
                project_root: Path,
                task_id: str,
                receipt: dict[str, Any],
            ) -> str:
                self.assertEqual(project_root, project)
                self.assertEqual(task_id, "target-task")
                self.assertEqual(receipt["source_task_id"], "source-task")
                receipt_path.unlink()
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.reopen_completed_guardian_session",
                    side_effect=reopen,
                ) as reopened:
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                reopened.assert_called_once()
                self.assertEqual(calls["start"], 0)
                self.assertEqual(calls["resume"], ["target-task"])
                self.assertTrue(
                    (project / ".context" / "runtime" / "target-task.json").is_file()
                )
                self.assertFalse(receipt_path.exists())
                self.assertFalse(
                    hasattr(client, "_reconciled_terminal_outcome")
                )
                self.assertFalse(
                    hasattr(client, "_reconciled_user_blocker_outcome")
                )
                self.assertIsNone(client.pending_handoff)
                self.assertEqual(
                    read_rollover_journal(
                        project,
                        lease=client._rollover_journal_lease,
                    ).phase,
                    "source_retired",
                )
            finally:
                self.release_controller(client)

    def test_completed_target_reopens_latest_nonterminal_status_instead_of_progress(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            write_rollover_journal(project, target)
            self.complete_target(project)
            turns = [
                {
                    "status": "completed",
                    "itemsView": "full",
                    "items": [{"type": "agentMessage", "text": "earlier progress"}],
                },
                {
                    "status": "completed",
                    "itemsView": "full",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "Automatic rollover is continuing; next action then handle tests.",
                        }
                    ],
                },
            ]
            client, calls = self.restarted_controller(project, target, turns=turns)
            receipt_path = (
                project / ".context" / "runtime" / "receipts" / "target-task.json"
            )

            def reopen(*_args: Any, **_kwargs: Any) -> str:
                receipt_path.unlink()
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with patch(
                    "continuous_cli.reopen_completed_guardian_session",
                    side_effect=reopen,
                ) as reopened:
                    self.assertEqual(client._reconcile_rollover_journal(), "active")
                reopened.assert_called_once()
                self.assertEqual(calls["resume"], ["target-task"])
                self.assertIsNotNone(client.pending_handoff)
                self.assertFalse(receipt_path.exists())
                current = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(current.phase, "target_created")
            finally:
                self.release_controller(client)

    def test_restart_final_uses_final_answer_phase_and_never_commentary(self) -> None:
        thread = SimpleNamespace(
            turns=[
                {
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "internal rollover progress must stay hidden",
                        },
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "genuine final result",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(
            ContinuousCodex._thread_final_response(thread),
            "genuine final result",
        )

    def test_marker_only_completion_finalizes_journal_without_empty_output(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        client._mark_active_target_completion = lambda value: events.append(
            "mark:" + value
        )
        client._clear_published_target_completion = lambda: events.append("clear")
        client._publish_automatic_terminal_response(
            TurnOutcome(CONTINUOUS_OBJECTIVE_COMPLETE_MARKER, None)
        )
        self.assertEqual(
            events,
            ["mark:" + CONTINUOUS_OBJECTIVE_COMPLETE_MARKER, "clear"],
        )

    def test_source_retired_restart_does_not_pause_on_plaintext_user_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            target = self.target_journal(self.prepared(root, project))
            retired = self.persist_source_retired(project, target)
            self.retire_source(project)
            question = "[CONTINUOUS_USER_INPUT_REQUIRED] Which deployment region should I use?"
            turns = [
                {
                    "status": "completed",
                    "itemsView": "full",
                    "items": [{"type": "agentMessage", "text": question}],
                }
            ]
            client, calls = self.restarted_controller(project, retired, turns=turns)
            try:
                self.assertEqual(client._reconcile_rollover_journal(), "active")
                self.assertEqual(calls["start"], 0)
                self.assertEqual(calls["resume"], ["target-task"])
                self.assertIsNone(client.pending_handoff)
                self.assertFalse(
                    hasattr(client, "_reconciled_user_blocker_outcome")
                )
                self.assertEqual(
                    read_rollover_journal(
                        project,
                        lease=client._rollover_journal_lease,
                    ).phase,
                    "source_retired",
                )
            finally:
                self.release_controller(client)

    def test_concrete_work_requires_completed_full_turn_tool_and_real_response(self) -> None:
        normal = {"type": "agentMessage", "text": "inspected and fixed the issue"}
        base = {
            "status": "completed",
            "itemsView": "full",
            "items": [
                {"type": "commandExecution", "status": "completed"},
                normal,
            ],
        }
        self.assertTrue(
            ContinuousCodex._thread_has_concrete_work(
                SimpleNamespace(turns=[base])
            )
        )
        for mutation in (
            {"status": "failed"},
            {"itemsView": "summary"},
            {
                "items": [
                    {"type": "commandExecution", "status": "failed"},
                    normal,
                ]
            },
            {
                "items": [
                    {"type": "webSearch"},
                    normal,
                ]
            },
            {
                "items": [
                    {"type": "commandExecution", "status": "completed"},
                    {
                        "type": "agentMessage",
                        "text": "Automatic rollover is continuing; next action then handle tests.",
                    },
                ]
            },
            {
                "items": [
                    {"type": "commandExecution", "status": "completed"},
                    {"type": "agentMessage", "text": PREMATURE_FINISH_RESPONSE},
                ]
            },
        ):
            candidate = dict(base)
            candidate.update(mutation)
            self.assertFalse(
                ContinuousCodex._thread_has_concrete_work(
                    SimpleNamespace(turns=[candidate])
                )
            )

    def test_dispatch_intent_is_durable_before_exact_hash_bound_prompt_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            self.write_target_runtime(project)
            handoff = "exact recovered bounded handoff"
            target = replace(
                self.target_journal(self.prepared(root, project)),
                handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            )
            write_rollover_journal(project, target)
            client, _calls = self.restarted_controller(project, target)
            client._active_rollover_journal = target
            client.pending_handoff = handoff
            client._pending_guardian_finish_candidate = GuardianFinishCandidate(
                old_task_id="source-task",
                target_task_id="target-task",
                handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            )
            client.thread = SimpleNamespace(id="target-task")
            client.verbose = True
            client._suppress_turn_output = False
            observed: list[str] = []

            def run_turn(prompt: str, *, stream_text: bool, **_kwargs: Any) -> TurnOutcome:
                self.assertFalse(stream_text)
                observed.append(prompt)
                durable = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(durable.phase, "dispatch_started")
                return TurnOutcome("real work", None, tool_activity=True)

            client.run_turn = run_turn
            try:
                prompt = handoff + "\n\nexecute one concrete unfinished action"
                outcome = client._run_controller_turn_silently(prompt)
                self.assertEqual(observed, [prompt])
                self.assertIn(handoff, observed[0])
                with patch(
                    "continuous_cli.finish_guardian_session",
                    side_effect=lambda *_args, **_kwargs: self.retire_source(project),
                ):
                    client._complete_handoff_dispatch(
                        outcome,
                        dispatched_handoff=handoff,
                    )
                durable = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(durable.phase, "source_retired")
            finally:
                self.release_controller(client)

    def test_preflight_failure_keeps_exact_target_for_journal_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            client = ContinuousCodex.__new__(ContinuousCodex)
            client.project_root = project
            client.thread = SimpleNamespace(id="source-task")
            client.latest_user = "continue objective"
            client.latest_assistant = "checkpointed"
            client.pending_handoff = None
            client._pending_guardian_finish_candidate = None
            client._pending_guardian_finishes = []
            client._active_rollover_journal = None
            client._rollover_journal_lease = None
            client._rewind_sources = []
            client.rollovers = 0
            client._checkpoint_for_rollover = lambda _reason: True
            client.start_cleared_thread = lambda: setattr(
                client,
                "thread",
                SimpleNamespace(id="target-task"),
            )
            target = GuardianTarget(
                workspace_root=root,
                project_id="demo",
                contextctl=root / ".agents/skills/context-guardian/scripts/contextctl.py",
            )
            try:
                with (
                    patch("continuous_cli.discover_guardian_target", return_value=target),
                    patch(
                        "continuous_cli.validated_guardian_bundle",
                        side_effect=RuntimeError("preflight crashed"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "retained for journal"),
                ):
                    client.prepare_rollover("context full")
                journal = read_rollover_journal(
                    project,
                    lease=client._rollover_journal_lease,
                )
                self.assertEqual(journal.phase, "prepared")
                self.assertEqual(client.thread.id, "source-task")
            finally:
                self.release_controller(client)

    def test_same_process_transport_recovery_reconciles_prepared_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, project = self.fixture(raw)
            self.write_source_runtime(project)
            prepared = self.prepared(root, project)
            write_rollover_journal(project, prepared)
            client, calls = self.restarted_controller(project, prepared)
            restarted: list[str | None] = []
            client._suppress_turn_output = False
            client._restart_app_server = (
                lambda *, resume_thread_id=None, reconnect_only=False: restarted.append(
                    (resume_thread_id, reconnect_only)
                )
            )
            def run_recovered(_prompt: str) -> TurnOutcome:
                client._begin_concrete_rollover_dispatch(str(client.pending_handoff))
                return TurnOutcome(
                    "concrete recovered work",
                    None,
                    tool_activity=True,
                )

            client._run_controller_turn_silently = run_recovered

            def preflight(*_args: Any, **_kwargs: Any) -> str:
                self.write_target_runtime(project)
                return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `target-task`\n"

            try:
                with (
                    patch(
                        "continuous_cli.validated_guardian_bundle",
                        side_effect=preflight,
                    ),
                    patch(
                        "continuous_cli.finish_guardian_session",
                        side_effect=lambda *_args, **_kwargs: self.retire_source(project),
                    ),
                ):
                    outcome = client.recover_transport_failure("thread/start disconnected")
                self.assertEqual(outcome.final_response, "concrete recovered work")
                self.assertEqual(restarted, [(None, True)])
                self.assertEqual(calls["start"], 0)
                self.assertFalse(
                    (project / ".context" / "runtime" / "source-task.json").exists()
                )
            finally:
                self.release_controller(client)

    def test_uncertain_target_creation_reconnects_before_authoritative_scan(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        client._rollover_target_creation_uncertain = True
        client._restart_app_server = lambda **kwargs: events.append(
            f"restart:{kwargs.get('reconnect_only')}"
        )
        client._reconcile_rollover_journal = lambda: events.append("scan") or "active"

        self.assertEqual(client._reconcile_rollover_journal_with_retry(), "active")
        self.assertEqual(events, ["restart:True", "scan"])
        self.assertFalse(client._rollover_target_creation_uncertain)

    def test_interactive_reconciles_before_any_fresh_thread(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        client._reconcile_rollover_journal = lambda: events.append("reconcile") or "complete"
        client._reconciled_terminal_outcome = TurnOutcome("real final", None)
        client._publish_automatic_terminal_response = lambda _outcome: events.append("publish")
        client.start_fresh_thread = lambda: events.append("start")
        self.assertEqual(client.interactive(), 0)
        self.assertEqual(events, ["reconcile", "publish"])

    def test_startup_reconciliation_retries_transient_failure_without_fresh_thread(
        self,
    ) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        attempts = iter((RuntimeError("temporary thread/list failure"), "complete"))

        def reconcile() -> str:
            events.append("reconcile")
            value = next(attempts)
            if isinstance(value, BaseException):
                raise value
            return value

        client._reconcile_rollover_journal = reconcile
        client._ensure_rollover_journal_lease = lambda: None
        client._reconciled_terminal_outcome = TurnOutcome("real final", None)
        client._publish_automatic_terminal_response = lambda _outcome: events.append(
            "publish"
        )
        client.start_fresh_thread = lambda: events.append("start")
        with patch("continuous_cli.time.sleep"):
            self.assertEqual(client.interactive(), 0)
        self.assertEqual(events, ["reconcile", "reconcile", "publish"])

    def test_automatic_rollover_exception_reconciles_and_keeps_running(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        client._unresolved_guardian_target_candidate = lambda: None
        client.continue_after_rollover = lambda _reason: (_ for _ in ()).throw(
            RuntimeError("target preflight disconnected")
        )
        client._reconcile_rollover_journal_with_retry = lambda: events.append(
            "reconcile"
        ) or "active"
        client._continue_reconciled_rollover = lambda _state: TurnOutcome(
            "real final",
            None,
            guardian_finished=True,
            guardian_finish_kind="completed",
        )
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        client._current_guardian_runtime_active = lambda: False
        client._wait_for_pending_guardian_finishes = lambda _outcome: None
        client._publish_automatic_terminal_response = lambda _outcome: events.append(
            "publish"
        )
        client._unguarded_objective_completion_proven = lambda _outcome: False
        client._run_automatic_objective_loop(reason="context threshold")
        self.assertEqual(events, ["reconcile", "publish"])

    def test_automatic_rollover_retries_transient_prejournal_failure(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        attempts = 0

        def continue_rollover(_reason: str) -> TurnOutcome:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("checkpoint transport failure")
            return TurnOutcome(
                "real final",
                None,
                guardian_finished=True,
                guardian_finish_kind="completed",
            )

        client._unresolved_guardian_target_candidate = lambda: None
        client.continue_after_rollover = continue_rollover
        client._reconcile_rollover_journal_with_retry = lambda: "none"
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        client._current_guardian_runtime_active = lambda: False
        client._wait_for_pending_guardian_finishes = lambda _outcome: None
        client._publish_automatic_terminal_response = lambda _outcome: events.append(
            "publish"
        )
        client._unguarded_objective_completion_proven = lambda _outcome: False
        with patch("continuous_cli.time.sleep"):
            client._run_automatic_objective_loop(reason="context threshold")
        self.assertEqual(attempts, 2)
        self.assertEqual(events, ["publish"])

    def test_nontransport_automatic_error_continues_without_reprompt(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        events: list[str] = []
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        client.continue_nonreplayable = lambda reason, **_kwargs: (
            events.append("continue:" + reason)
            or TurnOutcome(
                "real final",
                None,
                guardian_finished=True,
                guardian_finish_kind="completed",
            )
        )
        client._current_guardian_runtime_active = lambda: False
        client._wait_for_pending_guardian_finishes = lambda _outcome: None
        client._publish_automatic_terminal_response = lambda _outcome: events.append(
            "publish"
        )
        client._unguarded_objective_completion_proven = lambda _outcome: False

        client._run_automatic_objective_loop(
            reason=None,
            initial_outcome=TurnOutcome(
                "",
                None,
                error_message="invalid response payload",
                error_code="invalidRequest",
            ),
        )

        self.assertEqual(
            events,
            [
                "continue:non-replayable automatic turn failure: "
                "invalid response payload",
                "publish",
            ],
        )


class DumpPayload:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.data


class Event:
    def __init__(self, method: str, data: dict[str, Any]) -> None:
        self.method = method
        self.payload = DumpPayload(data)


class FakeHandle:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def stream(self):
        yield from self.events
        raise AssertionError("run_turn read past turn/completed")

    def interrupt(self) -> None:
        return None


class FakeThread:
    id = "thread-test"

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.last_input: Any = None
        self.last_kwargs: dict[str, Any] = {}

    def turn(self, value: Any, **kwargs: Any) -> FakeHandle:
        self.last_input = value
        self.last_kwargs = kwargs
        return FakeHandle(self.events)


class FakeRewindRenderer:
    _native_stream = True
    claude_like = True
    line_open = False

    def __init__(self, selections: tuple[int, ...] = (0, 0)) -> None:
        self.epoch = 0
        self._selections = list(selections)
        self.menus: list[
            tuple[str, list[tuple[str, str]], dict[str, Any]]
        ] = []
        self.reset_calls: list[bool] = []
        self.clear_calls = 0

    def select_menu(
        self,
        title: str,
        options: list[tuple[str, str]],
        **kwargs: Any,
    ) -> SimpleNamespace:
        self.menus.append((title, options, kwargs))
        if not self._selections:
            raise AssertionError("rewind requested an unexpected menu")
        return SimpleNamespace(index=self._selections.pop(0))

    def reset_conversation_view(self, *, clear_input_history: bool) -> None:
        self.reset_calls.append(clear_input_history)
        self.epoch += 1

    def clear_screen(self) -> None:
        self.clear_calls += 1
        self.epoch += 1


def run_with_events(
    events: list[Any],
    *,
    guardian_receipt_value: dict[str, Any] | None = None,
) -> TurnOutcome:
    client = ContinuousCodex.__new__(ContinuousCodex)
    client.thread = FakeThread(events)
    client.project_root = Path("C:/project")
    client._output_lock = threading.RLock()
    client._compacted_threads = set()
    client._service_pending_input = lambda _timeout=0.1: None
    client.client = SimpleNamespace(close=lambda: None)
    found_receipt = (
        (
            GuardianTarget(
                workspace_root=Path("C:/workspace"),
                project_id=str(guardian_receipt_value.get("project_id", "demo")),
                contextctl=Path("C:/workspace/contextctl.py"),
            ),
            guardian_receipt_value,
        )
        if guardian_receipt_value is not None
        else None
    )
    with (
        contextlib.redirect_stdout(io.StringIO()),
        patch("continuous_cli.guardian_receipt", return_value=found_receipt),
    ):
        return client.run_turn("test", stream_text=False)


class TerminalRendererTests(unittest.TestCase):
    @staticmethod
    def renderer(
        *,
        tty: bool,
        color: bool,
        unicode: bool,
        width: int = 80,
        rows: int = 40,
    ) -> tuple[TerminalRenderer, io.StringIO]:
        output = io.StringIO()
        renderer = TerminalRenderer(
            stream=output,
            lock=threading.RLock(),
            capabilities=RenderCapabilities(
                tty=tty,
                color=color,
                unicode=unicode,
                width=width,
                rows=rows,
            ),
        )
        return renderer, output

    def test_detect_uses_flat_mode_for_dumb_terminal_and_no_color_for_tty(self) -> None:
        stream = SimpleNamespace(isatty=lambda: True, encoding="utf-8")
        terminal_size = os.terminal_size((80, 40))

        with (
            patch.dict(os.environ, {"TERM": "dumb"}, clear=False),
            patch("continuous_cli.shutil.get_terminal_size", return_value=terminal_size),
        ):
            dumb = RenderCapabilities.detect(stream)
        self.assertFalse(dumb.tty)
        self.assertFalse(dumb.color)

        with (
            patch.dict(
                os.environ,
                {"TERM": "xterm-256color", "NO_COLOR": "1"},
                clear=False,
            ),
            patch("continuous_cli.shutil.get_terminal_size", return_value=terminal_size),
        ):
            no_color = RenderCapabilities.detect(stream)
        self.assertTrue(no_color.tty)
        self.assertFalse(no_color.color)

    def test_full_tui_layout_requires_unicode_width_and_height(self) -> None:
        cases = [
            (True, 80, 40, True),
            (False, 80, 40, False),
            (True, 43, 40, False),
            (True, 80, 29, False),
        ]
        for unicode, width, rows, expected in cases:
            with self.subTest(unicode=unicode, width=width, rows=rows):
                renderer, _ = self.renderer(
                    tty=True,
                    color=False,
                    unicode=unicode,
                    width=width,
                    rows=rows,
                )
                self.assertEqual(renderer._full_tui_layout(), expected)

    def test_tty_color_renders_welcome_status_assistant_and_tools(self) -> None:
        renderer, output = self.renderer(tty=True, color=True, unicode=True)

        renderer.banner("0.146.0", "demo")
        renderer.status("\u6b63\u5728\u63a5\u7e8c", level=RenderLevel.PROGRESS)
        renderer.assistant_delta("\u5148\u6aa2\u67e5\u3002", prefix=True)
        renderer.tool_started(
            ToolPresentation(
                kind="commandExecution",
                label="Run",
                detail="python -m unittest",
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done",
                detail="exit=0",
                success=True,
            )
        )
        renderer.assistant_delta("\u5b8c\u6210\u3002", prefix=True)
        renderer.end_assistant()

        rendered = output.getvalue()
        self.assertIn("\x1b[", rendered)
        self.assertIn("Codex Code v0.146.0", rendered)
        self.assertIn("demo", rendered)
        self.assertNotIn("continuous mode", rendered)
        self.assertIn("◇", rendered)
        self.assertIn("●", rendered)
        self.assertIn("↳", rendered)
        self.assertIn("⎿", rendered)
        self.assertIn("python -m unittest", rendered)
        self.assertIn("exit=0", rendered)
        self.assertNotIn("\r", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_tty_no_color_keeps_layout_without_escape_sequences(self) -> None:
        renderer, output = self.renderer(tty=True, color=False, unicode=True)

        renderer.banner("0.146.0", "demo")
        renderer.status("\u9700\u8981\u6ce8\u610f", level=RenderLevel.WARNING)
        renderer.assistant_delta("answer", prefix=True)
        renderer.end_assistant()

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertIn("╭", rendered)
        self.assertIn("⚠ \u9700\u8981\u6ce8\u610f", rendered)
        self.assertIn("● answer", rendered)

    def test_ascii_capability_uses_plain_glyph_fallbacks(self) -> None:
        renderer, output = self.renderer(tty=True, color=False, unicode=False)

        renderer.status("warning", level=RenderLevel.WARNING)
        renderer.assistant_delta("answer", prefix=True)
        renderer.end_assistant()
        renderer.tool_started(ToolPresentation(kind="commandExecution", label="Run"))
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done",
                success=True,
            )
        )

        rendered = output.getvalue()
        self.assertIn("! warning", rendered)
        self.assertIn("* answer", rendered)
        self.assertIn("  > Run", rendered)
        self.assertIn("  - Done", rendered)
        for glyph in "⚠●↳⎿":
            self.assertNotIn(glyph, rendered)

    def test_narrow_tty_bounds_tool_metadata_to_terminal_width(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=24,
            rows=20,
        )
        detail = "abcdefghijklmnopqrstuvwxyz"

        renderer.tool_started(
            ToolPresentation(
                kind="commandExecution",
                label="Run",
                detail=detail,
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done",
                detail=detail,
                success=True,
            )
        )

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(display_width(line) <= 24 for line in lines), lines)
        self.assertTrue(all(line.endswith("…") for line in lines), lines)

    def test_tool_metadata_strips_ansi_and_collapses_crlf(self) -> None:
        renderer, output = self.renderer(tty=True, color=False, unicode=True)
        unsafe = "echo one\r\n\x1b[31mecho two\x1b[0m\t--flag"

        renderer.tool_started(
            ToolPresentation(
                kind="commandExecution",
                label="Run",
                detail=unsafe,
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done",
                detail=unsafe,
                success=False,
            )
        )

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertIn("echo one echo two --flag", rendered)
        self.assertEqual(len(rendered.splitlines()), 2)

    def test_non_tty_preserves_legacy_plain_text_format(self) -> None:
        renderer, output = self.renderer(tty=False, color=False, unicode=True)

        renderer.banner("0.146.0", "demo")
        renderer.status("ready", level=RenderLevel.SUCCESS)
        renderer.assistant_delta("answer", prefix=True)
        renderer.end_assistant()
        renderer.tool_started(
            ToolPresentation(
                kind="commandExecution",
                label="Run",
                detail="echo ok",
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done",
                detail="exit=0",
                success=True,
            )
        )

        self.assertEqual(
            output.getvalue(),
            "Codex Continuous 0.146.0 | demo\n"
            "System: ready\n"
            "Codex: answer\n"
            "  tool: commandExecution  echo ok\n"
            "  done: commandExecution  exit=0\n",
        )

    def test_tty_prompt_renders_borders_footer_and_prompt_marker(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=48,
            rows=40,
        )

        with patch("builtins.input", return_value="continue") as mocked_input:
            answer = renderer.prompt("/status  /verbose  /new  /exit")

        self.assertEqual(answer, "continue")
        mocked_input.assert_called_once_with("> ")
        border = "─" * 48
        lines = output.getvalue().splitlines()
        self.assertEqual(lines.count(border), 2)
        self.assertEqual(lines[-1], "  /status  /verbose  /new  /exit")
        self.assertNotIn("You:", output.getvalue())

    def test_toolkit_prompt_keeps_only_compact_submission_in_scrollback(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=48,
            rows=40,
        )

        with patch.object(renderer, "_prompt_with_toolkit", return_value="ship\nnow"):
            answer = renderer.prompt("continuous mode on")

        self.assertEqual(answer, "ship\nnow")
        self.assertEqual(output.getvalue(), "> ship\n  now\n")
        self.assertNotIn("continuous mode on", output.getvalue())
        self.assertNotIn("─", output.getvalue())

    def test_toolkit_rewind_token_leaves_no_scrollback_row(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=48,
            rows=40,
        )

        with patch.object(renderer, "_prompt_with_toolkit", return_value=REWIND_TOKEN):
            answer = renderer.prompt("continuous mode on")

        self.assertEqual(answer, REWIND_TOKEN)
        self.assertEqual(output.getvalue(), "")

    def test_toolkit_prompt_accepts_in_place_tool_details_toggle(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
        )
        state = {"expanded": False}

        def toggle() -> None:
            state["expanded"] = not state["expanded"]

        def fake_toolkit(
            footer: Callable[[], str],
            callback: Callable[[], None],
            **_kwargs: Any,
        ) -> str:
            self.assertEqual(footer(), "collapsed")
            callback()
            self.assertEqual(footer(), "expanded")
            return "draft preserved"

        with patch.object(
            renderer,
            "_prompt_with_toolkit",
            side_effect=fake_toolkit,
        ):
            answer = renderer.prompt(
                lambda: "expanded" if state["expanded"] else "collapsed",
                on_tool_details_toggle=toggle,
            )

        self.assertEqual(answer, "draft preserved")
        self.assertEqual(output.getvalue(), "> draft preserved\n")

    def test_help_describes_ctrl_o_as_transcript_shortcut(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
        )
        renderer.help()
        self.assertIn("ctrl + o to open transcript", output.getvalue())
        self.assertNotIn("toggle tool details", output.getvalue())

    def test_tty_question_preserves_option_order_and_descriptions(self) -> None:
        renderer, output = self.renderer(tty=True, color=False, unicode=True)
        options = [
            {"label": "Alpha", "description": "first choice"},
            {"label": "Beta", "description": "second choice"},
            {"label": "Gamma", "description": "third choice"},
        ]

        renderer.question("Choose a mode", options)

        rendered = output.getvalue()
        expected_fragments = [
            "? Choose a mode",
            "  1. Alpha",
            "     first choice",
            "  2. Beta",
            "     second choice",
            "  3. Gamma",
            "     third choice",
        ]
        positions = [rendered.index(fragment) for fragment in expected_fragments]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Your choice is required:", rendered)
        self.assertNotIn("You:", rendered)
        self.assertNotIn("Codex:", rendered)

    def test_short_narrow_tty_uses_one_line_bounded_welcome(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=32,
            rows=20,
        )

        renderer.banner("0.146.0", "a-very-long-project-name")

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(display_width(lines[0]), 32, lines)
        self.assertIn("Codex Code", lines[0])

    def test_plain_renderer_sanitizes_every_metadata_surface(self) -> None:
        renderer, output = self.renderer(tty=False, color=False, unicode=True)

        renderer.status("\x1b[31mstatus one\r\nstatus two\x1b[0m")
        renderer.question(
            "\x1b[2Jquestion one\r\nquestion two",
            [
                {
                    "label": "\x1b[32moption one\r\noption two\x1b[0m",
                    "description": "\x1b[Hdescription one\r\ndescription two",
                }
            ],
        )
        renderer.auto_choice("\x1b[33mchoice one\r\nchoice two\x1b[0m")
        renderer.tool_started(
            ToolPresentation(
                kind="\x1b[34mcommand\r\nExecution\x1b[0m",
                label="Run",
                detail="\x1b[35mdetail one\r\ndetail two\x1b[0m",
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="\x1b[34mcommand\r\nExecution\x1b[0m",
                label="Done",
                detail="\x1b[35mresult one\r\nresult two\x1b[0m",
                success=True,
            )
        )

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertIn("System: status one status two", rendered)
        self.assertIn("question one question two", rendered)
        self.assertIn(
            "1. option one option two — description one description two",
            rendered,
        )
        self.assertIn("Automatically selected: choice one choice two", rendered)
        self.assertIn("tool: command Execution  detail one detail two", rendered)
        self.assertIn("done: command Execution  result one result two", rendered)
        self.assertEqual(len(rendered.splitlines()), 7)

    def test_plain_assistant_strips_cursor_controls_and_preserves_lines(self) -> None:
        renderer, output = self.renderer(tty=False, color=False, unicode=True)

        renderer.assistant_delta(
            "\x1b[2Jfirst\rsecond\r\nthird\n\x1b[Hfourth",
            prefix=True,
        )
        renderer.end_assistant()

        self.assertEqual(
            output.getvalue(),
            "Codex: first\nsecond\nthird\nfourth\n",
        )
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("\r", output.getvalue())

    def test_dynamic_resize_rebounds_tool_footer_and_banner(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=80,
            rows=40,
        )
        renderer._dynamic_size = True
        terminal = {"size": SimpleNamespace(columns=80, lines=40)}

        def current_size(*_: Any, **__: Any) -> SimpleNamespace:
            return terminal["size"]

        with patch(
            "continuous_cli.shutil.get_terminal_size",
            side_effect=current_size,
        ):
            renderer.banner("0.146.0", "wide-project")
            wide_lines = output.getvalue().splitlines()
            self.assertTrue(any(display_width(line) == 80 for line in wide_lines))

            output.seek(0)
            output.truncate(0)
            terminal["size"] = SimpleNamespace(columns=24, lines=20)

            renderer.tool_started(
                ToolPresentation(
                    kind="commandExecution",
                    label="Run",
                    detail="abcdefghijklmnopqrstuvwxyz",
                )
            )
            renderer.tool_completed(
                ToolPresentation(
                    kind="commandExecution",
                    label="Done",
                    detail="abcdefghijklmnopqrstuvwxyz",
                    success=True,
                )
            )
            with patch("builtins.input", return_value="continue"):
                renderer.prompt("/status /verbose /new /exit and more")
            renderer.banner("0.146.0", "a-very-long-project-name")

        resized_lines = output.getvalue().splitlines()
        self.assertTrue(resized_lines)
        self.assertTrue(
            all(display_width(line) <= 24 for line in resized_lines),
            resized_lines,
        )
        self.assertEqual(resized_lines.count("─" * 24), 2)
        self.assertTrue(any("Codex Code" in line for line in resized_lines))

    def test_ascii_encoded_stream_truncates_without_unicode_punctuation(self) -> None:
        class AsciiStream(io.StringIO):
            @property
            def encoding(self) -> str:
                return "ascii"

            def write(self, value: str) -> int:
                value.encode(self.encoding)
                return super().write(value)

        output = AsciiStream()
        renderer = TerminalRenderer(
            stream=output,
            lock=threading.RLock(),
            capabilities=RenderCapabilities(
                tty=True,
                color=False,
                unicode=False,
                width=20,
                rows=20,
            ),
        )

        renderer.banner("0.146.0", "a-very-long-project-name")
        renderer.tool_started(
            ToolPresentation(
                kind="commandExecution",
                label="Run-a-very-long-label",
                detail="abcdefghijklmnopqrstuvwxyz",
            )
        )
        renderer.tool_completed(
            ToolPresentation(
                kind="commandExecution",
                label="Done-a-very-long-label",
                detail="abcdefghijklmnopqrstuvwxyz",
                success=True,
            )
        )

        rendered = output.getvalue()
        rendered.encode("ascii")
        self.assertIn("~", rendered)
        self.assertNotIn("…", rendered)
        self.assertNotIn("·", rendered)
        self.assertTrue(
            all(display_width(line) <= 20 for line in rendered.splitlines()),
            rendered.splitlines(),
        )

    def test_eight_column_banner_and_prompt_stay_within_width(self) -> None:
        renderer, output = self.renderer(
            tty=True,
            color=False,
            unicode=True,
            width=8,
            rows=20,
        )

        renderer.banner("0.146.0", "long-project")
        with patch("builtins.input", return_value="ok") as mocked_input:
            self.assertEqual(renderer.prompt("/status /verbose"), "ok")

        mocked_input.assert_called_once_with("> ")
        lines = output.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(
            all(display_width(line) <= 8 for line in lines),
            lines,
        )
        self.assertEqual(lines.count("─" * 8), 2)

    def test_assistant_preserves_emoji_zwj_and_zwnj(self) -> None:
        renderer, output = self.renderer(tty=False, color=False, unicode=True)
        content = "Developer 👩‍💻 uses non\u200cjoining text."

        renderer.assistant_delta(content, prefix=True)
        renderer.end_assistant()

        self.assertEqual(output.getvalue(), f"Codex: {content}\n")
        self.assertIn("👩‍💻", output.getvalue())
        self.assertIn("\u200c", output.getvalue())


class ContinuousCliTests(unittest.TestCase):
    def test_trim_keeps_head_and_tail(self) -> None:
        value = "A" * 200 + "Z" * 200
        trimmed = trim_text(value, 120)
        self.assertLessEqual(len(trimmed), 130)
        self.assertTrue(trimmed.startswith("A"))
        self.assertTrue(trimmed.endswith("Z"))

    def test_auto_input_grace_is_short_but_user_configurable(self) -> None:
        with patch.dict(
            "os.environ",
            {"CODEX_CONTINUOUS_INPUT_GRACE_MS": "invalid"},
        ):
            self.assertEqual(auto_input_grace_ms(60_000), 1500)
        with patch.dict(
            "os.environ",
            {"CODEX_CONTINUOUS_INPUT_GRACE_MS": "2500"},
        ):
            self.assertEqual(auto_input_grace_ms(60_000), 2500)
            self.assertEqual(auto_input_grace_ms(1000), 1000)

    def test_generic_block_detection_is_specific(self) -> None:
        self.assertTrue(is_generic_content_block("This content can't be shown"))
        self.assertTrue(
            is_generic_content_block(
                "This content can't be shown\nWe take extra caution with cybersecurity requests."
            )
        )
        self.assertTrue(
            is_generic_content_block(
                "This content was flagged for possible cybersecurity risk. "
                "If this seems wrong, try rephrasing your request."
            )
        )
        self.assertFalse(
            is_generic_content_block(
                "The UI displayed ‘This content can't be shown’, so I inspected the logs."
            )
        )
        self.assertFalse(
            is_generic_content_block(
                "The server said ‘This content was flagged for possible cybersecurity risk’."
            )
        )
        self.assertFalse(is_generic_content_block("A unit test failed"))

    def test_rollover_at_ratio(self) -> None:
        self.assertIsNone(rollover_reason(TurnOutcome("done", usage(619)), None, 0.62))
        self.assertEqual(
            rollover_reason(TurnOutcome("done", usage(620)), None, 0.62),
            "context 62%",
        )
        self.assertEqual(
            rollover_reason(TurnOutcome("done", usage(621)), None, 0.62),
            "context 62%",
        )

    def test_rollover_on_compaction_or_token_drop(self) -> None:
        compacted = TurnOutcome("done", usage(200), compacted=True)
        self.assertEqual(rollover_reason(compacted, 700, 0.62), "compaction event")
        dropped = TurnOutcome("done", usage(300))
        self.assertEqual(
            rollover_reason(dropped, 600, 0.80),
            "post-compaction token drop",
        )

    def test_manual_new_handoff_is_a_narrow_rollover_failsafe(self) -> None:
        handoff = (
            "\u9019\u500b\u820a\u5c0d\u8a71\u5df2\u6b63\u5f0f\u6536\u5c3e\u3002\u6211\u7121\u6cd5\u66ff\u4f60\u64cd\u4f5c\u4ecb\u9762\u5efa\u7acb\u804a\u5929\uff1b"
            "\u8acb\u6309\u300c\u65b0\u589e\u5c0d\u8a71\u300d\uff0c\u7b2c\u4e00\u53e5\u8f38\u5165\uff1a\u7e7c\u7e8c sample-project\uff08resume\uff09\u3002"
        )
        self.assertTrue(assistant_requests_manual_fresh_thread(handoff))
        self.assertFalse(assistant_response_allows_terminal_settlement(handoff))
        self.assertFalse(
            outcome_allows_terminal_settlement(
                TurnOutcome(
                    handoff,
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                )
            )
        )
        self.assertEqual(
            rollover_reason(
                TurnOutcome(
                    handoff,
                    None,
                    guardian_checkpointed=True,
                    guardian_audited=True,
                ),
                None,
                0.62,
            ),
            "assistant requested manual fresh thread",
        )
        self.assertIsNone(rollover_reason(TurnOutcome(handoff, None), None, 0.62))
        self.assertFalse(
            assistant_requests_manual_fresh_thread(
                "Stock TUI \u53ef\u80fd\u6703\u986f\u793a\u300e\u8acb\u6309\u65b0\u589e\u5c0d\u8a71\u300f\uff1bcontinuous client \u6703\u81ea\u52d5\u8655\u7406\u3002"
            )
        )

    def test_future_work_rollover_receipt_is_status_only(self) -> None:
        fake_success = """\
\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5\u5c08\u6848\uff1a

- \u65b0 task\uff1a019ff739-b0e1-7b12-abaf-7576f1cd0283
- \u5df2\u9a57\u8b49 checkpoint SHA\uff1a401f6f7f87340cc319ddfd16e6e19483629552191012ecdb45cb877ed9f3d916
- \u820a task \u5df2\u5b89\u5168\u7d50\u675f\uff0c\u6c92\u6709\u5efa\u7acb\u91cd\u8907 task\u3002
- \u65b0 task \u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c\uff1a\u5148\u5b8c\u6210 release_id manifest \u5951\u7d04\uff0c\u518d\u8655\u7406 multi-root \u8207 exact verifier\u3002

\u4e0d\u9700\u8981\u518d\u6b21\u8f38\u5165\u300c\u7e7c\u7e8c\u300d\u3002
"""
        self.assertTrue(assistant_reports_rollover_status_only(fake_success))
        self.assertFalse(
            assistant_reports_rollover_status_only(
                "manifest \u5951\u7d04\u8207 exact verifier \u5df2\u5be6\u4f5c\u4e26\u901a\u904e\u5168\u90e8\u6e2c\u8a66\u3002"
            )
        )
        self.assertTrue(assistant_defers_unfinished_work(PREMATURE_FINISH_RESPONSE))
        self.assertFalse(
            assistant_response_allows_terminal_settlement(
                PREMATURE_FINISH_RESPONSE
            )
        )
        deferred_variants = (
            "artifact \u7a3d\u6838\u5c1a\u672a\u5168\u90e8\u5b8c\u6210\u3002",
            "artifact \u7a3d\u6838\u5c1a\u672a \u5b8c\u6210\u3002",
            "artifact \u7a3d\u6838\u5c1a\u672a\n\u5b8c\u6210\u3002",
            "artifact \u7a3d\u6838\u9084\u6c92\u5b8c\u6210\u3002",
            "artifact \u7a3d\u6838\u672a\u5b8c\u6210\u3002",
            "\u654f\u611f\u8cc7\u6599\u908a\u754c\u4ecd\u672a\u5b8c\u6574\u9a57\u8b49\u3002",
            "\u654f\u611f\u8cc7\u6599\u908a\u754c\u9084\u9700\u8981\u9a57\u8b49\u3002",
            "artifact \u5f85\u5b8c\u6210\u3002",
            "\u6c92\u6709\u5c1a\u672a\u5b8c\u6210\u7684\u6e2c\u8a66\uff1bartifact \u7a3d\u6838\u4ecd\u9700\u5b8c\u6210\uff0c\u5c07\u81ea\u52d5\u7e8c\u63a5\u3002",
            "\u4e26\u7121\u5c1a\u672a\u9a57\u8b49\u9805\u76ee\uff1b\u4f46\u654f\u611f\u8cc7\u6599\u908a\u754c\u5c07\u5728\u4e0b\u4e00\u8f2a\u9a57\u8b49\u3002",
            "\u654f\u611f\u6570\u636e\u8fb9\u754c\u8fd8\u6ca1\u5b8c\u6210\uff0c\u5c06\u81ea\u52a8\u63a5\u7eed\u3002",
            "The remaining verifier work will be handled by another task.",
            "\u9918\u4e0b verifier \u9a57\u8b49\u5c07\u4ea4\u7531\u53e6\u4e00\u500b task \u8655\u7406\u3002",
            "I have not completed the tests.",
            "The tests have not been completed.",
            "I did not finish the audit.",
            "Further verification is required.",
            "The objective is not complete.",
            "The audit remains to be done.",
            "I haven't completed the tests.",
            "We haven't verified the fix.",
            "The audit isn't complete.",
            "Tests aren't done.",
            "There is still work to do.",
            "There is still more work to do.",
            "Some work remains.",
            "Tests remain.",
            "The audit remains to be completed.",
            "I need another turn to finish the audit.",
            "Open is empty; Next Actions: finish deployment later.",
            "\u672a\u9a57\u8b49 live \u884c\u70ba\uff1aproduction Stop path\uff1b\u4ea4\u4ed8\u524d\u4ecd\u9700\u9a57\u8b49\u3002",
            "Live behavior was not verified and must be verified before release.",
            "Live validation was not run; it will be run next.",
            "\u672a\u5b8c\u6210\u9805\u76ee\uff1a0\uff1bartifact \u7a3d\u6838\u4ecd\u9700\u5b8c\u6210\u3002",
            "All remaining work is complete; another audit remains.",
            "\u6e2c\u8a66\u5c1a\u672a\u5b8c\u6210\uff0c\u5b8c\u6210\u5f8c\u624d\u6703\u57f7\u884c\u90e8\u7f72\u3002",
            "\u5b8c\u6210\u5f8c\u624d\u6703\u57f7\u884c\u90e8\u7f72\u3002",
            "\u6e2c\u8a66\u5c1a\u672a\u7d50\u675f\u7684\u6b63\u4f8b\u4ecd\u6b63\u78ba\u963b\u64cb\uff1b\u90e8\u7f72\u5c1a\u672a\u5b8c\u6210\u3002",
            (
                "Positive unfinished-work cases are correctly blocked; "
                "the audit is incomplete."
            ),
        )
        for response in deferred_variants:
            with self.subTest(response=response):
                self.assertTrue(assistant_defers_unfinished_work(response))
                self.assertFalse(
                    assistant_response_allows_terminal_settlement(response)
                )
        completed_variants = (
            "\u6c92\u6709\u5c1a\u672a\u5b8c\u6210\u7684\u6e2c\u8a66\u3002",
            "\u6c92\u6709\u4efb\u4f55\u672a\u5b8c\u6210\u9805\u76ee\u3002",
            "\u4e26\u7121\u5c1a\u672a\u9a57\u8b49\u9805\u76ee\u3002",
            "\u7121\u672a\u5b8c\u6210\u5de5\u4f5c\u3002",
            "\u4e0d\u5b58\u5728\u4efb\u4f55\u9084\u6c92\u5b8c\u6210\u7684\u9805\u76ee\u3002",
            "\u672a\u767c\u73fe\u4efb\u4f55\u672a\u5b8c\u6210\u9805\u76ee\u3002",
            "\u6c92\u6709\u4ecd\u9700\u8655\u7406\u7684\u9805\u76ee\u3002",
            "\u4e26\u6c92\u6709\u5f85\u9a57\u8b49\u9805\u76ee\u3002",
            "\u4e0d\u6703\u518d\u81ea\u52d5\u7e8c\u63a5\u3002",
            "There is no remaining work.",
            "No work remains.",
            "No tasks remain.",
            "No further work remains.",
            "None of the tasks remain.",
            "Nothing remains to be completed.",
            "Nothing remains to be done.",
            "Zero tasks remain.",
            "0 tests remain.",
            "No outstanding tests remain.",
            "Nothing remains pending.",
            "The implementation is not incomplete.",
            "The controller will not continue.",
            "No further verification will be run.",
            "No further verification is required.",
            "No additional work is needed.",
            "No further audit is pending.",
            "There is no work that remains pending.",
            "The objective is not incomplete.",
            "\u5df2\u5b8c\u6210\u3002\u672a\u5b8c\u6210\u9805\u76ee\uff1a0\u3002",
            "\u672a\u5b8c\u6210\uff1a\u7121\uff1b\u672a\u9a57\u8b49\uff1a\u7121\u3002",
            "\u5b8c\u6210\u9805\u76ee 12\uff0c\u672a\u5b8c\u6210\u9805\u76ee 0\u3002",
            "\u6240\u6709\u5de5\u4f5c\u5b8c\u6210\uff1b\u672a\u5b8c\u6210\u9805\u76ee\u4e0d\u5b58\u5728\u3002",
            "\u6c92\u6709\u5269\u9918\u5de5\u4f5c\u3002",
            "\u7121\u5269\u9918\u6e2c\u8a66\u3002",
            "\u5269\u9918\u5de5\u4f5c\uff1a\u7121\u3002",
            "All remaining work is complete.",
            "All remaining tests now pass.",
            "All outstanding tasks have been completed.",
            "The outstanding audit is complete.",
            "\u5df2\u4fee\u5fa9\u300c\u5c1a\u672a\u5b8c\u6210\u537b\u88ab\u7576\u4f5c final\u300d\u7684\u554f\u984c\uff1b\u6240\u6709\u6e2c\u8a66\u901a\u904e\u3002",
            (
                "\u975e managed hook \u5fc5\u9808\u7d93\u4fe1\u4efb\u5f8c\u624d\u6703\u57f7\u884c\uff1b"
                "\u6b64\u76ee\u6a19\u6c92\u6709\u4efb\u4f55\u672a\u5b8c\u6210\u5de5\u4f5c\u3002"
            ),
            (
                "A non-managed hook must be trusted before it will execute. "
                "All requested work is complete."
            ),
            "\u771f\u6b63\u8868\u793a\u5ef6\u5f8c\u90e8\u7f72\u6216\u6e2c\u8a66\u5c1a\u672a\u7d50\u675f\u7684\u6b63\u4f8b\u4ecd\u6b63\u78ba\u963b\u64cb\u3002",
            "Positive unfinished-work cases are correctly blocked.",
            (
                'The "objective is not complete" false-termination case is fixed; '
                "all work is complete."
            ),
            (
                'Regression coverage includes "Further verification is required"; '
                "implementation and audit are complete."
            ),
            (
                "\u5df2\u6838\u5c0d\uff1a\u672c\u6b21\u76ee\u6a19\u5b8c\u6574\u5b8c\u6210\uff0c\u6c92\u6709\u672a\u5b8c\u6210\u52d5\u4f5c\u6216\u5f85\u8fa6\u3002"
                "\u6b0a\u5a01\u72c0\u614b\u7684 Open \u8207 Next Actions \u5747\u70ba\u7a7a\uff0ccontext audit \u8207 "
                "finish \u518d\u6b21\u901a\u904e\uff0c\u4e14\u6c92\u6709\u65b0\u589e\u6a94\u6848\u8b8a\u66f4\u3002"
            ),
            (
                "The Open / Uncertain and Next Actions sections are both empty; "
                "context audit and finish passed."
            ),
            "\u672a\u9a57\u8b49 live \u884c\u70ba\uff1a\u7121\u3002",
            "Open / Next Actions\uff1a\u7a7a\uff1b\u672a\u9a57\u8b49 live \u884c\u70ba\uff1a\u7121\u3002",
            "\u7a7a\u7684 Open\uff0fNext Actions \u8207 completed receipt lineage \u9a57\u8b49\u90fd\u5df2\u901a\u904e\u3002",
            (
                "Empty Open and Next Actions are confirmed; "
                "completed receipt validation passed."
            ),
        )
        for response in completed_variants:
            with self.subTest(response=response):
                self.assertFalse(assistant_defers_unfinished_work(response))
        validation_disclosures = (
            "\u672a\u9a57\u8b49 live \u884c\u70ba\uff1aproduction Stop path\u3002",
            (
                "\u672a\u57f7\u884c\u771f\u5be6 sample App Server --resume \u5de5\u4f5c\u968e\u6bb5\uff0c\u56e0\u6b64\u4e0d\u5ba3\u7a31 "
                "live resume \u884c\u70ba\u5df2\u9a57\u8b49\u3002"
            ),
            "Live behavior was not verified; no claim of live validation is made.",
            "NOT RUN live sample --resume; no live claim is made.",
            "Protocol-level validation passed; live behavior was not verified.",
            (
                "Checkpoint \u4e5f\u4e0d\u6703\u62b5\u9054 target \u5efa\u7acb\u3002Context audit \u8207 finish \u901a\u904e\u3002"
                "\u672a\u57f7\u884c\u771f\u5be6 sample App Server --resume \u5de5\u4f5c\u968e\u6bb5\uff0c\u56e0\u6b64\u4e0d\u5ba3\u7a31 "
                "live resume \u884c\u70ba\u5df2\u9a57\u8b49\u3002"
            ),
        )
        for response in validation_disclosures:
            with self.subTest(validation_disclosure=response):
                self.assertFalse(assistant_defers_unfinished_work(response))
                self.assertTrue(
                    assistant_response_allows_terminal_settlement(response)
                )
                self.assertFalse(
                    assistant_response_contradicts_guardian_completion(response)
                )
        for response in (
            "There are no remaining tests; the audit still needs work.",
            "Nothing remains pending, but verification will continue next.",
            "No work remains; the audit still needs work.",
            "Nothing remains to be completed; verification will continue next.",
            "\u6c92\u6709\u5269\u9918\u5de5\u4f5c\uff1b\u7a3d\u6838\u4ecd\u9700\u5b8c\u6210\u3002",
        ):
            with self.subTest(response=response):
                self.assertTrue(assistant_defers_unfinished_work(response))
        self.assertTrue(
            assistant_response_allows_terminal_settlement(
                "artifact \u8207\u654f\u611f\u8cc7\u6599\u908a\u754c\u7a3d\u6838\u5747\u5df2\u5b8c\u6210\uff1b106/106 \u6e2c\u8a66\u901a\u904e\u3002"
            )
        )

    def test_handoff_is_bounded_and_omits_block_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ACTIVE_STATE.md").write_text(
                "# \u72c0\u614b\n\n\u4e0b\u4e00\u6b65\uff1a\u9a57\u8b49 fresh thread\u3002\n",
                encoding="utf-8",
            )
            handoff = build_handoff(
                root,
                "RAW-USER-PAYLOAD-DO-NOT-CARRY " + "x" * 5000,
                "This content can't be shown",
                "test",
                resume_bundle=(
                    "# BOUNDED CONTEXT BUNDLE\n\n"
                    "## Active State\n\n\u4e0b\u4e00\u6b65\uff1a\u9a57\u8b49 fresh thread\u3002"
                ),
            )
        self.assertIn("--resume", handoff)
        self.assertIn("Do not replay its mutations", handoff)
        self.assertIn("\u4e0b\u4e00\u6b65\uff1a\u9a57\u8b49 fresh thread\u3002", handoff)
        self.assertNotIn("This content can't be shown", handoff)
        self.assertNotIn("RAW-USER-PAYLOAD-DO-NOT-CARRY", handoff)
        self.assertLess(len(handoff), 8000)

    def test_active_state_snapshot_is_bounded_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ACTIVE_STATE.md").write_text("\u7532" * 5000, encoding="utf-8")
            path, digest, content = active_state_snapshot(root)
        self.assertTrue(path.endswith("ACTIVE_STATE.md"))
        self.assertEqual(len(digest), 16)
        self.assertLess(len(content), 3700)
        self.assertIn("bounded handoff", content)

    def test_generic_retry_guards_side_effects_and_structured_policy(self) -> None:
        plain = TurnOutcome("This content can't be shown", None)
        from_error = TurnOutcome("", None, error_message="This content can't be shown")
        changed = TurnOutcome("This content can't be shown", None, side_effects=True)
        policy = TurnOutcome(
            "This content can't be shown",
            None,
            error_code="cyberPolicy",
        )
        policy_code_only = TurnOutcome(
            "",
            None,
            error_message="request unavailable",
            error_code="cyber-policy",
        )
        policy_message_only = TurnOutcome(
            "",
            None,
            error_message=(
                "This content was flagged for possible cybersecurity risk. "
                "If this seems wrong, try rephrasing your request."
            ),
        )
        self.assertTrue(outcome_has_generic_block(from_error))
        self.assertTrue(outcome_has_generic_block(policy_code_only))
        self.assertTrue(outcome_has_generic_block(policy_message_only))
        self.assertTrue(outcome_has_policy_boundary(policy_message_only))
        self.assertTrue(should_retry_generic(plain))
        self.assertTrue(should_retry_generic(from_error))
        self.assertFalse(should_retry_generic(changed))
        self.assertFalse(should_retry_generic(policy))
        self.assertFalse(should_retry_generic(policy_code_only))
        self.assertFalse(should_retry_generic(policy_message_only))
        partial_then_error = TurnOutcome(
            "A partial normal message",
            None,
            error_message="This content can't be shown",
        )
        self.assertTrue(outcome_has_generic_block(partial_then_error))

    def test_turn_error_details_accepts_root_or_string_code(self) -> None:
        self.assertEqual(
            turn_error_details(
                {
                    "message": "blocked",
                    "codexErrorInfo": {"root": "cyberPolicy"},
                }
            ),
            ("blocked", "cyberPolicy"),
        )
        self.assertEqual(
            turn_error_details(
                {"message": "full", "codexErrorInfo": "contextWindowExceeded"}
            ),
            ("full", "contextWindowExceeded"),
        )

    def test_run_turn_handles_completion_compaction_error_and_side_effects(self) -> None:
        outcome = run_with_events(
            [
                Event(
                    "item/started",
                    {"item": {"type": "commandExecution", "command": "echo ok"}},
                ),
                Event("thread/compacted", {"threadId": "thread-test", "turnId": "t"}),
                Event(
                    "item/completed",
                    {
                        "item": {
                            "type": "agentMessage",
                            "text": "This content can't be shown",
                            "phase": "final_answer",
                        }
                    },
                ),
                Event(
                    "turn/completed",
                    {
                        "turn": {
                            "id": "turn-completed-id",
                            "status": "failed",
                            "error": {
                                "message": "This content can't be shown",
                                "codexErrorInfo": "cyberPolicy",
                            },
                        }
                    },
                ),
            ]
        )
        self.assertTrue(outcome.compacted)
        self.assertTrue(outcome.side_effects)
        self.assertEqual(outcome.error_code, "cyberPolicy")
        self.assertEqual(outcome.error_message, "This content can't be shown")
        self.assertEqual(outcome.turn_id, "turn-completed-id")
        self.assertFalse(should_retry_generic(outcome))

    def test_run_turn_breaks_on_unknown_completion_payload(self) -> None:
        outcome = run_with_events([Event("turn/completed", {})])
        self.assertEqual(outcome.final_response, "")
        self.assertIn("unparseable", outcome.error_message or "")

    def test_completion_items_choose_last_unphased_message(self) -> None:
        outcome = run_with_events(
            [
                Event(
                    "turn/completed",
                    {
                        "turn": {
                            "status": "completed",
                            "items": [
                                {"type": "agentMessage", "text": "commentary"},
                                {"type": "agentMessage", "text": "final conclusion"},
                            ],
                        }
                    },
                )
            ]
        )
        self.assertEqual(outcome.final_response, "final conclusion")

    def test_read_only_command_is_replay_safe_and_guardian_signal_is_seen(self) -> None:
        read_item = {
            "type": "commandExecution",
            "command": "python C:/project/contextctl.py --root C:/project pulse --project demo",
            "commandActions": [{"type": "search", "query": "TODO"}],
            "aggregatedOutput": "CONTEXT_ROLLOVER_REQUIRED project=demo",
            "exitCode": 0,
        }
        self.assertFalse(item_may_have_side_effect(read_item))
        outcome = run_with_events(
            [
                Event("item/started", {"item": read_item}),
                Event("item/completed", {"item": read_item}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(outcome.side_effects)
        self.assertEqual(outcome.rollover_signal, "context-guardian rollover signal")

        searched = dict(read_item, command="rg CONTEXT_ROLLOVER_REQUIRED contextctl.py")
        searched_outcome = run_with_events(
            [
                Event("item/completed", {"item": searched}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertIsNone(searched_outcome.rollover_signal)

    def test_contextctl_detection_rejects_searches_and_quotes(self) -> None:
        self.assertEqual(
            contextctl_subcommands(
                "python C:/project/contextctl.py --root C:/project audit --project demo"
            ),
            frozenset({"audit"}),
        )
        self.assertEqual(
            contextctl_subcommands("rg 'finish|context session finished' contextctl.py"),
            frozenset(),
        )

    def test_declined_command_does_not_block_safe_generic_retry(self) -> None:
        started = {
            "id": "cmd-1",
            "type": "commandExecution",
            "command": "mutating command",
            "commandActions": [{"type": "unknown", "command": "mutating command"}],
            "status": "inProgress",
        }
        declined = dict(started, status="declined", exitCode=None)
        outcome = run_with_events(
            [
                Event("item/started", {"item": started}),
                Event("item/completed", {"item": declined}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": [declined]}},
                ),
            ]
        )
        self.assertFalse(outcome.side_effects)

        started_unknown = {
            "id": "cmd-2",
            "type": "commandExecution",
            "command": "rg TODO",
            "status": "inProgress",
        }
        completed_read = dict(
            started_unknown,
            status="completed",
            commandActions=[{"type": "search", "query": "TODO"}],
        )
        read_outcome = run_with_events(
            [
                Event("item/started", {"item": started_unknown}),
                Event("item/completed", {"item": completed_read}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": [completed_read]}},
                ),
            ]
        )
        self.assertFalse(read_outcome.side_effects)

    def test_guardian_checkpoint_and_audit_require_command_evidence(self) -> None:
        checkpoint = {
            "type": "commandExecution",
            "command": "python contextctl.py checkpoint --project demo --input candidate.json",
            "commandActions": [{"type": "unknown", "command": "checkpoint"}],
            "aggregatedOutput": "checkpointed demo; archived state.json",
            "exitCode": 0,
        }
        audit = {
            "type": "commandExecution",
            "command": (
                "python contextctl.py audit --project demo "
                "--task-id thread-test"
            ),
            "commandActions": [{"type": "read", "path": "state.json"}],
            "aggregatedOutput": (
                "Context audit: 1/1 projects pass.\n"
                "task audit recorded: demo task=thread-test\n"
            ),
            "exitCode": 0,
        }
        outcome = run_with_events(
            [
                Event("item/completed", {"item": checkpoint}),
                Event("item/completed", {"item": audit}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertTrue(outcome.guardian_checkpointed)
        self.assertTrue(outcome.guardian_audited)

        project_only_audit = dict(
            audit,
            command="python contextctl.py audit --project demo",
            aggregatedOutput="Context audit: 1/1 projects pass.\n",
        )
        project_only = run_with_events(
            [
                Event("item/completed", {"item": project_only_audit}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(project_only.guardian_audited)

    def test_contextctl_task_binding_requires_exact_task(self) -> None:
        command = (
            "python contextctl.py audit --project demo --task-id exact-task"
        )
        self.assertTrue(
            contextctl_command_binds_task(command, "audit", "exact-task")
        )
        self.assertFalse(
            contextctl_command_binds_task(command, "audit", "other-task")
        )

    def test_user_input_escape_requires_one_concrete_question(self) -> None:
        marker = "[CONTINUOUS_USER_INPUT_REQUIRED]"
        self.assertTrue(
            assistant_requires_user_input(
                marker + " Should the output be JSON or CSV?"
            )
        )
        for value in (
            marker,
            marker + " Work will continue.",
            marker + " ?",
            marker + " Choose JSON? " + marker,
            marker + " \u8981\u6211\u7e7c\u7e8c\u55ce\uff1f",
            marker + " Should I /new and continue?",
            "ordinary progress report",
        ):
            with self.subTest(value=value):
                self.assertFalse(assistant_requires_user_input(value))
                if marker in value:
                    self.assertTrue(
                        assistant_response_contradicts_guardian_completion(value)
                    )

    def test_guardian_finish_stdout_is_non_authoritative_and_receipt_wins(self) -> None:
        finish = {
            "id": "finish-1",
            "type": "commandExecution",
            "command": (
                "python contextctl.py --root C:/project finish --project demo "
                "--task-id thread-test"
            ),
            "commandActions": [{"type": "read", "path": "state.json"}],
            "aggregatedOutput": None,
            "exitCode": 0,
        }
        outcome = run_with_events(
            [
                Event(
                    "item/commandExecution/outputDelta",
                    {
                        "itemId": "finish-1",
                        "delta": "context session finished: demo task=thread-test",
                    },
                ),
                Event("item/completed", {"item": finish}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(outcome.guardian_finished)
        self.assertIsNone(outcome.guardian_finish_kind)

        completed = dict(
            finish,
            aggregatedOutput=(
                "context session finished: demo task=thread-test kind=completed"
            ),
        )
        completed_outcome = run_with_events(
            [
                Event("item/completed", {"item": completed}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(completed_outcome.guardian_finished)
        self.assertIsNone(completed_outcome.guardian_finish_kind)

        receipt_outcome = run_with_events(
            [
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                )
            ],
            guardian_receipt_value=guardian_lifecycle_receipt("thread-test"),
        )
        self.assertTrue(receipt_outcome.guardian_finished)
        self.assertEqual(receipt_outcome.guardian_finish_kind, "completed")

        retired = dict(
            finish,
            command=finish["command"] + " --replaced-by successor-task",
            aggregatedOutput=(
                "context session finished: demo task=thread-test kind=retired "
                "replacement=successor-task"
            ),
        )
        retired_outcome = run_with_events(
            [
                Event("item/completed", {"item": retired}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(retired_outcome.guardian_finished)
        self.assertIsNone(retired_outcome.guardian_finish_kind)
        retired_receipt_outcome = run_with_events(
            [
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                )
            ],
            guardian_receipt_value=guardian_lifecycle_receipt(
                "thread-test",
                kind="retired",
                replacement_task_id="successor-task",
            ),
        )
        self.assertTrue(retired_receipt_outcome.guardian_finished)
        self.assertEqual(retired_receipt_outcome.guardian_finish_kind, "retired")
        self.assertFalse(
            outcome_allows_terminal_settlement(
                TurnOutcome(
                    "objective complete",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="retired",
                )
            )
        )
        self.assertFalse(
            outcome_allows_terminal_settlement(
                TurnOutcome(
                    "objective complete",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind=None,
                )
            )
        )
        self.assertTrue(
            outcome_allows_terminal_settlement(
                TurnOutcome("unguarded objective complete", None)
            )
        )

        wrong_task = dict(
            finish,
            command=(
                "python contextctl.py --root C:/project finish --project demo "
                "--task-id other-task"
            ),
            aggregatedOutput=(
                "context session finished: demo task=other-task kind=completed"
            ),
        )
        wrong_outcome = run_with_events(
            [
                Event("item/completed", {"item": wrong_task}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(wrong_outcome.guardian_finished)

        failed = dict(finish, aggregatedOutput="context session finished: demo", exitCode=1)
        failed_outcome = run_with_events(
            [
                Event("item/completed", {"item": failed}),
                Event(
                    "turn/completed",
                    {"turn": {"status": "completed", "items": []}},
                ),
            ]
        )
        self.assertFalse(failed_outcome.guardian_finished)

    def test_finished_task_rollover_is_idle_and_sentinel_has_priority(self) -> None:
        calls: list[tuple[str, bool]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.prepare_rollover = lambda reason, checkpoint=True, **_: calls.append(
            (reason, checkpoint)
        )
        client._write_status = lambda _message, **_: None
        client._current_guardian_runtime_active = lambda: None
        client._current_completed_guardian_receipt = lambda: None

        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    "done",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                )
            )
        )
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    "[CONTINUOUS_USER_INPUT_REQUIRED] Which region should I use?",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                sentinel_has_priority=False,
            )
        )
        self.assertEqual(calls, [])

        completed_receipt = guardian_lifecycle_receipt("thread-test")
        client._current_completed_guardian_receipt = lambda: completed_receipt
        self.assertTrue(
            client._settle_finished_rollover(
                TurnOutcome(
                    "done",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                )
            )
        )
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    "[CONTINUOUS_USER_INPUT_REQUIRED] Which region should I use?",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                sentinel_has_priority=False,
            )
        )

        client._current_guardian_runtime_active = lambda: True
        client._current_completed_guardian_receipt = lambda: None
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    "wrong-task finish",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                )
            )
        )
        client._current_guardian_runtime_active = lambda: None
        client._current_completed_guardian_receipt = lambda: completed_receipt

        calls.clear()
        fake_success = (
            "\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5\u5c08\u6848\uff1b\u65b0 task \u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c\uff1a"
            "\u5148\u5b8c\u6210 manifest \u5951\u7d04\uff0c\u518d\u8655\u7406 verifier\u3002"
        )
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    fake_success,
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                sentinel_has_priority=False,
            )
        )
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    PREMATURE_FINISH_RESPONSE,
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                sentinel_has_priority=False,
            )
        )
        self.assertEqual(calls, [])

        calls.clear()
        self.assertFalse(
            client._settle_finished_rollover(
                TurnOutcome(
                    "done",
                    None,
                    rollover_signal="context-guardian rollover signal",
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                )
            )
        )
        self.assertEqual(calls, [])

        self.assertTrue(
            client._settle_finished_rollover(
                TurnOutcome(
                    "done",
                    None,
                    rollover_signal="context-guardian rollover signal",
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                sentinel_has_priority=False,
            )
        )
        self.assertEqual(calls, [])

    def test_automatic_rollover_stops_after_finished_continuation(self) -> None:
        continued: list[str] = []
        fresh: list[tuple[str, bool]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.rollover_ratio = 0.62
        client.continue_after_rollover = lambda reason: (
            continued.append(reason)
            or TurnOutcome(
                "done",
                None,
                rollover_signal="context-guardian rollover signal",
                guardian_finished=True,
                guardian_finish_kind="completed",
            )
        )
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "same-target-task"
        )
        client.prepare_rollover = lambda reason, checkpoint=True, **_: fresh.append(
            (reason, checkpoint)
        )
        client._write_status = lambda _message, **_: None

        client.run_automatic_rollovers("context-guardian rollover signal")

        self.assertEqual(continued, ["context-guardian rollover signal"])
        self.assertEqual(fresh, [])

    def test_automatic_rollover_drives_status_only_reply_until_real_work_finishes(self) -> None:
        fake_success = (
            "\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5\u5c08\u6848\uff1b\u65b0 task \u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c\uff1a"
            "\u5148\u5b8c\u6210 release_id manifest \u5951\u7d04\uff0c\u518d\u8655\u7406 verifier\u3002"
        )
        progress_prompts: list[tuple[str, str]] = []
        fresh: list[tuple[str, bool]] = []
        renderer, output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.rollover_ratio = 0.62
        client.pending_handoff = None
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="same-target-task")
        client.renderer = renderer
        client._output_lock = renderer.lock
        runtime_states = [True, True, True, False, False]
        client._current_guardian_runtime_active = lambda: runtime_states.pop(0)
        client.continue_after_rollover = lambda _reason: TurnOutcome(
            fake_success,
            None,
        )
        outcomes = iter(
            (
                TurnOutcome(
                    "manifest \u5951\u7d04\u5df2\u5b8c\u6210\uff1b\u4e0b\u4e00\u6b65\u5be6\u4f5c multi-root\u3002",
                    None,
                    side_effects=True,
                ),
                TurnOutcome(
                    "\u672c\u8f2a checkpoint \u5df2\u4fdd\u5b58\uff1bverifier \u5c1a\u5f85\u5b8c\u6210\u3002",
                    None,
                    guardian_checkpointed=True,
                    guardian_audited=True,
                ),
                TurnOutcome(
                    PREMATURE_FINISH_RESPONSE,
                    None,
                    side_effects=True,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
                TurnOutcome(
                    "manifest\u3001multi-root \u8207 verifier \u5df2\u5be6\u4f5c\u4e26\u9a57\u8b49\u5b8c\u6210\u3002",
                    None,
                    side_effects=True,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                ),
            )
        )

        def run_progress(prompt: str, *, stream_text: bool = True) -> TurnOutcome:
            progress_prompts.append((client.thread.id, prompt))
            return next(outcomes)

        client.run_turn = run_progress
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "same-target-task"
        )
        client.prepare_rollover = lambda reason, checkpoint=True, **_: fresh.append(
            (reason, checkpoint)
        )
        client._write_status = lambda _message, **_: None

        with patch(
            "continuous_cli.reopen_completed_guardian_session",
            return_value=(
                "# BOUNDED CONTEXT BUNDLE\n- Task ID: `same-target-task`\n"
            ),
        ) as reopened:
            client.run_automatic_rollovers("context 62%")
        reopened.assert_called_once()

        self.assertEqual(len(progress_prompts), 4)
        self.assertEqual(runtime_states, [])
        self.assertEqual(
            [thread_id for thread_id, _prompt in progress_prompts],
            ["same-target-task"] * 4,
        )
        self.assertIn(
            "only a rollover/handoff status report",
            progress_prompts[0][1],
        )
        self.assertIn(
            "Execute the first unresolved `next_action` now",
            progress_prompts[0][1],
        )
        self.assertIn("explicitly left required work unfinished", progress_prompts[1][1])
        self.assertIn("explicitly left required work unfinished", progress_prompts[2][1])
        self.assertIn("explicitly left required work unfinished", progress_prompts[3][1])
        self.assertIn("REOPENED GUARDIAN RUNTIME", progress_prompts[3][1])
        self.assertNotIn("run the required preflight", progress_prompts[3][1])
        self.assertEqual(fresh, [])
        rendered = output.getvalue()
        self.assertNotIn("\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5", rendered)
        self.assertNotIn("same-target-task", rendered)
        self.assertNotIn("checkpoint", rendered.casefold())
        self.assertNotIn(PREMATURE_FINISH_RESPONSE, rendered)
        self.assertIn("manifest\u3001multi-root \u8207 verifier \u5df2\u5be6\u4f5c\u4e26\u9a57\u8b49\u5b8c\u6210\u3002", rendered)

    def test_automatic_objective_recovers_transport_without_reprompt(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client._automatic_objective_active = False
        client._suppress_turn_output = False
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        recovered: list[str] = []
        published: list[TurnOutcome] = []

        def recover(detail: str) -> TurnOutcome:
            self.assertTrue(client._suppress_turn_output)
            recovered.append(detail)
            return TurnOutcome(
                "objective complete",
                None,
                guardian_finished=True,
                guardian_finish_kind="completed",
                tool_activity=True,
            )

        client.recover_transport_failure = recover
        client._current_guardian_runtime_active = lambda: False
        client._publish_automatic_terminal_response = published.append
        client._unguarded_objective_completion_proven = lambda _outcome: False

        client.run_automatic_objective(
            TurnOutcome(
                "",
                None,
                error_message="transport closed unexpectedly",
                error_code="streamDisconnected",
            )
        )

        self.assertEqual(recovered, ["transport closed unexpectedly"])
        self.assertEqual(len(published), 1)
        self.assertFalse(client._automatic_objective_active)
        self.assertFalse(client._suppress_turn_output)

    def test_contradictory_completed_receipt_reopens_before_second_live_turn(
        self,
    ) -> None:
        for contradictory in (
            PREMATURE_FINISH_RESPONSE,
            "[CONTINUOUS_USER_INPUT_REQUIRED] Which region should I use?",
        ):
            with self.subTest(contradictory=contradictory):
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="same-target-task")
                client.rollover_ratio = 0.62
                client.pending_handoff = None
                client._pending_guardian_finishes = []
                client._automatic_objective_active = False
                client._adopt_outcome = lambda _outcome: None
                client._write_status = lambda _message, **_kwargs: None
                client._current_guardian_runtime_active = lambda: True
                client._unguarded_objective_completion_proven = (
                    lambda _outcome: False
                )
                client._finish_guardian_sessions_after_dispatch = (
                    lambda _outcome: None
                )
                client._wait_for_pending_guardian_finishes = lambda _outcome: None
                published: list[TurnOutcome] = []
                client._publish_automatic_terminal_response = published.append

                stale = guardian_lifecycle_receipt("same-target-task")
                fresh = guardian_lifecycle_receipt("same-target-task")
                receipt_state: dict[str, dict[str, Any] | None] = {"value": stale}
                client._current_completed_guardian_receipt = (
                    lambda: receipt_state["value"]
                )
                order: list[str] = []

                def reopen(
                    project_root: Path,
                    task_id: str,
                    receipt: dict[str, Any],
                ) -> str:
                    self.assertEqual(project_root, Path("C:/project"))
                    self.assertEqual(task_id, "same-target-task")
                    self.assertIs(receipt, stale)
                    order.append("reopen")
                    receipt_state["value"] = None
                    return "# BOUNDED CONTEXT BUNDLE\n- Task ID: `same-target-task`\n"

                progress = 0

                def continue_work(
                    _outcome: TurnOutcome,
                    *,
                    sequence: int,
                ) -> TurnOutcome:
                    nonlocal progress
                    progress += 1
                    self.assertEqual(order[0], "reopen")
                    self.assertIsNone(receipt_state["value"])
                    self.assertIn(
                        "BOUNDED CONTEXT BUNDLE",
                        client._pending_reopened_guardian_bundle,
                    )
                    if progress == 1:
                        order.append("ordinary-turn")
                        return TurnOutcome(
                            "ordinary work completed; no fresh finish",
                            None,
                            side_effects=True,
                        )
                    self.assertEqual(sequence, 2)
                    order.append("fresh-finish")
                    receipt_state["value"] = fresh
                    return TurnOutcome(
                        "objective genuinely complete",
                        None,
                        guardian_finished=True,
                        guardian_finish_kind="completed",
                        side_effects=True,
                    )

                client.continue_active_objective = continue_work
                with patch(
                    "continuous_cli.reopen_completed_guardian_session",
                    side_effect=reopen,
                ) as reopened:
                    client.run_automatic_objective(
                        TurnOutcome(
                            contradictory,
                            None,
                            guardian_finished=True,
                            guardian_finish_kind="completed",
                            side_effects=True,
                        )
                    )

                reopened.assert_called_once()
                self.assertEqual(
                    order,
                    ["reopen", "ordinary-turn", "fresh-finish"],
                )
                self.assertEqual(
                    [item.final_response for item in published],
                    ["objective genuinely complete"],
                )

    def test_guarded_plaintext_user_marker_continues_without_reprompt(self) -> None:
        marker = (
            "[CONTINUOUS_USER_INPUT_REQUIRED] "
            "Which deployment region should I use?"
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client._automatic_objective_active = False
        client._adopt_outcome = lambda _outcome: None
        client._current_guardian_runtime_active = lambda: True
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        client._unguarded_objective_completion_proven = lambda _outcome: False
        client._finish_guardian_sessions_after_dispatch = lambda _outcome: None
        continued: list[str] = []
        published: list[TurnOutcome] = []
        client.continue_active_objective = lambda previous, **_kwargs: (
            continued.append(previous.final_response)
            or TurnOutcome(
                "objective complete",
                None,
                guardian_finished=True,
                guardian_finish_kind="completed",
            )
        )
        client._wait_for_pending_guardian_finishes = lambda _outcome: None
        client._publish_automatic_terminal_response = published.append

        client.run_automatic_objective(TurnOutcome(marker, None))

        self.assertEqual(continued, [marker])
        self.assertEqual(
            [outcome.final_response for outcome in published],
            ["objective complete"],
        )

    def test_automatic_completion_waits_for_queued_source_retirement(self) -> None:
        candidate = GuardianFinishCandidate(
            old_task_id="source-task",
            target_task_id="target-task",
            handoff_sha256="a" * 64,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="target-task")
        client._automatic_objective_active = False
        client._pending_guardian_finishes = [candidate]
        client._pending_guardian_finish_candidate = None
        client._active_rollover_journal = concrete_rollover_journal(
            candidate.old_task_id,
            candidate.target_task_id,
            candidate.handoff_sha256,
        )
        client._finish_active_rollover_journal = lambda: None
        client._adopt_outcome = lambda _outcome: None
        client._current_completed_guardian_receipt = lambda: guardian_lifecycle_receipt(
            "target-task"
        )
        client._current_guardian_runtime_active = lambda: False
        client._unguarded_objective_completion_proven = lambda _outcome: False
        published: list[TurnOutcome] = []
        client._publish_automatic_terminal_response = published.append
        client.continue_active_objective = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("source retirement retried through a model turn")
        )
        attempts: list[str] = []

        def finish(_root: Path, task_id: str, *, replaced_by: str) -> bool:
            attempts.append(f"{task_id}->{replaced_by}")
            if len(attempts) < 3:
                raise RuntimeError("temporary retirement failure")
            return True

        with (
            patch("continuous_cli.finish_guardian_session", side_effect=finish),
            patch("continuous_cli.time.sleep"),
        ):
            client.run_automatic_objective(
                TurnOutcome(
                    "objective complete",
                    None,
                    guardian_finished=True,
                    guardian_finish_kind="completed",
                    tool_activity=True,
                )
            )

        self.assertEqual(
            attempts,
            ["source-task->target-task"] * 3,
        )
        self.assertEqual(client._pending_guardian_finishes, [])
        self.assertEqual(len(published), 1)

    def test_automatic_rollover_treats_policy_boundary_as_terminal(self) -> None:
        continued: list[str] = []
        boundary = TurnOutcome(
            "",
            None,
            error_message=(
                "This content was flagged for possible cybersecurity risk. "
                "If this seems wrong, try rephrasing your request."
            ),
            error_code="cyber_policy",
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.rollover_ratio = 0.62
        client.continue_after_rollover = lambda reason: (
            continued.append(reason)
            or boundary
        )
        client.continue_nonreplayable = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("policy boundary was replayed through non-replayable recovery")
        )
        client.prepare_rollover = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("policy boundary created another thread")
        )
        client._continue_unresolved_guardian_target = lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("policy boundary repaired a Guardian target"))
        published: list[TurnOutcome] = []
        client._publish_policy_boundary_notice = published.append
        client._adopt_outcome = lambda _outcome: None

        client.run_automatic_rollovers("context 62%")

        self.assertEqual(continued, ["context 62%"])
        self.assertEqual(published, [boundary])

    def test_explicit_startup_resume_runs_before_user_input(self) -> None:
        prompts: list[str] = []
        stream_modes: list[bool] = []
        outcomes = iter(
            (
                TurnOutcome(
                    "\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5\uff1b\u65b0 task \u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c\uff1a\u5148\u5b8c\u6210 manifest\u3002",
                    None,
                ),
                TurnOutcome(
                    "manifest \u5df2\u5be6\u4f5c\u4e26\u9a57\u8b49\u3002 [CONTINUOUS_OBJECTIVE_COMPLETE]",
                    None,
                ),
            )
        )
        renderer, output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="normal-user-task")
        client.start_fresh_thread = lambda: None
        client._write_status = lambda _message, **_: None

        def run(prompt: str, *, stream_text: bool = True) -> TurnOutcome:
            prompts.append(prompt)
            stream_modes.append(stream_text)
            return next(outcomes)

        client.run_turn = run
        client.latest_user = ""
        client.latest_assistant = ""
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client.rollover_ratio = 0.62
        client.verbose = False
        client.renderer = renderer
        client._output_lock = renderer.lock
        client._render_epoch = 0
        client._line_open = False

        with patch("builtins.input", return_value="/exit") as mocked_input:
            self.assertEqual(client.interactive(resume_on_start=True), 0)

        self.assertEqual(len(prompts), 2)
        self.assertIn("context-guardian preflight with --resume", prompts[0])
        self.assertIn("AUTOMATIC OBJECTIVE PROGRESS TURN", prompts[1])
        self.assertEqual(stream_modes, [False, False])
        mocked_input.assert_called_once()
        self.assertNotIn("\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5", output.getvalue())
        self.assertIn("manifest \u5df2\u5be6\u4f5c\u4e26\u9a57\u8b49\u3002", output.getvalue())

    def test_explicit_startup_policy_boundary_publishes_once_without_recovery(
        self,
    ) -> None:
        notice = (
            "This content was flagged for possible cybersecurity risk. "
            "If this seems wrong, try rephrasing your request."
        )
        boundary = TurnOutcome(
            "",
            None,
            error_message=notice,
            error_code="cyber_policy",
        )
        prompts: list[str] = []
        starts: list[str] = []
        renderer, output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="startup-task")
        client._reconcile_rollover_journal_with_retry = lambda: "none"
        client.start_fresh_thread = lambda: starts.append("initial")
        client.run_turn = lambda prompt, *, stream_text=True: (
            prompts.append(prompt) or boundary
        )
        client.continue_nonreplayable = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup boundary was replayed")
        )
        client.prepare_rollover = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup boundary created another thread")
        )
        client._continue_unresolved_guardian_target = lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("startup boundary repaired a Guardian target"))
        client.latest_user = ""
        client.latest_assistant = ""
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client.rollover_ratio = 0.62
        client.verbose = False
        client._suppress_turn_output = False
        client.renderer = renderer
        client._output_lock = renderer.lock
        client._render_epoch = 0
        client._line_open = False

        with patch("builtins.input", return_value="/exit") as mocked_input:
            self.assertEqual(client.interactive(resume_on_start=True), 0)

        self.assertEqual(prompts, [STARTUP_RESUME_PROMPT])
        self.assertEqual(starts, ["initial"])
        self.assertEqual(output.getvalue().count(notice), 1)
        self.assertTrue(boundary.policy_notice_published)
        mocked_input.assert_called_once()

    def test_normal_user_turn_with_active_guardian_never_returns_to_prompt(self) -> None:
        prompts = iter(("implement the feature", "/exit"))
        continued: list[TurnOutcome] = []
        stream_modes: list[bool] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="normal-user-task")
        client.start_fresh_thread = lambda: None
        client._write_status = lambda _message, **_: None
        client.run_turn = lambda _prompt, *, stream_text=True: (
            stream_modes.append(stream_text)
            or TurnOutcome(
                "first work batch ended but objective remains active",
                None,
                side_effects=True,
            )
        )
        client.run_automatic_objective = continued.append
        client._current_guardian_runtime_active = lambda: True
        client.latest_user = ""
        client.latest_assistant = ""
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client.rollover_ratio = 0.62
        client.verbose = False
        renderer, _output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client.renderer = renderer
        client._output_lock = renderer.lock
        client._render_epoch = 0
        client._line_open = False

        with patch("builtins.input", side_effect=lambda *_args, **_kwargs: next(prompts)):
            self.assertEqual(client.interactive(), 0)

        self.assertEqual(len(continued), 1)
        self.assertEqual(stream_modes, [False])
        self.assertEqual(
            continued[0].final_response,
            "first work batch ended but objective remains active",
        )

    def test_premature_finish_on_normal_turn_routes_to_automatic_continuation(self) -> None:
        prompts = iter(("finish the audit", "/exit"))
        continued: list[TurnOutcome] = []
        stream_modes: list[bool] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="normal-user-task")
        client.start_fresh_thread = lambda: None
        client._write_status = lambda _message, **_: None
        client.run_turn = lambda _prompt, *, stream_text=True: (
            stream_modes.append(stream_text)
            or TurnOutcome(
                PREMATURE_FINISH_RESPONSE,
                None,
                side_effects=True,
                guardian_finished=True,
                guardian_finish_kind="completed",
            )
        )
        client.run_automatic_objective = continued.append
        client._current_guardian_runtime_active = lambda: True
        client.latest_user = ""
        client.latest_assistant = ""
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client.rollover_ratio = 0.62
        client.verbose = False
        renderer, output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client.renderer = renderer
        client._output_lock = renderer.lock
        client._render_epoch = 0
        client._line_open = False

        with patch("builtins.input", side_effect=lambda *_args, **_kwargs: next(prompts)):
            self.assertEqual(client.interactive(), 0)

        self.assertEqual(stream_modes, [False])
        self.assertEqual([item.final_response for item in continued], [PREMATURE_FINISH_RESPONSE])
        self.assertNotIn(PREMATURE_FINISH_RESPONSE, output.getvalue())

    def test_tool_events_are_quiet_by_default_and_visible_on_demand(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client._output_lock = threading.RLock()
        item = {
            "type": "commandExecution",
            "command": "python -m unittest",
            "exitCode": 0,
        }
        payload = DumpPayload({"item": item})

        client.verbose = False
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            self.assertEqual(client._show_item_started(payload), "commandExecution")
            client._show_item_completed(item)
        self.assertEqual(quiet.getvalue(), "")

        client.verbose = True
        detailed = io.StringIO()
        with contextlib.redirect_stdout(detailed):
            client._show_item_started(payload)
            client._show_item_completed(item)
        self.assertIn("tool: commandExecution", detailed.getvalue())
        self.assertIn("done: commandExecution", detailed.getvalue())

    def test_controller_turn_suppresses_all_tool_rows_even_when_verbose(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.verbose = True
        client._suppress_turn_output = False
        client._output_lock = threading.RLock()
        item = {
            "type": "commandExecution",
            "command": "python contextctl.py checkpoint",
            "exitCode": 1,
        }
        payload = DumpPayload({"item": item})

        def run(_prompt: str, *, stream_text: bool = True) -> TurnOutcome:
            self.assertFalse(stream_text)
            self.assertTrue(client._suppress_turn_output)
            client._show_item_started(payload)
            client._show_item_completed(item)
            return TurnOutcome("internal receipt", None, tool_activity=True)

        client.run_turn = run
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            outcome = client._run_controller_turn_silently("internal")

        self.assertEqual(outcome.final_response, "internal receipt")
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(client.verbose)
        self.assertFalse(client._suppress_turn_output)

    def test_verbose_only_status_is_suppressed_in_clean_mode(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client._output_lock = threading.RLock()
        client.verbose = False
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            client._write_status("hidden", verbose_only=True)
            client._write_status("shown")
        self.assertNotIn("hidden", output.getvalue())
        self.assertIn("System: shown", output.getvalue())

    def test_multiple_agent_items_share_one_clean_codex_label(self) -> None:
        events = [
            Event("item/agentMessage/delta", {"itemId": "a", "delta": "\u5148\u8655\u7406\u3002"}),
            Event("item/agentMessage/delta", {"itemId": "b", "delta": "\u5df2\u5b8c\u6210\u3002"}),
            Event(
                "turn/completed",
                {"turn": {"status": "completed", "items": []}},
            ),
        ]
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = FakeThread(events)
        client.verbose = False
        client._render_epoch = 0
        client._output_lock = threading.RLock()
        client._compacted_threads = set()
        client._service_pending_input = lambda _timeout=0.1: None
        client.client = SimpleNamespace(close=lambda: None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            client.run_turn("test", stream_text=True)

        self.assertEqual(output.getvalue().count("Codex: "), 1)
        self.assertIn("\u5148\u8655\u7406\u3002", output.getvalue())
        self.assertIn("\u5df2\u5b8c\u6210\u3002", output.getvalue())

    def test_run_turn_routes_tool_events_to_verbose_only(self) -> None:
        item = {
            "id": "cmd-ui",
            "type": "commandExecution",
            "command": "echo clean",
            "commandActions": [{"type": "read", "path": "state.json"}],
            "exitCode": 0,
        }
        events = [
            Event("item/started", {"item": item}),
            Event("item/completed", {"item": item}),
            Event("item/agentMessage/delta", {"itemId": "answer", "delta": "\u5b8c\u6210"}),
            Event(
                "turn/completed",
                {"turn": {"status": "completed", "items": []}},
            ),
        ]

        def captured(verbose: bool) -> str:
            client = ContinuousCodex.__new__(ContinuousCodex)
            client.thread = FakeThread(events)
            client.verbose = verbose
            client._render_epoch = 0
            client._line_open = False
            client._output_lock = threading.RLock()
            client._compacted_threads = set()
            client._service_pending_input = lambda _timeout=0.1: None
            client.client = SimpleNamespace(close=lambda: None)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                client.run_turn("test", stream_text=True)
            return output.getvalue()

        quiet = captured(False)
        self.assertIn("Codex: \u5b8c\u6210", quiet)
        self.assertNotIn("tool:", quiet)
        self.assertNotIn("done:", quiet)

        detailed = captured(True)
        self.assertIn("tool: commandExecution", detailed)
        self.assertIn("done: commandExecution", detailed)
        self.assertIn("Codex: \u5b8c\u6210", detailed)

    def test_verbose_slash_command_toggles_details_without_a_turn(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.start_fresh_thread = lambda: None
        client.verbose = False
        client._output_lock = threading.RLock()
        client._render_epoch = 0
        client._line_open = False
        output = io.StringIO()
        with patch("builtins.input", side_effect=["/verbose", "/exit"]):
            with contextlib.redirect_stdout(output):
                self.assertEqual(client.interactive(), 0)

        self.assertTrue(client.verbose)
        self.assertIn("\u5de5\u5177\u7d30\u7bc0\uff1a\u958b", output.getvalue())

    def test_unknown_notifications_preserve_delta_and_usage(self) -> None:
        events = [
            SimpleNamespace(
                method="item/agentMessage/delta",
                payload=SimpleNamespace(params={"itemId": "a", "delta": "raw final"}),
            ),
            SimpleNamespace(
                method="thread/tokenUsage/updated",
                payload=SimpleNamespace(
                    params={
                        "tokenUsage": {
                            "last": {"inputTokens": 700},
                            "modelContextWindow": 1000,
                        }
                    }
                ),
            ),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(
                    params={"turn": {"status": "completed", "items": []}}
                ),
            ),
        ]
        outcome = run_with_events(events)
        self.assertEqual(outcome.final_response, "raw final")
        self.assertEqual(usage_input_and_window(outcome.usage), (700, 1000))

    def test_start_thread_inherits_approval_and_sandbox_config(self) -> None:
        captured: dict[str, Any] = {}

        ids = iter(("fresh", "fresher"))
        requests: list[tuple[str, dict[str, Any]]] = []

        class FakeStarted:
            def __init__(self, thread_id: str) -> None:
                self.thread = SimpleNamespace(id=thread_id)

        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.model = None
        client.previous_input_tokens = 99
        client._ThreadUnsubscribeResponse = object
        client.client = SimpleNamespace(
            thread_start=lambda params: captured.update(params) or FakeStarted(next(ids)),
            request=lambda method, params, **_: requests.append((method, params)),
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client.start_fresh_thread()
        self.assertNotIn("approvalPolicy", captured)
        self.assertNotIn("sandbox", captured)
        self.assertEqual(client.thread.id, "fresh")
        self.assertIsNone(client.previous_input_tokens)
        client.start_fresh_thread()
        self.assertEqual(client.thread.id, "fresher")
        self.assertEqual(
            requests,
            [("thread/unsubscribe", {"threadId": "fresh"})],
        )

    def test_start_fresh_thread_rejects_missing_or_reused_target_id(self) -> None:
        for target_id, message in (
            (None, "did not return a target thread id"),
            ("   ", "did not return a target thread id"),
            ("old-task", "did not create a fresh thread"),
        ):
            with self.subTest(target_id=target_id):
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="old-task")
                client.model = None
                client.previous_input_tokens = 99
                started = SimpleNamespace(thread=SimpleNamespace(id=target_id))
                client.client = SimpleNamespace(thread_start=lambda _params: started)
                client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)

                with self.assertRaisesRegex(RuntimeError, message):
                    client.start_fresh_thread()

                self.assertEqual(client.thread.id, "old-task")
                self.assertEqual(client.previous_input_tokens, 99)

    def test_automatic_clear_resets_view_and_marks_app_server_thread_source(self) -> None:
        events: list[tuple[Any, ...]] = []
        renderer = SimpleNamespace(
            reset_conversation_view=lambda *, clear_input_history: events.append(
                ("reset", clear_input_history)
            ),
            clear_screen=lambda: events.append(("clear",)),
            line_open=False,
            epoch=0,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.renderer = renderer
        client._output_lock = threading.RLock()
        client._sync_render_state = lambda: None
        client.start_fresh_thread = lambda **kwargs: events.append(
            ("start", kwargs.get("session_start_source"))
        )

        client.start_cleared_thread()

        self.assertEqual(
            events,
            [("reset", False), ("clear",), ("start", "clear")],
        )

    def test_fresh_thread_preserves_model_effort_and_fast_tier(self) -> None:
        captured: list[dict[str, Any]] = []
        responses = iter(
            [
                SimpleNamespace(
                    thread=SimpleNamespace(id="fresh"),
                    model="gpt-5.6-luna",
                    reasoning_effort="medium",
                    service_tier="default",
                ),
                SimpleNamespace(
                    thread=SimpleNamespace(id="fresher"),
                    model="gpt-5.6-luna",
                    reasoning_effort="medium",
                    service_tier="default",
                ),
            ]
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = None
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "priority"
        client.personality = "pragmatic"
        client.active_permission_profile = ":read-only"
        client.previous_input_tokens = 99
        client.previous_context_window = 100
        client.client = SimpleNamespace(
            thread_start=lambda params: captured.append(dict(params)) or next(responses),
            request=lambda *_args, **_kwargs: object(),
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._ThreadUnsubscribeResponse = object

        client.start_fresh_thread()

        self.assertEqual(captured[0]["model"], "gpt-5.6-sol")
        self.assertEqual(
            captured[0]["config"],
            {
                "model_reasoning_effort": "xhigh",
                "default_permissions": ":read-only",
            },
        )
        self.assertEqual(captured[0]["serviceTier"], "priority")
        self.assertEqual(captured[0]["personality"], "pragmatic")
        self.assertEqual((client.model, client.reasoning_effort), ("gpt-5.6-luna", "medium"))
        client.start_fresh_thread()
        self.assertEqual(captured[1]["model"], "gpt-5.6-luna")
        self.assertEqual(
            captured[1]["config"],
            {
                "model_reasoning_effort": "medium",
                "default_permissions": ":read-only",
            },
        )
        self.assertEqual(captured[1]["serviceTier"], "default")
        self.assertIsNone(client.previous_input_tokens)
        self.assertIsNone(client.previous_context_window)

    def test_approval_handler_accepts_in_scope_runtime_requests(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        self.assertEqual(
            client._handle_server_request(
                "item/commandExecution/requestApproval",
                {"command": "build"},
            ),
            {"decision": "accept"},
        )
        permissions = {"network": {"enabled": True}}
        self.assertEqual(
            client._handle_server_request(
                "item/permissions/requestApproval",
                {"permissions": permissions},
            ),
            {"permissions": permissions, "scope": "turn"},
        )
        client.active_permission_profile = ":read-only"
        self.assertEqual(
            client._handle_server_request(
                "item/commandExecution/requestApproval",
                {"command": "write"},
            ),
            {"decision": "decline"},
        )
        self.assertEqual(
            client._handle_server_request(
                "item/permissions/requestApproval",
                {"permissions": permissions},
            ),
            {"permissions": {}, "scope": "turn"},
        )

    def test_continue_after_rollover_runs_without_new_user_input(self) -> None:
        captured: dict[str, Any] = {}
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.pending_handoff = None

        def prepare(reason: str, *, checkpoint: bool = True) -> None:
            captured["reason"] = reason
            captured["checkpoint"] = checkpoint
            client.pending_handoff = "validated handoff"

        def run(prompt: str, *, stream_text: bool = True) -> TurnOutcome:
            captured["prompt"] = prompt
            captured["stream_text"] = stream_text
            return TurnOutcome("continued", None, tool_activity=True)

        client.prepare_rollover = prepare
        client.run_turn = run
        outcome = client.continue_after_rollover("context 62%")
        self.assertEqual(outcome.final_response, "continued")
        self.assertTrue(captured["checkpoint"])
        self.assertIn("AUTOMATIC CONTINUATION", captured["prompt"])
        self.assertFalse(captured["stream_text"])
        self.assertIsNone(client.pending_handoff)

    def test_status_only_handoff_does_not_authorize_old_finish(self) -> None:
        handoff = "validated handoff"
        fake_success = (
            "\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5\u5c08\u6848\uff1b\u65b0 task \u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c\uff1a"
            "\u5148\u5b8c\u6210 manifest\uff0c\u518d\u8655\u7406 verifier\u3002"
        )
        finished: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="new-task")
        client.pending_handoff = handoff
        client._pending_guardian_finishes = []
        client._pending_guardian_finish_candidate = SimpleNamespace(
            old_task_id="old-task",
            target_task_id="new-task",
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
        )
        client._active_rollover_journal = replace(
            concrete_rollover_journal(
                "old-task",
                "new-task",
                hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            ),
            phase="dispatch_started",
        )
        install_memory_rollover_transitions(client)
        client._write_status = lambda _message, **_: None

        with patch(
            "continuous_cli.finish_guardian_session",
            side_effect=lambda _project, task_id, *, replaced_by: (
                finished.append(f"{task_id}->{replaced_by}") or True
            ),
        ):
            for nonterminal in (fake_success, PREMATURE_FINISH_RESPONSE):
                client._complete_handoff_dispatch(
                    TurnOutcome(
                        nonterminal,
                        None,
                        guardian_finished=True,
                        tool_activity=True,
                    ),
                    dispatched_handoff=handoff,
                )
                self.assertEqual(finished, [])
                self.assertEqual(client.pending_handoff, handoff)
                self.assertIsNotNone(client._pending_guardian_finish_candidate)

            client._complete_handoff_dispatch(
                TurnOutcome("manifest implementation started", None, side_effects=True),
                dispatched_handoff=handoff,
            )

        self.assertEqual(finished, ["old-task->new-task"])
        self.assertIsNone(client.pending_handoff)
        self.assertIsNone(client._pending_guardian_finish_candidate)

    def test_guardian_rollover_orders_new_preflight_dispatch_then_old_finish(self) -> None:
        old_task = "019-old-task"
        new_task = "019-new-task"
        events: list[tuple[Any, ...]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id=old_task)
        client.latest_user = "continue the unfinished objective"
        client.latest_assistant = "checkpoint ready"
        client.pending_handoff = None
        client._pending_guardian_finishes = []
        client._rewind_sources = []
        client.rollovers = 0
        client.verbose = False
        client._write_status = lambda _message, **_: None
        guardian_target = GuardianTarget(
            workspace_root=Path("C:/workspace"),
            project_id="demo",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        client._checkpoint_for_rollover = lambda _reason: (
            events.append(("checkpoint", client.thread.id)) or True
        )

        def prepare_journal(task_id: str) -> RolloverJournal:
            return replace(
                concrete_rollover_journal(
                    task_id,
                    new_task,
                    "0" * 64,
                ),
                phase="prepared",
                target_task_id=None,
                target_state_sha256=None,
                target_rules_sha256=None,
                handoff_sha256=None,
            )

        def record_target(
            journal: RolloverJournal,
            task_id: str,
            handoff: str,
        ) -> RolloverJournal:
            target_journal = replace(
                journal,
                phase="target_created",
                target_task_id=task_id,
                target_state_sha256="a" * 64,
                target_rules_sha256="b" * 64,
                handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            )
            client._active_rollover_journal = target_journal
            return target_journal

        client._prepare_rollover_journal = prepare_journal
        client._record_rollover_target = record_target
        install_memory_rollover_transitions(client)

        def start(*, session_start_source: str | None = None) -> None:
            events.append(("start", new_task, session_start_source))
            client.thread = SimpleNamespace(id=new_task)

        def preflight(
            _project: Path,
            task_id: str,
            *,
            reject_task_id: str | None = None,
        ) -> str:
            events.append(("preflight", task_id, reject_task_id))
            return f"# BOUNDED CONTEXT BUNDLE\n\n- Task ID: `{task_id}`\n"

        def dispatch(prompt: str, *, stream_text: bool = True) -> TurnOutcome:
            events.append(("dispatch", client.thread.id, prompt, stream_text))
            return TurnOutcome("continued successfully", None, tool_activity=True)

        def finish(_project: Path, task_id: str, *, replaced_by: str) -> bool:
            events.append(("finish", task_id, replaced_by))
            return True

        client.start_fresh_thread = start
        client.run_turn = dispatch
        with (
            patch(
                "continuous_cli.discover_guardian_target",
                return_value=guardian_target,
            ),
            patch("continuous_cli.validated_guardian_bundle", side_effect=preflight),
            patch("continuous_cli.finish_guardian_session", side_effect=finish),
        ):
            outcome = client.continue_after_rollover(
                f"CONTEXT_ROLLOVER_REQUIRED task={old_task}"
            )

        self.assertEqual(outcome.final_response, "continued successfully")
        self.assertEqual(
            [event[0] for event in events],
            ["checkpoint", "start", "preflight", "dispatch", "finish"],
        )
        self.assertEqual(events[0][1], old_task)
        self.assertEqual(events[1][2], "clear")
        self.assertEqual(events[2][1:], (new_task, old_task))
        self.assertEqual(events[-1][1], old_task)
        self.assertEqual(events[-1][2], new_task)
        continuation = events[3][2]
        self.assertIn(f"Task ID: `{new_task}`", continuation)
        self.assertNotIn(old_task, continuation)
        self.assertNotIn("CONTEXT_ROLLOVER_REQUIRED", continuation)
        self.assertEqual(client._pending_guardian_finishes, [])

    def test_guardian_checkpoint_failure_never_creates_target(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="old-task")
        client.latest_user = "continue"
        client.latest_assistant = "working"
        client.pending_handoff = None
        client._pending_guardian_finish_candidate = None
        client._pending_guardian_finishes = []
        client._rewind_sources = []
        client.rollovers = 0
        client._checkpoint_for_rollover = lambda _reason: False
        created: list[bool] = []
        client.start_fresh_thread = lambda **_kwargs: created.append(True)

        with patch("continuous_cli.discover_guardian_target", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "checkpoint/audit failed"):
                client.prepare_rollover("context full")

        self.assertEqual(created, [])
        self.assertEqual(client.thread.id, "old-task")
        self.assertIsNone(client.pending_handoff)

    def test_transport_recovery_uses_guardian_replacement_transaction(self) -> None:
        events: list[tuple[Any, ...]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="transport-old")
        client.latest_user = "implement the objective"
        client.latest_assistant = "partial work"
        client.pending_handoff = None
        client._pending_guardian_finish_candidate = None
        client._pending_guardian_finishes = []
        client._rewind_sources = []
        client._suppress_turn_output = False

        def restart() -> None:
            events.append(("restart",))
            client.thread = SimpleNamespace(id="transport-new")

        def dispatch(prompt: str, *, handle: Any | None = None) -> TurnOutcome:
            del handle
            events.append(("dispatch", prompt))
            candidate = client._pending_guardian_finish_candidate
            client._active_rollover_journal = replace(
                concrete_rollover_journal(
                    candidate.old_task_id,
                    candidate.target_task_id,
                    candidate.handoff_sha256,
                ),
                phase="dispatch_started",
            )
            return TurnOutcome("real work completed", None, tool_activity=True)

        client._restart_app_server = restart
        client._run_controller_turn_silently = dispatch
        install_memory_rollover_transitions(client)

        with (
            patch(
                "continuous_cli.prepare_guardian_source_for_replacement",
                side_effect=lambda _root, task_id: (
                    events.append(("source-audit", task_id)) or True
                ),
            ),
            patch(
                "continuous_cli.validated_guardian_bundle",
                side_effect=lambda _root, task_id, *, reject_task_id=None: (
                    events.append(("target-preflight", task_id, reject_task_id))
                    or f"# BOUNDED CONTEXT BUNDLE\n- Task ID: `{task_id}`\n"
                ),
            ),
            patch(
                "continuous_cli.build_handoff",
                return_value=(
                    "source=transport-old CONTEXT_ROLLOVER_REQUIRED\n"
                    "validated handoff"
                ),
            ),
            patch(
                "continuous_cli.finish_guardian_session",
                side_effect=lambda _root, task_id, *, replaced_by=None: (
                    events.append(("finish", task_id, replaced_by)) or True
                ),
            ),
        ):
            outcome = client.recover_transport_failure("connection reset")

        self.assertEqual(outcome.final_response, "real work completed")
        self.assertEqual(
            [event[0] for event in events],
            ["source-audit", "restart", "target-preflight", "dispatch", "finish"],
        )
        self.assertEqual(events[-1], ("finish", "transport-old", "transport-new"))
        dispatched = events[3][1]
        self.assertNotIn("transport-old", dispatched)
        self.assertNotIn("CONTEXT_ROLLOVER_REQUIRED", dispatched)
        self.assertEqual(client._pending_guardian_finishes, [])
        self.assertFalse(client._suppress_turn_output)

    def test_prepare_transport_source_refreshes_then_task_audits(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="demo",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        completed = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch(
                "continuous_cli.subprocess.run",
                side_effect=[completed, completed],
            ) as run,
        ):
            self.assertTrue(
                prepare_guardian_source_for_replacement(
                    Path("C:/project"),
                    "source-task",
                )
            )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("refresh", commands[0])
        self.assertIn("audit", commands[1])
        for command in commands:
            self.assertEqual(
                command[command.index("--task-id") + 1],
                "source-task",
            )

    def test_normal_user_objective_preflights_exact_task_without_resume(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="demo",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        runtime = Path("C:/workspace/demo/.context/runtime/user-task.json")
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="# BOUNDED CONTEXT BUNDLE\n- Task ID: `user-task`\n",
            stderr="",
        )
        with (
            patch(
                "continuous_cli.guardian_runtime_location",
                return_value=(target, runtime),
            ),
            patch("continuous_cli.subprocess.run", return_value=completed) as run,
            patch("pathlib.Path.is_file", return_value=False),
        ):
            bundle = prepare_guardian_user_objective(
                Path("C:/workspace/demo"),
                "user-task",
            )

        self.assertIn("Task ID: `user-task`", bundle or "")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--task-id") + 1], "user-task")
        self.assertNotIn("--resume", command)

    def test_normal_guardian_setup_retries_transient_preflight_and_runtime_reads(
        self,
    ) -> None:
        bundle = "# BOUNDED CONTEXT BUNDLE\n- Task ID: `user-task`\n"
        cases = (
            (
                [RuntimeError("contextctl temporarily unavailable"), bundle],
                [True],
                2,
            ),
            (
                [bundle, None],
                [RuntimeError("runtime read raced with replace"), True],
                2,
            ),
        )
        for preflight_results, runtime_results, expected_calls in cases:
            with self.subTest(runtime_results=runtime_results):
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="user-task")
                client._current_guardian_runtime_active = Mock(
                    side_effect=runtime_results
                )
                with (
                    patch(
                        "continuous_cli.prepare_guardian_user_objective",
                        side_effect=preflight_results,
                    ) as preflight,
                    patch("continuous_cli.time.sleep"),
                ):
                    prompt, buffered = client._prepare_normal_guardian_user_turn(
                        "implement the feature"
                    )

                self.assertTrue(buffered)
                self.assertIn(bundle.strip(), prompt)
                self.assertIn("implement the feature", prompt)
                self.assertEqual(preflight.call_count, expected_calls)

    def test_unresolved_guardian_candidate_repairs_exact_target_for_blocks(self) -> None:
        modes = ("generic", "context")
        for mode in modes:
            with self.subTest(mode=mode):
                handoff = "validated exact handoff"
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="target-task")
                client.pending_handoff = handoff
                client._pending_guardian_finishes = []
                client._pending_guardian_finish_candidate = GuardianFinishCandidate(
                    old_task_id="source-task",
                    target_task_id="target-task",
                    handoff_sha256=hashlib.sha256(
                        handoff.encode("utf-8")
                    ).hexdigest(),
                )
                client._active_rollover_journal = replace(
                    concrete_rollover_journal(
                        "source-task",
                        "target-task",
                        hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
                    ),
                    phase="dispatch_started",
                )
                install_memory_rollover_transitions(client)
                client.latest_user = "original request"
                prompts: list[str] = []
                client._run_controller_turn_silently = lambda prompt: (
                    prompts.append(prompt)
                    or TurnOutcome("concrete repair work", None, tool_activity=True)
                )
                client.prepare_rollover = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("unresolved target created another task")
                )

                with patch(
                    "continuous_cli.finish_guardian_session",
                    return_value=True,
                ) as finish:
                    if mode == "generic":
                        outcome = client.recover_generic_block()
                    else:
                        outcome = client.continue_nonreplayable(
                            f"{mode} recovery",
                        )

                self.assertEqual(outcome.final_response, "concrete repair work")
                self.assertEqual(client.thread.id, "target-task")
                self.assertEqual(len(prompts), 1)
                self.assertIn("Stay in this exact current task", prompts[0])
                self.assertIsNone(client.pending_handoff)
                self.assertIsNone(client._pending_guardian_finish_candidate)
                self.assertEqual(client._pending_guardian_finishes, [])
                finish.assert_called_once_with(
                    Path("C:/project"),
                    "source-task",
                    replaced_by="target-task",
                )

    def test_policy_boundary_leaves_unresolved_guardian_target_untouched(self) -> None:
        handoff = "validated exact handoff"
        boundary = TurnOutcome(
            "",
            None,
            error_message=(
                "This content was flagged for possible cybersecurity risk. "
                "If this seems wrong, try rephrasing your request."
            ),
            error_code="cyber_policy",
        )
        candidate = GuardianFinishCandidate(
            old_task_id="source-task",
            target_task_id="target-task",
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="target-task")
        client.pending_handoff = handoff
        client._pending_guardian_finish_candidate = candidate
        client._adopt_outcome = lambda _outcome: None
        published: list[TurnOutcome] = []
        client._publish_policy_boundary_notice = published.append
        repair_attempts: list[str] = []
        client._continue_unresolved_guardian_target = lambda reason: (
            repair_attempts.append(reason) or boundary
        )
        client.prepare_rollover = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("policy boundary created another target")
        )

        client.run_automatic_rollovers("unresolved target resume")

        self.assertEqual(repair_attempts, ["unresolved target resume"])
        self.assertEqual(published, [boundary])
        self.assertIs(client._pending_guardian_finish_candidate, candidate)
        self.assertEqual(client.pending_handoff, handoff)

    def test_unresolved_guardian_transport_resumes_exact_target(self) -> None:
        handoff = "validated exact handoff"
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="target-task")
        client.pending_handoff = handoff
        client._pending_guardian_finishes = []
        client._pending_guardian_finish_candidate = GuardianFinishCandidate(
            old_task_id="source-task",
            target_task_id="target-task",
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
        )
        client._active_rollover_journal = replace(
            concrete_rollover_journal(
                "source-task",
                "target-task",
                hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            ),
            phase="dispatch_started",
        )
        install_memory_rollover_transitions(client)
        client._suppress_turn_output = False
        resumed: list[str | None] = []
        client._restart_app_server = lambda *, resume_thread_id=None: resumed.append(
            resume_thread_id
        )
        client._run_controller_turn_silently = lambda _prompt: TurnOutcome(
            "repair work began",
            None,
            tool_activity=True,
        )

        with (
            patch(
                "continuous_cli.prepare_guardian_source_for_replacement",
                side_effect=AssertionError("source replacement must not run"),
            ),
            patch("continuous_cli.finish_guardian_session", return_value=True),
        ):
            outcome = client.recover_transport_failure("stream disconnected")

        self.assertEqual(outcome.final_response, "repair work began")
        self.assertEqual(resumed, ["target-task"])
        self.assertEqual(client.thread.id, "target-task")
        self.assertIsNone(client._pending_guardian_finish_candidate)
        self.assertEqual(client._pending_guardian_finishes, [])

    def test_repeated_nonreplayable_result_repairs_candidate_without_new_target(
        self,
    ) -> None:
        handoff = "validated exact handoff"
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="target-task")
        client.pending_handoff = handoff
        client._pending_guardian_finishes = []
        client._pending_guardian_finish_candidate = GuardianFinishCandidate(
            old_task_id="source-task",
            target_task_id="target-task",
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
        )
        client._active_rollover_journal = replace(
            concrete_rollover_journal(
                "source-task",
                "target-task",
                hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            ),
            phase="dispatch_started",
        )
        install_memory_rollover_transitions(client)
        client._adopt_outcome = lambda _outcome: None
        client._run_controller_turn_silently = lambda _prompt: TurnOutcome(
            "concrete repair",
            None,
            tool_activity=True,
        )
        client.prepare_rollover = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("repeated block created another target")
        )
        automatic: list[TurnOutcome] = []
        client.run_automatic_objective = automatic.append

        with patch("continuous_cli.finish_guardian_session", return_value=True):
            client._settle_nonreplayable_outcome(
                TurnOutcome("This content can't be shown", None)
            )

        self.assertEqual(client.thread.id, "target-task")
        self.assertEqual(len(automatic), 1)
        self.assertEqual(automatic[0].final_response, "concrete repair")
        self.assertIsNone(client._pending_guardian_finish_candidate)

    def test_failed_target_restore_handles_lightweight_client_and_clears_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "target-task.json"
            runtime.write_text("{}", encoding="utf-8")
            source = SimpleNamespace(id="source-task")
            client = ContinuousCodex.__new__(ContinuousCodex)
            client.project_root = Path(temp_dir)
            client.thread = SimpleNamespace(id="target-task")
            client.pending_handoff = "handoff"
            client._pending_guardian_finish_candidate = object()
            client.client = SimpleNamespace(
                thread_resume=lambda task_id, _params: SimpleNamespace(
                    thread=SimpleNamespace(id=task_id)
                )
            )
            discarded: list[str] = []
            client._unsubscribe_thread = lambda _task_id: None
            client._discard_thread_runtime_state = discarded.append

            with patch(
                "continuous_cli.guardian_runtime_location",
                return_value=(object(), runtime),
            ):
                client._restore_source_after_failed_target(
                    source,
                    "target-task",
                )

            self.assertIs(client.thread, source)
            self.assertIsNone(client.pending_handoff)
            self.assertIsNone(client._pending_guardian_finish_candidate)
            self.assertEqual(discarded, ["target-task"])
            self.assertFalse(runtime.exists())

    def test_prose_only_target_turn_cannot_retire_source(self) -> None:
        handoff = "validated handoff"
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="new-task")
        client.pending_handoff = handoff
        client._pending_guardian_finishes = []
        candidate = GuardianFinishCandidate(
            old_task_id="old-task",
            target_task_id="new-task",
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
        )
        client._pending_guardian_finish_candidate = candidate

        with patch("continuous_cli.finish_guardian_session") as finish:
            client._complete_handoff_dispatch(
                TurnOutcome("continued successfully", None),
                dispatched_handoff=handoff,
            )

        finish.assert_not_called()
        self.assertEqual(client.pending_handoff, handoff)
        self.assertEqual(client._pending_guardian_finish_candidate, candidate)

    def test_failed_new_dispatch_keeps_old_guardian_session(self) -> None:
        for failure_kind in (
            "exception",
            "error-outcome",
            "interrupted",
            "generic-block",
        ):
            with self.subTest(failure_kind=failure_kind):
                finished: list[str] = []
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="old-task")
                client.latest_user = "continue"
                client.latest_assistant = "checkpointed"
                client.pending_handoff = None
                client._pending_guardian_finishes = []
                client._rewind_sources = []
                client.rollovers = 0
                client.verbose = False
                client._write_status = lambda _message, **_: None
                handoff = "failed dispatch handoff"

                def prepare(_reason: str, *, checkpoint: bool) -> None:
                    self.assertTrue(checkpoint)
                    handoff_sha256 = hashlib.sha256(
                        handoff.encode("utf-8")
                    ).hexdigest()
                    client.pending_handoff = handoff
                    client.thread = SimpleNamespace(id="new-task")
                    client._pending_guardian_finish_candidate = (
                        GuardianFinishCandidate(
                            old_task_id="old-task",
                            target_task_id="new-task",
                            handoff_sha256=handoff_sha256,
                        )
                    )
                    client._active_rollover_journal = replace(
                        concrete_rollover_journal(
                            "old-task",
                            "new-task",
                            handoff_sha256,
                        ),
                        phase="target_created",
                    )

                client.prepare_rollover = prepare
                install_memory_rollover_transitions(client)
                if failure_kind == "exception":
                    client.run_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("dispatch failed")
                    )
                elif failure_kind == "error-outcome":
                    client.run_turn = lambda *_args, **_kwargs: TurnOutcome(
                        "",
                        None,
                        error_message="dispatch failed",
                    )
                elif failure_kind == "interrupted":
                    client.run_turn = lambda *_args, **_kwargs: TurnOutcome(
                        "",
                        None,
                        interrupted=True,
                    )
                else:
                    client.run_turn = lambda *_args, **_kwargs: TurnOutcome(
                        "This content can't be shown",
                        None,
                    )

                with patch(
                    "continuous_cli.finish_guardian_session",
                    side_effect=lambda _project, task_id, *, replaced_by: (
                        finished.append(f"{task_id}->{replaced_by}") or True
                    ),
                ):
                    if failure_kind == "exception":
                        with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
                            client.continue_after_rollover("context 62%")
                    else:
                        outcome = client.continue_after_rollover("context 62%")
                        if failure_kind == "error-outcome":
                            self.assertEqual(outcome.error_message, "dispatch failed")

                    # Abandoning the failed handoff must not let a later,
                    # unrelated successful turn authorize OLD finish.
                    client.pending_handoff = None
                    client._finish_guardian_sessions_after_dispatch(
                        TurnOutcome("unrelated success", None)
                    )

                self.assertEqual(finished, [])
                self.assertEqual(client._pending_guardian_finishes, [])
                candidate = client._pending_guardian_finish_candidate
                self.assertEqual(candidate.old_task_id, "old-task")
                self.assertEqual(candidate.target_task_id, "new-task")

    def test_failed_new_create_or_preflight_never_finishes_old_guardian_session(self) -> None:
        for failure_kind in ("create", "preflight-exception", "preflight-none"):
            with self.subTest(failure_kind=failure_kind):
                finished: list[str] = []
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="old-task")
                client.latest_user = "continue"
                client.latest_assistant = "checkpointed"
                client.pending_handoff = None
                client._pending_guardian_finishes = []
                client._rewind_sources = []
                client.rollovers = 0
                client.verbose = False
                client._write_status = lambda _message, **_: None
                client._checkpoint_for_rollover = lambda _reason: True

                def start(**_kwargs: Any) -> None:
                    if failure_kind == "create":
                        raise RuntimeError("create failed")
                    client.thread = SimpleNamespace(id="new-task")

                client.start_fresh_thread = start
                client.run_turn = lambda *_args, **_kwargs: TurnOutcome(
                    "continued with pointer-only handoff",
                    None,
                )
                preflight = (
                    RuntimeError("preflight failed")
                    if failure_kind == "preflight-exception"
                    else (
                        None
                        if failure_kind == "preflight-none"
                        else "# bundle\n- Task ID: `new-task`\n"
                    )
                )

                with (
                    patch("continuous_cli.discover_guardian_target", return_value=object()),
                    patch(
                        "continuous_cli.validated_guardian_bundle",
                        side_effect=preflight
                        if isinstance(preflight, BaseException)
                        else None,
                        return_value=preflight
                        if not isinstance(preflight, BaseException)
                        else None,
                    ),
                    patch(
                        "continuous_cli.finish_guardian_session",
                        side_effect=lambda _project, task_id, *, replaced_by: (
                            finished.append(f"{task_id}->{replaced_by}") or True
                        ),
                    ),
                ):
                    expected = (
                        "create failed"
                        if failure_kind == "create"
                        else "target preflight"
                    )
                    with self.assertRaisesRegex(RuntimeError, expected):
                        client.continue_after_rollover("context 62%")

                self.assertEqual(finished, [])
                self.assertEqual(client._pending_guardian_finishes, [])

    def test_confirmed_guardian_finish_retries_only_after_successful_handoff(self) -> None:
        attempts: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="old-task")
        client.latest_user = "continue"
        client.latest_assistant = "checkpointed"
        client.pending_handoff = None
        client._pending_guardian_finishes = []
        client._rewind_sources = []
        client.rollovers = 0
        client.verbose = False
        client._write_status = lambda _message, **_: None
        handoff = "exact bounded handoff"

        def prepare(_reason: str, *, checkpoint: bool) -> None:
            self.assertTrue(checkpoint)
            handoff_sha256 = hashlib.sha256(handoff.encode("utf-8")).hexdigest()
            client.pending_handoff = handoff
            client.thread = SimpleNamespace(id="new-task")
            client._pending_guardian_finish_candidate = GuardianFinishCandidate(
                old_task_id="old-task",
                target_task_id="new-task",
                handoff_sha256=handoff_sha256,
            )
            client._active_rollover_journal = replace(
                concrete_rollover_journal(
                    "old-task",
                    "new-task",
                    handoff_sha256,
                ),
                phase="target_created",
            )

        def begin(dispatched_handoff: str) -> None:
            self.assertEqual(dispatched_handoff, handoff)
            client._active_rollover_journal = replace(
                client._active_rollover_journal,
                phase="dispatch_started",
            )

        def record_concrete() -> RolloverJournal:
            client._active_rollover_journal = replace(
                client._active_rollover_journal,
                phase="concrete_started",
            )
            return client._active_rollover_journal

        client.prepare_rollover = prepare
        client._begin_concrete_rollover_dispatch = begin
        client._record_concrete_rollover_dispatch = record_concrete
        client._finish_active_rollover_journal = lambda: None
        client.run_turn = lambda *_args, **_kwargs: TurnOutcome(
            "handoff accepted",
            None,
            tool_activity=True,
        )

        def finish(_project: Path, task_id: str, *, replaced_by: str) -> bool:
            attempts.append(f"{task_id}->{replaced_by}")
            if len(attempts) == 1:
                raise RuntimeError("temporary finish failure")
            return True

        with patch("continuous_cli.finish_guardian_session", side_effect=finish):
            outcome = client.continue_after_rollover("context 62%")
            self.assertEqual(outcome.final_response, "handoff accepted")
            self.assertEqual(len(client._pending_guardian_finishes), 1)
            queued = client._pending_guardian_finishes[0]
            self.assertEqual(queued.old_task_id, "old-task")
            self.assertEqual(queued.target_task_id, "new-task")
            self.assertIsNone(client._pending_guardian_finish_candidate)

            client._finish_guardian_sessions_after_dispatch(
                TurnOutcome("later successful turn", None, tool_activity=True)
            )

        self.assertEqual(
            attempts,
            ["old-task->new-task", "old-task->new-task"],
        )
        self.assertEqual(client._pending_guardian_finishes, [])

    def test_finish_subprocess_failures_remain_queued_for_retry(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="control",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        failures = (
            subprocess.TimeoutExpired(["contextctl", "finish"], 30),
            OSError("could not start contextctl"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                client = ContinuousCodex.__new__(ContinuousCodex)
                client.project_root = Path("C:/project")
                client.thread = SimpleNamespace(id="new-task")
                receipt = GuardianFinishCandidate(
                    old_task_id="old-task",
                    target_task_id="new-task",
                    handoff_sha256="a" * 64,
                )
                client._pending_guardian_finishes = [receipt]
                client._active_rollover_journal = concrete_rollover_journal(
                    receipt.old_task_id,
                    receipt.target_task_id,
                    receipt.handoff_sha256,
                )
                client._finish_active_rollover_journal = lambda: None
                client.verbose = False
                client._write_status = lambda _message, **_: None
                completed = subprocess.CompletedProcess(
                    [], 0, stdout="CONTEXT_SESSION_FINISHED\n", stderr=""
                )
                with (
                    patch(
                        "continuous_cli.discover_guardian_target",
                        return_value=target,
                    ),
                    patch(
                        "continuous_cli.subprocess.run",
                        side_effect=(failure, completed),
                    ),
                    patch(
                        "continuous_cli.guardian_receipt",
                        return_value=(
                            target,
                            guardian_lifecycle_receipt(
                                "old-task",
                                project_id="control",
                                kind="retired",
                                replacement_task_id="new-task",
                            ),
                        ),
                    ),
                ):
                    client._finish_guardian_sessions_after_dispatch(
                        TurnOutcome("handoff accepted", None, tool_activity=True)
                    )
                    self.assertEqual(
                        client._pending_guardian_finishes, [receipt]
                    )
                    client._finish_guardian_sessions_after_dispatch(
                        TurnOutcome("later successful turn", None, tool_activity=True)
                    )
                self.assertEqual(client._pending_guardian_finishes, [])

    def test_finish_guardian_session_passes_exact_replacement_target(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="control",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="context session finished: control task=old kind=retired\n",
            stderr="",
        )
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch("continuous_cli.subprocess.run", return_value=completed) as run,
            patch(
                "continuous_cli.guardian_receipt",
                return_value=(
                    target,
                    guardian_lifecycle_receipt(
                        "old-task",
                        project_id="control",
                        kind="retired",
                        replacement_task_id="new-task",
                    ),
                ),
            ),
        ):
            self.assertTrue(
                finish_guardian_session(
                    Path("C:/project"),
                    "old-task",
                    replaced_by="new-task",
                )
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--task-id") + 1], "old-task")
        self.assertEqual(command[command.index("--replaced-by") + 1], "new-task")

    def test_finish_guardian_session_rejects_success_without_exact_receipt(self) -> None:
        target = SimpleNamespace(
            workspace_root=Path("C:/workspace"),
            project_id="control",
            contextctl=Path("C:/workspace/contextctl.py"),
        )
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="context session finished: control task=old-task kind=retired\n",
            stderr="",
        )
        with (
            patch("continuous_cli.discover_guardian_target", return_value=target),
            patch("continuous_cli.subprocess.run", return_value=completed),
            patch("continuous_cli.guardian_receipt", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "without an exact"):
                finish_guardian_session(
                    Path("C:/project"),
                    "old-task",
                    replaced_by="new-task",
                )

    def test_terminal_wrapper_uses_local_continuous_bootstrap(self) -> None:
        wrapper = (
            Path(__file__).resolve().parent / "codex-continuous.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("bootstrap-legacy.ps1", wrapper)
        self.assertIn("-ProjectRoot $ProjectRoot -CliArgs $CliArgs", wrapper)
        self.assertNotIn("CodexClaudeBridge", wrapper)

    def test_policy_notice_is_published_exactly_once(self) -> None:
        notice = (
            "This content was flagged for possible cybersecurity risk. "
            "If this seems wrong, try rephrasing your request."
        )
        outcome = TurnOutcome(
            "",
            None,
            error_message=notice,
            error_code="cyber_policy",
        )
        renderer, output = TerminalRendererTests.renderer(
            tty=False,
            color=False,
            unicode=True,
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.renderer = renderer
        client._output_lock = renderer.lock
        client._render_epoch = 0
        client._line_open = False
        client.latest_assistant = ""
        client.previous_input_tokens = None
        client.previous_context_window = None

        self.assertTrue(client._settle_policy_boundary(outcome))
        self.assertTrue(client._settle_policy_boundary(outcome))

        self.assertTrue(outcome.policy_notice_published)
        self.assertEqual(output.getvalue().count(notice), 1)

    def test_policy_boundary_during_checkpoint_never_creates_target(self) -> None:
        boundary = TurnOutcome(
            "",
            None,
            error_message="platform policy notice",
            error_code="cyber_policy",
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        source_thread = SimpleNamespace(id="source-task")
        client.thread = source_thread
        client.project_root = Path("C:/project")
        client.pending_handoff = None
        client.latest_user = "current objective"
        client.latest_assistant = ""
        client._pending_guardian_finish_candidate = None
        client._run_controller_turn_silently = lambda _prompt: boundary
        client._adopt_outcome = lambda _outcome: None
        published: list[TurnOutcome] = []
        client._publish_policy_boundary_notice = published.append
        client.start_cleared_thread = lambda: (_ for _ in ()).throw(
            AssertionError("checkpoint boundary created a target")
        )

        with patch(
            "continuous_cli.discover_guardian_target",
            return_value=SimpleNamespace(project_id="control"),
        ):
            outcome = client.continue_after_rollover("context threshold")

        self.assertIs(outcome, boundary)
        self.assertIs(client.thread, source_thread)
        self.assertIsNone(client.pending_handoff)
        self.assertEqual(published, [boundary])

    def test_normal_resume_and_display_recovery_prompts_omit_policy_vocabulary(
        self,
    ) -> None:
        prompts = [
            CONTINUOUS_DEVELOPER_INSTRUCTIONS,
            STARTUP_RESUME_PROMPT,
            RECOVERED_ROLLOVER_PROMPT,
            RECOVERED_ROLLOVER_DISPATCH_PROMPT,
            build_handoff(
                Path("C:/project"),
                "current objective",
                "",
                "context threshold",
            ),
        ]
        captured: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="source-task")
        client.pending_handoff = None
        client.latest_user = "current objective"
        client.latest_assistant = ""
        client.previous_input_tokens = None
        client.previous_context_window = None
        client.rollovers = 0
        client._pending_guardian_finish_candidate = None
        client._current_guardian_runtime_active = lambda: False
        client._remember_rewind_source = lambda _thread_id: None
        client.start_cleared_thread = lambda: setattr(
            client,
            "thread",
            SimpleNamespace(id="recovery-task"),
        )
        client._run_controller_turn_silently = lambda prompt: (
            captured.append(prompt) or TurnOutcome("recovered response", None)
        )

        client.recover_generic_block()
        prompts.extend(captured)

        forbidden = (
            "boundary",
            "safeguard",
            "bypass",
            "structured policy",
            "cyberpolicy",
            "restricted request",
            "blocked request",
        )
        for prompt in prompts:
            lowered = prompt.casefold()
            for term in forbidden:
                with self.subTest(term=term, prompt=prompt[:80]):
                    self.assertNotIn(term, lowered)

    def test_model_and_fast_use_catalog_settings_and_persist(self) -> None:
        calls: list[tuple[str, Any]] = []
        sol = SimpleNamespace(
            model="gpt-5.6-sol",
            id="gpt-5.6-sol",
            display_name="GPT-5.6 Sol",
            hidden=False,
            is_default=True,
            default_reasoning_effort="low",
            supported_reasoning_efforts=[
                SimpleNamespace(reasoning_effort=value)
                for value in ("low", "high", "xhigh")
            ],
            service_tiers=[SimpleNamespace(id="priority", name="Fast")],
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.client = SimpleNamespace(
            model_list=lambda _hidden=False: SimpleNamespace(data=[sol]),
            _request_raw=lambda method, params=None: calls.append((method, params)) or {},
        )
        client.thread = SimpleNamespace(id="thread-model")
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client._model_catalog = None
        client._fast_tier_id = None
        client.verbose = False
        client._output_lock = threading.RLock()
        client._render_epoch = 0
        client._line_open = False

        with contextlib.redirect_stdout(io.StringIO()):
            client.handle_slash_command("/model gpt-5.6-sol high")
            client.handle_slash_command("/fast on")
            client.handle_slash_command("/fast off")

        self.assertEqual(client.model, "gpt-5.6-sol")
        self.assertEqual(client.reasoning_effort, "high")
        self.assertEqual(client.service_tier, "default")
        settings = [params for method, params in calls if method == "thread/settings/update"]
        self.assertEqual(settings[0]["model"], "gpt-5.6-sol")
        self.assertEqual(settings[0]["effort"], "high")
        self.assertEqual(settings[1]["serviceTier"], "priority")
        self.assertEqual(settings[2]["serviceTier"], "default")
        config_calls = [params for method, params in calls if method == "config/batchWrite"]
        self.assertTrue(all(call["reloadUserConfig"] for call in config_calls))
        self.assertEqual(
            [(edit["keyPath"], edit["value"]) for edit in config_calls[0]["edits"]],
            [("model", "gpt-5.6-sol"), ("model_reasoning_effort", "high")],
        )
        self.assertEqual(config_calls[1]["edits"][0]["value"], "fast")
        self.assertEqual(config_calls[2]["edits"][0]["value"], "default")

        before = (client.model, client.reasoning_effort)
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            client.handle_slash_command("/model gpt-5.6-sol ultra")
        self.assertEqual((client.model, client.reasoning_effort), before)

    def test_failed_fast_persistence_restores_runtime_and_memory_state(self) -> None:
        calls: list[tuple[str, Any]] = []
        model = SimpleNamespace(
            model="gpt-5.6-sol",
            is_default=True,
            service_tiers=[SimpleNamespace(id="priority", name="Fast")],
        )

        def raw(method: str, params: Any = None) -> object:
            calls.append((method, params))
            if method == "config/batchWrite":
                raise RuntimeError("disk full")
            return {}

        client = ContinuousCodex.__new__(ContinuousCodex)
        client.client = SimpleNamespace(
            model_list=lambda _hidden=False: SimpleNamespace(data=[model]),
            _request_raw=raw,
        )
        client.thread = SimpleNamespace(id="thread-model")
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client._model_catalog = None
        client._fast_tier_id = None

        with self.assertRaisesRegex(RuntimeError, "disk full"):
            client._handle_fast_command(["on"])

        settings = [params for method, params in calls if method == "thread/settings/update"]
        self.assertEqual(settings[0]["serviceTier"], "priority")
        self.assertEqual(settings[1]["serviceTier"], "default")
        self.assertEqual(client.service_tier, "default")

    def test_run_turn_passes_runtime_settings(self) -> None:
        events = [Event("turn/completed", {"turn": {"status": "completed", "items": []}})]
        thread = FakeThread(events)
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = thread
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "priority"
        client.personality = "pragmatic"
        client._output_lock = threading.RLock()
        client._compacted_threads = set()
        client._service_pending_input = lambda _timeout=0.1: None
        client.client = SimpleNamespace(close=lambda: None)

        with contextlib.redirect_stdout(io.StringIO()):
            client.run_turn("test", stream_text=False)

        self.assertEqual(
            thread.last_kwargs,
            {
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "service_tier": "priority",
                "personality": "pragmatic",
            },
        )

    def test_selected_skill_or_mention_is_attached_once_to_next_turn(self) -> None:
        events = [Event("turn/completed", {"turn": {"status": "completed", "items": []}})]
        thread = FakeThread(events)
        attachment = object()
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = thread
        client._pending_turn_inputs = [attachment]
        client._TextInput = lambda text: ("text", text)
        client._output_lock = threading.RLock()
        client._compacted_threads = set()
        client._service_pending_input = lambda _timeout=0.1: None
        client.client = SimpleNamespace(close=lambda: None)

        with contextlib.redirect_stdout(io.StringIO()):
            client.run_turn("inspect", stream_text=False)

        self.assertEqual(thread.last_input, [attachment, ("text", "inspect")])
        self.assertEqual(client._pending_turn_inputs, [])

    def test_rewind_forks_before_last_turn_then_archives_source(self) -> None:
        def turn(turn_id: str, user: str, assistant: str) -> SimpleNamespace:
            return SimpleNamespace(
                id=turn_id,
                model_dump=lambda **_: {
                    "id": turn_id,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": user}],
                        },
                        {"type": "agentMessage", "text": assistant},
                    ],
                },
            )

        turns = [turn("turn-1", "first", "one"), turn("turn-2", "second", "two")]
        fork_params: dict[str, Any] = {}
        archived: list[str] = []
        unsubscribed: list[str] = []
        renderer = FakeRewindRenderer()
        response = SimpleNamespace(
            thread=SimpleNamespace(id="forked", turns=turns[:-1]),
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            service_tier="default",
        )

        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="source")
        client.client = SimpleNamespace(
            thread_read=lambda _thread_id, include_turns=False: SimpleNamespace(
                thread=SimpleNamespace(turns=turns)
            ),
            thread_fork=lambda _thread_id, params: fork_params.update(params) or response,
            thread_archive=archived.append,
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client.personality = None
        client.latest_user = "second"
        client.latest_assistant = "two"
        client.pending_handoff = "stale"
        client.previous_input_tokens = 90
        client.previous_context_window = 100
        client._compacted_threads = {"source"}
        client._file_checkpoints = {}
        client._pending_turn_inputs = [object()]
        client._prefill_prompt = ""
        client._output_lock = threading.RLock()
        client.renderer = renderer
        client._render_epoch = 0
        client._line_open = False

        with contextlib.redirect_stdout(io.StringIO()):
            restored = client.rewind_previous_exchange()

        self.assertEqual(restored, "second")
        self.assertEqual(client.thread.id, "forked")
        self.assertEqual(fork_params["lastTurnId"], "turn-1")
        self.assertEqual(archived, ["source"])
        self.assertEqual(unsubscribed, ["source"])
        self.assertEqual(
            [menu[0] for menu in renderer.menus],
            ["Rewind", "What should be restored?"],
        )
        self.assertEqual((client.latest_user, client.latest_assistant), ("first", "one"))
        self.assertEqual(client._prefill_prompt, "second")
        self.assertIsNone(client.pending_handoff)
        self.assertEqual(client._pending_turn_inputs, [])
        self.assertNotIn("source", client._compacted_threads)

    def test_model_fast_and_rewind_slashes_never_start_model_turn(self) -> None:
        seen: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.start_fresh_thread = lambda **_: None
        client._handle_model_command = lambda _arguments: seen.append("model")
        client._handle_fast_command = lambda _arguments: seen.append("fast")
        client.rewind_previous_exchange = lambda: seen.append("rewind") or "draft"
        client.run_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("slash command reached model")
        )
        client.verbose = False
        client._prefill_prompt = ""
        client._output_lock = threading.RLock()
        client._render_epoch = 0
        client._line_open = False

        with (
            patch(
                "builtins.input",
                side_effect=["/model gpt-5.6-sol xhigh", "/fast", "/rewind", "/exit"],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(client.interactive(), 0)

        self.assertEqual(seen, ["model", "fast", "rewind"])

    def test_redirected_input_bom_still_routes_slash_command(self) -> None:
        messages: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.status = lambda: "status-ok"
        client._write_status = lambda message, **_: messages.append(message)

        outcome = client.handle_slash_command("\ufeff   /status")

        self.assertTrue(outcome.handled)
        self.assertEqual(messages, ["status-ok"])

        unknown = client.handle_slash_command("\ufeff  /not-a-command")
        self.assertTrue(unknown.handled)
        self.assertIn("\u672a\u77e5\u6307\u4ee4", messages[-1])

    def test_rewind_adopts_candidate_when_source_archive_fails(self) -> None:
        only_turn = SimpleNamespace(
            id="turn-1",
            model_dump=lambda **_: {
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "keep me"}],
                    }
                ]
            },
        )
        archive_attempts: list[str] = []
        unsubscribed: list[str] = []
        statuses: list[str] = []
        renderer = FakeRewindRenderer()

        def archive(thread_id: str) -> None:
            archive_attempts.append(thread_id)
            raise RuntimeError("archive failed")

        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        source = SimpleNamespace(id="source")
        client.thread = source
        client.client = SimpleNamespace(
            thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                thread=SimpleNamespace(turns=[only_turn])
            ),
            thread_start=lambda _params: SimpleNamespace(
                thread=SimpleNamespace(id="candidate"),
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                service_tier="default",
            ),
            thread_archive=archive,
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client._write_status = lambda message, **_: statuses.append(message)
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client.personality = None
        client.latest_user = "keep me"
        client.latest_assistant = ""
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client._rewind_sources = []
        client._compacted_threads = {"source"}
        client._file_checkpoints = {}
        client._pending_turn_inputs = []
        client._prefill_prompt = ""
        client._output_lock = threading.RLock()
        client.renderer = renderer
        client._render_epoch = 0
        client._line_open = False

        restored = client.rewind_previous_exchange()

        self.assertEqual(restored, "keep me")
        self.assertEqual(client.thread.id, "candidate")
        self.assertEqual(archive_attempts, ["source"])
        self.assertEqual(unsubscribed, ["source"])
        self.assertTrue(any("archive failed" in message for message in statuses))

    def test_rewind_crosses_controller_rollover_and_removes_real_exchange(self) -> None:
        def turn(turn_id: str, user: str, assistant: str = "") -> SimpleNamespace:
            items: list[dict[str, Any]] = [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": user}],
                }
            ]
            if assistant:
                items.append({"type": "agentMessage", "text": assistant})
            return SimpleNamespace(
                id=turn_id,
                model_dump=lambda **_: {"id": turn_id, "items": items},
            )

        old_turns = [
            turn("turn-1", "first", "one"),
            turn("turn-2", "second", "two"),
            turn("checkpoint", "[CONTROLLER CHECKPOINT-ONLY TURN]", "saved"),
        ]
        current_turns = [
            turn("handoff", "[AUTOMATIC CONTINUATION — authoritative]", "continued")
        ]
        fork_params: dict[str, Any] = {}
        archived: list[str] = []
        unsubscribed: list[str] = []
        renderer = FakeRewindRenderer()
        forked = SimpleNamespace(
            thread=SimpleNamespace(id="forked"),
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            service_tier="default",
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="current")
        client.client = SimpleNamespace(
            thread_read=lambda thread_id, include_turns=False: SimpleNamespace(
                thread=SimpleNamespace(
                    turns=current_turns if thread_id == "current" else old_turns
                )
            ),
            thread_fork=lambda thread_id, params: (
                self.assertEqual(thread_id, "old")
                or fork_params.update(params)
                or forked
            ),
            thread_archive=archived.append,
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client.personality = None
        client.latest_user = "second"
        client.latest_assistant = "two"
        client.pending_handoff = None
        client.previous_input_tokens = 90
        client.previous_context_window = 100
        client._rewind_sources = ["old"]
        client._compacted_threads = {"current", "old"}
        client._file_checkpoints = {}
        client._pending_turn_inputs = []
        client._prefill_prompt = ""
        client._output_lock = threading.RLock()
        client.renderer = renderer
        client._render_epoch = 0
        client._line_open = False

        with contextlib.redirect_stdout(io.StringIO()):
            restored = client.rewind_previous_exchange()

        self.assertEqual(restored, "second")
        self.assertEqual(client.thread.id, "forked")
        self.assertEqual(fork_params["lastTurnId"], "turn-1")
        self.assertEqual(renderer.menus[0][1][0], ("second", "Most recent prompt"))
        self.assertEqual(archived, ["current", "old"])
        self.assertEqual(unsubscribed, ["current", "old"])
        self.assertEqual((client.latest_user, client.latest_assistant), ("first", "one"))
        self.assertEqual(client._rewind_sources, [])
        self.assertEqual(client._compacted_threads, set())

    def test_rewind_refuses_non_text_user_input_without_mutation(self) -> None:
        attached_turn = SimpleNamespace(
            id="turn-image",
            model_dump=lambda **_: {
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "inspect"},
                            {"type": "image", "url": "local://image"},
                        ],
                    }
                ]
            },
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        source = SimpleNamespace(id="source")
        archived: list[str] = []
        unsubscribed: list[str] = []
        renderer = FakeRewindRenderer()
        client.thread = source
        client._rewind_sources = []
        client._file_checkpoints = {}
        client.renderer = renderer
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client.client = SimpleNamespace(
            thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                thread=SimpleNamespace(turns=[attached_turn])
            ),
            thread_start=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("rewind created a candidate")
            ),
            thread_fork=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("rewind forked a candidate")
            ),
            thread_archive=archived.append,
        )

        with self.assertRaisesRegex(RuntimeError, "\u5716\u7247\u6216\u9644\u4ef6"):
            client.rewind_previous_exchange()

        self.assertIs(client.thread, source)
        self.assertEqual([menu[0] for menu in renderer.menus], ["Rewind", "What should be restored?"])
        self.assertEqual(archived, [])
        self.assertEqual(unsubscribed, [])

    def test_custom_model_does_not_borrow_default_fast_tier(self) -> None:
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.model = "private-custom-model"
        catalog = [SimpleNamespace(model="gpt-default", is_default=True)]
        self.assertIsNone(client._current_catalog_model(catalog))

        client.model = None
        self.assertIs(client._current_catalog_model(catalog), catalog[0])

    def test_custom_model_fast_status_and_off_are_safe(self) -> None:
        messages: list[str] = []
        mutations: list[tuple[dict[str, Any], list[tuple[str, Any]], dict[str, Any]]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.model = "private-custom-model"
        client.service_tier = "priority"
        client.thread = SimpleNamespace(id="thread")
        client._load_model_catalog = lambda: [
            SimpleNamespace(model="gpt-default", is_default=True)
        ]
        client._write_status = lambda message, **_: messages.append(message)
        client._update_settings_and_persist = (
            lambda settings, edits, rollback: mutations.append((settings, edits, rollback))
        )

        client._handle_fast_command(["status"])
        with self.assertRaisesRegex(RuntimeError, "\u7121\u6cd5\u78ba\u8a8d Fast tier"):
            client._handle_fast_command(["on"])
        self.assertEqual(mutations, [])

        client._handle_fast_command(["off"])
        self.assertEqual(client.service_tier, "default")
        self.assertEqual(mutations[0][0]["serviceTier"], "default")
        self.assertEqual(mutations[0][1], [("service_tier", "default")])
        self.assertEqual(messages, ["Fast: priority", "Fast: off"])

    def test_goal_command_is_view_or_clear_only(self) -> None:
        messages: list[str] = []
        cleared: list[str] = []
        requests: list[tuple[str, dict[str, Any]]] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.thread = SimpleNamespace(id="thread")
        client._raw_request = lambda method, params: (
            requests.append((method, params)) or {"goal": None}
        )
        client._write_status = lambda message, **_: messages.append(message)
        client.client = SimpleNamespace(
            thread_goal_clear=lambda thread_id: cleared.append(thread_id)
        )

        client._handle_goal_command("")
        client._handle_goal_command("clear")
        with self.assertRaisesRegex(RuntimeError, "/goal clear"):
            client._handle_goal_command("resume")

        self.assertEqual(messages[:2], ["Goal: none", "Goal cleared."])
        self.assertEqual(cleared, ["thread"])
        self.assertEqual(requests, [("thread/goal/get", {"threadId": "thread"})])

    def test_debug_config_reports_structure_without_values(self) -> None:
        rendered: list[str] = []
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client.active_permission_profile = None
        client.personality = None

        def request(method: str, _params: Any) -> dict[str, Any]:
            if method == "config/read":
                return {
                    "config": {
                        "model": "SHOULD_NOT_APPEAR",
                        "mcp_servers": {"private": {"bearer_token": "TOP_SECRET"}},
                    },
                    "layers": [{"headers": {"Authorization": "Bearer PRIVATE"}}],
                    "origins": {"model": "C:/secret/config.toml"},
                }
            return {"allowed": "REQUIREMENT_SECRET"}

        client._raw_request = request
        client._write_output_block = lambda value: rendered.append(value)
        client._show_debug_config()

        output = rendered[0]
        self.assertIn('"config_keys"', output)
        self.assertIn('"mcp_servers"', output)
        for secret in (
            "SHOULD_NOT_APPEAR",
            "TOP_SECRET",
            "Bearer PRIVATE",
            "C:/secret/config.toml",
            "REQUIREMENT_SECRET",
        ):
            self.assertNotIn(secret, output)

    def test_resume_deletes_bootstrap_thread_and_clears_old_view(self) -> None:
        deleted: list[str] = []
        unsubscribed: list[str] = []
        renderer, _output = TerminalRendererTests.renderer(
            tty=True,
            color=False,
            unicode=True,
        )
        target = SimpleNamespace(id="target", name="saved", preview="saved")
        resumed = SimpleNamespace(
            thread=SimpleNamespace(id="target", turns=[]),
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            service_tier="default",
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="bootstrap")
        client.client = SimpleNamespace(
            thread_list=lambda _params: SimpleNamespace(data=[target]),
            thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                thread=SimpleNamespace(turns=[])
            ),
            thread_resume=lambda _thread_id, _params: resumed,
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._delete_thread = lambda thread_id: deleted.append(thread_id)
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client._write_status = lambda *_args, **_kwargs: None
        client._output_lock = renderer.lock
        client.renderer = renderer
        client._render_epoch = 0
        client._line_open = False
        client.active_permission_profile = None
        client._pending_turn_inputs = [object()]
        client._rewind_sources = ["older"]
        client._prefill_prompt = "draft"
        client._manual_compaction_thread = "bootstrap"
        client._manual_compaction_done = threading.Event()

        client._resume_saved_thread("target")

        self.assertEqual(client.thread.id, "target")
        self.assertEqual(deleted, ["bootstrap"])
        self.assertEqual(unsubscribed, [])
        self.assertEqual(client._rewind_sources, [])
        self.assertEqual(client._pending_turn_inputs, [])
        self.assertEqual(client._prefill_prompt, "")
        self.assertIsNone(client._manual_compaction_thread)
        self.assertTrue(client._manual_compaction_done.is_set())
        self.assertGreater(renderer.epoch, 0)

    def test_resume_failure_keeps_bootstrap_thread(self) -> None:
        deleted: list[str] = []
        target = SimpleNamespace(id="target", name="saved", preview="saved")
        client = ContinuousCodex.__new__(ContinuousCodex)
        source = SimpleNamespace(id="bootstrap")
        client.project_root = Path("C:/project")
        client.thread = source
        client.active_permission_profile = None
        client.client = SimpleNamespace(
            thread_list=lambda _params: SimpleNamespace(data=[target]),
            thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                thread=SimpleNamespace(turns=[])
            ),
            thread_resume=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("resume failed")
            ),
        )
        client._delete_thread = lambda thread_id: deleted.append(thread_id)

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            client._resume_saved_thread("target")

        self.assertIs(client.thread, source)
        self.assertEqual(deleted, [])

    def test_rewind_refills_original_prompt_for_translated_turn(self) -> None:
        translated = SimpleNamespace(
            id="turn-init",
            model_dump=lambda **_: {
                "id": "turn-init",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {
                                "type": "text",
                                "text": "[NATIVE /init REQUEST]\nCreate AGENTS.md",
                            }
                        ],
                    }
                ],
            },
        )
        archived: list[str] = []
        unsubscribed: list[str] = []
        renderer = FakeRewindRenderer()
        started = SimpleNamespace(
            thread=SimpleNamespace(id="candidate"),
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            service_tier="default",
        )
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.project_root = Path("C:/project")
        client.thread = SimpleNamespace(id="source")
        client.client = SimpleNamespace(
            thread_read=lambda *_args, **_kwargs: SimpleNamespace(
                thread=SimpleNamespace(turns=[translated])
            ),
            thread_start=lambda _params: started,
            thread_archive=archived.append,
        )
        client._Thread = lambda _client, thread_id: SimpleNamespace(id=thread_id)
        client._unsubscribe_thread = lambda thread_id: unsubscribed.append(thread_id)
        client.model = "gpt-5.6-sol"
        client.reasoning_effort = "xhigh"
        client.service_tier = "default"
        client.personality = None
        client._rewind_sources = []
        client._logical_prompts = {("source", "turn-init"): "/init"}
        client._file_checkpoints = {}
        client._pending_turn_inputs = []
        client.pending_handoff = None
        client.previous_input_tokens = None
        client.previous_context_window = None
        client._output_lock = threading.RLock()
        client.renderer = renderer
        client._render_epoch = 0
        client._line_open = False

        with contextlib.redirect_stdout(io.StringIO()):
            restored = client.rewind_previous_exchange()

        self.assertEqual(restored, "/init")
        self.assertEqual(renderer.menus[0][1][0][0], "/init")
        self.assertEqual(archived, ["source"])
        self.assertEqual(unsubscribed, ["source"])
        self.assertNotIn(("source", "turn-init"), client._logical_prompts)

    def test_double_escape_cancel_restores_unsent_draft(self) -> None:
        defaults: list[str] = []

        class FakeRenderer:
            def __init__(self) -> None:
                self.calls = 0

            def prompt(self, _footer: Any, **kwargs: Any) -> str:
                defaults.append(kwargs.get("default", ""))
                self.calls += 1
                return REWIND_TOKEN if self.calls == 1 else "/exit"

            @staticmethod
            def take_rewind_draft() -> str:
                return "unsent draft"

        renderer = FakeRenderer()
        client = ContinuousCodex.__new__(ContinuousCodex)
        client.start_fresh_thread = lambda **_: None
        client._get_renderer = lambda: renderer
        client._sync_render_state = lambda: None
        client.rewind_previous_exchange = lambda: None
        client._output_lock = threading.RLock()
        client._prefill_prompt = ""

        with patch("builtins.input", return_value="n"):
            self.assertEqual(client.interactive(), 0)

        self.assertEqual(defaults, ["", "unsent draft"])

    def test_sdk_cli_pair_must_be_explicitly_compatible(self) -> None:
        validate_sdk_cli_pair("0.144.4", "0.146.0")
        validate_sdk_cli_pair("0.144.4", "0.147.0")
        validate_sdk_cli_pair("0.146.0", "0.146.0")
        with self.assertRaisesRegex(RuntimeError, "untested"):
            validate_sdk_cli_pair("0.144.4", "0.148.0")


if __name__ == "__main__":
    unittest.main()
