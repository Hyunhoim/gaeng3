from __future__ import annotations

import pytest

from finance_agent_core.agent.routed_service import _external_route_covers_ambiguity
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import KnowledgeIntentRequirement


def _requirement(
    capability: KnowledgeCapability, subject: str, relation: str
) -> KnowledgeIntentRequirement:
    return KnowledgeIntentRequirement(
        capability=capability, subject_span=subject, relation_span=relation
    )


HOLDING = _requirement(KnowledgeCapability.PORTFOLIO_HOLDINGS, "KODEX 반도체", "구성종목")
RISK = _requirement(KnowledgeCapability.PRODUCT_RISK_FACTORS, "KODEX 반도체", "위험요인")


def test_branded_product_subject_is_owned_by_the_relation() -> None:
    """The gate flags a brand name as a possible filter; here it is the subject."""

    assert _external_route_covers_ambiguity(
        "KODEX 반도체",
        question="KODEX 반도체 구성종목 상위 5개 알려줘",
        requirements=(HOLDING,),
        product_mentions=(),
    )


def test_branded_name_without_identity_surface_is_left_to_the_db_intersection() -> None:
    assert _external_route_covers_ambiguity(
        "KODEX 미국AI전력핵심인프라",
        question="KODEX 미국AI전력핵심인프라 구성종목 알려줘",
        requirements=(
            _requirement(KnowledgeCapability.PORTFOLIO_HOLDINGS, "미국AI전력", "구성종목"),
        ),
        product_mentions=(),
    )


def test_product_mention_alone_covers_the_span() -> None:
    assert _external_route_covers_ambiguity(
        "TIGER 반도체TOP10",
        question="TIGER 반도체TOP10에 편입된 종목",
        requirements=(HOLDING,),
        product_mentions=("TIGER 반도체TOP10",),
    )


@pytest.mark.parametrize(
    ("question", "covered"),
    [
        ("에코프로 자회사를 편입한 ETF 중 순자산 상위 3개", True),
        ("에코프로 자회사를 편입한 ETF 상위 3개", False),
    ],
)
def test_direction_word_needs_a_metric_the_external_plan_can_bind(
    question: str, covered: bool
) -> None:
    requirement = _requirement(
        KnowledgeCapability.CORPORATE_SUBSIDIARY, "에코프로", "자회사를 편입한"
    )
    assert (
        _external_route_covers_ambiguity(
            "상위", question=question, requirements=(requirement,), product_mentions=()
        )
        is covered
    )


def test_bare_risk_word_is_covered_only_by_a_risk_factor_requirement() -> None:
    question = "KODEX 반도체 위험요인 알려줘"
    assert _external_route_covers_ambiguity(
        "위험", question=question, requirements=(RISK,), product_mentions=()
    )
    assert not _external_route_covers_ambiguity(
        "위험", question=question, requirements=(HOLDING,), product_mentions=()
    )


@pytest.mark.parametrize("span", ["가격 매력", "안정적인 ETF", "대표적인", "  "])
def test_genuine_db_ambiguities_keep_clarify(span: str) -> None:
    assert not _external_route_covers_ambiguity(
        span,
        question=f"KODEX 반도체 구성종목 중 {span} 상품",
        requirements=(HOLDING, RISK),
        product_mentions=("KODEX 반도체",),
    )
