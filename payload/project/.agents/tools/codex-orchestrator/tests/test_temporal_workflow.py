from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from temporalio.converter import DataConverter

from codex_orchestrator.contracts import UserMessageCommand, WorkflowConfig
from codex_orchestrator.domain import (
    Budget,
    OperationKind,
    OperationResult,
    ResultDisposition,
    SupervisorState,
    SupervisorStatus,
)
from codex_orchestrator.temporal_workflow import CodexSupervisorWorkflow


def active_state() -> SupervisorState:
    supervisor = SupervisorState(
        workflow_key="project-a",
        project_root=r"C:\project-a",
        budget=Budget(
            max_automatic_turns=7,
            max_tokens=8_000,
            max_elapsed_seconds=900,
            max_failures=2,
            max_rollovers=3,
        ),
    )
    supervisor.start_goal(
        command_seq=1,
        objective="Pinned objective",
        now_seconds=100,
    )
    return supervisor


class TemporalWorkflowPreemptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_update_is_ready_before_run_method_starts(self) -> None:
        supervisor = active_state()
        workflow = CodexSupervisorWorkflow(WorkflowConfig(state=supervisor))

        snapshot = await workflow.user_message(
            UserMessageCommand(
                command_seq=2,
                message_id="message-2",
                text="Stop the automatic task and answer this.",
            )
        )

        self.assertEqual(SupervisorStatus.MANUAL_PENDING, supervisor.status)
        self.assertEqual(2, supervisor.intent_epoch)
        self.assertEqual("message-2", supervisor.pending_manual.message_id)
        self.assertEqual(
            SupervisorStatus.MANUAL_PENDING.value,
            snapshot["status"],
        )

    async def test_user_update_cancels_task_after_fencing_old_epoch(self) -> None:
        supervisor = active_state()
        old_operation = supervisor.next_operation(now_seconds=101)
        self.assertEqual(OperationKind.AUTOMATIC_TURN, old_operation.kind)
        workflow = CodexSupervisorWorkflow(WorkflowConfig(state=supervisor))

        blocker = asyncio.Event()

        async def in_flight() -> OperationResult:
            await blocker.wait()
            raise AssertionError("cancelled task unexpectedly continued")

        activity_task = asyncio.create_task(in_flight())
        workflow._activity_task = activity_task

        await workflow.user_message(
            UserMessageCommand(
                command_seq=2,
                message_id="message-2",
                text="hard preempt",
            )
        )
        await asyncio.sleep(0)

        self.assertTrue(activity_task.cancelled())
        self.assertEqual(old_operation, supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.current_operation)
        self.assertEqual(SupervisorStatus.RECONCILE_REQUIRED, supervisor.status)
        self.assertEqual(old_operation.intent_epoch + 1, supervisor.intent_epoch)

        with contextlib.suppress(asyncio.CancelledError):
            await activity_task

    async def test_exhausted_reconcile_fence_does_not_hot_loop_or_dispatch(self) -> None:
        supervisor = active_state()
        supervisor.next_operation(now_seconds=101)
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="message-2",
            text="take control",
        )
        supervisor.next_operation(now_seconds=102)
        reconcile = supervisor.current_operation
        assert reconcile is not None
        supervisor.record_result(
            OperationResult(
                operation_id=reconcile.operation_id,
                intent_epoch=reconcile.intent_epoch,
                disposition=ResultDisposition.AMBIGUOUS,
                tokens=0,
                detail="interrupt_unconfirmed",
            ),
            now_seconds=103,
        )
        workflow = CodexSupervisorWorkflow(WorkflowConfig(state=supervisor))

        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertIsNotNone(supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.next_operation(now_seconds=104))
        self.assertFalse(workflow._work_available())


class TemporalContinueAsNewTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_as_new_round_trip_preserves_lifetime_budget_state(
        self,
    ) -> None:
        supervisor = active_state()
        operation = supervisor.next_operation(now_seconds=101)
        supervisor.record_result(
            OperationResult(
                operation_id=operation.operation_id,
                intent_epoch=operation.intent_epoch,
                disposition=ResultDisposition.CONTINUE,
                tokens=321,
                thread_id="thread-1",
                turn_id="turn-1",
                context_window=10_000,
                thread_total_tokens=1_200,
            ),
            now_seconds=102,
        )
        supervisor.failure_count = 1
        supervisor.rollover_count = 2

        original = WorkflowConfig(
            state=supervisor,
            activity_start_to_close_seconds=111,
            activity_schedule_to_close_seconds=222,
            activity_heartbeat_seconds=7,
            rollover_threshold_millis=701,
        )
        workflow = CodexSupervisorWorkflow(original)
        carried = workflow._continue_as_new_config()

        self.assertIs(carried.state, supervisor)
        self.assertEqual(original, carried)

        payloads = await DataConverter.default.encode([carried])
        (restored,) = await DataConverter.default.decode(
            payloads,
            [WorkflowConfig],
        )

        self.assertEqual(supervisor.budget, restored.state.budget)
        self.assertEqual(1, restored.state.automatic_turns)
        self.assertEqual(321, restored.state.tokens_used)
        self.assertEqual(1, restored.state.failure_count)
        self.assertEqual(2, restored.state.rollover_count)
        self.assertEqual(100, restored.state.started_at_seconds)
        self.assertEqual(1_000, restored.state.deadline_seconds)
        self.assertEqual(supervisor.intent_epoch, restored.state.intent_epoch)
        self.assertEqual(supervisor.action_seq, restored.state.action_seq)
        self.assertEqual(supervisor.last_command_seq, restored.state.last_command_seq)
        self.assertEqual(
            original.activity_start_to_close_seconds,
            restored.activity_start_to_close_seconds,
        )
        self.assertEqual(
            original.activity_schedule_to_close_seconds,
            restored.activity_schedule_to_close_seconds,
        )
        self.assertEqual(
            original.activity_heartbeat_seconds,
            restored.activity_heartbeat_seconds,
        )
        self.assertEqual(
            original.rollover_threshold_millis,
            restored.rollover_threshold_millis,
        )


if __name__ == "__main__":
    unittest.main()
