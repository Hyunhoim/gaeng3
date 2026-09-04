from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    KnowledgeTimeRange,
    predicate_for_capability,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.hangul_romanization import (
    PHONETIC_MARGIN,
    PHONETIC_MATCH_THRESHOLD,
    hangul_romanization_key,
    latin_label_phonetic_score,
)


class TemporalRelationError(RuntimeError):
    """Raised when an external relation lacks a complete authoritative chain."""


class TemporalRelationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemporalEntityKind(StrEnum):
    COMPANY = "company"
    PRODUCT = "product"
    THEME = "theme"
    FUND = "fund"
    RISK_CLAIM = "risk_claim"
    POLICY_EVENT = "policy_event"


class TemporalEntity(TemporalRelationModel):
    entity_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    kind: TemporalEntityKind
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = Field(default=(), max_length=50)

    @field_validator("canonical_name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("temporal entity name cannot be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if list(values) != sorted(values) or len(values) != len(set(values)):
            raise ValueError("temporal entity aliases must be unique and sorted")
        if any(not item.strip() or len(item) > 500 for item in values):
            raise ValueError("temporal entity aliases must be non-blank and bounded")
        return values

    def labels(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


class ExternalEvidenceReference(TemporalRelationModel):
    evidence_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,159}$")
    source_registry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=300)
    source_uri: str = Field(min_length=8, max_length=2000)
    published_at: date | None = None
    as_of: date
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=2000)

    @field_validator("title", "publisher", "locator", "excerpt")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("external evidence text cannot be blank")
        return value


