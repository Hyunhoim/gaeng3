from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import (
    FundComparisonDraft,
    FundProductResolver,
    RoutedFinanceAgent,
    compile_fund_comparison_query_plan,
)
from finance_agent_core.agent.providers import (
    fund_comparison_plan,
    fund_vertical_slice_plan,
)
from finance_agent_core.answering import (
    ExpectedGroundedAnswerProvider,
    build_grounded_answer_context,
    compose_grounded_answer,
)
from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest, NormalizedPublicFundRecord
from finance_agent_core.evaluation.answer_cli import build_parser as build_answer_parser
from finance_agent_core.evaluation.answer_runner import (
    AnswerEvaluationRunner,
    build_answer_report,
)
from finance_agent_core.evaluation.comparison_cli import (
    build_parser as build_comparison_parser,
)
from finance_agent_core.evaluation.comparison_e2e_cli import (
    build_parser as build_comparison_e2e_parser,
)
from finance_agent_core.evaluation.comparison_e2e_runner import (
    FundComparisonE2EEvaluationRunner,
    FundComparisonE2EExpectation,
    build_fund_comparison_e2e_report,
    comparison_cell_value_fingerprints,
    comparison_evidence_fingerprints,
    load_fund_comparison_e2e_suite,
)
from finance_agent_core.evaluation.comparison_parser_runner import (
    ExpectedFundComparisonDraftProvider,
    FundComparisonParserCase,
    FundComparisonParserExpectation,
    fund_comparison_plan_contract_exact,
)
from finance_agent_core.evaluation.comparison_runner import (
    FundComparisonCase,
    FundComparisonEvaluationRunner,
    FundComparisonExpectation,
    build_fund_comparison_report,
    load_fund_comparison_suite,
)
from finance_agent_core.evaluation.models import (
    EvaluationCase,
    EvaluationSplit,
    ExpectedBlocker,
    ExpectedConstraint,
    ExpectedDisposition,
    OracleExpectation,
)
from finance_agent_core.evaluation.runner import EvaluationRunner
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
    build_fund_comparison,
    build_product_evidence,
    render_verified_comparison,
    render_verified_search,
    require_executable_comparison,
    require_executable_search,
    require_internal_evaluation_comparison,
    require_internal_evaluation_search,
)
from finance_agent_core.execution.oracle import compile_search_sql
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
    verifier_projection_fields,
)
from finance_agent_core.normalization import (
    PublicFundNormalizationError,
    PublicFundNormalizationResult,
    normalize_public_fund_rows,
)
from finance_agent_core.storage import (
    connect_read_only,
    load_all_records,
    load_manifest,
    load_public_fund_attributes,
    load_public_fund_quarantine,
    write_public_fund_database,
)


def _validated(plan: QueryPlan, path: Path):
    return authorize_internal_evaluation_plan(plan, path)


def fund_product(
    product_id: str,
    *,
    public_offering: str,
    aum: str,
    management_attribute: str | None = "주식형",
    one_year_return: str | None = "10",
) -> dict[str, object]:
    return {
        "itm_no": product_id,
        "itm_nm": f"테스트 공모펀드 {product_id}",
        "itm_abrv_nm": f"테스트 {product_id}",
        "curr_cd": "KRW",
        "prvo_pbff_desc": public_offering,
        "sale_yn": "판매중",
        "thco_sale_yn": "Y",
        "exchdg_yn": "N",
        "fd_ivst_rgn_desc": "글로벌",
        "ovrs_fd_desc": "해외",
        "pers_corp_desc": "개인",
        "or_attr_desc": management_attribute,
        "zrin_fd_ivst_risk_gcd": "2",
        "fd_nast_suma": aum,
        "bmrk_nm": "테스트 지수",
        "fd_wk1_ern_r": "1.25",
        "fd_mm1_ern_r": "2.5",
        "fd_mm3_ern_r": "3.75",
        "fd_mm6_ern_r": "4.125",
        "fd_mm18_ern_r": "20",
        "fd_yr1_ern_r": one_year_return,
        "fd_yr2_ern_r": "30",
        "fd_yr3_ern_r": "40",
        "fd_yr5_ern_r": "50",
    }


def sample_fund_result() -> PublicFundNormalizationResult:
    public = fund_product(
        "KR0000000001",
        public_offering="공모",
        aum="123456.7891",
    )
    private = fund_product(
        "KR0000000002",
        public_offering="사모",
        aum="0",
        management_attribute="06",
    )
    return normalize_public_fund_rows(
        [
            (2, 25, {**public, "prfd_attr_cd": "A01"}),
            (3, 25, {**public, "prfd_attr_cd": "A02"}),
            (4, 25, {**private, "prfd_attr_cd": "A01"}),
            (
                5,
                2,
                {
                    "itm_no": '"',
                    "prfd_attr_cd": None,
                },
            ),
        ],
        source_snapshot_date=date(2026, 7, 11),
    )


def write_sample_fund_database(
    tmp_path: Path,
) -> tuple[Path, PublicFundNormalizationResult, DatabaseManifest]:
    result = sample_fund_result()
    path = tmp_path / "fund.sqlite3"
    manifest = DatabaseManifest(
        schema_version="1.1",
        dataset="fund",
        registry_schema_version="1.3",
        source_file_name="synthetic_public_fund.xlsx",
        source_file_sha256="d" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=4,
        searchable_rows=1,
        quarantined_rows=1,
        logical_product_rows=2,
        attribute_rows=3,
        scope_excluded_rows=1,
    )
    write_public_fund_database(path, result, manifest)
    return path, result, manifest


def write_comparison_fund_database(
    tmp_path: Path,
    *,
    second_currency: str = "KRW",
    second_three_month_return: str | None = "5.25",
    second_one_year_return: str | None = "600",
) -> tuple[Path, PublicFundNormalizationResult, DatabaseManifest]:
    first = fund_product(
        "KR0000000001",
        public_offering="공모",
        aum="123456.7891",
    )
    second = fund_product(
        "KR0000000002",
        public_offering="공모",
        aum="223456.7891",
    )
    second.update(
        {
            "curr_cd": second_currency,
            "zrin_fd_ivst_risk_gcd": "4",
            "fd_mm3_ern_r": second_three_month_return,
            "fd_yr1_ern_r": second_one_year_return,
        }
    )
    result = normalize_public_fund_rows(
        [
            (2, 25, {**first, "prfd_attr_cd": "A01"}),
            (3, 25, {**second, "prfd_attr_cd": "A01"}),
        ],
        source_snapshot_date=date(2026, 7, 11),
    )
    return_label = (
        "missing"
        if second_three_month_return is None
        else second_three_month_return.replace(".", "_")
    )
    path = tmp_path / f"fund-comparison-{second_currency}-{return_label}.sqlite3"
    manifest = DatabaseManifest(
        schema_version="1.1",
        dataset="fund",
        registry_schema_version="1.3",
        source_file_name="synthetic_public_fund_comparison.xlsx",
        source_file_sha256="e" * 64,
        source_file_size_bytes=2345,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=2,
        searchable_rows=2,
        quarantined_rows=0,
        logical_product_rows=2,
        attribute_rows=2,
        scope_excluded_rows=0,
    )
    write_public_fund_database(path, result, manifest)
    return path, result, manifest


