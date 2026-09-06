"""External DB prefilter must not demand explicit 국내/해외 clauses for a bare ``ETF``.

Official example questions 3·4·5 (``캠브리콘이 편입된 중국 반도체 ETF``,
``우주항공 테마와 연결 이력이 있는 관련 ETF``, ``에코프로의 자회사를 편입한 ETF 중
순자산이 큰 상품``) name the generic ETP class once.  The sanitizer widens that to
both ETP families on purpose, so the prefilter has to apply the same DB conditions
to each market instead of failing the cross-family clause-ownership check.  The
second half pins the relation-span tiling that let ``자회사를 편입`` through.
"""

from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.agent.compiler import PlanCompilationBlockedError, ServerQueryPlanCompiler
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentExecutionClause,
    SemanticFrameDirection,
    SemanticFrameOperation,
    SemanticFrameOperator,
    SemanticFrameStatus,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanCompiler,
    _deduplicate_structural_requirements,
    _external_product_result_limit,
    _requirement_families,
)
from finance_agent_core.agent.product_family_language import (
    explicit_etp_market_families,
    has_standalone_bond_product_scope,
)
from finance_agent_core.agent.routed_service import (
    _external_control_span_is_authorized,
    _external_db_family_scope_groups,
    _external_db_prefilter_questions,
    _external_identity_family_scope,
    _external_rank_frames_by_authority,
    _external_rank_validation_surface,
    _external_route_covers_ambiguity,
    _external_semantic_prefilter_questions,
    _scope_external_semantic_draft,
    _validated_external_final_rank_frames,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts import MinimalQueryDraft, RouteDecision, RouteDisposition
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeDocumentTopNScope,
    KnowledgeIntentRequirement,
    KnowledgePredicate,
    KnowledgeProductSelection,
    KnowledgeRequirement,
    KnowledgeTimeRange,
)
from finance_agent_core.contracts.queryplan import Intent, ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent

ETP = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("국내 상장 ETF", (ProductFamily.DOMESTIC_ETP,)),
        ("국내 채권형 ETF", (ProductFamily.DOMESTIC_ETP,)),
        ("국내 반도체 ETF", (ProductFamily.DOMESTIC_ETP,)),
        ("해외 액티브 ETF", (ProductFamily.OVERSEAS_ETP,)),
        ("해외 채권 ETF", (ProductFamily.OVERSEAS_ETP,)),
        ("국내·해외 ETF", ETP),
        ("국내 및 해외 ETF", ETP),
        ("국내 및 해외 반도체 ETF", ETP),
        ("국내와 해외 상장 ETF", ETP),
        ("국내 ETF와 해외 채권형 ETF", ETP),
        ("국내 증시에 상장된 ETF", (ProductFamily.DOMESTIC_ETP,)),
        ("해외 시장에 상장된 ETF", (ProductFamily.OVERSEAS_ETP,)),
    ],
)
def test_shared_etp_market_parser_supports_clause_local_modifiers(
    question: str,
    expected: tuple[ProductFamily, ...],
) -> None:
    assert set(explicit_etp_market_families(question)) == set(expected)


def test_shared_etp_market_parser_does_not_cross_an_opposite_market_clause() -> None:
    assert explicit_etp_market_families("국내 주식과 해외 ETF") == (
        ProductFamily.OVERSEAS_ETP,
    )


def test_shared_etp_market_parser_does_not_treat_underlying_region_as_listing() -> None:
    assert explicit_etp_market_families("국내 주식을 추종하는 미국 ETF") == ()


@pytest.mark.parametrize("surface", ["국내 채권형 ETF", "해외 채권 ETF", "회사채 ETF"])
def test_bond_etp_modifier_is_not_a_standalone_bond_product(surface: str) -> None:
    assert not has_standalone_bond_product_scope(surface)


def test_real_bond_clause_is_preserved_next_to_a_bond_etp() -> None:
    assert has_standalone_bond_product_scope("국내채권과 해외 채권 ETF")


@pytest.mark.parametrize(
    ("question", "lexical_families", "expected"),
    [
        (
            "공식 편입표에서 A를 담은 국내 반도체 ETF",
            ETP,
            (ProductFamily.DOMESTIC_ETP,),
        ),
        (
            "공식 편입표에서 A를 담은 해외 채권 ETF",
            (ProductFamily.BOND, ProductFamily.OVERSEAS_ETP),
            (ProductFamily.OVERSEAS_ETP,),
        ),
        (
            "공식 편입표에서 A를 담은 국내·해외 ETF",
            (ProductFamily.DOMESTIC_ETP,),
            ETP,
        ),
    ],
)
def test_external_identity_scope_uses_shared_explicit_market_authority(
    question: str,
    lexical_families: tuple[ProductFamily, ...],
    expected: tuple[ProductFamily, ...],
) -> None:
    assert set(_external_identity_family_scope(question, lexical_families)) == set(expected)


