from __future__ import annotations

from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.observability import BoundedAsyncAuditSink, InMemoryAuditSink
from finance_agent_core.retrieval.schema_shadow import (
    AsyncSchemaLinkShadowObserver,
    HybridSchemaLinkShadow,
    SchemaShadowMode,
    SchemaShadowSettings,
)

from app.config import Settings
from app.main import create_app
from app.shadow_runtime import ShadowRuntimeState


def _observer(
    mode: SchemaShadowMode = SchemaShadowMode.SHADOW,
    *,
    audit_sink: BoundedAsyncAuditSink | None = None,
):
    return AsyncSchemaLinkShadowObserver(
        HybridSchemaLinkShadow(
            settings=SchemaShadowSettings(mode=mode),
            audit_sink=audit_sink,
        )
    )


def test_shadow_runtime_distinguishes_disabled_and_lazy_ok() -> None:
    assert ShadowRuntimeState(None).status() == "disabled"

    off_observer = _observer(SchemaShadowMode.OFF)
    off_runtime = ShadowRuntimeState(off_observer)
    assert off_runtime.status() == "disabled"
    assert off_runtime.close(timeout_seconds=1)

    active_observer = _observer()
    active_runtime = ShadowRuntimeState(active_observer)
    snapshot = active_observer.snapshot()
    assert snapshot.enabled
    assert not snapshot.started
    assert active_runtime.status() == "ok"
    assert active_runtime.close(timeout_seconds=1)
    assert active_runtime.shutdown_drained is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("queue_drop_count", 1),
        ("operational_failure_count", 1),
        ("correlation_failure_count", 1),
        ("audit_emit_failure_count", 1),
        ("stalled", True),
    ],
)
def test_shadow_runtime_latches_readiness_failures(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    observer = _observer()
    runtime = ShadowRuntimeState(observer)
    healthy = observer.snapshot()
    updates = {field: value}
    if field == "audit_emit_failure_count":
        updates["audit_emit_attempt_count"] = 1
    failed = healthy.model_copy(update=updates)
    monkeypatch.setattr(observer, "snapshot", lambda: failed)

    assert runtime.status() == "degraded"
    monkeypatch.setattr(observer, "snapshot", lambda: healthy)
    assert runtime.status() == "degraded"
    assert runtime.close(timeout_seconds=1)


def test_shadow_runtime_detects_dead_worker_and_counter_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer()
    runtime = ShadowRuntimeState(observer)
    snapshot = observer.snapshot().model_copy(
        update={
            "started": True,
            "worker_alive": False,
            "accepted_count": 1,
            "completed_count": 0,
            "pending_count": 0,
        }
    )
    monkeypatch.setattr(observer, "snapshot", lambda: snapshot)

    assert runtime.status() == "degraded"
    assert runtime.close(timeout_seconds=1)


def test_create_app_owns_real_shadow_observer_lifecycle() -> None:
    observer = _observer()
    agent = RoutedFinanceAgent({}, schema_link_shadow_observer=observer)
    application = create_app(settings=Settings(), agent=agent)

    with TestClient(application) as client:
        response = client.get("/health")
        assert response.json()["shadow_status"] == "ok"
        assert application.state.shadow_runtime.enabled

    snapshot = observer.snapshot()
    assert snapshot.shutdown_completed
    assert snapshot.shutdown_succeeded is True
    assert application.state.shadow_shutdown_drained is True


def test_audit_enabled_app_rejects_shadow_without_the_same_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    app_sink = BoundedAsyncAuditSink(InMemoryAuditSink(), start_worker=False)
    observer = _observer(audit_sink=app_sink)
    agent = RoutedFinanceAgent({}, schema_link_shadow_observer=observer)
    monkeypatch.setattr(main_module, "build_audit_sink", lambda _settings: app_sink)

    with pytest.raises(RuntimeError, match="Agent and Schema Shadow must use"):
        create_app(settings=Settings(FINANCE_AUDIT_MODE="disabled"), agent=agent)

    assert observer.snapshot().shutdown_succeeded is True
    assert app_sink.snapshot().closed is True


def test_audit_enabled_app_accepts_agent_and_shadow_with_the_same_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    app_sink = BoundedAsyncAuditSink(InMemoryAuditSink(), start_worker=False)
    observer = _observer(audit_sink=app_sink)
    agent = RoutedFinanceAgent(
        {},
        schema_link_shadow_observer=observer,
        audit_sink=app_sink,
    )
    monkeypatch.setattr(main_module, "build_audit_sink", lambda _settings: app_sink)

    application = create_app(
        settings=Settings(FINANCE_AUDIT_MODE="disabled"),
        agent=agent,
    )
    with TestClient(application) as client:
        response = client.get("/health")
        assert response.json()["audit_status"] == "ok"
        assert response.json()["shadow_status"] == "ok"

    assert observer.snapshot().shutdown_succeeded is True
    assert app_sink.snapshot().closed is True


def test_lifespan_uses_request_shadow_audit_order_and_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    timeout_seconds = 0.05
    observer = _observer()
    agent = RoutedFinanceAgent({}, schema_link_shadow_observer=observer)
    application = create_app(
        settings=Settings(
            FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS=timeout_seconds,
        ),
        agent=agent,
    )
    calls: list[tuple[str, float, bool | None]] = []

    def wait_for_requests(*, timeout_seconds: float) -> bool:
        calls.append(("request", timeout_seconds, None))
        sleep(0.01)
        return True

    def close_shadow(*, timeout_seconds: float, drain: bool) -> bool:
        calls.append(("shadow", timeout_seconds, drain))
        sleep(0.01)
        return False

    def close_audit(*, timeout_seconds: float, upstream_drained: bool = True) -> bool:
        calls.append(("audit", timeout_seconds, upstream_drained))
        return upstream_drained

    monkeypatch.setattr(main_module, "wait_for_request_workers", wait_for_requests)
    monkeypatch.setattr(observer, "shutdown", close_shadow)
    monkeypatch.setattr(application.state.audit_runtime, "close", close_audit)

    started = monotonic()
    with TestClient(application):
        pass
    elapsed = monotonic() - started

    assert [name for name, _, _ in calls] == ["request", "shadow", "audit"]
    assert calls[0][1] == timeout_seconds
    assert timeout_seconds > calls[1][1] > calls[2][1] > 0
    assert calls[1][2] is True
    assert calls[2][2] is False
    assert elapsed < timeout_seconds + 0.1
    assert application.state.shadow_shutdown_drained is False
    assert application.state.audit_shutdown_drained is False

    # The monkeypatched shutdown did not close the real lazy observer.
    assert AsyncSchemaLinkShadowObserver.shutdown(observer, timeout_seconds=1)
