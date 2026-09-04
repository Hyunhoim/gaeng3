from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from time import perf_counter

from pydantic import BaseModel, ConfigDict

from finance_agent_core.agent.external_search_planning import normalize_product_identity
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    ExternalKnowledgeOutcome,
    ExternalKnowledgeOutcomeStatus,
    ExternalKnowledgeSearchPlan,
    KnowledgeMatchMode,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.execution.external_knowledge_executor import (
    ExternalKnowledgeExecution,
    ExternalKnowledgeExecutionError,
    ExternalKnowledgeExecutor,
    OfficialProductFactProvider,
    ProductKey,
    matched_product_keys,
    selected_product_keys,
)
from finance_agent_core.release import ExternalKnowledgeArtifactRelease
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
)
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
    OfficialEvidenceAcquisitionResult,
    OfficialEvidenceAcquisitionStatus,
)
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate
from finance_agent_core.retrieval.official_live_evidence import (
    OfficialLiveEvidenceError,
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntityKind,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_PRODUCT_EVIDENCE_DATE = (
    r"(?:(20\d{2})[-./](0?[1-9]|1[0-2])[-./](0?[1-9]|[12]\d|3[01])(?!\d)"
    r"|(20\d{2})\s*년\s*(0?[1-9]|1[0-2])\s*월\s*"
    r"(0?[1-9]|[12]\d|3[01])\s*일)"
)


def _parse_product_evidence_date(match: re.Match[str]) -> date | None:
    values = match.groups()
    parts = values[:3] if values[0] is not None else values[3:]
    try:
        return date(*(int(value) for value in parts if value is not None))
    except ValueError:
        return None


def _dates_bound_to_product_surface(question: str, surface: str) -> frozenset[date]:
    """Read an exact date immediately before or after one DB-bound product.

    Both ISO-like dates and natural Korean dates are supported.  The match is
    intentionally local and requires the exact supplied product surface, so a
    general date elsewhere in the question cannot be attached to every product
    in a comparison.
    """

    escaped = re.escape(surface)
    patterns = (
        rf"(?<![0-9A-Za-z가-힣]){escaped}\s*(?:\(\s*)?"
        rf"{_PRODUCT_EVIDENCE_DATE}(?:\s*\))?\s*(?:기준)?",
        rf"{_PRODUCT_EVIDENCE_DATE}\s*(?:기준(?:의|으로)?\s*)?"
        rf"{escaped}(?:과|와|은|는|이|가|의|을|를)?"
        rf"(?![0-9A-Za-z가-힣])",
    )
    parsed: set[date] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, question, re.IGNORECASE):
            if (value := _parse_product_evidence_date(match)) is not None:
                parsed.add(value)
    return frozenset(parsed)


class ExternalKnowledgeRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    outcome: ExternalKnowledgeOutcome
    search_plan: ExternalKnowledgeSearchPlan
    candidate_universe: ProvidedProductCandidateUniverse
    acquisitions: tuple[OfficialEvidenceAcquisitionResult, ...]
    execution: ExternalKnowledgeExecution | None = None
    identity_evidence: tuple[ExternalEvidenceReference, ...] = ()


_TRAILING_PARTICLES = frozenset(
    {"로", "의", "은", "는", "이", "가", "을", "를", "와", "과", "에", "도"}
)
_IMPLAUSIBLE_ENTITY = re.compile(
    r"^\d+(?:년|월|일|%)?$|_|^(?:업무위탁|대표이사|이사회|감사|주주|임원)$"
)


def _plausible_dependency_entity(name: str) -> bool:
    """Reject tokens that cannot be a company: years, underscores, filing headings."""

    value = name.strip()
    return len(value) >= 2 and _IMPLAUSIBLE_ENTITY.search(value) is None


def _relation_survives_universe(relation, approved_candidate_keys) -> bool:
    """Keep product relations only inside the fixed DB universe; pass company links through.

    A corporate-subsidiary relation binds two companies and carries no product.
    It is the *input* of the dependent holdings step, not an answer, so the
    provided-DB intersection cannot apply to it.  Filtering it here silently
    ended every ``자회사 → 편입 → 위험요인`` chain at step one.
    """

    if (
        relation.product_id is None
        and relation.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
    ):
        return True
    return (relation.product_family, relation.product_id) in approved_candidate_keys


