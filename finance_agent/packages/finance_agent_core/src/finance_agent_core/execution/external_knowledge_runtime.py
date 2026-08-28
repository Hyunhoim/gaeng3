from __future__ import annotations

import hashlib
import json
from time import perf_counter

from pydantic import BaseModel, ConfigDict

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    ExternalKnowledgeOutcome,
    ExternalKnowledgeOutcomeStatus,
    ExternalKnowledgeSearchPlan,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.execution.external_knowledge_executor import (
    ExternalKnowledgeExecution,
    ExternalKnowledgeExecutionError,
    ExternalKnowledgeExecutor,
    OfficialProductFactProvider,
)
from finance_agent_core.release import ExternalKnowledgeArtifactRelease
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
    OfficialEvidenceAcquisitionResult,
    OfficialEvidenceAcquisitionStatus,
)
from finance_agent_core.retrieval.official_live_evidence import (
    OfficialLiveEvidenceError,
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalEntityKind,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)


class ExternalKnowledgeRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    outcome: ExternalKnowledgeOutcome
    search_plan: ExternalKnowledgeSearchPlan
    candidate_universe: ProvidedProductCandidateUniverse
    acquisitions: tuple[OfficialEvidenceAcquisitionResult, ...]
    execution: ExternalKnowledgeExecution | None = None


