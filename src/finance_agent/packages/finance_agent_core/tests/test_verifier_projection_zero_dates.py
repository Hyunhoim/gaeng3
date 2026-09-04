from finance_agent_core.config import ValueType
from finance_agent_core.execution.sql_schema import SQL_FIELDS_BY_FAMILY
from finance_agent_core.execution.verifier_projection import _canonical_value


def test_official_zero_date_is_unavailable_in_sql_and_verifier() -> None:
    listing_date = SQL_FIELDS_BY_FAMILY["overseas_etp"]["listing_date"]

    assert "00000000" in listing_date.column
    assert "0000-00-00" in listing_date.column
    assert (
        _canonical_value(
            field_name="listing_date",
            raw_value="0000-00-00",
            value_type=ValueType.DATE,
            scale=None,
        )
        is None
    )
