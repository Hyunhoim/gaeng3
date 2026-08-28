from __future__ import annotations

import hashlib
import json
from time import perf_counter

from pydantic import BaseModel, ConfigDict

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

        if not evidence_execution_authorized:
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

        all_relations = {
            relation.relation_id: relation for item in acquisitions for relation in item.relations
        }
        for requirement, acquisition in zip(
            search_plan.composite_plan.requirements,
            acquisitions,
            strict=True,
        ):
            if acquisition.status is not (
                OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
            ):
                continue
            try:
                extracted = tuple(
                    relation
                    for snapshot in acquisition.snapshots
                    for relation in self.live_relation_extractor.extract(
                        snapshot,
                        requirement,
                        universe,
                    )
                )
            except (OfficialLiveEvidenceError, ValueError):
                extracted = ()
            if not extracted:
                return ExternalKnowledgeRuntimeResult(
                    outcome=ExternalKnowledgeOutcome(
                        status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                        search_complete=False,
                        reason_codes=("live_exact_evidence_span_missing",),
                    ),
                    search_plan=search_plan,
                    candidate_universe=universe,
                    acquisitions=acquisitions,
                )
            all_relations.update({item.relation_id: item for item in extracted})
        relation_store = self.acquisition.relation_store
        if any(
            item.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
            for item in acquisitions
        ):
            corpus_hash = hashlib.sha256(
                (
                    json.dumps(
                        [
                            snapshot.content_sha256
                            for item in acquisitions
                            for snapshot in item.snapshots
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
            return ExternalKnowledgeRuntimeResult(
                outcome=ExternalKnowledgeOutcome(
                    status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    search_complete=False,
                    reason_codes=("verified_relation_chain_incomplete",),
                ),
                search_plan=search_plan,
                candidate_universe=universe,
                acquisitions=acquisitions,
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
                acquisitions=acquisitions,
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
            acquisitions=acquisitions,
            execution=execution,
        )


__all__ = ["ExternalKnowledgeRuntime", "ExternalKnowledgeRuntimeResult"]
