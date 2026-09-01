import pytest

from finance_agent_core.agent.compiler import (
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
    _mask_bound_product_mentions,
    _ranked_identity_requests_single_winner,
)
from finance_agent_core.agent.fund_comparison_parser import extract_fund_comparison_fields
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    has_server_authorized_knowledge_requirements,
    sanitize_server_authorized_knowledge_draft,
    verify_intent_analysis,
)
from finance_agent_core.agent.knowledge_router import product_search_has_precedence
from finance_agent_core.agent.linker import build_lexical_hints, canonicalize_query_plan_payload
from finance_agent_core.agent.presentation import semantic_query_surface
from finance_agent_core.agent.product_comparison import extract_comparison_fields
from finance_agent_core.agent.product_identity import extract_named_product_mentions
from finance_agent_core.agent.providers import bond_vertical_slice_plan
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.safety import SafetyDisposition, SafetyEnvelope
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.oracle import compile_search_sql
from finance_agent_core.execution.sql_schema import SQL_FIELDS_BY_FAMILY


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        (
            "후보 범위를 먼저 파악한 뒤 상위 상품을 보고 싶습니다. "
            "공모펀드 KR5187440016, KR5157450126 중 1년 수익률이 더 높은 "
            "상품을 답해 주세요. 몇 건이 후보였는지 먼저 밝히고, 반환 상품별 "
            "식별자와 실제 기준값도 보여 주세요.",
            "compare",
        ),
        (
            "신용등급 AA+ 채권 중 액면에 붙은 이자율이 높은 종목 3개만 "
            "부탁해요. 결과마다 원천 상품번호와 필터·정렬값을 붙이고, "
            "제외 전이 아닌 유효 후보 수를 알려 주세요.",
            "search",
        ),
        (
            "국내 ETF와 공모펀드를 3개월 수익률로 한 줄에 비교해 주세요. "
            "-3% 이상 3% 이하인 상품만 남겨 높은 순 7개를 뽑고 상품 "
            "종류·ID·실제 수익률과 상품군별 후보 수도 적어 주세요.",
            "search",
        ),
        (
            "국내 ETF와 해외 ETF에서 0.2% 이상 2% 이하인 상품만 추린 뒤 "
            "1일 수익률 높은 순 6개를 한 목록으로 만들고 ID·실제 값·"
            "상품군별 후보 수를 밝혀 주세요.",
            "search",
        ),
    ],
)
def test_phase3_response_projection_cannot_change_query_intent(
    question: str,
    intent: str,
) -> None:
    decision = IntentRouter().route(question, "phase3-presentation")

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == intent


def test_phase3_auditable_fund_comparison_preamble_is_presentation_only() -> None:
    question = (
        "검색 결과가 맞는지 확인할 수 있게 정리해 주세요. 공모펀드 종목번호 "
        "KR510902684M, KR5157450126 가운데 3년 수익률이 더 높은 상품을 "
        "알려 주세요. 각 항목에 상품 식별자와 사용한 실제 값을 표시하고 "
        "조건에 남은 상품 수까지 적어 주세요."
    )

    assert semantic_query_surface(question).startswith("공모펀드 종목번호")
    assert IntentRouter().route(question, "phase3-fund-compare").draft.intent.value == "compare"


def test_phase3_presentation_mask_keeps_real_exclusion_and_maturity_order() -> None:
    exclusion = "AUM 말고 거래대금을 기준으로 국내 ETF를 보여 주세요."
    maturity = "만기가 먼저 오는 국내채권 세 종목을 보여 주세요."

    assert "말고" in semantic_query_surface(exclusion)
    assert "먼저" in semantic_query_surface(maturity)
    assert (
        SemanticCoverageGate()
        .evaluate(
            exclusion,
            interaction_intent="search",
        )
        .ambiguity_spans
    )
    assert IntentRouter().route(maturity, "phase3-real-order").disposition.value == "execute"


def test_phase3_exact_identity_alias_disclaimer_is_presentation_only() -> None:
    question = (
        "검색 누락 여부를 확인하는 요청입니다. KODEX AI로봇ETF의 1년 수익률과 "
        "운용사를 확인해 주세요. 공백·대소문자 외에는 임의 별칭을 만들어 대체하지 "
        "말아 주세요."
    )

    surface = semantic_query_surface(question)

    assert "KODEX AI로봇ETF" in surface
    assert "1년 수익률" in surface
    assert "운용사" in surface
    assert "임의 별칭" not in surface
    assert "외에는" not in surface


def test_fund_comparison_supports_all_official_long_return_fields() -> None:
    fields = extract_fund_comparison_fields(
        "두 공모펀드의 18개월 수익률, 2년 수익률, 3년 수익률, 5년 수익률을 비교해 주세요."
    )

    assert set(fields) >= {
        "eighteen_month_return_pct",
        "two_year_return_pct",
        "three_year_return_pct",
        "five_year_return_pct",
    }


@pytest.mark.parametrize(
    ("question", "field", "operator", "value"),
    [
        (
            "1일 수익률 기준으로 국내 ETF와 해외 ETF를 함께 정렬해 주세요. "
            "1%를 초과하는 상품만 선택하고 높은 순 5개를 보여 주세요.",
            "one_day_return_pct",
            "gt",
            1,
        ),
        (
            "3개월 수익률 기준으로 국내 ETF와 공모펀드를 함께 정렬해 주세요. "
            "-2%보다 낮은 상품만 선택하고 낮은 순 6개를 보여 주세요.",
            "three_month_return_pct",
            "lt",
            -2,
        ),
    ],
)
def test_cross_family_shared_return_threshold_is_locked(
    question: str,
    field: str,
    operator: str,
    value: int,
) -> None:
    decision = IntentRouter().route(question, "phase3-shared-threshold")
    searches = ServerQueryPlanCompiler({}).compile_family_searches(decision)

    assert decision.disposition.value == "execute"
    assert all(item.plan.ranking[0].field == field for item in searches)
    assert all(
        (field, operator, value)
        in [
            (
                constraint.field,
                constraint.operator.value,
                constraint.value,
            )
            for constraint in item.plan.constraints
        ]
        for item in searches
    )


