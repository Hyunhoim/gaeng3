from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
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
    def _dependency_subjects(
        requirement: KnowledgeRequirement,
        requirement_relations: dict[str, tuple[TemporalRelationRecord, ...]],
    ) -> tuple[str, ...]:
        labels: set[str] = set()
        for dependency in requirement.depends_on:
            for relation in requirement_relations.get(dependency, ()):
                if (
                    requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and relation.product_id is not None
                ):
                    product_entities = (
                        item
                        for item in (relation.subject, relation.object)
                        if item.kind is TemporalEntityKind.PRODUCT
                    )
                    labels.update(item.canonical_name for item in product_entities)
                else:
                    labels.add(relation.object.canonical_name)
        return tuple(sorted(labels))

    def execute(self, plan: CompositeKnowledgeQueryPlan) -> ExternalKnowledgeExecution:
        resolved: dict[str, tuple[TemporalRelationRecord, ...]] = {}
        all_relations: dict[str, TemporalRelationRecord] = {}
        for requirement in plan.requirements:
            subjects = (
                self._dependency_subjects(requirement, resolved)
                if requirement.depends_on
                else (requirement.subject,)
            )
            if not subjects:
                raise ExternalKnowledgeExecutionError(
                    f"{requirement.requirement_id} dependency produced no entity"
                )
            matches: dict[str, TemporalRelationRecord] = {}
            for subject in subjects:
                for relation in self.relation_store.search(
                    TemporalRelationSearchRequest(
                        capability=requirement.capability,
                        subject=subject,
                        product_families=requirement.product_families,
                        time_range=requirement.time_range,
                        top_k=50,
                    )
                ):
                    matches[relation.relation_id] = relation
            if not matches:
                raise ExternalKnowledgeExecutionError(
                    f"{requirement.requirement_id} has no sealed relation evidence"
                )
            ordered = tuple(matches[key] for key in sorted(matches))
            resolved[requirement.requirement_id] = ordered
            all_relations.update(matches)

        product_keys = tuple(
            sorted(
                {
                    (relation.product_family, relation.product_id)
                    for relation in all_relations.values()
                    if relation.product_family is not None and relation.product_id is not None
                },
                key=lambda item: (item[0].value, item[1]),
            )
        )
        facts = self.product_fact_provider.fetch(
            product_keys,
            plan.product_selection,
            plan.requested_product_fields,
        )
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
]
