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
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
    load_official_source_registry,
)
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate

_MAX_SECRET_BYTES = 4096
_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
_URL_DATE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)")


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
        if self.queried_source_registry_ids != tuple(
            sorted(set(self.queried_source_registry_ids))
        ):
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
        api_key_id_file: str | Path,
        api_key_file: str | Path,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("search timeout must be between 0.1 and 30 seconds")
        self.api_key_id = _read_secret_file(api_key_id_file)
        self.api_key = _read_secret_file(api_key_file)
        self.timeout_seconds = timeout_seconds

    def search(self, *, query: str, display: int, start: int) -> OfficialWebSearchPage:
        if not query.strip() or not 1 <= display <= 100 or not 1 <= start <= 1000:
            raise ValueError("invalid official web search request")
        url = _API_URL + "?" + urllib.parse.urlencode(
            {"query": query, "display": display, "start": start, "sort": "sim"}
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-NCP-APIGW-API-KEY-ID": self.api_key_id,
                "X-NCP-APIGW-API-KEY": self.api_key,
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


_RELATION_QUERY_TERMS: dict[KnowledgeCapability, str] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: "편입 보유 구성종목 holdings",
    KnowledgeCapability.CORPORATE_SUBSIDIARY: "자회사 계열사 가족사 subsidiary",
    KnowledgeCapability.SECTOR_THEME_HISTORY: "테마 섹터 산업 동향",
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: "펀드 구조 투자전략 운용 동향",
    KnowledgeCapability.PRODUCT_RISK_FACTORS: "상품 위험요인 투자위험 risk",
}


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
        candidates: list[OfficialDiscoveryCandidate] = []
        queried: list[str] = []
        failures: list[DiscoveryFailureCode] = []
        seen: set[str] = set()
        for entry in entries[:query_budget]:
            queried.append(entry.source_id)
            search_query = (
                f"site:{entry.hostname} {query.strip()} "
                f"{_RELATION_QUERY_TERMS[capability]}"
            )
            try:
                page = self.transport.search(query=search_query, display=top_k, start=1)
            except OfficialDiscoveryError:
                failures.append(DiscoveryFailureCode.PROVIDER_ERROR)
                continue
            for candidate in page.items:
                try:
                    authorized = self.registry.authorize_url(candidate.url, capability)
                except OfficialSourceRegistryError:
                    continue
                if authorized.source_id != entry.source_id or candidate.url in seen:
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
                candidates.append(
                    candidate.model_copy(
                        update={
                            "published_at": published_at,
                            "as_of": published_at,
                        }
                    )
                )
                seen.add(candidate.url)
                if len(candidates) >= 40:
                    break
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