def test_official_fund_online_sales_channel_is_not_web_search() -> None:
    decision = IntentRouter().route(
        "클래스 판매채널이 온라인인 공모펀드를 판매회사보수가 낮은 순으로 3개 찾아 주세요.",
        "phase3-fund-sales-channel",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"


@pytest.mark.parametrize(
    ("question", "field"),
    [
        (
            "판매중으로 적힌 공모펀드를 최근 한 달 수익률이 높은 순으로 4개 보여 주세요.",
            "one_month_return_pct",
        ),
        (
            "투자지역이 미국으로 적힌 국내 ETF 중 반년 수익률 상위 5개를 보고 싶습니다.",
            "six_month_return_pct",
        ),
        (
            "USD 통화 공모펀드 중 반년 수익률이 좋은 상품 4개를 찾아 주세요.",
            "six_month_return_pct",
        ),
        (
            "투자지역이 United States of America인 해외 ETF를 "
            "일간 순자산총액이 큰 순으로 4개 알려 주세요.",
            "aum",
        ),
    ],
)
def test_explicit_metric_variants_do_not_leave_direction_residuals(
    question: str,
    field: str,
) -> None:
    decision = IntentRouter().route(question, f"official-explicit-{field}")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert hints["required_rankings"] == [{"field": field, "direction": "desc", "nulls": "last"}]
    assert hints["ambiguity_spans"] == []


def test_generic_good_product_without_metric_still_clarifies() -> None:
    decision = IntentRouter().route(
        "좋은 공모펀드 4개를 추천해 주세요.",
        "official-vague-good-product",
    )

    assert decision.disposition.value == "clarify"


def test_native_korean_result_count_is_shared_by_router_and_linker() -> None:
    question = "국내 채권형 ETF를 AUM 큰 순서로 다섯 개 찾아줘."

    routed = IntentRouter().route(question, "official-limit-five")
    hints = build_lexical_hints(question)

    assert routed.draft.requested_limit == 5
    assert hints["limit"] == 5


def test_product_family_phrase_is_not_a_single_result_counter() -> None:
    question = (
        "한 상품군 안에서 후보를 확인하려 합니다. "
        "유럽 지역 공모펀드 중 5년 수익률이 큰 세 상품을 조회해 주세요."
    )

    decision = IntentRouter().route(question, "official-product-family-counter")
    hints = build_lexical_hints(question)

    assert decision.draft.requested_limit == 3
    assert hints["limit"] == 3


def test_korean_suffix_in_passed_product_count_is_not_cardinal_one() -> None:
    question = (
        "일반사채 가운데 상환까지 남은 날짜가 가장 짧은 세 종목을 보여 주세요. "
        "조건을 통과한 상품 수도 부탁합니다."
    )

    decision = IntentRouter().route(question, "official-passed-product-count")

    assert decision.draft.requested_limit == 3


def test_two_exact_bond_ids_with_comparative_cue_route_to_compare() -> None:
    decision = IntentRouter().route(
        "국내채권 종목번호 KR60006J1F92, KR6091812F88 가운데 듀레이션이 더 긴 종목을 알려 주세요.",
        "official-bond-compare",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "compare"
    assert decision.draft.product_mentions == ["KR60006J1F92", "KR6091812F88"]


@pytest.mark.parametrize(
    "question",
    [
        "국내채권 종목번호 KR60006J1F92 대 KR6091812F88 중 듀레이션이 더 높은 종목은?",
        "KR60006J1F92와 KR6091812F88 가운데 상환일이 먼저 오는 채권은?",
        "국내채권 KR60006J1F92, KR6091812F88 중 어느 쪽의 표면이자율이 더 높아?",
        "채권 KR6092791G64, KR103502GB33 중 남은 발행 물량이 더 큰 하나는?",
    ],
)
def test_two_exact_bond_ids_with_natural_comparative_variants_route_to_compare(
    question: str,
) -> None:
    decision = IntentRouter().route(question, "official-bond-compare-variant")

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "compare"
    assert len(decision.draft.product_mentions) == 2


@pytest.mark.parametrize(
    "question",
    [
        "ETF 두 상품을 AUM으로 비교하면 어느 쪽이 더 큰가요?",
        "채권 두 종목 중 남은 발행 물량이 더 큰 하나를 알려 주세요.",
        "두 상품 가운데 듀레이션이 더 긴 종목을 알려 주세요.",
        "두 채권 중 상환이 먼저 오는 상품을 골라 주세요.",
    ],
)
def test_candidate_count_words_do_not_override_single_winner_comparison(
    question: str,
) -> None:
    assert _ranked_identity_requests_single_winner(question)


@pytest.mark.parametrize(
    "question",
    [
        "ETF를 반년 수익률이 높은 순으로 5개 보여 주세요.",
        "ETF를 연초 이후 수익률이 높은 순으로 5개 보여 주세요.",
    ],
)
def test_generic_etf_with_domestic_only_official_field_routes_domestic(
    question: str,
) -> None:
    decision = IntentRouter().route(question, "official-generic-domestic-etf")

    assert decision.disposition.value == "execute"
    assert [family.value for family in decision.draft.product_families] == ["domestic_etp"]


def test_generic_etf_with_one_day_return_remains_family_ambiguous() -> None:
    decision = IntentRouter().route(
        "ETF를 하루 수익률이 높은 순으로 5개 보여 주세요.",
        "official-generic-etf-one-day",
    )

    assert decision.disposition.value == "clarify"


def test_credit_card_bond_label_routes_to_bond() -> None:
    decision = IntentRouter().route(
        "신용카드채를 표면이자율이 높은 순으로 3개 보여 주세요.",
        "official-credit-card-bond",
    )

    assert decision.disposition.value == "execute"
    assert [family.value for family in decision.draft.product_families] == ["bond"]


def test_detail_field_projection_is_not_misread_as_vague_risk_request() -> None:
    decision = IntentRouter().route(
        "국내 ETF 상품번호 KR7279530000의 운용사, 투자자산군, 상품 위험등급명을 알려 주세요.",
        "official-detail-fields",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "detail"


def test_plain_database_fields_cannot_open_external_knowledge_runtime() -> None:
    question = "국내 ETF의 운용사와 위험등급을 알려 주세요."
    false_positive = IntentAnalysisDraft.model_validate(
        {
            "intent": "search",
            "product_families": ["domestic_etp"],
            "evidence_spans": ["국내 ETF"],
            "needs_clarification": False,
            "knowledge_requirements": [
                {
                    "capability": "product_risk_factors",
                    "subject_span": "국내 ETF",
                    "relation_span": "위험등급",
                    "search_aliases": [],
                }
            ],
        }
    )

    assert not has_server_authorized_knowledge_requirements(question, false_positive)


def test_hclx_intent_evidence_allows_only_presentation_normalization() -> None:
    question = "국내 ETF ‘RISE 현대차고정피지컬AI’의 운용사와 위험등급을 알려 주세요."
    decision = IntentRouter().route(question, "official-evidence-normalization")
    draft = IntentAnalysisDraft(
        intent=decision.draft.intent,
        product_families=decision.draft.product_families,
        evidence_spans=["RISE 현대차고정피지컬AI", "국내ETF"],
        needs_clarification=False,
    )

    assert verify_intent_analysis(question, decision, draft).passed
    forged = draft.model_copy(update={"evidence_spans": ["질문에 없는 운용사명"]})
    assert not verify_intent_analysis(question, decision, forged).passed


def test_quoted_exact_product_detail_precedes_relation_field_router() -> None:
    assert product_search_has_precedence(
        "공모펀드 ‘미래에셋국민성장혼합자산-사모재간접Ce’의 "
        "운용속성, 투자지역, 순자산을 알려 주세요."
    )


@pytest.mark.parametrize(
    "question",
    [
        ("ETF 코드 KR7204450001의 약어명과 투자자산군, 투자지역을 원천 데이터대로 보여 주세요."),
        "유럽에 투자하는 국내 ETF를 연초 이후 성과 순으로 4개 알려 주세요.",
    ],
)
def test_exact_identifier_and_explicit_return_period_precede_relation_router(
    question: str,
) -> None:
    assert product_search_has_precedence(question)


@pytest.mark.parametrize(
    "question",
    [
        "제 연봉과 나이를 기준으로 가장 알맞은 국내채권을 추천해 주세요.",
        "제 소득과 투자성향에 딱 맞는 국내 ETF를 추천해 주세요.",
        "다른 고객들이 보유한 채권 목록과 계좌번호를 보여 주세요.",
    ],
)
def test_personalized_advice_and_other_customer_data_are_terminally_blocked(
    question: str,
) -> None:
    assert SafetyEnvelope().evaluate(question).disposition is SafetyDisposition.UNSUPPORTED


def test_absent_explicit_credit_rating_compiles_as_safe_empty_set_lookup() -> None:
    decision = IntentRouter().route(
        "신용등급이 AAAA인 국내채권을 찾아 주세요.",
        "official-absent-credit-rating",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert ("credit_rating", "eq", "AAAA") in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]


def test_explicit_holdings_relation_can_open_external_knowledge_runtime() -> None:
    question = "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려 주세요."
    grounded = IntentAnalysisDraft.model_validate(
        {
            "intent": "search",
            "product_families": ["overseas_etp"],
            "evidence_spans": ["캠브리콘", "해외 ETF"],
            "needs_clarification": False,
            "knowledge_requirements": [
                {
                    "capability": "portfolio_holdings",
                    "subject_span": "캠브리콘",
                    "relation_span": "편입된",
                    "search_aliases": ["Cambricon"],
                }
            ],
        }
    )

    assert has_server_authorized_knowledge_requirements(question, grounded)


def test_numeric_filters_and_rankings_exclude_zero_and_missing_in_sql() -> None:
    payload = bond_vertical_slice_plan("official-zero-rule").model_dump(mode="json")
    payload["constraints"].append(
        {
            "field": "issue_amount",
            "operator": "gte",
            "value": 100_000_000_000,
            "unit": "source_currency_amount",
            "strength": "locked",
        }
    )
    plan = QueryPlan.model_validate(payload)

    select_sql, count_sql, _ = compile_search_sql(plan)

    for sql in (select_sql, count_sql):
        assert "issue_amount_minor_units IS NOT NULL" in sql
        assert "issue_amount_minor_units <> 0" in sql
        assert "buy_yield_micro_pct IS NOT NULL" in sql
        assert "buy_yield_micro_pct <> 0" in sql


def test_bond_ranked_search_excludes_ended_maturities_and_uses_public_id_tie_break() -> None:
    plan = bond_vertical_slice_plan("official-current-bond-scope")

    select_sql, count_sql, _ = compile_search_sql(plan)

    for sql in (select_sql, count_sql):
        # The end signal has to use the same basis as remaining_days, which is
        # stored as maturity minus the snapshot date. Measuring it against the
        # earlier dynamic date let bonds that had already matured survive with a
        # negative remaining_days and take the top of every ascending answer.
        assert "maturity_date >= source_snapshot_date" in sql
        assert "maturity_date > dynamic_as_of" not in sql
    assert "buy_yield_micro_pct ELSE NULL END DESC" in select_sql
    assert select_sql.endswith("ticker ASC LIMIT ?")


def test_derived_remaining_days_zero_is_a_real_temporal_value() -> None:
    decision = IntentRouter().route(
        "AA급 채권 중 만기까지 2년을 넘기지 않는 상품을 잔존일수 짧은 순으로 5개 보여 주세요.",
        "official-derived-remaining-zero",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)

    select_sql, count_sql, _ = compile_search_sql(plan)

    for sql in (select_sql, count_sql):
        assert "remaining_days IS NOT NULL" in sql
        assert "remaining_days <> 0" not in sql


@pytest.mark.parametrize(
    ("question", "field", "operator", "value"),
    [
        ("Money Market 해외 ETF를 거래량 큰 순으로 3개", "asset_type", "eq", "Money Market"),
        ("Mixed Assets 해외 ETF를 AUM 큰 순으로 5개", "asset_type", "eq", "Mixed Assets"),
        ("AMX 거래소코드가 붙은 해외 ETF", "exchange_code", "eq", "AMX"),
        ("NAS에서 거래되는 해외 ETF", "exchange_code", "eq", "NAS"),
        ("단기자금에 투자하는 국내 ETF", "asset_type", "eq", "단기자금"),
        ("혼합자산 국내 ETF", "asset_type", "eq", "혼합자산"),
        ("원자재 ETF 중 한 달 수익률 상위 5개", "asset_type", "eq", "원자재"),
        ("대체투자 자산군 국내 ETF", "asset_type", "eq", "대체투자"),
        ("일거래량이 10만 주를 넘는 국내 ETF", "daily_volume", "gt", 100000),
        (
            "20거래일 연환산 변동성이 20 이하인 국내 ETF",
            "twenty_day_volatility_pct",
            "lte",
            20,
        ),
        ("최근 한 달 수익률이 5를 넘는 국내 ETF", "one_month_return_pct", "gt", 5),
        ("당일 수익률이 -2 이하인 해외 ETF", "one_day_return_pct", "lte", -2),
        ("배수 3인 해외 ETF", "leverage_factor", "eq", 3.0),
        ("레버리지 배수 -2로 적힌 해외 ETF", "leverage_factor", "eq", -2.0),
        ("신용카드채 중 듀레이션이 짧은 순", "bond_type", "eq", "신용카드채"),
        ("유동화회사채 중 민평 수익률 상위 3개", "bond_type", "eq", "유동화회사채"),
        ("AA- 채권 중 평가가격이 낮은 상품", "credit_rating", "eq", "AA-"),
    ],
)
def test_phase2_high_frequency_filter_surfaces_are_deterministic(
    question: str,
    field: str,
    operator: str,
    value: object,
) -> None:
    forced_family = (
        "bond"
        if field in {"bond_type", "credit_rating"}
        else "overseas_etp"
        if "해외" in question or value in {"Money Market", "Mixed Assets"}
        else "domestic_etp"
    )
    hints = build_lexical_hints(
        question,
        product_family_hint=forced_family,
        force_product_family_hint=True,
    )

    assert (field, operator, value) in {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }


@pytest.mark.parametrize(
    ("question", "family", "expected_manager"),
    [
        ("미래에셋 운용 국내 ETF", "domestic_etp", "미래에셋"),
        ("First Trust Advisors LP 운용 ETF", "overseas_etp", "First Trust Advisors LP"),
        (
            "Invesco Capital Management LLC의 해외 ETF",
            "overseas_etp",
            "Invesco Capital Management LLC",
        ),
    ],
)
def test_manager_possessive_and_bare_operation_surfaces(
    question: str,
    family: str,
    expected_manager: str,
) -> None:
    hints = build_lexical_hints(
        question,
        product_family_hint=family,
        force_product_family_hint=True,
    )

    assert ("manager", "eq", expected_manager) in {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }


def test_multiple_credit_ratings_and_overseas_regions_compile_as_in_sets() -> None:
    rating_plan = canonicalize_query_plan_payload(
        "AAA나 AA+가 붙은 국내채권을 발행잔액 큰 순으로 4개",
        {"question_id": "phase2-rating-set", "product_families": ["bond"]},
        force_product_family_hint=True,
    )
    region_plan = canonicalize_query_plan_payload(
        "투자지역이 China, Japan, India 중 하나인 해외 ETF를 보수 낮은 순으로 5개",
        {"question_id": "phase2-region-set", "product_families": ["overseas_etp"]},
        force_product_family_hint=True,
    )

    rating = next(item for item in rating_plan["constraints"] if item["field"] == "credit_rating")
    region = next(
        item for item in region_plan["constraints"] if item["field"] == "investment_region"
    )
    assert rating["operator"] == "in"
    assert set(rating["value"]) == {"AAA", "AA+"}
    assert region["operator"] == "in"
    assert set(region["value"]) == {"China", "Japan", "India"}


def test_comma_separated_amount_is_not_truncated_to_zero() -> None:
    hints = build_lexical_hints(
        "발행잔액이 1,000억 이상인 국내채권을 발행잔액 큰 순으로 3개 보여 주세요."
    )

    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } >= {("issue_amount", "gte", 100_000_000_000)}


def test_fund_aum_ranking_does_not_manufacture_currency_clarification() -> None:
    hints = build_lexical_hints("AUM이 큰 공모펀드 5개를 보여 주세요.")

    assert hints["required_rankings"] == [{"field": "aum", "direction": "desc", "nulls": "last"}]
    assert hints["ambiguity_spans"] == []


@pytest.mark.parametrize(
    ("question", "constraint_field", "ranking_field"),
    [
        (
            "한 달 수익률이 3을 넘는 공모펀드 가운데 순자산 규모가 큰 4개를 보여 주세요.",
            "one_month_return_pct",
            "aum",
        ),
        (
            "최근 3개월 수익률이 0 이상인 공모펀드를 6개월 수익률이 높은 순으로 5개 보여 주세요.",
            "three_month_return_pct",
            "six_month_return_pct",
        ),
        (
            "1년 성과가 20 이상인 공모펀드 중 3년 수익률 상위 세 개를 찾습니다.",
            "one_year_return_pct",
            "three_year_return_pct",
        ),
        (
            "3년 수익률이 50을 넘는 공모펀드를 5년 수익률이 큰 순서로 4개 알려 주세요.",
            "three_year_return_pct",
            "five_year_return_pct",
        ),
    ],
)
def test_filter_metric_cannot_steal_later_ranking_metric(
    question: str,
    constraint_field: str,
    ranking_field: str,
) -> None:
    hints = build_lexical_hints(question)

    assert any(item["field"] == constraint_field for item in hints["required_constraints"])
    assert hints["required_rankings"] == [
        {"field": ranking_field, "direction": "desc", "nulls": "last"}
    ]
    assert hints["ambiguity_spans"] == []


def test_bond_display_risk_label_uses_new_official_name_column() -> None:
    hints = build_lexical_hints("보통위험(4등급) 채권을 만기가 빠른 순으로 네 개 보여 주세요.")

    assert ("risk_level", "eq", "보통위험(4등급)") in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]
    assert hints["required_rankings"] == [
        {"field": "remaining_days", "direction": "asc", "nulls": "last"}
    ]
    assert hints["unsupported_spans"] == []


def test_listed_index_fund_term_routes_to_domestic_etf() -> None:
    decision = IntentRouter().route(
        "주식에 투자하는 국내 상장지수펀드를 석 달 수익률이 높은 순으로 다섯 개 보여 주세요.",
        "official-listed-index-fund",
    )

    assert decision.disposition.value == "execute"
    assert [family.value for family in decision.draft.product_families] == ["domestic_etp"]


def test_overseas_search_metadata_token_is_not_misread_as_product_ticker() -> None:
    decision = IntentRouter().route(
        "거래소코드 NYS인 해외 ETF를 일간 순자산이 큰 순으로 3개 보여 주세요.",
        "official-overseas-exchange-filter",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"
    assert decision.draft.product_mentions == []


def test_generic_etf_with_official_overseas_asset_type_routes_overseas() -> None:
    decision = IntentRouter().route(
        "Commodity 자산군 ETF 중 오늘 수익률이 높은 순서로 4개 보여 주세요.",
        "official-overseas-asset-type",
    )

    assert decision.disposition.value == "execute"
    assert [family.value for family in decision.draft.product_families] == ["overseas_etp"]


@pytest.mark.parametrize(
    ("question", "family", "field", "direction"),
    [
        ("국내 ETF를 거래량 큰 순으로 3개 보여줘.", "domestic_etp", "daily_volume", "desc"),
        (
            "해외 ETF를 외화거래대금 큰 순으로 3개 보여줘.",
            "overseas_etp",
            "daily_trading_value",
            "desc",
        ),
        ("해외 ETF를 종가 낮은 순으로 3개 보여줘.", "overseas_etp", "close_price", "asc"),
        (
            "국내 ETF를 추적오차율 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "tracking_error_pct",
            "asc",
        ),
        (
            "국내 ETF를 괴리율 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "discrepancy_pct",
            "asc",
        ),
        (
            "국내 ETF를 20거래일 변동성 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "twenty_day_volatility_pct",
            "asc",
        ),
        (
            "국내 ETF를 60거래일 변동성 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "sixty_day_volatility_pct",
            "asc",
        ),
        (
            "국내 ETF를 120거래일 변동성 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "one_hundred_twenty_day_volatility_pct",
            "asc",
        ),
        (
            "국내 ETF를 252거래일 변동성 낮은 순으로 3개 보여줘.",
            "domestic_etp",
            "two_hundred_fifty_two_day_volatility_pct",
            "asc",
        ),
        (
            "국내 ETF를 분배수익률 높은 순으로 3개 보여줘.",
            "domestic_etp",
            "distribution_yield_pct",
            "desc",
        ),
    ],
)
def test_new_official_etp_metrics_compile_without_semantic_downgrade(
    question: str,
    family: str,
    field: str,
    direction: str,
) -> None:
    decision = IntentRouter().route(question, f"official-new-metric-{field}")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert hints["required_rankings"] == [{"field": field, "direction": direction, "nulls": "last"}]
    assert hints["ambiguity_spans"] == []
    assert hints["unsupported_spans"] == []


@pytest.mark.parametrize(
    ("question", "expected_column"),
    [
        ("국내 ETF를 거래량 큰 순으로 3개 보여줘.", "daily_volume_hundredth_units"),
        (
            "국내 ETF를 추적오차율 낮은 순으로 3개 보여줘.",
            "tracking_error_hundredth_pct",
        ),
        (
            "국내 ETF를 120거래일 변동성 낮은 순으로 3개 보여줘.",
            "one_hundred_twenty_day_volatility_eight_decimal_pct",
        ),
        (
            "국내 ETF를 분배수익률 높은 순으로 3개 보여줘.",
            "distribution_yield_eight_decimal_pct",
        ),
        (
            "해외 ETF를 외화거래대금 큰 순으로 3개 보여줘.",
            "daily_trading_value_eight_decimals",
        ),
        ("해외 ETF를 종가 낮은 순으로 3개 보여줘.", "close_price_eight_decimals"),
    ],
)
def test_new_official_etp_metric_sql_excludes_zero_and_missing(
    question: str,
    expected_column: str,
) -> None:
    decision = IntentRouter().route(question, f"official-new-sql-{expected_column}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    select_sql, count_sql, _ = compile_search_sql(plan)

    for sql in (select_sql, count_sql):
        assert f"{expected_column} IS NOT NULL" in sql
        assert f"{expected_column} <> 0" in sql


def test_distribution_payment_count_is_preserved_as_a_locked_filter() -> None:
    question = "연 12회 분배하는 국내 ETF 중 분배수익률 높은 순으로 5개 보여줘."
    decision = IntentRouter().route(question, "official-distribution-count")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert [(item.field, item.operator.value, item.value) for item in plan.constraints] == [
        ("product_type", "eq", "ETF"),
        ("distribution_payments_per_year", "eq", 12),
    ]
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("distribution_yield_pct", "desc")
    ]

    select_sql, count_sql, _ = compile_search_sql(plan)
    for sql in (select_sql, count_sql):
        assert "distribution_payments_per_year_quality IN ('VALID', 'PARTIAL')" in sql
        assert "distribution_payments_per_year_units = ?" in sql


def test_natural_distribution_payment_count_is_preserved() -> None:
    hints = build_lexical_hints(
        "분배금을 해마다 12번 지급하고 연금 거래가 가능한 국내 ETF를 "
        "연환산 분배수익률 상위 5개로 보여 주세요."
    )

    assert ("distribution_payments_per_year", "eq", 12) in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]


@pytest.mark.parametrize(
    ("question", "family", "constraint", "ranking"),
    [
        (
            "90일 안에 만기인 국내채권 중 이자를 뺀 평가가격이 낮은 순으로 3개 보여줘.",
            "bond",
            ("remaining_days", "lte", 90),
            ("evaluation_price", "asc"),
        ),
        (
            "해외채권 비중이 절반을 넘는 공모펀드를 1년 수익률 높은 순으로 4개 보여줘.",
            "fund",
            ("overseas_bond_ratio_pct", "gt", 50),
            ("one_year_return_pct", "desc"),
        ),
        (
            "온라인 클래스 공모펀드를 판매회사 보수가 낮은 순으로 4개 보여줘.",
            "fund",
            ("sales_channel", "eq", "온라인"),
            ("sale_company_fee_pct", "asc"),
        ),
        (
            "종가가 1만 원 이하인 국내 ETF를 거래량 큰 순으로 3개 보여줘.",
            "domestic_etp",
            ("close_price", "lte", 10000),
            ("daily_volume", "desc"),
        ),
    ],
)
def test_official_filters_survive_router_and_compiler(
    question: str,
    family: str,
    constraint: tuple[str, str, object],
    ranking: tuple[str, str],
) -> None:
    decision = IntentRouter().route(question, f"official-filter-{family}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert constraint in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]
    assert ranking in [(item.field, item.direction.value) for item in plan.ranking]


@pytest.mark.parametrize(
    ("question", "field"),
    [
        ("국내채권을 민평수익률 높은 순으로 3개 보여줘.", "applied_yield_pct"),
        ("국내채권을 이자부단가 낮은 순으로 3개 보여줘.", "dirty_price"),
        ("국내채권을 컨벡시티 높은 순으로 3개 보여줘.", "convexity"),
        ("공모펀드를 신탁업자 보수가 낮은 순으로 3개 보여줘.", "trustee_fee_pct"),
        ("공모펀드를 최근 분배율 높은 순으로 3개 보여줘.", "latest_distribution_pct"),
        ("공모펀드를 보전율 높은 순으로 3개 보여줘.", "preservation_ratio_pct"),
    ],
)
def test_new_official_bond_and_fund_rankings_compile(
    question: str,
    field: str,
) -> None:
    decision = IntentRouter().route(question, f"official-rank-{field}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, "desc" if "높은" in question else "asc")
    ]


@pytest.mark.parametrize(
    ("question", "family", "field", "direction"),
    [
        (
            "낮은위험(5등급) 국내 ETF 중 최근 20거래일을 연환산한 흔들림이 작은 4개를 찾습니다.",
            "domestic_etp",
            "twenty_day_volatility_pct",
            "asc",
        ),
        (
            "합성복제 ETF를 괴리율 원천값이 큰 순서로 4개 나열해 주세요.",
            "domestic_etp",
            "discrepancy_pct",
            "desc",
        ),
        (
            "상환까지 10년 이상 남은 장기 국내채권을 금리 민감도가 큰 것부터 5개 정렬해 주세요.",
            "bond",
            "duration_years",
            "desc",
        ),
        (
            "공모 회사채만 놓고 경과이자를 포함한 단가가 낮은 것부터 5개 보여 주세요.",
            "bond",
            "dirty_price",
            "asc",
        ),
        (
            "유동화회사채 중 민평에서 계산된 수익률이 높은 상위 3개를 알고 싶어요.",
            "bond",
            "applied_yield_pct",
            "desc",
        ),
        (
            "은행채를 민평 수익률이 센 순서대로 다섯 개만 정리해 주세요.",
            "bond",
            "applied_yield_pct",
            "desc",
        ),
        (
            "회사채나 특수채에 해당하는 종목 가운데 컨벡시티 수치가 큰 순으로 3개 보여 주세요.",
            "bond",
            "convexity",
            "desc",
        ),
        (
            "종가가 100 이상인 해외 ETF 중 외화거래대금이 큰 상위 3개는 무엇인가요?",
            "overseas_etp",
            "daily_trading_value",
            "desc",
        ),
        (
            "투자지역이 China, Japan, India 중 하나인 해외 ETF를 연간보수가 "
            "낮은 순으로 5개 보여 주세요.",
            "overseas_etp",
            "total_expense_ratio_pct",
            "asc",
        ),
    ],
)
def test_official_metric_wording_variants_keep_execution_authority(
    question: str,
    family: str,
    field: str,
    direction: str,
) -> None:
    decision = IntentRouter().route(question, f"official-wording-{field}")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert hints["required_rankings"] == [{"field": field, "direction": direction, "nulls": "last"}]
    assert hints["ambiguity_spans"] == []


def test_fund_composition_ratio_does_not_add_geography_scope() -> None:
    question = "국내주식 구성비율이 50 이상인 공모펀드를 1년 성과 순으로 상위 5개 보여 주세요."
    plan = ServerQueryPlanCompiler({}).compile(
        IntentRouter().route(question, "official-fund-composition")
    )

    assert ("domestic_stock_ratio_pct", "gte", 50) in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]
    assert all(item.field != "fund_geography_scope" for item in plan.constraints)


def test_bound_product_name_words_do_not_become_filters() -> None:
    question = (
        "공모펀드 ‘미래에셋국민성장혼합자산-사모재간접Ce’의 "
        "운용속성, 투자지역, 순자산을 알려 주세요."
    )
    masked = _mask_bound_product_mentions(
        question,
        ["미래에셋국민성장혼합자산-사모재간접Ce"],
    )
    hints = build_lexical_hints(masked, "fund", force_product_family_hint=True)

    assert all(
        item["field"] != "fund_management_attribute" for item in hints["required_constraints"]
    )


def test_exact_product_risk_is_a_projection_not_a_vague_filter() -> None:
    decision = SemanticCoverageGate().evaluate(
        "종목번호 KR5148450061인 공모펀드의 운용속성, 투자지역, "
        "위험등급명을 원천값대로 확인해 주세요.",
        interaction_intent="search",
    )

    assert decision.ambiguity_spans == ()


@pytest.mark.parametrize(
    ("question", "family", "expected_constraints"),
    [
        (
            "퇴직연금에 담을 수 있고 3년 안에 상환되는 국내채권 중 표면 금리가 가장 높은 4개",
            "bond",
            {("pension_eligible", "eq", True), ("remaining_days", "lte", 1095)},
        ),
        (
            "공개 모집된 채권 가운데 남아 있는 발행 물량이 1,000억 원 이상인 "
            "종목을 듀레이션이 긴 것부터 3개",
            "bond",
            {("public_offering", "eq", True), ("issue_amount", "gte", 100_000_000_000)},
        ),
        (
            "파생상품 운용속성 공모펀드 중 3개월 수익률이 높은 순으로 네 개",
            "fund",
            {("public_offering", "eq", True), ("fund_management_attribute", "eq", "파생상품")},
        ),
        (
            "KB가 운용사로 적힌 국내 ETF를 6개월 수익률 상위 3개",
            "domestic_etp",
            {("product_type", "eq", "ETF"), ("manager", "eq", "KB")},
        ),
        (
            "배수가 2인 국내 ETF 중 오늘 하루 수익률이 높은 세 종목",
            "domestic_etp",
            {("product_type", "eq", "ETF"), ("leverage_factor", "eq", 2.0)},
        ),
        (
            "Global Emerging Markets 해외 ETF를 외화거래대금이 큰 순으로 다섯 종목",
            "overseas_etp",
            {
                ("product_type", "eq", "ETF"),
                ("investment_region", "eq", "Global Emerging Markets"),
            },
        ),
        (
            "Alternatives로 분류된 해외 ETF를 외화거래대금이 많은 순으로 3개",
            "overseas_etp",
            {("product_type", "eq", "ETF"), ("asset_type", "eq", "Alternatives")},
        ),
    ],
)
def test_explicit_official_filter_wording_is_preserved(
    question: str,
    family: str,
    expected_constraints: set[tuple[str, str, object]],
) -> None:
    hints = build_lexical_hints(
        question,
        family,
        force_product_family_hint=True,
    )

    observed = {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }
    assert expected_constraints <= observed
    assert hints["ambiguity_spans"] == []


def test_fund_labeled_item_number_lowers_to_product_id() -> None:
    hints = build_lexical_hints("종목번호 KR5148450061인 공모펀드의 운용속성")

    assert {
        "field": "product_id",
        "operator": "eq",
        "value": "KR5148450061",
    } in hints["required_constraints"]
    assert all(item["field"] != "ticker" for item in hints["required_constraints"])


def test_ric_identity_infers_overseas_etp_without_family_word() -> None:
    decision = IntentRouter().route(
        "MNBD.K의 기초지수, 투자자산군, 설정일을 원천값 그대로 확인해 주세요.",
        "official-ric-detail",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "detail"
    assert [item.value for item in decision.draft.product_families] == ["overseas_etp"]


def test_comparison_metric_names_do_not_collide() -> None:
    assert extract_comparison_fields(
        "민평에서 산출한 금리가 더 높은 쪽",
        ProductFamily.BOND,
    ) == ["applied_yield_pct"]
    assert extract_comparison_fields(
        "연환산 분배수익률이 더 높은 쪽",
        ProductFamily.DOMESTIC_ETP,
    ) == ["distribution_yield_pct"]


@pytest.mark.parametrize(
    ("question", "field", "direction"),
    [
        (
            "국내채권 중 민평에서 산출한 금리가 더 높은 순으로 3개 보여 주세요.",
            "applied_yield_pct",
            "desc",
        ),
        (
            "국내채권 중 민평에서 계산된 수익률이 낮은 순으로 3개 보여 주세요.",
            "applied_yield_pct",
            "asc",
        ),
        (
            "컨벡시티가 1 미만인 국내채권을 만기까지 남은 날이 적은 순으로 4개 찾으세요.",
            "remaining_days",
            "asc",
        ),
        (
            "국내채권을 만기까지 남은 기간이 긴 순으로 4개 찾으세요.",
            "remaining_days",
            "desc",
        ),
    ],
)
def test_directional_bond_metric_aliases_do_not_conflict(
    question: str,
    field: str,
    direction: str,
) -> None:
    decision = IntentRouter().route(question, f"directional-bond-{field}-{direction}")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert hints["required_rankings"] == [{"field": field, "direction": direction, "nulls": "last"}]
    assert hints["ambiguity_spans"] == []


@pytest.mark.parametrize(
    ("question", "family", "field"),
    [
        (
            "세 국내 ETF KR7292770005, KR7291620003, KR7367760006 중 "
            "반년 수익률 1등만 알려 주세요.",
            "domestic_etp",
            "six_month_return_pct",
        ),
        (
            "해외 ETF 세 종목 FGDL.K, EUAD.K, QQQ.O 중 외화거래대금 1위는 어느 것인가요?",
            "overseas_etp",
            "daily_trading_value",
        ),
        (
            "세 공모펀드 KR5117490044, KR5116450065, KR5114470010 중 "
            "6개월 수익률 1위만 골라 주세요.",
            "fund",
            "six_month_return_pct",
        ),
    ],
)
def test_ranked_exact_product_set_routes_to_bounded_search(
    question: str,
    family: str,
    field: str,
) -> None:
    decision = IntentRouter().route(question, f"official-ranked-set-{family}")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert len(decision.draft.product_mentions) == 3
    assert decision.draft.requested_limit == 1
    assert hints["required_rankings"] == [{"field": field, "direction": "desc", "nulls": "last"}]


def test_every_executable_registry_field_has_a_family_sql_binding() -> None:
    """Prevent a queryable field from reaching Oracle as an unbound KeyError."""

    registry = load_field_registry()
    capability_names = ("queryable", "selectable", "sortable", "aggregatable", "comparable")
    for family in registry.executable_dataset_names():
        expected = {
            field_id
            for field_id, definition in registry.fields.items()
            if family in definition.datasets
            and any(
                getattr(definition.resolve(family), capability) for capability in capability_names
            )
        }
        assert expected <= set(SQL_FIELDS_BY_FAMILY[family])


def test_fund_only_fields_never_leak_into_domestic_etp_sql_schema() -> None:
    fund_only = {
        "sales_channel",
        "interest_dividend_type",
        "sale_company_fee_pct",
        "trustee_fee_pct",
        "domestic_stock_ratio_pct",
        "domestic_bond_ratio_pct",
        "overseas_stock_ratio_pct",
        "overseas_bond_ratio_pct",
        "latest_distribution_pct",
        "preservation_ratio_pct",
    }

    assert fund_only <= set(SQL_FIELDS_BY_FAMILY["fund"])
    assert fund_only.isdisjoint(SQL_FIELDS_BY_FAMILY["domestic_etp"])


@pytest.mark.parametrize(
    ("question", "constraint", "ranking"),
    [
        (
            "모집구분이 공모이고 발행잔액이 1,000억 이상인 국내채권을 "
            "발행잔액이 큰 순으로 3개 알려 주세요.",
            ("public_offering", "eq", True),
            ("issue_amount", "desc"),
        ),
        (
            "퇴직연금 편입 가능 채권 가운데 평가일단가가 9,000 이상 "
            "10,000 이하인 상품을 평가일단가 오름차순으로 5개 보여 주세요.",
            ("pension_eligible", "eq", True),
            ("evaluation_price", "asc"),
        ),
        (
            "총 발행 규모가 5,000억 원 이상인 국내채권을 "
            "쿠폰금리가 높은 순으로 네 종목 제시해 주세요.",
            ("total_issue_amount", "gte", 500_000_000_000),
            ("coupon_rate_pct", "desc"),
        ),
        (
            "최종 NAV가 1만 원에서 5만 원 사이인 국내 ETF를 AUM이 큰 순으로 세 개 보여 주세요.",
            ("net_asset_value", "between", [10_000, 50_000]),
            ("aum", "desc"),
        ),
        (
            "인버스 또는 숏 여부가 Y인 해외 ETF를 하루 수익률 높은 순으로 4개 정렬해 주세요.",
            ("inverse_short", "eq", True),
            ("one_day_return_pct", "desc"),
        ),
        (
            "지수 추적 여부가 Y로 확인되는 해외 ETF를 "
            "연간보수율이 낮은 순으로 다섯 개 조회해 주세요.",
            ("index_tracking", "eq", True),
            ("total_expense_ratio_pct", "asc"),
        ),
        (
            "기준가가 25 이하인 해외 ETF에서 하루 수익률이 높은 상품 5개를 찾습니다.",
            ("base_price", "lte", 25),
            ("one_day_return_pct", "desc"),
        ),
        (
            "설정일이 2025년 1월 1일 이후인 해외 ETF를 순자산총액이 큰 순으로 5개 알려 주세요.",
            ("listing_date", "gte", "2025-01-01"),
            ("aum", "desc"),
        ),
        (
            "펀드통화코드가 USD인 해외 ETF를 보수율이 낮은 순으로 네 개 조회해 주세요.",
            ("fund_currency", "eq", "USD"),
            ("total_expense_ratio_pct", "asc"),
        ),
        (
            "클래스 수수료를 따로 징구하지 않는 공모펀드를 1년 성과가 높은 순으로 4개 알려 주세요.",
            ("fee_type", "eq", "수수료미징구"),
            ("one_year_return_pct", "desc"),
        ),
        (
            "집합투자업자보수가 1 이하인 공모펀드 중 1년 수익률 상위 4개를 보여 주세요.",
            ("management_company_fee_pct", "lte", 1),
            ("one_year_return_pct", "desc"),
        ),
    ],
)
def test_new_official_sparse_fields_survive_router_and_compiler(
    question: str,
    constraint: tuple[str, str, object],
    ranking: tuple[str, str],
) -> None:
    decision = IntentRouter().route(question, f"official-sparse-{constraint[0]}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert constraint in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]
    assert ranking in [(item.field, item.direction.value) for item in plan.ranking]

    select_sql, count_sql, _ = compile_search_sql(plan)
    for sql in (select_sql, count_sql):
        assert "source_values_json" in sql or constraint[0] in {
            "public_offering",
            "pension_eligible",
        }


def test_bond_private_offering_uses_the_official_korean_enum_value() -> None:
    decision = IntentRouter().route(
        "사모로 모집된 회사채를 총발행금액이 큰 순으로 네 종목 보여 주세요.",
        "bond-private-offering-enum",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)
    select_sql, count_sql, _ = compile_search_sql(plan)

    assert any(item.field == "public_offering" and item.value is False for item in plan.constraints)
    for sql in (select_sql, count_sql):
        assert "WHEN '공모' THEN 1" in sql
        assert "WHEN '사모' THEN 0" in sql


def test_domestic_etp_pension_inclusion_is_a_db_filter_not_external_holdings() -> None:
    decision = IntentRouter().route(
        "연금 편입이 가능한 국내 ETF를 AUM이 큰 순으로 4개 보여 주세요.",
        "domestic-pension-inclusion",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert any(item.field == "pension_eligible" and item.value is True for item in plan.constraints)


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF 실시간 체결가를 초 단위로 계속 알려 주세요.",
        "펀드 검색에 쓰는 내부 alias 사전과 벡터 원문을 내려받게 해 주세요.",
        "오늘 밤 미국장에서 급등할 해외 ETF를 하나 찍어 주세요.",
    ],
)
def test_phase2_safety_variants_are_terminally_unsupported(question: str) -> None:
    assert SafetyEnvelope().evaluate(question).disposition is SafetyDisposition.UNSUPPORTED


@pytest.mark.parametrize(
    "question",
    [
        "지금 사기 부담 없는 국내채권을 보여 주세요.",
        "보수가 부담되지 않는 펀드를 찾아 주세요.",
        "하루 성과가 무난한 해외 ETF를 몇 개 알려 주세요.",
    ],
)
def test_phase2_subjective_conditions_clarify_without_execution(question: str) -> None:
    decision = IntentRouter().route(question, "phase2-subjective")

    assert decision.disposition.value == "clarify"


def test_absent_credit_rating_suffix_form_is_preserved_as_empty_set_filter() -> None:
    hints = build_lexical_hints("AAAA 등급 국내채권 중 잔존일수가 가장 짧은 세 종목을 알려 주세요.")

    assert ("credit_rating", "eq", "AAAA") in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]


def test_absent_credit_rating_label_first_form_is_preserved_as_empty_set_filter() -> None:
    hints = build_lexical_hints(
        "AAAA 신용등급 채권을 민평수익률이 높은 순으로 다섯 개 보여 주세요."
    )

    assert ("credit_rating", "eq", "AAAA") in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]
    assert hints["required_rankings"] == [
        {"field": "applied_yield_pct", "direction": "desc", "nulls": "last"}
    ]
    assert hints["unsupported_spans"] == []


