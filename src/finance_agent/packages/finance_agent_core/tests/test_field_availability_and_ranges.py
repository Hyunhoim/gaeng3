from __future__ import annotations

from decimal import Decimal

import pytest

from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.execution.oracle import _constraint_sql
from finance_agent_core.execution.sql_schema import SqlField


@pytest.mark.parametrize(
    ("question", "family", "field", "expected"),
    [
        (
            "만기까지 180일 이상 365일 이하 남은 채권을 쿠폰금리 높은 순으로 6개 찾아줘.",
            "bond",
            "remaining_days",
            [180, 365],
        ),
        (
            "장외 채권 중 세전 매수수익률이 3.8%부터 4.2%까지인 상품을 "
            "잔존기간 짧은 순으로 6개 보여줘.",
            "bond",
            "buy_yield_pct",
            [3.8, 4.2],
        ),
        (
            "경비율이 0.2% 이상 0.4% 이하인 해외 ETF를 자산규모 큰 순으로 7개 보여줘.",
            "overseas_etp",
            "total_expense_ratio_pct",
            [0.2, 0.4],
        ),
        (
            "최근 1개월 성과가 1% 이상 3% 이하인 공모펀드를 순자산 큰 순으로 7개 보여줘.",
            "fund",
            "one_month_return_pct",
            [1, 3],
        ),
        (
            "국내주식 편입비중이 70% 이상 90% 이하인 공모펀드를 "
            "6개월 수익률 높은 순으로 6개 찾아줘.",
            "fund",
            "domestic_stock_ratio_pct",
            [70, 90],
        ),
    ],
)
def test_labelled_inclusive_ranges_remain_owned_by_their_fields(
    question: str,
    family: str,
    field: str,
    expected: list[float],
) -> None:
    payload = canonicalize_query_plan_payload(
        question,
        {"question_id": "range-generalization", "product_families": [family]},
        force_product_family_hint=True,
    )
    matching = [item for item in payload["constraints"] if item["field"] == field]
    assert matching == [
        {
            "field": field,
            "operator": "between",
            "value": expected,
            "unit": matching[0]["unit"],
            "strength": "locked",
        }
    ]


@pytest.mark.parametrize(
    ("question", "family", "field", "operator"),
    [
        (
            "신용등급이 기재된 채권을 잔존일수 순으로 보여줘.",
            "bond",
            "credit_rating",
            "is_present",
        ),
        (
            "신용등급 정보가 비어 있는 채권을 보여줘.",
            "bond",
            "credit_rating",
            "is_missing",
        ),
        (
            "기초지수명이 등록된 국내 ETP를 상품명 오름차순으로 보여줘.",
            "domestic_etp",
            "base_index",
            "is_present",
        ),
        (
            "운용전략이 기록되지 않은 국내 ETP를 상품명 오름차순으로 보여줘.",
            "domestic_etp",
            "strategy",
            "is_missing",
        ),
        (
            "ISIN이 기재되지 않은 해외 ETP를 상품명 오름차순으로 보여줘.",
            "overseas_etp",
            "isin",
            "is_missing",
        ),
        (
            "판매채널 정보가 비어 있는 공모펀드를 1개월 수익률 순으로 보여줘.",
            "fund",
            "sales_channel",
            "is_missing",
        ),
    ],
)
def test_explicit_field_availability_becomes_a_typed_constraint(
    question: str,
    family: str,
    field: str,
    operator: str,
) -> None:
    payload = canonicalize_query_plan_payload(
        question,
        {"question_id": "availability-generalization", "product_families": [family]},
        force_product_family_hint=True,
    )
    plan = QueryPlan.model_validate(payload)
    matching = [item for item in plan.constraints if item.field == field]
    assert len(matching) == 1
    assert matching[0].operator.value == operator
    assert matching[0].value is True


def test_availability_sql_distinguishes_numeric_zero_and_text_blank() -> None:
    numeric_payload = canonicalize_query_plan_payload(
        "세후수익률 값이 있는 채권을 세후수익률 높은 순으로 보여줘.",
        {"question_id": "numeric-present", "product_families": ["bond"]},
        force_product_family_hint=True,
    )
    numeric_plan = QueryPlan.model_validate(numeric_payload)
    numeric = next(
        item for item in numeric_plan.constraints if item.field == "after_tax_yield_pct"
    )
    numeric_sql, numeric_params = _constraint_sql(
        numeric,
        {
            "after_tax_yield_pct": SqlField(
                "after_tax_yield_micro_pct",
                scale=Decimal("1000000"),
                quality_column="after_tax_yield_quality",
            )
        },
    )
    assert "IS NOT NULL" in numeric_sql
    assert "<> 0" in numeric_sql
    assert numeric_params == []

    text_payload = canonicalize_query_plan_payload(
        "ISIN이 기재되지 않은 해외 ETP를 상품명 오름차순으로 보여줘.",
        {"question_id": "text-missing", "product_families": ["overseas_etp"]},
        force_product_family_hint=True,
    )
    text_plan = QueryPlan.model_validate(text_payload)
    text = next(item for item in text_plan.constraints if item.field == "isin")
    text_sql, text_params = _constraint_sql(text, {"isin": SqlField("isin")})
    assert "IS NULL" in text_sql
    assert "TRIM" in text_sql
    assert text_params == []
