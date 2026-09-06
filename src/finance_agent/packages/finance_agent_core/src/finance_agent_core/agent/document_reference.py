from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

DOMESTIC_ETP_BRANDS = (
    "TIGER",
    "KODEX",
    "ACE",
    "RISE",
    "SOL",
    "HANARO",
    "TIMEFOLIO",
    "ARIRANG",
    "KBSTAR",
)

_KOREAN_PERIOD = (
    r"20\d{2}\s*년\s*(?:"
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|"
    r"(?:제?\s*[1-4]\s*분기|Q[1-4])|"
    r"\d{1,2}\s*월(?:\s*(?:말|말일))?|상반기|하반기)?"
)
_DELIMITED_DAY = r"20\d{2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
_REFERENCE_PERIOD = rf"(?:{_DELIMITED_DAY}|{_KOREAN_PERIOD})"
_DOCUMENT_KIND = (
    r"가이드(?:\s*북)?|(?:월간\s*)?팩트\s*시트|보고서|브로슈어|프레젠테이션|"
    r"공시|자료|문서"
)
_INNER_AS_OF = re.compile(
    rf"(?:구성\s*종목\s*)?기준\s*(?P<period>{_DELIMITED_DAY}|"
    r"20\d{2}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DatedOfficialDocumentReference:
    """Exact user-text reference used as a source hint, never a product ID."""

    start: int
    end: int
    text: str
    publisher_alias_span: str
    period_span: str
    time_evidence_span: str
    document_kind_span: str


@lru_cache(maxsize=1)
def official_document_publisher_aliases() -> tuple[str, ...]:
    """Derive dated-document publishers from the approved Source Registry.

    The date + document-kind grammar below keeps these aliases from becoming
    free-form product names.  Source selection and product-identity masking
    now consume the same approved metadata instead of maintaining two drifting
    brand lists.
    """

    from finance_agent_core.config.source_registry import (
        load_official_source_registry,
    )

    registry = load_official_source_registry()
    return tuple(
        sorted(
            {
                *DOMESTIC_ETP_BRANDS,
                *(entry.publisher for entry in registry.entries),
                *(
                    alias
                    for entry in registry.entries
                    for alias in entry.selection_aliases
                ),
            },
            key=lambda value: (-len(value), value.casefold()),
        )
    )


def extract_dated_official_document_references(
    text: str,
    *,
    publisher_aliases: tuple[str, ...] | None = None,
) -> tuple[DatedOfficialDocumentReference, ...]:
    """Parse bounded publisher/brand + period + document-kind references.

    The caller may pass aliases from Source Registry metadata.  The result has
    no network or DB authority: it merely prevents a dated guide/factsheet from
    being mistaken for a hard product identity and preserves exact offsets for
    later source selection.
    """

    aliases = tuple(
        sorted(
            {
                alias.strip()
                for alias in (
                    official_document_publisher_aliases()
                    if publisher_aliases is None
                    else publisher_aliases
                )
                if alias.strip()
            },
            key=lambda value: (-len(value), value.casefold()),
        )
    )
    if not aliases:
        return ()
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    patterns = (
        re.compile(
            rf"(?<![0-9A-Za-z가-힣])(?P<publisher>{alias_pattern})"
            rf"(?![0-9A-Za-z가-힣])\s+(?P<period>{_REFERENCE_PERIOD})"
            rf"(?:\s*(?:자|기준))?\s*[^/;；\n()]{{0,60}}?"
            rf"(?P<kind>{_DOCUMENT_KIND})"
            rf"(?P<scope>\s*\([^()\n]{{1,100}}\))?",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?<![0-9A-Za-z가-힣])(?P<period>{_REFERENCE_PERIOD})"
            rf"(?:\s*(?:자|기준))?\s+(?P<publisher>{alias_pattern})"
            rf"(?![0-9A-Za-z가-힣])\s*[^/;；\n()]{{0,60}}?"
            rf"(?P<kind>{_DOCUMENT_KIND})"
            rf"(?P<scope>\s*\([^()\n]{{1,100}}\))?",
            re.IGNORECASE,
        ),
    )
    matches = sorted(
        (match for pattern in patterns for match in pattern.finditer(text)),
        key=lambda match: (match.start(), -(match.end() - match.start())),
    )
    references: list[DatedOfficialDocumentReference] = []
    for match in matches:
        if any(match.start() < item.end and item.start < match.end() for item in references):
            continue
        scope = match.group("scope") or ""
        inner_as_of = _INNER_AS_OF.search(scope)
        time_span = (
            inner_as_of.group("period").strip()
            if inner_as_of is not None
            else match.group("period").strip()
        )
        references.append(
            DatedOfficialDocumentReference(
                start=match.start(),
                end=match.end(),
                text=match.group(0),
                publisher_alias_span=match.group("publisher"),
                period_span=match.group("period").strip(),
                time_evidence_span=time_span,
                document_kind_span=match.group("kind"),
            )
        )
    return tuple(references)


def dated_document_time_bounds(
    reference: DatedOfficialDocumentReference,
    *,
    cutoff: date,
) -> tuple[date | None, date]:
    """Normalize only the date literally owned by one document reference."""

    span = reference.time_evidence_span
    day_match = re.search(
        r"(?P<year>20\d{2})\s*(?:년\s*|[-/.])\s*"
        r"(?P<month>\d{1,2})\s*(?:월\s*|[-/.])\s*"
        r"(?P<day>\d{1,2})\s*일?",
        span,
    )
    if day_match is not None:
        try:
            selected = date(
                int(day_match.group("year")),
                int(day_match.group("month")),
                int(day_match.group("day")),
            )
        except ValueError as error:
            raise ValueError("document reference date is invalid") from error
        if selected > cutoff:
            raise ValueError("document reference date exceeds the evidence cutoff")
        return selected, selected

    year_match = re.search(r"(?P<year>20\d{2})\s*년", span)
    if year_match is None:
        raise ValueError("document reference lacks a supported date")
    year = int(year_match.group("year"))
    quarter_match = re.search(
        r"(?:제?\s*(?P<number>[1-4])\s*분기|Q(?P<q>[1-4]))",
        span,
        re.IGNORECASE,
    )
    if quarter_match is not None:
        quarter = int(quarter_match.group("number") or quarter_match.group("q"))
        start_month = (quarter - 1) * 3 + 1
        start = date(year, start_month, 1)
        end_month = start_month + 2
        end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    elif "상반기" in span:
        start, end = date(year, 1, 1), date(year, 6, 30)
    elif "하반기" in span:
        start, end = date(year, 7, 1), date(year, 12, 31)
    elif month_match := re.search(r"(?P<month>\d{1,2})\s*월", span):
        month = int(month_match.group("month"))
        if not 1 <= month <= 12:
            raise ValueError("document reference month is invalid")
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        if re.search(r"월\s*(?:말|말일)", span):
            start = end = month_end
        else:
            start, end = date(year, month, 1), month_end
    else:
        start, end = date(year, 1, 1), date(year, 12, 31)
    if start > cutoff:
        raise ValueError("document reference period starts after the evidence cutoff")
    return start, min(end, cutoff)


def overlaps_dated_document_reference(
    start: int,
    end: int,
    references: tuple[DatedOfficialDocumentReference, ...],
) -> bool:
    return any(start < reference.end and reference.start < end for reference in references)


__all__ = [
    "DOMESTIC_ETP_BRANDS",
    "DatedOfficialDocumentReference",
    "dated_document_time_bounds",
    "extract_dated_official_document_references",
    "official_document_publisher_aliases",
    "overlaps_dated_document_reference",
]