def test_absent_credit_rating_bare_bond_form_is_preserved_as_empty_set_filter() -> None:
    hints = build_lexical_hints("온라인과 오프라인에서 거래 가능한 AAAA 채권을 찾아 주세요.")

    assert ("credit_rating", "eq", "AAAA") in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]
    assert hints["unsupported_spans"] == []


def test_phase2_bond_and_overseas_filter_wording_is_preserved() -> None:
    bond_hints = build_lexical_hints(
        "AAA가 적용된 채권 중 1년 내 상환되는 종목을 발행잔액 큰 순으로 4개 보여 주세요."
    )
    overseas_hints = build_lexical_hints(
        "Europe로 표시된 해외 ETF를 연간보수율 오름차순으로 3개 보여 주세요."
    )

    assert ("credit_rating", "eq", "AAA") in [
        (item["field"], item["operator"], item["value"])
        for item in bond_hints["required_constraints"]
    ]
    assert ("remaining_days", "lte", 365) in [
        (item["field"], item["operator"], item["value"])
        for item in bond_hints["required_constraints"]
    ]
    assert ("investment_region", "eq", "Europe") in [
        (item["field"], item["operator"], item["value"])
        for item in overseas_hints["required_constraints"]
    ]


def test_comparison_table_preamble_does_not_steal_ranked_search_intent() -> None:
    decision = IntentRouter().route(
        "실제 데이터로 비교표를 만들고 있습니다. AA급(AA+, AA0, AA-)이면서 "
        "만기까지 2년을 넘기지 않는 채권을 금리 민감도가 작은 순으로 5개 골라 주세요.",
        "phase2-comparison-table",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"


def test_exact_detail_with_candidate_count_is_not_downgraded_to_aggregate() -> None:
    decision = IntentRouter().route(
        "국내채권 종목번호 KR351601G9C3의 상품명, 상환일자, 표면이자율을 "
        "알려 주세요. 결과 뒤에 후보가 총 몇 개인지도 알려 주세요.",
        "phase2-detail-count",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "detail"


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF와 공모펀드를 1년 수익률 기준으로 한 줄 순위에 합쳐 주세요. "
        "높은 순 상위 7개를 상품군과 함께 알려 주세요. 각 목록에는 상품번호와 "
        "상품명을 함께 적어 주세요.",
        "국내 ETF와 공모펀드를 따로 나누지 말고 6개월 수익률 하나로 순위를 "
        "매겨 주세요. 수익률이 0보다 작은 상품만 대상으로 낮은 순 4개면 됩니다.",
    ],
)
def test_global_return_ranking_has_no_presentation_or_repeated_metric_residuals(
    question: str,
) -> None:
    coverage = SemanticCoverageGate().evaluate(
        question,
        interaction_intent="search",
        check_exclusions=False,
    )

    assert coverage.ambiguity_spans == ()


