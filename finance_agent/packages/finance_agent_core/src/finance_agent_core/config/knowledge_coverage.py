from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeCapability(StrEnum):
    FUND_STRUCTURE_STRATEGY_TREND = "fund_structure_strategy_trend"
    PORTFOLIO_HOLDINGS = "portfolio_holdings"
    CORPORATE_SUBSIDIARY = "corporate_subsidiary"
    SECTOR_THEME_HISTORY = "sector_theme_history"
    PRODUCT_RISK_FACTORS = "product_risk_factors"


class KnowledgeCoverageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: KnowledgeCapability
    status: Literal["not_configured", "approved", "approved_live_official"]
    runtime_execution_enabled: bool
    allowed_source_kinds: tuple[Literal["provided", "external_approved"], ...] = Field(min_length=1)
    corpus_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    index_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_trust_boundary(self) -> KnowledgeCoverageEntry:
        if self.status == "not_configured":
            if self.runtime_execution_enabled or self.corpus_sha256 or self.index_sha256:
                raise ValueError("unconfigured knowledge cannot carry runtime authority")
        elif self.status == "approved" and (not self.corpus_sha256 or not self.index_sha256):
            raise ValueError("approved knowledge requires corpus and index SHA-256")
        elif self.status == "approved_live_official":
            if not self.runtime_execution_enabled:
                raise ValueError("live official knowledge must be enabled explicitly")
            if self.corpus_sha256 is not None or self.index_sha256 is not None:
                raise ValueError("live-only knowledge cannot claim a sealed corpus or index")
            if "external_approved" not in self.allowed_source_kinds:
                raise ValueError("live official knowledge requires approved external sources")
        return self


class ApprovedKnowledgeCoverageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    manifest_id: str = Field(min_length=1, max_length=120)
    dataset_release_id: str = Field(min_length=1, max_length=160)
    capabilities: tuple[KnowledgeCoverageEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_unique_registry(self) -> ApprovedKnowledgeCoverageManifest:
        observed = [entry.capability for entry in self.capabilities]
        if len(observed) != len(set(observed)):
            raise ValueError("knowledge capabilities must be unique")
        if set(observed) != set(KnowledgeCapability):
            raise ValueError("knowledge coverage manifest must enumerate every capability")
        return self

    def require(self, capability: KnowledgeCapability) -> KnowledgeCoverageEntry:
        return next(entry for entry in self.capabilities if entry.capability is capability)

    @property
    def canonical_sha256(self) -> str:
        payload = (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def load_approved_knowledge_coverage_manifest() -> ApprovedKnowledgeCoverageManifest:
    resource = files("finance_agent_core.config").joinpath(
        "approved_knowledge_coverage_manifest.json"
    )
    return ApprovedKnowledgeCoverageManifest.model_validate(
        json.loads(resource.read_text(encoding="utf-8"))
    )


__all__ = [
    "ApprovedKnowledgeCoverageManifest",
    "KnowledgeCapability",
    "KnowledgeCoverageEntry",
    "load_approved_knowledge_coverage_manifest",
]
