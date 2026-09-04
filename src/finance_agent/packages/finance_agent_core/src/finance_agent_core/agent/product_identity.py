from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage.sqlite import connect_read_only

_DOMESTIC_ETP_BRANDS = (
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
_BRAND = "|".join(_DOMESTIC_ETP_BRANDS)
_BRANDED_PRODUCT = re.compile(
    rf"(?P<name>\b(?:{_BRAND})\b(?:\s+[0-9A-Za-z가-힣&·+._/-]+){{1,10}}?)"
    r"(?=\s+(?:(?:국내|해외)\s*)?(?:ETF|ETN|ETP)(?:\s|$)|"
    r"\s*(?:의|이|가|은|는|을|를)?\s*(?:정보|상세|찾아|알려|보여|검색|조회|비교)|"
    r"[?!.,]|$)",
    re.IGNORECASE,
)
_EXPLICIT_BRANDED_NAME = re.compile(
    rf"(?:정확한?\s*)?(?:상품명|이름|명칭)(?:이|은|가)?\s*[:：]?\s*"
    rf"(?P<name>\b(?:{_BRAND})\b(?:\s+[0-9A-Za-z가-힣&·+._/-]+){{1,10}}?)"
    r"(?=\s*인\s+(?:(?:국내|한국)\s*)?(?:ETF|ETN|ETP)(?:을|를|이|가|은|는|\s|$))",
    re.IGNORECASE,
)
_NAMED_FUND = re.compile(
    r"(?<![0-9A-Za-z가-힣])"
    r"(?P<name>[0-9A-Za-z가-힣&·+._/-]{2,48}펀드)"
    r"(?=의|이|가|은|는|을|를|와|과|에|에서|으로|도|\s|[?!.,]|$)",
    re.IGNORECASE,
)
_GENERIC_FUND_NAMES = frozenset(
    {
        "공모펀드",
        "사모펀드",
        "주식형펀드",
        "채권형펀드",
        "혼합형펀드",
        "부동산펀드",
        "파생형펀드",
        "재간접펀드",
        "단기금융펀드",
        "mmf펀드",
        "상장지수펀드",
    }
)
_FUND_AGGREGATE_GRAMMAR = re.compile(
    r"(?:구분|유형|속성|시장|지역|통화|위험\s*등급|운용\s*속성)\s*별|"
    r"별\s*[·/,]\s*$",
    re.IGNORECASE,
)

_TABLE_BY_FAMILY = {
    ProductFamily.BOND: "bond_products",
    ProductFamily.DOMESTIC_ETP: "domestic_etp_products",
    ProductFamily.OVERSEAS_ETP: "overseas_etp_products",
    ProductFamily.FUND: "fund_products",
}
_IDENTITY_COLUMNS_BY_FAMILY = {
    ProductFamily.BOND: ("product_id", "product_name", "short_name", "ticker"),
    ProductFamily.DOMESTIC_ETP: (
        "product_id",
        "product_name",
        "short_name",
        "ticker",
        "isin",
    ),
    ProductFamily.OVERSEAS_ETP: (
        "product_id",
        "product_name",
        "ticker",
        "isin",
    ),
    ProductFamily.FUND: ("product_id", "product_name", "short_name"),
}
_GENERIC_IDENTITY_ALIASES = frozenset(
    {
        "etf",
        "etn",
        "etp",
        "fund",
        "펀드",
        "공모펀드",
        "채권",
        "국내채권",
        "국내 etf",
        "해외 etf",
        "국내 etn",
        "해외 etn",
        "kodex",
        "tiger",
        "ace",
        "rise",
        "sol",
        "hanaro",
        "timefolio",
        "arirang",
        "kbstar",
        "aum",
        "nav",
        "isin",
        "ytd",
        "top",
        "bottom",
        "mmf",
        "krw",
        "usd",
    }
)
_IDENTITY_SUFFIX = re.compile(
    r"^(?:의|이|가|은|는|을|를|와|과|에|에서|으로|로|도|보다|대비|중|부터|까지|끼리(?:만)?)"
)
_IDENTITY_CONTEXT_AFTER = re.compile(
    # Whitespace alone is not an identity boundary.  Otherwise an approved
    # short name such as ``KODEX 200`` would also authorize an invented
    # continuation such as ``KODEX 200 가짜 ETF``.  After optional whitespace
    # require punctuation, a Korean particle, or request grammar.
    r"^\s*(?:[?!.,;:()\[\]{}'\"“”‘’]|"
    r"(?:의|이|가|은|는|을|를|와|과|에|로|도|중)(?![가-힣])|"
    r"에서|으로|보다|대비|부터|까지|끼리(?:만)?|및|정보|상세|비교|조회|검색|확인|"
    r"알려|보여|찾아|수익률|보수|순자산|AUM|가격|종가|위험|운용사|발행사|"
    # A DB-exact bond name may be followed by its physical market qualifier
    # before the read action (``... 22-10 장외 상품 상세``).  Likewise, the
    # last member of a closed ticker list naturally owns the following Korean
    # counter (``VOO와 IVV와 SPY 세 종목``).  These are suffix boundaries,
    # never fuzzy identity evidence: the literal itself still has to exist in
    # the approved database index.
    r"(?:장내|장외)(?=\s|의|인|상품|채권|상세|정보|$)|"
    r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+)\s*"
    r"(?:개|종목|상품)(?:을|를|이|가|은|는)?(?=\s|[,.;:!?]|$))"
)
_IDENTITY_CONTEXT_BEFORE = re.compile(
    r"(?:^|\s|[?!.,;:()\[\]{}'\"“”‘’]|상품명|종목명|이름|명칭)$"
)
_IDENTITY_LIKE_SURFACE = re.compile(
    r"(?P<name>(?:[A-Z][0-9A-Za-z&·+._/ -]{2,70}(?:ETF|ETN|ETP)|"
    r"[0-9A-Za-z가-힣&·+._/-]{2,60}(?:펀드|채권(?:\s*\d{1,2}-\d{1,2})?)))"
    # Quotation marks delimit rather than validate a caller-supplied name.
    # Keep the unresolved-name detector active for quoted unknown products so
    # they cannot fall through to a broad or zero-row executable DETAIL.
    r"(?=\s*[\"'”’]?\s*(?:의|보다|대비))",
    re.IGNORECASE,
)
_LABELED_PRODUCT_IDENTIFIER = re.compile(
    r"(?:"
    r"(?:금융\s*)?(?:상품|종목|채권|펀드)\s*"
    r"(?:코드|번호|ID|아이디|식별자|식별\s*번호)|"
    r"티커|ISIN"
    r")(?:가|는|은|이)?\s*[:：=]?\s*"
    r"[\"'“‘]?(?P<identity>[A-Z0-9][A-Z0-9._:-]{1,47})[\"'”’]?",
    re.IGNORECASE,
)
_PLAIN_IDENTITY_LOOKUP_ACTION = re.compile(
    r"상세|정보|조회|확인|찾아|검색|설명|알려|보여",
    re.IGNORECASE,
)
_NON_PLAIN_IDENTITY_OPERATION = re.compile(
    r"(?:보다|대비|비교|순위|정렬|상위|하위|높은|낮은|큰|작은|많은|적은)|"
    r"(?:편입|보유|구성\s*종목|자회사|계열사|테마|섹터|연결\s*이력|"
    r"위험\s*요인|구조|운용\s*동향|판매\s*동향)|"
    r"(?:추가|삽입|끼워\s*넣|섞어\s*넣|지어\s*내|만들어|생성|가정)",
    re.IGNORECASE,
)
_GENERIC_IDENTITY_SURFACES = frozenset(
    {
        "etf",
        "etn",
        "etp",
        "국내 etf",
        "해외 etf",
        "국내 etn",
        "해외 etn",
        "공모펀드",
        "주식형펀드",
        "채권형펀드",
        "국내채권",
        "원화채권",
    }
)