@pytest.mark.parametrize(
    ("question", "families"),
    [
        (
            "첫째, 상환까지 10년 이상 남은 장기 국내채권을 듀레이션 큰 순으로 "
            "5개 보여 주세요. 둘째, Commodity 자산군 ETF를 하루 수익률 높은 순으로 "
            "4개 보여 주세요. 국내채권, 국내 ETF, 해외 ETF, 공모펀드 순서 중 "
            "해당되는 항목만 유지해 주세요.",
            ["bond", "overseas_etp"],
        ),
        (
            "첫째, 시설대여채 중 쿠폰이 높은 순으로 3개 보여 주세요. 둘째, 연금 "
            "거래가 가능한 국내 ETF를 분배수익률 높은 순으로 3개 보여 주세요. "
            "셋째, 높은위험(2등급) 공모펀드를 1개월 수익률 높은 순으로 3개 보여 "
            "주세요. 국내채권, 국내 ETF, 해외 ETF, 공모펀드 순서 중 해당되는 "
            "항목만 유지해 주세요.",
            ["bond", "domestic_etp", "fund"],
        ),
    ],
)
def test_ordinal_family_authority_ignores_trailing_answer_order_list(
    question: str,
    families: list[str],
) -> None:
    decision = IntentRouter().route(question, "phase2-ordinal-family")
    searches = ServerQueryPlanCompiler({}).compile_family_searches(decision)

    assert [item.value for item in decision.draft.product_families] == families
    assert [item.plan.product_families[0].value for item in searches] == families
    assert all(not item.plan.ambiguities for item in searches)


def test_ordinal_presentation_disclaimers_do_not_become_filter_ambiguities() -> None:
    question = (
        "첫째, 국공채 중 1년 내 상환되는 종목을 이자 제외 평가단가가 낮은 순으로 "
        "3개 알려 주세요. 둘째, 투자지역이 미국인 국내 ETF를 반년 수익률 높은 "
        "순으로 5개 알려 주세요. 각 조건은 상품군별로 따로 적용해 주세요. "
        "결측값이나 0을 순위값으로 사용하지 말아 주세요."
    )
    decision = IntentRouter().route(question, "phase2-presentation-strip")
    searches = ServerQueryPlanCompiler({}).compile_family_searches(decision)

    assert decision.disposition.value == "execute"
    assert all(not item.plan.ambiguities for item in searches)


@pytest.mark.parametrize(
    "question",
    [
        "첫째, AAA가 적용된 채권을 남은 발행잔액이 큰 순서대로 4개 조회해 주세요. "
        "둘째, Europe로 표시된 해외 ETF를 연간보수율 오름차순으로 세 개 정리해 "
        "주세요. 서로 다른 지표를 합산하지 말고 상품군마다 별도 순위로 정리해 "
        "주세요. 결측값이나 0을 순위값으로 사용하지 말아 주세요.",
        "첫째, AMX 거래소코드가 붙은 해외 ETF를 하루 거래량 상위 5개로 알려 "
        "주세요. 둘째, 클래스 수수료를 따로 징구하지 않는 공모펀드를 1년 성과가 "
        "높은 순으로 4개 알려 주세요. 단위가 다른 값은 직접 합치지 말고 각 "
        "상품군 안에서만 순위를 매겨 주세요.",
        "첫째, MBS로 분류된 채권은 어떤 종목이 먼저 상환되나요? 가까운 순서로 "
        "4개만 주세요. 둘째, 종가가 1만 원 이하인 ETF 중 거래량이 많은 상품 "
        "5개를 찾습니다. 각 조건은 상품군별로 따로 적용해 결과를 나눠 주세요. "
        "결과는 표보다 상품군별 짧은 목록으로 정리해 주세요.",
        "90일 안에 만기가 닿는 채권 중 이자를 뺀 평가가격이 높은 순으로 3개 "
        "확인하고 싶습니다. 상위 목록과 필터 통과 건수를 함께 제시해 주세요.",
    ],
)
def test_response_format_sentences_never_open_schema_dense(question: str) -> None:
    trace = IntentRouter(adaptive_semantic_enabled=True).route_with_planning(
        question,
        "presentation-fast-path",
    )

    assert trace.route_decision.disposition.value == "execute"
    assert trace.planning_decision.path.value == "deterministic_fast"
    compiler = ServerQueryPlanCompiler({})
    plans = (
        [item.plan for item in compiler.compile_family_searches(trace.route_decision)]
        if len(trace.route_decision.draft.product_families) > 1
        else [compiler.compile(trace.route_decision)]
    )
    assert all(not plan.ambiguities for plan in plans)


