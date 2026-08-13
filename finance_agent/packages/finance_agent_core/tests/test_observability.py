from __future__ import annotations

import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.observability import (
    AppendOnlyJsonlAuditSink,
    AuditEvent,
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    BoundedMetrics,
    FaultTolerantAuditSink,
    InMemoryAuditSink,
    MetricCounter,
    NullAuditSink,
    RequestAuditRecorder,
    assert_safe_audit_payload,
    observe_call,
    sha256_text,
)

_REQUEST_ID = "official-request-007"
_QUESTION = "총보수가 낮은 미국 ETF를 찾아줘"
_API_KEY = "nv-live-secret-should-never-appear"
_AUTHORIZATION = f"Bearer {_API_KEY}"
_DATABASE_PATH = "/srv/private/approved/overseas_etp.sqlite3"
_BLIND_GOLD = "external-blind-gold: product_id=SECRET:ETF"
_CHAIN_OF_THOUGHT = "chain of thought: hidden reasoning tokens"
_AGENT_RELEASE_ID = "agent-release-2026-08-13-v1"
_DATASET_RELEASE_ID = "approved-dataset-2026-07-11-v1"
_INVOCATION_ID = "de305d54-75b4-431b-adb2-eb6b9e546014"
_PRODUCT_IDS = ("ETF:US:AAA", "ETF:US:BBB", "ETF:US:CCC")
_EVIDENCE_IDS = tuple(f"ev-{index}" for index in range(9))


def _event(
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
    duration_ms: float = 12.5,
) -> AuditEvent:
    return AuditEvent.redacted(
        stage=AuditStage.SCHEMA_LINK_SHADOW,
        outcome=outcome,
        reason_code="shadow_observed",
        duration_ms=duration_ms,
        request_id=_REQUEST_ID,
        question=_QUESTION,
        invocation_id=_INVOCATION_ID,
        event_sequence=7,
        observed_at_utc=datetime(2026, 8, 13, tzinfo=UTC),
        route_disposition=RouteDisposition.EXECUTE,
        interaction_intent=InteractionIntent.SEARCH,
        product_families=(ProductFamily.OVERSEAS_ETP,),
        agent_release_id=_AGENT_RELEASE_ID,
        agent_release_manifest_sha256="a" * 64,
        deployment_binding_sha256="b" * 64,
        release_context_sha256="c" * 64,
        dataset_release_id=_DATASET_RELEASE_ID,
        approved_dataset_manifest_sha256="d" * 64,
        database_manifest_sha256="e" * 64,
        database_snapshot_sha256="f" * 64,
        source_snapshot_sha256="a" * 64,
        plan_sha256="b" * 64,
        plan_bundle_sha256="e" * 64,
        dataset_bundle_sha256="f" * 64,
        model_revision="5617a9f61b028005a4858fdac845db406aefb181",
        model_snapshot_manifest_sha256="c" * 64,
        index_manifest_sha256="d" * 64,
        candidate_count=8,
        result_count=3,
        evidence_count=9,
        shadow_candidate_count=10,
        product_ids=_PRODUCT_IDS,
        evidence_ids=_EVIDENCE_IDS,
    )


