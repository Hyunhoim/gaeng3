from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from finance_agent_core.agent.field_language import registry_field_mentions
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    ProductFamily,
    Unit,
    search_projection,
)
from finance_agent_core.storage import ProductIdentityRecord


class ProductComparisonParseError(ValueError):
    """Raised when an exact two-product comparison cannot be compiled safely."""


class ProductIdentityNotFoundError(ProductComparisonParseError):
    """An explicit identity has no exact approved alias in the snapshot."""


class ProductIdentityAmbiguousError(ProductComparisonParseError):
    """An explicit identity resolves to more than one canonical product."""


_FIELD_PATTERNS: dict[ProductFamily, tuple[tuple[str, tuple[str, ...]], ...]] = {
    ProductFamily.OVERSEAS_ETP: (
        (
            "one_day_return_pct",
            (
                r"1\s*일\s*(?:수익률|성과)",
                r"(?:하루|당일|오늘|일간)\s*(?:수익률|성과)",
                r"1D\s*수익률",
            ),
        ),
        ("total_expense_ratio_pct", (r"총\s*보수(?:율)?", r"보수율", r"expense\s*ratio")),
        ("aum", (r"(?<![A-Z])AUM(?![A-Z])", r"순자산", r"운용\s*자산")),
        ("close_price", (r"종가", r"마감\s*가격")),
        ("daily_trading_value", (r"(?:외화|일간?)?\s*거래대금",)),
        ("daily_volume", (r"(?:일간?)?\s*거래량",)),
        ("discrepancy_pct", (r"괴리율", r"NAV\s*괴리율")),
        ("product_name", (r"상품명", r"상품\s*이름", r"정식\s*명칭")),
        (
            "product_type",
            (
                r"상품\s*유형",
                r"ETP\s*유형",
                r"ETF\s*ETN\s*구분",
                r"(?:^|의|[,，])\s*유형(?=\s*(?:[,，]|과|와|및|을|를|이|가|은|는|$))",
            ),
        ),
        ("exchange_code", (r"거래소(?:\s*코드)?",)),
        (
            "sellable",
            (
                r"판매\s*(?:가능\s*)?여부",
                r"판매\s*상태",
                r"매매\s*가능(?:\s*여부)?",
            ),
        ),
        (
            "trading_suspended",
            (
                r"거래\s*(?:중지|정지)\s*여부",
                r"거래\s*가능\s*여부",
                r"거래\s*상태",
            ),
        ),
        ("asset_type", (r"투자\s*자산(?:\s*유형)?", r"자산군")),
        ("investment_region", (r"투자\s*(?:지역|국가)",)),
        ("trading_currency", (r"거래\s*통화", r"표시\s*통화")),
    ),
    ProductFamily.DOMESTIC_ETP: (
        ("one_day_return_pct", (r"1\s*일\s*(?:수익률|성과)", r"일간\s*수익률", r"1D\s*수익률")),
        (
            "one_month_return_pct",
            (
                r"1\s*개월\s*(?:수익률|성과)",
                r"한\s*달\s*(?:수익률|성과)",
                r"월간\s*수익률",
                r"1M\s*수익률",
            ),
        ),
        ("three_month_return_pct", (r"3\s*개월\s*(?:수익률|성과)", r"3M\s*수익률")),
        ("six_month_return_pct", (r"6\s*개월\s*(?:수익률|성과)", r"6M\s*수익률")),
        (
            "one_year_return_pct",
            (r"1\s*년\s*(?:수익률|성과)", r"연간\s*수익률", r"1Y\s*수익률"),
        ),
        ("ytd_return_pct", (r"YTD\s*수익률", r"연초\s*(?:이후|대비)\s*수익률")),
        ("total_expense_ratio_pct", (r"총\s*보수(?:율)?", r"보수율")),
        ("daily_trading_value", (r"일(?:간)?\s*거래대금", r"거래대금")),
        ("daily_volume", (r"일(?:간)?\s*거래량", r"거래량")),
        ("tracking_error_pct", (r"추적\s*오차(?:율)?",)),
        ("discrepancy_pct", (r"괴리율", r"NAV\s*괴리율")),
        (
            "twenty_day_volatility_pct",
            (r"(?:20\s*거래일|20\s*일|1\s*개월).{0,8}?변동성",),
        ),
        (
            "sixty_day_volatility_pct",
            (r"(?:60\s*거래일|60\s*일|3\s*개월).{0,8}?변동성",),
        ),
        (
            "one_hundred_twenty_day_volatility_pct",
            (r"(?:120\s*거래일|120\s*일|6\s*개월).{0,8}?변동성",),
        ),
        (
            "two_hundred_fifty_two_day_volatility_pct",
            (r"(?:252\s*거래일|252\s*일|1\s*년).{0,8}?변동성",),
        ),
        ("distribution_yield_pct", (r"(?:연환산\s*)?분배\s*수익률",)),
        ("leverage_factor", (r"레버리지\s*배수", r"(?<!분)배수")),
        ("close_price", (r"종가", r"마감\s*가격")),
        ("aum", (r"(?<![A-Z])AUM(?![A-Z])", r"순자산", r"운용\s*자산")),
        ("product_name", (r"상품명", r"상품\s*이름", r"정식\s*명칭")),
        (
            "product_type",
            (
                r"상품\s*유형",
                r"ETP\s*유형",
                r"ETF\s*ETN\s*구분",
                r"(?:^|의|[,，])\s*유형(?=\s*(?:[,，]|과|와|및|을|를|이|가|은|는|$))",
            ),
        ),
        ("exchange_code", (r"거래소(?:\s*코드)?",)),
        (
            "sellable",
            (
                r"판매\s*(?:가능\s*)?여부",
                r"판매\s*상태",
                r"매매\s*가능(?:\s*여부)?",
            ),
        ),
        (
            "trading_suspended",
            (
                r"거래\s*(?:중지|정지)\s*여부",
                r"거래\s*가능\s*여부",
                r"거래\s*상태",
            ),
        ),
        ("asset_type", (r"투자\s*자산(?:\s*유형)?", r"자산군")),
        ("investment_region", (r"투자\s*(?:지역|국가)",)),
        ("manager", (r"운용사", r"자산운용사", r"발행사")),
        ("base_index", (r"기초\s*지수", r"추종\s*지수")),
        ("strategy", (r"운용\s*전략", r"복제\s*방식")),
        ("risk_level", (r"위험\s*등급", r"위험도")),
        (
            "pension_eligible",
            (
                r"연금\s*(?:거래\s*)?가능\s*여부",
                r"연금\s*계좌.{0,12}?거래\s*가능",
            ),
        ),
        (
            "core_etf",
            (
                r"핵심\s*ETF\s*여부",
                r"코어\s*ETF\s*여부",
                r"핵심\s*ETF.{0,12}?해당(?:하는지)?\s*여부",
            ),
        ),
        ("trading_currency", (r"거래\s*통화", r"표시\s*통화")),
    ),
    ProductFamily.BOND: (
        ("product_name", (r"상품명", r"상품\s*이름", r"정식\s*명칭")),
        ("after_tax_yield_pct", (r"세후\s*수익률",)),
        ("buy_yield_pct", (r"매수\s*수익률", r"세전\s*수익률")),
        (
            "applied_yield_pct",
            (
                r"민평\s*(?:수익률|금리)",
                r"민평에서\s*(?:산출|계산)한\s*(?:수익률|금리)",
                r"민간평가\s*금리",
                r"평가기관\s*수익률",
                r"적용\s*수익률",
            ),
        ),
        (
            "evaluation_price",
            (r"평가일단가", r"평가\s*단가", r"평가\s*가격", r"클린\s*프라이스"),
        ),
        (
            "dirty_price",
            (r"이자부단가", r"경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)"),
        ),
        ("convexity", (r"컨벡시티", r"가격\s*굴곡")),
        (
            "coupon_rate_pct",
            (
                r"표면\s*(?:이율|금리)",
                r"쿠폰(?:\s*금리)?",
                r"액면\s*쿠폰",
                r"액면에?\s*붙은\s*이자율",
            ),
        ),
        (
            "issue_amount",
            (r"발행\s*(?:잔액|금액|액)", r"남은\s*발행\s*물량"),
        ),
        (
            "remaining_days",
            (
                r"잔존\s*일수?",
                r"잔여\s*일",
                r"(?:상환|만기)까지\s*남은\s*(?:일수|날짜|날|기간)",
                r"(?:상환|만기)(?:이|가)?\s*(?:먼저|빨리)",
            ),
        ),
        ("duration_years", (r"듀레이션",)),
        ("bond_market", (r"채권\s*거래\s*시장", r"장내외\s*구분")),
        ("issuer", (r"발행\s*(?:기관|사|자)",)),
        ("bond_major_class", (r"채권\s*대분류", r"(?<!세부)대분류")),
        ("bond_subclass", (r"채권\s*소분류", r"소분류", r"세부\s*분류")),
        ("bond_type", (r"채권\s*(?:종류|유형)", r"채종")),
        ("issue_date", (r"발행일",)),
        ("maturity_date", (r"만기일", r"상환일")),
        ("credit_rating", (r"신용\s*등급",)),
        ("bond_risk_code", (r"위험\s*(?:등급\s*)?코드",)),
        ("currently_buyable", (r"(?:현재|스냅샷\s*기준)?\s*매수\s*가능\s*여부",)),
        ("trading_currency", (r"거래\s*통화", r"표시\s*통화")),
    ),
}

