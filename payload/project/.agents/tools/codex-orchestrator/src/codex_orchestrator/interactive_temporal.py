"""Temporal implementation of the thin interactive backend protocol.

The host injects the already-connected Temporal ``Client`` used by the local
process.  This module starts no server, Worker, app-server, or model turn on its
own.  It reuses the one-shot CLI's bounded typed helpers so command IDs, RPC
deadlines, Workflow creation policy, and result validation stay identical.

There is intentionally no retry loop here.  A timeout or transport failure is
returned to :class:`~codex_orchestrator.interactive_frontend.InteractiveSession`,
which suspends output until the operator explicitly runs ``/status``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from temporalio.client import Client, WorkflowUpdateFailedError

from .cli import (
    DEFAULT_RPC_TIMEOUT_SECONDS,
    MAX_RPC_TIMEOUT_SECONDS,
    _ensure_workflow,
    _execute_update,
    _query_state,
    _update_id,
)
from .command_outbox import (
    EncryptedCommandOutbox,
    OUTBOX_SCHEMA_VERSION,
    PreparedCommandRecord,
)
from .contracts import ControlCommand, StartGoalCommand, UserMessageCommand
from .domain import Budget
from .interactive_frontend import (
    CommandKind,
    PendingCommand,
    UnknownCommandOutcome,
)
from .local_runtime import (
    RuntimePaths,
    command_lock,
    task_queue_for_project,
    validate_task_queue,
    workflow_id_for_project,
)
from .temporal_workflow import CodexSupervisorWorkflow


# ``command_lock`` uses a bounded synchronous non-blocking probe.  Keeping this
# tiny prevents another CLI process from stalling the same-process Worker event
# loop and its Activity heartbeat.  Busy is a known-not-accepted result; callers
# may explicitly retry with the same command ID.
DEFAULT_LOCK_PROBE_SECONDS = 0.01


PayloadFactory = Callable[[int], Any]


@dataclass(repr=False, slots=True)
class _PreparedUpdate:
    """Exact wire payload retained in memory only while outcome is unknown."""

    command_name: str
    method: Callable[..., Any]
    payload: Any
    sequence: int
    record: PreparedCommandRecord


class TemporalInteractiveBackend:
    """Map interactive actions one-for-one onto typed Workflow Updates."""

    def __init__(
        self,
        client: Client,
        *,
        paths: RuntimePaths,
        outbox: EncryptedCommandOutbox,
        task_queue: str | None = None,
        rpc_timeout_seconds: int = DEFAULT_RPC_TIMEOUT_SECONDS,
        lock_probe_seconds: float = DEFAULT_LOCK_PROBE_SECONDS,
        goal_budget: Budget | None = None,
    ) -> None:
        paths.validate()
        selected_queue = task_queue or task_queue_for_project(paths.project_root)
        validate_task_queue(selected_queue, paths.project_root)
        if type(rpc_timeout_seconds) is not int or not (
            1 <= rpc_timeout_seconds <= MAX_RPC_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"rpc_timeout_seconds must be between 1 and {MAX_RPC_TIMEOUT_SECONDS}"
            )
        if not 0 < lock_probe_seconds <= 0.1:
            raise ValueError("lock_probe_seconds must be between 0 and 0.1 seconds")
        selected_budget = goal_budget or Budget()
        selected_budget.validate()

        self._client = client
        self._paths = paths
        self._task_queue = selected_queue
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._lock_probe_seconds = lock_probe_seconds
        self._goal_budget = selected_budget
        self._workflow_id = workflow_id_for_project(paths.project_root)
        self._outbox = outbox
        # Serialize this ingress before taking the interprocess command lock.
        self._local_command_gate = asyncio.Lock()
        self._pending_updates: dict[str, _PreparedUpdate] = {}
        self._recovered_pending_command: PendingCommand | None = None
        recovered = outbox.load_pending()
        if recovered is not None:
            prepared = self._prepared_from_record(recovered)
            self._pending_updates[recovered.command_id] = prepared
            self._recovered_pending_command = self._frontend_pending(recovered)

    @property
    def recovered_pending_command(self) -> PendingCommand | None:
        """Return startup recovery state; never dispatch it automatically."""

        return self._recovered_pending_command

    async def start_goal(
        self,
        *,
        command_id: str,
        objective: str,
    ) -> Mapping[str, Any]:
        return await self._update(
            command_name="start-goal",
            command_id=command_id,
            method=CodexSupervisorWorkflow.start_goal,
            payload_factory=lambda sequence: StartGoalCommand(
                command_seq=sequence,
                objective=objective,
                budget=self._goal_budget,
            ),
        )

    async def user_message(
        self,
        *,
        command_id: str,
        message_id: str,
        text: str,
    ) -> Mapping[str, Any]:
        return await self._update(
            command_name="message",
            command_id=command_id,
            method=CodexSupervisorWorkflow.user_message,
            payload_factory=lambda sequence: UserMessageCommand(
                command_seq=sequence,
                message_id=message_id,
                text=text,
            ),
        )

    async def pause(self, *, command_id: str) -> Mapping[str, Any]:
        return await self._control(
            command_name="pause",
            command_id=command_id,
            method=CodexSupervisorWorkflow.pause_goal,
        )

    async def resume(self, *, command_id: str) -> Mapping[str, Any]:
        return await self._control(
            command_name="resume",
            command_id=command_id,
            method=CodexSupervisorWorkflow.resume_goal,
        )

    async def clear(self, *, command_id: str) -> Mapping[str, Any]:
        # `/cancel` is canonical at the interactive boundary; `/clear` is an
        # alias.  Both share this one stable Temporal Update namespace.
        return await self._control(
            command_name="cancel",
            command_id=command_id,
            method=CodexSupervisorWorkflow.clear_goal,
        )

    async def status(self) -> Mapping[str, Any]:
        args = self._args(command_name="status", command_id=None)
        async with self._local_command_gate:
            handle = self._client.get_workflow_handle(self._workflow_id)
            return await _query_state(handle, args)

    async def _control(
        self,
        *,
        command_name: str,
        command_id: str,
        method: Callable[..., Any],
    ) -> Mapping[str, Any]:
        return await self._update(
            command_name=command_name,
            command_id=command_id,
            method=method,
            payload_factory=lambda sequence: ControlCommand(command_seq=sequence),
        )

    async def _update(
        self,
        *,
        command_name: str,
        command_id: str,
        method: Callable[..., Any],
        payload_factory: PayloadFactory,
    ) -> Mapping[str, Any]:
        if not command_id.strip():
            raise ValueError("command_id must not be blank")
        args = self._args(command_name=command_name, command_id=command_id)
        async with self._local_command_gate:
            # Probe only once.  No sleep/retry loop is added to the event loop.
            with command_lock(
                self._paths,
                timeout_seconds=self._lock_probe_seconds,
            ):
                prepared = self._pending_updates.get(command_id)
                if prepared is not None:
                    candidate = payload_factory(prepared.sequence)
                    if (
                        prepared.command_name != command_name
                        or prepared.method is not method
                        or prepared.payload != candidate
                    ):
                        raise ValueError(
                            "command_id retry does not match its original typed payload"
                        )
                    handle = self._client.get_workflow_handle(self._workflow_id)
                    return await self._send_prepared(
                        handle,
                        args=args,
                        command_id=command_id,
                        prepared=prepared,
                    )

                handle = await _ensure_workflow(
                    self._client,
                    args=args,
                    paths=self._paths,
                    workflow_id=self._workflow_id,
                )
                state = await _query_state(handle, args)
                sequence = state.get("last_command_seq")
                if type(sequence) is not int or sequence < 0:
                    raise TypeError("supervisor returned an invalid last_command_seq")
                payload = payload_factory(sequence + 1)
                record = self._record_for(
                    command_name=command_name,
                    command_id=command_id,
                    payload=payload,
                )
                # Crash boundary: no execute_update may occur until the exact
                # wire payload and command sequence are encrypted and durable.
                self._outbox.prepare(record)
                prepared = _PreparedUpdate(
                    command_name=command_name,
                    method=method,
                    payload=payload,
                    sequence=sequence + 1,
                    record=record,
                )
                self._pending_updates[command_id] = prepared
                self._recovered_pending_command = self._frontend_pending(record)
                return await self._send_prepared(
                    handle,
                    args=args,
                    command_id=command_id,
                    prepared=prepared,
                )

    async def _send_prepared(
        self,
        handle: Any,
        *,
        args: SimpleNamespace,
        command_id: str,
        prepared: _PreparedUpdate,
    ) -> Mapping[str, Any]:
        # Exactly one attempt per explicit frontend action. Unknown outcomes
        # retain this object, including the original command_seq and text.
        try:
            result = await _execute_update(
                handle,
                args,
                prepared.method,
                prepared.payload,
            )
        except WorkflowUpdateFailedError as update_failure:
            # The Workflow durably rejected/failed this Update. Its outcome is
            # known, but the frontend fence may clear only after the encrypted
            # outbox carries a durable resolved tombstone.
            try:
                self._outbox.resolve(command_id)
            except Exception as exc:
                raise UnknownCommandOutcome() from exc
            self._pending_updates.pop(command_id, None)
            self._recovered_pending_command = None
            raise update_failure
        except asyncio.CancelledError:
            # The frontend already retains the semantic command before
            # propagating cancellation; keep the exact wire payload here.
            raise
        except Exception as exc:
            # Once execute_update was sent, transport/protocol failure cannot
            # prove whether the handler committed. Preserve the ID and require
            # an explicit same-ID /retry.
            raise UnknownCommandOutcome() from exc
        self._validate_update_result(result, prepared.record)
        try:
            self._outbox.resolve(command_id)
        except Exception as exc:
            # The server result is known but crash recovery would still replay
            # the unresolved file. Keep both frontend and backend fenced.
            raise UnknownCommandOutcome() from exc
        self._pending_updates.pop(command_id, None)
        self._recovered_pending_command = None
        return result

    def _prepared_from_record(self, record: PreparedCommandRecord) -> _PreparedUpdate:
        expected_update_id = _update_id(record.kind, record.command_id)
        if record.update_id != expected_update_id:
            raise ValueError("recovered command has an invalid stable Update ID")
        method = self._method_for(record.kind)
        return _PreparedUpdate(
            command_name=record.kind,
            method=method,
            payload=record.typed_payload(),
            sequence=record.command_seq,
            record=record,
        )

    def _record_for(
        self,
        *,
        command_name: str,
        command_id: str,
        payload: Any,
    ) -> PreparedCommandRecord:
        message_id: str | None = None
        text: str | None = None
        budget: Budget | None = None
        if isinstance(payload, StartGoalCommand):
            text = payload.objective
            budget = payload.budget
        elif isinstance(payload, UserMessageCommand):
            message_id = payload.message_id
            text = payload.text
        elif not isinstance(payload, ControlCommand):
            raise TypeError("unsupported interactive typed payload")
        record = PreparedCommandRecord(
            schema_version=OUTBOX_SCHEMA_VERSION,
            project_key=self._paths.project_key,
            workflow_id=self._workflow_id,
            kind=command_name,
            update_id=_update_id(command_name, command_id),
            command_id=command_id,
            command_seq=payload.command_seq,
            message_id=message_id,
            text=text,
            budget=budget,
        )
        record.validate()
        return record

    @staticmethod
    def _method_for(command_name: str) -> Callable[..., Any]:
        try:
            return {
                "start-goal": CodexSupervisorWorkflow.start_goal,
                "message": CodexSupervisorWorkflow.user_message,
                "pause": CodexSupervisorWorkflow.pause_goal,
                "resume": CodexSupervisorWorkflow.resume_goal,
                "cancel": CodexSupervisorWorkflow.clear_goal,
            }[command_name]
        except KeyError as exc:
            raise ValueError("recovered command has an invalid kind") from exc

    @staticmethod
    def _frontend_pending(record: PreparedCommandRecord) -> PendingCommand:
        kind = {
            "start-goal": CommandKind.START_GOAL,
            "message": CommandKind.USER_MESSAGE,
            "pause": CommandKind.PAUSE,
            "resume": CommandKind.RESUME,
            "cancel": CommandKind.CANCEL,
        }[record.kind]
        return PendingCommand(
            kind=kind,
            command_id=record.command_id,
            message_id=record.message_id,
            text=record.text,
        )

    @staticmethod
    def _validate_update_result(
        result: Mapping[str, Any],
        record: PreparedCommandRecord,
    ) -> None:
        epoch = result.get("intent_epoch")
        sequence = result.get("last_command_seq")
        if type(epoch) is not int or epoch < 0:
            raise UnknownCommandOutcome()
        if type(sequence) is not int or sequence != record.command_seq:
            raise UnknownCommandOutcome()

    def _args(
        self,
        *,
        command_name: str,
        command_id: str | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            command=command_name,
            command_id=command_id,
            task_queue=self._task_queue,
            rpc_timeout_seconds=self._rpc_timeout_seconds,
        )
