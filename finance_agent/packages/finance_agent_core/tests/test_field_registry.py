import pytest

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry


def test_overseas_etp_dataset_contract_matches_audit() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["overseas_etp"]

    assert dataset.primary_key == ["pd_exg_mkt_cd", "pd_itm_no"]
    assert dataset.row_count == 5_646
    assert dataset.quarantined_rows == 10
    assert dataset.snapshot_date.isoformat() == "2026-07-11"


def test_risky_fields_are_encoded_fail_closed() -> None:
    registry = load_field_registry()
    fee = registry.fields["total_expense_ratio_pct"]
    one_day_return = registry.fields["one_day_return_pct"]
    isin = registry.fields["isin"]

    assert fee.quality is QualityStatus.PARTIAL
    assert fee.sentinel_values == {"0": "UNKNOWN"}
    assert registry.fields["aum"].as_of_basis is AsOfBasis.DYNAMIC
    assert one_day_return.quality is QualityStatus.INVALID
    assert not one_day_return.queryable
    assert not one_day_return.selectable
    assert not one_day_return.sortable
    assert isin.quality is QualityStatus.PARTIAL
    assert isin.coverage_pct == pytest.approx(99.8406)


def test_registry_exposes_only_frozen_capabilities() -> None:
    registry = load_field_registry()

    assert list(registry.datasets) == ["overseas_etp", "domestic_etp", "bond", "fund"]
    assert registry.executable_dataset_names() == [
        "overseas_etp",
        "domestic_etp",
        "bond",
    ]
    overseas_return = registry.require_field("one_day_return_pct", ["overseas_etp"])
    domestic_return = registry.require_field("one_day_return_pct", ["domestic_etp"])
    assert not overseas_return.selectable
    assert domestic_return.selectable
    assert domestic_return.queryable
    assert "one_month_return_pct" in registry.aggregatable_fields()
    assert "daily_trading_value" in registry.sortable_fields()


def test_fund_grain_and_capabilities_match_product_grain_audit() -> None:
    registry = load_field_registry()
    dataset = registry.require_dataset("fund")

    assert dataset.primary_key == ["itm_no"]
    assert dataset.raw_primary_key == ["itm_no", "prfd_attr_cd"]
    assert dataset.row_count == 95_619
    assert dataset.logical_row_count == 11_138
    assert dataset.quarantined_rows == 1
    assert not dataset.execution_enabled

    public = registry.require_field("public_offering", ["fund"])
    aum = registry.require_field("aum", ["fund"])
    risk = registry.require_field("risk_level", ["fund"])
    one_week = registry.require_field("one_week_return_pct", ["fund"])
    one_year = registry.require_field("one_year_return_pct", ["fund"])
    fund_type = registry.require_field("fund_management_attribute", ["fund"])

    assert public.coverage_pct == pytest.approx(99.9282)
    assert public.source.value_map == {"공모": True, "사모": False}
    assert aum.coverage_pct == pytest.approx(83.4082)
    assert aum.sentinel_values == {"0": "UNKNOWN"}
    assert risk.coverage_pct == pytest.approx(76.8989)
    assert one_week.queryable and one_week.sortable and one_week.aggregatable
    assert one_year.queryable and one_year.sortable and one_year.aggregatable
    assert one_year.comparable
    assert one_year.comparison_mode == "numeric_delta"
    assert fund_type.sentinel_values == {"06": "UNKNOWN"}


def test_fund_contract_is_frozen_but_execution_fails_closed() -> None:
    registry = load_field_registry()

    assert registry.require_dataset("fund").source_id == "PRFD01N001"
    with pytest.raises(ValueError, match="execution is not enabled"):
        registry.require_executable_dataset("fund")


def test_domestic_etp_dataset_contract_matches_audit() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["domestic_etp"]

    assert dataset.primary_key == ["pd_itm_no"]
    assert dataset.row_count == 1_734
    assert dataset.quarantined_rows == 1
    assert dataset.snapshot_date.isoformat() == "2026-07-11"
    fee = registry.require_field("total_expense_ratio_pct", ["domestic_etp"])
    assert fee.coverage_pct == pytest.approx(12.5216)
    assert fee.sentinel_values == {"0": "UNKNOWN"}


def test_bond_dataset_contract_matches_audit_and_fails_closed() -> None:
    registry = load_field_registry()
    dataset = registry.datasets["bond"]

    assert dataset.primary_key == ["PD_NO"]
    assert dataset.row_count == 42_394
    assert dataset.quarantined_rows == 0
    assert dataset.snapshot_date.isoformat() == "2026-07-11"
    availability = registry.require_field("currently_buyable", ["bond"])
    rating = registry.require_field("credit_rating", ["bond"])
    remaining = registry.require_field("remaining_days", ["bond"])
    assert availability.coverage_pct == pytest.approx(2.0781)
    assert availability.source.columns == ["BUYABLE_QUANTITY", "MAT_DT"]
    assert rating.allowed_operators == ["eq", "in"]
    assert remaining.source.transform.value == "days_from_snapshot"


def test_unknown_family_and_field_fail_closed() -> None:
    registry = load_field_registry()

    with pytest.raises(ValueError, match="no frozen field registry"):
        registry.require_dataset("imaginary_family")
    with pytest.raises(ValueError, match="unknown canonical field"):
        registry.require_field("imaginary_return", ["overseas_etp"])