class TemporalRelationRecord(TemporalRelationModel):
    relation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,159}$")
    capability: KnowledgeCapability
    predicate: KnowledgePredicate
    subject: TemporalEntity
    object: TemporalEntity
    product_family: ProductFamily | None = None
    product_id: str | None = Field(default=None, min_length=1, max_length=128)
    valid_from: date | None = None
    valid_to: date | None = None
    observed_at: date
    evidence: tuple[ExternalEvidenceReference, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_relation(self) -> TemporalRelationRecord:
        if self.predicate is not predicate_for_capability(self.capability):
            raise ValueError("temporal relation predicate differs from capability")
        if (self.product_family is None) != (self.product_id is None):
            raise ValueError("product family and product ID must be present together")
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_from > self.valid_to:
                raise ValueError("temporal relation validity dates are reversed")
        if self.valid_from is not None and self.observed_at < self.valid_from:
            raise ValueError("relation cannot be observed before it becomes valid")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if evidence_ids != sorted(evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("relation evidence must be unique and sorted")
        if (
            self.capability
            in {
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
                KnowledgeCapability.SECTOR_THEME_HISTORY,
            }
            and self.product_id is None
        ):
            raise ValueError("product relation capabilities require an official product reference")
        return self

    def overlaps(self, time_range: KnowledgeTimeRange | None) -> bool:
        if time_range is None:
            return True
        relation_start = self.valid_from or self.observed_at
        relation_end = self.valid_to or self.observed_at
        requested_start = time_range.start or date.min
        return relation_start <= time_range.end and relation_end >= requested_start


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


class TemporalRelationManifest(TemporalRelationModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["approved_temporal_relations"] = "approved_temporal_relations"
    evidence_cutoff: date
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_count: int = Field(ge=0)
    relation_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerifiedTemporalRelationBundle(TemporalRelationModel):
    manifest: TemporalRelationManifest
    relations: tuple[TemporalRelationRecord, ...] = ()

    @model_validator(mode="after")
    def verify_bundle(self) -> VerifiedTemporalRelationBundle:
        relation_ids = [item.relation_id for item in self.relations]
        if relation_ids != sorted(relation_ids) or len(relation_ids) != len(set(relation_ids)):
            raise ValueError("temporal relations must be unique and sorted")
        if self.manifest.relation_count != len(self.relations):
            raise ValueError("temporal relation count differs from manifest")
        digest = hashlib.sha256(
            _canonical_json_bytes([item.model_dump(mode="json") for item in self.relations])
        ).hexdigest()
        if digest != self.manifest.relation_set_sha256:
            raise ValueError("temporal relation set differs from manifest")
        return self


class TemporalRelationSearchRequest(TemporalRelationModel):
    capability: KnowledgeCapability
    subject: str = Field(min_length=1, max_length=500)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    time_range: KnowledgeTimeRange | None = None
    top_k: int = Field(default=20, ge=1, le=50)


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


class TemporalRelationStore:
    """Read-only exact entity/alias lookup over a sealed temporal relation bundle."""

    def __init__(
        self,
        bundle: VerifiedTemporalRelationBundle,
        registry: OfficialSourceRegistry | None = None,
    ) -> None:
        self.registry = registry or load_official_source_registry()
        if bundle.manifest.source_registry_sha256 != self.registry.canonical_sha256:
            raise TemporalRelationError("relation bundle source registry differs")
        if bundle.manifest.evidence_cutoff != self.registry.evidence_cutoff:
            raise TemporalRelationError("relation bundle evidence cutoff differs")
        self.bundle = bundle
        self._verify_sources()

    def _verify_sources(self) -> None:
        for relation in self.bundle.relations:
            if relation.observed_at > self.registry.evidence_cutoff:
                raise TemporalRelationError("relation was observed after the cutoff")
            for evidence in relation.evidence:
                source = self.registry.authorize_url(
                    evidence.source_uri,
                    relation.capability,
                )
                if source.source_id != evidence.source_registry_id:
                    raise TemporalRelationError("relation evidence source ID differs")
                if evidence.publisher != source.publisher:
                    raise TemporalRelationError("relation evidence publisher differs")
                if evidence.as_of > self.registry.evidence_cutoff:
                    raise TemporalRelationError("relation evidence date exceeds the cutoff")
                if (
                    evidence.published_at is not None
                    and evidence.published_at > self.registry.evidence_cutoff
                ):
                    raise TemporalRelationError("relation publication date exceeds the cutoff")
                if source.publication_date_required and evidence.published_at is None:
                    raise TemporalRelationError(
                        "relation evidence lacks a required publication date"
                    )

    def search(
        self,
        request: TemporalRelationSearchRequest,
    ) -> tuple[TemporalRelationRecord, ...]:
        query = _normalize(request.subject)
        if not query:
            raise ValueError("temporal relation subject has no searchable characters")
        families = set(request.product_families)
        matches = []
        for relation in self.bundle.relations:
            if relation.capability is not request.capability:
                continue
            if (
                families
                and relation.product_family is not None
                and relation.product_family not in families
            ):
                continue
            if not relation.overlaps(request.time_range):
                continue
            labels = {*relation.subject.labels(), *relation.object.labels()}
            if query not in {_normalize(label) for label in labels}:
                continue
            matches.append(relation)
        return tuple(matches[: request.top_k])

    def search_cross_script(
        self,
        request: TemporalRelationSearchRequest,
    ) -> tuple[TemporalRelationRecord, ...]:
        """Reach Latin-labelled relations from a Hangul subject by phonetic key.

        Holdings graphs carry the publisher's spelling (``Cambricon Technologies
        Corp Ltd``) while a question says ``캠브리콘``.  This mirrors the live
        extractor's recovery rule: the Hangul key must be uniquely close to one
        source-authored entity (ratio >= 0.64, length ratio 0.55-1.45, and a
        0.08 margin over the runner-up entity).  The alias only addresses the
        lookup; each relation keeps its own official evidence.
        """

        key = hangul_romanization_key(request.subject)
        if len(key) < 5:
            return ()
        families = set(request.product_families)
        eligible = [
            relation
            for relation in self.bundle.relations
            if relation.capability is request.capability
            and (
                not families
                or relation.product_family is None
                or relation.product_family in families
            )
            and relation.overlaps(request.time_range)
        ]
        entity_scores: dict[str, float] = {}
        label_scores: dict[str, float] = {}
        for relation in eligible:
            for entity in (relation.subject, relation.object):
                if entity.entity_id in entity_scores:
                    continue
                best = 0.0
                for label in entity.labels():
                    if label not in label_scores:
                        label_scores[label] = latin_label_phonetic_score(key, label)
                    best = max(best, label_scores[label])
                entity_scores[entity.entity_id] = best
        # Only entities that clear the threshold compete; a whole graph holds
        # thousands of names, so a sub-threshold runner-up (``Emerson`` next to
        # ``Cambricon``) is noise, not an ambiguity.
        ranked = sorted(
            (
                (score, entity_id)
                for entity_id, score in entity_scores.items()
                if score >= PHONETIC_MATCH_THRESHOLD
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if not ranked:
            return ()
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < PHONETIC_MARGIN:
            return ()
        winner = ranked[0][1]
        return tuple(
            relation
            for relation in eligible
            if winner in (relation.subject.entity_id, relation.object.entity_id)
        )[: request.top_k]


def seal_temporal_relation_bundle(
    relations: tuple[TemporalRelationRecord, ...],
    *,
    corpus_manifest_sha256: str,
    registry: OfficialSourceRegistry | None = None,
) -> VerifiedTemporalRelationBundle:
    active_registry = registry or load_official_source_registry()
    ordered = tuple(sorted(relations, key=lambda item: item.relation_id))
    relation_set_sha256 = hashlib.sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in ordered])
    ).hexdigest()
    manifest = TemporalRelationManifest(
        evidence_cutoff=active_registry.evidence_cutoff,
        source_registry_sha256=active_registry.canonical_sha256,
        relation_count=len(ordered),
        relation_set_sha256=relation_set_sha256,
        corpus_manifest_sha256=corpus_manifest_sha256,
    )
    bundle = VerifiedTemporalRelationBundle(manifest=manifest, relations=ordered)
    # Constructor performs the independent source/cutoff verification.
    TemporalRelationStore(bundle, active_registry)
    return bundle


def temporal_relation_bundle_bytes(bundle: VerifiedTemporalRelationBundle) -> bytes:
    return _canonical_json_bytes(bundle.model_dump(mode="json"))


def load_verified_temporal_relation_bundle(
    path: str | Path,
    *,
    expected_file_sha256: str,
    registry: OfficialSourceRegistry | None = None,
) -> VerifiedTemporalRelationBundle:
    if len(expected_file_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_file_sha256
    ):
        raise TemporalRelationError("temporal relation trust anchor is invalid")
    target = Path(path)
    if not target.is_absolute() or target.is_symlink():
        raise TemporalRelationError("temporal relation bundle path is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or before.st_size <= 0
                or before.st_size > 64 * 1024 * 1024
            ):
                raise TemporalRelationError("temporal relation bundle file is unsafe")
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise TemporalRelationError("temporal relation bundle changed while loading")
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise TemporalRelationError("temporal relation bundle changed while loading")
        finally:
            os.close(descriptor)
    except TemporalRelationError:
        raise
    except OSError as error:
        raise TemporalRelationError("temporal relation bundle is unavailable") from error
    if hashlib.sha256(data).hexdigest() != expected_file_sha256:
        raise TemporalRelationError("temporal relation bundle SHA-256 differs")
    try:
        bundle = VerifiedTemporalRelationBundle.model_validate_json(data)
    except ValueError as error:
        raise TemporalRelationError("temporal relation bundle schema is invalid") from error
    if data != temporal_relation_bundle_bytes(bundle):
        raise TemporalRelationError("temporal relation bundle is not canonical JSON")
    TemporalRelationStore(bundle, registry or load_official_source_registry())
    return bundle


__all__ = [
    "ExternalEvidenceReference",
    "TemporalEntity",
    "TemporalEntityKind",
    "TemporalRelationError",
    "TemporalRelationManifest",
    "TemporalRelationRecord",
    "TemporalRelationSearchRequest",
    "TemporalRelationStore",
    "VerifiedTemporalRelationBundle",
    "seal_temporal_relation_bundle",
    "load_verified_temporal_relation_bundle",
    "temporal_relation_bundle_bytes",
]
