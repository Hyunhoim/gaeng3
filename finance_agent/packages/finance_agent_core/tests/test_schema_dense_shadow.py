from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from time import monotonic, perf_counter, sleep

import pytest

from finance_agent_core.agent.planning_policy import (
    PlanningDecision,
    PlanningPath,
    PlanningSemanticIssue,
    PlanningTrace,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactGateEvidence,
    load_schema_embedding_candidate_link,
)
from finance_agent_core.evaluation.schema_embedding_models import SentenceTransformerCpuProvider
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    BoundedMetrics,
    FaultTolerantAuditSink,
    InMemoryAuditSink,
    RequestAuditRecorder,
    bind_request_audit,
)
from finance_agent_core.release import ResolvedAgentRelease
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    EmbeddingProviderMetadata,
    SchemaFieldCandidate,
    build_schema_field_entries,
)
from finance_agent_core.retrieval.schema_shadow import (
    AsyncSchemaLinkShadowObserver,
    HybridSchemaLinkShadow,
    SchemaFieldCapability,
    SchemaLinkStatus,
    SchemaShadowMode,
    SchemaShadowQueueSettings,
    SchemaShadowSettings,
)


def _vector(key: str, dimension: int = 64) -> list[float]:
    chunks = []
    counter = 0
    while len(chunks) < dimension:
        digest = hashlib.sha256(f"{key}:{counter}".encode()).digest()
        chunks.extend(1.0 if byte & 1 else -1.0 for byte in digest)
        counter += 1
    return chunks[:dimension]


class _CountingEmbeddingProvider(SentenceTransformerCpuProvider):
    def __init__(self, *, query_targets: dict[str, str] | None = None) -> None:
        candidate = load_schema_embedding_candidate_link("bge-m3")
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="shadow-contract-fake",
            model_id=candidate.model_id,
            model_revision=candidate.revision,
            license_id="mit-test-only",
            dimension=64,
            pooling="mean",
        )
        self.query_targets = query_targets or {"운용 비용률": "total_expense_ratio_pct"}
        self.query_calls = 0
        self.document_calls = 0
        self._artifact_gate_evidence = _artifact_evidence()

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    @property
    def artifact_gate_evidence(self) -> SchemaEmbeddingArtifactGateEvidence:
        return self._artifact_gate_evidence

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        return [_vector(text.split(" | ", maxsplit=1)[0]) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return _vector(self.query_targets.get(text, text))


class _FailingQueryProvider(_CountingEmbeddingProvider):
    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        raise RuntimeError(f"embedding service failed for {text}")


class _BlockingQueryProvider(_CountingEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release blocking embedding provider")
        return _vector(self.query_targets.get(text, text))


def _index(provider: _CountingEmbeddingProvider) -> DenseSchemaIndex:
    return DenseSchemaIndex.build(build_schema_field_entries(), provider)


def test_current_public_release_rejects_schema_shadow_observer() -> None:
    unresolved_fixture = object.__new__(ResolvedAgentRelease)

    with pytest.raises(ValueError, match="Schema Dense shadow disabled"):
        RoutedFinanceAgent(
            {},
            release_guard=unresolved_fixture,
            require_agent_release=True,
            schema_link_shadow_observer=object(),  # type: ignore[arg-type]
        )


def test_submit_racing_shutdown_cannot_leave_an_unprocessed_trace() -> None:
    provider = _CountingEmbeddingProvider()
    worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
    )
    observer = AsyncSchemaLinkShadowObserver(worker)
    entered = threading.Event()
    release = threading.Event()
    original_should_enqueue = worker.should_enqueue

    def delayed_should_enqueue(trace: PlanningTrace) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        return original_should_enqueue(trace)

    worker.should_enqueue = delayed_should_enqueue  # type: ignore[method-assign]
    trace = _trace()
    accepted: list[bool] = []
    submitter = threading.Thread(target=lambda: accepted.append(observer.submit(trace)))
    submitter.start()
    assert entered.wait(timeout=2)
    assert observer.shutdown(timeout_seconds=1) is True
    release.set()
    submitter.join(timeout=2)

    assert accepted == [False]
    assert observer.drain(timeout_seconds=0) is True
    assert observer.is_alive is False
    assert provider.query_calls == 0


def _artifact_evidence() -> SchemaEmbeddingArtifactGateEvidence:
    return SchemaEmbeddingArtifactGateEvidence(
        mode="shadow",
        candidate=load_schema_embedding_candidate_link("bge-m3"),
        snapshot_file_manifest_sha256="a" * 64,
        manifest_file_sha256="b" * 64,
    )


def _trace(
    *,
    family: ProductFamily = ProductFamily.DOMESTIC_ETP,
    intent: InteractionIntent = InteractionIntent.SEARCH,
    span: str = "운용 비용률",
) -> PlanningTrace:
    draft = MinimalQueryDraft(
        request_id="schema-shadow-001",
        question=f"국내 ETF 중 {span} 기준으로 찾아줘",
        intent=intent,
        product_families=[family],
        product_mentions=[],
    )
    route = RouteDecision(
        draft=draft,
        disposition=RouteDisposition.EXECUTE,
        reason_code="capability_executable",
        reason="server-owned route is executable",
        query_plan_intent={
            InteractionIntent.SEARCH: Intent.SEARCH,
            InteractionIntent.DETAIL: Intent.SEARCH,
            InteractionIntent.COMPARE: Intent.COMPARE,
            InteractionIntent.AGGREGATE: Intent.AGGREGATE,
            InteractionIntent.EXPLAIN: Intent.SEARCH,
        }[intent],
        capability_matrix_version="1.0",
    )
    planning = PlanningDecision(
        path=PlanningPath.SCHEMA_LINK_SHADOW,
        semantic_issue=PlanningSemanticIssue.SCHEMA_LINK_GAP,
        unresolved_spans=(span,),
        product_families=(family,),
        route_reason_code=route.reason_code,
        reason_code="schema_link_gap_observed",
    )
    return PlanningTrace(route_decision=route, planning_decision=planning)


def _shadow(
    provider: _CountingEmbeddingProvider,
    **settings: float | int,
) -> HybridSchemaLinkShadow:
    return HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW, **settings),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
    )


