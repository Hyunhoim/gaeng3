from copy import deepcopy

import pytest

from finance_agent_core.contracts.hcx_schema import (
    HCX_SCHEMA_KEYWORDS,
    load_hcx_queryplan_schema,
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
