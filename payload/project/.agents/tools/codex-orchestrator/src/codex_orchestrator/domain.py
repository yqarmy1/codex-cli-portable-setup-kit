"""Pure orchestration policy.

This module has no Temporal or Codex dependency.  The durable workflow and its
unit tests both use these transitions, so intent authority and budget rules are
not hidden in prompts or transport callbacks.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1


class DomainError(ValueError):
    """A typed command violates the supervisor contract."""


class StaleCommandError(DomainError):
    """A command sequence was already applied or arrived out of order."""


class InvalidTransitionError(DomainError):
    """A command is not valid for the current supervisor state."""


class SupervisorStatus(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    MANUAL_PENDING = "manual_pending"
    PAUSED = "paused"
    NEEDS_USER = "needs_user"
    RECONCILE_REQUIRED = "reconcile_required"
    BUDGET_LIMITED = "budget_limited"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class OperationKind(StrEnum):
    AUTOMATIC_TURN = "automatic_turn"
    MANUAL_TURN = "manual_turn"
    RECONCILE = "reconcile"
    ROLLOVER = "rollover"


class ResultDisposition(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    NEEDS_USER = "needs_user"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Budget:
    """Lifetime limits for one explicitly created Goal."""

    max_automatic_turns: int = 12
    max_tokens: int = 120_000
    max_elapsed_seconds: int = 7_200
    max_failures: int = 3
    max_rollovers: int = 4

    def validate(self) -> None:
        values = asdict(self)
        invalid = [name for name, value in values.items() if value <= 0]
        if invalid:
            raise DomainError(f"budget values must be positive: {', '.join(invalid)}")


@dataclass(frozen=True)
class PendingManualMessage:
    command_seq: int
    message_id: str
    text: str


@dataclass(frozen=True)
class Operation:
    operation_id: str
    kind: OperationKind
    intent_epoch: int
    action_seq: int
    objective_sha256: str | None
    thread_id: str | None
    message_id: str | None = None
    message_text: str | None = None
    reconcile_of: str | None = None


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    intent_epoch: int
    disposition: ResultDisposition
    tokens: int | None
    thread_id: str | None = None
    turn_id: str | None = None
    context_window: int | None = None
    thread_total_tokens: int | None = None
    detail: str | None = None
    safe_to_retry: bool = False


@dataclass
class SupervisorState:
    """Serializable durable state for one single-writer supervisor."""

    workflow_key: str
    project_root: str
    budget: Budget = field(default_factory=Budget)
    schema_version: int = SCHEMA_VERSION
    status: SupervisorStatus = SupervisorStatus.IDLE
    objective: str | None = None
    objective_sha256: str | None = None
    goal_generation: int = 0
    intent_epoch: int = 0
    last_command_seq: int = 0
    action_seq: int = 0
    started_at_seconds: int | None = None
    deadline_seconds: int | None = None
    thread_id: str | None = None
    current_turn_id: str | None = None
    current_operation: Operation | None = None
    ambiguous_operation: Operation | None = None
    post_reconcile_status: SupervisorStatus | None = None
    pending_manual: PendingManualMessage | None = None
    automatic_turns: int = 0
    manual_turns: int = 0
    tokens_used: int = 0
    failure_count: int = 0
    rollover_count: int = 0
    last_thread_total_tokens: int | None = None
    last_context_window: int | None = None
    discarded_late_results: int = 0
    stop_reason: str | None = None
    last_operation_id: str | None = None

    def __post_init__(self) -> None:
        self.budget.validate()
        if self.schema_version != SCHEMA_VERSION:
            raise DomainError(
                f"unsupported supervisor state schema {self.schema_version}; "
                f"expected {SCHEMA_VERSION}"
            )

    @staticmethod
    def hash_objective(objective: str) -> str:
        return hashlib.sha256(objective.encode("utf-8")).hexdigest()

    def _accept_command(self, command_seq: int) -> None:
        if command_seq <= self.last_command_seq:
            raise StaleCommandError(
                f"command_seq {command_seq} is not newer than {self.last_command_seq}"
            )
        self.last_command_seq = command_seq

    def _reject_if_unresolved_fence(self) -> None:
        if (
            self.status in {SupervisorStatus.FAILED, SupervisorStatus.CANCELLED}
            and self.ambiguous_operation is not None
        ):
            raise InvalidTransitionError(
                "an unresolved Codex operation exhausted reconciliation; "
                "the durable controller is fenced off"
            )

    def _invalidate_inflight(self) -> None:
        operation = self.current_operation
        if operation is not None:
            # A reconciliation Activity does not create a new Codex side
            # effect.  If it is preempted, keep fencing the original operation
            # instead of trying to reconcile the reconciliation operation ID.
            if operation.kind != OperationKind.RECONCILE or self.ambiguous_operation is None:
                self.ambiguous_operation = operation
        self.intent_epoch += 1
        self.current_operation = None
        self.current_turn_id = None

    def _set_target_status(self, target: SupervisorStatus) -> None:
        """Enter a target state, fencing uncertain old work first when needed."""

        if self.ambiguous_operation is not None:
            self.post_reconcile_status = target
            self.status = SupervisorStatus.RECONCILE_REQUIRED
        else:
            self.post_reconcile_status = None
            self.status = target

    def start_goal(
        self,
        *,
        command_seq: int,
        objective: str,
        now_seconds: int,
        budget: Budget | None = None,
    ) -> None:
        """Create or replace a Goal only through an explicit typed command."""

        if not objective.strip():
            raise DomainError("objective must not be blank")
        selected_budget = budget or self.budget
        selected_budget.validate()
        self._reject_if_unresolved_fence()
        self._accept_command(command_seq)
        self._invalidate_inflight()
        self.goal_generation += 1
        self.objective = objective
        self.objective_sha256 = self.hash_objective(objective)
        self.budget = selected_budget
        self._set_target_status(SupervisorStatus.ACTIVE)
        self.started_at_seconds = now_seconds
        self.deadline_seconds = now_seconds + selected_budget.max_elapsed_seconds
        self.pending_manual = None
        self.automatic_turns = 0
        self.manual_turns = 0
        self.tokens_used = 0
        self.failure_count = 0
        self.rollover_count = 0
        self.last_thread_total_tokens = None
        self.last_context_window = None
        self.stop_reason = None

    def preempt_with_user_message(
        self,
        *,
        command_seq: int,
        message_id: str,
        text: str,
    ) -> None:
        """Hard-preempt automation without interpreting text as an objective."""

        if not message_id.strip():
            raise DomainError("message_id must not be blank")
        if not text.strip():
            raise DomainError("user message must not be blank")
        self._reject_if_unresolved_fence()
        self._accept_command(command_seq)
        self._invalidate_inflight()
        self.pending_manual = PendingManualMessage(command_seq, message_id, text)
        self._set_target_status(SupervisorStatus.MANUAL_PENDING)
        self.stop_reason = "preempted_by_user_message"

    def pause_goal(self, *, command_seq: int) -> None:
        if self.objective is None:
            raise InvalidTransitionError("there is no Goal to pause")
        self._reject_if_unresolved_fence()
        self._accept_command(command_seq)
        self._invalidate_inflight()
        self.pending_manual = None
        self._set_target_status(SupervisorStatus.PAUSED)
        self.stop_reason = "paused_by_user"

    def resume_goal(self, *, command_seq: int, now_seconds: int) -> None:
        if self.objective is None:
            raise InvalidTransitionError("there is no Goal to resume")
        if self.status in {SupervisorStatus.COMPLETED, SupervisorStatus.CANCELLED}:
            raise InvalidTransitionError(f"cannot resume a {self.status.value} Goal")
        self._reject_if_unresolved_fence()
        reason = self.budget_stop_reason(now_seconds)
        if reason is not None:
            self.status = SupervisorStatus.BUDGET_LIMITED
            self.stop_reason = reason
            raise InvalidTransitionError(f"cannot resume: {reason}")
        self._accept_command(command_seq)
        self._invalidate_inflight()
        self.pending_manual = None
        self._set_target_status(SupervisorStatus.ACTIVE)
        self.stop_reason = None

    def clear_goal(self, *, command_seq: int) -> None:
        self._accept_command(command_seq)
        self._invalidate_inflight()
        self.objective = None
        self.objective_sha256 = None
        self.pending_manual = None
        self.post_reconcile_status = None
        self.status = SupervisorStatus.CANCELLED
        self.stop_reason = (
            "cleared_by_user_with_unresolved_operation"
            if self.ambiguous_operation is not None
            else "cleared_by_user"
        )

    def budget_stop_reason(self, now_seconds: int) -> str | None:
        if self.automatic_turns >= self.budget.max_automatic_turns:
            return "automatic_turn_budget_exhausted"
        if self.tokens_used >= self.budget.max_tokens:
            return "token_budget_exhausted"
        if self.failure_count >= self.budget.max_failures:
            return "failure_budget_exhausted"
        if self.rollover_count >= self.budget.max_rollovers:
            return "rollover_budget_exhausted"
        if self.deadline_seconds is not None and now_seconds >= self.deadline_seconds:
            return "wall_clock_budget_exhausted"
        return None

    def next_operation(self, *, now_seconds: int) -> Operation | None:
        """Reserve exactly one operation; repeated calls return the same lease."""

        if self.current_operation is not None:
            return self.current_operation

        if (
            self.ambiguous_operation is not None
            and self.status == SupervisorStatus.RECONCILE_REQUIRED
        ):
            kind = OperationKind.RECONCILE
            message_id = None
            message_text = None
        elif self.ambiguous_operation is not None:
            # A retained ambiguity outside RECONCILE_REQUIRED is an exhausted
            # safety fence, not permission to perform another read loop.
            return None
        elif self.status == SupervisorStatus.MANUAL_PENDING:
            if self.pending_manual is None:
                raise InvalidTransitionError("manual_pending state lost its message")
            kind = OperationKind.MANUAL_TURN
            message_id = self.pending_manual.message_id
            message_text = self.pending_manual.text
        elif self.status == SupervisorStatus.ACTIVE:
            reason = self.budget_stop_reason(now_seconds)
            if reason is not None:
                self.status = SupervisorStatus.BUDGET_LIMITED
                self.stop_reason = reason
                return None
            if self.objective is None or self.objective_sha256 is None:
                raise InvalidTransitionError("active state has no pinned objective")
            kind = OperationKind.AUTOMATIC_TURN
            message_id = None
            message_text = None
        else:
            return None

        self.action_seq += 1
        operation_id = (
            f"codex-op:{self.workflow_key}:{self.goal_generation}:"
            f"{self.intent_epoch}:{self.action_seq}:{kind.value}"
        )
        self.current_operation = Operation(
            operation_id=operation_id,
            kind=kind,
            intent_epoch=self.intent_epoch,
            action_seq=self.action_seq,
            objective_sha256=self.objective_sha256,
            thread_id=self.thread_id,
            message_id=message_id,
            message_text=message_text,
            reconcile_of=(
                self.ambiguous_operation.operation_id
                if kind == OperationKind.RECONCILE and self.ambiguous_operation is not None
                else None
            ),
        )
        return self.current_operation

    def request_rollover(self, *, now_seconds: int) -> Operation | None:
        if self.status != SupervisorStatus.ACTIVE or self.current_operation is not None:
            return None
        reason = self.budget_stop_reason(now_seconds)
        if reason is not None:
            self.status = SupervisorStatus.BUDGET_LIMITED
            self.stop_reason = reason
            return None
        self.action_seq += 1
        operation_id = (
            f"codex-op:{self.workflow_key}:{self.goal_generation}:"
            f"{self.intent_epoch}:{self.action_seq}:{OperationKind.ROLLOVER.value}"
        )
        self.current_operation = Operation(
            operation_id=operation_id,
            kind=OperationKind.ROLLOVER,
            intent_epoch=self.intent_epoch,
            action_seq=self.action_seq,
            objective_sha256=self.objective_sha256,
            thread_id=self.thread_id,
        )
        return self.current_operation

    def record_result(self, result: OperationResult, *, now_seconds: int) -> bool:
        """Apply a result only when its operation and epoch still own the lease."""

        operation = self.current_operation
        if (
            operation is None
            or result.operation_id != operation.operation_id
            or result.intent_epoch != self.intent_epoch
            or result.intent_epoch != operation.intent_epoch
        ):
            self.discarded_late_results += 1
            return False

        # Validate the complete Activity payload before mutating durable state.
        # An invalid result must not release the lease or partially move the
        # thread/token projections before the Workflow Task fails closed.
        if result.tokens is not None and result.tokens < 0:
            raise DomainError("result tokens must not be negative")
        if result.thread_total_tokens is not None and result.thread_total_tokens < 0:
            raise DomainError("thread_total_tokens must not be negative")
        if result.context_window is not None and result.context_window <= 0:
            raise DomainError("context_window must be positive")
        if type(result.safe_to_retry) is not bool:
            raise DomainError("safe_to_retry must be a boolean")

        self.last_operation_id = operation.operation_id
        self.current_operation = None
        if result.thread_id is not None:
            self.thread_id = result.thread_id
        self.current_turn_id = result.turn_id
        if result.thread_total_tokens is not None:
            self.last_thread_total_tokens = result.thread_total_tokens
        if result.context_window is not None:
            self.last_context_window = result.context_window

        if operation.kind == OperationKind.RECONCILE:
            target_operation = self.ambiguous_operation
            if result.disposition == ResultDisposition.AMBIGUOUS:
                # One bounded reconciliation attempt only. Re-running an
                # inconclusive read loop would recreate the old runaway poller.
                self.post_reconcile_status = None
                self.pending_manual = None
                self.status = SupervisorStatus.FAILED
                self.stop_reason = result.detail or "reconciliation_inconclusive"
                return True

            expected_disposition = (
                ResultDisposition.CONTINUE
                if target_operation is not None
                and target_operation.kind == OperationKind.ROLLOVER
                else ResultDisposition.INTERRUPTED
            )
            invalid_rollover_thread = (
                target_operation is not None
                and target_operation.kind == OperationKind.ROLLOVER
                and (
                    result.thread_id is None
                    or result.thread_id == target_operation.thread_id
                )
            )
            if (
                target_operation is None
                or result.disposition != expected_disposition
                or invalid_rollover_thread
            ):
                # Adapter invariant failures and category-confused results are
                # not evidence that the uncertain Codex side effect is safe.
                self.post_reconcile_status = None
                self.pending_manual = None
                self.status = SupervisorStatus.FAILED
                self.stop_reason = result.detail or (
                    "reconciliation_unexpected_disposition:"
                    f"{result.disposition.value}"
                )
                return True

            self.ambiguous_operation = None
            target = self.post_reconcile_status or SupervisorStatus.PAUSED
            self.post_reconcile_status = None
            if target != SupervisorStatus.MANUAL_PENDING:
                self.pending_manual = None
            self.status = target
            self.stop_reason = (
                None
                if target in {SupervisorStatus.ACTIVE, SupervisorStatus.MANUAL_PENDING}
                else result.detail or "reconciled_requires_explicit_resume"
            )
            return True

        if result.disposition == ResultDisposition.AMBIGUOUS:
            self.ambiguous_operation = operation
            self.post_reconcile_status = (
                SupervisorStatus.PAUSED
                if self.objective is not None
                else SupervisorStatus.IDLE
            )
            self.status = SupervisorStatus.RECONCILE_REQUIRED
            self.stop_reason = result.detail or "operation_outcome_ambiguous"
            return True

        if operation.kind == OperationKind.ROLLOVER:
            valid_new_thread = (
                result.thread_id is not None
                and result.thread_id != operation.thread_id
            )
            if (
                result.disposition != ResultDisposition.CONTINUE
                or not valid_new_thread
            ):
                self.failure_count += 1
                self.status = SupervisorStatus.FAILED
                self.stop_reason = result.detail or "rollover_result_not_proven"
            else:
                self.rollover_count += 1
                self.last_thread_total_tokens = None
                self.last_context_window = None
                self.status = SupervisorStatus.ACTIVE
                self.stop_reason = None
            return True

        is_automatic = operation.kind == OperationKind.AUTOMATIC_TURN
        if is_automatic and result.tokens is None:
            self.ambiguous_operation = operation
            self.post_reconcile_status = SupervisorStatus.PAUSED
            self.status = SupervisorStatus.RECONCILE_REQUIRED
            self.stop_reason = "authoritative_usage_missing"
            return True
        if result.tokens is not None:
            self.tokens_used += result.tokens

        if is_automatic:
            self.automatic_turns += 1
        else:
            self.manual_turns += 1
            self.pending_manual = None

        if result.disposition == ResultDisposition.COMPLETE and is_automatic:
            self.status = SupervisorStatus.COMPLETED
            self.stop_reason = None
            return True
        if result.disposition == ResultDisposition.NEEDS_USER:
            self.status = SupervisorStatus.NEEDS_USER
            self.stop_reason = result.detail or "codex_requires_user_input"
            return True
        if result.disposition == ResultDisposition.FAILED:
            self.failure_count += 1
            reason = self.budget_stop_reason(now_seconds)
            if is_automatic and result.safe_to_retry and reason is None:
                self.status = SupervisorStatus.ACTIVE
                self.stop_reason = result.detail or "automatic_turn_failed"
            else:
                self.status = SupervisorStatus.FAILED
                self.stop_reason = reason or result.detail or "turn_failed"
            return True
        if result.disposition == ResultDisposition.INTERRUPTED:
            self.status = SupervisorStatus.PAUSED if self.objective is not None else SupervisorStatus.IDLE
            self.stop_reason = "turn_interrupted"
            return True

        if is_automatic:
            reason = self.budget_stop_reason(now_seconds)
            if reason is not None:
                self.status = SupervisorStatus.BUDGET_LIMITED
                self.stop_reason = reason
            else:
                self.status = SupervisorStatus.ACTIVE
                self.stop_reason = None
        else:
            # A manual message never silently resumes an earlier Goal.
            self.status = SupervisorStatus.PAUSED if self.objective is not None else SupervisorStatus.IDLE
            self.stop_reason = "manual_turn_completed"
        return True

    def rollover_due(self, *, threshold_millis: int = 650) -> bool:
        """Return whether the current thread crossed the configured context ratio."""

        if threshold_millis <= 0 or threshold_millis >= 1_000:
            raise DomainError("rollover threshold must be between 1 and 999 millis")
        if self.last_thread_total_tokens is None or self.last_context_window is None:
            return False
        return (
            self.last_thread_total_tokens * 1_000
            >= self.last_context_window * threshold_millis
        )

    def public_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe audit view without raw user-controlled text."""

        snapshot = asdict(self)
        snapshot.pop("objective", None)
        pending = snapshot.get("pending_manual")
        if isinstance(pending, dict):
            pending.pop("text", None)
        for field_name in ("current_operation", "ambiguous_operation"):
            operation = snapshot.get(field_name)
            if isinstance(operation, dict):
                operation.pop("message_text", None)
        snapshot["objective_present"] = self.objective is not None
        return snapshot
