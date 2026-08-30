from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rollover_stop_hook as hook


class RolloverStopHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_patch = patch.object(hook, "TRUSTED_WORKSPACE_ROOT", self.root)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.project = self.root / "demo"
        self.contextctl = self.root.joinpath(
            ".agents", "skills", "context-guardian", "scripts", "contextctl.py"
        )
        self.contextctl.parent.mkdir(parents=True)
        self.contextctl.write_text("# test placeholder\n", encoding="utf-8")
        self.project.joinpath(".context", "runtime").mkdir(parents=True)
        registry = {
            "automatic_rollover": {
                "enabled": True,
                "continue_until_objective_complete": True,
                "objective_completion_authority": (
                    "exact-task-completed-lifecycle-receipt"
                ),
                "source_retirement_authority": (
                    "exact-source-retired-receipt-after-verified-target-work"
                ),
                "desktop_scheduler": "native-goal-worker-lease",
                "desktop_stop_boundary_guard": "trusted-synchronous-user-hook",
                "desktop_visual_silence": False,
                "strict_clear_semantics": "app-server-only",
            },
            "projects": [
                {"id": "control", "path": ".", "state": ".context/state.json"},
                {"id": "demo", "path": "demo", "state": ".context/state.json"},
            ]
        }
        self.root.joinpath(".context").mkdir()
        self.root.joinpath(".context", "registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        self.task_id = "thread-test"
        self.source_task_id = "source-task"
        self.checkpoint_sha256 = "a" * 64
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).write_text("{}", encoding="utf-8")
        self.payload = {
            "hook_event_name": "Stop",
            "session_id": self.task_id,
            "cwd": str(self.project),
            "stop_hook_active": False,
        }

    def controller_prompt(self, declared_path: str = "demo") -> str:
        return (
            "[CONTROLLER-CREATED AUTOMATIC ROLLOVER]\n"
            f"Registered project: demo at {declared_path}\n"
            f"Source task: {self.source_task_id}\n"
            f"Checkpoint state SHA-256: {self.checkpoint_sha256}\n"
            "Continue the bounded objective."
        )

    def controller_bundle(self) -> str:
        return (
            "# BOUNDED CONTEXT BUNDLE\n\n"
            f"> State SHA-256: `{self.checkpoint_sha256}`\n\n"
            f"- Task ID: `{self.task_id}`\n"
        )

    @staticmethod
    def work_evidence(task_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_id": task_id,
            "rollout_file_id": "1:2",
            "baseline_offset": 10,
            "observed_offset": 20,
            "call_type": "custom_tool_call",
            "output_type": "custom_tool_call_output",
            "call_id_sha256": "d" * 64,
            "call_record_sha256": "e" * 64,
            "output_record_sha256": "f" * 64,
            "observed_at": "2026-08-13T00:00:00+00:00",
        }

    def lifecycle_receipt(
        self,
        *,
        kind: str = "completed",
        source_task_id: str | None = None,
        replacement_task_id: str | None = None,
    ) -> dict[str, object | None]:
        receipt: dict[str, object | None] = {
            "schema_version": 2,
            "project_id": "demo",
            "task_id": self.task_id,
            "kind": kind,
            "state_sha256": "a" * 64,
            "started_state_sha256": "a" * 64,
            "started_rules_fingerprint_sha256": "b" * 64,
            "audited_state_sha256": "a" * 64,
            "audit_rules_fingerprint_sha256": "b" * 64,
            "audit_fingerprint_sha256": "c" * 64,
            "source_task_id": source_task_id,
            "source_audited_state_sha256": None,
            "source_audit_rules_fingerprint_sha256": None,
            "source_audit_fingerprint_sha256": None,
            "replacement_task_id": replacement_task_id,
            "work_evidence": None,
            "replacement_work_evidence": None,
            "recorded_at": "2026-08-13T00:00:00+00:00",
        }
        if source_task_id is not None:
            receipt.update(
                {
                    "source_audited_state_sha256": "a" * 64,
                    "source_audit_rules_fingerprint_sha256": "b" * 64,
                    "source_audit_fingerprint_sha256": "d" * 64,
                    "work_evidence": self.work_evidence(self.task_id),
                }
            )
        if kind == "retired" and replacement_task_id is not None:
            receipt["replacement_work_evidence"] = self.work_evidence(
                replacement_task_id
            )
        return receipt

    def write_lifecycle_receipt(
        self, receipt: dict[str, object | None]
    ) -> None:
        runtime = self.project.joinpath(".context", "runtime")
        runtime.joinpath(f"{self.task_id}.json").unlink(missing_ok=True)
        receipts = runtime.joinpath("receipts")
        receipts.mkdir(exist_ok=True)
        receipts.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )

    def assert_receipt_fails_closed(
        self, receipt: dict[str, object | None]
    ) -> None:
        self.write_lifecycle_receipt(receipt)
        with patch("rollover_stop_hook.subprocess.run") as run:
            decision = hook.run(self.payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("objective remains active", decision["reason"])
        run.assert_not_called()

    def write_bound_lineage(self) -> None:
        rules_sha256 = "b" * 64
        audit_sha256 = "c" * 64
        runtime = self.project / ".context" / "runtime"
        runtime.joinpath(f"{self.source_task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "task_id": self.source_task_id,
                    "base_sha256": self.checkpoint_sha256,
                    "audited_state_sha256": self.checkpoint_sha256,
                    "audit_rules_fingerprint_sha256": rules_sha256,
                    "audit_fingerprint_sha256": audit_sha256,
                }
            ),
            encoding="utf-8",
        )
        runtime.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "task_id": self.task_id,
                    "base_sha256": self.checkpoint_sha256,
                    "started_state_sha256": self.checkpoint_sha256,
                    "source_task_id": self.source_task_id,
                    "source_audited_state_sha256": self.checkpoint_sha256,
                    "source_audit_rules_fingerprint_sha256": rules_sha256,
                    "source_audit_fingerprint_sha256": audit_sha256,
                    "codex_telemetry": {"available": True, "parse_errors": 0},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rollover_sentinel_forces_automatic_control_turn(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            2,
            stdout=(
                "CONTEXT_ROLLOVER_REQUIRED project=demo task=thread-test "
                "reasons=context_fill action=checkpoint-and-rollover\n"
            ),
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=result) as run:
            decision = hook.run(self.payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("desktop-rollover.md", decision["reason"])
        self.assertIn("DO NOT ECHO", decision["reason"])
        self.assertIn("Never ask for /new", decision["reason"])
        self.assertNotIn(self.task_id, decision["reason"])
        self.assertNotIn("CONTEXT_ROLLOVER_REQUIRED", decision["reason"])
        self.assertEqual(run.call_args.args[0][-2:], ["--count", "0"])

    def test_active_runtime_keeps_normal_and_recursive_stop_working(self) -> None:
        ok = subprocess.CompletedProcess(
            [],
            0,
            stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=ok):
            decision = hook.run(self.payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("DO NOT ECHO", decision["reason"])
        self.assertIn("next concrete unfinished action", decision["reason"])
        self.assertNotIn(self.task_id, decision["reason"])
        self.assertNotIn("project=", decision["reason"])

        recursive = dict(self.payload, stop_hook_active=True)
        with patch("rollover_stop_hook.subprocess.run", return_value=ok) as run:
            recursive_decision = hook.run(recursive)
        self.assertEqual(recursive_decision["decision"], "block")
        self.assertIn("Do not stop with a status", recursive_decision["reason"])
        self.assertEqual(run.call_args.args[0][-2:], ["--count", "0"])

    def test_plaintext_user_input_marker_cannot_pause_active_objective(self) -> None:
        paused = dict(
            self.payload,
            last_assistant_message=(
                "A user-only choice is required.\n\n"
                "[CONTINUOUS_USER_INPUT_REQUIRED]\n"
                "Should the generated report use JSON or CSV?"
            ),
        )
        runtime = self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        )
        ok = subprocess.CompletedProcess(
            [],
            0,
            stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=ok) as run:
            decision = hook.run(paused)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("next concrete unfinished action", decision["reason"])
        run.assert_called_once()
        self.assertTrue(runtime.is_file())

        answer = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt="Use JSON.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(answer), {})
            run.assert_not_called()
        self.assertTrue(runtime.is_file())

    def test_invalid_user_input_markers_cannot_stop_active_objective(self) -> None:
        ok = subprocess.CompletedProcess(
            [],
            0,
            stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
            stderr="",
        )
        invalid_messages = (
            "[CONTINUOUS_USER_INPUT_REQUIRED]",
            "[CONTINUOUS_USER_INPUT_REQUIRED] Work will continue automatically.",
            "[CONTINUOUS_USER_INPUT_REQUIRED] ?",
            (
                "[CONTINUOUS_USER_INPUT_REQUIRED] Choose JSON? "
                "[CONTINUOUS_USER_INPUT_REQUIRED]"
            ),
            "Checkpoint complete; the new task is continuing.",
            "[CONTINUOUS_USER_INPUT_REQUIRED] \u8981\u6211\u7e7c\u7e8c\u55ce\uff1f",
            "[CONTINUOUS_USER_INPUT_REQUIRED] Should I /new and continue?",
        )
        for message in invalid_messages:
            with self.subTest(message=message):
                payload = dict(self.payload, last_assistant_message=message)
                with patch(
                    "rollover_stop_hook.subprocess.run", return_value=ok
                ) as run:
                    decision = hook.run(payload)
                self.assertEqual(decision["decision"], "block")
                self.assertIn("next concrete unfinished action", decision["reason"])
                self.assertEqual(run.call_args.args[0][-2:], ["--count", "0"])

    def test_finished_runtime_allows_stop(self) -> None:
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        receipts = self.project.joinpath(".context", "runtime", "receipts")
        receipts.mkdir()
        receipts.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_id": "demo",
                    "task_id": self.task_id,
                    "kind": "completed",
                    "state_sha256": "a" * 64,
                    "started_state_sha256": "a" * 64,
                    "started_rules_fingerprint_sha256": "b" * 64,
                    "audited_state_sha256": "a" * 64,
                    "audit_rules_fingerprint_sha256": "b" * 64,
                    "audit_fingerprint_sha256": "c" * 64,
                    "source_task_id": None,
                    "source_audited_state_sha256": None,
                    "source_audit_rules_fingerprint_sha256": None,
                    "source_audit_fingerprint_sha256": None,
                    "replacement_task_id": None,
                    "work_evidence": None,
                    "replacement_work_evidence": None,
                    "recorded_at": "2026-08-13T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(self.payload), {})
            run.assert_not_called()

    def test_completed_receipt_with_contradictory_prose_keeps_working(self) -> None:
        unfinished_messages = (
            "The objective is not complete. I will continue automatically.",
            "I have not completed the tests.",
            "Further verification is required.",
            "I haven't completed the tests.",
            "We haven't verified the fix.",
            "The audit isn't complete.",
            "Tests aren't done.",
            "There is still more work to do.",
            "Some work remains.",
            "Tests remain.",
            "The audit remains to be completed.",
            "I need another turn to finish the audit.",
            "[CONTINUOUS_USER_INPUT_REQUIRED] Which region should I use?",
            "[CONTINUOUS_USER_INPUT_REQUIRED] ?",
            (
                "[CONTINUOUS_USER_INPUT_REQUIRED] Choose JSON? "
                "[CONTINUOUS_USER_INPUT_REQUIRED]"
            ),
            "[CONTINUOUS_USER_INPUT_REQUIRED] Should I /new and continue?",
            "\u672a\u5b8c\u6210\u9805\u76ee\uff1a0\uff1bartifact \u7a3d\u6838\u4ecd\u9700\u5b8c\u6210\u3002",
            "No work remains; the audit still needs work.",
            "Nothing remains to be completed; verification will continue next.",
            "Open is empty; Next Actions: finish deployment later.",
            "\u672a\u9a57\u8b49 live \u884c\u70ba\uff1aproduction Stop path\u3002",
            "\u6c92\u6709\u5269\u9918\u5de5\u4f5c\uff1b\u7a3d\u6838\u4ecd\u9700\u5b8c\u6210\u3002",
            "\u6e2c\u8a66\u5c1a\u672a\u5b8c\u6210\uff0c\u5b8c\u6210\u5f8c\u624d\u6703\u57f7\u884c\u90e8\u7f72\u3002",
            "\u5b8c\u6210\u5f8c\u624d\u6703\u57f7\u884c\u90e8\u7f72\u3002",
            "\u6e2c\u8a66\u5c1a\u672a\u7d50\u675f\u7684\u6b63\u4f8b\u4ecd\u6b63\u78ba\u963b\u64cb\uff1b\u90e8\u7f72\u5c1a\u672a\u5b8c\u6210\u3002",
            (
                "Positive unfinished-work cases are correctly blocked; "
                "the audit is incomplete."
            ),
            (
                "The old conversation is complete. I cannot create a new one; "
                "please start a new chat."
            ),
        )
        for message in unfinished_messages:
            with self.subTest(message=message):
                self.write_lifecycle_receipt(self.lifecycle_receipt())
                runtime = self.project / ".context" / "runtime"
                receipt_path = runtime / "receipts" / f"{self.task_id}.json"

                def reopen(*_args, **_kwargs):
                    runtime.joinpath(f"{self.task_id}.json").write_text(
                        json.dumps(
                            {
                                "project_id": "demo",
                                "task_id": self.task_id,
                                "started_state_sha256": "a" * 64,
                                "started_rules_fingerprint_sha256": "b" * 64,
                            }
                        ),
                        encoding="utf-8",
                    )
                    receipt_path.unlink()
                    return subprocess.CompletedProcess(
                        [], 0, stdout="# BOUNDED CONTEXT BUNDLE\n", stderr=""
                    )

                payload = dict(self.payload, last_assistant_message=message)
                with patch(
                    "rollover_stop_hook.subprocess.run", side_effect=reopen
                ) as run:
                    decision = hook.run(payload)
                self.assertEqual(decision["decision"], "block")
                self.assertIn("already reopened", decision["reason"])
                self.assertTrue(runtime.joinpath(f"{self.task_id}.json").is_file())
                self.assertFalse(receipt_path.exists())
                command = run.call_args.args[0]
                self.assertIn("--resume", command)

                recursive = dict(
                    self.payload,
                    stop_hook_active=True,
                    last_assistant_message="All requested work is complete.",
                )
                ok = subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
                    stderr="",
                )
                with patch(
                    "rollover_stop_hook.subprocess.run", return_value=ok
                ) as pulse:
                    recursive_decision = hook.run(recursive)
                self.assertEqual(recursive_decision["decision"], "block")
                self.assertEqual(pulse.call_args.args[0][-2:], ["--count", "0"])

    def test_completed_receipt_accepts_negated_unfinished_prose(self) -> None:
        self.write_lifecycle_receipt(self.lifecycle_receipt())
        completed_messages = (
            "No further verification is required.",
            "No additional work is needed.",
            "No further audit is pending.",
            "There is no work that remains pending.",
            "No work remains.",
            "No tasks remain.",
            "No further work remains.",
            "None of the tasks remain.",
            "Nothing remains to be completed.",
            "Nothing remains to be done.",
            "Zero tasks remain.",
            "0 tests remain.",
            "The objective is not incomplete.",
            "No further verification will be run.",
            "None of the tests remains pending.",
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
        for message in completed_messages:
            with self.subTest(message=message):
                payload = dict(self.payload, last_assistant_message=message)
                with patch("rollover_stop_hook.subprocess.run") as run:
                    self.assertEqual(hook.run(payload), {})
                    run.assert_not_called()

    def test_contradictory_completion_reopen_preserves_original_start(self) -> None:
        receipt = self.lifecycle_receipt()
        receipt["started_state_sha256"] = "e" * 64
        receipt["started_rules_fingerprint_sha256"] = "f" * 64
        self.write_lifecycle_receipt(receipt)
        runtime = self.project / ".context" / "runtime"
        receipt_path = runtime / "receipts" / f"{self.task_id}.json"

        def reopen(command, **_kwargs):
            runtime.joinpath(f"{self.task_id}.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "task_id": self.task_id,
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
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="# BOUNDED CONTEXT BUNDLE\n",
                stderr="",
            )

        payload = dict(
            self.payload,
            last_assistant_message="The audit remains to be completed.",
        )
        with patch("rollover_stop_hook.subprocess.run", side_effect=reopen):
            decision = hook.run(payload)

        self.assertEqual(decision["decision"], "block")
        self.assertIn("already reopened", decision["reason"])
        self.assertTrue(runtime.joinpath(f"{self.task_id}.json").is_file())
        self.assertFalse(receipt_path.exists())

    def test_source_linked_contradictory_completion_reopens_exact_lineage(
        self,
    ) -> None:
        receipt = self.lifecycle_receipt(source_task_id="source-task")
        self.write_lifecycle_receipt(receipt)
        runtime = self.project / ".context" / "runtime"
        receipt_path = runtime / "receipts" / f"{self.task_id}.json"

        def reopen(command, **_kwargs):
            self.assertEqual(
                command[command.index("--replaces-task") + 1],
                "source-task",
            )
            runtime.joinpath(f"{self.task_id}.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "task_id": self.task_id,
                        "started_state_sha256": receipt["started_state_sha256"],
                        "started_rules_fingerprint_sha256": receipt[
                            "started_rules_fingerprint_sha256"
                        ],
                        "source_task_id": "source-task",
                        "source_audited_state_sha256": receipt[
                            "source_audited_state_sha256"
                        ],
                        "source_audit_rules_fingerprint_sha256": receipt[
                            "source_audit_rules_fingerprint_sha256"
                        ],
                        "source_audit_fingerprint_sha256": receipt[
                            "source_audit_fingerprint_sha256"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            receipt_path.unlink()
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="# BOUNDED CONTEXT BUNDLE\n",
                stderr="",
            )

        payload = dict(
            self.payload,
            last_assistant_message="The audit remains to be completed.",
        )
        with patch("rollover_stop_hook.subprocess.run", side_effect=reopen) as run:
            decision = hook.run(payload)

        self.assertEqual(decision["decision"], "block")
        self.assertIn("already reopened", decision["reason"])
        self.assertIn("--replaces-task", run.call_args.args[0])
        self.assertTrue(runtime.joinpath(f"{self.task_id}.json").is_file())
        self.assertFalse(receipt_path.exists())

    def test_retired_receipt_is_not_overridden_by_source_prose(self) -> None:
        receipt = self.lifecycle_receipt(
            kind="retired",
            replacement_task_id="replacement-task",
        )
        self.write_lifecycle_receipt(receipt)
        payload = dict(
            self.payload,
            last_assistant_message="The replacement target will continue the work.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(payload), {})
            run.assert_not_called()

    def test_schema_v1_lifecycle_receipt_fails_closed(self) -> None:
        receipt = self.lifecycle_receipt()
        receipt["schema_version"] = 1

        self.assert_receipt_fails_closed(receipt)

    def test_source_linked_completed_receipt_requires_exact_work_evidence(
        self,
    ) -> None:
        invalid_evidence = (
            None,
            self.work_evidence("different-task"),
            {
                **self.work_evidence(self.task_id),
                "output_type": "function_call_output",
            },
        )
        for work_evidence in invalid_evidence:
            with self.subTest(work_evidence=work_evidence):
                receipt = self.lifecycle_receipt(source_task_id="old-task")
                receipt["work_evidence"] = work_evidence

                self.assert_receipt_fails_closed(receipt)

    def test_retired_receipt_requires_exact_replacement_work_evidence(self) -> None:
        invalid_evidence = (
            None,
            self.work_evidence("different-task"),
        )
        for replacement_work_evidence in invalid_evidence:
            with self.subTest(replacement_work_evidence=replacement_work_evidence):
                receipt = self.lifecycle_receipt(
                    kind="retired", replacement_task_id="replacement-task"
                )
                receipt["replacement_work_evidence"] = replacement_work_evidence

                self.assert_receipt_fails_closed(receipt)

        malformed = self.lifecycle_receipt(
            kind="retired",
            replacement_task_id="../replacement",
        )
        self.assert_receipt_fails_closed(malformed)

    def test_mismatched_audited_state_and_source_rules_fail_closed(self) -> None:
        mutations = {
            "audited state does not match final state": {
                "audited_state_sha256": "e" * 64,
            },
            "source audited state does not match started state": {
                "source_audited_state_sha256": "e" * 64,
            },
            "source audit rules do not match started rules": {
                "source_audit_rules_fingerprint_sha256": "e" * 64,
            },
        }
        for label, mutation in mutations.items():
            with self.subTest(case=label):
                receipt = self.lifecycle_receipt(source_task_id="old-task")
                receipt.update(mutation)

                self.assert_receipt_fails_closed(receipt)

    def test_malformed_lifecycle_receipt_fails_closed(self) -> None:
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        receipts = self.project.joinpath(".context", "runtime", "receipts")
        receipts.mkdir()
        receipts.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_id": "demo",
                    "task_id": self.task_id,
                    "kind": "completed",
                }
            ),
            encoding="utf-8",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            decision = hook.run(self.payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("objective remains active", decision["reason"])
        run.assert_not_called()

    def test_incomplete_lifecycle_lineage_fails_closed(self) -> None:
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        receipts = self.project.joinpath(".context", "runtime", "receipts")
        receipts.mkdir()
        receipts.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_id": "demo",
                    "task_id": self.task_id,
                    "kind": "completed",
                    "state_sha256": "a" * 64,
                    "started_state_sha256": "a" * 64,
                    "started_rules_fingerprint_sha256": "b" * 64,
                    "audited_state_sha256": "a" * 64,
                    "audit_rules_fingerprint_sha256": "b" * 64,
                    "audit_fingerprint_sha256": "c" * 64,
                    "source_task_id": "old-task",
                    "source_audited_state_sha256": "a" * 64,
                    # Missing both source rules and audit fingerprints.
                    "replacement_task_id": None,
                    "work_evidence": self.work_evidence(self.task_id),
                    "replacement_work_evidence": None,
                    "recorded_at": "2026-08-13T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        decision = hook.run(self.payload)

        self.assertEqual(decision["decision"], "block")

    def test_missing_runtime_without_lifecycle_receipt_fails_closed(self) -> None:
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        with patch("rollover_stop_hook.subprocess.run") as run:
            decision = hook.run(self.payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("underlying user objective remains active", decision["reason"])
        run.assert_not_called()

    def test_first_prompt_runs_preflight_and_controller_target_resumes(self) -> None:
        runtime = self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        )
        runtime.unlink()
        preflight = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "# BOUNDED CONTEXT BUNDLE\n\n"
                f"- Task ID: `{self.task_id}`\n"
            ),
            stderr="",
        )
        prompt_payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt="Build the requested feature.",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=preflight) as run:
            result = hook.run(prompt_payload)
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertNotIn("--resume", run.call_args.args[0])

        self.write_bound_lineage()
        prompt_payload["prompt"] = self.controller_prompt()
        preflight = subprocess.CompletedProcess(
            [], 0, stdout=self.controller_bundle(), stderr=""
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=preflight) as run:
            result = hook.run(prompt_payload)
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        command = run.call_args.args[0]
        self.assertIn("--resume", command)
        self.assertEqual(
            command[command.index("--replaces-task") + 1], self.source_task_id
        )

    def test_later_generic_prompt_does_not_repeat_preflight(self) -> None:
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt="One more request.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(payload), {})
            run.assert_not_called()

    def test_controller_target_preflight_failure_blocks_dispatch(self) -> None:
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt=self.controller_prompt(),
        )
        failures = (
            subprocess.CompletedProcess([], 2, stdout="", stderr="failed"),
            OSError("could not start"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                behavior = (
                    {"side_effect": failure}
                    if isinstance(failure, BaseException)
                    else {"return_value": failure}
                )
                with patch("rollover_stop_hook.subprocess.run", **behavior):
                    result = hook.run(payload)
                self.assertEqual(result["decision"], "block")
                self.assertIn("before dispatch", result["reason"])
                self.assertIn("source Guardian session active", result["reason"])
                self.assertIn("do not ask the user for /new", result["reason"])

    def test_normal_preflight_failure_blocks_instead_of_running_unguarded(self) -> None:
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt="Build the requested feature.",
        )
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
        with patch("rollover_stop_hook.subprocess.run", return_value=failed):
            result = hook.run(payload)
        self.assertEqual(result["decision"], "block")
        self.assertNotIn("systemMessage", result)
        self.assertIn("objective active", result["reason"])

    def test_stop_probe_failure_keeps_objective_active_without_ui_warning(self) -> None:
        with patch(
            "rollover_stop_hook.subprocess.run",
            side_effect=OSError("probe unavailable"),
        ):
            result = hook.run(self.payload)
        self.assertEqual(result["decision"], "block")
        self.assertNotIn("systemMessage", result)
        self.assertIn("DO NOT ECHO", result["reason"])

    def test_controller_target_invalid_success_bundle_blocks_dispatch(self) -> None:
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt=self.controller_prompt(),
        )
        invalid_bundles = {
            "empty": "",
            "not-bounded": f"- Task ID: `{self.task_id}`\n",
            "wrong-task": (
                "# BOUNDED CONTEXT BUNDLE\n\n"
                f"> State SHA-256: `{self.checkpoint_sha256}`\n\n"
                "- Task ID: `different-task`\n"
            ),
            "wrong-state": (
                "# BOUNDED CONTEXT BUNDLE\n\n"
                f"> State SHA-256: `{'d' * 64}`\n\n"
                f"- Task ID: `{self.task_id}`\n"
            ),
            "rollover-sentinel": (
                "# BOUNDED CONTEXT BUNDLE\n\n"
                "CONTEXT_ROLLOVER_REQUIRED project=demo task=old-task\n"
                f"> State SHA-256: `{self.checkpoint_sha256}`\n\n"
                f"- Task ID: `{self.task_id}`\n"
            ),
        }
        for case, stdout in invalid_bundles.items():
            with self.subTest(case=case):
                completed = subprocess.CompletedProcess(
                    [], 0, stdout=stdout, stderr=""
                )
                with patch(
                    "rollover_stop_hook.subprocess.run", return_value=completed
                ):
                    result = hook.run(payload)
                self.assertEqual(result["decision"], "block")
                self.assertIn("invalid bundle before dispatch", result["reason"])
                self.assertIn("source Guardian session active", result["reason"])
                self.assertIn("do not ask the user for /new", result["reason"])

    def test_controller_target_invalid_runtime_lineage_blocks_dispatch(self) -> None:
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt=self.controller_prompt(),
        )
        completed = subprocess.CompletedProcess(
            [], 0, stdout=self.controller_bundle(), stderr=""
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=completed):
            result = hook.run(payload)
        self.assertEqual(result["decision"], "block")
        self.assertIn("invalid source lineage before dispatch", result["reason"])
        self.assertIn("source Guardian session active", result["reason"])

    def test_corrupt_registry_fails_closed_for_prompt_and_stop(self) -> None:
        self.root.joinpath(".context", "registry.json").write_text(
            "{not-json", encoding="utf-8"
        )
        for event in ("Stop", "UserPromptSubmit"):
            with self.subTest(event=event):
                payload = dict(self.payload, hook_event_name=event, prompt="work")
                with patch("rollover_stop_hook.subprocess.run") as run:
                    result = hook.run(payload)
                self.assertEqual(result["decision"], "block")
                self.assertIn("registry is unreadable", result["reason"])
                run.assert_not_called()

    def test_invalid_rollover_policy_fails_closed(self) -> None:
        registry_path = self.root / ".context" / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["automatic_rollover"].pop("objective_completion_authority")
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        with patch("rollover_stop_hook.subprocess.run") as run:
            result = hook.run(self.payload)
        self.assertEqual(result["decision"], "block")
        self.assertIn("policy is missing or invalid", result["reason"])
        run.assert_not_called()

    def test_invalid_session_id_fails_closed_before_task_path_construction(self) -> None:
        for task_id in ("../escape", "bad/task", "x" * 129):
            with self.subTest(task_id=task_id):
                payload = dict(self.payload, session_id=task_id)
                with (
                    patch("rollover_stop_hook.runtime_file") as runtime_file,
                    patch("rollover_stop_hook.subprocess.run") as run,
                ):
                    result = hook.run(payload)
                self.assertEqual(result["decision"], "block")
                self.assertIn("session identity is invalid", result["reason"])
                runtime_file.assert_not_called()
                run.assert_not_called()

    def test_unregistered_nested_repo_does_not_inherit_control(self) -> None:
        nested = self.root / "unknown" / "child"
        nested.mkdir(parents=True)
        nested.parent.joinpath(".git").mkdir()
        registry = json.loads(
            self.root.joinpath(".context", "registry.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(hook.select_project(self.root, registry, nested))

    def test_global_hook_rejects_foreign_lookalike_workspace(self) -> None:
        foreign = self.root.parent / f"{self.root.name}-foreign"
        self.addCleanup(lambda: foreign.exists() and shutil.rmtree(foreign))
        foreign.joinpath(".context").mkdir(parents=True)
        foreign.joinpath(
            ".agents", "skills", "context-guardian", "scripts"
        ).mkdir(parents=True)
        foreign.joinpath(".context", "registry.json").write_text(
            self.root.joinpath(".context", "registry.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        foreign.joinpath(
            ".agents", "skills", "context-guardian", "scripts", "contextctl.py"
        ).write_text("raise SystemExit('must not execute')\n", encoding="utf-8")

        with patch("rollover_stop_hook.subprocess.run") as run:
            result = hook.run(dict(self.payload, cwd=str(foreign)))

        self.assertEqual(result, {})
        run.assert_not_called()

    def test_unregistered_nested_repo_does_not_inherit_registered_child(self) -> None:
        nested = self.project / "unknown" / "child"
        nested.mkdir(parents=True)
        nested.parent.joinpath(".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        registry = json.loads(
            self.root.joinpath(".context", "registry.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(hook.select_project(self.root, registry, nested))
        selected, error = hook.select_task_bound_project(
            self.root,
            registry,
            nested,
            self.task_id,
        )
        self.assertIsNone(selected)
        self.assertIsNone(error)

    def test_controller_marker_selects_child_from_workspace_fallback(self) -> None:
        self.write_bound_lineage()
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            cwd=str(self.root),
            prompt=self.controller_prompt(),
        )
        preflight = subprocess.CompletedProcess(
            [],
            0,
            stdout=self.controller_bundle(),
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=preflight) as run:
            result = hook.run(payload)
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--project") + 1], "demo")
        self.assertEqual(
            command[command.index("--replaces-task") + 1], self.source_task_id
        )

    def test_root_cwd_keeps_exact_child_task_binding_after_controller_prompt(self) -> None:
        stop_payload = dict(self.payload, cwd=str(self.root))
        ok = subprocess.CompletedProcess(
            [],
            0,
            stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=ok) as run:
            decision = hook.run(stop_payload)
        self.assertEqual(decision["decision"], "block")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--project") + 1], "demo")

        later_prompt = dict(
            stop_payload,
            hook_event_name="UserPromptSubmit",
            prompt="Continue the feature work.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(later_prompt), {})
            run.assert_not_called()

        self.write_lifecycle_receipt(self.lifecycle_receipt())
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(stop_payload), {})
            run.assert_not_called()

    def test_nonroot_sibling_cwd_uses_global_exact_task_binding(self) -> None:
        sibling = self.root / "sibling"
        sibling.mkdir()
        registry_path = self.root / ".context" / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["projects"].append(
            {"id": "sibling", "path": "sibling", "state": ".context/state.json"}
        )
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        payload = dict(self.payload, cwd=str(sibling))
        ok = subprocess.CompletedProcess(
            [],
            0,
            stdout="CONTEXT_BUDGET_OK project=demo task=thread-test\n",
            stderr="",
        )
        with patch("rollover_stop_hook.subprocess.run", return_value=ok) as run:
            decision = hook.run(payload)
        self.assertEqual(decision["decision"], "block")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--project") + 1], "demo")

    def test_ambiguous_exact_task_project_binding_fails_closed(self) -> None:
        duplicate = self.root / ".context" / "runtime"
        duplicate.mkdir(exist_ok=True)
        duplicate.joinpath(f"{self.task_id}.json").write_text("{}", encoding="utf-8")
        payload = dict(self.payload, cwd=str(self.root))
        with patch("rollover_stop_hook.subprocess.run") as run:
            decision = hook.run(payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("match multiple registered projects", decision["reason"])
        run.assert_not_called()

    def test_same_rollout_duplicate_binding_uses_furthest_runtime(self) -> None:
        rollout_path = f"sessions/rollout-{self.task_id}.jsonl"
        control_runtime = self.root / ".context" / "runtime"
        control_runtime.mkdir(exist_ok=True)
        control_runtime.joinpath(f"{self.task_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "control",
                    "task_id": self.task_id,
                    "codex_telemetry": {
                        "parse_errors": 0,
                        "rollout_path": rollout_path,
                        "rollout_offset": 100,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "task_id": self.task_id,
                    "codex_telemetry": {
                        "parse_errors": 0,
                        "rollout_path": rollout_path,
                        "rollout_offset": 250,
                    },
                }
            ),
            encoding="utf-8",
        )
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            cwd=str(self.root),
            prompt="Continue the feature work.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(payload), {})
            run.assert_not_called()

    def test_cross_rollout_duplicate_binding_still_fails_closed(self) -> None:
        control_runtime = self.root / ".context" / "runtime"
        control_runtime.mkdir(exist_ok=True)
        for path, project_id, rollout_name, offset in (
            (
                control_runtime / f"{self.task_id}.json",
                "control",
                f"first-{self.task_id}.jsonl",
                100,
            ),
            (
                self.project / ".context" / "runtime" / f"{self.task_id}.json",
                "demo",
                f"second-{self.task_id}.jsonl",
                250,
            ),
        ):
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "task_id": self.task_id,
                        "codex_telemetry": {
                            "parse_errors": 0,
                            "rollout_path": rollout_name,
                            "rollout_offset": offset,
                        },
                    }
                ),
                encoding="utf-8",
            )
        payload = dict(self.payload, cwd=str(self.root))
        with patch("rollover_stop_hook.subprocess.run") as run:
            decision = hook.run(payload)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("match multiple registered projects", decision["reason"])
        run.assert_not_called()

    def test_same_offset_duplicate_binding_uses_latest_runtime_touch(self) -> None:
        rollout_path = f"sessions/rollout-{self.task_id}.jsonl"
        control_runtime = self.root / ".context" / "runtime"
        control_runtime.mkdir(exist_ok=True)
        for path, project_id, last_seen in (
            (
                control_runtime / f"{self.task_id}.json",
                "control",
                "2026-08-14T03:20:00+00:00",
            ),
            (
                self.project / ".context" / "runtime" / f"{self.task_id}.json",
                "demo",
                "2026-08-14T03:21:00+00:00",
            ),
        ):
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "task_id": self.task_id,
                        "last_seen_at": last_seen,
                        "codex_telemetry": {
                            "parse_errors": 0,
                            "rollout_path": rollout_path,
                            "rollout_offset": 250,
                        },
                    }
                ),
                encoding="utf-8",
            )
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            cwd=str(self.root),
            prompt="Continue the feature work.",
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            self.assertEqual(hook.run(payload), {})
            run.assert_not_called()

    def test_controller_marker_project_path_mismatch_blocks_before_preflight(self) -> None:
        payload = dict(
            self.payload,
            hook_event_name="UserPromptSubmit",
            prompt=self.controller_prompt("wrong-path"),
        )
        with patch("rollover_stop_hook.subprocess.run") as run:
            result = hook.run(payload)
        self.assertEqual(result["decision"], "block")
        self.assertIn("project binding is invalid", result["reason"])
        run.assert_not_called()

    def test_cli_decodes_utf8_payload_under_legacy_windows_code_page(self) -> None:
        unicode_project = self.root / "\u4e2d\u6587\u5c08\u6848"
        self.project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).unlink()
        unicode_project.joinpath(".context", "runtime").mkdir(parents=True)
        unicode_project.joinpath(
            ".context", "runtime", f"{self.task_id}.json"
        ).write_text("{}", encoding="utf-8")
        registry_path = self.root / ".context" / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["projects"].append(
            {"id": "unicode", "path": unicode_project.name, "state": ".context/state.json"}
        )
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        payload = json.dumps(
            dict(self.payload, cwd=str(unicode_project)), ensure_ascii=False
        ).encode("utf-8")
        environment = os.environ.copy()
        environment.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp950"})
        cli_script = self.contextctl.with_name("rollover_stop_hook.py")
        shutil.copyfile(Path(hook.__file__).resolve(), cli_script)

        completed = subprocess.run(
            [sys.executable, str(cli_script)],
            input=payload,
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        result = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(result["decision"], "block")
        self.assertIn("underlying user objective remains active", result["reason"])


if __name__ == "__main__":
    unittest.main()