# Fund users commonly omit the legal vehicle/type segment while retaining the
# distinctive product stem and sale class.  These aliases are derived only
# from an approved DB product name; collisions stay in the index and therefore
# fail closed instead of turning into fuzzy product guesses.
_FUND_LEGAL_VEHICLE = re.compile(
    r"(?:증권|혼합자산|주식|채권|부동산|특별자산|단기금융)?"
    r"(?:자)?투자신탁|투자회사",
    re.IGNORECASE,
)
_FUND_CLASSIFICATION = re.compile(r"\s*(?:\([^()]{1,40}\)|\[[^\[\]]{1,40}\])\s*")
_FUND_KIND_MARKER = re.compile(r"(?<![0-9A-Za-z가-힣])종류\s*(?=[A-Za-z0-9])")
_FUND_TRAILING_CLASS = re.compile(r"(?<=[가-힣\]])(?=C[0-9A-Za-z-]*$)", re.IGNORECASE)
_FUND_NUMBERED_SERIES = re.compile(r"(?<=[A-Za-z가-힣+])(?=\d+호(?:\s|$))")
_NON_IDENTITY_VALUE_PREFIX = re.compile(
    r"(?:신용|위험)\s*(?:등급|등급명|단계)(?:이|가|은|는)?\s*[:：=]?\s*$",
    re.IGNORECASE,
)
_IDENTITY_CORROBORATION_GAP = re.compile(
    r"^[\s,，:：;/'\"`“”‘’()\[\]{}-]*"
    r"(?:(?:ID|아이디|티커|ISIN|상품\s*(?:ID|코드)|종목\s*(?:ID|코드))"
    r"[\s,，:：;/'\"`“”‘’()\[\]{}-]*)*$",
    re.IGNORECASE,
)


