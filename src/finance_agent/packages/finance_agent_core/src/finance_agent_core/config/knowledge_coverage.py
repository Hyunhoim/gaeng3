from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
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
        elif self.status == "approved":
            if not self.corpus_sha256 or not self.index_sha256:
                raise ValueError("approved knowledge requires corpus and index SHA-256")
            if "external_approved" not in self.allowed_source_kinds:
                raise ValueError("approved cached knowledge requires approved external sources")
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

    def require_cache_only_binding(
        self,
        *,
        capabilities: Iterable[KnowledgeCapability],
        corpus_manifest_sha256: str,
        document_dense_artifact_sha256: str,
    ) -> None:
        """Authorize one immutable cache without borrowing live-search authority.

        For ``status=approved`` entries, ``corpus_sha256`` is the canonical
        approved-corpus manifest hash and ``index_sha256`` is the canonical
        approved Document Dense artifact hash.  The activation receipt binds
        that Dense artifact to its BM25 payload and calibration policy; callers
        must verify the receipt independently before invoking this method.

        Only capabilities represented by the activation receipt may be
        executable.  This prevents a live-only or unrelated approved entry
        from granting evaluation cache authority.
        """

        activated = frozenset(capabilities)
        if not activated:
            raise ValueError("cache-only coverage requires at least one activated capability")
        if not activated.issubset(set(KnowledgeCapability)):
            raise ValueError("cache-only coverage contains an unknown capability")
        for entry in self.capabilities:
            if entry.capability in activated:
                if (
                    entry.status != "approved"
                    or not entry.runtime_execution_enabled
                    or "external_approved" not in entry.allowed_source_kinds
                    or entry.corpus_sha256 != corpus_manifest_sha256
                    or entry.index_sha256 != document_dense_artifact_sha256
                ):
                    raise ValueError(
                        "cache-only capability is not bound to the approved corpus and index"
                    )
            elif entry.runtime_execution_enabled:
                raise ValueError(
                    "cache-only coverage enables a capability absent from the activation receipt"
                )

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
