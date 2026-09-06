from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundAuthorizationReceipt,
    CompoundExecution,
    CompoundFamilyPlan,
    CompoundMetric,
    TypedCompoundPlan,
    _compound_plan_sha256,
)
from finance_agent_core.agent.hybrid_query import (
    HybridClauseOwner,
    HybridExecutionAborted,
    HybridPreflightBlocked,
    HybridPreflightCode,
    HybridQueryCoordinator,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    ExternalKnowledgeOutcome,
    ExternalKnowledgeOutcomeStatus,
    ExternalKnowledgeSearchPlan,
    ExternalSearchBudget,
    ExternalSearchExecutionReceipt,
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntimeResult,
)

QUESTION = (
    "공식 자료에서 삼성전자를 편입한 국내 ETF를 확인하고 순자산을 연결해 주세요. "
    "별도 비교군으로 채권종류가 일반회사채인 국내채권에서 듀레이션 상위 5개를 "
    "먼저 추린 다음 발행잔액 큰 순으로 2개를 뽑되 ETF와 통합 순위를 만들지는 "
    "말아 주세요. 자료 날짜와 ETF 위험요인도 설명해 주세요."
)

DOCUMENT_TABLE_QUESTION = (
    "삼성 KODEX 2026년 2분기 가이드에서 로봇 ETF 설명을 확인해 주세요. "
    "같은 자료의 상위 10개에 LS ELECTRIC과 효성중공업이 있는지와 비중 합계를 "
    "검증해 주세요. 별도 비교군으로 일반회사채 국내채권에서 듀레이션 상위 5개를 "
    "먼저 추린 다음 발행잔액 큰 순으로 2개를 뽑되 ETF와 통합 순위를 만들지는 "
    "말아 주세요. 자료 날짜와 ETF 위험요인도 설명해 주세요."
)


