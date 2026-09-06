from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.retrieval.sqlite_fts import chunk_document, normalize_document_text

OPEN_DART_EVIDENCE_CUTOFF = date(2026, 8, 24)
_CORP_CODE_ENDPOINT = "/api/corpCode.xml"
_DISCLOSURE_LIST_ENDPOINT = "/api/list.json"
_ORIGINAL_DOCUMENT_ENDPOINT = "/api/document.xml"
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_INNER_FILE_BYTES = 32 * 1024 * 1024
_MAX_CORP_CODE_INNER_FILE_BYTES = 48 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_PENDING_TEXT_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100
_MAX_DISCLOSURE_PAGES = 1000
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_ID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{2,159}$"
_RECEIPT_NUMBER_PATTERN = r"^\d{14}$"
_CORPORATION_CODE_PATTERN = r"^\d{8}$"
# OpenDART currently returns both numeric KRX codes (for example ``005930``)
# and official six-character alphanumeric codes (for example ``0001A0``).
# Keep the surface exact and uppercase instead of discarding those issuers.
_STOCK_CODE_PATTERN = r"^[0-9A-Z]{6}$"
_XML_DECLARATION_ENCODING = re.compile(
    rb"<\?xml[^>]{0,200}\bencoding=[\"'](?P<encoding>[A-Za-z0-9._-]+)[\"']",
    re.IGNORECASE,
)
_CORRECTION_REPORT_PREFIXES = frozenset({"기재정정", "발행조건확정", "첨부정정"})
_WITHDRAWAL_REPORT_TERMS = frozenset({"철회간주안내", "철회신고서"})
_LEADING_LEGAL_DESIGNATOR = re.compile(r"^(?:\(주\)|\(유\)|주식회사|유한회사)\s*")
_TRAILING_LEGAL_DESIGNATOR = re.compile(r"\s*(?:\(주\)|\(유\)|주식회사|유한회사)$")


class OpenDartCollectionError(RuntimeError):
    """Raised when an Open DART response cannot enter the pending corpus."""


class OpenDartOriginalDocumentUnavailable(OpenDartCollectionError):
    """Raised for OpenDART status 014 when a listed filing has no source ZIP."""


class OpenDartModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class OpenDartHttpResponse:
    """Key-free transport response.

    The injected transport owns authentication. API credentials are deliberately
    absent from this DTO and therefore cannot enter a collection manifest.
    """

    status_code: int
    content_type: str
    body: bytes


class OpenDartTransport(Protocol):
    def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> OpenDartHttpResponse: ...


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("collection timestamp must use UTC")
    return value


def _identity_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _legal_name_identity_key(value: str) -> str:
    """Normalize only explicit Korean legal-form markers for exact matching.

    OpenDART and the provided product data place ``(주)``/``주식회사`` on
    different sides of the same legal name.  No brand, abbreviation or fuzzy
    alias expansion is performed here; the resulting key must still identify
    exactly one OpenDART corporation.
    """

    surface = " ".join(unicodedata.normalize("NFKC", value).split())
    previous = None
    while surface != previous:
        previous = surface
        surface = _LEADING_LEGAL_DESIGNATOR.sub("", surface)
        surface = _TRAILING_LEGAL_DESIGNATOR.sub("", surface)
    return _identity_key(surface)


def _normalized_report_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _report_lineage_surface(value: str) -> tuple[str, bool, bool]:
    """Return a conservative base title plus correction/withdrawal markers.

    Open DART prepends bracketed filing-state markers to ``report_nm``.  Only
    the documented correction markers and an explicit withdrawal marker are
    removed here; an unknown bracketed prefix remains part of the identity so
    unrelated reports cannot be merged accidentally.
    """

    normalized = _normalized_report_name(value)
    is_correction = False
    is_withdrawal = any(term in normalized for term in _WITHDRAWAL_REPORT_TERMS)
    while normalized.startswith("["):
        closing = normalized.find("]")
        if closing <= 1:
            break
        marker = normalized[1:closing].strip()
        if marker in _CORRECTION_REPORT_PREFIXES:
            is_correction = True
        elif "철회" in marker:
            is_withdrawal = True
        else:
            break
        normalized = normalized[closing + 1 :].strip()
    return normalized, is_correction, is_withdrawal


def _filing_lineage_key(disclosure: OpenDartDisclosure) -> tuple[str, str, str, str]:
    report_name, _, _ = _report_lineage_surface(disclosure.report_nm)
    return (
        disclosure.corp_code,
        disclosure.pblntf_ty,
        disclosure.pblntf_detail_ty,
        report_name,
    )


def _rm_contains(disclosure: OpenDartDisclosure, marker: str) -> bool:
    return marker in unicodedata.normalize("NFKC", disclosure.rm)


def _is_withdrawn_disclosure(disclosure: OpenDartDisclosure) -> bool:
    _, _, report_name_marks_withdrawal = _report_lineage_surface(disclosure.report_nm)
    return _rm_contains(disclosure, "철") or report_name_marks_withdrawal


