"""Temporal durable supervisor.

The Workflow contains scheduling decisions only.  All app-server, process,
filesystem, and network I/O remains in Activities.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from .contracts import (
        CodexOperationInput,
        ControlCommand,
        StartGoalCommand,
        UserMessageCommand,
        WorkflowConfig,
    )
    from .domain import (
        Operation,
        OperationResult,
        ResultDisposition,
        SupervisorState,
        SupervisorStatus,
    )


ACTIVITY_NAME = "run_codex_operation"


@workflow.defn
class CodexSupervisorWorkflow:
    """Single-writer durable controller for one project objective stream."""

    @workflow.init
    def __init__(self, config: WorkflowConfig) -> None:
        # Temporal may deliver Updates before the run method starts. Initialize
        # all command-authority state in the Workflow initializer so an
        # immediate ordinary user message can fence work instead of failing an
        # uninitialized handler.
        config.validate()
        self._config: WorkflowConfig | None = config
        self._state: SupervisorState | None = config.state
        self._activity_task: asyncio.Task[OperationResult] | None = None

    @workflow.run
    async def run(self, config: WorkflowConfig) -> None:
        config.validate()

        while True:
            await self._continue_as_new_if_needed()
            operation = self._reserve_next_operation()
            if operation is None:
                await workflow.wait_condition(self._work_available)
                continue

            activity_input = self._activity_input(operation)
            runtime_config = self._config_required()
            task = asyncio.create_task(
                workflow.execute_activity(
                    ACTIVITY_NAME,
                    activity_input,
                    result_type=OperationResult,
                    schedule_to_close_timeout=timedelta(
                        seconds=runtime_config.activity_schedule_to_close_seconds
                    ),
                    start_to_close_timeout=timedelta(
                        seconds=runtime_config.activity_start_to_close_seconds
                    ),
                    heartbeat_timeout=timedelta(
                        seconds=runtime_config.activity_heartbeat_seconds
                    ),
                    # Non-idempotent app-server calls must never be replayed by
                    # Temporal behind our back. Unknown outcomes reconcile.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    activity_id=operation.operation_id,
                    summary=f"Codex {operation.kind.value}",
                )
            )
            self._activity_task = task
            try:
                outcome = await task
            except asyncio.CancelledError:
                # A typed user/control Update already advanced intent_epoch and
                # fenced the old result. The next loop performs reconciliation
                # before dispatching replacement work.
                continue
            except ActivityError as exc:
                if self._operation_still_current(operation):
                    self._state_required().record_result(
                        OperationResult(
                            operation_id=operation.operation_id,
                            intent_epoch=operation.intent_epoch,
                            disposition=ResultDisposition.AMBIGUOUS,
                            tokens=None,
                            thread_id=operation.thread_id,
                            detail=f"activity outcome unknown: {type(exc).__name__}",
                        ),
                        now_seconds=self._now_seconds(),
                    )
                continue
            finally:
                if self._activity_task is task:
                    self._activity_task = None

            self._state_required().record_result(
                outcome,
                now_seconds=self._now_seconds(),
            )

    @workflow.update
    async def start_goal(self, command: StartGoalCommand) -> dict[str, Any]:
        state = self._state_required()
        state.start_goal(
            command_seq=command.command_seq,
            objective=command.objective,
            budget=command.budget,
            now_seconds=self._now_seconds(),
        )
        self._cancel_inflight_activity()
        return state.public_snapshot()

    @workflow.update
    async def user_message(self, command: UserMessageCommand) -> dict[str, Any]:
        state = self._state_required()
        state.preempt_with_user_message(
            command_seq=command.command_seq,
            message_id=command.message_id,
            text=command.text,
        )
        self._cancel_inflight_activity()
        return state.public_snapshot()

    @workflow.update
    async def pause_goal(self, command: ControlCommand) -> dict[str, Any]:
        state = self._state_required()
        state.pause_goal(command_seq=command.command_seq)
        self._cancel_inflight_activity()
        return state.public_snapshot()

    @workflow.update
    async def resume_goal(self, command: ControlCommand) -> dict[str, Any]:
        state = self._state_required()
        state.resume_goal(
            command_seq=command.command_seq,
            now_seconds=self._now_seconds(),
        )
        self._cancel_inflight_activity()
        return state.public_snapshot()

    @workflow.update
    async def clear_goal(self, command: ControlCommand) -> dict[str, Any]:
        state = self._state_required()
        state.clear_goal(command_seq=command.command_seq)
        self._cancel_inflight_activity()
        return state.public_snapshot()

    @workflow.query
    def state(self) -> dict[str, Any]:
        return self._state_required().public_snapshot()

    def _cancel_inflight_activity(self) -> None:
        task = self._activity_task
        if task is not None and not task.done():
            task.cancel()

    def _reserve_next_operation(self) -> Operation | None:
        state = self._state_required()
        now = self._now_seconds()
        if (
            state.status == SupervisorStatus.ACTIVE
            and state.current_operation is None
            and state.rollover_due(
                threshold_millis=self._config_required().rollover_threshold_millis
            )
        ):
            return state.request_rollover(now_seconds=now)
        return state.next_operation(now_seconds=now)

    def _activity_input(self, operation: Operation) -> CodexOperationInput:
        state = self._state_required()
        return CodexOperationInput(
            operation=operation,
            project_root=state.project_root,
            objective=state.objective,
            objective_sha256=state.objective_sha256,
            lifetime_tokens_used=state.tokens_used,
            lifetime_automatic_turns=state.automatic_turns,
            lifetime_rollovers=state.rollover_count,
            native_thread_id=state.thread_id,
            ambiguous_operation=state.ambiguous_operation,
            metadata={"workflow_key": state.workflow_key},
        )

    def _work_available(self) -> bool:
        state = self._state_required()
        return (
            state.current_operation is not None
            or state.status
            in {
                SupervisorStatus.ACTIVE,
                SupervisorStatus.MANUAL_PENDING,
                SupervisorStatus.RECONCILE_REQUIRED,
            }
        )

    def _operation_still_current(self, operation: Operation) -> bool:
        current = self._state_required().current_operation
        return (
            current is not None
            and current.operation_id == operation.operation_id
            and current.intent_epoch == operation.intent_epoch
        )

    async def _continue_as_new_if_needed(self) -> None:
        if self._activity_task is not None:
            return
        info = workflow.info()
        if not info.is_continue_as_new_suggested():
            return
        if not workflow.all_handlers_finished():
            return
        # WorkflowConfig carries the same SupervisorState object, including the
        # original deadline and all lifetime counters. No rollover resets quota.
        workflow.continue_as_new(self._continue_as_new_config())

    def _continue_as_new_config(self) -> WorkflowConfig:
        # Preserve the entire versioned config rather than reconstructing it
        # from defaults. The durable state contains the original deadline and
        # lifetime counters, so Continue-As-New can compact history only.
        return replace(self._config_required(), state=self._state_required())

    @staticmethod
    def _now_seconds() -> int:
        return int(workflow.now().timestamp())

    def _state_required(self) -> SupervisorState:
        if self._state is None:
            raise RuntimeError("workflow state is not initialized")
        return self._state

    def _config_required(self) -> WorkflowConfig:
        if self._config is None:
            raise RuntimeError("workflow config is not initialized")
        return self._config
