from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
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
    KnowledgeDocumentTopNScope,
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


_BOUNDED_TOP_N_HOLDINGS_HEADING = re.compile(
    r"^\s*(?:"
    r"top\s*(?P<english_limit>[1-9]\d?)\s+"
    r"(?:portfolio\s+)?(?:holdings?|constituents?)(?:\s*\(\s*%\s*\))?"
    r"|"
    r"상위\s*(?P<korean_limit>[1-9]\d?)\s*"
    r"(?:개\s*)?(?:구성\s*종목|보유\s*종목|종목)"
    r")"
    r"(?:\s+(?:holdings?\s+subject\s+to\s+change|"
    r"구성\s*종목(?:은|이)?\s*변경될\s*수\s*있(?:음|습니다)))?\s*$",
    re.IGNORECASE,
)
_BOUNDED_HOLDINGS_TABLE_PREAMBLE = re.compile(
    r"^\s*(?:holdings?\s+subject\s+to\s+change|"
    r"(?:name|security|company|constituent|holding)\s+"
    r"(?:weight(?:\s*\(%\))?|%|percentage)|"
    r"(?:종목명|구성\s*종목)\s+(?:비중(?:\(%\))?|%))\s*$",
    re.IGNORECASE,
)
_BOUNDED_HOLDING_PERCENT_ROW = re.compile(
    r"^\s*(?P<name>\S(?:.*?\S)?)\s+"
    r"(?P<weight>[+-]?\d{1,3}(?:\.\d+)?)\s*%\s*$"
)
_HOLDING_ENTITY_SUFFIX = re.compile(
    r"(?:[\s,.-]+(?:incorporated|inc|corporation|corp|company|co|"
    r"limited|ltd|plc|holdings?|group|technologies?|technology|"
    r"class\s+[a-z]|adr))\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BoundedTopNHoldingsSection:
    """One structurally complete, source-authored Top-N percentage table.

    This is document-structure evidence only.  It carries no authority to bind
    the document to a provided-DB product; callers must prove that separately.
    """

    limit: int
    excerpt: str
    row_names: tuple[str, ...]


def _holding_identity_keys(value: str) -> frozenset[str]:
    """Return exact labels plus variants with only legal suffixes removed."""

    keys: set[str] = set()
    surface = " ".join(value.split()).strip()
    while surface:
        normalized = _normalize(surface)
        if normalized:
            keys.add(normalized)
        stripped = _HOLDING_ENTITY_SUFFIX.sub("", surface).rstrip(" ,.-")
        if stripped == surface:
            break
        surface = stripped
    return frozenset(keys)


def parse_bounded_top_n_holding_sections(
    text: str,
) -> tuple[BoundedTopNHoldingsSection, ...]:
    """Parse only complete Top-N holdings tables with explicit percentage rows."""

    lines = tuple(line.strip() for line in text.splitlines() if line.strip())
    sections: dict[tuple[int, str], BoundedTopNHoldingsSection] = {}
    for heading_index, heading_line in enumerate(lines):
        heading = _BOUNDED_TOP_N_HOLDINGS_HEADING.fullmatch(heading_line)
        if heading is None:
            continue
        raw_limit = heading.group("english_limit") or heading.group("korean_limit")
        limit = int(raw_limit)
        cursor = heading_index + 1
        preamble_count = 0
        while (
            cursor < len(lines)
            and preamble_count < 3
            and _BOUNDED_HOLDINGS_TABLE_PREAMBLE.fullmatch(lines[cursor]) is not None
        ):
            cursor += 1
            preamble_count += 1
        rows: list[tuple[str, str]] = []
        while cursor < len(lines):
            row = _BOUNDED_HOLDING_PERCENT_ROW.fullmatch(lines[cursor])
            if row is None:
                break
            name = " ".join(row.group("name").split()).strip()
            weight = float(row.group("weight"))
            normalized_name = _normalize(name)
            if (
                not 2 <= len(normalized_name) <= 200
                or not 0.0 < weight <= 100.0
                or any(normalized_name == existing for existing, _ in rows)
            ):
                rows = []
                break
            rows.append((normalized_name, name))
            cursor += 1
        if len(rows) != limit:
            continue
        excerpt = "\n".join(lines[heading_index:cursor])
        if len(excerpt) > 2000:
            continue
        section = BoundedTopNHoldingsSection(
            limit=limit,
            excerpt=excerpt,
            row_names=tuple(row_name for _, row_name in rows),
        )
        sections[(limit, excerpt)] = section
    return tuple(sections[key] for key in sorted(sections))


def _bounded_top_n_holding_membership(
    text: str,
    subject_labels: tuple[str, ...],
) -> tuple[int, str] | None:
    """Prove membership in a complete source-authored Top-N table.

    An unnumbered table proves only that a holding occurs somewhere inside the
    stated Top-N set; it does not prove the row's exact rank.  The conservative
    proof therefore returns the heading's N as an upper bound.  A section is
    admitted only when exactly N unique, positive percentage rows occur before
    the next non-table line and one whole row identifies the requested entity.
    """

    requested_keys = frozenset(
        key for label in subject_labels for key in _holding_identity_keys(label)
    )
    if not requested_keys:
        return None
    matches = {
        (section.limit, section.excerpt)
        for section in parse_bounded_top_n_holding_sections(text)
        if any(
            requested_keys.intersection(_holding_identity_keys(row_name))
            for row_name in section.row_names
        )
    }
    if len(matches) != 1:
        return None
    return next(iter(matches))


class TemporalRelationDocumentRank(TemporalRelationModel):
    """Exact document rank or conservative source-evidenced rank upper bound."""

    scope_kind: Literal["document_top_n"] = "document_top_n"
    position: int = Field(ge=1, le=500)
    basis: Literal[
        "explicit_document_rank",
        "official_ordered_holdings_array",
        "bounded_top_n_membership",
    ]
    evidence_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,159}$")


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
    document_rank: TemporalRelationDocumentRank | None = None
    # An independently evidenced entity may scope a relation without being its
    # semantic subject or object. For example, an issuer filing can prove
    # issuer-level risks for one DB-bound bond. Keeping that issuer here avoids
    # pretending it is an alias of either the bond or the risk claim.
    scope_entities: tuple[TemporalEntity, ...] = Field(default=(), max_length=8)

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
        if self.document_rank is not None:
            if self.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS:
                raise ValueError("document rank is allowed only for portfolio holdings")
            rank_evidence = next(
                (
                    item
                    for item in self.evidence
                    if item.evidence_id == self.document_rank.evidence_id
                ),
                None,
            )
            if rank_evidence is None:
                raise ValueError("document rank must name relation evidence")
            object_labels = tuple(
                "".join(
                    character
                    for character in unicodedata.normalize("NFKC", label).casefold()
                    if character.isalnum()
                )
                for label in self.object.labels()
            )
            normalized_rank_excerpt = "".join(
                character
                for character in unicodedata.normalize("NFKC", rank_evidence.excerpt).casefold()
                if character.isalnum()
            )
            if not any(label and label in normalized_rank_excerpt for label in object_labels):
                raise ValueError("document rank evidence does not name the holding")
            if self.document_rank.basis == "official_ordered_holdings_array":
                expected_locator = f"pdf.list[{self.document_rank.position - 1}]"
                if rank_evidence.locator != expected_locator:
                    raise ValueError("ordered holdings rank differs from its source locator")
            elif self.document_rank.basis == "explicit_document_rank":
                position = re.escape(str(self.document_rank.position))
                ranked_lines = tuple(
                    line
                    for line in rank_evidence.excerpt.splitlines()
                    if re.search(
                        rf"(?i)^\s*(?:no\.?\s*)?{position}(?:\s*위|[.)]|\s+)",
                        line,
                    )
                    is not None
                    or re.search(
                        rf"(?i)(?:순위|rank)\s*[:#]?\s*{position}(?!\d)",
                        line,
                    )
                    is not None
                )
                if not any(
                    label
                    and label
                    in "".join(
                        character
                        for character in unicodedata.normalize("NFKC", line).casefold()
                        if character.isalnum()
                    )
                    for line in ranked_lines
                    for label in object_labels
                ):
                    raise ValueError("explicit document rank is absent from its evidence")
            else:
                membership = _bounded_top_n_holding_membership(
                    rank_evidence.excerpt,
                    self.object.labels(),
                )
                if membership is None or membership[0] != self.document_rank.position:
                    raise ValueError("bounded top-N membership is absent from its evidence")
        if self.scope_entities:
            entity_keys = [(item.kind.value, item.entity_id) for item in self.scope_entities]
            if entity_keys != sorted(entity_keys) or len(entity_keys) != len(set(entity_keys)):
                raise ValueError("relation scope entities must be unique and sorted")
            if self.capability is not KnowledgeCapability.PRODUCT_RISK_FACTORS:
                raise ValueError("relation scope entities are allowed only for product risks")
            if any(item.kind is not TemporalEntityKind.COMPANY for item in self.scope_entities):
                raise ValueError("product-risk scope entities must be companies")
            evidence_texts = tuple(
                "".join(
                    character
                    for character in unicodedata.normalize("NFKC", evidence.excerpt).casefold()
                    if character.isalnum()
                )
                for evidence in self.evidence
            )
            for entity in self.scope_entities:
                labels = tuple(
                    "".join(
                        character
                        for character in unicodedata.normalize("NFKC", label).casefold()
                        if character.isalnum()
                    )
                    for label in entity.labels()
                )
                if not any(
                    label and label in evidence_text
                    for label in labels
                    for evidence_text in evidence_texts
                ):
                    raise ValueError("relation scope entity is absent from its evidence")
        return self

    def overlaps(self, time_range: KnowledgeTimeRange | None) -> bool:
        if time_range is None:
            return True
        relation_start = self.valid_from or self.observed_at
        relation_end = self.valid_to or self.observed_at
        requested_start = time_range.start or date.min
        return relation_start <= time_range.end and relation_end >= requested_start

    def satisfies_document_scope(self, scope: KnowledgeDocumentTopNScope | None) -> bool:
        if scope is None:
            return True
        # ``position`` is either an exact rank or, for an unnumbered but
        # complete Top-N table, the conservative upper bound N.  Consequently
        # a Top-10 membership can satisfy Top-10/Top-20, never Top-5.
        return self.document_rank is not None and self.document_rank.position <= scope.limit


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
        payloads = [item.model_dump(mode="json") for item in self.relations]
        compatible_digests = {
            hashlib.sha256(_canonical_json_bytes(variant)).hexdigest()
            for variant in _compatible_relation_payloads(payloads)
        }
        if self.manifest.relation_set_sha256 not in compatible_digests:
            raise ValueError("temporal relation set differs from manifest")
        return self


