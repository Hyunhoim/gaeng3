from __future__ import annotations

from dataclasses import dataclass

from finance_agent_core.agent.providers.hyperclova import HyperClovaXCallRecord
from finance_agent_core.observability import (
    BoundedAsyncAuditSink,
    MetricCounter,
    current_request_audit,
)

_OUTCOME_COUNTERS = {
    "success": MetricCounter.HCLX_SUCCESSES,
    "authentication_error": MetricCounter.HCLX_AUTHENTICATION_FAILURES,
    "rate_limited": MetricCounter.HCLX_RATE_LIMITS,
    "service_error": MetricCounter.HCLX_SERVICE_FAILURES,
    "timeout": MetricCounter.HCLX_TIMEOUTS,
    "transport_error": MetricCounter.HCLX_TRANSPORT_FAILURES,
    "response_error": MetricCounter.HCLX_RESPONSE_FAILURES,
}


@dataclass(frozen=True, slots=True)
class HyperClovaXCallObserver:
    """Record only bounded HCX outcome and usage metrics on the approved Audit sink."""

    expected_audit_sink: BoundedAsyncAuditSink

    def __call__(self, record: HyperClovaXCallRecord) -> None:
        recorder = current_request_audit()
        if recorder is None or recorder.sink is not self.expected_audit_sink:
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return
        self._safe_increment(_OUTCOME_COUNTERS[record.outcome])
        if record.usage is None:
            return
        self._safe_increment(MetricCounter.HCLX_INPUT_TOKENS, record.usage.input_tokens)
        self._safe_increment(MetricCounter.HCLX_OUTPUT_TOKENS, record.usage.output_tokens)

    def _safe_increment(self, counter: MetricCounter, amount: int = 1) -> None:
        try:
            self.expected_audit_sink.metrics.increment(counter, amount)
        except Exception:  # noqa: BLE001 - metrics never acquire response authority
            return


__all__ = ["HyperClovaXCallObserver"]
