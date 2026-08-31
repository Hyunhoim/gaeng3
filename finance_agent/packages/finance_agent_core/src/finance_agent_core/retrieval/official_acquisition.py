from __future__ import annotations

from datetime import date
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import OfficialSourceRegistryError
from finance_agent_core.contracts.external_knowledge import KnowledgeRequirement
from finance_agent_core.retrieval.official_connector import (
    BoundedOfficialDiscovery,
    OfficialConnectorError,
    OfficialDiscoveryCandidate,
    OfficialDocumentLinkResolver,
    OfficialFetchRequest,
    OfficialNavigationSnapshot,
    OfficialSourceConnector,
    OfficialSourceSnapshot,
)
from finance_agent_core.retrieval.official_discovery import (
    DiscoveryFailureCode,
    OfficialDiscoveryError,
    OfficialDiscoveryReport,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationRecord,
    TemporalRelationSearchRequest,
    TemporalRelationStore,
)


class OfficialEvidenceAcquisitionStatus(StrEnum):
    CACHE_HIT = "cache_hit"
    LIVE_EVIDENCE_CANDIDATE = "live_evidence_candidate"
    INTAKE_PENDING = "intake_pending"
    NOT_FOUND = "not_found"
    CANNOT_VERIFY = "cannot_verify"


class OfficialEvidenceAcquisitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class OfficialEvidenceAcquisitionResult(OfficialEvidenceAcquisitionModel):
    status: OfficialEvidenceAcquisitionStatus
    relations: tuple[TemporalRelationRecord, ...] = Field(default=(), max_length=50)
    # One discovery candidate can fan out to linked official PDF/API documents.
    # Keep this bound aligned with the validated per-acquisition fetch budget;
    # otherwise a valid ninth snapshot raises at the result boundary and turns
    # an evidence miss into an HTTP 5xx.
    snapshots: tuple[OfficialSourceSnapshot, ...] = Field(default=(), max_length=40)
    discovered_candidate_count: int = Field(ge=0, le=20)
    rejected_candidate_count: int = Field(ge=0, le=20)
    search_complete: bool
    failure_codes: tuple[str, ...] = Field(default=(), max_length=20)
    search_query_count: int = Field(default=0, ge=0, le=40)
    fetch_count: int = Field(default=0, ge=0, le=40)
    fetched_bytes: int = Field(default=0, ge=0, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_result(self) -> OfficialEvidenceAcquisitionResult:
        if self.status is OfficialEvidenceAcquisitionStatus.CACHE_HIT:
            if not self.relations or self.snapshots or not self.search_complete:
                raise ValueError("cache hit requires relations and no intake snapshots")
        elif self.status is OfficialEvidenceAcquisitionStatus.INTAKE_PENDING:
            if self.relations or not self.snapshots or self.search_complete:
                raise ValueError("pending intake requires untrusted snapshots only")
        elif self.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE:
            if self.relations or not self.snapshots or self.search_complete:
                raise ValueError("live evidence candidate requires fetched snapshots only")
        elif self.status is OfficialEvidenceAcquisitionStatus.NOT_FOUND:
            if self.relations or self.snapshots or not self.search_complete:
                raise ValueError("not-found acquisition requires complete empty search")
        elif self.relations or self.snapshots or self.search_complete:
            raise ValueError("cannot-verify acquisition requires incomplete empty evidence")
        if self.failure_codes != tuple(sorted(set(self.failure_codes))):
            raise ValueError("acquisition failure codes must be unique and sorted")
        return self


class OfficialDiscoveryProvider(Protocol):
    def discover(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
    ) -> tuple[OfficialDiscoveryCandidate, ...]: ...


class OfficialEvidenceAcquisitionCoordinator:
    """Use the sealed relation cache first; quarantine every cache-miss fetch.

    Discovery snippets are never evidence. Freshly fetched bytes remain either
    intake-only snapshots or exact-span candidates; only a server verifier and
    a release-authorized runtime may turn the latter into answer evidence.
    """

    def __init__(
        self,
        *,
        relation_store: TemporalRelationStore,
        discovery_provider: OfficialDiscoveryProvider,
        connector: OfficialSourceConnector,
        discovery_gate: BoundedOfficialDiscovery | None = None,
        link_resolver: OfficialDocumentLinkResolver | None = None,
    ) -> None:
        self.relation_store = relation_store
        self.discovery_provider = discovery_provider
        self.connector = connector
        self.discovery_gate = discovery_gate or BoundedOfficialDiscovery(connector.registry)
        self.link_resolver = link_resolver or OfficialDocumentLinkResolver(connector.registry)
        if self.relation_store.registry.canonical_sha256 != (
            self.connector.registry.canonical_sha256
        ):
            raise ValueError("relation cache and connector use different source registries")
        if self.discovery_gate.registry.canonical_sha256 != (
            self.connector.registry.canonical_sha256
        ):
            raise ValueError("discovery gate and connector use different source registries")
        if self.link_resolver.registry.canonical_sha256 != (
            self.connector.registry.canonical_sha256
        ):
            raise ValueError("link resolver and connector use different source registries")

    def acquire(
        self,
        requirement: KnowledgeRequirement,
        *,
        discovery_terms: tuple[str, ...] = (),
        top_k: int = 8,
        max_search_queries: int = 8,
        max_fetches: int = 8,
        max_total_bytes: int = 16 * 1024 * 1024,
        deadline_ms: int = 12_000,
    ) -> OfficialEvidenceAcquisitionResult:
        if (
            len(discovery_terms) > 3
            or len(discovery_terms) != len(set(discovery_terms))
            or any(
                value != value.strip()
                or not 2 <= len(value) <= 120
                or "http://" in value.casefold()
                or "https://" in value.casefold()
                for value in discovery_terms
            )
        ):
            raise ValueError("discovery terms must be unique bounded entity text")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        if not 1 <= max_search_queries <= 40 or not 1 <= max_fetches <= 40:
            raise ValueError("acquisition query/fetch budgets are out of bounds")
        if not 1 <= max_total_bytes <= 64 * 1024 * 1024:
            raise ValueError("acquisition byte budget is out of bounds")
        if not 100 <= deadline_ms <= 60_000:
            raise ValueError("acquisition deadline is out of bounds")
        deadline = perf_counter() + deadline_ms / 1000
        cached = self.relation_store.search(
            TemporalRelationSearchRequest(
                capability=requirement.capability,
                subject=requirement.subject,
                product_families=requirement.product_families,
                time_range=requirement.time_range,
                top_k=min(top_k, 50),
            )
        )
        if cached:
            return OfficialEvidenceAcquisitionResult(
                status=OfficialEvidenceAcquisitionStatus.CACHE_HIT,
                relations=cached,
                discovered_candidate_count=0,
                rejected_candidate_count=0,
                search_complete=True,
            )
        report_method = getattr(self.discovery_provider, "discover_report", None)
        try:
            if callable(report_method):
                report = report_method(
                    query=" ".join(
                        (
                            requirement.subject,
                            *requirement.search_aliases,
                            *discovery_terms,
                        )
                    ),
                    capability=requirement.capability,
                    source_registry_ids=requirement.source_registry_ids,
                    evidence_cutoff=self.connector.registry.evidence_cutoff,
                    top_k=top_k,
                    max_queries=max_search_queries,
                )
                if type(report) is not OfficialDiscoveryReport:
                    raise TypeError("discovery report provider returned an invalid contract")
                discovered_pool = report.candidates[:20]
            else:
                discovered_pool = self.discovery_provider.discover(
                    query=" ".join(
                        (
                            requirement.subject,
                            *requirement.search_aliases,
                            *discovery_terms,
                        )
                    ),
                    capability=requirement.capability,
                    source_registry_ids=requirement.source_registry_ids,
                    evidence_cutoff=self.connector.registry.evidence_cutoff,
                    top_k=top_k,
                )[:20]
                report = OfficialDiscoveryReport(
                    candidates=discovered_pool,
                    queried_source_registry_ids=(),
                    exhaustive=False,
                    failure_codes=(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE,),
                )
        except (OfficialDiscoveryError, TimeoutError):
            return OfficialEvidenceAcquisitionResult(
                status=OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                discovered_candidate_count=0,
                rejected_candidate_count=0,
                search_complete=False,
                failure_codes=("official_discovery_failed",),
                search_query_count=1,
            )

        def is_time_eligible(candidate: OfficialDiscoveryCandidate) -> bool:
            if (
                requirement.time_range is None
                or candidate.published_at is None
                or (
                    candidate.published_at <= requirement.time_range.end
                    and (
                        requirement.time_range.start is None
                        or candidate.published_at >= requirement.time_range.start
                    )
                )
            ):
                return True
            try:
                source = self.connector.registry.authorize_url(
                    candidate.url,
                    requirement.capability,
                )
            except OfficialSourceRegistryError:
                return False
            # A registry-approved product page can be only a navigation root.
            # Its linked official API/PDF carries the requested historical date,
            # so the page publication date must not discard that linked evidence.
            return requirement.capability in source.historical_link_capabilities

        time_filtered = tuple(
            candidate for candidate in discovered_pool if is_time_eligible(candidate)
        )
        if requirement.time_range is not None:
            time_filtered = tuple(
                sorted(
                    time_filtered,
                    key=lambda candidate: (
                        candidate.published_at is None,
                        -(
                            candidate.published_at.toordinal()
                            if candidate.published_at is not None
                            else 0
                        ),
                    ),
                )
            )
        authorized = self.discovery_gate.authorize_candidates(
            time_filtered,
            requirement.capability,
            max_candidates=top_k,
        )
        snapshots: list[OfficialSourceSnapshot] = []
        rejected = len(discovered_pool) - len(authorized)
        fetched_bytes = 0
        fetch_attempt_count = 0
        fetch_failure_codes: set[str] = set()
        seen_snapshot_urls: set[str] = set()
        for candidate in authorized:
            if fetch_attempt_count >= max_fetches:
                rejected += 1
                continue
            remaining_seconds = deadline - perf_counter()
            if remaining_seconds <= 0:
                return OfficialEvidenceAcquisitionResult(
                    status=OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                    discovered_candidate_count=len(discovered_pool),
                    rejected_candidate_count=rejected + len(authorized) - len(snapshots),
                    search_complete=False,
                    failure_codes=("acquisition_deadline_exceeded",),
                    search_query_count=len(report.queried_source_registry_ids),
                    fetch_count=fetch_attempt_count,
                    fetched_bytes=fetched_bytes,
                )
            remaining_bytes = max_total_bytes - fetched_bytes
            if remaining_bytes <= 0:
                return OfficialEvidenceAcquisitionResult(
                    status=OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                    discovered_candidate_count=len(discovered_pool),
                    rejected_candidate_count=rejected + len(authorized) - len(snapshots),
                    search_complete=False,
                    failure_codes=("acquisition_byte_budget_exceeded",),
                    search_query_count=len(report.queried_source_registry_ids),
                    fetch_count=fetch_attempt_count,
                    fetched_bytes=fetched_bytes,
                )
            try:
                source = self.connector.registry.authorize_url(
                    candidate.url,
                    requirement.capability,
                )
                if source.source_id not in requirement.source_registry_ids:
                    rejected += 1
                    continue
                fetch_attempt_count += 1
                snapshot = self.connector.fetch(
                    candidate,
                    OfficialFetchRequest(
                        url=candidate.url,
                        capability=requirement.capability,
                        published_at=candidate.published_at,
                        as_of=candidate.as_of,
                        timeout_seconds=min(30.0, max(0.1, remaining_seconds)),
                        max_bytes=min(16 * 1024 * 1024, remaining_bytes),
                        allow_navigation_only=True,
                    ),
                )
                seen_snapshot_urls.add(snapshot.source_uri)
                fetched_bytes += len(snapshot.content)
                if not isinstance(snapshot, OfficialNavigationSnapshot):
                    snapshots.append(snapshot)
                remaining_link_fetches = max_fetches - fetch_attempt_count
                if remaining_link_fetches <= 0:
                    continue
                linked = self.link_resolver.resolve(
                    snapshot,
                    max_links=min(8, remaining_link_fetches),
                    evidence_as_of=(
                        requirement.time_range.end if requirement.time_range is not None else None
                    ),
                )
                for linked_candidate in linked:
                    if linked_candidate.url in seen_snapshot_urls:
                        continue
                    try:
                        linked_source = self.connector.registry.authorize_url(
                            linked_candidate.url,
                            requirement.capability,
                        )
                    except OfficialSourceRegistryError:
                        fetch_failure_codes.add("linked_document_source_rejected")
                        continue
                    if linked_source.source_id not in requirement.source_registry_ids:
                        fetch_failure_codes.add("linked_document_source_not_selected")
                        continue
                    remaining_seconds = deadline - perf_counter()
                    remaining_bytes = max_total_bytes - fetched_bytes
                    if (
                        fetch_attempt_count >= max_fetches
                        or remaining_seconds <= 0
                        or remaining_bytes <= 0
                    ):
                        fetch_failure_codes.add("linked_document_budget_exhausted")
                        break
                    linked_snapshot = None
                    for _linked_attempt in range(2):
                        remaining_seconds = deadline - perf_counter()
                        remaining_bytes = max_total_bytes - fetched_bytes
                        if (
                            fetch_attempt_count >= max_fetches
                            or remaining_seconds <= 0
                            or remaining_bytes <= 0
                        ):
                            break
                        try:
                            fetch_attempt_count += 1
                            linked_snapshot = self.connector.fetch(
                                linked_candidate,
                                OfficialFetchRequest(
                                    url=linked_candidate.url,
                                    capability=requirement.capability,
                                    published_at=linked_candidate.published_at,
                                    as_of=linked_candidate.as_of,
                                    timeout_seconds=min(30.0, max(0.1, remaining_seconds)),
                                    max_bytes=min(16 * 1024 * 1024, remaining_bytes),
                                ),
                            )
                            break
                        except (OfficialConnectorError, OfficialSourceRegistryError):
                            continue
                    if linked_snapshot is None:
                        fetch_failure_codes.add("linked_document_fetch_failed")
                        continue
                    snapshots.append(linked_snapshot)
                    seen_snapshot_urls.add(linked_snapshot.source_uri)
                    fetched_bytes += len(linked_snapshot.content)
            except (OfficialConnectorError, OfficialSourceRegistryError):
                rejected += 1
        if snapshots:
            live_evidence = all(
                snapshot.source.status == "approved_for_live_evidence" for snapshot in snapshots
            )
            return OfficialEvidenceAcquisitionResult(
                status=(
                    OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
                    if live_evidence
                    else OfficialEvidenceAcquisitionStatus.INTAKE_PENDING
                ),
                snapshots=tuple(snapshots),
                discovered_candidate_count=len(discovered_pool),
                rejected_candidate_count=rejected,
                search_complete=False,
                failure_codes=(
                    tuple(
                        sorted(
                            {
                                "live_evidence_requires_verification",
                                *fetch_failure_codes,
                            }
                        )
                    )
                    if live_evidence
                    else tuple(sorted({"evidence_intake_pending", *fetch_failure_codes}))
                ),
                search_query_count=len(report.queried_source_registry_ids),
                fetch_count=fetch_attempt_count,
                fetched_bytes=fetched_bytes,
            )
        status = (
            OfficialEvidenceAcquisitionStatus.NOT_FOUND
            if report.exhaustive
            else OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY
        )
        failure_codes = tuple(
            sorted(
                {
                    *(item.value for item in report.failure_codes),
                    *(() if report.exhaustive else ("search_not_exhaustive",)),
                    *(("all_candidates_rejected",) if discovered_pool and rejected else ()),
                }
            )
        )
        return OfficialEvidenceAcquisitionResult(
            status=status,
            discovered_candidate_count=len(discovered_pool),
            rejected_candidate_count=rejected,
            search_complete=report.exhaustive,
            failure_codes=failure_codes,
            search_query_count=len(report.queried_source_registry_ids),
            fetch_count=fetch_attempt_count,
            fetched_bytes=fetched_bytes,
        )


__all__ = [
    "OfficialDiscoveryProvider",
    "OfficialEvidenceAcquisitionCoordinator",
    "OfficialEvidenceAcquisitionResult",
    "OfficialEvidenceAcquisitionStatus",
]