def write_one_year_return_fund_database(
    tmp_path: Path,
) -> tuple[Path, PublicFundNormalizationResult, DatabaseManifest]:
    rows = [
        fund_product(
            "KR0000000001",
            public_offering="공모",
            aum="100",
            one_year_return="975.10",
        ),
        fund_product(
            "KR0000000002",
            public_offering="공모",
            aum="200",
            one_year_return="10",
        ),
        fund_product(
            "KR0000000003",
            public_offering="공모",
            aum="300",
            one_year_return=None,
        ),
    ]
    result = normalize_public_fund_rows(
        [
            (source_row, 25, {**row, "prfd_attr_cd": "A01"})
            for source_row, row in enumerate(rows, start=2)
        ],
        source_snapshot_date=date(2026, 7, 11),
    )
    path = tmp_path / "fund-one-year-return.sqlite3"
    manifest = DatabaseManifest(
        schema_version="1.1",
        dataset="fund",
        registry_schema_version="1.3",
        source_file_name="synthetic_public_fund_one_year.xlsx",
        source_file_sha256="f" * 64,
        source_file_size_bytes=3456,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=3,
        searchable_rows=3,
        quarantined_rows=0,
        logical_product_rows=3,
        attribute_rows=3,
        scope_excluded_rows=0,
    )
    write_public_fund_database(path, result, manifest)
    return path, result, manifest


def test_routed_fund_comparison_accepts_required_family_prefix(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)

    result = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    ).answer(
        ("공모펀드 KR0000000001과 KR0000000002의 3개월 수익률과 AUM을 비교해줘"),
        "routed-fund-compare-001",
    )

    assert result.status == "executed"
    assert [product.product_id for product in result.products] == [
        "KR0000000001",
        "KR0000000002",
    ]
    assert [item.canonical_field for item in result.comparisons] == [
        "three_month_return_pct",
        "aum",
    ]


def test_routed_fund_execution_requires_explicit_family_override(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    question = "공모펀드 KR0000000001과 KR0000000002의 3개월 수익률을 비교해줘"

    locked = RoutedFinanceAgent({"fund": path}).answer(question, "fund-locked-001")
    approved = RoutedFinanceAgent(
        {"fund": path},
        capability_execution_overrides={"fund"},
    ).answer(question, "fund-approved-001")

    assert locked.status == "unsupported"
    assert locked.query_plan is None
    assert locked.decision.draft.intent.value == "unsupported"
    assert approved.status == "executed"
    assert approved.query_plan is not None
    assert approved.query_plan.product_families == [ProductFamily.FUND]
    assert [product.product_id for product in approved.products] == [
        "KR0000000001",
        "KR0000000002",
    ]


def test_fund_aggregate_groups_risk_levels_in_public_scope(tmp_path: Path) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    result = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    ).answer(
        "공모펀드의 위험등급별 분포를 집계해줘",
        "aggregate-fund-001",
    )

    assert result.status == "executed"
    assert result.candidate_count == 2
    assert {(item.group_values["risk_level"], item.value) for item in result.aggregates} == {
        ("높은위험(2등급)", 1),
        ("보통위험(4등급)", 1),
    }
    assert "공모펀드 기본 범위" in result.answer
    assert "클래스 단위" in result.answer


def test_fund_verifier_projection_matches_normalized_records(tmp_path: Path) -> None:
    path, records, manifest = write_sample_fund_database(tmp_path)
    agent = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    )
    decision = agent.router.route(
        "공모펀드의 위험등급별 분포를 집계해줘",
        "projection-fund-001",
    )
    plan = agent.compiler.compile(decision)
    validated = _validated(plan, path)
    projected = load_projected_verifier_records(path, validated)
    expected = {record.product_id: record for record in records.products}

    assert validated.receipt.max_verifier_rows == manifest.logical_product_rows
    assert validated.receipt.max_verifier_rows != manifest.total_rows
    assert [record.product_id for record in projected] == sorted(expected)
    for record in projected:
        original = expected[record.product_id]
        for field_name in verifier_projection_fields(plan):
            assert record.canonical_value(field_name) == original.canonical_value(field_name)
            assert (
                record.row_level_quality(field_name)[0] == original.row_level_quality(field_name)[0]
            )


def test_fund_amount_aggregate_requires_currency_or_currency_group(tmp_path: Path) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    agent = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    )

    blocked = agent.answer(
        "공모펀드의 AUM 평균을 집계해줘",
        "aggregate-fund-002",
    )
    grouped = agent.answer(
        "공모펀드의 통화별 AUM 평균을 집계해줘",
        "aggregate-fund-003",
    )

    assert blocked.status == "clarify"
    assert "trading_currency" in blocked.answer
    assert grouped.status == "executed"
    assert grouped.aggregates[0].group_values == {"trading_currency": "KRW"}
    assert grouped.aggregates[0].value == "173456.7891"