def test_shadow_exact_cosine_returns_only_canonical_capable_fields_with_frozen_ids() -> None:
    provider = _CountingEmbeddingProvider()
    observer = _shadow(provider)

    decision = observer.observe(_trace())[0]

    assert decision.status is SchemaLinkStatus.FOUND
    assert decision.candidates[0].field_id == "total_expense_ratio_pct"
    assert decision.candidates[0].product_family is ProductFamily.DOMESTIC_ETP
    assert decision.candidates[0].dense_score == pytest.approx(1.0)
    assert SchemaFieldCapability.QUERYABLE in decision.candidates[0].capabilities
    assert [candidate.fused_rank for candidate in decision.candidates] == list(
        range(1, len(decision.candidates) + 1)
    )
    assert decision.field_registry_schema_version
    assert decision.field_registry_sha256
    assert decision.index_manifest_id
    canonical_provider = json.dumps(
        provider.metadata.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert decision.provider_manifest_id == hashlib.sha256(canonical_provider).hexdigest()
    assert decision.model_snapshot_manifest_id == "a" * 64
    assert decision.unresolved_span_sha256 == hashlib.sha256("운용 비용률".encode()).hexdigest()
    assert "운용 비용률" not in decision.model_dump_json()
    assert provider.query_calls == 1


def test_lexical_first_disagreement_is_a_non_authoritative_conflict() -> None:
    provider = _CountingEmbeddingProvider(query_targets={"보수율": "aum"})
    observer = _shadow(provider)

    decision = observer.observe(_trace(span="보수율"))[0]

    assert decision.status is SchemaLinkStatus.CONFLICT
    assert decision.reason_code == "shadow_lexical_dense_conflict"
    assert decision.candidates[0].field_id == "total_expense_ratio_pct"
    assert decision.candidates[0].lexical_rank == 1
    assert provider.query_calls == 1


def test_low_margin_abstains_instead_of_asserting_a_field() -> None:
    provider = _CountingEmbeddingProvider()
    observer = _shadow(provider, minimum_margin=2.0)

    decision = observer.observe(_trace())[0]

    assert decision.status is SchemaLinkStatus.ABSTAIN
    assert decision.reason_code == "shadow_low_margin"
    assert decision.candidates


def test_aggregate_intent_filters_every_candidate_by_registry_capability() -> None:
    provider = _CountingEmbeddingProvider(query_targets={"운용 비용률": "product_name"})
    observer = _shadow(provider, dense_min_score=-1.0, minimum_margin=0.0)

    decision = observer.observe(_trace(intent=InteractionIntent.AGGREGATE))[0]

    assert decision.status in {
        SchemaLinkStatus.FOUND,
        SchemaLinkStatus.ABSTAIN,
        SchemaLinkStatus.CONFLICT,
    }
    assert decision.candidates
    assert all(
        SchemaFieldCapability.AGGREGATABLE in candidate.capabilities
        for candidate in decision.candidates
    )
    assert all(candidate.field_id != "product_name" for candidate in decision.candidates)


def test_missing_or_mismatched_artifact_fails_before_embedding() -> None:
    provider = _CountingEmbeddingProvider()
    index = _index(provider)

    def fail_gate() -> SchemaEmbeddingArtifactGateEvidence:
        raise RuntimeError("snapshot hash mismatch")

    observer = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=index,
        artifact_precondition=fail_gate,
    )

    decision = observer.observe(_trace())[0]

    assert decision.status is SchemaLinkStatus.ABSTAIN
    assert decision.reason_code == "shadow_artifact_unverified"
    assert provider.query_calls == 0