_UNSUPPORTED_PATTERNS: dict[ProductFamily, tuple[tuple[str, str], ...]] = {
    ProductFamily.OVERSEAS_ETP: (),
    ProductFamily.DOMESTIC_ETP: (),
    ProductFamily.BOND: ((r"예상\s*수익률|만기\s*수익률", "제공 데이터에 없는 수익률"),),
}
_UNSAFE_TARGET_ROLE_PATTERN = re.compile(
    r"제외(?:하고|한|해)?|빼고|말고|대신|미포함|포함(?:되)?지\s*않",
    flags=re.IGNORECASE,
)
_INCLUSION_ROLE_PATTERN = re.compile(r"포함", flags=re.IGNORECASE)
_EXPLICIT_TWO_ID_SET_PATTERN = re.compile(
    r"(?:상품\s*(?:ID|아이디|번호)|종목\s*(?:코드|번호)|식별자)"
    r"(?:가|는|은|이)?\s*[:：]?\s*"
    r"[A-Z0-9._:-]{2,30}\s*(?:와|과|및)\s*"
    r"[A-Z0-9._:-]{2,30}\s*에\s*포함되는",
    flags=re.IGNORECASE,
)
_DOMESTIC_RETURN_FIELDS = {
    "one_day_return_pct",
    "one_month_return_pct",
    "three_month_return_pct",
    "six_month_return_pct",
    "one_year_return_pct",
    "ytd_return_pct",
}
_OVERSEAS_TICKER_WITH_EXCHANGE_SUFFIX = re.compile(
    r"^(?P<base>[A-Z0-9][A-Z0-9-]{0,31})\.(?P<exchange>[A-Z0-9]{1,5})$",
    flags=re.IGNORECASE,
)
_INPUT_ORDER_COMPARISON = re.compile(
    r"(?:입력(?:한)?|나열(?:한)?|제시(?:한)?|작성(?:한)?|"
    r"(?:적은|쓴))\s*순서(?:\s*그대로|대로|로)?\s*비교|"
    r"비교.{0,24}?(?:입력(?:한)?|나열(?:한)?|제시(?:한)?|작성(?:한)?|"
    r"(?:적은|쓴))\s*순서|"
    r"(?:입력(?:한)?|질문에\s*(?:적은|쓴)|나열(?:한)?|제시(?:한)?)\s*"
    r"(?:그\s*)?(?:순서|차례)(?:를\s*유지해서|대로|로)?\s*"
    r"(?:조회|확인|나열|적어|보여|알려)|"
    r"(?:순서|차례)(?:를\s*유지해서|대로|로)\s*"
    r"(?:조회|확인|나열|적어|보여|알려)|나란히\s*(?:조회|확인|나열|적어|보여|알려)",
    re.IGNORECASE,
)
_COORDINATED_RETURN_PERIOD = re.compile(
    r"(?P<periods>"
    r"(?:1\s*일|하루|1D|1\s*주|일주일|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y)"
    r"(?:\s*(?:및|와|과|·|/|,)\s*"
    r"(?:1\s*일|하루|1D|1\s*주|일주일|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y))+"
    r")\s*(?:수익률|수익|성과)",
    re.IGNORECASE,
)
_LEADING_RETURN_PERIOD = re.compile(
    r"(?:수익률|수익|성과)(?:을|를|은|는)?\s*(?:각각\s*)?"
    r"(?P<periods>"
    r"(?:1\s*일|하루|1D|1\s*주|일주일|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y)"
    r"(?:\s*(?:및|와|과|·|/|,)\s*"
    r"(?:1\s*일|하루|1D|1\s*주|일주일|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y))+"
    r")\s*(?:기준|구간|기간)",
    re.IGNORECASE,
)
_RETURN_PERIOD_TOKEN = re.compile(
    r"1\s*일|하루|1D|1\s*주|일주일|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y",
    re.IGNORECASE,
)
_RETURN_FIELD_BY_PERIOD = {
    "1일": "one_day_return_pct",
    "하루": "one_day_return_pct",
    "1d": "one_day_return_pct",
    "1주": "one_week_return_pct",
    "일주일": "one_week_return_pct",
    "1w": "one_week_return_pct",
    "1개월": "one_month_return_pct",
    "한달": "one_month_return_pct",
    "1m": "one_month_return_pct",
    "3개월": "three_month_return_pct",
    "석달": "three_month_return_pct",
    "3m": "three_month_return_pct",
    "6개월": "six_month_return_pct",
    "반년": "six_month_return_pct",
    "6m": "six_month_return_pct",
    "18개월": "eighteen_month_return_pct",
    "18m": "eighteen_month_return_pct",
    "1년": "one_year_return_pct",
    "1y": "one_year_return_pct",
    "2년": "two_year_return_pct",
    "2y": "two_year_return_pct",
    "3년": "three_year_return_pct",
    "3y": "three_year_return_pct",
    "5년": "five_year_return_pct",
    "5y": "five_year_return_pct",
}
_COORDINATED_FUND_RATIO = re.compile(
    r"(?P<assets>"
    r"(?:국내\s*주식|국내\s*채권|해외\s*주식|해외\s*채권)"
    r"(?:\s*(?:및|와|과|·|/|,)\s*"
    r"(?:국내\s*주식|국내\s*채권|해외\s*주식|해외\s*채권))+"
    r")\s*(?:편입\s*비|구성\s*비율|구성비|비중)",
    re.IGNORECASE,
)
_FUND_RATIO_TOKEN = re.compile(
    r"국내\s*주식|국내\s*채권|해외\s*주식|해외\s*채권",
    re.IGNORECASE,
)
_FUND_RATIO_FIELD = {
    "국내주식": "domestic_stock_ratio_pct",
    "국내채권": "domestic_bond_ratio_pct",
    "해외주식": "overseas_stock_ratio_pct",
    "해외채권": "overseas_bond_ratio_pct",
}
_DEFAULT_COMPARISON_FIELDS: dict[ProductFamily, tuple[str, ...]] = {
    ProductFamily.BOND: (
        "credit_rating",
        "maturity_date",
        "coupon_rate_pct",
    ),
    ProductFamily.DOMESTIC_ETP: (
        "product_type",
        "manager",
        "asset_type",
        "investment_region",
        "aum",
        "total_expense_ratio_pct",
    ),
    ProductFamily.OVERSEAS_ETP: (
        "product_type",
        "exchange_code",
        "manager",
        "asset_type",
        "investment_region",
        "aum",
        "total_expense_ratio_pct",
    ),
    ProductFamily.FUND: (
        "short_name",
        "fund_management_attribute",
        "investment_region",
        "risk_level",
        "aum",
        "sellable",
    ),
}


