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
    normalized_fund_programme_core,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
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
_FUND_RELATION_DIMENSIONS: tuple[
    tuple[re.Pattern[str], re.Pattern[str]],
    ...,
] = (
    (
        re.compile(
            r"구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
            r"자금\s*(?:조달|배분)\s*구조|형태|structure|framework",
            re.IGNORECASE,
        ),
        re.compile(
            r"구조|조성|재원|출자|모자형|재간접|투자\s*기구|"
            r"structure|framework|funding\s+vehicle",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"(?:투자|운용)\s*(?:전략|기조|방침)|strategy",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:투자|운용)\s*(?:전략|기조|방침|방식)|자산\s*배분|"
            r"strategy|investment\s+approach|asset\s+allocation",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"판매|모집|출시|공모|subscription|sale", re.IGNORECASE),
        re.compile(
            r"판매|모집|출시|공모|청약|가입|납입|"
            r"subscription|offering|sale|launch",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"동향|현황|진행\s*(?:상황|현황)|향후|앞으로|계획|일정|"
            r"운용\s*(?:방향|계획|현황)|trend|status",
            re.IGNORECASE,
        ),
        re.compile(
            r"동향|현황|진행\s*상황|향후|계획|일정|완료|예정|"
            r"trend|status|schedule|completed|planned",
            re.IGNORECASE,
        ),
    ),
)


def _requirement_relation_signal(requirement: KnowledgeRequirement) -> re.Pattern[str]:
    """Bind each fund question dimension to corresponding official text.

    Fund structure, strategy, sales and trend share one capability, but they
    are not interchangeable evidence.  Returning a structure paragraph for a
    requested sales schedule is a grounded-answer error even when the same
    fund name occurs.  Other capabilities retain their dedicated predicate.
    """

    base = _RELATION_SIGNAL[requirement.capability]
    if requirement.capability is not KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND:
        return base
    patterns = tuple(
        evidence
        for request, evidence in _FUND_RELATION_DIMENSIONS
        if request.search(requirement.relation_evidence_span) is not None
    )
    if not patterns:
        return base
    unique_patterns = tuple(dict.fromkeys(item.pattern for item in patterns))
    # A compound sales-status request such as ``모집 진행상황`` asks for
    # both the subscription event and its status.  A generic status paragraph
    # cannot substitute for that sales evidence.  Other compound descriptions
    # (for example a policy document's ``구조와 투자 전략 동향``) retain
    # the established alternative-dimension behavior because one official
    # programme paragraph may legitimately describe them together without
    # repeating each heading verbatim.
    request_surface = requirement.relation_evidence_span
    sales_requested = _FUND_RELATION_DIMENSIONS[2][0].search(request_surface) is not None
    status_requested = _FUND_RELATION_DIMENSIONS[3][0].search(request_surface) is not None
    if sales_requested and status_requested:
        required_patterns = (
            _FUND_RELATION_DIMENSIONS[2][1].pattern,
            _FUND_RELATION_DIMENSIONS[3][1].pattern,
        )
        return re.compile(
            "".join(f"(?=.*(?:{pattern}))" for pattern in required_patterns),
            re.IGNORECASE | re.DOTALL,
        )
    return re.compile("|".join(f"(?:{pattern})" for pattern in unique_patterns), re.IGNORECASE)


