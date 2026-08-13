from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    sha256_text,
)
from starlette.types import Message, Receive, Scope, Send

from app.http_audit import AnswerHttpAuditMiddleware


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