def test_embedding_failure_isolated_as_abstain() -> None:
    provider = _FailingQueryProvider()
    observer = _shadow(provider)

    decision = observer.observe(_trace())[0]

    assert decision.status is SchemaLinkStatus.ABSTAIN
    assert decision.reason_code == "shadow_embedding_failure"
    assert decision.candidates == ()
    assert provider.query_calls == 1


class _OutOfFamilyIndex:
    def __init__(self, trusted: DenseSchemaIndex) -> None:
        self.manifest = trusted.manifest
        self.provider = trusted.provider

    def search(
        self,
        _span: str,
        _family: ProductFamily,
        *,
        top_k: int,
    ) -> list[SchemaFieldCandidate]:
        del top_k
        return [
            SchemaFieldCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                field_id="total_expense_ratio_pct",
                score=1.0,
                rank=1,
            )
        ]


def test_untrusted_index_cannot_return_a_candidate_from_another_family() -> None:
    provider = _CountingEmbeddingProvider()
    trusted = _index(provider)
    observer = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_OutOfFamilyIndex(trusted),  # type: ignore[arg-type]
        artifact_precondition=_artifact_evidence,
    )

    decision = observer.observe(_trace())[0]

    assert decision.status is SchemaLinkStatus.ABSTAIN
    assert decision.reason_code == "shadow_no_registry_candidate"
    assert decision.candidates == ()


@pytest.mark.parametrize(
    "question",
    [
        "파스타 만드는 법을 알려줘",
        "제일 좋은 ETF를 추천해줘",
        "국내 ETF 상품 3개를 보여줘",
    ],
)
def test_router_control_ood_and_deterministic_fast_paths_call_embedding_zero_times(
    question: str,
) -> None:
    provider = _CountingEmbeddingProvider()
    observer = _shadow(provider)
    request_hash = hashlib.sha256(question.encode()).hexdigest()[:8]
    trace = IntentRouter().route_with_planning(question, f"blocked-{request_hash}")

    decisions = observer.observe(trace)

    assert all(decision.status is SchemaLinkStatus.DISABLED for decision in decisions)
    assert provider.query_calls == 0


