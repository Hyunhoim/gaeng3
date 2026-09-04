"""External DB prefilter must not demand explicit 국내/해외 clauses for a bare ``ETF``.

Official example questions 3·4·5 (``캠브리콘이 편입된 중국 반도체 ETF``,
``우주항공 테마와 연결 이력이 있는 관련 ETF``, ``에코프로의 자회사를 편입한 ETF 중
순자산이 큰 상품``) name the generic ETP class once.  The sanitizer widens that to
both ETP families on purpose, so the prefilter has to apply the same DB conditions
to each market instead of failing the cross-family clause-ownership check.  The
second half pins the relation-span tiling that let ``자회사를 편입`` through.
"""

from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.routed_service import _external_control_span_is_authorized
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily

ETP = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)


@pytest.mark.parametrize(
    "question",
    [
        "캠브리콘이 편입된 중국 반도체 ETF를 알려줘",
        "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 관련 ETF를 정리해줘",
        "에코프로의 자회사를 편입한 ETF 중 순자산이 큰 상품의 위험요인 알려줘",
        "삼성전자를 담은 상장지수상품 중 총보수 낮은 순 3개",
    ],
)
def test_bare_etp_class_scopes_the_same_question_to_both_families(question: str) -> None:
    scoped = ServerQueryPlanCompiler.generic_etp_class_scope(question, ETP)

    assert scoped == {family: question for family in ETP}


@pytest.mark.parametrize(
    ("question", "families"),
    [
        # Explicit per-family clauses keep the ordinary clause-ownership scoping.
        (
            "삼성전자를 보유한 국내 ETF는 순자산 1조 이상, 해외 ETF는 AUM 10억 이상인 상품",
            ETP,
        ),
        ("삼성전자를 보유한 해외 ETF 중 총보수 0.15% 이하", ETP),
        ("삼성전자를 보유한 국내 ETF", ETP),
        # Only the two ETP families form the generic class.
        ("삼성전자 관련 투자 상품 있어?", tuple(ProductFamily)),
        ("한국주택금융공사가 발행한 채권과 공모펀드", (ProductFamily.BOND, ProductFamily.FUND)),
        # Without an ETP surface there is nothing to widen.
        ("삼성전자를 편입한 상품을 알려줘", ETP),
    ],
)
def test_generic_scope_declines_explicit_or_non_etp_requests(
    question: str,
    families: tuple[ProductFamily, ...],
) -> None:
    assert ServerQueryPlanCompiler.generic_etp_class_scope(question, families) is None


def test_explicit_cross_family_thresholds_still_use_clause_ownership() -> None:
    question = (
        "삼성전자를 보유한 국내 ETF는 순자산 1조 이상, 해외 ETF는 AUM 10억 이상인 상품을 찾아줘"
    )

    scoped = ServerQueryPlanCompiler.scope_family_questions(question, list(ETP))

    assert "1조 이상" in scoped[ProductFamily.DOMESTIC_ETP]
    assert "10억 이상" not in scoped[ProductFamily.DOMESTIC_ETP]
    assert "10억 이상" in scoped[ProductFamily.OVERSEAS_ETP]


def _requirement(
    number: int,
    capability: KnowledgeCapability,
    predicate: KnowledgePredicate,
    subject: str,
    relation: str,
) -> KnowledgeRequirement:
    return KnowledgeRequirement(
        requirement_id=f"need_{number}",
        capability=capability,
        predicate=predicate,
        subject=subject,
        subject_evidence_span=subject,
        relation_evidence_span=relation,
        product_families=ETP,
        source_registry_ids=("ecopro-corporate",),
    )


_ECOPRO_REQUIREMENTS = (
    _requirement(
        1,
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgePredicate.SUBSIDIARY_OF,
        "에코프로",
        "에코프로의 자회사",
    ),
    _requirement(
        2,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgePredicate.HOLDS_COMPANY,
        "에코프로",
        "편입한",
    ),
    _requirement(
        3,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgePredicate.HAS_RISK_FACTOR,
        "상품",
        "위험요인",
    ),
)


@pytest.mark.parametrize("span", ["자회사를 편입", "에코프로의 자회사", "편입한", "자회사"])
def test_relation_only_control_spans_are_authorized(span: str) -> None:
    assert _external_control_span_is_authorized(span, _ECOPRO_REQUIREMENTS)


@pytest.mark.parametrize(
    "span",
    ["순자산이 큰", "총보수 0.15% 이하", "1년 수익률 높은 순", "위험등급 2등급", "최근 6개월"],
)
def test_db_condition_spans_never_tile_from_relation_words(span: str) -> None:
    assert not _external_control_span_is_authorized(span, _ECOPRO_REQUIREMENTS)
