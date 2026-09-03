from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from finance_agent_core.config.registry import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection

SemanticOperation = Literal["rank"]
FilterOperator = Literal["eq", "neq", "contains"]
TypedAliasOperation = Literal["filter", "rank"]
TYPED_ALIAS_POLICY_VERSION = "typed-alias-v2"


@dataclass(frozen=True, slots=True)
class SemanticAliasRule:
    """Versioned, family-scoped meaning that may enter the deterministic Fast Path."""

    rule_id: str
    families: frozenset[ProductFamily]
    field_id: str
    operation: SemanticOperation
    direction: SortDirection
    pattern: re.Pattern[str]
    policy_version: Literal["semantic-alias-v1"] = "semantic-alias-v1"


@dataclass(frozen=True, slots=True)
class SemanticAliasMatch:
    rule_id: str
    field_id: str
    operation: SemanticOperation
    direction: SortDirection
    text: str
    start: int
    end: int
    policy_version: Literal["semantic-alias-v1"] = "semantic-alias-v1"


@dataclass(frozen=True, slots=True)
class FilterAliasRule:
    """Versioned static financial meaning that produces one locked filter.

    Dynamic grammar such as numbers, dates and product identifiers deliberately
    stays in the parser.  This registry owns only stable domain mappings backed
    by the packaged field registry and the organizer's approved schema.
    """

    rule_id: str
    families: frozenset[ProductFamily]
    field_id: str
    operator: FilterOperator
    value: Any
    pattern: re.Pattern[str]
    forbidden_contexts: tuple[re.Pattern[str], ...] = ()
    evidence_source: str = "field-registry-v1.5"
    policy_version: Literal["typed-alias-v2"] = TYPED_ALIAS_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class FilterAliasMatch:
    rule_id: str
    field_id: str
    operator: FilterOperator
    value: Any
    text: str
    start: int
    end: int
    evidence_source: str
    policy_version: Literal["typed-alias-v2"] = TYPED_ALIAS_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class TypedAliasMatch:
    rule_id: str
    field_id: str
    operation: TypedAliasOperation
    text: str
    start: int
    end: int
    operator: FilterOperator | None = None
    value: Any = None
    direction: SortDirection | None = None
    evidence_source: str = "field-registry-v1.5"
    policy_version: str = TYPED_ALIAS_POLICY_VERSION


def _rule(
    rule_id: str,
    families: tuple[ProductFamily, ...],
    field_id: str,
    direction: SortDirection,
    pattern: str,
) -> SemanticAliasRule:
    return SemanticAliasRule(
        rule_id=rule_id,
        families=frozenset(families),
        field_id=field_id,
        operation="rank",
        direction=direction,
        pattern=re.compile(pattern, re.IGNORECASE),
    )


def _filter_rule(
    rule_id: str,
    families: tuple[ProductFamily, ...],
    field_id: str,
    value: Any,
    pattern: str,
    *,
    operator: FilterOperator = "eq",
    forbidden_contexts: tuple[str, ...] = (),
    evidence_source: str = "field-registry-v1.5",
) -> FilterAliasRule:
    return FilterAliasRule(
        rule_id=rule_id,
        families=frozenset(families),
        field_id=field_id,
        operator=operator,
        value=value,
        pattern=re.compile(pattern, re.IGNORECASE),
        forbidden_contexts=tuple(
            re.compile(context, re.IGNORECASE) for context in forbidden_contexts
        ),
        evidence_source=evidence_source,
    )


_ETP_FAMILIES = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
_ETP_AND_FUND = (*_ETP_FAMILIES, ProductFamily.FUND)
_DOMESTIC_ETP_AND_FUND = (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND)


