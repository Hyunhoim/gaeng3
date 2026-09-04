from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    bind_request_audit,
    current_request_audit,
    sha256_text,
)
from starlette.types import Message, Receive, Scope, Send

from app import request_execution as execution_module
from app.http_audit import (
    AnswerHttpAuditMiddleware,
    mark_request_audit_terminal,
    mark_response_serialization_start,
    request_agent_audit,
)
from app.request_execution import (
    IdempotentRequestCoordinator,
    RequestExecutionDisposition,
    wait_for_request_workers,
)


def _scope(sink: BoundedAsyncAuditSink) -> Scope:
    application = SimpleNamespace(
        state=SimpleNamespace(audit_sink=sink, release_guard=None),
    )
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/answer",
            "raw_path": b"/answer",
            "query_string": b"raw-secret-query=must-not-be-read",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "app": application,
            "state": {},
        },
    )


async def _receive() -> Message:
    return {"type": "http.request", "body": b"raw-secret-body", "more_body": False}


def _terminal_events(downstream: InMemoryAuditSink) -> tuple[AuditEvent, ...]:
    events = downstream.snapshot()
    assert [(event.stage, event.outcome, event.reason_code) for event in events] == [
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received"),
        (AuditStage.REQUEST, AuditOutcome.FAILED, "response_aborted"),
    ]
    assert [event.event_sequence for event in events] == [1, 2]
    assert len({event.invocation_id_sha256 for event in events}) == 1
    assert all(event.request_id_sha256 == sha256_text("") for event in events)
    assert all(event.question_sha256 == sha256_text("") for event in events)
    return events


def test_transport_audit_marks_response_aborted_when_final_send_fails() -> None:
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)

    async def application(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"secret-answer"})

    async def failing_send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise RuntimeError("transport closed")

    with pytest.raises(RuntimeError, match="transport closed"):
        asyncio.run(AnswerHttpAuditMiddleware(application)(_scope(sink), _receive, failing_send))

    assert sink.close(timeout_seconds=1) is True
    _terminal_events(downstream)


def test_transport_audit_marks_response_aborted_on_cancellation() -> None:
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)

    async def cancelled_application(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise asyncio.CancelledError

    async def unused_send(_message: Message) -> None:
        raise AssertionError("cancelled application must not send")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            AnswerHttpAuditMiddleware(cancelled_application)(
                _scope(sink),
                _receive,
                unused_send,
            )
        )

    assert sink.close(timeout_seconds=1) is True
    _terminal_events(downstream)


