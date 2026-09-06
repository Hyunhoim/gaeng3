from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from finance_agent_core.config import ValueType
from finance_agent_core.config.registry import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection

SemanticOperation = Literal["rank"]
FilterOperator = Literal["eq", "neq", "in", "not_in", "contains"]
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
class NumericFieldReferenceRule:
    """Family-scoped field names that may own an adjacent numeric bound.

    These aliases select only the field referenced by an explicit number and
    comparison operator.  They never create a value or operator themselves,
    which keeps colloquial field names in the shared typed registry without
    granting a bare word SQL authority.
    """

    rule_id: str
    families: frozenset[ProductFamily]
    field_id: str
    aliases: tuple[str, ...]
    evidence_source: str = "field-registry-v1.5"
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


def _two_stage_rank_rules(
    group_id: str,
    families: tuple[ProductFamily, ...],
    field_id: str,
    field_pattern: str,
) -> tuple[SemanticAliasRule, SemanticAliasRule]:
    """Own an explicit first-stage metric together with its transition cue.

    This grammar is intentionally field-labelled: it never infers a metric
    from a bare adjective.  Covering ``먼저`` as part of the typed span keeps a
    valid two-stage request from being mistaken for an unexplained direction,
    while the compound compiler still owns both limits and the final ranking.
    """

    count = r"(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
    prefix = rf"(?:먼저|우선)\s*(?:{field_pattern})(?:이|가|은|는|을|를)?\s*"
    suffix = (
        rf"(?:{field_pattern})(?:이|가|은|는|을|를)?\s*"
        rf"(?P<direction>{{direction}})\s*{count}\s*(?:개|종목|상품)(?:를|을)?\s*"
        r"(?:먼저|우선)"
    )
    return (
        _rule(
            f"{group_id}.two_stage_high",
            families,
            field_id,
            SortDirection.DESC,
            prefix + r"(?:큰|높은|많은|상위)|" + suffix.format(direction="상위"),
        ),
        _rule(
            f"{group_id}.two_stage_low",
            families,
            field_id,
            SortDirection.ASC,
            prefix + r"(?:작은|낮은|적은|하위|짧은|빠른)|"
            + suffix.format(direction="하위"),
        ),
    )


def _referential_rank_rules(
    group_id: str,
    families: tuple[ProductFamily, ...],
    field_id: str,
    field_pattern: str,
    reference_pattern: str,
) -> tuple[SemanticAliasRule, SemanticAliasRule]:
    """Bind a later adjective to one explicitly named metric.

    Natural questions often name a field, add an availability condition, and
    refer to it later as ``그 값`` or ``비중``.  The field name is mandatory,
    so this does not grant a bare adjective any execution authority.
    """

    # A reference must either be explicitly anaphoric (``그 값``) or begin at
    # a lexical boundary.  Without this boundary, the bare alternative ``값``
    # could match the suffix of a different metric such as ``시장체력값`` and
    # wrongly let an earlier numeric filter steal the later ranking cue.
    reference = (
        rf"(?:(?:그|해당)\s*(?:{reference_pattern})|"
        rf"(?<![0-9A-Za-z가-힣])(?:{reference_pattern}))"
    )
    stem = (
        rf"(?:{field_pattern}).{{0,64}}?{reference}"
        r"(?:이|가|은|는|을|를)?\s*(?:(?:중|가운데)(?:에서|서)?\s*)?"
        r"(?:가장\s*)?"
    )
    return (
        _rule(
            f"{group_id}.referential_high",
            families,
            field_id,
            SortDirection.DESC,
            stem + r"(?:높|크|큰|많)",
        ),
        _rule(
            f"{group_id}.referential_low",
            families,
            field_id,
            SortDirection.ASC,
            stem + r"(?:낮|작|적|싼|싸|저렴)",
        ),
    )


_ETP_FAMILIES = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
_ETP_AND_FUND = (*_ETP_FAMILIES, ProductFamily.FUND)
_DOMESTIC_ETP_AND_FUND = (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND)


NUMERIC_FIELD_REFERENCE_RULES: tuple[NumericFieldReferenceRule, ...] = (
    NumericFieldReferenceRule(
        rule_id="product.aum.colloquial_numeric_reference",
        families=frozenset(_ETP_AND_FUND),
        field_id="aum",
        aliases=("체급", "덩치", "몸집"),
    ),
)