class TemporalRelationSearchRequest(TemporalRelationModel):
    capability: KnowledgeCapability
    subject: str = Field(min_length=1, max_length=500)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    time_range: KnowledgeTimeRange | None = None
    document_scope: KnowledgeDocumentTopNScope | None = None
    top_k: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def validate_document_scope(self) -> TemporalRelationSearchRequest:
        if (
            self.document_scope is not None
            and self.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS
        ):
            raise ValueError("document top-N scope is allowed only for portfolio holdings")
        return self


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
            if not relation.satisfies_document_scope(request.document_scope):
                continue
            labels = {
                *relation.subject.labels(),
                *relation.object.labels(),
                *(label for entity in relation.scope_entities for label in entity.labels()),
            }
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
            and relation.satisfies_document_scope(request.document_scope)
        ]
        # Uniqueness is judged on the matched *label*, not the entity: two
        # publishers spell one company differently (``Cambricon Technologies
        # Corp Ltd`` vs the truncated ``Cambricon Technologies Corp Lt``), yet
        # both carry the alias ``Cambricon``, which is the label the question
        # sounds like.  Only a different label that also clears the threshold
        # within the margin makes the match ambiguous.
        label_scores: dict[str, float] = {}
        for relation in eligible:
            for label in (
                *relation.subject.labels(),
                *relation.object.labels(),
                *(label for entity in relation.scope_entities for label in entity.labels()),
            ):
                folded = "".join(ch for ch in label.casefold() if ch.isalnum())
                if folded and folded not in label_scores:
                    label_scores[folded] = latin_label_phonetic_score(key, label)
        ranked = sorted(
            (
                (score, folded)
                for folded, score in label_scores.items()
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
            if any(
                "".join(ch for ch in label.casefold() if ch.isalnum()) == winner
                for label in (
                    *relation.subject.labels(),
                    *relation.object.labels(),
                    *(label for entity in relation.scope_entities for label in entity.labels()),
                )
            )
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


def _compatible_relation_payloads(
    payloads: list[dict[str, object]],
) -> tuple[list[dict[str, object]], ...]:
    """Return canonical schema-1.0 payload shapes with optional defaults omitted.

    ``document_rank`` and ``scope_entities`` were introduced independently.
    Bundles sealed between those additions may omit either default while still
    carrying the other.  Accept only those four exact shapes; no evidence,
    identity, date, or authority field is relaxed.
    """

    variants: list[list[dict[str, object]]] = []
    for omit_empty_rank, omit_empty_scope in (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ):
        variant = [dict(payload) for payload in payloads]
        for payload in variant:
            if omit_empty_rank and payload.get("document_rank") is None:
                payload.pop("document_rank", None)
            if omit_empty_scope and not payload.get("scope_entities"):
                payload.pop("scope_entities", None)
        variants.append(variant)
    return tuple(variants)


def _compatible_temporal_relation_bundle_bytes(
    bundle: VerifiedTemporalRelationBundle,
) -> frozenset[bytes]:
    payload = bundle.model_dump(mode="json")
    return frozenset(
        _canonical_json_bytes(
            {
                "manifest": payload["manifest"],
                "relations": relations,
            }
        )
        for relations in _compatible_relation_payloads(payload["relations"])
    )


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
                # A holdings graph over two publishers (~29k relations) is ~64 MiB.
                or before.st_size > 256 * 1024 * 1024
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
    if data not in _compatible_temporal_relation_bundle_bytes(bundle):
        raise TemporalRelationError("temporal relation bundle is not canonical JSON")
    TemporalRelationStore(bundle, registry or load_official_source_registry())
    return bundle


__all__ = [
    "BoundedTopNHoldingsSection",
    "ExternalEvidenceReference",
    "TemporalEntity",
    "TemporalEntityKind",
    "TemporalRelationError",
    "TemporalRelationManifest",
    "TemporalRelationRecord",
    "TemporalRelationDocumentRank",
    "TemporalRelationSearchRequest",
    "TemporalRelationStore",
    "VerifiedTemporalRelationBundle",
    "load_verified_temporal_relation_bundle",
    "parse_bounded_top_n_holding_sections",
    "seal_temporal_relation_bundle",
    "temporal_relation_bundle_bytes",
]
