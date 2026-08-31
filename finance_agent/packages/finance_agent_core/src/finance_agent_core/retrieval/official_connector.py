from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pypdf import PdfReader

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
    SourceConnectorKind,
    SourceContentPlatform,
    load_official_source_registry,
)

_MAX_FETCH_BYTES = 16 * 1024 * 1024
_CELERO_API_ORIGIN = "https://p2api.celero.io"
_CELERO_SHORT_PATH = re.compile(r"^/s/(?P<short_hash>[0-9a-f]{8})(?:/.*)?$")
_CELERO_CONTENT_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PUBLICATION_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[-._/]?(0[1-9]|1[0-2])[-._/]?([0-2]\d|3[01])(?!\d)"
)
_PUBLICATION_META_KEYS = frozenset(
    {
        "article:published_time",
        "date",
        "datepublished",
        "dc.date",
        "dcterms.date",
        "publishdate",
        "published_time",
    }
)
_KOREAN_EVIDENCE_DATE_PATTERN = re.compile(
    r"(?:기준일|자료\s*기준|평가\s*기준|구성\s*종목\s*기준)\s*[:：]?\s*"
    r"(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?",
    re.IGNORECASE,
)
_ENGLISH_EVIDENCE_DATE_PATTERN = re.compile(
    r"(?:as\s+of|holdings\s+(?:date|as\s+of))\s*[:：]?\s*"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(20\d{2})",
    re.IGNORECASE,
)
_ENGLISH_NUMERIC_EVIDENCE_DATE_PATTERN = re.compile(
    r"(?:as\s+of|holdings\s+(?:date|as\s+of))\s*[:：]?\s*"
    r"(\d{1,2})[./-](\d{1,2})[./-](20\d{2})",
    re.IGNORECASE,
)
_ENGLISH_MONTHS = {
    value.casefold(): index
    for index, value in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}


class OfficialConnectorError(RuntimeError):
    """Raised when discovery or fetching crosses the approved source boundary."""


class OfficialConnectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialDiscoveryCandidate(OfficialConnectorModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    published_at: date | None = None
    as_of: date | None = None

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("discovery title cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> OfficialDiscoveryCandidate:
        if self.as_of is not None and self.published_at is not None:
            if self.as_of > self.published_at:
                raise ValueError("candidate as_of cannot be later than publication date")
        return self


class OfficialFetchRequest(OfficialConnectorModel):
    url: str = Field(min_length=8, max_length=2000)
    capability: KnowledgeCapability
    published_at: date | None = None
    as_of: date | None = None
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=30.0)
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=_MAX_FETCH_BYTES)
    allow_navigation_only: bool = False

    @model_validator(mode="after")
    def validate_dates(self) -> OfficialFetchRequest:
        if self.as_of is not None and self.published_at is not None:
            if self.as_of > self.published_at:
                raise ValueError("source as_of cannot be later than its publication date")
        return self


@dataclass(frozen=True, slots=True)
class OfficialHttpResponse:
    status_code: int
    final_url: str
    content_type: str
    body: bytes
    metadata_as_of: date | None = None


@dataclass(frozen=True, slots=True)
class OfficialSourceSnapshot:
    source: OfficialSourceEntry
    capability: KnowledgeCapability
    source_uri: str
    title: str
    content_type: str
    content: bytes
    content_sha256: str
    published_at: date | None
    as_of: date


@dataclass(frozen=True, slots=True)
class OfficialNavigationSnapshot:
    """Approved official HTML usable only to discover linked evidence.

    It deliberately has no evidence date and therefore can never enter the
    external evidence/result contract. Only the link resolver may consume it.
    """

    source: OfficialSourceEntry
    capability: KnowledgeCapability
    source_uri: str
    title: str
    content_type: str
    content: bytes
    content_sha256: str
    published_at: None = None
    as_of: None = None


class OfficialHttpTransport(Protocol):
    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse: ...