def basic_input_order_comparison_requested(question: str) -> bool:
    """Return whether a closed product set asks for a basic ordered comparison."""

    return _INPUT_ORDER_COMPARISON.search(question) is not None


def comparison_projection(
    family: ProductFamily,
    comparison_fields: Sequence[str],
) -> list[str]:
    """Return standard product evidence plus every requested comparison field."""

    registry = load_field_registry()
    candidates = list(comparison_fields)
    if any(
        "trading_currency" in registry.require_field(field_name, [family.value]).comparison_scope
        for field_name in comparison_fields
    ):
        candidates.append("trading_currency")
    return search_projection(family, *candidates)


def _has_unsafe_target_role(
    question: str,
    mentions: Sequence[str],
    family: ProductFamily,
) -> bool:
    if _UNSAFE_TARGET_ROLE_PATTERN.search(question):
        return True
    inclusion_matches = list(_INCLUSION_ROLE_PATTERN.finditer(question))
    if not inclusion_matches:
        return False
    if len(mentions) != 2:
        return True
    # Database identity binding intentionally canonicalizes an exchange-qualified
    # product ID to its public ticker (for example ``AMX:B1`` -> ``B1``).
    # Target-role validation must therefore be based on the explicit *shape* of
    # the user's two-ID set, not string equality with the downstream canonical
    # literals.  The compiler independently proves both canonical identities
    # against the approved DB before execution.  Requiring the label and the
    # closed ``X and Y are included`` grammar keeps additional/negated inclusion
    # requests fail-closed while accepting equivalent IDs after canonicalization.
    allowed_spans = [
        match.span() for match in _EXPLICIT_TWO_ID_SET_PATTERN.finditer(question)
    ]
    # Some canonical metric names contain the word ``포함`` (for example the
    # bond dirty price, "경과이자 포함 가격").  Registry-owned comparable
    # field spans describe what to compare, not which products to add.  They
    # are safe only as exact field spans; every other positive inclusion still
    # requires the closed two-ID grammar above.
    allowed_spans.extend(
        (mention.start, mention.end)
        for mention in registry_field_mentions(question, family.value, "comparable")
        if "포함" in mention.phrase
    )
    return any(
        not any(start <= match.start() and match.end() <= end for start, end in allowed_spans)
        for match in inclusion_matches
    )


