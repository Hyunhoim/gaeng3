from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi import Request
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    RequestAuditRecorder,
)
from finance_agent_core.release import ResolvedAgentRelease
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.request_execution import (
    RequestAuditWorkerBarrier,
    bind_request_audit_worker_barrier,
)

_RECORDER_STATE_KEY = "finance_answer_transport_audit_recorder"
_TERMINAL_STATE_KEY = "finance_answer_transport_audit_terminal"
_SERIALIZATION_STARTED_STATE_KEY = "finance_answer_serialization_started"
_CLOCK_STATE_KEY = "finance_answer_audit_clock"


@dataclass(frozen=True, slots=True)
class HttpAuditTerminal:
    outcome: AuditOutcome
    reason_code: str


def _application_state(scope: Scope) -> Any | None:
    application = scope.get("app")
    return getattr(application, "state", None)


def _release_fields(release: object) -> dict[str, str | None]:
    if type(release) is not ResolvedAgentRelease:
        return {
            "agent_release_id": None,
            "agent_release_manifest_sha256": None,
            "deployment_binding_sha256": None,
            "release_context_sha256": None,
        }
    return {
        "agent_release_id": release.release_id,
        "agent_release_manifest_sha256": release.manifest_file_sha256,
        "deployment_binding_sha256": release.binding_file_sha256,
        "release_context_sha256": release.release_context_sha256,
    }


def _transport_recorder(scope: Scope) -> RequestAuditRecorder | None:
    application_state = _application_state(scope)
    if application_state is None:
        return None
    sink = getattr(application_state, "audit_sink", None)
    if type(sink) is not BoundedAsyncAuditSink:
        return None
    return RequestAuditRecorder(
        request_id="",
        question="",
        sink=sink,
        **_release_fields(getattr(application_state, "release_guard", None)),
    )


def _scope_state(scope: Scope) -> dict[str, Any]:
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    return state


def request_transport_audit(request: Request) -> RequestAuditRecorder | None:
    recorder = getattr(request.state, _RECORDER_STATE_KEY, None)
    return recorder if type(recorder) is RequestAuditRecorder else None


def request_agent_audit(
    request: Request,
    *,
    request_id: str,
    question: str,
) -> RequestAuditRecorder | None:
    recorder = request_transport_audit(request)
    if recorder is None:
        return None
    enriched = recorder.with_request(request_id=request_id, question=question)
    setattr(request.state, _RECORDER_STATE_KEY, enriched)
    return enriched


def mark_request_audit_terminal(
    request: Request,
    *,
    outcome: AuditOutcome,
    reason_code: str,
) -> None:
    if request_transport_audit(request) is None:
        return
    setattr(
        request.state,
        _TERMINAL_STATE_KEY,
        HttpAuditTerminal(outcome=outcome, reason_code=reason_code),
    )


def mark_response_serialization_start(request: Request) -> None:
    """Mark the DTO-return boundary without carrying response content."""

    if request_transport_audit(request) is None:
        return
    state = _scope_state(request.scope)
    clock = state.get(_CLOCK_STATE_KEY)
    if not callable(clock):
        clock = perf_counter
    state[_SERIALIZATION_STARTED_STATE_KEY] = float(clock())


