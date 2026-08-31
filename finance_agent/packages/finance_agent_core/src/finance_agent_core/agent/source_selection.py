from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    SourceAuthority,
)
from finance_agent_core.contracts.queryplan import ProductFamily


def normalize_selection_surface(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


@dataclass(frozen=True, slots=True)
class SourceSelectionSignals:
    """Server-derived DB signals allowed to influence official source selection."""

    product_families: tuple[ProductFamily, ...]
    db_confirmed_aliases: tuple[str, ...] = ()
    matched_product_names: tuple[str, ...] = ()
    matched_source_owners: tuple[str, ...] = ()
    explicit_identity_bound: bool = False


def _family_compatible(
    entry: OfficialSourceEntry,
    families: tuple[ProductFamily, ...],
) -> bool:
    return (
        not families
        or not entry.product_families
        or bool(set(entry.product_families).intersection(families))
    )


class GeneralizedOfficialSourceSelector:
    """Select sources from registry metadata and provided-DB identity signals only.

    Product or company names never appear in this policy. A new brand, manager,
    issuer, or product family is supported by approved metadata instead of code.
    """

    def __init__(self, registry: OfficialSourceRegistry) -> None:
        self.registry = registry

    def select_source_ids(
        self,
        capability: KnowledgeCapability,
        *,
        question: str,
        subject: str,
        signals: SourceSelectionSignals | None = None,
    ) -> tuple[str, ...]:
        families = () if signals is None else signals.product_families
        eligible = tuple(
            entry
            for entry in self.registry.entries
            if capability in entry.capabilities and _family_compatible(entry, families)
        )
        if not eligible:
            raise ValueError(f"no approved official source for {capability.value}")

        question_surface = normalize_selection_surface(f"{question} {subject}")
        subject_surface = normalize_selection_surface(subject)
        confirmed_aliases = {
            normalize_selection_surface(value)
            for value in (() if signals is None else signals.db_confirmed_aliases)
        }
        db_surfaces = {
            normalize_selection_surface(value)
            for value in (
                ()
                if signals is None
                else (
                    *signals.matched_product_names,
                    *signals.matched_source_owners,
                )
            )
        }
        selected: list[OfficialSourceEntry] = []
        for entry in eligible:
            aliases = {
                normalize_selection_surface(value)
                for value in (entry.publisher, *entry.selection_aliases)
                if normalize_selection_surface(value)
            }
            question_matches = {
                alias
                for alias in aliases
                if alias
                and (
                    alias in question_surface
                    or (len(subject_surface) >= 4 and subject_surface in alias)
                )
            }
            db_matches = {
                alias
                for alias in aliases
                if alias in confirmed_aliases
                or any(alias in surface or surface in alias for surface in db_surfaces)
            }
            if entry.authority in {
                SourceAuthority.ASSET_MANAGER,
                SourceAuthority.PRODUCT_ISSUER,
            }:
                if question_matches.intersection(confirmed_aliases) or db_matches:
                    selected.append(entry)
            elif question_matches:
                selected.append(entry)

        chosen = selected or list(eligible)
        return tuple(sorted({entry.source_id for entry in chosen}))


__all__ = [
    "GeneralizedOfficialSourceSelector",
    "SourceSelectionSignals",
    "normalize_selection_surface",
]
