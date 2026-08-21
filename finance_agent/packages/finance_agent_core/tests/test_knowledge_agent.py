from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.knowledge_cli import main as knowledge_cli_main
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.knowledge_service import (
    KnowledgeAgent,
    KnowledgeServiceError,
)
from finance_agent_core.answering.claims import (
    KnowledgeAnswerContext,
    KnowledgeAnswerDraft,
    expected_knowledge_answer_draft,
)
from finance_agent_core.audit_validation import (
    AuditValidationPolicy,
    AuditValidationStatus,
    validate_audit_jsonl,
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendErrorCode,
    BackendStatus,
)
from finance_agent_core.contracts.knowledge import (
    DocumentKnowledgeOperation,
    KnowledgePlanAuthorityError,
    KnowledgePlanAuthorityGate,
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.execution import SQLiteAggregateOracle, SQLiteOracle
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    RequestAuditRecorder,
    bind_request_audit,
)
from finance_agent_core.release import (
    AgentReleaseCode,
    AgentReleaseError,
    DocumentRetrievalArtifactRelease,
    KnowledgeRetrievalRelease,
    PublicDocumentRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    RelationRetrievalArtifactRelease,
)
from finance_agent_core.retrieval import (
    DocumentInput,
    DocumentSourceKind,
    RelationIndexError,
    RelationType,
    SQLiteDocumentIndex,
    SQLiteRelationIndex,
    VerifiedProductDatabase,
    build_provided_relation_index,
)
from finance_agent_core.storage.approval import sha256_file
from finance_agent_core.storage.identity_cache import load_product_identities


class SyntheticDatabaseVerifier:
    def __init__(self, approval_manifest_sha256: str = "f" * 64) -> None:
        self._approval_manifest_sha256 = approval_manifest_sha256

    @property
    def approval_manifest_sha256(self) -> str:
        return self._approval_manifest_sha256

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase:
        resolved = Path(path).resolve(strict=True)
        manifest, identities = load_product_identities(resolved)
        if manifest.dataset != product_family.value:
            raise RelationIndexError("synthetic verifier family mismatch")
        return VerifiedProductDatabase(
            product_family=product_family,
            path=resolved,
            manifest=manifest,
            database_sha256=sha256_file(resolved),
            identities=identities,
        )


class FakeClaimProvider:
    provider_name = "fake-structured-claims"
    model_name = "fake-model-v1"

    def __init__(self, behavior: Literal["valid", "product", "excerpt", "error"] = "valid"):
        self.behavior = behavior
        self.calls = 0

    def generate_claims(self, context: KnowledgeAnswerContext) -> KnowledgeAnswerDraft:
        self.calls += 1
        if self.behavior == "error":
            raise TimeoutError("synthetic provider timeout")
        expected = expected_knowledge_answer_draft(context)
        payload = expected.model_dump(mode="python")
        if self.behavior == "product":
            payload["claims"][0]["product_id"] = "INVENTED-PRODUCT"
        elif self.behavior == "excerpt":
            payload["claims"][0]["excerpt"] = "문서에 없는 수익률 99% 주장"
        return KnowledgeAnswerDraft.model_validate(payload)


def _relation_plan(
    *,
    query: str = "테스트운용",
    top_k: int = 3,
) -> KnowledgeQueryPlan:
    return KnowledgeQueryPlan(
        question_id="relation-q1",
        question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        operation=RelationKnowledgeOperation(
            query=query,
            relation_types=(RelationType.MANAGED_BY,),
            product_families=(ProductFamily.DOMESTIC_ETP,),
            top_k=top_k,
        ),
    )


def _document_plan(query: str = "위험등급 손실 가능성") -> KnowledgeQueryPlan:
    return KnowledgeQueryPlan(
        question_id="document-q1",
        question="금융상품 위험등급이 무엇인지 설명해줘",
        operation=DocumentKnowledgeOperation(
            query=query,
            source_kinds=(DocumentSourceKind.PROVIDED,),
            top_k=2,
        ),
    )


