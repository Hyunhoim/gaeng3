from __future__ import annotations

import json
import math
from importlib.resources import files
from typing import Any, Literal

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


def _capable_fields(
    product_families: list[str],
    capability: Literal["queryable", "sortable", "selectable", "aggregatable"],
) -> list[str]:
    registry = load_field_registry()
    return sorted(
        name
        for name, definition in registry.fields.items()
        if any(
            dataset in definition.datasets and getattr(definition.resolve(dataset), capability)
            for dataset in product_families
        )
    )


def _load_queryplan_schema(product_families: list[str]) -> dict[str, Any]:
    resource = files("finance_agent_core.contracts").joinpath("queryplan.hcx.schema.json")
    schema: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    properties = schema["properties"]
    properties["product_families"]["items"]["enum"] = product_families
    properties["constraints"]["items"]["properties"]["field"]["enum"] = _capable_fields(
        product_families,
        "queryable",
    )
    properties["ranking"]["items"]["properties"]["field"]["enum"] = _capable_fields(
        product_families,
        "sortable",
    )
    selectable = _capable_fields(product_families, "selectable")
    properties["projection"]["items"]["enum"] = selectable
    payload = properties["intent_payload"]["properties"]
    payload["comparison_fields"]["items"]["enum"] = selectable
    payload["group_by"]["items"]["enum"] = selectable
    payload["aggregations"]["items"]["properties"]["field"]["enum"] = sorted(
        set(_capable_fields(product_families, "aggregatable")) | {"product_id"}
    )
    return schema


def load_hcx_queryplan_schema() -> dict[str, Any]:
    """Load the official schema with only execution-enabled product families."""

    return _load_queryplan_schema(load_field_registry().executable_dataset_names())


def load_internal_evaluation_queryplan_schema(
    product_family: Literal["fund"],
) -> dict[str, Any]:
    """Load a development-only schema without changing the official HCX surface."""

    if product_family != "fund":
        raise ValueError("the internal evaluation schema is restricted to fund")
    return _load_queryplan_schema([product_family])


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


def validate_hcx_payload(schema: dict[str, Any], payload: Any) -> None:
    """Validate one model response locally against the supported HCX schema subset.

    Structured Outputs narrows model output, but it is not a trust boundary.  The
    server repeats the relevant type, enum, range and object-shape checks before a
    response can enter the finance contracts.
    """

    validate_hcx_schema(schema)

    def is_number(value: Any) -> bool:
        has_numeric_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        return has_numeric_type and math.isfinite(value)

    def enum_contains(options: list[Any], value: Any) -> bool:
        for option in options:
            if isinstance(option, bool) != isinstance(value, bool):
                continue
            if option == value:
                return True
        return False

    def visit(node: dict[str, Any], value: Any, path: str) -> None:
        alternatives = node.get("anyOf")
        if alternatives is not None:
            matched = False
            for child in alternatives:
                try:
                    visit(child, value, path)
                except ValueError:
                    continue
                matched = True
                break
            if not matched:
                raise ValueError(f"{path} must match at least one anyOf alternative")

        node_type = node.get("type")
        type_matches = {
            "string": isinstance(value, str),
            "number": is_number(value),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }
        if node_type is not None and not type_matches[node_type]:
            raise ValueError(f"{path} does not match schema type {node_type}")

        options = node.get("enum")
        if options is not None and not enum_contains(options, value):
            raise ValueError(f"{path} is outside the allowed enum")

        if node_type in {"number", "integer"}:
            minimum = node.get("minimum")
            maximum = node.get("maximum")
            if minimum is not None and value < minimum:
                raise ValueError(f"{path} is below the allowed minimum")
            if maximum is not None and value > maximum:
                raise ValueError(f"{path} is above the allowed maximum")

        if node_type == "object":
            properties = node.get("properties", {})
            required = node.get("required", [])
            missing = [name for name in required if name not in value]
            if missing:
                raise ValueError(f"{path} is missing required properties")
            extra = set(value) - set(properties)
            if extra:
                raise ValueError(f"{path} contains undeclared properties")
            for name, child in properties.items():
                if name in value:
                    visit(child, value[name], f"{path}.{name}")

        if node_type == "array":
            minimum_items = node.get("minItems")
            maximum_items = node.get("maxItems")
            if minimum_items is not None and len(value) < minimum_items:
                raise ValueError(f"{path} has fewer than the allowed items")
            if maximum_items is not None and len(value) > maximum_items:
                raise ValueError(f"{path} has more than the allowed items")
            item_schema = node.get("items")
            if item_schema is not None:
                for index, item in enumerate(value):
                    visit(item_schema, item, f"{path}[{index}]")

        if node.get("format") is not None:
            # Current finance response schemas do not use format.  Refuse to
            # claim validation until a format receives an explicit local check.
            raise ValueError(f"{path} uses an unsupported local format validator")

    visit(schema, payload, "$")


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
