from __future__ import annotations

import html
import json
import os
import re
import stat
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
    SourceAuthority,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import KnowledgeTimeRange
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate

_MAX_SECRET_BYTES = 4096
_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
_URL_DATE = re.compile(r"(?<!\d)(20\d{2})[_-]?(0[1-9]|1[0-2])[_-]?([0-2]\d|3[01])")
_SEARCH_MARKUP = re.compile(r"<[^>]{1,200}>")
_SEARCH_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _canonical_candidate_url(url: str) -> str:
    """Normalize transport/tracking noise without changing evidence identity."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname is None:
        return url
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    tracking_names = {
        "fbclid",
        "gbraid",
        "gclid",
        "wbraid",
        "_gl",
    }
    stable_query = urllib.parse.urlencode(
        tuple(
            (key, value)
            for key, value in query
            if key.casefold() not in tracking_names
            and not key.casefold().startswith(("utm_", "gad_"))
        ),
        doseq=True,
    )
    scheme = "https" if parsed.scheme == "http" else parsed.scheme
    return urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, stable_query, ""))


def _candidate_specificity(url: str) -> int:
    """Prefer concrete official documents/products over listing and home pages."""

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.casefold()
    score = min(3, path.count("/"))
    if path.endswith(".pdf"):
        score += 8
    if any(token in path for token in ("/view", "/detail", "/download", "/file")):
        score += 5
    if parsed.query and any(token in parsed.query.casefold() for token in ("id=", "fund", "fid=")):
        score += 3
    if path in {"", "/"} or any(token in path for token in ("/index", "/list")):
        score -= 5
    return score


_CAPABILITY_SEARCH_CUES: dict[KnowledgeCapability, tuple[str, str, str, str]] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: (
        "구성종목",
        "holdings",
        "구성종목 투자설명서",
        "factsheet portfolio",
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: (
        "자회사",
        "subsidiaries",
        # KIND's business report (사업보고서) lists 종속회사 in one table; the
        # disclosure viewer pages that "관계회사" surfaced carry no such table.
        "사업보고서 종속회사",
        "annual report subsidiaries",
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: (
        "테마",
        "theme ETF",
        "뉴스룸 리포트 테마",
        "news report theme",
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: (
        "펀드 구조 운용전략",
        "fund strategy",
        "보도자료 펀드 구조 전략",
        "fund report strategy",
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: (
        "투자위험",
        "risk factsheet",
        "투자설명서 위험요인",
        "prospectus key risks",
    ),
}

_DISCOVERY_DOCUMENT_CUES = re.compile(
    r"fact\s*sheet|factsheet|holdings?|portfolio|prospectus|"
    r"투자\s*설명서|간이\s*투자|구성\s*종목|보유\s*종목|상품\s*소개|"
    r"회사\s*소개|자회사|계열사|테마|theme|sector",
    re.IGNORECASE,
)


def _authority_priority(
    entry: OfficialSourceEntry,
    capability: KnowledgeCapability,
) -> int:
    """Prefer the primary official authority without excluding fallbacks."""

    if capability is KnowledgeCapability.CORPORATE_SUBSIDIARY:
        return {
            SourceAuthority.CORPORATE_OFFICIAL: 8,
            SourceAuthority.REGULATOR: 1,
            SourceAuthority.ASSET_MANAGER: 1,
            SourceAuthority.PRODUCT_ISSUER: 1,
        }[entry.authority]
    if capability in {
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    }:
        return (
            4
            if entry.authority
            in {
                SourceAuthority.ASSET_MANAGER,
                SourceAuthority.PRODUCT_ISSUER,
            }
            else 1
        )
    return (
        4
        if entry.authority
        in {
            SourceAuthority.REGULATOR,
            SourceAuthority.ASSET_MANAGER,
            SourceAuthority.PRODUCT_ISSUER,
        }
        else 1
    )


def _search_tokens(value: str) -> tuple[str, ...]:
    """Return stable exact tokens for metadata-only discovery ranking."""

    normalized = unicodedata.normalize("NFKC", urllib.parse.unquote(value)).casefold()
    return tuple(dict.fromkeys(_SEARCH_TOKEN.findall(normalized)))


def _normalized_search_surface(value: str) -> str:
    return "".join(_search_tokens(value))


def _entry_matches_query_subject(
    entry: OfficialSourceEntry,
    *,
    query_tokens: tuple[str, ...],
    query_surface: str,
) -> bool:
    """Return whether approved source metadata is grounded in the query.

    Discovery seeds are useful for dynamic official sites, but injecting every
    configured home/list page into a broad search lets unrelated publishers
    consume the bounded fetch window.  Exact token matching handles short
    brands safely, while bounded surface containment supports Korean compound
    forms such as ``에코프로비엠`` against the approved ``에코프로`` alias.
    """

    query_token_set = set(query_tokens)
    for value in (entry.publisher, *entry.selection_aliases):
        alias_tokens = _search_tokens(value)
        if alias_tokens and set(alias_tokens).issubset(query_token_set):
            return True
        alias_surface = _normalized_search_surface(value)
        if len(alias_surface) >= 4 and alias_surface in query_surface:
            return True
    return False


def _candidate_order_key(
    candidate: OfficialDiscoveryCandidate,
    *,
    query_tokens: tuple[str, ...],
    entity_terms: tuple[str, ...],
    product_terms: tuple[str, ...],
    relation_terms: tuple[str, ...],
    capability: KnowledgeCapability,
    source: OfficialSourceEntry,
    time_range: KnowledgeTimeRange | None,
    query_entity_provenance: bool,
    rank: int,
) -> tuple[int, int, int, int, int, int, int, int, int, int, str]:
    """Rank search metadata without treating it as answer evidence.

    Earlier server-query tokens receive more weight because the acquisition
    query starts with the validated relation subject. Exact token membership
    prevents short entities such as ``AI`` from matching unrelated words such
    as ``retail``. Title matches are stronger than URL matches; URL specificity
    and the provider rank break relevance ties deterministically.
    """

    summary = candidate.summary or ""
    title_tokens = set(_search_tokens(candidate.title))
    summary_tokens = set(_search_tokens(summary))
    url_tokens = set(_search_tokens(candidate.url))
    weights = {token: len(query_tokens) - index for index, token in enumerate(query_tokens)}
    title_matches = title_tokens.intersection(weights)
    summary_matches = summary_tokens.intersection(weights)
    url_matches = url_tokens.intersection(weights)
    title_score = sum(weights[token] for token in title_matches)
    summary_score = sum(weights[token] for token in summary_matches)
    url_score = sum(weights[token] for token in url_matches)
    matched_tokens = title_matches | summary_matches | url_matches
    searchable = _normalized_search_surface(" ".join((candidate.title, summary, candidate.url)))

    def group_score(terms: tuple[str, ...]) -> int:
        return sum(
            1
            for term in terms
            if len(_normalized_search_surface(term)) >= 2
            and _normalized_search_surface(term) in searchable
        )

    # Search providers do not always echo the exact query term in their
    # title/snippet even when their top result is the right official product
    # page.  Preserve one top result from an exact, site-scoped entity query as
    # a discovery candidate.  It is *not* evidence: fetched source bytes still
    # need the exact entity/relation/product span before execution.
    entity_score = max(
        group_score(entity_terms),
        int(query_entity_provenance and rank >= 0 and rank % 1000 == 0),
    )
    product_score = group_score(product_terms)
    relation_score = group_score(relation_terms)
    document_score = int(
        _DISCOVERY_DOCUMENT_CUES.search(" ".join((candidate.title, summary, candidate.url)))
        is not None
    )
    time_score = int(
        time_range is not None
        and candidate.published_at is not None
        and candidate.published_at <= time_range.end
        and (time_range.start is None or candidate.published_at >= time_range.start)
    )
    typed_relevance = (
        7 * entity_score
        + 9 * product_score
        + 3 * relation_score
        + 3 * document_score
        + 2 * time_score
        + 2 * _authority_priority(source, capability)
    )
    return (
        -typed_relevance,
        -product_score,
        -entity_score,
        -relation_score,
        -document_score,
        -_authority_priority(source, capability),
        -(title_score * 2 + summary_score + url_score),
        -len(matched_tokens),
        -_candidate_specificity(candidate.url),
        rank,
        candidate.url,
    )


def _quoted_search_term(value: str) -> str:
    normalized = " ".join(value.replace('"', " ").split())
    return f'"{normalized}"' if normalized else ""


def _exact_search_query(
    *,
    hostname: str,
    capability: KnowledgeCapability,
    entity_terms: tuple[str, ...],
    product_terms: tuple[str, ...],
    variant: int,
) -> str | None:
    """Build one bounded typed query; fetched bytes remain authoritative."""

    raw_entities = tuple(dict.fromkeys(item for item in entity_terms if item.strip()))[:2]
    entity_keys = {_normalized_search_surface(item) for item in raw_entities}
    raw_products = tuple(
        item
        for item in dict.fromkeys(item for item in product_terms if item.strip())
        if _normalized_search_surface(item) not in entity_keys
    )[:2]
    quoted_entities = tuple(_quoted_search_term(item) for item in raw_entities)
    quoted_products = tuple(_quoted_search_term(item) for item in raw_products)
    if not quoted_entities and not quoted_products:
        return None
    cues = _CAPABILITY_SEARCH_CUES[capability]
    has_hangul = re.search(r"[가-힣]", " ".join((*entity_terms, *product_terms))) is not None
    cue = cues[(0 if has_hangul else 1) if variant == 0 else (2 if has_hangul else 3)]
    return " ".join(
        (
            f"site:{hostname}",
            *quoted_entities,
            *quoted_products,
            cue,
        )
    )


def _clean_search_title(value: object, fallback: str) -> str:
    title = html.unescape(str(value or fallback))
    title = _SEARCH_MARKUP.sub("", title)
    return " ".join(title.split())[:500] or fallback[:500]


def _clean_search_summary(value: object) -> str | None:
    summary = html.unescape(str(value or ""))
    summary = _SEARCH_MARKUP.sub("", summary)
    normalized = " ".join(summary.split())[:2000]
    return normalized or None


class OfficialDiscoveryError(RuntimeError):
    """Raised when official-only discovery cannot complete safely."""


class DiscoveryFailureCode(StrEnum):
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"
    RESULT_WINDOW_INCOMPLETE = "result_window_incomplete"
    SOURCE_SCOPE_INCOMPLETE = "source_scope_incomplete"


class OfficialDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialDiscoveryReport(OfficialDiscoveryModel):
    candidates: tuple[OfficialDiscoveryCandidate, ...] = Field(default=(), max_length=40)
    queried_source_registry_ids: tuple[str, ...]
    search_query_count: int = Field(default=0, ge=0, le=40)
    exhaustive: bool
    failure_codes: tuple[DiscoveryFailureCode, ...] = ()

    @model_validator(mode="after")
    def validate_report(self) -> OfficialDiscoveryReport:
        if self.queried_source_registry_ids != tuple(sorted(set(self.queried_source_registry_ids))):
            raise ValueError("queried source IDs must be unique and sorted")
        if self.failure_codes != tuple(sorted(set(self.failure_codes), key=str)):
            raise ValueError("discovery failure codes must be unique and sorted")
        if self.exhaustive and self.failure_codes:
            raise ValueError("an exhaustive discovery report cannot carry failures")
        return self


class OfficialWebSearchPage(OfficialDiscoveryModel):
    total: int = Field(ge=0)
    start: int = Field(ge=1)
    display: int = Field(ge=0, le=100)
    items: tuple[OfficialDiscoveryCandidate, ...] = Field(default=(), max_length=100)


class OfficialWebSearchTransport(Protocol):
    def search(self, *, query: str, display: int, start: int) -> OfficialWebSearchPage: ...


def _read_secret_file(path: str | Path) -> str:
    secret_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(secret_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or metadata.st_size > _MAX_SECRET_BYTES
        ):
            raise OfficialDiscoveryError("discovery credential file is invalid")
        value = os.read(descriptor, _MAX_SECRET_BYTES + 1).decode("utf-8").strip()
    finally:
        os.close(descriptor)
    if not value or len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise OfficialDiscoveryError("discovery credential file is empty or oversized")
    return value


class NaverApiHubWebSearchTransport:
    """NAVER API HUB web search; result snippets are candidate metadata only."""

    def __init__(
        self,
        *,
        client_id_file: str | Path,
        client_secret_file: str | Path,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("search timeout must be between 0.1 and 30 seconds")
        self.client_id = _read_secret_file(client_id_file)
        self.client_secret = _read_secret_file(client_secret_file)
        self.timeout_seconds = timeout_seconds

    def search(self, *, query: str, display: int, start: int) -> OfficialWebSearchPage:
        if not query.strip() or not 1 <= display <= 100 or not 1 <= start <= 1000:
            raise ValueError("invalid official web search request")
        url = (
            _API_URL
            + "?"
            + urllib.parse.urlencode(
                {"query": query, "display": display, "start": start, "sort": "sim"}
            )
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-NCP-APIGW-API-KEY-ID": self.client_id,
                "X-NCP-APIGW-API-KEY": self.client_secret,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise OfficialDiscoveryError("official search provider returned non-200")
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OfficialDiscoveryError("official search provider request failed") from error
        if len(raw) > 2 * 1024 * 1024:
            raise OfficialDiscoveryError("official search provider response is oversized")
        try:
            payload = json.loads(raw)
            items = tuple(
                OfficialDiscoveryCandidate(
                    url=html.unescape(str(item["link"])),
                    title=_clean_search_title(item.get("title"), str(item["link"])),
                    summary=_clean_search_summary(item.get("description")),
                )
                for item in payload.get("items", [])
            )
            return OfficialWebSearchPage(
                total=int(payload["total"]),
                start=int(payload["start"]),
                display=int(payload["display"]),
                items=items,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OfficialDiscoveryError("official search provider response is invalid") from error


_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_DOMESTIC_FAMILIES = frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND, ProductFamily.BOND})


def order_entries_for_entity_script(
    entries: tuple[OfficialSourceEntry, ...] | list[OfficialSourceEntry],
    *,
    entity_terms: tuple[str, ...],
    product_surfaces: tuple[str, ...] = (),
) -> tuple[OfficialSourceEntry, ...]:
    """Spend a bounded query budget on publishers that can name the entity.

    Two server-owned signals order the *same* approved entries; the registry
    stays the only authority on which publishers exist.

    1. Script.  A Hangul-only company such as ``에코프로비엠`` is disclosed by
       Korean asset managers, not by a US issuer's English factsheet; a
       Latin-only ticker or name is the reverse.  Mixed or unknown scripts
       skip this signal.
    2. Universe weight.  Publishers whose brand aliases (``KODEX``, ``TIGER``,
       ``VanEck`` …) appear in more provided-DB candidate product names are
       queried first: they are the issuers most likely to hold the entity.

    Ties keep registry order.
    """

    joined = " ".join(entity_terms)
    hangul = _HANGUL.search(joined) is not None
    latin = _LATIN.search(joined) is not None
    surfaces = tuple(_normalized_search_surface(item) for item in product_surfaces if item)

    def script_rank(entry: OfficialSourceEntry) -> int:
        if hangul == latin:
            return 0
        families = set(entry.product_families)
        domestic = bool(families & _DOMESTIC_FAMILIES)
        overseas = ProductFamily.OVERSEAS_ETP in families
        if not families or (domestic and overseas):
            return 1
        if hangul:
            return 0 if domestic else 2
        return 0 if overseas else 2

    def brand_weight(entry: OfficialSourceEntry) -> int:
        if not surfaces:
            return 0
        aliases = tuple(
            _normalized_search_surface(alias)
            for alias in (*entry.selection_aliases, entry.publisher)
            if len(alias.strip()) >= 2
        )
        return sum(1 for surface in surfaces if any(alias in surface for alias in aliases))

    indexed = list(enumerate(entries))
    indexed.sort(key=lambda item: (script_rank(item[1]), -brand_weight(item[1]), item[0]))
    return tuple(entry for _, entry in indexed)


class BoundedOfficialDiscoveryProvider:
    """Search only approved domains, with server-owned query and result budgets.

    A general web index is deliberately never marked exhaustive.  Therefore an
    empty result from this provider alone can produce CANNOT_VERIFY, never
    NOT_FOUND.
    """

    def __init__(
        self,
        transport: OfficialWebSearchTransport,
        registry: OfficialSourceRegistry | None = None,
    ) -> None:
        self.transport = transport
        self.registry = registry or load_official_source_registry()

    def discover_report(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
        max_queries: int | None = None,
        entity_terms: tuple[str, ...] = (),
        product_terms: tuple[str, ...] = (),
        entity_term_groups: tuple[tuple[str, ...], ...] = (),
        product_term_groups: tuple[tuple[str, ...], ...] = (),
        relation_terms: tuple[str, ...] = (),
        time_range: KnowledgeTimeRange | None = None,
        product_surfaces: tuple[str, ...] = (),
    ) -> OfficialDiscoveryReport:
        if evidence_cutoff != self.registry.evidence_cutoff:
            raise OfficialDiscoveryError("discovery evidence cutoff differs from registry")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        entries = order_entries_for_entity_script(
            tuple(
                entry
                for entry in self.registry.entries_for_capability(
                    capability,
                    source_registry_ids=source_registry_ids,
                )
                if entry.status == "approved_for_live_evidence"
            ),
            entity_terms=entity_terms,
            product_surfaces=product_surfaces,
        )
        # Snapshot-intake sources may enter an approved immutable corpus after
        # review, but they are never live-search authorities. A registry that
        # contains only intake sources therefore produces a bounded empty miss
        # without issuing a network request.
        if not entries:
            return OfficialDiscoveryReport(
                candidates=(),
                queried_source_registry_ids=(),
                search_query_count=0,
                exhaustive=False,
                failure_codes=(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE,),
            )
        query_budget = len(entries) if max_queries is None else max_queries
        if not 1 <= query_budget <= 40:
            raise ValueError("max_queries must be between 1 and 40")
        entity_terms = tuple(dict.fromkeys(item.strip() for item in entity_terms if item.strip()))
        product_terms = tuple(dict.fromkeys(item.strip() for item in product_terms if item.strip()))
        # Target grouping is consumed by the cache-first orchestrator. The live
        # provider still accepts the typed contract so a forced cache-miss path
        # cannot fail by signature, while its concrete query remains bounded by
        # the already narrowed flat entity/product terms.
        _ = entity_term_groups, product_term_groups
        relation_terms = tuple(
            dict.fromkeys(item.strip() for item in relation_terms if item.strip())
        )
        query_tokens = _search_tokens(query)
        query_surface = _normalized_search_surface(query)
        # Several registered hosts can belong to the same publisher (for
        # example product/mobile/document hosts).  In that already-targeted
        # case, their bounded seeds remain useful even when the relation
        # subject itself is a holding company rather than the publisher name.
        targeted_publisher = (
            len(
                {
                    (
                        _normalized_search_surface(entry.publisher),
                        entry.authority,
                    )
                    for entry in entries
                }
            )
            == 1
        )
        candidate_buckets: dict[
            str,
            list[
                tuple[
                    tuple[int, int, int, int, int, int, int, int, int, int, str],
                    OfficialDiscoveryCandidate,
                ]
            ],
        ] = {entry.source_id: [] for entry in entries}
        queried: list[str] = []
        failures: list[DiscoveryFailureCode] = []
        seen: set[str] = set()
        requested_source_ids = {entry.source_id for entry in entries}
        # Some official pages are intentionally registered as bounded
        # discovery seeds because general web indexes do not reliably expose
        # dynamic forums or document endpoints.  A seed is candidate metadata
        # only: the connector still fetches it and the exact-span/date verifier
        # must approve the original bytes before it can become evidence.
        for entry in entries:
            if not targeted_publisher and not _entry_matches_query_subject(
                entry,
                query_tokens=query_tokens,
                query_surface=query_surface,
            ):
                continue
            for seed_url in entry.discovery_urls:
                candidate = OfficialDiscoveryCandidate(
                    url=seed_url,
                    title=f"{entry.publisher} official source",
                )
                candidate_buckets[entry.source_id].append(
                    (
                        _candidate_order_key(
                            candidate,
                            query_tokens=query_tokens,
                            entity_terms=entity_terms,
                            product_terms=product_terms,
                            relation_terms=relation_terms,
                            capability=capability,
                            source=entry,
                            time_range=time_range,
                            query_entity_provenance=False,
                            rank=-1,
                        ),
                        candidate,
                    )
                )
                seen.add(seed_url)

        query_attempt_count = 0
        attempted_queries: set[tuple[str, str]] = set()

        def search_entry(entry: OfficialSourceEntry, search_query: str) -> None:
            nonlocal query_attempt_count
            # One site-scoped request covers every approved path registered on
            # the same hostname.  Counting duplicate registry paths as network
            # calls wasted the bounded budget and prevented the factsheet
            # fallback query from running.
            key = (entry.hostname, search_query)
            if key in attempted_queries or query_attempt_count >= query_budget:
                return
            attempted_queries.add(key)
            query_attempt_count += 1
            queried.extend(item.source_id for item in entries if item.hostname == entry.hostname)
            try:
                page = self.transport.search(
                    query=search_query,
                    display=min(100, max(20, top_k * 4)),
                    start=1,
                )
            except OfficialDiscoveryError:
                failures.append(DiscoveryFailureCode.PROVIDER_ERROR)
                return
            query_entity_provenance = any(
                quoted and quoted in search_query
                for value in entity_terms
                if (quoted := _quoted_search_term(value))
            )
            for rank, raw_candidate in enumerate(page.items):
                candidate = raw_candidate.model_copy(
                    update={"url": _canonical_candidate_url(raw_candidate.url)}
                )
                try:
                    authorized = self.registry.authorize_url(candidate.url, capability)
                except OfficialSourceRegistryError:
                    continue
                if authorized.source_id not in requested_source_ids or candidate.url in seen:
                    continue
                published_at = candidate.published_at
                if published_at is None:
                    match = _URL_DATE.search(urllib.parse.unquote(candidate.url))
                    if match is not None:
                        try:
                            published_at = date(
                                int(match.group(1)),
                                int(match.group(2)),
                                int(match.group(3)),
                            )
                        except ValueError:
                            published_at = None
                admitted = candidate.model_copy(
                    update={
                        "published_at": published_at,
                        # A date encoded in a URL is publication metadata, not
                        # necessarily the evidence date printed inside the
                        # document. The connector independently extracts and
                        # verifies ``as_of`` from the fetched official bytes.
                        "as_of": candidate.as_of,
                    }
                )
                candidate_buckets[authorized.source_id].append(
                    (
                        _candidate_order_key(
                            admitted,
                            query_tokens=query_tokens,
                            entity_terms=entity_terms,
                            product_terms=product_terms,
                            relation_terms=relation_terms,
                            capability=capability,
                            source=authorized,
                            time_range=time_range,
                            query_entity_provenance=query_entity_provenance,
                            rank=query_attempt_count * 1000 + rank,
                        ),
                        admitted,
                    )
                )
                seen.add(candidate.url)

        # First cover every approved source once.  When the caller supplies a
        # typed entity/product boundary, use the exact relation-aware query as
        # the primary request.  Untyped callers retain the legacy broad query.
        # Fallback variants may use only the remaining budget; they can never
        # hide a later publisher merely because an earlier source is noisy.
        for entry in entries:
            if query_attempt_count >= query_budget:
                break
            primary_query = _exact_search_query(
                hostname=entry.hostname,
                capability=capability,
                entity_terms=entity_terms,
                product_terms=product_terms,
                variant=0,
            )
            search_entry(
                entry,
                primary_query or f"site:{entry.hostname} {query.strip()}",
            )
        # A second exact document-oriented query is more valuable than the
        # broad natural-language fallback for PDF/factsheet-heavy publishers.
        # Run it first while keeping the broad query as the final safety net.
        for variant in (1, -1):
            for entry in entries:
                if query_attempt_count >= query_budget:
                    break
                fallback_query = (
                    f"site:{entry.hostname} {query.strip()}"
                    if variant < 0
                    else _exact_search_query(
                        hostname=entry.hostname,
                        capability=capability,
                        entity_terms=entity_terms,
                        product_terms=product_terms,
                        variant=variant,
                    )
                )
                if fallback_query is not None:
                    search_entry(entry, fallback_query)
        ranked_buckets: list[
            tuple[
                tuple[int, int, int, int, int, int, int, int, int, int, str],
                list[
                    tuple[
                        tuple[int, int, int, int, int, int, int, int, int, int, str],
                        OfficialDiscoveryCandidate,
                    ]
                ],
            ]
        ] = []
        for entry in entries:
            bucket = candidate_buckets[entry.source_id]
            if not bucket:
                continue
            bucket.sort(key=lambda item: item[0])
            ranked_buckets.append((bucket[0][0], bucket))
        # Emit every metadata candidate that actually matches a query token
        # before generic official pages.  This prevents unrelated first-round
        # buckets from pushing a second, highly relevant result outside the
        # acquisition top-k.  Remaining results are still interleaved by source
        # to preserve publisher diversity under a bounded fetch window.
        ranked_buckets.sort(key=lambda item: item[0])
        relevant = sorted(
            (
                item
                for _, bucket in ranked_buckets
                for item in bucket
                if (item[0][1] < 0 or item[0][2] < 0 or item[0][6] < 0)
            ),
            key=lambda item: item[0],
        )
        candidates = [item[1] for item in relevant[:40]]
        relevant_urls = {candidate.url for candidate in candidates}
        ordered_buckets = [
            [item[1] for item in bucket if item[1].url not in relevant_urls]
            for _, bucket in ranked_buckets
        ]
        depth = 0
        while len(candidates) < 40 and any(depth < len(bucket) for bucket in ordered_buckets):
            for bucket in ordered_buckets:
                if depth < len(bucket):
                    candidates.append(bucket[depth])
                    if len(candidates) == 40:
                        break
            depth += 1
        if len(set(queried)) < len(entries):
            failures.append(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE)
        return OfficialDiscoveryReport(
            candidates=tuple(candidates),
            queried_source_registry_ids=tuple(sorted(set(queried))),
            search_query_count=query_attempt_count,
            # Search-engine indexes cannot prove that an official domain contains
            # no matching document, even when every configured domain was queried.
            exhaustive=False,
            failure_codes=tuple(sorted(set(failures), key=str)),
        )

    def discover(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        return self.discover_report(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
        ).candidates


__all__ = [
    "BoundedOfficialDiscoveryProvider",
    "DiscoveryFailureCode",
    "NaverApiHubWebSearchTransport",
    "OfficialDiscoveryError",
    "OfficialDiscoveryReport",
    "OfficialWebSearchPage",
    "OfficialWebSearchTransport",
]