def test_audit_event_contains_only_redacted_hashes_counts_and_codes() -> None:
    event = _event()
    payload = event.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False)

    assert event.request_id_sha256 == sha256_text(_REQUEST_ID)
    assert event.question_sha256 == sha256_text(_QUESTION)
    assert event.invocation_id_sha256 == sha256_text(_INVOCATION_ID)
    assert event.event_sequence == 7
    assert event.agent_release_id_sha256 == sha256_text(_AGENT_RELEASE_ID)
    assert event.dataset_release_id_sha256 == sha256_text(_DATASET_RELEASE_ID)
    assert event.product_id_sha256s == tuple(sha256_text(item) for item in _PRODUCT_IDS)
    assert event.evidence_id_sha256s == tuple(sha256_text(item) for item in _EVIDENCE_IDS)
    assert event.model_revision_sha256 == sha256_text("5617a9f61b028005a4858fdac845db406aefb181")
    assert set(payload) == {
        "schema_version",
        "observed_at_utc",
        "stage",
        "outcome",
        "reason_code",
        "duration_ms",
        "request_id_sha256",
        "question_sha256",
        "invocation_id_sha256",
        "event_sequence",
        "route_disposition",
        "interaction_intent",
        "product_families",
        "agent_release_id_sha256",
        "agent_release_manifest_sha256",
        "deployment_binding_sha256",
        "release_context_sha256",
        "dataset_release_id_sha256",
        "approved_dataset_manifest_sha256",
        "database_manifest_sha256",
        "database_snapshot_sha256",
        "source_snapshot_sha256",
        "plan_sha256",
        "plan_bundle_sha256",
        "dataset_bundle_sha256",
        "model_revision_sha256",
        "model_snapshot_manifest_sha256",
        "index_manifest_sha256",
        "product_family_count",
        "candidate_count",
        "result_count",
        "evidence_count",
        "shadow_candidate_count",
        "product_id_sha256s",
        "evidence_id_sha256s",
    }
    for protected in (
        _REQUEST_ID,
        _QUESTION,
        _INVOCATION_ID,
        _API_KEY,
        _AUTHORIZATION,
        _DATABASE_PATH,
        _BLIND_GOLD,
        _CHAIN_OF_THOUGHT,
        _AGENT_RELEASE_ID,
        _DATASET_RELEASE_ID,
        *_PRODUCT_IDS,
        *_EVIDENCE_IDS,
    ):
        assert protected not in serialized


def test_audit_event_safe_route_and_family_codes_reconstruct_request_path() -> None:
    event = _event()

    assert event.route_disposition is RouteDisposition.EXECUTE
    assert event.interaction_intent is InteractionIntent.SEARCH
    assert event.product_families == (ProductFamily.OVERSEAS_ETP,)
    assert event.product_family_count == 1


def test_audit_event_normalizes_timezone_to_utc() -> None:
    event = AuditEvent.redacted(
        stage=AuditStage.SAFETY,
        outcome=AuditOutcome.BLOCKED,
        reason_code="policy_blocked",
        duration_ms=0,
        request_id=_REQUEST_ID,
        question=_QUESTION,
        observed_at_utc=datetime.fromisoformat("2026-08-13T09:00:00+09:00"),
    )

    assert event.observed_at_utc == datetime(2026, 8, 13, tzinfo=UTC)


def test_audit_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        AuditEvent.redacted(
            stage=AuditStage.SAFETY,
            outcome=AuditOutcome.BLOCKED,
            reason_code="policy_blocked",
            duration_ms=0,
            request_id=_REQUEST_ID,
            question=_QUESTION,
            observed_at_utc=datetime(2026, 8, 13),
        )


@pytest.mark.parametrize(
    "field",
    [
        "question",
        "raw_question",
        "invocation_id",
        "prompt",
        "answer",
        "headers",
        "authorization",
        "api_key",
        "database_path",
        "agent_release_id",
        "dataset_release_id",
        "product_ids",
        "evidence_ids",
        "blind_gold",
        "chain_of_thought",
        "cot",
    ],
)
def test_audit_event_schema_rejects_sensitive_and_open_ended_fields(field: str) -> None:
    payload = _event().model_dump(mode="python")
    payload[field] = "protected material"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuditEvent.model_validate(payload)
    with pytest.raises(ValueError, match="forbidden fields"):
        assert_safe_audit_payload(payload)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("agent_release_manifest_sha256", "agent release linkage"),
        ("database_snapshot_sha256", "dataset linkage"),
    ],
)
def test_audit_event_rejects_partial_release_or_dataset_linkage(
    field: str,
    message: str,
) -> None:
    payload = _event().model_dump(mode="python")
    payload[field] = None

    with pytest.raises(ValidationError, match=message):
        AuditEvent.model_validate(payload)


