from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_orchestrator.command_outbox import EncryptedCommandOutbox
from codex_orchestrator.domain import Budget
from codex_orchestrator.interactive_frontend import (
    CommandKind,
    InteractiveSession,
    PendingCommandError,
    UnknownCommandOutcome,
)
from codex_orchestrator.interactive_temporal import TemporalInteractiveBackend
from codex_orchestrator.local_runtime import RuntimePaths, task_queue_for_project
from codex_orchestrator.temporal_workflow import CodexSupervisorWorkflow


class TemporalInteractiveBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        workspace = Path(self.temporary.name)
        project = workspace / "project"
        project.mkdir()
        self.paths = RuntimePaths.from_project_root(
            project,
            workspace_root=workspace,
        )
        self.handle = object()
        self.client = SimpleNamespace(
            get_workflow_handle=Mock(return_value=self.handle)
        )
        self.outbox = EncryptedCommandOutbox(
            paths=self.paths,
            key_id="test-key-v1",
            key=bytes(range(32)),
        )
        self.backend = TemporalInteractiveBackend(
            self.client,
            paths=self.paths,
            outbox=self.outbox,
            task_queue=task_queue_for_project(project),
            goal_budget=Budget(max_automatic_turns=3),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def helper_patches(
        self,
        *,
        state: dict[str, object] | None = None,
        result: dict[str, object] | None = None,
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock, object]:
        ensure = AsyncMock(return_value=self.handle)
        query = AsyncMock(
            return_value=state
            or {"last_command_seq": 4, "intent_epoch": 8}
        )
        execute = AsyncMock(
            return_value=result
            or {"last_command_seq": 5, "intent_epoch": 9}
        )
        stack = (
            patch(
                "codex_orchestrator.interactive_temporal.command_lock",
                return_value=nullcontext(),
            ),
            patch(
                "codex_orchestrator.interactive_temporal._ensure_workflow",
                ensure,
            ),
            patch(
                "codex_orchestrator.interactive_temporal._query_state",
                query,
            ),
            patch(
                "codex_orchestrator.interactive_temporal._execute_update",
                execute,
            ),
        )
        return ensure, query, execute, stack

    async def test_start_goal_reuses_cli_helpers_and_preserves_objective(self) -> None:
        ensure, query, execute, patches = self.helper_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            snapshot = await self.backend.start_goal(
                command_id="command-1",
                objective=" exact objective bytes ",
            )

        self.assertEqual(9, snapshot["intent_epoch"])
        ensure.assert_awaited_once()
        query.assert_awaited_once()
        execute.assert_awaited_once()
        _handle, args, method, payload = execute.await_args.args
        self.assertEqual("start-goal", args.command)
        self.assertEqual("command-1", args.command_id)
        self.assertIs(method, CodexSupervisorWorkflow.start_goal)
        self.assertEqual(5, payload.command_seq)
        self.assertEqual(" exact objective bytes ", payload.objective)
        self.assertEqual(3, payload.budget.max_automatic_turns)

    async def test_user_message_maps_once_to_typed_preemption_update(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            await self.backend.user_message(
                command_id="command-2",
                message_id="message-2",
                text="ordinary /goal quotation",
            )

        execute.assert_awaited_once()
        _handle, args, method, payload = execute.await_args.args
        self.assertEqual("message", args.command)
        self.assertIs(method, CodexSupervisorWorkflow.user_message)
        self.assertEqual(5, payload.command_seq)
        self.assertEqual("message-2", payload.message_id)
        self.assertEqual("ordinary /goal quotation", payload.text)

    async def test_cancel_and_clear_share_canonical_clear_update(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            await self.backend.clear(command_id="command-3")

        _handle, args, method, payload = execute.await_args.args
        self.assertEqual("cancel", args.command)
        self.assertIs(method, CodexSupervisorWorkflow.clear_goal)
        self.assertEqual(5, payload.command_seq)

    async def test_status_queries_existing_workflow_without_creating_it(self) -> None:
        _ensure, query, _execute, patches = self.helper_patches(
            state={"last_command_seq": 9, "intent_epoch": 12}
        )
        with patches[0], patches[1], patches[2], patches[3]:
            snapshot = await self.backend.status()

        self.client.get_workflow_handle.assert_called_once()
        _ensure.assert_not_awaited()
        query.assert_awaited_once_with(self.handle, query.await_args.args[1])
        self.assertEqual(12, snapshot["intent_epoch"])

    async def test_invalid_sequence_fails_before_update(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches(
            state={"last_command_seq": "bad", "intent_epoch": 1}
        )
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(TypeError):
                await self.backend.pause(command_id="command-4")

        execute.assert_not_awaited()

    async def test_unknown_update_outcome_is_never_retried(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        execute.side_effect = TimeoutError("outcome unknown")
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(UnknownCommandOutcome):
                await self.backend.resume(command_id="stable-command-5")

        execute.assert_awaited_once()

    async def test_explicit_retry_reuses_exact_id_sequence_and_payload(self) -> None:
        ensure, query, execute, patches = self.helper_patches()
        execute.side_effect = [
            TimeoutError("outcome unknown"),
            {"last_command_seq": 5, "intent_epoch": 9},
        ]
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(UnknownCommandOutcome):
                await self.backend.user_message(
                    command_id="stable-command-6",
                    message_id="stable-command-6:message",
                    text="exact pending text",
                )
            snapshot = await self.backend.user_message(
                command_id="stable-command-6",
                message_id="stable-command-6:message",
                text="exact pending text",
            )

        self.assertEqual(9, snapshot["intent_epoch"])
        ensure.assert_awaited_once()
        query.assert_awaited_once()
        self.assertEqual(2, execute.await_count)
        first = execute.await_args_list[0].args
        second = execute.await_args_list[1].args
        self.assertEqual("stable-command-6", first[1].command_id)
        self.assertEqual("stable-command-6", second[1].command_id)
        self.assertIs(first[3], second[3])
        self.assertEqual(5, first[3].command_seq)

    async def test_retry_id_cannot_be_rebound_to_different_text(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        execute.side_effect = TimeoutError("outcome unknown")
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(UnknownCommandOutcome):
                await self.backend.start_goal(
                    command_id="stable-command-7",
                    objective="original exact objective",
                )
            with self.assertRaises(ValueError):
                await self.backend.start_goal(
                    command_id="stable-command-7",
                    objective="different objective",
                )

        execute.assert_awaited_once()

    async def test_outbox_prepare_is_durable_before_first_execute_attempt(self) -> None:
        events: list[str] = []
        original_prepare = self.outbox.prepare

        def prepare(record: object) -> None:
            original_prepare(record)
            events.append("prepare-durable")

        async def execute(*_args: object) -> object:
            events.append("execute-update")
            raise TimeoutError("simulated crash window")

        _ensure, _query, execute_mock, patches = self.helper_patches()
        execute_mock.side_effect = execute
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(self.outbox, "prepare", side_effect=prepare),
        ):
            with self.assertRaises(UnknownCommandOutcome):
                await self.backend.user_message(
                    command_id="crash-command",
                    message_id="crash-command:message",
                    text="crash-safe exact text",
                )

        self.assertEqual(["prepare-durable", "execute-update"], events)
        persisted = self.outbox.load_pending()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(5, persisted.command_seq)
        self.assertEqual("crash-safe exact text", persisted.text)

    async def test_prepare_failure_proves_execute_was_not_called(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(
                self.outbox,
                "prepare",
                side_effect=OSError("durability unavailable"),
            ),
        ):
            with self.assertRaises(OSError):
                await self.backend.pause(command_id="not-sent-command")

        execute.assert_not_awaited()
        self.assertIsNone(self.outbox.load_pending())

    async def test_restart_hydrates_fence_and_retry_uses_exact_wire_record(self) -> None:
        _ensure, _query, execute, patches = self.helper_patches()
        execute.side_effect = TimeoutError("host crashed after send")
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(UnknownCommandOutcome):
                await self.backend.user_message(
                    command_id="restart-command",
                    message_id="restart-command:message",
                    text="restart exact text",
                )

        restarted = TemporalInteractiveBackend(
            self.client,
            paths=self.paths,
            outbox=self.outbox,
            task_queue=task_queue_for_project(self.paths.project_root),
            goal_budget=Budget(max_automatic_turns=3),
        )
        recovered = restarted.recovered_pending_command
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertIs(CommandKind.USER_MESSAGE, recovered.kind)
        self.assertEqual("restart-command", recovered.command_id)
        self.assertEqual("restart exact text", recovered.text)
        session = InteractiveSession(restarted, pending_command=recovered)
        self.assertTrue(session.output_suspended)

        with self.assertRaises(PendingCommandError):
            await session.submit("a new message must not bypass recovery")

        retry_execute = AsyncMock(
            return_value={"last_command_seq": 5, "intent_epoch": 9}
        )
        with (
            patch(
                "codex_orchestrator.interactive_temporal.command_lock",
                return_value=nullcontext(),
            ),
            patch(
                "codex_orchestrator.interactive_temporal._execute_update",
                retry_execute,
            ),
        ):
            result = await session.submit("/retry")

        self.assertEqual(CommandKind.RETRY, result.kind)
        self.assertFalse(session.output_suspended)
        self.assertIsNone(session.pending_command)
        self.assertIsNone(self.outbox.load_pending())
        sent_payload = retry_execute.await_args.args[3]
        self.assertEqual(5, sent_payload.command_seq)
        self.assertEqual("restart-command:message", sent_payload.message_id)
        self.assertEqual("restart exact text", sent_payload.text)

    async def test_result_is_not_accepted_when_durable_resolve_fails(self) -> None:
        session = InteractiveSession(
            self.backend,
            id_factory=lambda: "resolve-failure-command",
        )
        _ensure, _query, execute, patches = self.helper_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(
                self.outbox,
                "resolve",
                side_effect=OSError("write-through resolve failed"),
            ),
        ):
            with self.assertRaises(UnknownCommandOutcome):
                await session.submit("known server result, unresolved disk fence")

        execute.assert_awaited_once()
        self.assertTrue(session.output_suspended)
        self.assertIsNotNone(session.pending_command)
        persisted = self.outbox.load_pending()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual("resolve-failure-command", persisted.command_id)

    def test_rejects_wrong_project_task_queue(self) -> None:
        with self.assertRaises(ValueError):
            TemporalInteractiveBackend(
                self.client,
                paths=self.paths,
                outbox=self.outbox,
                task_queue="shared-global-queue",
            )


if __name__ == "__main__":
    unittest.main()
