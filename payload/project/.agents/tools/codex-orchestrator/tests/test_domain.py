from __future__ import annotations

import copy
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_orchestrator.domain import (
    Budget,
    InvalidTransitionError,
    OperationKind,
    OperationResult,
    ResultDisposition,
    StaleCommandError,
    SupervisorState,
    SupervisorStatus,
)


def state(*, budget: Budget | None = None) -> SupervisorState:
    return SupervisorState(
        workflow_key="project-a",
        project_root=r"C:\project-a",
        budget=budget or Budget(),
    )


def result_for(
    supervisor: SupervisorState,
    *,
    disposition: ResultDisposition = ResultDisposition.CONTINUE,
    tokens: int | None = 100,
    thread_id: str = "thread-1",
    turn_id: str | None = "turn-1",
) -> OperationResult:
    operation = supervisor.current_operation
    assert operation is not None
    return OperationResult(
        operation_id=operation.operation_id,
        intent_epoch=operation.intent_epoch,
        disposition=disposition,
        tokens=tokens,
        thread_id=thread_id,
        turn_id=turn_id,
        context_window=10_000,
        thread_total_tokens=1_000,
    )


class SupervisorAuthorityTests(unittest.TestCase):
    def test_public_snapshot_never_exposes_objective_or_message_text(self) -> None:
        supervisor = state()
        secret_objective = "exact objective text must remain private"
        secret_message = "raw user message must remain private"
        supervisor.start_goal(
            command_seq=1,
            objective=secret_objective,
            now_seconds=10,
        )
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="message-2",
            text=secret_message,
        )
        supervisor.next_operation(now_seconds=11)

        snapshot = supervisor.public_snapshot()
        encoded = repr(snapshot)
        self.assertNotIn(secret_objective, encoded)
        self.assertNotIn(secret_message, encoded)
        self.assertTrue(snapshot["objective_present"])
        self.assertEqual(
            supervisor.objective_sha256,
            snapshot["objective_sha256"],
        )

    def test_ordinary_message_cannot_replace_goal_even_when_it_quotes_old_work(self) -> None:
        supervisor = state()
        exact = "Ship exactly one verified APK."
        supervisor.start_goal(command_seq=1, objective=exact, now_seconds=10)

        quoted_complaint = (
            'The previous task said "collect Java/native evidence". '
            "Stop drifting and fix the controller."
        )
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="user-2",
            text=quoted_complaint,
        )

        self.assertEqual(exact, supervisor.objective)
        self.assertEqual(SupervisorState.hash_objective(exact), supervisor.objective_sha256)
        self.assertEqual(SupervisorStatus.MANUAL_PENDING, supervisor.status)
        self.assertEqual(quoted_complaint, supervisor.pending_manual.text)

    def test_objective_bytes_are_preserved_exactly(self) -> None:
        supervisor = state()
        exact = "  Preserve spacing, \u4e2d\u6587, and newline\nexactly.  "
        supervisor.start_goal(command_seq=1, objective=exact, now_seconds=10)
        self.assertEqual(exact, supervisor.objective)
        self.assertEqual(SupervisorState.hash_objective(exact), supervisor.objective_sha256)

    def test_duplicate_or_out_of_order_command_is_rejected(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=5, objective="A", now_seconds=10)
        with self.assertRaises(StaleCommandError):
            supervisor.pause_goal(command_seq=5)
        with self.assertRaises(StaleCommandError):
            supervisor.pause_goal(command_seq=4)

    def test_invalid_blank_command_does_not_consume_sequence(self) -> None:
        supervisor = state()
        with self.assertRaises(ValueError):
            supervisor.start_goal(command_seq=1, objective="  ", now_seconds=10)
        supervisor.start_goal(command_seq=1, objective="valid", now_seconds=10)
        self.assertEqual(1, supervisor.last_command_seq)