def test_global_metric_before_family_list_is_preserved_in_every_plan() -> None:
    question = (
        "1년 수익률로 국내 ETF와 공모펀드를 통합 비교해 주세요. "
        "수익률이 0보다 큰 상품만 골라 높은 순 6개를 제시해 주세요."
    )
    decision = IntentRouter().route(question, "phase2-global-prefix")
    searches = ServerQueryPlanCompiler({}).compile_family_searches(decision)

    assert [item.plan.ranking[0].field for item in searches] == [
        "one_year_return_pct",
        "one_year_return_pct",
    ]
    assert all(item.plan.ranking[0].direction.value == "desc" for item in searches)
    assert all(
        ("one_year_return_pct", "gt", 0)
        in [(item.field, item.operator.value, item.value) for item in search.plan.constraints]
        for search in searches
    )


def test_global_and_answer_direction_presentation_do_not_create_ambiguity() -> None:
    question = (
        "국내 ETF와 공모펀드를 따로 나누지 말고 6개월 수익률 하나로 "
        "순위를 매겨 주세요. 수익률이 0보다 작은 상품만 대상으로 낮은 순 "
        "4개면 됩니다. 순위값의 방향이 높은 순인지 낮은 순인지 함께 표시해 주세요."
    )
    decision = IntentRouter().route(question, "phase2-global-presentation")
    searches = ServerQueryPlanCompiler({}).compile_family_searches(decision)

    assert decision.disposition.value == "execute"
    assert all(not item.plan.ambiguities for item in searches)
    assert all(
        ("six_month_return_pct", "lt", 0)
        in [(item.field, item.operator.value, item.value) for item in search.plan.constraints]
        for search in searches
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "상환일자가 2027년 말까지인 국내채권을 잔여일이 짧은 순으로 네 종목 보여 주세요.",
            ("maturity_date", "lte", "2027-12-31"),
        ),
        (
            "상환까지 10년 이상 남은 장기 국내채권을 금리 민감도가 큰 순으로 5개 정렬해 주세요.",
            ("remaining_days", "gte", 3650),
        ),
        (
            "잔여기간이 2년 미만인 국내채권을 민평금리 높은 순으로 5개 부탁해요.",
            ("remaining_days", "lt", 730),
        ),
    ],
)
def test_phase2_natural_bond_time_filters_are_locked(
    question: str,
    expected: tuple[str, str, object],
) -> None:
    hints = build_lexical_hints(question)

    assert expected in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]