def test_requirement_scope_does_not_add_bond_for_bond_etf_asset_type() -> None:
    assert _requirement_families(
        "A",
        "해외 채권 ETF에 편입된",
        (ProductFamily.BOND, ProductFamily.OVERSEAS_ETP),
    ) == (ProductFamily.OVERSEAS_ETP,)


def test_external_db_family_group_owns_a_bond_etf_only_as_etp() -> None:
    groups = _external_db_family_scope_groups("해외 채권 ETF는 AUM 10억 이상")

    assert len(groups) == 1
    assert groups[0][2] == frozenset((ProductFamily.OVERSEAS_ETP,))


@pytest.mark.parametrize(
    ("question", "fallback_families", "expected"),
    [
        (
            "A를 편입한 국내 채권형 ETF",
            (ProductFamily.DOMESTIC_ETP,),
            (ProductFamily.DOMESTIC_ETP,),
        ),
        (
            "A를 편입한 해외 채권 ETF",
            (ProductFamily.BOND, ProductFamily.OVERSEAS_ETP),
            (ProductFamily.OVERSEAS_ETP,),
        ),
        (
            "A를 편입한 국내·해외 ETF",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            ETP,
        ),
        (
            "국내채권과 A를 편입한 해외 채권 ETF",
            (ProductFamily.BOND, ProductFamily.OVERSEAS_ETP),
            (ProductFamily.BOND, ProductFamily.OVERSEAS_ETP),
        ),
    ],
)
def test_intent_sanitizer_preserves_only_server_grounded_family_scope(
    question: str,
    fallback_families: tuple[ProductFamily, ...],
    expected: tuple[ProductFamily, ...],
) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(fallback_families),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=fallback_families,
    )

    assert set(sanitized.product_families) == set(expected)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "공식 상위 10개 편입 내역에서 회사 A를 보유한 ETF 중 "
            "총보수가 낮은 상품을 골라줘.",
            1,
        ),
        (
            "공식 구성종목 상위 10개에서 회사 A를 보유한 ETF를 "
            "총보수 낮은 순으로 3개 알려줘.",
            3,
        ),
        (
            "상위 10개 종목 중 회사 A를 보유한 ETF를 AUM 높은 순으로 두 개 알려줘.",
            2,
        ),
    ],
)
def test_document_table_size_never_changes_final_product_limit(
    question: str,
    expected: int,
) -> None:
    assert _external_product_result_limit(question) == expected


def test_unqualified_external_etf_identity_scope_ignores_domestic_router_default() -> None:
    assert _external_identity_family_scope(
        "ALFA와 BETA의 공식 편입표를 확인한 ETF를 찾아줘.",
        (ProductFamily.DOMESTIC_ETP,),
    ) == ETP


def test_unqualified_external_etf_identity_scope_preserves_other_families() -> None:
    assert set(
        _external_identity_family_scope(
            "공모펀드와 ETF의 공식 자료를 비교해줘.",
            (ProductFamily.FUND, ProductFamily.DOMESTIC_ETP),
        )
    ) == {
        ProductFamily.FUND,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }


def test_explicit_external_etf_market_keeps_lexical_scope() -> None:
    assert _external_identity_family_scope(
        "ALFA의 공식 편입표를 확인한 해외 ETF를 찾아줘.",
        (ProductFamily.OVERSEAS_ETP,),
    ) == (ProductFamily.OVERSEAS_ETP,)


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


