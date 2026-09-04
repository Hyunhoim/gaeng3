from __future__ import annotations

from threading import Lock
from typing import Literal

from finance_agent_core.observability import BoundedAsyncAuditSink

AuditRuntimeStatus = Literal["disabled", "ok", "degraded"]


class AuditRuntimeState:
    """Small application-owned view of the durable audit sink lifecycle.

    Audit remains non-authoritative for an individual Agent response, but a
    process that has lost an event can no longer claim complete operational
    audit coverage.  The degraded state therefore latches until process
    restart and is exposed only as a bounded readiness status.
    """

    def __init__(self, sink: BoundedAsyncAuditSink | None) -> None:
        self._sink = sink
        self._lock = Lock()
        self._started = False
        self._start_failed = False
        self._shutdown_drained: bool | None = None
        self._degraded_latched = False

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    @property
    def shutdown_drained(self) -> bool | None:
        with self._lock:
            return self._shutdown_drained

    def start(self) -> None:
        if self._sink is None:
            return
        try:
            self._sink.start()
        except Exception:
            with self._lock:
                self._start_failed = True
                self._degraded_latched = True
            raise
        with self._lock:
            self._started = True

    def close(
        self,
        *,
        timeout_seconds: float,
        upstream_drained: bool = True,
    ) -> bool:
        if self._sink is None:
            with self._lock:
                self._shutdown_drained = upstream_drained
                if not upstream_drained:
                    self._degraded_latched = True
            return upstream_drained
        try:
            sink_drained = self._sink.close(timeout_seconds=timeout_seconds)
        except Exception:  # noqa: BLE001 - readiness must not expose sink details
            sink_drained = False
        drained = upstream_drained and sink_drained
        with self._lock:
            self._shutdown_drained = drained
            if not drained:
                self._degraded_latched = True
        return drained

    def status(self) -> AuditRuntimeStatus:
        if self._sink is None:
            return "disabled"
        with self._lock:
            if (
                self._start_failed
                or not self._started
                or self._shutdown_drained is False
                or self._degraded_latched
            ):
                return "degraded"
        try:
            snapshot = self._sink.snapshot()
        except Exception:  # noqa: BLE001 - readiness must not expose sink details
            self._latch_degraded()
            return "degraded"
        if (
            not snapshot.started
            or not snapshot.accepting
            or snapshot.closed
            or not snapshot.worker_alive
            or snapshot.dropped_event_count > 0
            or snapshot.downstream_failure_count > 0
            or snapshot.flush_failure_count > 0
            or snapshot.sink_failure_count > 0
            or (
                snapshot.pending_event_count > 0
                and snapshot.no_progress_age_seconds is not None
                and snapshot.no_progress_age_seconds >= snapshot.stall_timeout_seconds
            )
        ):
            self._latch_degraded()
            return "degraded"
        return "ok"

    def _latch_degraded(self) -> None:
        with self._lock:
            self._degraded_latched = True


__all__ = ["AuditRuntimeState", "AuditRuntimeStatus"]
