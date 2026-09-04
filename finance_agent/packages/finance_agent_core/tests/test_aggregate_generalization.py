from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.numeric_language import requested_result_limit
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import AggregateFunction
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import write_database


def _compile(question: str):
    decision = IntentRouter().route(question, f"aggregate-generalization-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.draft.intent is InteractionIntent.AGGREGATE
    return ServerQueryPlanCompiler({}).compile(decision)


@pytest.mark.parametrize(
    "question",
    [
        "미국에 투자하는 국내 ETF가 몇 종목인지 알려 주세요.",
        "판매 중인 공모펀드는 상품이 몇 개인가요?",
        "AA 이상 국내채권이 몇 상품인지 세어 주세요.",
    ],
)
def test_natural_noun_count_wording_compiles_to_typed_count(question: str) -> None:
    plan = _compile(question)

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


def test_korean_product_kind_counter_is_not_confused_with_close_price() -> None:
    assert requested_result_limit("총보수 낮은 해외 ETF 7종 보여줘") == 7
    assert requested_result_limit("종가 낮은 해외 ETF 7종 보여줘") == 7


def test_how_many_kinds_is_a_count_aggregate() -> None:
    plan = _compile("해외 ETF가 몇 종인지 숫자만 집계해 줘.")

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


@pytest.mark.parametrize(
    ("question", "family", "field", "value"),
    [
        ("회사채의 전체 후보 수를 세어 주세요.", "bond", "bond_major_class", "회사채"),
        ("주식형 국내 ETP의 전체 후보 수를 세어 주세요.", "domestic_etp", "asset_type", "주식"),
        (
            "주식 자산형 해외 ETP의 전체 후보 수를 세어 주세요.",
            "overseas_etp",
            "asset_type",
            "Equity",
        ),
        ("국내형 공모펀드의 전체 후보 수를 세어 주세요.", "fund", "fund_geography_scope", "국내"),
    ],
)
def test_total_candidate_count_is_a_filtered_aggregate(
    question: str,
    family: str,
    field: str,
    value: object,
) -> None:
    plan = _compile(question)

    assert plan.product_families[0].value == family
    assert [(item.field, item.value) for item in plan.constraints] == [(field, value)]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


def test_etp_asset_adjective_owns_embedded_bond_family_surface() -> None:
    plan = _compile("총 몇 종목인지 부탁드립니다: 채권 자산형 해외 ETP.")

    assert [item.value for item in plan.product_families] == ["overseas_etp"]
    assert [(item.field, item.value) for item in plan.constraints] == [
        ("asset_type", "Bond")
    ]


def test_count_only_presentation_exclusion_is_not_a_filter_ambiguity() -> None:
    plan = _compile("상품 목록 말고 개수만 확인할게요. 장외 채권이 몇 개인지 알려줘.")

    assert [(item.field, item.value) for item in plan.constraints] == [
        ("bond_market", "장외")
    ]


def test_company_sale_scope_does_not_add_general_fund_sale_scope() -> None:
    plan = _compile(
        "상품 목록 말고 개수만 확인할게요. "
        "미래에셋에서 판매 가능한 공모펀드가 몇 개인지 알려줘."
    )

    assert [(item.field, item.value) for item in plan.constraints] == [
        ("company_sellable", True)
    ]


def test_bare_public_fund_dataset_name_is_not_an_offering_attribute_filter() -> None:
    plan = _compile("현재 판매 가능한 공모펀드는 모두 몇 개인가요?")

    assert [(item.field, item.value) for item in plan.constraints] == [("sellable", True)]


def test_credit_rating_literal_survives_aggregate_compilation() -> None:
    plan = _compile("AAA 등급 국내채권 표면금리 평균을 산출해 주세요.")

    assert [(item.field, item.operator.value, item.value) for item in plan.constraints] == [
        ("credit_rating", "eq", "AAA")
    ]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.AVG, "coupon_rate_pct")
    ]


@pytest.mark.parametrize(
    "question",
    [
        "공모펀드를 당사 판매 가능 여부별로 나눠 각 상품 수를 알려 주세요.",
        "공모펀드 상품 수를 미래에셋증권 판매 여부별로 구분해 주세요.",
        "공모펀드를 판매 회사 여부마다 묶어 각각 몇 개인지 알려 주세요.",
    ],
)
def test_company_sale_group_surface_is_not_a_true_only_filter(question: str) -> None:
    plan = _compile(question)

    assert plan.intent_payload.group_by == ["company_sellable"]
    assert all(item.field != "company_sellable" for item in plan.constraints)
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