def test_audit_event_correlation_fields_are_paired_and_legacy_v11_remains_readable() -> None:
    payload = _event().model_dump(mode="python")
    payload["event_sequence"] = None
    with pytest.raises(ValidationError, match="present together"):
        AuditEvent.model_validate(payload)

    legacy_payload = _event().model_dump(mode="python")
    legacy_payload.pop("invocation_id_sha256")
    legacy_payload.pop("event_sequence")
    legacy = AuditEvent.model_validate(legacy_payload)
    assert legacy.schema_version == "1.1"
    assert legacy.invocation_id_sha256 is None
    assert legacy.event_sequence is None

    with pytest.raises(ValueError, match="invocation_id"):
        AuditEvent.redacted(
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.STARTED,
            reason_code="request_started",
            duration_ms=0,
            request_id="",
            question="",
            invocation_id="",
        )


def test_request_recorder_enrichment_shares_invocation_and_monotonic_enqueue_order() -> None:
    memory = InMemoryAuditSink(max_events=100)
    sink = BoundedAsyncAuditSink(memory, queue_capacity=100)
    middleware = RequestAuditRecorder(request_id="", question="", sink=sink)

    assert middleware.emit(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.STARTED,
        reason_code="request_started",
        duration_ms=0,
    )
    enriched = middleware.with_request(request_id=_REQUEST_ID, question=_QUESTION)
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(
            executor.map(
                lambda _index: enriched.emit(
                    stage=AuditStage.LEXICAL,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="lexical_completed",
                    duration_ms=1,
                ),
                range(32),
            )
        )

    assert all(accepted)
    assert sink.close(timeout_seconds=2)
    events = memory.snapshot()
    assert [event.event_sequence for event in events] == list(range(1, 34))
    assert {event.invocation_id_sha256 for event in events} == {middleware.invocation_id_sha256}
    assert enriched.invocation_id_sha256 == middleware.invocation_id_sha256
    assert middleware.last_event_sequence == enriched.last_event_sequence == 33
    assert _REQUEST_ID not in repr(enriched)
    assert _QUESTION not in repr(enriched)
    assert events[0].request_id_sha256 == sha256_text("")
    assert all(event.request_id_sha256 == sha256_text(_REQUEST_ID) for event in events[1:])


def test_separate_recorders_receive_distinct_server_invocations() -> None:
    sink = BoundedAsyncAuditSink(NullAuditSink())
    first = RequestAuditRecorder(request_id=_REQUEST_ID, question=_QUESTION, sink=sink)
    second = RequestAuditRecorder(request_id=_REQUEST_ID, question=_QUESTION, sink=sink)

    assert first.invocation_id_sha256 != second.invocation_id_sha256
    assert sink.close(timeout_seconds=1)


def test_audit_event_rejects_inconsistent_route_family_and_plan_linkage() -> None:
    payload = _event().model_dump(mode="python")
    payload["interaction_intent"] = None
    with pytest.raises(ValidationError, match="present together"):
        AuditEvent.model_validate(payload)

    payload = _event().model_dump(mode="python")
    payload["product_family_count"] = 0
    with pytest.raises(ValidationError, match="must match"):
        AuditEvent.model_validate(payload)

    payload = _event().model_dump(mode="python")
    payload["product_families"] = (
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.OVERSEAS_ETP,
    )
    payload["product_family_count"] = 2
    with pytest.raises(ValidationError, match="must be unique"):
        AuditEvent.model_validate(payload)

    payload = _event().model_dump(mode="python")
    payload["route_disposition"] = RouteDisposition.UNSUPPORTED
    payload["interaction_intent"] = InteractionIntent.UNSUPPORTED
    with pytest.raises(ValidationError, match="cannot link an executable plan"):
        AuditEvent.model_validate(payload)