def test_off_mode_and_multiple_family_scope_call_embedding_zero_times() -> None:
    provider = _CountingEmbeddingProvider()
    index = _index(provider)
    off = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.OFF),
        index=index,
        artifact_precondition=_artifact_evidence,
    )
    assert off.observe(_trace())[0].status is SchemaLinkStatus.DISABLED
    assert provider.query_calls == 0

    trace = _trace()
    invalid_planning = trace.planning_decision.model_copy(
        update={
            "product_families": (
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
            )
        }
    )
    # An invalid injected trace is rejected by the observer's safe boundary;
    # it must never reach the provider even when Pydantic model_copy bypasses validation.
    invalid_trace = trace.model_copy(update={"planning_decision": invalid_planning})
    shadow = _shadow(provider)
    decisions = shadow.observe(invalid_trace)
    assert all(decision.status is SchemaLinkStatus.DISABLED for decision in decisions)
    assert provider.query_calls == 0


def test_async_request_seam_does_not_start_a_thread_for_control_or_fast_paths() -> None:
    provider = _CountingEmbeddingProvider()
    observer = AsyncSchemaLinkShadowObserver(_shadow(provider))
    try:
        for question in (
            "파스타 만드는 법을 알려줘",
            "제일 좋은 ETF를 추천해줘",
            "국내 ETF 상품 3개를 보여줘",
        ):
            trace = IntentRouter().route_with_planning(question, f"async-control-{len(question)}")
            assert not observer.submit(trace)
        assert not observer.is_alive
        assert provider.query_calls == 0
    finally:
        assert observer.shutdown(timeout_seconds=1)


class _ExplodingSink:
    def emit(self, _event: object) -> None:
        raise RuntimeError("audit backend unavailable")


def test_redacted_audit_and_sink_failure_do_not_change_shadow_decision() -> None:
    provider = _CountingEmbeddingProvider()
    memory = InMemoryAuditSink()
    metrics = BoundedMetrics()
    observed = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
        audit_sink=FaultTolerantAuditSink(memory, metrics),
    ).observe(_trace())[0]
    event = memory.snapshot()[0]
    assert event.stage.value == "schema_link_shadow"
    assert (
        event.question_sha256
        == hashlib.sha256(_trace().route_decision.draft.question.encode()).hexdigest()
    )
    assert _trace().route_decision.draft.question not in event.model_dump_json()

    failed = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(_CountingEmbeddingProvider()),
        artifact_precondition=_artifact_evidence,
        audit_sink=FaultTolerantAuditSink(_ExplodingSink(), BoundedMetrics()),
    ).observe(_trace())[0]
    assert failed.status is observed.status is SchemaLinkStatus.FOUND


def test_async_shadow_audit_reuses_request_invocation_and_sequence_without_raw_text() -> None:
    provider = _CountingEmbeddingProvider()
    memory = InMemoryAuditSink(max_events=10)
    audit_metrics = BoundedMetrics()
    audit_sink = BoundedAsyncAuditSink(memory, metrics=audit_metrics, queue_capacity=10)
    worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
        audit_sink=audit_sink,
    )
    observer = AsyncSchemaLinkShadowObserver(worker)
    trace = _trace()
    recorder = RequestAuditRecorder(
        request_id=trace.route_decision.draft.request_id,
        question=trace.route_decision.draft.question,
        sink=audit_sink,
    )

    try:
        assert recorder.emit(
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.STARTED,
            reason_code="request_received",
            duration_ms=0,
        )
        with bind_request_audit(recorder):
            assert observer.submit(trace)
        assert observer.drain(timeout_seconds=2)
        shadow_snapshot = observer.snapshot()
    finally:
        assert observer.shutdown(timeout_seconds=2)
        assert audit_sink.close(timeout_seconds=2)

    events = memory.snapshot()
    assert [event.event_sequence for event in events] == [1, 2]
    assert {event.invocation_id_sha256 for event in events} == {recorder.invocation_id_sha256}
    shadow_event = events[1]
    assert shadow_event.stage is AuditStage.SCHEMA_LINK_SHADOW
    serialized = shadow_event.model_dump_json()
    assert trace.route_decision.draft.question not in serialized
    assert "운용 비용률" not in serialized
    assert shadow_snapshot.audit_emit_attempt_count == 1
    assert shadow_snapshot.audit_emit_success_count == 1
    assert shadow_snapshot.audit_emit_failure_count == 0
    assert shadow_snapshot.correlation_failure_count == 0