_GRAPH_PAGE_URL = re.compile(
    r"/etf/product/view\.do\?id=|/tigeretf/ko/product/search/detail/index\.do\?ksdFund="
)


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
            if (
                release_artifact.retrieval_mode == "verified_live_official_with_relation_graph"
                and not acquisition.relation_store.bundle.relations
            ):
                raise ValueError("relation graph external runtime requires a non-empty graph")
            if release_artifact.retrieval_mode == "approved_corpus_cache_first":
                discovery = acquisition.discovery_provider
                if (
                    type(discovery) is not CacheFirstOfficialDiscoveryProvider
                    or not discovery.production_approved
                    or discovery.activation_binding_sha256
                    != release_artifact.document_activation_receipt_sha256
                    or discovery.artifact.canonical_sha256 != release_artifact.document_dense_sha256
                ):
                    raise ValueError(
                        "cache-first external runtime requires its release-pinned approved cache"
                    )
        self.release_artifact = release_artifact
        self.release_artifact_file_sha256 = release_artifact_file_sha256
        self.live_relation_extractor = live_relation_extractor or VerifiedLiveRelationExtractor()

    @staticmethod
    def _dependency_targets(
        requirement,
        resolved: dict[str, tuple],
        *,
        selected_keys: frozenset[ProductKey] = frozenset(),
        max_targets: int | None = None,
    ) -> tuple[tuple[str, tuple[str, ...], tuple | None], ...]:
        targets: dict[str, tuple[str, tuple[str, ...], tuple | None]] = {}
        support: dict[str, int] = {}
        for dependency in requirement.depends_on:
            for relation in resolved.get(dependency, ()):
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    product_key = (
                        relation.product_family,
                        relation.product_id,
                    )
                    if (
                        product_key[0] is None
                        or product_key[1] is None
                        or product_key not in selected_keys
                    ):
                        continue
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
                    if not _plausible_dependency_entity(entity.canonical_name):
                        continue
                    targets[entity.entity_id] = (
                        entity.canonical_name,
                        entity.aliases,
                        None,
                    )
                    support[entity.entity_id] = support.get(entity.entity_id, 0) + 1
        # A long official filing names one real subsidiary many times and a
        # noisy token once.  Spend the dependent step's bounded budget on the
        # entities the evidence supports most, drop a name that is only another
        # target plus a trailing particle, and cap the list to the plan budget.
        ordered = sorted(targets, key=lambda key: (-support.get(key, 0), targets[key][0]))
        names = {targets[key][0] for key in ordered}
        ordered = [
            key
            for key in ordered
            if not any(
                targets[key][0].startswith(other)
                and targets[key][0][len(other) :] in _TRAILING_PARTICLES
                for other in names
                if other != targets[key][0]
            )
        ]
        if max_targets is not None:
            ordered = ordered[: max(1, max_targets)]
        return tuple(targets[key] for key in ordered)

    @staticmethod
    def _explicit_product_discovery_terms(
        requirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[str, ...]:
        """Return DB-bound names and identifiers for official discovery only."""

        if not universe.explicit_identity_bound or requirement.capability not in {
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgeCapability.SECTOR_THEME_HISTORY,
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            KnowledgeCapability.PRODUCT_RISK_FACTORS,
        }:
            return ()
        terms = (
            value
            for candidate in universe.candidates
            if candidate.product_family in requirement.product_families
            for value in (candidate.product_name, *candidate.aliases)
        )
        return tuple(dict.fromkeys(terms))[:3]

    def _graph_page_seeds(
        self,
        requirement,
        required_product_key,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        """Official product pages the sealed graph already binds to this product.

        A dependent step that names one exact product (``선택된 상품의 위험요인``)
        should open that product's own approved page first.  The graph's
        evidence carries those URLs with the publisher's identity binding, so
        they are provenance, not a search guess.  Only pages approved for the
        requirement's capability are seeded; the connector still fetches and
        verifies them like any other candidate.
        """

        if required_product_key is None:
            return ()
        registry = self.acquisition.connector.registry
        seeds: dict[str, OfficialDiscoveryCandidate] = {}
        for relation in self.acquisition.relation_store.bundle.relations:
            if (relation.product_family, relation.product_id) != required_product_key:
                continue
            for evidence in relation.evidence:
                if evidence.source_uri in seeds or not _GRAPH_PAGE_URL.search(evidence.source_uri):
                    continue
                try:
                    registry.authorize_url(evidence.source_uri, requirement.capability)
                except Exception:  # noqa: BLE001 - unapproved for this capability
                    continue
                # Undated official product pages are dated at the evidence
                # cutoff, exactly as the graph builder and the holdings link
                # resolver do; otherwise the connector keeps the page as
                # navigation-only and the extractor cannot bind identity to it.
                seeds[evidence.source_uri] = OfficialDiscoveryCandidate(
                    url=evidence.source_uri,
                    title=evidence.title,
                    as_of=registry.evidence_cutoff,
                )
                if len(seeds) >= 4:
                    return tuple(seeds.values())
        return tuple(seeds.values())

    @staticmethod
    def _provided_product_discovery_surfaces(
        requirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[str, ...]:
        """Expose only the already-fixed DB universe to local candidate ranking."""

        if requirement.capability not in {
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgeCapability.SECTOR_THEME_HISTORY,
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            KnowledgeCapability.PRODUCT_RISK_FACTORS,
        }:
            return ()
        values = (
            value
            for candidate in universe.candidates
            if candidate.product_family in requirement.product_families
            for value in (candidate.product_name, *candidate.aliases)
        )
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _provided_product_evidence_dates(
        question: str,
        *,
        requirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[tuple[str, date], ...]:
        """Bind explicit per-product ISO dates to DB-authorized identities.

        A comparison can ask for two named products at different dates.  Such
        dates cannot be represented by the requirement-wide ``time_range``.
        Only a literal product name/ticker already present in the fixed DB
        universe may create a binding; dates never widen that universe.
        """

        if (
            requirement.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS
            or not universe.explicit_identity_bound
        ):
            return ()
        bindings: dict[str, date] = {}
        ambiguous_surfaces: set[str] = set()
        for candidate in universe.candidates:
            if candidate.product_family not in requirement.product_families:
                continue
            surfaces = tuple(
                sorted(
                    dict.fromkeys((candidate.product_name, *candidate.aliases)),
                    key=lambda value: (-len(value), value.casefold()),
                )
            )
            candidate_dates: set[date] = set()
            for surface in surfaces:
                if len(normalize_product_identity(surface)) < 2:
                    continue
                candidate_dates.update(_dates_bound_to_product_surface(question, surface))
            if len(candidate_dates) != 1:
                continue
            selected_date = next(iter(candidate_dates))
            for surface in surfaces:
                key = normalize_product_identity(surface)
                if len(key) < 2 or key in ambiguous_surfaces:
                    continue
                previous = bindings.get(key)
                if previous is not None and previous != selected_date:
                    bindings.pop(key, None)
                    ambiguous_surfaces.add(key)
                    continue
                bindings[key] = selected_date
        return tuple(sorted(bindings.items(), key=lambda item: (item[0], item[1])))

    @staticmethod
    def _identity_only_empty_intersection_allowed(
        requirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> bool:
        """Allow NOT_FOUND only for an exact external-name/product lookup.

        ``Kimi related overseas ETF`` can be resolved as an empty provided-DB
        intersection after an official source proves the Kimi identity and the
        full overseas-ETF identity universe contains no such name.  A holdings,
        risk, dated-history, or generic theme request still requires the full
        relation extractor and therefore cannot use this shortcut.
        """

        if (
            requirement.capability is not KnowledgeCapability.SECTOR_THEME_HISTORY
            or requirement.time_range is not None
            or re.search(
                r"(?:관련\s*(?:된\s*)?)?(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP)|"
                r"관련\s*(?:투자\s*)?상품",
                requirement.relation_evidence_span,
                re.IGNORECASE,
            )
            is None
        ):
            return False
        subject = normalize_product_identity(requirement.subject)
        if not subject or any(
            subject.endswith(normalize_product_identity(suffix))
            for suffix in ("테마", "섹터", "산업")
        ):
            return False
        for candidate in universe.candidates:
            if candidate.product_family not in requirement.product_families:
                continue
            surfaces = (candidate.product_name, *candidate.aliases)
            if any(
                subject in normalize_product_identity(surface) for surface in surfaces if surface
            ):
                return False
        return True

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
        partial_reasons: set[str] = set()
        resolved: dict[str, tuple] = {}
        all_relations = {}
        all_snapshots = []

        identity_evidence: dict[str, ExternalEvidenceReference] = {}
        approved_candidate_keys = frozenset(
            (candidate.product_family, candidate.product_id) for candidate in universe.candidates
        )

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
                identity_evidence=tuple(
                    identity_evidence[key] for key in sorted(identity_evidence)
                ),
            )

        requirements = search_plan.composite_plan.requirements
        for requirement_index, requirement in enumerate(requirements):
            if requirement.depends_on:
                selected_keys: frozenset[ProductKey] = frozenset()
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    matched_keys = matched_product_keys(search_plan.composite_plan, resolved)
                    if not matched_keys:
                        return terminal(
                            ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                            ("verified_external_relation_set_empty",),
                            complete=True,
                        )
                    try:
                        selected_keys = frozenset(
                            selected_product_keys(
                                search_plan.composite_plan,
                                resolved,
                                self.product_fact_provider,
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        return terminal(
                            ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                            ("provided_db_ranking_unavailable",),
                        )
                    if not selected_keys:
                        return terminal(
                            ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                            ("provided_db_numeric_ranking_basis_empty",),
                        )
                targets = self._dependency_targets(
                    requirement,
                    resolved,
                    selected_keys=selected_keys,
                    max_targets=search_plan.budget.max_candidates_per_requirement,
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
            for target_index, (subject, aliases, required_product_key) in enumerate(targets):
                remaining_ms = search_plan.budget.deadline_ms - int(
                    (perf_counter() - started) * 1000
                )
                targets_left = len(targets) - target_index
                budget = search_plan.budget
                if requirement.depends_on and requirement_relations:
                    # Verified targets already answer this step.  Unlisted
                    # subsidiaries would burn the rest of the budget on searches
                    # that cannot succeed, starving the requirements that still
                    # follow (e.g. the selected product's risk factors).  Keep
                    # half of every budget for them and record the partial scope.
                    if (
                        remaining_queries <= budget.max_search_queries // 2
                        or remaining_fetches <= budget.max_fetches // 2
                        or remaining_bytes <= budget.max_total_bytes // 2
                        or remaining_ms <= budget.deadline_ms // 2
                    ):
                        partial_reasons.add("dependency_targets_partially_verified")
                        break
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
                try:
                    extract_many = getattr(self.live_relation_extractor, "extract_many", None)
                    reused = (
                        list(extract_many(tuple(all_snapshots), effective, universe))
                        if callable(extract_many)
                        else [
                            relation
                            for snapshot in all_snapshots
                            for relation in self.live_relation_extractor.extract(
                                snapshot,
                                effective,
                                universe,
                            )
                        ]
                    )
                except (OfficialLiveEvidenceError, ValueError):
                    reused = []
                if required_product_key is not None:
                    reused = [
                        relation
                        for relation in reused
                        if (relation.product_family, relation.product_id) == required_product_key
                    ]
                reused = [
                    relation
                    for relation in reused
                    if _relation_survives_universe(relation, approved_candidate_keys)
                ]
                if reused:
                    requirement_relations.update(
                        {relation.relation_id: relation for relation in reused}
                    )
                    continue
                future_requirement_slots = len(requirements) - requirement_index - 1
                acquisition_slots = max(
                    1,
                    targets_left + future_requirement_slots,
                )
                query_budget = max(1, remaining_queries // acquisition_slots)
                source_count = len(effective.source_registry_ids)
                # One exact typed query per source is the coverage floor. A
                # second broad/factsheet variant is admitted only when the
                # global budget leaves room for later requirements/targets.
                query_budget = min(
                    remaining_queries,
                    max(
                        min(source_count, query_budget),
                        min(source_count * 2, query_budget),
                    ),
                )
                explicit_product_terms = self._explicit_product_discovery_terms(
                    effective,
                    universe,
                )
                product_evidence_dates = self._provided_product_evidence_dates(
                    search_plan.composite_plan.question,
                    requirement=effective,
                    universe=universe,
                )
                item = self.acquisition.acquire(
                    effective,
                    discovery_terms=explicit_product_terms,
                    provided_product_surfaces=self._provided_product_discovery_surfaces(
                        effective,
                        universe,
                    ),
                    provided_product_evidence_dates=product_evidence_dates,
                    seed_candidates=self._graph_page_seeds(effective, required_product_key),
                    top_k=min(
                        search_plan.budget.max_candidates_per_requirement,
                        max(1, remaining_fetches // targets_left),
                    ),
                    max_search_queries=query_budget,
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
                        extract_many = getattr(
                            self.live_relation_extractor,
                            "extract_many",
                            None,
                        )
                        extracted = (
                            tuple(extract_many(item.snapshots, effective, universe))
                            if callable(extract_many)
                            else tuple(
                                relation
                                for snapshot in item.snapshots
                                for relation in self.live_relation_extractor.extract(
                                    snapshot,
                                    effective,
                                    universe,
                                )
                            )
                        )
                    except (OfficialLiveEvidenceError, ValueError):
                        extracted = ()
                if required_product_key is not None:
                    extracted = tuple(
                        relation
                        for relation in extracted
                        if (relation.product_family, relation.product_id) == required_product_key
                    )
                raw_relation_evidence_found = bool(extracted)
                extracted = tuple(
                    relation
                    for relation in extracted
                    if _relation_survives_universe(relation, approved_candidate_keys)
                )
                if not extracted and raw_relation_evidence_found and item.search_complete:
                    # The official evidence search completed and proved only
                    # products outside the DB-filtered universe.  This is a
                    # verified empty intersection, not a retrieval failure.
                    target_not_found += 1
                    continue
                evidence_snapshots = item.snapshots
                if (
                    not extracted
                    and item.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
                    and item.search_query_count == 0
                    and isinstance(
                        self.acquisition.discovery_provider,
                        CacheFirstOfficialDiscoveryProvider,
                    )
                ):
                    # A Document Dense hit is only a URL proposal.  If the
                    # refetched bytes fail the exact subject/relation/product
                    # checks, treat it as a semantic cache miss and spend the
                    # remaining bounded budget on the official live provider.
                    # Without this second stage, one high-scoring but stale or
                    # neighbouring official document permanently suppresses a
                    # valid result that live search could verify.
                    remaining_ms = search_plan.budget.deadline_ms - int(
                        (perf_counter() - started) * 1000
                    )
                    if (
                        min(remaining_queries, remaining_fetches, remaining_bytes) > 0
                        and remaining_ms >= 100
                    ):
                        live_item = self.acquisition.acquire(
                            effective,
                            discovery_terms=explicit_product_terms,
                            provided_product_surfaces=(
                                self._provided_product_discovery_surfaces(
                                    effective,
                                    universe,
                                )
                            ),
                            provided_product_evidence_dates=product_evidence_dates,
                            top_k=min(
                                search_plan.budget.max_candidates_per_requirement,
                                max(1, remaining_fetches // targets_left),
                            ),
                            max_search_queries=max(1, min(query_budget, remaining_queries)),
                            max_fetches=max(1, remaining_fetches // targets_left),
                            max_total_bytes=remaining_bytes,
                            deadline_ms=remaining_ms,
                            force_live_discovery=True,
                        )
                        acquisitions.append(live_item)
                        remaining_queries -= live_item.search_query_count
                        remaining_fetches -= live_item.fetch_count
                        remaining_bytes -= live_item.fetched_bytes
                        if live_item.status is OfficialEvidenceAcquisitionStatus.NOT_FOUND:
                            target_not_found += 1
                            continue
                        if live_item.status in {
                            OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                            OfficialEvidenceAcquisitionStatus.INTAKE_PENDING,
                        }:
                            target_incomplete = True
                            continue
                        if live_item.status is OfficialEvidenceAcquisitionStatus.CACHE_HIT:
                            # ``force_live_discovery`` bypasses only the
                            # document-URL cache, not the sealed relation
                            # cache.  Its relation records remain authoritative.
                            extracted = live_item.relations
                        else:
                            all_snapshots.extend(live_item.snapshots)
                            evidence_snapshots = tuple((*evidence_snapshots, *live_item.snapshots))
                            try:
                                extract_many = getattr(
                                    self.live_relation_extractor,
                                    "extract_many",
                                    None,
                                )
                                extracted = (
                                    tuple(
                                        extract_many(
                                            live_item.snapshots,
                                            effective,
                                            universe,
                                        )
                                    )
                                    if callable(extract_many)
                                    else tuple(
                                        relation
                                        for snapshot in live_item.snapshots
                                        for relation in self.live_relation_extractor.extract(
                                            snapshot,
                                            effective,
                                            universe,
                                        )
                                    )
                                )
                            except (OfficialLiveEvidenceError, ValueError):
                                extracted = ()
                        if required_product_key is not None:
                            extracted = tuple(
                                relation
                                for relation in extracted
                                if (
                                    relation.product_family,
                                    relation.product_id,
                                )
                                == required_product_key
                            )
                        raw_relation_evidence_found = bool(extracted)
                        extracted = tuple(
                            relation
                            for relation in extracted
                            if _relation_survives_universe(relation, approved_candidate_keys)
                        )
                        if (
                            not extracted
                            and raw_relation_evidence_found
                            and live_item.search_complete
                        ):
                            target_not_found += 1
                            continue
                if not extracted:
                    subject_evidence = self.live_relation_extractor.extract_subject_evidence(
                        evidence_snapshots,
                        effective,
                    )
                    if subject_evidence and self._identity_only_empty_intersection_allowed(
                        effective, universe
                    ):
                        identity_evidence.update(
                            {value.evidence_id: value for value in subject_evidence}
                        )
                        target_not_found += 1
                        continue
                    target_incomplete = True
                    continue
                requirement_relations.update(
                    {relation.relation_id: relation for relation in extracted}
                )

            if target_incomplete and requirement.depends_on and requirement_relations:
                # ``자회사를 편입한 ETF``: the parent relation also yields entities
                # that no official product document can name (unlisted
                # subsidiaries).  Their searches end unverified, but the
                # verified targets are a real, evidence-backed answer.  Keep it
                # and say so instead of discarding the whole chain.
                partial_reasons.add("dependency_targets_partially_verified")
            elif target_incomplete:
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
                    if (
                        requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                        and not requirement.depends_on
                        and search_plan.composite_plan.holding_match_mode is KnowledgeMatchMode.ANY
                    ):
                        resolved[requirement.requirement_id] = ()
                        continue
                    return terminal(
                        ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                        ("complete_official_search_empty",),
                        complete=True,
                    )
                return terminal(
                    ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    ("verified_relation_chain_incomplete",),
                )
            ordered = tuple(requirement_relations[key] for key in sorted(requirement_relations))
            resolved[requirement.requirement_id] = ordered
            all_relations.update(requirement_relations)

        matched_keys = matched_product_keys(search_plan.composite_plan, resolved)
        if (
            any(
                requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                for requirement in requirements
            )
            and not matched_keys
        ):
            return terminal(
                ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                ("verified_external_relation_set_empty",),
                complete=True,
            )
        if search_plan.composite_plan.product_selection is not None:
            try:
                ranked_keys = selected_product_keys(
                    search_plan.composite_plan,
                    resolved,
                    self.product_fact_provider,
                )
            except (KeyError, TypeError, ValueError):
                return terminal(
                    ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                    ("provided_db_ranking_unavailable",),
                )
            if not ranked_keys:
                return terminal(
                    ExternalKnowledgeOutcomeStatus.NOT_FOUND,
                    ("provided_db_numeric_ranking_basis_empty",),
                    complete=True,
                )

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
        approved_ids = set(approved_candidate_keys)
        executor = ExternalKnowledgeExecutor(
            relation_store=relation_store,
            product_fact_provider=self.product_fact_provider,
            approved_product_ids=approved_ids,
        )
        try:
            execution = executor.execute(search_plan.composite_plan)
        except ExternalKnowledgeExecutionError as error:
            reason = (
                "provided_db_requested_fact_unavailable"
                if "provided product DB" in str(error)
                else "verified_relation_chain_incomplete"
            )
            return terminal(
                ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                (reason,),
            )
        product_keys = tuple(
            dict.fromkeys(
                (fact.product_family, fact.product_id)
                for fact in execution.evidence.product_facts
                if (fact.product_family, fact.product_id) in approved_ids
            )
        )
        if not product_keys:
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
                verified_product_keys=product_keys,
                verified_product_ids=tuple(product_id for _family, product_id in product_keys),
                reason_codes=tuple(sorted({"verified_external_db_intersection", *partial_reasons})),
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
        evidence_execution_authorized: bool | None = None,
    ) -> ExternalKnowledgeRuntimeResult:
        if search_plan.candidate_universe_sha256 != universe.canonical_sha256:
            raise ValueError("external search plan candidate universe changed")
        if search_plan.candidate_count != len(universe.candidates):
            raise ValueError("external search plan candidate count changed")
        if search_plan.source_registry_sha256 != (
            self.acquisition.connector.registry.canonical_sha256
        ):
            raise ValueError("external search plan source registry changed")
        receipt_authorized = search_plan.execution_receipt is not None
        if (
            evidence_execution_authorized is not None
            and evidence_execution_authorized is not receipt_authorized
        ):
            raise ValueError(
                "caller execution flag differs from the server-bound external search receipt"
            )
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
        if receipt_authorized:
            return self._execute_authorized_staged(search_plan, universe)
        return ExternalKnowledgeRuntimeResult(
            outcome=ExternalKnowledgeOutcome(
                status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
                search_complete=False,
                reason_codes=("sealed_evidence_not_release_authorized",),
            ),
            search_plan=search_plan,
            candidate_universe=universe,
            acquisitions=(),
        )


__all__ = ["ExternalKnowledgeRuntime", "ExternalKnowledgeRuntimeResult"]
