from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from collections import OrderedDict
from datetime import date
from difflib import SequenceMatcher
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from pypdf import PdfReader

from finance_agent_core.agent.external_search_planning import (
    intersect_evidence_with_provided_products,
    normalize_product_identity,
    normalize_source_owner_identity,
    normalized_fund_programme_core,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import SourceAuthority
from finance_agent_core.contracts.external_evidence_semantics import requirement_relation_signal
from finance_agent_core.contracts.external_knowledge import (
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.hangul_romanization import hangul_romanization_key
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.sqlite_fts import (
    chunk_document,
    normalize_document_text,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationDocumentRank,
    TemporalRelationRecord,
    _bounded_top_n_holding_membership,
)

_MAX_PDF_PAGES = 250
_MAX_EXTRACTED_CHARS = 2_000_000
_CONTENT_TYPE_CHARSET = re.compile(
    r"(?:^|;)\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^;\s]+))",
    re.IGNORECASE,
)
_HTML_META_CHARSET = re.compile(
    rb"<meta\b[^>]{0,512}?charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_TEXT_CHARSET_ALLOWLIST = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "cp949": "cp949",
    "ms949": "cp949",
    "windows-949": "cp949",
    "x-windows-949": "cp949",
    "uhc": "cp949",
    "euc-kr": "euc-kr",
    "euckr": "euc-kr",
    "ks-c-5601-1987": "euc-kr",
    "ksc5601": "euc-kr",
}
_RISK_FACTOR_SIGNAL = re.compile(
    r"(?:원금|원본|투자금).{0,16}(?:손실|잃)|"
    r"손실.{0,16}(?:가능|발생|위험|입을\s*수)|"
    r"(?:시장|가격|금리|신용|유동성|환율|통화|산업|섹터|국가|집중|"
    r"파생|레버리지|거래상대방).{0,20}(?:위험|리스크|변동성)|"
    r"(?:위험|리스크).{0,20}(?:요인|요소|가능|발생|노출|존재|있)|"
    r"(?:변동성|추적\s*오차|괴리율).{0,20}(?:위험|확대|발생|높|클\s*수)|"
    r"(?:투자\s*)?위험\s*(?:요인|내용|고지|유의사항)|"
    r"(?:invest(?:ing|ment)|fund|etf).{0,32}(?:involves?|subject\s+to).{0,16}risk|"
    r"(?:possible|potential|risk\s+of).{0,16}loss\s+of\s+(?:principal|capital)|"
    r"risk\s+of\s+capital\s+loss|"
    r"(?:risk\s+factors?|subject\s+to\s+(?:the\s+)?risks?|may\s+lose\s+money)",
    re.IGNORECASE,
)
_RISK_ACCOUNTING_NOISE = re.compile(
    r"(?:회계|재무제표|당기|영업|법인세|평가|처분|매매).{0,24}"
    r"(?:손실|비용|보수|수수료)|"
    r"(?:손실|비용|보수|수수료).{0,24}(?:계상|인식|회계|재무제표)",
    re.IGNORECASE,
)
_RISK_MEASUREMENT_NOISE = re.compile(
    r"위험\s*평가액|위험액|위험\s*측정액|위험\s*등급\s*산정|"
    r"(?:시장|가격|금리|환율)\s*위험.{0,12}(?:헤지|관리|측정)",
    re.IGNORECASE,
)
_RISK_DISTRIBUTION_OR_TAX_NOISE = re.compile(
    r"(?:이익금이\s*없|분배할\s*금액|분배금.{0,12}(?:부족|작은))|"
    r"(?:세금|과세).{0,50}(?:투자\s*결과|손실)",
    re.IGNORECASE,
)
_RISK_FOOTNOTE_NOISE = re.compile(
    r"^\s*주\s*\d+\).{0,80}(?:분류\s*기준|투자\s*비율|위험\s*평가액)",
    re.IGNORECASE,
)
_RISK_RATING_ONLY = re.compile(
    r"^(?:투자\s*)?위험\s*등급(?:은|이|:|：|\s)*[1-6]?(?:등급)?[가-힣\s]*$",
    re.IGNORECASE,
)
_CONTENTS_OR_PAGE_NUMBER = re.compile(
    r"(?:^|\s)(?:목차|contents)(?:\s|$)|\.{3,}\s*\d+\s*$|^\s*\d+\s*$",
    re.IGNORECASE,
)
_RISK_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "principal_loss",
        re.compile(
            r"(?:원금|원본|투자금).{0,20}(?:손실|잃)|최대\s*손실\s*예상액|"
            r"투자자.{0,30}손실.{0,12}(?:입|발생|가능)|"
            r"(?:possible|potential|risk\s+of).{0,16}loss\s+of\s+"
            r"(?:principal|capital)|may\s+lose\s+money",
            re.IGNORECASE,
        ),
        12,
    ),
    (
        "tracking_error",
        re.compile(r"추적\s*오차|괴리율|tracking\s+(?:error|difference)", re.IGNORECASE),
        11,
    ),
    (
        "concentration",
        re.compile(
            r"(?:집중\s*(?:투자)?|특정\s*(?:산업|종목|국가)).{0,20}위험|"
            r"(?:concentrat(?:ed|ion)|non[- ]diversified).{0,32}(?:risk|loss)",
            re.I,
        ),
        10,
    ),
    (
        "liquidity",
        re.compile(
            r"유동성.{0,20}(?:위험|부족)|현금화.{0,20}(?:어렵|불가)|"
            r"처분.{0,20}(?:어렵|불가)|환매.{0,20}(?:지연|중단)|"
            r"(?:liquidity|illiquid).{0,24}(?:risk|loss|unable|difficult)",
            re.IGNORECASE,
        ),
        10,
    ),
    (
        "foreign_exchange",
        re.compile(
            r"(?:환율|통화|환\s*노출).{0,20}(?:위험|변동|손실)|"
            r"(?:currency|foreign\s+exchange).{0,40}(?:risk|fluctuat|loss)|"
            r"(?:risk|fluctuat|loss).{0,96}(?:currency|foreign\s+exchange)",
            re.I,
        ),
        9,
    ),
    (
        "credit",
        re.compile(
            r"(?:신용|부도|채무\s*불이행).{0,20}(?:위험|손실|하락)|"
            r"(?:credit|default).{0,20}(?:risk|loss)",
            re.I,
        ),
        9,
    ),
    (
        "interest_rate",
        re.compile(
            r"금리.{0,20}(?:위험|변동|상승|하락).{0,20}(?:손실|가격)?|"
            r"interest\s+rate.{0,24}(?:risk|rise|fall|change|loss)",
            re.I,
        ),
        9,
    ),
    (
        "volatility",
        re.compile(
            r"(?:가격|시장|수익률).{0,20}(?:변동성|급락|하락|변동).{0,20}"
            r"(?:위험|손실|높|확대)?|변동성.{0,20}(?:위험|높|확대|손실)|"
            r"(?:market|price).{0,24}(?:volatil|fluctuat).{0,24}(?:risk|loss)|"
            r"volatil(?:e|ity).{0,24}(?:risk|loss)",
            re.IGNORECASE,
        ),
        8,
    ),
    (
        "derivative_or_leverage",
        re.compile(
            r"(?:파생\s*상품|레버리지|인버스|차입).{0,30}"
            r"(?:고위험|위험|손실|변동성)|"
            r"(?:derivative|leverage|inverse).{0,30}(?:risk|loss|volatil)",
            re.IGNORECASE,
        ),
        8,
    ),
    (
        "counterparty",
        re.compile(
            r"(?:거래\s*상대방|상대방).{0,20}(?:위험|부도|불이행)|"
            r"counterparty.{0,20}(?:risk|default|fail)",
            re.I,
        ),
        8,
    ),
    (
        "termination_or_listing",
        re.compile(
            r"(?:상장\s*폐지|조기\s*청산|거래\s*정지).{0,20}(?:위험|가능|손실)|"
            r"(?:delist|terminat|liquidat).{0,24}(?:risk|possible|loss)",
            re.I,
        ),
        8,
    ),
    (
        "explicit_risk_factor",
        re.compile(
            r"(?:투자\s*)?(?:위험|리스크)\s*(?:요인|요소|내용|유의사항)|"
            r"risk\s+factors?|subject\s+to\s+(?:the\s+)?risks?",
            re.I,
        ),
        6,
    ),
)
_SUBSIDIARY_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)인\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=$|(?:이|가|을|를|은|는)?\s*(?:편입|보유|담|넣|포함|들어|ETF|ETN|ETP))",
    re.IGNORECASE,
)
_SUBSIDIARY_SELECTION = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*의\s*"
    r"(?:자회사|계열사|가족사)\s*(?:가운데|중(?:에서)?)\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=$|(?:이|가|을|를)?\s*(?:편입|보유|담|넣|포함|들어))",
    re.IGNORECASE,
)


_ASCII_ENTITY_TOKEN = re.compile(r"(?<![A-Za-z])[A-Z][A-Za-z]{3,39}(?![A-Za-z])")
_GENERIC_ASCII_ENTITY_TOKENS = frozenset(
    {
        "company",
        "corporation",
        "fund",
        "holding",
        "holdings",
        "investment",
        "official",
        "portfolio",
        "semiconductor",
        "strategy",
        "technologies",
    }
)