def test_audit_event_rejects_inconsistent_and_duplicate_hashed_result_links() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AuditEvent.redacted(
            stage=AuditStage.ANSWER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="answer_ready",
            duration_ms=1,
            request_id=_REQUEST_ID,
            question=_QUESTION,
            candidate_count=2,
            result_count=2,
            product_ids=("duplicate", "duplicate"),
        )

    payload = _event().model_dump(mode="python")
    payload["product_id_sha256s"] = payload["product_id_sha256s"][:-1]
    with pytest.raises(ValidationError, match="product linkage count"):
        AuditEvent.model_validate(payload)

    payload = _event().model_dump(mode="python")
    payload["evidence_id_sha256s"] = payload["evidence_id_sha256s"][:-1]
    with pytest.raises(ValidationError, match="evidence linkage count"):
        AuditEvent.model_validate(payload)

    payload = _event().model_dump(mode="python")
    payload["candidate_count"] = 2
    with pytest.raises(ValidationError, match="cannot exceed"):
        AuditEvent.model_validate(payload)


def test_audit_event_enforces_bounded_product_and_evidence_hash_tuples() -> None:
    with pytest.raises(ValidationError, match="at most 100 items"):
        AuditEvent.redacted(
            stage=AuditStage.ANSWER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="answer_ready",
            duration_ms=1,
            request_id=_REQUEST_ID,
            question=_QUESTION,
            candidate_count=101,
            result_count=101,
            product_ids=tuple(f"product-{index}" for index in range(101)),
        )

    with pytest.raises(ValidationError, match="at most 2000 items"):
        AuditEvent.redacted(
            stage=AuditStage.ANSWER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="answer_ready",
            duration_ms=1,
            request_id=_REQUEST_ID,
            question=_QUESTION,
            evidence_count=2_001,
            evidence_ids=tuple(f"evidence-{index}" for index in range(2_001)),
        )


@pytest.mark.parametrize(
    "reason_code",
    [
        "raw_question",
        "blind_gold_match",
        "chain_of_thought_recorded",
        "authorization_header",
        "database_path_error",
        "api_key_failure",
    ],
)
def test_reason_code_rejects_sensitive_channels(reason_code: str) -> None:
    with pytest.raises(ValidationError, match="non-sensitive"):
        AuditEvent.redacted(
            stage=AuditStage.ROUTE,
            outcome=AuditOutcome.FAILED,
            reason_code=reason_code,
            duration_ms=1,
            request_id=_REQUEST_ID,
            question=_QUESTION,
        )


@pytest.mark.parametrize(
    "leaked_value",
    [_AUTHORIZATION, f"api_key={_API_KEY}", _DATABASE_PATH],
)
def test_generic_payload_guard_rejects_secret_headers_and_database_paths(
    leaked_value: str,
) -> None:
    payload = _event().model_dump(mode="python")
    payload["reason_code"] = leaked_value

    with pytest.raises(ValueError, match="secret-like or path-like"):
        assert_safe_audit_payload(payload)


def test_in_memory_sink_is_bounded_detached_and_thread_safe() -> None:
    sink = InMemoryAuditSink(max_events=5)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: sink.emit(_event(duration_ms=float(index))), range(20)))

    snapshot = sink.snapshot()
    assert len(snapshot) == 5
    assert all(isinstance(event, AuditEvent) for event in snapshot)
    assert snapshot == sink.snapshot()


def test_bounded_metrics_reports_counters_percentiles_and_gauges() -> None:
    metrics = BoundedMetrics(max_latency_samples_per_stage=4)
    for latency in (1.0, 2.0, 3.0, 4.0, 100.0):
        metrics.observe_event(_event(duration_ms=latency))
    metrics.increment(MetricCounter.SHADOW_OBSERVATIONS, 5)
    metrics.set_gauges(queue_depth=7, inflight=3)
    metrics.set_gauges(queue_depth=1, inflight=1)

    snapshot = metrics.snapshot()
    latency = snapshot.latency_by_stage[AuditStage.SCHEMA_LINK_SHADOW.value]
    assert snapshot.counters["audit_events_total"] == 5
    assert snapshot.counters["shadow_observations_total"] == 5
    assert latency.sample_count == 4
    assert latency.p50_ms == 3.5
    assert latency.p95_ms == 85.6
    assert latency.p99_ms == 97.12
    assert snapshot.queue_depth == snapshot.inflight == 1
    assert snapshot.peak_queue_depth == 7
    assert snapshot.peak_inflight == 3