class OfficialDynamicPageRenderer(Protocol):
    """Injected renderer; production may use an isolated browser service."""

    def render(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class UrllibOfficialHttpTransport:
    """Small production transport with redirects disabled and bounded reads."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/pdf,application/json,text/plain;q=0.8",
                "User-Agent": "MiraeAssetFinanceAgentEvidence/1.0",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(response.status)
                final_url = str(response.geturl())
                content_type = response.headers.get_content_type().casefold()
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            raise OfficialConnectorError(f"official source returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OfficialConnectorError("official source fetch failed") from error
        if len(body) > max_bytes:
            raise OfficialConnectorError("official source exceeded the response size limit")
        return OfficialHttpResponse(
            status_code=status,
            final_url=final_url,
            content_type=content_type,
            body=body,
        )


class CeleroShortLinkDocumentMaterializer:
    """Resolve an approved Celero short link to its immutable original PDF.

    Celero is a document-hosting platform rather than an evidence authority.
    The caller must first authorize the public short URL through Source Registry.
    This adapter accepts no arbitrary vendor URL: both API paths are constructed
    locally from a bounded short hash and a server-returned canonical UUID.
    """

    def __init__(self, transport: OfficialHttpTransport) -> None:
        self.transport = transport

    @staticmethod
    def is_thin_shell(response: OfficialHttpResponse) -> bool:
        media_type = response.content_type.casefold().split(";", maxsplit=1)[0].strip()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            return False
        lowered = response.body.lower()
        return b"/static/js/main-" in lowered and b'id="root"' in lowered

    @staticmethod
    def _require_response(
        response: OfficialHttpResponse,
        *,
        expected_url: str,
        expected_content_type: str,
    ) -> bytes:
        media_type = response.content_type.casefold().split(";", maxsplit=1)[0].strip()
        if response.status_code != 200:
            raise OfficialConnectorError("Celero document API did not return HTTP 200")
        if response.final_url != expected_url:
            raise OfficialConnectorError("Celero document API redirected unexpectedly")
        if media_type != expected_content_type:
            raise OfficialConnectorError("Celero document API returned an invalid content type")
        if not response.body:
            raise OfficialConnectorError("Celero document API returned an empty body")
        return response.body

    def materialize(
        self,
        official_url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        parsed = urllib.parse.urlsplit(official_url)
        matched = _CELERO_SHORT_PATH.fullmatch(parsed.path)
        if matched is None or parsed.query or parsed.fragment:
            raise OfficialConnectorError("Celero official URL is not a canonical short link")
        short_hash = matched.group("short_hash")
        metadata_url = f"{_CELERO_API_ORIGIN}/api/shorten/{short_hash}/?viewer=true"
        metadata_response = self.transport.fetch(
            metadata_url,
            timeout_seconds=min(10.0, max(0.1, timeout_seconds / 3)),
            max_bytes=min(512 * 1024, max_bytes),
        )
        metadata_bytes = self._require_response(
            metadata_response,
            expected_url=metadata_url,
            expected_content_type="application/json",
        )
        try:
            metadata = json.loads(metadata_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OfficialConnectorError("Celero document metadata is invalid JSON") from error
        if not isinstance(metadata, dict) or metadata.get("hash") != short_hash:
            raise OfficialConnectorError("Celero document metadata changed the short hash")
        content_uuid = metadata.get("content_uuid")
        if (
            not isinstance(content_uuid, str)
            or _CELERO_CONTENT_UUID.fullmatch(content_uuid) is None
        ):
            raise OfficialConnectorError("Celero document metadata has no canonical content UUID")
        content_url = metadata.get("content_url")
        if not isinstance(content_url, str):
            raise OfficialConnectorError("Celero document metadata has no official content URL")
        linked = urllib.parse.urlsplit(content_url)
        linked_match = _CELERO_SHORT_PATH.fullmatch(linked.path)
        if (
            linked.scheme != parsed.scheme
            or linked.hostname != parsed.hostname
            or linked_match is None
            or linked_match.group("short_hash") != short_hash
        ):
            raise OfficialConnectorError("Celero metadata left the approved official short link")
        content_metadata_url = f"{_CELERO_API_ORIGIN}/api/content/{content_uuid}/?viewer=true"
        content_metadata_response = self.transport.fetch(
            content_metadata_url,
            timeout_seconds=min(10.0, max(0.1, timeout_seconds / 3)),
            max_bytes=min(1024 * 1024, max_bytes),
        )
        content_metadata_bytes = self._require_response(
            content_metadata_response,
            expected_url=content_metadata_url,
            expected_content_type="application/json",
        )
        try:
            content_metadata = json.loads(content_metadata_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OfficialConnectorError("Celero content metadata is invalid JSON") from error
        if not isinstance(content_metadata, dict) or content_metadata.get("uuid") != content_uuid:
            raise OfficialConnectorError("Celero content metadata changed the content UUID")
        original_name = content_metadata.get("original_name")
        if not isinstance(original_name, str) or not original_name.strip():
            raise OfficialConnectorError("Celero content metadata has no original filename")
        metadata_as_of = _parse_publication_date(original_name)
        download_url = f"{_CELERO_API_ORIGIN}/api/content/{content_uuid}/download/"
        pdf_response = self.transport.fetch(
            download_url,
            timeout_seconds=min(20.0, max(0.1, timeout_seconds * 2 / 3)),
            max_bytes=max_bytes,
        )
        pdf_bytes = self._require_response(
            pdf_response,
            expected_url=download_url,
            expected_content_type="application/pdf",
        )
        if len(pdf_bytes) > max_bytes or not pdf_bytes.startswith(b"%PDF-"):
            raise OfficialConnectorError("Celero original document is oversized or not a PDF")
        return OfficialHttpResponse(
            status_code=200,
            final_url=official_url,
            content_type="application/pdf",
            body=pdf_bytes,
            metadata_as_of=metadata_as_of,
        )


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in {"a", "link"}:
            return
        values = dict(attrs)
        value = values.get("href")
        if value:
            self.links.append(value)


def _parse_publication_date(value: str) -> date | None:
    match = _PUBLICATION_DATE_PATTERN.search(value)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


class _PublicationDateParser(HTMLParser):
    """Collect only explicit machine-readable publication dates."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() == "meta":
            key = (
                values.get("property") or values.get("name") or values.get("itemprop") or ""
            ).casefold()
            content = values.get("content")
            if key in _PUBLICATION_META_KEYS and content:
                self.values.append(content)
        elif tag.casefold() == "time" and values.get("datetime"):
            self.values.append(values["datetime"])


class _EvidenceTextParser(HTMLParser):
    ignored = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self.ignored:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.ignored and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def infer_official_publication_date(
    url: str,
    content_type: str,
    body: bytes,
) -> date | None:
    """Resolve one unambiguous date from the official URL or HTML metadata."""

    candidates: set[date] = set()
    url_date = _parse_publication_date(urllib.parse.unquote(url))
    if url_date is not None:
        candidates.add(url_date)
    media_type = content_type.casefold().split(";", maxsplit=1)[0].strip()
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _PublicationDateParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        candidates.update(
            parsed
            for value in parser.values
            if (parsed := _parse_publication_date(value)) is not None
        )
        # Some regulator pages expose the publication date only as visible
        # text (for example a single ``<div class="day">2026-05-21``) and do
        # not provide machine-readable metadata.  Accept that date only when
        # the entire official HTML contains exactly one distinct full calendar
        # date. Multiple visible dates remain ambiguous and fail closed.
        if not candidates:
            visible = _EvidenceTextParser()
            visible.feed(body.decode("utf-8", errors="replace"))
            visible.close()
            visible_dates = {
                parsed
                for match in _PUBLICATION_DATE_PATTERN.finditer(" ".join(visible.parts))
                if (parsed := _parse_publication_date(match.group(0))) is not None
            }
            if len(visible_dates) == 1:
                candidates.update(visible_dates)
    return next(iter(candidates)) if len(candidates) == 1 else None


def infer_official_evidence_date(content_type: str, body: bytes) -> date | None:
    """Resolve one explicit as-of date from bounded official document text."""

    media_type = content_type.casefold().split(";", maxsplit=1)[0].strip()
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _EvidenceTextParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        text = " ".join(parser.parts)
    elif media_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(body), strict=False)
            if len(reader.pages) > 250:
                return None
            parts: list[str] = []
            size = 0
            for page in reader.pages:
                value = page.extract_text() or ""
                size += len(value)
                if size > 2_000_000:
                    return None
                parts.append(value)
            text = "\n".join(parts)
        except Exception:  # noqa: BLE001 - a malformed date source is rejected
            return None
    elif media_type == "text/plain":
        text = body.decode("utf-8", errors="replace")
    elif media_type == "application/json":
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        values: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.casefold() in {
                        "asof",
                        "as_of",
                        "date",
                        "gijunymd",
                        "holdingsdate",
                    } and isinstance(item, (str, int)):
                        values.append(str(item))
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(payload)
        candidates = {
            parsed for value in values if (parsed := _parse_publication_date(value)) is not None
        }
        return next(iter(candidates)) if len(candidates) == 1 else None
    else:
        return None
    candidates: set[date] = set()
    for year, month, day in _KOREAN_EVIDENCE_DATE_PATTERN.findall(text):
        try:
            candidates.add(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    for month, day, year in _ENGLISH_EVIDENCE_DATE_PATTERN.findall(text):
        try:
            candidates.add(date(int(year), _ENGLISH_MONTHS[month.casefold()], int(day)))
        except ValueError:
            continue
    for month, day, year in _ENGLISH_NUMERIC_EVIDENCE_DATE_PATTERN.findall(text):
        try:
            candidates.add(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return next(iter(candidates)) if len(candidates) == 1 else None


class OfficialDocumentLinkResolver:
    """Resolve static HTML links to approved HTML/PDF documents only."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()

    def resolve(
        self,
        snapshot: OfficialSourceSnapshot | OfficialNavigationSnapshot,
        *,
        max_links: int = 8,
        evidence_as_of: date | None = None,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        if not 1 <= max_links <= 20:
            raise ValueError("max_links must be between 1 and 20")
        media_type = snapshot.content_type.casefold().split(";", maxsplit=1)[0]
        if media_type not in {"text/html", "application/xhtml+xml"}:
            return ()
        # KODEX product pages are server-rendered for identity but load their
        # holdings from a same-origin official JSON endpoint.  Resolve that
        # endpoint generically from the page's opaque product ID; company names
        # and ETF names are never embedded in this connector.
        parsed_source = urllib.parse.urlsplit(snapshot.source_uri)
        if snapshot.source.source_id == "samsungfund-kodex-product":
            product_ids = urllib.parse.parse_qs(parsed_source.query).get("id", [])
            if len(product_ids) == 1 and re.fullmatch(r"[0-9A-Za-z_-]{2,40}", product_ids[0]):
                product_id = product_ids[0]
                if snapshot.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    relative = f"/upload/invest/{product_id}-A.pdf"
                    risk_url = urllib.parse.urljoin(snapshot.source_uri, relative)
                    source = self.registry.authorize_url(risk_url, snapshot.capability)
                    if source.source_id != snapshot.source.source_id:
                        raise OfficialConnectorError("linked KODEX PDF changed source scope")
                    return (
                        OfficialDiscoveryCandidate(
                            url=risk_url,
                            title=snapshot.title,
                        ),
                    )
                target_date = min(
                    evidence_as_of or self.registry.evidence_cutoff,
                    self.registry.evidence_cutoff,
                )
                api_url = (
                    "https://www.samsungfund.com/api/v1/kodex/product-pdf/"
                    f"{urllib.parse.quote(product_id, safe='')}.do?"
                    f"gijunYMD={target_date:%Y.%m.%d}"
                )
                source = self.registry.authorize_url(api_url, snapshot.capability)
                if source.source_id != snapshot.source.source_id:
                    raise OfficialConnectorError("linked KODEX API changed source scope")
                return (
                    OfficialDiscoveryCandidate(
                        url=api_url,
                        title=snapshot.title,
                    ),
                )
        parser = _LinkParser()
        parser.feed(snapshot.content.decode("utf-8", errors="replace"))
        parser.close()
        resolved: list[OfficialDiscoveryCandidate] = []
        seen: set[str] = set()
        for raw_link in parser.links:
            url = urllib.parse.urljoin(snapshot.source_uri, raw_link)
            if url in seen:
                continue
            try:
                source = self.registry.authorize_url(url, snapshot.capability)
            except OfficialSourceRegistryError:
                continue
            if source.source_id != snapshot.source.source_id and (
                source.publisher != snapshot.source.publisher
                or source.authority != snapshot.source.authority
            ):
                # One approved publisher may split product pages and documents
                # across registered official hosts. Crossing to another
                # publisher or authority remains forbidden.
                continue
            is_pdf = urllib.parse.urlsplit(url).path.casefold().endswith(".pdf")
            if (is_pdf and source.connector_kind is SourceConnectorKind.HTML) or (
                not is_pdf and source.connector_kind is SourceConnectorKind.PDF
            ):
                continue
            resolved.append(
                OfficialDiscoveryCandidate(
                    url=url,
                    title=snapshot.title,
                    published_at=snapshot.published_at,
                    as_of=snapshot.as_of,
                )
            )
            seen.add(url)
            if len(resolved) == max_links:
                break
        return tuple(resolved)


class StaticThenDynamicOfficialHttpTransport:
    """Use bounded HTTP first and an injected isolated renderer only on thin shells."""

    def __init__(
        self,
        renderer: OfficialDynamicPageRenderer,
        *,
        static_transport: OfficialHttpTransport | None = None,
        minimum_static_bytes: int = 256,
    ) -> None:
        if minimum_static_bytes < 1:
            raise ValueError("minimum_static_bytes must be positive")
        self.static_transport = static_transport or UrllibOfficialHttpTransport()
        self.renderer = renderer
        self.minimum_static_bytes = minimum_static_bytes

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        static = self.static_transport.fetch(
            url,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
        media_type = static.content_type.casefold().split(";", maxsplit=1)[0]
        if media_type not in {"text/html", "application/xhtml+xml"}:
            return static
        lowered = static.body.lower()
        dynamic_markers = (b"__next_data__", b'id="__next"', b'id="root"', b'id="app"')
        if len(static.body) >= self.minimum_static_bytes and not any(
            marker in lowered for marker in dynamic_markers
        ):
            return static
        rendered = self.renderer.render(
            url,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
        if rendered.final_url != url:
            raise OfficialConnectorError("dynamic renderer changed the requested URL")
        if not rendered.body or len(rendered.body) > max_bytes:
            raise OfficialConnectorError("dynamic renderer returned invalid content")
        return rendered


class BoundedOfficialDiscovery:
    """Validate search-engine candidates; search snippets never become evidence."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()

    def authorize_candidates(
        self,
        candidates: tuple[OfficialDiscoveryCandidate, ...],
        capability: KnowledgeCapability,
        *,
        max_candidates: int = 8,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        if not 1 <= max_candidates <= 20:
            raise ValueError("max_candidates must be between 1 and 20")
        authorized: list[OfficialDiscoveryCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.url in seen:
                continue
            try:
                self.registry.authorize_url(candidate.url, capability)
            except OfficialSourceRegistryError:
                continue
            authorized.append(candidate)
            seen.add(candidate.url)
            if len(authorized) == max_candidates:
                break
        return tuple(authorized)


class OfficialSourceConnector:
    """Fetch one already discovered official document into snapshot intake."""

    def __init__(
        self,
        transport: OfficialHttpTransport,
        registry: OfficialSourceRegistry | None = None,
    ) -> None:
        self.transport = transport
        self.registry = registry or load_official_source_registry()
        self._celero_materializer = CeleroShortLinkDocumentMaterializer(transport)

    @staticmethod
    def _content_type_allowed(entry: OfficialSourceEntry, content_type: str) -> bool:
        normalized = content_type.casefold().split(";", maxsplit=1)[0].strip()
        is_html = normalized in {"text/html", "application/xhtml+xml"}
        is_pdf = normalized == "application/pdf"
        is_json = normalized == "application/json"
        if entry.connector_kind is SourceConnectorKind.HTML:
            return is_html
        if entry.connector_kind is SourceConnectorKind.PDF:
            return is_pdf
        if entry.connector_kind is SourceConnectorKind.HTML_OR_JSON:
            return is_html or is_json
        if entry.connector_kind is SourceConnectorKind.HTML_PDF_OR_JSON:
            return is_html or is_pdf or is_json
        return is_html or is_pdf

    def fetch(
        self,
        candidate: OfficialDiscoveryCandidate,
        request: OfficialFetchRequest,
    ) -> OfficialSourceSnapshot | OfficialNavigationSnapshot:
        if candidate.url != request.url:
            raise OfficialConnectorError("fetch request differs from its discovered candidate")
        source = self.registry.authorize_url(request.url, request.capability)
        if (
            request.published_at is not None
            and request.published_at > self.registry.evidence_cutoff
        ):
            raise OfficialConnectorError("source was published after the evaluation cutoff")
        if request.as_of is not None and request.as_of > self.registry.evidence_cutoff:
            raise OfficialConnectorError("source evidence date exceeds the evaluation cutoff")

        response = self.transport.fetch(
            request.url,
            timeout_seconds=request.timeout_seconds,
            max_bytes=request.max_bytes,
        )
        if (
            source.content_platform is SourceContentPlatform.CELERO_SHORT_LINK
            and self._celero_materializer.is_thin_shell(response)
        ):
            response = self._celero_materializer.materialize(
                request.url,
                timeout_seconds=request.timeout_seconds,
                max_bytes=request.max_bytes,
            )
        if response.status_code != 200:
            raise OfficialConnectorError("official source did not return HTTP 200")
        # Redirects are disabled in the production transport.  Rechecking the final
        # URL also protects injected test/alternate transports from changing hosts.
        final_source = self.registry.authorize_url(response.final_url, request.capability)
        if final_source.source_id != source.source_id:
            raise OfficialConnectorError("official source changed trust domain while fetching")
        if not self._content_type_allowed(source, response.content_type):
            raise OfficialConnectorError("official source content type is not approved")
        if not response.body or len(response.body) > request.max_bytes:
            raise OfficialConnectorError("official source body is empty or oversized")
        published_at = request.published_at or infer_official_publication_date(
            response.final_url,
            response.content_type,
            response.body,
        )
        if source.publication_date_required and published_at is None:
            raise OfficialConnectorError("official source requires an explicit publication date")
        evidence_at = infer_official_evidence_date(response.content_type, response.body)
        if (
            evidence_at is not None
            and response.metadata_as_of is not None
            and evidence_at != response.metadata_as_of
        ):
            raise OfficialConnectorError(
                "official source body conflicts with platform metadata date"
            )
        evidence_at = evidence_at or response.metadata_as_of
        if request.as_of is not None:
            if evidence_at is not None and request.as_of != evidence_at:
                raise OfficialConnectorError(
                    "official source evidence date conflicts with the request"
                )
        effective_as_of = request.as_of or evidence_at or published_at
        if effective_as_of is None:
            if request.allow_navigation_only and not source.publication_date_required:
                return OfficialNavigationSnapshot(
                    source=source,
                    capability=request.capability,
                    source_uri=response.final_url,
                    title=candidate.title,
                    content_type=response.content_type,
                    content=response.body,
                    content_sha256=hashlib.sha256(response.body).hexdigest(),
                )
            raise OfficialConnectorError("official source requires an explicit evidence date")
        if published_at is not None and published_at > self.registry.evidence_cutoff:
            raise OfficialConnectorError("source was published after the evaluation cutoff")
        if effective_as_of > self.registry.evidence_cutoff:
            raise OfficialConnectorError("source evidence date exceeds the evaluation cutoff")
        return OfficialSourceSnapshot(
            source=source,
            capability=request.capability,
            source_uri=response.final_url,
            title=candidate.title,
            content_type=response.content_type,
            content=response.body,
            content_sha256=hashlib.sha256(response.body).hexdigest(),
            published_at=published_at,
            as_of=effective_as_of,
        )


__all__ = [
    "BoundedOfficialDiscovery",
    "CeleroShortLinkDocumentMaterializer",
    "OfficialConnectorError",
    "OfficialDiscoveryCandidate",
    "OfficialFetchRequest",
    "OfficialNavigationSnapshot",
    "OfficialHttpResponse",
    "OfficialHttpTransport",
    "infer_official_publication_date",
    "OfficialDocumentLinkResolver",
    "OfficialDynamicPageRenderer",
    "OfficialSourceConnector",
    "OfficialSourceSnapshot",
    "StaticThenDynamicOfficialHttpTransport",
    "UrllibOfficialHttpTransport",
    "infer_official_evidence_date",
]