def _official_cross_script_aliases(subject: str, text: str) -> tuple[str, ...]:
    """Find one conservative exact Latin spelling for a Hangul proper name.

    HCLX aliases remain proposals.  If a proposal is misspelled but discovery
    still reaches an approved document, this function may recover a repeated
    source-authored proper-name token whose phonetic key is uniquely close to
    the Hangul subject.  A relation signal and a provided-DB product identity
    must still occur in the same verified excerpt before execution.
    """

    romanized = hangul_romanization_key(subject)
    if len(romanized) < 5:
        return ()
    occurrences: dict[str, int] = {}
    for match in _ASCII_ENTITY_TOKEN.finditer(text):
        token = match.group(0)
        normalized = token.casefold()
        if normalized in _GENERIC_ASCII_ENTITY_TOKENS:
            continue
        occurrences[token] = occurrences.get(token, 0) + 1
    scored = []
    for token, count in occurrences.items():
        if count < 2:
            continue
        folded = token.casefold()
        length_ratio = len(folded) / len(romanized)
        if not 0.55 <= length_ratio <= 1.45:
            continue
        score = SequenceMatcher(None, romanized, folded).ratio()
        if score >= 0.64:
            scored.append((score, token))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    if not scored:
        return ()
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
        return ()
    return (scored[0][1],)