def _write_private_audit_jsonl(path: Path, events: tuple[AuditEvent, ...]) -> None:
    """Create a new owner-only Audit fixture without trusting process umask."""

    payload = "".join(event.model_dump_json() + "\n" for event in events).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def test_private_audit_fixture_is_owner_only_under_standard_umask(tmp_path: Path) -> None:
    path = tmp_path / "standard-umask-audit.jsonl"
    previous_umask = os.umask(0o022)
    try:
        _write_private_audit_jsonl(path, ())
    finally:
        os.umask(previous_umask)

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.fixture
def relation_agent_factory(tmp_path: Path, domestic_sample_database):
    product_database, _, _ = domestic_sample_database
    relation_index = tmp_path / "relations.sqlite3"
    verifier = SyntheticDatabaseVerifier()
    build_provided_relation_index(
        {ProductFamily.DOMESTIC_ETP: product_database},
        relation_index,
        verifier=verifier,
    )
    manifest = SQLiteRelationIndex(relation_index).manifest()
    release = KnowledgeRetrievalRelease(
        relation=RelationRetrievalArtifactRelease(
            index_sha256=sha256_file(relation_index),
            approval_manifest_sha256=manifest.approval_manifest_sha256,
            relation_set_sha256=manifest.relation_set_sha256,
        )
    )

    def create(provider: FakeClaimProvider | None = None) -> KnowledgeAgent:
        return KnowledgeAgent(
            release=release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=verifier,
            claim_provider=provider,
        )

    return create, relation_index, release, product_database


@pytest.fixture
def document_agent_factory(tmp_path: Path):
    document_index = tmp_path / "documents.sqlite3"
    index = SQLiteDocumentIndex(document_index)
    index.initialize()
    index.ingest(
        DocumentInput(
            document_id="provided-risk-terms",
            title="금융상품 위험등급 용어",
            text="위험등급은 금융상품의 손실 가능성을 비교하기 위한 분류입니다.",
            source_uri="approved://provided-risk-terms",
            source_kind=DocumentSourceKind.PROVIDED,
            as_of=date(2026, 7, 11),
            metadata={"category": "glossary"},
        )
    )
    os.chmod(document_index, 0o444)
    release = KnowledgeRetrievalRelease(
        document=DocumentRetrievalArtifactRelease(
            index_sha256=sha256_file(document_index),
            corpus_manifest_sha256="a" * 64,
            file_manifest_sha256="b" * 64,
        )
    )

    def create(provider: FakeClaimProvider | None = None) -> KnowledgeAgent:
        return KnowledgeAgent(
            release=release,
            document_index_path=document_index,
            claim_provider=provider,
        )

    return create, document_index, release


def test_typed_relation_plan_rejects_fund_and_incompatible_relations() -> None:
    with pytest.raises(ValidationError, match="fund relation search is disabled"):
        RelationKnowledgeOperation(
            query="운용사",
            relation_types=(RelationType.MANAGED_BY,),
            product_families=(ProductFamily.FUND,),
        )
    with pytest.raises(ValidationError, match="issued_by is unavailable"):
        RelationKnowledgeOperation(
            query="한국전력공사",
            relation_types=(RelationType.ISSUED_BY,),
            product_families=(ProductFamily.DOMESTIC_ETP,),
        )


def test_typed_plan_requires_canonical_unique_filters() -> None:
    with pytest.raises(ValidationError, match="canonical sorted order"):
        RelationKnowledgeOperation(
            query="미국",
            relation_types=(RelationType.INVESTS_IN_REGION,),
            product_families=(
                ProductFamily.OVERSEAS_ETP,
                ProductFamily.DOMESTIC_ETP,
            ),
        )
    with pytest.raises(ValidationError, match="source_kinds must not contain duplicates"):
        DocumentKnowledgeOperation(
            query="위험등급",
            source_kinds=(DocumentSourceKind.PROVIDED, DocumentSourceKind.PROVIDED),
        )


def test_relation_plan_allows_only_one_predicate_per_request() -> None:
    with pytest.raises(ValidationError, match="at most 1 item"):
        RelationKnowledgeOperation(
            query="미국",
            relation_types=(
                RelationType.CLASSIFIED_AS_ASSET,
                RelationType.INVESTS_IN_REGION,
            ),
            product_families=(ProductFamily.DOMESTIC_ETP,),
        )


