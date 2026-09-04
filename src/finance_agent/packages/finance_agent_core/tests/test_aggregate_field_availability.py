from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import AggregateFunction
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import write_database


def _compile(question: str):
    decision = IntentRouter().route(question, f"availability-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    return ServerQueryPlanCompiler({}).compile(decision)


@pytest.mark.parametrize(
    ("question", "family", "field", "group_by"),
    [
        (
            "매수수익률이 유효한 채권과 사용할 수 없는 채권(0 또는 누락)을 "
            "두 그룹으로 나눠 개수를 알려줘요.",
            "bond",
            "buy_yield_pct",
            [],
        ),
        (
            "국내 ETP의 자산군별로 1년 수익률 유효 건수와 0·결측 건수를 "
            "함께 보여 주세요.",
            "domestic_etp",
            "one_year_return_pct",
            ["asset_type"],
        ),
        (
            "해외 ETP 중 자산군이 기재된 종목과 미기재 종목이 각각 몇 개인가요?",
            "overseas_etp",
            "asset_type",
            [],
        ),
        (
            "펀드를 국내·해외·국내외혼합으로 구분해 AUM 유효/미유효 건수를 "
            "각각 계산해 주세요.",
            "fund",
            "aum",
            ["fund_geography_scope"],
        ),
        (
            "국내 상장상품을 ETF와 ETN으로 나눠 기초지수 값이 있는 경우와 "
            "없는 경우의 수를 각각 집계해 주세요.",
            "domestic_etp",
            "base_index",
            ["product_type"],
        ),
    ],
)
def test_explicit_field_availability_compiles_to_count_field(
    question: str,
    family: str,
    field: str,
    group_by: list[str],
) -> None:
    plan = _compile(question)

    assert plan.product_families[0].value == family
    assert plan.intent_payload.group_by == group_by
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, field)
    ]
    assert all(
        not (
            item.field == field
            and item.operator.value in {"is_present", "is_missing"}
        )
        for item in plan.constraints
    )


def test_ordinary_filtered_count_remains_product_count() -> None:
    plan = _compile("주식형 해외 ETP 상품은 몇 개인가요?")

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]


@pytest.mark.parametrize(
    "question",
    [
        "AUM 값이 있는 해외 ETP는 몇 개인가요?",
        "AUM 값이 없는 해외 ETP는 몇 개인가요?",
    ],
)
def test_one_sided_availability_keeps_filter_and_counts_products(question: str) -> None:
    plan = _compile(question)

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]
    assert len(plan.constraints) == 1
    assert plan.constraints[0].field == "aum"
    assert plan.constraints[0].operator.value in {"is_present", "is_missing"}


@pytest.mark.parametrize(
    ("question", "field", "group_by"),
    [
        (
            "해외 ETP에서 AUM 값이 있는 종목과 없는 종목은 각각 몇 건인가요?",
            "aum",
            [],
        ),
        (
            "해외 ETP를 거래소별로 묶어 일간 수익률 유효 건수와 "
            "누락 건수를 계산해 줘.",
            "one_day_return_pct",
            ["exchange_code"],
        ),
        (
            "채권을 채권시장별로 나눠 표면금리 기재 수와 미기재 수를 집계해 줘.",
            "coupon_rate_pct",
            ["bond_market"],
        ),
        (
            "공모펀드 운용 속성별 판매회사 보수 입력 건수와 "
            "미입력 건수를 함께 알려줘.",
            "sale_company_fee_pct",
            ["fund_management_attribute"],
        ),
        (
            "해외 ETF 운용사 정보가 채워진 종목과 비어 있는 종목은 "
            "각각 몇 개야?",
            "manager",
            [],
        ),
        (
            "국내 ETP의 총보수 자료 보유·미보유 건수를 알려줘.",
            "total_expense_ratio_pct",
            [],
        ),
        (
            "펀드 투자지역별로 1년 수익률이 적힌 건수와 공란인 건수를 "
            "계산해 주세요.",
            "one_year_return_pct",
            ["investment_region"],
        ),
        (
            "채권을 발행사별로 표면금리 제공 건수와 미제공 건수를 "
            "합쳐 보여줘.",
            "coupon_rate_pct",
            ["issuer"],
        ),
        (
            "해외 ETP의 운용사 미제공 종목 수와 제공 종목 수를 각각 알려줘.",
            "manager",
            [],
        ),
        (
            "국내 ETP에서 기초지수 공란 건수와 적힌 건수를 함께 집계해 줘.",
            "base_index",
            [],
        ),
        (
            "공모펀드에서 환헤지 정보가 비어 있는 경우와 채워진 경우의 수를 "
            "나눠 보여줘.",
            "currency_hedged",
            [],
        ),
    ],
)
def test_availability_count_paraphrases_remain_field_bound(
    question: str,
    field: str,
    group_by: list[str],
) -> None:
    plan = _compile(question)

    assert plan.intent_payload.group_by == group_by
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, field)
    ]


def test_count_field_reports_available_and_missing_without_filtering_either_side(
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
    missing = records[0].model_copy(
        update={"asset_type": None, "source_values": source_values}
    )
    database_path = tmp_path / "availability.sqlite3"
    write_database(database_path, [missing, *records[1:]], manifest)

    result = RoutedFinanceAgent({"overseas_etp": database_path}).answer(
        "해외 ETP 중 자산군이 기재된 종목과 미기재 종목이 각각 몇 개인가요?",
        "availability-e2e",
    )

    assert result.status == "executed"
    searchable = sum(not record.is_quarantined for record in records)
    assert result.candidate_count == searchable
    assert len(result.aggregates) == 1
    evidence = result.aggregates[0]
    assert evidence.field == "asset_type"
    assert evidence.value == searchable - 1
    assert evidence.valid_count == searchable - 1
    assert evidence.missing_count == 1
    assert "제외 1개" in result.answer
