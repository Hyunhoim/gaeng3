from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

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
)
from finance_agent_core.storage import ProductIdentityRecord


class ProductComparisonParseError(ValueError):
    """Raised when an exact two-product comparison cannot be compiled safely."""


_FIELD_PATTERNS: dict[ProductFamily, tuple[tuple[str, tuple[str, ...]], ...]] = {
    ProductFamily.OVERSEAS_ETP: (
        ("total_expense_ratio_pct", (r"총\s*보수(?:율)?", r"보수율", r"expense\s*ratio")),
        ("aum", (r"(?<![A-Z])AUM(?![A-Z])", r"순자산", r"운용\s*자산")),
        ("product_type", (r"상품\s*유형", r"ETF\s*ETN\s*구분")),
        ("exchange_code", (r"거래소(?:\s*코드)?",)),
        ("sellable", (r"판매\s*(?:가능\s*)?여부", r"판매\s*상태")),
        ("trading_suspended", (r"거래\s*(?:중지|정지)\s*여부", r"거래\s*상태")),
        ("asset_type", (r"투자\s*자산(?:\s*유형)?", r"자산군")),
        ("investment_region", (r"투자\s*(?:지역|국가)",)),
        ("trading_currency", (r"거래\s*통화", r"표시\s*통화")),
    ),
    ProductFamily.DOMESTIC_ETP: (
        ("one_day_return_pct", (r"1\s*일\s*(?:수익률|성과)", r"일간\s*수익률")),
        ("one_month_return_pct", (r"1\s*개월\s*(?:수익률|성과)", r"월간\s*수익률")),
        ("three_month_return_pct", (r"3\s*개월\s*(?:수익률|성과)",)),
        ("six_month_return_pct", (r"6\s*개월\s*(?:수익률|성과)",)),
        ("one_year_return_pct", (r"1\s*년\s*(?:수익률|성과)", r"연간\s*수익률")),
        ("ytd_return_pct", (r"YTD\s*수익률", r"연초\s*(?:이후|대비)\s*수익률")),
        ("total_expense_ratio_pct", (r"총\s*보수(?:율)?", r"보수율")),
        ("daily_trading_value", (r"일(?:간)?\s*거래대금", r"거래대금")),
        ("leverage_factor", (r"레버리지\s*배수", r"배수")),
        ("close_price", (r"종가", r"마감\s*가격")),
        ("aum", (r"(?<![A-Z])AUM(?![A-Z])", r"순자산", r"운용\s*자산")),
        ("product_type", (r"상품\s*유형", r"ETF\s*ETN\s*구분")),
        ("exchange_code", (r"거래소(?:\s*코드)?",)),
        ("sellable", (r"판매\s*(?:가능\s*)?여부", r"판매\s*상태")),
        ("trading_suspended", (r"거래\s*(?:중지|정지)\s*여부", r"거래\s*상태")),
        ("asset_type", (r"투자\s*자산(?:\s*유형)?", r"자산군")),
        ("investment_region", (r"투자\s*(?:지역|국가)",)),
        ("manager", (r"운용사", r"자산운용사", r"발행사")),
        ("base_index", (r"기초\s*지수", r"추종\s*지수")),
        ("strategy", (r"운용\s*전략", r"복제\s*방식")),
        ("risk_level", (r"위험\s*등급", r"위험도")),
        ("pension_eligible", (r"연금\s*(?:거래\s*)?가능\s*여부",)),
        ("core_etf", (r"핵심\s*ETF\s*여부", r"코어\s*ETF\s*여부")),
        ("trading_currency", (r"거래\s*통화", r"표시\s*통화")),
    ),
    ProductFamily.BOND: (
        ("after_tax_yield_pct", (r"세후\s*수익률",)),
        ("buy_yield_pct", (r"매수\s*수익률", r"세전\s*수익률")),
        ("coupon_rate_pct", (r"표면\s*(?:이율|금리)", r"쿠폰\s*금리")),
        ("issue_amount", (r"발행\s*(?:잔액|금액)",)),
        ("buyable_quantity", (r"매수\s*가능\s*수량", r"주문\s*가능\s*수량")),
        ("remaining_days", (r"잔존\s*일수?", r"만기까지\s*남은\s*일수")),
        ("duration_years", (r"듀레이션",)),
        ("bond_market", (r"채권\s*거래\s*시장", r"장내외\s*구분")),
        ("issuer", (r"발행\s*(?:기관|사|자)",)),
        ("bond_major_class", (r"채권\s*대분류",)),
        ("bond_subclass", (r"채권\s*소분류",)),
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
    ProductFamily.OVERSEAS_ETP: (
        (r"수익률|성과", "해외 ETP 수익률"),
        (r"가격|종가", "해외 ETP 가격"),
    ),
    ProductFamily.DOMESTIC_ETP: (),
    ProductFamily.BOND: ((r"예상\s*수익률|만기\s*수익률", "제공 데이터에 없는 수익률"),),
}
_UNSAFE_TARGET_ROLE_PATTERN = re.compile(
    r"제외(?:하고|한|해)?|빼고|말고|대신|포함",
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


def normalize_product_mention(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


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
    elif family is ProductFamily.BOND:
        if record.ticker:
            yield record.ticker


def resolve_comparison_product_ids(
    family: ProductFamily,
    mentions: list[str],
    records: Sequence[ProductIdentityRecord],
) -> list[str]:
    if len(mentions) != 2:
        raise ProductComparisonParseError("비교에는 서로 다른 두 상품의 정확한 식별자가 필요합니다")
    alias_index: dict[str, set[str]] = {}
    expected_family = family.value
    for record in records:
        if record.product_family != expected_family or record.is_quarantined:
            continue
        for alias in _identity_aliases(family, record):
            normalized = normalize_product_mention(alias)
            if normalized:
                alias_index.setdefault(normalized, set()).add(record.product_id)

    resolved: list[str] = []
    for mention in mentions:
        candidates = sorted(alias_index.get(normalize_product_mention(mention), set()))
        if not candidates:
            raise ProductComparisonParseError(
                f"상품 식별자 {mention!r}를 제공 데이터에서 정확히 찾지 못했습니다"
            )
        if len(candidates) != 1:
            raise ProductComparisonParseError(
                f"상품 식별자 {mention!r}가 여러 상품과 일치합니다: {candidates}"
            )
        resolved.append(candidates[0])
    if len(set(resolved)) != 2:
        raise ProductComparisonParseError("같은 상품을 서로 비교할 수 없습니다")
    return resolved


def extract_comparison_fields(
    question: str,
    family: ProductFamily,
) -> list[str]:
    for pattern, label in _UNSUPPORTED_PATTERNS.get(family, ()):
        if re.search(pattern, question, flags=re.IGNORECASE):
            raise ProductComparisonParseError(f"{label} 비교는 현재 지원하지 않습니다")
    matches: list[tuple[int, str]] = []
    for field_name, patterns in _FIELD_PATTERNS[family]:
        positions = [
            match.start()
            for pattern in patterns
            for match in re.finditer(pattern, question, flags=re.IGNORECASE)
        ]
        if positions:
            matches.append((min(positions), field_name))
    fields = list(dict.fromkeys(field for _, field in sorted(matches)))
    if (
        family is ProductFamily.DOMESTIC_ETP
        and re.search(r"수익률|성과", question, flags=re.IGNORECASE)
        and not _DOMESTIC_RETURN_FIELDS.intersection(fields)
    ):
        raise ProductComparisonParseError(
            "기간이 없는 국내 ETP 수익률 비교는 현재 지원하지 않습니다"
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
    if family is ProductFamily.FUND:
        raise ProductComparisonParseError("공모펀드는 검증된 전용 비교 parser를 사용해야 합니다")
    if _UNSAFE_TARGET_ROLE_PATTERN.search(question):
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
    comparison_fields = extract_comparison_fields(field_surface, family)
    registry = load_field_registry()
    for field_name in comparison_fields:
        definition = registry.require_field(field_name, [family.value])
        if not definition.comparable:
            raise ProductComparisonParseError(
                f"{definition.label}은 현재 비교 가능한 필드가 아닙니다"
            )

    projection = ["product_id", "product_name", "ticker", *comparison_fields]
    if any(
        "trading_currency" in registry.require_field(field_name, [family.value]).comparison_scope
        for field_name in comparison_fields
    ):
        projection.append("trading_currency")
    projection = list(dict.fromkeys(projection))
    return QueryPlan(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.COMPARE,
        product_families=[family],
        constraints=[
            Constraint(
                field="product_id",
                operator=ConstraintOperator.IN,
                value=product_ids,
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            )
        ],
        ranking=[],
        projection=projection,
        limit=2,
        intent_payload=IntentPayload(
            comparison_fields=comparison_fields,
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )
