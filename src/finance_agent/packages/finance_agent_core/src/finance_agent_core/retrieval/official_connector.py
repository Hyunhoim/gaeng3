from __future__ import annotations

import hashlib
import html
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
    r"(?:as\s+of|holdings\s+(?:date|as\s+of))\s*[,;:：]?\s*[\"']?"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
    r"\s+(\d{1,2}),?\s+(20\d{2})",
    re.IGNORECASE,
)
_ENGLISH_NUMERIC_EVIDENCE_DATE_PATTERN = re.compile(
    r"(?:as\s+of|holdings\s+(?:date|as\s+of))\s*[,;:：]?\s*[\"']?"
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
_ENGLISH_MONTHS.update(
    {name[:3].casefold(): month for name, month in tuple(_ENGLISH_MONTHS.items())}
)


class OfficialConnectorError(RuntimeError):
    """Raised when discovery or fetching crosses the approved source boundary."""


class OfficialUrlExcludedError(OfficialConnectorError):
    """Raised before an already-collected official URL can be fetched or admitted."""


class OfficialConnectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialDiscoveryCandidate(OfficialConnectorModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    # Search metadata is useful only for candidate ordering.  It is never
    # copied into an evidence reference; the connector still has to fetch the
    # original official bytes and the exact-span extractor must approve them.
    summary: str | None = Field(default=None, max_length=2000)
    published_at: date | None = None
    as_of: date | None = None
    # A ticker may cross an official-document graph only after it was derived
    # from an approved issuer URL and matched exactly to the provided DB
    # universe.  It is provenance, not a value inferred from search snippets.
    db_ticker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9-]{0,9}$")

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("discovery title cannot be blank")
        return value

    @field_validator("summary")
    @classmethod
    def normalize_optional_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

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
    db_ticker: str | None = None
    # Present only when the bytes came from a verified approved corpus. Live
    # HTTP snapshots keep ``None`` and retain their existing derived IDs.
    approved_document_id: str | None = None


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
    db_ticker: str | None = None


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
            if error.code in {301, 302, 303, 307, 308}:
                location = error.headers.get("Location")
                if location:
                    return OfficialHttpResponse(
                        status_code=int(error.code),
                        final_url=str(location),
                        content_type=error.headers.get_content_type().casefold(),
                        body=b"",
                    )
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
        self.links: list[tuple[str, str]] = []
        self._active_link: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in {"a", "link"}:
            return
        values = dict(attrs)
        value = values.get("href")
        if value:
            label = " ".join(
                item
                for item in (
                    values.get("aria-label"),
                    values.get("title"),
                    values.get("rel"),
                )
                if item
            )
            self.links.append((value, label[:500]))
            self._active_link = len(self.links) - 1 if tag.casefold() == "a" else None

    def handle_data(self, data: str) -> None:
        if self._active_link is None or not data.strip():
            return
        href, label = self.links[self._active_link]
        combined = " ".join((label, data.strip())).strip()
        self.links[self._active_link] = (href, combined[:500])

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a":
            self._active_link = None


_EMBEDDED_OFFICIAL_URL = re.compile(
    r"https?://[^\s\"'<>]+(?:\.pdf|\.csv|\.xlsx|\.json)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_GLOBALX_PRODUCT_PATH = re.compile(
    r"^/funds/(?P<ticker>[a-z][a-z0-9-]{0,9})/?$",
    re.IGNORECASE,
)
_GLOBALX_HOLDINGS_PATH = re.compile(
    r"^/funds/holdings/(?P<ticker>[a-z][a-z0-9-]{0,9})_full-holdings_"
    r"(?P<as_of>20\d{6})\.csv$",
    re.IGNORECASE,
)


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
    elif media_type in {"text/plain", "text/csv", "application/csv"}:
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
        if snapshot.source.source_id in {
            "samsungfund-kodex-product",
            "samsungfund-kodex-product-mobile",
        }:
            product_ids = urllib.parse.parse_qs(parsed_source.query).get("id", [])
            if len(product_ids) == 1 and re.fullmatch(r"[0-9A-Za-z_-]{2,40}", product_ids[0]):
                product_id = product_ids[0]
                if snapshot.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    relative = f"/upload/invest/{product_id}-A.pdf"
                    risk_url = urllib.parse.urljoin("https://www.samsungfund.com", relative)
                    source = self.registry.authorize_url(risk_url, snapshot.capability)
                    if source.source_id != snapshot.source.source_id and (
                        source.publisher != snapshot.source.publisher
                        or source.authority != snapshot.source.authority
                    ):
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
                if source.source_id != snapshot.source.source_id and (
                    source.publisher != snapshot.source.publisher
                    or source.authority != snapshot.source.authority
                ):
                    raise OfficialConnectorError("linked KODEX API changed source scope")
                return (
                    OfficialDiscoveryCandidate(
                        url=api_url,
                        title=snapshot.title,
                    ),
                )
        parser = _LinkParser()
        decoded_content = snapshot.content.decode("utf-8", errors="replace")
        parser.feed(decoded_content)
        parser.close()
        discovered_links = list(parser.links[:500])
        # Modern official product pages frequently publish their canonical
        # DataDownload URL only in JSON-LD or an embedded application payload,
        # not in an anchor.  Admit only bounded document-like URLs and run the
        # same Source Registry authorization below; script text itself is never
        # evidence and cannot add a new trust domain.
        for match in _EMBEDDED_OFFICIAL_URL.finditer(decoded_content):
            raw_link = html.unescape(match.group(0)).replace(r"\/", "/")
            context = decoded_content[max(0, match.start() - 160) : match.end() + 80]
            discovered_links.append((raw_link, html.unescape(context)[:500]))
            if len(discovered_links) >= 700:
                break

        # Global X exposes the current dated holdings CSV from each official
        # product page.  Historical files use the same issuer-authored naming
        # convention.  Rewriting the date is allowed only when the product page
        # itself carries a DB-validated ticker provenance and actually exposes
        # the exact same-ticker CSV template. A search result or arbitrary
        # product URL cannot create this provenance.
        product_match = _GLOBALX_PRODUCT_PATH.fullmatch(parsed_source.path)
        if (
            snapshot.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and snapshot.source.source_id == "globalx-etf-official"
            and snapshot.db_ticker is not None
            and product_match is not None
            and not parsed_source.query
            and product_match.group("ticker").casefold() == snapshot.db_ticker.casefold()
        ):
            target_date = min(
                evidence_as_of or self.registry.evidence_cutoff,
                self.registry.evidence_cutoff,
            )
            historical_urls: set[str] = set()
            for raw_link, _link_label in discovered_links:
                exposed_url = urllib.parse.urljoin(snapshot.source_uri, raw_link)
                parsed_link = urllib.parse.urlsplit(exposed_url)
                holdings_match = _GLOBALX_HOLDINGS_PATH.fullmatch(parsed_link.path)
                if (
                    parsed_link.scheme != "https"
                    or parsed_link.hostname != "assets.globalxetfs.com"
                    or parsed_link.query
                    or parsed_link.fragment
                    or holdings_match is None
                    or holdings_match.group("ticker").casefold() != snapshot.db_ticker.casefold()
                ):
                    continue
                try:
                    exposed_source = self.registry.authorize_url(
                        exposed_url,
                        snapshot.capability,
                    )
                except OfficialSourceRegistryError:
                    continue
                if (
                    exposed_source.source_id != "globalx-etf-factsheets"
                    or exposed_source.publisher != snapshot.source.publisher
                    or exposed_source.authority != snapshot.source.authority
                ):
                    continue
                target_path = (
                    f"/funds/holdings/{snapshot.db_ticker.casefold()}_full-holdings_"
                    f"{target_date:%Y%m%d}.csv"
                )
                historical_url = urllib.parse.urlunsplit(
                    ("https", "assets.globalxetfs.com", target_path, "", "")
                )
                target_source = self.registry.authorize_url(
                    historical_url,
                    snapshot.capability,
                )
                if (
                    target_source.source_id != exposed_source.source_id
                    or target_source.publisher != snapshot.source.publisher
                    or target_source.authority != snapshot.source.authority
                ):
                    continue
                historical_urls.add(historical_url)
            if len(historical_urls) == 1:
                return (
                    OfficialDiscoveryCandidate(
                        url=next(iter(historical_urls)),
                        title=f"{snapshot.db_ticker} official full holdings",
                        as_of=target_date,
                        db_ticker=snapshot.db_ticker,
                    ),
                )

        ranked_links: list[tuple[tuple[int, int, str], OfficialDiscoveryCandidate]] = []
        seen: set[str] = set()
        capability_cues = {
            KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
                r"holdings?|portfolio|facts?heet|구성종목|보유종목",
                re.IGNORECASE,
            ),
            KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
                r"subsidiar|group|company|brochure|자회사|계열사|회사소개|브로슈어",
                re.IGNORECASE,
            ),
            KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
                r"theme|sector|insight|news|report|테마|섹터|뉴스|리포트",
                re.IGNORECASE,
            ),
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
                r"strategy|report|prospectus|fund|전략|보고서|투자설명서|펀드",
                re.IGNORECASE,
            ),
            KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
                r"risk|prospectus|invest|투자위험|투자설명서|간이투자",
                re.IGNORECASE,
            ),
        }[snapshot.capability]
        for link_ordinal, (raw_link, link_label) in enumerate(discovered_links):
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
            parsed_link = urllib.parse.urlsplit(url)
            search_surface = urllib.parse.unquote(
                " ".join((parsed_link.path, parsed_link.query, link_label))
            )
            link_score = (
                10 * int(is_pdf)
                + 7 * int(capability_cues.search(search_surface) is not None)
                + 4 * int(_PUBLICATION_DATE_PATTERN.search(search_surface) is not None)
                + 3
                * int(
                    snapshot.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                    and re.search(r"api|json|excel|download", search_surface, re.I) is not None
                )
            )
            ranked_links.append(
                (
                    (-link_score, link_ordinal, url),
                    OfficialDiscoveryCandidate(
                        url=url,
                        title=link_label or snapshot.title,
                        published_at=snapshot.published_at,
                        as_of=snapshot.as_of,
                    ),
                )
            )
            seen.add(url)
        ranked_links.sort(key=lambda item: item[0])
        return tuple(candidate for _, candidate in ranked_links[:max_links])


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
        *,
        url_is_excluded: Callable[[str], bool] | None = None,
    ) -> None:
        self.transport = transport
        self.registry = registry or load_official_source_registry()
        self._url_is_excluded = url_is_excluded
        self._celero_materializer = CeleroShortLinkDocumentMaterializer(transport)

    def _require_url_not_excluded(self, url: str) -> None:
        if self._url_is_excluded is not None and self._url_is_excluded(url):
            raise OfficialUrlExcludedError("official source URL was already collected")

    def _authorize_url(
        self,
        url: str,
        capability: KnowledgeCapability,
    ) -> OfficialSourceEntry:
        try:
            return self.registry.authorize_url(url, capability)
        except OfficialSourceRegistryError as error:
            raise OfficialConnectorError(
                "official source URL is outside the approved Registry scope"
            ) from error

    @staticmethod
    def _content_type_allowed(
        entry: OfficialSourceEntry,
        content_type: str,
        *,
        url: str,
        capability: KnowledgeCapability,
    ) -> bool:
        normalized = content_type.casefold().split(";", maxsplit=1)[0].strip()
        is_html = normalized in {"text/html", "application/xhtml+xml"}
        is_pdf = normalized == "application/pdf"
        is_json = normalized == "application/json"
        path = urllib.parse.urlsplit(url).path.casefold()
        # Holdings downloads linked from an already-authorized official product
        # page are often served as text/plain or text/csv.  Keep this exception
        # capability- and extension-scoped so an arbitrary text endpoint cannot
        # become evidence.
        is_holdings_text = (
            capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and path.endswith(".csv")
            and normalized in {"text/plain", "text/csv", "application/csv"}
        )
        if is_holdings_text:
            return True
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
        self._require_url_not_excluded(request.url)
        source = self._authorize_url(request.url, request.capability)
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
        if response.status_code in {301, 302, 303, 307, 308}:
            redirected_url = urllib.parse.urljoin(request.url, response.final_url)
            parsed_redirect = urllib.parse.urlsplit(redirected_url)
            # Some legacy official endpoints emit an HTTP canonical URL even
            # though the same target is available over HTTPS.  Never contact
            # the downgraded URL: upgrade it locally, then require its exact
            # hostname/path to belong to an explicitly trusted source ID.
            if parsed_redirect.scheme == "http":
                redirected_url = urllib.parse.urlunsplit(parsed_redirect._replace(scheme="https"))
            redirected_source = self._authorize_url(
                redirected_url,
                request.capability,
            )
            if redirected_source.source_id not in source.trusted_redirect_source_ids:
                raise OfficialConnectorError("official redirect target is not explicitly trusted")
            self._require_url_not_excluded(redirected_url)
            response = self.transport.fetch(
                redirected_url,
                timeout_seconds=request.timeout_seconds,
                max_bytes=request.max_bytes,
            )
            source = redirected_source
        if (
            source.content_platform is SourceContentPlatform.CELERO_SHORT_LINK
            and self._celero_materializer.is_thin_shell(response)
        ):
            response = self._celero_materializer.materialize(
                response.final_url,
                timeout_seconds=request.timeout_seconds,
                max_bytes=request.max_bytes,
            )
        self._require_url_not_excluded(response.final_url)
        if response.status_code != 200:
            raise OfficialConnectorError("official source did not return HTTP 200")
        # Redirects are disabled in the production transport.  Rechecking the final
        # URL also protects injected test/alternate transports from changing hosts.
        final_source = self._authorize_url(response.final_url, request.capability)
        if final_source.source_id != source.source_id:
            raise OfficialConnectorError("official source changed trust domain while fetching")
        if not self._content_type_allowed(
            source,
            response.content_type,
            url=response.final_url,
            capability=request.capability,
        ):
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
                    db_ticker=candidate.db_ticker,
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
            db_ticker=candidate.db_ticker,
        )


__all__ = [
    "BoundedOfficialDiscovery",
    "CeleroShortLinkDocumentMaterializer",
    "OfficialConnectorError",
    "OfficialUrlExcludedError",
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
