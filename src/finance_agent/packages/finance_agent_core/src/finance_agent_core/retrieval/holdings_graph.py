"""Prebuilt official holdings graph (구성종목 Graph) for the external knowledge path.

Live official search answers ``product -> facts`` well but cannot answer
``company -> products that hold it``: site search ignores the company name and
the request budget cannot open every provided product page.  This module builds
that inverse index ahead of time from the same kind of approved official
resources the live path fetches per request:

* a product page proves the publisher's product identifier -> provided-DB
  product identity binding (the live KODEX path does the same join);
* the publisher's holdings resource (KODEX historical holdings JSON, TIGER
  constituents table) proves each holding and its as-of date.

Every relation keeps both excerpts with their content hashes, and the sealed
bundle is re-verified by ``TemporalRelationStore`` against the source registry,
so a graph relation has no more authority than a live one.  Snapshots are read
from bytes saved by an operator, never fetched here.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from finance_agent_core.agent.external_search_planning import (
    ProvidedProductUniverseBuilder,
    intersect_evidence_with_provided_products,
    normalize_product_identity,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.external_knowledge_release import (
    _write_new_read_only_files,
    live_corpus_manifest_sha256,
)
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import (
    OfficialLiveEvidenceError,
    VerifiedLiveRelationExtractor,
    _entity_id,
    _product_entity,
)
from finance_agent_core.retrieval.sqlite_fts import chunk_document
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
    VerifiedTemporalRelationBundle,
    seal_temporal_relation_bundle,
    temporal_relation_bundle_bytes,
)

HoldingsPublisher = Literal["kodex", "tiger"]

KODEX_PAGE_URL = "https://{host}/etf/product/view.do?id={page_id}"
KODEX_HOLDINGS_URL = (
    "https://www.samsungfund.com/api/v1/kodex/product-pdf/{page_id}.do?gijunYMD={as_of:%Y.%m.%d}"
)
TIGER_PAGE_URL = (
    "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do?ksdFund={isin}"
)
TIGER_HOLDINGS_URL = (
    "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/pdfListAjax.ajax?{query}"
)
_PAGE_ID = re.compile(r"^[0-9A-Za-z_-]{2,40}$")
# Deposits and settlement cash are portfolio lines, not companies a question can ask about.
_NON_SECURITY_ROW = re.compile(r"예금|현금|설정현금|\bCASH\b", re.IGNORECASE)
_DEPOSIT_ITEM_PREFIX = "KRD"
_CORPORATE_SUFFIXES = (
    "주식회사",
    "(주)",
    "㈜",
    "Inc.",
    "Inc",
    "Ltd.",
    "Ltd",
    "Limited",
    "Co.",
    "Co",
    "Corp.",
    "Corp",
    "Corporation",
    "Company",
    "Technologies",
    "Technology",
    "Holdings",
    "Group",
    "PLC",
    "ADR",
    "Class A",
    "Class B",
    "Class C",
    "-A",
    "-B",
    "ORD",
)
_TABLE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TABLE_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HTML_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.IGNORECASE)


class HoldingsGraphError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SavedHoldingsProduct:
    """Operator-saved bytes of one product page and its holdings resource."""

    publisher: HoldingsPublisher
    page_id: str
    page_url: str
    page_content: bytes
    holdings_url: str
    holdings_content: bytes


@dataclass(frozen=True, slots=True)
class HoldingRow:
    name: str
    locator: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class ParsedHoldings:
    as_of: date | None
    rows: tuple[HoldingRow, ...]
    skipped_rows: int


@dataclass(frozen=True, slots=True)
class HoldingsGraphBuildReport:
    relations: tuple[TemporalRelationRecord, ...]
    bound_page_ids: tuple[str, ...]
    unbound_page_ids: tuple[str, ...]
    skipped_rows: int


def _parse_gijun_ymd(value: object) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _is_security_name(name: str, item_no: object = None) -> bool:
    if not name or _NON_SECURITY_ROW.search(name):
        return False
    return not (isinstance(item_no, str) and item_no.startswith(_DEPOSIT_ITEM_PREFIX))


def _parse_kodex_holdings(item: SavedHoldingsProduct) -> ParsedHoldings:
    """KODEX ``product-pdf`` JSON: ``pdf.gijunYMD`` dates every ``pdf.list`` row."""

    try:
        payload = json.loads(item.holdings_content)
        raw = item.holdings_content.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HoldingsGraphError(f"{item.page_id}: holdings payload is not JSON") from error
    pdf = payload.get("pdf") if isinstance(payload, dict) else None
    rows = pdf.get("list") if isinstance(pdf, dict) else None
    as_of = _parse_gijun_ymd(pdf.get("gijunYMD")) if isinstance(pdf, dict) else None
    if not isinstance(rows, list):
        return ParsedHoldings(as_of=as_of, rows=(), skipped_rows=0)
    parsed: list[HoldingRow] = []
    skipped = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("secNm"), str):
            skipped += 1
            continue
        name = row["secNm"].strip()
        if not _is_security_name(name, row.get("itmNo")):
            skipped += 1
            continue
        serialized = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        excerpt = serialized if serialized in raw else None
        if excerpt is None:
            position = raw.find(json.dumps(name, ensure_ascii=False))
            left = raw.rfind("{", 0, position) if position >= 0 else -1
            right = raw.find("}", position) if position >= 0 else -1
            excerpt = raw[left : right + 1] if left >= 0 and right >= 0 else None
        if excerpt is None:
            skipped += 1
            continue
        parsed.append(HoldingRow(name=name, locator=f"pdf.list[{index}]", excerpt=excerpt))
    return ParsedHoldings(as_of=as_of, rows=tuple(parsed), skipped_rows=skipped)


def _cell_text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def _parse_tiger_holdings(item: SavedHoldingsProduct) -> ParsedHoldings:
    """TIGER ``pdfListAjax`` table: one ``<tr>`` per holding, dated by ``fixDate``.

    The table body carries no date, so the relation is dated by the ``fixDate``
    the operator requested in the URL; the live connector dates undated bodies
    from the request the same way.
    """

    query = parse_qs(urlsplit(item.holdings_url).query)
    fix_dates = query.get("fixDate", [])
    as_of = None
    if len(fix_dates) == 1:
        try:
            as_of = datetime.strptime(fix_dates[0], "%Y.%m.%d").date()
        except ValueError:
            as_of = None
    body = item.holdings_content.decode("utf-8", errors="replace")
    parsed: list[HoldingRow] = []
    skipped = 0
    for index, row in enumerate(_TABLE_ROW.findall(body)):
        cells = [_cell_text(cell) for cell in _TABLE_CELL.findall(row)]
        if len(cells) < 2:
            skipped += 1
            continue
        name = cells[1]
        if not _is_security_name(name):
            skipped += 1
            continue
        parsed.append(
            HoldingRow(name=name, locator=f"table row {index}", excerpt=" | ".join(cells))
        )
    return ParsedHoldings(as_of=as_of, rows=tuple(parsed), skipped_rows=skipped)


_PARSERS: dict[str, Callable[[SavedHoldingsProduct], ParsedHoldings]] = {
    "kodex": _parse_kodex_holdings,
    "tiger": _parse_tiger_holdings,
}


def company_aliases(name: str) -> tuple[str, ...]:
    """Lookup labels for a holding name; evidence stays the official excerpt.

    The relation store matches whole labels, while questions name a company
    without its corporate suffix (``캠브리콘`` for ``Cambricon Technologies``).
    Only suffix-stripped forms and the leading word are added, never a new name.
    """

    variants: set[str] = set()
    stripped = name.strip()
    changed = True
    while changed:
        changed = False
        for suffix in _CORPORATE_SUFFIXES:
            if stripped.casefold().endswith(suffix.casefold()) and len(stripped) > len(suffix):
                stripped = stripped[: -len(suffix)].rstrip(" ,.-")
                changed = True
                # Keep every intermediate form: ``Cambricon Technologies`` is as
                # likely a question surface as the fully stripped ``Cambricon``.
                if len(stripped) >= 2:
                    variants.add(stripped)
    leading = re.split(r"[\s,]+", stripped, maxsplit=1)[0] if stripped else ""
    if len(leading) >= 2 and not leading.isdigit():
        variants.add(leading)
    variants.discard(name.strip())
    return tuple(sorted(variants))


def _narrow(
    universe: ProvidedProductCandidateUniverse, product: ProvidedProductCandidate
) -> ProvidedProductCandidateUniverse:
    return ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s=universe.database_sha256s,
        candidate_filter_sha256=universe.candidate_filter_sha256,
        explicit_identity_bound=True,
    )


def _tighten_excerpt(
    excerpt: str,
    product: ProvidedProductCandidate,
    universe: ProvidedProductCandidateUniverse,
) -> str:
    """Shrink the page excerpt to the identity window while it still binds the product."""

    narrowed = _narrow(universe, product)
    folded = excerpt.casefold()
    for surface in sorted((product.product_name, *product.aliases), key=len, reverse=True):
        position = folded.find(surface.casefold())
        if position < 0:
            continue
        window = excerpt[max(0, position - 160) : position + len(surface) + 160].strip()
        if intersect_evidence_with_provided_products(narrowed, (window,)) == (product,):
            return window
    return excerpt


def _header_product(
    candidates: tuple[ProvidedProductCandidate, ...],
    header: str,
) -> tuple[ProvidedProductCandidate, ...]:
    """Drop header candidates matched only through a substring of another match.

    ``TIGER 미국배당다우존스타겟데일리커버드콜`` also contains the sibling short name
    ``TIGER 미국배당다우존스``; the publisher's title names the longer product, so the
    contained match is not an ambiguity.  Two distinct full names stay ambiguous.
    """

    if len(candidates) < 2:
        return candidates
    # Publisher titles space names differently from the provided DB
    # (``TIGER 200 커버드콜OTM`` vs ``TIGER 200커버드콜OTM``), so compare identities
    # the way the DB intersection does: NFKC, casefolded, alphanumerics only.
    folded = normalize_product_identity(header)
    best_surface: dict[str, str] = {}
    for candidate in candidates:
        surfaces = [
            normalized
            for surface in (candidate.product_name, *candidate.aliases)
            if (normalized := normalize_product_identity(surface)) and normalized in folded
        ]
        best_surface[candidate.product_id] = max(surfaces, key=len, default="")
    kept = tuple(
        candidate
        for candidate in candidates
        if not any(
            other != candidate.product_id
            and best_surface[candidate.product_id]
            and best_surface[candidate.product_id] != surface
            and best_surface[candidate.product_id] in surface
            for other, surface in best_surface.items()
        )
    )
    return kept or candidates


def _bind_page(
    extractor: VerifiedLiveRelationExtractor,
    snapshot: OfficialSourceSnapshot,
    universe: ProvidedProductCandidateUniverse,
) -> tuple[ProvidedProductCandidate, str, int] | None:
    """Bind one official product page to exactly one provided product.

    The page title and URL are the publisher's own statement of which product
    the page is about, so they decide first (a TIGER URL carries the ISIN).  The
    body decides only when it names exactly one provided product; a page that
    mentions sibling products without such a header binds nothing.
    """

    try:
        text = extractor._snapshot_text(snapshot)
    except OfficialLiveEvidenceError:
        return None
    html_title = _HTML_TITLE.search(snapshot.content.decode("utf-8", errors="replace"))
    header = " ".join(
        part
        for part in (_cell_text(html_title.group(1)) if html_title else "", snapshot.source_uri)
        if part
    )
    candidates = _header_product(
        intersect_evidence_with_provided_products(universe, (header,)), header
    )
    if len(candidates) != 1:
        candidates = intersect_evidence_with_provided_products(universe, (text,))
    if len(candidates) != 1:
        return None
    product = candidates[0]
    narrowed = _narrow(universe, product)
    for ordinal, chunk in enumerate(chunk_document(text, max_chars=1200, overlap_chars=200)):
        if intersect_evidence_with_provided_products(narrowed, (chunk,)) == (product,):
            return product, _tighten_excerpt(chunk, product, universe), ordinal
    if intersect_evidence_with_provided_products(narrowed, (header,)) == (product,):
        return product, header, 0
    return None


def build_holdings_relations(
    products: Sequence[SavedHoldingsProduct],
    *,
    universe: ProvidedProductCandidateUniverse,
    registry: OfficialSourceRegistry | None = None,
) -> HoldingsGraphBuildReport:
    """Turn saved page/holdings pairs into verified holdings relations.

    A page that binds to no provided product, or to more than one, produces no
    relation: the graph never guesses a product identity.  Both resources must
    be approved for ``portfolio_holdings`` and belong to the same publisher.
    """

    active_registry = registry or load_official_source_registry()
    extractor = VerifiedLiveRelationExtractor()
    capability = KnowledgeCapability.PORTFOLIO_HOLDINGS
    relations: dict[str, TemporalRelationRecord] = {}
    bound: list[str] = []
    unbound: list[str] = []
    skipped = 0
    for item in products:
        page_source = active_registry.authorize_url(item.page_url, capability)
        holdings_source = active_registry.authorize_url(item.holdings_url, capability)
        if page_source.publisher != holdings_source.publisher:
            raise HoldingsGraphError(f"{item.page_id}: page and holdings publishers differ")
        parsed = _PARSERS[item.publisher](item)
        skipped += parsed.skipped_rows
        if parsed.as_of is None or not parsed.rows:
            unbound.append(item.page_id)
            continue
        if parsed.as_of > active_registry.evidence_cutoff:
            raise HoldingsGraphError(
                f"{item.page_id}: holdings are dated after the evidence cutoff"
            )
        page_snapshot = OfficialSourceSnapshot(
            source=page_source,
            capability=capability,
            source_uri=item.page_url,
            title=f"{page_source.publisher} product page {item.page_id}",
            content_type="text/html",
            content=item.page_content,
            content_sha256=hashlib.sha256(item.page_content).hexdigest(),
            published_at=None,
            # Undated official pages are dated at the cutoff, as the live connector does.
            as_of=active_registry.evidence_cutoff,
        )
        holdings_snapshot = OfficialSourceSnapshot(
            source=holdings_source,
            capability=capability,
            source_uri=item.holdings_url,
            title=f"{holdings_source.publisher} official full holdings {item.page_id}",
            content_type="application/json" if item.publisher == "kodex" else "text/html",
            content=item.holdings_content,
            content_sha256=hashlib.sha256(item.holdings_content).hexdigest(),
            published_at=None,
            as_of=parsed.as_of,
        )
        binding = _bind_page(extractor, page_snapshot, universe)
        if binding is None:
            unbound.append(item.page_id)
            continue
        product, page_excerpt, page_ordinal = binding
        bound.append(item.page_id)
        product_entity = _product_entity(product)
        page_evidence_id = _entity_id(
            "evidence", f"{page_snapshot.content_sha256}:{page_ordinal}:{product.product_id}"
        )
        for row in parsed.rows:
            holdings_evidence_id = _entity_id(
                "evidence", f"{holdings_snapshot.content_sha256}:{row.locator}:{row.name}"
            )
            relation_id = _entity_id(
                "relation", f"holdings-graph:{product.product_id}:{holdings_evidence_id}"
            )
            evidence = tuple(
                sorted(
                    (
                        ExternalEvidenceReference(
                            evidence_id=page_evidence_id,
                            source_registry_id=page_source.source_id,
                            document_id=_entity_id("doc", page_snapshot.source_uri),
                            title=page_snapshot.title,
                            publisher=page_source.publisher,
                            source_uri=page_snapshot.source_uri,
                            published_at=None,
                            as_of=page_snapshot.as_of,
                            content_sha256=page_snapshot.content_sha256,
                            locator=f"extracted chunk {page_ordinal}",
                            excerpt=page_excerpt,
                        ),
                        ExternalEvidenceReference(
                            evidence_id=holdings_evidence_id,
                            source_registry_id=holdings_source.source_id,
                            document_id=_entity_id("doc", holdings_snapshot.source_uri),
                            title=holdings_snapshot.title,
                            publisher=holdings_source.publisher,
                            source_uri=holdings_snapshot.source_uri,
                            published_at=None,
                            as_of=holdings_snapshot.as_of,
                            content_sha256=holdings_snapshot.content_sha256,
                            locator=row.locator,
                            excerpt=row.excerpt,
                        ),
                    ),
                    key=lambda reference: reference.evidence_id,
                )
            )
            relations[relation_id] = TemporalRelationRecord(
                relation_id=relation_id,
                capability=capability,
                predicate=KnowledgePredicate.HOLDS_COMPANY,
                subject=product_entity,
                object=TemporalEntity(
                    entity_id=_entity_id("company", row.name),
                    kind=TemporalEntityKind.COMPANY,
                    canonical_name=row.name,
                    aliases=company_aliases(row.name),
                ),
                product_family=product.product_family,
                product_id=product.product_id,
                valid_from=parsed.as_of,
                valid_to=parsed.as_of,
                observed_at=parsed.as_of,
                evidence=evidence,
            )
    return HoldingsGraphBuildReport(
        relations=tuple(relations[key] for key in sorted(relations)),
        bound_page_ids=tuple(bound),
        unbound_page_ids=tuple(unbound),
        skipped_rows=skipped,
    )


def load_saved_kodex_products(
    raw_dir: str | Path,
    *,
    requested_as_of: date,
    page_host: str = "www.samsungfund.com",
) -> tuple[SavedHoldingsProduct, ...]:
    """Pair ``<page id>.pdf.json`` with ``<page id>.page.html`` under ``raw_dir``.

    ``requested_as_of`` is the ``gijunYMD`` the operator asked the endpoint
    for; the relation itself is dated by the ``gijunYMD`` the payload reports.
    ``page_host`` names the approved KODEX page host the HTML was saved from.
    """

    root = Path(raw_dir)
    products = []
    for api_path in sorted(root.glob("*.pdf.json")):
        page_id = api_path.name[: -len(".pdf.json")]
        if _PAGE_ID.fullmatch(page_id) is None:
            raise HoldingsGraphError(f"unexpected KODEX page id file: {api_path.name}")
        page_path = root / f"{page_id}.page.html"
        if not page_path.is_file():
            continue
        products.append(
            SavedHoldingsProduct(
                publisher="kodex",
                page_id=page_id,
                page_url=KODEX_PAGE_URL.format(host=page_host, page_id=page_id),
                page_content=page_path.read_bytes(),
                holdings_url=KODEX_HOLDINGS_URL.format(page_id=page_id, as_of=requested_as_of),
                holdings_content=api_path.read_bytes(),
            )
        )
    return tuple(products)


def tiger_holdings_url(isin: str, *, fix_date: date) -> str:
    query = urlencode(
        {
            "ksdFund": isin,
            "pageIndex": "1",
            "firstIndex": "0",
            "listCnt": "1000",
            "fixDate": f"{fix_date:%Y.%m.%d}",
        }
    )
    return TIGER_HOLDINGS_URL.format(query=query)


def load_saved_tiger_products(
    raw_dir: str | Path,
    *,
    fix_date: date,
) -> tuple[SavedHoldingsProduct, ...]:
    """Pair ``<ISIN>.pdf.html`` with ``<ISIN>.page.html`` saved for ``fix_date``."""

    root = Path(raw_dir)
    products = []
    for table_path in sorted(root.glob("*.pdf.html")):
        isin = table_path.name[: -len(".pdf.html")]
        if re.fullmatch(r"KR7[0-9A-Z]{9}", isin) is None:
            raise HoldingsGraphError(f"unexpected TIGER ISIN file: {table_path.name}")
        page_path = root / f"{isin}.page.html"
        if not page_path.is_file():
            continue
        products.append(
            SavedHoldingsProduct(
                publisher="tiger",
                page_id=isin,
                page_url=TIGER_PAGE_URL.format(isin=isin),
                page_content=page_path.read_bytes(),
                holdings_url=tiger_holdings_url(isin, fix_date=fix_date),
                holdings_content=table_path.read_bytes(),
            )
        )
    return tuple(products)


def seal_holdings_graph(
    relations: Sequence[TemporalRelationRecord],
    *,
    registry: OfficialSourceRegistry | None = None,
) -> VerifiedTemporalRelationBundle:
    """Seal graph relations as the relation cache of a live-mode release."""

    if not relations:
        raise HoldingsGraphError("a holdings graph needs at least one relation")
    return seal_temporal_relation_bundle(
        tuple(relations),
        corpus_manifest_sha256=live_corpus_manifest_sha256(),
        registry=registry,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the sealed holdings graph from operator-saved official snapshots."
    )
    parser.add_argument("--kodex-raw-dir", type=Path)
    parser.add_argument("--kodex-requested-as-of", type=date.fromisoformat)
    parser.add_argument("--kodex-page-host", default="www.samsungfund.com")
    parser.add_argument("--tiger-raw-dir", type=Path)
    parser.add_argument("--tiger-fix-date", type=date.fromisoformat)
    parser.add_argument(
        "--database-dir",
        type=Path,
        required=True,
        help="directory holding the approved bond/domestic_etp/overseas_etp/fund SQLite files",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if (arguments.kodex_raw_dir is None) != (arguments.kodex_requested_as_of is None):
        parser.error("--kodex-raw-dir and --kodex-requested-as-of go together")
    if (arguments.tiger_raw_dir is None) != (arguments.tiger_fix_date is None):
        parser.error("--tiger-raw-dir and --tiger-fix-date go together")
    if arguments.kodex_raw_dir is None and arguments.tiger_raw_dir is None:
        parser.error("at least one raw directory is required")

    registry = load_official_source_registry()
    universe = ProvidedProductUniverseBuilder(
        {family: arguments.database_dir / f"{family.value}.sqlite3" for family in ProductFamily}
    ).build(families=(ProductFamily.DOMESTIC_ETP,))
    products: list[SavedHoldingsProduct] = []
    if arguments.kodex_raw_dir is not None:
        products.extend(
            load_saved_kodex_products(
                arguments.kodex_raw_dir,
                requested_as_of=arguments.kodex_requested_as_of,
                page_host=arguments.kodex_page_host,
            )
        )
    if arguments.tiger_raw_dir is not None:
        products.extend(
            load_saved_tiger_products(arguments.tiger_raw_dir, fix_date=arguments.tiger_fix_date)
        )
    report = build_holdings_relations(products, universe=universe, registry=registry)
    bundle = seal_holdings_graph(report.relations, registry=registry)
    payload = temporal_relation_bundle_bytes(bundle)
    _write_new_read_only_files(((arguments.output.resolve(), payload),))
    print(f"saved_products={len(products)}")
    print(f"bound_products={len(report.bound_page_ids)}")
    print(f"unbound_page_ids={','.join(report.unbound_page_ids) or '-'}")
    print(f"skipped_rows={report.skipped_rows}")
    print(f"relation_count={bundle.manifest.relation_count}")
    print(f"relation_set_sha256={bundle.manifest.relation_set_sha256}")
    print(f"relation_graph_file_sha256={hashlib.sha256(payload).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
