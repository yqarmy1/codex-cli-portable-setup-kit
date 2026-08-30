from __future__ import annotations

import asyncio
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openai_codex.errors import InvalidParamsError, JsonRpcError, TransportClosedError
from openai_codex.generated.v2_all import ThreadSource

from codex_orchestrator.activities import CodexActivities
from codex_orchestrator.codex_adapter import (
    AdapterInvariantError,
    CodexAdapter,
    EXPECTED_SDK_VERSION,
)
from codex_orchestrator.contracts import CodexOperationInput
from codex_orchestrator.domain import (
    Operation,
    OperationKind,
    ResultDisposition,
)


def operation(
    operation_id: str,
    kind: OperationKind,
    *,
    thread_id: str | None = None,
    intent_epoch: int = 1,
    reconcile_of: str | None = None,
    message_text: str | None = None,
) -> Operation:
    return Operation(
        operation_id=operation_id,
        kind=kind,
        intent_epoch=intent_epoch,
        action_seq=1,
        objective_sha256="objective-sha" if kind == OperationKind.AUTOMATIC_TURN else None,
        thread_id=thread_id,
        reconcile_of=reconcile_of,
        message_text=message_text,
    )


def request_for(
    current: Operation,
    *,
    native_thread_id: str | None = None,
    ambiguous_operation: Operation | None = None,
    project_root: str = r"C:\project-a",
) -> CodexOperationInput:
    return CodexOperationInput(
        operation=current,
        project_root=project_root,
        objective="exact objective",
        objective_sha256="objective-sha",
        lifetime_tokens_used=0,
        lifetime_automatic_turns=0,
        lifetime_rollovers=0,
        native_thread_id=native_thread_id,
        ambiguous_operation=ambiguous_operation,
    )


def client_with_cleared_goal() -> SimpleNamespace:
    return SimpleNamespace(
        thread_goal_clear=AsyncMock(return_value=SimpleNamespace(cleared=True)),
        request=AsyncMock(return_value=SimpleNamespace(goal=None)),
        thread_set_name=AsyncMock(),
        thread_read=AsyncMock(),
        turn_interrupt=AsyncMock(),
    )


class RuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_canonical_project_root_gets_its_own_app_server(self) -> None:
        instances: list[SimpleNamespace] = []

        def make_runtime(config: object) -> SimpleNamespace:
            runtime = SimpleNamespace(
                config=config,
                metadata=SimpleNamespace(
                    user_agent=f"codex-cli/{EXPECTED_SDK_VERSION}"
                ),
                _ensure_initialized=AsyncMock(),
                close=AsyncMock(),
            )
            instances.append(runtime)
            return runtime

        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with (
                patch(
                    "codex_orchestrator.codex_adapter.AsyncCodex",
                    side_effect=make_runtime,
                ),
                patch.object(
                    CodexAdapter,
                    "_verified_codex_binary",
                    return_value=r"C:\verified\codex.exe",
                ),
            ):
                first_runtime = await adapter._runtime(first)
                first_again = await adapter._runtime(first)
                second_runtime = await adapter._runtime(second)

        self.assertIs(first_runtime, first_again)
        self.assertIsNot(first_runtime, second_runtime)
        self.assertEqual(2, len(instances))
        self.assertEqual(str(Path(first).resolve()), instances[0].config.cwd)
        self.assertEqual(str(Path(second).resolve()), instances[1].config.cwd)
        await adapter.close()
        instances[0].close.assert_awaited_once()
        instances[1].close.assert_awaited_once()

    @unittest.skipUnless(
        sys.platform == "win32"
        and platform.machine().casefold() in {"amd64", "x86_64", "x64"},
        "the checked digest is for the official win_amd64 wheel",
    )
    def test_official_bundled_binary_passes_digest_gate(self) -> None:
        binary = Path(CodexAdapter._verified_codex_binary())
        self.assertTrue(binary.is_file())
        self.assertEqual("codex.exe", binary.name.casefold())


class ThreadAndGoalContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_thread_uses_closed_thread_source_and_exact_name_tag(self) -> None:
        current = operation("codex-op:project:1", OperationKind.AUTOMATIC_TURN)
        request = request_for(current)
        client = client_with_cleared_goal()
        codex = SimpleNamespace(
            _client=client,
            thread_start=AsyncMock(return_value=SimpleNamespace(id="thread-new")),
        )

        thread_id = await CodexAdapter._start_tagged_thread(codex, request)

        self.assertEqual("thread-new", thread_id)
        self.assertIs(
            ThreadSource.user,
            codex.thread_start.await_args.kwargs["thread_source"],
        )
        client.thread_set_name.assert_awaited_once_with(
            "thread-new",
            CodexAdapter._operation_thread_tag(current.operation_id),
        )

    async def test_native_goal_is_cleared_and_read_back_as_absent(self) -> None:
        events: list[str] = []

        async def clear(_thread_id: str) -> object:
            events.append("clear")
            return SimpleNamespace(cleared=True)

        async def read(method: str, params: object, *, response_model: object) -> object:
            self.assertEqual("thread/goal/get", method)
            self.assertEqual({"threadId": "thread-1"}, params)
            events.append("get")
            return SimpleNamespace(goal=None)

        codex = SimpleNamespace(
            _client=SimpleNamespace(thread_goal_clear=clear, request=read)
        )
        await CodexAdapter._clear_native_goal(codex, "thread-1")
        self.assertEqual(["clear", "get"], events)

    async def test_native_goal_clear_fails_closed_if_goal_remains(self) -> None:
        client = client_with_cleared_goal()
        client.request.return_value = SimpleNamespace(
            goal=SimpleNamespace(status="active")
        )
        codex = SimpleNamespace(_client=client)
        with self.assertRaises(AdapterInvariantError):
            await CodexAdapter._clear_native_goal(codex, "thread-1")

    async def test_existing_thread_goal_is_cleared_before_resume(self) -> None:
        events: list[str] = []

        async def clear(_thread_id: str) -> object:
            events.append("clear")
            return SimpleNamespace(cleared=True)

        async def read(_method: str, _params: object, *, response_model: object) -> object:
            events.append("get")
            return SimpleNamespace(goal=None)

        async def resume(_thread_id: str, **_kwargs: object) -> object:
            events.append("resume")
            return SimpleNamespace(id="thread-1")

        current = operation(
            "codex-op:turn:resume",
            OperationKind.AUTOMATIC_TURN,
            thread_id="thread-1",
        )
        codex = SimpleNamespace(
            _client=SimpleNamespace(thread_goal_clear=clear, request=read),
            thread_resume=resume,
        )

        thread_id = await CodexAdapter()._ensure_thread(
            codex,
            request_for(current, native_thread_id="thread-1"),
        )

        self.assertEqual("thread-1", thread_id)
        self.assertEqual(["clear", "get", "resume"], events)

    async def test_cancellation_clears_goal_before_interrupt(self) -> None:
        events: list[str] = []
        turn_finished = asyncio.Event()

        async def clear(_thread_id: str) -> object:
            events.append("clear")
            return SimpleNamespace(cleared=True)

        async def read(_method: str, _params: object, *, response_model: object) -> object:
            events.append("get")
            return SimpleNamespace(goal=None)

        async def wait_for_turn() -> None:
            await turn_finished.wait()

        async def interrupt() -> object:
            events.append("interrupt")
            turn_finished.set()
            return SimpleNamespace()

        codex = SimpleNamespace(
            _client=SimpleNamespace(thread_goal_clear=clear, request=read)
        )
        handle = SimpleNamespace(interrupt=interrupt)
        task = asyncio.create_task(wait_for_turn())

        await CodexAdapter()._clear_interrupt_and_settle(
            codex,
            "thread-1",
            handle,
            task,
        )

        self.assertEqual(["clear", "get", "interrupt"], events)
        self.assertTrue(task.done())


class ReconciliationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollover_reconciles_tagged_successor_not_old_source_thread(self) -> None:
        target = operation(
            "codex-op:rollover:1",
            OperationKind.ROLLOVER,
            thread_id="thread-old",
        )
        current = operation(
            "codex-op:reconcile:1",
            OperationKind.RECONCILE,
            thread_id="thread-old",
            intent_epoch=2,
            reconcile_of=target.operation_id,
        )
        client = client_with_cleared_goal()
        tag = CodexAdapter._operation_thread_tag(target.operation_id)
        codex = SimpleNamespace(
            _client=client,
            thread_list=AsyncMock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id="thread-successor", name=tag)],
                    next_cursor=None,
                )
            ),
        )

        result = await CodexAdapter()._reconcile(
            codex,
            request_for(
                current,
                native_thread_id="thread-old",
                ambiguous_operation=target,
            ),
            heartbeat=lambda _details: None,
        )

        self.assertEqual(ResultDisposition.CONTINUE, result.disposition)
        self.assertEqual("thread-successor", result.thread_id)
        client.thread_read.assert_not_awaited()
        client.thread_goal_clear.assert_awaited_once_with("thread-successor")

    async def test_missing_rollover_tag_never_accepts_old_thread_as_successor(self) -> None:
        target = operation(
            "codex-op:rollover:missing",
            OperationKind.ROLLOVER,
            thread_id="thread-old",
        )
        current = operation(
            "codex-op:reconcile:missing",
            OperationKind.RECONCILE,
            thread_id="thread-old",
            intent_epoch=2,
            reconcile_of=target.operation_id,
        )
        client = client_with_cleared_goal()
        codex = SimpleNamespace(
            _client=client,
            thread_list=AsyncMock(
                return_value=SimpleNamespace(data=[], next_cursor=None)
            ),
        )

        with patch("codex_orchestrator.codex_adapter.MAX_RECONCILE_TAG_POLLS", 1):
            result = await CodexAdapter()._reconcile(
                codex,
                request_for(
                    current,
                    native_thread_id="thread-old",
                    ambiguous_operation=target,
                ),
                heartbeat=lambda _details: None,
            )

        self.assertEqual(ResultDisposition.AMBIGUOUS, result.disposition)
        self.assertIsNone(result.thread_id)
        self.assertEqual("rollover_successor_not_found", result.detail)
        client.thread_read.assert_not_awaited()
        client.thread_goal_clear.assert_not_awaited()

    async def test_interrupt_ack_is_followed_until_terminal_turn_state(self) -> None:
        target = operation(
            "codex-op:turn:1",
            OperationKind.AUTOMATIC_TURN,
            thread_id="thread-1",
        )
        current = operation(
            "codex-op:reconcile:turn:1",
            OperationKind.RECONCILE,
            thread_id="thread-1",
            intent_epoch=2,
            reconcile_of=target.operation_id,
        )

        def tagged_turn(status: str) -> object:
            return SimpleNamespace(
                id="turn-1",
                status=SimpleNamespace(value=status),
                items=[
                    SimpleNamespace(
                        root=SimpleNamespace(client_id=target.operation_id)
                    )
                ],
            )

        client = client_with_cleared_goal()
        client.thread_read.side_effect = [
            SimpleNamespace(thread=SimpleNamespace(turns=[tagged_turn("inProgress")])),
            SimpleNamespace(thread=SimpleNamespace(turns=[tagged_turn("interrupted")])),
        ]
        codex = SimpleNamespace(_client=client)

        with patch("codex_orchestrator.codex_adapter.RECONCILE_POLL_SECONDS", 0):
            result = await CodexAdapter()._reconcile(
                codex,
                request_for(
                    current,
                    native_thread_id="thread-1",
                    ambiguous_operation=target,
                ),
                heartbeat=lambda _details: None,
            )

        self.assertEqual(ResultDisposition.INTERRUPTED, result.disposition)
        self.assertEqual("tagged_turn_interrupted_reconciled", result.detail)
        self.assertEqual(2, client.thread_read.await_count)
        client.turn_interrupt.assert_awaited_once_with("thread-1", "turn-1")

    async def test_nonterminal_turn_after_interrupt_fails_reconciliation_closed(self) -> None:
        target = operation(
            "codex-op:turn:stuck",
            OperationKind.AUTOMATIC_TURN,
            thread_id="thread-1",
        )
        current = operation(
            "codex-op:reconcile:stuck",
            OperationKind.RECONCILE,
            thread_id="thread-1",
            intent_epoch=2,
            reconcile_of=target.operation_id,
        )
        active_turn = SimpleNamespace(
            id="turn-stuck",
            status=SimpleNamespace(value="inProgress"),
            items=[
                SimpleNamespace(root=SimpleNamespace(client_id=target.operation_id))
            ],
        )
        client = client_with_cleared_goal()
        client.thread_read.return_value = SimpleNamespace(
            thread=SimpleNamespace(turns=[active_turn])
        )
        codex = SimpleNamespace(_client=client)

        with (
            patch("codex_orchestrator.codex_adapter.MAX_RECONCILE_TURN_POLLS", 2),
            patch("codex_orchestrator.codex_adapter.RECONCILE_POLL_SECONDS", 0),
        ):
            result = await CodexAdapter()._reconcile(
                codex,
                request_for(
                    current,
                    native_thread_id="thread-1",
                    ambiguous_operation=target,
                ),
                heartbeat=lambda _details: None,
            )

        self.assertEqual(ResultDisposition.AMBIGUOUS, result.disposition)
        self.assertEqual("active_turn_not_terminal_after_interrupt", result.detail)
        client.turn_interrupt.assert_awaited_once_with("thread-1", "turn-stuck")


class DispositionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operation = operation("codex-op:auto:1", OperationKind.AUTOMATIC_TURN)

    def disposition(self, payload: object) -> tuple[ResultDisposition, str]:
        return CodexAdapter._disposition(self.operation, json.dumps(payload))

    def test_continue_requires_nonempty_detail_and_concrete_evidence(self) -> None:
        missing_evidence = self.disposition(
            {"disposition": "continue", "detail": "worked", "evidence": []}
        )
        missing_detail = self.disposition(
            {"disposition": "continue", "detail": "  ", "evidence": ["test passed"]}
        )
        valid = self.disposition(
            {
                "disposition": "continue",
                "detail": "implemented bounded slice",
                "evidence": ["unit test X passed"],
            }
        )

        self.assertEqual(ResultDisposition.FAILED, missing_evidence[0])
        self.assertEqual("continuation_evidence_missing", missing_evidence[1])
        self.assertEqual(ResultDisposition.FAILED, missing_detail[0])
        self.assertEqual("structured_detail_missing", missing_detail[1])
        self.assertEqual(
            (ResultDisposition.CONTINUE, "automatic_slice_completed"),
            valid,
        )

    def test_complete_rejects_blank_evidence_without_persisting_raw_text(self) -> None:
        result = self.disposition(
            {
                "disposition": "complete",
                "detail": "claimed complete",
                "evidence": ["  "],
            }
        )
        self.assertEqual(
            (ResultDisposition.FAILED, "completion_evidence_missing"),
            result,
        )


class EphemeralObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_manual_turn_emits_exact_text_and_observer_failure_is_ignored(
        self,
    ) -> None:
        raw_response = "Exact manual assistant response; do not persist."
        current = operation(
            "codex-op:observer:1",
            OperationKind.MANUAL_TURN,
            intent_epoch=7,
            message_text="manual question",
        )
        request = request_for(current)
        seen: list[object] = []

        def broken_terminal_observer(event: object) -> bool:
            seen.append(event)
            raise RuntimeError("ephemeral terminal is gone")

        adapter = CodexAdapter(assistant_observer=broken_terminal_observer)
        codex = SimpleNamespace(
            _client=SimpleNamespace(
                turn_start=AsyncMock(
                    return_value=SimpleNamespace(turn=SimpleNamespace(id="turn-1"))
                )
            )
        )

        class FakeTurnHandle:
            def __init__(self, *_args: object) -> None:
                self.id = "turn-1"

            async def run(self) -> object:
                return SimpleNamespace(final_response=raw_response, usage=None)

        heartbeats: list[dict[str, object]] = []
        with (
            patch(
                "codex_orchestrator.codex_adapter.AsyncTurnHandle",
                FakeTurnHandle,
            ),
            patch.object(
                adapter,
                "_ensure_thread",
                AsyncMock(return_value="thread-1"),
            ),
            patch.object(
                adapter,
                "_clear_native_goal",
                AsyncMock(),
            ),
        ):
            result = await adapter._run_turn(
                codex,
                request,
                heartbeat=heartbeats.append,
            )

        self.assertEqual(ResultDisposition.CONTINUE, result.disposition)
        self.assertEqual("manual_turn_completed", result.detail)
        self.assertEqual(1, len(seen))
        event = seen[0]
        self.assertEqual(current.operation_id, event.operation_id)
        self.assertEqual(7, event.intent_epoch)
        self.assertEqual(raw_response, event.text)
        self.assertNotIn(raw_response, repr(result))
        self.assertNotIn(raw_response, json.dumps(heartbeats))

    async def test_empty_final_response_is_not_observed(self) -> None:
        seen: list[object] = []
        adapter = CodexAdapter(assistant_observer=lambda event: not seen.append(event))
        current = operation("codex-op:observer:empty", OperationKind.AUTOMATIC_TURN)

        adapter._observe_final_response(
            current, "", disposition=ResultDisposition.CONTINUE
        )
        adapter._observe_final_response(
            current, None, disposition=ResultDisposition.FAILED
        )

        self.assertEqual([], seen)

    async def test_automatic_continue_and_failed_json_are_silent(self) -> None:
        seen: list[object] = []
        adapter = CodexAdapter(assistant_observer=lambda event: not seen.append(event))
        current = operation("codex-op:observer:auto", OperationKind.AUTOMATIC_TURN)
        raw = json.dumps(
            {
                "disposition": "continue",
                "detail": "internal status that must not wash the console",
                "evidence": ["test passed"],
            }
        )

        adapter._observe_final_response(
            current,
            raw,
            disposition=ResultDisposition.CONTINUE,
        )
        adapter._observe_final_response(
            current,
            "malformed raw model output",
            disposition=ResultDisposition.FAILED,
        )

        self.assertEqual([], seen)

    async def test_automatic_terminal_summary_is_reconstructed_and_bounded(self) -> None:
        seen: list[object] = []
        adapter = CodexAdapter(assistant_observer=lambda event: not seen.append(event))
        current = operation("codex-op:observer:complete", OperationKind.AUTOMATIC_TURN)
        raw = json.dumps(
            {
                "disposition": "complete",
                "detail": "D" * 1_500,
                "evidence": ["E" * 700] + [f"item-{index}" for index in range(20)],
                "private_extra": "must not be displayed",
            }
        )

        adapter._observe_final_response(
            current,
            raw,
            disposition=ResultDisposition.COMPLETE,
        )

        self.assertEqual(1, len(seen))
        display = seen[0].text
        self.assertTrue(display.startswith("[Goal completed]\n"))
        self.assertNotIn("private_extra", display)
        self.assertNotIn(raw, display)
        lines = display.splitlines()
        self.assertEqual(1_000, len(lines[1]))
        self.assertEqual(500, len(lines[3][2:]))
        self.assertEqual(10, len(lines[3:]))

    async def test_needs_user_displays_only_validated_bounded_fields(self) -> None:
        seen: list[object] = []
        adapter = CodexAdapter(assistant_observer=lambda event: not seen.append(event))
        current = operation(
            "codex-op:observer:needs-user",
            OperationKind.AUTOMATIC_TURN,
        )
        raw = json.dumps(
            {
                "disposition": "needs_user",
                "detail": "Choose the target device.",
                "evidence": ["No authorized device ID is available."],
            }
        )
        disposition, _detail = adapter._disposition(current, raw)

        adapter._observe_final_response(
            current,
            raw,
            disposition=disposition,
        )

        self.assertEqual(ResultDisposition.NEEDS_USER, disposition)
        self.assertEqual(1, len(seen))
        self.assertEqual(
            "[User input required]\nChoose the target device.\nEvidence:\n"
            "- No authorized device ID is available.",
            seen[0].text,
        )


class ActivityErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_error(self, error: BaseException):
        current = operation("codex-op:error:1", OperationKind.AUTOMATIC_TURN)
        adapter = SimpleNamespace(
            run=AsyncMock(side_effect=error),
            close=AsyncMock(),
        )
        activities = CodexActivities(adapter=adapter)
        return await activities.run_codex_operation(request_for(current))

    async def test_explicit_invalid_params_is_failed_not_ambiguous(self) -> None:
        result = await self._run_with_error(InvalidParamsError(-32602, "invalid"))
        self.assertEqual(ResultDisposition.FAILED, result.disposition)
        self.assertEqual(
            "adapter_protocol_rejected:InvalidParamsError",
            result.detail,
        )
        self.assertFalse(result.safe_to_retry)

    async def test_adapter_invariant_is_fatal_not_retryable(self) -> None:
        result = await self._run_with_error(AdapterInvariantError("mismatch"))
        self.assertEqual(ResultDisposition.FAILED, result.disposition)
        self.assertEqual("adapter_invariant:AdapterInvariantError", result.detail)
        self.assertFalse(result.safe_to_retry)

    async def test_explicit_ingress_overload_is_failed_not_unknown_outcome(self) -> None:
        result = await self._run_with_error(
            JsonRpcError(-32001, "Server overloaded; retry later.")
        )
        self.assertEqual(ResultDisposition.FAILED, result.disposition)
        self.assertEqual("adapter_overloaded", result.detail)
        self.assertTrue(result.safe_to_retry)

    async def test_transport_loss_still_propagates_for_reconciliation(self) -> None:
        with self.assertRaises(TransportClosedError):
            await self._run_with_error(TransportClosedError("closed"))


if __name__ == "__main__":
    unittest.main()
