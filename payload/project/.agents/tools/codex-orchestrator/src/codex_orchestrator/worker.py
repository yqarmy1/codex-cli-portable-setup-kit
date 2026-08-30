"""Temporal Worker process for the isolated local candidate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.worker import Worker

from .activities import CodexActivities
from .local_runtime import (
    PAYLOAD_ENCRYPTION_ALGORITHM,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    ProcessRecord,
    RuntimePaths,
    remove_process_record_if_owned,
    validate_task_queue,
    validate_temporal_address,
    workflow_id_for_project,
    write_process_record,
)
from .payload_security import encrypted_data_converter_for_runtime
from .temporal_workflow import CodexSupervisorWorkflow


CONNECT_TIMEOUT_SECONDS = 10
GRACEFUL_SHUTDOWN_SECONDS = 30


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-orchestrator-worker",
        description="Run the pinned loopback-only Temporal Worker.",
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--temporal-address", default=TEMPORAL_ADDRESS)
    parser.add_argument("--task-queue", required=True)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit without connecting or writing a PID record",
    )
    return parser


def _validated_paths(args: argparse.Namespace) -> RuntimePaths:
    validate_temporal_address(args.temporal_address)
    paths = RuntimePaths.from_project_root(
        args.project_root, workspace_root=args.workspace_root
    )
    validate_task_queue(args.task_queue, paths.project_root)
    return paths


def _expected_module_arguments(args: argparse.Namespace, paths: RuntimePaths) -> tuple[str, ...]:
    arguments = [
        "-m",
        "codex_orchestrator.worker",
        "--workspace-root",
        str(paths.workspace_root),
        "--project-root",
        str(paths.project_root),
        "--temporal-address",
        args.temporal_address,
        "--task-queue",
        args.task_queue,
    ]
    if args.check_config:
        arguments.append("--check-config")
    return tuple(arguments)


def _assert_exact_invocation(arguments: tuple[str, ...]) -> None:
    original = tuple(sys.orig_argv[1:])
    if original != arguments:
        raise ValueError(
            "worker must be invoked with the fixed `python -m "
            "codex_orchestrator.worker ...` command line"
        )


async def _run_worker(
    args: argparse.Namespace,
    paths: RuntimePaths,
    data_converter: DataConverter,
) -> None:
    arguments = _expected_module_arguments(args, paths)
    _assert_exact_invocation(arguments)

    client = await asyncio.wait_for(
        Client.connect(
            args.temporal_address,
            namespace=TEMPORAL_NAMESPACE,
            tls=False,
            data_converter=data_converter,
        ),
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    activities = CodexActivities()
    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=[CodexSupervisorWorkflow],
        activities=[activities.run_codex_operation],
        max_concurrent_activities=1,
        max_concurrent_workflow_tasks=2,
        graceful_shutdown_timeout=timedelta(seconds=GRACEFUL_SHUTDOWN_SECONDS),
    )
    record = ProcessRecord.create(
        role="worker",
        pid=os.getpid(),
        executable=sys.executable,
        arguments=arguments,
        paths=paths,
    )
    write_process_record(paths.worker_pid_record, record, paths)
    try:
        await worker.run()
    finally:
        try:
            await asyncio.wait_for(
                activities.close(), timeout=GRACEFUL_SHUTDOWN_SECONDS
            )
        finally:
            remove_process_record_if_owned(
                paths.worker_pid_record,
                role="worker",
                pid=os.getpid(),
                paths=paths,
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = _validated_paths(args)
        arguments = _expected_module_arguments(args, paths)
        data_converter = encrypted_data_converter_for_runtime(paths)
        if args.check_config:
            # The check path deliberately performs no network or process I/O.
            print(
                json.dumps(
                    {
                        "ok": True,
                        "project_root": str(paths.project_root),
                        "workspace_root": str(paths.workspace_root),
                        "runtime_root": str(paths.runtime_root),
                        "temporal_address": args.temporal_address,
                        "task_queue": args.task_queue,
                        "workflow_id": workflow_id_for_project(paths.project_root),
                        "payload_encryption": {
                            "algorithm": PAYLOAD_ENCRYPTION_ALGORITHM,
                            "key_id": paths.payload_key_id,
                        },
                        "worker_arguments": list(arguments),
                    },
                    sort_keys=True,
                )
            )
            return 0
        asyncio.run(_run_worker(args, paths, data_converter))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