def test_metrics_cover_all_roadmap_stage_boundaries_and_bounded_counters() -> None:
    metrics = BoundedMetrics(max_latency_samples_per_stage=2)
    expected_stages = set(AuditStage)
    for stage in expected_stages:
        metrics.observe_event(_event().model_copy(update={"stage": stage}))
    for counter in MetricCounter:
        metrics.increment(counter)

    snapshot = metrics.snapshot()
    assert set(snapshot.latency_by_stage) == {stage.value for stage in expected_stages}
    for stage in expected_stages:
        assert snapshot.counters[f"stage.{stage.value}.total"] == 1
    for counter in MetricCounter:
        assert snapshot.counters[counter.value] == 1


def test_metrics_reject_unbounded_counter_labels() -> None:
    metrics = BoundedMetrics()

    with pytest.raises(ValueError, match="outside the bounded metrics contract"):
        metrics.increment("tenant.user.question.dynamic-label")


def test_fault_tolerant_sink_failure_changes_only_failure_counter() -> None:
    class BrokenSink:
        @staticmethod
        def emit(_event: AuditEvent) -> None:
            raise RuntimeError(f"sink failure {_API_KEY} {_DATABASE_PATH}")

    metrics = BoundedMetrics()
    sink = FaultTolerantAuditSink(BrokenSink(), metrics)

    assert sink.emit(_event()) is False
    snapshot = metrics.snapshot()
    assert "audit_events_total" not in snapshot.counters
    assert snapshot.counters["audit_sink_failures_total"] == 1
    assert snapshot.counters["audit_downstream_failures_total"] == 1
    assert _API_KEY not in snapshot.model_dump_json()
    assert _DATABASE_PATH not in snapshot.model_dump_json()


def test_fault_tolerant_sink_isolates_event_factory_failure() -> None:
    sink = FaultTolerantAuditSink(NullAuditSink())

    def invalid_event() -> AuditEvent:
        raise ValueError("invalid telemetry adapter input")

    assert sink.emit_lazy(invalid_event) is False
    assert sink.metrics.snapshot().counters == {"audit_sink_failures_total": 1}


def test_observe_call_preserves_result_when_sink_fails() -> None:
    class BrokenSink:
        @staticmethod
        def emit(_event: AuditEvent) -> None:
            raise OSError("telemetry unavailable")

    sink = FaultTolerantAuditSink(BrokenSink())

    result = observe_call(
        lambda: {"status": "executed", "products": ("A", "B")},
        sink=sink,
        event_factory=lambda outcome, duration: _event(
            outcome=outcome,
            duration_ms=duration,
        ),
    )

    assert result == {"status": "executed", "products": ("A", "B")}
    assert sink.metrics.snapshot().counters["audit_sink_failures_total"] == 1


def test_observe_call_preserves_result_when_event_factory_fails() -> None:
    sink = FaultTolerantAuditSink(NullAuditSink())

    def invalid_factory(_outcome: AuditOutcome, _duration: float) -> AuditEvent:
        raise ValueError("invalid telemetry adapter input")

    result = observe_call(
        lambda: "unchanged Agent result",
        sink=sink,
        event_factory=invalid_factory,
    )

    assert result == "unchanged Agent result"
    assert sink.metrics.snapshot().counters == {"audit_sink_failures_total": 1}


def test_observe_call_preserves_original_agent_exception_when_sink_fails() -> None:
    class BrokenSink:
        @staticmethod
        def emit(_event: AuditEvent) -> None:
            raise OSError("telemetry unavailable")

    sink = FaultTolerantAuditSink(BrokenSink())

    def fail_agent() -> None:
        raise LookupError("original Agent failure")

    with pytest.raises(LookupError, match="original Agent failure"):
        observe_call(
            fail_agent,
            sink=sink,
            event_factory=lambda outcome, duration: _event(
                outcome=outcome,
                duration_ms=duration,
            ),
        )
    snapshot = sink.metrics.snapshot()
    assert "outcome.failed.total" not in snapshot.counters
    assert snapshot.counters["audit_sink_failures_total"] == 1
    assert snapshot.counters["audit_downstream_failures_total"] == 1


