"""Thin interactive command and ephemeral-output boundary.

This module deliberately contains no scheduler, agent loop, Temporal state, or
Codex client.  A host wires :class:`InteractiveBackend` to the existing typed
Workflow Updates and wires :class:`EphemeralAssistantBus.observe` directly to
the in-process Codex adapter.  Assistant text exists only in the bounded
``asyncio.Queue`` and in the display callback; it is never returned to the
Workflow or included in a state snapshot.

The command grammar is intentionally narrow.  Only ``/goal <objective>`` can
create or replace an automatic Goal.  Every non-blank input that is not one of
the exact control commands is a normal user message, including quotations and
unknown slash-prefixed text.  ``/cancel`` is the canonical destructive control;
``/clear`` remains an exact compatibility alias for the same typed Update.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


MAX_EPHEMERAL_TEXT_BYTES = 1024 * 1024
DEFAULT_EPHEMERAL_QUEUE_SIZE = 16


class CommandSyntaxError(ValueError):
    """Input cannot be represented by the explicit command grammar."""


class BackendSnapshotError(RuntimeError):
    """A backend response cannot safely establish the current intent epoch."""


class UnknownCommandOutcome(RuntimeError):
    """A typed Update may have been accepted but its result was not observed."""

    def __init__(self) -> None:
        super().__init__("typed Update outcome is unknown; use exact /retry")


class PendingCommandError(RuntimeError):
    """A new mutation is fenced behind an unresolved command ID."""


class CommandKind(str, Enum):
    START_GOAL = "start_goal"
    USER_MESSAGE = "user_message"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    CLEAR = "clear"
    STATUS = "status"
    RETRY = "retry"
    QUIT = "quit"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    kind: CommandKind
    # Keep user-controlled text out of dataclass reprs and accidental logs.
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """Exact in-memory payload retained only for explicit same-ID retry."""

    kind: CommandKind
    command_id: str
    message_id: str | None = None
    text: str | None = field(default=None, repr=False)
    exit_after_success: bool = False


def parse_input(line: str) -> ParsedCommand:
    """Parse one input without promoting ordinary text into a Goal.

    Control commands must match exactly.  The one ASCII space or tab following
    ``/goal`` is syntax and is removed; every byte after it is objective data.
    This means ``/goal  keep-leading-space`` deliberately pins an objective
    beginning with one space.  ``/goalx`` and ``/pause please`` are ordinary
    user messages, not fuzzy command matches.
    """

    if not isinstance(line, str):
        raise TypeError("interactive input must be text")
    if not line.strip():
        raise CommandSyntaxError("input must not be blank")

    exact_controls = {
        "/pause": CommandKind.PAUSE,
        "/resume": CommandKind.RESUME,
        "/cancel": CommandKind.CANCEL,
        "/clear": CommandKind.CLEAR,
        "/status": CommandKind.STATUS,
        "/retry": CommandKind.RETRY,
        "/quit": CommandKind.QUIT,
    }
    control = exact_controls.get(line)
    if control is not None:
        return ParsedCommand(control)

    for delimiter in (" ", "\t"):
        prefix = f"/goal{delimiter}"
        if line.startswith(prefix):
            objective = line[len(prefix) :]
            if not objective.strip():
                raise CommandSyntaxError("/goal requires a non-blank objective")
            return ParsedCommand(CommandKind.START_GOAL, objective)

    # A bare /goal is an attempted typed command with missing data.  It must not
    # become a manual model turn whose text might later be misread as authority.
    if line == "/goal":
        raise CommandSyntaxError("/goal requires a non-blank objective")
    return ParsedCommand(CommandKind.USER_MESSAGE, line)


class InteractiveBackend(Protocol):
    """Typed Update/query surface supplied by the Temporal client host.

    Implementations must perform no automatic retry when an Update outcome is
    unknown.  ``command_id`` and ``message_id`` are stable for that one call so
    the host can reconcile explicitly.
    """

    async def start_goal(
        self, *, command_id: str, objective: str
    ) -> Mapping[str, Any]: ...

    async def user_message(
        self, *, command_id: str, message_id: str, text: str
    ) -> Mapping[str, Any]: ...

    async def pause(self, *, command_id: str) -> Mapping[str, Any]: ...

    async def resume(self, *, command_id: str) -> Mapping[str, Any]: ...

    async def clear(self, *, command_id: str) -> Mapping[str, Any]: ...

    async def status(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AssistantOutputEvent:
    """One non-durable display event emitted by the adapter.

    ``repr=False`` keeps raw assistant text out of diagnostic representations.
    The object has no serialization helper by design.
    """

    operation_id: str
    intent_epoch: int
    text: str = field(repr=False)

    def validate(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        if self.intent_epoch < 0:
            raise ValueError("intent_epoch must not be negative")
        if not isinstance(self.text, str):
            raise TypeError("assistant output must be text")


_CLOSED = object()


class EphemeralAssistantBus:
    """Bounded, same-event-loop output handoff with drop-oldest pressure.

    ``observe`` is synchronous and non-blocking so a slow or absent terminal
    cannot hold a Codex Activity open.  This class is intentionally in-memory;
    do not replace it with Temporal Signals, files, databases, or logging.
    """

    def __init__(
        self,
        *,
        max_events: int = DEFAULT_EPHEMERAL_QUEUE_SIZE,
        max_text_bytes: int = MAX_EPHEMERAL_TEXT_BYTES,
    ) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        if max_text_bytes <= 0:
            raise ValueError("max_text_bytes must be positive")
        self._queue: asyncio.Queue[AssistantOutputEvent | object] = asyncio.Queue(
            maxsize=max_events
        )
        self._max_text_bytes = max_text_bytes
        self._closed = False

    def observe(self, event: AssistantOutputEvent) -> bool:
        """Offer an event without blocking; return whether it was accepted."""

        event.validate()
        if self._closed:
            return False
        if len(event.text.encode("utf-8")) > self._max_text_bytes:
            # Do not retain a giant response or manufacture a persistent copy.
            return False
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - same-loop race guard
                pass
        self._queue.put_nowait(event)
        return True

    async def receive(self) -> AssistantOutputEvent | None:
        item = await self._queue.get()
        if item is _CLOSED:
            return None
        if not isinstance(item, AssistantOutputEvent):  # pragma: no cover
            raise TypeError("ephemeral output queue was corrupted")
        return item

    def close(self) -> None:
        """Drop all retained text and wake one receiver."""

        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_CLOSED)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    kind: CommandKind
    snapshot: Mapping[str, Any]
    should_exit: bool = False


class RenderDisposition(str, Enum):
    DISPLAYED = "displayed"
    DROPPED_STALE = "dropped_stale"
    DROPPED_SUSPENDED = "dropped_suspended"
    CLOSED = "closed"


DisplayWriter = Callable[[str], Awaitable[None] | None]


class InteractiveSession:
    """Route explicit input to typed Updates and epoch-fence output.

    The session never decides that another model turn should run.  For every
    state-changing command it blocks display first, submits exactly one backend
    call, then advances the local epoch floor from the authoritative snapshot.
    An unknown RPC result retains the exact typed payload and stable command ID
    in memory. ``/status`` may inspect state but cannot clear that fence. Only
    an explicit ``/retry`` resubmits the same ID so Temporal deduplication can
    return the authoritative result without duplicating the mutation.
    """

    def __init__(
        self,
        backend: InteractiveBackend,
        *,
        id_factory: Callable[[], str] | None = None,
        pending_command: PendingCommand | None = None,
    ) -> None:
        if pending_command is not None:
            self._validate_pending_command(pending_command)
        self._backend = backend
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._display_gate = asyncio.Lock()
        self._intent_epoch_floor = 0
        self._output_suspended = pending_command is not None
        self._pending_command = pending_command

    @property
    def intent_epoch_floor(self) -> int:
        return self._intent_epoch_floor

    @property
    def output_suspended(self) -> bool:
        return self._output_suspended

    @property
    def pending_command(self) -> PendingCommand | None:
        return self._pending_command

    async def submit(self, line: str) -> DispatchResult:
        command = parse_input(line)
        if command.kind == CommandKind.STATUS:
            async with self._display_gate:
                try:
                    snapshot = await self._backend.status()
                    self._accept_snapshot(snapshot)
                    # Querying state does not resolve whether an Update ID was
                    # accepted. Keep the mutation and output fences in place.
                    self._output_suspended = self._pending_command is not None
                except BaseException:
                    # A query that cannot establish the authoritative epoch is
                    # not permission to unmute possibly stale output.
                    self._output_suspended = True
                    raise
            return DispatchResult(command.kind, snapshot)

        if command.kind == CommandKind.RETRY:
            return await self._retry_pending()

        if self._pending_command is not None:
            raise PendingCommandError(
                "an Update outcome is unresolved; use /status or exact /retry"
            )

        if command.kind == CommandKind.QUIT:
            return await self._quit_fail_closed()

        pending = self._pending_for(command)
        async with self._display_gate:
            try:
                snapshot = await self._dispatch_pending(pending)
                self._accept_snapshot(snapshot)
                self._output_suspended = False
            except BaseException as exc:
                # Includes cancellation of the UI task.  Without an
                # authoritative response, rendering any queued output is unsafe.
                if self._is_unknown_outcome(exc):
                    self._pending_command = pending
                self._output_suspended = True
                raise
        return DispatchResult(command.kind, snapshot)

    async def _retry_pending(self) -> DispatchResult:
        pending = self._pending_command
        if pending is None:
            raise PendingCommandError("there is no unresolved Update to retry")
        async with self._display_gate:
            try:
                snapshot = await self._dispatch_pending(pending)
                self._accept_snapshot(snapshot)
            except BaseException:
                # Never replace the stable ID/payload with a fresh command. A
                # failed retry remains fenced until that exact ID returns.
                self._output_suspended = True
                raise
            self._pending_command = None
            self._output_suspended = pending.exit_after_success
        return DispatchResult(
            CommandKind.RETRY,
            snapshot,
            should_exit=pending.exit_after_success,
        )

    def _pending_for(self, command: ParsedCommand) -> PendingCommand:
        command_id = self._new_id("command")
        message_id = (
            f"{command_id}:message"
            if command.kind == CommandKind.USER_MESSAGE
            else None
        )
        return PendingCommand(
            kind=command.kind,
            command_id=command_id,
            message_id=message_id,
            text=command.text,
        )

    async def _dispatch_pending(
        self,
        pending: PendingCommand,
    ) -> Mapping[str, Any]:
        if pending.kind == CommandKind.START_GOAL:
            assert pending.text is not None
            return await self._backend.start_goal(
                command_id=pending.command_id,
                objective=pending.text,
            )
        if pending.kind == CommandKind.USER_MESSAGE:
            assert pending.text is not None and pending.message_id is not None
            # This typed Update is the first and only action for normal input.
            # The Workflow advances intent_epoch before scheduling a manual turn.
            return await self._backend.user_message(
                command_id=pending.command_id,
                message_id=pending.message_id,
                text=pending.text,
            )
        if pending.kind == CommandKind.PAUSE:
            return await self._backend.pause(command_id=pending.command_id)
        if pending.kind == CommandKind.RESUME:
            return await self._backend.resume(command_id=pending.command_id)
        if pending.kind in {CommandKind.CANCEL, CommandKind.CLEAR}:
            return await self._backend.clear(command_id=pending.command_id)
        raise AssertionError(f"unsupported pending command {pending.kind}")

    @staticmethod
    def _is_unknown_outcome(exc: BaseException) -> bool:
        return isinstance(
            exc,
            (
                UnknownCommandOutcome,
                TimeoutError,
                asyncio.CancelledError,
                BackendSnapshotError,
            ),
        )

    async def _quit_fail_closed(self) -> DispatchResult:
        """Quiesce work before allowing the interactive host to exit.

        A Goal is paused so its objective remains resumable.  With no Goal,
        ``clear`` is used to fence a possibly active manual turn.  Failure does
        not return ``should_exit=True``.
        """

        pending: PendingCommand | None = None
        async with self._display_gate:
            try:
                before = await self._backend.status()
                self._accept_snapshot(before)
                command_id = self._new_id("quit")
                if before.get("objective_present") is True:
                    pending = PendingCommand(
                        kind=CommandKind.PAUSE,
                        command_id=command_id,
                        exit_after_success=True,
                    )
                else:
                    pending = PendingCommand(
                        kind=CommandKind.CANCEL,
                        command_id=command_id,
                        exit_after_success=True,
                    )
                snapshot = await self._dispatch_pending(pending)
                self._accept_snapshot(snapshot)
                self._output_suspended = True
            except BaseException as exc:
                if pending is not None and self._is_unknown_outcome(exc):
                    self._pending_command = pending
                self._output_suspended = True
                raise
        return DispatchResult(CommandKind.QUIT, snapshot, should_exit=True)

    async def render_next(
        self,
        bus: EphemeralAssistantBus,
        writer: DisplayWriter,
    ) -> RenderDisposition:
        """Display at most one current event without retaining its text."""

        event = await bus.receive()
        if event is None:
            return RenderDisposition.CLOSED
        async with self._display_gate:
            if self._output_suspended:
                return RenderDisposition.DROPPED_SUSPENDED
            if event.intent_epoch < self._intent_epoch_floor:
                return RenderDisposition.DROPPED_STALE
            written = writer(event.text)
            if inspect.isawaitable(written):
                await written
        return RenderDisposition.DISPLAYED

    def _accept_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        if not isinstance(snapshot, Mapping):
            raise BackendSnapshotError("backend snapshot must be a mapping")
        epoch = snapshot.get("intent_epoch")
        if type(epoch) is not int or epoch < 0:
            raise BackendSnapshotError("snapshot has no valid intent_epoch")
        if epoch < self._intent_epoch_floor:
            raise BackendSnapshotError("backend snapshot moved intent_epoch backwards")
        self._intent_epoch_floor = epoch

    def _new_id(self, label: str) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{label} id factory returned an invalid value")
        return value

    @staticmethod
    def _validate_pending_command(pending: PendingCommand) -> None:
        if pending.kind not in {
            CommandKind.START_GOAL,
            CommandKind.USER_MESSAGE,
            CommandKind.PAUSE,
            CommandKind.RESUME,
            CommandKind.CANCEL,
            CommandKind.CLEAR,
        }:
            raise ValueError("recovered pending command has an invalid kind")
        if not pending.command_id.strip():
            raise ValueError("recovered pending command has no command ID")
        if pending.kind in {CommandKind.START_GOAL, CommandKind.USER_MESSAGE}:
            if pending.text is None or not pending.text.strip():
                raise ValueError("recovered pending command lost its text")
        elif pending.text is not None:
            raise ValueError("recovered control command unexpectedly contains text")
        if pending.kind == CommandKind.USER_MESSAGE:
            if pending.message_id is None or not pending.message_id.strip():
                raise ValueError("recovered user message lost its message ID")
        elif pending.message_id is not None:
            raise ValueError("recovered command unexpectedly contains a message ID")