def test_routed_agent_executes_internal_fund_comparison(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    agent = RoutedFinanceAgent(
        {"fund": path},
        answer_provider=ExpectedGroundedAnswerProvider(),
        allow_internal_disabled_dataset=True,
    )

    result = agent.answer(
        "두 공모펀드 KR0000000001와 KR0000000002의 3개월 수익률을 비교해줘",
        "routed-fund-compare-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "compare"
    assert [product.product_id for product in result.products] == [
        "KR0000000001",
        "KR0000000002",
    ]
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer_composition.verification.passed


def test_public_fund_normalization_preserves_product_grain_and_quality() -> None:
    result = sample_fund_result()

    assert result.raw_rows == 4
    assert len(result.products) == 2
    assert len(result.attributes) == 3
    assert len(result.quarantine) == 1

    public, private = result.products
    assert public.product_id == "KR0000000001"
    assert public.attribute_count == 2
    assert public.public_offering is True
    assert public.risk_level == "높은위험(2등급)"
    assert public.one_month_return_pct == Decimal("2.5")
    assert public.field_quality["one_month_return_pct"] is QualityStatus.PARTIAL
    assert public.field_quality["one_year_return_pct"] is QualityStatus.PARTIAL
    assert public.one_year_return_pct == Decimal("10")

    assert private.public_offering is False
    assert private.aum == 0
    assert private.field_quality["aum"] is QualityStatus.UNKNOWN
    assert private.fund_management_attribute is None
    assert private.field_quality["fund_management_attribute"] is QualityStatus.UNKNOWN
    assert result.quarantine[0].source_row == 5


def test_public_fund_normalization_quarantines_conflicting_product() -> None:
    first = fund_product(
        "KR0000000001",
        public_offering="공모",
        aum="100",
    )
    second = {**first, "itm_nm": "서로 다른 상품명"}

    result = normalize_public_fund_rows(
        [
            (2, 25, {**first, "prfd_attr_cd": "A01"}),
            (3, 25, {**second, "prfd_attr_cd": "A02"}),
        ]
    )

    assert result.products == ()
    assert result.attributes == ()
    assert len(result.quarantine) == 2
    assert all(
        row.quarantine_reason.startswith("conflicting_product_common_fields:")
        for row in result.quarantine
    )


def test_public_fund_normalization_rejects_duplicate_raw_key() -> None:
    product = fund_product(
        "KR0000000001",
        public_offering="공모",
        aum="100",
    )

    with pytest.raises(PublicFundNormalizationError, match="duplicate raw key"):
        normalize_public_fund_rows(
            [
                (2, 25, {**product, "prfd_attr_cd": "A01"}),
                (3, 25, {**product, "prfd_attr_cd": "A01"}),
            ]
        )


def test_public_fund_sqlite_round_trip_preserves_all_three_grains(
    tmp_path: Path,
) -> None:
    path, _, manifest = write_sample_fund_database(tmp_path)

    with connect_read_only(path) as connection:
        loaded_manifest = load_manifest(connection)
        products = load_all_records(connection)
        attributes = load_public_fund_attributes(connection)
        quarantine = load_public_fund_quarantine(connection)
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert loaded_manifest == manifest
    assert len(products) == 2
    assert products[0].aum == Decimal("123456.7891")
    assert products[0].attribute_count == 2
    assert [attribute.attribute_code for attribute in attributes] == [
        "A01",
        "A02",
        "A01",
    ]
    assert attributes[0].quality is QualityStatus.UNKNOWN
    assert quarantine[0].raw_item_number == '"'
    assert foreign_key_errors == []


def test_public_fund_oracle_verifier_evidence_and_renderer_agree(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)
    plan = fund_vertical_slice_plan("fund-oracle-001")

    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    answer, warnings = render_verified_search(plan, verified)

    assert executed.candidate_count == 1
    assert "public_offering_quality IN ('VALID', 'PARTIAL')" in executed.sql_template
    assert len(verified.records) == 1
    assert isinstance(verified.records[0], NormalizedPublicFundRecord)
    assert verified.records[0].product_id == "KR0000000001"
    assert products[0].ticker is None
    evidence = {field.canonical_field: field for field in products[0].fields}
    assert evidence["public_offering"].normalized_value is True
    assert evidence["public_offering"].source_key == {"itm_no": "KR0000000001"}
    assert evidence["three_month_return_pct"].source_columns == ["fd_mm3_ern_r"]
    assert evidence["three_month_return_pct"].quality is QualityStatus.PARTIAL
    assert evidence["three_month_return_pct"].as_of == date(2026, 7, 11)
    assert "KR0000000001" in answer
    assert "공모펀드 기본 범위" in answer
    assert "클래스 단위" in answer
    assert len(warnings) == 5


def test_public_fund_one_year_return_search_preserves_outlier_and_excludes_missing(
    tmp_path: Path,
) -> None:
    path, _, _ = write_one_year_return_fund_database(tmp_path)
    result = RoutedFinanceAgent(
        {"fund": path},
        answer_provider=ExpectedGroundedAnswerProvider(),
        allow_internal_disabled_dataset=True,
    ).answer(
        "1년 수익률이 높은 공모펀드를 5개 찾아줘",
        "fund-one-year-search-001",
    )

    assert result.status == "executed"
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer_composition.verification.passed
    assert result.query_plan is not None
    assert result.query_plan.ranking[0].field == "one_year_return_pct"
    assert result.candidate_count == 2
    assert [product.product_id for product in result.products] == [
        "KR0000000001",
        "KR0000000002",
    ]
    first_fields = {field.canonical_field: field for field in result.products[0].fields}
    assert first_fields["one_year_return_pct"].raw_values == {"fd_yr1_ern_r": "975.10"}
    assert first_fields["one_year_return_pct"].normalized_value == "975.1"
    assert first_fields["one_year_return_pct"].source_columns == ["fd_yr1_ern_r"]
    assert first_fields["one_year_return_pct"].as_of == date(2026, 7, 11)
    assert any("상한 처리하지 않았습니다" in warning for warning in result.warnings)
    assert any("스냅샷 2026-07-11" in warning for warning in result.warnings)


def test_public_fund_one_year_return_detail_preserves_field_evidence(
    tmp_path: Path,
) -> None:
    path, _, _ = write_one_year_return_fund_database(tmp_path)
    result = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    ).answer(
        "공모펀드 상품 번호 KR0000000001의 1년 수익률 상세 정보를 알려줘",
        "fund-one-year-detail-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.limit == 1
    assert [product.product_id for product in result.products] == ["KR0000000001"]
    fields = {field.canonical_field: field for field in result.products[0].fields}
    assert fields["one_year_return_pct"].raw_values == {"fd_yr1_ern_r": "975.10"}
    assert fields["one_year_return_pct"].quality is QualityStatus.PARTIAL
    assert any("상한 처리하지 않았습니다" in warning for warning in result.warnings)


def test_public_fund_one_year_return_aggregate_uses_raw_valid_values_only(
    tmp_path: Path,
) -> None:
    path, _, _ = write_one_year_return_fund_database(tmp_path)
    result = RoutedFinanceAgent(
        {"fund": path},
        allow_internal_disabled_dataset=True,
    ).answer(
        "공모펀드의 1년 수익률 평균을 집계해줘",
        "fund-one-year-aggregate-001",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "aggregate"
    assert result.candidate_count == 3
    assert result.aggregates[0].value == "492.55"
    assert result.aggregates[0].valid_count == 2
    assert result.aggregates[0].missing_count == 1
    assert any("상한 처리하지 않았습니다" in warning for warning in result.warnings)


def test_public_fund_one_year_return_comparison_uses_raw_delta_and_warning(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    plan = fund_comparison_plan(
        "fund-one-year-compare-001",
        ["KR0000000001", "KR0000000002"],
        ["one_year_return_pct"],
    )
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)
    comparison = build_fund_comparison(plan, verified, evidence)
    field = comparison.fields[0]

    assert field.canonical_field == "one_year_return_pct"
    assert field.status == "numeric_delta"
    assert field.delta == Decimal("590")
    second_fields = {
        evidence_field.canonical_field: evidence_field
        for evidence_field in comparison.products[1].fields
    }
    assert second_fields["one_year_return_pct"].source_columns == ["fd_yr1_ern_r"]
    answer, warnings = render_verified_comparison(
        plan,
        comparison.verified,
        list(comparison.products),
    )
    assert "차이(두 번째-첫 번째) 590%p" in answer
    assert any("상한 처리하지 않았습니다" in warning for warning in warnings)


def test_public_fund_grounded_answer_compiles_and_verifies_field_evidence(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)
    plan = fund_vertical_slice_plan("fund-answer-001")
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    context = build_grounded_answer_context(
        question="해외 주식형 공모펀드를 3개월 수익률 순으로 보여줘",
        plan=plan,
        verified=verified,
        products=products,
    )

    composition = compose_grounded_answer(
        question=context.question,
        plan=plan,
        verified=verified,
        products=products,
        provider=ExpectedGroundedAnswerProvider(),
    )

    assert composition.mode == "llm_grounded"
    assert composition.verification.passed
    assert composition.verification.checks["compiled_core_exact"]
    assert composition.verification.checks["compiled_evidence_citations_exact"]
    assert composition.verification.checks["compiled_source_date_present"]
    assert "테스트 공모펀드 KR0000000001" in composition.answer
    assert "3개월 수익률 3.75%" in composition.answer
    assert "PRFD01N001 원본 행 2, fd_mm3_ern_r, 기준일 2026-07-11" in composition.answer
    assert "스냅샷 2026-07-11" in composition.answer
    assert "공모펀드 기본 범위" in composition.answer
    assert "클래스 단위" in composition.answer


def test_public_fund_answer_verifier_fails_closed_on_product_claim(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)
    plan = fund_vertical_slice_plan("fund-answer-fallback-001")
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    context = build_grounded_answer_context(
        question="공모펀드 결과를 설명해줘",
        plan=plan,
        verified=verified,
        products=products,
    )
    expected = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
    first = expected.products[0].model_copy(
        update={"explanation": "테스트 공모펀드 KR0000000001이 가장 좋은 상품입니다."}
    )
    tampered = expected.model_copy(update={"products": [first]})

    class TamperedProvider:
        provider_name = "tampered"
        model_name = "tampered-model"

        def generate_grounded_answer(self, _context):
            return tampered

    composition = compose_grounded_answer(
        question=context.question,
        plan=plan,
        verified=verified,
        products=products,
        provider=TamperedProvider(),
    )

    assert composition.mode == "deterministic_fallback"
    assert composition.answer == context.deterministic_answer
    assert not composition.verification.checks["prose_has_no_advice_or_forecast"]
    assert not composition.verification.checks["prose_has_no_product_identifiers"]
    assert "가장 좋은" not in composition.answer


def test_public_fund_answer_evaluation_is_internal_only(tmp_path: Path) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)
    runner = AnswerEvaluationRunner(
        path,
        ExpectedGroundedAnswerProvider(),
        allow_internal_disabled_dataset=True,
    )

    assert runner.product_family == "fund"
    assert build_answer_parser().parse_args(["--dataset", "fund"]).dataset == "fund"

    class UnapprovedAnswerProvider:
        provider_name = "unapproved"
        model_name = None

        def generate_grounded_answer(self, context):
            return ExpectedGroundedAnswerProvider().generate_grounded_answer(context)

    with pytest.raises(ValueError, match="restricted to expected or local_test"):
        AnswerEvaluationRunner(
            path,
            UnapprovedAnswerProvider(),
            allow_internal_disabled_dataset=True,
        )


def test_public_fund_answer_runner_preserves_safe_fallback_metrics(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)
    plan = fund_vertical_slice_plan("fund-answer-runner-001")
    case = EvaluationCase(
        id=plan.question_id,
        split=EvaluationSplit.DEVELOPMENT,
        category="grounded_fallback",
        question="해외 주식형 공모펀드를 3개월 수익률 순으로 보여줘",
        constraints=[
            ExpectedConstraint.model_validate(
                {
                    "field": constraint.field,
                    "operator": constraint.operator,
                    "value": constraint.value,
                    "strength": constraint.strength,
                }
            )
            for constraint in plan.constraints
        ],
        ranking=plan.ranking,
        limit=plan.limit,
        disposition=ExpectedDisposition.EXECUTE,
        oracle=OracleExpectation(
            candidate_count=1,
            top_product_ids=["KR0000000001"],
        ),
    )

    class InvalidEvidenceProvider:
        provider_name = "local_test"
        model_name = "invalid-evidence-model"

        def generate_grounded_answer(self, context):
            draft = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
            first = draft.products[0].model_copy(update={"evidence_fields": ["missing_field"]})
            return draft.model_copy(update={"products": [first]})

    runner = AnswerEvaluationRunner(
        path,
        InvalidEvidenceProvider(),
        allow_internal_disabled_dataset=True,
    )
    result = runner.run_case(case)
    report = build_answer_report(
        suite_id="fund-answer-fallback-test",
        suite_version="1.0",
        suite_sha256="a" * 64,
        database_sha256="b" * 64,
        manifest_sha256="c" * 64,
        provider="local_test",
        model="invalid-evidence-model",
        split="development",
        workers=1,
        results=[result],
    )

    assert result.mode == "deterministic_fallback"
    assert result.error is None
    assert result.checks["safe_answer"]
    assert report.summary.fallback_cases == 1
    assert report.summary.fallback_rate == 1.0


def test_public_fund_scope_is_mandatory_while_agent_execution_stays_disabled() -> None:
    plan = fund_vertical_slice_plan("fund-policy-001")
    payload = plan.model_dump(mode="json")
    payload["constraints"] = [
        constraint
        for constraint in payload["constraints"]
        if constraint["field"] != "public_offering"
    ]
    unscoped = QueryPlan.model_validate(payload)

    with pytest.raises(PlanExecutionBlockedError, match="public_offering = true"):
        compile_search_sql(unscoped)
    with pytest.raises(PlanExecutionBlockedError, match="not enabled for execution"):
        require_executable_search(plan)
    require_internal_evaluation_search(plan)


def test_public_fund_aum_comparison_requires_locked_currency_scope() -> None:
    payload = fund_vertical_slice_plan("fund-aum-policy-001").model_dump(mode="json")
    payload["ranking"] = [
        {
            "field": "aum",
            "direction": "desc",
            "nulls": "last",
        }
    ]
    without_currency = QueryPlan.model_validate(payload)

    with pytest.raises(PlanExecutionBlockedError, match="trading_currency = KRW or USD"):
        compile_search_sql(without_currency)

    payload["constraints"].append(
        {
            "field": "trading_currency",
            "operator": "eq",
            "value": "KRW",
            "unit": "code",
            "strength": "locked",
        }
    )
    scoped = QueryPlan.model_validate(payload)

    compile_search_sql(scoped)
    require_internal_evaluation_search(scoped)


def test_public_fund_compare_contract_is_exact_and_internal_only() -> None:
    plan = fund_comparison_plan(
        "fund-compare-contract-001",
        ["KR0000000002", "KR0000000001"],
        ["risk_level", "three_month_return_pct", "aum"],
    )

    require_internal_evaluation_comparison(plan)
    with pytest.raises(PlanExecutionBlockedError, match="not enabled"):
        require_executable_comparison(plan)
    assert plan.intent.value == "compare"
    assert plan.limit == 2
    assert plan.ranking == []
    assert plan.intent_payload.comparison_fields == [
        "risk_level",
        "three_month_return_pct",
        "aum",
    ]
    assert "trading_currency" in plan.projection

    payload = plan.model_dump(mode="json")
    payload["constraints"][1]["value"] = [
        "KR0000000001",
        "KR0000000002",
        "KR0000000003",
    ]
    invalid = QueryPlan.model_validate(payload)
    with pytest.raises(PlanExecutionBlockedError, match="exactly two unique"):
        require_internal_evaluation_comparison(invalid)


def test_public_fund_compare_engine_preserves_requested_order_and_deltas(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    plan = fund_comparison_plan(
        "fund-compare-engine-001",
        ["KR0000000002", "KR0000000001"],
        ["risk_level", "three_month_return_pct", "aum"],
    )
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)
    comparison = build_fund_comparison(plan, verified, evidence)

    assert [record.product_id for record in executed.records] == [
        "KR0000000001",
        "KR0000000002",
    ]
    assert comparison.found_product_ids == (
        "KR0000000002",
        "KR0000000001",
    )
    assert [record.product_id for record in comparison.verified.records] == [
        "KR0000000002",
        "KR0000000001",
    ]
    by_field = {field.canonical_field: field for field in comparison.fields}
    assert by_field["risk_level"].status == "value_only"
    assert by_field["three_month_return_pct"].status == "numeric_delta"
    assert by_field["three_month_return_pct"].delta == Decimal("-1.50")
    assert by_field["aum"].status == "numeric_delta"
    assert by_field["aum"].delta == Decimal("-100000.0000")

    answer, warnings = render_verified_comparison(
        plan,
        comparison.verified,
        list(comparison.products),
    )
    assert "차이(두 번째-첫 번째) -1.5%p" in answer
    assert "차이(두 번째-첫 번째) -100,000.00 KRW" in answer
    assert "PRFD01N001 원본 행 2, fd_mm3_ern_r, 기준일 2026-07-11" in answer
    assert "historical_return_not_forecast" not in warnings
    assert any("과거 성과" in warning for warning in warnings)


def test_public_fund_compare_handles_currency_mismatch_and_missing_values(
    tmp_path: Path,
) -> None:
    cross_path, _, _ = write_comparison_fund_database(
        tmp_path,
        second_currency="USD",
    )
    plan = fund_comparison_plan(
        "fund-compare-currency-001",
        ["KR0000000001", "KR0000000002"],
        ["aum", "three_month_return_pct"],
    )
    executed = SQLiteOracle(cross_path).execute(_validated(plan, cross_path))
    with connect_read_only(cross_path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)
    comparison = build_fund_comparison(plan, verified, evidence)
    by_field = {field.canonical_field: field for field in comparison.fields}

    assert by_field["aum"].status == "currency_mismatch"
    assert by_field["aum"].delta is None
    assert by_field["three_month_return_pct"].delta == Decimal("1.50")
    answer, _ = render_verified_comparison(plan, comparison.verified, list(comparison.products))
    assert "거래 통화가 달라 금액 차이를 계산하지 않음" in answer

    missing_path, _, _ = write_comparison_fund_database(
        tmp_path,
        second_three_month_return=None,
    )
    executed = SQLiteOracle(missing_path).execute(_validated(plan, missing_path))
    with connect_read_only(missing_path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)
    comparison = build_fund_comparison(plan, verified, evidence)
    by_field = {field.canonical_field: field for field in comparison.fields}
    assert by_field["three_month_return_pct"].status == "unavailable"
    assert by_field["three_month_return_pct"].delta is None


def test_public_fund_compare_blocks_aum_delta_when_both_currencies_are_missing(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    plan = fund_comparison_plan(
        "fund-compare-currency-missing-001",
        ["KR0000000001", "KR0000000002"],
        ["aum"],
    )
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    verified_without_currencies = verified.model_copy(
        update={
            "records": [
                record.model_copy(update={"trading_currency": None}) for record in verified.records
            ]
        }
    )
    evidence = build_product_evidence(plan, verified_without_currencies)
    comparison = build_fund_comparison(
        plan,
        verified_without_currencies,
        evidence,
    )
    field = comparison.fields[0]

    assert field.status == "unavailable"
    assert field.reason == "trading_currency_unavailable"
    assert field.delta is None
    answer, _ = render_verified_comparison(
        plan,
        comparison.verified,
        list(comparison.products),
    )
    assert "거래 통화를 확인할 수 없어 금액 차이를 계산하지 않음" in answer


def test_public_fund_compare_rejects_duplicate_product_evidence(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    plan = fund_comparison_plan(
        "fund-compare-duplicate-evidence-001",
        ["KR0000000001", "KR0000000002"],
        ["aum"],
    )
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)

    with pytest.raises(ValueError, match="duplicate product IDs"):
        build_fund_comparison(plan, verified, [*evidence, evidence[0]])


def test_public_fund_compare_grounded_answer_and_missing_product_fallback(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    plan = fund_comparison_plan(
        "fund-compare-answer-001",
        ["KR0000000002", "KR0000000001"],
        ["risk_level", "three_month_return_pct", "aum"],
    )
    executed = SQLiteOracle(path).execute(_validated(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    evidence = build_product_evidence(plan, verified)
    comparison = build_fund_comparison(plan, verified, evidence)
    composition = compose_grounded_answer(
        question="두 공모펀드의 위험등급, 3개월 수익률과 AUM을 비교해줘",
        plan=plan,
        verified=comparison.verified,
        products=list(comparison.products),
        provider=ExpectedGroundedAnswerProvider(),
    )

    assert composition.mode == "llm_grounded"
    assert composition.verification.passed
    assert "차이(두 번째-첫 번째) -1.5%p" in composition.answer
    assert "fd_mm3_ern_r, 기준일 2026-07-11" in composition.answer

    missing_plan = fund_comparison_plan(
        "fund-compare-missing-001",
        ["KR0000000999", "KR0000000001"],
        ["three_month_return_pct"],
    )
    executed = SQLiteOracle(path).execute(_validated(missing_plan, path))
    verified = ResultVerifier().verify(missing_plan, executed, universe)
    evidence = build_product_evidence(missing_plan, verified)
    comparison = build_fund_comparison(missing_plan, verified, evidence)
    composition = compose_grounded_answer(
        question="존재하지 않는 상품과 공모펀드를 비교해줘",
        plan=missing_plan,
        verified=comparison.verified,
        products=list(comparison.products),
        provider=ExpectedGroundedAnswerProvider(),
    )

    assert composition.mode == "deterministic"
    assert composition.draft is None
    assert composition.verification.checks["comparison_incomplete_deterministic"]
    assert "KR0000000999 — 제공 데이터에서 확인되지 않음" in composition.answer


def test_fund_compare_core_suite_is_versioned_and_split() -> None:
    loaded = load_fund_comparison_suite()

    assert loaded.suite.suite_id == "fund-compare-core-20"
    assert len(loaded.suite.cases) == 20
    assert sum(case.split == EvaluationSplit.DEVELOPMENT for case in loaded.suite.cases) == 16
    assert sum(case.split == EvaluationSplit.HOLDOUT for case in loaded.suite.cases) == 4
    assert len(loaded.suite_sha256) == 64
    assert (
        build_comparison_parser().parse_args(["--provider", "expected", "--split", "holdout"]).split
        == "holdout"
    )


def test_fund_compare_evaluation_preserves_integer_trailing_zero(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = FundComparisonCase(
        id="fund-compare-evaluation-001",
        split=EvaluationSplit.DEVELOPMENT,
        category="synthetic",
        question="두 공모펀드의 3개월 수익률과 AUM을 비교해줘",
        product_ids=["KR0000000002", "KR0000000001"],
        comparison_fields=["three_month_return_pct", "aum"],
        expected=FundComparisonExpectation(
            found_product_ids=["KR0000000002", "KR0000000001"],
            missing_product_ids=[],
            field_statuses={
                "three_month_return_pct": "numeric_delta",
                "aum": "numeric_delta",
            },
            deltas={
                "three_month_return_pct": "-1.5",
                "aum": "-100000",
            },
            answer_mode="llm_grounded",
        ),
    )
    provider = ExpectedGroundedAnswerProvider()
    runner = FundComparisonEvaluationRunner(path, provider)

    result = runner.run_case(case)
    loaded = load_fund_comparison_suite()
    report = build_fund_comparison_report(
        suite=loaded,
        provider=provider,
        split="development",
        workers=1,
        results=[result],
    )

    assert result.passed
    assert result.deltas["aum"] == "-100000"
    assert report.summary.strict_accuracy == 1.0
    assert report.summary.numeric_delta_accuracy == 1.0


def test_fund_compare_evaluation_records_verifier_fallback(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = FundComparisonCase(
        id="fund-compare-fallback-001",
        split=EvaluationSplit.DEVELOPMENT,
        category="fallback",
        question="두 공모펀드의 3개월 수익률을 비교해줘",
        product_ids=["KR0000000001", "KR0000000002"],
        comparison_fields=["three_month_return_pct"],
        expected=FundComparisonExpectation(
            found_product_ids=["KR0000000001", "KR0000000002"],
            missing_product_ids=[],
            field_statuses={"three_month_return_pct": "numeric_delta"},
            deltas={"three_month_return_pct": "1.5"},
            answer_mode="llm_grounded",
        ),
    )

    class UnsafeComparisonProvider:
        provider_name = "local_test"
        model_name = "unsafe-comparison-model"

        def generate_grounded_answer(self, context):
            draft = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
            first = draft.products[0].model_copy(
                update={"explanation": ("KR0000000001은 수익성이 더 좋아 매수하기에 유리합니다.")}
            )
            return draft.model_copy(update={"products": [first, *draft.products[1:]]})

    provider = UnsafeComparisonProvider()
    result = FundComparisonEvaluationRunner(path, provider).run_case(case)
    report = build_fund_comparison_report(
        suite=load_fund_comparison_suite(),
        provider=provider,
        split="development",
        workers=1,
        results=[result],
    )

    assert result.mode == "deterministic_fallback"
    assert result.checks["safe_answer"]
    assert not result.checks["verifier_passed"]
    assert "매수하기에 유리" not in result.answer
    assert report.summary.fallback_cases == 1
    assert report.summary.fallback_rate == 1.0


def _synthetic_comparison_parser_case(
    *,
    blocked: bool = False,
) -> FundComparisonParserCase:
    first = "테스트 공모펀드 KR0000000002"
    second = "테스트 공모펀드 KR0000000001"
    if blocked:
        question = f'"{first}"와 "{second}"의 총보수를 비교해줘'
        fields: list[str] = []
    else:
        question = f'"{first}"와 "{second}"의 3개월 수익률과 AUM을 비교해줘'
        fields = ["three_month_return_pct", "aum"]
    return FundComparisonParserCase(
        id="fund-compare-e2e-blocked" if blocked else "fund-compare-e2e-execute",
        split=EvaluationSplit.DEVELOPMENT,
        category="synthetic_blocked" if blocked else "synthetic_execute",
        question=question,
        expected=FundComparisonParserExpectation(
            draft=FundComparisonDraft(
                target_mentions=[first, second],
                comparison_fields=fields,
            ),
            resolution_statuses=["resolved", "resolved"],
            resolved_product_ids=["KR0000000002", "KR0000000001"],
            comparison_fields=fields,
            disposition=(ExpectedDisposition.BLOCK if blocked else ExpectedDisposition.EXECUTE),
            blocker=ExpectedBlocker.UNSUPPORTED if blocked else None,
        ),
    )


def _synthetic_e2e_expectations(
    case: FundComparisonParserCase,
    database_path: Path,
) -> dict[str, FundComparisonE2EExpectation]:
    if case.expected.disposition is ExpectedDisposition.BLOCK:
        return {}
    with connect_read_only(database_path) as connection:
        universe = load_all_records(connection)
    compiled = compile_fund_comparison_query_plan(
        question=case.question,
        question_id=case.id,
        draft=case.expected.draft,
        resolver=FundProductResolver(universe),
    )
    oracle = SQLiteOracle(database_path)
    verified = ResultVerifier().verify(
        compiled.plan,
        oracle.execute(_validated(compiled.plan, database_path)),
        universe,
    )
    comparison = build_fund_comparison(
        compiled.plan,
        verified,
        build_product_evidence(compiled.plan, verified),
    )
    return {
        case.id: FundComparisonE2EExpectation(
            case_id=case.id,
            field_statuses={
                "three_month_return_pct": "numeric_delta",
                "aum": "numeric_delta",
            },
            deltas={
                "three_month_return_pct": "-1.5",
                "aum": "-100000",
            },
            cell_value_fingerprints=comparison_cell_value_fingerprints(comparison),
            evidence_fingerprints=comparison_evidence_fingerprints(comparison),
        )
    }


def test_fund_compare_e2e_connects_natural_question_to_verified_answer(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    draft_provider = ExpectedFundComparisonDraftProvider([case])
    answer_provider = ExpectedGroundedAnswerProvider()
    runner = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        answer_provider,
        _synthetic_e2e_expectations(case, path),
    )

    result = runner.run_case(case)
    report = build_fund_comparison_e2e_report(
        loaded=load_fund_comparison_e2e_suite(),
        draft_provider=draft_provider,
        answer_provider=answer_provider,
        split="development",
        workers=1,
        results=[result],
    )

    assert result.passed
    assert result.mode == "llm_grounded"
    assert result.found_product_ids == ["KR0000000002", "KR0000000001"]
    assert result.checks["plan_exact"]
    assert result.checks["oracle_exact"]
    assert result.checks["field_statuses_exact"]
    assert result.checks["numeric_deltas_exact"]
    assert result.checks["answer_verifier_passed"]
    assert result.checks["evidence_citations_present"]
    assert report.summary.strict_accuracy == 1.0
    assert report.summary.answer_generation_attempts == 1
    assert report.summary.fallback_rate == 0.0


def test_fund_compare_e2e_blocks_before_oracle_and_answer_provider(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case(blocked=True)
    draft_provider = ExpectedFundComparisonDraftProvider([case])

    class FailIfCalledAnswerProvider:
        provider_name = "expected"
        model_name = None

        def generate_grounded_answer(self, context):
            raise AssertionError(f"answer provider must not be called: {context.question}")

    result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        FailIfCalledAnswerProvider(),
        _synthetic_e2e_expectations(case, path),
    ).run_case(case)

    assert result.passed
    assert result.mode == "blocked"
    assert result.checks["execution_blocked"]
    assert result.checks["answer_generation_suppressed"]
    assert "공모펀드 비교를 실행하지 않았습니다" in result.answer
    assert result.answer_draft is None


def test_fund_compare_e2e_records_verified_answer_fallback(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    draft_provider = ExpectedFundComparisonDraftProvider([case])

    class UnsafeAnswerProvider:
        provider_name = "expected"
        model_name = None

        def generate_grounded_answer(self, context):
            draft = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
            first = draft.products[0].model_copy(
                update={"explanation": "이 상품은 수익성이 좋아 매수하기에 유리합니다."}
            )
            return draft.model_copy(update={"products": [first, *draft.products[1:]]})

    answer_provider = UnsafeAnswerProvider()
    result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        answer_provider,
        _synthetic_e2e_expectations(case, path),
    ).run_case(case)
    report = build_fund_comparison_e2e_report(
        loaded=load_fund_comparison_e2e_suite(),
        draft_provider=draft_provider,
        answer_provider=answer_provider,
        split="development",
        workers=1,
        results=[result],
    )

    assert not result.passed
    assert result.mode == "deterministic_fallback"
    assert not result.checks["answer_verifier_passed"]
    assert "매수하기에 유리" not in result.answer
    assert report.summary.fallback_cases == 1
    assert report.summary.fallback_rate == 1.0


def test_fund_compare_e2e_rejects_frozen_delta_regression(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    draft_provider = ExpectedFundComparisonDraftProvider([case])
    expectations = _synthetic_e2e_expectations(case, path)
    expectations[case.id] = expectations[case.id].model_copy(
        update={
            "deltas": {
                "three_month_return_pct": "999",
                "aum": "-100000",
            }
        }
    )

    result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        ExpectedGroundedAnswerProvider(),
        expectations,
    ).run_case(case)

    assert not result.passed
    assert result.checks["field_statuses_exact"]
    assert not result.checks["numeric_deltas_exact"]


def test_fund_compare_e2e_rejects_frozen_cell_and_provenance_regressions(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    draft_provider = ExpectedFundComparisonDraftProvider([case])
    expectations = _synthetic_e2e_expectations(case, path)
    expectation = expectations[case.id]

    wrong_cells = {
        field_name: list(values)
        for field_name, values in expectation.cell_value_fingerprints.items()
    }
    wrong_cells["aum"][0] = "0" * 64
    cell_result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        ExpectedGroundedAnswerProvider(),
        {case.id: expectation.model_copy(update={"cell_value_fingerprints": wrong_cells})},
    ).run_case(case)

    wrong_evidence = {
        field_name: list(values) for field_name, values in expectation.evidence_fingerprints.items()
    }
    wrong_evidence["aum"][0] = "f" * 64
    evidence_result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        ExpectedGroundedAnswerProvider(),
        {case.id: expectation.model_copy(update={"evidence_fingerprints": wrong_evidence})},
    ).run_case(case)

    assert not cell_result.passed
    assert not cell_result.checks["cell_values_exact"]
    assert cell_result.checks["evidence_provenance_exact"]
    assert not evidence_result.passed
    assert evidence_result.checks["cell_values_exact"]
    assert not evidence_result.checks["evidence_provenance_exact"]


def test_fund_compare_cell_fingerprint_uses_actual_comparison_value(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    compiled = compile_fund_comparison_query_plan(
        question=case.question,
        question_id=case.id,
        draft=case.expected.draft,
        resolver=FundProductResolver(universe),
    )
    oracle = SQLiteOracle(path)
    verified = ResultVerifier().verify(
        compiled.plan,
        oracle.execute(_validated(compiled.plan, path)),
        universe,
    )
    comparison = build_fund_comparison(
        compiled.plan,
        verified,
        build_product_evidence(compiled.plan, verified),
    )
    original_field = comparison.fields[0]
    mutated_cell = replace(original_field.cells[0], value=Decimal("999"))
    mutated_field = replace(
        original_field,
        cells=(mutated_cell, original_field.cells[1]),
    )
    mutated_comparison = replace(
        comparison,
        fields=(mutated_field, *comparison.fields[1:]),
    )

    assert comparison_cell_value_fingerprints(
        mutated_comparison
    ) != comparison_cell_value_fingerprints(comparison)
    assert comparison_evidence_fingerprints(mutated_comparison) == comparison_evidence_fingerprints(
        comparison
    )


def test_fund_compare_plan_contract_does_not_depend_on_actual_grounding(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    compiled = compile_fund_comparison_query_plan(
        question=case.question.replace(
            '"테스트 공모펀드 KR0000000002"',
            '"테스트 공모펀드 KR0000000002가"',
        ),
        question_id=case.id,
        draft=case.expected.draft,
        resolver=FundProductResolver(universe),
    )

    assert not all(compiled.mentions_grounded)
    assert not fund_comparison_plan_contract_exact(case, compiled)


def test_fund_compare_e2e_parser_error_counts_against_every_parser_metric(
    tmp_path: Path,
) -> None:
    path, _, _ = write_comparison_fund_database(tmp_path)
    case = _synthetic_comparison_parser_case()

    class FailingDraftProvider:
        provider_name = "expected"
        model_name = None

        def generate_comparison_draft(self, question: str, question_id: str):
            raise RuntimeError(f"synthetic parser failure: {question_id}: {question}")

    draft_provider = FailingDraftProvider()
    answer_provider = ExpectedGroundedAnswerProvider()
    result = FundComparisonE2EEvaluationRunner(
        path,
        draft_provider,
        answer_provider,
        _synthetic_e2e_expectations(case, path),
    ).run_case(case)
    report = build_fund_comparison_e2e_report(
        loaded=load_fund_comparison_e2e_suite(),
        draft_provider=draft_provider,
        answer_provider=answer_provider,
        split="development",
        workers=1,
        results=[result],
    )

    assert result.mode == "error"
    assert report.summary.strict_accuracy == 0.0
    assert report.summary.draft_target_exact_rate == 0.0
    assert report.summary.draft_field_exact_rate == 0.0
    assert report.summary.mention_grounding_rate == 0.0
    assert report.summary.resolution_exact_rate == 0.0
    assert report.summary.plan_exact_rate == 0.0
    assert report.summary.oracle_exact_rate == 0.0


def test_fund_compare_e2e_cli_reuses_frozen_parser_suite() -> None:
    arguments = build_comparison_e2e_parser().parse_args(
        ["--provider", "expected", "--split", "holdout", "--workers", "2"]
    )

    assert arguments.provider == "expected"
    assert arguments.split == "holdout"
    assert arguments.workers == 2
    loaded = load_fund_comparison_e2e_suite()
    assert loaded.suite.suite_id == "fund-compare-e2e-core-24"
    assert loaded.parser_suite.suite.suite_id == "fund-compare-parser-core-24"


def test_public_fund_internal_evaluation_rejects_unapproved_provider(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)

    class NonExpectedProvider:
        provider_name = "mock"

        def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
            return fund_vertical_slice_plan(question_id)

    with pytest.raises(ValueError, match="restricted to expected or local_test"):
        EvaluationRunner(
            path,
            NonExpectedProvider(),
            allow_internal_disabled_dataset=True,
        )


def test_public_fund_internal_evaluation_accepts_local_test_provider(
    tmp_path: Path,
) -> None:
    path, _, _ = write_sample_fund_database(tmp_path)

    class InternalLocalProvider:
        provider_name = "local_test"

        def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
            return fund_vertical_slice_plan(question_id)

    runner = EvaluationRunner(
        path,
        InternalLocalProvider(),
        allow_internal_disabled_dataset=True,
    )

    assert runner.product_family == "fund"


def test_public_fund_one_year_return_can_drive_execution() -> None:
    payload = fund_vertical_slice_plan("fund-long-return-001").model_dump(mode="json")
    payload["ranking"] = [
        {
            "field": "one_year_return_pct",
            "direction": "desc",
            "nulls": "last",
        }
    ]

    plan = QueryPlan.model_validate(payload)

    assert plan.ranking[0].field == "one_year_return_pct"


def test_public_fund_other_long_returns_cannot_drive_execution() -> None:
    payload = fund_vertical_slice_plan("fund-long-return-002").model_dump(mode="json")
    payload["ranking"] = [
        {
            "field": "two_year_return_pct",
            "direction": "desc",
            "nulls": "last",
        }
    ]

    with pytest.raises(ValidationError, match="two_year_return_pct is not sortable"):
        QueryPlan.model_validate(payload)


def test_public_fund_unknown_sentinels_cannot_match_oracle_filters(
    tmp_path: Path,
) -> None:
    product = fund_product(
        "KR0000000003",
        public_offering="공모",
        aum="0",
        management_attribute="06",
    )
    result = normalize_public_fund_rows(
        [(2, 25, {**product, "prfd_attr_cd": "A01"})],
        source_snapshot_date=date(2026, 7, 11),
    )
    path = tmp_path / "fund-unknown.sqlite3"
    manifest = DatabaseManifest(
        schema_version="1.1",
        dataset="fund",
        registry_schema_version="1.3",
        source_file_name="synthetic_public_fund_unknown.xlsx",
        source_file_sha256="e" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
        logical_product_rows=1,
        attribute_rows=1,
        scope_excluded_rows=0,
    )
    write_public_fund_database(path, result, manifest)

    base = fund_vertical_slice_plan("fund-sentinel-001").model_dump(mode="json")
    public_scope = base["constraints"][0]
    cases = [
        {
            "field": "aum",
            "operator": "eq",
            "value": 0,
            "unit": "source_currency_amount",
            "strength": "locked",
        },
        {
            "field": "fund_management_attribute",
            "operator": "eq",
            "value": "주식형",
            "unit": "code",
            "strength": "locked",
        },
    ]
    for condition in cases:
        constraints = [public_scope, condition]
        if condition["field"] == "aum":
            constraints.append(
                {
                    "field": "trading_currency",
                    "operator": "eq",
                    "value": "KRW",
                    "unit": "code",
                    "strength": "locked",
                }
            )
        payload = {**base, "constraints": constraints}
        plan = QueryPlan.model_validate(payload)
        assert SQLiteOracle(path).execute(_validated(plan, path)).candidate_count == 0