class SupervisorPreemptionTests(unittest.TestCase):
    def test_user_message_fences_inflight_before_manual_dispatch(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        old = supervisor.next_operation(now_seconds=11)
        self.assertEqual(OperationKind.AUTOMATIC_TURN, old.kind)

        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="m2",
            text="Change direction",
        )

        self.assertEqual(SupervisorStatus.RECONCILE_REQUIRED, supervisor.status)
        cleanup = supervisor.next_operation(now_seconds=12)
        self.assertEqual(OperationKind.RECONCILE, cleanup.kind)
        self.assertEqual(old.operation_id, cleanup.reconcile_of)

        supervisor.record_result(
            result_for(supervisor, disposition=ResultDisposition.INTERRUPTED, tokens=0),
            now_seconds=13,
        )
        self.assertEqual(SupervisorStatus.MANUAL_PENDING, supervisor.status)
        manual = supervisor.next_operation(now_seconds=14)
        self.assertEqual(OperationKind.MANUAL_TURN, manual.kind)
        self.assertEqual("Change direction", manual.message_text)

    def test_late_old_epoch_result_is_discarded(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        old = supervisor.next_operation(now_seconds=11)
        old_result = OperationResult(
            operation_id=old.operation_id,
            intent_epoch=old.intent_epoch,
            disposition=ResultDisposition.COMPLETE,
            tokens=1_000,
            thread_id="old-thread",
            turn_id="old-turn",
        )
        supervisor.pause_goal(command_seq=2)

        self.assertFalse(supervisor.record_result(old_result, now_seconds=12))
        self.assertNotEqual(SupervisorStatus.COMPLETED, supervisor.status)
        self.assertEqual(0, supervisor.tokens_used)
        self.assertEqual(1, supervisor.discarded_late_results)

    def test_preempting_a_reconcile_keeps_the_original_side_effect_target(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        old_turn = supervisor.next_operation(now_seconds=11)
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="m2",
            text="first correction",
        )
        old_reconcile = supervisor.next_operation(now_seconds=12)
        self.assertEqual(OperationKind.RECONCILE, old_reconcile.kind)

        supervisor.preempt_with_user_message(
            command_seq=3,
            message_id="m3",
            text="latest correction",
        )
        replacement_reconcile = supervisor.next_operation(now_seconds=13)

        self.assertEqual(OperationKind.RECONCILE, replacement_reconcile.kind)
        self.assertEqual(old_turn.operation_id, replacement_reconcile.reconcile_of)
        self.assertNotEqual(old_reconcile.operation_id, replacement_reconcile.reconcile_of)

        late_reconcile_result = OperationResult(
            operation_id=old_reconcile.operation_id,
            intent_epoch=old_reconcile.intent_epoch,
            disposition=ResultDisposition.INTERRUPTED,
            tokens=0,
            thread_id="thread-1",
        )
        self.assertFalse(
            supervisor.record_result(late_reconcile_result, now_seconds=14)
        )
        self.assertEqual(replacement_reconcile, supervisor.current_operation)

        supervisor.record_result(
            result_for(
                supervisor,
                disposition=ResultDisposition.INTERRUPTED,
                tokens=0,
            ),
            now_seconds=15,
        )
        manual = supervisor.next_operation(now_seconds=16)
        self.assertEqual(OperationKind.MANUAL_TURN, manual.kind)
        self.assertEqual("m3", manual.message_id)
        self.assertEqual("latest correction", manual.message_text)

    def test_ambiguous_manual_turn_without_goal_reconciles_to_idle(self) -> None:
        supervisor = state()
        supervisor.preempt_with_user_message(
            command_seq=1,
            message_id="m1",
            text="one manual turn",
        )
        supervisor.next_operation(now_seconds=10)
        supervisor.record_result(
            result_for(
                supervisor,
                disposition=ResultDisposition.AMBIGUOUS,
                tokens=None,
            ),
            now_seconds=11,
        )

        self.assertEqual(SupervisorStatus.RECONCILE_REQUIRED, supervisor.status)
        supervisor.next_operation(now_seconds=12)
        supervisor.record_result(
            result_for(
                supervisor,
                disposition=ResultDisposition.INTERRUPTED,
                tokens=0,
            ),
            now_seconds=13,
        )

        self.assertEqual(SupervisorStatus.IDLE, supervisor.status)
        self.assertIsNone(supervisor.pending_manual)
        self.assertIsNone(supervisor.next_operation(now_seconds=14))

    def test_manual_turn_never_silently_resumes_goal(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.preempt_with_user_message(command_seq=2, message_id="m", text="status?")
        manual = supervisor.next_operation(now_seconds=11)
        self.assertEqual(OperationKind.MANUAL_TURN, manual.kind)
        supervisor.record_result(result_for(supervisor), now_seconds=12)
        self.assertEqual(SupervisorStatus.PAUSED, supervisor.status)
        self.assertIsNone(supervisor.next_operation(now_seconds=13))


class SupervisorBudgetTests(unittest.TestCase):
    def test_turn_budget_is_lifetime_and_survives_state_rollover(self) -> None:
        supervisor = state(
            budget=Budget(
                max_automatic_turns=2,
                max_tokens=10_000,
                max_elapsed_seconds=1_000,
                max_failures=2,
                max_rollovers=2,
            )
        )
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        supervisor.record_result(result_for(supervisor, tokens=100), now_seconds=12)

        restored = copy.deepcopy(supervisor)
        restored.next_operation(now_seconds=13)
        restored.record_result(result_for(restored, tokens=100), now_seconds=14)

        self.assertEqual(2, restored.automatic_turns)
        self.assertIsNone(restored.next_operation(now_seconds=15))
        self.assertEqual(SupervisorStatus.BUDGET_LIMITED, restored.status)
        self.assertEqual("automatic_turn_budget_exhausted", restored.stop_reason)

    def test_missing_authoritative_usage_fails_closed(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        supervisor.record_result(result_for(supervisor, tokens=None), now_seconds=12)
        self.assertEqual(SupervisorStatus.RECONCILE_REQUIRED, supervisor.status)
        self.assertEqual("authoritative_usage_missing", supervisor.stop_reason)
        self.assertEqual(OperationKind.RECONCILE, supervisor.next_operation(now_seconds=13).kind)

    def test_wall_clock_budget_does_not_reset_at_turn_boundary(self) -> None:
        supervisor = state(
            budget=Budget(
                max_automatic_turns=10,
                max_tokens=10_000,
                max_elapsed_seconds=10,
                max_failures=2,
                max_rollovers=2,
            )
        )
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=100)
        self.assertIsNotNone(supervisor.next_operation(now_seconds=109))
        supervisor.record_result(result_for(supervisor, tokens=10), now_seconds=110)
        self.assertEqual(SupervisorStatus.BUDGET_LIMITED, supervisor.status)
        self.assertEqual("wall_clock_budget_exhausted", supervisor.stop_reason)

    def test_context_rollover_preserves_lifetime_counters(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        outcome = result_for(supervisor, tokens=500)
        outcome = replace(outcome, context_window=1_000, thread_total_tokens=700)
        supervisor.record_result(outcome, now_seconds=12)
        self.assertTrue(supervisor.rollover_due())
        rollover = supervisor.request_rollover(now_seconds=13)
        self.assertEqual(OperationKind.ROLLOVER, rollover.kind)
        supervisor.record_result(
            result_for(supervisor, tokens=0, thread_id="thread-2", turn_id=None),
            now_seconds=14,
        )
        self.assertEqual(1, supervisor.rollover_count)
        self.assertEqual(1, supervisor.automatic_turns)
        self.assertEqual(500, supervisor.tokens_used)
        self.assertEqual("thread-2", supervisor.thread_id)

    def test_rollover_must_prove_a_distinct_new_thread(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        outcome = replace(
            result_for(supervisor, tokens=500),
            context_window=1_000,
            thread_total_tokens=700,
        )
        supervisor.record_result(outcome, now_seconds=12)
        rollover = supervisor.request_rollover(now_seconds=13)

        supervisor.record_result(
            OperationResult(
                operation_id=rollover.operation_id,
                intent_epoch=rollover.intent_epoch,
                disposition=ResultDisposition.CONTINUE,
                tokens=0,
                thread_id="thread-1",
                detail="claimed_success_without_new_thread",
            ),
            now_seconds=14,
        )

        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertEqual(0, supervisor.rollover_count)
        self.assertEqual(1, supervisor.failure_count)
        self.assertIsNone(supervisor.next_operation(now_seconds=15))


class SupervisorReconciliationTests(unittest.TestCase):
    def test_automatic_failure_retries_only_when_explicitly_proven_safe(self) -> None:
        fatal = state()
        fatal.start_goal(command_seq=1, objective="A", now_seconds=10)
        fatal.next_operation(now_seconds=11)
        fatal.record_result(
            result_for(
                fatal,
                disposition=ResultDisposition.FAILED,
                tokens=10,
            ),
            now_seconds=12,
        )
        self.assertEqual(SupervisorStatus.FAILED, fatal.status)
        self.assertIsNone(fatal.next_operation(now_seconds=13))

        retryable = state()
        retryable.start_goal(command_seq=1, objective="A", now_seconds=10)
        retryable.next_operation(now_seconds=11)
        operation = retryable.current_operation
        assert operation is not None
        retryable.record_result(
            OperationResult(
                operation_id=operation.operation_id,
                intent_epoch=operation.intent_epoch,
                disposition=ResultDisposition.FAILED,
                tokens=0,
                detail="known_pre_submit_rejection",
                safe_to_retry=True,
            ),
            now_seconds=12,
        )
        self.assertEqual(SupervisorStatus.ACTIVE, retryable.status)
        self.assertIsNotNone(retryable.next_operation(now_seconds=13))

    def test_clear_is_always_accepted_but_preserves_unresolved_fence(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="message-2",
            text="stop now",
        )

        unresolved = supervisor.ambiguous_operation
        self.assertIsNotNone(unresolved)
        supervisor.clear_goal(command_seq=3)

        self.assertEqual(SupervisorStatus.CANCELLED, supervisor.status)
        self.assertIsNone(supervisor.objective)
        self.assertIsNone(supervisor.pending_manual)
        self.assertEqual(unresolved, supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.next_operation(now_seconds=12))
        with self.assertRaises(InvalidTransitionError):
            supervisor.start_goal(command_seq=4, objective="B", now_seconds=13)
        self.assertEqual(3, supervisor.last_command_seq)

    def _reconciling_preempted_turn(self) -> SupervisorState:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="m2",
            text="take control",
        )
        reconcile = supervisor.next_operation(now_seconds=12)
        self.assertEqual(OperationKind.RECONCILE, reconcile.kind)
        return supervisor

    def test_inconclusive_reconcile_fails_closed_after_one_attempt(self) -> None:
        supervisor = self._reconciling_preempted_turn()
        inconclusive = replace(
            result_for(
                supervisor,
                disposition=ResultDisposition.AMBIGUOUS,
                tokens=0,
            ),
            detail="active_turn_interrupt_unconfirmed",
        )

        self.assertTrue(supervisor.record_result(inconclusive, now_seconds=13))
        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertEqual("active_turn_interrupt_unconfirmed", supervisor.stop_reason)
        unresolved = supervisor.ambiguous_operation
        self.assertIsNotNone(unresolved)
        self.assertEqual(OperationKind.AUTOMATIC_TURN, unresolved.kind)
        self.assertIsNone(supervisor.pending_manual)
        self.assertIsNone(supervisor.next_operation(now_seconds=14))
        self.assertIsNone(supervisor.next_operation(now_seconds=15))

        with self.assertRaises(InvalidTransitionError):
            supervisor.preempt_with_user_message(
                command_seq=3,
                message_id="m3",
                text="do not cross the unresolved fence",
            )
        self.assertEqual(2, supervisor.last_command_seq)
        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertEqual(unresolved, supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.next_operation(now_seconds=16))

    def test_failed_reconcile_result_cannot_resume_pending_manual_work(self) -> None:
        supervisor = self._reconciling_preempted_turn()
        failed = replace(
            result_for(
                supervisor,
                disposition=ResultDisposition.FAILED,
                tokens=0,
            ),
            detail="adapter_invariant:AdapterInvariantError",
        )

        self.assertTrue(supervisor.record_result(failed, now_seconds=13))
        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertEqual(
            "adapter_invariant:AdapterInvariantError",
            supervisor.stop_reason,
        )
        self.assertIsNotNone(supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.next_operation(now_seconds=14))

    def test_non_rollover_reconcile_rejects_continue_as_category_confusion(self) -> None:
        supervisor = self._reconciling_preempted_turn()

        supervisor.record_result(
            result_for(
                supervisor,
                disposition=ResultDisposition.CONTINUE,
                tokens=0,
            ),
            now_seconds=13,
        )

        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertIsNotNone(supervisor.ambiguous_operation)
        self.assertIsNone(supervisor.next_operation(now_seconds=14))

    def test_rollover_reconcile_rejects_the_old_thread_as_success(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        supervisor.next_operation(now_seconds=11)
        outcome = replace(
            result_for(supervisor, tokens=500),
            context_window=1_000,
            thread_total_tokens=700,
        )
        supervisor.record_result(outcome, now_seconds=12)
        rollover = supervisor.request_rollover(now_seconds=13)
        self.assertEqual("thread-1", rollover.thread_id)
        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="m2",
            text="stop during rollover",
        )
        reconcile = supervisor.next_operation(now_seconds=14)

        supervisor.record_result(
            OperationResult(
                operation_id=reconcile.operation_id,
                intent_epoch=reconcile.intent_epoch,
                disposition=ResultDisposition.CONTINUE,
                tokens=0,
                thread_id="thread-1",
                detail="old_thread_is_not_rollover_proof",
            ),
            now_seconds=15,
        )

        self.assertEqual(SupervisorStatus.FAILED, supervisor.status)
        self.assertEqual(rollover, supervisor.ambiguous_operation)
        self.assertEqual(0, supervisor.rollover_count)
        self.assertIsNone(supervisor.next_operation(now_seconds=16))

    def test_last_operation_id_changes_only_for_an_owned_result(self) -> None:
        supervisor = state()
        supervisor.start_goal(command_seq=1, objective="A", now_seconds=10)
        operation = supervisor.next_operation(now_seconds=11)
        self.assertIsNone(supervisor.last_operation_id)

        supervisor.record_result(result_for(supervisor), now_seconds=12)
        self.assertEqual(operation.operation_id, supervisor.last_operation_id)

        supervisor.preempt_with_user_message(
            command_seq=2,
            message_id="m2",
            text="manual",
        )
        self.assertEqual(operation.operation_id, supervisor.last_operation_id)


if __name__ == "__main__":
    unittest.main()
