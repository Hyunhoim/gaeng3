from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.queryplan import ProductFamily


class OfficialSourceRegistryError(RuntimeError):
    """Raised when a discovered URL cannot enter the external evidence pipeline."""


class SourceAuthority(StrEnum):
    REGULATOR = "regulator"
    ASSET_MANAGER = "asset_manager"
    PRODUCT_ISSUER = "product_issuer"
    CORPORATE_OFFICIAL = "corporate_official"


class SourceConnectorKind(StrEnum):
    HTML = "html"
    PDF = "pdf"
    HTML_OR_PDF = "html_or_pdf"
    HTML_OR_JSON = "html_or_json"
    HTML_PDF_OR_JSON = "html_pdf_or_json"


class SourceContentPlatform(StrEnum):
    DIRECT = "direct"
    CELERO_SHORT_LINK = "celero_short_link"


class SourceRegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _path_is_in_scope(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(f"{prefix}/")


class OfficialSourceEntry(SourceRegistryModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    publisher: str = Field(min_length=1, max_length=200)
    authority: SourceAuthority
    hostname: str = Field(pattern=r"^[a-z0-9.-]+$")
    path_prefixes: tuple[str, ...] = Field(min_length=1, max_length=20)
    connector_kind: SourceConnectorKind
    content_platform: SourceContentPlatform = SourceContentPlatform.DIRECT
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1)
    selection_aliases: tuple[str, ...] = Field(default=(), max_length=30)
    discovery_urls: tuple[str, ...] = Field(default=(), max_length=20)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    historical_link_capabilities: tuple[KnowledgeCapability, ...] = Field(
        default=(),
        max_length=5,
    )
    status: str = Field(pattern=r"^(?:approved_for_snapshot_intake|approved_for_live_evidence)$")
    publication_date_required: bool = True

    @field_validator("publisher")
    @classmethod
    def reject_blank_publisher(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publisher cannot be blank")
        return value

    @field_validator("hostname")
    @classmethod
    def require_canonical_hostname(cls, value: str) -> str:
        if value != value.casefold() or value.startswith(".") or value.endswith("."):
            raise ValueError("hostname must be a canonical lower-case DNS name")
        return value

    @field_validator("path_prefixes")
    @classmethod
    def validate_path_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("path prefixes must be unique and sorted")
        if any(not item.startswith("/") or ".." in item or "\\" in item for item in values):
            raise ValueError("path prefixes must be canonical absolute URL paths")
        return values

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(
        cls,
        values: tuple[KnowledgeCapability, ...],
    ) -> tuple[KnowledgeCapability, ...]:
        serialized = [item.value for item in values]
        if serialized != sorted(serialized) or len(serialized) != len(set(serialized)):
            raise ValueError("source capabilities must be unique and sorted")
        return values

    @field_validator("historical_link_capabilities")
    @classmethod
    def validate_historical_link_capabilities(
        cls,
        values: tuple[KnowledgeCapability, ...],
    ) -> tuple[KnowledgeCapability, ...]:
        serialized = [item.value for item in values]
        if serialized != sorted(serialized) or len(serialized) != len(set(serialized)):
            raise ValueError("historical link capabilities must be unique and sorted")
        return values

    @model_validator(mode="after")
    def validate_historical_link_scope(self) -> OfficialSourceEntry:
        if not set(self.historical_link_capabilities).issubset(self.capabilities):
            raise ValueError("historical link capabilities must be authorized capabilities")
        return self

    @field_validator("selection_aliases")
    @classmethod
    def validate_selection_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [item.strip() for item in values]
        if any(not item or len(item) > 100 for item in normalized):
            raise ValueError("source selection aliases must be non-blank and bounded")
        if normalized != sorted(normalized, key=str.casefold) or len(normalized) != len(
            {item.casefold() for item in normalized}
        ):
            raise ValueError("source selection aliases must be unique and sorted")
        return tuple(normalized)

    @field_validator("product_families")
    @classmethod
    def validate_product_families(
        cls,
        values: tuple[ProductFamily, ...],
    ) -> tuple[ProductFamily, ...]:
        serialized = [item.value for item in values]
        if serialized != sorted(serialized) or len(serialized) != len(set(serialized)):
            raise ValueError("source product families must be unique and sorted")
        return values

    @field_validator("discovery_urls")
    @classmethod
    def validate_discovery_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("source discovery URLs must be unique and sorted")
        for value in values:
            parsed = urlsplit(value)
            if (
                value != value.strip()
                or parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.fragment
            ):
                raise ValueError("source discovery URLs must be canonical public HTTPS URLs")
        return values

    @model_validator(mode="after")
    def validate_discovery_url_scope(self) -> OfficialSourceEntry:
        for value in self.discovery_urls:
            parsed = urlsplit(value)
            if parsed.hostname != self.hostname or not any(
                _path_is_in_scope(parsed.path or "/", prefix) for prefix in self.path_prefixes
            ):
                raise ValueError("source discovery URL is outside its domain/path scope")
        return self


class OfficialSourceRegistry(SourceRegistryModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    registry_id: str = Field(min_length=1, max_length=120)
    evidence_cutoff: date
    entries: tuple[OfficialSourceEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_entries(self) -> OfficialSourceRegistry:
        source_ids = [item.source_id for item in self.entries]
        if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
            raise ValueError("source registry entries must be unique and sorted")
        host_paths: set[tuple[str, str]] = set()
        for entry in self.entries:
            for prefix in entry.path_prefixes:
                key = (entry.hostname, prefix)
                if key in host_paths:
                    raise ValueError("source registry path scopes must not overlap exactly")
                host_paths.add(key)
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()

    def authorize_url(
        self,
        url: str,
        capability: KnowledgeCapability,
    ) -> OfficialSourceEntry:
        parsed = urlsplit(url)
        if (
            url != url.strip()
            or any(ord(character) < 33 for character in url)
            or parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            raise OfficialSourceRegistryError(
                "external evidence URL must be credential-free HTTPS on the default port"
            )
        hostname = parsed.hostname.casefold()
        path = parsed.path or "/"
        candidates = [
            entry
            for entry in self.entries
            if entry.hostname == hostname
            and capability in entry.capabilities
            and any(_path_is_in_scope(path, prefix) for prefix in entry.path_prefixes)
        ]
        if len(candidates) != 1:
            raise OfficialSourceRegistryError(
                "external evidence URL is outside the approved source/capability scope"
            )
        return candidates[0]

    def entries_for_capability(
        self,
        capability: KnowledgeCapability,
        *,
        source_registry_ids: tuple[str, ...] | None = None,
    ) -> tuple[OfficialSourceEntry, ...]:
        """Return the exact official domain/path policies usable for discovery."""

        requested = None if source_registry_ids is None else set(source_registry_ids)
        if requested is not None:
            known = {entry.source_id for entry in self.entries}
            if requested - known:
                raise OfficialSourceRegistryError("unknown source registry ID")
        selected = tuple(
            entry
            for entry in self.entries
            if capability in entry.capabilities
            and (requested is None or entry.source_id in requested)
        )
        if requested is not None and {entry.source_id for entry in selected} != requested:
            raise OfficialSourceRegistryError(
                "source registry ID is not approved for the requested capability"
            )
        return selected

    def require_source_id(self, source_id: str) -> OfficialSourceEntry:
        matches = [entry for entry in self.entries if entry.source_id == source_id]
        if len(matches) != 1:
            raise OfficialSourceRegistryError("unknown source registry ID")
        return matches[0]


@lru_cache(maxsize=1)
def load_official_source_registry() -> OfficialSourceRegistry:
    resource = files("finance_agent_core.config").joinpath("approved_source_registry.json")
    return OfficialSourceRegistry.model_validate(json.loads(resource.read_text(encoding="utf-8")))


__all__ = [
    "OfficialSourceEntry",
    "OfficialSourceRegistry",
    "OfficialSourceRegistryError",
    "SourceAuthority",
    "SourceConnectorKind",
    "SourceContentPlatform",
    "load_official_source_registry",
]