_RISK_FACTOR_SIGNAL = re.compile(
    r"(?:원금|투자금).{0,16}(?:손실|잃)|"
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
            r"(?:원금|투자금).{0,20}(?:손실|잃)|최대\s*손실\s*예상액|"
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
        parser.feed(snapshot.content.decode("utf-8", errors="replace"))
        parser.close()
        extracted = "".join(parser.parts)
    elif media_type in {"text/plain", "text/csv", "application/csv"}:
        extracted = snapshot.content.decode("utf-8", errors="strict")
    elif media_type == "application/json":
        try:
            payload = json.loads(snapshot.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
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
                r"원금.{0,12}손실|손실.{0,12}(?:가능|발생|위험)|"
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
                suffix = f"{snapshot.content_sha256[:24]}:{ordinal}"
                item = ExternalEvidenceReference(
                    evidence_id=f"live-subject:{suffix}",
                    source_registry_id=snapshot.source.source_id,
                    document_id=(
                        f"live-{snapshot.source.source_id}-{snapshot.content_sha256[:16]}"
                    ),
                    title=snapshot.title,
                    publisher=snapshot.source.publisher,
                    source_uri=snapshot.source_uri,
                    published_at=snapshot.published_at,
                    as_of=snapshot.as_of,
                    content_sha256=snapshot.content_sha256,
                    locator=f"extracted chunk {ordinal}",
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
                            page_evidence_id = _entity_id(
                                "evidence",
                                f"{page_snapshot.content_sha256}:{page_ordinal}:{product.product_id}",
                            )
                            api_evidence_id = _entity_id(
                                "evidence",
                                f"{api_snapshot.content_sha256}:{row_index}:{holding_name}",
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
                                            document_id=_entity_id("doc", page_snapshot.source_uri),
                                            title=page_snapshot.title,
                                            publisher=page_snapshot.source.publisher,
                                            source_uri=page_snapshot.source_uri,
                                            published_at=page_snapshot.published_at,
                                            as_of=page_snapshot.as_of,
                                            content_sha256=page_snapshot.content_sha256,
                                            locator=f"extracted chunk {page_ordinal}",
                                            excerpt=page_excerpt,
                                        ),
                                        ExternalEvidenceReference(
                                            evidence_id=api_evidence_id,
                                            source_registry_id=api_snapshot.source.source_id,
                                            document_id=_entity_id("doc", api_snapshot.source_uri),
                                            title=api_snapshot.title,
                                            publisher=api_snapshot.source.publisher,
                                            source_uri=api_snapshot.source_uri,
                                            published_at=api_snapshot.published_at,
                                            as_of=api_snapshot.as_of,
                                            content_sha256=api_snapshot.content_sha256,
                                            locator=f"pdf.list[{row_index}]",
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
                    page_evidence_id = _entity_id(
                        "evidence",
                        f"{page_snapshot.content_sha256}:{page_ordinal}:{product.product_id}",
                    )
                    for risk_ordinal, risk_excerpt in enumerate(risk_units):
                        risk_evidence_id = _entity_id(
                            "evidence",
                            (
                                f"{pdf_snapshot.content_sha256}:{risk_ordinal}:"
                                f"{product.product_id}:{risk_excerpt}"
                            ),
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
                                            document_id=_entity_id("doc", page_snapshot.source_uri),
                                            title=page_snapshot.title,
                                            publisher=page_snapshot.source.publisher,
                                            source_uri=page_snapshot.source_uri,
                                            published_at=page_snapshot.published_at,
                                            as_of=page_snapshot.as_of,
                                            content_sha256=page_snapshot.content_sha256,
                                            locator=f"extracted chunk {page_ordinal}",
                                            excerpt=page_excerpt,
                                        ),
                                        ExternalEvidenceReference(
                                            evidence_id=risk_evidence_id,
                                            source_registry_id=pdf_snapshot.source.source_id,
                                            document_id=_entity_id("doc", pdf_snapshot.source_uri),
                                            title=pdf_snapshot.title,
                                            publisher=pdf_snapshot.source.publisher,
                                            source_uri=pdf_snapshot.source_uri,
                                            published_at=pdf_snapshot.published_at,
                                            as_of=pdf_snapshot.as_of,
                                            content_sha256=pdf_snapshot.content_sha256,
                                            locator=f"extracted risk unit {risk_ordinal}",
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
        return tuple(relations[key] for key in sorted(relations))

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
        text = self._snapshot_text(snapshot)
        subject_surfaces = tuple(dict.fromkeys((requirement.subject, *requirement.search_aliases)))
        normalized_document = _normalized(text)
        if not any(_normalized(surface) in normalized_document for surface in subject_surfaces):
            subject_surfaces = tuple(
                dict.fromkeys(
                    (
                        *subject_surfaces,
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
        signal = _requirement_relation_signal(requirement)
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
                document_product_bindings.setdefault(
                    (candidate.product_family, candidate.product_id),
                    (candidate, chunk_ordinal, chunk_excerpt),
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
        risk_document_binding = (
            unique_document_binding
            if (
                requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and unique_document_binding is not None
                and _subjects_identify_product(subjects, unique_document_binding[0])
            )
            else None
        )
        seen_product_risk_units: set[tuple[ProductFamily, str, str]] = set()
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
                        and risk_document_binding is not None
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
                            observed_at=_observed_at(snapshot),
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
            products = intersect_evidence_with_provided_products(document_universe, (excerpt,))
            document_identity_evidence: dict[tuple[ProductFamily, str], tuple[int, str]] = {}
            fallback_document_binding = (
                risk_document_binding
                if requirement.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                else unique_document_binding
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
                    if risk_document_binding is not None:
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
                        risk_key = (
                            product.product_family,
                            product.product_id,
                            _normalized(relation_excerpt),
                        )
                        if risk_key in seen_product_risk_units:
                            continue
                        seen_product_risk_units.add(risk_key)
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
                    evidence_id = _entity_id(
                        "evidence",
                        (
                            f"{snapshot.content_sha256}:{ordinal}:{product.product_family.value}:"
                            f"{product.product_id}:"
                            f"{relation_subject_surface}"
                        ),
                    )
                    evidence = [
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
                            excerpt=relation_excerpt,
                        )
                    ]
                    identity_binding = document_identity_evidence.get(
                        (product.product_family, product.product_id)
                    )
                    if identity_binding is not None and identity_binding[0] != ordinal:
                        identity_ordinal, identity_excerpt = identity_binding
                        identity_evidence_id = _entity_id(
                            "evidence",
                            (
                                f"{snapshot.content_sha256}:{identity_ordinal}:"
                                f"{product.product_family.value}:{product.product_id}:"
                                "document-identity"
                            ),
                        )
                        evidence.append(
                            ExternalEvidenceReference(
                                evidence_id=identity_evidence_id,
                                source_registry_id=snapshot.source.source_id,
                                document_id=_entity_id("doc", snapshot.source_uri),
                                title=snapshot.title,
                                publisher=snapshot.source.publisher,
                                source_uri=snapshot.source_uri,
                                published_at=snapshot.published_at,
                                as_of=snapshot.as_of,
                                content_sha256=snapshot.content_sha256,
                                locator=f"extracted chunk {identity_ordinal}",
                                excerpt=identity_excerpt,
                            )
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
                            observed_at=_observed_at(snapshot),
                            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
                        )
                    )
        unique = {item.relation_id: item for item in relations}
        return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "OfficialLiveEvidenceError",
    "VerifiedLiveRelationExtractor",
    "extract_official_snapshot_text",
]