SEMANTIC_ALIAS_RULES: tuple[SemanticAliasRule, ...] = (
    *_two_stage_rank_rules(
        "product.aum",
        _ETP_AND_FUND,
        "aum",
        r"AUM|순자산|운용\s*(?:자산|규모)",
    ),
    *_two_stage_rank_rules(
        "domestic_etp.daily_trading_value",
        (ProductFamily.DOMESTIC_ETP,),
        "daily_trading_value",
        r"(?:일|하루|일간)?\s*거래대금|일거래대금",
    ),
    *_two_stage_rank_rules(
        "etp.daily_volume",
        _ETP_FAMILIES,
        "daily_volume",
        r"(?:일|하루|일간)?\s*거래량",
    ),
    *_two_stage_rank_rules(
        "bond.issue_amount",
        (ProductFamily.BOND,),
        "issue_amount",
        r"발행금액|발행\s*금액|발행액|발행잔액|남은\s*발행\s*물량",
    ),
    *_two_stage_rank_rules(
        "bond.remaining_days",
        (ProductFamily.BOND,),
        "remaining_days",
        r"잔존\s*일수|잔여\s*일수|남은\s*기간",
    ),
    *_two_stage_rank_rules(
        "bond.coupon_rate",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        r"표면\s*(?:금리|이율)|쿠폰\s*금리",
    ),
    *_two_stage_rank_rules(
        "product.one_year_return",
        _DOMESTIC_ETP_AND_FUND,
        "one_year_return_pct",
        r"1\s*년\s*(?:수익률|수익|성과)|연간\s*(?:수익률|수익|성과)",
    ),
    *_referential_rank_rules(
        "etp.total_expense",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        r"총\s*보수(?:율)?|연간\s*보수(?:율)?",
        r"보수|비율|값|비용|상품",
    ),
    *_referential_rank_rules(
        "fund.sale_company_fee.availability",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        r"판매\s*(?:회사\s*)?보수|판매보수",
        r"보수|비율|값|비용|상품",
    ),
    *_referential_rank_rules(
        "bond.coupon_rate.referential",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        r"표면\s*(?:금리|이율)|쿠폰\s*금리",
        r"금리|이율|값",
    ),
    *_referential_rank_rules(
        "bond.buy_yield.referential",
        (ProductFamily.BOND,),
        "buy_yield_pct",
        r"(?:세전\s*)?매수\s*수익률|매수\s*수익",
        r"수익률|수익|값",
    ),
    *_referential_rank_rules(
        "bond.after_tax_yield.referential",
        (ProductFamily.BOND,),
        "after_tax_yield_pct",
        r"세후\s*(?:매수\s*)?수익률|세금\s*(?:공제|차감)\s*후\s*수익",
        r"수익률|수익|값",
    ),
    *_referential_rank_rules(
        "product.aum.referential",
        _ETP_AND_FUND,
        "aum",
        r"순\s*자산(?:\s*총액)?|AUM|자산\s*규모",
        r"순\s*자산|AUM|규모|값",
    ),
    *(
        rule
        for group_id, field_id, field_pattern in (
            (
                "fund.domestic_stock_ratio",
                "domestic_stock_ratio_pct",
                r"국내\s*주식\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))",
            ),
            (
                "fund.domestic_bond_ratio",
                "domestic_bond_ratio_pct",
                r"국내\s*채권\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))",
            ),
            (
                "fund.overseas_stock_ratio",
                "overseas_stock_ratio_pct",
                r"해외\s*주식\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))",
            ),
            (
                "fund.overseas_bond_ratio",
                "overseas_bond_ratio_pct",
                r"해외\s*채권\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))",
            ),
        )
        for rule in _referential_rank_rules(
            group_id,
            (ProductFamily.FUND,),
            field_id,
            field_pattern,
            r"비중|비율|편입비|값",
        )
    ),
    _rule(
        "bond.issue_amount.explicit_high",
        (ProductFamily.BOND,),
        "issue_amount",
        SortDirection.DESC,
        r"(?:발행\s*금액|발행액|발행잔액)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*)?(?:큰|높은|많은)\s*(?:순|순서)?",
    ),
    _rule(
        "bond.issue_amount.explicit_low",
        (ProductFamily.BOND,),
        "issue_amount",
        SortDirection.ASC,
        r"(?:발행\s*금액|발행액|발행잔액)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*)?(?:작은|낮은|적은)\s*(?:순|순서)?",
    ),
    _rule(
        "etp.return.one_day_ordinal_high",
        _ETP_FAMILIES,
        "one_day_return_pct",
        SortDirection.DESC,
        r"(?:하루|당일|오늘|1\s*일|일간).{0,12}?"
        r"(?:수익률|수익|성과|성적|상승률|등락률)(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:상위|내림차순)",
    ),
    _rule(
        "etp.return.one_day_ordinal_low",
        _ETP_FAMILIES,
        "one_day_return_pct",
        SortDirection.ASC,
        r"(?:하루|당일|오늘|1\s*일|일간).{0,12}?"
        r"(?:수익률|수익|성과|성적|상승률|등락률)(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:하위|오름차순)",
    ),
    _rule(
        "product.risk_level.low_first",
        (ProductFamily.BOND, ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
        "risk_level",
        SortDirection.ASC,
        r"(?:위험\s*(?:수준|도|단계|등급)|리스크)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:낮|적|덜)",
    ),
    _rule(
        "product.risk_level.high_first",
        (ProductFamily.BOND, ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
        "risk_level",
        SortDirection.DESC,
        r"(?:위험\s*(?:수준|도|단계|등급)|리스크)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:높|크|많)",
    ),
    _rule(
        "bond.issue_amount.market_supply",
        (ProductFamily.BOND,),
        "issue_amount",
        SortDirection.DESC,
        r"시장에\s*풀린\s*규모(?:가|이)?\s*(?:가장\s*)?(?:넉넉|큰|크|많)",
    ),
    _rule(
        "bond.total_issue_amount.issued_supply",
        (ProductFamily.BOND,),
        "total_issue_amount",
        SortDirection.DESC,
        r"(?:(?:처음|최초|발행\s*당시)\s*발행(?:한|된)?\s*"
        r"(?:총액|금액|물량|규모)|총\s*발행\s*(?:금액|액|물량|규모)|"
        r"(?<!총\s)(?<!총)발행\s*(?:물량|규모))"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:넉넉|큰|크|많|높)",
    ),
    _rule(
        "bond.buy_yield.purchase_reward",
        (ProductFamily.BOND,),
        "buy_yield_pct",
        SortDirection.DESC,
        # ``세후 매수수익률`` is its own registry field. Without this guard the
        # pre-tax rule claimed the phrase and answered with the wrong yield.
        r"(?<!세후)(?<!세후\s)"
        r"(?:(?:(?:지금\s*)?(?:샀을|살)\s*때).{0,24}?|"
        r"매수\s*(?:기준(?:으로)?\s*)?|"
        r"(?:지금\s*)?사는\s*가격(?:을|이)?\s*반영(?:한|해서|하여)?\s*)(?:세전\s*)?"
        r"(?:보상|이자\s*수익|수익률?|이자)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?"
        r"(?:높|큰|크|많|좋|상위|내림차순|순)",
    ),
    _rule(
        "bond.buy_yield.expected_pretax",
        (ProductFamily.BOND,),
        "buy_yield_pct",
        SortDirection.DESC,
        r"매수\s*시\s*(?:기대되는\s*)?세전\s*(?:수익률|수익|보상)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:높|큰|크|많|상위|내림차순|순)",
    ),
    _rule(
        "bond.buy_yield.purchase_price_reflected",
        (ProductFamily.BOND,),
        "buy_yield_pct",
        SortDirection.DESC,
        r"(?:현재|지금)?\s*매수\s*가격(?:을|이)?\s*반영(?:한|해서|하여)?"
        r".{0,16}?세전\s*(?:보상|수익률?|이자)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:높|크|큰)",
    ),
    _rule(
        "bond.after_tax_yield.purchase_price_net_share_high",
        (ProductFamily.BOND,),
        "after_tax_yield_pct",
        SortDirection.DESC,
        r"(?:(?:현재|지금)\s*)?(?:매입|매수)\s*가격(?:을|이)?\s*"
        r"반영(?:한|해서|하여)?\s*(?:세금(?:을)?\s*뗀\s*뒤의?\s*|세후\s*)"
        r"(?:몫|실수령(?:액|\s*보상)?|순수익)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:크|큰|높|많)",
    ),
    _rule(
        "bond.after_tax_yield.net_reward",
        (ProductFamily.BOND,),
        "after_tax_yield_pct",
        SortDirection.DESC,
        r"(?:세금(?:을)?\s*떼고|세후).{0,24}?(?:실제로?\s*)?"
        r"(?:남는\s*)?(?:매수\s*)?(?:보상|수익)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:높|큰|크|많|상위|내림차순|순)",
    ),
    _rule(
        "bond.after_tax_yield.tax_deduction",
        (ProductFamily.BOND,),
        "after_tax_yield_pct",
        SortDirection.DESC,
        r"매수\s*후.{0,16}?세금\s*(?:공제|차감)(?:한|된)?\s*(?:기준\s*)?"
        r"(?:수익률?|보상)(?:이|가|은|는)?\s*(?:가장\s*)?(?:높|크|큰)",
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
        "bond.duration.less_rate_fluctuation",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.ASC,
        r"금리\s*(?:변화|변동)에\s*(?:상대적으로\s*)?"
        r"(?:덜|적게|작게)\s*(?:흔들리|움직이|반응)",
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
        "bond.duration.more_rate_fluctuation",
        (ProductFamily.BOND,),
        "duration_years",
        SortDirection.DESC,
        r"금리\s*(?:변화|변동)에\s*(?:상대적으로\s*)?"
        r"(?:더|많이|크게)\s*(?:흔들리|움직이|반응)",
    ),
    _rule(
        "bond.coupon.regular_interest",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.DESC,
        r"(?:정기적으로\s*붙는|정기)\s*이자율(?:이|가|은|는)?\s*"
        r"(?:더\s*|가장\s*)?(?:높|크)",
    ),
    _rule(
        "bond.coupon.regular_interest_low",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.ASC,
        r"(?:정기적으로\s*붙는|정기)\s*이자율(?:이|가|은|는)?\s*"
        r"(?:더\s*|가장\s*)?(?:낮|작)",
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
        "bond.coupon.descriptive_low",
        (ProductFamily.BOND,),
        "coupon_rate_pct",
        SortDirection.ASC,
        r"(?:(?:액면에\s*)?(?:정기적으로\s*)?붙는\s*이자(?:율)?|"
        r"표면에\s*(?:적힌|표시된|기재된)\s*이율)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:낮|작)",
    ),
    _rule(
        "bond.maturity.sooner_redemption",
        (ProductFamily.BOND,),
        "maturity_date",
        SortDirection.ASC,
        r"상환\s*시점(?:이|가)?\s*(?:가장\s*)?(?:빨리\s*돌아오|빠르)",
    ),
    _rule(
        "bond.maturity.repayment_day_near",
        (ProductFamily.BOND,),
        "maturity_date",
        SortDirection.ASC,
        r"갚는\s*날(?:이|가)?\s*(?:가장\s*)?(?:가까|빠르|먼저)",
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
        r"상환\s*시점(?:이|가)?\s*(?:바로\s*)?코앞|"
        r"(?:상환|만기).{0,16}?가까운|"
        r"(?:상환까지|만기까지)\s*남은\s*(?:날짜|날|일수|기간)(?:이|가)?\s*"
        r"(?:가장\s*)?(?:짧|적|작)|"
        r"(?:상환까지|만기까지)\s*(?:가장\s*)?(?:짧|적|작)(?:게|은)?\s*남|"
        r"잔여\s*일(?:이|가)?\s*(?:짧|적|작)|"
        r"남은\s*기간(?:이|가)?\s*(?:짧|적|작))",
    ),
    _rule(
        "product.aum.colloquial_size",
        _ETP_AND_FUND,
        "aum",
        SortDirection.DESC,
        r"(?:(?:체급|덩치|몸집)(?:이|가)?\s*(?:가장\s*)?(?:큰|높은|크|높)|"
        r"자금(?:이|가)?\s*(?:가장\s*)?많이\s*(?:모인|담긴)|"
        r"자산(?:을|를|이|가)?\s*(?:가장\s*)?많이\s*"
        r"(?:끌어\s*모은|모은|유치한)|"
        r"운용\s*자산(?:이|가)?\s*(?:풍부|넉넉)|"
        r"시장에서\s*큰\s*축|규모(?:가|이)?\s*두드러|"
        r"(?:각자(?:의)?\s*)?(?:운용\s*)?규모(?:를|가|이)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:큰|높|상위))",
    ),
    _rule(
        "product.aum.colloquial_size_small",
        _ETP_AND_FUND,
        "aum",
        SortDirection.ASC,
        r"(?:체급|덩치|몸집)(?:이|가)?\s*(?:가장\s*)?(?:작|낮|가벼)",
    ),
    _rule(
        "domestic_etp.trading_value.turnover",
        (ProductFamily.DOMESTIC_ETP,),
        "daily_trading_value",
        SortDirection.DESC,
        r"(?:활발하게\s*사고\s*팔|사고파는|"
        r"(?:매일|하루(?:에)?|일간)?\s*돈(?:이|가)?\s*"
        r"(?:많이|활발히|활발하게)\s*오가|"
        r"손바뀜(?:이|가)?\s*(?:가장\s*)?(?:많|활발))",
    ),
    _rule(
        "overseas_etp.volume.turnover",
        (ProductFamily.OVERSEAS_ETP,),
        "daily_volume",
        SortDirection.DESC,
        r"손바뀜(?:이|가)?\s*(?:가장\s*)?(?:많|활발)|"
        r"활발하게\s*사고\s*팔(?:리|리는)",
    ),
    _rule(
        "overseas_etp.volume.executed_trades",
        (ProductFamily.OVERSEAS_ETP,),
        "daily_volume",
        SortDirection.DESC,
        r"거래(?:가|는|이)?\s*(?:많이|활발하게)\s*(?:된|이뤄진|체결된)"
        r"(?:\s*(?:것|상품|종목))?(?:이|가|은|는|을|를)?\s*(?:순|순서)",
    ),
    _rule(
        "domestic_etp.trading_value.money_flow",
        (ProductFamily.DOMESTIC_ETP,),
        "daily_trading_value",
        SortDirection.DESC,
        r"(?:(?:매일|하루(?:에)?|일간)\s*)?돈(?:이|가)?\s*"
        r"(?:많이|활발히|활발하게)\s*오가",
    ),
    _rule(
        "domestic_etp.trading_value.explicit_activity",
        (ProductFamily.DOMESTIC_ETP,),
        "daily_trading_value",
        SortDirection.DESC,
        r"(?:(?:하루|일간?)\s*)?(?:거래\s*대금|자금\s*거래)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:활발|크|큰|많)",
    ),
    _rule(
        "overseas_etp.volume.explicit_activity",
        (ProductFamily.OVERSEAS_ETP,),
        "daily_volume",
        SortDirection.DESC,
        r"(?:(?:하루|일간?)\s*)?(?:거래\s*(?:주식\s*)?수|거래량)"
        r"(?:이|가|은|는)?\s*(?:기준(?:으로)?\s*)?"
        r"(?:가장\s*)?(?:활발|크|큰|많)",
    ),
    _rule(
        "etp.expense.holding_burden",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:(?:들고\s*가는|장기간\s*보유|오래\s*들고\s*있어도|보유|유지|관리|"
        r"운용|운영).{0,20}?(?:비용|부담).{0,16}?(?:작|적|덜|가벼|저렴)|"
        r"(?:보유\s*중\s*)?(?:계속\s*)?(?:드는\s*)?운용비(?:가|는|이)?\s*"
        r"(?:싸|싼|낮|적|저렴)|"
        r"(?:총\s*보수(?:율)?|연간\s*보수율).{0,20}?(?:비용|부담)(?:이|가)?\s*"
        r"(?:작|적|덜|가벼|저렴)|"
        r"보유\s*부담(?:이|가)?\s*(?:낮|덜|작|적|가벼)|"
        r"(?:수수료|비용)\s*부담(?:이|가)?\s*(?:낮|덜|작|적|가벼))",
    ),
    _rule(
        "etp.expense.explicit_holding_cost",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:(?:보유|유지|관리|운용|운영)\s*)?비용(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:싸|싼|낮|적|덜|작|저렴)",
    ),
    _rule(
        "etp.expense.explicit_low_cost",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:보유\s*)?(?:관리비|운용\s*보수|운영\s*비용|총\s*운용\s*보수)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:싸|싼|낮|적|저렴)",
    ),
    _rule(
        "etp.expense.explicit_cheap_order",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:총\s*보수(?:율)?|연간\s*보수(?:율)?|운용비|운영비|관리비|"
        r"비용\s*부담|수수료\s*부담)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*)?(?:싼|싸게|낮은|적은|가벼운|저렴한)\s*순",
    ),
    _rule(
        "fund.sale_company_fee.colloquial_low",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        SortDirection.ASC,
        r"판매\s*(?:회사|사)(?:가|에서)?\s*(?:가져가는|받는|떼는)\s*"
        r"(?:몫|보수|비용)(?:이|가|은|는)?\s*(?:가장\s*)?(?:적|작|낮|싸)",
    ),
    _rule(
        "fund.sale_company_fee.referential_low",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        SortDirection.ASC,
        r"판매\s*(?:회사|사)\s*보수.{0,40}?그\s*보수(?:가|는|이)?\s*"
        r"(?:가장\s*)?(?:낮|적|작|싸)",
    ),
    _rule(
        "fund.sale_company_fee.repeated_noun_low",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        SortDirection.ASC,
        r"판매\s*(?:회사\s*)?보수.{0,64}?"
        r"보수(?:가|는|이)?\s*(?:가장\s*)?(?:낮|적|작|싸|싼|저렴)",
    ),
    _rule(
        "bond.issue_date.recent",
        (ProductFamily.BOND,),
        "issue_date",
        SortDirection.DESC,
        r"발행일(?:이|가|은|는)?\s*(?:확인|기록|입력|제공)(?:된|되는)?"
        r".{0,40}?(?:최근\s*발행|최신|최근).{0,12}?(?:순|정렬)",
    ),
    _rule(
        "fund.sale_company_fee.referential_high",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        SortDirection.DESC,
        r"판매\s*(?:회사|사)\s*보수.{0,40}?그\s*보수(?:가|는|이)?\s*"
        r"(?:가장\s*)?(?:높|많|크|큰|비싸)",
    ),
    _rule(
        "fund.sale_company_fee.explicit_cheap_order",
        (ProductFamily.FUND,),
        "sale_company_fee_pct",
        SortDirection.ASC,
        r"(?:판매\s*(?:회사\s*)?보수|판매\s*(?:회사\s*)?(?:몫|수수료)|"
        r"판매\s*회사\s*몫\s*수수료)"
        r"(?:이|가|은|는|을|를)?\s*(?:가장\s*)?"
        r"(?:싼|싸게|낮은|적은|작은|가벼운|저렴한)\s*(?:순|순서)",
    ),
    _rule(
        "fund.trustee_fee.explicit_cheap_order",
        (ProductFamily.FUND,),
        "trustee_fee_pct",
        SortDirection.ASC,
        r"(?:수탁\s*(?:회사\s*)?보수|신탁\s*업자\s*보수)"
        r"(?:이|가|은|는|을|를)?\s*(?:가장\s*)?"
        r"(?:싼|싸게|낮은|적은|작은|가벼운|저렴한)\s*(?:순|순서)",
    ),
    _rule(
        "domestic_etp.tracking_error.following_gap_low",
        (ProductFamily.DOMESTIC_ETP,),
        "tracking_error_pct",
        SortDirection.ASC,
        r"(?:지수\s*)?(?:추종\s*)?오차(?:율)?(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:작은|낮은|적은)\s*순",
    ),
    _rule(
        "etp.discrepancy.nav_market_gap_low",
        _ETP_FAMILIES,
        "discrepancy_pct",
        SortDirection.ASC,
        r"(?:NAV(?:와|과)?\s*시장\s*가격\s*)?(?:가격\s*)?괴리(?:율)?"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:작은|낮은|적은)\s*(?:순|순서)",
    ),
    _rule(
        "etp.leverage_factor.explicit_high",
        _ETP_FAMILIES,
        "leverage_factor",
        SortDirection.DESC,
        r"(?:레버리지\s*)?배수(?:이|가|은|는|를|을)?\s*"
        r"(?:가장\s*)?(?:큰|높은)\s*(?:순|순서)",
    ),
    _rule(
        "domestic_etp.volatility.twenty_day_colloquial_low",
        (ProductFamily.DOMESTIC_ETP,),
        "twenty_day_volatility_pct",
        SortDirection.ASC,
        r"(?:최근|지난)?\s*(?:스무|20)\s*거래일(?:의)?\s*"
        r"(?:등락\s*폭|가격\s*변동\s*폭|흔들림)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:작|낮|적)",
    ),
    _rule(
        "domestic_etp.volatility.one_month_colloquial_low",
        (ProductFamily.DOMESTIC_ETP,),
        "twenty_day_volatility_pct",
        SortDirection.ASC,
        r"(?:최근|지난)?\s*(?:한\s*달|1\s*개월)(?:의)?\s*"
        r"(?:등락\s*폭|가격\s*변동\s*폭|변동성|변동|흔들림)"
        r"(?:이|가|은|는)?\s*(?:가장\s*)?(?:작|낮|적)",
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
        r"(?:수익률|수익|성과|성적|상승률|등락률)(?:이|가|은|는|을|를)?\s*"
        r"(?:높|좋|큰)",
    ),
    _rule(
        "etp.return.one_day_low",
        _ETP_FAMILIES,
        "one_day_return_pct",
        SortDirection.ASC,
        r"(?:최근\s*)?(?:하루|당일|오늘|1\s*일|일간).{0,16}?"
        r"(?:수익률|수익|성과|성적|상승률|등락률)(?:이|가|은|는|을|를)?\s*"
        r"(?:낮|작)",
    ),
    _rule(
        "domestic_etp.distribution_yield.explicit_desc",
        (ProductFamily.DOMESTIC_ETP,),
        "distribution_yield_pct",
        SortDirection.DESC,
        r"(?:최근\s*1\s*년\s*)?(?:연환산\s*)?분배(?:금)?\s*수익률"
        r"(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:높은\s*순|큰\s*순|내림차순|순(?:서)?(?:대로|로)?)",
    ),
    _rule(
        "domestic_etp.distribution_yield.explicit_asc",
        (ProductFamily.DOMESTIC_ETP,),
        "distribution_yield_pct",
        SortDirection.ASC,
        r"(?:최근\s*1\s*년\s*)?(?:연환산\s*)?분배(?:금)?\s*수익률"
        r"(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:낮은\s*순|작은\s*순|오름차순)",
    ),
    _rule(
        "product.return.one_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "one_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*달|1\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가|은|는|을|를)?\s*(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.three_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "three_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:한\s*분기|분기|석\s*달|3\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*|제일\s*)?(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.six_month_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:반년|여섯\s*달|6\s*개월).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*|제일\s*)?(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "product.return.one_year_colloquial",
        _DOMESTIC_ETP_AND_FUND,
        "one_year_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*(?:1\s*년|일\s*년).{0,12}?"
        r"(?:(?:수익률|수익|성적(?:표)?|성과)(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*|제일\s*)?(?:좋|높)|잘\s*나간)",
    ),
    _rule(
        "domestic_etp.return.ytd_colloquial",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.DESC,
        r"(?:올해\s*들어|연초\s*(?:이후|대비)).{0,16}?"
        r"(?:(?:성적|성과)(?:이|가|은|는|을|를)?\s*(?:좋|높|우수)|잘\s*나간)",
    ),
    _rule(
        "domestic_etp.return.ytd_rise_width",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.DESC,
        r"(?:올해\s*들어.{0,20}?오른\s*폭(?:이|가)?\s*(?:크|큰|높)|"
        r"오른\s*폭(?:이|가)?\s*(?:크|큰|높).{0,24}?YTD\s*기준)",
    ),
    _rule(
        "product.return.six_month_weak",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.ASC,
        r"(?:최근|지난)?\s*(?:반년|여섯\s*달|6\s*개월).{0,16}?"
        r"(?:(?:수익률|수익|성적|성과)(?:이|가|은|는)?\s*(?:부진|낮|약)|"
        r"(?:덜\s*오른|오른\s*폭(?:이|가)?\s*(?:작|낮)))",
    ),
    _rule(
        "product.return.two_quarters_high",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.DESC,
        r"(?:최근|지난)?\s*두\s*분기\s*(?:수익률|수익|성적|성과)"
        r"(?:이|가|은|는)?\s*(?:높|큰|상위)",
    ),
    _rule(
        "product.return.two_quarters_low",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.ASC,
        r"(?:최근|지난)?\s*두\s*분기\s*(?:수익률|수익|성적|성과)"
        r"(?:이|가|은|는)?\s*(?:낮|작|하위)",
    ),
    _rule(
        "product.return.three_month_global_ordinal_high",
        _DOMESTIC_ETP_AND_FUND,
        "three_month_return_pct",
        SortDirection.DESC,
        r"(?:한\s*분기|분기|석\s*달|3\s*개월).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:상위|높은\s*순|가장\s*높)",
    ),
    _rule(
        "product.return.three_month_global_ordinal_low",
        _DOMESTIC_ETP_AND_FUND,
        "three_month_return_pct",
        SortDirection.ASC,
        r"(?:한\s*분기|분기|석\s*달|3\s*개월).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:하위|낮은\s*순|가장\s*낮)",
    ),
    _rule(
        "product.return.six_month_global_ordinal_high",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.DESC,
        r"(?:반년|여섯\s*달|6\s*개월).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:상위|높은\s*순|가장\s*높)",
    ),
    _rule(
        "product.return.six_month_global_ordinal_low",
        _DOMESTIC_ETP_AND_FUND,
        "six_month_return_pct",
        SortDirection.ASC,
        r"(?:반년|여섯\s*달|6\s*개월).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:하위|낮은\s*순|가장\s*낮)",
    ),
    _rule(
        "product.return.one_year_global_ordinal_high",
        _DOMESTIC_ETP_AND_FUND,
        "one_year_return_pct",
        SortDirection.DESC,
        r"(?:1\s*년|일\s*년|12\s*개월|연간).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:상위|높은\s*순|가장\s*높)",
    ),
    _rule(
        "product.return.one_year_global_ordinal_low",
        _DOMESTIC_ETP_AND_FUND,
        "one_year_return_pct",
        SortDirection.ASC,
        r"(?:1\s*년|일\s*년|12\s*개월|연간).{0,8}?"
        r"(?:수익률|수익|성적|성과)(?:을|를|로|이|가|은|는)?"
        r".{0,120}?(?:하위|낮은\s*순|가장\s*낮)",
    ),
    _rule(
        "etp.close_price_postposed_high",
        _ETP_FAMILIES,
        "close_price",
        SortDirection.DESC,
        r"가격(?:이|가)?\s*(?:비싼|높은|큰).{0,80}?종가\s*기준",
    ),
    _rule(
        "etp.close_price_postposed_low",
        _ETP_FAMILIES,
        "close_price",
        SortDirection.ASC,
        r"가격(?:이|가)?\s*(?:싼|낮은|작은).{0,80}?종가\s*기준",
    ),
    _rule(
        "domestic_etp.aum.bottom_ordinal",
        (ProductFamily.DOMESTIC_ETP,),
        "aum",
        SortDirection.ASC,
        r"(?:AUM|순자산)(?:을|를|은|는|이|가)?\s*기준\s*"
        r"(?:아래|밑)(?:에서|부터)",
    ),
    _rule(
        "etp.total_expense.explicit_ordinal_low",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.ASC,
        r"(?:총\s*)?보수(?:율)?(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?하위",
    ),
    _rule(
        "etp.total_expense.explicit_ordinal_high",
        _ETP_FAMILIES,
        "total_expense_ratio_pct",
        SortDirection.DESC,
        r"(?:총\s*)?보수(?:율)?(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?상위",
    ),
    _rule(
        "domestic_etp.monthly_average_trading_value.high",
        (ProductFamily.DOMESTIC_ETP,),
        "one_month_average_trading_value",
        SortDirection.DESC,
        r"(?:최근\s*)?(?:1\s*개월|한\s*달|월)\s*평균\s*거래대금"
        r"(?:이|가|은|는|을|를)?\s*(?:기준(?:으로)?\s*)?"
        r"(?:큰|높은|많은|상위)(?:\s*순(?:으로)?)?",
    ),
    _rule(
        "domestic_etp.monthly_average_trading_value.low",
        (ProductFamily.DOMESTIC_ETP,),
        "one_month_average_trading_value",
        SortDirection.ASC,
        r"(?:최근\s*)?(?:1\s*개월|한\s*달|월)\s*평균\s*거래대금"
        r"(?:이|가|은|는|을|를)?\s*(?:기준(?:으로)?\s*)?"
        r"(?:작은|낮은|적은|하위)(?:\s*순(?:으로)?)?",
    ),
    _rule(
        "domestic_etp.return.ytd_explicit_high",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.DESC,
        r"(?:YTD|연초\s*(?:이후|대비))(?:\s*(?:수익률|수익|성과|성적))?"
        r"(?:이|가|은|는|을|를)?\s*(?:기준(?:으로)?\s*)?"
        r"(?:높은|큰|우수한|상위)",
    ),
    _rule(
        "domestic_etp.return.ytd_explicit_low",
        (ProductFamily.DOMESTIC_ETP,),
        "ytd_return_pct",
        SortDirection.ASC,
        r"(?:YTD|연초\s*(?:이후|대비))(?:\s*(?:수익률|수익|성과|성적))?"
        r"(?:이|가|은|는|을|를)?\s*(?:기준(?:으로)?\s*)?"
        r"(?:낮은|작은|부진한|하위)",
    ),
    _rule(
        "fund.return.five_year_cumulative_high",
        (ProductFamily.FUND,),
        "five_year_return_pct",
        SortDirection.DESC,
        r"(?:5\s*년|오\s*년)(?:간)?\s*(?:누적\s*)?"
        r"(?:수익률|수익|성과|성적)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:높|좋|우수|큰|상위)",
    ),
    _rule(
        "fund.return.five_year_cumulative_low",
        (ProductFamily.FUND,),
        "five_year_return_pct",
        SortDirection.ASC,
        r"(?:5\s*년|오\s*년)(?:간)?\s*(?:누적\s*)?"
        r"(?:수익률|수익|성과|성적)(?:이|가|은|는)?\s*"
        r"(?:가장\s*)?(?:낮|부진|약|작|하위)",
    ),
    _rule(
        "bond.dirty_price.explicit_high",
        (ProductFamily.BOND,),
        "dirty_price",
        SortDirection.DESC,
        r"(?:이자부\s*(?:단가|가격)|경과이자(?:가|를)?\s*포함(?:된|한)?\s*"
        r"(?:단가|가격))(?:이|가|은|는)?\s*(?:높은|큰|상위)",
    ),
    _rule(
        "bond.dirty_price.explicit_low",
        (ProductFamily.BOND,),
        "dirty_price",
        SortDirection.ASC,
        r"(?:이자부\s*(?:단가|가격)|경과이자(?:가|를)?\s*포함(?:된|한)?\s*"
        r"(?:단가|가격))(?:이|가|은|는)?\s*(?:낮은|작은|하위)",
    ),
    _rule(
        "fund.preservation_ratio.explicit_high",
        (ProductFamily.FUND,),
        "preservation_ratio_pct",
        SortDirection.DESC,
        r"(?:보전율|보전\s*비율|원본\s*보존\s*비율|"
        r"원본(?:을)?\s*남겨\s*둔\s*비율)(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:높은|큰|많은|상위)",
    ),
    _rule(
        "fund.preservation_ratio.explicit_low",
        (ProductFamily.FUND,),
        "preservation_ratio_pct",
        SortDirection.ASC,
        r"(?:보전율|보전\s*비율|원본\s*보존\s*비율|"
        r"원본(?:을)?\s*남겨\s*둔\s*비율)(?:이|가|은|는|을|를)?\s*"
        r"(?:기준(?:으로)?\s*)?(?:낮은|작은|적은|하위)",
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
                r"해외\s*(?:주식|채권)\s*편입\s*(?:비|비중|비율)",
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
                r"국내\s*(?:주식|채권)\s*편입\s*(?:비|비중|비율)",
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
        # ``국고채권`` is an official ``bd_knd`` value, while ``국고채`` is
        # the normalized standard subclass.  Longest semantic ownership keeps
        # the former from being narrowed to the latter by substring matching.
        ("국고채권", "bond_type", "국고채권", ()),
        ("국고채", "bond_subclass", "국고채", (r"국고채권",)),
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
        ("일반특수법인채", "bond_type", "일반특수법인채", ()),
        ("유동화회사채", "bond_type", "유동화회사채", ()),
        ("신용카드채", "bond_type", "신용카드채", ()),
        ("할부금융채", "bond_type", "할부금융채", ()),
        ("고정+변동금리", "interest_rate_type", "고정+변동금리", ()),
        ("고정금리", "interest_rate_type", "고정금리", (r"고정\+변동금리",)),
        ("변동금리", "interest_rate_type", "변동금리", (r"고정\+변동금리",)),
        ("이표채", "interest_payment_type", "이표채", ()),
        ("복리채", "interest_payment_type", "복리채", ()),
        ("할인채", "interest_payment_type", "할인채", ()),
        ("단리채", "interest_payment_type", "단리채", ()),
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
        ("나스닥", "exchange_code", "NAS", ()),
        ("뉴욕증권거래소", "exchange_code", "NYS", ()),
        ("뉴욕 거래소", "exchange_code", "NYS", ()),
        ("아멕스", "exchange_code", "AMX", ()),
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

# Context-bound schema aliases. A bare ``주식`` may be a company holding,
# whereas ``주식에 투자하는 해외 ETF`` is an unambiguous asset-type filter.
# Risk-grade aliases also require the literal ``위험등급`` label so ordinary
# result counts cannot be mistaken for a risk filter.
_CONTEXTUAL_FILTER_ALIASES: tuple[FilterAliasRule, ...] = (
    # Brand spellings are executable only when the sentence explicitly gives
    # the brand a manager role.  This is vocabulary normalization, not a
    # product-specific shortcut: the resulting predicate still searches the
    # approved manager column and is rechecked by the Oracle.
    _filter_rule(
        "etp.manager.blackrock_korean_brand",
        _ETP_FAMILIES,
        "manager",
        "BlackRock",
        r"블랙록(?:이|이가|에서)?\s*(?:운용|굴리|관리|운영)"
        r"(?:하는|하고\s*있는|중인|는)",
        operator="contains",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "etp.manager.invesco_korean_brand",
        _ETP_FAMILIES,
        "manager",
        "Invesco",
        r"인베스코(?:가|이|에서)?\s*(?:운용|굴리|관리|운영)"
        r"(?:하는|하고\s*있는|중인|\s*상품)?",
        operator="contains",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.manager.miraeasset_brand",
        (ProductFamily.DOMESTIC_ETP,),
        "manager",
        "미래에셋",
        r"미래에셋(?:이|가|에서)?\s*(?:(?:운용|관리|굴리)(?:하는)?\s*)?"
        r"(?:국내\s*)?(?:ETF|ETN|ETP)",
        operator="contains",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.region.global_ex_us_colloquial",
        (ProductFamily.OVERSEAS_ETP,),
        "investment_region",
        "Global Ex US",
        r"글로벌\s*(?:중\s*)?미국\s*제외(?:\s*(?:지역|시장|노출|투자))?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.region.global_emerging_colloquial",
        (ProductFamily.OVERSEAS_ETP,),
        "investment_region",
        "Global Emerging Markets",
        r"(?:글로벌\s*)?(?:신흥\s*시장|신흥국)(?:\s*전반)?"
        r"(?:에|을|으로)?\s*(?:투자|노출)(?:하는|한|된)?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.region.global_exposure",
        (ProductFamily.OVERSEAS_ETP,),
        "investment_region",
        "Global",
        r"(?:글로벌|전\s*세계)(?:\s*전반)?\s*(?:노출|투자|시장)",
        forbidden_contexts=(r"미국\s*제외", r"신흥\s*시장|신흥국"),
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.region.korea_investment",
        (ProductFamily.OVERSEAS_ETP,),
        "investment_region",
        "Korea",
        r"한국(?:에|으로|을|을\s*중심으로)?\s*(?:주로\s*)?"
        r"(?:투자|노출)(?:하는|한|된)\s*(?:해외\s*)?(?:ETF|ETN|ETP)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.exchange.new_york_listing",
        (ProductFamily.OVERSEAS_ETP,),
        "exchange_code",
        "NYS",
        r"뉴욕(?:\s*(?:증권)?거래소)?(?:에|에서)?\s*"
        r"(?:(?:상장|거래)(?:된|되는|하는)?|올라온|올라오는)|"
        r"뉴욕(?=\s*(?:양쪽|두\s*곳))",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.asset_type.equity_coordinated",
        (ProductFamily.OVERSEAS_ETP,),
        "asset_type",
        "Equity",
        r"주식(?=\s*(?:이나|나|또는|혹은|및|와|과|하고|/|·|,)\s*채권\s*자산)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.asset_type.alternatives_colloquial",
        (ProductFamily.OVERSEAS_ETP,),
        "asset_type",
        "Alternatives",
        r"대체\s*(?:자산|투자)(?:\s*(?:성격|노출|위주))?(?:의|인)?\s*"
        r"(?:해외\s*)?(?:ETF|ETN|ETP)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.asset_type.commodity_holding",
        (ProductFamily.OVERSEAS_ETP,),
        "asset_type",
        "Commodity",
        r"원자재(?:를|에|로|을)?\s*(?:담는|투자하는|추종하는|노출된)\s*"
        r"(?:해외\s*)?(?:ETF|ETN|ETP)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.asset_type.money_market_colloquial",
        (ProductFamily.OVERSEAS_ETP,),
        "asset_type",
        "Money Market",
        r"단기\s*자금(?:\s*(?:성격|위주|노출))?(?:의|인)?\s*"
        r"(?:해외\s*)?(?:ETF|ETN|ETP)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.core.classified",
        (ProductFamily.DOMESTIC_ETP,),
        "core_etf",
        True,
        r"코어(?:로|\s*ETF로)?\s*(?:분류|표시|지정)(?:된|되는|한)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.core.product_noun",
        (ProductFamily.DOMESTIC_ETP,),
        "core_etf",
        True,
        r"코어\s*(?:ETF|ETP)(?:만|중|가운데|상품)?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "etp.sellable.colloquial_available",
        _ETP_FAMILIES,
        "sellable",
        True,
        r"(?:현재\s*)?(?:판매할|팔)\s*수\s*있(?:는|고)|"
        r"(?:현재\s*)?판매\s*(?:가능한|중인)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.asset_type.equity_context",
        (ProductFamily.DOMESTIC_ETP,),
        "asset_type",
        "주식",
        r"(?:미국|국내|해외)?\s*주식(?:에|을|으로)?\s*(?:투자(?:하는|한)?\s*)?"
        r"(?=(?:국내\s*)?(?:ETF|ETN|ETP))",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.asset_type.commodity_context",
        (ProductFamily.DOMESTIC_ETP,),
        "asset_type",
        "원자재",
        r"원자재(?:\s*(?:자산|상품))?\s*(?=(?:국내\s*)?(?:ETF|ETN|ETP))",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.region.domestic_equity_context",
        (ProductFamily.DOMESTIC_ETP,),
        "investment_region",
        "국내",
        r"국내\s*주식(?:에|을|으로)?\s*(?:투자|노출)(?:하는|한|된)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "etp.trading_suspended.not_stopped_colloquial",
        _ETP_FAMILIES,
        "trading_suspended",
        False,
        r"(?:매매|거래)(?:가|는|이)?\s*(?:멈춰|\uc911지|\uc815지)\s*"
        r"(?:있지\s*않|되지\s*않|아닌|아니)|"
        r"거래(?:가|는|이)?\s*살아\s*있",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "etp.trading_suspended.normal_trading",
        _ETP_FAMILIES,
        "trading_suspended",
        False,
        r"(?:(?:매매|거래)(?:가|는|이)?\s*"
        r"(?:정상|원활)(?:적으로)?(?:인|이고|이며|하게)?|"
        r"(?:정상|원활)(?:적인|한)?\s*(?:매매|거래))",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.pension_eligible.account_holding",
        (ProductFamily.DOMESTIC_ETP,),
        "pension_eligible",
        True,
        r"연금\s*계좌(?:에|에서|로)?\s*(?:담을|편입할|거래할|살)\s*"
        r"수\s*있(?:는|고|다)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.pension_eligible.pension_use",
        (ProductFamily.DOMESTIC_ETP,),
        "pension_eligible",
        True,
        r"연금(?:\s*계좌)?(?:용|\s*대상)(?:인|으로)?\s*(?:국내\s*)?(?:ETF|ETN|ETP)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.pension_eligible.tradable",
        (ProductFamily.DOMESTIC_ETP,),
        "pension_eligible",
        True,
        r"연금(?:\s*계좌)?\s*(?:거래|편입|매수)(?:가|는|이)?\s*"
        r"(?:되(?:는|고)|가능(?:한|하고))",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.strategy.active_colloquial",
        (ProductFamily.DOMESTIC_ETP,),
        "strategy",
        "액티브",
        r"액티브(?:\s*(?:방식|전략|운용|복제))?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "domestic_etp.strategy.synthetic_colloquial",
        (ProductFamily.DOMESTIC_ETP,),
        "strategy",
        "합성복제",
        r"합성(?:으로)?\s*(?:복제|추종)(?:하는|한)?|합성\s*방식",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.scope.overseas_equity_colloquial",
        (ProductFamily.FUND,),
        "fund_geography_scope",
        "해외",
        r"(?:나라\s*밖|해외)\s*주식(?:에|을|으로)?\s*(?:투자|담)(?:하는|한|고)?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.management_attribute.overseas_equity_colloquial",
        (ProductFamily.FUND,),
        "fund_management_attribute",
        "주식형",
        r"(?:나라\s*밖|해외)\s*주식(?:에|을|으로)?\s*(?:투자|담)(?:하는|한|고)?",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.currency_hedged.exchange_rate_defence",
        (ProductFamily.FUND,),
        "currency_hedged",
        True,
        r"환율\s*(?:변동\s*)?(?:방어|방어를\s*해\s*둔|막아\s*둔|줄인|막는)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.scope.domestic_overseas_mixed_colloquial",
        (ProductFamily.FUND,),
        "fund_geography_scope",
        "국내외혼합",
        r"(?:국내와\s*(?:해외|국외)|국내외)(?:을|를)?\s*"
        r"(?:섞어|섞은|함께)\s*투자",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.company_sellable.our_company_colloquial",
        (ProductFamily.FUND,),
        "company_sellable",
        True,
        r"(?:우리\s*회사|당사|미래에셋(?:증권)?)(?:에서|에서도)?\s*"
        r"(?:팔\s*수\s*있(?:는|고)|판매할\s*수\s*있(?:는|고)|"
        r"판매(?:하는|\s*중인)?|취급하는)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.investor_type.individual_colloquial",
        (ProductFamily.FUND,),
        "investor_type",
        "개인",
        r"개인(?:이|은|용|\s*투자자\s*대상)?\s*"
        r"(?:(?:온라인으로\s*)?(?:신청|가입)(?:하는|할\s*수\s*있는)\s*)?"
        r"(?:공모\s*)?펀드",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.investor_type.corporate_colloquial",
        (ProductFamily.FUND,),
        "investor_type",
        "법인",
        r"법인(?:용|\s*투자자\s*대상|이\s*신청하는)?\s*"
        r"(?:원화\s*)?(?:MMF|공모\s*펀드|펀드)",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.sales_channel.online_enrollment",
        (ProductFamily.FUND,),
        "sales_channel",
        "온라인",
        r"온라인(?:으로|에서)?\s*(?:신청|가입)(?:하는|할\s*수\s*있는)",
        operator="contains",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.sales_channel.offline_class",
        (ProductFamily.FUND,),
        "sales_channel",
        "오프라인",
        r"오프라인\s*(?:판매\s*채널|클래스)",
        operator="contains",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.risk_level.high_colloquial",
        (ProductFamily.FUND,),
        "risk_level",
        ["높은 위험", "매우 높은 위험"],
        r"(?<!높은)(?<!높은 )(?<!낮은)(?<!낮은 )"
        r"위험(?:도|수준|등급)?(?:가|이|은|는)?\s*(?:높은|큰)",
        operator="in",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "fund.risk_level.low_colloquial",
        (ProductFamily.FUND,),
        "risk_level",
        ["낮은 위험", "매우 낮은 위험"],
        r"(?<!높은)(?<!높은 )(?<!낮은)(?<!낮은 )"
        r"위험(?:도|수준|등급)?(?:가|이|은|는)?\s*(?:낮은|작은|적은)",
        operator="in",
        evidence_source="field-registry-v1.8",
    ),
    _filter_rule(
        "overseas_etp.asset_type.equity_investment",
        (ProductFamily.OVERSEAS_ETP,),
        "asset_type",
        "Equity",
        r"(?:주식(?:에|을|으로)?\s*(?:주로\s*)?투자(?:하는|한|하는\s*형태)|"
        r"주식\s*(?:중심|위주)(?:의)?\s*(?:해외\s*)?(?:ETF|ETN|ETP)|"
        r"(?:해외\s*)?주식\s*(?:(?:자산\s*)?형\s*)?"
        r"(?:해외\s*)?(?:ETF|ETN|ETP))",
    ),
    *(
        _filter_rule(
            "overseas_etp.asset_type.explicit_english_"
            + canonical_value.casefold().replace(" ", "_"),
            (ProductFamily.OVERSEAS_ETP,),
            "asset_type",
            canonical_value,
            rf"(?<![A-Za-z]){re.escape(canonical_value)}(?![A-Za-z])"
            r"(?=\s*(?:이나|나|또는|혹은|및|와|과|하고|/|·|,|"
            r"\s+자산\s*(?:군|유형)))",
            evidence_source="field-registry-v1.8",
        )
        for canonical_value in ("Equity", "Bond", "Commodity", "Alternatives", "Money Market")
    ),
    *(
        _filter_rule(
            f"overseas_etp.exchange.explicit_code_{code.casefold()}",
            (ProductFamily.OVERSEAS_ETP,),
            "exchange_code",
            code,
            rf"(?<![A-Z0-9]){code}(?![A-Z0-9])"
            r"(?=\s*(?:이나|나|또는|혹은|및|와|과|하고|/|·|,|\s+거래소))",
            evidence_source="field-registry-v1.8",
        )
        for code in ("NAS", "NYS", "AMX")
    ),
    *(
        _filter_rule(
            f"overseas_etp.asset_type.korean_schema_{suffix}",
            (ProductFamily.OVERSEAS_ETP,),
            "asset_type",
            canonical_value,
            rf"(?:{surface})\s*(?:투자\s*)?자산\s*군(?:의|인)?\s*"
            r"(?:해외\s*)?(?:ETF|ETN|ETP)",
            evidence_source="field-registry-v1.8",
        )
        for suffix, canonical_value, surface in (
            ("equity", "Equity", r"주식"),
            ("bond", "Bond", r"채권"),
            ("commodity", "Commodity", r"원자재"),
            ("alternatives", "Alternatives", r"대체\s*자산|대체\s*투자"),
            ("mixed", "Mixed Assets", r"혼합\s*자산"),
            ("money_market", "Money Market", r"머니\s*마켓|단기\s*자금"),
        )
    ),
    *(
        _filter_rule(
            f"overseas_etp.index_replication_method.{rule_suffix}",
            (ProductFamily.OVERSEAS_ETP,),
            "index_replication_method",
            canonical_value,
            r"(?:지수|인덱스)?\s*복제\s*(?:방법|방식)"
            r"(?:이|가|은|는)?\s*[:：]?\s*(?:"
            + value_surface
            + r")(?=\s*(?:인|이고|이며|만|해외|ETF|ETN|ETP|상품|종목|[,.;:!?]|$))",
            evidence_source="field-registry-v1.8",
        )
        for rule_suffix, canonical_value, value_surface in (
            ("optimized", "Optimized", r"Optimized|최적화(?:\s*복제)?|표본(?:\s*복제)?"),
            ("swap", "Swap", r"Swap|스왑(?:\s*복제)?|합성(?:\s*복제)?"),
            ("full", "Full", r"Full|완전(?:\s*복제)?"),
            ("other", "Other", r"Other|기타"),
        )
    ),
    *(
        _filter_rule(
            f"product.risk_level.explicit_grade_{grade}",
            (ProductFamily.BOND, ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
            "risk_level",
            value,
            rf"위험(?:\s*등급)?(?:이|가|은|는)?\s*{grade}\s*등급",
            forbidden_contexts=(r"신용\s*등급",),
        )
        for grade, value in (
            (1, "매우높은위험(1등급)"),
            (2, "높은위험(2등급)"),
            (3, "다소높은위험(3등급)"),
            (4, "보통위험(4등급)"),
            (5, "낮은위험(5등급)"),
            (6, "매우낮은위험(6등급)"),
        )
    ),
)

FILTER_ALIAS_RULES: tuple[FilterAliasRule, ...] = (
    *_DOMESTIC_ETP_FILTER_ALIASES,
    *_FUND_FILTER_ALIASES,
    *_BOND_FILTER_ALIASES,
    *_OVERSEAS_ETP_FILTER_ALIASES,
    *_CONTEXTUAL_FILTER_ALIASES,
)


_EXPLICIT_ORDER = re.compile(
    r"(?:오름차순|내림차순|상위|하위|TOP|BOTTOM|순위|정렬|"
    r"가나다\s*순|알파벳\s*순|이름\s*순|역순|앞선\s*순서|"
    r"\d+\s*(?:등(?!급)|위(?!험))|"
    r"(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)(?!\s*위험).{0,5}?순)",
    re.IGNORECASE,
)
_COMPARATIVE_CUE = re.compile(
    r"(?:가장|제일)\s*(?:높|낮|크|작|많|적|길|짧|빠르|멀|좋|싸)|"
    r"(?:높은|낮은|큰|작은|많은|적은|(?<!남)긴|짧은|빠른|먼(?!저)|좋은|싼)"
    r"(?!\s*위험)|"
    # "민감도" is the name of a field (duration), not a comparison cue. Only the
    # adjectival "금리에 민감한" form asks for a ranking.
    r"(?:넉넉|풍부|두드러|민감(?!도)|가벼|저렴|잘\s*나간|활발하게\s*사고\s*팔|"
    r"손바뀜|돈(?:이|가)?\s*많이\s*오가|인기|뜨거운|우수한|알짜|"
    r"안정적|안전한)",
    re.IGNORECASE,
)
_EXPLICIT_NAME_ORDER = re.compile(
    r"(?:상품명|종목명|펀드명|ETF\s*명|ETN\s*명|채권명|이름|"
    r"짧은\s*이름|티커|종목\s*코드).{0,24}?"
    r"(?:순서|순으로|오름차순|내림차순|이름순|가나다순|알파벳순|"
    r"역순|앞선\s*순서|기준\s*(?:앞쪽|뒤쪽))",
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
    # Prefer the most specific lexical prototype when a broader alias is
    # wholly nested in it.  For example, ``세금을 떼고 남는 매수 수익`` is an
    # after-tax yield signal; the nested words ``매수 수익`` must not create a
    # second pretax-yield candidate.  Equal or merely crossing spans remain in
    # the set and therefore still fail closed as a genuine conflict.
    maximal_matches = [
        item
        for item in matches
        if not any(
            other.start <= item.start
            and item.end <= other.end
            and (other.start < item.start or item.end < other.end)
            for other in matches
        )
    ]
    return tuple(sorted(maximal_matches, key=lambda item: (item.start, item.end, item.rule_id)))


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
        *(rule.rule_id for rule in NUMERIC_FIELD_REFERENCE_RULES),
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
    for rule in NUMERIC_FIELD_REFERENCE_RULES:
        if not rule.families or not rule.field_id or not rule.aliases:
            raise RuntimeError(f"numeric field-reference alias is incomplete: {rule.rule_id}")
        if len(rule.aliases) != len(set(rule.aliases)):
            raise RuntimeError(f"numeric field-reference aliases repeat: {rule.rule_id}")
        for family in rule.families:
            definition = field_registry.require_field(rule.field_id, [family.value])
            if definition.value_type is not ValueType.NUMBER or not definition.queryable:
                raise RuntimeError(
                    f"numeric field-reference alias is not queryable: {rule.rule_id}"
                )


def numeric_field_reference_aliases(
    product_family: ProductFamily | str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return typed numeric field names admitted for one product family."""

    family = ProductFamily(product_family)
    merged: dict[str, list[str]] = {}
    for rule in NUMERIC_FIELD_REFERENCE_RULES:
        if family not in rule.families:
            continue
        values = merged.setdefault(rule.field_id, [])
        values.extend(alias for alias in rule.aliases if alias not in values)
    return tuple((field, tuple(aliases)) for field, aliases in sorted(merged.items()))


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
    "NUMERIC_FIELD_REFERENCE_RULES",
    "SEMANTIC_ALIAS_RULES",
    "FilterAliasMatch",
    "FilterAliasRule",
    "NumericFieldReferenceRule",
    "SemanticAliasMatch",
    "SemanticAliasRule",
    "TYPED_ALIAS_POLICY_VERSION",
    "TypedAliasMatch",
    "explicit_product_name_order",
    "filter_alias_matches",
    "numeric_field_reference_aliases",
    "rank_alias_matches",
    "ranking_cue_spans",
    "ranking_intent_present",
    "typed_alias_matches",
    "validate_typed_alias_registry",
]
