from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

import pytest

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.comparison_e2e_runner import (
    FundComparisonE2EEvaluationRunner,
    load_fund_comparison_e2e_suite,
)
from finance_agent_core.evaluation.comparison_parser_runner import (
    ExpectedFundComparisonDraftProvider,
)
from finance_agent_core.evaluation.product_comparison_runner import (
    ProductComparisonEvaluationRunner,
    load_product_comparison_suite,
)
from finance_agent_core.evaluation.runner import sha256_file
from finance_agent_core.evaluation.search_aggregate_benchmark import (
    execute_benchmark_case,
    load_search_aggregate_benchmark_suite,
)

_STAGE2_DATABASE_DIR = os.environ.get("FINANCE_STAGE2_DATABASE_DIR")


def test_frozen_public_fingerprints_cover_every_stage2_execution_cell() -> None:
    """Pin the public pre-Stage-2 A-side for all four families and Oracle intents.

    The family execution tests run these result shapes through the post-Stage-2
    ValidatedPlan path.  This matrix test prevents a family/intent baseline from
    disappearing while those distributed runtime regressions continue to pass.
    """

    covered: set[tuple[ProductFamily, str]] = set()
    search_aggregate = load_search_aggregate_benchmark_suite()

    assert len(search_aggregate.suite_sha256) == 64
    for family, data in search_aggregate.suite.data.items():
        assert len(data.database_sha256) == 64
        assert len(data.manifest_sha256) == 64
        assert len(data.source_file_sha256) == 64
        assert family in ProductFamily
    for case in search_aggregate.suite.cases:
        assert case.expected.candidate_count >= 0
        if case.intent == "search":
            assert case.expected.top_product_ids
            assert not case.expected.aggregates
        else:
            assert case.expected.aggregates
            assert not case.expected.top_product_ids
        covered.add((case.product_family, case.intent))

    product_comparison = load_product_comparison_suite()
    assert len(product_comparison.suite_sha256) == 64
    for family in product_comparison.suite.data:
        executed = [
            case
            for case in product_comparison.suite.cases
            if case.product_family is family and case.expected.status == "executed"
        ]
        assert executed
        for case in executed:
            assert len(case.expected.product_ids) == 2
            assert case.expected.comparison_fields
            assert list(case.expected.field_statuses) == case.expected.comparison_fields
            assert list(case.expected.deltas) == case.expected.comparison_fields
        covered.add((family, "compare"))

    fund_comparison = load_fund_comparison_e2e_suite()
    assert len(fund_comparison.suite_sha256) == 64
    assert fund_comparison.suite.dataset == ProductFamily.FUND.value
    assert fund_comparison.suite.cases
    for case in fund_comparison.suite.cases:
        assert case.field_statuses
        assert list(case.cell_value_fingerprints) == list(case.field_statuses)
        assert list(case.evidence_fingerprints) == list(case.field_statuses)
        for fingerprints in (
            *case.cell_value_fingerprints.values(),
            *case.evidence_fingerprints.values(),
        ):
            assert len(fingerprints) == 2
            assert all(len(value) == 64 for value in fingerprints)
    covered.add((ProductFamily.FUND, "compare"))

    assert covered == {
        (family, intent)
        for family in ProductFamily
        for intent in ("search", "compare", "aggregate")
    }