def test_explicit_public_offering_attribute_remains_locked() -> None:
    plan = _compile("모집구분이 공모인 펀드는 총 몇 개인가요?")

    assert [(item.field, item.value) for item in plan.constraints] == [
        ("public_offering", True)
    ]


def test_total_product_count_and_average_keep_both_aggregations() -> None:
    plan = _compile("판매 중인 공모펀드의 전체 종목 수와 1년 수익률 평균을 알려 주세요.")

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id"),
        (AggregateFunction.AVG, "one_year_return_pct"),
    ]


@pytest.mark.parametrize(
    ("question", "group_field"),
    [
        ("공모펀드를 판매 회사 여부별로 나눠 각 종목 수를 알려 주세요.", "company_sellable"),
        ("국내채권을 금리 유형별로 나눠 각 종목 수를 알려 주세요.", "interest_rate_type"),
        ("국내채권을 금리 적용 방식별로 나눠 각 종목 수를 알려 주세요.", "interest_rate_type"),
        ("국내채권을 이자 지급 방식별로 나눠 각 종목 수를 알려 주세요.", "interest_payment_type"),
    ],
)
def test_new_group_keys_compile_as_typed_count(question: str, group_field: str) -> None:
    plan = _compile(question)

    assert plan.intent_payload.group_by == [group_field]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


def test_median_compiles_as_a_first_class_aggregate_function() -> None:
    plan = _compile("해외 ETF의 총보수율 중앙값을 알려 주세요.")

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.MEDIAN, "total_expense_ratio_pct")
    ]


def test_min_and_max_in_one_question_remain_two_typed_metrics() -> None:
    plan = _compile("해외 ETF의 AUM 최댓값과 최솟값을 함께 알려 주세요.")

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.MAX, "aum"),
        (AggregateFunction.MIN, "aum"),
    ]


@pytest.mark.parametrize(
    "question",
    [
        "해외 ETF를 거래 정지 여부별로 몇 종목인지 알려 주세요.",
        "해외 ETF의 거래 가능한 종목과 거래 정지 종목을 나눠 각각 몇 개인지 알려 주세요.",
    ],
)
def test_trading_status_count_uses_one_boolean_group_contract(question: str) -> None:
    plan = _compile(question)

    assert plan.intent_payload.group_by == ["trading_suspended"]
    assert all(item.field != "trading_suspended" for item in plan.constraints)
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


def test_median_min_max_and_status_count_execute_without_server_error(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    database_path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": database_path})

    median = agent.answer(
        "해외 ETF의 총보수율 중앙값을 알려 주세요.",
        "aggregate-generalization-median",
    )
    extrema = agent.answer(
        "해외 ETF의 AUM 최댓값과 최솟값을 함께 알려 주세요.",
        "aggregate-generalization-extrema",
    )
    grouped = agent.answer(
        "해외 ETF를 거래 정지 여부별로 몇 종목인지 알려 주세요.",
        "aggregate-generalization-status",
    )

    assert median.status == "executed"
    assert [(item.function, item.field, item.value) for item in median.aggregates] == [
        (AggregateFunction.MEDIAN, "total_expense_ratio_pct", "0.12")
    ]
    assert extrema.status == "executed"
    assert [(item.function, item.field, item.value) for item in extrema.aggregates] == [
        (AggregateFunction.MAX, "aum", "10000"),
        (AggregateFunction.MIN, "aum", "1000"),
    ]
    assert grouped.status == "executed"
    assert grouped.candidate_count == 10
    assert {
        (item.group_values["trading_suspended"], item.value)
        for item in grouped.aggregates
    } == {(False, 9), (True, 1)}


def test_grouped_aggregate_does_not_surface_missing_value_as_a_category(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    tmp_path: Path,
) -> None:
    _, records, manifest = sample_database
    source_values = dict(records[0].source_values)
    source_values["wu_inv_ast_type"] = None
    missing_group = records[0].model_copy(
        update={"asset_type": None, "source_values": source_values}
    )
    database_path = tmp_path / "missing-group.sqlite3"
    write_database(database_path, [missing_group, *records[1:]], manifest)

    result = RoutedFinanceAgent({"overseas_etp": database_path}).answer(
        "해외 ETF를 자산유형별로 나눠 각 종목 수를 알려 주세요.",
        "aggregate-generalization-missing-group",
    )

    assert result.status == "executed"
    assert all(item.group_values["asset_type"] is not None for item in result.aggregates)
