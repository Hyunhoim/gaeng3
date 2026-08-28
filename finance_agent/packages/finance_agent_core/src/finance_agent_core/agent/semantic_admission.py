from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from finance_agent_core.contracts.queryplan import ProductFamily

SemanticOperation = Literal["rank", "project", "filter", "aggregate"]


@dataclass(frozen=True, slots=True)
class SemanticFieldRule:
    field_id: str
    semantic_group: str
    positive: tuple[re.Pattern[str], ...]
    forbidden: tuple[re.Pattern[str], ...] = ()
    families: frozenset[ProductFamily] = frozenset(ProductFamily)
    operations: frozenset[SemanticOperation] = frozenset({"rank"})
    deterministic_anchor: bool = False


@dataclass(frozen=True, slots=True)
class SemanticFieldEvidence:
    status: Literal["supported", "insufficient", "contradicted", "generic_ambiguity"]
    reason_code: str
    semantic_group: str | None = None
    evidence_spans: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.status == "supported"


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


_RETURN_FIELDS = frozenset(
    {
        "one_day_return_pct",
        "one_week_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "ytd_return_pct",
    }
)
_METADATA_RANKING_FIELDS = frozenset(
    {
        "dynamic_as_of",
        "static_as_of",
        "product_id",
        "product_name",
        "short_name",
        "ticker",
        "issuer",
        "manager",
        "issue_date",
    }
)

