from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection

SemanticOperation = Literal["rank"]


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


_ETP_FAMILIES = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
_ETP_AND_FUND = (*_ETP_FAMILIES, ProductFamily.FUND)


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
        "bond.duration.less_rate_sensitive",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.ASC,
        r"금리\s*(?:변화|변동)에\s*(?:덜|작게)\s*민감",
    ),
    _rule(
        "bond.duration.more_rate_sensitive",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.DESC,
        r"금리\s*(?:변화|변동)에\s*(?:더|크게)\s*민감",
    ),
    _rule(
        "bond.coupon.regular_interest",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.DESC,
        r"정기적으로\s*붙는\s*이자율(?:이|가)?\s*(?:가장\s*)?(?:높|크)",
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
        r"만기까지\s*(?:시간|기간|날|일수)?(?:이|가)?\s*"
        r"(?:가장\s*)?(?:많이\s*)?남(?:은|아|았)",
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
        r"(?:최근\s*)?(?:하루|1\s*일|일간).{0,16}?"
        r"(?:수익률|수익|성과|성적)(?:이|가)?\s*(?:높|좋|큰)",
    ),
    _rule(
        "product.return.one_month_colloquial",
        _ETP_AND_FUND,
        "one_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*달|1\s*개월).{0,20}?"
        r"(?:(?:성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.three_month_colloquial",
        _ETP_AND_FUND,
        "three_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*분기|분기|석\s*달|3\s*개월).{0,24}?"
        r"(?:(?:성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.six_month_colloquial",
        _ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:반년|여섯\s*달|6\s*개월).{0,24}?"
        r"(?:(?:성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.one_year_colloquial",
        _ETP_AND_FUND,
        "one_year_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:1\s*년|일\s*년).{0,24}?"
        r"(?:(?:성적(?:표)?|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "domestic_etp.return.ytd_colloquial",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.DESC,
        r"올해\s*들어.{0,16}?(?:(?:성적|성과)(?:이|가)?\s*(?:좋|높)|잘\s*나간)",
    ),
)


_EXPLICIT_ORDER = re.compile(
    r"(?:오름차순|내림차순|상위|하위|순위|정렬|"
    r"(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)(?!\s*위험).{0,5}?순)",
    re.IGNORECASE,
)
_COMPARATIVE_CUE = re.compile(
    r"(?:가장|제일)\s*(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)|"
    r"(?:높은|낮은|큰|작은|많은|적은|긴|짧은|빠른|먼|좋은|싼)(?!\s*위험)|"
    r"(?:넉넉|풍부|두드러|민감|가벼|저렴|잘\s*나간|활발하게\s*사고\s*팔|"
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
    "SEMANTIC_ALIAS_RULES",
    "SemanticAliasMatch",
    "SemanticAliasRule",
    "explicit_product_name_order",
    "rank_alias_matches",
    "ranking_cue_spans",
    "ranking_intent_present",
]
