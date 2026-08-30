from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping
from typing import Any

from codex_orchestrator.interactive_frontend import (
    AssistantOutputEvent,
    BackendSnapshotError,
    CommandKind,
    CommandSyntaxError,
    EphemeralAssistantBus,
    InteractiveSession,
    PendingCommandError,
    RenderDisposition,
    parse_input,
)


class FakeBackend:
    def __init__(self) -> None:
        self.epoch = 0
        self.objective_present = False
        self.calls: list[tuple[Any, ...]] = []
        self.failure: BaseException | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent_epoch": self.epoch,
            "objective_present": self.objective_present,
            "status": "active" if self.objective_present else "idle",
        }

    async def _mutate(self, call: tuple[Any, ...]) -> Mapping[str, Any]:
        self.calls.append(call)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.epoch += 1
        return self.snapshot()

    async def start_goal(
        self, *, command_id: str, objective: str
    ) -> Mapping[str, Any]:
        self.objective_present = True
        return await self._mutate(("start_goal", command_id, objective))

    async def user_message(
        self, *, command_id: str, message_id: str, text: str
    ) -> Mapping[str, Any]:
        return await self._mutate(("user_message", command_id, message_id, text))

    async def pause(self, *, command_id: str) -> Mapping[str, Any]:
        return await self._mutate(("pause", command_id))

    async def resume(self, *, command_id: str) -> Mapping[str, Any]:
        return await self._mutate(("resume", command_id))

    async def clear(self, *, command_id: str) -> Mapping[str, Any]:
        self.objective_present = False
        return await self._mutate(("clear", command_id))

    async def status(self) -> Mapping[str, Any]:
        self.calls.append(("status",))
        if self.failure is not None:
            raise self.failure
        return self.snapshot()


class ParserTests(unittest.TestCase):
    def test_only_typed_goal_creates_goal(self) -> None:
        parsed = parse_input("/goal exact objective")
        self.assertEqual(parsed.kind, CommandKind.START_GOAL)
        self.assertEqual(parsed.text, "exact objective")

        for ordinary in (
            "quoted /goal old objective",
            "> /goal old objective",
            "/goalx objective",
            "/pause please",
            " /goal leading-space-is-message",
        ):
            with self.subTest(ordinary=ordinary):
                parsed = parse_input(ordinary)
                self.assertEqual(parsed.kind, CommandKind.USER_MESSAGE)
                self.assertEqual(parsed.text, ordinary)

    def test_goal_payload_preserves_bytes_after_one_delimiter(self) -> None:
        parsed = parse_input("/goal  keep this leading space")
        self.assertEqual(parsed.text, " keep this leading space")

    def test_bare_or_blank_goal_is_rejected(self) -> None:
        for value in ("/goal", "/goal ", "/goal\t\t"):
            with self.subTest(value=value):
                with self.assertRaises(CommandSyntaxError):
                    parse_input(value)

    def test_controls_require_exact_match(self) -> None:
        expected = {
            "/pause": CommandKind.PAUSE,
            "/resume": CommandKind.RESUME,
            "/cancel": CommandKind.CANCEL,
            "/clear": CommandKind.CLEAR,
            "/status": CommandKind.STATUS,
            "/retry": CommandKind.RETRY,
            "/quit": CommandKind.QUIT,
        }
        for text, kind in expected.items():
            with self.subTest(text=text):
                self.assertEqual(parse_input(text).kind, kind)
        self.assertEqual(parse_input("/pause ").kind, CommandKind.USER_MESSAGE)
        self.assertEqual(parse_input("/cancel now").kind, CommandKind.USER_MESSAGE)
        self.assertEqual(parse_input("/retry later").kind, CommandKind.USER_MESSAGE)


class InteractiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.backend = FakeBackend()
        ids = iter((f"id-{index}" for index in range(20)))
        self.session = InteractiveSession(self.backend, id_factory=lambda: next(ids))

    async def test_normal_input_first_calls_user_message(self) -> None:
        result = await self.session.submit("do not replace my Goal")
        self.assertEqual(result.kind, CommandKind.USER_MESSAGE)
        self.assertEqual(
            self.backend.calls,
            [
                (
                    "user_message",
                    "id-0",
                    "id-0:message",
                    "do not replace my Goal",
                )
            ],
        )

    async def test_goal_is_only_start_goal_route(self) -> None:
        await self.session.submit("/goal pinned objective")
        await self.session.submit("quoted /goal replacement")
        self.assertEqual(self.backend.calls[0], ("start_goal", "id-0", "pinned objective"))
        self.assertEqual(self.backend.calls[1][0], "user_message")

    async def test_user_preemption_blocks_old_output_until_epoch_advances(self) -> None:
        self.backend.release = asyncio.Event()
        submit = asyncio.create_task(self.session.submit("new user intent"))
        await self.backend.entered.wait()

        bus = EphemeralAssistantBus()
        self.assertTrue(bus.observe(AssistantOutputEvent("old-op", 0, "stale")))
        displayed: list[str] = []
        render = asyncio.create_task(self.session.render_next(bus, displayed.append))
        await asyncio.sleep(0)
        self.assertFalse(render.done(), "display must wait behind the preemption Update")

        self.backend.release.set()
        await submit
        self.assertEqual(await render, RenderDisposition.DROPPED_STALE)
        self.assertEqual(displayed, [])

    async def test_unknown_outcome_requires_explicit_same_id_retry(self) -> None:
        self.backend.failure = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            await self.session.submit("preempt")
        self.assertTrue(self.session.output_suspended)
        pending = self.session.pending_command
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual("id-0", pending.command_id)
        self.assertEqual("id-0:message", pending.message_id)
        self.assertNotIn("preempt", repr(pending))

        bus = EphemeralAssistantBus()
        bus.observe(AssistantOutputEvent("maybe-old", 0, "must not show"))
        displayed: list[str] = []
        self.assertEqual(
            await self.session.render_next(bus, displayed.append),
            RenderDisposition.DROPPED_SUSPENDED,
        )
        self.assertEqual(displayed, [])

        self.backend.failure = None
        await self.session.submit("/status")
        self.assertTrue(self.session.output_suspended)
        self.assertIs(pending, self.session.pending_command)

        calls_before_retype = list(self.backend.calls)
        with self.assertRaises(PendingCommandError):
            await self.session.submit("preempt")
        self.assertEqual(calls_before_retype, self.backend.calls)

        result = await self.session.submit("/retry")
        self.assertEqual(CommandKind.RETRY, result.kind)
        self.assertFalse(self.session.output_suspended)
        self.assertIsNone(self.session.pending_command)
        self.assertEqual(
            self.backend.calls[-1],
            ("user_message", "id-0", "id-0:message", "preempt"),
        )

    async def test_retry_without_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(PendingCommandError):
            await self.session.submit("/retry")
        self.assertEqual([], self.backend.calls)

    async def test_backward_epoch_snapshot_fails_closed(self) -> None:
        await self.session.submit("first")
        self.backend.epoch = 0
        with self.assertRaises(BackendSnapshotError):
            await self.session.submit("/status")
        self.assertTrue(self.session.output_suspended)

    async def test_quit_pauses_goal_and_only_then_allows_exit(self) -> None:
        await self.session.submit("/goal keep me")
        result = await self.session.submit("/quit")
        self.assertTrue(result.should_exit)
        self.assertEqual(self.backend.calls[-2], ("status",))
        self.assertEqual(self.backend.calls[-1], ("pause", "id-1"))
        self.assertTrue(self.session.output_suspended)

    async def test_quit_clears_when_no_goal_to_fence_manual_work(self) -> None:
        result = await self.session.submit("/quit")
        self.assertTrue(result.should_exit)
        self.assertEqual(self.backend.calls, [("status",), ("clear", "id-0")])

    async def test_cancel_and_clear_both_use_the_typed_clear_update(self) -> None:
        cancel = await self.session.submit("/cancel")
        clear = await self.session.submit("/clear")
        self.assertEqual(cancel.kind, CommandKind.CANCEL)
        self.assertEqual(clear.kind, CommandKind.CLEAR)
        self.assertEqual(
            self.backend.calls,
            [("clear", "id-0"), ("clear", "id-1")],
        )

    async def test_quit_failure_does_not_authorize_exit(self) -> None:
        self.backend.failure = RuntimeError("no acknowledgement")
        with self.assertRaises(RuntimeError):
            await self.session.submit("/quit")
        self.assertTrue(self.session.output_suspended)


class EphemeralBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_bounded_and_drops_oldest(self) -> None:
        bus = EphemeralAssistantBus(max_events=1)
        bus.observe(AssistantOutputEvent("old", 0, "old text"))
        bus.observe(AssistantOutputEvent("new", 0, "new text"))
        event = await bus.receive()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.operation_id, "new")
        self.assertEqual(event.text, "new text")

    async def test_oversized_text_is_not_retained(self) -> None:
        bus = EphemeralAssistantBus(max_text_bytes=3)
        self.assertFalse(bus.observe(AssistantOutputEvent("op", 0, "four")))
        bus.close()
        self.assertIsNone(await bus.receive())

    async def test_close_drops_pending_text_and_wakes_receiver(self) -> None:
        bus = EphemeralAssistantBus()
        bus.observe(AssistantOutputEvent("op", 0, "secret-like output"))
        bus.close()
        self.assertIsNone(await bus.receive())

    async def test_event_repr_omits_assistant_text(self) -> None:
        event = AssistantOutputEvent("op", 0, "do not leak me")
        self.assertNotIn("do not leak me", repr(event))


if __name__ == "__main__":
    unittest.main()