def test_public_relation_release_verifies_readiness_and_preserves_provenance(
    relation_agent_factory,
) -> None:
    create, relation_index, internal_release, product_database = relation_agent_factory
    internal_agent = create()
    assert internal_release.relation is not None
    public_release = PublicKnowledgeRetrievalRelease(
        relation=PublicRelationRetrievalRelease(
            status="activated",
            artifact=internal_release.relation,
            artifact_file_sha256="a" * 64,
        ),
        document=PublicDocumentRetrievalRelease(),
    )
    agent = KnowledgeAgent(
        release=public_release,
        relation_index_path=relation_index,
        relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
        relation_verifier=internal_agent.relation_verifier,
    )
    with pytest.raises(ValueError, match="claim generation disabled"):
        KnowledgeAgent(
            release=public_release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=internal_agent.relation_verifier,
            claim_provider=FakeClaimProvider(),
        )

    agent.verify_ready()
    result = agent.execute(_relation_plan())

    assert result.release_contract_sha256 == public_release.contract_sha256

    assert public_release.relation.artifact is not None
    mismatched_release = public_release.model_copy(
        update={
            "relation": public_release.relation.model_copy(
                update={
                    "artifact": public_release.relation.artifact.model_copy(
                        update={"relation_set_sha256": "e" * 64}
                    )
                }
            )
        }
    )
    with pytest.raises(KnowledgeServiceError, match="runtime differs"):
        KnowledgeAgent(
            release=mismatched_release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=internal_agent.relation_verifier,
        ).verify_ready()


def test_relation_readiness_detects_post_start_file_drift(relation_agent_factory) -> None:
    create, relation_index, _, _ = relation_agent_factory
    agent = create()
    agent.verify_ready()
    agent.assert_ready_current()

    os.chmod(relation_index, 0o644)

    with pytest.raises(KnowledgeServiceError, match="changed after readiness"):
        agent.assert_ready_current()


def test_public_router_executes_relation_and_preserves_product_fallthrough(
    relation_agent_factory,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )

    relation = execute_answer_request(
        service,
        BackendAgentRequest(
            request_id="public-relation-001",
            question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        ),
    )
    aggregate = execute_answer_request(
        service,
        BackendAgentRequest(
            request_id="public-product-001",
            question="국내 ETP의 상품유형별 분포를 집계해줘",
        ),
    )

    assert relation.http_status_code == 200
    assert relation.response.status is BackendStatus.SUCCESS
    assert relation.response.request_id == "public-relation-001"
    assert len(relation.response.products) == 3
    assert relation.response.query_plan is not None
    assert relation.response.query_plan.operation.kind == "relation_search"
    assert aggregate.http_status_code == 200
    assert aggregate.response.status is BackendStatus.SUCCESS
    assert aggregate.response.aggregates
    assert aggregate.response.query_plan is not None
    assert aggregate.response.query_plan.intent.value == "aggregate"


@pytest.mark.parametrize("relation_enabled", [False, True])
def test_relation_wiring_preserves_existing_product_intents(
    relation_agent_factory,
    relation_enabled: bool,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=(DeterministicKnowledgeRouter() if relation_enabled else None),
        knowledge_agent=(create() if relation_enabled else None),
    )
    cases = (
        (
            "existing-search",
            "미국에 투자하는 국내 ETF 3개를 보여줘",
            InteractionIntent.SEARCH,
        ),
        (
            "existing-detail",
            "상품 ID KR7000000002인 국내 ETF의 운용사 상세 정보를 조회해줘",
            InteractionIntent.DETAIL,
        ),
        (
            "existing-compare",
            "국내 ETF KR7000000003과 KR7000000002의 운용사를 비교해줘",
            InteractionIntent.COMPARE,
        ),
        (
            "existing-aggregate",
            "국내 ETF의 운용사별 상품 개수를 집계해줘",
            InteractionIntent.AGGREGATE,
        ),
    )

    for request_id, question, intent in cases:
        result = execute_answer_request(
            service,
            BackendAgentRequest(request_id=request_id, question=question),
        )

        assert result.http_status_code == 200
        assert result.response.status is BackendStatus.SUCCESS
        assert result.response.intent is intent
        assert result.response.query_plan is not None
        if intent is InteractionIntent.COMPARE:
            assert result.response.comparisons
            assert result.response.comparisons[0].canonical_field == "manager"
        elif intent is InteractionIntent.AGGREGATE:
            assert result.response.aggregates
            assert result.response.query_plan.intent.value == "aggregate"
        elif intent is InteractionIntent.DETAIL:
            assert [item.product_id for item in result.response.products] == ["KR7000000002"]


