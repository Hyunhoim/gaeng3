from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from html.parser import HTMLParser

from pypdf import PdfReader

from finance_agent_core.agent.external_search_planning import (
    intersect_evidence_with_provided_products,
    normalize_product_identity,
    normalized_product_surface_core,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.sqlite_fts import chunk_document, normalize_document_text
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
)

_MAX_PDF_PAGES = 250
_MAX_EXTRACTED_CHARS = 2_000_000
_RELATION_SIGNAL = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"편입|보유|구성\s*종목|holding", re.IGNORECASE
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|가족사|지주회사|분할\s*설립|family|subsidiar", re.IGNORECASE
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"테마|섹터|산업|우주|항공|theme|sector", re.IGNORECASE
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"구조|전략|운용|판매|일정|structure|strategy", re.IGNORECASE
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험|리스크|손실|변동|risk", re.IGNORECASE
    ),
}
_SUBSIDIARY_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)인\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80})",
    re.IGNORECASE,
)


def _parent_only_subsidiaries(parent: str, excerpt: str) -> tuple[str, ...]:
    """Extract exact child spans from a parent-authorized official excerpt.

    This is deliberately conservative: it accepts either an explicit
    parent/relation/child phrase or a company token sharing the parent's
    official name prefix inside a relation-bearing excerpt. It never generates
    a name that is absent from the fetched source.
    """

    candidates: set[str] = set()
    escaped = re.escape(parent)
    explicit_patterns = (
        re.compile(
            rf"{escaped}\s*(?:의\s*)?(?:주요\s*)?(?:자회사|계열사|가족사)(?:인|로|는)?\s*"
            r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80})",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80})\s*(?:은|는|이|가)?\s*"
            rf"{escaped}\s*(?:의\s*)?(?:자회사|계열사|가족사)",
            re.IGNORECASE,
        ),
    )
    for pattern in explicit_patterns:
        candidates.update(match.group("child") for match in pattern.finditer(excerpt))
    prefix_pattern = re.compile(rf"(?<![0-9A-Za-z가-힣]){escaped}[0-9A-Za-z가-힣&·+._/-]{{1,40}}")
    candidates.update(match.group(0) for match in prefix_pattern.finditer(excerpt))

    cleaned = set()
    for candidate in candidates:
        value = candidate.strip(" ,.;:()[]{}‘’'\"")
        for suffix in ("입니다", "이었다", "이라는", "이며", "에서", "으로", "이다"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
                break
        if len(value) >= 3 and value[-1:] in {"은", "는", "이", "가", "을", "를", "와", "과"}:
            value = value[:-1]
        if value and _normalized(value) != _normalized(parent) and value in excerpt:
            cleaned.add(value)
    return tuple(sorted(cleaned, key=str.casefold))[:20]


class OfficialLiveEvidenceError(RuntimeError):
    """Raised when fetched official bytes cannot become exact-span evidence."""


class _VisibleText(HTMLParser):
    ignored = frozenset({"script", "style", "noscript", "svg"})
    blocks = frozenset(
        {
            "article",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "li",
            "p",
            "section",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in self.ignored:
            self.depth += 1
        elif not self.depth and tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.ignored and self.depth:
            self.depth -= 1
        elif not self.depth and tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def extract_official_snapshot_text(snapshot: OfficialSourceSnapshot) -> str:
    if hashlib.sha256(snapshot.content).hexdigest() != snapshot.content_sha256:
        raise OfficialLiveEvidenceError("official snapshot content SHA-256 differs")
    media_type = snapshot.content_type.casefold().split(";", maxsplit=1)[0]
    if media_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(snapshot.content), strict=True)
            if len(reader.pages) > _MAX_PDF_PAGES:
                raise OfficialLiveEvidenceError("official PDF exceeds the page budget")
            parts = []
            size = 0
            for page in reader.pages:
                text = page.extract_text(extraction_mode="layout") or ""
                size += len(text)
                if size > _MAX_EXTRACTED_CHARS:
                    raise OfficialLiveEvidenceError("official PDF text exceeds the budget")
                parts.append(text)
            extracted = "\n".join(parts)
        except OfficialLiveEvidenceError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed on malformed PDFs
            raise OfficialLiveEvidenceError("official PDF text extraction failed") from error
    elif media_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText()
        parser.feed(snapshot.content.decode("utf-8", errors="replace"))
        parser.close()
        extracted = "".join(parser.parts)
    elif media_type == "text/plain":
        extracted = snapshot.content.decode("utf-8", errors="strict")
    else:
        raise OfficialLiveEvidenceError("official document media type is unsupported")
    normalized = normalize_document_text(extracted)
    if not normalized:
        raise OfficialLiveEvidenceError("official document has no extractable text")
    return normalized


def _normalized(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _entity_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _product_entity(candidate: ProvidedProductCandidate) -> TemporalEntity:
    return TemporalEntity(
        entity_id=_entity_id("product", f"{candidate.product_family.value}:{candidate.product_id}"),
        kind=TemporalEntityKind.PRODUCT,
        canonical_name=candidate.product_name,
        aliases=candidate.aliases,
    )


class VerifiedLiveRelationExtractor:
    """Conservative exact-span extraction from an approved official source.

    It never infers a product identity: a provided DB name/ticker/ISIN must be
    present in the same bounded excerpt as the subject and relation signal.
    """

    def extract(
        self,
        snapshot: OfficialSourceSnapshot,
        requirement: KnowledgeRequirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[TemporalRelationRecord, ...]:
        if snapshot.source.status != "approved_for_live_evidence":
            raise OfficialLiveEvidenceError("source is not approved for live evidence")
        if snapshot.capability is not requirement.capability:
            raise OfficialLiveEvidenceError("snapshot capability differs from requirement")
        text = extract_official_snapshot_text(snapshot)
        subject_surfaces = tuple(
            dict.fromkeys((requirement.subject, *requirement.search_aliases))
        )
        subjects = tuple(
            value for surface in subject_surfaces if (value := _normalized(surface))
        )
        if requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY:
            expanded_subjects = set(subjects)
            for subject in subjects:
                for suffix in ("테마", "섹터", "산업"):
                    if subject.endswith(suffix) and len(subject) > len(suffix) + 1:
                        expanded_subjects.add(subject[: -len(suffix)])
            subjects = tuple(sorted(expanded_subjects))
        signal = _RELATION_SIGNAL[requirement.capability]
        relations: list[TemporalRelationRecord] = []
        subsidiary_pair = (
            _SUBSIDIARY_PAIR.search(requirement.relation_evidence_span)
            if requirement.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
            else None
        )
        for ordinal, excerpt in enumerate(chunk_document(text, max_chars=1200, overlap_chars=200)):
            normalized_excerpt = _normalized(excerpt)
            if (
                not subjects
                or not any(subject in normalized_excerpt for subject in subjects)
                or signal.search(excerpt) is None
            ):
                continue
            if requirement.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY:
                parent = (
                    subsidiary_pair.group("parent").strip()
                    if subsidiary_pair is not None
                    else requirement.subject
                )
                children = (
                    (subsidiary_pair.group("child").strip(),)
                    if subsidiary_pair is not None
                    else _parent_only_subsidiaries(parent, excerpt)
                )
                for child in children:
                    if (
                        _normalized(parent) not in normalized_excerpt
                        or _normalized(child) not in normalized_excerpt
                    ):
                        continue
                    evidence_id = _entity_id(
                        "evidence",
                        f"{snapshot.content_sha256}:{ordinal}:{parent}:{child}",
                    )
                    relations.append(
                        TemporalRelationRecord(
                            relation_id=_entity_id(
                                "relation",
                                f"{requirement.requirement_id}:{evidence_id}",
                            ),
                            capability=requirement.capability,
                            predicate=requirement.predicate,
                            subject=TemporalEntity(
                                entity_id=_entity_id("company", parent),
                                kind=TemporalEntityKind.COMPANY,
                                canonical_name=parent,
                            ),
                            object=TemporalEntity(
                                entity_id=_entity_id("company", child),
                                kind=TemporalEntityKind.COMPANY,
                                canonical_name=child,
                            ),
                            valid_from=snapshot.published_at or snapshot.as_of,
                            valid_to=snapshot.published_at or snapshot.as_of,
                            observed_at=snapshot.as_of,
                            evidence=(
                                ExternalEvidenceReference(
                                    evidence_id=evidence_id,
                                    source_registry_id=snapshot.source.source_id,
                                    document_id=_entity_id("doc", snapshot.source_uri),
                                    title=snapshot.title,
                                    publisher=snapshot.source.publisher,
                                    source_uri=snapshot.source_uri,
                                    published_at=snapshot.published_at,
                                    as_of=snapshot.as_of,
                                    content_sha256=snapshot.content_sha256,
                                    locator=f"extracted chunk {ordinal}",
                                    excerpt=excerpt,
                                ),
                            ),
                        )
                    )
                continue
            products = intersect_evidence_with_provided_products(universe, (excerpt,))
            if (
                not products
                and requirement.capability is KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
                and universe.explicit_identity_bound
            ):
                # The question has already been bound to provided-DB product
                # identities through an explicit, sufficiently long product
                # surface. An approved policy document may describe the named
                # fund programme without repeating every sale-class label.
                # The subject core is rechecked for every candidate here.
                core = normalized_product_surface_core(requirement.subject)
                if core is not None:
                    products = tuple(
                        candidate
                        for candidate in universe.candidates
                        if core in normalize_product_identity(candidate.product_name)
                        or any(
                            core in normalize_product_identity(alias)
                            for alias in candidate.aliases
                        )
                    )
            relation_subject_surfaces = (
                tuple(
                    surface
                    for surface in subject_surfaces
                    if _normalized(surface) in normalized_excerpt
                )
                if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                else (requirement.subject,)
            )
            for product in products:
                for relation_subject_surface in relation_subject_surfaces:
                    subject_kind = (
                        TemporalEntityKind.THEME
                        if requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
                        else TemporalEntityKind.COMPANY
                    )
                    subject_entity = TemporalEntity(
                        entity_id=_entity_id(subject_kind.value, relation_subject_surface),
                        kind=subject_kind,
                        canonical_name=relation_subject_surface,
                    )
                    product_entity = _product_entity(product)
                    if requirement.predicate is KnowledgePredicate.HOLDS_COMPANY:
                        relation_subject, relation_object = product_entity, subject_entity
                    elif requirement.predicate is KnowledgePredicate.THEME_RELATED:
                        relation_subject, relation_object = product_entity, subject_entity
                    elif requirement.predicate is KnowledgePredicate.HAS_RISK_FACTOR:
                        relation_subject = product_entity
                        relation_object = TemporalEntity(
                            entity_id=_entity_id("risk", excerpt),
                            kind=TemporalEntityKind.RISK_CLAIM,
                            canonical_name=excerpt[:500],
                        )
                    elif requirement.predicate is KnowledgePredicate.DESCRIBES_FUND_STRUCTURE_TREND:
                        # The lookup subject is the explicitly evidenced fund
                        # programme name, while product_family/product_id below
                        # retain the immutable join to each provided-DB sale class.
                        relation_subject = TemporalEntity(
                            entity_id=_entity_id("fund", requirement.subject),
                            kind=TemporalEntityKind.FUND,
                            canonical_name=requirement.subject,
                            aliases=requirement.search_aliases,
                        )
                        relation_object = TemporalEntity(
                            entity_id=_entity_id("event", excerpt),
                            kind=TemporalEntityKind.POLICY_EVENT,
                            canonical_name=excerpt[:500],
                        )
                    else:
                        continue
                    evidence_id = _entity_id(
                        "evidence",
                        (
                            f"{snapshot.content_sha256}:{ordinal}:{product.product_id}:"
                            f"{relation_subject_surface}"
                        ),
                    )
                    relations.append(
                        TemporalRelationRecord(
                            relation_id=_entity_id(
                                "relation",
                                f"{requirement.requirement_id}:{evidence_id}",
                            ),
                            capability=requirement.capability,
                            predicate=requirement.predicate,
                            subject=relation_subject,
                            object=relation_object,
                            product_family=product.product_family,
                            product_id=product.product_id,
                            # Relation time comes from the official document, never
                            # from the user's requested interval.
                            valid_from=snapshot.published_at or snapshot.as_of,
                            valid_to=snapshot.published_at or snapshot.as_of,
                            observed_at=snapshot.as_of,
                            evidence=(
                                ExternalEvidenceReference(
                                    evidence_id=evidence_id,
                                    source_registry_id=snapshot.source.source_id,
                                    document_id=_entity_id("doc", snapshot.source_uri),
                                    title=snapshot.title,
                                    publisher=snapshot.source.publisher,
                                    source_uri=snapshot.source_uri,
                                    published_at=snapshot.published_at,
                                    as_of=snapshot.as_of,
                                    content_sha256=snapshot.content_sha256,
                                    locator=f"extracted chunk {ordinal}",
                                    excerpt=excerpt,
                                ),
                            ),
                        )
                    )
        unique = {item.relation_id: item for item in relations}
        return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "OfficialLiveEvidenceError",
    "VerifiedLiveRelationExtractor",
    "extract_official_snapshot_text",
]