def test_external_document_prefilter_projects_only_family_owned_db_clauses() -> None:
    question = (
        "공식 팩트시트의 구성표를 대조해 회사 A를 담은 ETF를 찾아주세요. "
        "주최측 제공 DB와 연결한 뒤 해외 ETF는 연간보수율 0.69 이하, "
        "국내 ETF는 추적오차율 0.13 이하만 남기고, "
        "국내·해외 ETF를 나눠 순자산 큰 순으로 3개를 알려주세요."
    )

    scoped = _external_db_prefilter_questions(question, ETP)

    assert scoped is not None
    assert "추적오차율 0.13 이하" in scoped[ProductFamily.DOMESTIC_ETP]
    assert "연간보수율 0.69 이하" not in scoped[ProductFamily.DOMESTIC_ETP]
    assert "연간보수율 0.69 이하" in scoped[ProductFamily.OVERSEAS_ETP]
    assert "추적오차율 0.13 이하" not in scoped[ProductFamily.OVERSEAS_ETP]
    assert all("순자산 큰 순" in value for value in scoped.values())
    assert all("팩트시트" not in value and "구성표" not in value for value in scoped.values())
    plans = {}
    for family, scoped_question in scoped.items():
        plans[family] = ServerQueryPlanCompiler({}).compile(
            RouteDecision(
                draft=MinimalQueryDraft(
                    request_id=f"prefilter-{family.value}",
                    question=scoped_question,
                    intent=InteractionIntent.SEARCH,
                    product_families=[family],
                    product_mentions=[],
                    requested_limit=None,
                ),
                disposition=RouteDisposition.EXECUTE,
                reason_code="test_external_prefilter",
                reason="test",
                query_plan_intent=Intent.SEARCH,
                capability_matrix_version="test",
            )
        )
    assert {item.field for item in plans[ProductFamily.DOMESTIC_ETP].constraints} >= {
        "tracking_error_pct",
        "product_type",
    }
    assert "total_expense_ratio_pct" not in {
        item.field for item in plans[ProductFamily.DOMESTIC_ETP].constraints
    }
    assert {item.field for item in plans[ProductFamily.OVERSEAS_ETP].constraints} >= {
        "total_expense_ratio_pct",
        "product_type",
    }
    assert all(
        not plan.ambiguities and not plan.unsupported_conditions
        for plan in plans.values()
    )


def test_external_document_prefilter_splits_unpunctuated_family_clauses() -> None:
    scoped = _external_db_prefilter_questions(
        "공식 자료를 확인하고 주최측 제공 DB에서 해외 ETF는 연간보수율 0.7 이하이고 "
        "국내 ETF는 추적오차율 1.0 이하인 상품을 찾아줘.",
        ETP,
    )

    assert scoped is not None
    assert "연간보수율 0.7 이하" in scoped[ProductFamily.OVERSEAS_ETP]
    assert "추적오차율 1.0 이하" not in scoped[ProductFamily.OVERSEAS_ETP]
    assert "추적오차율 1.0 이하" in scoped[ProductFamily.DOMESTIC_ETP]
    assert "연간보수율 0.7 이하" not in scoped[ProductFamily.DOMESTIC_ETP]


@pytest.mark.parametrize(
    "combined_scope",
    [
        "국내 ETF와 해외 ETF",
        "해외 ETF 및 국내 ETF",
        "국내와 해외 ETF",
        "해외·국내 ETF",
    ],
)
def test_external_document_prefilter_replicates_combined_family_filter(
    combined_scope: str,
) -> None:
    scoped = _external_db_prefilter_questions(
        f"공식 자료를 확인하고 주최측 제공 DB에서 {combined_scope}는 "
        "순자산 10억 이상만 찾아줘.",
        ETP,
    )

    assert scoped is not None
    assert all("순자산 10억 이상" in value for value in scoped.values())


@pytest.mark.parametrize(
    ("question", "families", "expected", "excluded"),
    [
        (
            "공식 자료를 확인하고 주최측 제공 DB에서 국내 ETF는 순자산 10억 이상이고 "
            "공모펀드는 위험등급 3등급 이하만 찾아줘.",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
            {
                ProductFamily.DOMESTIC_ETP: "순자산 10억 이상",
                ProductFamily.FUND: "위험등급 3등급 이하",
            },
            {
                ProductFamily.DOMESTIC_ETP: "위험등급 3등급 이하",
                ProductFamily.FUND: "순자산 10억 이상",
            },
        ),
        (
            "공식 자료와 제공 DB를 대조해 공모펀드는 순자산 10억 이상이며 "
            "국내채권은 신용등급 AA 이상만 찾아줘.",
            (ProductFamily.FUND, ProductFamily.BOND),
            {
                ProductFamily.FUND: "순자산 10억 이상",
                ProductFamily.BOND: "신용등급 AA 이상",
            },
            {
                ProductFamily.FUND: "신용등급 AA 이상",
                ProductFamily.BOND: "순자산 10억 이상",
            },
        ),
    ],
)
def test_external_prefilter_splits_all_family_pairs_without_punctuation(
    question: str,
    families: tuple[ProductFamily, ...],
    expected: dict[ProductFamily, str],
    excluded: dict[ProductFamily, str],
) -> None:
    scoped = _external_db_prefilter_questions(question, families)

    assert scoped is not None
    for family in families:
        assert expected[family] in scoped[family]
        assert excluded[family] not in scoped[family]


def test_external_prefilter_replicates_a_combined_etp_and_fund_filter() -> None:
    families = (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND)
    scoped = _external_db_prefilter_questions(
        "공식 자료와 제공 DB를 대조해 국내 ETF와 공모펀드는 "
        "순자산 10억 이상만 찾아줘.",
        families,
    )

    assert scoped is not None
    assert all("순자산 10억 이상" in scoped[family] for family in families)