def _external_plan(question: str, request_id: str) -> ExternalKnowledgeSearchPlan:
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate=KnowledgePredicate.HOLDS_COMPANY,
        subject="삼성전자",
        subject_evidence_span="삼성전자",
        relation_evidence_span="편입",
        product_families=(ProductFamily.DOMESTIC_ETP,),
        source_registry_ids=("official_asset_manager",),
    )
    composite = CompositeKnowledgeQueryPlan(
        question_id=request_id,
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(requirement,),
        requested_product_fields=("aum", "product_name"),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    budget = ExternalSearchBudget()
    execution_receipt = ExternalSearchExecutionReceipt.bind(
        composite_plan=composite,
        candidate_universe_sha256="a" * 64,
        candidate_count=1,
        source_registry_sha256="b" * 64,
        budget=budget,
    )
    return ExternalKnowledgeSearchPlan(
        composite_plan=composite,
        candidate_universe_sha256="a" * 64,
        candidate_count=1,
        source_registry_sha256="b" * 64,
        budget=budget,
        execution_receipt=execution_receipt,
    )


def _database_authority(
    question: str,
    request_id: str,
    *,
    family: ProductFamily = ProductFamily.BOND,
    kind: str = "two_stage",
) -> tuple[TypedCompoundPlan, CompoundAuthorizationReceipt]:
    family_plan = CompoundFamilyPlan(
        product_family=family,
        filters=(),
        metric=CompoundMetric(kind="field", source_fields=("issue_amount",)),
        direction=SortDirection.DESC,
        limit=2,
        shortlist_metric=CompoundMetric(
            kind="field",
            source_fields=("duration_years",),
        ),
        shortlist_limit=5,
    )
    plan = TypedCompoundPlan(
        question_id=request_id,
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        kind=kind,
        family_plans=(family_plan,),
    )
    receipt = CompoundAuthorizationReceipt(
        request_id=request_id,
        question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
        plan_sha256=_compound_plan_sha256(plan),
        prior_reason_code="typed_compound_plan_authorized",
        covered_unresolved_spans=(),
        canonical_clause_ids=(),
        validated_field_ids=("duration_years", "issue_amount"),
        authorized_intent=InteractionIntent.SEARCH.value,
        authorized_families=(family.value,),
        authorized_query_plan_intent="search",
        authority_mac="f" * 64,
    )
    return plan, receipt


def _preflight(question: str = QUESTION):
    events: list[str] = []
    coordinator = HybridQueryCoordinator()

    def authorize_external(clause, request_id):
        events.append("authorize_external")
        return _external_plan(clause.question, request_id)

    def authorize_database(clause, request_id):
        events.append("authorize_database")
        return _database_authority(clause.question, request_id)

    receipt = coordinator.preflight(
        question,
        "hybrid-1",
        authorize_external=authorize_external,
        authorize_database=authorize_database,
    )
    assert receipt is not None
    return coordinator, receipt, events


def _external_result(
    plan: ExternalKnowledgeSearchPlan,
    status: ExternalKnowledgeOutcomeStatus,
) -> ExternalKnowledgeRuntimeResult:
    complete = status in {
        ExternalKnowledgeOutcomeStatus.SUCCESS,
        ExternalKnowledgeOutcomeStatus.NOT_FOUND,
    }
    if status is ExternalKnowledgeOutcomeStatus.SUCCESS:
        verified_keys = ((ProductFamily.DOMESTIC_ETP, "KR7000000001"),)
        verified_ids = ("KR7000000001",)
    else:
        verified_keys = ()
        verified_ids = ()
    outcome = ExternalKnowledgeOutcome(
        status=status,
        search_complete=complete,
        verified_product_keys=verified_keys,
        verified_product_ids=verified_ids,
        reason_codes=("test_outcome",),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(),
        database_sha256s={ProductFamily.DOMESTIC_ETP: "d" * 64},
        candidate_filter_sha256="e" * 64,
    )
    return ExternalKnowledgeRuntimeResult(
        outcome=outcome,
        search_plan=plan,
        candidate_universe=universe,
        acquisitions=(),
        execution=None,
    )


def test_partition_preserves_exact_offset_hash_ownership_and_external_tail() -> None:
    partition = HybridQueryCoordinator().partition(QUESTION)

    assert partition is not None
    assert "삼성전자를 편입" in partition.external.question
    assert "ETF 위험요인" in partition.external.question
    assert "일반회사채인 국내채권" in partition.database.question
    assert "ETF 위험요인" not in partition.database.question
    assert "ETF와 통합 순위" not in partition.database.question
    control_text = "".join(
        span.text
        for span in partition.spans
        if span.owner is HybridClauseOwner.CONTROL
    )
    assert "통합 순위" in control_text
    assert [span.owner for span in partition.spans] == [
        HybridClauseOwner.EXTERNAL_EVIDENCE,
        HybridClauseOwner.CONTROL,
        HybridClauseOwner.DATABASE_COMPOUND,
        HybridClauseOwner.CONTROL,
        HybridClauseOwner.EXTERNAL_EVIDENCE,
    ]
    partition.validate()


def test_partition_recognizes_official_document_table_without_relation_verb() -> None:
    partition = HybridQueryCoordinator().partition(DOCUMENT_TABLE_QUESTION)

    assert partition is not None
    assert "같은 자료의 상위 10개" in partition.external.question
    assert "비중 합계" in partition.external.question
    assert "일반회사채 국내채권" in partition.database.question
    assert "ETF와 통합 순위" not in partition.database.question
    assert {"듀레이션", "발행잔액"} <= set(partition.database.question.split())
    partition.validate()


def test_sanitized_literal_hint_can_classify_unseen_external_wording() -> None:
    question = (
        "발행사가 공개한 표에서 알파기업 노출값을 교차 확인해 주세요. "
        "별도 비교군으로 일반회사채 국내채권에서 듀레이션 상위 5개를 먼저 추린 "
        "다음 발행잔액 큰 순으로 2개를 뽑고 두 결과는 합치지 말아 주세요."
    )
    hint_text = "알파기업 노출값"
    hint_start = question.index(hint_text)
    partition = HybridQueryCoordinator().partition(
        question,
        external_span_hints=((hint_start, hint_start + len(hint_text)),),
    )

    assert partition is not None
    assert hint_text in partition.external.question
    assert hint_text not in partition.database.question
    partition.validate()


def test_preflight_returns_authority_only_after_both_authorizers_succeed() -> None:
    _coordinator, receipt, events = _preflight()

    assert events == ["authorize_external", "authorize_database"]
    assert receipt.external_plan.execution_receipt is not None
    assert receipt.database_receipt.plan_sha256 == receipt.database_plan_sha256


def test_preflight_failure_never_returns_partial_authority() -> None:
    events: list[str] = []

    def authorize_external(clause, request_id):
        events.append("authorize_external")
        return _external_plan(clause.question, request_id)

    def reject_database(_clause, _request_id):
        events.append("authorize_database")
        raise HybridPreflightBlocked(
            HybridPreflightCode.DATABASE_AUTHORITY_MISSING,
            "no compound authority",
        )

    with pytest.raises(HybridPreflightBlocked) as captured:
        HybridQueryCoordinator().preflight(
            QUESTION,
            "hybrid-1",
            authorize_external=authorize_external,
            authorize_database=reject_database,
        )

    assert captured.value.code is HybridPreflightCode.DATABASE_AUTHORITY_MISSING
    assert events == ["authorize_external", "authorize_database"]


def test_preflight_does_not_downgrade_infrastructure_failure_to_clarify() -> None:
    events: list[str] = []

    def authorize_external(clause, request_id):
        events.append("authorize_external")
        return _external_plan(clause.question, request_id)

    def fail_database(_clause, _request_id):
        events.append("authorize_database")
        raise TimeoutError("database deadline exceeded")

    with pytest.raises(TimeoutError, match="database deadline exceeded"):
        HybridQueryCoordinator().preflight(
            QUESTION,
            "hybrid-infrastructure-error",
            authorize_external=authorize_external,
            authorize_database=fail_database,
        )

    assert events == ["authorize_external", "authorize_database"]


def test_integrated_global_ranking_is_rejected_before_authorization() -> None:
    question = (
        "공식 자료에서 삼성전자를 편입한 국내 ETF를 확인해 주세요. "
        "별도 비교군으로 일반회사채 국내채권도 찾아 두 결과를 통합 순위로 보여주세요."
    )
    called = False

    def should_not_run(*_args):
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(HybridPreflightBlocked) as captured:
        HybridQueryCoordinator().preflight(
            question,
            "hybrid-rank",
            authorize_external=should_not_run,
            authorize_database=should_not_run,
        )

    assert captured.value.code is HybridPreflightCode.INTEGRATED_RANKING_FORBIDDEN
    assert not called


def test_overlapping_product_family_authority_is_rejected() -> None:
    def authorize_external(clause, request_id):
        return _external_plan(clause.question, request_id)

    def authorize_database(clause, request_id):
        return _database_authority(
            clause.question,
            request_id,
            family=ProductFamily.DOMESTIC_ETP,
        )

    with pytest.raises(HybridPreflightBlocked) as captured:
        HybridQueryCoordinator().preflight(
            QUESTION,
            "hybrid-overlap",
            authorize_external=authorize_external,
            authorize_database=authorize_database,
        )

    assert captured.value.code is HybridPreflightCode.FAMILY_SCOPE_OVERLAP


def test_tampered_source_span_is_rejected() -> None:
    partition = HybridQueryCoordinator().partition(QUESTION)
    assert partition is not None
    changed = replace(partition.spans[0], text="변조된 절")
    tampered = replace(partition, spans=(changed, *partition.spans[1:]))

    with pytest.raises(HybridPreflightBlocked) as captured:
        tampered.validate()

    assert captured.value.code is HybridPreflightCode.CLAUSE_OWNERSHIP_INCOMPLETE


def test_atomic_execution_exposes_both_complete_sections_in_order() -> None:
    coordinator, receipt, events = _preflight()

    def execute_external(plan):
        events.append("execute_external")
        return _external_result(plan, ExternalKnowledgeOutcomeStatus.NOT_FOUND)

    def execute_database(plan, _authority):
        events.append("execute_database")
        return CompoundExecution(plan=plan, families=(), answer="검증된 DB 결과")

    result = coordinator.execute_atomic(
        receipt,
        execute_external=execute_external,
        execute_database=execute_database,
    )

    assert events == [
        "authorize_external",
        "authorize_database",
        "execute_external",
        "execute_database",
    ]
    assert result.external.outcome.status is ExternalKnowledgeOutcomeStatus.NOT_FOUND
    assert result.database.answer == "검증된 DB 결과"


def test_incomplete_external_evidence_returns_no_partial_bundle_or_db_execution() -> None:
    coordinator, receipt, events = _preflight()

    def execute_external(plan):
        events.append("execute_external")
        return _external_result(plan, ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY)

    def execute_database(_plan, _authority):
        events.append("execute_database")
        raise AssertionError("DB execution must not start")

    with pytest.raises(HybridExecutionAborted) as captured:
        coordinator.execute_atomic(
            receipt,
            execute_external=execute_external,
            execute_database=execute_database,
        )

    assert captured.value.reason_code == "hybrid_external_evidence_incomplete"
    assert events == [
        "authorize_external",
        "authorize_database",
        "execute_external",
    ]


def test_execution_abort_preserves_failure_cause_without_partial_result() -> None:
    coordinator, receipt, events = _preflight()
    timeout = TimeoutError("official source deadline exceeded")

    def execute_external(_plan):
        events.append("execute_external")
        raise timeout

    def execute_database(_plan, _authority):
        events.append("execute_database")
        raise AssertionError("DB execution must not start")

    with pytest.raises(HybridExecutionAborted) as captured:
        coordinator.execute_atomic(
            receipt,
            execute_external=execute_external,
            execute_database=execute_database,
        )

    assert captured.value.reason_code == "hybrid_external_execution_failed"
    assert captured.value.cause is timeout
    assert events == ["authorize_external", "authorize_database", "execute_external"]