def test_null_sink_accepts_redacted_events_without_state() -> None:
    sink = FaultTolerantAuditSink(NullAuditSink())

    assert sink.emit(_event()) is True
    assert sink.metrics.snapshot().counters == {
        "audit_events_total": 1,
        "outcome.succeeded.total": 1,
        "stage.schema_link_shadow.total": 1,
    }


def test_bounded_async_sink_drains_events_and_isolates_slow_storage() -> None:
    import time

    class SlowSink:
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []

        def emit(self, event: AuditEvent) -> None:
            time.sleep(0.05)
            self.events.append(event)

    downstream = SlowSink()
    sink = BoundedAsyncAuditSink(downstream, queue_capacity=4)
    started = time.perf_counter()

    assert sink.emit(_event()) is True
    elapsed = time.perf_counter() - started
    assert elapsed < 0.04
    assert sink.close(timeout_seconds=1) is True
    assert len(downstream.events) == 1


def test_append_only_jsonl_sink_preserves_existing_records_and_redaction(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = AppendOnlyJsonlAuditSink(path, fsync_each_event=False)
    first.emit(_event(duration_ms=1))
    first.close()
    size_before = path.stat().st_size

    second = AppendOnlyJsonlAuditSink(path, fsync_each_event=False)
    second.emit(_event(duration_ms=2))
    second.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert path.stat().st_size > size_before
    serialized = "\n".join(lines)
    for protected in (_REQUEST_ID, _QUESTION, _API_KEY, _DATABASE_PATH):
        assert protected not in serialized
    assert all(AuditEvent.model_validate_json(line) for line in lines)


def test_append_only_jsonl_sink_preserves_existing_empty_file(tmp_path) -> None:
    path = tmp_path / "audit-empty.jsonl"
    path.touch(mode=0o600)

    sink = AppendOnlyJsonlAuditSink(path, fsync_each_event=False)
    sink.emit(_event())
    sink.close()

    lines = path.read_bytes().splitlines()
    assert len(lines) == 1
    assert AuditEvent.model_validate_json(lines[0])


@pytest.mark.parametrize(
    ("tail", "expected_error"),
    [
        (
            _event().model_dump_json().encode("utf-8"),
            "incomplete final record",
        ),
        (
            b'{"schema_version":"1.1","raw_question":"PRIVATE-TAIL"}\n',
            "invalid final record",
        ),
        (b"\n", "invalid final record"),
    ],
)
def test_append_only_jsonl_sink_rejects_partial_or_invalid_existing_tail_without_leak(
    tmp_path,
    tail: bytes,
    expected_error: str,
) -> None:
    path = tmp_path / "audit-invalid-tail.jsonl"
    path.write_bytes(tail)
    path.chmod(0o600)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match=expected_error) as captured:
        AppendOnlyJsonlAuditSink(path, fsync_each_event=False)

    assert "PRIVATE-TAIL" not in str(captured.value)
    assert path.read_bytes() == before


def test_append_only_jsonl_sink_rejects_oversized_tail_with_bounded_read(
    tmp_path,
    monkeypatch,
) -> None:
    import finance_agent_core.observability as observability

    path = tmp_path / "audit-oversized-tail.jsonl"
    path.write_bytes(b"{" + b"x" * (observability._MAX_AUDIT_EVENT_BYTES + 100) + b"}\n")
    path.chmod(0o600)
    original_pread = observability.os.pread
    requested_sizes: list[int] = []

    def recording_pread(descriptor: int, size: int, offset: int) -> bytes:
        requested_sizes.append(size)
        return original_pread(descriptor, size, offset)

    monkeypatch.setattr(observability.os, "pread", recording_pread)

    with pytest.raises(RuntimeError, match="exceeds the size limit"):
        AppendOnlyJsonlAuditSink(path, fsync_each_event=False)

    assert requested_sizes
    assert max(requested_sizes) <= observability._MAX_AUDIT_EVENT_BYTES + 1