def test_external_db_projection_keeps_unknown_db_condition_fail_closed() -> None:
    scoped = _external_db_prefilter_questions(
        "공식 문서를 확인한 뒤 제공 DB에서 가격 매력이 높은 ETF만 남겨줘.",
        ETP,
    )

    assert scoped is not None
    assert all("가격 매력이 높은" in value for value in scoped.values())


def test_external_relation_ranking_without_db_filter_uses_family_universe() -> None:
    scoped = _external_db_prefilter_questions(
        "ALFA와 BETA의 공식 편입표에서 회사 A를 담은 ETF를 "
        "일거래대금 낮은 순으로 찾아줘.",
        ETP,
        ranking_field="daily_trading_value",
    )

    assert scoped == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF",
        ProductFamily.OVERSEAS_ETP: "해외 ETF",
    }


def test_external_relation_with_explicit_db_filter_stays_fail_closed() -> None:
    assert (
        _external_db_prefilter_questions(
            "회사 A를 담은 해외 ETF 중 AUM 10억 이상을 찾아줘.",
            ETP,
        )
        is None
    )


def test_external_semantic_filter_projection_preserves_literal_numeric_clause() -> None:
    question = "회사 A를 담은 국내 ETF 중 돈그릇이 100억 원 이상인 상품을 찾아줘."
    requirement = _requirement(
        1,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgePredicate.HOLDS_COMPANY,
        "회사 A",
        "담은",
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="filter",
                text_span="돈그릇이 100억 원 이상인",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.FILTER,
                operator=SemanticFrameOperator.GTE,
                field_span="돈그릇",
                value_span="100억 원",
            )
        ],
    )

    scoped = _external_semantic_prefilter_questions(
        question,
        (ProductFamily.DOMESTIC_ETP,),
        intent_draft=draft,
        ranking_field=None,
        requested_fields=(),
        requirements=(requirement,),
        identity_surfaces=(),
    )

    assert scoped == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF 돈그릇이 100억 원 이상인"
    }


def test_external_semantic_filter_projection_cannot_drop_an_unframed_hard_filter() -> None:
    question = (
        "회사 A를 담은 국내 ETF 중 위험등급 3등급이고 "
        "돈그릇이 100억 원 이상인 상품을 찾아줘."
    )
    requirement = _requirement(
        1,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgePredicate.HOLDS_COMPANY,
        "회사 A",
        "담은",
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="filter",
                text_span="돈그릇이 100억 원 이상인",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.FILTER,
                operator=SemanticFrameOperator.GTE,
                field_span="돈그릇",
                value_span="100억 원",
            )
        ],
    )

    assert (
        _external_semantic_prefilter_questions(
            question,
            (ProductFamily.DOMESTIC_ETP,),
            intent_draft=draft,
            ranking_field=None,
            requested_fields=(),
            requirements=(requirement,),
            identity_surfaces=(),
        )
        is None
    )


def test_external_semantic_draft_is_narrowed_to_server_selected_family() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(ETP),
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="filter",
                text_span="돈그릇이 100억 이상인",
                product_families=list(ETP),
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.FILTER,
                operator=SemanticFrameOperator.GTE,
                field_span="돈그릇",
                value_span="100억",
            )
        ],
    )

    scoped = _scope_external_semantic_draft(draft, ProductFamily.DOMESTIC_ETP)

    assert scoped.product_families == [ProductFamily.DOMESTIC_ETP]
    assert scoped.execution_clauses[0].product_families == [ProductFamily.DOMESTIC_ETP]


def test_external_semantic_draft_cannot_add_an_unapproved_family() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
    )

    with pytest.raises(PlanCompilationBlockedError, match="승인되지 않은 상품군"):
        _scope_external_semantic_draft(draft, ProductFamily.OVERSEAS_ETP)


@pytest.mark.parametrize(
    "question",
    [
        "순자산 10억 이상인 국내 ETF 중 회사 A를 편입한 상품을 "
        "공식 자료와 제공 DB에서 찾아줘.",
        "운용사가 한빛인 ETF 중 회사 A를 담은 상품을 공식 자료와 제공 데이터에서 "
        "확인해줘.",
        "위험등급 높은 국내 ETF 중 회사 A를 편입한 상품을 "
        "공식 자료와 제공 DB에서 찾아줘.",
        "순자산 큰 해외 ETF 중 회사 A를 담은 상품을 "
        "공식 자료와 제공 데이터에서 확인해줘.",
    ],
)
def test_external_prefilter_never_drops_a_db_filter_before_transition_marker(
    question: str,
) -> None:
    requirements = (
        _requirement(
            1,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            "회사 A",
            "편입한" if "편입한" in question else "담은",
        ),
    )

    assert (
        _external_db_prefilter_questions(
            question,
            ETP,
            requirements=requirements,
        )
        is None
    )


