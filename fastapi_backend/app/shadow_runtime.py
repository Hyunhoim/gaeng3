from __future__ import annotations

from threading import Lock
from typing import Literal

from finance_agent_core.retrieval.schema_shadow import AsyncSchemaLinkShadowObserver

ShadowRuntimeStatus = Literal["disabled", "ok", "degraded"]


class ShadowRuntimeState:
    """Application-owned readiness and shutdown view of Schema Dense Shadow.

    Shadow remains non-authoritative for user answers. Operational loss is
    nevertheless readiness-significant once the observer is enabled because a
    process that silently drops or cannot correlate observations cannot claim a
    complete Shadow experiment.
    """

    def __init__(self, observer: AsyncSchemaLinkShadowObserver | None) -> None:
        if observer is not None and type(observer) is not AsyncSchemaLinkShadowObserver:
            raise TypeError("shadow runtime requires the bounded async observer")
        self._observer = observer
        self._lock = Lock()
        self._shutdown_drained: bool | None = None
        self._degraded_latched = False

    @property
    def enabled(self) -> bool:
        return self._observer is not None and self._observer.enabled

    @property
    def shutdown_drained(self) -> bool | None:
        with self._lock:
            return self._shutdown_drained

    def close(
        self,
        *,
        timeout_seconds: float,
        upstream_drained: bool = True,
    ) -> bool:
        if timeout_seconds < 0:
            raise ValueError("shadow shutdown timeout cannot be negative")
        observer_drained = True
        if self._observer is not None:
            try:
                observer_drained = self._observer.shutdown(
                    timeout_seconds=timeout_seconds,
                    drain=True,
                )
            except Exception:  # noqa: BLE001 - lifecycle status must stay bounded
                observer_drained = False
        drained = upstream_drained and observer_drained
        with self._lock:
            self._shutdown_drained = drained
            if not drained:
                self._degraded_latched = True
        return drained

    def status(self) -> ShadowRuntimeStatus:
        observer = self._observer
        if observer is None or not observer.enabled:
            return "disabled"
        with self._lock:
            if self._shutdown_drained is False or self._degraded_latched:
                return "degraded"
        try:
            snapshot = observer.snapshot()
        except Exception:  # noqa: BLE001 - readiness must not expose observer details
            self._latch_degraded()
            return "degraded"
        degraded = (
            not snapshot.enabled
            or snapshot.queue_drop_count > 0
            or snapshot.operational_failure_count > 0
            or snapshot.correlation_failure_count > 0
            or snapshot.audit_emit_failure_count > 0
            or snapshot.stalled
            or snapshot.shutdown_succeeded is False
            or snapshot.shutdown_started
            or (snapshot.started and not snapshot.worker_alive and not snapshot.shutdown_completed)
            or not snapshot.accepting
            or snapshot.completed_count > snapshot.accepted_count
            or snapshot.pending_count != snapshot.accepted_count - snapshot.completed_count
            or snapshot.audit_emit_attempt_count
            != snapshot.audit_emit_success_count + snapshot.audit_emit_failure_count
        )
        if degraded:
            self._latch_degraded()
            return "degraded"
        return "ok"

    def _latch_degraded(self) -> None:
        with self._lock:
            self._degraded_latched = True


__all__ = ["ShadowRuntimeState", "ShadowRuntimeStatus"]
