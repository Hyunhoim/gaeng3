from __future__ import annotations

import re
import unicodedata
import urllib.parse
from datetime import date
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
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


_IDENTITY_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_GLOBALX_FACTSHEET_PATH = re.compile(
    r"^/(?:dev/documents|funds/documents)/"
    r"(?P<ticker>[a-z][a-z0-9-]{0,9})/(?P<filename>[^/]{1,240}\.pdf)$",
    re.IGNORECASE,
)
_DB_TICKER_SURFACE = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9-]{0,9})(?:\.[A-Z0-9]{1,6})?$",
    re.IGNORECASE,
)


def _identity_key(value: str) -> str:
    return "".join(
        _IDENTITY_TOKEN.findall(
            unicodedata.normalize("NFKC", urllib.parse.unquote(value)).casefold()
        )
    )


def _snapshot_product_evidence_date(
    snapshot: OfficialSourceSnapshot | OfficialNavigationSnapshot,
    bindings: tuple[tuple[str, date], ...],
) -> tuple[date | None, bool]:
    """Return one DB-bound product date and whether identity matching succeeded.

    Matching is intentionally restricted to discovery title and connector-
    verified ticker provenance.  Page body text can mention neighbouring
    products and must not choose a historical endpoint date.
    """

    if not bindings:
        return None, False
    ticker_key = _identity_key(snapshot.db_ticker or "")
    title_tokens = _IDENTITY_TOKEN.findall(
        unicodedata.normalize("NFKC", snapshot.title).casefold()
    )[:40]
    title_identity_keys: set[str] = set()
    for start in range(len(title_tokens)):
        combined = ""
        for end in range(start, min(len(title_tokens), start + 12)):
            combined += title_tokens[end]
            if len(combined) >= 2:
                title_identity_keys.add(combined)
    ticker_dates = {
        bound_date for key, bound_date in bindings if ticker_key and key == ticker_key
    }
    if ticker_dates:
        return (next(iter(ticker_dates)), True) if len(ticker_dates) == 1 else (None, True)
    title_dates = {
        bound_date
        for key, bound_date in bindings
        if len(key) >= 2 and key in title_identity_keys
    }
    if title_dates:
        return (next(iter(title_dates)), True) if len(title_dates) == 1 else (None, True)
    return None, False


def _candidate_product_identity_score(
    candidate: OfficialDiscoveryCandidate,
    provided_identity_keys: frozenset[str],
) -> int:
    """Score only literal product identities already present in provided DB.

    NAVER may rank the correct official product page first without repeating
    the holding entity in its snippet.  This local matcher never widens the DB
    universe: it promotes a candidate only when a contiguous title/URL token
    span is an exact provided product name, ticker or identifier.
    """

    if not provided_identity_keys:
        return 0
    tokens = _IDENTITY_TOKEN.findall(
        unicodedata.normalize(
            "NFKC",
            urllib.parse.unquote(" ".join((candidate.title, candidate.url))),
        ).casefold()
    )[:40]
    best = 0
    for start in range(len(tokens)):
        combined = ""
        for end in range(start, min(len(tokens), start + 12)):
            combined += tokens[end]
            if len(combined) >= 2 and combined in provided_identity_keys:
                best = max(best, min(len(combined), 80))
    return best