class ExternalKnowledgeRuntime:
    """Cache-first orchestration with strict DB intersection and terminal states."""

    def __init__(
        self,
        *,
        acquisition: OfficialEvidenceAcquisitionCoordinator,
        product_fact_provider: OfficialProductFactProvider,
        release_artifact: ExternalKnowledgeArtifactRelease | None = None,
        release_artifact_file_sha256: str | None = None,
        live_relation_extractor: VerifiedLiveRelationExtractor | None = None,
    ) -> None:
        self.acquisition = acquisition
        self.product_fact_provider = product_fact_provider
        if (release_artifact is None) != (release_artifact_file_sha256 is None):
            raise ValueError("external runtime release artifact and file hash must agree")
        if release_artifact is not None:
            if release_artifact.source_registry_sha256 != (
                acquisition.connector.registry.canonical_sha256
            ):
                raise ValueError("external runtime source registry differs from release")
            relation_manifest = acquisition.relation_store.bundle.manifest
            if release_artifact.temporal_relation_sha256 != (relation_manifest.relation_set_sha256):
                raise ValueError("external runtime relation store differs from release")
            if release_artifact.corpus_manifest_sha256 != (
                relation_manifest.corpus_manifest_sha256
            ):
                raise ValueError("external runtime corpus differs from release")
            if (
                release_artifact.retrieval_mode == "verified_live_official"
                and acquisition.relation_store.bundle.relations
            ):
                raise ValueError("live-only external runtime requires an empty relation cache")
        self.release_artifact = release_artifact
        self.release_artifact_file_sha256 = release_artifact_file_sha256
        self.live_relation_extractor = live_relation_extractor or VerifiedLiveRelationExtractor()

    @staticmethod
    def _dependency_targets(
        requirement,
        resolved: dict[str, tuple],
    ) -> tuple[tuple[str, tuple[str, ...], tuple | None], ...]:
        targets: dict[str, tuple[str, tuple[str, ...], tuple | None]] = {}
        for dependency in requirement.depends_on:
            for relation in resolved.get(dependency, ()):
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    entities = (
                        item
                        for item in (relation.subject, relation.object)
                        if item.kind is TemporalEntityKind.PRODUCT
                    )
                    for entity in entities:
                        targets[entity.entity_id] = (
                            entity.canonical_name,
                            entity.aliases,
                            (
                                relation.product_family,
                                relation.product_id,
                            ),
                        )
                else:
                    entity = relation.object
                    targets[entity.entity_id] = (
                        entity.canonical_name,
                        entity.aliases,
                        None,
                    )
        return tuple(targets[key] for key in sorted(targets))

    def _execute_authorized_staged(
        self,
        search_plan: ExternalKnowledgeSearchPlan,
        universe: ProvidedProductCandidateUniverse,
    ) -> ExternalKnowledgeRuntimeResult:
        """Resolve live multi-hop evidence in dependency order.

        A dependent source query is opened only after its parent relation has
        exact official evidence. This prevents a parent company name from
        being silently substituted for a subsidiary or product identity.
        """

        started = perf_counter()
        remaining_queries = search_plan.budget.max_search_queries
        remaining_fetches = search_plan.budget.max_fetches
        remaining_bytes = search_plan.budget.max_total_bytes
        acquisitions: list[OfficialEvidenceAcquisitionResult] = []
        resolved: dict[str, tuple] = {}
        all_relations = {}
        all_snapshots = []

        def terminal(status, reasons, *, complete=False):
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=status,
                    search_complete=complete,
                    reason_codes=tuple(sorted(set(reasons))),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=tuple(acquisitions),
            )

        requirements = search_plan.composite_plan.requirements
        for requirement in requirements:
            if requirement.depends_on:
                targets = self._dependency_targets(requirement, resolved)
                if (
                    requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and search_plan.composite_plan.product_selection is not None
                ):
                    product_keys = tuple(
                        sorted(
                            {
                                key
                                for _, _, key in targets
                                if key is not None
                                and key[0] is not None
                                and key[1] is not None
                            },
                            key=lambda item: (item[0].value, item[1]),
                        )
                    )
                    rank_facts = self.product_fact_provider.fetch(
                        product_keys,
                        search_plan.composite_plan.product_selection,
                        (),
                    )
                    selected_keys = {
                        (item.product_family, item.product_id) for item in rank_facts
                    }
                    targets = tuple(
                        item for item in targets if item[2] in selected_keys
                    )
            else:
                targets = ((requirement.subject, requirement.search_aliases, None),)
            if not targets:
                return terminal(
                    ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    ("verified_dependency_entity_missing",),
                )

            requirement_relations = {}
            target_incomplete = False
            target_not_found = 0
            for target_index, (subject, aliases, _) in enumerate(targets):
                remaining_ms = search_plan.budget.deadline_ms - int(
                    (perf_counter() - started) * 1000
                )
                targets_left = len(targets) - target_index
                if (
                    min(remaining_queries, remaining_fetches, remaining_bytes) <= 0
                    or remaining_ms < 100
                ):
                    return terminal(
                        ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                        ("external_search_budget_exhausted",),
                    )
                effective = requirement.model_copy(
                    update={
                        "subject": subject,
                        "search_aliases": tuple(sorted(set(aliases))),
                    }
                )
                # One official holdings/report document often covers several
                # entities from the verified parent relation. Re-run exact-span
                # extraction over already fetched bytes before opening another
                # web search. This is evidence reuse, not inferred relation
                # reuse: a subject still has to occur in the official text.
                reused = []
                for snapshot in all_snapshots:
                    try:
                        reused.extend(
                            self.live_relation_extractor.extract(
                                snapshot,
                                effective,
                                universe,
                            )
                        )
                    except (OfficialLiveEvidenceError, ValueError):
                        continue
                if reused:
                    requirement_relations.update(
                        {relation.relation_id: relation for relation in reused}
                    )
                    continue
                item = self.acquisition.acquire(
                    effective,
                    top_k=min(
                        search_plan.budget.max_candidates_per_requirement,
                        max(1, remaining_fetches // targets_left),
                    ),
                    max_search_queries=min(
                        remaining_queries,
                        len(effective.source_registry_ids),
                    ),
                    max_fetches=max(1, remaining_fetches // targets_left),
                    max_total_bytes=remaining_bytes,
                    deadline_ms=remaining_ms,
                )
                acquisitions.append(item)
                remaining_queries -= item.search_query_count
                remaining_fetches -= item.fetch_count
                remaining_bytes -= item.fetched_bytes
                if item.status in {
                    OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                    OfficialEvidenceAcquisitionStatus.INTAKE_PENDING,
                }:
                    target_incomplete = True
                    continue
                if item.status is OfficialEvidenceAcquisitionStatus.NOT_FOUND:
                    target_not_found += 1
                    continue
                if item.status is OfficialEvidenceAcquisitionStatus.CACHE_HIT:
                    extracted = item.relations
                else:
                    all_snapshots.extend(item.snapshots)
                    try:
                        extracted = tuple(
                            relation
                            for snapshot in item.snapshots
                            for relation in self.live_relation_extractor.extract(
                                snapshot,
                                effective,
                                universe,
                            )
                        )
                    except (OfficialLiveEvidenceError, ValueError):
                        extracted = ()
                if not extracted:
                    target_incomplete = True
                    continue
                requirement_relations.update(
                    {relation.relation_id: relation for relation in extracted}
                )

            if target_incomplete:
                reasons = {
                    reason
                    for item in acquisitions
                    for reason in item.failure_codes
                    if reason != "live_evidence_requires_verification"
                }
                reasons.add("live_exact_evidence_span_missing")
                return terminal(ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY, reasons)
            if not requirement_relations:
                if target_not_found == len(targets):
                    return terminal(
                        ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                        ("complete_official_search_empty",),
                        complete=True,
                    )
                return terminal(
                    ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    ("verified_relation_chain_incomplete",),
                )
            ordered = tuple(
                requirement_relations[key] for key in sorted(requirement_relations)
            )
            resolved[requirement.requirement_id] = ordered
            all_relations.update(requirement_relations)

        corpus_hash = hashlib.sha256(
            (
                json.dumps(
                    [
                        (
                            "sealed-cache:"
                            + self.acquisition.relation_store.bundle.manifest.corpus_manifest_sha256
                        ),
                        *(f"live:{snapshot.content_sha256}" for snapshot in all_snapshots),
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        relation_store = TemporalRelationStore(
            seal_temporal_relation_bundle(
                tuple(all_relations[key] for key in sorted(all_relations)),
                corpus_manifest_sha256=corpus_hash,
                registry=self.acquisition.connector.registry,
            ),
            self.acquisition.connector.registry,
        )
        approved_ids = {
            (candidate.product_family, candidate.product_id) for candidate in universe.candidates
        }
        executor = ExternalKnowledgeExecutor(
            relation_store=relation_store,
            product_fact_provider=self.product_fact_provider,
            approved_product_ids=approved_ids,
        )
        try:
            execution = executor.execute(search_plan.composite_plan)
        except ExternalKnowledgeExecutionError:
            return terminal(
                ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                ("verified_relation_chain_incomplete",),
            )
        product_ids = tuple(
            dict.fromkeys(
                fact.product_id
                for fact in execution.evidence.product_facts
                if (fact.product_family, fact.product_id) in approved_ids
            )
        )
        if not product_ids:
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                    search_complete=True,
                    reason_codes=("verified_external_db_intersection_empty",),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=tuple(acquisitions),
                execution=execution,
            )
        return ExternalKnowledgeRuntimeResult(
            outcome=ExternalKnowledgeOutcome(
                status=ExternalKnowledgeOutcomeStatus.SUCCESS,
                search_complete=True,
                verified_product_ids=product_ids,
                reason_codes=("verified_external_db_intersection",),
            ),
            search_plan=search_plan,
            candidate_universe=universe,
            acquisitions=tuple(acquisitions),
            execution=execution,
        )

    def execute(
        self,
        search_plan: ExternalKnowledgeSearchPlan,
        universe: ProvidedProductCandidateUniverse,
        *,
        evidence_execution_authorized: bool = False,
    ) -> ExternalKnowledgeRuntimeResult:
        if search_plan.candidate_universe_sha256 != universe.canonical_sha256:
            raise ValueError("external search plan candidate universe changed")
        if search_plan.candidate_count != len(universe.candidates):
            raise ValueError("external search plan candidate count changed")
        if search_plan.source_registry_sha256 != (
            self.acquisition.connector.registry.canonical_sha256
        ):
            raise ValueError("external search plan source registry changed")
        if not universe.candidates:
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                    search_complete=True,
                    reason_codes=("provided_db_candidate_universe_empty",),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=(),
            )
        if evidence_execution_authorized:
            return self._execute_authorized_staged(search_plan, universe)

        started = perf_counter()
        remaining_queries = search_plan.budget.max_search_queries
        remaining_fetches = search_plan.budget.max_fetches
        remaining_bytes = search_plan.budget.max_total_bytes
        acquired: list[OfficialEvidenceAcquisitionResult] = []
        requirements = search_plan.composite_plan.requirements
        for requirement_index, requirement in enumerate(requirements):
            requirements_left = len(requirements) - requirement_index
            remaining_ms = search_plan.budget.deadline_ms - int((perf_counter() - started) * 1000)
            if (
                min(remaining_queries, remaining_fetches, remaining_bytes) <= 0
                or remaining_ms < 100
            ):
                return ExternalKnowledgeRuntimeResult(
                    outcome=ExternalKnowledgeOutcome(
                        status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                        search_complete=False,
                        reason_codes=("external_search_budget_exhausted",),
                    ),
                    search_plan=search_plan,
                    candidate_universe=universe,
                    acquisitions=tuple(acquired),
                )
            # Reserve fetch capacity for every remaining hop.  Passing the
            # entire remaining budget to an early requirement allowed its
            # linked documents to starve later holdings/risk joins.
            requirement_fetch_budget = max(
                1,
                remaining_fetches // requirements_left,
            )
            requirement_query_budget = min(
                remaining_queries,
                len(requirement.source_registry_ids),
            )
            item = self.acquisition.acquire(
                requirement,
                top_k=min(
                    search_plan.budget.max_candidates_per_requirement,
                    requirement_fetch_budget,
                ),
                max_search_queries=requirement_query_budget,
                max_fetches=requirement_fetch_budget,
                max_total_bytes=remaining_bytes,
                deadline_ms=remaining_ms,
            )
            acquired.append(item)
            remaining_queries -= item.search_query_count
            remaining_fetches -= item.fetch_count
            remaining_bytes -= item.fetched_bytes
        acquisitions = tuple(acquired)
        if any(
            item.status
            in {
                OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                OfficialEvidenceAcquisitionStatus.INTAKE_PENDING,
            }
            for item in acquisitions
        ):
            reasons = {
                reason
                for item in acquisitions
                for reason in (
                    item.failure_codes
                    or (
                        "external_evidence_intake_pending"
                        if item.status is OfficialEvidenceAcquisitionStatus.INTAKE_PENDING
                        else "external_search_incomplete",
                    )
                )
            }
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    search_complete=False,
                    reason_codes=tuple(sorted(reasons)),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=acquisitions,
            )
        if any(item.status is OfficialEvidenceAcquisitionStatus.NOT_FOUND for item in acquisitions):
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                    search_complete=True,
                    reason_codes=("complete_official_search_empty",),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=acquisitions,
            )

        return ExternalKnowledgeRuntimeResult(
            outcome=ExternalKnowledgeOutcome(
                status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                search_complete=False,
                reason_codes=("sealed_evidence_not_release_authorized",),
            ),
            search_plan=search_plan,
            candidate_universe=universe,
            acquisitions=acquisitions,
        )


__all__ = ["ExternalKnowledgeRuntime", "ExternalKnowledgeRuntimeResult"]