def _clean_derived_fund_alias(value: str) -> str:
    value = _FUND_NUMBERED_SERIES.sub(" ", value)
    value = _FUND_TRAILING_CLASS.sub(" ", value)
    return " ".join(value.split()).strip(" -_/.,")


def _derived_fund_name_aliases(value: str) -> tuple[str, ...]:
    """Return conservative DB-derived abbreviations for one official fund name."""

    without_vehicle = _FUND_LEGAL_VEHICLE.sub(" ", value)
    if without_vehicle == value:
        return ()
    candidates = {_clean_derived_fund_alias(without_vehicle)}
    without_classification = _FUND_CLASSIFICATION.sub(" ", without_vehicle)
    candidates.add(_clean_derived_fund_alias(without_classification))
    candidates.add(
        _clean_derived_fund_alias(_FUND_KIND_MARKER.sub("", without_classification))
    )
    original = normalize_database_identity(value)
    return tuple(
        sorted(
            {
                candidate
                for candidate in candidates
                if candidate and normalize_database_identity(candidate) != original
            },
            key=lambda item: (-len(item), item),
        )
    )


def _looks_like_non_identity_field_value(
    question: str,
    *,
    start: int,
    candidates: tuple[DatabaseProductIdentityCandidate, ...],
) -> bool:
    """Keep enum values such as credit grade AAA out of the identity channel."""

    if not candidates or not all(
        item.alias_kind in {"product_id", "ticker", "isin"} for item in candidates
    ):
        return False
    return _NON_IDENTITY_VALUE_PREFIX.search(question[max(0, start - 32) : start]) is not None