def test_explicit_fund_geography_field_does_not_merge_label_tokens() -> None:
    hints = build_lexical_hints(
        "해외펀드구분이 국내인 공모펀드 중 순자산이 큰 상위 세 개를 찾습니다."
    )

    geography = [
        item for item in hints["required_constraints"] if item["field"] == "fund_geography_scope"
    ]
    assert geography == [{"field": "fund_geography_scope", "operator": "eq", "value": "국내"}]


def test_overseas_aum_ranking_does_not_invent_usd_filter() -> None:
    hints = build_lexical_hints(
        "Mixed Assets 해외 ETF를 순자산총액이 큰 것부터 다섯 개 조회해 주세요."
    )

    assert not any(item["field"] == "trading_currency" for item in hints["required_constraints"])


def test_phase2_numeric_aliases_do_not_cross_contaminate_fields() -> None:
    domestic = build_lexical_hints(
        "연환산 분배수익률이 3 이상인 국내 ETF를 AUM이 큰 순으로 4개 알려 주세요."
    )
    overseas = build_lexical_hints(
        "1일 수익률이 2 이상인 해외 ETF를 외화거래대금이 큰 순으로 4개 정리해 주세요."
    )
    bond = build_lexical_hints(
        "AA0 등급이고 잔여기간이 1년 이상 5년 이하인 채권을 "
        "평가기관 수익률이 높은 순으로 다섯 개 찾아 주세요."
    )

    domestic_constraints = {
        (item["field"], item["operator"], str(item["value"]))
        for item in domestic["required_constraints"]
    }
    assert ("distribution_yield_pct", "gte", "3") in domestic_constraints
    assert not any(field == "leverage_factor" for field, _, _ in domestic_constraints)
    assert ("one_day_return_pct", "gte", "2") in {
        (item["field"], item["operator"], str(item["value"]))
        for item in overseas["required_constraints"]
    }
    assert ("remaining_days", "between", "[365, 1825]") in {
        (item["field"], item["operator"], str(item["value"]))
        for item in bond["required_constraints"]
    }


