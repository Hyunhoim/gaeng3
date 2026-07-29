from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent.providers import fund_vertical_slice_plan
from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.domain import DatabaseManifest, NormalizedPublicFundRecord
from finance_agent_core.evaluation.runner import EvaluationRunner
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
    render_verified_search,
    require_executable_search,
    require_internal_evaluation_search,
)
from finance_agent_core.execution.oracle import compile_search_sql
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


def fund_product(
    product_id: str,
    *,
    public_offering: str,
    aum: str,
    management_attribute: str | None = "주식형",
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
        "fd_yr1_ern_r": "10",
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
    assert public.field_quality["one_year_return_pct"] is QualityStatus.UNKNOWN

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

    executed = SQLiteOracle(path).execute(plan)
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


def test_public_fund_long_return_cannot_drive_execution() -> None:
    payload = fund_vertical_slice_plan("fund-long-return-001").model_dump(mode="json")
    payload["ranking"] = [
        {
            "field": "one_year_return_pct",
            "direction": "desc",
            "nulls": "last",
        }
    ]

    with pytest.raises(ValidationError, match="one_year_return_pct is not sortable"):
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
        payload = {**base, "constraints": [public_scope, condition]}
        plan = QueryPlan.model_validate(payload)
        assert SQLiteOracle(path).execute(plan).candidate_count == 0
