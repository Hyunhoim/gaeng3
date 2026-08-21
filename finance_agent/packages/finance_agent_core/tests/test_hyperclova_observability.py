from __future__ import annotations

from finance_agent_core.agent.providers import (
    HyperClovaXCallObserver,
    HyperClovaXCallRecord,
    HyperClovaXTokenUsage,
)
from finance_agent_core.observability import (
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    MetricCounter,
    RequestAuditRecorder,
    bind_request_audit,
)


def _sink() -> BoundedAsyncAuditSink:
    return BoundedAsyncAuditSink(InMemoryAuditSink(max_events=32), queue_capacity=32)


def _record(
    outcome: str,
    *,
    usage: HyperClovaXTokenUsage | None = None,
) -> HyperClovaXCallRecord:
    return HyperClovaXCallRecord(
        operation="query_plan",
        model="HCX-007-PRIVATE-MODEL-SENTINEL",
        outcome=outcome,  # type: ignore[arg-type]
        status_code=429 if outcome == "rate_limited" else 200,
        latency_ms=12.5,
        request_id="private-provider-request-id",
        usage=usage,
    )


def test_hcx_call_observer_records_bounded_outcomes_and_token_totals() -> None:
    sink = _sink()
    observer = HyperClovaXCallObserver(sink)
    recorder = RequestAuditRecorder(
        request_id="private-evaluator-id",
        question="private evaluator question",
        sink=sink,
    )
    outcomes = (
        "success",
        "authentication_error",
        "rate_limited",
        "service_error",
        "timeout",
        "transport_error",
        "response_error",
    )

    with bind_request_audit(recorder):
        for outcome in outcomes:
            observer(
                _record(
                    outcome,
                    usage=(
                        HyperClovaXTokenUsage(
                            input_tokens=17,
                            output_tokens=5,
                            total_tokens=22,
                        )
                        if outcome == "success"
                        else None
                    ),
                )
            )

    counters = sink.metrics.snapshot().counters
    assert counters[MetricCounter.HCLX_SUCCESSES.value] == 1
    assert counters[MetricCounter.HCLX_AUTHENTICATION_FAILURES.value] == 1
    assert counters[MetricCounter.HCLX_RATE_LIMITS.value] == 1
    assert counters[MetricCounter.HCLX_SERVICE_FAILURES.value] == 1
    assert counters[MetricCounter.HCLX_TIMEOUTS.value] == 1
    assert counters[MetricCounter.HCLX_TRANSPORT_FAILURES.value] == 1
    assert counters[MetricCounter.HCLX_RESPONSE_FAILURES.value] == 1
    assert counters[MetricCounter.HCLX_INPUT_TOKENS.value] == 17
    assert counters[MetricCounter.HCLX_OUTPUT_TOKENS.value] == 5
    serialized = sink.metrics.snapshot().model_dump_json()
    assert "PRIVATE-MODEL-SENTINEL" not in serialized
    assert "private-provider-request-id" not in serialized
    assert "private evaluator question" not in serialized
    assert sink.close(timeout_seconds=2)


def test_hcx_call_observer_rejects_missing_or_foreign_request_audit_context() -> None:
    expected = _sink()
    foreign = _sink()
    observer = HyperClovaXCallObserver(expected)

    observer(_record("success"))
    with bind_request_audit(
        RequestAuditRecorder(request_id="foreign", question="foreign", sink=foreign)
    ):
        observer(_record("success"))

    counters = expected.metrics.snapshot().counters
    assert counters[MetricCounter.AUDIT_SINK_FAILURES.value] == 2
    assert counters.get(MetricCounter.HCLX_SUCCESSES.value, 0) == 0
    assert expected.close(timeout_seconds=2)
    assert foreign.close(timeout_seconds=2)


def test_hcx_call_observer_metric_failure_never_changes_provider_behavior(monkeypatch) -> None:
    sink = _sink()
    observer = HyperClovaXCallObserver(sink)

    def fail_increment(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("PRIVATE_METRIC_FAILURE")

    monkeypatch.setattr(sink.metrics, "increment", fail_increment)
    with bind_request_audit(
        RequestAuditRecorder(request_id="request", question="question", sink=sink)
    ):
        observer(_record("success"))

    assert sink.close(timeout_seconds=2)
