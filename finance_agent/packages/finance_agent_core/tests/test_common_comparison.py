from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.backend import routed_result_to_backend
from finance_agent_core.contracts.queryplan import QueryPlan
from finance_agent_core.execution import (
    ComparisonResultVerifier,
    ResultVerifier,
    SQLiteOracle,
    build_product_comparison,
    build_product_evidence,
)
from finance_agent_core.storage import connect_read_only, load_all_records


def test_overseas_comparison_runs_end_to_end_and_exposes_backend_dto(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "해외 ETF AMX:B2와 AMX:B1의 총보수율과 AUM을 비교해줘",
        "compare-overseas-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent_payload.comparison_fields == [
        "total_expense_ratio_pct",
        "aum",
    ]
    assert [product.product_id for product in result.products] == ["AMX:B2", "AMX:B1"]
    assert [item.status for item in result.comparisons] == [
        "numeric_delta",
        "numeric_delta",
    ]
    assert [item.delta for item in result.comparisons] == ["-0.05", "-2000"]
    assert "차이(두 번째-첫 번째) -0.05%p" in result.answer

    backend = routed_result_to_backend(result)
    assert backend.comparisons == result.comparisons
    assert {citation.kind for citation in backend.citations} >= {
        "product_field",
        "comparison_field",
    }
    comparison_citation = next(
        item for item in backend.citations if item.kind == "comparison_field"
    )
    assert comparison_citation.evidence_refs == [
        "AMX:B2:total_expense_ratio_pct",
        "AMX:B1:total_expense_ratio_pct",
    ]


def test_domestic_comparison_preserves_request_order(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = domestic_sample_database
    result = RoutedFinanceAgent({"domestic_etp": path}).answer(
        "국내 ETF KR7000000003과 KR7000000002의 1개월 수익률과 종가를 비교해줘",
        "compare-domestic-001",
    )

    assert result.status == "executed"
    assert [product.product_id for product in result.products] == [
        "KR7000000003",
        "KR7000000002",
    ]
    assert [item.canonical_field for item in result.comparisons] == [
        "one_month_return_pct",
        "close_price",
    ]
    assert [item.delta for item in result.comparisons] == ["-20", "0"]
    assert "요청한 국내 ETP 2개 중 2개를 확인했습니다" in result.answer


def test_comparison_identity_failure_returns_clarification(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "해외 ETF AMX:B1과 AMX:NOPE의 AUM을 비교해줘",
        "compare-overseas-002",
    )

    assert result.status == "clarify"
    assert "정확히 찾지 못했습니다" in result.answer
    assert result.query_plan is None
    assert result.products == []
    assert result.comparisons == []


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "해외 ETF AMX:B1과 AMX:B2의 총보수율과 수익률을 비교해줘",
            "해외 ETP 수익률 비교는 현재 지원하지 않습니다",
        ),
        (
            "해외 ETF AMX:B1은 제외하고 AMX:B2의 AUM을 비교해줘",
            "비교 대상 역할을 바꾸는 표현",
        ),
    ],
)
def test_comparison_unsupported_field_and_target_role_fail_closed(
    sample_database: tuple[Path, list[object], object],
    question: str,
    expected: str,
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        question,
        f"compare-overseas-control-{expected[:3]}",
    )

    assert result.status == "clarify"
    assert expected in result.answer
    assert result.products == []
    assert result.comparisons == []


def test_queryplan_rejects_selectable_but_non_comparable_field() -> None:
    with pytest.raises(ValidationError, match="isin is not comparable"):
        QueryPlan.model_validate(
            {
                "schema_version": "1.0",
                "question_id": "compare-invalid-001",
                "intent": "compare",
                "product_families": ["overseas_etp"],
                "constraints": [
                    {
                        "field": "product_id",
                        "operator": "in",
                        "value": ["AMX:B1", "AMX:B2"],
                        "unit": "code",
                        "strength": "locked",
                    }
                ],
                "ranking": [],
                "projection": ["product_id", "product_name", "isin"],
                "limit": 2,
                "intent_payload": {
                    "comparison_fields": ["isin"],
                    "group_by": [],
                    "aggregations": [],
                    "explain_product_ids": [],
                },
                "ambiguities": [],
                "unsupported_conditions": [],
            }
        )


def test_comparison_result_verifier_rejects_tampered_delta(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    decision = agent.router.route(
        "해외 ETF AMX:B1과 AMX:B2의 총보수율을 비교해줘",
        "compare-overseas-003",
    )
    plan = agent.compiler.compile(decision)
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    comparison = build_product_comparison(
        plan,
        verified,
        build_product_evidence(plan, verified),
    )
    tampered = replace(
        comparison,
        fields=(replace(comparison.fields[0], delta=comparison.fields[0].delta + 1),),
    )

    with pytest.raises(ValueError, match="differs"):
        ComparisonResultVerifier().verify(plan, tampered)


@pytest.mark.parametrize(
    ("field_update", "expected_status", "expected_delta"),
    [
        ({"as_of": date(2026, 6, 15)}, "as_of_mismatch", None),
        ({"quality": QualityStatus.STALE}, "stale_input", "2000"),
    ],
)
def test_comparison_surfaces_as_of_and_stale_states(
    sample_database: tuple[Path, list[object], object],
    field_update: dict[str, object],
    expected_status: str,
    expected_delta: str | None,
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    decision = agent.router.route(
        "해외 ETF AMX:B1과 AMX:B2의 AUM을 비교해줘",
        f"compare-overseas-{expected_status}",
    )
    plan = agent.compiler.compile(decision)
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    second = products[1]
    changed_fields = [
        field.model_copy(update=field_update) if field.canonical_field == "aum" else field
        for field in second.fields
    ]
    products[1] = second.model_copy(update={"fields": changed_fields})

    comparison = build_product_comparison(plan, verified, products)
    evidence = comparison.fields[0]

    assert evidence.status == expected_status
    actual_delta = None if evidence.delta is None else str(evidence.delta)
    assert actual_delta == expected_delta