def test_external_relation_never_drops_a_labeled_string_filter() -> None:
    assert (
        _external_db_prefilter_questions(
            "회사 A를 담은 ETF 중 운용사가 한빛인 상품을 찾아줘.",
            ETP,
            requested_fields=("manager",),
        )
        is None
    )


@pytest.mark.parametrize(
    "condition",
    ["환헤지형", "신용등급 AA 이상", "표면금리 높은"],
)
def test_external_relation_never_drops_another_family_schema_condition(
    condition: str,
) -> None:
    assert (
        _external_db_prefilter_questions(
            f"회사 A를 담은 ETF 중 {condition} 상품을 찾아줘.",
            ETP,
        )
        is None
    )


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


def test_external_time_range_is_not_recompiled_as_product_return_filter() -> None:
    question = "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 관련 ETF를 정리해줘"
    requirement = _requirement(
        1,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgePredicate.THEME_RELATED,
        "우주항공 테마",
        "연결 이력이 있는",
    ).model_copy(
        update={
            "time_range": KnowledgeTimeRange(
                start=date(2026, 2, 24),
                end=date(2026, 8, 24),
            )
        }
    )

    scoped = _external_db_prefilter_questions(
        question,
        ETP,
        requirements=(requirement,),
    )

    assert scoped == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF",
        ProductFamily.OVERSEAS_ETP: "해외 ETF",
    }


def test_composite_planner_drops_model_created_predicate_only_theme_subject() -> None:
    """A connective emitted as a subject must not multiply live searches."""

    question = "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 관련 ETF를 정리해줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(ETP),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span="우주항공 테마",
                relation_span="연결 이력이 있는",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span="있는",
                relation_span="관련 ETF",
                search_aliases=("available", "exist", "present"),
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "predicate-only-subject",
        draft,
    )

    assert [(item.capability, item.subject) for item in plan.requirements] == [
        (KnowledgeCapability.SECTOR_THEME_HISTORY, "우주항공 테마")
    ]


def test_predicate_only_risk_subject_requires_an_upstream_product_selector() -> None:
    """A generic risk target alone must not become a root live-search entity."""

    requirements = [
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
            subject_span="오로라그룹",
            relation_span="오로라그룹의 자회사",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            subject_span="상품",
            relation_span="위험요인",
        ),
    ]

    retained = _deduplicate_structural_requirements(
        "오로라그룹의 자회사와 상품의 위험요인을 알려줘.",
        requirements,
    )

    assert [item.capability for item in retained] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY
    ]


def test_predicate_only_risk_does_not_inherit_a_discarded_fake_selector() -> None:
    """A model-created relation word cannot authorize a dependent risk search."""

    requirements = [
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
            subject_span="관련된",
            relation_span="ETF",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            subject_span="상품",
            relation_span="위험요인",
        ),
    ]

    retained = _deduplicate_structural_requirements(
        "관련된 ETF 상품의 위험요인을 알려줘.",
        requirements,
    )

    assert retained == []


def test_predicate_only_risk_requires_selector_evidence_in_the_same_clause() -> None:
    """An unrelated earlier selector must not grant risk retrieval authority."""

    requirements = [
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="오로라전자",
            relation_span="담은",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            subject_span="상품",
            relation_span="위험요인",
        ),
    ]

    retained = _deduplicate_structural_requirements(
        "오로라전자를 담은 ETF를 찾아줘. 별도 상품의 위험요인을 알려줘.",
        requirements,
    )

    assert [item.capability for item in retained] == [
        KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]


def test_predicate_only_risk_does_not_cross_an_explicit_separate_clause() -> None:
    """A comma plus an explicit separate target terminates selector ownership."""

    requirements = [
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="오로라전자",
            relation_span="담은",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            subject_span="상품",
            relation_span="위험요인",
        ),
    ]

    retained = _deduplicate_structural_requirements(
        "오로라전자를 담은 ETF를 찾아주고, 별도 상품의 위험요인을 알려줘.",
        requirements,
    )

    assert [item.capability for item in retained] == [
        KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]


def test_repeated_identical_risk_spans_do_not_bind_to_the_first_selector() -> None:
    """Offset-free repeated risk text is ambiguous and must not be guessed."""

    requirements = [
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="오로라전자",
            relation_span="담은",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="네뷸라소재",
            relation_span="편입한",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            subject_span="ETF",
            relation_span="위험요인",
        ),
    ]

    retained = _deduplicate_structural_requirements(
        "오로라전자를 담은 ETF의 위험요인과 "
        "네뷸라소재를 편입한 ETF의 위험요인을 알려줘.",
        requirements,
    )

    assert all(
        item.capability is not KnowledgeCapability.PRODUCT_RISK_FACTORS
        for item in retained
    )