@pytest.mark.parametrize(
    "question",
    [
        "미국에 투자하는 국내 ETF 중 위험이 낮은 상품을 보여줘",
        "미국에 투자하는 국내 ETF 중 위험등급 2등급 이하 상품을 보여줘",
        "미국에 투자하는 국내 ETF 중 판매 가능한 상품을 보여줘",
        "테스트운용이 운용하는 매수 가능한 국내 ETF를 보여줘",
        "미국에 투자하는 국내 ETF 중 거래 정지 상품은 제외해줘",
        "미국에 투자하는 국내 ETF 중 테스트운용 상품은 제외해줘",
        "미국에 투자하는 국내 ETF 중 거래량 100만 이상을 보여줘",
        "미국에 투자하는 국내 ETF를 거래대금 많은 순으로 보여줘",
    ],
)
def test_relation_mixed_conditions_stop_before_any_execution(
    relation_agent_factory,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )

    def unexpected_execution(*args: object, **kwargs: object) -> None:
        raise AssertionError("mixed relation conditions crossed the execution boundary")

    monkeypatch.setattr(KnowledgeAgent, "execute", unexpected_execution)
    monkeypatch.setattr(SQLiteOracle, "execute", unexpected_execution)
    monkeypatch.setattr(SQLiteAggregateOracle, "execute", unexpected_execution)

    result = execute_answer_request(
        service,
        BackendAgentRequest(request_id="mixed-relation-control", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status in {
        BackendStatus.CLARIFICATION,
        BackendStatus.UNSUPPORTED,
    }
    assert result.response.query_plan is None
    assert result.response.products == []
    assert result.response.comparisons == []
    assert result.response.aggregates == []
    assert result.response.citations == []


class _ReleaseGuardProbe:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def assert_request_current(self) -> None:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                "synthetic immutable release drift",
            )


@pytest.mark.parametrize(
    ("question", "expected_status"),
    [
        (
            "테스트운용이 운용하는 국내 ETF 3개를 알려줘",
            BackendStatus.SUCCESS,
        ),
        (
            "없는운용사가 운용하는 국내 ETF를 알려줘",
            BackendStatus.NOT_FOUND,
        ),
        (
            "테스트운용이 운용하는 ETF를 보여줘",
            BackendStatus.CLARIFICATION,
        ),
        (
            "테스트운용이 운용하는 해외 ETF를 보여줘",
            BackendStatus.UNSUPPORTED,
        ),
    ],
)
def test_every_relation_outcome_uses_common_release_guard(
    relation_agent_factory,
    question: str,
    expected_status: BackendStatus,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )
    guard = _ReleaseGuardProbe()
    service.release_guard = guard  # type: ignore[assignment]

    result = execute_answer_request(
        service,
        BackendAgentRequest(request_id="relation-release-guard", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is expected_status
    assert guard.calls >= 2


@pytest.mark.parametrize(
    "question",
    [
        "테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        "없는운용사가 운용하는 국내 ETF를 알려줘",
        "테스트운용이 운용하는 ETF를 보여줘",
        "테스트운용이 운용하는 해외 ETF를 보여줘",
    ],
)
def test_every_relation_outcome_rejects_post_route_release_drift(
    relation_agent_factory,
    question: str,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )
    guard = _ReleaseGuardProbe(fail_on_call=2)
    service.release_guard = guard  # type: ignore[assignment]

    result = execute_answer_request(
        service,
        BackendAgentRequest(request_id="relation-release-drift", question=question),
    )

    assert result.http_status_code == 503
    assert result.response.status is BackendStatus.ERROR
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.INTERNAL_ERROR
    assert result.response.products == []
    assert result.response.citations == []
    assert "synthetic immutable release drift" not in result.response.model_dump_json()


def test_relation_timeout_preserves_trusted_backend_route_context(
    relation_agent_factory,
) -> None:
    create, _, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )
    request = BackendAgentRequest(
        request_id="public-relation-timeout-001",
        question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
    )

    with bind_request_deadline(RequestDeadline(expires_at=0.0)):
        result = execute_answer_request(service, request)

    assert result.http_status_code == 504
    assert result.response.status is BackendStatus.ERROR
    assert result.response.intent is InteractionIntent.SEARCH
    assert result.response.product_families == [ProductFamily.DOMESTIC_ETP]
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.PROVIDER_UNAVAILABLE


def test_public_relation_emits_ordered_release_linked_audit(
    relation_agent_factory,
    tmp_path: Path,
) -> None:
    create, _, release, product_database = relation_agent_factory
    assert release.relation is not None
    memory = InMemoryAuditSink(max_events=100)
    audit = BoundedAsyncAuditSink(memory, queue_capacity=100)
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(FakeClaimProvider()),
        audit_sink=audit,
    )
    request_id = "public-relation-audit-001"
    question = "테스트운용이 운용하는 국내 ETF 3개를 알려줘"
    recorder = RequestAuditRecorder(request_id="", question="", sink=audit)
    recorder.emit(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.STARTED,
        reason_code="received",
        duration_ms=0,
    )
    enriched = recorder.with_request(request_id=request_id, question=question)

    with bind_request_audit(enriched):
        response = execute_answer_request(
            service,
            BackendAgentRequest(request_id=request_id, question=question),
        )
        enriched.emit(
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="response_completed",
            duration_ms=0,
        )
    assert audit.close(timeout_seconds=2)
    events = memory.snapshot()
    expected_reasons = [
        "guard_allowed",
        "knowledge_routed_execute",
        "knowledge_plan_compiled",
        "knowledge_authority_granted",
        "relation_lookup_completed",
        "relation_evidence_verified",
        "knowledge_claims_verified",
        "knowledge_rendering_completed",
        "knowledge_execution_completed",
    ]

    assert response.http_status_code == 200
    assert [
        event.reason_code for event in events if event.reason_code in expected_reasons
    ] == expected_reasons
    release_linked_reasons = {
        *expected_reasons[1:],
        "knowledge_generation_completed",
    }
    assert all(
        event.relation_set_sha256 == release.relation.relation_set_sha256
        for event in events
        if event.reason_code in release_linked_reasons
    )
    audit_path = tmp_path / "public-relation-audit.jsonl"
    _write_private_audit_jsonl(audit_path, events)
    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(require_relation_linkage=True),
    )
    assert report.status is AuditValidationStatus.PASSED
    assert report.issue_count == 0


