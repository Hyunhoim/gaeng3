from copy import deepcopy

import pytest

from finance_agent_core.contracts.hcx_schema import (
    HCX_SCHEMA_KEYWORDS,
    load_hcx_queryplan_schema,
    load_internal_evaluation_queryplan_schema,
    validate_hcx_payload,
    validate_hcx_schema,
    validate_hcx_schema_registry_alignment,
)


def test_packaged_hcx_schema_uses_documented_subset() -> None:
    schema = load_hcx_queryplan_schema()

    validate_hcx_schema(schema)
    validate_hcx_schema_registry_alignment(schema)
    assert schema["properties"]["product_families"]["items"]["enum"] == [
        "overseas_etp",
        "domestic_etp",
        "bond",
    ]


def test_internal_fund_schema_does_not_change_official_hcx_surface() -> None:
    internal = load_internal_evaluation_queryplan_schema("fund")
    official = load_hcx_queryplan_schema()

    validate_hcx_schema(internal)
    assert internal["properties"]["product_families"]["items"]["enum"] == ["fund"]
    constraint_fields = internal["properties"]["constraints"]["items"]["properties"]["field"][
        "enum"
    ]
    ranking_fields = internal["properties"]["ranking"]["items"]["properties"]["field"]["enum"]
    assert "public_offering" in constraint_fields
    assert "three_month_return_pct" in ranking_fields
    assert "one_year_return_pct" not in ranking_fields
    assert "fund" not in official["properties"]["product_families"]["items"]["enum"]


def test_server_only_keyword_is_rejected() -> None:
    schema = deepcopy(load_hcx_queryplan_schema())
    schema["additionalProperties"] = False

    with pytest.raises(ValueError, match="unsupported keywords"):
        validate_hcx_schema(schema)
    assert "additionalProperties" not in HCX_SCHEMA_KEYWORDS


def test_every_hcx_object_property_is_required() -> None:
    schema = deepcopy(load_hcx_queryplan_schema())
    schema["required"].remove("unsupported_conditions")

    with pytest.raises(ValueError, match="require every property"):
        validate_hcx_schema(schema)


def test_hcx_payload_validator_enforces_enum_shape_and_array_bounds() -> None:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["safe"]},
            "values": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 3},
                "minItems": 1,
                "maxItems": 2,
            },
        },
        "required": ["mode", "values"],
    }

    validate_hcx_payload(schema, {"mode": "safe", "values": [1, 3]})

    for unsafe in (
        {"mode": "model-chosen", "values": [1]},
        {"mode": "safe", "values": []},
        {"mode": "safe", "values": [1, 2, 3]},
        {"mode": "safe", "values": [0]},
        {"mode": "safe", "values": [True]},
        {"mode": "safe", "values": [1], "extra": "not-declared"},
    ):
        with pytest.raises(ValueError):
            validate_hcx_payload(schema, unsafe)


def test_hcx_payload_validator_requires_at_least_one_anyof_branch() -> None:
    schema = {
        "anyOf": [
            {"type": "number"},
            {"type": "integer"},
        ]
    }

    validate_hcx_payload(schema, 1)

    with pytest.raises(ValueError, match="at least one"):
        validate_hcx_payload(schema, False)