def test_independent_selector_risk_clauses_keep_local_dependencies() -> None:
    """Two independent selector/risk pairs must not become one global join."""

    question = (
        "오로라전자를 담은 ETF의 위험요인과 "
        "네뷸라소재를 편입한 ETF의 리스크 요인을 알려줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="오로라전자",
                relation_span="담은",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="ETF",
                relation_span="위험요인",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="네뷸라소재",
                relation_span="편입한",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="ETF",
                relation_span="리스크 요인",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "local-risk-joins", draft)
    risks = tuple(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )

    assert [item.depends_on for item in risks] == [("need_1",), ("need_2",)]


def test_named_product_risk_does_not_inherit_an_unrelated_holding_selector() -> None:
    """An explicit product risk remains independent from another search clause."""

    question = "오로라전자를 담은 ETF를 찾고 BETA ETF의 위험요인을 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="오로라전자",
                relation_span="담은",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="BETA ETF",
                relation_span="위험요인",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "named-risk-root", draft)
    risk = next(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )

    assert risk.depends_on == ()


def test_hclx_deictic_risk_subject_binds_only_to_its_retained_selector() -> None:
    """HCX may preserve ``각 상품`` instead of reducing it to ``상품``."""

    question = "오로라전자를 담은 ETF 중 각 상품의 위험요인을 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="오로라전자",
                relation_span="담은",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="각 상품",
                relation_span="위험요인",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "deictic-risk", draft)
    risk = next(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )

    assert risk.depends_on == ("need_1",)


def test_external_prefilter_masks_source_identity_document_scope_and_output_rank() -> None:
    question = (
        "ALPHA 미국액티브레버리지TOP10과 BETA의 각 기준일 공식 상위 10개 "
        "구성종목을 기준으로 회사 A와 회사 B를 담은 ETF를 "
        "1일 수익률 높은 순으로 찾아줘."
    )
    requirements = (
        _requirement(
            1,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            "회사 A",
            "담은",
        ),
        _requirement(
            2,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            "회사 B",
            "담은",
        ),
    )

    scoped = _external_db_prefilter_questions(
        question,
        ETP,
        ranking_field="one_day_return_pct",
        requirements=requirements,
        identity_surfaces=("ALPHA 미국액티브레버리지TOP10", "BETA"),
    )

    assert scoped == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF",
        ProductFamily.OVERSEAS_ETP: "해외 ETF",
    }


def test_external_prefilter_projects_joined_db_filter_without_evidence_presentation() -> None:
    question = (
        "ALPHA와 BETA의 공식 상위 10개 구성표를 대조해 회사 A가 상위 10개에 "
        "들어 있는 ETF를 찾아주세요. 제공 DB와 상품번호를 연결한 뒤 해외 ETF는 "
        "연간보수율 0.68 이하 조건까지 적용하고, 해당 상품군 안에서 순자산 큰 "
        "순으로 최대 3개를 알려주세요. 문서 기준일과 일치 종목별 비중 합계도 "
        "구분해 주세요."
    )
    requirement = _requirement(
        1,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgePredicate.HOLDS_COMPANY,
        "회사 A",
        "들어 있는",
    ).model_copy(
        update={
            "product_families": (ProductFamily.OVERSEAS_ETP,),
            "document_scope": KnowledgeDocumentTopNScope(
                limit=10,
                selection_evidence_span="상위 10개 구성표",
            ),
        }
    )

    scoped = _external_db_prefilter_questions(
        question,
        (ProductFamily.OVERSEAS_ETP,),
        ranking_field="aum",
        requested_fields=("aum",),
        requirements=(requirement,),
        identity_surfaces=("ALPHA", "BETA"),
    )

    assert scoped == {
        ProductFamily.OVERSEAS_ETP: "해외 ETF 연간보수율 0.68 이하 조건까지 적용하고"
    }
    decision = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="joined-filter",
            question=scoped[ProductFamily.OVERSEAS_ETP],
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.OVERSEAS_ETP],
            product_mentions=[],
            requested_limit=None,
        ),
        disposition=RouteDisposition.EXECUTE,
        reason_code="test_external_prefilter",
        reason="test",
        query_plan_intent=Intent.SEARCH,
        capability_matrix_version="test",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)
    assert [(item.field, item.operator.value, item.value) for item in plan.constraints] == [
        ("total_expense_ratio_pct", "lte", 0.68),
        ("product_type", "eq", "ETF"),
    ]
    assert not plan.ambiguities
    assert not plan.unsupported_conditions


