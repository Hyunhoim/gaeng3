from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    ExternalKnowledgeSearchPlan,
    ExternalSearchBudget,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database_paths,
)
from finance_agent_core.storage.identity_cache import (
    ProductIdentityRecord,
    ProductIdentitySnapshotCache,
)


def _canonical_sha256(payload: object) -> str:
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def normalize_product_identity(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _candidate_from_record(record: ProductIdentityRecord) -> ProvidedProductCandidate:
    aliases = tuple(
        sorted(
            {
                value.strip()
                for value in (record.ticker, record.isin, record.short_name)
                if value is not None and value.strip() and value.strip() != record.product_name
            }
        )
    )
    return ProvidedProductCandidate(
        product_family=ProductFamily(record.product_family),
        product_id=record.product_id,
        product_name=record.product_name,
        aliases=aliases,
    )


def build_provided_candidate_universe(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    *,
    database_sha256s: Mapping[ProductFamily, str],
    candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
) -> ProvidedProductCandidateUniverse:
    """Freeze SQL-produced candidates before external evidence can affect results."""

    allowed_ids = candidate_ids_by_family or {}
    candidates = []
    for family in sorted(records_by_family, key=lambda item: item.value):
        if family not in database_sha256s:
            raise ValueError("candidate family lacks an approved database SHA-256")
        family_allowed = allowed_ids.get(family)
        for record in records_by_family[family]:
            if record.is_quarantined:
                continue
            if family is ProductFamily.FUND and record.public_offering is False:
                continue
            if family_allowed is not None and record.product_id not in family_allowed:
                continue
            candidates.append(_candidate_from_record(record))
    candidates.sort(key=lambda item: (item.product_family.value, item.product_id))
    filter_payload = {
        family.value: sorted(values)
        for family, values in sorted(allowed_ids.items(), key=lambda item: item[0].value)
    }
    return ProvidedProductCandidateUniverse(
        candidates=tuple(candidates),
        database_sha256s=dict(database_sha256s),
        candidate_filter_sha256=_canonical_sha256(filter_payload),
    )


class ProvidedProductUniverseBuilder:
    """Load only approved, non-quarantined identities from the four official DBs."""

    def __init__(
        self,
        database_paths: Mapping[ProductFamily | str, str | Path],
        *,
        identity_cache: ProductIdentitySnapshotCache | None = None,
        verify_approved_databases: bool = True,
    ) -> None:
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        if verify_approved_databases:
            require_approved_database_paths(self.database_paths)
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )

    def build(
        self,
        *,
        families: tuple[ProductFamily, ...],
        candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
    ) -> ProvidedProductCandidateUniverse:
        if not families:
            raise ValueError("external search requires at least one product family")
        approval = load_approved_dataset_manifest()
        records = {
            family: self.identity_cache.get(self.database_paths[family]).records
            for family in sorted(set(families), key=lambda item: item.value)
        }
        hashes = {
            family: approval.datasets[family.value].database_sha256 for family in records
        }
        return build_provided_candidate_universe(
            records,
            database_sha256s=hashes,
            candidate_ids_by_family=candidate_ids_by_family,
        )


def intersect_evidence_with_provided_products(
    universe: ProvidedProductCandidateUniverse,
    evidence_texts: Sequence[str],
) -> tuple[ProvidedProductCandidate, ...]:
    """Exact alias intersection; external documents cannot create a product."""

    normalized_texts = tuple(normalize_product_identity(text) for text in evidence_texts)
    folded_texts = tuple(
        unicodedata.normalize("NFKC", text).casefold() for text in evidence_texts
    )
    matches = []
    for candidate in universe.candidates:
        canonical = normalize_product_identity(candidate.product_name)
        canonical_match = len(canonical) >= 3 and any(
            canonical in text for text in normalized_texts
        )
        alias_match = False
        for alias in candidate.aliases:
            folded_alias = unicodedata.normalize("NFKC", alias).casefold().strip()
            normalized_alias = normalize_product_identity(alias)
            if len(normalized_alias) < 4:
                continue
            if folded_alias.isascii() and folded_alias.isalnum():
                pattern = re.compile(
                    rf"(?<![0-9a-z]){re.escape(folded_alias)}(?![0-9a-z])"
                )
                if any(pattern.search(text) is not None for text in folded_texts):
                    alias_match = True
                    break
            elif any(normalized_alias in text for text in normalized_texts):
                alias_match = True
                break
        if canonical_match or alias_match:
            matches.append(candidate)
    return tuple(matches)


class ExternalSearchPlanCompiler:
    """Bind a server-authorized plan to the precomputed DB candidate universe."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()

    def compile(
        self,
        plan: CompositeKnowledgeQueryPlan,
        universe: ProvidedProductCandidateUniverse,
        *,
        budget: ExternalSearchBudget | None = None,
    ) -> ExternalKnowledgeSearchPlan:
        plan_families = {
            family for requirement in plan.requirements for family in requirement.product_families
        }
        universe_families = {item.product_family for item in universe.candidates}
        if universe_families - plan_families:
            raise ValueError("provided DB candidates exceed the Typed QueryPlan families")
        if not plan.execution_authorized:
            plan = CompositeKnowledgeQueryPlan(
                **plan.model_dump(mode="python", exclude={"execution_authorized"}),
                execution_authorized=True,
            )
        return ExternalKnowledgeSearchPlan(
            composite_plan=plan,
            candidate_universe_sha256=universe.canonical_sha256,
            candidate_count=len(universe.candidates),
            source_registry_sha256=self.registry.canonical_sha256,
            budget=budget or ExternalSearchBudget(),
        )


__all__ = [
    "ExternalSearchPlanCompiler",
    "ProvidedProductUniverseBuilder",
    "build_provided_candidate_universe",
    "intersect_evidence_with_provided_products",
    "normalize_product_identity",
]
