from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from finance_agent_core.agent.numeric_language import requested_result_limit
from finance_agent_core.agent.presentation import semantic_query_surface
from finance_agent_core.agent.safety import normalize_user_question
from finance_agent_core.agent.semantic_aliases import (
    explicit_product_name_order,
    filter_alias_matches,
    rank_alias_matches,
    ranking_cue_spans,
    ranking_intent_present,
)
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    search_projection,
)

NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
SCALED_NUMBER = (
    rf"{NUMBER}\s*(?:천억\s*원|백억\s*원|십억\s*원|조\s*원|억\s*원|만\s*원|"
    r"천억원|백억원|십억원|조원|억원|만원|천억|백억|십억|억|조|만|원)?"
)
_NEGATED_LITERAL_SUFFIX = re.compile(
    r"^\s*(?:형|유형|지역|시장|방식|상품|종목)?\s*"
    r"(?:가|이|은|는|을|를|으로|로|이나|나)?\s*"
    r"(?:아닌|아니|아님|지\s*않|하지\s*않|없|제외|이외|말고|빼고|"
    r"외(?:의|에는)?(?![0-9A-Za-z가-힣]))"
)

_SEMANTIC_COVERAGE_GATE = SemanticCoverageGate()


def _explicit_projection_fields(question: str, family: str) -> list[str]:
    """Link field labels and aliases that the user explicitly asks to see."""

    registry = load_field_registry()
    normalized_question = question.casefold()
    requested: list[str] = []
    for field, definition in registry.fields.items():
        if family not in definition.datasets or not definition.selectable:
            continue
        phrases = (definition.label, *definition.aliases)
        if any(phrase.casefold() in normalized_question for phrase in phrases if phrase.strip()):
            requested.append(field)
    return requested


def _negated_literal_span(question: str, literal: str) -> str | None:
    """Return local text when an identical literal is semantically negated.

    A short lexical match must not hide the suffix in ``ETF가 아닌`` or
    ``미국을 제외한``.  If the same token appears once positively and once
    negatively, the string-only linker cannot identify which occurrence a
    downstream model meant, so the whole request still fails closed.
    """

    start = 0
    while True:
        index = question.find(literal, start)
        if index < 0:
            return None
        suffix = question[index + len(literal) :][:20]
        match = _NEGATED_LITERAL_SUFFIX.search(suffix)
        if match is not None:
            return question[index : index + len(literal) + match.end()].strip()
        start = index + max(len(literal), 1)


def _negated_comparison_span(
    question: str,
    aliases: tuple[str, ...],
) -> str | None:
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    match = re.search(
        rf"(?:{alias_pattern}).{{0,30}}?"
        r"(?:이하|미만|이상|초과|정확히|이전|이후|까지)"
        r"\s*(?:가|이|은|는)?\s*(?:아닌|아니|아님)",
        question,
        flags=re.IGNORECASE,
    )
    return None if match is None else match.group(0)


def _product_family(question: str) -> str | None:
    if "공모펀드" in question:
        return "fund"
    if re.search(r"상장\s*지수\s*펀드", question):
        return "domestic_etp"
    mentions_etp = re.search(r"ETF|ETN|ETP", question)
    if not mentions_etp and re.search(
        r"채권|회사채|국채|국공채|국고채|특수채|금융채|지역개발채|도시철도공채",
        question,
    ):
        return "bond"
    if "국내" in question:
        return "domestic_etp"
    if "해외" in question:
        return "overseas_etp"
    return None


def _scaled_number(text: str) -> int | float:
    compact = (
        text.replace(",", "")
        .replace(" ", "")
        .replace("%", "")
        .replace("배", "")
        .removesuffix("일")
        .removesuffix("년")
    )
    scales = {
        "천억원": Decimal("100000000000"),
        "백억원": Decimal("10000000000"),
        "십억원": Decimal("1000000000"),
        "조원": Decimal("1000000000000"),
        "억원": Decimal("100000000"),
        "만원": Decimal("10000"),
        "천억": Decimal("100000000000"),
        "백억": Decimal("10000000000"),
        "십억": Decimal("1000000000"),
        "억": Decimal("100000000"),
        "조": Decimal("1000000000000"),
        "만": Decimal("10000"),
        "원": Decimal("1"),
    }
    scale = Decimal("1")
    for suffix in scales:
        if compact.endswith(suffix):
            compact = compact[: -len(suffix)]
            scale = scales[suffix]
            break
    value = Decimal(compact) * scale
    return int(value) if value == value.to_integral_value() else float(value)


def _numeric_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    between = re.search(
        rf"(?:{alias_pattern}).{{0,12}}?({SCALED_NUMBER})\s*(?:%|배|일|년|주)?\s*"
        rf"(?:달러|USD|KRW)?\s*에서\s*({SCALED_NUMBER})\s*(?:%|배|일|년|주)?\s*"
        rf"(?:달러|USD|KRW)?\s*사이",
        question,
        flags=re.IGNORECASE,
    )
    if between:
        return {
            "field": field,
            "operator": "between",
            "value": [_scaled_number(between.group(1)), _scaled_number(between.group(2))],
        }
    bounded = re.search(
        rf"(?:{alias_pattern}).{{0,12}}?({SCALED_NUMBER})\s*(?:%|배|일|년|주)?\s*"
        r"이상\s*(?:이고|이면서|,|부터)?\s*"
        rf"({SCALED_NUMBER})\s*(?:%|배|일|년|주)?\s*이하",
        question,
        flags=re.IGNORECASE,
    )
    if bounded:
        return {
            "field": field,
            "operator": "between",
            "value": [_scaled_number(bounded.group(1)), _scaled_number(bounded.group(2))],
        }
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({SCALED_NUMBER})\s*(?:%|배|일|년|주)?\s*"
        r"(?:달러|USD|KRW)?(?:을|를)?\s*"
        r"(이하|미만|이상|초과|정확히|넘는|넘은|넘게|넘지\s*않는|"
        r"초과하지\s*않는)",
        question,
        flags=re.IGNORECASE,
    )
    if comparison:
        operators = {
            "이하": "lte",
            "미만": "lt",
            "이상": "gte",
            "초과": "gt",
            "정확히": "eq",
            "넘는": "gt",
            "넘은": "gt",
            "넘게": "gt",
            "넘지 않는": "lte",
            "초과하지 않는": "lte",
        }
        boundary = re.sub(r"\s+", " ", comparison.group(2).strip())
        return {
            "field": field,
            "operator": operators[boundary],
            "value": _scaled_number(comparison.group(1)),
        }
    if field == "leverage_factor":
        direct = re.search(rf"({NUMBER})\s*배", question)
        if direct:
            return {
                "field": field,
                "operator": "eq",
                "value": _scaled_number(direct.group(1)),
            }
    return None


def _excluded_numeric_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    """Invert a simple excluded numeric range into an executable constraint.

    ``잔존일수 365일 초과는 빼고`` means ``remaining_days <= 365``.
    Treating the visible ``초과`` as a positive constraint silently reverses
    the request, so only this small, explicit exclusion grammar is lowered.
    """

    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({SCALED_NUMBER})\s*(?:%|배|일|년)?\s*"
        r"(?:달러|USD|KRW)?\s*(이하|미만|이상|초과)"
        r".{0,20}?(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
        flags=re.IGNORECASE,
    )
    if comparison is None:
        return None
    inverse_operators = {
        "이하": "gt",
        "미만": "gte",
        "이상": "lt",
        "초과": "lte",
    }
    return {
        "field": field,
        "operator": inverse_operators[comparison.group(2)],
        "value": _scaled_number(comparison.group(1)),
    }


def _date_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    iso_date = r"\d{4}-\d{2}-\d{2}"
    between = re.search(
        rf"(?:{alias_pattern}).{{0,12}}?({iso_date})\s*에서\s*"
        rf"({iso_date})\s*사이",
        question,
    )
    if between:
        return {
            "field": field,
            "operator": "between",
            "value": [between.group(1), between.group(2)],
        }
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({iso_date})\s*"
        r"(이전|이후|이하|이상|초과|정확히|까지)",
        question,
    )
    if comparison:
        operators = {
            "이전": "lt",
            "이후": "gt",
            "이하": "lte",
            "이상": "gte",
            "초과": "gt",
            "정확히": "eq",
            "까지": "lte",
        }
        return {
            "field": field,
            "operator": operators[comparison.group(2)],
            "value": comparison.group(1),
        }
    return None