def test_external_prefilter_treats_sibling_any_as_relation_not_numeric_filter() -> None:
    question = (
        "ALPHA와 BETA의 공식 상위 10개 구성표를 대조해 회사 A·회사 B 중 하나 "
        "이상이 상위 10개에 들어 있는 ETF를 찾아주세요. 제공 DB와 상품번호를 "
        "연결한 뒤 해외 ETF는 연간보수율 0.68 이하, 국내 ETF는 추적오차율 "
        "6.71 이하 조건까지 적용하고, 국내·해외 ETF를 나눠 1일 수익률 큰 "
        "순으로 최대 3개를 알려주세요. 최종 후보별로 해당 운용사 자료에서 "
        "확인되는 손실 관련 유의점도 덧붙여 주세요."
    )
    requirements = tuple(
        _requirement(
            sequence,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            subject,
            "들어 있는",
        ).model_copy(
            update={
                "document_scope": KnowledgeDocumentTopNScope(
                    limit=10,
                    selection_evidence_span="상위 10개 구성표",
                )
            }
        )
        for sequence, subject in enumerate(("회사 A", "회사 B"), start=1)
    )

    scoped = _external_db_prefilter_questions(
        question,
        ETP,
        ranking_field="one_day_return_pct",
        requested_fields=("one_day_return_pct",),
        requirements=requirements,
        identity_surfaces=("ALPHA", "BETA"),
    )

    assert scoped == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF 추적오차율 6.71 이하 조건까지 적용하고",
        ProductFamily.OVERSEAS_ETP: "해외 ETF 연간보수율 0.68 이하",
    }


def test_external_rank_revalidation_masks_only_the_separately_owned_top_k_limit() -> None:
    clause = IntentExecutionClause(
        sequence=1,
        role="ranking",
        text_span="해당 상품군 안에서 순자산 큰 순으로 최대 3개를 알려주세요",
        product_families=[ProductFamily.OVERSEAS_ETP],
        semantic_status=SemanticFrameStatus.RESOLVED,
        field_id="aum",
        operation=SemanticFrameOperation.RANK,
        direction=SemanticFrameDirection.DESC,
        field_span="순자산",
        limit_span="3개",
    )
    rank_question = f"해외 ETF {_external_rank_validation_surface(clause)}"
    trace = IntentRouter().route_with_planning(rank_question, "external-rank-limit")

    plan = ServerQueryPlanCompiler({}).compile(trace.route_decision)

    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("aum", "desc")
    ]
    assert "해당 상품군" not in rank_question
    assert plan.limit == 5
    assert not plan.ambiguities
    assert not plan.unsupported_conditions


def _document_and_final_rank_draft(
    *,
    final_field: str = "aum",
    final_direction: SemanticFrameDirection = SemanticFrameDirection.DESC,
    final_limit_span: str = "3개",
) -> tuple[str, IntentAnalysisDraft]:
    question = (
        "공식 팩트시트의 CLOU, BUG, AIQ 상위 10개 구성표를 대조해 "
        "Zscaler가 들어 있는 해외 ETF를 찾고 순자산 큰 순으로 최대 3개를 알려줘."
    )
    return question, IntentAnalysisDraft(
        schema_version="1.0",
        semantic_frame_version="2.0",
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=["해외 ETF"],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span="상위 10개 구성표",
                product_families=[ProductFamily.OVERSEAS_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                # This is a deliberately irrelevant *product* field.  The
                # clause ranks document rows, so it must never veto the final
                # provided-DB product ranking below.
                field_id="daily_volume",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="구성표",
                limit_span="10개",
            ),
            IntentExecutionClause(
                sequence=2,
                role="ranking",
                text_span="순자산 큰 순으로 최대 3개",
                product_families=[ProductFamily.OVERSEAS_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id=final_field,
                operation=SemanticFrameOperation.RANK,
                direction=final_direction,
                field_span="순자산",
                limit_span=final_limit_span,
            ),
        ],
    )


def test_holding_document_rank_does_not_veto_canonical_final_product_rank() -> None:
    question, draft = _document_and_final_rank_draft()
    document_frames, final_frames = _external_rank_frames_by_authority(
        question,
        draft,
        (ProductFamily.OVERSEAS_ETP,),
    )

    validated = _validated_external_final_rank_frames(
        question,
        draft,
        (ProductFamily.OVERSEAS_ETP,),
        KnowledgeProductSelection(
            rank_field="aum",
            direction=SortDirection.DESC,
            limit=3,
        ),
    )

    assert [item.limit_span for item in document_frames] == ["10개"]
    assert [item.field_id for item in final_frames] == ["aum"]
    assert validated == final_frames