def test_expected_audit_sink_rejects_missing_or_wrong_recorder_before_provider() -> None:
    provider = _CountingEmbeddingProvider()
    expected_memory = InMemoryAuditSink()
    expected_metrics = BoundedMetrics()
    expected_sink = BoundedAsyncAuditSink(expected_memory, metrics=expected_metrics)
    wrong_sink = BoundedAsyncAuditSink(InMemoryAuditSink())
    worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
        audit_sink=expected_sink,
    )
    observer = AsyncSchemaLinkShadowObserver(worker)
    trace = _trace()
    wrong_recorder = RequestAuditRecorder(
        request_id="wrong-recorder",
        question=trace.route_decision.draft.question,
        sink=wrong_sink,
    )
    wrong_request_same_sink = RequestAuditRecorder(
        request_id="other-request",
        question=trace.route_decision.draft.question,
        sink=expected_sink,
    )
    wrong_question_same_sink = RequestAuditRecorder(
        request_id=trace.route_decision.draft.request_id,
        question="다른 요청의 원문",
        sink=expected_sink,
    )
    audit_before = expected_metrics.snapshot()

    try:
        assert not observer.submit(trace)
        with bind_request_audit(wrong_recorder):
            assert not observer.submit(trace)
        with bind_request_audit(wrong_request_same_sink):
            assert not observer.submit(trace)
        with bind_request_audit(wrong_question_same_sink):
            assert not observer.submit(trace)
        shadow_snapshot = observer.snapshot()
        audit_after = expected_metrics.snapshot()
    finally:
        assert observer.shutdown(timeout_seconds=1)
        assert expected_sink.close(timeout_seconds=1)
        assert wrong_sink.close(timeout_seconds=1)

    assert provider.query_calls == 0
    assert not shadow_snapshot.started
    assert shadow_snapshot.correlation_failure_count == 4
    assert shadow_snapshot.accepted_count == 0
    assert audit_before.counters == audit_after.counters == {}
    assert audit_before.queue_depth == audit_after.queue_depth == 0
    assert audit_before.inflight == audit_after.inflight == 0
    assert expected_memory.snapshot() == ()


def test_shadow_snapshot_counts_a_correlated_audit_enqueue_failure() -> None:
    provider = _CountingEmbeddingProvider()
    audit_sink = BoundedAsyncAuditSink(InMemoryAuditSink(), start_worker=False)
    worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
        audit_sink=audit_sink,
    )
    observer = AsyncSchemaLinkShadowObserver(worker)
    trace = _trace()
    recorder = RequestAuditRecorder(
        request_id=trace.route_decision.draft.request_id,
        question=trace.route_decision.draft.question,
        sink=audit_sink,
    )
    try:
        with bind_request_audit(recorder):
            assert observer.submit(trace)
        assert observer.drain(timeout_seconds=2)
        snapshot = observer.snapshot()
    finally:
        assert observer.shutdown(timeout_seconds=2)
        assert not audit_sink.close(timeout_seconds=2)

    assert provider.query_calls == 1
    assert snapshot.audit_emit_attempt_count == 1
    assert snapshot.audit_emit_success_count == 0
    assert snapshot.audit_emit_failure_count == 1