def normalize_database_identity(value: str) -> str:
    """Normalize only representation differences, never financial meaning."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True, slots=True)
class DatabaseProductIdentityCandidate:
    product_family: ProductFamily
    product_id: str
    canonical_literal: str
    alias_kind: str


@dataclass(frozen=True, slots=True)
class DatabaseProductIdentityMatch:
    surface: str
    start: int
    end: int
    normalized_alias: str
    candidates: tuple[DatabaseProductIdentityCandidate, ...]

    @property
    def product_families(self) -> tuple[ProductFamily, ...]:
        return tuple(dict.fromkeys(item.product_family for item in self.candidates))

    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.product_id for item in self.candidates))

    @property
    def is_unique(self) -> bool:
        # Bond rows are physical venue/lot observations, while the organizer's
        # ticker is the public logical identity used by QueryPlan and Oracle.
        # Multiple rows with the same canonical literal are therefore one
        # product; different tickers/share classes remain ambiguous.
        logical_products = {
            (item.product_family, item.canonical_literal) for item in self.candidates
        }
        return len(logical_products) == 1


@dataclass(frozen=True, slots=True)
class _IdentityIndex:
    versions: tuple[tuple[ProductFamily, int, int, int, int], ...]
    aliases: Mapping[str, tuple[DatabaseProductIdentityCandidate, ...]]
    max_alias_length: int


def _normalized_question_with_offsets(value: str) -> tuple[str, tuple[int, ...]]:
    """Return a normalized question and an original-character offset map."""

    output: list[str] = []
    offsets: list[int] = []
    pending_space = False
    pending_offset = 0
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for item in normalized:
            if item.isspace():
                if output:
                    pending_space = True
                    pending_offset = index
                continue
            if pending_space:
                output.append(" ")
                offsets.append(pending_offset)
                pending_space = False
            output.append(item)
            offsets.append(index)
    return "".join(output), tuple(offsets)


_RIC_TICKER = re.compile(r"(?P<base>[A-Z0-9][A-Z0-9-]{0,31})\.(?P<venue>[A-Z0-9]{1,5})")


def _is_alias_eligible(value: str, *, alias_kind: str) -> bool:
    normalized = normalize_database_identity(value)
    if not normalized or normalized in _GENERIC_IDENTITY_ALIASES:
        return False
    if alias_kind in {"product_id", "ticker", "isin"}:
        return len(normalized) >= 2 and any(character.isalpha() for character in normalized)
    compact = re.sub(r"\s+", "", normalized)
    return len(compact) >= 4 and not compact.isdecimal()


class DatabaseProductIdentityMatcher:
    """Longest exact product-name matcher backed by the approved SQLite DBs.

    The matcher proposes no fuzzy identity.  It indexes the DB identity columns,
    preserves collisions, and therefore lets the caller CLARIFY instead of
    arbitrarily choosing one share class, venue, or product family.
    """

    def __init__(self, database_paths: Mapping[ProductFamily | str, str | Path]) -> None:
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        self._lock = RLock()
        self._index: _IdentityIndex | None = None

    def _versions(self) -> tuple[tuple[ProductFamily, int, int, int, int], ...]:
        versions: list[tuple[ProductFamily, int, int, int, int]] = []
        for family, path in sorted(self.database_paths.items(), key=lambda item: item[0].value):
            stat = path.stat()
            versions.append(
                (family, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            )
        return tuple(versions)

    def _load(
        self,
        versions: tuple[tuple[ProductFamily, int, int, int, int], ...],
    ) -> _IdentityIndex:
        aliases: dict[str, dict[tuple[ProductFamily, str], DatabaseProductIdentityCandidate]] = {}
        for family, path in self.database_paths.items():
            table = _TABLE_BY_FAMILY[family]
            requested_columns = _IDENTITY_COLUMNS_BY_FAMILY[family]
            with connect_read_only(path) as connection:
                available = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                identity_columns = tuple(
                    column for column in requested_columns if column in available
                )
                rows = connection.execute(
                    f"SELECT {', '.join(identity_columns)} FROM {table} "
                    "WHERE is_quarantined = 0"
                ).fetchall()
            for row in rows:
                product_id = str(row["product_id"])
                canonical_literal = (
                    str(row["ticker"])
                    if family in {ProductFamily.BOND, ProductFamily.OVERSEAS_ETP}
                    and "ticker" in identity_columns
                    and row["ticker"] is not None
                    else product_id
                )
                for column in identity_columns:
                    raw = row[column]
                    if raw is None:
                        continue
                    value = str(raw).strip()
                    if not _is_alias_eligible(value, alias_kind=column):
                        continue
                    alias = normalize_database_identity(value)
                    candidate = DatabaseProductIdentityCandidate(
                        product_family=family,
                        product_id=product_id,
                        canonical_literal=canonical_literal,
                        alias_kind=column,
                    )
                    # One record can expose the same literal through multiple
                    # columns.  Product collision is defined by family+ID, not
                    # by how many identity columns repeated the value.
                    aliases.setdefault(alias, {}).setdefault((family, product_id), candidate)
                    if family is ProductFamily.OVERSEAS_ETP and column == "ticker":
                        # 5,334 of the 6,037 provided overseas tickers carry a
                        # Reuters venue suffix (``QQQ.O``, ``TLT.O``) while a
                        # question writes the public base ticker.  Index the
                        # base as one more exact alias of the same row; a base
                        # shared by two venues stays a collision and CLARIFYs.
                        ric = _RIC_TICKER.fullmatch(value)
                        if ric is not None and _is_alias_eligible(
                            ric.group("base"), alias_kind="ticker"
                        ):
                            aliases.setdefault(
                                normalize_database_identity(ric.group("base")), {}
                            ).setdefault((family, product_id), candidate)
                    if family is ProductFamily.FUND and column == "product_name":
                        for derived_value in _derived_fund_name_aliases(value):
                            if not _is_alias_eligible(
                                derived_value,
                                alias_kind="derived_product_name",
                            ):
                                continue
                            derived_alias = normalize_database_identity(derived_value)
                            aliases.setdefault(derived_alias, {}).setdefault(
                                (family, product_id),
                                DatabaseProductIdentityCandidate(
                                    product_family=family,
                                    product_id=product_id,
                                    canonical_literal=canonical_literal,
                                    alias_kind="derived_product_name",
                                ),
                            )
        frozen = {
            alias: tuple(
                sorted(
                    candidates.values(),
                    key=lambda item: (item.product_family.value, item.product_id),
                )
            )
            for alias, candidates in aliases.items()
        }
        return _IdentityIndex(
            versions=versions,
            aliases=frozen,
            max_alias_length=max((len(alias) for alias in frozen), default=0),
        )

    def _current_index(self) -> _IdentityIndex:
        # Several pure compiler/contract tests deliberately use sentinel DB
        # paths.  Exact DB enrichment is optional at that boundary and must
        # not turn a DB-less compile into filesystem I/O failure.
        if not self.database_paths or not all(
            path.is_file() for path in self.database_paths.values()
        ):
            return _IdentityIndex(versions=(), aliases={}, max_alias_length=0)
        versions = self._versions()
        with self._lock:
            if self._index is None or self._index.versions != versions:
                self._index = self._load(versions)
            return self._index

    @staticmethod
    def _has_identity_boundary(question: str, start: int, end: int) -> bool:
        before = question[max(0, start - 6) : start]
        after = question[end : end + 12]
        if start > 0 and question[start - 1].isalnum():
            return False
        if end < len(question) and question[end].isalnum():
            if _IDENTITY_SUFFIX.match(after) is None:
                return False
        return (
            _IDENTITY_CONTEXT_BEFORE.search(before) is not None
            or start == 0
        ) and (
            end == len(question)
            or _IDENTITY_CONTEXT_AFTER.match(after) is not None
        )

    @staticmethod
    def _corroborate_adjacent_aliases(
        question: str,
        matches: list[DatabaseProductIdentityMatch],
    ) -> list[DatabaseProductIdentityMatch]:
        """Narrow a colliding name only when an adjacent exact ID proves it.

        The same official product name can appear in more than one supplied
        family or share class.  Users commonly print that name followed by its
        ticker and product ID in parentheses.  In that closed presentation
        group, a unique ID is authoritative corroboration for the colliding
        name.  Text outside punctuation and identity labels is never ignored,
        so an unrelated ID elsewhere in the question cannot widen or select a
        name candidate.
        """

        unique_matches = [item for item in matches if item.is_unique]
        narrowed: list[DatabaseProductIdentityMatch] = []
        for match in matches:
            if match.is_unique:
                narrowed.append(match)
                continue
            candidate_keys = {
                (item.product_family, item.canonical_literal) for item in match.candidates
            }
            corroborated_keys: set[tuple[ProductFamily, str]] = set()
            for exact in unique_matches:
                left, right = sorted((match, exact), key=lambda item: item.start)
                if right.start - left.end > 64:
                    continue
                gap = question[left.end : right.start]
                for intermediate in matches:
                    if left.end <= intermediate.start and intermediate.end <= right.start:
                        gap = gap.replace(intermediate.surface, " ")
                if _IDENTITY_CORROBORATION_GAP.fullmatch(gap) is None:
                    continue
                exact_keys = {
                    (item.product_family, item.canonical_literal)
                    for item in exact.candidates
                }
                corroborated_keys.update(candidate_keys.intersection(exact_keys))
            if len(corroborated_keys) != 1:
                narrowed.append(match)
                continue
            key = next(iter(corroborated_keys))
            narrowed.append(
                DatabaseProductIdentityMatch(
                    surface=match.surface,
                    start=match.start,
                    end=match.end,
                    normalized_alias=match.normalized_alias,
                    candidates=tuple(
                        item
                        for item in match.candidates
                        if (item.product_family, item.canonical_literal) == key
                    ),
                )
            )
        return narrowed

    def find(self, question: str) -> tuple[DatabaseProductIdentityMatch, ...]:
        index = self._current_index()
        normalized, offsets = _normalized_question_with_offsets(question)
        if not normalized or not offsets or not index.aliases:
            return ()
        matches: list[DatabaseProductIdentityMatch] = []
        for start in range(len(normalized)):
            if start > 0 and normalized[start - 1].isalnum():
                continue
            maximum = min(len(normalized), start + index.max_alias_length)
            for end in range(maximum, start + 1, -1):
                candidates = index.aliases.get(normalized[start:end])
                if candidates is None:
                    continue
                original_start = offsets[start]
                original_end = offsets[end - 1] + 1
                if not self._has_identity_boundary(question, original_start, original_end):
                    continue
                if _looks_like_non_identity_field_value(
                    question,
                    start=original_start,
                    candidates=candidates,
                ):
                    continue
                matches.append(
                    DatabaseProductIdentityMatch(
                        surface=question[original_start:original_end],
                        start=original_start,
                        end=original_end,
                        normalized_alias=normalized[start:end],
                        candidates=candidates,
                    )
                )
                # Longest alias at one start owns that surface.
                break
        selected: list[DatabaseProductIdentityMatch] = []
        for match in sorted(matches, key=lambda item: (-(item.end - item.start), item.start)):
            if any(match.start < item.end and item.start < match.end for item in selected):
                continue
            selected.append(match)
        ordered = sorted(selected, key=lambda item: item.start)
        return tuple(self._corroborate_adjacent_aliases(question, ordered))

    def resolve_literal(
        self,
        literal: str,
        *,
        family: ProductFamily | None = None,
    ) -> DatabaseProductIdentityMatch | None:
        index = self._current_index()
        alias = normalize_database_identity(literal)
        candidates = tuple(
            item
            for item in index.aliases.get(alias, ())
            if family is None or item.product_family is family
        )
        if not candidates:
            return None
        return DatabaseProductIdentityMatch(
            surface=literal,
            start=0,
            end=len(literal),
            normalized_alias=alias,
            candidates=candidates,
        )


def unresolved_identity_like_spans(
    question: str,
    matches: tuple[DatabaseProductIdentityMatch, ...] = (),
) -> tuple[str, ...]:
    """Find explicit name-shaped request spans not covered by exact DB identity."""

    covered = tuple((match.start, match.end) for match in matches)
    unresolved: list[str] = []
    for match in _IDENTITY_LIKE_SURFACE.finditer(question):
        if any(match.start("name") < end and start < match.end("name") for start, end in covered):
            continue
        value = " ".join(match.group("name").split()).strip()
        if normalize_database_identity(value) in _GENERIC_IDENTITY_SURFACES:
            continue
        unresolved.append(value)
    return tuple(dict.fromkeys(unresolved))


def extract_plain_labeled_product_lookup(question: str) -> str | None:
    """Return one explicitly labelled identity for a read-only exact lookup.

    This grammar does not decide that a product exists.  It only distinguishes
    an ordinary request to look up a caller-supplied code from an instruction
    to fabricate/insert a product or from a comparison, ranking, or external
    relation request.  The caller must prove absence against every in-scope
    provided database before returning NOT_FOUND.
    """

    identities = tuple(
        dict.fromkeys(
            match.group("identity").strip()
            for match in _LABELED_PRODUCT_IDENTIFIER.finditer(question)
        )
    )
    if len(identities) != 1:
        return None
    if not any(character.isalpha() for character in identities[0]):
        # Numeric exchange codes are resolved by the family-specific Router
        # and Oracle; the cross-database identity index intentionally does not
        # claim them without a product-family namespace.
        return None
    if _PLAIN_IDENTITY_LOOKUP_ACTION.search(question) is None:
        return None
    if _NON_PLAIN_IDENTITY_OPERATION.search(question) is not None:
        return None
    return identities[0]


def _trim_branded_product_surface(value: str) -> str:
    """Remove request grammar greedily captured after one branded name."""

    normalized = " ".join(value.split())
    normalized = re.split(
        r"\s+(?:최신|DB|데이터|값|정보|상세|찾아|알려|보여|검색|조회|비교|"
        r"각각|나란히|상품군별로)(?:\s|$)",
        normalized,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"(?:의|을|를|은|는|이|가)$", "", normalized).strip()


def extract_named_product_mentions(question: str) -> list[str]:
    """Extract narrow product-name surfaces without guessing a DB identity."""

    matches: list[tuple[int, int, str]] = []
    exact_spans = [
        (match.start("name"), match.end("name"), match.group("name").strip())
        for match in _EXPLICIT_BRANDED_NAME.finditer(question)
    ]
    matches.extend(exact_spans)
    matches.extend(
        (
            match.start(),
            match.end(),
            _trim_branded_product_surface(match.group("name")),
        )
        for match in _BRANDED_PRODUCT.finditer(question)
        if not any(
            match.start() < exact_end and exact_start < match.end()
            for exact_start, exact_end, _ in exact_spans
        )
    )
    for match in _NAMED_FUND.finditer(question):
        value = match.group("name").strip()
        if (
            value.casefold() not in _GENERIC_FUND_NAMES
            and _FUND_AGGREGATE_GRAMMAR.search(value) is None
        ):
            matches.append((match.start(), match.end(), value))
    return list(dict.fromkeys(value for _, _, value in sorted(matches)))


def is_named_product_mention(value: str) -> bool:
    """Return whether a mention belongs to the server-owned name grammar."""

    stripped = value.strip()
    if _BRANDED_PRODUCT.fullmatch(stripped) is not None:
        return True
    match = _NAMED_FUND.fullmatch(stripped)
    return match is not None and stripped.casefold() not in _GENERIC_FUND_NAMES


def is_quoted_product_mention(question: str, value: str) -> bool:
    escaped = re.escape(value.strip())
    return any(
        re.search(pattern, question) is not None
        for pattern in (
            rf'"{escaped}"',
            rf"'{escaped}'",
            rf"“{escaped}”",
            rf"‘{escaped}’",
        )
    )


def is_domestic_etp_brand_question(question: str) -> bool:
    return re.search(rf"\b(?:{_BRAND})\b", question, re.IGNORECASE) is not None


__all__ = [
    "DatabaseProductIdentityCandidate",
    "DatabaseProductIdentityMatch",
    "DatabaseProductIdentityMatcher",
    "extract_plain_labeled_product_lookup",
    "extract_named_product_mentions",
    "is_domestic_etp_brand_question",
    "is_named_product_mention",
    "is_quoted_product_mention",
    "normalize_database_identity",
    "unresolved_identity_like_spans",
]
