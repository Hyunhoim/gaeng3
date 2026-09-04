import pytest

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry


def test_overseas_etp_dataset_contract_matches_audit() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["overseas_etp"]

    assert dataset.primary_key == ["pd_exg_mkt_cd", "pd_itm_no"]
    assert dataset.row_count == 6_037
    assert dataset.quarantined_rows == 0
    assert dataset.snapshot_date.isoformat() == "2026-08-24"


def test_risky_fields_are_encoded_fail_closed() -> None:
    registry = load_field_registry()
    fee = registry.fields["total_expense_ratio_pct"]
    one_day_return = registry.fields["one_day_return_pct"]
    isin = registry.fields["isin"]

    assert fee.quality is QualityStatus.PARTIAL
    assert fee.sentinel_values == {"0": "UNKNOWN"}
    assert registry.fields["aum"].as_of_basis is AsOfBasis.DYNAMIC
    assert one_day_return.quality is QualityStatus.PARTIAL
    assert one_day_return.queryable
    assert one_day_return.selectable
    assert one_day_return.sortable
    assert isin.quality is QualityStatus.PARTIAL
    assert isin.coverage_pct == pytest.approx(99.8012)


def test_registry_exposes_only_frozen_capabilities() -> None:
    registry = load_field_registry()

    assert list(registry.datasets) == ["overseas_etp", "domestic_etp", "bond", "fund"]
    assert registry.executable_dataset_names() == [
        "overseas_etp",
        "domestic_etp",
        "bond",
        "fund",
    ]
    overseas_return = registry.require_field("one_day_return_pct", ["overseas_etp"])
    domestic_return = registry.require_field("one_day_return_pct", ["domestic_etp"])
    assert overseas_return.selectable
    assert overseas_return.queryable
    assert domestic_return.selectable
    assert domestic_return.queryable
    assert "one_month_return_pct" in registry.aggregatable_fields()
    assert "daily_trading_value" in registry.sortable_fields()


def test_fund_grain_and_capabilities_match_product_grain_audit() -> None:
    registry = load_field_registry()
    dataset = registry.require_dataset("fund")

    assert dataset.primary_key == ["itm_no"]
    assert dataset.raw_primary_key == ["itm_no"]
    assert dataset.row_count == 23_676
    assert dataset.logical_row_count == 23_560
    assert dataset.quarantined_rows == 116
    assert dataset.execution_enabled

    public = registry.require_field("public_offering", ["fund"])
    aum = registry.require_field("aum", ["fund"])
    risk = registry.require_field("risk_level", ["fund"])
    one_week = registry.require_field("one_week_return_pct", ["fund"])
    one_year = registry.require_field("one_year_return_pct", ["fund"])
    fund_type = registry.require_field("fund_management_attribute", ["fund"])

    assert public.coverage_pct == pytest.approx(100.0)
    assert public.source.value_map == {"공모": True, "사모": False}
    assert aum.coverage_pct == pytest.approx(39.9533)
    assert aum.sentinel_values == {"0": "UNKNOWN"}
    assert risk.coverage_pct == pytest.approx(36.8803)
    assert not one_week.queryable
    assert not one_week.sortable
    assert not one_week.aggregatable
    assert one_year.queryable and one_year.sortable and one_year.aggregatable
    assert one_year.comparable
    assert one_year.comparison_mode == "numeric_delta"
    assert fund_type.sentinel_values == {"06": "UNKNOWN"}


def test_fund_contract_is_frozen_and_execution_is_enabled() -> None:
    registry = load_field_registry()

    assert registry.require_dataset("fund").source_id == "PRFD01N001"
    assert registry.require_executable_dataset("fund").execution_enabled


def test_domestic_etp_dataset_contract_matches_audit() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["domestic_etp"]

    assert dataset.primary_key == ["pd_itm_no"]
    assert dataset.row_count == 1_780
    assert dataset.quarantined_rows == 3
    assert dataset.snapshot_date.isoformat() == "2026-08-24"
    fee = registry.require_field("total_expense_ratio_pct", ["domestic_etp"])
    assert fee.coverage_pct == pytest.approx(12.191)
    assert fee.sentinel_values == {"0": "UNKNOWN"}


def test_bond_dataset_contract_matches_audit_and_fails_closed() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["bond"]

    assert dataset.primary_key == ["pd_no", "pd_exg_mkt", "info_base_dt", "info_seq"]
    assert dataset.row_count == 21_882
    assert dataset.quarantined_rows == 0
    assert dataset.snapshot_date.isoformat() == "2026-08-24"
    availability = registry.require_field("currently_buyable", ["bond"])
    rating = registry.require_field("credit_rating", ["bond"])
    remaining = registry.require_field("remaining_days", ["bond"])
    assert availability.coverage_pct == pytest.approx(100.0)
    assert availability.source.columns == []
    assert availability.source.constant_value == "true"
    assert rating.allowed_operators == ["eq", "in"]
    assert remaining.source.transform.value == "source_preferred_then_days_from_snapshot"
    assert remaining.source.columns == ["remaining_days", "mat_dt"]


def test_unknown_family_and_field_fail_closed() -> None:
    registry = load_field_registry()

    with pytest.raises(ValueError, match="no frozen field registry"):
        registry.require_dataset("imaginary_family")
    with pytest.raises(ValueError, match="unknown canonical field"):
        registry.require_field("imaginary_return", ["overseas_etp"])


def test_dataset_override_resolution_is_cached_without_changing_registry_dump() -> None:
    registry = load_field_registry()
    before = registry.model_dump(mode="json")

    first = registry.require_field("aum", ["domestic_etp"])
    second = registry.require_field("aum", ["domestic_etp"])

    assert first is second
    assert first.datasets == ["domestic_etp"]
    assert registry.model_dump(mode="json") == before
