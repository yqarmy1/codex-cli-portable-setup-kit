"""Typed Temporal command and activity payloads."""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import Budget, Operation, SupervisorState


@dataclass(frozen=True)
class WorkflowConfig:
    state: SupervisorState
    activity_start_to_close_seconds: int = 1_800
    activity_schedule_to_close_seconds: int = 1_860
    activity_heartbeat_seconds: int = 10
    rollover_threshold_millis: int = 650

    def validate(self) -> None:
        if self.activity_start_to_close_seconds <= 0:
            raise ValueError("activity_start_to_close_seconds must be positive")
        if self.activity_schedule_to_close_seconds < self.activity_start_to_close_seconds:
            raise ValueError("schedule-to-close must cover start-to-close")
        if self.activity_heartbeat_seconds <= 0:
            raise ValueError("activity_heartbeat_seconds must be positive")
        if not 0 < self.rollover_threshold_millis < 1_000:
            raise ValueError("rollover_threshold_millis must be between 1 and 999")


@dataclass(frozen=True)
class StartGoalCommand:
    command_seq: int
    objective: str
    budget: Budget | None = None


@dataclass(frozen=True)
class UserMessageCommand:
    command_seq: int
    message_id: str
    text: str


@dataclass(frozen=True)
class ControlCommand:
    command_seq: int


@dataclass(frozen=True)
class CodexOperationInput:
    operation: Operation
    project_root: str
    objective: str | None
    objective_sha256: str | None
    lifetime_tokens_used: int
    lifetime_automatic_turns: int
    lifetime_rollovers: int
    native_thread_id: str | None
    ambiguous_operation: Operation | None = None
    metadata: dict[str, str] = field(default_factory=dict)