def test_public_relation_timeout_emits_valid_causal_audit(
    relation_agent_factory,
    tmp_path: Path,
) -> None:
    create, _, _, product_database = relation_agent_factory
    memory = InMemoryAuditSink(max_events=100)
    audit = BoundedAsyncAuditSink(memory, queue_capacity=100)
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
        audit_sink=audit,
    )
    request_id = "public-relation-timeout-audit-001"
    question = "테스트운용이 운용하는 국내 ETF 3개를 알려줘"
    recorder = RequestAuditRecorder(request_id="", question="", sink=audit)
    recorder.emit(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.STARTED,
        reason_code="received",
        duration_ms=0,
    )
    enriched = recorder.with_request(request_id=request_id, question=question)

    with (
        bind_request_audit(enriched),
        bind_request_deadline(RequestDeadline(expires_at=0.0)),
    ):
        response = execute_answer_request(
            service,
            BackendAgentRequest(request_id=request_id, question=question),
        )
        enriched.emit(
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.TIMED_OUT,
            reason_code="deadline_exceeded",
            duration_ms=0,
        )
    assert audit.close(timeout_seconds=2)
    events = memory.snapshot()

    assert response.http_status_code == 504
    assert [
        event.reason_code
        for event in events
        if event.reason_code
        in {
            "knowledge_authority_timed_out",
            "knowledge_deadline_exceeded",
            "deadline_exceeded",
        }
    ] == [
        "knowledge_authority_timed_out",
        "knowledge_deadline_exceeded",
        "deadline_exceeded",
    ]
    audit_path = tmp_path / "public-relation-timeout-audit.jsonl"
    _write_private_audit_jsonl(audit_path, events)
    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(require_relation_linkage=True),
    )
    assert report.status is AuditValidationStatus.PASSED
    assert report.issue_count == 0