def test_append_only_jsonl_sink_rejects_path_replacement_during_tail_validation(
    tmp_path,
    monkeypatch,
) -> None:
    import finance_agent_core.observability as observability

    path = tmp_path / "audit-replaced-tail.jsonl"
    path.write_bytes(f"{_event().model_dump_json()}\n".encode())
    path.chmod(0o600)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(f"{_event(duration_ms=2).model_dump_json()}\n".encode())
    replacement.chmod(0o600)
    original_pread = observability.os.pread
    replaced = False

    def replacing_pread(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal replaced
        observed = original_pread(descriptor, size, offset)
        if not replaced:
            replaced = True
            replacement.replace(path)
        return observed

    monkeypatch.setattr(observability.os, "pread", replacing_pread)

    with pytest.raises(RuntimeError, match="changed during startup validation"):
        AppendOnlyJsonlAuditSink(path, fsync_each_event=False)

    assert replaced is True


def test_append_only_jsonl_sink_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(target)

    with pytest.raises(OSError):
        AppendOnlyJsonlAuditSink(alias)


def test_append_only_jsonl_sink_retries_short_and_interrupted_writes(
    tmp_path,
    monkeypatch,
) -> None:
    import finance_agent_core.observability as observability

    path = tmp_path / "audit-short-write.jsonl"
    sink = AppendOnlyJsonlAuditSink(path, fsync_each_event=False)
    original_write = observability.os.write
    calls = 0

    def short_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError
        return original_write(descriptor, payload[:17])

    monkeypatch.setattr(observability.os, "write", short_write)
    sink.emit(_event())
    sink.close()

    assert calls > 2
    AuditEvent.model_validate_json(path.read_text(encoding="utf-8").strip())


def test_append_only_jsonl_sink_fsyncs_parent_for_new_file(
    tmp_path,
    monkeypatch,
) -> None:
    import finance_agent_core.observability as observability

    path = tmp_path / "audit-new-file.jsonl"
    original_fsync = observability.os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(observability.os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(observability.os, "fsync", recording_fsync)
    sink = AppendOnlyJsonlAuditSink(path, fsync_each_event=True)
    sink.emit(_event())
    sink.close()

    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)


def test_async_snapshot_exposes_queue_drop_without_payloads() -> None:
    sink = BoundedAsyncAuditSink(NullAuditSink(), queue_capacity=1, start_worker=False)

    assert sink.emit(_event()) is False
    before_close = sink.snapshot()
    assert before_close.dropped_event_count == 1
    assert before_close.accepted_event_count == 0
    assert before_close.successful_downstream_emit_count == 0
    assert before_close.queue_depth == 0
    assert _QUESTION not in before_close.model_dump_json()
    assert _REQUEST_ID not in before_close.model_dump_json()

    assert sink.close(timeout_seconds=1) is False
    after_close = sink.snapshot()
    assert after_close.flush_completed is True
    assert after_close.flush_succeeded is False
    assert after_close.flush_failure_count == 1


def test_async_snapshot_exposes_payload_free_monotonic_progress_for_stall_detection() -> None:
    class ManualClock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    class BlockingSink:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def emit(self, _event: AuditEvent) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)

    clock = ManualClock()
    downstream = BlockingSink()
    sink = BoundedAsyncAuditSink(
        downstream,
        queue_capacity=2,
        stall_timeout_seconds=5.0,
        monotonic_clock=clock,
    )
    assert sink.emit(_event()) is True
    assert downstream.entered.wait(timeout=1)

    initial = sink.snapshot()
    assert initial.worker_alive is True
    assert initial.pending_event_count == 1
    assert initial.oldest_pending_age_seconds == 0
    assert initial.no_progress_age_seconds == 0
    assert initial.stall_timeout_seconds == 5
    assert initial.downstream_progress_count == 0

    clock.advance(4.999)
    below_threshold = sink.snapshot()
    assert below_threshold.oldest_pending_age_seconds == pytest.approx(4.999)
    assert below_threshold.no_progress_age_seconds == pytest.approx(4.999)
    serialized = below_threshold.model_dump_json()
    assert _QUESTION not in serialized
    assert _REQUEST_ID not in serialized

    downstream.release.set()
    assert sink.close(timeout_seconds=1) is True
    completed = sink.snapshot()
    assert completed.pending_event_count == 0
    assert completed.oldest_pending_age_seconds is None
    assert completed.no_progress_age_seconds is None
    assert completed.downstream_progress_count == 1