def test_shadow_runtime_metrics_separate_operational_from_normal_abstention() -> None:
    operational_provider = _CountingEmbeddingProvider()

    def fail_gate() -> SchemaEmbeddingArtifactGateEvidence:
        raise RuntimeError("artifact unavailable")

    operational = AsyncSchemaLinkShadowObserver(
        HybridSchemaLinkShadow(
            settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
            index=_index(operational_provider),
            artifact_precondition=fail_gate,
        )
    )
    normal_provider = _CountingEmbeddingProvider()
    normal = AsyncSchemaLinkShadowObserver(_shadow(normal_provider, minimum_margin=2.0))
    try:
        assert operational.submit(_trace())
        assert normal.submit(_trace())
        assert operational.drain(timeout_seconds=2)
        assert normal.drain(timeout_seconds=2)
        operational_snapshot = operational.snapshot()
        normal_snapshot = normal.snapshot()
    finally:
        assert operational.shutdown(timeout_seconds=2)
        assert normal.shutdown(timeout_seconds=2)

    assert operational_provider.query_calls == 0
    assert operational_snapshot.completed_count == 1
    assert operational_snapshot.operational_failure_count == 1
    assert normal_provider.query_calls == 1
    assert normal_snapshot.completed_count == 1
    assert normal_snapshot.operational_failure_count == 0


class _ManualMonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_shadow_snapshot_reports_stall_and_shutdown_uses_one_deadline() -> None:
    provider = _BlockingQueryProvider()
    clock = _ManualMonotonicClock()
    worker = _shadow(provider)
    observer = AsyncSchemaLinkShadowObserver(
        worker,
        settings=SchemaShadowQueueSettings(
            queue_capacity=1,
            stall_timeout_seconds=0.1,
        ),
        monotonic_clock=clock,
    )
    assert observer.enabled
    assert observer.submit(_trace())
    assert provider.entered.wait(timeout=1)
    clock.advance(0.2)
    stalled = observer.snapshot()
    assert stalled.stalled
    assert stalled.pending_count == 1
    assert stalled.inflight == 1
    assert stalled.oldest_pending_age_seconds == pytest.approx(0.2)
    assert stalled.no_progress_age_seconds == pytest.approx(0.2)

    started = monotonic()
    assert not observer.shutdown(timeout_seconds=0.1)
    elapsed = monotonic() - started
    assert elapsed < 0.17
    failed_shutdown = observer.snapshot()
    assert failed_shutdown.shutdown_started
    assert failed_shutdown.shutdown_completed
    assert failed_shutdown.shutdown_succeeded is False
    assert not failed_shutdown.accepting

    provider.release.set()
    deadline = monotonic() + 2
    while observer.is_alive and monotonic() < deadline:
        sleep(0.01)
    assert not observer.is_alive


def test_shadow_snapshot_exposes_an_unexpected_dead_worker() -> None:
    provider = _CountingEmbeddingProvider()
    observer = AsyncSchemaLinkShadowObserver(_shadow(provider))
    observer._run = lambda: None  # type: ignore[method-assign]  # noqa: SLF001

    assert observer.submit(_trace())
    deadline = monotonic() + 1
    while observer.is_alive and monotonic() < deadline:
        sleep(0.01)
    snapshot = observer.snapshot()
    assert snapshot.started
    assert not snapshot.worker_alive
    assert snapshot.accepting
    assert snapshot.pending_count == 1
    assert snapshot.accepted_count == 1
    assert snapshot.completed_count == 0
    assert provider.query_calls == 0
    started = monotonic()
    assert not observer.shutdown(timeout_seconds=1, drain=True)
    assert monotonic() - started < 0.2


class _MutatingExplodingObserver:
    def submit(self, trace: PlanningTrace) -> bool:
        trace.route_decision.draft.product_families.clear()
        raise RuntimeError("shadow observer must not affect response")


def test_agent_rejects_an_arbitrary_synchronous_shadow_observer() -> None:
    with pytest.raises(TypeError, match="bounded async observer"):
        RoutedFinanceAgent(
            {},
            router=_FixedTraceRouter(_trace()),  # type: ignore[arg-type]
            schema_link_shadow_observer=_MutatingExplodingObserver(),  # type: ignore[arg-type]
        )


