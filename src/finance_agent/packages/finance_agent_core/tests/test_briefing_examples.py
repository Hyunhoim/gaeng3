from __future__ import annotations

from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.briefing_examples import (
    BriefingAnswerability,
    BriefingDifficulty,
    BriefingEvaluationMode,
    BriefingExampleSuite,
    evaluate_briefing_example,
    load_briefing_example_suite,
)
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind


def test_briefing_example_suite_preserves_official_distribution() -> None:
    loaded = load_briefing_example_suite()

    assert loaded.suite.status == "official_examples_20260824_current_not_blind"
    assert loaded.suite.suite_version == "2.0"
    assert loaded.suite.dataset_release_id == "miraeasset-ai-festival-2026-20260824-v5"
    assert len(loaded.suite.cases) == 8
    assert Counter(case.answerability for case in loaded.suite.cases) == Counter(
        {
            BriefingAnswerability.ANSWERABLE: 5,
            BriefingAnswerability.UNANSWERABLE: 3,
        }
    )
    assert Counter(
        case.difficulty
        for case in loaded.suite.cases
        if case.answerability is BriefingAnswerability.ANSWERABLE
    ) == Counter(
        {
            BriefingDifficulty.LOW: 1,
            BriefingDifficulty.MEDIUM: 2,
            BriefingDifficulty.HIGH: 2,
        }
    )
    assert all(
        set(case.mode_expectations) == set(BriefingEvaluationMode) for case in loaded.suite.cases
    )
    assert all(
        case.mode_expectations[BriefingEvaluationMode.FINAL_CACHE_ONLY].allowed_statuses
        == (BackendStatus.SUCCESS,)
        for case in loaded.suite.cases[:5]
    )


def test_briefing_example_suite_rejects_duplicate_question() -> None:
    payload = load_briefing_example_suite().suite.model_dump(mode="json")
    payload["cases"][1]["question"] = payload["cases"][0]["question"]

    with pytest.raises(ValidationError, match="questions must be unique"):
        BriefingExampleSuite.model_validate(payload)


def test_unknown_entity_is_safely_controlled_without_evidence() -> None:
    case = load_briefing_example_suite().suite.cases[6]
    service = RoutedFinanceAgent({})
    request = BackendAgentRequest(request_id=case.id, question=case.question)

    result = evaluate_briefing_example(
        case,
        execute_answer_request(service, request),
        latency_ms=1.0,
    )

    assert result.passed
    assert result.actual_backend_status.value == "cannot_verify"
    assert result.evidence_count == 0


def test_pre_activation_cannot_verify_fails_the_final_cache_only_gate() -> None:
    case = load_briefing_example_suite().suite.cases[1]
    service = RoutedFinanceAgent({})
    adapter = execute_answer_request(
        service,
        BackendAgentRequest(request_id=case.id, question=case.question),
    )

    pre_activation = evaluate_briefing_example(
        case,
        adapter,
        latency_ms=1.0,
        mode=BriefingEvaluationMode.PRE_ACTIVATION,
    )
    final = evaluate_briefing_example(
        case,
        adapter,
        latency_ms=1.0,
        mode=BriefingEvaluationMode.FINAL_CACHE_ONLY,
    )

    assert pre_activation.passed
    assert not final.passed
    assert final.checks["mode_status_allowed"] is False
    assert final.checks["mode_evidence_contract"] is False


def test_final_cache_only_requires_capability_document_and_db_intersection() -> None:
    case = load_briefing_example_suite().suite.cases[1]
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        predicate=KnowledgePredicate.DESCRIBES_FUND_STRUCTURE_TREND,
        subject="국민성장펀드",
        subject_evidence_span="국민성장펀드",
        relation_evidence_span="구조와 투자전략 동향",
        product_families=(ProductFamily.FUND,),
        source_registry_ids=("fsc-policy",),
    )
    plan = CompositeKnowledgeQueryPlan(
        question_id=case.id,
        question=case.question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(requirement,),
        requested_product_fields=("product_name",),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    document = DocumentEvidence(
        evidence_id="document-evidence-1",
        document_id="fsc-national-growth",
        chunk_ordinal=0,
        title="국민성장펀드 구조와 동향",
        text="국민성장펀드의 구조와 투자전략 동향을 설명합니다.",
        source_uri="https://www.fsc.go.kr/po010103/86950",
        source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
        as_of=date(2026, 8, 24),
        document_sha256="a" * 64,
        metadata={"capability": "fund_structure_strategy_trend"},
        relevance_score=1.0,
    )
    product = SimpleNamespace(
        product_id="FUND-001",
        fields=[
            SimpleNamespace(
                source_key={"product_family": "fund"},
                source_dataset="fund",
            )
        ],
    )
    response = SimpleNamespace(
        request_id=case.id,
        status=BackendStatus.SUCCESS,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.FUND],
        answer="검증된 공식 자료와 제공 DB 교집합입니다.",
        query_plan=plan,
        candidate_count=1,
        products=[product],
        comparisons=[],
        aggregates=[],
        documents=[document],
        citations=[
            SourceCitation(
                citation_id="document:1",
                kind="document_chunk",
                label="공식 문서",
                source_id="fsc-national-growth",
                source_locator=document.source_uri,
                as_of=date(2026, 8, 24),
                evidence_refs=[document.evidence_id],
            ),
            SourceCitation(
                citation_id="product:1",
                kind="product_field",
                label="제공 DB 상품",
                source_id="PRFD01N001",
                source_locator="provided DB",
                as_of=date(2026, 8, 24),
                evidence_refs=["provided:fund:FUND-001:product_name"],
            ),
        ],
        as_of_dates=[date(2026, 8, 24)],
        warnings=[],
        answer_mode=BackendAnswerMode.LLM_GROUNDED,
        fallback_used=False,
        provider_model="HCX-007",
        clarification=None,
        error=None,
    )
    adapter = SimpleNamespace(http_status_code=200, response=response)

    result = evaluate_briefing_example(
        case,
        adapter,
        latency_ms=1.0,
        mode=BriefingEvaluationMode.FINAL_CACHE_ONLY,
    )

    assert result.passed
    assert result.returned_product_ids == ["FUND-001"]
    assert result.observed_external_capabilities == [
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    ]

    response.product_families = [ProductFamily.FUND, ProductFamily.DOMESTIC_ETP]
    over_broad = evaluate_briefing_example(
        case,
        adapter,
        latency_ms=1.0,
        mode=BriefingEvaluationMode.FINAL_CACHE_ONLY,
    )
    assert over_broad.checks["product_families_exact"] is False
    assert not over_broad.passed