class AnswerHttpAuditMiddleware:
    """Audit only the public answer transport without reading request payloads.

    `response_completed` means the final ASGI response body frame was accepted
    by the downstream server callable. It is not a client delivery receipt.
    """

    def __init__(self, app: ASGIApp, *, clock: Callable[[], float] = perf_counter) -> None:
        self._app = app
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in {"/answer", "/answer/"}:
            await self._app(scope, receive, send)
            return

        recorder = _transport_recorder(scope)
        if recorder is None:
            await self._app(scope, receive, send)
            return
        state = _scope_state(scope)
        state[_RECORDER_STATE_KEY] = recorder
        state[_CLOCK_STATE_KEY] = self._clock
        started = self._clock()
        recorder.emit(
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.STARTED,
            reason_code="received",
            duration_ms=0,
        )
        worker_barrier = RequestAuditWorkerBarrier()
        terminal_requested = False
        terminal_emitted = False
        response_status: int | None = None

        def emit_terminal_now(
            terminal: HttpAuditTerminal,
            *,
            requested_at: float,
        ) -> None:
            nonlocal terminal_emitted
            if terminal_emitted:
                return
            terminal_emitted = True
            active_recorder = state.get(_RECORDER_STATE_KEY)
            if (
                type(active_recorder) is not RequestAuditRecorder
                or active_recorder.sink is not recorder.sink
                or active_recorder.invocation_id_sha256 != recorder.invocation_id_sha256
            ):
                active_recorder = recorder
            active_recorder.emit(
                stage=AuditStage.REQUEST,
                outcome=terminal.outcome,
                reason_code=terminal.reason_code,
                duration_ms=max(0.0, (requested_at - started) * 1000),
            )

        def request_terminal(terminal: HttpAuditTerminal) -> None:
            nonlocal terminal_requested
            if terminal_requested:
                return
            terminal_requested = True
            requested_at = self._clock()
            worker_barrier.defer_terminal(
                lambda: emit_terminal_now(terminal, requested_at=requested_at)
            )

        async def audited_send(message: Message) -> None:
            nonlocal response_status
            serialization_finished = (
                self._clock() if message["type"] == "http.response.start" else None
            )
            try:
                await send(message)
            except BaseException:
                request_terminal(
                    HttpAuditTerminal(
                        outcome=AuditOutcome.FAILED,
                        reason_code="response_aborted",
                    )
                )
                raise
            if message["type"] == "http.response.start":
                status = message.get("status")
                response_status = status if isinstance(status, int) else None
                serialization_started = state.pop(
                    _SERIALIZATION_STARTED_STATE_KEY,
                    None,
                )
                active_recorder = state.get(_RECORDER_STATE_KEY)
                if (
                    isinstance(serialization_started, float)
                    and serialization_finished is not None
                    and type(active_recorder) is RequestAuditRecorder
                ):
                    active_recorder.emit(
                        stage=AuditStage.SERIALIZATION,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="http_response_serialized",
                        duration_ms=max(
                            0.0,
                            (serialization_finished - serialization_started) * 1000,
                        ),
                    )
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                terminal = state.get(_TERMINAL_STATE_KEY)
                if type(terminal) is not HttpAuditTerminal:
                    if response_status is not None and 200 <= response_status < 400:
                        terminal = HttpAuditTerminal(
                            outcome=AuditOutcome.SUCCEEDED,
                            reason_code="response_completed",
                        )
                    elif response_status is not None and 400 <= response_status < 500:
                        terminal = HttpAuditTerminal(
                            outcome=AuditOutcome.BLOCKED,
                            reason_code="client_error",
                        )
                    else:
                        terminal = HttpAuditTerminal(
                            outcome=AuditOutcome.FAILED,
                            reason_code="response_completed",
                        )
                request_terminal(terminal)

        with bind_request_audit_worker_barrier(worker_barrier):
            try:
                await self._app(scope, receive, audited_send)
            except BaseException:
                request_terminal(
                    HttpAuditTerminal(
                        outcome=AuditOutcome.FAILED,
                        reason_code="response_aborted",
                    )
                )
                raise
            finally:
                if not terminal_requested:
                    request_terminal(
                        HttpAuditTerminal(
                            outcome=AuditOutcome.FAILED,
                            reason_code="response_aborted",
                        )
                    )


__all__ = [
    "AnswerHttpAuditMiddleware",
    "HttpAuditTerminal",
    "mark_request_audit_terminal",
    "mark_response_serialization_start",
    "request_agent_audit",
    "request_transport_audit",
]
