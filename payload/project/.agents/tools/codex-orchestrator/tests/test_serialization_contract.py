from __future__ import annotations

import unittest

from temporalio.converter import DataConverter

from codex_orchestrator.contracts import (
    CodexOperationInput,
    ControlCommand,
    StartGoalCommand,
    UserMessageCommand,
    WorkflowConfig,
)
from codex_orchestrator.domain import (
    Budget,
    Operation,
    OperationKind,
    OperationResult,
    PendingManualMessage,
    ResultDisposition,
    SupervisorState,
    SupervisorStatus,
)


class TemporalSerializationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_converter_reconstructs_nested_domain_types(self) -> None:
        original = WorkflowConfig(
            state=SupervisorState(
                workflow_key="serialization-probe",
                project_root="C:/workspace/project",
            )
        )

        payloads = await DataConverter.default.encode([original])
        decoded = await DataConverter.default.decode(payloads, [WorkflowConfig])

        self.assertEqual(1, len(decoded))
        restored = decoded[0]
        self.assertIsInstance(restored, WorkflowConfig)
        self.assertIsInstance(restored.state, SupervisorState)
        self.assertIsInstance(restored.state.budget, Budget)
        self.assertIsInstance(restored.state.status, SupervisorStatus)
        self.assertIs(restored.state.status, SupervisorStatus.IDLE)

    async def test_converter_reconstructs_every_durable_string_enum(self) -> None:
        operation = Operation(
            operation_id="operation-1",
            kind=OperationKind.AUTOMATIC_TURN,
            intent_epoch=2,
            action_seq=3,
            objective_sha256="0" * 64,
            thread_id=None,
        )
        result = OperationResult(
            operation_id="operation-1",
            intent_epoch=2,
            disposition=ResultDisposition.NEEDS_USER,
            tokens=12,
        )

        payloads = await DataConverter.default.encode([operation, result])
        restored_operation, restored_result = await DataConverter.default.decode(
            payloads,
            [Operation, OperationResult],
        )

        self.assertIs(restored_operation.kind, OperationKind.AUTOMATIC_TURN)
        self.assertIs(restored_result.disposition, ResultDisposition.NEEDS_USER)

    async def test_all_temporal_wire_dataclasses_round_trip_with_types(self) -> None:
        budget = Budget(
            max_automatic_turns=7,
            max_tokens=70_000,
            max_elapsed_seconds=700,
            max_failures=2,
            max_rollovers=3,
        )
        operation = Operation(
            operation_id="operation-wire-1",
            kind=OperationKind.MANUAL_TURN,
            intent_epoch=5,
            action_seq=8,
            objective_sha256="a" * 64,
            thread_id="thread-1",
            message_id="message-1",
            message_text="ordinary user text",
        )
        state = SupervisorState(
            workflow_key="workflow-wire-1",
            project_root="C:/workspace/project",
            budget=budget,
            status=SupervisorStatus.RECONCILE_REQUIRED,
            objective="exact objective",
            objective_sha256="a" * 64,
            goal_generation=2,
            intent_epoch=5,
            last_command_seq=4,
            action_seq=8,
            current_operation=operation,
            ambiguous_operation=operation,
            post_reconcile_status=SupervisorStatus.MANUAL_PENDING,
            pending_manual=PendingManualMessage(4, "message-1", "ordinary user text"),
        )
        values = [
            WorkflowConfig(state=state),
            StartGoalCommand(5, " exact objective bytes ", budget),
            UserMessageCommand(6, "message-2", "new user text"),
            ControlCommand(7),
            CodexOperationInput(
                operation=operation,
                project_root=state.project_root,
                objective=state.objective,
                objective_sha256=state.objective_sha256,
                lifetime_tokens_used=123,
                lifetime_automatic_turns=3,
                lifetime_rollovers=1,
                native_thread_id="thread-1",
                ambiguous_operation=operation,
                metadata={"workflow_key": state.workflow_key},
            ),
            OperationResult(
                operation_id=operation.operation_id,
                intent_epoch=operation.intent_epoch,
                disposition=ResultDisposition.INTERRUPTED,
                tokens=321,
                thread_id="thread-1",
                turn_id="turn-1",
                context_window=200_000,
                thread_total_tokens=50_000,
            ),
        ]
        hints = [
            WorkflowConfig,
            StartGoalCommand,
            UserMessageCommand,
            ControlCommand,
            CodexOperationInput,
            OperationResult,
        ]

        payloads = await DataConverter.default.encode(values)
        restored = await DataConverter.default.decode(payloads, hints)

        self.assertEqual(values, restored)
        restored_config = restored[0]
        self.assertIsInstance(restored_config, WorkflowConfig)
        restored_config.validate()
        self.assertIs(
            restored_config.state.post_reconcile_status,
            SupervisorStatus.MANUAL_PENDING,
        )
        self.assertIs(
            restored_config.state.current_operation.kind,
            OperationKind.MANUAL_TURN,
        )


if __name__ == "__main__":
    unittest.main()
