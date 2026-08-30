from __future__ import annotations

import json
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("contextctl.py")


class ContextCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "demo"
        self.codex_home = self.root / "codex-home"
        (self.root / ".context").mkdir()
        (self.project / ".context").mkdir(parents=True)
        (self.root / "AGENTS.md").write_text(
            "# Root\n\nUse $context-guardian. On CONTEXT_ROLLOVER_REQUIRED, "
            "checkpoint and create a fresh thread. ACTIVE_STATE.md is generated.\n",
            encoding="utf-8",
        )
        (self.project / "AGENTS.md").write_text(
            "# Demo rules\n\nUse `ACTIVE_STATE.md`.\n", encoding="utf-8"
        )
        registry = {
            "schema_version": 1,
            "bundle_limit_bytes": 16384,
            "checkpoint_after_iterations": 3,
            "projects": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "path": "demo",
                    "agent": "AGENTS.md",
                    "active": "ACTIVE_STATE.md",
                    "state": ".context/state.json",
                    "history": ".context/history",
                    "required_authorization": {
                        "schema_version": 1,
                        "status": "authorized",
                        "scope": "project",
                        "executor": "gpt-codex",
                        "project_id": "demo",
                        "environment": "authorized-test-environment",
                        "participant_consent": "all-test-participants-consented",
                        "approved_effects": "controlled-fault-injection",
                        "provider_authorization": "provider-approved-test",
                        "external_dependency_rule": "measure-dont-stop",
                        "decision_rule": "evaluate-action",
                        "continuation_rule": "continue-unaffected-work",
                    },
                }
            ],
        }
        self.registry_path = self.root / ".context" / "registry.json"
        self.registry_path.write_text(
            json.dumps(registry, indent=2), encoding="utf-8"
        )
        self.state = {
            "schema_version": 1,
            "project_id": "demo",
            "project": "Demo",
            "updated_at": "2026-07-27",
            "authorization": {
                "schema_version": 1,
                "status": "authorized",
                "scope": "project",
                "executor": "gpt-codex",
                "project_id": "demo",
                "environment": "authorized-test-environment",
                "participant_consent": "all-test-participants-consented",
                "approved_effects": "controlled-fault-injection",
                "provider_authorization": "provider-approved-test",
                "external_dependency_rule": "measure-dont-stop",
                "decision_rule": "evaluate-action",
                "continuation_rule": "continue-unaffected-work",
            },
            "objective": "Keep the context bundle bounded.",
            "confirmed": [
                "The baseline is deterministic.",
            ],
            "open": ["The next task is not selected."],
            "next_actions": ["Wait for a scoped task."],
            "validation": ["Run the context audit."],
            "lookup": ["Search history only for a named fact."],
        }
        self.state_path = self.project / ".context" / "state.json"
        self.state_path.write_text(
            json.dumps(self.state, indent=2), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_ctl(
        self,
        *args: str,
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        env["CODEX_HOME"] = str(self.codex_home)
        if env_updates:
            env.update(env_updates)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            env=env,
        )

    def set_registry_limits(self, **limits: int) -> None:
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry.update(limits)
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")

    def rollout_path(self, task_id: str) -> Path:
        directory = self.codex_home / "sessions" / "2026" / "07" / "31"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"rollout-2026-07-31T00-00-00-{task_id}.jsonl"

    def write_rollout(self, task_id: str, records: list[dict[str, object]]) -> Path:
        path = self.rollout_path(task_id)
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def session_meta(task_id: str, context_window: int = 1_000) -> dict[str, object]:
        return {
            "timestamp": "2026-07-31T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": task_id,
                "context_window": context_window,
            },
        }

    @staticmethod
    def token_count(input_tokens: int, context_window: int) -> dict[str, object]:
        return {
            "timestamp": "2026-07-31T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": input_tokens},
                    "model_context_window": context_window,
                },
            },
        }

    @staticmethod
    def append_records(path: Path, records: list[dict[str, object]]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def concrete_tool_pair(
        call_id: str,
        *,
        call_type: str = "custom_tool_call",
        output_type: str = "custom_tool_call_output",
    ) -> list[dict[str, object]]:
        return [
            {
                "timestamp": "2026-07-31T00:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": call_type,
                    "call_id": call_id,
                    "name": "exec",
                    "status": "completed",
                    "input": "await tools.shell_command({command: 'git status'})",
                },
            },
            {
                "timestamp": "2026-07-31T00:00:07Z",
                "type": "response_item",
                "payload": {
                    "type": output_type,
                    "call_id": call_id,
                    "output": "clean",
                },
            },
        ]

    @staticmethod
    def state_hash(state: dict[str, object]) -> str:
        canonical = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def render(self) -> None:
        result = self.run_ctl("render")
        self.assertEqual(result.returncode, 0, result.stderr)

    def prepare_replacement_runtime(
        self,
        source_task: str,
        target_task: str,
    ) -> tuple[Path, Path, Path]:
        self.render()
        source_preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_preflight.returncode, 0, source_preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)

        target_rollout = self.write_rollout(
            target_task,
            [self.session_meta(target_task)],
        )
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            source_task,
            "--resume",
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)
        runtime_dir = self.project / ".context" / "runtime"
        return (
            runtime_dir / f"{source_task}.json",
            runtime_dir / f"{target_task}.json",
            target_rollout,
        )

    def test_preflight_is_unchanged_when_history_grows(self) -> None:
        self.render()
        before = self.run_ctl("preflight", "--project", "demo")
        self.assertEqual(before.returncode, 0, before.stderr)

        history = self.project / "legacy_handoff.md"
        history.write_text("x" * 1_000_000, encoding="utf-8")

        after = self.run_ctl("preflight", "--project", "demo")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(before.stdout, after.stdout)
        self.assertLess(len(after.stdout.encode("utf-8")), 16384)
        self.assertIn("Static Rule References", after.stdout)
        self.assertIn("## Project Rules", after.stdout)
        self.assertIn("Demo rules", after.stdout)
        self.assertNotIn("# Root", after.stdout)

        full = self.run_ctl(
            "preflight", "--project", "demo", "--full-rules"
        )
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("Demo rules", full.stdout)

    def test_full_rules_deduplicates_shared_root_agent(self) -> None:
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["projects"][0]["agent"] = "../AGENTS.md"
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        self.render()

        result = self.run_ctl(
            "preflight", "--project", "demo", "--full-rules"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("\n# Root\n"), 1)
        self.assertNotIn("## Project Rules", result.stdout)

    def test_state_lookup_cannot_point_into_another_workspace_project(self) -> None:
        foreign = self.root / "other-project" / "README.md"
        foreign.parent.mkdir()
        foreign.write_text("fixture\n", encoding="utf-8")
        self.state["lookup"] = [str(foreign)]
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

        result = self.run_ctl("render", "--project", "demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("points outside the registered project root", result.stderr)

    def test_render_keeps_scope_values_out_of_default_context(self) -> None:
        self.render()
        rendered = (self.project / "ACTIVE_STATE.md").read_text(encoding="utf-8")
        self.assertIn("## Scope Metadata", rendered)
        self.assertIn("Structured project scope is present and validated.", rendered)
        self.assertNotIn("authorized-test-environment", rendered)
        self.assertNotIn("all-test-participants-consented", rendered)
        self.assertNotIn("controlled-fault-injection", rendered)
        self.assertNotIn("provider-approved-test", rendered)
        self.assertNotIn("measure-dont-stop", rendered)
        self.assertNotIn("## Lookup On Demand", rendered)
        self.assertNotIn("Search history only for a named fact.", rendered)

    def test_audit_detects_generated_view_drift(self) -> None:
        self.render()
        active = self.project / "ACTIVE_STATE.md"
        active.write_text(
            active.read_text(encoding="utf-8") + "\nmanual drift\n",
            encoding="utf-8",
        )
        result = self.run_ctl("audit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ACTIVE_STATE drift", result.stdout)

    def test_audit_requires_fresh_thread_rollover_contract(self) -> None:
        self.render()
        (self.root / "AGENTS.md").write_text(
            "# Root\n\nCheckpoint forever in the same thread.\n",
            encoding="utf-8",
        )
        result = self.run_ctl("audit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing fresh-thread rollover contract", result.stdout)

    def test_audit_accepts_explicit_clean_room_hooks_opt_out(self) -> None:
        self.render()
        (self.root / "AGENTS.md").write_text(
            "# Root\n\nUse $context-guardian only for opted-in long tasks.\n",
            encoding="utf-8",
        )
        codex_dir = self.root / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\nhooks = false\n",
            encoding="utf-8",
        )

        result = self.run_ctl("audit")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_desktop_rollover_contract_gates_source_finish_on_private_receipt(self) -> None:
        contract = Path(__file__).resolve().parents[1].joinpath(
            "references",
            "desktop-rollover.md",
        ).read_text(encoding="utf-8")
        checkpoint = contract.index("Checkpoint the unfinished objective")
        create = contract.index("Call `create_thread` exactly once")
        receipt = contract.index("Validate the private")
        wait = contract.index("Follow the target with `wait_threads`")
        finish = contract.index("Only after the validated target receipt")

        self.assertLess(checkpoint, create)
        self.assertLess(create, wait)
        self.assertLess(wait, finish)
        self.assertLess(receipt, finish)
        self.assertNotIn("ROLLOVER_TARGET_READY", contract)
        self.assertIn("authorize source finish", contract)
        self.assertIn("never create a duplicate", contract)
        self.assertIn("Do not mention the rollover", contract)
        self.assertIn("Emit no user-facing rollover", contract)
        self.assertIn("trusted synchronous Stop boundary guard", contract)
        self.assertIn("paused Goal lease", contract)
        self.assertIn("without another user message", contract)
        self.assertIn("plaintext marker", contract)

        workspace_root = Path(__file__).resolve().parents[4]
        registry = json.loads(
            workspace_root.joinpath(".context", "registry.json").read_text(
                encoding="utf-8"
            )
        )
        policy = registry["automatic_rollover"]
        self.assertEqual(
            policy["objective_completion_authority"],
            "exact-task-completed-lifecycle-receipt",
        )
        self.assertEqual(
            policy["source_retirement_authority"],
            "exact-source-retired-receipt-after-verified-target-work",
        )
        self.assertTrue(policy["continue_until_objective_complete"])
        self.assertEqual(
            policy["desktop_stop_boundary_guard"],
            "trusted-synchronous-user-hook",
        )
        self.assertFalse(policy["desktop_visual_silence"])
        self.assertEqual(policy["strict_clear_semantics"], "app-server-only")

    def test_root_gitignore_protects_runtime_and_child_repo_boundaries(self) -> None:
        workspace_root = Path(__file__).resolve().parents[4]
        ignored = (
            "ACTIVE_STATE.md",
            ".context/state.json",
            ".context/history/example.json",
            ".context/runtime/example.json",
            ".context/sample-project/state.json",
            ".context/sample-project/runtime/example.json",
            ".workspace/.cache/example",
            ".workspace/npm-cache/example",
            ".workspace/releases/example",
            ".wrangler/example",
            "risk-attribution-auth-boundary/example",
            "sample-alias/example",
            "sample-adaptive-pool/example",
            "sample-child-project/example",
        )
        for path in ignored:
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", "--", path],
                    cwd=workspace_root,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
        for path in (
            ".context/registry.json",
            ".agents/skills/context-guardian/scripts/contextctl.py",
            ".agents/tools/codex-continuous/continuous_cli.py",
        ):
            with self.subTest(trackable=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", "--", path],
                    cwd=workspace_root,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)

    def test_checkpoint_archives_and_regenerates(self) -> None:
        self.render()
        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Use a new bounded objective."
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(
            json.dumps(candidate, indent=2), encoding="utf-8"
        )

        result = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(installed["objective"], candidate["objective"])
        active = (self.project / "ACTIVE_STATE.md").read_text(encoding="utf-8")
        self.assertIn(candidate["objective"], active)
        archives = list((self.project / ".context" / "history").glob("*.json"))
        self.assertEqual(len(archives), 1)

    def test_invalid_state_is_rejected(self) -> None:
        self.state["objective"] = "x" * 801
        self.state_path.write_text(
            json.dumps(self.state, indent=2), encoding="utf-8"
        )
        result = self.run_ctl("render")
        self.assertEqual(result.returncode, 1)
        self.assertIn("objective exceeds 800 characters", result.stderr)

    def test_stale_checkpoint_cannot_overwrite_newer_state(self) -> None:
        self.render()
        base_hash = self.state_hash(self.state)

        first = dict(self.state)
        first["base_sha256"] = base_hash
        first["objective"] = "Install the first writer's state."
        first_path = self.root / "first.json"
        first_path.write_text(json.dumps(first, indent=2), encoding="utf-8")

        stale = dict(self.state)
        stale["base_sha256"] = base_hash
        stale["objective"] = "A stale writer must not overwrite the first."
        stale_path = self.root / "stale.json"
        stale_path.write_text(json.dumps(stale, indent=2), encoding="utf-8")

        first_result = self.run_ctl(
            "checkpoint", "--project", "demo", "--input", str(first_path)
        )
        self.assertEqual(first_result.returncode, 0, first_result.stderr)

        stale_result = self.run_ctl(
            "checkpoint", "--project", "demo", "--input", str(stale_path)
        )
        self.assertEqual(stale_result.returncode, 1)
        self.assertIn("stale checkpoint", stale_result.stderr)
        installed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(installed["objective"], first["objective"])

    def test_structured_authorization_cannot_be_downgraded(self) -> None:
        self.render()
        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["authorization"] = dict(self.state["authorization"])
        candidate["authorization"]["status"] = "revoked"
        candidate_path = self.root / "forbidden.json"
        candidate_path.write_text(
            json.dumps(candidate, indent=2), encoding="utf-8"
        )
        result = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("authorization invariant mismatch", result.stderr)

    def test_checkpoint_can_migrate_legacy_state_to_structured_authorization(self) -> None:
        legacy = dict(self.state)
        legacy.pop("authorization")
        self.state_path.write_text(json.dumps(legacy), encoding="utf-8")
        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(legacy)
        candidate_path = self.root / "migration.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        result = self.run_ctl(
            "checkpoint", "--project", "demo", "--input", str(candidate_path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(installed["authorization"]["status"], "authorized")

    def test_stale_checkpoint_lock_is_recovered(self) -> None:
        self.render()
        lock_path = self.project / ".context" / "checkpoint.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 999_999_999,
                    "hostname": socket.gethostname(),
                    "created_at": "2000-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Recover the stale lock."
        candidate_path = self.root / "stale-lock.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        result = self.run_ctl(
            "checkpoint", "--project", "demo", "--input", str(candidate_path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock_path.exists())

    def test_live_checkpoint_lock_is_rejected(self) -> None:
        self.render()
        lock_path = self.project / ".context" / "checkpoint.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "created_at": "2026-07-27T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate_path = self.root / "live-lock.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        result = self.run_ctl(
            "checkpoint", "--project", "demo", "--input", str(candidate_path)
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("checkpoint lock is active", result.stderr)

    def test_task_budget_forces_checkpoint_and_resets(self) -> None:
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", "task-1"
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("Iterations: 0/3", preflight.stdout)

        first = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", "task-1", "--count", "2"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("CONTEXT_BUDGET_OK", first.stdout)
        due = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", "task-1"
        )
        self.assertEqual(due.returncode, 2)
        self.assertIn("CONTEXT_CHECKPOINT_DUE", due.stdout)

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Reset the task budget."
        candidate_path = self.root / "budget.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            "task-1",
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", "task-1"
        )
        self.assertEqual(pulse.returncode, 0, pulse.stderr)
        self.assertIn("iterations=1/3", pulse.stdout)

    def test_checkpoint_before_budget_due_resets_iteration_counter(self) -> None:
        task_id = "task-proactive-checkpoint"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        pulse = self.run_ctl(
            "pulse",
            "--project",
            "demo",
            "--task-id",
            task_id,
            "--count",
            "2",
        )
        self.assertEqual(pulse.returncode, 0, pulse.stderr)

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Checkpoint proactively before the limit."
        candidate_path = self.root / "proactive.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            task_id,
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        after = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("iterations=1/3", after.stdout)

    def test_finish_respects_live_runtime_lock(self) -> None:
        task_id = "task-finish-lock"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        runtime_path = (
            self.project / ".context" / "runtime" / f"{task_id}.json"
        )
        lock_path = runtime_path.with_suffix(".lock")
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "created_at": "2026-07-31T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        finish = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(finish.returncode, 1)
        self.assertIn("checkpoint lock is active", finish.stderr)
        self.assertTrue(runtime_path.is_file())

    def test_plain_finish_requires_completed_state_and_task_bound_audit(self) -> None:
        task_id = "task-completion-gate"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        started_rules_fingerprint = json.loads(
            runtime_path.read_text(encoding="utf-8")
        )["started_rules_fingerprint_sha256"]

        task_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(task_audit.returncode, 0, task_audit.stderr)
        unfinished = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(unfinished.returncode, 1)
        self.assertIn("objective is still active", unfinished.stderr)
        self.assertTrue(runtime_path.is_file())

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["open"] = []
        candidate["next_actions"] = []
        candidate_path = self.root / "completed-candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            task_id,
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

        stale_audit = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(stale_audit.returncode, 1)
        self.assertIn("task-bound audit receipt is missing or stale", stale_audit.stderr)
        self.assertTrue(runtime_path.is_file())

        final_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(final_audit.returncode, 0, final_audit.stderr)
        finished = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertFalse(runtime_path.exists())
        receipt = json.loads(
            self.project.joinpath(
                ".context", "runtime", "receipts", f"{task_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["kind"], "completed")
        self.assertEqual(
            receipt["started_rules_fingerprint_sha256"],
            started_rules_fingerprint,
        )

        retry = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_completed_task_resume_recreates_exact_runtime_and_removes_receipt(
        self,
    ) -> None:
        task_id = "completed-task-resume"
        self.state["open"] = []
        self.state["next_actions"] = []
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        rollout = self.write_rollout(task_id, [self.session_meta(task_id)])
        self.render()

        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        finished_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

        finish = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        receipt_path = self.project.joinpath(
            ".context", "runtime", "receipts", f"{task_id}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertFalse(runtime_path.exists())

        invalid_rebind = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            task_id,
            "--replaces-task",
            "invented-source",
            "--resume",
        )
        self.assertEqual(invalid_rebind.returncode, 1)
        self.assertIn("source-free completed receipt", invalid_rebind.stderr)
        self.assertFalse(runtime_path.exists())
        self.assertTrue(receipt_path.is_file())

        resumed = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            task_id,
            "--resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        recreated = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(recreated["schema_version"], 1)
        self.assertEqual(recreated["project_id"], receipt["project_id"])
        self.assertEqual(recreated["task_id"], receipt["task_id"])
        self.assertEqual(recreated["base_sha256"], receipt["state_sha256"])
        self.assertEqual(
            recreated["started_state_sha256"],
            receipt["started_state_sha256"],
        )
        self.assertEqual(
            recreated["started_rules_fingerprint_sha256"],
            receipt["started_rules_fingerprint_sha256"],
        )
        self.assertEqual(recreated["substantive_iterations"], 0)
        self.assertEqual(recreated["started_rollout_offset"], rollout.stat().st_size)
        self.assertEqual(
            recreated["started_rollout_file_id"],
            finished_runtime["started_rollout_file_id"],
        )
        self.assertNotIn("audited_state_sha256", recreated)
        self.assertNotIn("audit_fingerprint_sha256", recreated)
        self.assertNotIn("audit_rules_fingerprint_sha256", recreated)
        self.assertNotIn("work_evidence", recreated)
        self.assertFalse(receipt_path.exists())

    def test_plain_finish_does_not_treat_codex_thread_id_as_replaced_by(self) -> None:
        task_id = "task-plain-finish-env"
        self.state["open"] = []
        self.state["next_actions"] = []
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        task_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(task_audit.returncode, 0, task_audit.stderr)

        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        finished = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            task_id,
            env_updates={"CODEX_THREAD_ID": task_id},
        )

        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("kind=completed", finished.stdout)
        self.assertFalse(runtime_path.exists())

    def test_plain_finish_rejects_root_agent_fingerprint_drift_after_audit(
        self,
    ) -> None:
        task_id = "task-stale-root-audit"
        self.state["open"] = []
        self.state["next_actions"] = []
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        task_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(task_audit.returncode, 0, task_audit.stderr)

        root_agent = self.root / "AGENTS.md"
        root_agent.write_text(
            root_agent.read_text(encoding="utf-8")
            + "\n<!-- semantically valid audit fingerprint drift -->\n",
            encoding="utf-8",
        )
        current_audit = self.run_ctl("audit", "--project", "demo")
        self.assertEqual(current_audit.returncode, 0, current_audit.stderr)

        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        finished = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )

        self.assertEqual(finished.returncode, 1)
        self.assertTrue(runtime_path.is_file())

    def test_finish_rejects_raw_runtime_absence_without_receipt(self) -> None:
        self.render()
        result = self.run_ctl(
            "finish", "--project", "demo", "--task-id", "never-started"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("completion is not proven", result.stderr)

    def test_finish_rejects_malformed_idempotent_receipt(self) -> None:
        self.render()
        task_id = "malformed-receipt"
        receipt = self.project.joinpath(
            ".context", "runtime", "receipts", f"{task_id}.json"
        )
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_id": "demo",
                    "task_id": task_id,
                    "kind": "completed",
                    "replacement_task_id": None,
                }
            ),
            encoding="utf-8",
        )
        result = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("receipt state_sha256", result.stderr)

    def test_retired_receipt_rejects_invalid_replacement_task_id(self) -> None:
        source_task = "invalid-retired-source"
        target_task = "invalid-retired-target"
        _, _, target_rollout = self.prepare_replacement_runtime(
            source_task,
            target_task,
        )
        self.append_records(
            target_rollout,
            self.concrete_tool_pair("call-retired-receipt"),
        )
        finish = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        receipt_path = self.project.joinpath(
            ".context", "runtime", "receipts", f"{source_task}.json"
        )
        valid_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

        for invalid_task_id in ("../invalid", "x" * 129):
            with self.subTest(invalid_task_id=invalid_task_id):
                receipt = json.loads(json.dumps(valid_receipt))
                receipt["replacement_task_id"] = invalid_task_id
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                retry = self.run_ctl(
                    "finish",
                    "--project",
                    "demo",
                    "--task-id",
                    source_task,
                    "--replaced-by",
                    target_task,
                )
                self.assertEqual(retry.returncode, 1)
                self.assertIn(
                    "retired receipt replacement task is invalid",
                    retry.stderr,
                )

    def test_repeated_preflight_same_task_emits_already_active(self) -> None:
        task_id = "task-repeat"
        self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        first = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("# BOUNDED CONTEXT BUNDLE", first.stdout)

        second = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(
            second.stdout.startswith("CONTEXT_PREFLIGHT_ALREADY_ACTIVE ")
        )
        self.assertEqual(len(second.stdout.strip().splitlines()), 1)
        self.assertNotIn("## Active State", second.stdout)

    def test_fresh_preflight_omits_prior_objective_unless_resumed(self) -> None:
        self.render()
        fresh = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", "fresh-task"
        )
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertIn("## Prior State Pointer", fresh.stdout)
        self.assertNotIn("## Active State", fresh.stdout)

        resumed = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            "resumed-task",
            "--resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("## Active State", resumed.stdout)
        self.assertNotIn("## Prior State Pointer", resumed.stdout)

    def test_preflight_without_task_identity_is_pointer_only(self) -> None:
        self.render()
        implicit = self.run_ctl("preflight", "--project", "demo")
        self.assertEqual(implicit.returncode, 0, implicit.stderr)
        self.assertIn("## Prior State Pointer", implicit.stdout)
        self.assertNotIn("## Active State", implicit.stdout)

        no_session = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--no-session",
        )
        self.assertEqual(no_session.returncode, 0, no_session.stderr)
        self.assertIn("## Prior State Pointer", no_session.stdout)
        self.assertNotIn("## Active State", no_session.stdout)

    def test_existing_task_resume_reinjects_active_state(self) -> None:
        task_id = "existing-resume"
        self.render()
        first = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        resumed = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            task_id,
            "--resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("## Active State", resumed.stdout)

    def test_refresh_rebases_external_state_and_preserves_runtime_budget(self) -> None:
        task_id = "task-refresh-state"
        self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        first_pulse = self.run_ctl(
            "pulse",
            "--project",
            "demo",
            "--task-id",
            task_id,
            "--count",
            "1",
        )
        self.assertEqual(first_pulse.returncode, 0, first_pulse.stderr)

        runtime_path = (
            self.project / ".context" / "runtime" / f"{task_id}.json"
        )
        before = json.loads(runtime_path.read_text(encoding="utf-8"))
        changed_state = dict(self.state)
        changed_state["objective"] = "Adopt a valid external state update."
        self.state_path.write_text(
            json.dumps(changed_state, indent=2), encoding="utf-8"
        )

        drifted_pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(drifted_pulse.returncode, 1)
        self.assertIn("run refresh --project demo", drifted_pulse.stderr)

        refresh = self.run_ctl(
            "refresh", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self.assertTrue(refresh.stdout.startswith("CONTEXT_STATE_REFRESHED "))
        self.assertEqual(len(refresh.stdout.strip().splitlines()), 1)
        self.assertIn(
            f"state_sha256={self.state_hash(changed_state)}", refresh.stdout
        )
        self.assertNotIn(changed_state["objective"], refresh.stdout)

        after = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(after["base_sha256"], self.state_hash(changed_state))
        self.assertEqual(
            after["substantive_iterations"], before["substantive_iterations"]
        )
        self.assertEqual(after["codex_telemetry"], before["codex_telemetry"])

        resumed_pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(resumed_pulse.returncode, 0, resumed_pulse.stderr)
        self.assertIn("CONTEXT_BUDGET_OK", resumed_pulse.stdout)

    def test_refresh_current_is_compact(self) -> None:
        task_id = "task-refresh-current"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)

        refresh = self.run_ctl(
            "refresh", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self.assertTrue(refresh.stdout.startswith("CONTEXT_STATE_CURRENT "))
        self.assertEqual(len(refresh.stdout.strip().splitlines()), 1)
        short_sha = self.state_hash(self.state)[:12]
        self.assertIn(f"old_sha256={short_sha}", refresh.stdout)
        self.assertIn(f"state_sha256={self.state_hash(self.state)}", refresh.stdout)
        self.assertNotIn("## Active State", refresh.stdout)

    def test_refresh_requires_an_existing_runtime(self) -> None:
        self.render()
        refresh = self.run_ctl(
            "refresh", "--project", "demo", "--task-id", "missing-task"
        )
        self.assertEqual(refresh.returncode, 1)
        self.assertIn("runtime session is missing", refresh.stderr)
        self.assertFalse((self.project / ".context" / "runtime").exists())

    def test_refresh_is_exposed_in_parser_help(self) -> None:
        top_help = self.run_ctl("--help")
        self.assertEqual(top_help.returncode, 0, top_help.stderr)
        self.assertIn("refresh", top_help.stdout)

        refresh_help = self.run_ctl("refresh", "--help")
        self.assertEqual(refresh_help.returncode, 0, refresh_help.stderr)
        self.assertIn("--project", refresh_help.stdout)
        self.assertIn("--task-id", refresh_help.stdout)

    def test_preflight_requires_rollover_for_already_compacted_thread(self) -> None:
        task_id = "task-compacted"
        self.set_registry_limits(max_session_compactions=0)
        self.write_rollout(
            task_id,
            [
                self.session_meta(task_id),
                {
                    "timestamp": "2026-07-31T00:00:02Z",
                    "type": "compacted",
                    "payload": {"replacement_history": []},
                },
                {
                    "timestamp": "2026-07-31T00:00:03Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
            ],
        )
        self.render()
        result = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id, "--resume"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("CONTEXT_ROLLOVER_REQUIRED "))
        self.assertIn("reasons=compacted", result.stdout)
        self.assertIn("compactions=1", result.stdout)
        self.assertIn("## Active State", result.stdout)
        runtime_path = (
            self.project / ".context" / "runtime" / f"{task_id}.json"
        )
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["codex_telemetry"]["compactions"], 1)
        self.assertFalse(
            Path(runtime["codex_telemetry"]["rollout_path"]).is_absolute()
        )

    def test_checkpoint_preserves_compaction_budget_until_rollover(self) -> None:
        task_id = "task-new-compaction"
        self.set_registry_limits(max_session_compactions=0)
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:04Z",
                    "type": "compacted",
                    "payload": {"replacement_history": []},
                },
                {
                    "timestamp": "2026-07-31T00:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("CONTEXT_ROLLOVER_REQUIRED", pulse.stdout)
        self.assertIn("reasons=compacted", pulse.stdout)
        self.assertIn("compactions=1", pulse.stdout)

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Preserve telemetry across checkpoint."
        candidate_path = self.root / "telemetry-checkpoint.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            task_id,
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        after = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(after.returncode, 2, after.stderr)
        self.assertIn("CONTEXT_ROLLOVER_REQUIRED", after.stdout)
        self.assertIn("compactions=1", after.stdout)

    def test_controller_rollover_creates_clean_target_runtime(self) -> None:
        old_task = "task-rollover-old"
        new_task = "task-rollover-new"
        self.set_registry_limits(max_session_compactions=0)
        rollout = self.write_rollout(
            old_task,
            [self.session_meta(old_task), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:04Z",
                    "type": "compacted",
                    "payload": {"replacement_history": []},
                }
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", old_task, "--count", "0"
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("CONTEXT_ROLLOVER_REQUIRED", pulse.stdout)

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["objective"] = "Continue automatically in the clean target task."
        candidate_path = self.root / "rollover-checkpoint.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            old_task,
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        source_runtime_path = (
            self.project / ".context" / "runtime" / f"{old_task}.json"
        )
        source_runtime = json.loads(
            source_runtime_path.read_text(encoding="utf-8")
        )

        target_rollout = self.write_rollout(
            new_task,
            [self.session_meta(new_task)],
        )
        target = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
            "--resume",
        )
        self.assertEqual(target.returncode, 0, target.stderr)
        self.assertIn(f"Task ID: `{new_task}`", target.stdout)
        self.assertNotIn(old_task, target.stdout)
        self.assertFalse(target.stdout.startswith("CONTEXT_ROLLOVER_REQUIRED"))
        target_runtime_path = (
            self.project / ".context" / "runtime" / f"{new_task}.json"
        )
        target_runtime = json.loads(target_runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(target_runtime["task_id"], new_task)
        self.assertEqual(target_runtime["substantive_iterations"], 0)
        self.assertEqual(target_runtime["codex_telemetry"]["compactions"], 0)
        self.assertEqual(
            target_runtime["started_state_sha256"],
            target_runtime["base_sha256"],
        )
        self.assertEqual(
            target_runtime["started_rules_fingerprint_sha256"],
            source_runtime["audit_rules_fingerprint_sha256"],
        )
        self.assertEqual(target_runtime["source_task_id"], old_task)
        self.assertEqual(
            target_runtime["source_audited_state_sha256"],
            source_runtime["audited_state_sha256"],
        )

        started_rules_fingerprint = target_runtime[
            "started_rules_fingerprint_sha256"
        ]
        root_agent = self.root / "AGENTS.md"
        root_agent.write_text(
            root_agent.read_text(encoding="utf-8")
            + "\n<!-- legal target-side rules change -->\n",
            encoding="utf-8",
        )
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["target_side_note"] = "rules changed after target start"
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        repeated_target = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
            "--resume",
        )
        self.assertEqual(repeated_target.returncode, 0, repeated_target.stderr)
        target_runtime = json.loads(
            target_runtime_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            target_runtime["started_rules_fingerprint_sha256"],
            started_rules_fingerprint,
        )

        preflight_only = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(preflight_only.returncode, 1)
        self.assertIn("no completed concrete tool call", preflight_only.stderr)
        self.assertTrue(source_runtime_path.is_file())

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-concrete-work",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git status'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-concrete-work",
                        "output": "clean",
                    },
                },
            ],
        )

        finish = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        self.assertTrue(target_runtime_path.is_file())
        retired_receipt = json.loads(
            self.project.joinpath(
                ".context", "runtime", "receipts", f"{old_task}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(retired_receipt["kind"], "retired")
        self.assertEqual(retired_receipt["replacement_task_id"], new_task)
        self.assertEqual(
            retired_receipt["started_rules_fingerprint_sha256"],
            source_runtime["started_rules_fingerprint_sha256"],
        )
        retry = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(target_runtime_path.is_file())

    def test_replacement_finish_requires_matched_concrete_call_output(self) -> None:
        source_task = "work-proof-source"
        target_task = "work-proof-target"
        source_runtime, _, target_rollout = self.prepare_replacement_runtime(
            source_task,
            target_task,
        )

        preflight_only = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(preflight_only.returncode, 1)
        self.assertIn("no completed concrete tool call", preflight_only.stderr)

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-lifecycle-only",
                        "name": "update_plan",
                        "status": "completed",
                        "input": {"plan": []},
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-lifecycle-only",
                        "output": "ok",
                    },
                },
            ],
        )
        lifecycle_only = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(lifecycle_only.returncode, 1)
        self.assertIn("no completed concrete tool call", lifecycle_only.stderr)

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-concrete-unmatched",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git status'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-wrong-id",
                        "output": "clean",
                    },
                },
            ],
        )
        unmatched = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(unmatched.returncode, 1)
        self.assertIn("no completed concrete tool call", unmatched.stderr)
        self.assertTrue(source_runtime.is_file())

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-concrete-unmatched",
                        "output": "clean",
                    },
                }
            ],
        )
        matched = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertFalse(source_runtime.exists())

    def test_replacement_finish_rejects_same_id_protocol_mismatch(self) -> None:
        source_task = "protocol-mismatch-source"
        target_task = "protocol-mismatch-target"
        source_runtime, _, target_rollout = self.prepare_replacement_runtime(
            source_task,
            target_task,
        )
        self.append_records(
            target_rollout,
            self.concrete_tool_pair(
                "call-protocol-mismatch",
                output_type="function_call_output",
            ),
        )

        mismatched = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("no completed concrete tool call", mismatched.stderr)
        self.assertTrue(source_runtime.is_file())

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:08Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-protocol-mismatch",
                        "output": "clean",
                    },
                }
            ],
        )
        matched = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertFalse(source_runtime.exists())

    def test_retired_receipt_rejects_same_id_protocol_mismatch(self) -> None:
        source_task = "receipt-protocol-source"
        target_task = "receipt-protocol-target"
        _, _, target_rollout = self.prepare_replacement_runtime(
            source_task,
            target_task,
        )
        self.append_records(
            target_rollout,
            self.concrete_tool_pair("call-valid-protocol"),
        )
        finish = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        receipt_path = self.project.joinpath(
            ".context", "runtime", "receipts", f"{source_task}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        evidence = receipt["replacement_work_evidence"]
        self.assertEqual(evidence["call_type"], "custom_tool_call")
        evidence["output_type"] = "function_call_output"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        retry = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(retry.returncode, 1)
        self.assertIn("call/output types are invalid", retry.stderr)

    def test_legacy_work_baseline_is_persisted_before_requiring_new_work(
        self,
    ) -> None:
        source_task = "legacy-baseline-source"
        target_task = "legacy-baseline-target"
        source_runtime, target_runtime, target_rollout = (
            self.prepare_replacement_runtime(source_task, target_task)
        )
        legacy_runtime = json.loads(target_runtime.read_text(encoding="utf-8"))
        legacy_runtime.pop("started_rollout_offset")
        legacy_runtime.pop("started_rollout_file_id")
        legacy_runtime.pop("started_tool_calls")
        target_runtime.write_text(json.dumps(legacy_runtime), encoding="utf-8")
        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-before-rebaseline",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git status'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-before-rebaseline",
                        "output": "clean",
                    },
                },
            ],
        )

        rebaseline = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(rebaseline.returncode, 1)
        self.assertIn("lacked a trusted work baseline", rebaseline.stderr)
        persisted = json.loads(target_runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["started_rollout_offset"],
            target_rollout.stat().st_size,
        )
        self.assertTrue(persisted["started_rollout_file_id"])
        self.assertNotIn("work_evidence", persisted)
        self.assertTrue(source_runtime.is_file())

        no_new_work = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(no_new_work.returncode, 1)
        self.assertIn("no completed concrete tool call", no_new_work.stderr)

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-after-rebaseline",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git diff'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-after-rebaseline",
                        "output": "clean",
                    },
                },
            ],
        )
        after_new_work = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(after_new_work.returncode, 0, after_new_work.stderr)
        self.assertFalse(source_runtime.exists())

    def test_replaced_target_rollout_rebaselines_then_accepts_new_work(self) -> None:
        source_task = "rotated-source"
        target_task = "rotated-target"
        source_runtime, target_runtime, target_rollout = (
            self.prepare_replacement_runtime(source_task, target_task)
        )
        replacement = target_rollout.with_suffix(".replacement")
        replacement.write_text(
            json.dumps(self.session_meta(target_task)) + "\n",
            encoding="utf-8",
        )
        replacement.replace(target_rollout)

        rebaseline = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(rebaseline.returncode, 1)
        self.assertIn("rollout changed after preflight", rebaseline.stderr)
        persisted = json.loads(target_runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["started_rollout_offset"],
            target_rollout.stat().st_size,
        )
        self.assertNotIn("work_evidence", persisted)
        self.assertTrue(source_runtime.is_file())

        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-after-rollout-rotation",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git status'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-after-rollout-rotation",
                        "output": "clean",
                    },
                },
            ],
        )
        finished = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertFalse(source_runtime.exists())

    def test_same_inode_rollout_truncation_rebaselines_before_new_work(
        self,
    ) -> None:
        source_task = "truncated-source"
        target_task = "truncated-target"
        source_runtime, target_runtime, target_rollout = (
            self.prepare_replacement_runtime(source_task, target_task)
        )
        original_stat = target_rollout.stat()
        original_runtime = json.loads(target_runtime.read_text(encoding="utf-8"))
        original_baseline = original_runtime["started_rollout_offset"]
        compact_session_meta = json.dumps(
            self.session_meta(target_task),
            separators=(",", ":"),
        ) + "\n"
        target_rollout.write_text(compact_session_meta, encoding="utf-8")
        rewritten_stat = target_rollout.stat()
        self.assertEqual(
            (rewritten_stat.st_dev, rewritten_stat.st_ino),
            (original_stat.st_dev, original_stat.st_ino),
        )
        self.assertLess(rewritten_stat.st_size, original_baseline)

        rebaseline = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(rebaseline.returncode, 1)
        self.assertIn("rollout changed after preflight", rebaseline.stderr)
        persisted = json.loads(target_runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["started_rollout_file_id"],
            original_runtime["started_rollout_file_id"],
        )
        self.assertEqual(
            persisted["started_rollout_offset"],
            rewritten_stat.st_size,
        )
        self.assertNotIn("work_evidence", persisted)
        self.assertTrue(source_runtime.is_file())

        self.append_records(
            target_rollout,
            self.concrete_tool_pair(
                "call-after-same-inode-truncation",
                call_type="function_call",
                output_type="function_call_output",
            ),
        )
        finished = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertFalse(source_runtime.exists())

    def test_replacement_target_is_bound_to_exact_audited_source(self) -> None:
        first_source = "first-source"
        other_source = "other-source"
        target_task = "exact-source-target"
        self.render()
        for source_task in (first_source, other_source):
            preflight = self.run_ctl(
                "preflight", "--project", "demo", "--task-id", source_task
            )
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            audit = self.run_ctl(
                "audit", "--project", "demo", "--task-id", source_task
            )
            self.assertEqual(audit.returncode, 0, audit.stderr)

        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            first_source,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)
        target_runtime_path = (
            self.project / ".context" / "runtime" / f"{target_task}.json"
        )
        target_runtime = json.loads(
            target_runtime_path.read_text(encoding="utf-8")
        )
        self.assertEqual(target_runtime["source_task_id"], first_source)

        wrong_source_finish = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            other_source,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(wrong_source_finish.returncode, 1)
        self.assertIn("not bound to the source task", wrong_source_finish.stderr)
        self.assertTrue(
            self.project.joinpath(
                ".context", "runtime", f"{other_source}.json"
            ).is_file()
        )

        rebound = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            other_source,
        )
        self.assertEqual(rebound.returncode, 1)
        self.assertIn("different source task", rebound.stderr)

    def test_source_linked_completed_resume_preserves_lineage_and_needs_fresh_work(
        self,
    ) -> None:
        source_task = "completed-lineage-source"
        target_task = "completed-lineage-target"
        self.state["open"] = []
        self.state["next_actions"] = []
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.render()

        source_preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_preflight.returncode, 0, source_preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)

        target_rollout = self.write_rollout(
            target_task,
            [self.session_meta(target_task)],
        )
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            source_task,
            "--resume",
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)
        self.append_records(
            target_rollout,
            self.concrete_tool_pair("call-before-completed-reopen"),
        )

        retire_source = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            source_task,
            "--replaced-by",
            target_task,
        )
        self.assertEqual(retire_source.returncode, 0, retire_source.stderr)
        runtime_dir = self.project / ".context" / "runtime"
        source_runtime_path = runtime_dir / f"{source_task}.json"
        target_runtime_path = runtime_dir / f"{target_task}.json"
        self.assertFalse(source_runtime_path.exists())

        target_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(target_audit.returncode, 0, target_audit.stderr)
        target_finish = self.run_ctl(
            "finish", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(target_finish.returncode, 0, target_finish.stderr)
        target_receipt_path = runtime_dir.joinpath(
            "receipts", f"{target_task}.json"
        )
        completed_receipt = json.loads(
            target_receipt_path.read_text(encoding="utf-8")
        )
        self.assertEqual(completed_receipt["kind"], "completed")
        self.assertEqual(completed_receipt["source_task_id"], source_task)
        self.assertFalse(target_runtime_path.exists())

        missing_source = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--resume",
        )
        self.assertEqual(missing_source.returncode, 1)
        self.assertIn("requires --replaces-task", missing_source.stderr)
        self.assertTrue(target_receipt_path.is_file())
        self.assertFalse(target_runtime_path.exists())

        wrong_source = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            "wrong-source",
            "--resume",
        )
        self.assertEqual(wrong_source.returncode, 1)
        self.assertIn("different source task", wrong_source.stderr)
        self.assertTrue(target_receipt_path.is_file())
        self.assertFalse(target_runtime_path.exists())

        resumed = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            source_task,
            "--resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(source_runtime_path.exists())
        self.assertFalse(target_receipt_path.exists())
        recreated = json.loads(target_runtime_path.read_text(encoding="utf-8"))
        for field_name in (
            "started_state_sha256",
            "started_rules_fingerprint_sha256",
            "source_task_id",
            "source_audited_state_sha256",
            "source_audit_rules_fingerprint_sha256",
            "source_audit_fingerprint_sha256",
        ):
            self.assertEqual(recreated[field_name], completed_receipt[field_name])
        for field_name in (
            "audited_at",
            "audited_state_sha256",
            "audit_rules_fingerprint_sha256",
            "audit_fingerprint_sha256",
            "work_evidence",
        ):
            self.assertNotIn(field_name, recreated)
        reopened_baseline = target_rollout.stat().st_size
        self.assertEqual(recreated["started_rollout_offset"], reopened_baseline)
        self.assertEqual(
            recreated["started_tool_calls"],
            recreated["codex_telemetry"]["tool_calls"],
        )

        reopened_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(reopened_audit.returncode, 0, reopened_audit.stderr)
        without_fresh_work = self.run_ctl(
            "finish", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(without_fresh_work.returncode, 1)
        self.assertIn("no completed concrete tool call", without_fresh_work.stderr)
        self.assertTrue(target_runtime_path.is_file())

        fresh_call_id = "call-after-completed-reopen"
        self.append_records(
            target_rollout,
            self.concrete_tool_pair(fresh_call_id),
        )
        finished_again = self.run_ctl(
            "finish", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(finished_again.returncode, 0, finished_again.stderr)
        reopened_receipt = json.loads(
            target_receipt_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            reopened_receipt["work_evidence"]["baseline_offset"],
            reopened_baseline,
        )
        self.assertEqual(
            reopened_receipt["work_evidence"]["call_id_sha256"],
            hashlib.sha256(fresh_call_id.encode("utf-8")).hexdigest(),
        )

    def test_task_audit_migrates_legacy_plain_runtime_only(self) -> None:
        task_id = "legacy-plain-runtime"
        self.state["open"] = []
        self.state["next_actions"] = []
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime.pop("started_state_sha256")
        runtime.pop("started_rules_fingerprint_sha256")
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        migrated = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(
            migrated["started_state_sha256"], migrated["audited_state_sha256"]
        )
        self.assertEqual(
            migrated["started_rules_fingerprint_sha256"],
            migrated["audit_rules_fingerprint_sha256"],
        )
        self.assertIn("legacy_lineage_migrated_at_audit", migrated)

        finish = self.run_ctl(
            "finish", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        receipt = json.loads(
            self.project.joinpath(
                ".context", "runtime", "receipts", f"{task_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["started_rules_fingerprint_sha256"],
            migrated["started_rules_fingerprint_sha256"],
        )
        self.assertIsNone(receipt["source_task_id"])

    def test_existing_unbound_target_cannot_guess_replacement_lineage(self) -> None:
        source_task = "source-for-unbound-target"
        target_task = "legacy-unbound-target"
        self.render()
        source_preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_preflight.returncode, 0, source_preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", source_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        unbound_target = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", target_task
        )
        self.assertEqual(unbound_target.returncode, 0, unbound_target.stderr)

        bind_late = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            target_task,
            "--replaces-task",
            source_task,
        )
        self.assertEqual(bind_late.returncode, 1)
        self.assertIn("different source task", bind_late.stderr)

    def test_replacement_finish_rejects_wrong_target_lineage(self) -> None:
        old_task = "old-lineage"
        new_task = "new-lineage"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)
        old_path = self.project / ".context" / "runtime" / f"{old_task}.json"
        new_path = self.project / ".context" / "runtime" / f"{new_task}.json"
        target = json.loads(new_path.read_text(encoding="utf-8"))
        target["started_state_sha256"] = "0" * 64
        new_path.write_text(json.dumps(target), encoding="utf-8")

        result = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("replacement runtime state does not match source", result.stderr)
        self.assertTrue(old_path.is_file())
        self.assertTrue(new_path.is_file())

    def test_replacement_finish_requires_source_task_bound_audit(self) -> None:
        old_task = "old-without-source-audit"
        new_task = "new-without-source-audit"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)

        old_path = self.project / ".context" / "runtime" / f"{old_task}.json"
        new_path = self.project / ".context" / "runtime" / f"{new_task}.json"
        result = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("replacement source audited_state_sha256", result.stderr)
        self.assertTrue(old_path.is_file())
        self.assertFalse(new_path.exists())

    def test_replacement_finish_rejects_missing_source_lineage_sha(self) -> None:
        old_task = "old-missing-lineage"
        new_task = "new-for-missing-lineage"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)

        old_path = self.project / ".context" / "runtime" / f"{old_task}.json"
        new_path = self.project / ".context" / "runtime" / f"{new_task}.json"
        source = json.loads(old_path.read_text(encoding="utf-8"))
        source.pop("base_sha256")
        old_path.write_text(json.dumps(source), encoding="utf-8")
        result = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("source runtime base_sha256", result.stderr)
        self.assertTrue(old_path.is_file())
        self.assertTrue(new_path.is_file())

    def test_replacement_finish_rejects_invalid_target_lineage_sha(self) -> None:
        old_task = "old-for-invalid-lineage"
        new_task = "new-invalid-lineage"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)

        old_path = self.project / ".context" / "runtime" / f"{old_task}.json"
        new_path = self.project / ".context" / "runtime" / f"{new_task}.json"
        target = json.loads(new_path.read_text(encoding="utf-8"))
        target["started_state_sha256"] = "not-a-sha256"
        new_path.write_text(json.dumps(target), encoding="utf-8")
        result = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("replacement started_state_sha256", result.stderr)
        self.assertTrue(old_path.is_file())
        self.assertTrue(new_path.is_file())

    def test_replacement_finish_rejects_missing_invalid_or_wrong_rules_lineage(
        self,
    ) -> None:
        old_task = "old-for-rules-lineage"
        new_task = "new-for-rules-lineage"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)

        old_path = self.project / ".context" / "runtime" / f"{old_task}.json"
        new_path = self.project / ".context" / "runtime" / f"{new_task}.json"
        target = json.loads(new_path.read_text(encoding="utf-8"))
        target.pop("started_rules_fingerprint_sha256")
        new_path.write_text(json.dumps(target), encoding="utf-8")

        missing = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn(
            "replacement started_rules_fingerprint_sha256",
            missing.stderr,
        )

        target["started_rules_fingerprint_sha256"] = "not-a-sha256"
        new_path.write_text(json.dumps(target), encoding="utf-8")
        invalid = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn(
            "replacement started_rules_fingerprint_sha256",
            invalid.stderr,
        )

        target["started_rules_fingerprint_sha256"] = "0" * 64
        new_path.write_text(json.dumps(target), encoding="utf-8")
        wrong = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("started rules do not match", wrong.stderr)
        self.assertTrue(old_path.is_file())
        self.assertTrue(new_path.is_file())

    def test_source_can_retire_after_target_completes_in_first_turn(self) -> None:
        old_task = "old-before-fast-completion"
        new_task = "target-fast-completion"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        source_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", old_task
        )
        self.assertEqual(source_audit.returncode, 0, source_audit.stderr)
        target_rollout = self.write_rollout(
            new_task,
            [self.session_meta(new_task)],
        )
        target_preflight = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            new_task,
            "--replaces-task",
            old_task,
        )
        self.assertEqual(target_preflight.returncode, 0, target_preflight.stderr)

        candidate = dict(self.state)
        candidate["base_sha256"] = self.state_hash(self.state)
        candidate["open"] = []
        candidate["next_actions"] = []
        candidate_path = self.root / "fast-completed-candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        checkpoint = self.run_ctl(
            "checkpoint",
            "--project",
            "demo",
            "--input",
            str(candidate_path),
            "--task-id",
            new_task,
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        task_audit = self.run_ctl(
            "audit", "--project", "demo", "--task-id", new_task
        )
        self.assertEqual(task_audit.returncode, 0, task_audit.stderr)
        self.append_records(
            target_rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-fast-completion",
                        "name": "exec",
                        "status": "completed",
                        "input": "await tools.shell_command({command: 'git status'})",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-fast-completion",
                        "output": "clean",
                    },
                },
            ],
        )
        target_finish = self.run_ctl(
            "finish", "--project", "demo", "--task-id", new_task
        )
        self.assertEqual(target_finish.returncode, 0, target_finish.stderr)

        source_finish = self.run_ctl(
            "finish",
            "--project",
            "demo",
            "--task-id",
            old_task,
            "--replaced-by",
            new_task,
        )
        self.assertEqual(source_finish.returncode, 0, source_finish.stderr)
        self.assertFalse(
            self.project.joinpath(
                ".context", "runtime", f"{old_task}.json"
            ).exists()
        )

    def test_zero_count_pulse_refreshes_only_telemetry(self) -> None:
        task_id = "task-hook-probe"
        self.set_registry_limits(max_context_fill_percent=90)
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        before = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.append_records(rollout, [self.token_count(600, 1_000)])

        probe = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id, "--count", "0"
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        after = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(
            after["substantive_iterations"], before["substantive_iterations"]
        )
        self.assertEqual(after["codex_telemetry"]["latest_input_tokens"], 600)

    def test_runtime_identity_mismatch_fails_closed(self) -> None:
        task_id = "task-runtime-identity"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        runtime_path = self.project / ".context" / "runtime" / f"{task_id}.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["task_id"] = "different-task"
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        for command in (
            ("preflight", "--project", "demo", "--task-id", task_id),
            ("pulse", "--project", "demo", "--task-id", task_id, "--count", "0"),
            ("finish", "--project", "demo", "--task-id", task_id),
        ):
            with self.subTest(command=command[0]):
                result = self.run_ctl(*command)
                self.assertEqual(result.returncode, 1)
                self.assertIn("runtime session task mismatch", result.stderr)
                self.assertTrue(runtime_path.is_file())

    def test_codex_thread_id_env_fallback_and_cli_precedence(self) -> None:
        self.render()
        fallback = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            env_updates={"CODEX_THREAD_ID": "task-from-env"},
        )
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertTrue(
            self.project.joinpath(
                ".context", "runtime", "task-from-env.json"
            ).is_file()
        )

        explicit = self.run_ctl(
            "preflight",
            "--project",
            "demo",
            "--task-id",
            "task-from-cli",
            env_updates={"CODEX_THREAD_ID": "ignored-env-task"},
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertTrue(
            self.project.joinpath(
                ".context", "runtime", "task-from-cli.json"
            ).is_file()
        )
        self.assertFalse(
            self.project.joinpath(
                ".context", "runtime", "ignored-env-task.json"
            ).exists()
        )

    def test_pulse_detects_context_fill(self) -> None:
        task_id = "task-context-fill"
        self.set_registry_limits(max_context_fill_percent=75)
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id, 100), self.token_count(10, 100)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(rollout, [self.token_count(76, 100)])
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("reasons=context_fill", pulse.stdout)
        self.assertIn("context_fill=76.0%", pulse.stdout)

    def test_pulse_detects_total_tool_output_budget(self) -> None:
        task_id = "task-tool-output"
        self.set_registry_limits(
            max_tool_output_chars=10,
            max_single_tool_output_chars=100,
        )
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "abcdef",
                    },
                },
                {
                    "timestamp": "2026-07-31T00:00:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "output": "ghijkl",
                    },
                },
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("reasons=tool_output_chars", pulse.stdout)
        self.assertIn("tool_output_chars=12", pulse.stdout)

    def test_tool_search_output_counts_text_but_not_opaque_urls(self) -> None:
        task_id = "task-tool-search-output"
        self.set_registry_limits(
            max_tool_output_chars=19,
            max_single_tool_output_chars=100,
        )
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:08Z",
                    "type": "response_item",
                    "payload": {
                        "type": "tool_search_output",
                        "tools": [
                            {
                                "name": "abc",
                                "description": "abcdefghijklmnop",
                                "image_url": "x" * 10_000,
                            }
                        ],
                    },
                }
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("tool_output_chars=19", pulse.stdout)
        self.assertIn("max_tool_output_chars=19", pulse.stdout)

    def test_nested_compacted_data_is_output_not_a_compaction(self) -> None:
        task_id = "task-nested-compacted"
        self.set_registry_limits(
            max_tool_output_chars=15,
            max_single_tool_output_chars=100,
        )
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:00:08Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": [
                            {
                                "type": "compacted",
                                "data": {"message": "abcdef"},
                            }
                        ],
                    },
                }
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("reasons=tool_output_chars", pulse.stdout)
        self.assertIn("compactions=0", pulse.stdout)
        self.assertIn("tool_output_chars=15", pulse.stdout)

    def test_session_meta_without_token_count_reports_unknown_fill(self) -> None:
        task_id = "task-no-token-count"
        self.write_rollout(task_id, [self.session_meta(task_id, 1_000)])
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("context_fill=unknown", preflight.stdout)

    def test_replaced_rollout_preserves_lifetime_counter_floor(self) -> None:
        task_id = "task-replaced-rollout"
        rollout = self.write_rollout(
            task_id,
            [
                self.session_meta(task_id),
                self.token_count(100, 1_000),
                {
                    "timestamp": "2026-07-31T00:00:09Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "abcdef",
                    },
                },
            ],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("tool_output_chars=6", preflight.stdout)

        replacement = rollout.with_suffix(".replacement")
        replacement.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in [
                    self.session_meta(task_id),
                    self.token_count(100, 1_000),
                    {
                        "timestamp": "2026-07-31T00:00:10Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "xy",
                        },
                    },
                    {
                        "timestamp": "2026-07-31T00:00:11Z",
                        "type": "response_item",
                        "payload": {
                            "type": "agent_message",
                            "content": "padding" * 1_000,
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        replacement.replace(rollout)
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 0, pulse.stderr)
        self.assertIn("tool_output_chars=6", pulse.stdout)

    def test_incomplete_rollout_tail_waits_for_newline(self) -> None:
        task_id = "task-partial-tail"
        self.set_registry_limits(
            max_tool_output_chars=6,
            max_single_tool_output_chars=100,
        )
        rollout = self.write_rollout(
            task_id,
            [self.session_meta(task_id), self.token_count(100, 1_000)],
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        partial = json.dumps(
            {
                "timestamp": "2026-07-31T00:00:09Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "abcdef",
                },
            }
        )
        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(partial)
        before_newline = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(before_newline.returncode, 0, before_newline.stderr)
        self.assertIn("tool_output_chars=0", before_newline.stdout)
        with rollout.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        after_newline = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(after_newline.returncode, 2, after_newline.stderr)
        self.assertIn("tool_output_chars=6", after_newline.stdout)

    def test_malformed_relevant_rollout_record_fails_closed(self) -> None:
        task_id = "task-malformed-telemetry"
        rollout = self.rollout_path(task_id)
        rollout.write_text(
            json.dumps(self.session_meta(task_id))
            + "\n"
            + '{"timestamp":"2026-07-31T00:00:10Z","type":"event_msg",'
            + '"payload":{"type":"token_count",BROKEN}}\n',
            encoding="utf-8",
        )
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 1)
        self.assertIn("telemetry is unreliable", preflight.stderr)
        runtime_path = (
            self.project / ".context" / "runtime" / f"{task_id}.json"
        )
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(runtime["codex_telemetry"]["parse_errors"], 1)

    def test_missing_rollout_is_graceful(self) -> None:
        task_id = "task-no-rollout"
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("status=unavailable", preflight.stdout)
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 0, pulse.stderr)
        self.assertIn("CONTEXT_BUDGET_OK", pulse.stdout)

    def test_typical_initial_investigation_stays_in_same_task(self) -> None:
        task_id = "task-initial-investigation"
        records: list[dict[str, object]] = [
            self.session_meta(task_id, 258_400),
            self.token_count(25_000, 258_400),
        ]
        records.extend(
            {
                "timestamp": "2026-07-31T00:01:00Z",
                "type": "response_item",
                "payload": {"type": "function_call"},
            }
            for _ in range(36)
        )
        records.extend(
            {
                "timestamp": "2026-07-31T00:01:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "x" * 42_052,
                },
            }
            for _ in range(4)
        )
        self.write_rollout(task_id, records)
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertFalse(preflight.stdout.startswith("CONTEXT_ROLLOVER_REQUIRED"))
        self.assertIn("tool_calls=36", preflight.stdout)
        self.assertIn("tool_output_chars=168208", preflight.stdout)
        self.assertIn("max_tool_output_chars=42052", preflight.stdout)

    def test_compaction_equal_to_limit_requires_rollover(self) -> None:
        task_id = "task-third-compaction"
        self.set_registry_limits(max_session_compactions=2)
        records: list[dict[str, object]] = [
            self.session_meta(task_id),
            self.token_count(100, 1_000),
        ]
        for index in range(1):
            records.extend(
                [
                    {
                        "timestamp": f"2026-07-31T00:02:0{index}Z",
                        "type": "compacted",
                        "payload": {"replacement_history": []},
                    },
                    {
                        "timestamp": f"2026-07-31T00:02:1{index}Z",
                        "type": "event_msg",
                        "payload": {"type": "context_compacted"},
                    },
                ]
            )
        rollout = self.write_rollout(task_id, records)
        self.render()
        preflight = self.run_ctl(
            "preflight", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertIn("compactions=1", preflight.stdout)
        self.append_records(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T00:02:30Z",
                    "type": "compacted",
                    "payload": {"replacement_history": []},
                },
                {
                    "timestamp": "2026-07-31T00:02:31Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
            ],
        )
        pulse = self.run_ctl(
            "pulse", "--project", "demo", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 2, pulse.stderr)
        self.assertIn("CONTEXT_ROLLOVER_REQUIRED", pulse.stdout)
        self.assertIn("reasons=compacted", pulse.stdout)
        self.assertIn("compactions=2", pulse.stdout)

    def test_registry_rejects_duplicate_project_ids(self) -> None:
        registry_path = self.root / ".context" / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["projects"].append(dict(registry["projects"][0]))
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        result = self.run_ctl("audit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate registry project id", result.stderr)

    def test_root_project_path_supports_context_lifecycle(self) -> None:
        self.root.joinpath("AGENTS.md").write_text(
            "# Root\n\nUse $context-guardian and `ACTIVE_STATE.md`. On "
            "CONTEXT_ROLLOVER_REQUIRED, checkpoint and create a fresh thread.\n",
            encoding="utf-8",
        )
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        project = registry["projects"][0]
        project.update(
            {
                "id": "control",
                "name": "Control Plane",
                "path": ".",
            }
        )
        project["required_authorization"]["project_id"] = "control"
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")

        state = dict(self.state)
        state.update({"project_id": "control", "project": "Control Plane"})
        state["open"] = []
        state["next_actions"] = []
        state["authorization"] = dict(self.state["authorization"])
        state["authorization"]["project_id"] = "control"
        root_state = self.root / ".context" / "state.json"
        root_state.write_text(json.dumps(state), encoding="utf-8")

        render = self.run_ctl("render", "--project", "control")
        self.assertEqual(render.returncode, 0, render.stderr)
        self.assertTrue(self.root.joinpath("ACTIVE_STATE.md").is_file())

        no_session = self.run_ctl(
            "preflight", "--project", "control", "--no-session"
        )
        self.assertEqual(no_session.returncode, 0, no_session.stderr)

        task_id = "root-path-lifecycle"
        preflight = self.run_ctl(
            "preflight", "--project", "control", "--task-id", task_id
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        pulse = self.run_ctl(
            "pulse", "--project", "control", "--task-id", task_id
        )
        self.assertEqual(pulse.returncode, 0, pulse.stderr)
        task_audit = self.run_ctl(
            "audit", "--project", "control", "--task-id", task_id
        )
        self.assertEqual(task_audit.returncode, 0, task_audit.stderr)
        finish = self.run_ctl(
            "finish", "--project", "control", "--task-id", task_id
        )
        self.assertEqual(finish.returncode, 0, finish.stderr)
        self.assertFalse(
            self.root.joinpath(".context", "runtime", f"{task_id}.json").exists()
        )

        audit = self.run_ctl("audit", "--project", "control")
        self.assertEqual(audit.returncode, 0, audit.stderr)


if __name__ == "__main__":
    unittest.main()