@pytest.mark.parametrize(
    ("final_field", "final_direction", "final_limit_span"),
    [
        ("daily_volume", SemanticFrameDirection.DESC, "3개"),
        ("aum", SemanticFrameDirection.ASC, "3개"),
        # HCX attached the grounded document count to the final rank frame.
        # It must not silently replace the server-owned result count.
        ("aum", SemanticFrameDirection.DESC, "10개"),
    ],
)
def test_wrong_hclx_final_product_rank_still_blocks(
    final_field: str,
    final_direction: SemanticFrameDirection,
    final_limit_span: str,
) -> None:
    question, draft = _document_and_final_rank_draft(
        final_field=final_field,
        final_direction=final_direction,
        final_limit_span=final_limit_span,
    )

    with pytest.raises(PlanCompilationBlockedError, match="외부 결과"):
        _validated_external_final_rank_frames(
            question,
            draft,
            (ProductFamily.OVERSEAS_ETP,),
            KnowledgeProductSelection(
                rank_field="aum",
                direction=SortDirection.DESC,
                limit=3,
            ),
        )


def test_external_prefilter_keeps_real_residual_numeric_filter_fail_closed() -> None:
    question = (
        "ALPHA 미국액티브레버리지TOP10의 공식 상위 10개 구성종목에서 "
        "회사 A를 담은 ETF 중 순자산 10억 이상만 찾아줘."
    )
    requirements = (
        _requirement(
            1,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            "회사 A",
            "담은",
        ),
    )

    assert (
        _external_db_prefilter_questions(
            question,
            ETP,
            requirements=requirements,
            identity_surfaces=("ALPHA 미국액티브레버리지TOP10",),
        )
        is None
    )


@pytest.mark.parametrize("span", ["자회사를 편입", "에코프로의 자회사", "편입한", "자회사"])
def test_relation_only_control_spans_are_authorized(span: str) -> None:
    assert _external_control_span_is_authorized(span, _ECOPRO_REQUIREMENTS)


def test_intent_requirements_tile_relation_restatement_for_ambiguity_coverage() -> None:
    requirements = (
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
            subject_span="에코프로",
            relation_span="에코프로의 자회사",
        ),
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="에코프로",
            relation_span="편입한",
        ),
    )
    question = "에코프로의 자회사를 편입한 ETF 중 순자산이 큰 상품을 알려줘."

    assert _external_route_covers_ambiguity(
        "자회사를 편입",
        question=question,
        requirements=requirements,
        product_mentions=(),
    )
    assert not _external_route_covers_ambiguity(
        "순자산이 큰",
        question=question,
        requirements=requirements,
        product_mentions=(),
    )


def test_rank_position_is_owned_by_the_external_holding_predicate() -> None:
    requirements = (
        _requirement(
            1,
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgePredicate.HOLDS_COMPANY,
            "회사 A",
            "상위 10개에 들어 있는",
        ),
    )

    assert _external_control_span_is_authorized("상위", requirements)


def test_official_table_top_n_is_not_mistaken_for_output_ranking() -> None:
    question = "공식 상위 10개 구성종목에서 회사 A를 담은 ETF인지 확인해줘."
    requirements = (
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="회사 A",
            relation_span="담은",
        ),
    )

    assert _external_route_covers_ambiguity(
        "상위",
        question=question,
        requirements=requirements,
        product_mentions=(),
    )


def test_metricless_output_top_n_remains_ambiguous() -> None:
    requirements = (
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="회사 A",
            relation_span="담은",
        ),
    )
    for question in (
        "공식 구성종목에서 회사 A를 담은 ETF 상위 3개를 알려줘.",
        "회사 A를 담은 ETF 상위 3개 종목을 알려줘.",
    ):
        assert not _external_route_covers_ambiguity(
            "상위",
            question=question,
            requirements=requirements,
            product_mentions=(),
        )


def test_reversed_official_holding_top_n_is_document_owned() -> None:
    question = "공식 구성종목 상위 10개에서 회사 A를 담았는지 확인해줘."
    requirements = (
        KnowledgeIntentRequirement(
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            subject_span="회사 A",
            relation_span="담았는지",
        ),
    )

    assert _external_route_covers_ambiguity(
        "상위",
        question=question,
        requirements=requirements,
        product_mentions=(),
    )


@pytest.mark.parametrize(
    "span",
    ["순자산이 큰", "총보수 0.15% 이하", "1년 수익률 높은 순", "위험등급 2등급", "최근 6개월"],
)
def test_db_condition_spans_never_tile_from_relation_words(span: str) -> None:
    assert not _external_control_span_is_authorized(span, _ECOPRO_REQUIREMENTS)