_RULES: tuple[SemanticFieldRule, ...] = (
    SemanticFieldRule(
        field_id="buy_yield_pct",
        semantic_group="bond_yield_tax_basis",
        positive=_patterns(
            r"세\s*전",
            r"매(?:수|입).{0,12}(?:가격|단가).{0,12}(?:수익|보상)",
            r"(?:현재|지금).{0,10}(?:사|샀|매수).{0,12}(?:수익|보상)",
            r"매수\s*수익(?:률)?",
        ),
        forbidden=_patterns(r"세\s*후", r"세금.{0,8}(?:공제|떼|뗀|반영).{0,10}(?:수익|보상)"),
        families=frozenset({ProductFamily.BOND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="after_tax_yield_pct",
        semantic_group="bond_yield_tax_basis",
        positive=_patterns(
            r"세\s*후",
            r"세금.{0,8}(?:공제|떼|뗀|반영)",
            r"(?:공제|세금).{0,12}(?:뒤|후).{0,10}(?:수익|보상)",
        ),
        forbidden=_patterns(r"세\s*전"),
        families=frozenset({ProductFamily.BOND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="coupon_rate_pct",
        semantic_group="bond_coupon",
        positive=_patterns(
            r"표면\s*(?:금리|이율|이자)",
            r"액면.{0,10}(?:금리|이율|이자)",
            r"약정.{0,8}(?:금리|이율|이자)",
            r"쿠폰\s*(?:금리|이율)",
            r"정기적.{0,10}(?:붙|받).{0,8}(?:금리|이율|이자)",
        ),
        forbidden=_patterns(r"매(?:수|입).{0,10}(?:가격|단가|수익)", r"세\s*후"),
        families=frozenset({ProductFamily.BOND}),
    ),
    SemanticFieldRule(
        field_id="issue_amount",
        semantic_group="bond_quantity_basis",
        positive=_patterns(
            r"(?:발행|찍어낸|풀린).{0,12}(?:규모|금액|물량|총액)",
            r"최초.{0,8}(?:발행|시장).{0,10}(?:규모|금액|물량)",
        ),
        forbidden=_patterns(r"(?:현재|지금|당장).{0,10}(?:매수|살|주문).{0,10}(?:수량|물량|양)"),
        families=frozenset({ProductFamily.BOND}),
    ),
    SemanticFieldRule(
        field_id="duration_years",
        semantic_group="bond_time_sensitivity",
        positive=_patterns(
            r"금리.{0,12}(?:민감|예민|반응)",
            r"금리.{0,12}가격.{0,10}(?:민감|예민|반응)",
            r"가격.{0,12}금리.{0,8}(?:민감|반응)",
            r"듀레이션",
        ),
        forbidden=_patterns(r"만기.{0,10}(?:남|임박|날짜|시점)"),
        families=frozenset({ProductFamily.BOND}),
    ),
    SemanticFieldRule(
        field_id="remaining_days",
        semantic_group="bond_maturity_basis",
        positive=_patterns(
            r"(?:만기|상환).{0,12}(?:남은|얼마\s*안\s*남|임박).{0,8}(?:일|기간|시간)?",
            r"잔존.{0,6}(?:일수|기간|시간)",
        ),
        forbidden=_patterns(r"(?:날짜|달력|시점).{0,8}(?:빠|늦|이른)", r"금리.{0,10}민감"),
        families=frozenset({ProductFamily.BOND}),
    ),
    SemanticFieldRule(
        field_id="maturity_date",
        semantic_group="bond_maturity_basis",
        positive=_patterns(
            r"(?:만기|상환).{0,12}(?:날짜|일자|시점|오는|도래)",
            r"(?:달력상|가장\s*먼저).{0,8}만기",
            r"만기.{0,8}(?:빠른|늦은|이른)",
        ),
        forbidden=_patterns(r"잔존.{0,6}(?:일수|기간)", r"금리.{0,10}민감"),
        families=frozenset({ProductFamily.BOND}),
    ),
    SemanticFieldRule(
        field_id="aum",
        semantic_group="asset_scale",
        positive=_patterns(
            r"(?:투자자\s*)?자금.{0,14}(?:모|쌓|두툼|두둑|규모|체급|덩치|몸집)",
            r"투자자.{0,6}돈.{0,10}(?:모|쌓|두툼|두둑)",
            r"(?:운용|관리).{0,10}(?:자산|자금).{0,10}(?:규모|체급|덩치|몸집|큰)",
            r"(?:순자산|운용자산|AUM)",
            r"(?:체급|덩치|몸집).{0,8}(?:크|큰|높|묵직)",
        ),
        forbidden=_patterns(r"(?:하루|일일|매일).{0,10}(?:거래|손바뀜)", r"거래대금"),
        families=frozenset(
            {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP, ProductFamily.FUND}
        ),
    ),
    SemanticFieldRule(
        field_id="daily_trading_value",
        semantic_group="market_activity",
        positive=_patterns(
            r"(?:하루|일일|매일).{0,12}(?:거래|손바뀜|돈).{0,12}(?:금액|대금|크|많|활발|오가)",
            r"(?:거래대금|손바뀜\s*금액)",
            r"(?:시장|장).{0,8}(?:거래|돈).{0,8}(?:뜨거|활발|많)",
        ),
        forbidden=_patterns(r"자금.{0,10}(?:모|쌓)", r"(?:순자산|운용자산|AUM)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP}),
    ),
    SemanticFieldRule(
        field_id="total_expense_ratio_pct",
        semantic_group="holding_cost",
        positive=_patterns(
            r"(?:총)?보수(?:율)?",
            r"(?:운용|운영|관리|보유).{0,10}(?:비용|보수|수수료)",
            r"(?:장기\s*)?보유.{0,10}(?:비용|관리비|운영비)",
            r"(?:들고|보유).{0,14}(?:관리|유지).{0,8}부담",
            r"비용.{0,8}(?:비율|부담|낮|적|작|덜)",
        ),
        forbidden=_patterns(r"(?:종가|마감|체결).{0,8}(?:가격|단가)", r"한\s*주\s*가격"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}),
    ),
    SemanticFieldRule(
        field_id="close_price",
        semantic_group="market_price",
        positive=_patterns(
            r"(?:종가|마감|마지막\s*체결).{0,8}(?:가격|단가)?",
            r"한\s*주.{0,6}(?:가격|단가)",
            r"(?:가격|단가).{0,6}(?:비싼|싼|높|낮)",
        ),
        forbidden=_patterns(r"(?:보수|운용|관리|보유).{0,8}(?:비용|부담|비율)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}),
    ),
    SemanticFieldRule(
        field_id="leverage_factor",
        semantic_group="leverage_design",
        positive=_patterns(
            r"(?:기초지수|지수).{0,12}(?:배수|배율|몇\s*배|추종)",
            r"(?:추종|레버리지).{0,8}(?:배수|배율|몇\s*배|센|강)",
        ),
        forbidden=_patterns(r"최근.{0,10}수익률", r"기간.{0,8}성과"),
        families=frozenset({ProductFamily.DOMESTIC_ETP}),
    ),
    SemanticFieldRule(
        field_id="one_day_return_pct",
        semantic_group="return_period",
        positive=_patterns(r"(?:최근\s*)?(?:하루|1\s*일).{0,10}(?:성과|성적|수익|실적)"),
        families=frozenset(
            {
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
            }
        ),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="one_month_return_pct",
        semantic_group="return_period",
        positive=_patterns(r"(?:최근\s*)?(?:1\s*개월|한\s*달|월간).{0,10}(?:성과|성적|수익|실적)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="three_month_return_pct",
        semantic_group="return_period",
        positive=_patterns(r"(?:최근\s*)?(?:3\s*개월|석\s*달|분기).{0,10}(?:성과|성적|수익|실적)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="six_month_return_pct",
        semantic_group="return_period",
        positive=_patterns(r"(?:최근\s*)?(?:6\s*개월|반년|반기).{0,10}(?:성과|성적|수익|실적)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="one_year_return_pct",
        semantic_group="return_period",
        positive=_patterns(
            r"(?:최근\s*)?(?:1\s*년|일\s*년|일년|열두\s*달|12\s*개월).{0,10}(?:성과|성적|수익|실적)"
        ),
        forbidden=_patterns(r"(?:연초|올해|YTD)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        deterministic_anchor=True,
    ),
    SemanticFieldRule(
        field_id="ytd_return_pct",
        semantic_group="return_period",
        positive=_patterns(r"(?:연초|올해\s*들어|올해|YTD).{0,12}(?:성과|성적|수익|실적)"),
        forbidden=_patterns(r"(?:최근\s*)?(?:1\s*년|열두\s*달|12\s*개월)"),
        families=frozenset({ProductFamily.DOMESTIC_ETP}),
        deterministic_anchor=True,
    ),
)

_RULE_BY_FIELD = {rule.field_id: rule for rule in _RULES}
_GENERIC_PREFERENCE = re.compile(
    r"(?:^|\s)(?:좋은|좋다|괜찮은|괜찮다|싼|안전한|안전하다|안정적인|안정적이다|"
    r"유망한|유망하다|대표적인|인기\s*있는|알짜인|추천할\s*만한|효율이\s*높은|"
    r"부담이\s*(?:적은|작은)|비용\s*대비\s*괜찮은)(?:\s|$)",
    re.IGNORECASE,
)


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> tuple[str, ...]:
    matches: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            surface = match.group(0).strip()
            if surface and surface not in matches:
                matches.append(surface)
    return tuple(matches)


def semantic_rule(field_id: str) -> SemanticFieldRule | None:
    return _RULE_BY_FIELD.get(field_id)


def semantic_field_evidence(
    text: str,
    *,
    field_id: str,
    product_family: ProductFamily,
    operation: SemanticOperation = "rank",
) -> SemanticFieldEvidence:
    rule = semantic_rule(field_id)
    if rule is None or product_family not in rule.families or operation not in rule.operations:
        return SemanticFieldEvidence(
            status="insufficient",
            reason_code="semantic_field_not_activated",
        )
    forbidden = _matches(rule.forbidden, text)
    if forbidden:
        return SemanticFieldEvidence(
            status="contradicted",
            reason_code="semantic_field_contradicted",
            semantic_group=rule.semantic_group,
            evidence_spans=forbidden,
        )
    positive = _matches(rule.positive, text)
    if not positive:
        if _GENERIC_PREFERENCE.search(text) is not None:
            return SemanticFieldEvidence(
                status="generic_ambiguity",
                reason_code="generic_semantic_ambiguity",
                semantic_group=rule.semantic_group,
            )
        return SemanticFieldEvidence(
            status="insufficient",
            reason_code="semantic_field_evidence_missing",
            semantic_group=rule.semantic_group,
        )
    return SemanticFieldEvidence(
        status="supported",
        reason_code="semantic_field_evidence_supported",
        semantic_group=rule.semantic_group,
        evidence_spans=positive[:3],
    )


def deterministic_anchor_field_ids(
    text: str,
    *,
    product_family: ProductFamily,
    operation: SemanticOperation = "rank",
) -> frozenset[str]:
    matches = {
        rule.field_id
        for rule in _RULES
        if rule.deterministic_anchor
        and product_family in rule.families
        and operation in rule.operations
        and semantic_field_evidence(
            text,
            field_id=rule.field_id,
            product_family=product_family,
            operation=operation,
        ).supported
    }
    return frozenset(matches)


def constrain_semantic_candidate_fields(
    text: str,
    *,
    product_family: ProductFamily,
    operation: SemanticOperation,
    eligible_field_ids: set[str],
) -> set[str]:
    if operation == "rank":
        eligible_field_ids = eligible_field_ids - _METADATA_RANKING_FIELDS
    activated = {
        field_id
        for field_id in eligible_field_ids
        if (rule := semantic_rule(field_id)) is not None
        and product_family in rule.families
        and operation in rule.operations
    }
    anchors = deterministic_anchor_field_ids(
        text,
        product_family=product_family,
        operation=operation,
    )
    if anchors:
        return activated.intersection(anchors)
    return {
        field_id
        for field_id in activated
        if semantic_field_evidence(
            text,
            field_id=field_id,
            product_family=product_family,
            operation=operation,
        ).status
        != "contradicted"
    }


def is_generic_semantic_ambiguity(
    text: str,
    *,
    product_family: ProductFamily,
    operation: SemanticOperation = "rank",
) -> bool:
    if _GENERIC_PREFERENCE.search(text) is None:
        return False
    return not any(
        semantic_field_evidence(
            text,
            field_id=rule.field_id,
            product_family=product_family,
            operation=operation,
        ).supported
        for rule in _RULES
        if product_family in rule.families and operation in rule.operations
    )


def evidence_spans_are_grounded(
    provided: tuple[str, ...],
    *,
    residual_span: str,
    server_evidence: tuple[str, ...],
) -> bool:
    if not provided or not server_evidence:
        return False
    if any(not value.strip() or value not in residual_span for value in provided):
        return False
    return any(left in right or right in left for left in provided for right in server_evidence)


__all__ = [
    "SemanticFieldEvidence",
    "SemanticFieldRule",
    "constrain_semantic_candidate_fields",
    "deterministic_anchor_field_ids",
    "evidence_spans_are_grounded",
    "is_generic_semantic_ambiguity",
    "semantic_field_evidence",
    "semantic_rule",
]
