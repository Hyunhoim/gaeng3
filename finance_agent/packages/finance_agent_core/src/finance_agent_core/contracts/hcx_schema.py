from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from finance_agent_core.config import load_field_registry

HCX_SCHEMA_KEYWORDS = {
    "type",
    "description",
    "properties",
    "required",
    "enum",
    "anyOf",
    "items",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "format",
}
HCX_TYPES = {"string", "number", "boolean", "integer", "object", "array"}


def load_hcx_queryplan_schema() -> dict[str, Any]:
    resource = files("finance_agent_core.contracts").joinpath("queryplan.hcx.schema.json")
    schema: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    registry = load_field_registry()
    properties = schema["properties"]
    properties["product_families"]["items"]["enum"] = registry.executable_dataset_names()
    properties["constraints"]["items"]["properties"]["field"]["enum"] = registry.queryable_fields(
        executable_only=True
    )
    properties["ranking"]["items"]["properties"]["field"]["enum"] = registry.sortable_fields(
        executable_only=True
    )
    selectable = registry.selectable_fields(executable_only=True)
    properties["projection"]["items"]["enum"] = selectable
    payload = properties["intent_payload"]["properties"]
    payload["comparison_fields"]["items"]["enum"] = selectable
    payload["group_by"]["items"]["enum"] = selectable
    payload["aggregations"]["items"]["properties"]["field"]["enum"] = sorted(
        set(registry.aggregatable_fields(executable_only=True)) | {"product_id"}
    )
    return schema


def validate_hcx_schema(schema: dict[str, Any]) -> None:
    """Fail if the schema uses keywords outside the documented HCX subset."""

    def visit(node: dict[str, Any], path: str) -> None:
        unknown = set(node) - HCX_SCHEMA_KEYWORDS
        if unknown:
            raise ValueError(f"{path} uses unsupported keywords: {sorted(unknown)}")

        node_type = node.get("type")
        if node_type is not None and node_type not in HCX_TYPES:
            raise ValueError(f"{path} uses unsupported type: {node_type}")

        properties = node.get("properties")
        if properties is not None:
            if node_type != "object" or not isinstance(properties, dict):
                raise ValueError(f"{path}.properties requires an object schema")
            required = node.get("required")
            if required != list(properties):
                raise ValueError(f"{path} must require every property in declaration order")
            for name, child in properties.items():
                if not isinstance(child, dict):
                    raise ValueError(f"{path}.properties.{name} is not a schema")
                visit(child, f"{path}.properties.{name}")

        items = node.get("items")
        if items is not None:
            if node_type != "array" or not isinstance(items, dict):
                raise ValueError(f"{path}.items requires an array schema")
            visit(items, f"{path}.items")

        alternatives = node.get("anyOf")
        if alternatives is not None:
            if not isinstance(alternatives, list) or not alternatives:
                raise ValueError(f"{path}.anyOf must be a non-empty list")
            for index, child in enumerate(alternatives):
                if not isinstance(child, dict):
                    raise ValueError(f"{path}.anyOf[{index}] is not a schema")
                visit(child, f"{path}.anyOf[{index}]")

    visit(schema, "$")


def validate_hcx_schema_registry_alignment(schema: dict[str, Any]) -> None:
    registry = load_field_registry()
    properties = schema["properties"]
    constraint_field_enum = properties["constraints"]["items"]["properties"]["field"]["enum"]
    ranking_field_enum = properties["ranking"]["items"]["properties"]["field"]["enum"]
    projection_field_enum = properties["projection"]["items"]["enum"]
    payload_properties = properties["intent_payload"]["properties"]
    aggregation_field_enum = payload_properties["aggregations"]["items"]["properties"]["field"][
        "enum"
    ]

    expected = {
        "constraint": registry.queryable_fields(executable_only=True),
        "ranking": registry.sortable_fields(executable_only=True),
        "projection": registry.selectable_fields(executable_only=True),
        "aggregation": sorted(
            set(registry.aggregatable_fields(executable_only=True)) | {"product_id"}
        ),
    }
    actual = {
        "constraint": constraint_field_enum,
        "ranking": ranking_field_enum,
        "projection": projection_field_enum,
        "aggregation": aggregation_field_enum,
    }
    if actual != expected:
        raise ValueError(f"HCX schema field enums differ from registry: {actual!r}")