def normalize_product_mention(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def _overseas_base_ticker(value: str) -> str | None:
    """Return the exact base of a supported exchange-suffixed overseas ticker."""

    normalized = unicodedata.normalize("NFKC", value).strip()
    match = _OVERSEAS_TICKER_WITH_EXCHANGE_SUFFIX.fullmatch(normalized)
    if match is None:
        return None
    return normalize_product_mention(match.group("base"))


def _identity_aliases(
    family: ProductFamily,
    record: ProductIdentityRecord,
) -> Iterable[str]:
    yield record.product_id
    yield record.product_name
    if family is ProductFamily.OVERSEAS_ETP:
        if record.ticker:
            yield record.ticker
        if record.isin:
            yield record.isin
    elif family is ProductFamily.DOMESTIC_ETP:
        for alias in (record.ticker, record.isin, record.short_name):
            if alias:
                yield alias
    elif family is ProductFamily.FUND:
        # The organizer's fund table exposes the sale-class identity in the
        # short name (for example C, C-e and C1-e).  Exact DETAIL requests must
        # resolve that approved DB identity before broad fund-group fallback.
        # Omitting this alias made a quoted, unique short name look like a group
        # search and could return the wrong share class.
        if record.short_name:
            yield record.short_name
    elif family is ProductFamily.BOND:
        if record.ticker:
            yield record.ticker


def resolve_comparison_product_ids(
    family: ProductFamily,
    mentions: list[str],
    records: Sequence[ProductIdentityRecord],
) -> list[str]:
    if not 2 <= len(mentions) <= 3:
        raise ProductComparisonParseError(
            "비교에는 서로 다른 2~3개 상품의 정확한 식별자가 필요합니다"
        )
    resolved = resolve_exact_product_ids(family, mentions, records)
    if len(set(resolved)) != len(resolved):
        raise ProductComparisonParseError("같은 상품을 서로 비교할 수 없습니다")
    return resolved


def resolve_exact_product_ids(
    family: ProductFamily,
    mentions: Sequence[str],
    records: Sequence[ProductIdentityRecord],
) -> list[str]:
    """Resolve every explicit mention to one non-quarantined canonical product ID."""

    if not mentions:
        raise ProductComparisonParseError("정확한 상품 식별자가 필요합니다")
    alias_index: dict[str, set[str]] = {}
    overseas_base_ticker_index: dict[str, set[str]] = {}
    expected_family = family.value
    for record in records:
        if record.product_family != expected_family or record.is_quarantined:
            continue
        canonical_product_id = (
            record.ticker
            if family is ProductFamily.BOND and record.ticker
            else record.product_id
        )
        for alias in _identity_aliases(family, record):
            normalized = normalize_product_mention(alias)
            if normalized:
                alias_index.setdefault(normalized, set()).add(canonical_product_id)
        if family is ProductFamily.OVERSEAS_ETP and record.ticker:
            base_ticker = _overseas_base_ticker(record.ticker)
            if base_ticker:
                overseas_base_ticker_index.setdefault(base_ticker, set()).add(record.product_id)

    resolved: list[str] = []
    for mention in mentions:
        normalized_mention = normalize_product_mention(mention)
        candidates = sorted(alias_index.get(normalized_mention, set()))
        if (
            not candidates
            and family is ProductFamily.OVERSEAS_ETP
            and "." not in normalized_mention
        ):
            candidates = sorted(overseas_base_ticker_index.get(normalized_mention, set()))
        if not candidates:
            raise ProductIdentityNotFoundError(
                f"상품 식별자 {mention!r}를 제공 데이터에서 정확히 찾지 못했습니다"
            )
        if len(candidates) != 1:
            raise ProductIdentityAmbiguousError(
                f"상품 식별자 {mention!r}가 여러 상품과 일치합니다: {candidates}"
            )
        resolved.append(candidates[0])
    return resolved


def extract_comparison_fields(
    question: str,
    family: ProductFamily,
) -> list[str]:
    for pattern, label in _UNSUPPORTED_PATTERNS.get(family, ()):
        if re.search(pattern, question, flags=re.IGNORECASE):
            raise ProductComparisonParseError(f"{label} 비교는 현재 지원하지 않습니다")
    matches: list[tuple[int, int, str]] = []
    for field_name, patterns in _FIELD_PATTERNS.get(family, ()):
        spans = [
            match.span()
            for pattern in patterns
            for match in re.finditer(pattern, question, flags=re.IGNORECASE)
        ]
        matches.extend((start, end, field_name) for start, end in spans)
    for coordinated in (
        *_COORDINATED_RETURN_PERIOD.finditer(question),
        *_LEADING_RETURN_PERIOD.finditer(question),
    ):
        periods = coordinated.group("periods")
        for token in _RETURN_PERIOD_TOKEN.finditer(periods):
            field_name = _RETURN_FIELD_BY_PERIOD[
                re.sub(r"\s+", "", token.group(0)).casefold()
            ]
            try:
                load_field_registry().require_field(field_name, [family.value])
            except KeyError:
                continue
            matches.append(
                (
                    coordinated.start("periods") + token.start(),
                    coordinated.start("periods") + token.end(),
                    field_name,
                )
            )
    if family is ProductFamily.FUND:
        for coordinated in _COORDINATED_FUND_RATIO.finditer(question):
            assets = coordinated.group("assets")
            for token in _FUND_RATIO_TOKEN.finditer(assets):
                matches.append(
                    (
                        coordinated.start("assets") + token.start(),
                        coordinated.start("assets") + token.end(),
                        _FUND_RATIO_FIELD[re.sub(r"\s+", "", token.group(0))],
                    )
                )
    matches.extend(
        (mention.start, mention.end, mention.field_id)
        for mention in registry_field_mentions(question, family.value, "comparable")
        # BUYABLE_QUANTITY was invalidated in the organizer's 2026-08-24
        # notice.  It remains selectable for provenance but must not regain
        # execution authority through a generic registry-label parser.
        if mention.field_id != "buyable_quantity"
    )
    owned = [
        item
        for item in matches
        if not any(
            other[2] != item[2]
            and other[0] <= item[0]
            and item[1] <= other[1]
            and (other[1] - other[0]) > (item[1] - item[0])
            for other in matches
        )
    ]
    fields = list(
        dict.fromkeys(
            field
            for _, _, field in sorted(
                owned,
                key=lambda item: (item[0], -(item[1] - item[0]), item[2]),
            )
        )
    )
    if (
        family is ProductFamily.DOMESTIC_ETP
        and re.search(r"수익률|성과", question, flags=re.IGNORECASE)
        and not ({*_DOMESTIC_RETURN_FIELDS, "distribution_yield_pct"} & set(fields))
    ):
        raise ProductComparisonParseError(
            "기간이 없는 국내 ETP 수익률 비교는 현재 지원하지 않습니다"
        )
    if (
        family is ProductFamily.OVERSEAS_ETP
        and re.search(r"수익률|성과", question, flags=re.IGNORECASE)
        and "one_day_return_pct" not in fields
    ):
        raise ProductComparisonParseError(
            "해외 ETP 수익률 비교는 제공 데이터의 1일 수익률만 지원합니다"
        )
    if fields:
        return fields
    raise ProductComparisonParseError(
        "비교할 항목을 확인할 수 없습니다. 보수·AUM·수익률·등급처럼 비교 기준을 명시해 주세요"
    )


def compile_product_comparison_plan(
    *,
    question: str,
    question_id: str,
    family: ProductFamily,
    mentions: list[str],
    records: Sequence[ProductIdentityRecord],
) -> QueryPlan:
    if _has_unsafe_target_role(question, mentions, family):
        raise ProductComparisonParseError(
            "제외·대신·포함처럼 비교 대상 역할을 바꾸는 표현은 현재 지원하지 않습니다"
        )
    product_ids = resolve_comparison_product_ids(family, mentions, records)
    field_surface = question
    for mention in mentions:
        field_surface = re.sub(
            re.escape(mention),
            " ",
            field_surface,
            flags=re.IGNORECASE,
        )
    try:
        comparison_fields = extract_comparison_fields(field_surface, family)
    except (KeyError, ProductComparisonParseError):
        if not basic_input_order_comparison_requested(field_surface):
            raise
        comparison_fields = list(_DEFAULT_COMPARISON_FIELDS[family])
    registry = load_field_registry()
    for field_name in comparison_fields:
        definition = registry.require_field(field_name, [family.value])
        if not definition.comparable:
            raise ProductComparisonParseError(
                f"{definition.label}은 현재 비교 가능한 필드가 아닙니다"
            )

    projection = comparison_projection(family, comparison_fields)
    constraints = [
        Constraint(
            field="product_id",
            operator=ConstraintOperator.IN,
            value=product_ids,
            unit=Unit.CODE,
            strength=ConstraintStrength.LOCKED,
        )
    ]
    if family is ProductFamily.FUND:
        constraints.append(
            Constraint(
                field="public_offering",
                operator=ConstraintOperator.EQ,
                value=True,
                unit=Unit.BOOLEAN,
                strength=ConstraintStrength.LOCKED,
            )
        )
    return QueryPlan(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.COMPARE,
        product_families=[family],
        constraints=constraints,
        ranking=[],
        projection=projection,
        limit=len(product_ids),
        intent_payload=IntentPayload(
            comparison_fields=comparison_fields,
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )
