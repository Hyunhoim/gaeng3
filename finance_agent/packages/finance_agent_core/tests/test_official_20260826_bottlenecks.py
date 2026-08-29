import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    has_server_authorized_knowledge_requirements,
    verify_intent_analysis,
)
from finance_agent_core.agent.knowledge_router import product_search_has_precedence
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.product_comparison import extract_comparison_fields
from finance_agent_core.agent.providers import bond_vertical_slice_plan
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.safety import SafetyDisposition, SafetyEnvelope
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.execution.oracle import compile_search_sql
from finance_agent_core.execution.sql_schema import SQL_FIELDS_BY_FAMILY


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
