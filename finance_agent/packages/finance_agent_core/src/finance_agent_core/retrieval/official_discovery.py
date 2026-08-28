from __future__ import annotations

import html
import json
import os
import re
import stat
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
    load_official_source_registry,
)
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate

_MAX_SECRET_BYTES = 4096
_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
_URL_DATE = re.compile(
    r"(?<!\d)(20\d{2})[_-]?(0[1-9]|1[0-2])[_-]?([0-2]\d|3[01])"
)


def _canonical_candidate_url(url: str) -> str:
    """Upgrade an HTTP search result to the same host/path HTTPS candidate."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        return url
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc, parsed.path, parsed.query, "")
    )


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


def _bounded_registry_terms(entry: OfficialSourceEntry, query: str) -> tuple[str, ...]:
    values = (entry.publisher, *entry.selection_aliases)
    seen: set[str] = set()
    selected: list[str] = []
    folded_query = query.casefold()
    for value in values:
        folded = value.casefold()
        if folded in seen or folded in folded_query:
            continue
        selected.append(value)
        seen.add(folded)
        if len(selected) == 2:
            break
    return tuple(selected)


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
                    title=html.unescape(str(item.get("title") or item["link"])),
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
    ) -> OfficialDiscoveryReport:
        if evidence_cutoff != self.registry.evidence_cutoff:
            raise OfficialDiscoveryError("discovery evidence cutoff differs from registry")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        entries = self.registry.entries_for_capability(
            capability,
            source_registry_ids=source_registry_ids,
        )
        query_budget = len(entries) if max_queries is None else max_queries
        if query_budget < 1:
            raise ValueError("max_queries must be positive")
        candidate_buckets: dict[str, list[tuple[int, int, OfficialDiscoveryCandidate]]] = {
            entry.source_id: [] for entry in entries
        }
        queried: list[str] = []
        failures: list[DiscoveryFailureCode] = []
        seen: set[str] = set()
        requested_source_ids = {entry.source_id for entry in entries}
        for entry in entries[:query_budget]:
            queried.append(entry.source_id)
            # ``query`` already contains the server-validated subject and at
            # most three bounded aliases. Appending every relation synonym at
            # once caused NAVER Web Search to return no result for valid
            # official documents. Relation semantics remain locked by the
            # Typed QueryPlan and exact-span extractor, not by search keywords.
            path_scope = entry.path_prefixes[0] if len(entry.path_prefixes) == 1 else ""
            registry_terms = _bounded_registry_terms(entry, query)
            # Source metadata only ranks discovery candidates; it is never
            # admitted as answer evidence. Limit expansion to two bounded
            # registry terms so a long AND-like web query does not erase recall.
            search_query = " ".join(
                (
                    f"site:{entry.hostname}{path_scope}",
                    query.strip(),
                    *registry_terms[:2],
                )
            )
            try:
                page = self.transport.search(
                    query=search_query,
                    display=min(100, max(20, top_k * 4)),
                    start=1,
                )
            except OfficialDiscoveryError:
                failures.append(DiscoveryFailureCode.PROVIDER_ERROR)
                continue
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
                    (-_candidate_specificity(candidate.url), rank, admitted)
                )
                seen.add(candidate.url)
        ordered_buckets = []
        for entry in entries:
            bucket = candidate_buckets[entry.source_id]
            bucket.sort(
                key=lambda item: (
                    item[0],
                    -(
                        item[2].published_at.toordinal()
                        if item[2].published_at is not None
                        else 0
                    ),
                    item[1],
                    item[2].url,
                )
            )
            ordered_buckets.append([item[2] for item in bucket])
        candidates: list[OfficialDiscoveryCandidate] = []
        depth = 0
        while len(candidates) < 40 and any(depth < len(bucket) for bucket in ordered_buckets):
            for bucket in ordered_buckets:
                if depth < len(bucket):
                    candidates.append(bucket[depth])
                    if len(candidates) == 40:
                        break
            depth += 1
        if len(entries) > query_budget:
            failures.append(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE)
        return OfficialDiscoveryReport(
            candidates=tuple(candidates),
            queried_source_registry_ids=tuple(sorted(queried)),
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
