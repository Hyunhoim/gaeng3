from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from finance_agent_core.agent.source_selection import (
    SourceSelectionSignals,
    normalize_selection_surface,
)
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


def resolve_explicit_candidate_ids(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    identity_surfaces: Sequence[str],
) -> dict[ProductFamily, set[str]]:
    """Resolve explicit question identities without inventing an external product.

    Every exact DB match is retained.  Unknown surfaces do not widen or shrink
    the universe by themselves because quoted company/theme names may also be
    present in an external-knowledge question.
    """

    normalized_surfaces = {
        normalize_product_identity(value)
        for value in identity_surfaces
        if normalize_product_identity(value)
    }
    if not normalized_surfaces:
        return {}
    resolved: dict[ProductFamily, set[str]] = {}
    for family, records in records_by_family.items():
        for record in records:
            if record.is_quarantined:
                continue
            exact_identities = (record.product_id, record.ticker, record.isin)
            name_identities = (record.product_name, record.short_name)
            exact_match = any(
                value is not None and normalize_product_identity(value) in normalized_surfaces
                for value in exact_identities
            )
            contained_name_match = any(
                value is not None
                and len(normalize_product_identity(value)) >= 5
                and any(
                    normalize_product_identity(value) in surface for surface in normalized_surfaces
                )
                for value in name_identities
            )
            if exact_match or contained_name_match:
                resolved.setdefault(family, set()).add(record.product_id)
    return resolved


def build_provided_candidate_universe(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    *,
    database_sha256s: Mapping[ProductFamily, str],
    candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
    explicit_identity_bound: bool = False,
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
        explicit_identity_bound=explicit_identity_bound,
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
        identity_surfaces: Sequence[str] = (),
    ) -> ProvidedProductCandidateUniverse:
        if not families:
            raise ValueError("external search requires at least one product family")
        approval = load_approved_dataset_manifest()
        records = {
            family: self.identity_cache.get(self.database_paths[family]).records
            for family in sorted(set(families), key=lambda item: item.value)
        }
        explicit_ids = resolve_explicit_candidate_ids(records, identity_surfaces)
        if candidate_ids_by_family is not None and explicit_ids:
            explicit_ids = {
                family: values.intersection(candidate_ids_by_family.get(family, set()))
                for family, values in explicit_ids.items()
            }
        effective_ids = explicit_ids if explicit_ids else candidate_ids_by_family
        hashes = {family: approval.datasets[family.value].database_sha256 for family in records}
        return build_provided_candidate_universe(
            records,
            database_sha256s=hashes,
            candidate_ids_by_family=effective_ids,
            explicit_identity_bound=bool(explicit_ids),
        )

    def source_selection_signals(
        self,
        *,
        families: tuple[ProductFamily, ...],
        question: str,
        identity_surfaces: Sequence[str] = (),
        registry: OfficialSourceRegistry | None = None,
    ) -> SourceSelectionSignals:
        """Derive brand/owner signals from approved DB identities and registry metadata."""

        if not families:
            raise ValueError("source selection requires at least one product family")
        effective_registry = registry or load_official_source_registry()
        records_by_family = {
            family: self.identity_cache.get(self.database_paths[family]).records
            for family in sorted(set(families), key=lambda item: item.value)
        }
        explicit_ids = resolve_explicit_candidate_ids(records_by_family, identity_surfaces)
        normalized_question = normalize_selection_surface(question)
        matched_records: list[ProductIdentityRecord] = []
        all_records = [
            record
            for family in sorted(records_by_family, key=lambda item: item.value)
            for record in records_by_family[family]
            if not record.is_quarantined
        ]
        for record in all_records:
            family = ProductFamily(record.product_family)
            if record.product_id in explicit_ids.get(family, set()):
                matched_records.append(record)
                continue
            identities = (
                record.product_name,
                record.short_name,
                record.ticker,
                record.isin,
            )
            if any(
                value is not None
                and len(normalize_selection_surface(value)) >= 4
                and normalize_selection_surface(value) in normalized_question
                for value in identities
            ):
                matched_records.append(record)

        registry_aliases = {
            alias
            for entry in effective_registry.entries
            for alias in (entry.publisher, *entry.selection_aliases)
            if normalize_selection_surface(alias)
        }
        confirmed_aliases = {
            alias
            for alias in registry_aliases
            if normalize_selection_surface(alias) in normalized_question
            and any(
                normalize_selection_surface(alias)
                in normalize_selection_surface(
                    " ".join(
                        value
                        for value in (record.product_name, record.source_owner)
                        if value is not None
                    )
                )
                for record in all_records
            )
        }
        return SourceSelectionSignals(
            product_families=tuple(sorted(set(families), key=lambda item: item.value)),
            db_confirmed_aliases=tuple(sorted(confirmed_aliases, key=str.casefold)),
            matched_product_names=tuple(
                sorted({record.product_name for record in matched_records}, key=str.casefold)
            ),
            matched_source_owners=tuple(
                sorted(
                    {
                        record.source_owner
                        for record in matched_records
                        if record.source_owner is not None
                    },
                    key=str.casefold,
                )
            ),
            explicit_identity_bound=bool(explicit_ids),
        )


def intersect_evidence_with_provided_products(
    universe: ProvidedProductCandidateUniverse,
    evidence_texts: Sequence[str],
) -> tuple[ProvidedProductCandidate, ...]:
    """Exact alias intersection; external documents cannot create a product."""

    normalized_texts = tuple(normalize_product_identity(text) for text in evidence_texts)
    folded_texts = tuple(unicodedata.normalize("NFKC", text).casefold() for text in evidence_texts)
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
                pattern = re.compile(rf"(?<![0-9a-z]){re.escape(folded_alias)}(?![0-9a-z])")
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
    "resolve_explicit_candidate_ids",
]