def _expand_globalx_product_roots(
    candidates: tuple[OfficialDiscoveryCandidate, ...],
    *,
    provided_product_surfaces: tuple[str, ...],
    requested_source_ids: frozenset[str],
    capability: KnowledgeCapability,
    registry: OfficialSourceRegistry,
) -> tuple[OfficialDiscoveryCandidate, ...]:
    """Bridge one approved Global X factsheet to its DB-bound product root.

    The bridge is deliberately candidate-driven: it never enumerates product
    pages from the thousands of provided DB rows.  A ticker must occur in both
    an approved Global X factsheet path and an exact DB identity surface
    (``TICKER`` or ``TICKER.market``).  The target product URL is then separately
    authorized and must preserve publisher and authority.
    """

    if capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS:
        return candidates
    db_tickers = {
        matched.group("ticker").upper()
        for value in provided_product_surfaces
        if (matched := _DB_TICKER_SURFACE.fullmatch(unicodedata.normalize("NFKC", value).strip()))
    }
    if not db_tickers:
        return candidates

    expanded: list[OfficialDiscoveryCandidate] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        parsed = urllib.parse.urlsplit(candidate.url)
        factsheet_match = _GLOBALX_FACTSHEET_PATH.fullmatch(
            urllib.parse.unquote(parsed.path)
        )
        ticker: str | None = None
        source = None
        if factsheet_match is not None and not parsed.query and not parsed.fragment:
            candidate_ticker = factsheet_match.group("ticker").upper()
            filename = factsheet_match.group("filename")
            ticker_in_filename = re.search(
                rf"(?:^|[^A-Z0-9]){re.escape(candidate_ticker)}(?:[^A-Z0-9]|$)",
                filename,
                re.IGNORECASE,
            )
            if re.search(r"facts?[\s_-]*sheet", filename, re.IGNORECASE) and ticker_in_filename:
                try:
                    source = registry.authorize_url(candidate.url, capability)
                except OfficialSourceRegistryError:
                    source = None
                if (
                    source is not None
                    and source.source_id == "globalx-etf-factsheets"
                    and source.source_id in requested_source_ids
                    and candidate_ticker in db_tickers
                ):
                    ticker = candidate_ticker

        if ticker is not None and source is not None:
            product_url = f"https://www.globalxetfs.com/funds/{ticker.casefold()}"
            try:
                target_source = registry.authorize_url(product_url, capability)
            except OfficialSourceRegistryError:
                target_source = None
            if (
                target_source is not None
                and target_source.source_id == "globalx-etf-official"
                and target_source.source_id in requested_source_ids
                and target_source.publisher == source.publisher
                and target_source.authority == source.authority
                and product_url not in seen_urls
            ):
                expanded.append(
                    OfficialDiscoveryCandidate(
                        url=product_url,
                        title=f"{ticker} Global X official product",
                        db_ticker=ticker,
                    )
                )
                seen_urls.add(product_url)
            candidate = candidate.model_copy(update={"db_ticker": ticker})
        if candidate.url not in seen_urls:
            expanded.append(candidate)
            seen_urls.add(candidate.url)
    return tuple(expanded[:20])


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
        provided_product_surfaces: tuple[str, ...] = (),
        provided_product_evidence_dates: tuple[tuple[str, date], ...] = (),
        top_k: int = 8,
        max_search_queries: int = 8,
        max_fetches: int = 8,
        max_total_bytes: int = 16 * 1024 * 1024,
        deadline_ms: int = 12_000,
        force_live_discovery: bool = False,
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
        if (
            len(provided_product_surfaces) > 250_000
            or len(provided_product_surfaces) != len(set(provided_product_surfaces))
            or any(not value.strip() or len(value) > 500 for value in provided_product_surfaces)
        ):
            raise ValueError("provided product surfaces must be unique and bounded")
        if (
            len(provided_product_evidence_dates) > 5_000
            or len(provided_product_evidence_dates)
            != len(set(provided_product_evidence_dates))
            or any(
                not key
                or len(key) > 500
                or key != _identity_key(key)
                for key, bound_date in provided_product_evidence_dates
            )
        ):
            raise ValueError("provided product evidence dates are invalid or unbounded")
        if any(
            bound_date > self.connector.registry.evidence_cutoff
            for _key, bound_date in provided_product_evidence_dates
        ):
            return OfficialEvidenceAcquisitionResult(
                status=OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY,
                discovered_candidate_count=0,
                rejected_candidate_count=0,
                search_complete=False,
                failure_codes=("product_evidence_date_after_cutoff",),
            )
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        if not 1 <= max_search_queries <= 40 or not 1 <= max_fetches <= 40:
            raise ValueError("acquisition query/fetch budgets are out of bounds")
        if not 1 <= max_total_bytes <= 64 * 1024 * 1024:
            raise ValueError("acquisition byte budget is out of bounds")
        if not 100 <= deadline_ms <= 60_000:
            raise ValueError("acquisition deadline is out of bounds")
        deadline = perf_counter() + deadline_ms / 1000
        cached = (
            ()
            if provided_product_evidence_dates
            else self.relation_store.search(
                TemporalRelationSearchRequest(
                    capability=requirement.capability,
                    subject=requirement.subject,
                    product_families=requirement.product_families,
                    time_range=requirement.time_range,
                    top_k=min(top_k, 50),
                )
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
        report_method = getattr(
            self.discovery_provider,
            "discover_live_report" if force_live_discovery else "discover_report",
            None,
        )
        if force_live_discovery and not callable(report_method):
            raise ValueError("discovery provider does not support an explicit live fallback")
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
                    entity_terms=tuple(
                        dict.fromkeys((requirement.subject, *requirement.search_aliases))
                    ),
                    product_terms=discovery_terms,
                    relation_terms=(requirement.relation_evidence_span,),
                    time_range=requirement.time_range,
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
                    search_query_count=1,
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

        discovered_pool = _expand_globalx_product_roots(
            tuple(discovered_pool),
            provided_product_surfaces=provided_product_surfaces,
            requested_source_ids=frozenset(requirement.source_registry_ids),
            capability=requirement.capability,
            registry=self.connector.registry,
        )
        provided_identity_keys = frozenset(
            key for value in provided_product_surfaces if len(key := _identity_key(value)) >= 2
        )
        discovered_pool = tuple(
            candidate
            for _, candidate in sorted(
                enumerate(discovered_pool),
                key=lambda item: (
                    -_candidate_product_identity_score(item[1], provided_identity_keys),
                    item[0],
                ),
            )
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
            max_candidates=min(20, max(top_k, top_k * 3)),
        )
        # Preserve global provider relevance while reserving root diversity.
        # A strict round-robin promoted generic pages over an exact factsheet;
        # allowing one publisher to occupy every slot had the opposite failure
        # mode.  Cap a source at half the root window on the first pass, then
        # fill any genuinely unused slots in provider order.
        candidate_source_ids: dict[str, str] = {}
        for candidate in authorized:
            source_id = self.connector.registry.authorize_url(
                candidate.url,
                requirement.capability,
            ).source_id
            candidate_source_ids[candidate.url] = source_id
        available_source_count = len(set(candidate_source_ids.values()))
        first_pass_cap = max(1, (top_k + 1) // 2)
        selected: list[OfficialDiscoveryCandidate] = []
        selected_urls: set[str] = set()
        selected_by_source: dict[str, int] = {}
        for candidate in authorized:
            source_id = candidate_source_ids[candidate.url]
            if (
                available_source_count > 1
                and selected_by_source.get(source_id, 0) >= first_pass_cap
            ):
                continue
            selected.append(candidate)
            selected_urls.add(candidate.url)
            selected_by_source[source_id] = selected_by_source.get(source_id, 0) + 1
            if len(selected) == top_k:
                break
        if len(selected) < top_k:
            for candidate in authorized:
                if candidate.url in selected_urls:
                    continue
                selected.append(candidate)
                selected_urls.add(candidate.url)
                if len(selected) == top_k:
                    break
        authorized = tuple(selected)
        active_buckets: dict[str, list[OfficialDiscoveryCandidate]] = {}
        active_source_order: list[str] = []
        for candidate in authorized:
            source_id = self.connector.registry.authorize_url(
                candidate.url,
                requirement.capability,
            ).source_id
            if source_id not in active_buckets:
                active_buckets[source_id] = []
                active_source_order.append(source_id)
            active_buckets[source_id].append(candidate)
        buckets = active_buckets
        source_order = active_source_order
        snapshots: list[OfficialSourceSnapshot] = []
        rejected = len(discovered_pool) - len(authorized)
        fetched_bytes = 0
        fetch_attempt_count = 0
        fetch_failure_codes: set[str] = set()
        seen_candidate_urls: set[str] = set()
        seen_snapshot_urls: set[str] = set()
        stop_acquisition = False
        # A registered discovery result is a trust root, not necessarily the
        # evidence document itself.  Some official sites use a mobile/list page
        # that links to a desktop product page, which then links to a dated
        # PDF/API.  Explore at most two official link hops with one global
        # visited set and the existing request/byte/deadline budgets.
        source_count = max(1, len(source_order))
        source_fetch_caps = {
            source_id: max(1, max_fetches // source_count) for source_id in source_order
        }
        for source_id in source_order[: max_fetches % source_count]:
            source_fetch_caps[source_id] += 1
        source_fetch_usage = {source_id: 0 for source_id in source_order}
        roots_per_source = {source_id: len(buckets[source_id]) for source_id in source_order}
        for root_candidate in authorized:
            if stop_acquisition:
                rejected += 1
                continue
            if fetch_attempt_count >= max_fetches:
                rejected += 1
                continue
            root_source_id = self.connector.registry.authorize_url(
                root_candidate.url,
                requirement.capability,
            ).source_id
            source_cap = source_fetch_caps[root_source_id]
            root_cap = max(
                2,
                min(
                    3,
                    max(
                        1,
                        source_cap // max(1, roots_per_source[root_source_id]),
                    ),
                ),
            )
            root_fetch_usage = 0
            frontier: list[tuple[OfficialDiscoveryCandidate, int]] = [(root_candidate, 0)]
            while frontier:
                if source_fetch_usage[root_source_id] >= source_cap or root_fetch_usage >= root_cap:
                    fetch_failure_codes.add("per_source_fetch_budget_exhausted")
                    break
                candidate, depth = frontier.pop()
                if candidate.url in seen_candidate_urls:
                    continue
                seen_candidate_urls.add(candidate.url)
                try:
                    source = self.connector.registry.authorize_url(
                        candidate.url,
                        requirement.capability,
                    )
                except OfficialSourceRegistryError:
                    if depth == 0:
                        rejected += 1
                    else:
                        fetch_failure_codes.add("linked_document_source_rejected")
                    continue
                if source.source_id not in requirement.source_registry_ids:
                    if depth == 0:
                        rejected += 1
                    else:
                        fetch_failure_codes.add("linked_document_source_not_selected")
                    continue

                snapshot: OfficialSourceSnapshot | OfficialNavigationSnapshot | None = None
                attempts = 1 if depth == 0 else 2
                for _attempt in range(attempts):
                    remaining_seconds = deadline - perf_counter()
                    remaining_bytes = max_total_bytes - fetched_bytes
                    if remaining_seconds <= 0:
                        fetch_failure_codes.add("acquisition_deadline_exceeded")
                        stop_acquisition = True
                        break
                    if remaining_bytes <= 0:
                        fetch_failure_codes.add("acquisition_byte_budget_exceeded")
                        stop_acquisition = True
                        break
                    if fetch_attempt_count >= max_fetches:
                        fetch_failure_codes.add("linked_document_budget_exhausted")
                        break
                    try:
                        fetch_attempt_count += 1
                        root_fetch_usage += 1
                        source_fetch_usage[root_source_id] += 1
                        snapshot = self.connector.fetch(
                            candidate,
                            OfficialFetchRequest(
                                url=candidate.url,
                                capability=requirement.capability,
                                published_at=candidate.published_at,
                                as_of=candidate.as_of,
                                timeout_seconds=min(30.0, max(0.1, remaining_seconds)),
                                max_bytes=min(16 * 1024 * 1024, remaining_bytes),
                                # Every intermediate page is navigation-only
                                # until it supplies an independently verified
                                # date; linked pages must not be privileged over
                                # the initial discovery root.
                                allow_navigation_only=True,
                            ),
                        )
                        break
                    except (OfficialConnectorError, OfficialSourceRegistryError):
                        continue
                if snapshot is None:
                    if stop_acquisition:
                        frontier.clear()
                        break
                    if depth == 0:
                        rejected += 1
                    else:
                        fetch_failure_codes.add("linked_document_fetch_failed")
                    continue

                fetched_bytes += len(snapshot.content)
                seen_candidate_urls.add(snapshot.source_uri)
                if (
                    not isinstance(snapshot, OfficialNavigationSnapshot)
                    and snapshot.source_uri not in seen_snapshot_urls
                ):
                    snapshots.append(snapshot)
                    seen_snapshot_urls.add(snapshot.source_uri)
                if (
                    depth >= 2
                    or fetch_attempt_count >= max_fetches
                    or root_fetch_usage >= root_cap
                    or source_fetch_usage[root_source_id] >= source_cap
                ):
                    continue
                try:
                    product_evidence_date, _ = _snapshot_product_evidence_date(
                        snapshot,
                        provided_product_evidence_dates,
                    )
                    if (
                        provided_product_evidence_dates
                        and requirement.capability
                        is KnowledgeCapability.PORTFOLIO_HOLDINGS
                        and requirement.capability in source.historical_link_capabilities
                        and product_evidence_date is None
                    ):
                        fetch_failure_codes.add("historical_product_date_identity_unresolved")
                        continue
                    linked = self.link_resolver.resolve(
                        snapshot,
                        # Link resolution itself is local and cheap. Do not
                        # truncate it to the remaining network budget because
                        # a cycle or already-visited link at the front of the
                        # page could otherwise hide the next valid document.
                        max_links=8,
                        evidence_as_of=(
                            product_evidence_date
                            or (
                                requirement.time_range.end
                                if requirement.time_range is not None
                                else None
                            )
                        ),
                    )
                except (OfficialConnectorError, OfficialSourceRegistryError):
                    fetch_failure_codes.add("linked_document_source_rejected")
                    continue
                admitted_links: list[OfficialDiscoveryCandidate] = []
                for linked_candidate in linked:
                    if linked_candidate.url in seen_candidate_urls:
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
                    admitted_links.append(linked_candidate)
                # Depth-first traversal gives an official product-page chain a
                # chance to reach its dated evidence before generic siblings
                # consume the bounded global fetch budget. Resolver order is
                # retained by reversing only for stack insertion.
                frontier.extend(
                    (linked_candidate, depth + 1) for linked_candidate in reversed(admitted_links)
                )
            if stop_acquisition:
                continue
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
                search_query_count=(
                    report.search_query_count or len(report.queried_source_registry_ids)
                ),
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
                    *fetch_failure_codes,
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
            search_query_count=(
                report.search_query_count or len(report.queried_source_registry_ids)
            ),
            fetch_count=fetch_attempt_count,
            fetched_bytes=fetched_bytes,
        )


__all__ = [
    "OfficialDiscoveryProvider",
    "OfficialEvidenceAcquisitionCoordinator",
    "OfficialEvidenceAcquisitionResult",
    "OfficialEvidenceAcquisitionStatus",
]