@pytest.mark.parametrize(
    ("question", "status"),
    [
        ("테스트운용이 운용하는 ETF를 보여줘", BackendStatus.CLARIFICATION),
        ("테스트운용이 운용하는 해외 ETF를 보여줘", BackendStatus.UNSUPPORTED),
    ],
)
def test_public_router_projects_relation_control_without_execution(
    relation_agent_factory,
    question: str,
    status: BackendStatus,
) -> None:
    create, _, _, product_database = relation_agent_factory
    response = execute_answer_request(
        RoutedFinanceAgent(
            {ProductFamily.DOMESTIC_ETP: product_database},
            knowledge_router=DeterministicKnowledgeRouter(),
            knowledge_agent=create(),
        ),
        BackendAgentRequest(request_id="public-control-001", question=question),
    )

    assert response.http_status_code == 200
    assert response.response.status is status
    assert response.response.query_plan is None
    assert response.response.products == []
    assert response.response.citations == []


def test_public_router_without_activated_relation_release_fails_closed(
    domestic_sample_database,
) -> None:
    product_database, _, _ = domestic_sample_database
    response = execute_answer_request(
        RoutedFinanceAgent(
            {ProductFamily.DOMESTIC_ETP: product_database},
            knowledge_router=DeterministicKnowledgeRouter(),
        ),
        BackendAgentRequest(
            request_id="public-relation-disabled-001",
            question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        ),
    )

    assert response.http_status_code == 200
    assert response.response.status is BackendStatus.UNSUPPORTED
    assert response.response.query_plan is None
    assert response.response.products == []
    assert response.response.error is None


def test_public_relation_integrity_failure_maps_to_retryable_503(
    relation_agent_factory,
) -> None:
    create, relation_index, _, product_database = relation_agent_factory
    service = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: product_database},
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=create(),
    )
    os.chmod(relation_index, 0o644)

    response = execute_answer_request(
        service,
        BackendAgentRequest(
            request_id="public-relation-drift-001",
            question="테스트운용이 운용하는 국내 ETF 3개를 알려줘",
        ),
    )

    assert response.http_status_code == 503
    assert response.response.status is BackendStatus.ERROR
    assert response.response.error is not None
    assert response.response.error.code is BackendErrorCode.DATASET_UNAVAILABLE
    assert response.response.error.retryable
    assert str(relation_index) not in response.model_dump_json()


