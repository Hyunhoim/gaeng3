from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeMatchMode,
    KnowledgeProductSelection,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.execution.external_knowledge_verifier import (
    ExternalKnowledgeEvidenceBundle,
    ExternalKnowledgeEvidenceVerifier,
    ExternalKnowledgeVerification,
    OfficialProductFact,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalEntityKind,
    TemporalRelationRecord,
    TemporalRelationSearchRequest,
    TemporalRelationStore,
)


class ExternalKnowledgeExecutionError(RuntimeError):
    """Raised when a composite knowledge plan lacks a complete verified chain."""


class OfficialProductFactProvider(Protocol):
    def fetch(
        self,
        product_keys: tuple[tuple[ProductFamily, str], ...],
        selection: KnowledgeProductSelection | None,
        requested_fields: tuple[str, ...],
    ) -> tuple[OfficialProductFact, ...]: ...


ProductKey = tuple[ProductFamily, str]


def _relation_product_key(relation: TemporalRelationRecord) -> ProductKey | None:
    if relation.product_family is None or relation.product_id is None:
        return None
    return relation.product_family, relation.product_id


def matched_product_keys(
    plan: CompositeKnowledgeQueryPlan,
    requirement_relations: dict[str, tuple[TemporalRelationRecord, ...]],
) -> tuple[ProductKey, ...]:
    """Intersect every independent product relation before DB ranking.

    Holdings have an explicit ALL/ANY set operation because a user may ask for
    products containing all or any of several constituents.  Other independent
    product-producing requirements are conjunctive clauses: a product requested
    as both ``X를 보유`` *and* ``Y 테마와 연결`` must satisfy both.  The old
    implementation stopped at the holding set whenever one existed, allowing a
    theme relation evidenced for a different product to be ignored.

    Corporate relations have no product key and dependent risk requirements are
    evaluated only after this selection, so neither may widen the candidate set.
    """

    holding_sets = [
        {
            key
            for relation in requirement_relations.get(requirement.requirement_id, ())
            if (key := _relation_product_key(relation)) is not None
        }
        for requirement in plan.requirements
        if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    product_sets: list[set[ProductKey]] = []
    if holding_sets:
        if plan.holding_match_mode is KnowledgeMatchMode.ALL:
            product_sets.append(set.intersection(*holding_sets))
        else:
            product_sets.append(set.union(*holding_sets))
    for requirement in plan.requirements:
        if (
            requirement.capability
            in {
                KnowledgeCapability.SECTOR_THEME_HISTORY,
                KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            }
            and not requirement.depends_on
        ):
            product_sets.append(
                {
                    key
                    for relation in requirement_relations.get(requirement.requirement_id, ())
                    if (key := _relation_product_key(relation)) is not None
                }
            )
    if product_sets:
        selected = set.intersection(*product_sets)
    else:
        selected = {
            key
            for relations in requirement_relations.values()
            for relation in relations
            if (key := _relation_product_key(relation)) is not None
        }
    return tuple(sorted(selected, key=lambda item: (item[0].value, item[1])))


def selected_product_keys(
    plan: CompositeKnowledgeQueryPlan,
    requirement_relations: dict[str, tuple[TemporalRelationRecord, ...]],
    product_fact_provider: OfficialProductFactProvider,
) -> tuple[ProductKey, ...]:
    matched = matched_product_keys(plan, requirement_relations)
    if plan.product_selection is None:
        return matched
    facts = product_fact_provider.fetch(matched, plan.product_selection, ())
    selected = {
        (fact.product_family, fact.product_id)
        for fact in facts
        if fact.canonical_field == plan.product_selection.rank_field
    }
    return tuple(sorted(selected, key=lambda item: (item[0].value, item[1])))


class ExternalKnowledgeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: ExternalKnowledgeEvidenceBundle
    verification: ExternalKnowledgeVerification


class ExternalKnowledgeExecutor:
    """Join sealed relations to approved DB facts and run the evidence verifier."""

    def __init__(
        self,
        *,
        relation_store: TemporalRelationStore,
        product_fact_provider: OfficialProductFactProvider,
        approved_product_ids: set[tuple[ProductFamily, str]],
        verifier: ExternalKnowledgeEvidenceVerifier | None = None,
    ) -> None:
        self.relation_store = relation_store
        self.product_fact_provider = product_fact_provider
        self.approved_product_ids = frozenset(approved_product_ids)
        self.verifier = verifier or ExternalKnowledgeEvidenceVerifier()

    @staticmethod
    def _dependency_targets(
        requirement: KnowledgeRequirement,
        requirement_relations: dict[str, tuple[TemporalRelationRecord, ...]],
        *,
        selected_keys: frozenset[ProductKey] = frozenset(),
    ) -> tuple[tuple[str, ProductKey | None], ...]:
        targets: dict[tuple[str, ProductKey | None], tuple[str, ProductKey | None]] = {}
        for dependency in requirement.depends_on:
            for relation in requirement_relations.get(dependency, ()):
                if (
                    requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and relation.product_id is not None
                ):
                    product_key = _relation_product_key(relation)
                    if product_key is None or product_key not in selected_keys:
                        continue
                    product_entities = (
                        item
                        for item in (relation.subject, relation.object)
                        if item.kind is TemporalEntityKind.PRODUCT
                    )
                    for entity in product_entities:
                        target = (entity.canonical_name, product_key)
                        targets[(entity.entity_id, product_key)] = target
                else:
                    target = (relation.object.canonical_name, None)
                    targets[(relation.object.entity_id, None)] = target
        ordered_keys = sorted(targets, key=lambda item: (item[0], str(item[1])))
        return tuple(targets[key] for key in ordered_keys)

    def execute(self, plan: CompositeKnowledgeQueryPlan) -> ExternalKnowledgeExecution:
        resolved: dict[str, tuple[TemporalRelationRecord, ...]] = {}
        all_relations: dict[str, TemporalRelationRecord] = {}
        for requirement in plan.requirements:
            try:
                selected_keys = (
                    frozenset(selected_product_keys(plan, resolved, self.product_fact_provider))
                    if (
                        requirement.depends_on
                        and requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    )
                    else frozenset()
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ExternalKnowledgeExecutionError(
                    "provided product DB could not resolve the requested ranking"
                ) from error
            targets = (
                self._dependency_targets(
                    requirement,
                    resolved,
                    selected_keys=selected_keys,
                )
                if requirement.depends_on
                else tuple(
                    (subject, None)
                    for subject in dict.fromkeys((requirement.subject, *requirement.search_aliases))
                )
            )
            if not targets:
                raise ExternalKnowledgeExecutionError(
                    f"{requirement.requirement_id} dependency produced no entity"
                )
            matches: dict[str, TemporalRelationRecord] = {}
            for subject, required_product_key in targets:
                lookup = TemporalRelationSearchRequest(
                    capability=requirement.capability,
                    subject=subject,
                    product_families=requirement.product_families,
                    time_range=requirement.time_range,
                    top_k=50,
                )
                found = self.relation_store.search(lookup)
                if not found and required_product_key is None:
                    # Same cross-script rule the acquisition cache lookup used, so a
                    # relation reached through ``캠브리콘`` -> ``Cambricon`` is not
                    # lost again when the plan is executed over the verified set.
                    found = self.relation_store.search_cross_script(lookup)
                target_matches = tuple(
                    relation
                    for relation in found
                    if (
                        required_product_key is None
                        or _relation_product_key(relation) == required_product_key
                    )
                )
                if (
                    required_product_key is not None
                    and requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and not target_matches
                ):
                    raise ExternalKnowledgeExecutionError(
                        f"{requirement.requirement_id} lacks risk evidence for a selected product"
                    )
                for relation in target_matches:
                    matches[relation.relation_id] = relation
            if not matches:
                if (
                    requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                    and not requirement.depends_on
                    and plan.holding_match_mode is KnowledgeMatchMode.ANY
                ):
                    resolved[requirement.requirement_id] = ()
                    continue
                raise ExternalKnowledgeExecutionError(
                    f"{requirement.requirement_id} has no sealed relation evidence"
                )
            ordered = tuple(matches[key] for key in sorted(matches))
            resolved[requirement.requirement_id] = ordered
            all_relations.update(matches)

        product_keys = matched_product_keys(plan, resolved)
        try:
            facts = self.product_fact_provider.fetch(
                product_keys,
                plan.product_selection,
                plan.requested_product_fields,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalKnowledgeExecutionError(
                "provided product DB could not resolve requested product facts"
            ) from error
        bundle = ExternalKnowledgeEvidenceBundle(
            plan=plan,
            relations=tuple(all_relations[key] for key in sorted(all_relations)),
            requirement_relations={
                requirement_id: tuple(item.relation_id for item in relations)
                for requirement_id, relations in resolved.items()
            },
            product_facts=facts,
        )
        verification = self.verifier.verify(
            bundle,
            approved_product_ids=set(self.approved_product_ids),
        )
        if not verification.passed:
            raise ExternalKnowledgeExecutionError(
                "external knowledge evidence verification failed: "
                + "; ".join(verification.violations)
            )
        return ExternalKnowledgeExecution(evidence=bundle, verification=verification)


__all__ = [
    "ExternalKnowledgeExecution",
    "ExternalKnowledgeExecutionError",
    "ExternalKnowledgeExecutor",
    "OfficialProductFactProvider",
    "ProductKey",
    "matched_product_keys",
    "selected_product_keys",
]
