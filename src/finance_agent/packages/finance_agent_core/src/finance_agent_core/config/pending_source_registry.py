from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    load_official_source_registry,
)


class PendingSourceRegistryError(RuntimeError):
    """Raised when the collection-only registry cannot bind to production policy."""


class PendingSourceRegistryExtension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    extension_id: str = Field(min_length=1, max_length=120)
    base_registry_id: str = Field(min_length=1, max_length=120)
    base_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[OfficialSourceEntry, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_entries(self) -> PendingSourceRegistryExtension:
        source_ids = [entry.source_id for entry in self.entries]
        if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
            raise ValueError("pending source extension entries must be unique and sorted")
        if any(entry.status != "approved_for_snapshot_intake" for entry in self.entries):
            raise ValueError("pending source extension cannot grant live evidence authority")
        return self


@lru_cache(maxsize=1)
def load_pending_intake_source_registry() -> OfficialSourceRegistry:
    base = load_official_source_registry()
    resource = files("finance_agent_core.config").joinpath("pending_source_registry_extension.json")
    extension = PendingSourceRegistryExtension.model_validate(
        json.loads(resource.read_text(encoding="utf-8"))
    )
    if (
        extension.base_registry_id != base.registry_id
        or extension.base_registry_sha256 != base.canonical_sha256
    ):
        raise PendingSourceRegistryError(
            "pending source extension is not bound to the current production registry"
        )
    return OfficialSourceRegistry(
        schema_version=base.schema_version,
        registry_id=extension.extension_id,
        evidence_cutoff=base.evidence_cutoff,
        entries=tuple(sorted((*base.entries, *extension.entries), key=lambda item: item.source_id)),
    )


__all__ = [
    "PendingSourceRegistryError",
    "PendingSourceRegistryExtension",
    "load_pending_intake_source_registry",
]