def test_cancelled_transport_defers_terminal_until_shared_worker_and_retry_finish() -> None:
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)
    coordinator = IdempotentRequestCoordinator()
    worker_started = Event()
    release_worker = Event()
    second_entered = asyncio.Event()
    call_count = 0
    attempt_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        recorder = current_request_audit()
        assert recorder is not None
        recorder.emit(
            stage=AuditStage.COMPILER,
            outcome=AuditOutcome.STARTED,
            reason_code="operation_started",
            duration_ms=0,
        )
        worker_started.set()
        assert release_worker.wait(1)
        recorder.emit(
            stage=AuditStage.COMPILER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="operation_completed",
            duration_ms=0,
        )
        return "safe-result"

    async def application(scope: Scope, _receive: Receive, send: Send) -> None:
        nonlocal attempt_count
        attempt_count += 1
        attempt = attempt_count
        request = Request(scope)
        recorder = request_agent_audit(
            request,
            request_id="Q-CANCEL-RETRY",
            question="동일한 재시도 질문",
        )
        assert recorder is not None
        if attempt == 2:
            second_entered.set()
        with bind_request_audit(recorder):
            execution = await coordinator.execute(
                operation,
                request_key="Q-CANCEL-RETRY",
                request_input="동일한 재시도 질문",
                timeout_seconds=1,
                max_inflight=1,
                cache_result=lambda _result: True,
            )
        if execution.disposition is RequestExecutionDisposition.JOINED:
            mark_request_audit_terminal(
                request,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="idempotent_request_joined",
            )
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"safe"})

    async def successful_send(_message: Message) -> None:
        return None

    async def scenario() -> None:
        middleware = AnswerHttpAuditMiddleware(application)
        first = asyncio.create_task(middleware(_scope(sink), _receive, successful_send))
        assert await asyncio.to_thread(worker_started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        # START and the worker's first event are accepted, but the cancelled
        # transport terminal must wait for that worker's final Audit event.
        assert sink.snapshot().accepted_event_count == 2

        second = asyncio.create_task(middleware(_scope(sink), _receive, successful_send))
        await second_entered.wait()
        await asyncio.sleep(0)
        release_worker.set()
        await second

    asyncio.run(scenario())
    assert wait_for_request_workers(timeout_seconds=1) is True
    assert sink.close(timeout_seconds=1) is True
    assert call_count == 1

    by_invocation: dict[str, list[AuditEvent]] = {}
    for event in downstream.snapshot():
        assert event.invocation_id_sha256 is not None
        by_invocation.setdefault(event.invocation_id_sha256, []).append(event)
    assert len(by_invocation) == 2
    worker_invocation = next(
        events
        for events in by_invocation.values()
        if any(event.stage is AuditStage.COMPILER for event in events)
    )
    retry_invocation = next(
        events for events in by_invocation.values() if events is not worker_invocation
    )
    assert [(event.stage, event.outcome, event.reason_code) for event in worker_invocation] == [
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received"),
        (AuditStage.COMPILER, AuditOutcome.STARTED, "operation_started"),
        (AuditStage.COMPILER, AuditOutcome.SUCCEEDED, "operation_completed"),
        (AuditStage.REQUEST, AuditOutcome.FAILED, "response_aborted"),
    ]
    assert [(event.stage, event.outcome, event.reason_code) for event in retry_invocation] == [
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received"),
        (AuditStage.REQUEST, AuditOutcome.SUCCEEDED, "idempotent_request_joined"),
    ]
    assert [event.event_sequence for event in worker_invocation] == [1, 2, 3, 4]
    assert [event.event_sequence for event in retry_invocation] == [1, 2]


def test_shutdown_drain_includes_terminal_enqueue_after_worker_counter_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ManualClock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)
    coordinator = IdempotentRequestCoordinator()
    clock = ManualClock()
    worker_started = Event()
    release_worker = Event()
    process_counter_released = Event()
    allow_remaining_callbacks = Event()
    original_release = execution_module._PROCESS_ADMISSION.release

    def release_process_counter_then_pause() -> None:
        original_release()
        process_counter_released.set()
        assert allow_remaining_callbacks.wait(1)

    monkeypatch.setattr(
        execution_module._PROCESS_ADMISSION,
        "release",
        release_process_counter_then_pause,
    )

    def operation() -> str:
        recorder = current_request_audit()
        assert recorder is not None
        recorder.emit(
            stage=AuditStage.COMPILER,
            outcome=AuditOutcome.STARTED,
            reason_code="operation_started",
            duration_ms=0,
        )
        worker_started.set()
        assert release_worker.wait(1)
        recorder.emit(
            stage=AuditStage.COMPILER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="operation_completed",
            duration_ms=0,
        )
        return "safe-result"

    async def application(scope: Scope, _receive: Receive, _send: Send) -> None:
        request = Request(scope)
        recorder = request_agent_audit(
            request,
            request_id="Q-SHUTDOWN-RACE",
            question="종료 경계 질문",
        )
        assert recorder is not None
        with bind_request_audit(recorder):
            await coordinator.execute(
                operation,
                request_key="Q-SHUTDOWN-RACE",
                request_input="종료 경계 질문",
                timeout_seconds=1,
                max_inflight=1,
                cache_result=lambda _result: True,
            )

    async def unused_send(_message: Message) -> None:
        raise AssertionError("cancelled transport must not send")

    async def scenario() -> None:
        task = asyncio.create_task(
            AnswerHttpAuditMiddleware(application, clock=clock)(
                _scope(sink),
                _receive,
                unused_send,
            )
        )
        assert await asyncio.to_thread(worker_started.wait, 1)
        clock.value = 101.0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Worker cleanup time must not inflate the completed HTTP attempt's
        # duration, even though terminal persistence is deliberately deferred.
        clock.value = 999.0
        release_worker.set()
        assert await asyncio.to_thread(process_counter_released.wait, 1)

        # The process worker counter is already idle, while its terminal
        # callback is paused behind this deterministic latch.
        assert (
            await asyncio.to_thread(
                wait_for_request_workers,
                timeout_seconds=0.01,
            )
            is False
        )
        allow_remaining_callbacks.set()
        assert (
            await asyncio.to_thread(
                wait_for_request_workers,
                timeout_seconds=1,
            )
            is True
        )

    asyncio.run(scenario())
    assert sink.close(timeout_seconds=1) is True
    events = downstream.snapshot()
    assert [(event.stage, event.reason_code) for event in events] == [
        (AuditStage.REQUEST, "received"),
        (AuditStage.COMPILER, "operation_started"),
        (AuditStage.COMPILER, "operation_completed"),
        (AuditStage.REQUEST, "response_aborted"),
    ]
    assert events[-1].duration_ms == pytest.approx(1_000.0)


def test_transport_audit_times_only_marked_http_response_serialization() -> None:
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)

    async def application(scope: Scope, _receive: Receive, send: Send) -> None:
        request = Request(scope)
        recorder = request_agent_audit(
            request,
            request_id="serialization-audit-001",
            question="직렬화 계측 질문",
        )
        assert recorder is not None
        mark_response_serialization_start(request)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"safe"})

    async def successful_send(_message: Message) -> None:
        return None

    asyncio.run(
        AnswerHttpAuditMiddleware(application)(
            _scope(sink),
            _receive,
            successful_send,
        )
    )
    assert sink.close(timeout_seconds=1) is True

    events = downstream.snapshot()
    assert [(event.stage, event.outcome, event.reason_code) for event in events] == [
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received"),
        (AuditStage.SERIALIZATION, AuditOutcome.SUCCEEDED, "http_response_serialized"),
        (AuditStage.REQUEST, AuditOutcome.SUCCEEDED, "response_completed"),
    ]
    assert events[1].duration_ms >= 0
    assert events[1].request_id_sha256 == sha256_text("serialization-audit-001")
    assert events[1].question_sha256 == sha256_text("직렬화 계측 질문")


def test_transport_audit_ignores_non_answer_routes() -> None:
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)
    called = False

    async def application(_scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal called
        called = True

    async def unused_send(_message: Message) -> None:
        raise AssertionError("non-answer test application must not send")

    scope = _scope(sink)
    scope["path"] = "/health"
    asyncio.run(AnswerHttpAuditMiddleware(application)(scope, _receive, unused_send))

    assert sink.close(timeout_seconds=1) is True
    assert called is True
    assert downstream.snapshot() == ()


def test_uvicorn_access_log_is_explicitly_disabled() -> None:
    start_script = Path(__file__).resolve().parents[1] / "start.sh"
    contents = start_script.read_text(encoding="utf-8")

    assert contents.count("--no-access-log") == 1