def test_knowledge_plan_forbids_untyped_extra_operations() -> None:
    payload = _relation_plan().model_dump(mode="python")
    payload["operation"]["aggregation"] = {"field": "aum", "function": "avg"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeQueryPlan.model_validate(payload)


def test_exact_plan_gate_rejects_any_model_change() -> None:
    server = _relation_plan()
    proposal = _relation_plan(top_k=4)

    with pytest.raises(KnowledgePlanAuthorityError, match="differs"):
        KnowledgePlanAuthorityGate().authorize(
            server,
            proposal,
            proposal_provider_name="hyperclova",
            proposal_model_name="HCX-007",
        )


def test_exact_plan_gate_records_matching_provider_proposal() -> None:
    plan = _relation_plan()
    validated = KnowledgePlanAuthorityGate().authorize(
        plan,
        KnowledgeQueryPlan.model_validate_json(plan.model_dump_json()),
        proposal_provider_name="hyperclova",
        proposal_model_name="HCX-007",
    )

    assert validated.plan == plan
    assert validated.receipt.status == "authorized_exact_match"
    assert validated.receipt.proposal_provider_name == "hyperclova"


def test_relation_agent_runs_exact_plan_and_verified_structured_claims(
    relation_agent_factory,
) -> None:
    provider = FakeClaimProvider()
    agent = relation_agent_factory[0](provider)

    result = agent.execute(_relation_plan())

    assert result.status == "found"
    assert result.candidate_count == 3
    assert result.answer.mode == "structured_grounded"
    assert result.answer.verification.passed
    assert provider.calls == 1
    assert [item.product_name for item in result.relation_response.evidence] == [
        "국내 테스트 A000002",
        "국내 테스트 A000003",
        "국내 테스트 A000004",
    ]
    assert "evidence relation:" in result.answer.answer
    assert "투자 추천이나 인과관계를 뜻하지 않습니다" in result.answer.answer


def test_relation_claim_hallucination_uses_full_deterministic_fallback(
    relation_agent_factory,
) -> None:
    agent = relation_agent_factory[0](FakeClaimProvider("product"))

    result = agent.execute(_relation_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert not result.answer.verification.passed
    assert result.answer.draft.claims[0].product_id == "INVENTED-PRODUCT"
    assert "국내 테스트 A000002" in result.answer.answer
    assert "INVENTED-PRODUCT" not in result.answer.answer


def test_relation_provider_failure_uses_deterministic_fallback(
    relation_agent_factory,
) -> None:
    result = relation_agent_factory[0](FakeClaimProvider("error")).execute(_relation_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert result.answer.draft is None
    assert result.answer.verification.violations[0].startswith("TimeoutError")


def test_relation_not_found_never_calls_claim_provider(relation_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = relation_agent_factory[0](provider).execute(_relation_plan(query="존재하지않는운용사"))

    assert result.status == "not_found"
    assert result.candidate_count == 0
    assert result.answer.mode == "deterministic"
    assert provider.calls == 0


def test_relation_release_rejects_writable_or_mismatched_index(
    relation_agent_factory,
) -> None:
    create, relation_index, release, product_database = relation_agent_factory
    os.chmod(relation_index, 0o644)
    with pytest.raises(KnowledgeServiceError, match="read-only"):
        create().execute(_relation_plan())

    os.chmod(relation_index, 0o444)
    bad_release = release.model_copy(
        update={
            "relation": release.relation.model_copy(update={"approval_manifest_sha256": "e" * 64})
        }
    )
    with pytest.raises(KnowledgeServiceError, match="manifest differs"):
        KnowledgeAgent(
            release=bad_release,
            relation_index_path=relation_index,
            relation_database_paths={ProductFamily.DOMESTIC_ETP: product_database},
            relation_verifier=SyntheticDatabaseVerifier(),
        ).execute(_relation_plan())


def test_document_agent_returns_approved_exact_excerpts(document_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = document_agent_factory[0](provider).execute(_document_plan())

    assert result.status == "found"
    assert result.candidate_count == 1
    assert result.answer.mode == "structured_grounded"
    assert result.document_response.evidence[0].source_kind is DocumentSourceKind.PROVIDED
    assert "손실 가능성을 비교하기 위한 분류" in result.answer.answer
    assert provider.calls == 1


def test_document_claim_not_in_source_uses_fallback(document_agent_factory) -> None:
    provider = FakeClaimProvider("excerpt")
    result = document_agent_factory[0](provider).execute(_document_plan())

    assert result.answer.mode == "deterministic_fallback"
    assert not result.answer.verification.passed
    assert "99%" not in result.answer.answer
    assert "손실 가능성" in result.answer.answer


def test_document_not_found_never_calls_claim_provider(document_agent_factory) -> None:
    provider = FakeClaimProvider()
    result = document_agent_factory[0](provider).execute(_document_plan("존재하지않는용어"))

    assert result.status == "not_found"
    assert result.answer.mode == "deterministic"
    assert provider.calls == 0


def test_document_release_rejects_index_permission_drift(document_agent_factory) -> None:
    create, document_index, _ = document_agent_factory
    os.chmod(document_index, 0o644)

    with pytest.raises(KnowledgeServiceError, match="read-only"):
        create().execute(_document_plan())


def test_knowledge_release_requires_a_pinned_artifact() -> None:
    with pytest.raises(ValidationError, match="at least one artifact"):
        KnowledgeRetrievalRelease()


def test_claim_schema_has_no_free_form_summary_or_numeric_claim_field() -> None:
    schema = KnowledgeAnswerDraft.model_json_schema()
    serialized = str(schema)

    assert "summary" not in serialized
    assert "answer" not in serialized
    assert "numeric_value" not in serialized


@pytest.mark.parametrize("kind", ["plan", "release", "result", "answer-draft"])
def test_knowledge_cli_exports_strict_schemas(kind: str, capsys) -> None:
    assert knowledge_cli_main(["schema", "--kind", kind]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert payload["additionalProperties"] is False


def test_knowledge_cli_rejects_ambiguous_duplicate_json_keys(tmp_path: Path) -> None:
    plan_path = tmp_path / "duplicate-plan.json"
    plan_path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")

    with pytest.raises(SystemExit, match="duplicate JSON key"):
        knowledge_cli_main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--release",
                str(plan_path),
            ]
        )
