from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.source_selection import normalize_selection_surface
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
    SourceAuthority,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.pending_corpus import PendingCorpusManifest
from finance_agent_core.retrieval.temporal_relations import (
    TemporalEntity,
    TemporalRelationError,
    TemporalRelationStore,
    VerifiedTemporalRelationBundle,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UNKNOWN_OWNER = "unknown_not_in_provided_schema"
_MAX_PENDING_TEXT_BYTES = 16 * 1024 * 1024
_COVER_TEXT_CHARS = 12_000
_ASCII_IDENTITY_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.:-]{1,127}")
_ORGANIZATION_SUFFIXES = tuple(
    sorted(
        (
            "assetmanagement",
            "investmentmanagement",
            "fundsadvisors",
            "자산운용",
            "투자신탁운용",
            "advisors",
            "advisor",
            "investments",
            "investment",
            "management",
            "funds",
            "shares",
            "incorporated",
            "corporation",
            "company",
            "corp",
            "inc",
            "ltd",
            "llc",
        ),
        key=len,
        reverse=True,
    )
)

_APPLICABLE_CAPABILITIES: Mapping[ProductFamily, tuple[KnowledgeCapability, ...]] = {
    ProductFamily.BOND: (KnowledgeCapability.PRODUCT_RISK_FACTORS,),
    ProductFamily.DOMESTIC_ETP: (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    ),
    ProductFamily.OVERSEAS_ETP: (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    ),
    ProductFamily.FUND: (
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    ),
}
_CAPABILITY_QUERY_TERMS: Mapping[KnowledgeCapability, str] = {
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: "펀드 구조 투자전략 운용 동향",
    KnowledgeCapability.PORTFOLIO_HOLDINGS: "구성종목 보유종목 포트폴리오",
    KnowledgeCapability.CORPORATE_SUBSIDIARY: "자회사 계열사 기업관계",
    KnowledgeCapability.SECTOR_THEME_HISTORY: "섹터 테마 연결 이력",
    KnowledgeCapability.PRODUCT_RISK_FACTORS: "투자설명서 위험요인 상품 위험",
}
_FAMILY_PRIORITY = {
    ProductFamily.DOMESTIC_ETP: 0,
    ProductFamily.OVERSEAS_ETP: 1,
    ProductFamily.FUND: 2,
    ProductFamily.BOND: 3,
}
_CAPABILITY_PRIORITY = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: 0,
    KnowledgeCapability.SECTOR_THEME_HISTORY: 1,
    KnowledgeCapability.PRODUCT_RISK_FACTORS: 2,
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: 3,
    KnowledgeCapability.CORPORATE_SUBSIDIARY: 4,
}


class CorpusCoverageError(RuntimeError):
    """Raised when an analysis input or output cannot be trusted."""


class CoverageAuthority(StrEnum):
    ACTIVE_VERIFIED = "active_verified"
    PENDING_REVIEW = "pending_review"
    LEGACY_LIVE_CANDIDATE = "legacy_live_candidate"


class PendingBindingMethod(StrEnum):
    EXACT_PRODUCT_ID = "exact_product_id"
    EXACT_ISIN = "exact_isin"
    UNIQUE_TICKER = "unique_ticker"
    UNIQUE_CANONICAL_NAME = "unique_canonical_name"
    UNIQUE_SHORT_NAME_PUBLISHER = "unique_short_name_publisher"
    COVER_UNIQUE_LONGEST = "cover_unique_longest"
    AMBIGUOUS = "ambiguous"
    ENTITY_ONLY = "entity_only"
    UNBOUND = "unbound"
    REJECTED_CUTOFF = "rejected_cutoff"
    REJECTED_AUTHORITY = "rejected_authority"
    VERIFIED_TEMPORAL_RELATION = "verified_temporal_relation"


class GapAction(StrEnum):
    REVIEW_EXISTING_DOCUMENT = "review_existing_document"
    REBUILD_OR_RESEAL_LEGACY_ARTIFACT = "rebuild_or_reseal_legacy_artifact"
    COLLECT_OFFICIAL_SNAPSHOT = "collect_official_snapshot"
    EXPAND_SOURCE_REGISTRY = "expand_source_registry"
    MANUAL_ENTITY_REVIEW = "manual_entity_review"


class CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductCoverageKey(CoverageModel):
    product_family: ProductFamily
    product_id: str = Field(min_length=1, max_length=128)


class DatasetCoverageReceipt(CoverageModel):
    product_family: ProductFamily
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_date: date
    product_count: int = Field(ge=0)


class RelationBundleInput(CoverageModel):
    source_id: str = Field(min_length=1, max_length=200)
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle: VerifiedTemporalRelationBundle


class CoverageWarning(CoverageModel):
    code: str = Field(pattern=r"^[a-z0-9_]{3,80}$")
    source_id: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=500)


class DocumentProductBinding(CoverageModel):
    document_id: str = Field(min_length=1, max_length=160)
    source_registry_id: str = Field(min_length=1, max_length=100)
    publisher: str = Field(min_length=1, max_length=300)
    capability: KnowledgeCapability
    authority: CoverageAuthority
    binding_method: PendingBindingMethod
    candidate_products: tuple[ProductCoverageKey, ...] = ()
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_authority(self) -> DocumentProductBinding:
        keys = [
            (_family_order(item.product_family), item.product_id)
            for item in self.candidate_products
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("document binding product candidates must be unique and sorted")
        if self.authority in {
            CoverageAuthority.ACTIVE_VERIFIED,
            CoverageAuthority.LEGACY_LIVE_CANDIDATE,
        }:
            if (
                self.binding_method is not PendingBindingMethod.VERIFIED_TEMPORAL_RELATION
                or len(self.candidate_products) != 1
            ):
                raise ValueError("relation-backed bindings require one exact product key")
        elif self.binding_method is PendingBindingMethod.VERIFIED_TEMPORAL_RELATION:
            raise ValueError("pending documents cannot claim verified relation binding")
        return self


class ProductCoverageMatrixRow(CoverageModel):
    product_family: ProductFamily
    source_owner: str = Field(min_length=1, max_length=300)
    capability: KnowledgeCapability
    total_products: int = Field(ge=0)
    active_covered_products: int = Field(ge=0)
    pending_review_products: int = Field(ge=0)
    legacy_live_candidate_products: int = Field(ge=0)
    source_scope_candidate_products: int = Field(ge=0)
    active_document_count: int = Field(ge=0)
    pending_document_count: int = Field(ge=0)
    legacy_document_count: int = Field(ge=0)
    active_coverage_ratio: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_counts(self) -> ProductCoverageMatrixRow:
        for value in (
            self.active_covered_products,
            self.pending_review_products,
            self.legacy_live_candidate_products,
            self.source_scope_candidate_products,
        ):
            if value > self.total_products:
                raise ValueError("coverage matrix numerator exceeds its product denominator")
        expected = (
            0.0 if not self.total_products else self.active_covered_products / self.total_products
        )
        if self.active_coverage_ratio != round(expected, 8):
            raise ValueError("coverage matrix ratio differs from its counts")
        return self


class EntityCoverageMatrixRow(CoverageModel):
    capability: KnowledgeCapability
    authority: CoverageAuthority
    entity_kind: str = Field(min_length=1, max_length=80)
    entity_id: str = Field(min_length=1, max_length=160)
    canonical_name: str = Field(min_length=1, max_length=500)
    relation_count: int = Field(gt=0)
    document_count: int = Field(gt=0)
    source_registry_ids: tuple[str, ...] = Field(min_length=1)


class PendingEntityDocumentSummary(CoverageModel):
    document_id: str = Field(min_length=1, max_length=160)
    source_registry_id: str = Field(min_length=1, max_length=100)
    publisher: str = Field(min_length=1, max_length=300)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1)
    status: Literal["unextracted_pending_entity_document"] = "unextracted_pending_entity_document"


class PendingDocumentReviewItem(CoverageModel):
    task_id: str = Field(pattern=r"^gap-document-[0-9a-f]{16}$")
    document_id: str = Field(min_length=1, max_length=160)
    source_registry_id: str = Field(min_length=1, max_length=100)
    publisher: str = Field(min_length=1, max_length=300)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1)
    action: GapAction
    binding_method: PendingBindingMethod
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_document_review(self) -> PendingDocumentReviewItem:
        if self.action not in {
            GapAction.REVIEW_EXISTING_DOCUMENT,
            GapAction.MANUAL_ENTITY_REVIEW,
        }:
            raise ValueError("pending document review action is invalid")
        if self.binding_method not in {
            PendingBindingMethod.UNBOUND,
            PendingBindingMethod.ENTITY_ONLY,
        }:
            raise ValueError("pending document review binding method is invalid")
        return self