@pytest.mark.skipif(
    not _STAGE2_DATABASE_DIR,
    reason="set FINANCE_STAGE2_DATABASE_DIR to verify the approved DB artifacts",
)
def test_approved_four_family_databases_recompute_stage2_fingerprints() -> None:
    """Recompute 8 SEARCH/AGGREGATE and 54 COMPARE frozen cases on approved DBs."""

    # Import after evaluation runners are initialized.  Importing answering first
    # can re-enter the evaluation package while its public modules are only partly
    # initialized when this test module is collected on its own.
    from finance_agent_core.agent import RoutedFinanceAgent
    from finance_agent_core.answering import ExpectedGroundedAnswerProvider

    database_dir = Path(_STAGE2_DATABASE_DIR or "")
    contract_resource = files("finance_agent_core.evaluation.suites").joinpath(
        "stage2_approved_db_contract_20260822_g5.json"
    )
    contract = json.loads(contract_resource.read_text(encoding="utf-8"))
    search_aggregate = load_search_aggregate_benchmark_suite()
    product_comparison = load_product_comparison_suite()
    fund_comparison = load_fund_comparison_e2e_suite()
    source_suites = contract["source_suites"]
    assert source_suites["search_aggregate"]["sha256"] == search_aggregate.suite_sha256
    assert source_suites["product_compare"]["sha256"] == product_comparison.suite_sha256
    assert (
        source_suites["fund_compare_parser"]["sha256"] == fund_comparison.parser_suite.suite_sha256
    )
    assert source_suites["fund_compare_e2e"]["sha256"] == fund_comparison.suite_sha256

    database_paths = {family: database_dir / f"{family.value}.sqlite3" for family in ProductFamily}

    for family in ProductFamily:
        data_contract = contract["data"][family.value]
        database = database_paths[family]
        manifest = Path(f"{database}.manifest.json")
        assert sha256_file(database) == data_contract["database_sha256"]
        assert sha256_file(manifest) == data_contract["manifest_sha256"]
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert manifest_payload["source_file_sha256"] == data_contract["source_file_sha256"]

    search_aggregate_results = [
        execute_benchmark_case(case, database_paths[case.product_family])
        for case in search_aggregate.suite.cases
    ]
    search_aggregate_failures = {
        result.case_id: {"error": result.error, "checks": result.checks}
        for result in search_aggregate_results
        if not result.passed
    }
    assert not search_aggregate_failures

    product_runner = ProductComparisonEvaluationRunner(
        {
            family: database_paths[family]
            for family in (
                ProductFamily.BOND,
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
            )
        }
    )
    product_results = product_runner.run(product_comparison.suite.cases, workers=1)
    product_failures = {
        result.case_id: {"error": result.error, "checks": result.checks}
        for result in product_results
        if not result.passed
    }
    assert not product_failures

    fund_cases = fund_comparison.parser_suite.suite.cases
    fund_runner = FundComparisonE2EEvaluationRunner(
        database_paths[ProductFamily.FUND],
        ExpectedFundComparisonDraftProvider(fund_cases),
        ExpectedGroundedAnswerProvider(),
        fund_comparison.expectations,
    )
    fund_results = fund_runner.run(fund_cases, workers=1)
    fund_failures = {
        result.case_id: {"error": result.error, "checks": result.checks}
        for result in fund_results
        if not result.passed
    }
    assert not fund_failures

    assert len(search_aggregate_results) + len(product_results) + len(fund_results) == 62

    fund_agent = RoutedFinanceAgent(
        {"fund": database_paths[ProductFamily.FUND]},
        capability_execution_overrides={"fund"},
    )
    one_year_search = fund_agent.answer(
        "1년 수익률이 높은 공모펀드를 3개 보여줘",
        "approved-g5-fund-one-year-search",
    )
    assert one_year_search.status == "executed"
    assert one_year_search.candidate_count == 7_017
    assert [product.product_id for product in one_year_search.products] == [
        "KR5129470010",
        "KR5129470016",
        "KR5129470012",
    ]
    assert [
        next(
            field.normalized_value
            for field in product.fields
            if field.canonical_field == "one_year_return_pct"
        )
        for product in one_year_search.products
    ] == ["975.1", "972.7", "971.88"]
    assert any("상한 처리하지 않았습니다" in warning for warning in one_year_search.warnings)
    assert any("스냅샷 2026-07-11" in warning for warning in one_year_search.warnings)

    one_year_detail = fund_agent.answer(
        "공모펀드 상품 번호 KR5129470010의 1년 수익률 상세 정보를 알려줘",
        "approved-g5-fund-one-year-detail",
    )
    assert one_year_detail.status == "executed"
    detail_field = next(
        field
        for field in one_year_detail.products[0].fields
        if field.canonical_field == "one_year_return_pct"
    )
    assert detail_field.normalized_value == "975.1"
    assert detail_field.raw_values == {"fd_yr1_ern_r": "975.10"}
    assert detail_field.as_of.isoformat() == "2026-07-11"

    one_year_compare = fund_agent.answer(
        "공모펀드 KR5129470010과 KR5129470016의 1년 수익률을 비교해줘",
        "approved-g5-fund-one-year-compare",
    )
    assert one_year_compare.status == "executed"
    assert one_year_compare.comparisons[0].canonical_field == "one_year_return_pct"
    assert str(one_year_compare.comparisons[0].delta) == "-2.4"

    one_year_aggregate = fund_agent.answer(
        "공모펀드의 1년 수익률 평균을 집계해줘",
        "approved-g5-fund-one-year-aggregate",
    )
    assert one_year_aggregate.status == "executed"
    assert one_year_aggregate.candidate_count == 11_115
    assert str(one_year_aggregate.aggregates[0].value) == "51.983682485393"
    assert one_year_aggregate.aggregates[0].valid_count == 7_017
    assert one_year_aggregate.aggregates[0].missing_count == 4_098

    unsupported_long_return = fund_agent.answer(
        "2년 수익률이 높은 공모펀드를 보여줘",
        "approved-g5-fund-two-year-blocked",
    )
    assert unsupported_long_return.status in {"clarify", "unsupported"}
    assert unsupported_long_return.products == []