def test_async_observer_rejects_an_untrusted_worker() -> None:
    with pytest.raises(TypeError, match="trusted HybridSchemaLinkShadow"):
        AsyncSchemaLinkShadowObserver(object())  # type: ignore[arg-type]


class _FixedTraceRouter:
    def __init__(self, trace: PlanningTrace) -> None:
        self.trace = trace

    def route_with_planning(self, _question: str, _request_id: str) -> PlanningTrace:
        return PlanningTrace.model_validate_json(self.trace.model_dump_json())


class _FixedCoverageGate:
    def evaluate(self, *_args: object, **_kwargs: object) -> SemanticCoverageDecision:
        return SemanticCoverageDecision(schema_link_gap_spans=("운용 비용률",))


def test_agent_does_not_wait_for_blocking_embedding_and_worker_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _BlockingQueryProvider()
    worker = _shadow(provider)
    observer = AsyncSchemaLinkShadowObserver(
        worker,
        settings=SchemaShadowQueueSettings(queue_capacity=2),
    )
    sentinel = object()
    agent = RoutedFinanceAgent(
        {},
        router=_FixedTraceRouter(_trace()),  # type: ignore[arg-type]
        schema_link_shadow_observer=observer,
    )
    monkeypatch.setattr(agent, "_answer_from_decision", lambda *_args: sentinel)

    try:
        started = perf_counter()
        result = agent.answer("ignored by fixed test router", "ignored-request-id")
        request_elapsed = perf_counter() - started

        assert result is sentinel
        assert request_elapsed < 0.1
        assert provider.entered.wait(timeout=1)
        assert observer.is_alive
    finally:
        provider.release.set()
        assert observer.drain(timeout_seconds=2)
        assert observer.shutdown(timeout_seconds=2)
    assert not observer.is_alive


def test_bounded_shadow_queue_drops_without_blocking_and_records_metric() -> None:
    provider = _BlockingQueryProvider()
    worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=_index(provider),
        artifact_precondition=_artifact_evidence,
    )
    observer = AsyncSchemaLinkShadowObserver(
        worker,
        settings=SchemaShadowQueueSettings(queue_capacity=1),
    )

    try:
        assert observer.submit(_trace())
        assert provider.entered.wait(timeout=1)
        assert observer.submit(_trace(span="운용 비용률"))
        started = perf_counter()
        assert not observer.submit(_trace(span="운용 비용률"))
        assert perf_counter() - started < 0.05
        snapshot = observer.snapshot()
        assert snapshot.queue_drop_count == 1
        assert snapshot.accepted_count == 2
        assert snapshot.pending_count == 2
    finally:
        provider.release.set()
        assert observer.drain(timeout_seconds=2)
        assert observer.shutdown(timeout_seconds=2)
    assert provider.query_calls == 2
    assert not observer.is_alive


def test_real_agent_queryplan_sql_and_response_are_byte_identical_in_shadow(
    domestic_sample_database: tuple[object, object, object],
) -> None:
    database_path = domestic_sample_database[0]
    question = "운용 비용률이 낮은 국내 ETF 3개를 보여줘."
    baseline_router = IntentRouter(semantic_coverage_gate=_FixedCoverageGate())
    observed_router = IntentRouter(semantic_coverage_gate=_FixedCoverageGate())
    baseline = RoutedFinanceAgent(
        {"domestic_etp": database_path},
        router=baseline_router,
    ).answer(question, "shadow-full-parity")

    provider = _CountingEmbeddingProvider()
    observer = AsyncSchemaLinkShadowObserver(_shadow(provider))
    try:
        observed = RoutedFinanceAgent(
            {"domestic_etp": database_path},
            router=observed_router,
            schema_link_shadow_observer=observer,
        ).answer(question, "shadow-full-parity")
        assert observer.drain(timeout_seconds=2)
    finally:
        assert observer.shutdown(timeout_seconds=2)

    assert observed.model_dump_json() == baseline.model_dump_json()
    assert observed.decision == baseline.decision
    assert observed.query_plan == baseline.query_plan
    assert provider.query_calls == 1
