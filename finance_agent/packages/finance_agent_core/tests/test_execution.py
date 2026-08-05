from pathlib import Path

import pytest

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.providers import first_vertical_slice_plan
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.execution import (
    AggregateResultVerifier,
    ResultVerificationError,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    build_product_evidence,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
    verifier_projection_fields,
)
from finance_agent_core.storage import connect_read_only, load_all_records


def test_sql_oracle_and_python_verifier_agree(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-001")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)

    verified = ResultVerifier().verify(plan, executed, universe)

    assert verified.candidate_count == 6
    assert [record.ticker for record in verified.records] == [
        "B6",
        "B5",
        "B4",
        "B2",
        "B3",
    ]
    assert "Z0" not in {record.ticker for record in verified.records}


def test_verifier_rejects_tampered_order(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-002")
    executed = SQLiteOracle(path).execute(plan)
    tampered = executed.model_copy(update={"records": list(reversed(executed.records))})
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)

    with pytest.raises(ResultVerificationError, match="top results mismatch"):
        ResultVerifier().verify(plan, tampered, universe)


def test_projected_verifier_rejects_tampered_order(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-projected-001")
    executed = SQLiteOracle(path).execute(plan)
    tampered = executed.model_copy(update={"records": list(reversed(executed.records))})
    universe = load_projected_verifier_records(path, plan)

    with pytest.raises(ResultVerificationError, match="top results mismatch"):
        ResultVerifier().verify(plan, tampered, universe)


def test_field_evidence_contains_raw_source_and_field_date(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-003")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)

    products = build_product_evidence(plan, verified)
    aum = next(field for field in products[0].fields if field.canonical_field == "aum")

    assert aum.source_columns == ["du_last_aum"]
    assert aum.raw_values == {"du_last_aum": "6000"}
    assert aum.normalized_value == "6000"
    assert aum.as_of.isoformat() == "2026-06-16"
    assert aum.quality.value == "VALID"


def test_overseas_aggregate_executes_with_missing_value_disclosure(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "해외 ETF의 총보수율 평균과 최댓값을 집계해줘",
        "aggregate-overseas-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "aggregate"
    assert result.candidate_count == 10
    assert [(item.function.value, item.field, item.value) for item in result.aggregates] == [
        ("avg", "total_expense_ratio_pct", "0.138888888889"),
        ("max", "total_expense_ratio_pct", "0.25"),
    ]
    assert all(item.valid_count == 9 for item in result.aggregates)
    assert all(item.missing_count == 1 for item in result.aggregates)
    assert "결측·UNKNOWN·INVALID 값은 0으로 바꾸지 않고" in result.answer


def test_aggregate_verifier_rejects_tampered_metric(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    decision = agent.router.route(
        "해외 ETF의 총보수율 평균을 집계해줘",
        "aggregate-overseas-002",
    )
    plan = agent.compiler.compile(decision)
    executed = SQLiteAggregateOracle(path).execute(plan)
    metric = executed.groups[0].metrics[0].model_copy(update={"value": "999"})
    group = executed.groups[0].model_copy(update={"metrics": [metric]})
    tampered = executed.model_copy(update={"groups": [group]})
    universe = load_projected_verifier_records(path, plan)

    with pytest.raises(ResultVerificationError, match="groups or metrics differ"):
        AggregateResultVerifier().verify(plan, tampered, universe)


def test_overseas_verifier_projection_matches_normalized_records(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, records, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    decision = agent.router.route(
        "해외 ETF의 총보수율 평균과 최댓값을 집계해줘",
        "projection-overseas-001",
    )
    plan = agent.compiler.compile(decision)
    projected = load_projected_verifier_records(path, plan)
    expected = {record.product_id: record for record in records}

    assert [record.product_id for record in projected] == sorted(expected)
    for record in projected:
        original = expected[record.product_id]
        assert record.is_quarantined == original.is_quarantined
        for field_name in verifier_projection_fields(plan):
            assert record.canonical_value(field_name) == original.canonical_value(field_name)
            assert (
                record.row_level_quality(field_name)[0] == original.row_level_quality(field_name)[0]
            )