def _bounded_ascii_tail_truncations(surface: str) -> tuple[str, ...]:
    """Recover a narrowly clipped final word in an official PDF table.

    Admit only multi-word Latin aliases, keep at least eight characters of
    the last word, and trim at most three trailing characters.  Exact relation
    and product-evidence checks still have to pass in the same official text.
    """

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&.-]*", surface)
    if len(tokens) < 2 or len(tokens[-1]) < 11:
        return ()
    prefix = " ".join(tokens[:-1])
    return tuple(
        f"{prefix} {tokens[-1][:-trim]}"
        for trim in range(1, 4)
        if len(tokens[-1]) - trim >= 8
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
        # The prefix fallback is intended for a real official company token
        # such as ``ParentMaterials``.  A Korean possessive/subject particle
        # immediately after the parent (``Parent의 자회사``) is grammar, not
        # a child company name; admitting it creates a spurious relation edge.
        if value.startswith(parent) and value[len(parent) :] in {
            "의",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "와",
            "과",
        }:
            continue
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
        tag = tag.casefold()
        if tag in self.ignored:
            self.depth += 1
        elif not self.depth:
            if tag in self.blocks:
                self.parts.append("\n")
            # Official product tables frequently expose an exact product or
            # constituent name through accessible markup while the visible
            # cell contains only an icon.  These attributes are fetched
            # original bytes, not search snippets, and remain subject to the
            # same exact relation/product checks downstream.
            values = dict(attrs)
            for key in ("aria-label", "alt", "title"):
                value = values.get(key)
                if value and len(value) <= 500:
                    self.parts.extend((" ", value, " "))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.ignored and self.depth:
            self.depth -= 1
        elif not self.depth and tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def _decode_official_text(content: bytes, content_type: str) -> str:
    """Strictly decode an allowlisted official text encoding.

    The HTTP Content-Type is authoritative when it declares a charset.  Legacy
    official HTML frequently declares CP949/EUC-KR only in an ASCII ``meta``
    element, so that declaration is accepted only when the HTTP header is
    silent.  No statistical byte guessing is performed.
    """

    matches = list(_CONTENT_TYPE_CHARSET.finditer(content_type))
    if not matches:
        if re.search(r"(?:^|;)\s*charset\s*=", content_type, re.IGNORECASE):
            raise OfficialLiveEvidenceError("official text extraction failed")
        media_type = content_type.casefold().split(";", maxsplit=1)[0].strip()
        meta_matches = (
            _HTML_META_CHARSET.findall(content[:8192])
            if media_type in {"text/html", "application/xhtml+xml"}
            else []
        )
        meta_codecs = {
            _TEXT_CHARSET_ALLOWLIST.get(value.decode("ascii").casefold().replace("_", "-"))
            for value in meta_matches
        }
        if None in meta_codecs or len(meta_codecs) > 1:
            raise OfficialLiveEvidenceError("official text extraction failed")
        codec = meta_codecs.pop() if meta_codecs else "utf-8"
    else:
        codecs = {
            _TEXT_CHARSET_ALLOWLIST.get(
                next(value for value in match.groups() if value is not None)
                .strip()
                .casefold()
                .replace("_", "-")
            )
            for match in matches
        }
        if None in codecs or len(codecs) != 1:
            raise OfficialLiveEvidenceError("official text extraction failed")
        codec = codecs.pop()
        assert codec is not None

    try:
        return content.decode(codec, errors="strict")
    except UnicodeDecodeError as error:
        raise OfficialLiveEvidenceError("official text extraction failed") from error


def extract_official_snapshot_text(snapshot: OfficialSourceSnapshot) -> str:
    if hashlib.sha256(snapshot.content).hexdigest() != snapshot.content_sha256:
        raise OfficialLiveEvidenceError("official snapshot content SHA-256 differs")
    media_type = snapshot.content_type.casefold().split(";", maxsplit=1)[0]
    if media_type == "application/pdf":
        try:
            # Default extraction preserves Korean glyph streams that pypdf's
            # layout mode can silently omit on otherwise valid organizer-era
            # asset-manager PDFs.  Hash, page and text budgets remain fixed;
            # malformed files still fail closed below.
            reader = PdfReader(io.BytesIO(snapshot.content), strict=False)
            if len(reader.pages) > _MAX_PDF_PAGES:
                raise OfficialLiveEvidenceError("official PDF exceeds the page budget")
            parts = []
            size = 0
            for page in reader.pages:
                text = page.extract_text() or ""
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
        parser.feed(_decode_official_text(snapshot.content, snapshot.content_type))
        parser.close()
        extracted = "".join(parser.parts)
    elif media_type in {"text/plain", "text/csv", "application/csv"}:
        extracted = _decode_official_text(snapshot.content, snapshot.content_type)
    elif media_type == "application/json":
        try:
            payload = json.loads(_decode_official_text(snapshot.content, snapshot.content_type))
        except json.JSONDecodeError as error:
            raise OfficialLiveEvidenceError("official JSON extraction failed") from error

        rows: list[str] = []

        def flatten(value: object, path: str = "root") -> None:
            if isinstance(value, dict):
                scalar = [
                    f"{key}={item}"
                    for key, item in value.items()
                    if item is None or isinstance(item, (str, int, float, bool))
                ]
                if scalar:
                    rows.append(f"{path} " + " ".join(scalar))
                for key, item in value.items():
                    if isinstance(item, (dict, list)):
                        flatten(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    flatten(item, f"{path}[{index}]")

        flatten(payload)
        extracted = "\n".join(rows)
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


def _subjects_identify_product(
    subjects: tuple[str, ...],
    product: ProvidedProductCandidate,
) -> bool:
    """Return whether a requested surface identifies one exact DB product.

    A single-product prospectus can place its name on the cover and the risk
    paragraphs many pages later.  That justifies a two-span join only when the
    user's requested risk subject identifies that same product.  Otherwise a
    neighbouring product report fetched by broad discovery could donate its
    risks to the wrong requested product.
    """

    product_surfaces = tuple(
        value
        for surface in (product.product_name, *product.aliases)
        if (value := _normalized(surface))
    )
    for subject in subjects:
        if subject in product_surfaces:
            return True
        if len(subject) >= 6 and any(
            len(surface) >= 6 and (subject in surface or surface in subject)
            for surface in product_surfaces
        ):
            return True
    return False


_PRODUCT_TABLE_START = re.compile(
    r"(?:상위\s*\d+\s*종목|구성\s*종목|보유\s*종목|"
    r"top\s*(?:\d+\s*)?(?:holdings?|constituents?)|portfolio\s+holdings?)",
    re.IGNORECASE,
)

# A narrowed dependency universe can contain one selected DB product even when
# the fetched PDF is a catalogue describing many products.  In that case the
# old "single product document" shortcut attached every risk paragraph in the
# file to the selected product.  Detect repeated source-authored product-code
# sections from the document itself; this classification therefore does not
# depend on which DB candidates happened to survive an earlier relation join.
_PRODUCT_SECTION_CODE = re.compile(
    r"(?im)^\s*(?:종목\s*코드|상품\s*코드|펀드\s*코드|티커(?:\s*심볼)?|"
    r"ticker(?:\s*symbol)?|fund\s*code|product\s*code|symbol|isin)\s*"
    r"[:#：]?\s*(?P<code>[A-Z0-9][A-Z0-9._-]{2,31})(?:\s|$)",
)
_PRODUCT_SECTION_NAME = re.compile(
    r"(?im)^\s*schedule\s+of\s+investments(?:\s*\([^\n)]*\))?[^\n]*\n"
    r"\s*(?P<name>[^\n]{4,180}?(?:ETF|Fund))\s*$",
)
_GLOBAL_RISK_NOTICE_HEADING = re.compile(
    r"(?im)^\s*(?:compliance\s+notice|(?:투자자?\s*)?유의\s*사항|"
    r"투자\s*위험\s*고지)\s*$"
)


def _is_multi_product_document(text: str) -> bool:
    """Return whether source structure proves that at least two products exist."""

    codes = {
        match.group("code").replace(" ", "").casefold()
        for match in _PRODUCT_SECTION_CODE.finditer(text)
    }
    names = {
        _normalized(match.group("name"))
        for match in _PRODUCT_SECTION_NAME.finditer(text)
    }
    return len(codes) >= 2 or len(names) >= 2


def _product_section_markers(text: str) -> tuple[tuple[int, str, str], ...]:
    """Return source-authored code and Schedule-of-Investments sections.

    Some official portfolio reports label every product with a code, while
    others repeat ``Schedule of Investments`` followed by a full product
    name.  Both structures are provenance boundaries; search-result titles
    and a narrowed DB candidate set are not.
    """

    markers = [
        (
            match.start(),
            "code",
            unicodedata.normalize("NFKC", match.group("code")).strip().casefold(),
        )
        for match in _PRODUCT_SECTION_CODE.finditer(text)
    ]
    markers.extend(
        (match.start(), "name", _normalized(match.group("name")))
        for match in _PRODUCT_SECTION_NAME.finditer(text)
    )
    return tuple(sorted(markers, key=lambda item: item[0]))


def _product_document_code_keys(product: ProvidedProductCandidate) -> frozenset[str]:
    """Return only code-shaped DB aliases usable in a source section marker."""

    keys: set[str] = set()
    for surface in (product.product_id, *product.aliases):
        value = unicodedata.normalize("NFKC", surface).strip().casefold()
        if not value or re.fullmatch(r"[a-z0-9][a-z0-9._-]{3,24}", value) is None:
            continue
        keys.add(value)
        # Domestic source PDFs commonly omit the DB ticker's leading ``A``.
        # New exchange codes can contain letters, so require the exact six-code
        # shape instead of assuming the suffix is numeric.
        if re.fullmatch(r"a[a-z0-9]{6}", value):
            keys.add(value[1:])
        elif re.fullmatch(r"[a-z0-9]{6}", value):
            keys.add(f"a{value}")
    return frozenset(keys)


def _product_local_section_bounds(
    text: str,
    product: ProvidedProductCandidate,
) -> tuple[int, int] | None:
    """Return one exact contiguous source section owned by ``product``.

    A fund guide can describe dozens of products in one immutable PDF.  The
    provided-DB candidate set may already have been narrowed to one product by
    an earlier relation, but that is execution state rather than document
    provenance.  Product-code markers in the source are the authoritative
    section boundaries: a matching section starts at its marker and stops
    immediately before the next source-authored product marker.

    The same product code appearing again after another product is ambiguous:
    two disjoint sections must never be concatenated into one evidence scope.
    If no unique DB-backed marker run exists, fail closed.
    """

    markers = _product_section_markers(text)
    if len(markers) < 2:
        return None
    product_codes = _product_document_code_keys(product)
    product_names = tuple(
        value
        for surface in (product.product_name, *product.aliases)
        if len(value := _normalized(surface)) >= 8
    )
    if not product_codes and not product_names:
        return None
    belongs = tuple(
        (
            marker_kind == "code"
            and marker_value in product_codes
        )
        or (
            marker_kind == "name"
            and any(
                product_name == marker_value
                or product_name in marker_value
                or marker_value in product_name
                for product_name in product_names
            )
        )
        for _, marker_kind, marker_value in markers
    )
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(markers):
        if not belongs[index]:
            index += 1
            continue
        start_index = index
        while index + 1 < len(markers) and belongs[index + 1]:
            index += 1
        runs.append((start_index, index))
        index += 1
    if len(runs) != 1:
        return None
    start_index, end_index = runs[0]
    start = markers[start_index][0]
    end = markers[end_index + 1][0] if end_index + 1 < len(markers) else len(text)
    notice = _GLOBAL_RISK_NOTICE_HEADING.search(text, start, end)
    if notice is not None:
        end = notice.start()
    if end <= start or end - start > 200_000:
        return None
    return start, end


def _exact_product_identity_span(
    excerpt: str,
    product: ProvidedProductCandidate,
) -> str | None:
    """Return the smallest source line that proves one provided-DB identity."""

    product_codes = _product_document_code_keys(product)
    candidates: list[tuple[int, int, str]] = []
    canonical = _normalized(product.product_name)
    aliases = tuple(
        value
        for alias in product.aliases
        if len(value := _normalized(alias)) >= 4
    )
    for raw_line in excerpt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _PRODUCT_SECTION_CODE.fullmatch(line)
        if marker is not None:
            code = unicodedata.normalize("NFKC", marker.group("code")).strip().casefold()
            if code in product_codes:
                candidates.append((0, len(line), line))
                continue
        normalized_line = _normalized(line)
        if canonical and canonical in normalized_line:
            candidates.append((1, len(line), line))
        elif any(alias in normalized_line for alias in aliases):
            candidates.append((2, len(line), line))
    if not candidates:
        return None
    return min(candidates)[2]


def _document_global_risk_notice(text: str) -> str | None:
    """Return one explicitly headed, product-neutral investor-risk notice.

    A catalogue-wide compliance page can legitimately apply to every product,
    but ordinary prose after a product section cannot.  Require one unambiguous
    heading, no later product-code marker, an investor/product subject and an
    explicit risk-factor statement.  The returned value is one exact suffix of
    the immutable source, bounded to avoid treating an entire malformed file as
    a universal notice.
    """

    headings = tuple(_GLOBAL_RISK_NOTICE_HEADING.finditer(text))
    if len(headings) != 1:
        return None
    start = headings[0].start()
    notice = text[start:].strip()
    if (
        not notice
        or len(notice) > 20_000
        or _PRODUCT_SECTION_CODE.search(notice) is not None
        or re.search(r"투자자|금융\s*투자\s*상품|investor|investment", notice, re.I) is None
        or _RISK_FACTOR_SIGNAL.search(notice) is None
    ):
        return None
    return notice

_EXPLICIT_HOLDING_RANK_PREFIX = re.compile(
    r"(?im)^\s*(?:no\.?\s*)?(?P<rank>[1-9]\d{0,2})(?:\s*위|[.)]|\s+)",
)
_EXPLICIT_HOLDING_RANK_LABEL = re.compile(
    r"(?i)(?:순위|rank)\s*[:#]?\s*(?P<rank>[1-9]\d{0,2})(?!\d)",
)


def _explicit_holding_document_position(excerpt: str, subject_surface: str) -> int | None:
    """Return a source-authored holding rank, never an inferred row position.

    Generic HTML/PDF extraction loses table structure, so proximity to a
    heading or a weight is not rank evidence.  A position is admitted only
    when the same source line names the requested constituent and carries an
    explicit ordinal.  Ambiguous or conflicting ordinals fail closed.
    """

    if _PRODUCT_TABLE_START.search(excerpt) is None:
        return None
    normalized_subject = _normalized(subject_surface)
    if not normalized_subject:
        return None
    positions: set[int] = set()
    for line in excerpt.splitlines():
        if normalized_subject not in _normalized(line):
            continue
        for pattern in (_EXPLICIT_HOLDING_RANK_PREFIX, _EXPLICIT_HOLDING_RANK_LABEL):
            match = pattern.search(line)
            if match is not None:
                positions.add(int(match.group("rank")))
    if len(positions) != 1:
        return None
    position = positions.pop()
    return position if position <= 500 else None


def _single_product_document_owner(
    text: str,
    candidates: tuple[ProvidedProductCandidate, ...],
) -> ProvidedProductCandidate | None:
    """Identify one report owner before reading its holdings table.

    A single-product factsheet can list another ETF as a constituent. Treating
    every product name anywhere in the file as the report owner reverses that
    relation and can let the constituent win a later DB ranking. The cover is
    inspected only before the first holdings-table heading and is bounded to
    3,000 source characters. A unique longest official DB surface must identify
    the owner; ties remain unresolved.
    """

    table = _PRODUCT_TABLE_START.search(text)
    boundary = min(3_000, table.start() if table is not None else len(text))
    cover = _normalized(text[:boundary])
    if not cover:
        return None
    scored: list[tuple[int, int, ProvidedProductCandidate]] = []
    for candidate in candidates:
        canonical = _normalized(candidate.product_name)
        alias_lengths = tuple(
            len(value)
            for alias in candidate.aliases
            if len(value := _normalized(alias)) >= 6 and value in cover
        )
        canonical_length = len(canonical) if len(canonical) >= 8 and canonical in cover else 0
        best_length = max((canonical_length, *alias_lengths), default=0)
        if best_length:
            scored.append((1 if canonical_length else 0, best_length, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].product_id))
    best = scored[0]
    if len(scored) > 1 and scored[1][:2] == best[:2]:
        return None
    return best[2]


def _relation_evidence_windows(
    text: str,
    *,
    subjects: tuple[str, ...],
    signal: re.Pattern[str],
) -> tuple[str, ...]:
    """Return verbatim bounded windows robust to PDF/HTML table layout.

    Normal document chunks remain the fallback.  Additional line windows join
    only neighbouring source-authored text and are admitted only when both an
    exact subject and a relation cue occur.  This handles a table heading on
    one line and its holding/product row on the next without weakening the
    exact-span evidence boundary.
    """

    normalized_subjects = tuple(value for value in subjects if value)
    output: list[str] = []
    seen: set[str] = set()

    def append(value: str) -> None:
        bounded = value.strip()
        key = _normalized(bounded)
        if not bounded or len(bounded) > 2000 or not key or key in seen:
            return
        seen.add(key)
        output.append(bounded)

    lines = tuple(" ".join(line.split()) for line in text.splitlines() if line.strip())
    for index, line in enumerate(lines):
        normalized_line = _normalized(line)
        if signal.search(line) is None and not any(
            subject in normalized_line for subject in normalized_subjects
        ):
            continue
        for radius in (2, 4, 8):
            left = max(0, index - radius)
            right = min(len(lines), index + radius + 1)
            window = "\n".join(lines[left:right])
            normalized_window = _normalized(window)
            if (
                len(window) <= 2000
                and signal.search(window) is not None
                and any(subject in normalized_window for subject in normalized_subjects)
            ):
                append(window)
                break
    for chunk in chunk_document(text, max_chars=1200, overlap_chars=300):
        append(chunk)
    return tuple(output)


def _precise_risk_evidence_units(excerpt: str, *, limit: int = 5) -> tuple[str, ...]:
    """Return bounded investment-risk statements, excluding accounting noise.

    A 1,200-character retrieval chunk is useful for recall but is too broad to
    become a risk citation.  In particular, a financial report may mention an
    accounting ``loss`` or operating ``cost`` without describing an investor
    risk.  This function requires an explicit risk-factor predicate in the
    same sentence/source line and returns only the highest-signal units.
    """

    if limit < 1:
        raise ValueError("risk evidence limit must be positive")
    base_units = tuple(
        " ".join(unit.split())
        for unit in re.split(
            r"(?:\r?\n)+|(?<=[.!?。])(?:\s+|(?=[0-9A-Za-z가-힣]))|[•●■▪▶]+",
            excerpt,
        )
        if unit.strip()
    )
    raw_units = tuple(
        dict.fromkeys(
            (
                *base_units,
                *(
                    " ".join(base_units[index : index + width])
                    for width in (2, 3)
                    for index in range(len(base_units) - width + 1)
                    if len(" ".join(base_units[index : index + width])) <= 1_200
                ),
            )
        )
    )
    best_by_family: dict[str, tuple[int, int, str]] = {}
    seen: set[tuple[str, str]] = set()
    for ordinal, unit in enumerate(raw_units):
        if not 12 <= len(unit) <= 1_200:
            continue
        if _CONTENTS_OR_PAGE_NUMBER.search(unit) is not None:
            continue
        if _RISK_RATING_ONLY.fullmatch(unit) is not None:
            continue
        if _RISK_FACTOR_SIGNAL.search(unit) is None:
            continue
        family_matches = [
            (family, pattern, base_score)
            for family, pattern, base_score in _RISK_FAMILY_PATTERNS
            if pattern.search(unit) is not None
        ]
        if not family_matches:
            continue
        # Accounting losses, regulatory exposure measurements, distribution
        # mechanics and classification footnotes are not investor risk-factor
        # excerpts.  A nearby risk word must not rescue one of these units.
        is_noise = any(
            pattern.search(unit) is not None
            for pattern in (
                _RISK_ACCOUNTING_NOISE,
                _RISK_MEASUREMENT_NOISE,
                _RISK_DISTRIBUTION_OR_TAX_NOISE,
                _RISK_FOOTNOTE_NOISE,
            )
        )
        if is_noise:
            continue
        key = _normalized(unit)
        if not key:
            continue
        for family, _, base_score in family_matches:
            if (family, key) in seen:
                continue
            seen.add((family, key))
            score = base_score + 2 * len(family_matches)
            if re.search(
                r"(?:원금|원본).{0,12}손실|손실.{0,12}(?:가능|발생|위험)|"
                r"loss\s+of\s+principal",
                unit,
                re.I,
            ):
                score += 4
            if re.search(
                r"집중.{0,12}(?:위험|리스크)|추적\s*오차|괴리율|"
                r"tracking\s+(?:error|difference)|concentrat",
                unit,
                re.I,
            ):
                score += 3
            if re.search(
                r"환율|유동성|신용|금리|변동성|currency|liquidity|"
                r"credit|interest\s+rate|volatil",
                unit,
                re.I,
            ):
                score += 2
            candidate = (score, ordinal, unit)
            previous = best_by_family.get(family)
            if previous is None or (-score, ordinal, unit) < (
                -previous[0],
                previous[1],
                previous[2],
            ):
                best_by_family[family] = candidate
    chosen = sorted(best_by_family.values(), key=lambda item: (-item[0], item[1]))[:limit]
    return tuple(dict.fromkeys(item[2] for item in sorted(chosen, key=lambda item: item[1])))


def _observed_at(snapshot: OfficialSourceSnapshot) -> date:
    """Return the date the relation was observed in ``snapshot``.

    ``as_of`` comes from document metadata and can predate publication (a
    factsheet whose PDF metadata carries the fund inception date).  A relation
    is observed no earlier than the document that states it was published, so
    the later of the two dates is the observation date.  Without this the
    record validator rejects ``observed_at < valid_from`` and the runtime
    silently drops a real relation as ``live_exact_evidence_span_missing``.
    """

    if snapshot.published_at is not None and snapshot.published_at > snapshot.as_of:
        return snapshot.published_at
    return snapshot.as_of


def _explicit_date_occurs(text: str, value: date) -> bool:
    """Prove a requested product-local date from the official text itself."""

    year = str(value.year)
    short_year = f"{value.year % 100:02d}"
    month = f"0?{value.month}"
    day = f"0?{value.day}"
    return re.search(
        rf"(?<!\d)(?:"
        rf"{year}\s*(?:년|[./-])\s*{month}\s*(?:월|[./-])\s*{day}\s*일?"
        rf"|{month}\s*/\s*{day}\s*/\s*{year}"
        rf"|'?{short_year}\s*[./-]\s*{month}\s*[./-]\s*{day}"
        rf")(?!\d)",
        text,
        re.IGNORECASE,
    ) is not None


def _entity_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _snapshot_evidence_document_id(
    snapshot: OfficialSourceSnapshot,
    *,
    live_fallback: str | None = None,
) -> str:
    """Preserve an approved corpus ID without changing live evidence identity."""

    return snapshot.approved_document_id or live_fallback or _entity_id("doc", snapshot.source_uri)


def _exact_evidence_id(
    snapshot: OfficialSourceSnapshot,
    *,
    locator: str,
    excerpt: str,
    discriminator: str = "",
) -> str:
    """Bind an evidence ID to the exact immutable public source span.

    Retrieval-window ordinals are local to a particular query signal.  The
    same ordinal can therefore refer to different text when one official
    document is inspected for two relation dimensions.  URL, document hash,
    locator, verbatim excerpt and a semantic discriminator jointly prevent
    those spans from sharing an ID while preserving deterministic replay.
    """

    return _entity_id(
        "evidence",
        "\x1f".join(
            (
                snapshot.source_uri,
                snapshot.content_sha256,
                locator,
                excerpt,
                discriminator,
            )
        ),
    )


def _product_entity(candidate: ProvidedProductCandidate) -> TemporalEntity:
    return TemporalEntity(
        entity_id=_entity_id("product", f"{candidate.product_family.value}:{candidate.product_id}"),
        kind=TemporalEntityKind.PRODUCT,
        canonical_name=candidate.product_name,
        aliases=candidate.aliases,
    )


_KODEX_PRODUCT_PAGE_SOURCES = frozenset(
    {"samsungfund-kodex-product", "samsungfund-kodex-product-mobile"}
)


class VerifiedLiveRelationExtractor:
    """Conservative exact-span extraction from an approved official source.

    It never infers a product identity: a provided DB name/ticker/ISIN must be
    present in the same bounded excerpt as the subject and relation signal.
    """

    def __init__(self, *, text_cache_entries: int = 64) -> None:
        if not 1 <= text_cache_entries <= 256:
            raise ValueError("text_cache_entries must be between 1 and 256")
        self._text_cache_entries = text_cache_entries
        self._text_cache: OrderedDict[str, str | None] = OrderedDict()
        self._text_cache_lock = Lock()

    @staticmethod
    def _has_evidence_authority(snapshot: OfficialSourceSnapshot) -> bool:
        """Separate live-fetch authority from approved-cache authority."""

        if snapshot.source.status == "approved_for_live_evidence":
            return True
        return (
            snapshot.source.status == "approved_for_snapshot_intake"
            and snapshot.approved_document_id is not None
        )

    def _snapshot_text(self, snapshot: OfficialSourceSnapshot) -> str:
        """Extract each immutable official document at most once per runtime."""

        if hashlib.sha256(snapshot.content).hexdigest() != snapshot.content_sha256:
            raise OfficialLiveEvidenceError("official snapshot content SHA-256 differs")
        with self._text_cache_lock:
            if snapshot.content_sha256 in self._text_cache:
                cached = self._text_cache.pop(snapshot.content_sha256)
                self._text_cache[snapshot.content_sha256] = cached
                if cached is None:
                    raise OfficialLiveEvidenceError(
                        "official document previously failed text extraction"
                    )
                return cached
            try:
                extracted = extract_official_snapshot_text(snapshot)
            except OfficialLiveEvidenceError:
                extracted = None
            self._text_cache[snapshot.content_sha256] = extracted
            self._text_cache.move_to_end(snapshot.content_sha256)
            while len(self._text_cache) > self._text_cache_entries:
                self._text_cache.popitem(last=False)
        if extracted is None:
            raise OfficialLiveEvidenceError("official document text extraction failed")
        return extracted

    def extract_subject_evidence(
        self,
        snapshots: tuple[OfficialSourceSnapshot, ...],
        requirement: KnowledgeRequirement,
    ) -> tuple[ExternalEvidenceReference, ...]:
        """Verify only the named external entity, without inventing a relation.

        This narrow primitive supports an empty-intersection result such as an
        officially documented company/model name for which the complete
        provided-product DB has no matching product identity.  It is not a
        holdings/theme relation extractor: callers must keep relation-bearing
        or time-bounded questions on the stricter ``extract`` path.
        """

        subjects = tuple(
            dict.fromkeys(
                value
                for value in (requirement.subject, *requirement.search_aliases)
                if _normalized(value)
            )
        )
        normalized_subjects = tuple(_normalized(value) for value in subjects)
        evidence: dict[str, ExternalEvidenceReference] = {}
        for snapshot in snapshots:
            if not self._has_evidence_authority(snapshot):
                continue
            if snapshot.capability is not requirement.capability:
                continue
            if requirement.time_range is not None:
                # A dated relationship/history request cannot be satisfied by
                # proving only that the subject itself exists.
                continue
            try:
                text = self._snapshot_text(snapshot)
            except OfficialLiveEvidenceError:
                continue
            for ordinal, excerpt in enumerate(
                chunk_document(text, max_chars=1200, overlap_chars=200)
            ):
                normalized_excerpt = _normalized(excerpt)
                if not any(subject in normalized_excerpt for subject in normalized_subjects):
                    continue
                locator = f"extracted chunk {ordinal}"
                item = ExternalEvidenceReference(
                    evidence_id=_exact_evidence_id(
                        snapshot,
                        locator=locator,
                        excerpt=excerpt,
                        discriminator="subject-identity",
                    ),
                    source_registry_id=snapshot.source.source_id,
                    document_id=_snapshot_evidence_document_id(
                        snapshot,
                        live_fallback=(
                            f"live-{snapshot.source.source_id}-{snapshot.content_sha256[:16]}"
                        ),
                    ),
                    title=snapshot.title,
                    publisher=snapshot.source.publisher,
                    source_uri=snapshot.source_uri,
                    published_at=snapshot.published_at,
                    as_of=snapshot.as_of,
                    content_sha256=snapshot.content_sha256,
                    locator=locator,
                    excerpt=excerpt,
                )
                evidence[item.evidence_id] = item
                # One exact official excerpt per fetched document is enough to
                # verify the entity identity and keeps the response bounded.
                break
        return tuple(evidence[key] for key in sorted(evidence))

    @staticmethod
    def _theme_product_has_explicit_relation(
        excerpt: str,
        product: ProvidedProductCandidate,
        subjects: tuple[str, ...],
        signal: re.Pattern[str],
    ) -> bool:
        """Require one sentence/line to bind theme, relation and DB product.

        A product whose approved name contains the requested theme remains a
        valid table-style match. Otherwise all three elements must co-occur in
        one bounded sentence or source line. This admits explicit prose such as
        ``상품 X는 우주항공 테마 관련 ETF`` without letting neighbouring table
        rows inherit the heading.
        """

        product_surfaces = tuple(
            value
            for value in (
                _normalized(product.product_name),
                *(_normalized(alias) for alias in product.aliases),
            )
            if value
        )
        if any(
            subject in product_surface
            for subject in subjects
            for product_surface in product_surfaces
        ):
            return True
        units = tuple(
            unit.strip() for unit in re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", excerpt) if unit.strip()
        )
        return any(
            signal.search(unit) is not None
            and any(subject in _normalized(unit) for subject in subjects)
            and any(product_surface in _normalized(unit) for product_surface in product_surfaces)
            for unit in units
        )

    @staticmethod
    def _kodex_product_page_id(snapshot: OfficialSourceSnapshot) -> str | None:
        # The mobile product page is the same publisher's page for the same
        # opaque ID (the holdings graph binds through it when www is throttled);
        # the linked JSON/PDF resources themselves still resolve on www only.
        if snapshot.source.source_id not in _KODEX_PRODUCT_PAGE_SOURCES:
            return None
        parsed = urlsplit(snapshot.source_uri)
        if parsed.path != "/etf/product/view.do":
            return None
        values = parse_qs(parsed.query).get("id", [])
        if len(values) != 1 or re.fullmatch(r"[0-9A-Za-z_-]{2,40}", values[0]) is None:
            return None
        return values[0]

    @staticmethod
    def _kodex_holdings_api_id(snapshot: OfficialSourceSnapshot) -> str | None:
        if snapshot.source.source_id != "samsungfund-kodex-product":
            return None
        match = re.fullmatch(
            r"/api/v1/kodex/product-pdf/([0-9A-Za-z_-]{2,40})\.do",
            urlsplit(snapshot.source_uri).path,
        )
        return None if match is None else match.group(1)

    @staticmethod
    def _kodex_risk_pdf_id(snapshot: OfficialSourceSnapshot) -> str | None:
        if snapshot.source.source_id != "samsungfund-kodex-product":
            return None
        match = re.fullmatch(
            r"/upload/invest/([0-9A-Za-z_-]{2,40})-A\.pdf",
            urlsplit(snapshot.source_uri).path,
        )
        return None if match is None else match.group(1)

    def _kodex_page_bindings(
        self,
        snapshots: tuple[OfficialSourceSnapshot, ...],
        universe: ProvidedProductCandidateUniverse,
    ) -> dict[str, list[tuple[OfficialSourceSnapshot, ProvidedProductCandidate, str, int]]]:
        bindings: dict[
            str,
            list[tuple[OfficialSourceSnapshot, ProvidedProductCandidate, str, int]],
        ] = {}
        for snapshot in snapshots:
            page_id = VerifiedLiveRelationExtractor._kodex_product_page_id(snapshot)
            if page_id is None:
                continue
            try:
                page_text = self._snapshot_text(snapshot)
            except OfficialLiveEvidenceError:
                continue
            page_candidates = intersect_evidence_with_provided_products(
                universe,
                (page_text,),
            )
            page_universe = self._narrowed_universe(universe, page_candidates)
            for ordinal, excerpt in enumerate(
                chunk_document(page_text, max_chars=1200, overlap_chars=200)
            ):
                for product in intersect_evidence_with_provided_products(
                    page_universe,
                    (excerpt,),
                ):
                    bindings.setdefault(page_id, []).append((snapshot, product, excerpt, ordinal))
        return bindings

    @staticmethod
    def _narrowed_universe(
        universe: ProvidedProductCandidateUniverse,
        candidates: tuple[ProvidedProductCandidate, ...],
    ) -> ProvidedProductCandidateUniverse:
        """Keep immutable DB provenance while reducing repeated chunk joins."""

        return ProvidedProductCandidateUniverse(
            candidates=candidates,
            database_sha256s=universe.database_sha256s,
            candidate_filter_sha256=universe.candidate_filter_sha256,
            explicit_identity_bound=universe.explicit_identity_bound and bool(candidates),
        )

    def _extract_linked_kodex_holdings(
        self,
        snapshots: tuple[OfficialSourceSnapshot, ...],
        requirement: KnowledgeRequirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[TemporalRelationRecord, ...]:
        """Join two same-source official resources without trusting search titles.

        The HTML page proves the opaque KODEX page ID -> provided-DB product
        identity binding.  The historical JSON endpoint proves the exact
        holding and as-of date.  Both original content hashes and excerpts are
        retained on the relation.
        """

        if requirement.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS:
            return ()
        page_bindings = self._kodex_page_bindings(snapshots, universe)
        api_snapshots: dict[str, list[OfficialSourceSnapshot]] = {}
        for snapshot in snapshots:
            api_id = self._kodex_holdings_api_id(snapshot)
            if api_id is not None:
                api_snapshots.setdefault(api_id, []).append(snapshot)

        subject_surfaces = tuple(dict.fromkeys((requirement.subject, *requirement.search_aliases)))
        normalized_subjects = {
            _normalized(surface): surface for surface in subject_surfaces if _normalized(surface)
        }
        relations: dict[str, TemporalRelationRecord] = {}
        for product_page_id in sorted(set(page_bindings) & set(api_snapshots)):
            for api_snapshot in api_snapshots[product_page_id]:
                if requirement.time_range is not None and (
                    api_snapshot.as_of > requirement.time_range.end
                    or (
                        requirement.time_range.start is not None
                        and api_snapshot.as_of < requirement.time_range.start
                    )
                ):
                    continue
                try:
                    payload = json.loads(api_snapshot.content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                holdings = (
                    payload.get("pdf", {}).get("list", []) if isinstance(payload, dict) else []
                )
                if not isinstance(holdings, list):
                    continue
                raw_api = api_snapshot.content.decode("utf-8", errors="strict")
                for row_index, row in enumerate(holdings):
                    document_position = row_index + 1
                    if (
                        requirement.document_scope is not None
                        and document_position > requirement.document_scope.limit
                    ):
                        continue
                    if not isinstance(row, dict) or not isinstance(row.get("secNm"), str):
                        continue
                    holding_name = row["secNm"].strip()
                    normalized_holding = _normalized(holding_name)
                    matched_surfaces = tuple(
                        surface
                        for normalized, surface in normalized_subjects.items()
                        if normalized in normalized_holding or normalized_holding in normalized
                    )
                    if not matched_surfaces:
                        continue
                    serialized_row = json.dumps(
                        row,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    api_excerpt = serialized_row if serialized_row in raw_api else ""
                    if not api_excerpt:
                        needle = json.dumps(holding_name, ensure_ascii=False)
                        position = raw_api.find(needle)
                        if position < 0:
                            continue
                        left = raw_api.rfind("{", 0, position)
                        right = raw_api.find("}", position)
                        if left < 0 or right < 0:
                            continue
                        api_excerpt = raw_api[left : right + 1]
                    for page_snapshot, product, page_excerpt, page_ordinal in page_bindings[
                        product_page_id
                    ]:
                        for matched_surface in matched_surfaces:
                            subject_entity = TemporalEntity(
                                entity_id=_entity_id("company", matched_surface),
                                kind=TemporalEntityKind.COMPANY,
                                canonical_name=matched_surface,
                            )
                            product_entity = _product_entity(product)
                            page_locator = f"extracted chunk {page_ordinal}"
                            page_evidence_id = _exact_evidence_id(
                                page_snapshot,
                                locator=page_locator,
                                excerpt=page_excerpt,
                                discriminator=f"product-page:{product.product_id}",
                            )
                            api_locator = f"pdf.list[{row_index}]"
                            api_evidence_id = _exact_evidence_id(
                                api_snapshot,
                                locator=api_locator,
                                excerpt=api_excerpt,
                                discriminator=f"holding:{product.product_id}:{holding_name}",
                            )
                            relation_id = _entity_id(
                                "relation",
                                f"{requirement.requirement_id}:{product.product_id}:{api_evidence_id}",
                            )
                            evidence = tuple(
                                sorted(
                                    (
                                        ExternalEvidenceReference(
                                            evidence_id=page_evidence_id,
                                            source_registry_id=page_snapshot.source.source_id,
                                            document_id=_snapshot_evidence_document_id(
                                                page_snapshot
                                            ),
                                            title=page_snapshot.title,
                                            publisher=page_snapshot.source.publisher,
                                            source_uri=page_snapshot.source_uri,
                                            published_at=page_snapshot.published_at,
                                            as_of=page_snapshot.as_of,
                                            content_sha256=page_snapshot.content_sha256,
                                            locator=page_locator,
                                            excerpt=page_excerpt,
                                        ),
                                        ExternalEvidenceReference(
                                            evidence_id=api_evidence_id,
                                            source_registry_id=api_snapshot.source.source_id,
                                            document_id=_snapshot_evidence_document_id(
                                                api_snapshot
                                            ),
                                            title=api_snapshot.title,
                                            publisher=api_snapshot.source.publisher,
                                            source_uri=api_snapshot.source_uri,
                                            published_at=api_snapshot.published_at,
                                            as_of=api_snapshot.as_of,
                                            content_sha256=api_snapshot.content_sha256,
                                            locator=api_locator,
                                            excerpt=api_excerpt,
                                        ),
                                    ),
                                    key=lambda item: item.evidence_id,
                                )
                            )
                            relations[relation_id] = TemporalRelationRecord(
                                relation_id=relation_id,
                                capability=requirement.capability,
                                predicate=requirement.predicate,
                                subject=product_entity,
                                object=subject_entity,
                                product_family=product.product_family,
                                product_id=product.product_id,
                                valid_from=api_snapshot.as_of,
                                valid_to=api_snapshot.as_of,
                                observed_at=_observed_at(api_snapshot),
                                evidence=evidence,
                                document_rank=TemporalRelationDocumentRank(
                                    position=document_position,
                                    basis="official_ordered_holdings_array",
                                    evidence_id=api_evidence_id,
                                ),
                            )
        return tuple(relations[key] for key in sorted(relations))

    def _extract_linked_kodex_risks(
        self,
        snapshots: tuple[OfficialSourceSnapshot, ...],
        requirement: KnowledgeRequirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[TemporalRelationRecord, ...]:
        """Bind product identity and risk text across two same-ID official resources."""

        if requirement.capability is not KnowledgeCapability.PRODUCT_RISK_FACTORS:
            return ()
        page_bindings = self._kodex_page_bindings(snapshots, universe)
        pdf_snapshots: dict[str, list[OfficialSourceSnapshot]] = {}
        for snapshot in snapshots:
            product_id = self._kodex_risk_pdf_id(snapshot)
            if product_id is not None:
                pdf_snapshots.setdefault(product_id, []).append(snapshot)

        relations: dict[str, TemporalRelationRecord] = {}
        for opaque_id in sorted(set(page_bindings) & set(pdf_snapshots)):
            for pdf_snapshot in pdf_snapshots[opaque_id]:
                try:
                    pdf_text = self._snapshot_text(pdf_snapshot)
                except OfficialLiveEvidenceError:
                    continue
                # Score the immutable PDF as a whole.  Chunk overlap is useful
                # for retrieval recall but would duplicate the same risk and
                # privilege whichever weak legal sentence appears first.
                risk_units = _precise_risk_evidence_units(pdf_text, limit=8)
                for page_snapshot, product, page_excerpt, page_ordinal in page_bindings[opaque_id]:
                    product_entity = _product_entity(product)
                    page_locator = f"extracted chunk {page_ordinal}"
                    page_evidence_id = _exact_evidence_id(
                        page_snapshot,
                        locator=page_locator,
                        excerpt=page_excerpt,
                        discriminator=f"product-page:{product.product_id}",
                    )
                    for risk_ordinal, risk_excerpt in enumerate(risk_units):
                        risk_locator = f"extracted risk unit {risk_ordinal}"
                        risk_evidence_id = _exact_evidence_id(
                            pdf_snapshot,
                            locator=risk_locator,
                            excerpt=risk_excerpt,
                            discriminator=f"risk:{product.product_id}",
                        )
                        relation_id = _entity_id(
                            "relation",
                            f"{requirement.requirement_id}:{product.product_id}:{risk_evidence_id}",
                        )
                        relations[relation_id] = TemporalRelationRecord(
                            relation_id=relation_id,
                            capability=requirement.capability,
                            predicate=requirement.predicate,
                            subject=product_entity,
                            object=TemporalEntity(
                                entity_id=_entity_id("risk", risk_excerpt),
                                kind=TemporalEntityKind.RISK_CLAIM,
                                canonical_name=risk_excerpt[:500],
                            ),
                            product_family=product.product_family,
                            product_id=product.product_id,
                            valid_from=pdf_snapshot.published_at or pdf_snapshot.as_of,
                            valid_to=pdf_snapshot.published_at or pdf_snapshot.as_of,
                            observed_at=_observed_at(pdf_snapshot),
                            evidence=tuple(
                                sorted(
                                    (
                                        ExternalEvidenceReference(
                                            evidence_id=page_evidence_id,
                                            source_registry_id=page_snapshot.source.source_id,
                                            document_id=_snapshot_evidence_document_id(
                                                page_snapshot
                                            ),
                                            title=page_snapshot.title,
                                            publisher=page_snapshot.source.publisher,
                                            source_uri=page_snapshot.source_uri,
                                            published_at=page_snapshot.published_at,
                                            as_of=page_snapshot.as_of,
                                            content_sha256=page_snapshot.content_sha256,
                                            locator=page_locator,
                                            excerpt=page_excerpt,
                                        ),
                                        ExternalEvidenceReference(
                                            evidence_id=risk_evidence_id,
                                            source_registry_id=pdf_snapshot.source.source_id,
                                            document_id=_snapshot_evidence_document_id(
                                                pdf_snapshot
                                            ),
                                            title=pdf_snapshot.title,
                                            publisher=pdf_snapshot.source.publisher,
                                            source_uri=pdf_snapshot.source_uri,
                                            published_at=pdf_snapshot.published_at,
                                            as_of=pdf_snapshot.as_of,
                                            content_sha256=pdf_snapshot.content_sha256,
                                            locator=risk_locator,
                                            excerpt=risk_excerpt,
                                        ),
                                    ),
                                    key=lambda item: item.evidence_id,
                                )
                            ),
                        )
        return tuple(relations[key] for key in sorted(relations))

    def extract_many(
        self,
        snapshots: tuple[OfficialSourceSnapshot, ...],
        requirement: KnowledgeRequirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[TemporalRelationRecord, ...]:
        relations = {
            relation.relation_id: relation
            for snapshot in snapshots
            for relation in self.extract(snapshot, requirement, universe)
        }
        relations.update(
            {
                relation.relation_id: relation
                for relation in self._extract_linked_kodex_holdings(
                    snapshots,
                    requirement,
                    universe,
                )
            }
        )
        linked_risks = self._extract_linked_kodex_risks(
            snapshots,
            requirement,
            universe,
        )
        if linked_risks:
            # Same opaque official product ID proves a stronger page→risk-PDF
            # binding than generic same-document proximity.  Do not return both
            # paths for the same product or the generic legal text can swamp
            # the precise risk-family evidence.
            linked_product_ids = {item.product_id for item in linked_risks}
            relations = {
                key: item
                for key, item in relations.items()
                if not (
                    item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and item.product_id in linked_product_ids
                )
            }
            relations.update({item.relation_id: item for item in linked_risks})
        if requirement.document_scope is not None:
            relations = {
                key: item
                for key, item in relations.items()
                if item.satisfies_document_scope(requirement.document_scope)
            }
        return tuple(relations[key] for key in sorted(relations))

    def extract(
        self,
        snapshot: OfficialSourceSnapshot,
        requirement: KnowledgeRequirement,
        universe: ProvidedProductCandidateUniverse,
    ) -> tuple[TemporalRelationRecord, ...]:
        if not self._has_evidence_authority(snapshot):
            raise OfficialLiveEvidenceError(
                "source is neither approved for live evidence nor bound to an approved document"
            )
        if snapshot.capability is not requirement.capability:
            raise OfficialLiveEvidenceError("snapshot capability differs from requirement")
        full_text = self._snapshot_text(snapshot)
        source_is_catalogue = _is_multi_product_document(full_text)
        section_scoped = False
        text = full_text
        if (
            source_is_catalogue
            and requirement.capability
            in {
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            }
            and universe.explicit_identity_bound
            and len(universe.candidates) == 1
        ):
            bounds = _product_local_section_bounds(full_text, universe.candidates[0])
            if bounds is None:
                return ()
            text = full_text[bounds[0] : bounds[1]]
            section_scoped = True
        allow_document_wide_binding = section_scoped or not source_is_catalogue
        relation_as_of = snapshot.as_of
        if (
            requirement.time_range is not None
            and requirement.time_range.start == requirement.time_range.end
            and universe.explicit_identity_bound
            and len(universe.candidates) == 1
            and allow_document_wide_binding
            and _explicit_date_occurs(text, requirement.time_range.end)
        ):
            # A catalogue can be published after the holdings date printed in
            # one exact product section.  The section's literal date owns the
            # relation interval; publication remains the later observation
            # date.  Neither the question nor HCX may supply a missing date.
            relation_as_of = requirement.time_range.end
        subject_surfaces = tuple(dict.fromkeys((requirement.subject, *requirement.search_aliases)))
        normalized_document = _normalized(text)
        if not any(_normalized(surface) in normalized_document for surface in subject_surfaces):
            subject_surfaces = tuple(
                dict.fromkeys(
                    (
                        *subject_surfaces,
                        *(
                            alias
                            for surface in subject_surfaces
                            for alias in _bounded_ascii_tail_truncations(surface)
                        ),
                        *_official_cross_script_aliases(requirement.subject, text),
                    )
                )
            )
        subjects = tuple(value for surface in subject_surfaces if (value := _normalized(surface)))
        if requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY:
            expanded_subjects = set(subjects)
            for subject in subjects:
                for suffix in ("테마", "섹터", "산업"):
                    if subject.endswith(suffix) and len(subject) > len(suffix) + 1:
                        expanded_subjects.add(subject[: -len(suffix)])
            subjects = tuple(sorted(expanded_subjects))
        signal = requirement_relation_signal(requirement)
        relations: list[TemporalRelationRecord] = []
        subsidiary_pair = (
            (
                _SUBSIDIARY_PAIR.search(requirement.relation_evidence_span)
                or _SUBSIDIARY_SELECTION.search(requirement.relation_evidence_span)
            )
            if requirement.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
            else None
        )
        chunks = _relation_evidence_windows(
            text,
            subjects=subjects,
            signal=signal,
        )
        # OpenDART and similar regulator/issuer filings describe the issuer,
        # not every bond name.  Admit that evidence only through the typed DB
        # ``source_owner`` field and only when the user-selected subject is the
        # same legal entity.  The owner is never a general product alias, so a
        # filing cannot accidentally bind another issuer's products.
        issuer_risk_products: tuple[ProvidedProductCandidate, ...] = ()
        issuer_identity_binding: tuple[int, str] | None = None
        if (
            requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
            and snapshot.source.authority
            in {
                SourceAuthority.REGULATOR,
                SourceAuthority.PRODUCT_ISSUER,
                SourceAuthority.CORPORATE_OFFICIAL,
            }
        ):
            subject_owner_key = normalize_source_owner_identity(requirement.subject)
            title_owner = re.split(r"\s+[—–-]\s+", snapshot.title, maxsplit=1)[0]
            title_owner_key = normalize_source_owner_identity(title_owner)
            if (
                subject_owner_key
                and title_owner_key == subject_owner_key
                and universe.explicit_identity_bound
                and 1 <= len(universe.candidates) <= 8
            ):
                issuer_risk_products = tuple(
                    candidate
                    for candidate in universe.candidates
                    if candidate.product_family is ProductFamily.BOND
                    and candidate.source_owner is not None
                    and normalize_source_owner_identity(candidate.source_owner)
                    == subject_owner_key
                )
                issuer_identity_binding = next(
                    (
                        (ordinal, excerpt)
                        for ordinal, excerpt in enumerate(chunks)
                        if subject_owner_key in _normalized(excerpt)
                    ),
                    None,
                )
                if issuer_identity_binding is None:
                    issuer_risk_products = ()
        if (
            requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
            and section_scoped
            and (global_notice := _document_global_risk_notice(full_text)) is not None
        ):
            chunks = tuple(
                dict.fromkeys(
                    (
                        *chunks,
                        *chunk_document(global_notice, max_chars=1200, overlap_chars=300),
                    )
                )
            )
        document_candidates = intersect_evidence_with_provided_products(
            universe,
            (text,),
        )
        document_universe = self._narrowed_universe(universe, document_candidates)
        document_product_bindings: dict[
            tuple[ProductFamily, str], tuple[ProvidedProductCandidate, int, str]
        ] = {}
        for chunk_ordinal, chunk_excerpt in enumerate(chunks):
            for candidate in intersect_evidence_with_provided_products(
                document_universe,
                (chunk_excerpt,),
            ):
                identity_excerpt = _exact_product_identity_span(chunk_excerpt, candidate)
                if identity_excerpt is None:
                    continue
                document_product_bindings.setdefault(
                    (candidate.product_family, candidate.product_id),
                    (candidate, chunk_ordinal, identity_excerpt),
                )
        # Some official single-product reports put the product identity on the
        # cover and holdings/risk evidence on later pages.  A same-chunk join
        # is therefore too strict.  Admit a two-span join only when the entire
        # immutable document binds exactly one provided-DB product.  Multi-
        # product catalogues remain on the stricter same-chunk path.
        unique_document_binding = (
            next(iter(document_product_bindings.values()))
            if len(document_product_bindings) == 1
            else None
        )
        owner = (
            _single_product_document_owner(text, document_candidates)
            if allow_document_wide_binding
            else None
        )
        owner_document_binding = (
            document_product_bindings.get((owner.product_family, owner.product_id))
            if owner is not None
            else None
        )
        # A narrowed DB candidate set is never proof that a source catalogue is
        # a single-product document.  Cross-chunk ownership is admitted only
        # when the source itself does not expose multiple product-code sections.
        # Multi-product documents must bind through their exact local section.
        single_product_document_binding = (
            owner_document_binding or unique_document_binding
            if allow_document_wide_binding
            else None
        )
        risk_document_binding = (
            single_product_document_binding
            if (
                requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and single_product_document_binding is not None
                and (
                    section_scoped
                    or _subjects_identify_product(subjects, single_product_document_binding[0])
                )
            )
            else None
        )
        seen_product_risk_units: set[tuple[ProductFamily, str, str]] = set()
        product_risk_relation_counts: dict[tuple[ProductFamily, str], int] = {}
        for ordinal, excerpt in enumerate(chunks):
            normalized_excerpt = _normalized(excerpt)
            risk_units = (
                _precise_risk_evidence_units(excerpt)
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                else ()
            )
            subject_is_present = any(subject in normalized_excerpt for subject in subjects)
            if (
                not subjects
                or (
                    not subject_is_present
                    and not (
                        requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                        and (
                            risk_document_binding is not None
                            or issuer_identity_binding is not None
                        )
                    )
                )
                or signal.search(excerpt) is None
                or (
                    requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                    and not risk_units
                )
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
                    locator = f"extracted chunk {ordinal}"
                    evidence_id = _exact_evidence_id(
                        snapshot,
                        locator=locator,
                        excerpt=excerpt,
                        discriminator=f"subsidiary:{parent}:{child}",
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
                            observed_at=_observed_at(snapshot),
                            evidence=(
                                ExternalEvidenceReference(
                                    evidence_id=evidence_id,
                                    source_registry_id=snapshot.source.source_id,
                                    document_id=_snapshot_evidence_document_id(snapshot),
                                    title=snapshot.title,
                                    publisher=snapshot.source.publisher,
                                    source_uri=snapshot.source_uri,
                                    published_at=snapshot.published_at,
                                    as_of=snapshot.as_of,
                                    content_sha256=snapshot.content_sha256,
                                    locator=locator,
                                    excerpt=excerpt,
                                ),
                            ),
                        )
                    )
                continue
            products = intersect_evidence_with_provided_products(document_universe, (excerpt,))
            document_identity_evidence: dict[tuple[ProductFamily, str], tuple[int, str]] = {}
            if (
                requirement.capability
                in {
                    KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    KnowledgeCapability.PRODUCT_RISK_FACTORS,
                }
                and owner_document_binding is not None
                and allow_document_wide_binding
            ):
                # The exact company/relation cue remains in this source
                # excerpt, while product identity comes from the same immutable
                # document's cover. Constituents that happen to be ETFs are not
                # promoted to owners of the report.
                owner_product, owner_ordinal, owner_excerpt = owner_document_binding
                products = (owner_product,)
                document_identity_evidence[
                    (owner_product.product_family, owner_product.product_id)
                ] = (owner_ordinal, owner_excerpt)
            fallback_document_binding = (
                risk_document_binding
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                else single_product_document_binding
            )
            if not products and fallback_document_binding is not None:
                bound_product, bound_ordinal, bound_excerpt = fallback_document_binding
                products = (bound_product,)
                document_identity_evidence[
                    (bound_product.product_family, bound_product.product_id)
                ] = (
                    bound_ordinal,
                    bound_excerpt,
                )
            if (
                not products
                and requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and issuer_risk_products
                and issuer_identity_binding is not None
            ):
                products = issuer_risk_products
                for issuer_product in products:
                    document_identity_evidence[
                        (issuer_product.product_family, issuer_product.product_id)
                    ] = issuer_identity_binding
            if requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY:
                # A thematic guide often lists many neighbouring ETFs in one
                # table.  Only a provided-DB product whose own approved name or
                # alias contains the requested theme surface can inherit that
                # relation; proximity alone is not evidence.
                products = tuple(
                    product
                    for product in products
                    if self._theme_product_has_explicit_relation(
                        excerpt,
                        product,
                        subjects,
                        signal,
                    )
                )
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
                core = normalized_fund_programme_core(requirement.subject)
                if core is not None:
                    products = tuple(
                        candidate
                        for candidate in universe.candidates
                        if core in normalize_product_identity(candidate.product_name)
                        or any(
                            core in normalize_product_identity(alias) for alias in candidate.aliases
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
                relation_excerpt = excerpt
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                    if risk_document_binding is not None or product in issuer_risk_products:
                        product_risk_units = risk_units
                    else:
                        # A catalogue may mention several products in one
                        # retrieval chunk.  Only statements naming this exact
                        # DB product may bind to it; proximity is insufficient.
                        product_surfaces = tuple(
                            _normalized(surface)
                            for surface in (product.product_name, *product.aliases)
                            if _normalized(surface)
                        )
                        product_risk_units = tuple(
                            unit
                            for unit in risk_units
                            if any(surface in _normalized(unit) for surface in product_surfaces)
                        )
                    if not product_risk_units:
                        continue
                    relation_excerpt = "\n".join(product_risk_units)
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
                        product_key = (product.product_family, product.product_id)
                        if product_risk_relation_counts.get(product_key, 0) >= 5:
                            continue
                        risk_key = (
                            product.product_family,
                            product.product_id,
                            _normalized(relation_excerpt),
                        )
                        if risk_key in seen_product_risk_units:
                            continue
                        seen_product_risk_units.add(risk_key)
                        product_risk_relation_counts[product_key] = (
                            product_risk_relation_counts.get(product_key, 0) + 1
                        )
                        relation_subject = product_entity
                        relation_object = TemporalEntity(
                            entity_id=_entity_id("risk", relation_excerpt),
                            kind=TemporalEntityKind.RISK_CLAIM,
                            canonical_name=relation_excerpt[:500],
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
                    document_position = None
                    document_rank_basis = None
                    if requirement.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
                        document_position = _explicit_holding_document_position(
                            relation_excerpt,
                            relation_subject_surface,
                        )
                        if document_position is not None:
                            document_rank_basis = "explicit_document_rank"
                        elif (
                            owner_document_binding is not None
                            and owner_document_binding[0].product_family is product.product_family
                            and owner_document_binding[0].product_id == product.product_id
                            and (
                                bounded_membership := _bounded_top_n_holding_membership(
                                    text,
                                    (relation_subject_surface,),
                                )
                            )
                            is not None
                        ):
                            # An exact report owner plus a complete, unnumbered
                            # Top-N percentage table proves only membership
                            # within N, never the holding's exact row rank.
                            document_position, relation_excerpt = bounded_membership
                            document_rank_basis = "bounded_top_n_membership"
                        if requirement.document_scope is not None and (
                            document_position is None
                            or document_position > requirement.document_scope.limit
                        ):
                            continue
                    locator = f"extracted chunk {ordinal}"
                    if document_rank_basis == "bounded_top_n_membership":
                        locator = f"bounded top-{document_position} holdings table"
                    evidence_id = _exact_evidence_id(
                        snapshot,
                        locator=locator,
                        excerpt=relation_excerpt,
                        discriminator=(
                            f"{requirement.capability.value}:"
                            f"{product.product_family.value}:{product.product_id}:"
                            f"{relation_subject_surface}"
                        ),
                    )
                    document_rank = (
                        TemporalRelationDocumentRank(
                            position=document_position,
                            basis=document_rank_basis,
                            evidence_id=evidence_id,
                        )
                        if document_position is not None and document_rank_basis is not None
                        else None
                    )
                    evidence = [
                        ExternalEvidenceReference(
                            evidence_id=evidence_id,
                            source_registry_id=snapshot.source.source_id,
                            document_id=_snapshot_evidence_document_id(snapshot),
                            title=snapshot.title,
                            publisher=snapshot.source.publisher,
                            source_uri=snapshot.source_uri,
                            published_at=snapshot.published_at,
                            as_of=relation_as_of,
                            content_sha256=snapshot.content_sha256,
                            locator=locator,
                            excerpt=relation_excerpt,
                        )
                    ]
                    identity_binding = document_identity_evidence.get(
                        (product.product_family, product.product_id)
                    )
                    if (
                        identity_binding is not None
                        and _normalized(identity_binding[1]) not in _normalized(relation_excerpt)
                    ):
                        identity_ordinal, identity_excerpt = identity_binding
                        identity_locator = f"extracted chunk {identity_ordinal}"
                        identity_evidence_id = _exact_evidence_id(
                            snapshot,
                            locator=identity_locator,
                            excerpt=identity_excerpt,
                            discriminator=(
                                f"document-identity:{product.product_family.value}:"
                                f"{product.product_id}"
                            ),
                        )
                        evidence.append(
                            ExternalEvidenceReference(
                                evidence_id=identity_evidence_id,
                                source_registry_id=snapshot.source.source_id,
                                document_id=_snapshot_evidence_document_id(snapshot),
                                title=snapshot.title,
                                publisher=snapshot.source.publisher,
                                source_uri=snapshot.source_uri,
                                published_at=snapshot.published_at,
                                as_of=relation_as_of,
                                content_sha256=snapshot.content_sha256,
                                locator=identity_locator,
                                excerpt=identity_excerpt,
                            )
                        )
                    scope_entities = ()
                    if (
                        requirement.capability
                        is KnowledgeCapability.PRODUCT_RISK_FACTORS
                        and product in issuer_risk_products
                    ):
                        scope_entities = (
                            TemporalEntity(
                                entity_id=_entity_id("company", requirement.subject),
                                kind=TemporalEntityKind.COMPANY,
                                canonical_name=requirement.subject,
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
                            # Relation time comes from the official document.
                            # A point interval is used only after that exact date
                            # is re-proven inside one DB-bound product section.
                            valid_from=relation_as_of,
                            valid_to=relation_as_of,
                            observed_at=_observed_at(snapshot),
                            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
                            document_rank=document_rank,
                            scope_entities=scope_entities,
                        )
                    )
        unique = {item.relation_id: item for item in relations}
        return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "OfficialLiveEvidenceError",
    "VerifiedLiveRelationExtractor",
    "extract_official_snapshot_text",
]
