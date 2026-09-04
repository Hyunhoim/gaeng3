from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from finance_agent_core.contracts.queryplan import QueryPlan


def canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_items(values: Sequence[object]) -> list[object]:
    return sorted(
        values,
        key=lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def query_plan_semantic_payload(plan: QueryPlan) -> dict[str, Any]:
    """Return executable meaning without request-specific or commutative ordering noise."""

    payload = plan.model_dump(mode="json")
    payload.pop("question_id", None)
    constraints = [
        constraint
        for constraint in payload.get("constraints", [])
        if not (
            constraint.get("field") == "product_type"
            and constraint.get("operator") == "in"
            and set(constraint.get("value", [])) == {"ETF", "ETN"}
        )
    ]
    for constraint in constraints:
        if constraint.get("operator") in {"in", "not_in"} and isinstance(
            constraint.get("value"),
            list,
        ):
            constraint["value"] = canonical_items(constraint["value"])
    payload["constraints"] = canonical_items(constraints)
    payload["projection"] = sorted(payload.get("projection", []))
    intent_payload = payload.get("intent_payload", {})
    for key in ("comparison_fields", "group_by", "aggregations", "explain_product_ids"):
        intent_payload[key] = canonical_items(intent_payload.get(key, []))
    payload["ambiguities"] = canonical_items(payload.get("ambiguities", []))
    payload["unsupported_conditions"] = canonical_items(payload.get("unsupported_conditions", []))
    return payload


def query_plan_semantic_sha256(plan: QueryPlan | None) -> str | None:
    if plan is None:
        return None
    return canonical_json_sha256(query_plan_semantic_payload(plan))