SEMANTIC_ALIAS_RULES: tuple[SemanticAliasRule, ...] = (
    _rule(
        "bond.issue_amount.market_supply",
        (ProductFamily.BOND,),
        "issue_amount",
        SortDirection.DESC,
        r"(?:시장에\s*풀린\s*규모|발행\s*(?:물량|규모)(?:이|가)?\s*"
        r"(?:가장\s*)?(?:넉넉|큰|크|많))",
    ),
    _rule(
        "bond.buy_yield.purchase_reward",
        (ProductFamily.BOND,),
        "buy_yield_pct",
        SortDirection.DESC,
        r"(?:(?:(?:지금\s*)?(?:샀을|살)\s*때).{0,24}?|"
        r"매수\s*(?:기준(?:으로)?\s*)?)(?:세전\s*)?"
        r"(?:보상|이자\s*수익|수익)",
    ),
    _rule(
        "bond.after_tax_yield.net_reward",
        (ProductFamily.BOND,),
        "after_tax_yield_pct",
        SortDirection.DESC,
        r"(?:세금(?:을)?\s*떼고|세후).{0,24}?(?:실제로?\s*)?"
        r"(?:남는\s*)?(?:보상|수익)",
    ),
    _rule(
        "bond.applied_yield.strong",
        (ProductFamily.BOND,),
        "applied_yield_pct",
        SortDirection.DESC,
        r"(?:민평\s*(?:(?:기준\s*)?수익률|금리)|"
        r"민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리))"
        r"(?:이|가|은|는)?\s*(?:더\s*|가장\s*)?(?:센|높|크)",
    ),
    _rule(
        "bond.applied_yield.weak",
        (ProductFamily.BOND,),
        "applied_yield_pct",
        SortDirection.ASC,
        r"(?:민평\s*(?:(?:기준\s*)?수익률|금리)|"
        r"민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리))"
        r"(?:이|가|은|는)?\s*(?:더\s*|가장\s*)?(?:약|낮|작)",
    ),
    _rule(
        "bond.duration.less_rate_sensitive",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.ASC,
        r"금리\s*(?:(?:변화|변동)?에\s*)?(?:덜|작게)\s*민감|"
        r"금리\s*민감도(?:가|이)?\s*(?:작|낮)",
    ),
    _rule(
        "bond.duration.more_rate_sensitive",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.DESC,
        r"금리\s*(?:(?:변화|변동)?에\s*)?(?:더|크게)\s*민감|"
        r"금리\s*민감도(?:가|이)?\s*(?:크|높)",
    ),
    _rule(
        "bond.coupon.regular_interest",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.DESC,
        r"정기적으로\s*붙는\s*이자율(?:이|가)?\s*(?:가장\s*)?(?:높|크)",
    ),
    _rule(
        "bond.coupon.explicit_high",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.DESC,
        r"(?:액면에?\s*붙은\s*이자율|액면\s*쿠폰|쿠폰(?:\s*금리)?|표면\s*금리)"
        r"(?:이|가|은|는)?\s*(?:더\s*|가장\s*)?(?:높|크)",
    ),
    _rule(
        "bond.coupon.explicit_low",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.ASC,
        r"(?:액면에?\s*붙은\s*이자율|액면\s*쿠폰|쿠폰(?:\s*금리)?|표면\s*금리)"
        r"(?:이|가|은|는)?\s*(?:더\s*|가장\s*)?(?:낮|작)",
    ),
    _rule(
        "bond.maturity.sooner_redemption",
        (ProductFamily.BOND,),
        "maturity_date",
        SortDirection.ASC,
        r"상환\s*시점(?:이|가)?\s*(?:가장\s*)?(?:빨리\s*돌아오|빠르)",
    ),
    _rule(
        "bond.maturity.later_redemption",
        (ProductFamily.BOND,),
        "maturity_date",
        SortDirection.DESC,
        r"상환\s*시점(?:이|가)?\s*(?:가장\s*)?(?:멀|먼|늦)",
    ),
    _rule(
        "bond.remaining_days.time_left",
        (ProductFamily.BOND,),
        "remaining_days",
        SortDirection.DESC,
        r"(?:만기까지\s*(?:시간|기간|날|일수)(?:이|가)?\s*"
        r"(?:가장\s*)?(?:많|길|오래)|"
        r"만기까지\s*(?:가장\s*)?많이\s*남(?:은|아|았)|"
        r"만기까지\s*남은\s*(?:시간|기간|날|일수)(?:이|가)?\s*"
        r"(?:가장\s*)?(?:많|길|크))",
    ),
    _rule(
        "bond.remaining_days.sooner",
        (ProductFamily.BOND,),
        "remaining_days",
        SortDirection.ASC,
        r"(?:(?:상환|만기)(?:이|가|를|를\s*맞는)?\s*(?:가장\s*)?(?:먼저|빨리)|"
        r"(?:가장\s*)?먼저\s*(?:상환|만기)|"
        r"(?:상환|만기)(?:이|가)?\s*(?:가장\s*)?임박|"
        r"(?:상환|만기).{0,16}?가까운|"
        r"(?:상환까지|만기까지)\s*남은\s*(?:날짜|날|일수|기간)(?:이|가)?\s*"
        r"(?:가장\s*)?(?:짧|적|작)|잔여\s*일(?:이|가)?\s*(?:짧|적|작)|"
        r"남은\s*기간(?:이|가)?\s*(?:짧|적|작))",
    ),
    _rule(
        "product.aum.colloquial_size",
        _ETP_AND_FUND,
        "aum",
        SortDirection.DESC,
        r"(?:(?:체급|덩치)(?:이|가)?\s*(?:가장\s*)?(?:큰|높은|크|높)|"
        r"자금(?:이|가)?\s*(?:가장\s*)?많이\s*(?:모인|담긴)|"
        r"운용\s*자산(?:이|가)?\s*(?:풍부|넉넉)|"
        r"시장에서\s*큰\s*축|규모(?:가|이)?\s*두드러)",
    ),
    _rule(
        "domestic_etp.trading_value.turnover",
        (ProductFamily.DOMESTIC_ETP,),
        "daily_trading_value",
        SortDirection.DESC,
        r"(?:활발하게\s*사고\s*팔|사고파는|하루에\s*돈(?:이|가)?\s*많이\s*오가|"
        r"손바뀜(?:이|가)?\s*(?:가장\s*)?많)",
    ),
    _rule(
        "etp.expense.holding_burden",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:(?:들고\s*가는|장기간\s*보유|오래\s*들고\s*있어도|보유|유지|관리|"
        r"운용|운영).{0,20}?(?:비용|부담).{0,16}?(?:작|적|덜|가벼|저렴)|"
        r"보유\s*부담(?:이|가)?\s*(?:덜|작|적|가벼))",
    ),
    _rule(
        "domestic_etp.close_price.share_price",
        (ProductFamily.DOMESTIC_ETP,),
        "close_price",
        SortDirection.ASC,
        r"(?:한\s*주|1\s*주)(?:당)?\s*가격(?:이|가)?\s*(?:가장\s*)?(?:싸|싼|낮)",
    ),
    _rule(
        "etp.return.one_day",
        _ETP_FAMILIES,
        "one_day_return_pct",
        SortDirection.DESC,
        r"(?:최근\s*)?(?:하루|당일|오늘|1\s*일|일간).{0,16}?"
        r"(?:수익률|수익|성과|성적)(?:이|가)?\s*(?:높|좋|큰)",
    ),
    _rule(
        "etp.return.one_day_low",
        _ETP_FAMILIES,
        "one_day_return_pct",
        SortDirection.ASC,
        r"(?:최근\s*)?(?:하루|당일|오늘|1\s*일|일간).{0,16}?"
        r"(?:수익률|수익|성과|성적)(?:이|가)?\s*(?:낮|작)",
    ),
    _rule(
        "product.return.one_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "one_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*달|1\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.three_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "three_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*분기|분기|석\s*달|3\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.six_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:반년|여섯\s*달|6\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.one_year_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "one_year_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:1\s*년|일\s*년).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "domestic_etp.return.ytd_colloquial",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.DESC,
        r"올해\s*들어.{0,16}?(?:(?:성적|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
)


def _literal_filter_rules(
    group_id: str,
    families: tuple[ProductFamily, ...],
    mappings: tuple[tuple[str, str, Any, tuple[str, ...]], ...],
) -> tuple[FilterAliasRule, ...]:
    return tuple(
        _filter_rule(
            f"{group_id}.{field_id}.{index:02d}",
            families,
            field_id,
            value,
            re.escape(surface),
            forbidden_contexts=forbidden_contexts,
        )
        for index, (surface, field_id, value, forbidden_contexts) in enumerate(
            mappings,
            start=1,
        )
    )


_DOMESTIC_ETP_FILTER_ALIASES = _literal_filter_rules(
    "domestic_etp",
    (ProductFamily.DOMESTIC_ETP,),
    (
        ("혼합자산형", "asset_type", "혼합자산", ()),
        ("혼합자산 국내", "asset_type", "혼합자산", ()),
        ("단기자금형", "asset_type", "단기자금", ()),
        ("원자재형", "asset_type", "원자재", ()),
        ("원자재 ETF", "asset_type", "원자재", ()),
        ("대체투자 자산군", "asset_type", "대체투자", ()),
        ("부동산형", "asset_type", "부동산", ()),
        ("주식형", "asset_type", "주식", ()),
        ("채권형", "asset_type", "채권", ()),
        ("통화형", "asset_type", "통화", ()),
        ("이머징/브릭스", "investment_region", "이머징/브릭스", ()),
        ("남미/북미", "investment_region", "남미/북미", ()),
        ("글로벌", "investment_region", "글로벌", ()),
        ("아시아", "investment_region", "아시아", ()),
        ("베트남", "investment_region", "베트남", ()),
        ("미국", "investment_region", "미국", (r"미국\s*제외\s*글로벌", r"Global\s*Ex\s*US")),
        ("일본", "investment_region", "일본", ()),
        ("중국", "investment_region", "중국", ()),
        ("유럽", "investment_region", "유럽", ()),
        ("인도", "investment_region", "인도", ()),
        ("원화", "trading_currency", "KRW", ()),
        ("KRW", "trading_currency", "KRW", ()),
    ),
)

_FUND_FILTER_ALIASES = _literal_filter_rules(
    "fund",
    (ProductFamily.FUND,),
    (
        ("국내외 혼합", "fund_geography_scope", "국내외혼합", ()),
        ("국내외혼합", "fund_geography_scope", "국내외혼합", ()),
        (
            "해외",
            "fund_geography_scope",
            "해외",
            (
                r"해외\s*펀드\s*구분(?:이|은|가)?\s*(?:국내|해외)",
                r"해외\s*(?:주식|채권)\s*(?:구성\s*비율|비중)",
            ),
        ),
        (
            "국내",
            "fund_geography_scope",
            "국내",
            (
                r"국내외",
                r"국내(?:에|로)?\s*투자",
                r"해외\s*펀드\s*구분(?:이|은|가)?\s*(?:국내|해외)",
                r"국내\s*(?:주식|채권)\s*(?:구성\s*비율|비중)",
            ),
        ),
        ("주식혼합", "fund_management_attribute", "주식혼합", ()),
        ("채권혼합", "fund_management_attribute", "채권혼합", ()),
        ("혼합자산", "fund_management_attribute", "혼합자산", ()),
        ("특별자산", "fund_management_attribute", "특별자산", ()),
        ("재간접", "fund_management_attribute", "재간접", ()),
        ("파생상품", "fund_management_attribute", "파생상품", ()),
        ("주식형", "fund_management_attribute", "주식형", ()),
        ("채권형", "fund_management_attribute", "채권형", ()),
        ("대출형", "fund_management_attribute", "대출형", ()),
        ("임대형", "fund_management_attribute", "임대형", ()),
        ("MMF", "fund_management_attribute", "MMF", ()),
        ("글로벌", "investment_region", "글로벌", ()),
        ("아시아", "investment_region", "아시아", ()),
        ("남미/북미", "investment_region", "남미/북미", ()),
        ("이머징/브릭스", "investment_region", "이머징/브릭스", ()),
        ("유럽", "investment_region", "유럽", ()),
        ("원화", "trading_currency", "KRW", ()),
        ("KRW", "trading_currency", "KRW", ()),
        ("달러", "trading_currency", "USD", ()),
        ("USD", "trading_currency", "USD", ()),
    ),
)

_BOND_FILTER_ALIASES = _literal_filter_rules(
    "bond",
    (ProductFamily.BOND,),
    (
        ("개인투자용국채", "bond_major_class", "개인투자용국채", ()),
        (
            "회사채",
            "bond_major_class",
            "회사채",
            (r"(?:일반|유동화|금융지주|보험)회사채",),
        ),
        ("특수채", "bond_major_class", "특수채", ()),
        ("국공채", "bond_major_class", "국공채", ()),
        ("국고채", "bond_subclass", "국고채", ()),
        ("일반사채", "bond_subclass", "일반사채", ()),
        ("특수은행채", "bond_type", "특수은행채", ()),
        ("공사채", "bond_subclass", "공사채", ()),
        ("은행채", "bond_subclass", "은행채", (r"특수은행채",)),
        ("중앙은행채", "bond_subclass", "중앙은행채", ()),
        ("MBS", "bond_type", "MBS", ()),
        ("시설대여채", "bond_type", "시설대여채(리스)", ()),
        ("지역개발채", "bond_type", "지역개발채", ()),
        ("도시철도공채", "bond_type", "도시철도공채", ()),
        ("금융지주회사채", "bond_type", "금융지주회사채", ()),
        ("보험회사채", "bond_type", "보험회사채", ()),
        ("일반회사채", "bond_type", "일반회사채", ()),
        ("유동화회사채", "bond_type", "유동화회사채", ()),
        ("신용카드채", "bond_type", "신용카드채", ()),
        ("할부금융채", "bond_type", "할부금융채", ()),
        ("장내", "bond_market", "장내", ()),
        ("장외", "bond_market", "장외", ()),
        ("원화", "trading_currency", "KRW", ()),
        ("KRW", "trading_currency", "KRW", ()),
        ("달러", "trading_currency", "USD", ()),
    ),
)

_OVERSEAS_ETP_FILTER_ALIASES = _literal_filter_rules(
    "overseas_etp",
    (ProductFamily.OVERSEAS_ETP,),
    (
        ("머니마켓", "asset_type", "Money Market", ()),
        ("혼합자산형", "asset_type", "Mixed Assets", ()),
        ("대체자산형", "asset_type", "Alternatives", ()),
        ("원자재형", "asset_type", "Commodity", ()),
        ("주식형", "asset_type", "Equity", ()),
        ("채권", "asset_type", "Bond", ()),
        ("글로벌 신흥국", "investment_region", "Global Emerging Markets", ()),
        ("미국 제외 글로벌", "investment_region", "Global Ex US", ()),
        (
            "미국",
            "investment_region",
            "United States of America",
            (r"미국\s*제외\s*글로벌", r"Global\s*Ex\s*US"),
        ),
        ("일본", "investment_region", "Japan", ()),
        ("중국", "investment_region", "China", ()),
        ("유럽", "investment_region", "Europe", ()),
        ("NASDAQ", "exchange_code", "NAS", ()),
        ("NYSE", "exchange_code", "NYS", ()),
        ("AMEX", "exchange_code", "AMX", ()),
        (
            "달러",
            "trading_currency",
            "USD",
            (r"(?:펀드|상품)\s*통화\s*코드(?:가|는|은|이)?\s*[A-Z]{3}",),
        ),
        (
            "USD",
            "trading_currency",
            "USD",
            (r"(?:펀드|상품)\s*통화\s*코드(?:가|는|은|이)?\s*[A-Z]{3}",),
        ),
    ),
)

FILTER_ALIAS_RULES: tuple[FilterAliasRule, ...] = (
    *_DOMESTIC_ETP_FILTER_ALIASES,
    *_FUND_FILTER_ALIASES,
    *_BOND_FILTER_ALIASES,
    *_OVERSEAS_ETP_FILTER_ALIASES,
)


_EXPLICIT_ORDER = re.compile(
    r"(?:오름차순|내림차순|상위|하위|순위|정렬|"
    r"\d+\s*(?:등(?!급)|위(?!험))|"
    r"(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)(?!\s*위험).{0,5}?순)",
    re.IGNORECASE,
)
_COMPARATIVE_CUE = re.compile(
    r"(?:가장|제일)\s*(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)|"
    r"(?:높은|낮은|큰|작은|많은|적은|긴|짧은|빠른|먼|좋은|싼)(?!\s*위험)|"
    # "민감도" is the name of a field (duration), not a comparison cue. Only the
    # adjectival "금리에 민감한" form asks for a ranking.
    r"(?:넉넉|풍부|두드러|민감(?!도)|가벼|저렴|잘\s*나간|활발하게\s*사고\s*팔|"
    r"손바뀜|돈(?:이|가)?\s*많이\s*오가|인기|뜨거운|우수한|알짜|"
    r"안정적|안전한)",
    re.IGNORECASE,
)
_EXPLICIT_NAME_ORDER = re.compile(
    r"(?:상품명|이름|짧은\s*이름|티커|종목\s*코드).{0,24}?"
    r"(?:순서|순으로|오름차순|내림차순|이름순|가나다순)",
    re.IGNORECASE,
)


def rank_alias_matches(
    question: str,
    product_family: ProductFamily | str | None = None,
) -> tuple[SemanticAliasMatch, ...]:
    family = None if product_family is None else ProductFamily(product_family)
    matches: list[SemanticAliasMatch] = []
    seen: set[tuple[str, int, int]] = set()
    for rule in SEMANTIC_ALIAS_RULES:
        if family is not None and family not in rule.families:
            continue
        for match in rule.pattern.finditer(question):
            key = (rule.rule_id, match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                SemanticAliasMatch(
                    rule_id=rule.rule_id,
                    field_id=rule.field_id,
                    operation=rule.operation,
                    direction=rule.direction,
                    text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
    return tuple(sorted(matches, key=lambda item: (item.start, item.end, item.rule_id)))


def filter_alias_matches(
    question: str,
    product_family: ProductFamily | str | None = None,
) -> tuple[FilterAliasMatch, ...]:
    family = None if product_family is None else ProductFamily(product_family)
    matches: list[FilterAliasMatch] = []
    seen: set[tuple[str, int, int]] = set()
    for rule in FILTER_ALIAS_RULES:
        if family is not None and family not in rule.families:
            continue
        if any(pattern.search(question) is not None for pattern in rule.forbidden_contexts):
            continue
        for match in rule.pattern.finditer(question):
            key = (rule.rule_id, match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                FilterAliasMatch(
                    rule_id=rule.rule_id,
                    field_id=rule.field_id,
                    operator=rule.operator,
                    value=rule.value,
                    text=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    evidence_source=rule.evidence_source,
                )
            )
    # Registry order is part of the deterministic QueryPlan contract: broad
    # field groups (for example asset type before region) retain the same
    # constraint order across releases even when their surface order differs.
    return tuple(matches)


def typed_alias_matches(
    question: str,
    product_family: ProductFamily | str | None = None,
) -> tuple[TypedAliasMatch, ...]:
    matches = [
        TypedAliasMatch(
            rule_id=item.rule_id,
            field_id=item.field_id,
            operation="rank",
            direction=item.direction,
            text=item.text,
            start=item.start,
            end=item.end,
            policy_version=item.policy_version,
        )
        for item in rank_alias_matches(question, product_family)
    ]
    matches.extend(
        TypedAliasMatch(
            rule_id=item.rule_id,
            field_id=item.field_id,
            operation="filter",
            operator=item.operator,
            value=item.value,
            text=item.text,
            start=item.start,
            end=item.end,
            evidence_source=item.evidence_source,
            policy_version=item.policy_version,
        )
        for item in filter_alias_matches(question, product_family)
    )
    return tuple(sorted(matches, key=lambda item: (item.start, item.end, item.rule_id)))


def validate_typed_alias_registry() -> None:
    field_registry = load_field_registry()
    rule_ids = [
        *(rule.rule_id for rule in SEMANTIC_ALIAS_RULES),
        *(rule.rule_id for rule in FILTER_ALIAS_RULES),
    ]
    if len(rule_ids) != len(set(rule_ids)):
        raise RuntimeError("typed alias registry rule IDs must be unique")
    for rule in FILTER_ALIAS_RULES:
        if not rule.families or not rule.field_id or rule.value is None:
            raise RuntimeError(f"typed filter alias rule is incomplete: {rule.rule_id}")
        if not rule.evidence_source or rule.policy_version != TYPED_ALIAS_POLICY_VERSION:
            raise RuntimeError(f"typed filter alias provenance is incomplete: {rule.rule_id}")
        for family in rule.families:
            definition = field_registry.require_field(rule.field_id, [family.value])
            if not definition.queryable:
                raise RuntimeError(f"typed filter alias field is not queryable: {rule.rule_id}")
    for rule in SEMANTIC_ALIAS_RULES:
        for family in rule.families:
            definition = field_registry.require_field(rule.field_id, [family.value])
            if not definition.sortable:
                raise RuntimeError(f"typed rank alias field is not sortable: {rule.rule_id}")


validate_typed_alias_registry()


def ranking_cue_spans(question: str) -> tuple[str, ...]:
    matches = [
        *list(_EXPLICIT_ORDER.finditer(question)),
        *list(_COMPARATIVE_CUE.finditer(question)),
    ]
    return tuple(
        dict.fromkeys(
            match.group(0).strip()
            for match in sorted(matches, key=lambda item: (item.start(), item.end()))
            if match.group(0).strip()
        )
    )


def ranking_intent_present(question: str) -> bool:
    return bool(ranking_cue_spans(question))


def explicit_product_name_order(question: str) -> bool:
    return _EXPLICIT_NAME_ORDER.search(question) is not None


__all__ = [
    "FILTER_ALIAS_RULES",
    "SEMANTIC_ALIAS_RULES",
    "FilterAliasMatch",
    "FilterAliasRule",
    "SemanticAliasMatch",
    "SemanticAliasRule",
    "TYPED_ALIAS_POLICY_VERSION",
    "TypedAliasMatch",
    "explicit_product_name_order",
    "filter_alias_matches",
    "rank_alias_matches",
    "ranking_cue_spans",
    "ranking_intent_present",
    "typed_alias_matches",
    "validate_typed_alias_registry",
]
