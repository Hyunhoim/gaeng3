from __future__ import annotations

import hashlib
from datetime import date

import pytest

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.external_knowledge import (
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import VerifiedLiveRelationExtractor


def _draft(*families: ProductFamily) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(families),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )


def _compile(question: str, *families: ProductFamily):
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(*families),
    )
    return CompositeKnowledgePlanCompiler().compile(
        question,
        "external-vocabulary-generalization",
        sanitized,
    )


@pytest.mark.parametrize(
    ("question", "expected_capabilities"),
    [
        (
            "청람그룹 계열기업을 담고 있는 ETF 중 순자산이 큰 상품의 위험요인을 알려줘.",
            (
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        ),
        (
            "청람그룹 소속 회사를 포함한 상장지수펀드 가운데 규모가 가장 큰 상품의 "
            "주의사항은?",
            (
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        ),
        (
            "지난 반년 동안 우주산업과 연계 이력이 확인되는 상장지수상품을 정리해줘.",
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "위성 산업과 접점이 확인된 ETF를 최근 넉 달 자료로 찾아줘.",
            (KnowledgeCapability.SECTOR_THEME_HISTORY,),
        ),
        (
            "새봄성장펀드의 조성 구조와 모집 진행상황, 앞으로 운용 계획을 설명해줘.",
            (
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            ),
        ),
        (
            "푸른혁신펀드의 설계 방식과 자금 배분 구조, 투자 방침을 공식 자료로 알려줘.",
            (
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            ),
        ),
    ],
)
def test_unseen_relation_vocabulary_compiles_without_entity_specific_rules(
    question: str,
    expected_capabilities: tuple[KnowledgeCapability, ...],
) -> None:
    plan = _compile(
        question,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.FUND,
    )

    assert tuple(item.capability for item in plan.requirements) == expected_capabilities
    assert all(item.subject_evidence_span in question for item in plan.requirements)
    assert all(item.relation_evidence_span in question for item in plan.requirements)


def test_corporate_holdings_risk_chain_keeps_typed_dependencies() -> None:
    plan = _compile(
        "청람그룹 관계회사를 보유한 ETF 중 순자산이 큰 상품의 유의사항을 알려줘.",
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    )

    corporate, holdings, risk = plan.requirements
    assert corporate.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
    assert corporate.subject == "청람그룹"
    assert holdings.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    assert holdings.subject == "청람그룹"
    assert holdings.depends_on == (corporate.requirement_id,)
    assert risk.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    assert risk.depends_on == (holdings.requirement_id,)


@pytest.mark.parametrize(
    ("question", "expected_start"),
    [
        (
            "지난 반년 동안 우주산업과 연계 이력이 확인되는 상장지수상품을 정리해줘.",
            date(2026, 2, 24),
        ),
        (
            "위성 산업과 접점이 확인된 ETF를 최근 넉 달 자료로 찾아줘.",
            date(2026, 4, 24),
        ),
        (
            "친환경 테마와 연결된 ETF를 지난 석 달 자료에서 확인해줘.",
            date(2026, 5, 24),
        ),
    ],
)
def test_korean_relative_time_is_normalized_against_signed_cutoff(
    question: str,
    expected_start: date,
) -> None:
    plan = _compile(
        question,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    )

    assert {item.time_range.start for item in plan.requirements if item.time_range} == {
        expected_start
    }
    assert {item.time_range.end for item in plan.requirements if item.time_range} == {
        date(2026, 8, 24)
    }


def test_korean_exchange_traded_fund_scope_is_not_misclassified_as_public_fund() -> None:
    plan = _compile(
        "로봇 산업과 연계된 상장지수펀드를 정리해줘.",
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.FUND,
    )

    assert all(
        set(item.product_families)
        == {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
        for item in plan.requirements
    )


@pytest.mark.parametrize(
    "question",
    [
        "위험등급이 낮은 국내 ETF를 순자산 큰 순으로 보여줘.",
        "운용사가 청람자산운용인 공모펀드를 찾아줘.",
        "총보수가 낮은 상장지수상품을 세 개 알려줘.",
        "원금 보장 상품으로 바꿔서 추천해줘.",
    ],
)
def test_plain_db_or_unsafe_language_does_not_gain_external_search_authority(
    question: str,
) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
            ProductFamily.FUND,
        ),
    )

    assert sanitized.knowledge_requirements == []


@pytest.mark.parametrize(
    "question",
    [
        "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 관련 ETF를 정리해줘.",
        "지난 반년 동안 우주산업과 연계 이력이 확인되는 상장지수상품을 정리해줘.",
        "청람그룹 계열기업을 담고 있는 ETF 중 순자산이 큰 상품의 위험요인을 알려줘.",
        "위성 산업과 접점이 확인된 ETF를 최근 넉 달 자료로 찾아줘.",
    ],
)
def test_real_agent_routes_new_relation_language_to_bounded_knowledge_search(
    sample_database,
    domestic_sample_database,
    question: str,
) -> None:
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="external-vocabulary-route", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.products == []
    assert result.response.query_plan is not None


@pytest.mark.parametrize(
    ("official_text", "expected_count"),
    [
        (
            "새봄성장펀드는 8월 1일 모집을 시작했으며 현재 모집 진행 현황은 "
            "계획대로 완료 단계입니다.",
            1,
        ),
        ("새봄성장펀드는 8월 1일 모집을 시작했습니다.", 0),
        ("새봄성장펀드의 현재 운용 현황은 안정적입니다.", 0),
    ],
)
def test_compound_fund_dimension_requires_both_sales_and_status_evidence(
    official_text: str,
    expected_count: int,
) -> None:
    question = "새봄성장펀드의 모집 진행상황을 공식 자료로 알려줘."
    requirement = _compile(question, ProductFamily.FUND).requirements[0]
    registry = load_official_source_registry()
    source_uri = "https://www.fsc.go.kr/po010103/86950"
    content = official_text.encode()
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(
            source_uri,
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        ),
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_uri=source_uri,
        title="새봄성장펀드 공식 자료",
        content_type="text/plain",
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        published_at=date(2026, 8, 20),
        as_of=date(2026, 8, 20),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.FUND,
                product_id="FUND-SAEBOM-A",
                product_name="새봄성장펀드증권투자신탁(주식)A",
            ),
        ),
        database_sha256s={ProductFamily.FUND: "a" * 64},
        candidate_filter_sha256="b" * 64,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert len(relations) == expected_count
