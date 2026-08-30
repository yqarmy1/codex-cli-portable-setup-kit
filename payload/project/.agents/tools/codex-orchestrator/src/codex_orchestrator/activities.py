"""Temporal Activity entry points."""

from __future__ import annotations

from openai_codex.errors import (
    InvalidParamsError,
    InvalidRequestError,
    JsonRpcError,
    MethodNotFoundError,
    ParseError,
)
from temporalio import activity

from .codex_adapter import AdapterInvariantError, CodexAdapter
from .contracts import CodexOperationInput
from .domain import OperationResult, ResultDisposition


class CodexActivities:
    def __init__(self, adapter: CodexAdapter | None = None) -> None:
        self.adapter = adapter or CodexAdapter()

    async def close(self) -> None:
        await self.adapter.close()

    @activity.defn(name="run_codex_operation")
    async def run_codex_operation(
        self,
        request: CodexOperationInput,
    ) -> OperationResult:
        try:
            return await self.adapter.run(
                request,
                heartbeat=lambda details: activity.heartbeat(details),
            )
        except AdapterInvariantError as exc:
            # Invariant failures happen before permitted automatic replay. Keep
            # the durable error categorical; raw process/model output is not
            # copied into Temporal state.
            return self._failed_result(
                request,
                detail=f"adapter_invariant:{type(exc).__name__}",
            )
        except (
            InvalidParamsError,
            InvalidRequestError,
            MethodNotFoundError,
            ParseError,
        ) as exc:
            # An explicit JSON-RPC rejection is a known failed submission, not
            # an unknown outcome that should trigger turn reconciliation.
            return self._failed_result(
                request,
                detail=f"adapter_protocol_rejected:{type(exc).__name__}",
            )
        except JsonRpcError as exc:
            if exc.code != -32001:
                raise
            # app-server documents -32001 as ingress overload before execution.
            # Let the durable failure budget decide whether to schedule a new
            # operation; never replay this activity behind Temporal's back.
            return self._failed_result(
                request,
                detail="adapter_overloaded",
                safe_to_retry=True,
            )

    @staticmethod
    def _failed_result(
        request: CodexOperationInput,
        *,
        detail: str,
        safe_to_retry: bool = False,
    ) -> OperationResult:
        return OperationResult(
            operation_id=request.operation.operation_id,
            intent_epoch=request.operation.intent_epoch,
            disposition=ResultDisposition.FAILED,
            tokens=0,
            thread_id=request.native_thread_id,
            detail=detail,
            safe_to_retry=safe_to_retry,
        )
