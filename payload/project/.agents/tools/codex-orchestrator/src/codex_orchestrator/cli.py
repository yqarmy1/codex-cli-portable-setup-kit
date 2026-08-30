"""One-shot operator CLI for the durable supervisor.

Every invocation performs a bounded number of RPCs and exits.  There is no
polling loop and an RPC timeout is reported as an unknown outcome, never
silently retried by this layer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from temporalio.client import Client, WorkflowHandle
from temporalio.common import (
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.converter import DataConverter

from .contracts import (
    ControlCommand,
    StartGoalCommand,
    UserMessageCommand,
    WorkflowConfig,
)
from .domain import Budget, SupervisorState
from .local_runtime import (
    CommandLockBusy,
    PAYLOAD_ENCRYPTION_ALGORITHM,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    RuntimePaths,
    command_lock,
    load_payload_encryption_config,
    task_queue_for_project,
    temporal_server_arguments,
    validate_task_queue,
    validate_temporal_address,
    workflow_id_for_project,
)
from .payload_security import encrypted_data_converter_for_runtime
from .temporal_workflow import CodexSupervisorWorkflow


DEFAULT_RPC_TIMEOUT_SECONDS = 15
MAX_RPC_TIMEOUT_SECONDS = 60
MAX_TEXT_BYTES = 128 * 1024
T = TypeVar("T")


def _bounded_timeout(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if not 1 <= seconds <= MAX_RPC_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"timeout must be between 1 and {MAX_RPC_TIMEOUT_SECONDS} seconds"
        )
    return seconds


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _add_command_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--command-id",
        required=True,
        help="stable caller-generated ID; reuse it when reconciling an unknown result",
    )


def _add_text_source(parser: argparse.ArgumentParser, label: str) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{label}")
    group.add_argument(
        f"--{label}-file",
        type=Path,
        help="UTF-8 file; use '-' to read from stdin",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-orchestrator")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--temporal-address", default=TEMPORAL_ADDRESS)
    parser.add_argument("--task-queue")
    parser.add_argument(
        "--rpc-timeout-seconds",
        type=_bounded_timeout,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("runtime-info", help="print the pinned local plan; no RPC")
    subparsers.add_parser("status", help="query the existing supervisor once")

    start = subparsers.add_parser("start-goal")
    _add_command_id(start)
    _add_text_source(start, "objective")
    defaults = Budget()
    start.add_argument(
        "--max-automatic-turns", type=_positive, default=defaults.max_automatic_turns
    )
    start.add_argument("--max-tokens", type=_positive, default=defaults.max_tokens)
    start.add_argument(
        "--max-elapsed-seconds", type=_positive, default=defaults.max_elapsed_seconds
    )
    start.add_argument("--max-failures", type=_positive, default=defaults.max_failures)
    start.add_argument("--max-rollovers", type=_positive, default=defaults.max_rollovers)

    message = subparsers.add_parser("message")
    _add_command_id(message)
    message.add_argument("--message-id", required=True)
    _add_text_source(message, "text")

    for name in ("pause", "resume", "cancel", "clear"):
        control = subparsers.add_parser(name)
        _add_command_id(control)

    return parser


def _read_text(args: argparse.Namespace, name: str) -> str:
    inline = getattr(args, name, None)
    if inline is not None:
        value = inline
    else:
        source: Path = getattr(args, f"{name}_file")
        if str(source) == "-":
            value = sys.stdin.read(MAX_TEXT_BYTES + 1)
        else:
            if source.stat().st_size > MAX_TEXT_BYTES:
                raise ValueError(f"{name} file exceeds {MAX_TEXT_BYTES} bytes")
            value = source.read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds {MAX_TEXT_BYTES} bytes")
    return value


def _update_id(command: str, caller_id: str) -> str:
    if not caller_id.strip():
        raise ValueError("command-id must not be blank")
    digest = hashlib.sha256(caller_id.encode("utf-8")).hexdigest()
    return f"codex-{command}-{digest}"


def _rpc_delta(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)


async def _bounded(awaitable: Awaitable[T], seconds: int) -> T:
    # The SDK RPC deadline is set separately.  The outer deadline is a final
    # guard against transport/library behavior that does not honor it.
    return await asyncio.wait_for(awaitable, timeout=seconds + 1)


async def _connect(
    args: argparse.Namespace,
    data_converter: DataConverter,
) -> Client:
    return await _bounded(
        Client.connect(
            args.temporal_address,
            namespace=TEMPORAL_NAMESPACE,
            tls=False,
            data_converter=data_converter,
        ),
        args.rpc_timeout_seconds,
    )


async def _ensure_workflow(
    client: Client,
    *,
    args: argparse.Namespace,
    paths: RuntimePaths,
    workflow_id: str,
) -> WorkflowHandle[Any, Any]:
    config = WorkflowConfig(
        state=SupervisorState(
            workflow_key=workflow_id,
            project_root=str(paths.project_root),
        )
    )
    return await _bounded(
        client.start_workflow(
            CodexSupervisorWorkflow.run,
            config,
            id=workflow_id,
            task_queue=args.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            static_summary="Codex durable supervisor",
            rpc_timeout=_rpc_delta(args.rpc_timeout_seconds),
        ),
        args.rpc_timeout_seconds,
    )


async def _query_state(
    handle: WorkflowHandle[Any, Any], args: argparse.Namespace
) -> dict[str, Any]:
    result = await _bounded(
        handle.query(
            CodexSupervisorWorkflow.state,
            rpc_timeout=_rpc_delta(args.rpc_timeout_seconds),
        ),
        args.rpc_timeout_seconds,
    )
    if not isinstance(result, dict):
        raise TypeError("supervisor state query returned a non-object")
    return result


async def _execute_update(
    handle: WorkflowHandle[Any, Any],
    args: argparse.Namespace,
    method: Callable[..., Any],
    payload: Any,
) -> dict[str, Any]:
    result = await _bounded(
        handle.execute_update(
            method,
            payload,
            id=_update_id(args.command, args.command_id),
            rpc_timeout=_rpc_delta(args.rpc_timeout_seconds),
        ),
        args.rpc_timeout_seconds,
    )
    if not isinstance(result, dict):
        raise TypeError("supervisor update returned a non-object")
    return result


async def _run(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    workflow_id = workflow_id_for_project(paths.project_root)
    encryption_config = load_payload_encryption_config(paths)
    data_converter = encrypted_data_converter_for_runtime(paths)
    if args.command == "runtime-info":
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "task_queue": args.task_queue,
            "temporal_address": args.temporal_address,
            "runtime_root": str(paths.runtime_root),
            "workspace_root": str(paths.workspace_root),
            "temporal_db": str(paths.temporal_db),
            "payload_encryption": {
                "algorithm": PAYLOAD_ENCRYPTION_ALGORITHM,
                "key_id": encryption_config.key_id,
            },
            "server_arguments": list(temporal_server_arguments(paths)),
        }

    client = await _connect(args, data_converter)
    if args.command == "status":
        handle = client.get_workflow_handle(workflow_id)
        return {"ok": True, "state": await _query_state(handle, args)}

    # The Workflow currently requires a monotonic command_seq.  Keep workflow
    # creation, sequence query, and Update inside one project-scoped OS lock so
    # two local callers cannot allocate the same sequence number.  The wait is
    # bounded; failure is visible and the caller must reuse its command ID.
    with command_lock(
        paths, timeout_seconds=min(10, args.rpc_timeout_seconds)
    ):
        handle = await _ensure_workflow(
            client,
            args=args,
            paths=paths,
            workflow_id=workflow_id,
        )
        state = await _query_state(handle, args)
        command_seq = state.get("last_command_seq")
        if type(command_seq) is not int or command_seq < 0:
            raise TypeError("supervisor returned an invalid last_command_seq")
        command_seq += 1

        if args.command == "start-goal":
            budget = Budget(
                max_automatic_turns=args.max_automatic_turns,
                max_tokens=args.max_tokens,
                max_elapsed_seconds=args.max_elapsed_seconds,
                max_failures=args.max_failures,
                max_rollovers=args.max_rollovers,
            )
            payload = StartGoalCommand(
                command_seq=command_seq,
                objective=_read_text(args, "objective"),
                budget=budget,
            )
            result = await _execute_update(
                handle, args, CodexSupervisorWorkflow.start_goal, payload
            )
        elif args.command == "message":
            payload = UserMessageCommand(
                command_seq=command_seq,
                message_id=args.message_id,
                text=_read_text(args, "text"),
            )
            result = await _execute_update(
                handle, args, CodexSupervisorWorkflow.user_message, payload
            )
        else:
            method = {
                "pause": CodexSupervisorWorkflow.pause_goal,
                "resume": CodexSupervisorWorkflow.resume_goal,
                "cancel": CodexSupervisorWorkflow.clear_goal,
                "clear": CodexSupervisorWorkflow.clear_goal,
            }[args.command]
            result = await _execute_update(
                handle, args, method, ControlCommand(command_seq=command_seq)
            )
    return {"ok": True, "state": result}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_temporal_address(args.temporal_address)
        paths = RuntimePaths.from_project_root(
            args.project_root, workspace_root=args.workspace_root
        )
        if args.task_queue is None:
            args.task_queue = task_queue_for_project(paths.project_root)
        validate_task_queue(args.task_queue, paths.project_root)
        result = asyncio.run(_run(args, paths))
        # ASCII-escaped JSON survives Windows PowerShell's native-pipe code
        # page; ConvertFrom-Json restores the original Unicode paths/text.
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, default=str))
        return 0
    except CommandLockBusy:
        print(
            json.dumps(
                {
                    "ok": False,
                    "accepted": False,
                    "error": "command_lock_busy",
                    "retry": "retry with the same --command-id",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 5
    except (asyncio.TimeoutError, TimeoutError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "rpc_timeout",
                    "outcome_unknown": args.command not in {"runtime-info", "status"},
                    "retry": "reuse the same --command-id after reconciling state",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 4
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