class GapCollectionItem(CoverageModel):
    task_id: str = Field(pattern=r"^gap-[0-9a-f]{16}$")
    product_family: ProductFamily
    product_id: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=500)
    source_owner: str = Field(min_length=1, max_length=300)
    capability: KnowledgeCapability
    action: GapAction
    reason_codes: tuple[str, ...] = Field(min_length=1)
    source_registry_ids: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_collection_boundary(self) -> GapCollectionItem:
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("gap reason codes must be unique and sorted")
        if tuple(sorted(set(self.source_registry_ids))) != self.source_registry_ids:
            raise ValueError("gap source registry IDs must be unique and sorted")
        if self.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT:
            if not self.source_registry_ids or not self.queries:
                raise ValueError("network collection requires scoped sources and product queries")
        elif self.queries:
            raise ValueError("only official snapshot collection can carry network queries")
        if self.action is GapAction.EXPAND_SOURCE_REGISTRY and self.source_registry_ids:
            raise ValueError("source-registry expansion cannot claim an existing source")
        return self


class GapCollectionQueue(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    queue_kind: Literal["corpus_gap_collection_queue"] = "corpus_gap_collection_queue"
    max_gap_items: int = Field(ge=1, le=20_000)
    total_gap_count: int = Field(ge=0)
    returned_gap_count: int = Field(ge=0)
    truncated: bool
    action_counts: dict[str, int]
    document_review_items: tuple[PendingDocumentReviewItem, ...]
    items: tuple[GapCollectionItem, ...]
    manual_review_items: tuple[GapCollectionItem, ...] = ()
    network_collection_task_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_queue(self) -> GapCollectionQueue:
        if self.returned_gap_count != (
            len(self.items) + len(self.document_review_items) + len(self.manual_review_items)
        ):
            raise ValueError("returned gap count differs from queue items")
        if self.truncated != (self.total_gap_count > self.returned_gap_count):
            raise ValueError("gap queue truncation flag differs")
        ids = [
            item.task_id
            for item in (
                *self.items,
                *self.document_review_items,
                *self.manual_review_items,
            )
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("gap tasks must be unique")
        expected_network = tuple(
            item.task_id
            for item in self.items
            if item.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT
        )
        if self.network_collection_task_ids != expected_network:
            raise ValueError("network collection list contains a non-collection action")
        if any(
            item.action is not GapAction.MANUAL_ENTITY_REVIEW for item in self.manual_review_items
        ):
            raise ValueError("manual review queue contains a non-manual action")
        return self


class CorpusCoverageAnalysis(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    report_kind: Literal["official_corpus_product_coverage"] = "official_corpus_product_coverage"
    algorithm_version: Literal["coverage-matrix-v2"] = "coverage-matrix-v2"
    analysis_id: str = Field(pattern=r"^coverage-[0-9a-f]{16}$")
    dataset_release_id: str = Field(min_length=1, max_length=160)
    evidence_cutoff: date
    source_registry_id: str = Field(min_length=1, max_length=120)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    knowledge_coverage_manifest_id: str = Field(min_length=1, max_length=120)
    knowledge_coverage_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    datasets: tuple[DatasetCoverageReceipt, ...]
    pending_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    active_relation_file_sha256s: tuple[str, ...] = ()
    legacy_relation_file_sha256s: tuple[str, ...] = ()
    bindings: tuple[DocumentProductBinding, ...]
    product_matrix: tuple[ProductCoverageMatrixRow, ...]
    entity_matrix: tuple[EntityCoverageMatrixRow, ...]
    pending_entity_documents: tuple[PendingEntityDocumentSummary, ...]
    warnings: tuple[CoverageWarning, ...]

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


class CoverageArtifactReceipt(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["corpus_coverage_artifact_receipt"] = "corpus_coverage_artifact_receipt"
    analysis_id: str = Field(pattern=r"^coverage-[0-9a-f]{16}$")
    input_sha256s: dict[str, str]
    output_sha256s: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Identity:
    record: ProductIdentityRecord
    key: ProductCoverageKey
    owner: str
    product_id_token: str
    isin_token: str | None
    ticker_token: str | None
    canonical_name: str
    short_name: str | None


def canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _family_order(family: ProductFamily) -> int:
    return _FAMILY_PRIORITY[family]


def _product_key_tuple(key: ProductCoverageKey) -> tuple[str, str]:
    return key.product_family.value, key.product_id


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _ascii_token(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return normalized or None


def _owner(record: ProductIdentityRecord) -> str:
    if record.product_family == ProductFamily.FUND.value or not record.source_owner:
        return _UNKNOWN_OWNER
    return " ".join(record.source_owner.split())


def _eligible_identities(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
) -> tuple[_Identity, ...]:
    identities: list[_Identity] = []
    seen: set[tuple[str, str]] = set()
    for family in sorted(records_by_family, key=_family_order):
        for record in records_by_family[family]:
            if ProductFamily(record.product_family) is not family:
                raise CorpusCoverageError("identity record differs from its family bucket")
            if record.is_quarantined or (
                family is ProductFamily.FUND and record.public_offering is False
            ):
                continue
            raw_key = (family.value, record.product_id)
            if raw_key in seen:
                raise CorpusCoverageError("provided DB contains a duplicate product key")
            seen.add(raw_key)
            identities.append(
                _Identity(
                    record=record,
                    key=ProductCoverageKey(product_family=family, product_id=record.product_id),
                    owner=_owner(record),
                    product_id_token=(
                        _ascii_token(record.product_id) or record.product_id.casefold()
                    ),
                    isin_token=_ascii_token(record.isin),
                    ticker_token=_ascii_token(record.ticker),
                    canonical_name=_fold(record.product_name),
                    short_name=_fold(record.short_name or "") or None,
                )
            )
    return tuple(
        sorted(
            identities,
            key=lambda item: (
                _family_order(item.key.product_family),
                item.key.product_id,
            ),
        )
    )


def _warning(code: str, source_id: str, detail: str) -> CoverageWarning:
    return CoverageWarning(code=code, source_id=source_id, detail=detail[:500])


def _deduplicate_warnings(warnings: Sequence[CoverageWarning]) -> tuple[CoverageWarning, ...]:
    by_key = {(item.code, item.source_id, item.detail): item for item in warnings}
    return tuple(by_key[key] for key in sorted(by_key))


def _header_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", unquote(value)).casefold()
    return frozenset(match.group(0) for match in _ASCII_IDENTITY_TOKEN.finditer(normalized))


def _unique_label_owners(
    identities: Sequence[_Identity],
    attribute: Literal["product_id_token", "isin_token", "ticker_token"],
) -> dict[str, tuple[_Identity, ...]]:
    owners: dict[str, list[_Identity]] = defaultdict(list)
    for identity in identities:
        value = getattr(identity, attribute)
        if value:
            owners[value].append(identity)
    return {
        value: tuple(sorted(items, key=lambda item: _product_key_tuple(item.key)))
        for value, items in owners.items()
    }


def _organization_brand_roots(value: str) -> frozenset[str]:
    root = normalize_selection_surface(value)
    if not root:
        return frozenset()
    changed = True
    while changed:
        changed = False
        for suffix in _ORGANIZATION_SUFFIXES:
            if root.endswith(suffix) and len(root) - len(suffix) >= 3:
                root = root[: -len(suffix)]
                changed = True
                break
    return frozenset((root,)) if len(root) >= 3 else frozenset()


def _publisher_matches_owner(identity: _Identity, entry: OfficialSourceEntry) -> bool:
    if identity.owner == _UNKNOWN_OWNER:
        return False
    owner = normalize_selection_surface(identity.owner)
    product_name = normalize_selection_surface(identity.record.product_name)
    if owner == product_name or (len(product_name) >= 8 and product_name in owner):
        return _entry_product_prefix_brand_score(identity, entry) is not None
    aliases = (entry.publisher, *entry.selection_aliases)
    for raw_alias in aliases:
        alias = normalize_selection_surface(raw_alias)
        if not alias:
            continue
        if owner == alias or (len(alias) >= 3 and (alias in owner or owner in alias)):
            return True
        if _organization_brand_roots(identity.owner).intersection(
            _organization_brand_roots(raw_alias)
        ):
            return True
    return False


def _entry_product_prefix_brand_score(
    identity: _Identity,
    entry: OfficialSourceEntry,
) -> tuple[int, int] | None:
    product_name = normalize_selection_surface(identity.record.product_name)
    matches = []
    for raw_alias in (entry.publisher, *entry.selection_aliases):
        alias = normalize_selection_surface(raw_alias)
        if len(alias) < 3:
            continue
        position = product_name.find(alias)
        if 0 <= position <= 5:
            matches.append((len(alias), -position))
    return max(matches, default=None)


def _select_source_publisher_group(
    identity: _Identity,
    capability: KnowledgeCapability,
    registry: OfficialSourceRegistry,
) -> tuple[OfficialSourceEntry, ...]:
    eligible = tuple(
        entry
        for entry in registry.entries
        if capability in entry.capabilities
        and identity.key.product_family in entry.product_families
        and entry.authority in {SourceAuthority.ASSET_MANAGER, SourceAuthority.PRODUCT_ISSUER}
    )
    owner = normalize_selection_surface(identity.owner)
    product_name = normalize_selection_surface(identity.record.product_name)
    contaminated = owner == product_name or (len(product_name) >= 8 and product_name in owner)
    scores: dict[str, tuple[int, int, int]] = {}
    for entry in eligible:
        publisher = normalize_selection_surface(entry.publisher)
        if contaminated:
            prefix_score = _entry_product_prefix_brand_score(identity, entry)
            if prefix_score is None:
                continue
            score = (1, *prefix_score)
        else:
            owner_scores = []
            for raw_alias in (entry.publisher, *entry.selection_aliases):
                alias = normalize_selection_surface(raw_alias)
                if len(alias) < 3:
                    continue
                if owner == alias:
                    owner_scores.append((2, len(alias), 0))
                elif alias in owner or owner in alias:
                    owner_scores.append((1, len(alias), 0))
            if not owner_scores:
                owner_roots = _organization_brand_roots(identity.owner)
                alias_roots = set(_organization_brand_roots(entry.publisher))
                for alias in entry.selection_aliases:
                    alias_roots.update(_organization_brand_roots(alias))
                shared_roots = owner_roots.intersection(alias_roots)
                if not shared_roots:
                    continue
                owner_scores.append((0, max(map(len, shared_roots)), 0))
            score = max(owner_scores)
        previous = scores.get(publisher)
        if previous is None or score > previous:
            scores[publisher] = score
    if not scores:
        return ()
    best_score = max(scores.values())
    winners = [publisher for publisher, score in scores.items() if score == best_score]
    if len(winners) != 1:
        return ()
    winner = winners[0]
    return tuple(
        entry for entry in eligible if normalize_selection_surface(entry.publisher) == winner
    )


def _identity_pool_for_source(
    identities: Sequence[_Identity],
    entry: OfficialSourceEntry,
    capability: KnowledgeCapability,
) -> tuple[_Identity, ...]:
    if not entry.product_families:
        return ()
    families = set(entry.product_families)
    return tuple(
        item
        for item in identities
        if item.key.product_family in families
        and capability in _APPLICABLE_CAPABILITIES[item.key.product_family]
    )


def _distinctive_product_name(value: str | None) -> bool:
    if not value:
        return False
    folded = _fold(value)
    if any("가" <= character <= "힣" for character in value):
        return len(folded) >= 4
    return len(folded) >= 8 or (
        len(folded) >= 5 and any(character.isdigit() for character in folded)
    )


def _brand_name_corroborates(
    identity: _Identity,
    entry: OfficialSourceEntry,
    folded_header: str,
) -> bool:
    names = tuple(
        value
        for value in (identity.canonical_name, identity.short_name)
        if _distinctive_product_name(value)
    )
    aliases = tuple(
        _fold(value)
        for value in (entry.publisher, *entry.selection_aliases)
        if len(_fold(value)) >= 3
    )
    return any(
        name in folded_header and any(alias in name and alias in folded_header for alias in aliases)
        for name in names
    )


def _trusted_named_identity_pool(
    identities: Sequence[_Identity],
    entry: OfficialSourceEntry,
    folded_header: str,
) -> tuple[_Identity, ...]:
    if entry.authority not in {
        SourceAuthority.ASSET_MANAGER,
        SourceAuthority.PRODUCT_ISSUER,
    }:
        return ()
    return tuple(
        item
        for item in identities
        if _publisher_matches_owner(item, entry)
        or _brand_name_corroborates(item, entry, folded_header)
    )


def _contained_name_survivors(
    matches: Mapping[tuple[str, str], set[str]],
    matched_text: str,
) -> tuple[tuple[str, str], ...]:
    """Remove only aliases wholly explained by a longer sibling match.

    Two unrelated product names survive together and therefore remain
    ambiguous.  This admits a product title containing its shorter sibling
    name while keeping a multi-product guide fail-closed.
    """

    survivors: list[tuple[str, str]] = []
    for key, own_surfaces in matches.items():
        other_surfaces = {
            surface
            for other_key, surfaces in matches.items()
            if other_key != key and other_key[0] == key[0]
            for surface in surfaces
        }
        contained = bool(own_surfaces)
        for surface in own_surfaces:
            longer = tuple(
                sorted(
                    (other for other in other_surfaces if surface != other and surface in other),
                    key=lambda value: (-len(value), value),
                )
            )
            residual = matched_text
            for other in longer:
                residual = residual.replace(other, "")
            if not longer or surface in residual:
                contained = False
                break
        if not contained:
            survivors.append(key)
    return tuple(
        sorted(
            survivors,
            key=lambda value: (
                _FAMILY_PRIORITY[ProductFamily(value[0])],
                value[1],
            ),
        )
    )


def _match_names(
    identities: Sequence[_Identity],
    folded_surface: str,
    *,
    attribute: Literal["canonical_name", "short_name"],
) -> dict[tuple[str, str], set[str]]:
    matched: dict[tuple[str, str], set[str]] = defaultdict(set)
    for identity in identities:
        value = getattr(identity, attribute)
        if value and _distinctive_product_name(value) and value in folded_surface:
            matched[_product_key_tuple(identity.key)].add(value)
    return matched


def _market_suffix_ticker_base(value: str) -> str | None:
    match = re.fullmatch(r"([a-z0-9]{2,15})\.[a-z]{1,3}", value)
    return None if match is None else match.group(1)


def _resolve_pending_binding(
    *,
    identities: Sequence[_Identity],
    entry: OfficialSourceEntry,
    capability: KnowledgeCapability,
    header: str,
    cover: str,
) -> tuple[PendingBindingMethod, tuple[ProductCoverageKey, ...]]:
    pool = _identity_pool_for_source(identities, entry, capability)
    if not pool:
        return PendingBindingMethod.ENTITY_ONLY, ()
    by_key = {_product_key_tuple(item.key): item for item in pool}
    tokens = _header_tokens(header)

    for attribute, method in (
        ("product_id_token", PendingBindingMethod.EXACT_PRODUCT_ID),
        ("isin_token", PendingBindingMethod.EXACT_ISIN),
    ):
        owners = _unique_label_owners(pool, attribute)
        matched = {
            _product_key_tuple(identity.key)
            for token in tokens
            for identity in owners.get(token, ())
        }
        if len(matched) == 1:
            key = next(iter(matched))
            return method, (by_key[key].key,)
        if len(matched) > 1:
            return PendingBindingMethod.AMBIGUOUS, tuple(
                by_key[key].key
                for key in sorted(
                    matched,
                    key=lambda value: (_FAMILY_PRIORITY[ProductFamily(value[0])], value[1]),
                )
            )

    folded_header = _fold(unquote(header))
    named_pool = _trusted_named_identity_pool(pool, entry, folded_header)
    ticker_owners = _unique_label_owners(named_pool, "ticker_token")
    ticker_matches = {
        _product_key_tuple(identity.key)
        for token in tokens
        if len(token) >= 4 or any(character.isdigit() for character in token)
        for identity in ticker_owners.get(token, ())
        if len(ticker_owners[token]) == 1
    }
    suffixed_matches: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for identity in named_pool:
        if not identity.ticker_token or not _publisher_matches_owner(identity, entry):
            continue
        identity_base = _market_suffix_ticker_base(identity.ticker_token)
        for token in tokens:
            token_base = _market_suffix_ticker_base(token)
            comparable_base = identity_base or token_base
            if (
                comparable_base is not None
                and len(comparable_base) >= 4
                and (identity_base or identity.ticker_token) == (token_base or token)
            ):
                suffixed_matches[comparable_base].add(_product_key_tuple(identity.key))
    ticker_matches.update(
        next(iter(matches)) for matches in suffixed_matches.values() if len(matches) == 1
    )
    if len(ticker_matches) == 1:
        key = next(iter(ticker_matches))
        return PendingBindingMethod.UNIQUE_TICKER, (by_key[key].key,)
    if len(ticker_matches) > 1:
        return PendingBindingMethod.AMBIGUOUS, tuple(
            by_key[key].key
            for key in sorted(
                ticker_matches,
                key=lambda value: (_FAMILY_PRIORITY[ProductFamily(value[0])], value[1]),
            )
        )

    canonical = _match_names(named_pool, folded_header, attribute="canonical_name")
    short_names = _match_names(named_pool, folded_header, attribute="short_name")
    header_matches = {
        key: set(canonical.get(key, ())).union(short_names.get(key, ()))
        for key in set(canonical).union(short_names)
    }
    if header_matches:
        survivors = _contained_name_survivors(header_matches, folded_header)
        if len(survivors) == 1:
            key = survivors[0]
            method = (
                PendingBindingMethod.UNIQUE_CANONICAL_NAME
                if key in canonical
                else PendingBindingMethod.UNIQUE_SHORT_NAME_PUBLISHER
            )
            return method, (by_key[key].key,)
        return PendingBindingMethod.AMBIGUOUS, tuple(by_key[key].key for key in survivors)

    folded_cover = _fold(cover[:_COVER_TEXT_CHARS])
    cover_matches = _match_names(named_pool, folded_cover, attribute="canonical_name")
    short_cover = _match_names(named_pool, folded_cover, attribute="short_name")
    for key, values in short_cover.items():
        cover_matches.setdefault(key, set()).update(values)
    if cover_matches:
        survivors = _contained_name_survivors(cover_matches, folded_cover)
        if not survivors:
            survivors = tuple(
                sorted(
                    cover_matches,
                    key=lambda value: (
                        _family_order(ProductFamily(value[0])),
                        value[1],
                    ),
                )
            )
        return PendingBindingMethod.AMBIGUOUS, tuple(by_key[key].key for key in survivors)
    return PendingBindingMethod.UNBOUND, ()


def load_verified_pending_texts(
    manifest: PendingCorpusManifest,
    root: str | Path,
) -> dict[str, str]:
    """Load the exact pending text bytes without granting retrieval authority."""

    corpus_root = Path(root)
    if not corpus_root.is_absolute() or corpus_root.is_symlink() or not corpus_root.is_dir():
        raise CorpusCoverageError("pending corpus root is unsafe")
    resolved_root = corpus_root.resolve()
    texts: dict[str, str] = {}
    for document in manifest.documents:
        relative = Path(document.text_relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise CorpusCoverageError("pending text relative path is unsafe")
        target = resolved_root / relative
        if target.is_symlink():
            raise CorpusCoverageError("pending text cannot be a symbolic link")
        resolved = target.resolve()
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise CorpusCoverageError("pending text escaped its corpus root")
        data = resolved.read_bytes()
        if (
            not data
            or len(data) > _MAX_PENDING_TEXT_BYTES
            or len(data) != document.text_size_bytes
            or hashlib.sha256(data).hexdigest() != document.text_sha256
        ):
            raise CorpusCoverageError("pending text differs from its manifest")
        try:
            texts[document.document_id] = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CorpusCoverageError("pending text is not UTF-8") from error
    return texts


def _pending_bindings(
    *,
    manifest: PendingCorpusManifest,
    texts: Mapping[str, str],
    identities: Sequence[_Identity],
    registry: OfficialSourceRegistry,
    warnings: list[CoverageWarning],
) -> tuple[DocumentProductBinding, ...]:
    registry_mismatch = manifest.source_registry_sha256 != registry.canonical_sha256
    if registry_mismatch:
        warnings.append(
            _warning(
                "pending_registry_hash_mismatch",
                manifest.catalog_id,
                "pending manifest was collected under a different Source Registry hash",
            )
        )
    if manifest.evidence_cutoff != registry.evidence_cutoff:
        warnings.append(
            _warning(
                "pending_cutoff_mismatch",
                manifest.catalog_id,
                "pending manifest cutoff differs from the current Source Registry cutoff",
            )
        )
    bindings: list[DocumentProductBinding] = []
    for document in manifest.documents:
        rejected_cutoff = document.as_of > registry.evidence_cutoff or (
            document.published_at is not None and document.published_at > registry.evidence_cutoff
        )
        try:
            entry = registry.require_source_id(document.source_registry_id)
            if document.publisher != entry.publisher:
                raise OfficialSourceRegistryError("pending publisher differs from registry")
            for capability in document.capabilities:
                authorized = registry.authorize_url(document.source_uri, capability)
                if authorized.source_id != entry.source_id:
                    raise OfficialSourceRegistryError("pending source ID differs")
        except OfficialSourceRegistryError as error:
            warnings.append(
                _warning("pending_authority_mismatch", document.document_id, str(error))
            )
            for capability in document.capabilities:
                bindings.append(
                    DocumentProductBinding(
                        document_id=document.document_id,
                        source_registry_id=document.source_registry_id,
                        publisher=document.publisher,
                        capability=capability,
                        authority=CoverageAuthority.PENDING_REVIEW,
                        binding_method=PendingBindingMethod.REJECTED_AUTHORITY,
                        reason_code="source_registry_authority_mismatch",
                    )
                )
            continue
        if rejected_cutoff:
            warnings.append(
                _warning(
                    "pending_document_after_cutoff",
                    document.document_id,
                    "pending document date exceeds the current evidence cutoff",
                )
            )
        for capability in document.capabilities:
            if rejected_cutoff:
                method = PendingBindingMethod.REJECTED_CUTOFF
                products: tuple[ProductCoverageKey, ...] = ()
            else:
                method, products = _resolve_pending_binding(
                    identities=identities,
                    entry=entry,
                    capability=capability,
                    header=f"{document.title}\n{document.source_uri}",
                    cover=texts.get(document.document_id, ""),
                )
            reason = None
            if registry_mismatch:
                reason = "pending_registry_hash_mismatch"
            elif method is PendingBindingMethod.AMBIGUOUS:
                reason = "product_identity_requires_manual_review"
            elif method is PendingBindingMethod.UNBOUND:
                reason = "no_exact_provided_product_identity"
            elif method is PendingBindingMethod.ENTITY_ONLY:
                reason = "capability_has_no_product_family_binding"
            elif method is PendingBindingMethod.REJECTED_CUTOFF:
                reason = "document_after_evidence_cutoff"
            bindings.append(
                DocumentProductBinding(
                    document_id=document.document_id,
                    source_registry_id=document.source_registry_id,
                    publisher=document.publisher,
                    capability=capability,
                    authority=CoverageAuthority.PENDING_REVIEW,
                    binding_method=method,
                    candidate_products=products,
                    reason_code=reason,
                )
            )
    return tuple(bindings)


def _relation_document_bindings(
    *,
    relation_input: RelationBundleInput,
    authority: CoverageAuthority,
    identities_by_key: Mapping[tuple[str, str], _Identity],
    registry: OfficialSourceRegistry,
    coverage: ApprovedKnowledgeCoverageManifest,
    warnings: list[CoverageWarning],
    entity_accumulator: dict[
        tuple[str, str, str, str, str],
        tuple[set[str], set[str], set[str]],
    ],
) -> tuple[DocumentProductBinding, ...]:
    bundle = relation_input.bundle
    registry_mismatch = bundle.manifest.source_registry_sha256 != registry.canonical_sha256
    cutoff_mismatch = bundle.manifest.evidence_cutoff != registry.evidence_cutoff
    approved_corpus_hashes = {
        entry.corpus_sha256
        for entry in coverage.capabilities
        if entry.status == "approved" and entry.runtime_execution_enabled
    }
    corpus_mismatch = bundle.manifest.corpus_manifest_sha256 not in approved_corpus_hashes
    prefix = "legacy" if authority is CoverageAuthority.LEGACY_LIVE_CANDIDATE else "active"
    if registry_mismatch:
        warnings.append(
            _warning(
                f"{prefix}_registry_hash_mismatch",
                relation_input.source_id,
                "relation bundle Source Registry hash differs from the current registry",
            )
        )
    if cutoff_mismatch:
        warnings.append(
            _warning(
                f"{prefix}_cutoff_mismatch",
                relation_input.source_id,
                "relation bundle evidence cutoff differs from the current cutoff",
            )
        )
    if corpus_mismatch:
        warnings.append(
            _warning(
                f"{prefix}_corpus_hash_mismatch",
                relation_input.source_id,
                "relation bundle corpus hash is not an enabled approved corpus hash",
            )
        )

    active_bundle_authorized = authority is CoverageAuthority.ACTIVE_VERIFIED
    if active_bundle_authorized and (registry_mismatch or cutoff_mismatch):
        return ()
    if active_bundle_authorized:
        try:
            TemporalRelationStore(bundle, registry)
        except TemporalRelationError as error:
            warnings.append(
                _warning(
                    "active_relation_verification_failed",
                    relation_input.source_id,
                    str(error),
                )
            )
            return ()

    bindings: list[DocumentProductBinding] = []
    for relation in bundle.relations:
        dates = (
            relation.observed_at,
            *(item.as_of for item in relation.evidence),
            *(item.published_at for item in relation.evidence if item.published_at is not None),
        )
        if any(item > registry.evidence_cutoff for item in dates):
            warnings.append(
                _warning(
                    f"{prefix}_relation_after_cutoff",
                    relation.relation_id,
                    "relation or evidence date exceeds the current evidence cutoff",
                )
            )
            continue
        reason_codes: list[str] = []
        if registry_mismatch:
            reason_codes.append("registry_hash_mismatch")
        if corpus_mismatch:
            reason_codes.append("corpus_hash_mismatch")
        if authority is CoverageAuthority.ACTIVE_VERIFIED:
            entry = coverage.require(relation.capability)
            if (
                entry.status != "approved"
                or not entry.runtime_execution_enabled
                or entry.corpus_sha256 != bundle.manifest.corpus_manifest_sha256
                or entry.index_sha256 is None
            ):
                warnings.append(
                    _warning(
                        "active_capability_authority_mismatch",
                        relation.relation_id,
                        "relation capability is not bound to the enabled approved cache",
                    )
                )
                continue

        if relation.product_family is not None and relation.product_id is not None:
            raw_key = (relation.product_family.value, relation.product_id)
            identity = identities_by_key.get(raw_key)
            if identity is None:
                warnings.append(
                    _warning(
                        "relation_product_outside_provided_db",
                        relation.relation_id,
                        f"relation product is outside provided DB: {raw_key[0]}:{raw_key[1]}",
                    )
                )
                continue
            for evidence in relation.evidence:
                bindings.append(
                    DocumentProductBinding(
                        document_id=evidence.document_id,
                        source_registry_id=evidence.source_registry_id,
                        publisher=evidence.publisher,
                        capability=relation.capability,
                        authority=authority,
                        binding_method=PendingBindingMethod.VERIFIED_TEMPORAL_RELATION,
                        candidate_products=(identity.key,),
                        reason_code=("+".join(sorted(reason_codes)) or None),
                    )
                )
            continue

        entities = (relation.subject, relation.object, *relation.scope_entities)
        for entity in entities:
            _accumulate_entity(
                entity_accumulator,
                capability=relation.capability,
                authority=authority,
                entity=entity,
                relation_id=relation.relation_id,
                document_ids={item.document_id for item in relation.evidence},
                source_registry_ids={item.source_registry_id for item in relation.evidence},
            )
    return tuple(bindings)


def _accumulate_entity(
    accumulator: dict[
        tuple[str, str, str, str, str],
        tuple[set[str], set[str], set[str]],
    ],
    *,
    capability: KnowledgeCapability,
    authority: CoverageAuthority,
    entity: TemporalEntity,
    relation_id: str,
    document_ids: set[str],
    source_registry_ids: set[str],
) -> None:
    key = (
        capability.value,
        authority.value,
        entity.kind.value,
        entity.entity_id,
        entity.canonical_name,
    )
    relation_ids, documents, sources = accumulator.setdefault(key, (set(), set(), set()))
    relation_ids.add(relation_id)
    documents.update(document_ids)
    sources.update(source_registry_ids)


def _entity_matrix(
    accumulator: Mapping[
        tuple[str, str, str, str, str],
        tuple[set[str], set[str], set[str]],
    ],
) -> tuple[EntityCoverageMatrixRow, ...]:
    rows = []
    for key in sorted(accumulator):
        capability, authority, kind, entity_id, name = key
        relations, documents, sources = accumulator[key]
        rows.append(
            EntityCoverageMatrixRow(
                capability=KnowledgeCapability(capability),
                authority=CoverageAuthority(authority),
                entity_kind=kind,
                entity_id=entity_id,
                canonical_name=name,
                relation_count=len(relations),
                document_count=len(documents),
                source_registry_ids=tuple(sorted(sources)),
            )
        )
    return tuple(rows)


def _pending_entity_document_summary(
    bindings: Sequence[DocumentProductBinding],
) -> tuple[PendingEntityDocumentSummary, ...]:
    grouped: dict[tuple[str, str, str], set[KnowledgeCapability]] = defaultdict(set)
    for binding in bindings:
        if (
            binding.authority is CoverageAuthority.PENDING_REVIEW
            and binding.binding_method is PendingBindingMethod.ENTITY_ONLY
        ):
            grouped[
                (
                    binding.document_id,
                    binding.source_registry_id,
                    binding.publisher,
                )
            ].add(binding.capability)
    return tuple(
        PendingEntityDocumentSummary(
            document_id=document_id,
            source_registry_id=source_id,
            publisher=publisher,
            capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        )
        for (document_id, source_id, publisher), capabilities in sorted(grouped.items())
    )


def _deduplicate_bindings(
    bindings: Sequence[DocumentProductBinding],
) -> tuple[DocumentProductBinding, ...]:
    by_payload = {canonical_json_bytes(item.model_dump(mode="json")): item for item in bindings}
    return tuple(
        by_payload[key]
        for key in sorted(
            by_payload,
            key=lambda payload: (
                json.loads(payload)["document_id"],
                json.loads(payload)["capability"],
                json.loads(payload)["authority"],
                payload,
            ),
        )
    )


def _source_scope_candidates(
    identity: _Identity,
    capability: KnowledgeCapability,
    registry: OfficialSourceRegistry,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.source_id
            for entry in _select_source_publisher_group(
                identity,
                capability,
                registry,
            )
        )
    )


def _binding_product_pairs(
    bindings: Sequence[DocumentProductBinding],
    *,
    authority: CoverageAuthority,
    exact_pending_only: bool = False,
) -> dict[tuple[str, str, str], set[str]]:
    exact_pending = {
        PendingBindingMethod.EXACT_PRODUCT_ID,
        PendingBindingMethod.EXACT_ISIN,
        PendingBindingMethod.UNIQUE_TICKER,
        PendingBindingMethod.UNIQUE_CANONICAL_NAME,
        PendingBindingMethod.UNIQUE_SHORT_NAME_PUBLISHER,
    }
    pairs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for binding in bindings:
        if binding.authority is not authority:
            continue
        if exact_pending_only and binding.binding_method not in exact_pending:
            continue
        for product in binding.candidate_products:
            pairs[(product.product_family.value, product.product_id, binding.capability.value)].add(
                binding.document_id
            )
    return pairs


def _ambiguous_pending_pairs(
    bindings: Sequence[DocumentProductBinding],
) -> dict[tuple[str, str, str], set[str]]:
    pairs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for binding in bindings:
        if (
            binding.authority is not CoverageAuthority.PENDING_REVIEW
            or binding.binding_method is not PendingBindingMethod.AMBIGUOUS
        ):
            continue
        for product in binding.candidate_products:
            pairs[(product.product_family.value, product.product_id, binding.capability.value)].add(
                binding.document_id
            )
    return pairs


def _build_product_matrix(
    *,
    identities: Sequence[_Identity],
    registry: OfficialSourceRegistry,
    bindings: Sequence[DocumentProductBinding],
) -> tuple[ProductCoverageMatrixRow, ...]:
    active = _binding_product_pairs(bindings, authority=CoverageAuthority.ACTIVE_VERIFIED)
    pending = _binding_product_pairs(
        bindings,
        authority=CoverageAuthority.PENDING_REVIEW,
        exact_pending_only=True,
    )
    legacy = _binding_product_pairs(
        bindings,
        authority=CoverageAuthority.LEGACY_LIVE_CANDIDATE,
    )
    grouped: dict[tuple[ProductFamily, str], list[_Identity]] = defaultdict(list)
    for identity in identities:
        grouped[(identity.key.product_family, identity.owner)].append(identity)
    rows: list[ProductCoverageMatrixRow] = []
    for family, owner in sorted(
        grouped,
        key=lambda item: (_family_order(item[0]), normalize_selection_surface(item[1])),
    ):
        products = grouped[(family, owner)]
        product_ids = {item.key.product_id for item in products}
        for capability in sorted(
            _APPLICABLE_CAPABILITIES[family], key=lambda item: _CAPABILITY_PRIORITY[item]
        ):
            active_products = {
                product_id
                for product_id in product_ids
                if (family.value, product_id, capability.value) in active
            }
            pending_products = {
                product_id
                for product_id in product_ids
                if (family.value, product_id, capability.value) in pending
            }
            legacy_products = {
                product_id
                for product_id in product_ids
                if (family.value, product_id, capability.value) in legacy
            }
            scoped_products = {
                item.key.product_id
                for item in products
                if _source_scope_candidates(item, capability, registry)
            }
            active_documents = {
                document_id
                for product_id in active_products
                for document_id in active[(family.value, product_id, capability.value)]
            }
            pending_documents = {
                document_id
                for product_id in pending_products
                for document_id in pending[(family.value, product_id, capability.value)]
            }
            legacy_documents = {
                document_id
                for product_id in legacy_products
                for document_id in legacy[(family.value, product_id, capability.value)]
            }
            total = len(products)
            rows.append(
                ProductCoverageMatrixRow(
                    product_family=family,
                    source_owner=owner,
                    capability=capability,
                    total_products=total,
                    active_covered_products=len(active_products),
                    pending_review_products=len(pending_products),
                    legacy_live_candidate_products=len(legacy_products),
                    source_scope_candidate_products=len(scoped_products),
                    active_document_count=len(active_documents),
                    pending_document_count=len(pending_documents),
                    legacy_document_count=len(legacy_documents),
                    active_coverage_ratio=round(len(active_products) / total, 8) if total else 0.0,
                )
            )
    return tuple(rows)


def _binding_sources(
    bindings: Sequence[DocumentProductBinding],
    *,
    authority: CoverageAuthority,
    method: PendingBindingMethod | None = None,
) -> dict[tuple[str, str, str], set[str]]:
    sources: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for binding in bindings:
        if binding.authority is not authority:
            continue
        if method is not None and binding.binding_method is not method:
            continue
        for product in binding.candidate_products:
            key = (
                product.product_family.value,
                product.product_id,
                binding.capability.value,
            )
            sources[key].add(binding.source_registry_id)
    return sources


def _binding_reasons(
    bindings: Sequence[DocumentProductBinding],
    *,
    authority: CoverageAuthority,
) -> dict[tuple[str, str, str], set[str]]:
    reasons: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for binding in bindings:
        if binding.authority is not authority or not binding.reason_code:
            continue
        for product in binding.candidate_products:
            key = (product.product_family.value, product.product_id, binding.capability.value)
            reasons[key].update(binding.reason_code.split("+"))
    return reasons


def _gap_task_id(
    identity: _Identity,
    capability: KnowledgeCapability,
    action: GapAction,
) -> str:
    payload = {
        "action": action.value,
        "capability": capability.value,
        "product_family": identity.key.product_family.value,
        "product_id": identity.key.product_id,
    }
    return f"gap-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:16]}"


def _gap_item(
    *,
    identity: _Identity,
    capability: KnowledgeCapability,
    action: GapAction,
    reason_codes: set[str],
    source_registry_ids: set[str],
) -> GapCollectionItem:
    sources = tuple(sorted(source_registry_ids))
    queries: tuple[str, ...] = ()
    if action is GapAction.COLLECT_OFFICIAL_SNAPSHOT:
        queries = (f'"{identity.record.product_name}" {_CAPABILITY_QUERY_TERMS[capability]}',)
    return GapCollectionItem(
        task_id=_gap_task_id(identity, capability, action),
        product_family=identity.key.product_family,
        product_id=identity.key.product_id,
        product_name=identity.record.product_name,
        source_owner=identity.owner,
        capability=capability,
        action=action,
        reason_codes=tuple(sorted(reason_codes)),
        source_registry_ids=sources,
        queries=queries,
    )


def _round_robin_sequences(
    sequences: Sequence[Sequence[GapCollectionItem]],
) -> list[GapCollectionItem]:
    ordered = tuple(sequence for sequence in sequences if sequence)
    output: list[GapCollectionItem] = []
    depth = 0
    while any(depth < len(sequence) for sequence in ordered):
        output.extend(sequence[depth] for sequence in ordered if depth < len(sequence))
        depth += 1
    return output


def _balanced_gap_items(items: Sequence[GapCollectionItem]) -> list[GapCollectionItem]:
    by_family: dict[ProductFamily, list[GapCollectionItem]] = defaultdict(list)
    for item in items:
        by_family[item.product_family].append(item)
    family_sequences: list[list[GapCollectionItem]] = []
    for family in sorted(by_family, key=_family_order):
        by_capability: dict[KnowledgeCapability, list[GapCollectionItem]] = defaultdict(list)
        for item in by_family[family]:
            by_capability[item.capability].append(item)
        capability_sequences: list[list[GapCollectionItem]] = []
        for capability in sorted(by_capability, key=lambda item: _CAPABILITY_PRIORITY[item]):
            by_source: dict[tuple[str, ...], list[GapCollectionItem]] = defaultdict(list)
            for item in by_capability[capability]:
                source_key = item.source_registry_ids or (
                    f"owner:{normalize_selection_surface(item.source_owner)}",
                )
                by_source[source_key].append(item)
            source_sequences: list[list[GapCollectionItem]] = []
            for source_key in sorted(by_source):
                by_action: dict[GapAction, list[GapCollectionItem]] = defaultdict(list)
                for item in by_source[source_key]:
                    by_action[item.action].append(item)
                action_sequences = []
                for action in sorted(by_action, key=lambda item: item.value):
                    action_sequences.append(
                        sorted(
                            by_action[action],
                            key=lambda item: (item.product_id, item.task_id),
                        )
                    )
                source_sequences.append(_round_robin_sequences(action_sequences))
            capability_sequences.append(_round_robin_sequences(source_sequences))
        family_sequences.append(_round_robin_sequences(capability_sequences))
    return _round_robin_sequences(family_sequences)


def _pending_document_review_items(
    bindings: Sequence[DocumentProductBinding],
) -> tuple[PendingDocumentReviewItem, ...]:
    grouped: dict[
        tuple[str, str, str, PendingBindingMethod],
        tuple[set[KnowledgeCapability], set[str]],
    ] = {}
    for binding in bindings:
        if (
            binding.authority is not CoverageAuthority.PENDING_REVIEW
            or binding.candidate_products
            or binding.binding_method
            not in {PendingBindingMethod.UNBOUND, PendingBindingMethod.ENTITY_ONLY}
        ):
            continue
        key = (
            binding.document_id,
            binding.source_registry_id,
            binding.publisher,
            binding.binding_method,
        )
        capabilities, reasons = grouped.setdefault(key, (set(), set()))
        capabilities.add(binding.capability)
        reasons.add(binding.reason_code or "pending_document_identity_unresolved")
    items = []
    for (document_id, source_id, publisher, method), (capabilities, reasons) in grouped.items():
        action = (
            GapAction.REVIEW_EXISTING_DOCUMENT
            if method is PendingBindingMethod.UNBOUND
            else GapAction.MANUAL_ENTITY_REVIEW
        )
        payload = {
            "action": action.value,
            "document_id": document_id,
            "source_registry_id": source_id,
        }
        items.append(
            PendingDocumentReviewItem(
                task_id=(
                    f"gap-document-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:16]}"
                ),
                document_id=document_id,
                source_registry_id=source_id,
                publisher=publisher,
                capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
                action=action,
                binding_method=method,
                reason_codes=tuple(sorted(reasons)),
            )
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (
                0 if item.action is GapAction.REVIEW_EXISTING_DOCUMENT else 1,
                item.source_registry_id,
                item.document_id,
            ),
        )
    )


def _build_gap_queue(
    *,
    identities: Sequence[_Identity],
    registry: OfficialSourceRegistry,
    bindings: Sequence[DocumentProductBinding],
    max_gap_items: int,
) -> GapCollectionQueue:
    if not 1 <= max_gap_items <= 20_000:
        raise CorpusCoverageError("max_gap_items must be between 1 and 20000")
    active = _binding_product_pairs(bindings, authority=CoverageAuthority.ACTIVE_VERIFIED)
    pending = _binding_product_pairs(
        bindings,
        authority=CoverageAuthority.PENDING_REVIEW,
        exact_pending_only=True,
    )
    legacy = _binding_product_pairs(
        bindings,
        authority=CoverageAuthority.LEGACY_LIVE_CANDIDATE,
    )
    ambiguous = _ambiguous_pending_pairs(bindings)
    pending_sources = _binding_sources(
        bindings,
        authority=CoverageAuthority.PENDING_REVIEW,
    )
    legacy_sources = _binding_sources(
        bindings,
        authority=CoverageAuthority.LEGACY_LIVE_CANDIDATE,
    )
    ambiguous_sources = _binding_sources(
        bindings,
        authority=CoverageAuthority.PENDING_REVIEW,
        method=PendingBindingMethod.AMBIGUOUS,
    )
    pending_reasons = _binding_reasons(
        bindings,
        authority=CoverageAuthority.PENDING_REVIEW,
    )
    legacy_reasons = _binding_reasons(
        bindings,
        authority=CoverageAuthority.LEGACY_LIVE_CANDIDATE,
    )

    by_family: dict[ProductFamily, list[_Identity]] = defaultdict(list)
    for identity in identities:
        by_family[identity.key.product_family].append(identity)
    all_items: list[GapCollectionItem] = []
    total_gap_count = 0
    action_counts: Counter[str] = Counter()
    for family in sorted(by_family, key=_family_order):
        family_products = sorted(
            by_family[family],
            key=lambda item: (normalize_selection_surface(item.owner), item.key.product_id),
        )
        for capability in sorted(
            _APPLICABLE_CAPABILITIES[family], key=lambda item: _CAPABILITY_PRIORITY[item]
        ):
            for identity in family_products:
                key = (family.value, identity.key.product_id, capability.value)
                if key in active:
                    continue
                scope_sources = set(_source_scope_candidates(identity, capability, registry))
                reasons: set[str]
                sources: set[str]
                if key in pending:
                    action = GapAction.REVIEW_EXISTING_DOCUMENT
                    reasons = {"authority_not_active", *pending_reasons.get(key, set())}
                    sources = set(pending_sources.get(key, set()))
                elif key in legacy:
                    action = GapAction.REBUILD_OR_RESEAL_LEGACY_ARTIFACT
                    reasons = {"authority_not_active", *legacy_reasons.get(key, set())}
                    sources = set(legacy_sources.get(key, set()))
                elif key in ambiguous:
                    action = GapAction.MANUAL_ENTITY_REVIEW
                    reasons = {"ambiguous_binding"}
                    sources = set(ambiguous_sources.get(key, set()))
                elif scope_sources:
                    action = GapAction.COLLECT_OFFICIAL_SNAPSHOT
                    reasons = {"authorized_source_no_document"}
                    sources = scope_sources
                else:
                    action = GapAction.EXPAND_SOURCE_REGISTRY
                    reasons = {"no_authorized_source"}
                    sources = set()
                    if identity.owner == _UNKNOWN_OWNER:
                        reasons.add("fund_manager_unavailable")
                total_gap_count += 1
                action_counts[action.value] += 1
                all_items.append(
                    _gap_item(
                        identity=identity,
                        capability=capability,
                        action=action,
                        reason_codes=reasons,
                        source_registry_ids=sources,
                    )
                )
    reuse_actions = {
        GapAction.REVIEW_EXISTING_DOCUMENT,
        GapAction.REBUILD_OR_RESEAL_LEGACY_ARTIFACT,
    }
    reuse = _balanced_gap_items([item for item in all_items if item.action in reuse_actions])
    new_work = _balanced_gap_items(
        [
            item
            for item in all_items
            if item.action
            in {GapAction.COLLECT_OFFICIAL_SNAPSHOT, GapAction.EXPAND_SOURCE_REGISTRY}
        ]
    )
    manual = tuple(
        _balanced_gap_items(
            [item for item in all_items if item.action is GapAction.MANUAL_ENTITY_REVIEW]
        )
    )
    document_reviews = _pending_document_review_items(bindings)
    total_gap_count += len(document_reviews)
    action_counts.update(item.action.value for item in document_reviews)
    selected_reuse = tuple(reuse[:max_gap_items])
    remaining = max_gap_items - len(selected_reuse)
    selected_document_reviews = document_reviews[:remaining]
    remaining -= len(selected_document_reviews)
    selected_new_work = tuple(new_work[:remaining])
    items = (*selected_reuse, *selected_new_work)
    represented_count = len(items) + len(selected_document_reviews) + len(manual)
    return GapCollectionQueue(
        max_gap_items=max_gap_items,
        total_gap_count=total_gap_count,
        returned_gap_count=represented_count,
        truncated=total_gap_count > represented_count,
        action_counts=dict(sorted(action_counts.items())),
        items=items,
        document_review_items=selected_document_reviews,
        manual_review_items=manual,
        network_collection_task_ids=tuple(
            item.task_id for item in items if item.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT
        ),
    )


def build_corpus_coverage_analysis(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    *,
    datasets: Sequence[DatasetCoverageReceipt],
    registry: OfficialSourceRegistry,
    coverage: ApprovedKnowledgeCoverageManifest,
    dataset_release_id: str | None = None,
    pending_manifest: PendingCorpusManifest | None = None,
    pending_texts: Mapping[str, str] | None = None,
    active_relations: Sequence[RelationBundleInput] = (),
    legacy_relations: Sequence[RelationBundleInput] = (),
    max_gap_items: int = 500,
) -> tuple[CorpusCoverageAnalysis, GapCollectionQueue]:
    """Build an analysis-only matrix without changing Runtime authority.

    Only relations that pass the current Source Registry and approved cache
    binding become the active numerator. Pending documents, legacy graphs and
    manager/source scopes stay visible but never contribute active coverage.
    """

    identities = _eligible_identities(records_by_family)
    actual_dataset_release_id = dataset_release_id or coverage.dataset_release_id
    identities_by_key = {_product_key_tuple(item.key): item for item in identities}
    dataset_rows = tuple(sorted(datasets, key=lambda item: _family_order(item.product_family)))
    if len({item.product_family for item in dataset_rows}) != len(dataset_rows):
        raise CorpusCoverageError("dataset coverage receipts must be unique by family")
    actual_counts = Counter(item.key.product_family for item in identities)
    for dataset in dataset_rows:
        if dataset.product_count != actual_counts[dataset.product_family]:
            raise CorpusCoverageError("dataset receipt product count differs from identities")

    warnings: list[CoverageWarning] = []
    if actual_dataset_release_id != coverage.dataset_release_id:
        warnings.append(
            _warning(
                "coverage_dataset_release_mismatch",
                coverage.manifest_id,
                "knowledge coverage dataset release "
                f"{coverage.dataset_release_id} differs from analyzed release "
                f"{actual_dataset_release_id}",
            )
        )
    bindings: list[DocumentProductBinding] = []
    if pending_manifest is not None:
        supplied_texts = pending_texts or {}
        unknown_text_ids = set(supplied_texts) - {
            item.document_id for item in pending_manifest.documents
        }
        if unknown_text_ids:
            raise CorpusCoverageError("pending text mapping contains an unknown document")
        if len(supplied_texts) != len(pending_manifest.documents):
            warnings.append(
                _warning(
                    "pending_text_not_supplied",
                    pending_manifest.catalog_id,
                    "one or more pending documents were matched from title and URL only",
                )
            )
        bindings.extend(
            _pending_bindings(
                manifest=pending_manifest,
                texts=supplied_texts,
                identities=identities,
                registry=registry,
                warnings=warnings,
            )
        )

    entity_accumulator: dict[
        tuple[str, str, str, str, str],
        tuple[set[str], set[str], set[str]],
    ] = {}
    for relation_input in active_relations:
        bindings.extend(
            _relation_document_bindings(
                relation_input=relation_input,
                authority=CoverageAuthority.ACTIVE_VERIFIED,
                identities_by_key=identities_by_key,
                registry=registry,
                coverage=coverage,
                warnings=warnings,
                entity_accumulator=entity_accumulator,
            )
        )
    for relation_input in legacy_relations:
        bindings.extend(
            _relation_document_bindings(
                relation_input=relation_input,
                authority=CoverageAuthority.LEGACY_LIVE_CANDIDATE,
                identities_by_key=identities_by_key,
                registry=registry,
                coverage=coverage,
                warnings=warnings,
                entity_accumulator=entity_accumulator,
            )
        )

    frozen_bindings = _deduplicate_bindings(bindings)
    matrix = _build_product_matrix(
        identities=identities,
        registry=registry,
        bindings=frozen_bindings,
    )
    entity_matrix = _entity_matrix(entity_accumulator)
    pending_entity_documents = _pending_entity_document_summary(frozen_bindings)
    frozen_warnings = _deduplicate_warnings(warnings)
    pending_sha = pending_manifest.canonical_sha256 if pending_manifest is not None else None
    queue = _build_gap_queue(
        identities=identities,
        registry=registry,
        bindings=frozen_bindings,
        max_gap_items=max_gap_items,
    )
    result_payload = {
        "bindings": [item.model_dump(mode="json") for item in frozen_bindings],
        "entity_matrix": [item.model_dump(mode="json") for item in entity_matrix],
        "gap_queue": queue.model_dump(mode="json"),
        "pending_entity_documents": [
            item.model_dump(mode="json") for item in pending_entity_documents
        ],
        "product_matrix": [item.model_dump(mode="json") for item in matrix],
        "warnings": [item.model_dump(mode="json") for item in frozen_warnings],
    }
    input_payload = {
        "algorithm_version": "coverage-matrix-v2",
        "active_relation_files": sorted(item.file_sha256 for item in active_relations),
        "coverage_manifest_sha256": coverage.canonical_sha256,
        "dataset_release_id": actual_dataset_release_id,
        "datasets": [item.model_dump(mode="json") for item in dataset_rows],
        "legacy_relation_files": sorted(item.file_sha256 for item in legacy_relations),
        "pending_manifest_sha256": pending_sha,
        "result_sha256": hashlib.sha256(canonical_json_bytes(result_payload)).hexdigest(),
        "source_registry_sha256": registry.canonical_sha256,
    }
    analysis = CorpusCoverageAnalysis(
        analysis_id=f"coverage-{hashlib.sha256(canonical_json_bytes(input_payload)).hexdigest()[:16]}",
        dataset_release_id=actual_dataset_release_id,
        evidence_cutoff=registry.evidence_cutoff,
        source_registry_id=registry.registry_id,
        source_registry_sha256=registry.canonical_sha256,
        knowledge_coverage_manifest_id=coverage.manifest_id,
        knowledge_coverage_manifest_sha256=coverage.canonical_sha256,
        datasets=dataset_rows,
        pending_manifest_sha256=pending_sha,
        active_relation_file_sha256s=tuple(sorted(item.file_sha256 for item in active_relations)),
        legacy_relation_file_sha256s=tuple(sorted(item.file_sha256 for item in legacy_relations)),
        bindings=frozen_bindings,
        product_matrix=matrix,
        entity_matrix=entity_matrix,
        pending_entity_documents=pending_entity_documents,
        warnings=frozen_warnings,
    )
    return analysis, queue


def coverage_analysis_bytes(analysis: CorpusCoverageAnalysis) -> bytes:
    return canonical_json_bytes(analysis.model_dump(mode="json"))


def gap_collection_queue_bytes(queue: GapCollectionQueue) -> bytes:
    return canonical_json_bytes(queue.model_dump(mode="json"))


def coverage_matrix_csv_bytes(analysis: CorpusCoverageAnalysis) -> bytes:
    output = io.StringIO(newline="")
    fieldnames = (
        "product_family",
        "source_owner",
        "capability",
        "total_products",
        "active_covered_products",
        "pending_review_products",
        "legacy_live_candidate_products",
        "source_scope_candidate_products",
        "active_document_count",
        "pending_document_count",
        "legacy_document_count",
        "active_coverage_ratio",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in analysis.product_matrix:
        writer.writerow(row.model_dump(mode="json"))
    return output.getvalue().encode("utf-8")


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def coverage_matrix_markdown_bytes(
    analysis: CorpusCoverageAnalysis,
    queue: GapCollectionQueue,
) -> bytes:
    active_pairs = sum(item.active_covered_products for item in analysis.product_matrix)
    pending_pairs = sum(item.pending_review_products for item in analysis.product_matrix)
    legacy_pairs = sum(item.legacy_live_candidate_products for item in analysis.product_matrix)
    lines = [
        "# 공식 외부 근거 Coverage Matrix",
        "",
        f"- 분석 ID: `{analysis.analysis_id}`",
        f"- 데이터 Release: `{analysis.dataset_release_id}`",
        f"- 근거 기준일: `{analysis.evidence_cutoff.isoformat()}`",
        f"- Active Coverage 상품·capability 쌍: **{active_pairs:,}**",
        f"- Pending Review 후보 쌍: **{pending_pairs:,}**",
        f"- Legacy 재봉인 후보 쌍: **{legacy_pairs:,}**",
        f"- 미추출 Pending entity 문서: **{len(analysis.pending_entity_documents):,}**",
        f"- 전체 Gap: **{queue.total_gap_count:,}** (출력 {queue.returned_gap_count:,})",
        "",
        "> Active Coverage는 현재 승인 corpus hash·Source Registry·cutoff와 일치하는 "
        "TemporalRelationRecord만 포함합니다. Pending, Legacy, 운용사 Source scope는 분자에 "
        "포함하지 않습니다.",
        "",
        "## 상품군·운용사·Capability별 Matrix",
        "",
        "| 상품군 | 운용사/발행사 | Capability | 상품 수 | Active | Pending | "
        "Legacy | Source scope | Active 비율 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in analysis.product_matrix:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    row.product_family.value,
                    row.source_owner,
                    row.capability.value,
                    row.total_products,
                    row.active_covered_products,
                    row.pending_review_products,
                    row.legacy_live_candidate_products,
                    row.source_scope_candidate_products,
                    f"{row.active_coverage_ratio:.2%}",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Gap action",
            "",
            "| Action | 전체 건수 |",
            "|---|---:|",
        )
    )
    for action, count in sorted(queue.action_counts.items()):
        lines.append(f"| `{_markdown_cell(action)}` | {count:,} |")
    lines.extend(("", "## 경고", ""))
    if analysis.warnings:
        for warning in analysis.warnings:
            lines.append(f"- `{warning.code}` / `{warning.source_id}`: {warning.detail}")
    else:
        lines.append("- 없음")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_coverage_artifact_receipt(
    analysis: CorpusCoverageAnalysis,
    *,
    output_payloads: Mapping[str, bytes],
) -> CoverageArtifactReceipt:
    input_hashes = {
        "knowledge_coverage_manifest": analysis.knowledge_coverage_manifest_sha256,
        "source_registry": analysis.source_registry_sha256,
        **{
            f"database:{item.product_family.value}": item.database_sha256
            for item in analysis.datasets
        },
        **(
            {}
            if analysis.pending_manifest_sha256 is None
            else {"pending_corpus_manifest": analysis.pending_manifest_sha256}
        ),
        **{
            f"active_relation:{index:03d}": value
            for index, value in enumerate(analysis.active_relation_file_sha256s, start=1)
        },
        **{
            f"legacy_relation:{index:03d}": value
            for index, value in enumerate(analysis.legacy_relation_file_sha256s, start=1)
        },
    }
    return CoverageArtifactReceipt(
        analysis_id=analysis.analysis_id,
        input_sha256s=dict(sorted(input_hashes.items())),
        output_sha256s={
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in sorted(output_payloads.items())
        },
    )


def coverage_artifact_receipt_bytes(receipt: CoverageArtifactReceipt) -> bytes:
    return canonical_json_bytes(receipt.model_dump(mode="json"))


def _write_new_read_only(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o444)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("coverage output write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise CorpusCoverageError(f"coverage output could not be written: {path}") from error


def write_corpus_coverage_artifacts(
    output_directory: str | Path,
    analysis: CorpusCoverageAnalysis,
    queue: GapCollectionQueue,
) -> tuple[CoverageArtifactReceipt, tuple[Path, ...]]:
    output = Path(output_directory)
    if not output.is_absolute() or output.is_symlink() or output.exists():
        raise CorpusCoverageError("coverage output directory must be a new absolute path")
    try:
        output.mkdir(parents=True, mode=0o755)
    except OSError as error:
        raise CorpusCoverageError("coverage output directory could not be created") from error
    payloads = {
        "corpus-coverage-matrix.json": coverage_analysis_bytes(analysis),
        "corpus-coverage-matrix.csv": coverage_matrix_csv_bytes(analysis),
        "corpus-coverage-matrix.md": coverage_matrix_markdown_bytes(analysis, queue),
        "corpus-gap-collection-queue.json": gap_collection_queue_bytes(queue),
    }
    receipt = build_coverage_artifact_receipt(analysis, output_payloads=payloads)
    payloads["corpus-coverage-receipt.json"] = coverage_artifact_receipt_bytes(receipt)
    paths: list[Path] = []
    try:
        for name, payload in sorted(payloads.items()):
            target = output / name
            _write_new_read_only(target, payload)
            paths.append(target)
    except CorpusCoverageError:
        for target in paths:
            target.chmod(0o600)
            target.unlink()
        output.rmdir()
        raise
    return receipt, tuple(paths)


__all__ = [
    "CorpusCoverageAnalysis",
    "CorpusCoverageError",
    "CoverageArtifactReceipt",
    "CoverageAuthority",
    "CoverageWarning",
    "DatasetCoverageReceipt",
    "DocumentProductBinding",
    "EntityCoverageMatrixRow",
    "GapAction",
    "GapCollectionItem",
    "GapCollectionQueue",
    "PendingBindingMethod",
    "PendingDocumentReviewItem",
    "PendingEntityDocumentSummary",
    "ProductCoverageKey",
    "ProductCoverageMatrixRow",
    "RelationBundleInput",
    "build_corpus_coverage_analysis",
    "build_coverage_artifact_receipt",
    "canonical_json_bytes",
    "coverage_analysis_bytes",
    "coverage_artifact_receipt_bytes",
    "coverage_matrix_csv_bytes",
    "coverage_matrix_markdown_bytes",
    "gap_collection_queue_bytes",
    "load_verified_pending_texts",
    "write_corpus_coverage_artifacts",
]