class OpenDartFileReference(OpenDartModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(gt=0, le=_MAX_UNCOMPRESSED_BYTES)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("Open DART paths must use safe POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Open DART paths must be canonical and relative")
        return value


class OpenDartCorporationCode(OpenDartModel):
    corp_code: str = Field(pattern=_CORPORATION_CODE_PATTERN)
    corp_name: str = Field(min_length=1, max_length=300)
    corp_eng_name: str | None = Field(default=None, max_length=500)
    stock_code: str | None = Field(default=None, pattern=_STOCK_CODE_PATTERN)
    modify_date: date


class OpenDartCorpCodeSnapshot(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    endpoint: Literal[_CORP_CODE_ENDPOINT] = _CORP_CODE_ENDPOINT
    collected_at_utc: datetime
    archive: OpenDartFileReference
    inner_xml: OpenDartFileReference
    record_count: int = Field(gt=0, le=200_000)
    record_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    records: tuple[OpenDartCorporationCode, ...] = Field(min_length=1, max_length=200_000)

    @field_validator("collected_at_utc")
    @classmethod
    def validate_collected_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def validate_records(self) -> OpenDartCorpCodeSnapshot:
        codes = [item.corp_code for item in self.records]
        if codes != sorted(codes) or len(codes) != len(set(codes)):
            raise ValueError("Open DART corporation codes must be unique and sorted")
        if self.record_count != len(self.records):
            raise ValueError("Open DART corporation record count differs")
        payload = [item.model_dump(mode="json") for item in self.records]
        if self.record_set_sha256 != _sha256(_canonical_json_bytes(payload)):
            raise ValueError("Open DART corporation record-set SHA-256 differs")
        return self


@dataclass(frozen=True, slots=True)
class CollectedOpenDartCorpCodes:
    snapshot: OpenDartCorpCodeSnapshot
    archive_bytes: bytes
    inner_xml_bytes: bytes


class OpenDartEntitySeed(OpenDartModel):
    """One reviewed entity supplied by the provided DB or an approved source."""

    entity_id: str = Field(pattern=_SAFE_ID_PATTERN)
    legal_name: str = Field(min_length=1, max_length=300)
    stock_code: str | None = Field(default=None, pattern=_STOCK_CODE_PATTERN)
    reviewed_aliases: tuple[str, ...] = Field(default=(), max_length=30)
    seed_source: Literal["provided_db", "approved_holdings", "reviewed_one_hop"]

    @field_validator("reviewed_aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("reviewed aliases must be unique and sorted")
        if any(not item.strip() or len(item) > 300 for item in value):
            raise ValueError("reviewed aliases must be non-blank and bounded")
        return value


class OpenDartEntityBinding(OpenDartModel):
    entity_id: str = Field(pattern=_SAFE_ID_PATTERN)
    corp_code: str = Field(pattern=_CORPORATION_CODE_PATTERN)
    corp_name: str = Field(min_length=1, max_length=300)
    stock_code: str | None = Field(default=None, pattern=_STOCK_CODE_PATTERN)
    seed_source: Literal["provided_db", "approved_holdings", "reviewed_one_hop"]
    match_method: Literal[
        "stock_code_exact",
        "legal_name_exact",
        "legal_designator_exact",
        "reviewed_alias_exact",
    ]
    matched_surface: str = Field(min_length=1, max_length=300)


class OpenDartEntitySelectionReport(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    seeds: tuple[OpenDartEntitySeed, ...] = ()
    bindings: tuple[OpenDartEntityBinding, ...] = ()
    unmatched_entity_ids: tuple[str, ...] = ()
    ambiguous_entity_ids: tuple[str, ...] = ()
    seed_set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_selection(self) -> OpenDartEntitySelectionReport:
        seed_ids = [item.entity_id for item in self.seeds]
        if seed_ids != sorted(seed_ids) or len(seed_ids) != len(set(seed_ids)):
            raise ValueError("Open DART entity seeds must be unique and sorted")
        seed_payload = [item.model_dump(mode="json") for item in self.seeds]
        if self.seed_set_sha256 != _sha256(_canonical_json_bytes(seed_payload)):
            raise ValueError("Open DART entity seed-set SHA-256 differs")
        bound_ids = [item.entity_id for item in self.bindings]
        if bound_ids != sorted(bound_ids) or len(bound_ids) != len(set(bound_ids)):
            raise ValueError("Open DART entity bindings must be unique and sorted")
        if self.unmatched_entity_ids != tuple(sorted(set(self.unmatched_entity_ids))):
            raise ValueError("unmatched entity IDs must be unique and sorted")
        if self.ambiguous_entity_ids != tuple(sorted(set(self.ambiguous_entity_ids))):
            raise ValueError("ambiguous entity IDs must be unique and sorted")
        groups = (set(bound_ids), set(self.unmatched_entity_ids), set(self.ambiguous_entity_ids))
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise ValueError("Open DART entity selection outcomes must be disjoint")
        if set(seed_ids) != set().union(*groups):
            raise ValueError("Open DART entity selection does not cover every seed")
        seeds_by_id = {item.entity_id: item for item in self.seeds}
        if any(
            seeds_by_id[item.entity_id].seed_source != item.seed_source for item in self.bindings
        ):
            raise ValueError("Open DART binding seed source differs")
        return self


class OpenDartEntityScopeReference(OpenDartModel):
    """Hash-bound provenance for an automatic provided-DB collection scope."""

    scope_kind: Literal["provided_relation_collection_scope"] = (
        "provided_relation_collection_scope"
    )
    status: Literal["collection_candidates_only_not_authorized_for_runtime_or_release"] = (
        "collection_candidates_only_not_authorized_for_runtime_or_release"
    )
    source_relation_index_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    scope_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    seed_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_count: int = Field(gt=0)
    eligible_relation_count: int = Field(gt=0)
    eligible_entity_count: int = Field(gt=0)
    selected_entity_count: int = Field(gt=0, le=500)
    selected_product_count: int = Field(gt=0)
    managed_by_review_required_count: int = Field(ge=0)
    truncated: bool


class OpenDartDisclosureListQuery(OpenDartModel):
    corp_code: str = Field(pattern=_CORPORATION_CODE_PATTERN)
    bgn_de: date
    end_de: date
    page_count: int = Field(default=100, ge=1, le=100)
    last_reprt_at: Literal["N"] = "N"

    @model_validator(mode="after")
    def validate_window(self) -> OpenDartDisclosureListQuery:
        if self.bgn_de > self.end_de:
            raise ValueError("Open DART disclosure start date follows end date")
        if self.end_de > OPEN_DART_EVIDENCE_CUTOFF:
            raise ValueError("Open DART disclosure end date exceeds the approved cutoff")
        return self

    def request_params(self, page_no: int) -> dict[str, str]:
        if not 1 <= page_no <= _MAX_DISCLOSURE_PAGES:
            raise OpenDartCollectionError("Open DART disclosure page is outside the bound")
        return {
            "bgn_de": self.bgn_de.strftime("%Y%m%d"),
            "corp_code": self.corp_code,
            "end_de": self.end_de.strftime("%Y%m%d"),
            "last_reprt_at": self.last_reprt_at,
            "page_count": str(self.page_count),
            "page_no": str(page_no),
        }


class OpenDartDisclosure(OpenDartModel):
    corp_code: str = Field(pattern=_CORPORATION_CODE_PATTERN)
    corp_name: str = Field(min_length=1, max_length=300)
    stock_code: str | None = Field(default=None, pattern=_STOCK_CODE_PATTERN)
    corp_cls: str = Field(min_length=1, max_length=4)
    report_nm: str = Field(min_length=1, max_length=500)
    rcept_no: str = Field(pattern=_RECEIPT_NUMBER_PATTERN)
    flr_nm: str = Field(min_length=1, max_length=300)
    rcept_dt: date
    rm: str = Field(default="", max_length=100)
    pblntf_ty: str = Field(default="", max_length=10)
    pblntf_detail_ty: str = Field(default="", max_length=20)


class OpenDartDisclosureListPage(OpenDartModel):
    page_no: int = Field(ge=1, le=_MAX_DISCLOSURE_PAGES)
    total_page: int = Field(ge=0, le=_MAX_DISCLOSURE_PAGES)
    total_count: int = Field(ge=0, le=1_000_000)
    returned_count: int = Field(ge=0, le=100)
    response_file: OpenDartFileReference
    response_status: Literal["000", "013"]
    response_message: str = Field(max_length=500)


class OpenDartDisclosureListSnapshot(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    endpoint: Literal[_DISCLOSURE_LIST_ENDPOINT] = _DISCLOSURE_LIST_ENDPOINT
    collected_at_utc: datetime
    query: OpenDartDisclosureListQuery
    pages: tuple[OpenDartDisclosureListPage, ...] = Field(min_length=1)
    disclosures: tuple[OpenDartDisclosure, ...] = ()
    disclosure_set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("collected_at_utc")
    @classmethod
    def validate_collected_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> OpenDartDisclosureListSnapshot:
        page_numbers = [item.page_no for item in self.pages]
        if page_numbers != list(range(1, len(self.pages) + 1)):
            raise ValueError("Open DART disclosure pages must be contiguous")
        if self.pages[-1].total_page not in {0, len(self.pages)}:
            raise ValueError("Open DART disclosure pagination is incomplete")
        receipts = [item.rcept_no for item in self.disclosures]
        if len(receipts) != len(set(receipts)):
            raise ValueError("Open DART disclosure receipts must be unique")
        payload = [item.model_dump(mode="json") for item in self.disclosures]
        if self.disclosure_set_sha256 != _sha256(_canonical_json_bytes(payload)):
            raise ValueError("Open DART disclosure-set SHA-256 differs")
        return self


@dataclass(frozen=True, slots=True)
class CollectedOpenDartDisclosureList:
    snapshot: OpenDartDisclosureListSnapshot
    page_bytes: tuple[bytes, ...]


class OpenDartFilingWhitelistRule(OpenDartModel):
    capability: KnowledgeCapability
    enabled: bool
    allowed_pblntf_types: tuple[str, ...] = ()
    allowed_pblntf_detail_types: tuple[str, ...] = ()
    report_name_terms_any: tuple[str, ...] = ()
    report_name_terms_excluded: tuple[str, ...] = ()

    @field_validator(
        "allowed_pblntf_types",
        "allowed_pblntf_detail_types",
        "report_name_terms_any",
        "report_name_terms_excluded",
    )
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Open DART filing policy values must be unique and sorted")
        if any(not item.strip() or len(item) > 100 for item in value):
            raise ValueError("Open DART filing policy values must be non-blank and bounded")
        return value

    @model_validator(mode="after")
    def validate_enabled_rule(self) -> OpenDartFilingWhitelistRule:
        criteria = (
            self.allowed_pblntf_types,
            self.allowed_pblntf_detail_types,
            self.report_name_terms_any,
        )
        if self.enabled and not any(criteria):
            raise ValueError("enabled Open DART filing rule requires a positive whitelist")
        if not self.enabled and any((*criteria, self.report_name_terms_excluded)):
            raise ValueError("disabled Open DART filing rule cannot carry selection criteria")
        return self

    def admits(self, disclosure: OpenDartDisclosure) -> bool:
        if not self.enabled:
            return False
        # ``pblntf_ty`` and ``pblntf_detail_ty`` are request filters in the
        # official list API and are not consistently echoed in response rows.
        # When the API does echo them, enforce the allowlist; otherwise the
        # exact report-name allowlist below remains the positive admission
        # criterion.
        if (
            self.allowed_pblntf_types
            and disclosure.pblntf_ty
            and disclosure.pblntf_ty not in self.allowed_pblntf_types
        ):
            return False
        if (
            self.allowed_pblntf_detail_types
            and disclosure.pblntf_detail_ty
            and disclosure.pblntf_detail_ty not in self.allowed_pblntf_detail_types
        ):
            return False
        report_name, _, _ = _report_lineage_surface(disclosure.report_nm)
        if self.report_name_terms_any and not any(
            report_name == term
            or report_name.startswith(f"{term} ")
            or report_name.startswith(f"{term}(")
            for term in self.report_name_terms_any
        ):
            return False
        return not any(term in disclosure.report_nm for term in self.report_name_terms_excluded)


class OpenDartFilingWhitelist(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str = Field(pattern=_SAFE_ID_PATTERN)
    evidence_cutoff: date
    max_documents_per_corporation: int = Field(default=3, ge=1, le=10)
    rules: tuple[OpenDartFilingWhitelistRule, ...]

    @model_validator(mode="after")
    def validate_policy(self) -> OpenDartFilingWhitelist:
        capabilities = [item.capability for item in self.rules]
        if capabilities != sorted(capabilities, key=lambda item: item.value):
            raise ValueError("Open DART filing rules must be sorted by capability")
        if set(capabilities) != set(KnowledgeCapability) or len(capabilities) != len(
            KnowledgeCapability
        ):
            raise ValueError("Open DART filing policy must enumerate every capability")
        if self.evidence_cutoff != OPEN_DART_EVIDENCE_CUTOFF:
            raise ValueError("Open DART filing policy uses an unexpected evidence cutoff")
        return self

    @property
    def canonical_sha256(self) -> str:
        return _sha256(_canonical_json_bytes(self.model_dump(mode="json")))


class OpenDartSelectedDisclosure(OpenDartModel):
    disclosure: OpenDartDisclosure
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(
        cls, value: tuple[KnowledgeCapability, ...]
    ) -> tuple[KnowledgeCapability, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("selected Open DART capabilities must be unique and sorted")
        return value


class OpenDartArchiveMember(OpenDartModel):
    archive_name: str = Field(min_length=1, max_length=4096)
    raw_file: OpenDartFileReference
    decoded_encoding: str = Field(min_length=1, max_length=30)
    normalized_text_sha256: str = Field(pattern=_SHA256_PATTERN)


class OpenDartChunkSnapshot(OpenDartModel):
    chunk_id: str = Field(pattern=_SAFE_ID_PATTERN)
    ordinal: int = Field(ge=0, le=100_000)
    text_file: OpenDartFileReference
    locator: str = Field(min_length=1, max_length=500)


class OpenDartOriginalDocumentSnapshot(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    endpoint: Literal[_ORIGINAL_DOCUMENT_ENDPOINT] = _ORIGINAL_DOCUMENT_ENDPOINT
    collected_at_utc: datetime
    rcept_no: str = Field(pattern=_RECEIPT_NUMBER_PATTERN)
    corp_code: str = Field(pattern=_CORPORATION_CODE_PATTERN)
    corp_name: str = Field(min_length=1, max_length=300)
    report_nm: str = Field(min_length=1, max_length=500)
    rcept_dt: date
    correction_marker: str = Field(default="", max_length=100)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1)
    source_uri: str = Field(min_length=8, max_length=2000)
    archive: OpenDartFileReference
    members: tuple[OpenDartArchiveMember, ...] = Field(min_length=1, max_length=100)
    normalized_snapshot: OpenDartFileReference
    normalized_text_sha256: str = Field(pattern=_SHA256_PATTERN)
    chunks: tuple[OpenDartChunkSnapshot, ...] = Field(min_length=1)

    @field_validator("collected_at_utc")
    @classmethod
    def validate_collected_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def validate_document(self) -> OpenDartOriginalDocumentSnapshot:
        member_names = [item.archive_name for item in self.members]
        if member_names != sorted(member_names) or len(member_names) != len(set(member_names)):
            raise ValueError("Open DART archive members must be unique and sorted")
        ordinals = [item.ordinal for item in self.chunks]
        if ordinals != list(range(len(self.chunks))):
            raise ValueError("Open DART chunks must use contiguous ordinals")
        if self.rcept_dt > OPEN_DART_EVIDENCE_CUTOFF:
            raise ValueError("Open DART original document exceeds the evidence cutoff")
        return self


@dataclass(frozen=True, slots=True)
class CollectedOpenDartOriginalDocument:
    snapshot: OpenDartOriginalDocumentSnapshot
    archive_bytes: bytes
    member_bytes: tuple[bytes, ...]
    normalized_snapshot_bytes: bytes
    chunk_bytes: tuple[bytes, ...]


class OpenDartPendingCorpusManifest(OpenDartModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["open_dart_pending_offline_corpus"] = "open_dart_pending_offline_corpus"
    status: Literal["not_authorized_for_retrieval_or_release"] = (
        "not_authorized_for_retrieval_or_release"
    )
    collection_id: str = Field(pattern=_SAFE_ID_PATTERN)
    evidence_cutoff: date
    filing_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    corp_codes: OpenDartCorpCodeSnapshot
    entity_selection: OpenDartEntitySelectionReport
    entity_scope: OpenDartEntityScopeReference | None = None
    disclosure_lists: tuple[OpenDartDisclosureListSnapshot, ...]
    selected_disclosures: tuple[OpenDartSelectedDisclosure, ...]
    unavailable_original_document_receipts: tuple[str, ...] = ()
    documents: tuple[OpenDartOriginalDocumentSnapshot, ...]
    document_count: int = Field(ge=0, le=100_000)
    chunk_count: int = Field(ge=0, le=1_000_000)
    file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    relation_claim_count: Literal[0] = 0
    relation_claim_status: Literal["not_extracted_pending_independent_review"] = (
        "not_extracted_pending_independent_review"
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> OpenDartPendingCorpusManifest:
        if self.entity_scope is not None and (
            self.entity_scope.seed_set_sha256 != self.entity_selection.seed_set_sha256
            or self.entity_scope.selected_entity_count != len(self.entity_selection.seeds)
        ):
            raise ValueError("Open DART entity scope differs from selected seeds")
        corp_codes = {item.corp_code: item for item in self.corp_codes.records}
        for binding in self.entity_selection.bindings:
            corporation = corp_codes.get(binding.corp_code)
            if (
                corporation is None
                or binding.corp_name != corporation.corp_name
                or binding.stock_code != corporation.stock_code
            ):
                raise ValueError("Open DART entity binding differs from corporation codes")
        queries = [
            (item.query.corp_code, item.query.bgn_de, item.query.end_de)
            for item in self.disclosure_lists
        ]
        if queries != sorted(queries) or len(queries) != len(set(queries)):
            raise ValueError("Open DART disclosure snapshots must be unique and sorted")
        bound_corp_codes = {item.corp_code for item in self.entity_selection.bindings}
        queried_corp_codes = {item.query.corp_code for item in self.disclosure_lists}
        if not queried_corp_codes.issubset(bound_corp_codes):
            raise ValueError("Open DART disclosure query escaped selected entities")
        listed_by_receipt: dict[str, OpenDartDisclosure] = {}
        for snapshot in self.disclosure_lists:
            for disclosure in snapshot.disclosures:
                previous = listed_by_receipt.get(disclosure.rcept_no)
                if previous is not None and previous != disclosure:
                    raise ValueError("Open DART list receipts carry conflicting metadata")
                listed_by_receipt[disclosure.rcept_no] = disclosure
        selected_receipts = [item.disclosure.rcept_no for item in self.selected_disclosures]
        if selected_receipts != sorted(selected_receipts) or len(selected_receipts) != len(
            set(selected_receipts)
        ):
            raise ValueError("Open DART selected disclosures must be unique and sorted")
        if any(
            _rm_contains(item.disclosure, "정") or _is_withdrawn_disclosure(item.disclosure)
            for item in self.selected_disclosures
        ):
            raise ValueError("Open DART selected disclosures must be terminal and active")
        if any(
            listed_by_receipt.get(item.disclosure.rcept_no) != item.disclosure
            for item in self.selected_disclosures
        ):
            raise ValueError("Open DART selected disclosure differs from its list receipt")
        selected_by_receipt = {item.disclosure.rcept_no: item for item in self.selected_disclosures}
        unavailable = list(self.unavailable_original_document_receipts)
        if unavailable != sorted(unavailable) or len(unavailable) != len(set(unavailable)):
            raise ValueError("Open DART unavailable receipts must be unique and sorted")
        if any(receipt not in selected_by_receipt for receipt in unavailable):
            raise ValueError("Open DART unavailable receipt was not selected")
        receipts = [item.rcept_no for item in self.documents]
        if receipts != sorted(receipts) or len(receipts) != len(set(receipts)):
            raise ValueError("Open DART original documents must be unique and sorted")
        for document in self.documents:
            selected = selected_by_receipt.get(document.rcept_no)
            if selected is None:
                raise ValueError("Open DART original document was not selected")
            disclosure = selected.disclosure
            if (
                document.corp_code != disclosure.corp_code
                or document.corp_name != disclosure.corp_name
                or document.report_nm != disclosure.report_nm
                or document.rcept_dt != disclosure.rcept_dt
                or document.correction_marker != disclosure.rm
                or document.capabilities != selected.capabilities
            ):
                raise ValueError("Open DART original document differs from its selected receipt")
        if set(receipts).intersection(unavailable):
            raise ValueError("Open DART receipt cannot be both downloaded and unavailable")
        if set(receipts).union(unavailable) != set(selected_by_receipt):
            raise ValueError("Open DART selected receipt coverage is incomplete")
        if self.document_count != len(self.documents):
            raise ValueError("Open DART pending document count differs")
        if self.chunk_count != sum(len(item.chunks) for item in self.documents):
            raise ValueError("Open DART pending chunk count differs")
        if self.evidence_cutoff != OPEN_DART_EVIDENCE_CUTOFF:
            raise ValueError("Open DART pending corpus uses an unexpected cutoff")
        if self.file_manifest_sha256 != _open_dart_file_manifest_sha256(self):
            raise ValueError("Open DART pending file-manifest SHA-256 differs")
        return self

    @property
    def canonical_sha256(self) -> str:
        return _sha256(open_dart_manifest_bytes(self))


@dataclass(frozen=True, slots=True)
class MaterializedOpenDartPendingCorpus:
    manifest: OpenDartPendingCorpusManifest
    manifest_sha256: str


class _VisibleTextParser(HTMLParser):
    _IGNORED = frozenset({"script", "style", "noscript", "svg"})
    _BLOCKS = frozenset(
        {"article", "br", "div", "h1", "h2", "h3", "li", "p", "section", "table", "td", "th", "tr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in self._IGNORED:
            self._ignored_depth += 1
        elif not self._ignored_depth and normalized in self._BLOCKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and normalized in self._BLOCKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return normalize_document_text(
            "\n".join(
                normalized
                for value in "".join(self._parts).splitlines()
                if (normalized := " ".join(value.split()))
            )
        )


def _validated_zip_members(
    content: bytes,
    *,
    allowed_suffixes: frozenset[str],
    max_member_bytes: int = _MAX_INNER_FILE_BYTES,
) -> tuple[tuple[str, bytes], ...]:
    if not content or len(content) > _MAX_ARCHIVE_BYTES:
        raise OpenDartCollectionError("Open DART ZIP size is outside the bound")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            if not infos or len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise OpenDartCollectionError("Open DART ZIP member count is outside the bound")
            members: list[tuple[str, bytes]] = []
            total_size = 0
            seen: set[str] = set()
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    "\\" in name
                    or "\x00" in name
                    or path.is_absolute()
                    or path.as_posix() != name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or name in seen
                    or PurePosixPath(name).suffix.casefold() not in allowed_suffixes
                    or info.flag_bits & 0x1
                    or stat.S_ISLNK(unix_mode)
                ):
                    raise OpenDartCollectionError("Open DART ZIP contains an unsafe member")
                if info.file_size <= 0 or info.file_size > max_member_bytes:
                    raise OpenDartCollectionError("Open DART ZIP member size is outside the bound")
                if info.compress_size == 0 or info.file_size > info.compress_size * 200:
                    raise OpenDartCollectionError("Open DART ZIP compression ratio is unsafe")
                total_size += info.file_size
                if total_size > _MAX_UNCOMPRESSED_BYTES:
                    raise OpenDartCollectionError("Open DART ZIP expands beyond the total bound")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise OpenDartCollectionError("Open DART ZIP member size differs")
                seen.add(name)
                members.append((name, data))
    except (BadZipFile, OSError) as error:
        raise OpenDartCollectionError("Open DART response is not a valid ZIP") from error
    return tuple(sorted(members, key=lambda item: item[0]))


def _response_body(
    response: OpenDartHttpResponse,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    if response.status_code != 200:
        raise OpenDartCollectionError(f"{label} did not return HTTP 200")
    if not response.body or len(response.body) > max_bytes:
        raise OpenDartCollectionError(f"{label} response size is outside the bound")
    return response.body


def _xml_text(element: ET.Element, name: str) -> str:
    child = element.find(name)
    return "" if child is None or child.text is None else child.text.strip()


def collect_open_dart_corp_codes(
    transport: OpenDartTransport,
    *,
    collected_at_utc: datetime,
) -> CollectedOpenDartCorpCodes:
    collected_at_utc = _require_utc(collected_at_utc)
    response = transport.get(
        _CORP_CODE_ENDPOINT,
        params={},
        timeout_seconds=30.0,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    archive_bytes = _response_body(
        response, label="Open DART corporation-code API", max_bytes=_MAX_ARCHIVE_BYTES
    )
    members = _validated_zip_members(
        archive_bytes,
        allowed_suffixes=frozenset({".xml"}),
        max_member_bytes=_MAX_CORP_CODE_INNER_FILE_BYTES,
    )
    if len(members) != 1:
        raise OpenDartCollectionError("Open DART corporation-code ZIP must contain one XML")
    archive_name, xml_bytes = members[0]
    if b"<!doctype" in xml_bytes.lower() or b"<!entity" in xml_bytes.lower():
        raise OpenDartCollectionError("Open DART corporation-code XML contains a DTD")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise OpenDartCollectionError("Open DART corporation-code XML is invalid") from error
    parsed: dict[str, OpenDartCorporationCode] = {}
    for item in root.findall(".//list"):
        corp_code = _xml_text(item, "corp_code")
        corp_name = _xml_text(item, "corp_name")
        corp_eng_name = _xml_text(item, "corp_eng_name") or None
        stock_code = _xml_text(item, "stock_code") or None
        modify_date_raw = _xml_text(item, "modify_date")
        try:
            record = OpenDartCorporationCode(
                corp_code=corp_code,
                corp_name=corp_name,
                corp_eng_name=corp_eng_name,
                stock_code=stock_code,
                modify_date=datetime.strptime(modify_date_raw, "%Y%m%d").date(),
            )
        except ValueError as error:
            raise OpenDartCollectionError("Open DART corporation-code record is invalid") from error
        if record.corp_code in parsed:
            raise OpenDartCollectionError("Open DART corporation-code response contains duplicates")
        parsed[record.corp_code] = record
    records = tuple(parsed[key] for key in sorted(parsed))
    if not records:
        raise OpenDartCollectionError("Open DART corporation-code response is empty")
    record_set_sha256 = _sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in records])
    )
    snapshot = OpenDartCorpCodeSnapshot(
        collected_at_utc=collected_at_utc,
        archive=OpenDartFileReference(
            relative_path="raw/corp-code/corpCode.zip",
            size_bytes=len(archive_bytes),
            sha256=_sha256(archive_bytes),
        ),
        inner_xml=OpenDartFileReference(
            relative_path=f"raw/corp-code/{PurePosixPath(archive_name).name}",
            size_bytes=len(xml_bytes),
            sha256=_sha256(xml_bytes),
        ),
        record_count=len(records),
        record_set_sha256=record_set_sha256,
        records=records,
    )
    return CollectedOpenDartCorpCodes(
        snapshot=snapshot,
        archive_bytes=archive_bytes,
        inner_xml_bytes=xml_bytes,
    )


def select_open_dart_entities(
    corp_codes: OpenDartCorpCodeSnapshot,
    seeds: tuple[OpenDartEntitySeed, ...],
) -> OpenDartEntitySelectionReport:
    ordered_seeds = tuple(sorted(seeds, key=lambda item: item.entity_id))
    if len({item.entity_id for item in ordered_seeds}) != len(ordered_seeds):
        raise OpenDartCollectionError("Open DART entity seeds contain duplicate IDs")
    by_stock: dict[str, list[OpenDartCorporationCode]] = {}
    by_name: dict[str, list[OpenDartCorporationCode]] = {}
    by_legal_name: dict[str, list[OpenDartCorporationCode]] = {}
    for item in corp_codes.records:
        if item.stock_code is not None:
            by_stock.setdefault(item.stock_code, []).append(item)
        by_name.setdefault(_identity_key(item.corp_name), []).append(item)
        by_legal_name.setdefault(_legal_name_identity_key(item.corp_name), []).append(item)

    bindings: list[OpenDartEntityBinding] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for seed in ordered_seeds:
        candidates: tuple[OpenDartCorporationCode, ...] = ()
        match_method: (
            Literal[
                "stock_code_exact",
                "legal_name_exact",
                "legal_designator_exact",
                "reviewed_alias_exact",
            ]
            | None
        ) = None
        matched_surface = ""
        if seed.stock_code is not None and by_stock.get(seed.stock_code):
            candidates = tuple(by_stock[seed.stock_code])
            match_method = "stock_code_exact"
            matched_surface = seed.stock_code
        elif by_name.get(_identity_key(seed.legal_name)):
            candidates = tuple(by_name[_identity_key(seed.legal_name)])
            match_method = "legal_name_exact"
            matched_surface = seed.legal_name
        elif by_legal_name.get(_legal_name_identity_key(seed.legal_name)):
            candidates = tuple(by_legal_name[_legal_name_identity_key(seed.legal_name)])
            match_method = "legal_designator_exact"
            matched_surface = seed.legal_name
        else:
            alias_matches = {
                item.corp_code: item
                for alias in seed.reviewed_aliases
                for item in by_name.get(_identity_key(alias), ())
            }
            candidates = tuple(alias_matches[key] for key in sorted(alias_matches))
            if candidates:
                match_method = "reviewed_alias_exact"
                matched_aliases = [
                    alias
                    for alias in seed.reviewed_aliases
                    if any(
                        _identity_key(alias) == _identity_key(item.corp_name) for item in candidates
                    )
                ]
                matched_surface = matched_aliases[0]
        if not candidates:
            unmatched.append(seed.entity_id)
            continue
        if len(candidates) != 1 or match_method is None:
            ambiguous.append(seed.entity_id)
            continue
        matched = candidates[0]
        bindings.append(
            OpenDartEntityBinding(
                entity_id=seed.entity_id,
                corp_code=matched.corp_code,
                corp_name=matched.corp_name,
                stock_code=matched.stock_code,
                seed_source=seed.seed_source,
                match_method=match_method,
                matched_surface=matched_surface,
            )
        )
    seed_set_sha256 = _sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in ordered_seeds])
    )
    return OpenDartEntitySelectionReport(
        seeds=ordered_seeds,
        bindings=tuple(bindings),
        unmatched_entity_ids=tuple(unmatched),
        ambiguous_entity_ids=tuple(ambiguous),
        seed_set_sha256=seed_set_sha256,
    )


def _parse_date(value: object, *, label: str) -> date:
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError as error:
        raise OpenDartCollectionError(f"Open DART {label} is invalid") from error


def _optional_string(value: object) -> str:
    normalized = "" if value is None else str(value).strip()
    return normalized


def _parse_disclosure(payload: object) -> OpenDartDisclosure:
    if not isinstance(payload, dict):
        raise OpenDartCollectionError("Open DART disclosure row is not an object")
    stock_code = _optional_string(payload.get("stock_code")) or None
    try:
        return OpenDartDisclosure(
            corp_code=_optional_string(payload.get("corp_code")),
            corp_name=_optional_string(payload.get("corp_name")),
            stock_code=stock_code,
            corp_cls=_optional_string(payload.get("corp_cls")),
            report_nm=_optional_string(payload.get("report_nm")),
            rcept_no=_optional_string(payload.get("rcept_no")),
            flr_nm=_optional_string(payload.get("flr_nm")),
            rcept_dt=_parse_date(payload.get("rcept_dt"), label="receipt date"),
            rm=_optional_string(payload.get("rm")),
            pblntf_ty=_optional_string(payload.get("pblntf_ty")),
            pblntf_detail_ty=_optional_string(payload.get("pblntf_detail_ty")),
        )
    except ValueError as error:
        raise OpenDartCollectionError("Open DART disclosure row is invalid") from error


def collect_open_dart_disclosure_list(
    transport: OpenDartTransport,
    query: OpenDartDisclosureListQuery,
    *,
    collected_at_utc: datetime,
) -> CollectedOpenDartDisclosureList:
    collected_at_utc = _require_utc(collected_at_utc)
    pages: list[OpenDartDisclosureListPage] = []
    page_bytes: list[bytes] = []
    disclosures: list[OpenDartDisclosure] = []
    total_pages: int | None = None
    page_no = 1
    while total_pages is None or page_no <= total_pages:
        if page_no > _MAX_DISCLOSURE_PAGES:
            raise OpenDartCollectionError("Open DART disclosure pagination exceeded the bound")
        params = query.request_params(page_no)
        response = transport.get(
            _DISCLOSURE_LIST_ENDPOINT,
            params=params,
            timeout_seconds=20.0,
            max_bytes=4 * 1024 * 1024,
        )
        body = _response_body(
            response, label="Open DART disclosure-list API", max_bytes=4 * 1024 * 1024
        )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenDartCollectionError("Open DART disclosure-list JSON is invalid") from error
        if not isinstance(payload, dict):
            raise OpenDartCollectionError("Open DART disclosure-list payload is not an object")
        status_code = _optional_string(payload.get("status"))
        message = _optional_string(payload.get("message"))
        if status_code == "013":
            if page_no != 1:
                raise OpenDartCollectionError("Open DART no-data status appeared after page one")
            page_total = 0
            total_count = 0
            rows: list[object] = []
        elif status_code == "000":
            try:
                page_total = int(payload.get("total_page", 0))
                total_count = int(payload.get("total_count", 0))
            except (TypeError, ValueError) as error:
                raise OpenDartCollectionError("Open DART pagination metadata is invalid") from error
            raw_rows = payload.get("list", [])
            if not isinstance(raw_rows, list):
                raise OpenDartCollectionError("Open DART disclosure list is not an array")
            rows = raw_rows
            if not 1 <= page_total <= _MAX_DISCLOSURE_PAGES:
                raise OpenDartCollectionError("Open DART total page count is outside the bound")
            if not 0 <= total_count <= 1_000_000 or len(rows) > query.page_count:
                raise OpenDartCollectionError("Open DART disclosure count is outside the bound")
        else:
            raise OpenDartCollectionError("Open DART disclosure-list API returned an error")
        if total_pages is None:
            total_pages = page_total
        elif page_total != total_pages:
            raise OpenDartCollectionError("Open DART total page count changed during collection")
        page_disclosures = tuple(_parse_disclosure(item) for item in rows)
        if any(item.corp_code != query.corp_code for item in page_disclosures):
            raise OpenDartCollectionError("Open DART disclosure row escaped the requested company")
        if any(
            item.rcept_dt < query.bgn_de or item.rcept_dt > query.end_de
            for item in page_disclosures
        ):
            raise OpenDartCollectionError("Open DART disclosure row escaped the requested dates")
        response_path = (
            f"raw/disclosure-lists/{query.corp_code}-"
            f"{query.bgn_de:%Y%m%d}-{query.end_de:%Y%m%d}-p{page_no:04d}.json"
        )
        pages.append(
            OpenDartDisclosureListPage(
                page_no=page_no,
                total_page=page_total,
                total_count=total_count,
                returned_count=len(page_disclosures),
                response_file=OpenDartFileReference(
                    relative_path=response_path,
                    size_bytes=len(body),
                    sha256=_sha256(body),
                ),
                response_status=status_code,
                response_message=message,
            )
        )
        page_bytes.append(body)
        disclosures.extend(page_disclosures)
        if status_code == "013":
            break
        page_no += 1
    receipts = [item.rcept_no for item in disclosures]
    if len(receipts) != len(set(receipts)):
        raise OpenDartCollectionError("Open DART pagination returned duplicate receipts")
    disclosure_tuple = tuple(disclosures)
    disclosure_set_sha256 = _sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in disclosure_tuple])
    )
    snapshot = OpenDartDisclosureListSnapshot(
        collected_at_utc=collected_at_utc,
        query=query,
        pages=tuple(pages),
        disclosures=disclosure_tuple,
        disclosure_set_sha256=disclosure_set_sha256,
    )
    return CollectedOpenDartDisclosureList(
        snapshot=snapshot,
        page_bytes=tuple(page_bytes),
    )


def load_open_dart_filing_whitelist() -> OpenDartFilingWhitelist:
    resource = files("finance_agent_core.config").joinpath("open_dart_filing_policy.json")
    try:
        return OpenDartFilingWhitelist.model_validate_json(resource.read_bytes())
    except (OSError, ValueError) as error:
        raise OpenDartCollectionError("packaged Open DART filing policy is invalid") from error


def select_open_dart_disclosures(
    snapshots: tuple[OpenDartDisclosureListSnapshot, ...],
    policy: OpenDartFilingWhitelist,
    *,
    skip_unresolved_correction_lineages: bool = False,
) -> tuple[OpenDartSelectedDisclosure, ...]:
    if policy.evidence_cutoff != OPEN_DART_EVIDENCE_CUTOFF:
        raise OpenDartCollectionError("Open DART policy cutoff differs")
    candidates: dict[str, OpenDartSelectedDisclosure] = {}
    for snapshot in snapshots:
        for disclosure in snapshot.disclosures:
            capabilities = tuple(
                rule.capability for rule in policy.rules if rule.admits(disclosure)
            )
            if not capabilities:
                continue
            candidate = OpenDartSelectedDisclosure(
                disclosure=disclosure,
                capabilities=capabilities,
            )
            previous = candidates.get(disclosure.rcept_no)
            if previous is not None and previous != candidate:
                raise OpenDartCollectionError(
                    "one Open DART receipt produced conflicting whitelist decisions"
                )
            candidates[disclosure.rcept_no] = candidate

    lineages: dict[
        tuple[str, str, str, str],
        list[tuple[OpenDartSelectedDisclosure, bool, bool]],
    ] = {}
    for receipt in sorted(candidates):
        candidate = candidates[receipt]
        _, is_correction, report_name_marks_withdrawal = _report_lineage_surface(
            candidate.disclosure.report_nm
        )
        is_withdrawal = _is_withdrawn_disclosure(candidate.disclosure)
        lineages.setdefault(_filing_lineage_key(candidate.disclosure), []).append(
            (candidate, is_correction, is_withdrawal or report_name_marks_withdrawal)
        )

    selected: dict[str, OpenDartSelectedDisclosure] = {}
    for lineage_key in sorted(lineages):
        lineage = sorted(
            lineages[lineage_key],
            key=lambda item: (item[0].disclosure.rcept_dt, item[0].disclosure.rcept_no),
        )
        if any(item[2] for item in lineage):
            continue
        superseded = [item for item in lineage if _rm_contains(item[0].disclosure, "정")]
        has_correction = any(item[1] for item in lineage)
        if not superseded and not has_correction:
            for candidate, _, _ in lineage:
                selected[candidate.disclosure.rcept_no] = candidate
            continue

        terminals = [
            item for item in lineage if not _rm_contains(item[0].disclosure, "정") and item[1]
        ]
        if superseded:
            latest_superseded_key = max(
                (item[0].disclosure.rcept_dt, item[0].disclosure.rcept_no) for item in superseded
            )
            terminals = [
                item
                for item in terminals
                if (item[0].disclosure.rcept_dt, item[0].disclosure.rcept_no)
                > latest_superseded_key
            ]
            if not terminals:
                if skip_unresolved_correction_lineages:
                    continue
                raise OpenDartCollectionError(
                    "Open DART corrected filing has no terminal correction within the cutoff"
                )
        if not terminals:
            if skip_unresolved_correction_lineages:
                continue
            raise OpenDartCollectionError(
                "Open DART correction lineage has no selectable terminal filing"
            )
        terminal = max(
            terminals,
            key=lambda item: (item[0].disclosure.rcept_dt, item[0].disclosure.rcept_no),
        )[0]
        selected[terminal.disclosure.rcept_no] = terminal
    # A large provided-DB company scope must not turn into an unbounded download
    # of every historical filing.  Keep only the latest terminal filing for the
    # same corporation, disclosure class and capability set, then apply the
    # policy's per-corporation ceiling.  The complete list responses remain in
    # the pending manifest, so excluded receipts are still auditable.
    latest_by_scope: dict[
        tuple[str, str, tuple[str, ...]],
        OpenDartSelectedDisclosure,
    ] = {}
    for item in selected.values():
        disclosure = item.disclosure
        scope = (
            disclosure.corp_code,
            disclosure.pblntf_ty,
            tuple(capability.value for capability in item.capabilities),
        )
        previous = latest_by_scope.get(scope)
        if previous is None or (
            disclosure.rcept_dt,
            disclosure.rcept_no,
        ) > (
            previous.disclosure.rcept_dt,
            previous.disclosure.rcept_no,
        ):
            latest_by_scope[scope] = item

    by_corporation: dict[str, list[OpenDartSelectedDisclosure]] = {}
    for item in latest_by_scope.values():
        by_corporation.setdefault(item.disclosure.corp_code, []).append(item)
    bounded: list[OpenDartSelectedDisclosure] = []
    for corp_code in sorted(by_corporation):
        newest_first = sorted(
            by_corporation[corp_code],
            key=lambda item: (
                item.disclosure.rcept_dt,
                item.disclosure.rcept_no,
            ),
            reverse=True,
        )
        bounded.extend(newest_first[: policy.max_documents_per_corporation])
    return tuple(sorted(bounded, key=lambda item: item.disclosure.rcept_no))


def _decode_dart_document(data: bytes) -> tuple[str, str]:
    if b"<!doctype" in data.lower() or b"<!entity" in data.lower():
        raise OpenDartCollectionError("Open DART original document contains a DTD")
    declared = _XML_DECLARATION_ENCODING.search(data[:512])
    candidates = []
    if declared is not None:
        candidates.append(declared.group("encoding").decode("ascii", errors="ignore"))
    candidates.extend(("utf-8-sig", "cp949", "euc-kr"))
    attempted: set[str] = set()
    for encoding in candidates:
        normalized_encoding = encoding.casefold()
        if not normalized_encoding or normalized_encoding in attempted:
            continue
        attempted.add(normalized_encoding)
        try:
            return data.decode(encoding), normalized_encoding
        except (LookupError, UnicodeDecodeError):
            continue
    raise OpenDartCollectionError("Open DART original document encoding is unsupported")


def _extract_visible_text(data: bytes) -> tuple[str, str]:
    decoded, encoding = _decode_dart_document(data)
    parser = _VisibleTextParser()
    try:
        parser.feed(decoded)
        parser.close()
    except (ValueError, TypeError) as error:
        raise OpenDartCollectionError("Open DART original document markup is invalid") from error
    text = parser.text()
    if not text:
        raise OpenDartCollectionError("Open DART original document produced no text")
    return text, encoding


def collect_open_dart_original_document(
    transport: OpenDartTransport,
    selected: OpenDartSelectedDisclosure,
    *,
    collected_at_utc: datetime,
) -> CollectedOpenDartOriginalDocument:
    collected_at_utc = _require_utc(collected_at_utc)
    disclosure = selected.disclosure
    response = transport.get(
        _ORIGINAL_DOCUMENT_ENDPOINT,
        params={"rcept_no": disclosure.rcept_no},
        timeout_seconds=30.0,
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    archive_bytes = _response_body(
        response, label="Open DART original-document API", max_bytes=_MAX_ARCHIVE_BYTES
    )
    if response.content_type in {
        "application/xml",
        "text/xml",
    } or archive_bytes.lstrip().startswith(b"<?xml"):
        if b"<!doctype" in archive_bytes.lower() or b"<!entity" in archive_bytes.lower():
            raise OpenDartCollectionError("Open DART original-document error XML contains a DTD")
        try:
            error_root = ET.fromstring(archive_bytes)
        except ET.ParseError as error:
            raise OpenDartCollectionError(
                "Open DART original-document error response is invalid"
            ) from error
        status_code = _xml_text(error_root, "status")
        if status_code == "014":
            raise OpenDartOriginalDocumentUnavailable(
                "Open DART original document is unavailable"
            )
        raise OpenDartCollectionError("Open DART original-document API returned an error")
    try:
        members = _validated_zip_members(
            archive_bytes,
            allowed_suffixes=frozenset({".htm", ".html", ".txt", ".xhtml", ".xml"}),
        )
    except OpenDartCollectionError as error:
        raise OpenDartCollectionError(
            "Open DART original-document receipt "
            f"{disclosure.rcept_no} is unusable: {error}"
        ) from error
    member_models: list[OpenDartArchiveMember] = []
    member_bytes: list[bytes] = []
    text_sections: list[str] = []
    for archive_name, raw in members:
        text, encoding = _extract_visible_text(raw)
        safe_name = archive_name.replace("/", "__")
        raw_path = f"raw/documents/{disclosure.rcept_no}/members/{safe_name}"
        member_models.append(
            OpenDartArchiveMember(
                archive_name=archive_name,
                raw_file=OpenDartFileReference(
                    relative_path=raw_path,
                    size_bytes=len(raw),
                    sha256=_sha256(raw),
                ),
                decoded_encoding=encoding,
                normalized_text_sha256=_sha256(text.encode("utf-8")),
            )
        )
        member_bytes.append(raw)
        text_sections.append(f"[OPEN_DART_FILE {archive_name}]\n{text}")
    normalized = normalize_document_text("\n\n".join(text_sections))
    normalized_bytes = normalized.encode("utf-8")
    if not normalized_bytes or len(normalized_bytes) > _MAX_PENDING_TEXT_BYTES:
        raise OpenDartCollectionError("Open DART normalized snapshot size is outside the bound")
    chunk_texts = tuple(chunk_document(normalized))
    chunk_bytes = tuple(item.encode("utf-8") for item in chunk_texts)
    chunk_models = tuple(
        OpenDartChunkSnapshot(
            chunk_id=f"opendart:{disclosure.rcept_no}:{ordinal:05d}",
            ordinal=ordinal,
            text_file=OpenDartFileReference(
                relative_path=f"chunks/{disclosure.rcept_no}/{ordinal:05d}.txt",
                size_bytes=len(content),
                sha256=_sha256(content),
            ),
            locator=f"rcept_no={disclosure.rcept_no};chunk={ordinal}",
        )
        for ordinal, content in enumerate(chunk_bytes)
    )
    archive_path = f"raw/documents/{disclosure.rcept_no}/document.zip"
    snapshot_path = f"snapshots/{disclosure.rcept_no}.txt"
    snapshot = OpenDartOriginalDocumentSnapshot(
        collected_at_utc=collected_at_utc,
        rcept_no=disclosure.rcept_no,
        corp_code=disclosure.corp_code,
        corp_name=disclosure.corp_name,
        report_nm=disclosure.report_nm,
        rcept_dt=disclosure.rcept_dt,
        correction_marker=disclosure.rm,
        capabilities=selected.capabilities,
        source_uri=("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + disclosure.rcept_no),
        archive=OpenDartFileReference(
            relative_path=archive_path,
            size_bytes=len(archive_bytes),
            sha256=_sha256(archive_bytes),
        ),
        members=tuple(member_models),
        normalized_snapshot=OpenDartFileReference(
            relative_path=snapshot_path,
            size_bytes=len(normalized_bytes),
            sha256=_sha256(normalized_bytes),
        ),
        normalized_text_sha256=_sha256(normalized_bytes),
        chunks=chunk_models,
    )
    return CollectedOpenDartOriginalDocument(
        snapshot=snapshot,
        archive_bytes=archive_bytes,
        member_bytes=tuple(member_bytes),
        normalized_snapshot_bytes=normalized_bytes,
        chunk_bytes=chunk_bytes,
    )


def open_dart_file_references(
    manifest: OpenDartPendingCorpusManifest,
) -> tuple[OpenDartFileReference, ...]:
    references = [manifest.corp_codes.archive, manifest.corp_codes.inner_xml]
    references.extend(
        page.response_file for item in manifest.disclosure_lists for page in item.pages
    )
    for document in manifest.documents:
        references.append(document.archive)
        references.extend(member.raw_file for member in document.members)
        references.append(document.normalized_snapshot)
        references.extend(chunk.text_file for chunk in document.chunks)
    return tuple(references)


def _open_dart_file_manifest_sha256(manifest: OpenDartPendingCorpusManifest) -> str:
    references = open_dart_file_references(manifest)
    payload = [
        item.model_dump(mode="json")
        for item in sorted(references, key=lambda item: item.relative_path)
    ]
    return _sha256(_canonical_json_bytes(payload))


def build_open_dart_pending_manifest(
    corp_codes: CollectedOpenDartCorpCodes,
    entity_selection: OpenDartEntitySelectionReport,
    disclosure_lists: tuple[CollectedOpenDartDisclosureList, ...],
    selected_disclosures: tuple[OpenDartSelectedDisclosure, ...],
    documents: tuple[CollectedOpenDartOriginalDocument, ...],
    policy: OpenDartFilingWhitelist,
    entity_scope: OpenDartEntityScopeReference | None = None,
    unavailable_original_document_receipts: tuple[str, ...] = (),
) -> OpenDartPendingCorpusManifest:
    ordered_lists = tuple(
        sorted(
            disclosure_lists,
            key=lambda item: (
                item.snapshot.query.corp_code,
                item.snapshot.query.bgn_de,
                item.snapshot.query.end_de,
            ),
        )
    )
    ordered_documents = tuple(sorted(documents, key=lambda item: item.snapshot.rcept_no))
    canonical_selected = select_open_dart_disclosures(
        tuple(item.snapshot for item in ordered_lists),
        policy,
        skip_unresolved_correction_lineages=entity_scope is not None,
    )
    if selected_disclosures != canonical_selected:
        raise OpenDartCollectionError(
            "Open DART selected disclosures differ from the exact selector output"
        )
    selected_by_receipt = {item.disclosure.rcept_no: item for item in canonical_selected}
    for acquisition in ordered_documents:
        document = acquisition.snapshot
        selected = selected_by_receipt.get(document.rcept_no)
        if selected is None:
            raise OpenDartCollectionError("Open DART original document was not selected")
        disclosure = selected.disclosure
        if (
            document.corp_code != disclosure.corp_code
            or document.corp_name != disclosure.corp_name
            or document.report_nm != disclosure.report_nm
            or document.rcept_dt != disclosure.rcept_dt
            or document.correction_marker != disclosure.rm
            or document.capabilities != selected.capabilities
        ):
            raise OpenDartCollectionError(
                "Open DART original document differs from its selected receipt"
            )
    core = {
        "corp_codes": corp_codes.snapshot.record_set_sha256,
        "disclosure_lists": [item.snapshot.disclosure_set_sha256 for item in ordered_lists],
        "documents": [item.snapshot.normalized_text_sha256 for item in ordered_documents],
        "entity_selection": entity_selection.seed_set_sha256,
        "entity_scope": None
        if entity_scope is None
        else entity_scope.model_dump(mode="json"),
        "filing_policy": policy.canonical_sha256,
        "selected_disclosures": [item.model_dump(mode="json") for item in canonical_selected],
        "unavailable_original_document_receipts": list(
            unavailable_original_document_receipts
        ),
    }
    collection_id = f"open-dart-20260824-{_sha256(_canonical_json_bytes(core))[:24]}"
    provisional = OpenDartPendingCorpusManifest.model_construct(
        collection_id=collection_id,
        evidence_cutoff=OPEN_DART_EVIDENCE_CUTOFF,
        filing_policy_sha256=policy.canonical_sha256,
        corp_codes=corp_codes.snapshot,
        entity_selection=entity_selection,
        entity_scope=entity_scope,
        disclosure_lists=tuple(item.snapshot for item in ordered_lists),
        selected_disclosures=canonical_selected,
        unavailable_original_document_receipts=unavailable_original_document_receipts,
        documents=tuple(item.snapshot for item in ordered_documents),
        document_count=len(ordered_documents),
        chunk_count=sum(len(item.snapshot.chunks) for item in ordered_documents),
        file_manifest_sha256="0" * 64,
    )
    return OpenDartPendingCorpusManifest.model_validate(
        {
            **provisional.model_dump(mode="json"),
            "file_manifest_sha256": _open_dart_file_manifest_sha256(provisional),
        }
    )


def open_dart_manifest_bytes(manifest: OpenDartPendingCorpusManifest) -> bytes:
    return _canonical_json_bytes(manifest.model_dump(mode="json"))


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize_open_dart_pending_corpus(
    output_root: str | Path,
    *,
    corp_codes: CollectedOpenDartCorpCodes,
    entity_selection: OpenDartEntitySelectionReport,
    disclosure_lists: tuple[CollectedOpenDartDisclosureList, ...],
    selected_disclosures: tuple[OpenDartSelectedDisclosure, ...],
    documents: tuple[CollectedOpenDartOriginalDocument, ...],
    policy: OpenDartFilingWhitelist,
    entity_scope: OpenDartEntityScopeReference | None = None,
    unavailable_original_document_receipts: tuple[str, ...] = (),
) -> MaterializedOpenDartPendingCorpus:
    """Atomically persist a key-free, review-pending offline corpus.

    No Relation Store claim or runtime retrieval authority is created here.
    Independent source-rights and finance-domain review must convert this output
    through the existing approved-corpus/relation sealing boundaries.
    """

    manifest = build_open_dart_pending_manifest(
        corp_codes,
        entity_selection,
        disclosure_lists,
        selected_disclosures,
        documents,
        policy,
        entity_scope,
        unavailable_original_document_receipts,
    )
    target = Path(output_root)
    if target.exists() or target.is_symlink():
        raise OpenDartCollectionError("Open DART pending-corpus output already exists")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as error:
        raise OpenDartCollectionError("Open DART pending-corpus parent is unavailable") from error
    if not parent.is_dir() or parent.is_symlink():
        raise OpenDartCollectionError("Open DART pending-corpus parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    completed = False
    try:
        files_by_path: dict[str, bytes] = {
            corp_codes.snapshot.archive.relative_path: corp_codes.archive_bytes,
            corp_codes.snapshot.inner_xml.relative_path: corp_codes.inner_xml_bytes,
        }
        for acquisition in disclosure_lists:
            if len(acquisition.snapshot.pages) != len(acquisition.page_bytes):
                raise OpenDartCollectionError("Open DART page bytes differ from page receipts")
            for page, content in zip(
                acquisition.snapshot.pages, acquisition.page_bytes, strict=True
            ):
                files_by_path[page.response_file.relative_path] = content
        for acquisition in documents:
            snapshot = acquisition.snapshot
            if len(snapshot.members) != len(acquisition.member_bytes):
                raise OpenDartCollectionError("Open DART member bytes differ from member receipts")
            if len(snapshot.chunks) != len(acquisition.chunk_bytes):
                raise OpenDartCollectionError("Open DART chunk bytes differ from chunk receipts")
            files_by_path[snapshot.archive.relative_path] = acquisition.archive_bytes
            for member, content in zip(snapshot.members, acquisition.member_bytes, strict=True):
                files_by_path[member.raw_file.relative_path] = content
            files_by_path[snapshot.normalized_snapshot.relative_path] = (
                acquisition.normalized_snapshot_bytes
            )
            for chunk, content in zip(snapshot.chunks, acquisition.chunk_bytes, strict=True):
                files_by_path[chunk.text_file.relative_path] = content
        references = open_dart_file_references(manifest)
        if len(files_by_path) != len(references):
            raise OpenDartCollectionError("Open DART output paths collide")
        for reference in references:
            content = files_by_path.get(reference.relative_path)
            if (
                content is None
                or len(content) != reference.size_bytes
                or _sha256(content) != reference.sha256
            ):
                raise OpenDartCollectionError("Open DART output bytes differ from the manifest")
            _write_file(staging / reference.relative_path, content)
        manifest_bytes = open_dart_manifest_bytes(manifest)
        _write_file(staging / "open-dart-pending-manifest.json", manifest_bytes)
        os.replace(staging, target)
        completed = True
        return MaterializedOpenDartPendingCorpus(
            manifest=manifest,
            manifest_sha256=_sha256(manifest_bytes),
        )
    finally:
        if not completed:
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "CollectedOpenDartCorpCodes",
    "CollectedOpenDartDisclosureList",
    "CollectedOpenDartOriginalDocument",
    "MaterializedOpenDartPendingCorpus",
    "OPEN_DART_EVIDENCE_CUTOFF",
    "OpenDartCollectionError",
    "OpenDartCorporationCode",
    "OpenDartCorpCodeSnapshot",
    "OpenDartDisclosure",
    "OpenDartDisclosureListQuery",
    "OpenDartDisclosureListSnapshot",
    "OpenDartEntityBinding",
    "OpenDartEntityScopeReference",
    "OpenDartEntitySeed",
    "OpenDartEntitySelectionReport",
    "OpenDartFilingWhitelist",
    "OpenDartFilingWhitelistRule",
    "OpenDartHttpResponse",
    "OpenDartOriginalDocumentSnapshot",
    "OpenDartOriginalDocumentUnavailable",
    "OpenDartPendingCorpusManifest",
    "OpenDartSelectedDisclosure",
    "OpenDartTransport",
    "build_open_dart_pending_manifest",
    "collect_open_dart_corp_codes",
    "collect_open_dart_disclosure_list",
    "collect_open_dart_original_document",
    "load_open_dart_filing_whitelist",
    "materialize_open_dart_pending_corpus",
    "open_dart_manifest_bytes",
    "open_dart_file_references",
    "select_open_dart_disclosures",
    "select_open_dart_entities",
]