def build_lexical_hints(
    question: str,
    product_family_hint: str | None = None,
    *,
    force_product_family_hint: bool = False,
) -> dict[str, Any]:
    question = semantic_query_surface(normalize_user_question(question))
    question = re.sub(
        r"(?m)(?:^|(?<=[.!?])\s+)"
        r"순위값의\s*방향이\s*높은\s*순인지\s*낮은\s*순인지"
        r"[^.!?]*(?:[.!?]|$)",
        " ",
        question,
        flags=re.IGNORECASE,
    ).strip()
    family = (
        product_family_hint
        if force_product_family_hint and product_family_hint in SEARCH_PROJECTION_BY_FAMILY
        else _product_family(question)
    )
    if family is None and product_family_hint in SEARCH_PROJECTION_BY_FAMILY:
        family = product_family_hint
    required: list[dict[str, Any]] = []
    semantic_ambiguity_spans: list[str] = []
    semantic_unsupported_spans: list[str] = []

    def add(field: str, value: Any, operator: str = "eq") -> None:
        hint = {"field": field, "operator": operator, "value": value}
        if hint not in required:
            required.append(hint)

    negated_product_types = {
        product_type: span
        for product_type in ("ETF", "ETN")
        if (span := _negated_literal_span(question, product_type)) is not None
    }
    explicit_product_type = re.search(
        r"(?:ETP\s*유형|ETF\s*여부|상품\s*유형)(?:이|가|은|는)?\s*[:：]?\s*(ETF|ETN)",
        question,
        re.IGNORECASE,
    )
    if explicit_product_type is None:
        explicit_product_type = re.search(
            r"(?<![A-Z])(ETF|ETN)(?:\s*유형(?:이|가|은|는)?\s*"
            r"(?:인|이며|이고|인데|에\s*해당)|"
            r"라면|이라면|인(?!지)|이며|이고|인데|에\s*해당|로\s*되어)",
            question,
            re.IGNORECASE,
        )
    explicit_product_type_set = re.search(
        r"(?:ETP\s*유형|ETF\s*ETN\s*구분|상품\s*유형)"
        r"(?:이|가|은|는)?\s*[:：]?\s*"
        r"ETF\s*(?:또는|및|와|과|나|·|/)\s*ETN|"
        r"ETF인지\s*ETN인지\s*(?:알려|구분|확인)",
        question,
        re.IGNORECASE,
    )
    conditional_product_type = re.search(
        r"(?<![A-Z])(ETF|ETN)(?:라면|이라면)",
        question,
        re.IGNORECASE,
    )
    if explicit_product_type_set is not None and conditional_product_type is None:
        for product_type in ("ETF", "ETN"):
            if product_type not in negated_product_types:
                add("product_type", product_type)
    elif (
        explicit_product_type is not None
        and explicit_product_type.group(1).upper() not in negated_product_types
    ):
        add("product_type", explicit_product_type.group(1).upper())
    else:
        positive_product_types = [
            product_type
            for product_type in ("ETF", "ETN")
            if re.search(rf"(?<![A-Z]){product_type}(?![A-Z])", question, re.IGNORECASE)
            and product_type not in negated_product_types
        ]
        if len(positive_product_types) == 1:
            add("product_type", positive_product_types[0])
    if family in {"domestic_etp", "overseas_etp"}:
        for product_type in negated_product_types:
            add("product_type", product_type, "neq")
        if len(negated_product_types) == 2:
            semantic_ambiguity_spans.append("ETF·ETN 모두 제외")
    else:
        semantic_ambiguity_spans.extend(negated_product_types.values())
    if family == "fund":
        negated_public = _negated_literal_span(question, "공모")
        if negated_public is None:
            add("public_offering", True)
        else:
            semantic_unsupported_spans.append(negated_public)

    common_lookup_patterns = [
        (
            r"상품\s*(?:ID|아이디|번호)(?:가|는|은|이)?\s*[:：]?\s*"
            r"([A-Z0-9._:-]{2,30})",
            "product_id",
        ),
        (
            r"(?:종목\s*(?:코드|번호)|티커)(?:가|는|은|이)?\s*[:：]?\s*"
            r"([A-Z0-9._:-]{2,30})",
            "ticker",
        ),
    ]
    for pattern, field in common_lookup_patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            identity = match.group(1)
            if negated_identity := _negated_literal_span(question, identity):
                semantic_ambiguity_spans.append(negated_identity)
            else:
                canonical_identity_field = (
                    "product_id" if family == "fund" and field == "ticker" else field
                )
                add(canonical_identity_field, identity, "eq")
    explicit_name_contains = re.search(
        r"(?P<value>[0-9A-Za-z가-힣&·+._/-]{2,80})\s*"
        r"(?:이름|상품명)(?:이|에)?\s*(?:들어간|붙은|포함된)",
        question,
        flags=re.IGNORECASE,
    )
    if explicit_name_contains is not None:
        add("product_name", explicit_name_contains.group("value").strip(), "contains")
    for match in re.finditer(
        r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9._:-]{1,29})"
        r"\s*(?:을|를|은|는)?\s*(?:제외|이외|말고|빼고|외(?:에는|의)?)",
        question,
        flags=re.IGNORECASE,
    ):
        if match.group(1).upper() in {"ETF", "ETN", "ETP"}:
            continue
        semantic_ambiguity_spans.append(match.group(0))

    explicit_fund_scope: re.Match[str] | None = None
    explicit_fund_currency: re.Match[str] | None = None
    if family == "overseas_etp":
        explicit_fund_currency = re.search(
            r"(?:펀드|상품)\s*통화\s*코드(?:가|는|은|이)?\s*([A-Z]{3})",
            question,
            flags=re.IGNORECASE,
        )
    if family == "domestic_etp":
        if re.search(r"상장\s*지수\s*펀드", question):
            add("product_type", "ETF")
        if re.search(r"투자\s*지역(?:이|은|가)?\s*국내|국내(?:에|로)\s*투자", question):
            add("investment_region", "국내")
        if re.search(r"국내\s*지역(?:에|으로)\s*투자", question):
            add("investment_region", "국내")
        if re.search(r"주식(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "주식")
        if re.search(r"채권(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "채권")
        if re.search(r"단기\s*자금(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "단기자금")
        for surface, value in (
            ("주식", "주식"),
            ("채권", "채권"),
            ("대체투자", "대체투자"),
            ("단기자금", "단기자금"),
        ):
            if re.search(
                rf"투자\s*자산\s*군(?:이|은|가)?\s*{re.escape(surface)}",
                question,
            ):
                add("asset_type", value)
        if re.search(r"^국내\s+(?:채권형|혼합자산형)", question):
            add("investment_region", "국내")
    elif family == "fund":
        explicit_fund_scope = re.search(
            r"해외\s*펀드\s*구분(?:이|은|가)?\s*(국내|해외)",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_fund_scope is not None:
            add("fund_geography_scope", explicit_fund_scope.group(1))
        domestic_investment = re.search(
            r"국내(?:에|로)?\s*투자",
            question,
            flags=re.IGNORECASE,
        )
        if domestic_investment is not None:
            add("investment_region", "국내")
    elif family == "overseas_etp":
        for surface, value in (
            ("Equity", "Equity"),
            ("Bond", "Bond"),
            ("Commodity", "Commodity"),
            ("Alternatives", "Alternatives"),
            ("Mixed Assets", "Mixed Assets"),
            ("Money Market", "Money Market"),
        ):
            if re.search(
                rf"(?:투자\s*자산\s*군|자산\s*군)(?:이|은|가)?\s*"
                rf"{re.escape(surface)}(?=\s|인|이고|이며|$)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}(?:로|으로)\s*(?:분류|표시|기재)(?:된|되어)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:투자\s*)?자산\s*군(?:의|에|인)?\s*"
                r"(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
        for surface, value in (
            ("United States of America", "United States of America"),
            ("Global Emerging Markets", "Global Emerging Markets"),
            ("Global Ex US", "Global Ex US"),
            ("Global", "Global"),
            ("China", "China"),
            ("Japan", "Japan"),
            ("India", "India"),
            ("Europe", "Europe"),
        ):
            if re.search(
                rf"(?:투자\s*지역(?:이|은|가)?\s*{re.escape(surface)}"
                r"(?=\s|인|이고|이며|$)|"
                rf"{re.escape(surface)}(?:로|으로)\s*(?:표시|분류|기재)(?:된|되어))",
                question,
                flags=re.IGNORECASE,
            ):
                add("investment_region", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:투자\s*)?지역(?:의|에|인)?\s*"
                r"(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("investment_region", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("investment_region", value)
        explicit_region_set = re.search(
            r"투자\s*지역(?:이|은|가)?\s*"
            r"((?:[A-Za-z ]+\s*[,·/]\s*)+[A-Za-z ]+)\s*중\s*하나",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_region_set is not None:
            allowed_regions = {
                "china": "China",
                "japan": "Japan",
                "india": "India",
                "europe": "Europe",
                "global": "Global",
            }
            for token in re.split(r"\s*[,·/]\s*", explicit_region_set.group(1)):
                if value := allowed_regions.get(token.strip().casefold()):
                    add("investment_region", value)
        if re.search(r"Global\s*Ex\s*US", question, flags=re.IGNORECASE):
            add("investment_region", "Global Ex US")
        for surface, value in (("NAS", "NAS"), ("NYS", "NYS"), ("AMX", "AMX")):
            if re.search(
                rf"거래소\s*코드(?:가|는|은|이)?\s*{surface}(?=\s|인|이고|이며|$)",
                question,
                flags=re.IGNORECASE,
            ):
                add("exchange_code", value)
            elif re.search(
                rf"{surface}\s*(?:거래소\s*코드|에서\s*거래)",
                question,
                flags=re.IGNORECASE,
            ):
                add("exchange_code", value)
        if re.search(r"글로벌\s*투자", question, flags=re.IGNORECASE):
            add("investment_region", "Global")

    # Static financial meanings are resolved by the one versioned Typed Alias
    # Registry shared with Router, Compiler coverage and the span ledger.
    # Dynamic values (numbers, dates, IDs and captured names) remain in their
    # dedicated parsers above and below this boundary.
    for alias in filter_alias_matches(question, family):
        if negated_alias := _negated_literal_span(question, alias.text):
            semantic_ambiguity_spans.append(negated_alias)
            continue
        add(alias.field_id, alias.value, alias.operator)

    etp_family = family in {"overseas_etp", "domestic_etp"}
    if etp_family and (
        leverage_factor := re.search(
            r"(?:레버리지\s*)?배수(?:가|는|은|이)?\s*"
            r"(-?\d+(?:\.\d+)?)\s*(?:인|로|배|$)",
            question,
            flags=re.IGNORECASE,
        )
    ):
        add("leverage_factor", float(leverage_factor.group(1)), "eq")
    negated_trade_available = re.search(
        r"(?:현재\s*)?거래(?:가|는|이)?\s*가능.{0,8}"
        r"(?:하지\s*않|아닌|없)",
        question,
    )
    if negated_trade_available is not None:
        semantic_ambiguity_spans.append(negated_trade_available.group(0))
    if etp_family and "현재 거래 가능" in question and negated_trade_available is None:
        add("sellable", True)
        add("trading_suspended", False)
    negated_trading_suspension = re.search(
        r"거래(?:가|는|이)?\s*(?:중지|정지)(?:가|는|이)?\s*"
        r"(?:되지\s*않|아니|아닌|아님)|"
        r"거래\s*(?:중지|정지)(?:된|인)?\s*(?:상품|것)?"
        r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|빼고|말고)|"
        r"거래(?:가|는|이)?\s*멈추지\s*않",
        question,
    )
    if etp_family and negated_trading_suspension is not None:
        add("trading_suspended", False)
    negated_sale_available = re.search(
        r"판매(?:가|이)?\s*가능.{0,8}(?:하지\s*않|아닌|없)",
        question,
    )
    if etp_family and negated_sale_available is not None:
        add("sellable", False)
    elif etp_family and re.search(r"판매(?:가|이)?\s*가능", question):
        add("sellable", True)
    if etp_family and ("판매할 수 없" in question or "판매 불가" in question):
        add("sellable", False)
    if (
        etp_family
        and negated_trading_suspension is None
        and re.search(
            r"거래(?:가)?\s*(?:중지|정지)"
            r"(?!\s*(?:(?:가\s*)?(?:아니|아닌|아님)|되지\s*않))"
            r"(?:된|됨| 상태)?",
            question,
        )
    ):
        add("trading_suspended", True)
    if family == "domestic_etp":
        excluded_pension_ineligible = re.search(
            r"연금\s*거래\s*(?:불가|불가능).{0,20}?"
            r"(?:제외(?:하고|한|해)?|빼고|말고)",
            question,
        )
        negated_pension = re.search(
            r"연금.{0,16}(?:거래|구매)?(?:가|는|이)?\s*가능.{0,8}"
            r"(?:하지\s*않|아닌|없)",
            question,
        )
        if excluded_pension_ineligible is not None:
            add("pension_eligible", True)
        elif negated_pension is not None:
            add("pension_eligible", False)
        elif (
            re.search(
                r"연금(?:\s*계좌)?(?:에서|으로|로|에서도|로도)?\s*"
                r"(?:(?:사고\s*팔|거래|구매)(?:가)?\s*(?:가능|(?:할\s*)?수\s*있)|"
                r"거래가?\s*가능)",
                question,
            )
            and re.search(r"연금.{0,16}?가능\s*여부", question) is None
        ):
            add("pension_eligible", True)
        if (
            negated_pension is None
            and re.search(r"연금.{0,16}?가능\s*여부", question) is None
            and re.search(
                r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:가능|Y)",
                question,
            )
        ):
            required[:] = [item for item in required if item["field"] != "pension_eligible"]
            add("pension_eligible", True)
        if re.search(r"주식(?:을|이)?\s*기초\s*자산", question):
            add("asset_type", "주식")
        if excluded_pension_ineligible is None and any(
            phrase in question
            for phrase in ("연금 거래가 불가능", "연금 거래 불가", "연금 거래 불가능")
        ):
            required[:] = [item for item in required if item["field"] != "pension_eligible"]
            add("pension_eligible", False)
        if "핵심" in question and "ETF" in question:
            add("core_etf", True)
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            loose = risk.replace("(", " ").replace(")", "")
            if risk in question or loose in question:
                add("risk_level", risk)
        for phrase, value in (
            ("실물복제", "실물복제"),
            ("합성복제", "합성복제"),
            ("액티브 전략", "액티브"),
            ("전략 코드 C", "C"),
        ):
            if phrase in question:
                add("strategy", value)

        lookup_patterns = [
            (
                r"(?:국내\s*)?ETF\s*[‘'\"]([^’'\"]+)[’'\"]",
                "short_name",
                "eq",
            ),
            (r"운용사에\s*(.+?)이 포함", "manager", "contains"),
            (r"약어명에\s*(.+?)(?:가|이) 들어", "short_name", "contains"),
            (r"기초지수에\s*(.+?)(?:가|이) 포함", "base_index", "contains"),
            (
                r"종목코드(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "ticker",
                "eq",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "product_id",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        manager = re.search(
            r"(.{1,40}?)\s*(?:가|이)\s*운용하는\s*(?:국내\s*)?(?:ETF|ETN)",
            question,
        )
        if manager is not None:
            add("manager", manager.group(1).strip(" '‘’\""), "eq")
        bare_manager = re.search(
            r"(?:^|[.!?。]\s*|(?:첫째|둘째|셋째|넷째),?\s*)"
            r"([0-9A-Za-z가-힣&·+._ -]{2,60}?)\s+운용\s+(?:국내\s*)?(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if bare_manager is not None:
            manager_value = bare_manager.group(1).strip(" '‘’\"")
            if (
                re.search(
                    r"(?:상품\s*(?:ID|아이디|번호)|종목\s*코드|티커)$",
                    manager_value,
                    flags=re.IGNORECASE,
                )
                is None
            ):
                add("manager", manager_value, "eq")
        stated_manager = re.search(
            r"([0-9A-Za-z가-힣&·+._/-]{1,40})(?:가|이)\s*"
            r"운용사(?:로)?\s*(?:적힌|표시|기재)(?:된|되어)?",
            question,
        )
        if stated_manager is not None:
            add("manager", stated_manager.group(1), "eq")
        leverage_factor = re.search(
            r"(?<!분)배수(?:\s*표기)?(?:가|는|은|이)?\s*([0-9]+(?:\.[0-9]+)?)",
            question,
        )
        if leverage_factor is not None:
            add("leverage_factor", float(leverage_factor.group(1)), "eq")

    if family == "fund":
        company_sale_context = re.search(
            r"(?:당사|미래에셋증권).{0,12}판매",
            question,
        )
        fund_sale_unavailable = re.search(
            r"판매(?:가|이)?\s*가능.{0,8}(?:하지\s*않|아닌|없)",
            question,
        )
        if fund_sale_unavailable is not None:
            add("sellable", False)
        elif (
            re.search(r"판매(?:가|이)?\s*가능|살\s*수\s*있", question)
            and company_sale_context is None
        ):
            add("sellable", True)
        if re.search(r"판매\s*중", question):
            add("sellable", True)
        if re.search(r"판매\s*중(?:으로)?\s*(?:적힌|표시|기재)", question):
            add("sellable", True)
        if any(phrase in question for phrase in ("판매가 완료", "판매 완료")):
            add("sellable", False)
        if any(
            phrase in question
            for phrase in (
                "당사에서 판매",
                "당사에서도 판매",
                "미래에셋증권에서 판매",
            )
        ) or re.search(r"당사\s*판매.{0,12}?(?:Y|가능)", question, re.IGNORECASE):
            add("company_sellable", True)
        if "개인용" in question or "개인 투자자 대상" in question or "개인으로 분류" in question:
            add("investor_type", "개인")
        if "법인용" in question or "법인 투자자 대상" in question or "법인으로 분류" in question:
            add("investor_type", "법인")
        excluded_unhedged = re.search(
            r"환헤지(?:를)?\s*하지\s*않는.{0,20}?"
            r"(?:제외(?:하고|한|해)?|빼고|말고)",
            question,
        )
        if excluded_unhedged is not None:
            add("currency_hedged", True)
        elif any(phrase in question for phrase in ("환헤지하지 않", "환헤지를 하지 않", "환노출")):
            add("currency_hedged", False)
        elif any(
            phrase in question for phrase in ("환헤지", "환율 변동을 막는", "환율 변동을 줄이는")
        ):
            add("currency_hedged", True)
        explicit_fund_risk = None
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            loose = risk.replace("(", " ").replace(")", "")
            if risk in question or loose in question:
                explicit_fund_risk = risk
                add("risk_level", risk)
                break
        exact_fund_risk_label = re.search(
            r"(?:(높은\s*위험|낮은\s*위험)으로\s*(?:표기|기재|적힌)|"
            r"위험\s*등급\s*명(?:이|은|가)?\s*(높은\s*위험|낮은\s*위험)(?:인|으로))",
            question,
        )
        if explicit_fund_risk is None and exact_fund_risk_label is not None:
            exact_label = next(
                value for value in exact_fund_risk_label.groups() if value is not None
            )
            add("risk_level", re.sub(r"\s+", " ", exact_label), "eq")
        fund_risk_aliases = (
            (r"매우\s*높은\s*위험(?:\s*\(?1등급\)?)?", ["매우 높은 위험", "매우높은위험(1등급)"]),
            (r"다소\s*높은\s*위험(?:\s*\(?3등급\)?)?", ["다소 높은 위험", "다소높은위험(3등급)"]),
            (
                r"(?<!매우\s)(?<!다소\s)높은\s*위험(?:\s*\(?2등급\)?)?",
                ["높은 위험", "높은위험", "높은위험(2등급)"],
            ),
            (r"보통\s*위험(?:\s*\(?4등급\)?)?", ["보통 위험", "보통위험", "보통위험(4등급)"]),
            (r"매우\s*낮은\s*위험(?:\s*\(?6등급\)?)?", ["매우 낮은 위험", "매우낮은위험(6등급)"]),
            (r"(?<!매우\s)낮은\s*위험(?:\s*\(?5등급\)?)?", ["낮은 위험", "낮은위험(5등급)"]),
        )
        if explicit_fund_risk is None and exact_fund_risk_label is None:
            for pattern, values in fund_risk_aliases:
                if re.search(pattern, question):
                    add("risk_level", values, "in")
        lookup_patterns = [
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)",
                "product_id",
                "eq",
            ),
            (r"짧은 이름에\s*(.+?)(?:가|이)\s*들어간", "short_name", "contains"),
            (
                r"(?:정식\s*)?상품명에\s*(.+?)(?:가|이)\s*포함된",
                "product_name",
                "contains",
            ),
            (
                r"클래스\s*판매채널(?:이|은|가)?\s*[:：]?\s*(온라인|오프라인)",
                "sales_channel",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        if re.search(r"온라인\s*클래스", question):
            add("sales_channel", "온라인")
        if re.search(r"오프라인\s*(?:판매채널|클래스)", question):
            add("sales_channel", "오프라인")
        if re.search(r"이자배당구분(?:이|은|가)?\s*배당|배당형", question):
            add("interest_dividend_type", "배당")
        if re.search(r"이자배당구분(?:이|은|가)?\s*이자|이자형", question):
            add("interest_dividend_type", "이자")
        if re.search(r"(?:수수료를?\s*따로\s*)?(?:징구하지\s*않|수수료\s*미징구)", question):
            add("fee_type", "수수료미징구")
        if re.search(r"(?:수수료\s*선취|선취\s*수수료)", question):
            add("fee_type", "수수료선취")
        for field, aliases in (
            ("domestic_stock_ratio_pct", r"국내주식\s*(?:구성비율|비중)"),
            ("domestic_bond_ratio_pct", r"국내채권\s*(?:구성비율|비중)"),
            ("overseas_stock_ratio_pct", r"해외주식\s*(?:구성비율|비중)"),
            ("overseas_bond_ratio_pct", r"해외채권\s*(?:구성비율|비중)"),
        ):
            if re.search(rf"{aliases}.{{0,16}}절반(?:을|보다)?\s*(?:넘|초과)", question):
                add(field, 50, "gt")

    if family == "bond":
        buy_unavailable = re.search(
            r"(?:매수|구매|주문)(?:가|는|이)?\s*가능.{0,8}"
            r"(?:하지\s*않|아닌|없)",
            question,
        )
        if buy_unavailable is not None:
            semantic_unsupported_spans.append(buy_unavailable.group(0))
        if re.search(
            r"(?:모집\s*구분(?:이|은|가)?\s*)?공모|공개\s*모집(?:된|한)?",
            question,
        ):
            add("public_offering", True)
        if re.search(r"(?:모집\s*구분(?:이|은|가)?\s*)?사모", question):
            add("public_offering", False)
        if re.search(r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:가능|Y)", question):
            add("pension_eligible", True)
        if re.search(
            r"(?:퇴직\s*)?연금(?:에|으로|계좌에)?\s*"
            r"(?:담|편입).{0,12}?(?:수\s*있|가능)",
            question,
        ):
            add("pension_eligible", True)
        if re.search(r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:불가|N)", question):
            add("pension_eligible", False)
        if re.search(r"온라인.{0,20}오프라인.{0,24}(?:모두|겸용|가능)", question):
            add("sales_channel", "온오프 겸용")
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            if risk in question or risk.replace("(", " ").replace(")", "") in question:
                add("risk_level", risk)
        # The organizer's 2026-08-24 rule invalidates BUYABLE_QUANTITY and
        # treats every provided, non-delisted bond as buyable.  The supplied
        # schema has no delisting field, so positive buyability wording is an
        # understood scope phrase, not a row-level filter.
        lookup_patterns = [
            (
                r"([0-9A-Za-z가-힣&·+._ -]{2,60}?)(?:가|이)\s*발행한\s*(?:국내\s*)?채권",
                "issuer",
                "eq",
            ),
            (r"발행(?:기관|사|자)에\s*(.+?)(?:가|이)?\s*포함", "issuer", "contains"),
            (r"발행(?:기관|사|자)\s*(.+?)의", "issuer", "contains"),
            (r"상품명에\s*(.+?)(?:가|이)?\s*포함", "product_name", "contains"),
            (
                r"종목코드(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "ticker",
                "eq",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "product_id",
                "eq",
            ),
            (
                r"(?:신용\s*등급(?:이|은)?\s*"
                r"(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?:인|$|\s)|"
                r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*등급|"
                r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?:가|이)?\s*적용된)",
                "credit_rating",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = next(group for group in match.groups() if group is not None).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        connected_ratings = re.findall(
            r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?![A-Z])",
            question,
            flags=re.IGNORECASE,
        )
        if len(set(connected_ratings)) >= 2 and re.search(r"(?:또는|이나|나|혹은|,)", question):
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            for rating in dict.fromkeys(value.upper() for value in connected_ratings):
                add("credit_rating", rating, "eq")
        elif bare_rating := re.search(
            r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*채권",
            question,
            flags=re.IGNORECASE,
        ):
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add("credit_rating", bare_rating.group(1).upper(), "eq")
        remaining_year_range = re.search(
            r"(?:잔여|잔존)\s*기간(?:이|은|가|을|를)?\s*"
            r"(\d+)\s*년\s*이상\s*(\d+)\s*년\s*이하",
            question,
        )
        if remaining_year_range is not None:
            lower, upper = map(int, remaining_year_range.groups())
            add("remaining_days", [lower * 365, upper * 365], "between")
        maturity_within_years = re.search(
            r"(\d+)\s*년\s*(?:안|이내)에?.{0,24}?(?:만기|상환)|"
            r"(?:만기|상환).{0,24}?(\d+)\s*년\s*(?:안|이내)",
            question,
        )
        if maturity_within_years is not None:
            years = next(int(value) for value in maturity_within_years.groups() if value)
            add("remaining_days", years * 365, "lte")
        maturity_years = re.search(
            r"(?:잔존\s*)?만기(?:가|는|를|는\s*기간)?\s*(\d+)\s*년\s*"
            r"(이하|미만|이상|초과)",
            question,
        )
        if maturity_years:
            days = int(maturity_years.group(1)) * 365
            boundary = maturity_years.group(2)
            if boundary == "이하":
                add("remaining_days", [0, days], "between")
            elif boundary == "미만":
                add("remaining_days", [0, max(0, days - 1)], "between")
            elif boundary == "이상":
                add("remaining_days", days, "gte")
            else:
                add("remaining_days", days, "gt")
        if remaining_days := re.search(
            r"(\d+)\s*일\s*(?:안|이내)에?.{0,24}?(?:만기|상환)",
            question,
        ):
            add("remaining_days", int(remaining_days.group(1)), "lte")
        if re.search(r"(?:앞으로\s*)?(?:반년|6\s*개월)\s*안(?:에)?", question):
            add("remaining_days", 180, "lte")
        elif re.search(
            r"(?:앞으로\s*)?(?:1\s*년|일\s*년)\s*(?:안|내)(?:에)?"
            r"(?:.{0,16}?(?:만기|상환))?",
            question,
        ):
            add("remaining_days", 365, "lte")
        elif re.search(r"만기까지\s*2\s*년(?:을)?\s*넘기지\s*않", question):
            add("remaining_days", 730, "lte")
        rating_group = re.search(
            r"(?<![A-Z])(AA|A|BBB|BB|B)\s*급(?:\s*\(([^)]*)\))?",
            question,
            flags=re.IGNORECASE,
        )
        if rating_group is not None:
            explicit_values = re.findall(
                r"AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-",
                rating_group.group(2) or "",
                flags=re.IGNORECASE,
            )
            defaults = {
                "AA": ["AA+", "AA0", "AA-"],
                "A": ["A+", "A0", "A-"],
                "BBB": ["BBB+", "BBB0", "BBB-"],
                "BB": ["BB+", "BB0", "BB-"],
                "B": ["B+", "B0", "B-"],
            }
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add(
                "credit_rating",
                [value.upper() for value in explicit_values]
                or defaults[rating_group.group(1).upper()],
                "in",
            )
    if family == "overseas_etp":
        lookup_patterns = [
            (
                r"(?:종목코드|티커)(?:가|는|은|이)?\s*[:：]?\s*"
                r"([A-Z0-9._-]+)(?:인|의|입니다|$|\s)",
                "ticker",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*"
                r"((?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+)(?:인|의|입니다|$|\s)",
                "product_id",
            ),
        ]
        for pattern, field in lookup_patterns:
            match = re.search(pattern, question, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, "eq")
        manager = re.search(
            r"(.{1,80}?)\s*(?:가|이)\s*운용하는\s*(?:해외\s*)?(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if manager is not None:
            add("manager", manager.group(1).strip(" '‘’\""), "eq")
        bare_manager = re.search(
            r"(?:^|[.!?。]\s*|(?:첫째|둘째|셋째|넷째),?\s*)"
            r"([0-9A-Za-z가-힣&·+._ -]{2,80}?)"
            r"(?:의\s+(?:해외\s*)?|\s+운용\s+(?:해외\s*)?)(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if bare_manager is not None:
            manager_value = bare_manager.group(1).strip(" '‘’\"")
            if (
                re.search(
                    r"(?:상품\s*(?:ID|아이디|번호)|종목\s*코드|티커)$",
                    manager_value,
                    flags=re.IGNORECASE,
                )
                is None
            ):
                add("manager", manager_value, "eq")
        if re.search(r"인버스\s*(?:또는|·|/)?\s*숏\s*여부.{0,12}(?:Y|예)", question):
            add("inverse_short", True)
        if re.search(r"지수\s*추적\s*여부.{0,12}(?:Y|예)", question):
            add("index_tracking", True)
        if explicit_fund_currency is not None:
            add("fund_currency", explicit_fund_currency.group(1).upper())
        listing_date = re.search(
            r"(?:설정일|상장일)(?:이|가)?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*"
            r"(\d{1,2})\s*일\s*(이후|이상|초과|이전|이하|미만)",
            question,
        )
        if listing_date is not None:
            year, month, day, boundary = listing_date.groups()
            operator = {
                "이후": "gte",
                "이상": "gte",
                "초과": "gt",
                "이전": "lte",
                "이하": "lte",
                "미만": "lt",
            }[boundary]
            add("listing_date", f"{year}-{int(month):02d}-{int(day):02d}", operator)

    if family == "bond":
        numeric_fields = [
            (
                "issue_amount",
                ("발행잔액", "남은 발행 물량", "남아 있는 발행 물량"),
            ),
            ("total_issue_amount", ("총발행금액", "총 발행금액", "총 발행 규모", "총발행액")),
            ("coupon_rate_pct", ("표면이율", "표면이자율", "쿠폰금리", "표면금리", "액면 쿠폰")),
            ("buy_yield_pct", ("매수수익률", "매수 수익률")),
            ("after_tax_yield_pct", ("세후수익률", "세후 수익률")),
            (
                "applied_yield_pct",
                (
                    "민평수익률",
                    "민평 수익률",
                    "민평금리",
                    "민평 금리",
                    "민평 기준 수익률",
                    "민간평가 금리",
                    "평가기관 수익률",
                    "민평 산출 수익률",
                ),
            ),
            (
                "evaluation_price",
                (
                    "평가일단가",
                    "평가단가",
                    "평가가격",
                    "클린 프라이스",
                    "이자 제외 평가단가",
                    "이자 제외 평가가격",
                    "이자를 뺀 평가가격",
                ),
            ),
            (
                "dirty_price",
                ("이자부단가", "경과이자 포함 단가", "경과이자 포함 가격"),
            ),
            ("convexity", ("컨벡시티", "가격 굴곡")),
            ("buyable_quantity", ("매수가능수량", "매수 가능 수량")),
            ("remaining_days", ("잔존일수", "잔존일")),
            ("duration_years", ("듀레이션", "금리 민감도")),
        ]
    elif family == "fund":
        numeric_fields = [
            ("aum", ("AUM", "순자산", "운용 자산")),
            ("sale_company_fee_pct", ("판매회사보수", "판매 회사 보수", "판매보수")),
            (
                "management_company_fee_pct",
                ("집합투자업자보수", "집합 투자 업자 보수", "운용사보수"),
            ),
            ("trustee_fee_pct", ("신탁업자보수", "신탁 업자 보수", "수탁보수")),
            ("domestic_stock_ratio_pct", ("국내주식 구성비율", "국내주식 비중")),
            ("domestic_bond_ratio_pct", ("국내채권 구성비율", "국내채권 비중")),
            ("overseas_stock_ratio_pct", ("해외주식 구성비율", "해외주식 비중")),
            ("overseas_bond_ratio_pct", ("해외채권 구성비율", "해외채권 비중")),
            ("latest_distribution_pct", ("최근 분배율", "최신 분배율")),
            ("preservation_ratio_pct", ("보전율", "보전 비율")),
            ("one_week_return_pct", ("1주 수익률", "일주일 수익률", "1주 성과")),
            ("one_month_return_pct", ("1개월 수익률", "한 달 수익률", "한 달 성과")),
            ("three_month_return_pct", ("3개월 수익률", "석 달 수익률", "3개월 성과")),
            ("six_month_return_pct", ("6개월 수익률", "반년 수익률", "6개월 성과")),
            ("one_year_return_pct", ("1년 수익률", "1년 성과", "1Y 수익률")),
            ("eighteen_month_return_pct", ("18개월 수익률", "18개월 성과", "18M 수익률")),
            ("two_year_return_pct", ("2년 수익률", "2년 성과", "2Y 수익률")),
            ("three_year_return_pct", ("3년 수익률", "3년 성과", "3Y 수익률")),
            ("five_year_return_pct", ("5년 수익률", "5년 성과", "5Y 수익률")),
        ]
    else:
        numeric_fields = [
            (
                "total_expense_ratio_pct",
                ("총보수", "보수", "연간보수", "연간보수율", "보수율"),
            ),
            ("aum", ("AUM", "순자산", "일간 순자산총액", "최종 순자산")),
            ("close_price", ("종가", "마감 가격")),
            ("daily_trading_value", ("일 거래대금", "거래대금", "외화거래대금")),
            (
                "daily_volume",
                ("일 거래량", "일거래량", "거래량", "일간 거래량", "하루 거래량"),
            ),
            ("discrepancy_pct", ("괴리율", "가격 괴리율", "NAV 괴리율")),
        ]
    if family == "domestic_etp":
        numeric_fields.extend(
            [
                ("net_asset_value", ("최종 NAV", "NAV", "순자산가치", "최종 기준가")),
                ("tracking_error_pct", ("추적오차율", "추적 오차율", "추적 오차")),
                (
                    "twenty_day_volatility_pct",
                    (
                        "20거래일 연환산 변동성",
                        "20거래일 변동성",
                        "20일 변동성",
                        "1개월 변동성",
                        "20거래일 흔들림",
                    ),
                ),
                (
                    "sixty_day_volatility_pct",
                    ("60거래일 변동성", "60일 변동성", "3개월 변동성", "60거래일 흔들림"),
                ),
                (
                    "one_hundred_twenty_day_volatility_pct",
                    ("120거래일 변동성", "120일 변동성", "6개월 변동성", "120거래일 흔들림"),
                ),
                (
                    "two_hundred_fifty_two_day_volatility_pct",
                    ("252거래일 변동성", "252일 변동성", "1년 변동성", "252거래일 흔들림"),
                ),
                (
                    "distribution_yield_pct",
                    ("분배수익률", "분배 수익률", "연환산 분배수익률"),
                ),
                (
                    "distribution_payments_per_year",
                    (
                        "연간 분배 횟수",
                        "연간 분배 지급횟수",
                        "연간 분배 지급 횟수",
                        "분배 지급 횟수",
                        "분배 횟수",
                    ),
                ),
                ("one_day_return_pct", ("1일 수익률", "하루 수익률", "일간 수익률")),
                (
                    "one_month_return_pct",
                    ("1개월 수익률", "한 달 수익률", "한 달 성과", "1M 수익률"),
                ),
                ("three_month_return_pct", ("3개월 수익률", "3M 수익률")),
                ("six_month_return_pct", ("6개월 수익률", "반년 수익률", "6M 수익률")),
                ("one_year_return_pct", ("1년 수익률", "1Y 수익률")),
                ("ytd_return_pct", ("YTD 수익률", "연초 이후 수익률")),
                (
                    "leverage_factor",
                    ("레버리지 배수", "레버리지배수", "레버리지", "배율"),
                ),
            ]
        )
    elif family == "overseas_etp":
        numeric_fields.extend(
            [
                (
                    "leverage_factor",
                    ("레버리지 배수", "레버리지배수", "레버리지", "배율"),
                ),
                (
                    "one_day_return_pct",
                    ("1일 수익률", "하루 수익률", "당일 수익률", "일간 수익률"),
                ),
                ("base_price", ("기준가", "펀드 기준가")),
            ]
        )
    for field, aliases in numeric_fields:
        if excluded_hint := _excluded_numeric_hint(question, field, aliases):
            add(
                excluded_hint["field"],
                excluded_hint["value"],
                excluded_hint["operator"],
            )
            continue
        if negated_comparison := _negated_comparison_span(question, aliases):
            semantic_ambiguity_spans.append(negated_comparison)
            continue
        hint = _numeric_hint(question, field, aliases)
        if hint is not None:
            add(hint["field"], hint["value"], hint["operator"])
    if family == "domestic_etp" and (
        payment_count := re.search(
            r"(?:연(?:간)?\s*)?(?:분배\s*)?(?:지급\s*)?횟수(?:가|는|은|이)?\s*"
            r"(\d+)\s*회|연(?:간)?\s*(\d+)\s*회\s*(?:분배|지급)|"
            r"(?:분배금을?\s*)?(?:해마다|매년|연간)\s*(\d+)\s*(?:번|회)\s*지급",
            question,
        )
    ):
        add(
            "distribution_payments_per_year",
            int(payment_count.group(1) or payment_count.group(2) or payment_count.group(3)),
            "eq",
        )
    if family == "fund" and re.search(
        r"(?:3개월|석 달)\s*(?:수익률|수익|성과).{0,12}마이너스가 아닌",
        question,
    ):
        add("three_month_return_pct", 0, "gte")
    if family == "bond":
        for field, aliases in [
            ("issue_date", ("발행일",)),
            ("maturity_date", ("만기일",)),
        ]:
            if negated_comparison := _negated_comparison_span(question, aliases):
                semantic_ambiguity_spans.append(negated_comparison)
                continue
            hint = _date_hint(question, field, aliases)
            if hint is not None:
                add(hint["field"], hint["value"], hint["operator"])
        issued_since_year = re.search(
            r"(\d{4})\s*년\s*(?:이후|부터)\s*발행(?:된|한)?",
            question,
        )
        if issued_since_year is not None:
            add("issue_date", f"{issued_since_year.group(1)}-01-01", "gte")
        maturity_year_end = re.search(
            r"(?:상환|만기)(?:일자|일)?(?:가|는|은|이)?\s*"
            r"(\d{4})\s*년\s*말(?:까지|이전|이하)",
            question,
        )
        if maturity_year_end is not None:
            add("maturity_date", f"{maturity_year_end.group(1)}-12-31", "lte")
        remaining_years = re.search(
            r"(?:(?:상환|만기)(?:까지)?\s*|(?:잔여|잔존)\s*기간(?:이|은|가)?\s*)"
            r"(\d+)\s*년\s*(이상|초과|이하|미만)(?:\s*남(?:은|아|은\s*것))?",
            question,
        )
        if remaining_years is not None:
            operator = {
                "이상": "gte",
                "초과": "gt",
                "이하": "lte",
                "미만": "lt",
            }[remaining_years.group(2)]
            add("remaining_days", int(remaining_years.group(1)) * 365, operator)

    unsupported_patterns = [
        "배당수익률",
        "원화로 환산",
        "가격 전망",
    ]
    if family == "fund":
        unsupported_patterns.extend(
            [
                "운용사 이름",
                "오늘 기준 최신 수익률",
                "총보수",
                "판매수수료",
                "대표 펀드",
                "클래스는 합쳐서",
            ]
        )
    elif family == "bond":
        unsupported_patterns.extend(["1일 수익률", "3개월 수익률", "국내 ETF"])
    elif family == "overseas_etp":
        unsupported_patterns.extend(["3개월 수익률", "국내 ETF"])
    if family == "bond":
        unsupported_patterns.extend(["AUM", "총보수", "거래정지"])
    ambiguity_patterns = ["적당한", "안전한", "괜찮은"]
    for match in re.finditer(
        r"(?:AUM|순자산|운용\s*자산|총보수|보수|수익률|거래대금|종가|"
        r"잔존\s*(?:일수|일|기일)|듀레이션|발행잔액|매수\s*수익률)"
        r".{0,14}(?:크|높|낮|작|적|짧|길)지\s*않",
        question,
        flags=re.IGNORECASE,
    ):
        semantic_ambiguity_spans.append(match.group(0))

    rankings: list[dict[str, str]] = []
    ascending = any(
        phrase in question
        for phrase in (
            "오름차순",
            "낮은 순",
            "낮은 쪽부터",
            "적은 순",
            "적은 쪽부터",
            "가장 적은",
            "작은 순",
            "작은 쪽부터",
            "짧은 순",
            "짧은 쪽부터",
            "빠른 순",
            "가까운 순",
            "오래된 순",
            "lowest-to-highest",
            "ascending",
        )
    )
    descending = (
        any(
            phrase in question
            for phrase in (
                "내림차순",
                "큰 순",
                "높은 순",
                "최신",
                "상위",
                "좋은",
                "성과순",
                "앞선",
                "가장 많이",
                "제일 큰",
                "높은 쪽부터",
                "큰 쪽부터",
                "긴 순",
                "highest-to-lowest",
                "descending",
            )
        )
        or re.search(r"\d+\s*(?:등(?!급)|위(?!험))", question) is not None
    )
    explicit_rank_patterns: list[tuple[str, str]] = []
    period_rank_fields = (
        (r"(?:1\s*일|하루|당일|일간|1D)", "one_day_return_pct"),
        (r"(?:1\s*개월|한\s*달|1M)", "one_month_return_pct"),
        (r"(?:3\s*개월|석\s*달|3M)", "three_month_return_pct"),
        (r"(?:6\s*개월|반년|6M)", "six_month_return_pct"),
        (r"(?:1\s*년|일\s*년|1Y)", "one_year_return_pct"),
        (r"(?:2\s*년|2Y)", "two_year_return_pct"),
        (r"(?:3\s*년|3Y)", "three_year_return_pct"),
        (r"(?:5\s*년|5Y)", "five_year_return_pct"),
        (r"(?:YTD|연초\s*이후)", "ytd_return_pct"),
    )
    period_rank_gap = (
        80
        if re.search(
            r"통합\s*(?:비교|정렬|순위)|직접\s*비교|한\s*줄\s*순위|"
            r"(?:한\s*순위에\s*)?합쳐|따로\s*나누지\s*말고",
            question,
            re.IGNORECASE,
        )
        else 24
    )
    for period, field_id in period_rank_fields:
        if re.search(
            rf"{period}\s*(?:수익률|수익|성과).{{0,{period_rank_gap}}}"
            r"(?:앞선|우수한|높은|낮은|상위|하위|좋은|순)",
            question,
            flags=re.IGNORECASE,
        ):
            explicit_rank_patterns.append(
                (
                    rf"{period}\s*(?:수익률|수익|성과).{{0,{period_rank_gap}}}"
                    r"(?:앞선|우수한|높은|낮은|상위|하위|좋은|순)",
                    field_id,
                )
            )
    if family == "fund":
        explicit_rank_patterns.append(
            (r"짧은 이름.{0,30}(?:순서|순으로|오름차순|내림차순|이름순)", "short_name")
        )
    explicit_rank_patterns.extend(
        [
            (
                r"(?:상품명|이름).{0,20}"
                r"(?:순서|순으로|오름차순|내림차순|이름순|가나다순)",
                "product_name",
            ),
            (r"(?:티커|종목코드).{0,20}(?:순서|순으로|오름차순|내림차순)", "ticker"),
        ]
    )
    if family == "bond":
        explicit_rank_patterns.extend(
            [
                (r"(?:매수수익률|매수 수익률).{0,30}(?:큰|작은|높은|낮은|순)", "buy_yield_pct"),
                (
                    r"(?:세후수익률|세후 수익률).{0,30}(?:큰|작은|높은|낮은|순)",
                    "after_tax_yield_pct",
                ),
                (
                    r"(?:민평\s*(?:(?:기준\s*)?수익률|금리)|민간평가\s*금리|평가기관\s*수익률|"
                    r"민평에서\s*(?:계산|산출)(?:된|한)\s*(?:수익률|금리)).{0,30}"
                    r"(?:센|큰|작은|높은|낮은|상위|하위|순)",
                    "applied_yield_pct",
                ),
                (
                    r"(?:평가일단가|평가\s*(?:단가|가격)|클린\s*프라이스|"
                    r"이자(?:를)?\s*(?:제외|뺀)\s*평가(?:단가|가격)).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|오름차순|내림차순|순)",
                    "evaluation_price",
                ),
                (
                    r"(?:이자부단가|경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "dirty_price",
                ),
                (
                    r"(?:컨벡시티|가격\s*굴곡).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "convexity",
                ),
                (
                    r"(?:표면\s*(?:이자율|이율|금리)|쿠폰(?:\s*금리)?|액면\s*쿠폰|"
                    r"액면에?\s*붙은\s*이자율)"
                    r".{0,30}(?:큰|작은|높은|낮은|순)",
                    "coupon_rate_pct",
                ),
                (
                    r"(?:발행잔액|(?<!총)\s?발행금액|남은\s*발행\s*물량).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "issue_amount",
                ),
                (
                    r"(?:총발행금액|총\s*발행금액|총\s*발행\s*규모|총발행액).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "total_issue_amount",
                ),
                (
                    r"(?:매수가능수량|매수 가능 수량).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "buyable_quantity",
                ),
                (
                    r"(?:잔존\s*(?:일수|일|기일)|잔여\s*일|"
                    r"(?:상환|만기)까지\s*남은\s*(?:일수|날짜|날|기간)|남은\s*기간).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|긴|짧은|순)",
                    "remaining_days",
                ),
                (
                    r"(?:듀레이션|duration|금리\s*민감도).{0,30}"
                    r"(?:큰|작은|높은|낮은|긴|짧은|순|ascending|descending)",
                    "duration_years",
                ),
                (
                    r"만기(?!일)(?:가|까지의 기간)?.{0,30}"
                    r"(?:긴|짧은|많은|적은|빠른|늦은)",
                    "remaining_days",
                ),
                (
                    r"(?:상환|만기)(?!\s*시점).{0,30}?"
                    r"(?:먼저|빨리|가까운\s*순|가장\s*먼저)",
                    "remaining_days",
                ),
                (
                    r"만기일.{0,30}(?:긴|짧은|빠른|늦은|먼저|순|최신)",
                    "maturity_date",
                ),
            ]
        )
    elif family == "fund":
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:판매회사보수|판매\s*회사\s*보수|판매보수).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "sale_company_fee_pct",
                ),
                (
                    r"(?:신탁업자보수|신탁\s*업자\s*보수|수탁보수).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "trustee_fee_pct",
                ),
                (
                    r"(?:국내주식\s*(?:구성비율|비중)).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "domestic_stock_ratio_pct",
                ),
                (
                    r"(?:국내채권\s*(?:구성비율|비중)).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "domestic_bond_ratio_pct",
                ),
                (
                    r"(?:해외주식\s*(?:구성비율|비중)).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "overseas_stock_ratio_pct",
                ),
                (
                    r"(?:해외채권\s*(?:구성비율|비중)).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "overseas_bond_ratio_pct",
                ),
                (
                    r"(?:최근|최신)\s*분배율.{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "latest_distribution_pct",
                ),
                (
                    r"(?:보전율|보전\s*비율).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "preservation_ratio_pct",
                ),
                (
                    r"(?:1주|일주일)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_week_return_pct",
                ),
                (
                    r"(?:1개월|한 달)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|3M|석 달)\s*(?:수익률|수익|성과|return).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|반년)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|1Y)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_year_return_pct",
                ),
                (
                    r"18\s*개월\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "eighteen_month_return_pct",
                ),
                (
                    r"2\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "two_year_return_pct",
                ),
                (
                    r"3\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "three_year_return_pct",
                ),
                (
                    r"5\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "five_year_return_pct",
                ),
                (
                    r"(?:AUM|순자산|운용\s*규모).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|순)",
                    "aum",
                ),
                (r"돈이.{0,30}(?:가장 많이|많이) 모인", "aum"),
            ]
        )
    else:
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:일 거래대금|거래대금|외화거래대금).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "daily_trading_value",
                ),
                (
                    r"(?:일 거래량|거래량|일간 거래량|하루 거래량).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "daily_volume",
                ),
                (
                    r"(?:추적\s*오차(?:율)?).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "tracking_error_pct",
                ),
                (
                    r"(?:괴리율|가격\s*괴리율|NAV\s*괴리율).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "discrepancy_pct",
                ),
                (
                    r"(?<!\d)(?:20\s*거래일|20\s*일|1\s*개월).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "twenty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:60\s*거래일|60\s*일|3\s*개월).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "sixty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:120\s*거래일|120\s*일|6\s*개월).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "one_hundred_twenty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:252\s*거래일|252\s*일|1\s*년).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "two_hundred_fifty_two_day_volatility_pct",
                ),
                (
                    r"(?:연환산\s*)?분배\s*수익률.{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "distribution_yield_pct",
                ),
                (
                    r"(?:1일|하루|당일|오늘|일간)\s*(?:수익률|수익|성과)"
                    r".{0,120}(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_day_return_pct",
                ),
                (
                    r"(?:1개월|한\s*달|1M)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|석\s*달|3M)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|반년|6M)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|일\s*년|1Y)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_year_return_pct",
                ),
                (
                    r"(?:YTD|연초\s*이후)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "ytd_return_pct",
                ),
                (
                    r"(?:종가|마감\s*가격).{0,20}(?:큰|작은|높은|낮은|순)",
                    "close_price",
                ),
                (
                    r"(?:AUM|순자산|일간\s*순자산총액|최종\s*(?:AUM|순자산)|"
                    r"운용\s*(?:자산|규모)).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|제일\s*큰|순|"
                    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
                    r"ascending|descending)",
                    "aum",
                ),
                (
                    r"(?:총보수|보수|연간\s*보수율).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|저렴한|비싼|상위|순)",
                    "total_expense_ratio_pct",
                ),
                (
                    r"(?:동적\s*(?:지표\s*)?기준일|AUM 업데이트일|최신 기준일)"
                    r".{0,80}(?:순|최신|오래된)",
                    "dynamic_as_of",
                ),
                (
                    r"정적\s*(?:지표\s*)?기준일.{0,80}(?:순|최신|오래된)",
                    "static_as_of",
                ),
            ]
        )
    if re.search(r"\d+\s*(?:등(?!급)|위(?!험))", question):
        for field, aliases in numeric_fields:
            alias_pattern = "|".join(re.escape(alias) for alias in aliases)
            explicit_rank_patterns.append(
                (rf"(?:{alias_pattern}).{{0,24}}?\d+\s*(?:등(?!급)|위(?!험))", field)
            )

    alias_matches = rank_alias_matches(question, family)
    alias_keys = {(item.field_id, item.direction.value) for item in alias_matches}
    alias_resolution = alias_matches[0] if len(alias_keys) == 1 else None
    if len(alias_keys) > 1:
        semantic_ambiguity_spans.extend(item.text for item in alias_matches)

    ranking_candidates: list[tuple[int, int, int, str, re.Match[str]]] = []
    for priority, (pattern, field) in enumerate(explicit_rank_patterns):
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match is not None:
            ranking_candidates.append((len(match.group(0)), -match.start(), priority, field, match))
    ranking_field: str | None = None
    ranking_match: re.Match[str] | None = None
    if ranking_candidates:
        _, _, _, ranking_field, ranking_match = min(ranking_candidates)
    if ranking_field is not None and family is not None:
        try:
            sortable = (
                load_field_registry()
                .require_field(
                    ranking_field,
                    [family],
                )
                .sortable
            )
        except ValueError:
            sortable = False
        if not sortable:
            ranking_field = None
    if (
        ranking_field is not None
        and alias_resolution is not None
        and ranking_field != alias_resolution.field_id
    ):
        semantic_ambiguity_spans.extend(
            (ranking_match.group(0), alias_resolution.text)
            if ranking_match is not None
            else (alias_resolution.text,)
        )
    if ranking_field is not None:
        assert ranking_match is not None
        ranking_surface = ranking_match.group(0)
        local_ascending = re.search(
            r"낮은|낮게|작은|작게|짧은|짧게|적은|적게|저렴한|저렴하게|"
            r"빠른|빠르게|가까운|오래된|오래된\s*순|"
            r"lowest\s*[- ]to\s*[- ]highest|ascending",
            ranking_surface,
            flags=re.IGNORECASE,
        )
        local_descending = re.search(
            r"높은|높게|큰|크게|긴|길게|많은|많이|비싼|비싸게|앞선|"
            r"\d+\s*(?:등(?!급)|위(?!험))|highest\s*[- ]to\s*[- ]lowest|descending",
            ranking_surface,
            flags=re.IGNORECASE,
        )
        if local_ascending is not None and local_descending is not None:
            semantic_ambiguity_spans.append(ranking_surface)
            direction = "asc"
        elif local_ascending is not None:
            direction = "asc"
        elif local_descending is not None:
            direction = "desc"
        elif alias_resolution is not None and alias_resolution.field_id == ranking_field:
            # The explicit pattern identifies the field, while the typed alias
            # may carry the only unambiguous direction (for example
            # ``상환이 먼저 오는``). Do not replace that evidence with the
            # generic descending fallback.
            direction = alias_resolution.direction.value
        elif ranking_field in {"product_name", "short_name", "ticker"} and not descending:
            direction = "asc"
        else:
            direction = "asc" if ascending else "desc"
        rankings.append({"field": ranking_field, "direction": direction, "nulls": "last"})
    elif alias_resolution is not None:
        rankings.append(
            {
                "field": alias_resolution.field_id,
                "direction": alias_resolution.direction.value,
                "nulls": "last",
            }
        )

    # When a shared cross-family sentence names the period once and then says
    # only "수익률이 0보다 ...", bind the threshold to the already verified
    # ranking field.  This avoids dropping a hard filter while still refusing
    # to guess a period when no return ranking was resolved.
    if rankings and rankings[0]["field"] in {
        "one_day_return_pct",
        "one_week_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "eighteen_month_return_pct",
        "two_year_return_pct",
        "three_year_return_pct",
        "five_year_return_pct",
        "ytd_return_pct",
    }:
        generic_return_band = re.search(
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*이상\s*"
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*이하",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_band is not None:
            add(
                rankings[0]["field"],
                [
                    _scaled_number(generic_return_band.group(1)),
                    _scaled_number(generic_return_band.group(2)),
                ],
                "between",
            )
        generic_return_comparison = re.search(
            rf"({NUMBER})\s*(?:%|퍼센트)?(?:을|를)?\s*"
            r"(이상|이하|초과|미만)",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_comparison is not None:
            add(
                rankings[0]["field"],
                _scaled_number(generic_return_comparison.group(1)),
                {
                    "이상": "gte",
                    "이하": "lte",
                    "초과": "gt",
                    "미만": "lt",
                }[generic_return_comparison.group(2)],
            )
        generic_return_threshold = re.search(
            rf"(?:(?:수익률|수익|성과)(?:이|가)?\s*)?"
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*보다\s*"
            r"(큰|높은|작은|낮은)",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_threshold is not None:
            operator = "gt" if generic_return_threshold.group(2) in {"큰", "높은"} else "lt"
            add(
                rankings[0]["field"],
                _scaled_number(generic_return_threshold.group(1)),
                operator,
            )

    coverage = _SEMANTIC_COVERAGE_GATE.evaluate(
        question,
        interaction_intent="search",
    )
    semantic_ambiguity_spans.extend(
        "AUM 비교 통화" if span == "trading_currency(AUM 비교 통화)" else span
        for span in coverage.ambiguity_spans
    )
    semantic_unsupported_spans.extend(coverage.unsupported_spans)

    if ranking_intent_present(question) and not rankings:
        semantic_ambiguity_spans.extend(
            coverage.ambiguity_spans or ranking_cue_spans(question) or ("ranking_metric",)
        )
    if (
        ranking_intent_present(question)
        and rankings
        and all(item["field"] in {"product_name", "short_name", "ticker"} for item in rankings)
        and not explicit_product_name_order(question)
    ):
        semantic_ambiguity_spans.extend(ranking_cue_spans(question) or ("ranking_metric",))

    # A natural range expression can be observed twice by independent lexical
    # rules (for example, ``1년 이상 5년 이하`` as both BETWEEN and GTE).
    # Preserve the range and discard only an exactly duplicated inclusive
    # endpoint; all genuinely narrower constraints remain intact.
    between_endpoints = {
        (item["field"], item["value"][0], item["value"][1])
        for item in required
        if item["operator"] == "between"
        and isinstance(item["value"], list)
        and len(item["value"]) == 2
    }
    if between_endpoints:
        required[:] = [
            item
            for item in required
            if not any(
                item["field"] == field
                and (
                    (item["operator"] == "gte" and item["value"] == lower)
                    or (item["operator"] == "lte" and item["value"] == upper)
                )
                for field, lower, upper in between_endpoints
            )
        ]

    exact_lookup = any(item["field"] in {"ticker", "product_id", "isin"} for item in required)
    if (
        family == "fund"
        and not rankings
        and not exact_lookup
        and not any(phrase in question for phrase in unsupported_patterns)
        and not any(phrase in question for phrase in ambiguity_patterns)
        and not semantic_unsupported_spans
        and not semantic_ambiguity_spans
        and not ranking_intent_present(question)
    ):
        rankings.append({"field": "product_name", "direction": "asc", "nulls": "last"})

    requested_limit = requested_result_limit(question)
    limit = requested_limit if requested_limit is not None else (1 if exact_lookup else 5)
    unsupported_spans = [
        *[phrase for phrase in unsupported_patterns if phrase in question],
        *semantic_unsupported_spans,
    ]
    if family == "fund" and unsupported_spans:
        rankings = []
    if family == "bond":
        rating_token = re.search(
            r"(?:신용\s*등급(?:이|은)?\s*(?P<prefix>[A-Z]{1,5}(?:[+0-])?)"
            r"(?=인|\s|$)|(?P<suffix>[A-Z]{1,5}(?:[+0-])?)\s*등급|"
            r"(?P<label_first>[A-Z]{1,5}(?:[+0-])?)\s*신용\s*등급|"
            r"(?<![A-Z])(?P<bare>[A-D]{1,5}(?:[+0-])?)\s*(?:국내\s*)?채권)",
            question,
        )
        valid_ratings = set(
            load_field_registry().require_field("credit_rating", ["bond"]).enum_values
        )
        rating_value = (
            None
            if rating_token is None
            else (
                rating_token.group("prefix")
                or rating_token.group("suffix")
                or rating_token.group("label_first")
                or rating_token.group("bare")
            )
        )
        if rating_value is not None and rating_value not in valid_ratings:
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add("credit_rating", rating_value, "eq")
        ordered_rating = re.search(
            r"(?:신용\s*등급(?:이|은)?\s*)?"
            r"(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*"
            r"(이상|이하|초과|미만)",
            question,
        )
        if ordered_rating:
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            ratings = list(
                load_field_registry().require_field("credit_rating", ["bond"]).enum_values
            )
            threshold_index = ratings.index(ordered_rating.group(1))
            boundary = ordered_rating.group(2)
            if boundary == "이상":
                selected_ratings = ratings[: threshold_index + 1]
            elif boundary == "초과":
                selected_ratings = ratings[:threshold_index]
            elif boundary == "이하":
                selected_ratings = ratings[threshold_index:]
            else:
                selected_ratings = ratings[threshold_index + 1 :]
            if selected_ratings:
                add("credit_rating", selected_ratings, "in")
            else:
                unsupported_spans.append(ordered_rating.group(0))
        else:
            vague_ordered_rating = re.search(
                r"신용등급.{0,20}?(?:높은|낮은)",
                question,
            )
            if vague_ordered_rating and not any(
                item["field"] == "credit_rating" for item in required
            ):
                required[:] = [item for item in required if item["field"] != "credit_rating"]
                unsupported_spans.append(vague_ordered_rating.group(0))
    ambiguity_spans = [
        *[phrase for phrase in ambiguity_patterns if phrase in question],
        *semantic_ambiguity_spans,
    ]
    return {
        "product_family": family,
        "required_constraints": required,
        "required_eq_constraints": [item for item in required if item["operator"] == "eq"],
        "required_rankings": rankings,
        "limit": limit,
        "unsupported_spans": list(dict.fromkeys(unsupported_spans)),
        "ambiguity_spans": list(dict.fromkeys(ambiguity_spans)),
    }


def _hint_constraints(
    hints: list[dict[str, Any]],
    product_family: str,
) -> list[dict[str, Any]]:
    registry = load_field_registry()
    grouped_equalities: dict[str, list[Any]] = defaultdict(list)
    constraints: list[dict[str, Any]] = []
    for hint in hints:
        if hint["operator"] == "eq":
            if hint["value"] not in grouped_equalities[hint["field"]]:
                grouped_equalities[hint["field"]].append(hint["value"])
            continue
        definition = registry.require_field(hint["field"], [product_family])
        constraints.append(
            {
                **hint,
                "unit": definition.unit,
                "strength": "locked",
            }
        )
    for field, values in grouped_equalities.items():
        definition = registry.require_field(field, [product_family])
        values = sorted(values, key=lambda value: (type(value).__name__, repr(value)))
        constraints.append(
            {
                "field": field,
                "operator": "eq" if len(values) == 1 else "in",
                "value": values[0] if len(values) == 1 else values,
                "unit": definition.unit,
                "strength": "locked",
            }
        )
    return constraints


def canonicalize_query_plan_payload(
    question: str,
    payload: dict[str, Any],
    *,
    force_product_family_hint: bool = False,
) -> dict[str, Any]:
    raw_families = payload.get("product_families")
    product_family_hint = (
        raw_families[0]
        if isinstance(raw_families, list)
        and len(raw_families) == 1
        and isinstance(raw_families[0], str)
        else None
    )
    hints = build_lexical_hints(
        question,
        product_family_hint,
        force_product_family_hint=force_product_family_hint,
    )
    family = hints["product_family"]
    if family is None:
        family = "overseas_etp"
    payload["schema_version"] = "1.0"
    payload["intent"] = "search"
    payload["product_families"] = [family]
    constraints = _hint_constraints(
        hints["required_constraints"],
        family,
    )
    rankings = hints["required_rankings"]
    explicitly_requested_fields = _explicit_projection_fields(question, family)
    payload["constraints"] = constraints
    payload["ranking"] = rankings
    payload["projection"] = search_projection(
        family,
        *(
            constraint["field"]
            for constraint in constraints
            if constraint["field"] != "public_offering"
        ),
        *(ranking["field"] for ranking in rankings),
        *explicitly_requested_fields,
    )
    payload["limit"] = hints["limit"]
    payload["intent_payload"] = {
        "comparison_fields": [],
        "group_by": [],
        "aggregations": [],
        "explain_product_ids": [],
    }

    unsupported_spans = hints["unsupported_spans"]
    ambiguity_spans = hints["ambiguity_spans"]
    if unsupported_spans:
        payload["unsupported_conditions"] = [
            {
                "span": span,
                "reason": "현재 동결된 field registry 또는 상품군에서 지원하지 않는 조건",
            }
            for span in unsupported_spans
        ]
        payload["ambiguities"] = []
    elif ambiguity_spans:
        payload["ambiguities"] = [
            {
                "span": span,
                "reason": "판단 기준이나 임계값이 명시되지 않음",
                "options": ["판단 기준을 구체화한다"],
            }
            for span in ambiguity_spans
        ]
        payload["unsupported_conditions"] = []
    else:
        payload["ambiguities"] = []
        payload["unsupported_conditions"] = []
    return payload


def canonicalize_linked_query_plan(question: str, plan: QueryPlan) -> QueryPlan:
    payload = canonicalize_query_plan_payload(question, plan.model_dump(mode="json"))
    return QueryPlan.model_validate(payload)
