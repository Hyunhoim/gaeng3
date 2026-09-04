from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.oracle import (
    ORACLE_AGGREGATABLE_FAMILIES,
    ORACLE_COMPARABLE_FAMILIES,
    ORACLE_SUPPORTED_INTENTS,
)


class CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityEntry(CapabilityModel):
    product_family: ProductFamily
    intent: InteractionIntent
    status: Literal["executable", "unsupported", "control"]
    query_plan_intent: Intent | None
    oracle_mode: Literal["search", "compare", "fund_compare", "aggregate", "none"]
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_execution_mapping(self) -> CapabilityEntry:
        if self.status == "executable":
            if self.query_plan_intent not in ORACLE_SUPPORTED_INTENTS:
                raise ValueError("executable capability is not supported by the SQLite Oracle")
            if self.oracle_mode == "none":
                raise ValueError("executable capability requires an Oracle mode")
            if self.query_plan_intent is Intent.COMPARE:
                if self.product_family not in ORACLE_COMPARABLE_FAMILIES:
                    raise ValueError("comparison capability exceeds the Oracle policy")
                if self.oracle_mode not in {"compare", "fund_compare"}:
                    raise ValueError("comparison requires compare Oracle mode")
            elif self.query_plan_intent is Intent.AGGREGATE:
                if self.product_family not in ORACLE_AGGREGATABLE_FAMILIES:
                    raise ValueError("aggregation capability exceeds the Oracle policy")
                if self.oracle_mode != "aggregate":
                    raise ValueError("aggregation requires aggregate Oracle mode")
            elif self.oracle_mode != "search":
                raise ValueError("search-lowered capability requires search Oracle mode")
        elif self.query_plan_intent is not None or self.oracle_mode != "none":
            raise ValueError("non-executable capability must not expose an Oracle mapping")
        return self


class CapabilityMatrix(CapabilityModel):
    schema_version: Literal["1.0"]
    matrix_version: str = Field(min_length=1, max_length=20)
    entries: list[CapabilityEntry]

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> CapabilityMatrix:
        keys = [(entry.product_family, entry.intent) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("capability entries must be unique")
        expected = {(family, intent) for family in ProductFamily for intent in InteractionIntent}
        if set(keys) != expected:
            missing = sorted(
                f"{family.value}/{intent.value}" for family, intent in expected - set(keys)
            )
            extra = sorted(
                f"{family.value}/{intent.value}" for family, intent in set(keys) - expected
            )
            raise ValueError(
                f"capability matrix coverage differs: missing={missing}, extra={extra}"
            )
        return self

    def require(
        self,
        product_family: ProductFamily,
        intent: InteractionIntent,
    ) -> CapabilityEntry:
        for entry in self.entries:
            if entry.product_family is product_family and entry.intent is intent:
                return entry
        raise KeyError(f"missing capability: {product_family.value}/{intent.value}")


@lru_cache(maxsize=1)
def load_capability_matrix() -> CapabilityMatrix:
    resource = files("finance_agent_core.config").joinpath("capability_matrix.json")
    return CapabilityMatrix.model_validate(json.loads(resource.read_text(encoding="utf-8")))