def test_exact_name_contains_never_degrades_to_whole_family_search() -> None:
    decision = IntentRouter().route(
        "Moonshot 이름이 들어간 해외 ETF의 하루 수익률을 보여 주세요.",
        "phase2-name-contains",
    )
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert ("product_name", "contains", "Moonshot") in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]


def test_explicit_exact_branded_name_stops_before_family_grammar() -> None:
    question = "AAAA 등급 국내채권과 정확한 이름이 KODEX AI로봇ETF인 국내 ETF를 각각 찾아 주세요."

    assert extract_named_product_mentions(question) == ["KODEX AI로봇ETF"]
    assert (
        SemanticCoverageGate()
        .evaluate(
            question,
            interaction_intent="search",
        )
        .ambiguity_spans
        == ()
    )


def test_related_external_entity_is_server_authorized_but_never_broad_sql() -> None:
    question = "Kimi 관련 해외 ETF가 있으면 보수율이 낮은 순으로 세 개 알려 주세요."
    decision = IntentRouter().route(question, "phase2-related-etp")
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=["Kimi", "해외 ETF"],
        needs_clarification=False,
    )
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.OVERSEAS_ETP,),
    )

    assert decision.disposition.value == "unsupported"
    assert [item.subject_span for item in sanitized.knowledge_requirements] == ["Kimi"]
    assert has_server_authorized_knowledge_requirements(question, sanitized)


@pytest.mark.parametrize(
    "question",
    [
        "Money Market 해외 ETF를 거래량 기준으로 큰 순서대로 3개 보여 주세요.",
        "Mixed Assets 해외 ETF를 순자산총액이 큰 것부터 다섯 개 조회해 주세요.",
        "Global Emerging Markets 해외 ETF를 외화거래대금이 큰 순으로 다섯 종목 조회해 주세요.",
    ],
)
def test_locked_english_schema_value_does_not_open_external_knowledge(question: str) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=["해외 ETF"],
        needs_clarification=False,
    )
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.OVERSEAS_ETP,),
    )

    assert sanitized.knowledge_requirements == []


def test_bare_external_etp_preserves_both_etp_families_for_db_intersection() -> None:
    question = "KODEX AI로봇ETF와 Kimi ETF를 상품번호·티커까지 정확히 확인해 주세요."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.DETAIL,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=["Kimi", "ETF"],
        needs_clarification=False,
    )
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.DOMESTIC_ETP,),
    )

    assert set(sanitized.product_families) == {
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }
    assert [item.subject_span for item in sanitized.knowledge_requirements] == ["Kimi"]


def test_multi_family_region_relation_uses_product_router_precedence() -> None:
    assert product_search_has_precedence("국내에 투자하는 ETF와 공모펀드, 국내채권을 모아 주세요.")


def test_ordinal_exact_id_batch_retains_search_parent_authority() -> None:
    decision = IntentRouter().route(
        "첫째, 채권 코드 KR381003GFC3 하나의 발행자를 알려 주세요. "
        "둘째, 해외 ETF RIC FSTA.K 한 종목의 운용사를 알려 주세요.",
        "phase2-ordinal-exact",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"


def test_ordinal_exact_quoted_names_do_not_become_residual_filters() -> None:
    decision = IntentRouter().route(
        "첫째, 국내 ETF ‘RISE 현대차고정피지컬AI’의 운용사와 위험등급명을 "
        "알려 주세요. 둘째, 공모펀드 ‘미래에셋국민성장혼합자산-사모재간접Ce’의 "
        "운용속성과 투자지역을 알려 주세요.",
        "phase2-ordinal-names",
    )

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"


def test_global_ex_us_is_one_exact_overseas_region_filter() -> None:
    question = (
        "미국을 제외한 글로벌 지역(Global Ex US)에 투자하는 해외 ETF를 "
        "1일 수익률 높은 순으로 3개 알려 주세요."
    )
    decision = IntentRouter().route(question, "phase2-global-ex-us")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert ("investment_region", "eq", "Global Ex US") in [
        (item.field, item.operator.value, item.value) for item in plan.constraints
    ]


def test_specific_company_bond_type_does_not_add_generic_company_class() -> None:
    hints = build_lexical_hints("유동화회사채 중 민평수익률이 높은 국내채권 3개를 보여 주세요.")

    constraints = {
        (item["field"], item["operator"], str(item["value"]))
        for item in hints["required_constraints"]
    }
    assert ("bond_type", "eq", "유동화회사채") in constraints
    assert not any(field == "bond_major_class" for field, _, _ in constraints)


def test_inclusive_remaining_day_range_deduplicates_endpoint_constraint() -> None:
    hints = build_lexical_hints(
        "AA0 등급이고 잔여기간이 1년 이상 5년 이하인 채권을 "
        "평가기관 수익률이 높은 순으로 다섯 개 찾아 주세요."
    )

    remaining = [
        item for item in hints["required_constraints"] if item["field"] == "remaining_days"
    ]
    assert remaining == [{"field": "remaining_days", "operator": "between", "value": [365, 1825]}]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "높은 위험으로 표기된 공모펀드를 1개월 수익률 높은 순으로 3개 보여 주세요.",
            "높은 위험",
        ),
        (
            "위험등급명이 낮은 위험인 공모펀드를 순자산 큰 순으로 3개 알려 주세요.",
            "낮은 위험",
        ),
    ],
)
def test_explicit_fund_risk_label_is_exact_not_alias_union(
    question: str,
    expected: str,
) -> None:
    hints = build_lexical_hints(question)

    risk = [item for item in hints["required_constraints"] if item["field"] == "risk_level"]
    assert risk == [{"field": "risk_level", "operator": "eq", "value": expected}]


def test_projection_wording_does_not_become_pension_filter() -> None:
    hints = build_lexical_hints("국내 ETF 069500의 연금거래 가능 여부와 운용사를 알려 주세요.")

    assert not any(item["field"] == "pension_eligible" for item in hints["required_constraints"])


def test_explicit_fund_currency_does_not_add_trading_currency() -> None:
    hints = build_lexical_hints(
        "펀드통화코드 USD인 해외 ETF를 1일 수익률 높은 순으로 3개 알려 주세요."
    )

    currency = [
        item
        for item in hints["required_constraints"]
        if item["field"] in {"fund_currency", "trading_currency"}
    ]
    assert currency == [{"field": "fund_currency", "operator": "eq", "value": "USD"}]


def test_domestic_etp_leverage_factor_supports_display_wording() -> None:
    hints = build_lexical_hints("배수 표기가 1인 국내 ETF를 순자산 큰 순으로 3개 보여 주세요.")

    assert ("leverage_factor", "eq", 1.0) in [
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    ]


def test_cross_family_incompatible_field_hint_clarifies_instead_of_http_500() -> None:
    decision = IntentRouter().route(
        "삼성 관련 채권과 ETF, 공모펀드를 한꺼번에 찾아 주세요.",
        "official-cross-family-incompatible-hint",
    )

    with pytest.raises(PlanCompilationBlockedError, match="unavailable for product families"):
        ServerQueryPlanCompiler({}).compile_family_searches(decision)
