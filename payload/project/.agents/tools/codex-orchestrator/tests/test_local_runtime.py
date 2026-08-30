from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_orchestrator import cli
from codex_orchestrator.local_runtime import (
    OPENAI_CODEX_SDK_VERSION,
    TEMPORAL_ADDRESS,
    TEMPORAL_CLI_DOWNLOAD_URL,
    TEMPORAL_CLI_VERSION,
    TEMPORAL_CLI_WINDOWS_AMD64_SHA256,
    TEMPORAL_PYTHON_SDK_VERSION,
    CommandLockBusy,
    ProcessRecord,
    RuntimePaths,
    command_lock,
    load_process_record,
    remove_process_record_if_owned,
    task_queue_for_project,
    temporal_server_arguments,
    validate_temporal_address,
    workflow_id_for_project,
    write_process_record,
)


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.project_a = self.workspace / "project-a"
        self.project_b = self.workspace / "project-b"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.paths_a = RuntimePaths.from_project_root(
            self.project_a, workspace_root=self.workspace
        )
        self.paths_b = RuntimePaths.from_project_root(
            self.project_b, workspace_root=self.workspace
        )

    def test_reviewed_versions_are_fixed(self) -> None:
        self.assertEqual(TEMPORAL_CLI_VERSION, "1.8.2")
        self.assertEqual(TEMPORAL_PYTHON_SDK_VERSION, "1.30.0")
        self.assertEqual(OPENAI_CODEX_SDK_VERSION, "0.144.4")
        self.assertIn("/v1.8.2/", TEMPORAL_CLI_DOWNLOAD_URL)
        self.assertNotIn("latest", TEMPORAL_CLI_DOWNLOAD_URL)
        self.assertEqual(
            TEMPORAL_CLI_WINDOWS_AMD64_SHA256,
            "72e02498fa7849657c369377f7de69a8709b3d2183b6f2749f6c8bd54a984501",
        )

    def test_one_workspace_server_and_project_isolation(self) -> None:
        self.assertEqual(self.paths_a.runtime_root, self.paths_b.runtime_root)
        self.assertEqual(self.paths_a.temporal_db, self.paths_b.temporal_db)
        self.assertEqual(
            self.paths_a.temporal_pid_record, self.paths_b.temporal_pid_record
        )
        self.assertNotEqual(self.paths_a.worker_pid_record, self.paths_b.worker_pid_record)
        self.assertNotEqual(self.paths_a.command_lock, self.paths_b.command_lock)
        self.assertNotEqual(
            task_queue_for_project(self.project_a),
            task_queue_for_project(self.project_b),
        )

    def test_project_must_be_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):
                RuntimePaths.from_project_root(
                    other, workspace_root=self.workspace
                )

    def test_only_exact_loopback_endpoint_is_accepted(self) -> None:
        self.assertEqual(validate_temporal_address(TEMPORAL_ADDRESS), TEMPORAL_ADDRESS)
        for rejected in (
            "localhost:7233",
            "0.0.0.0:7233",
            "127.0.0.1:7234",
            "[::1]:7233",
            "10.0.0.2:7233",
        ):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                validate_temporal_address(rejected)

    def test_server_plan_is_loopback_and_persistent(self) -> None:
        arguments = temporal_server_arguments(self.paths_a)
        self.assertEqual(arguments[:2], ("server", "start-dev"))
        self.assertEqual(arguments[arguments.index("--ip") + 1], "127.0.0.1")
        self.assertEqual(arguments[arguments.index("--ui-ip") + 1], "127.0.0.1")
        self.assertEqual(
            arguments[arguments.index("--db-filename") + 1],
            str(self.paths_a.temporal_db),
        )

    def test_workflow_id_is_stable_without_revealing_path(self) -> None:
        first = workflow_id_for_project(self.project_a)
        second = workflow_id_for_project(self.project_a)
        self.assertEqual(first, second)
        self.assertNotIn("project-a", first)
        self.assertNotIn(str(self.workspace), first)

    def test_pid_records_are_exact_and_owned(self) -> None:
        self.paths_a.create_directories()
        server = ProcessRecord.create(
            role="temporal-server",
            pid=111,
            executable=self.paths_a.temporal_exe,
            arguments=temporal_server_arguments(self.paths_a),
            paths=self.paths_a,
        )
        write_process_record(
            self.paths_a.temporal_pid_record, server, self.paths_a
        )
        loaded = load_process_record(self.paths_a.temporal_pid_record, self.paths_a)
        self.assertEqual(loaded.pid, 111)
        self.assertFalse(
            remove_process_record_if_owned(
                self.paths_a.temporal_pid_record,
                role="temporal-server",
                pid=222,
                paths=self.paths_a,
            )
        )
        self.assertTrue(
            remove_process_record_if_owned(
                self.paths_a.temporal_pid_record,
                role="temporal-server",
                pid=111,
                paths=self.paths_a,
            )
        )

    def test_worker_record_rejects_cross_project_queue(self) -> None:
        arguments = (
            "-m",
            "codex_orchestrator.worker",
            "--workspace-root",
            str(self.workspace),
            "--project-root",
            str(self.project_a),
            "--temporal-address",
            TEMPORAL_ADDRESS,
            "--task-queue",
            task_queue_for_project(self.project_b),
        )
        with self.assertRaises(ValueError):
            ProcessRecord.create(
                role="worker",
                pid=123,
                executable=self.paths_a.venv_python,
                arguments=arguments,
                paths=self.paths_a,
            )

    def test_command_lock_is_interprocess_and_bounded(self) -> None:
        self.paths_a.create_directories()
        program = textwrap.dedent(
            """
            import sys, time
            from codex_orchestrator.local_runtime import RuntimePaths, command_lock
            paths = RuntimePaths.from_project_root(sys.argv[2], workspace_root=sys.argv[1])
            with command_lock(paths, timeout_seconds=1):
                print('locked', flush=True)
                time.sleep(1)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(self.workspace), str(self.project_a)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(CommandLockBusy):
                with command_lock(self.paths_a, timeout_seconds=0.1):
                    self.fail("second process unexpectedly acquired the command lock")
            child.wait(timeout=3)
            self.assertEqual(child.returncode, 0, child.stderr.read())
            with command_lock(self.paths_a, timeout_seconds=0.1):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()

    def test_lock_busy_is_visible_and_not_accepted(self) -> None:
        async def raise_busy(*_args, **_kwargs):
            raise CommandLockBusy("busy")

        stderr = io.StringIO()
        with patch.object(cli, "_run", raise_busy), contextlib.redirect_stderr(stderr):
            exit_code = cli.main(
                [
                    "--workspace-root",
                    str(self.workspace),
                    "--project-root",
                    str(self.project_a),
                    "message",
                    "--command-id",
                    "command-1",
                    "--message-id",
                    "message-1",
                    "--text",
                    "preempt now",
                ]
            )
        self.assertEqual(exit_code, 5)
        payload = json.loads(stderr.getvalue())
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["error"], "command_lock_busy")

    def test_cli_native_json_ascii_escapes_unicode_paths(self) -> None:
        unicode_root = self.workspace / "\u4e2d\u6587\u5de5\u4f5c\u5340"
        unicode_root.mkdir()

        async def return_path(_args, paths):
            return {"ok": True, "runtime_root": str(paths.runtime_root)}

        stdout = io.StringIO()
        with patch.object(cli, "_run", return_path), contextlib.redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "--workspace-root",
                    str(unicode_root),
                    "--project-root",
                    str(unicode_root),
                    "runtime-info",
                ]
            )
        self.assertEqual(exit_code, 0)
        raw = stdout.getvalue()
        self.assertIn("\\u", raw)
        self.assertNotIn("\u4e2d\u6587\u5de5\u4f5c\u5340", raw)
        self.assertEqual(
            json.loads(raw)["runtime_root"],
            str(unicode_root / ".workspace" / "tools" / "codex-orchestrator"),
        )


class StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_bootstrap_does_not_start_services(self) -> None:
        script = (self.root / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("1.8.2", script)
        self.assertIn(TEMPORAL_CLI_WINDOWS_AMD64_SHA256, script)
        self.assertNotIn("/latest", script)
        self.assertNotIn("Start-Process", script)
        self.assertNotIn(" -c @'", script)

    def test_runtime_probe_executes_without_native_inline_code(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "codex_orchestrator.runtime_probe"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["temporalio"], "1.30.0")
        self.assertEqual(payload["openai-codex"], "0.144.4")

    def test_start_and_verify_are_bounded_and_loopback_only(self) -> None:
        start = (self.root / "scripts" / "start-local.ps1").read_text(
            encoding="utf-8"
        )
        verify = (self.root / "scripts" / "verify.ps1").read_text(
            encoding="utf-8"
        )
        combined = start + verify
        self.assertNotIn("while ($true)", combined.lower())
        self.assertNotIn("Stop-Process -Name", combined)
        stop = (self.root / "scripts" / "stop-local.ps1").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(start + stop, r"(?i)\$pid\b")
        self.assertIn("-WindowStyle Hidden", start)
        self.assertIn("--db-filename", start)
        self.assertNotIn("--ip', '0.0.0.0", start)
        self.assertIn("launcher_replacement_ready = $false", verify)
        self.assertNotIn(" -c @'", verify)
        self.assertIn("payload_encryption_not_configured", verify)
        self.assertIn("user_output_sink_not_implemented", verify)

    def test_python_and_project_dependencies_are_pinned(self) -> None:
        pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        lock = (self.root / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn('"temporalio==1.30.0"', pyproject)
        self.assertIn('"openai-codex==0.144.4"', pyproject)
        self.assertIn("temporalio==1.30.0", lock)
        self.assertIn("openai-codex==0.144.4", lock)

    def test_cancel_is_a_first_class_cli_command(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "--workspace-root",
                "C:\\workspace",
                "--project-root",
                "C:\\workspace\\project",
                "cancel",
                "--command-id",
                "cancel-1",
            ]
        )
        self.assertEqual(args.command, "cancel")


if __name__ == "__main__":
    unittest.main()