def test_async_close_uses_one_deadline_when_queue_is_full_and_can_retry() -> None:
    class BlockingSink:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.events: list[AuditEvent] = []
            self.close_count = 0

        def emit(self, event: AuditEvent) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)
            self.events.append(event)

        def close(self) -> None:
            self.close_count += 1

    downstream = BlockingSink()
    sink = BoundedAsyncAuditSink(downstream, queue_capacity=1)
    assert sink.emit(_event(duration_ms=1))
    assert downstream.entered.wait(timeout=1)
    assert sink.emit(_event(duration_ms=2))

    started = time.monotonic()
    assert sink.close(timeout_seconds=0.05) is False
    elapsed = time.monotonic() - started
    timed_out = sink.snapshot()
    assert elapsed < 0.12
    assert timed_out.sentinel_enqueued is False
    assert timed_out.flush_completed is False
    assert timed_out.flush_failure_count == 1
    assert timed_out.queue_depth == 1

    downstream.release.set()
    assert sink.close(timeout_seconds=1) is True
    completed = sink.snapshot()
    assert completed.sentinel_enqueued is True
    assert completed.flush_completed is True
    assert completed.flush_succeeded is True
    assert completed.accepted_event_count == 2
    assert completed.successful_downstream_emit_count == 2
    assert completed.queue_depth == 0
    assert downstream.close_count == 1


def test_async_close_enqueues_sentinel_once_across_join_timeout_and_retry() -> None:
    class BlockingSink:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.close_count = 0

        def emit(self, _event: AuditEvent) -> None:
            self.entered.set()
            assert self.release.wait(timeout=2)

        def close(self) -> None:
            self.close_count += 1

    downstream = BlockingSink()
    sink = BoundedAsyncAuditSink(downstream, queue_capacity=2)
    assert sink.emit(_event())
    assert downstream.entered.wait(timeout=1)

    started = time.monotonic()
    assert sink.close(timeout_seconds=0.05) is False
    assert time.monotonic() - started < 0.12
    timed_out = sink.snapshot()
    assert timed_out.sentinel_enqueued is True
    assert timed_out.flush_completed is False

    downstream.release.set()
    assert sink.close(timeout_seconds=1) is True
    assert sink.close(timeout_seconds=1) is True
    completed = sink.snapshot()
    assert completed.sentinel_enqueued is True
    assert completed.queue_depth == 0
    assert downstream.close_count == 1


def test_async_snapshot_distinguishes_downstream_and_flush_failure() -> None:
    class BrokenSink:
        @staticmethod
        def emit(_event: AuditEvent) -> None:
            raise OSError("durable append unavailable")

    sink = BoundedAsyncAuditSink(BrokenSink(), queue_capacity=2)
    assert sink.emit(_event())
    assert sink.close(timeout_seconds=1) is False

    snapshot = sink.snapshot()
    assert snapshot.accepted_event_count == 1
    assert snapshot.successful_downstream_emit_count == 0
    assert snapshot.dropped_event_count == 0
    assert snapshot.downstream_failure_count == 1
    assert snapshot.sink_failure_count == 1
    assert snapshot.flush_failure_count == 1
    assert snapshot.flush_completed is True
    assert snapshot.flush_succeeded is False
