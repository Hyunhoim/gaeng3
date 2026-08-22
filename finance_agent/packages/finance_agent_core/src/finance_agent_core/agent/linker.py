from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from finance_agent_core.agent.safety import normalize_user_question
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    search_projection,
)

NUMBER = r"-?\d+(?:\.\d+)?"
SCALED_NUMBER = rf"{NUMBER}\s*(?:천억원|백억원|십억원|조원|억원|만원|천억|백억|십억|억|조|만|원)?"
_NEGATED_LITERAL_SUFFIX = re.compile(
    r"^\s*(?:형|유형|지역|시장|방식|상품|종목)?\s*"
    r"(?:가|이|은|는|을|를|으로|로|이나|나)?\s*"
    r"(?:아닌|아니|아님|지\s*않|하지\s*않|없|제외|이외|말고|빼고|외(?:의|에는)?)"
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
        rf"(?:{alias_pattern}).{{0,12}}?({SCALED_NUMBER})\s*(?:%|배|일|년)?\s*"
        rf"(?:달러|USD|KRW)?\s*에서\s*({SCALED_NUMBER})\s*(?:%|배|일|년)?\s*"
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
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({SCALED_NUMBER})\s*(?:%|배|일|년)?\s*"
        r"(?:달러|USD|KRW)?\s*(이하|미만|이상|초과|정확히)",
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
        }
        return {
            "field": field,
            "operator": operators[comparison.group(2)],
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
    question = normalize_user_question(question)
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
                add(field, identity, "eq")
    for match in re.finditer(
        r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9._:-]{1,29})"
        r"\s*(?:을|를|은|는)?\s*(?:제외|이외|말고|빼고|외(?:에는|의)?)",
        question,
        flags=re.IGNORECASE,
    ):
        if match.group(1).upper() in {"ETF", "ETN", "ETP"}:
            continue
        semantic_ambiguity_spans.append(match.group(0))

    if family == "domestic_etp":
        phrase_mappings = [
            ("혼합자산형", "asset_type", "혼합자산"),
            ("단기자금형", "asset_type", "단기자금"),
            ("원자재형", "asset_type", "원자재"),
            ("부동산형", "asset_type", "부동산"),
            ("주식형", "asset_type", "주식"),
            ("채권형", "asset_type", "채권"),
            ("통화형", "asset_type", "통화"),
            ("이머징/브릭스", "investment_region", "이머징/브릭스"),
            ("남미/북미", "investment_region", "남미/북미"),
            ("글로벌", "investment_region", "글로벌"),
            ("아시아", "investment_region", "아시아"),
            ("베트남", "investment_region", "베트남"),
            ("미국", "investment_region", "미국"),
            ("일본", "investment_region", "일본"),
            ("중국", "investment_region", "중국"),
            ("유럽", "investment_region", "유럽"),
            ("인도", "investment_region", "인도"),
            ("원화", "trading_currency", "KRW"),
            ("KRW", "trading_currency", "KRW"),
        ]
        if re.search(r"^국내\s+(?:채권형|혼합자산형)", question):
            add("investment_region", "국내")
    elif family == "fund":
        phrase_mappings = [
            ("국내외 혼합", "fund_geography_scope", "국내외혼합"),
            ("국내외혼합", "fund_geography_scope", "국내외혼합"),
            ("해외", "fund_geography_scope", "해외"),
            ("국내", "fund_geography_scope", "국내"),
            ("주식혼합", "fund_management_attribute", "주식혼합"),
            ("채권혼합", "fund_management_attribute", "채권혼합"),
            ("혼합자산", "fund_management_attribute", "혼합자산"),
            ("특별자산", "fund_management_attribute", "특별자산"),
            ("재간접", "fund_management_attribute", "재간접"),
            ("주식형", "fund_management_attribute", "주식형"),
            ("채권형", "fund_management_attribute", "채권형"),
            ("대출형", "fund_management_attribute", "대출형"),
            ("임대형", "fund_management_attribute", "임대형"),
            ("MMF", "fund_management_attribute", "MMF"),
            ("글로벌", "investment_region", "글로벌"),
            ("아시아", "investment_region", "아시아"),
            ("남미/북미", "investment_region", "남미/북미"),
            ("이머징/브릭스", "investment_region", "이머징/브릭스"),
            ("유럽", "investment_region", "유럽"),
            ("원화", "trading_currency", "KRW"),
            ("KRW", "trading_currency", "KRW"),
            ("달러", "trading_currency", "USD"),
            ("USD", "trading_currency", "USD"),
        ]
    elif family == "bond":
        phrase_mappings = [
            ("개인투자용국채", "bond_major_class", "개인투자용국채"),
            ("회사채", "bond_major_class", "회사채"),
            ("특수채", "bond_major_class", "특수채"),
            ("국공채", "bond_major_class", "국공채"),
            ("국고채", "bond_subclass", "국고채"),
            ("지역개발채", "bond_type", "지역개발채"),
            ("도시철도공채", "bond_type", "도시철도공채"),
            ("금융지주회사채", "bond_type", "금융지주회사채"),
            ("보험회사채", "bond_type", "보험회사채"),
            ("일반회사채", "bond_type", "일반회사채"),
            ("할부금융채", "bond_type", "할부금융채"),
            ("장내", "bond_market", "장내"),
            ("장외", "bond_market", "장외"),
            ("원화", "trading_currency", "KRW"),
            ("KRW", "trading_currency", "KRW"),
            ("달러", "trading_currency", "USD"),
        ]
    else:
        phrase_mappings = [
            ("머니마켓", "asset_type", "Money Market"),
            ("혼합자산형", "asset_type", "Mixed Assets"),
            ("대체자산형", "asset_type", "Alternatives"),
            ("원자재형", "asset_type", "Commodity"),
            ("주식형", "asset_type", "Equity"),
            ("채권", "asset_type", "Bond"),
            ("글로벌 신흥국", "investment_region", "Global Emerging Markets"),
            ("미국 제외 글로벌", "investment_region", "Global Ex US"),
            ("미국", "investment_region", "United States of America"),
            ("일본", "investment_region", "Japan"),
            ("중국", "investment_region", "China"),
            ("유럽", "investment_region", "Europe"),
            ("NASDAQ", "exchange_code", "NAS"),
            ("NYSE", "exchange_code", "NYS"),
            ("AMEX", "exchange_code", "AMX"),
            ("달러", "trading_currency", "USD"),
            ("USD", "trading_currency", "USD"),
        ]
    for phrase, field, value in phrase_mappings:
        if phrase not in question:
            continue
        if phrase == "미국" and "미국 제외 글로벌" in question:
            continue
        if family == "fund" and phrase == "국내" and "국내외" in question:
            continue
        if negated_phrase := _negated_literal_span(question, phrase):
            semantic_ambiguity_spans.append(negated_phrase)
            continue
        add(field, value)

    etp_family = family in {"overseas_etp", "domestic_etp"}
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
        r"거래\s*(?:중지|정지)(?:가|는|이)?\s*"
        r"(?:되지\s*않|아니|아닌|아님)|"
        r"거래\s*(?:중지|정지)(?:된|인)?\s*(?:상품|것)?"
        r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|빼고|말고)",
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
            r"(?!\s*(?:가\s*)?(?:아니|아닌|아님))(?:된|됨| 상태)?",
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
        elif re.search(
            r"연금(?:\s*계좌)?(?:에서|으로|로|에서도|로도)?\s*"
            r"(?:(?:사고\s*팔|거래|구매)(?:가)?\s*(?:가능|(?:할\s*)?수\s*있)|"
            r"거래가?\s*가능)",
            question,
        ):
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
        if "판매 중" in question:
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
        ):
            add("company_sellable", True)
        if "개인용" in question or "개인 투자자 대상" in question:
            add("investor_type", "개인")
        if "법인용" in question or "법인 투자자 대상" in question:
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
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)

    if family == "bond":
        buy_unavailable = re.search(
            r"(?:매수|구매|주문)(?:가|는|이)?\s*가능.{0,8}"
            r"(?:하지\s*않|아닌|없)",
            question,
        )
        if buy_unavailable is not None:
            add("currently_buyable", False)
        elif any(
            phrase in question
            for phrase in (
                "현재 매수 가능",
                "매수 가능",
                "현재 매수할 수 있는",
                "매수할 수 있는",
                "현재 판매 가능",
                "판매 가능",
                "살 수 있는",
                "주문 가능한",
                "구매 가능",
            )
        ):
            add("currently_buyable", True)
        lookup_patterns = [
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
                r"신용등급(?:이|은)?\s*(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?:인|$|\s)",
                "credit_rating",
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

    if family == "bond":
        numeric_fields = [
            ("issue_amount", ("발행잔액", "발행금액")),
            ("coupon_rate_pct", ("표면이율", "쿠폰금리", "표면금리")),
            ("buy_yield_pct", ("매수수익률", "매수 수익률")),
            ("after_tax_yield_pct", ("세후수익률", "세후 수익률")),
            ("buyable_quantity", ("매수가능수량", "매수 가능 수량")),
            ("remaining_days", ("잔존일수", "잔존일")),
            ("duration_years", ("듀레이션",)),
        ]
    elif family == "fund":
        numeric_fields = [
            ("aum", ("AUM", "순자산", "운용 자산")),
            ("one_week_return_pct", ("1주 수익률", "일주일 수익률")),
            ("one_month_return_pct", ("1개월 수익률", "한 달 수익률")),
            ("three_month_return_pct", ("3개월 수익률", "석 달 수익률")),
            ("six_month_return_pct", ("6개월 수익률", "반년 수익률")),
            ("one_year_return_pct", ("1년 수익률", "1Y 수익률")),
        ]
    else:
        numeric_fields = [
            ("total_expense_ratio_pct", ("총보수", "보수")),
            ("aum", ("AUM", "순자산")),
        ]
    if family == "domestic_etp":
        numeric_fields.extend(
            [
                ("close_price", ("종가", "마감 가격")),
                ("daily_trading_value", ("일 거래대금", "거래대금")),
                ("one_day_return_pct", ("1일 수익률",)),
                ("one_month_return_pct", ("1개월 수익률", "1M 수익률")),
                ("three_month_return_pct", ("3개월 수익률", "3M 수익률")),
                ("six_month_return_pct", ("6개월 수익률", "6M 수익률")),
                ("one_year_return_pct", ("1년 수익률", "1Y 수익률")),
                ("ytd_return_pct", ("YTD 수익률", "연초 이후 수익률")),
                ("leverage_factor", ("배수", "레버리지")),
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

    unsupported_patterns = [
        "배당수익률",
        "원화로 환산",
        "추적오차율",
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
    elif family != "domestic_etp":
        unsupported_patterns.extend(["1일 수익률", "3개월 수익률", "국내 ETF"])
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
            "오래된 순",
            "lowest-to-highest",
            "ascending",
        )
    )
    descending = any(
        phrase in question
        for phrase in (
            "내림차순",
            "큰 순",
            "높은 순",
            "최신",
            "상위",
            "좋은",
            "성과순",
            "가장 많이",
            "제일 큰",
            "높은 쪽부터",
            "큰 쪽부터",
            "긴 순",
            "highest-to-lowest",
            "descending",
        )
    )
    explicit_rank_patterns: list[tuple[str, str]] = []
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
                    r"(?:표면\s*(?:이율|금리)|쿠폰\s*금리)"
                    r".{0,30}(?:큰|작은|높은|낮은|순)",
                    "coupon_rate_pct",
                ),
                (
                    r"(?:발행잔액|발행금액).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "issue_amount",
                ),
                (
                    r"(?:매수가능수량|매수 가능 수량).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "buyable_quantity",
                ),
                (
                    r"(?:잔존\s*(?:일수|일|기일)|만기까지\s*남은\s*(?:일수|날|기간)).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|긴|짧은|순)",
                    "remaining_days",
                ),
                (
                    r"(?:듀레이션|duration).{0,30}"
                    r"(?:큰|작은|높은|낮은|긴|짧은|순|ascending|descending)",
                    "duration_years",
                ),
                (
                    r"만기(?:가|까지의 기간)?.{0,30}(?:긴|짧은|많은|적은)",
                    "remaining_days",
                ),
                (r"만기일.{0,30}(?:순|최신)", "maturity_date"),
            ]
        )
    elif family == "fund":
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:1주|일주일)\s*(?:수익률|수익|성과).{0,80}"
                    r"(?:큰|작은|높은|낮은|좋은|성과순|순)",
                    "one_week_return_pct",
                ),
                (
                    r"(?:1개월|한 달)\s*(?:수익률|수익|성과).{0,80}"
                    r"(?:큰|작은|높은|낮은|좋은|성과순|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|3M|석 달)\s*(?:수익률|수익|성과|return).{0,80}"
                    r"(?:큰|작은|높은|낮은|좋은|성과순|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|반년)\s*(?:수익률|수익|성과).{0,80}"
                    r"(?:큰|작은|높은|낮은|좋은|성과순|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|1Y)\s*(?:수익률|수익|성과).{0,80}"
                    r"(?:큰|작은|높은|낮은|좋은|성과순|순)",
                    "one_year_return_pct",
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
                    r"(?:일 거래대금|거래대금).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "daily_trading_value",
                ),
                (r"1일 수익률.{0,80}(?:큰|작은|높은|낮은|순)", "one_day_return_pct"),
                (
                    r"(?:1개월|1M) 수익률.{0,80}(?:큰|작은|높은|낮은|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|3M) 수익률.{0,80}(?:큰|작은|높은|낮은|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|6M) 수익률.{0,80}(?:큰|작은|높은|낮은|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|1Y) 수익률.{0,80}(?:큰|작은|높은|낮은|순)",
                    "one_year_return_pct",
                ),
                (
                    r"(?:YTD|연초 이후) 수익률.{0,80}(?:큰|작은|높은|낮은|순)",
                    "ytd_return_pct",
                ),
                (
                    r"(?:종가|마감\s*가격).{0,20}(?:큰|작은|높은|낮은|순)",
                    "close_price",
                ),
                (
                    r"(?:AUM|순자산|운용\s*(?:자산|규모)).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|제일\s*큰|순|"
                    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
                    r"ascending|descending)",
                    "aum",
                ),
                (
                    r"(?:총보수|보수).{0,20}"
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
    ranking_field: str | None = None
    ranking_match: re.Match[str] | None = None
    for pattern, field in explicit_rank_patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match is not None:
            ranking_field = field
            ranking_match = match
            break
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
    if ranking_field is not None:
        assert ranking_match is not None
        ranking_surface = ranking_match.group(0)
        local_ascending = re.search(
            r"낮은|낮게|작은|작게|짧은|짧게|적은|적게|저렴한|저렴하게|"
            r"빠른|빠르게|오래된|오래된\s*순|"
            r"lowest\s*[- ]to\s*[- ]highest|ascending",
            ranking_surface,
            flags=re.IGNORECASE,
        )
        local_descending = re.search(
            r"높은|높게|큰|크게|긴|길게|많은|많이|비싼|비싸게|"
            r"highest\s*[- ]to\s*[- ]lowest|descending",
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
        elif ranking_field in {"product_name", "short_name", "ticker"} and not descending:
            direction = "asc"
        else:
            direction = "asc" if ascending else "desc"
        rankings.append({"field": ranking_field, "direction": direction, "nulls": "last"})

    coverage = _SEMANTIC_COVERAGE_GATE.evaluate(
        question,
        interaction_intent="search",
    )
    semantic_ambiguity_spans.extend(
        "AUM 비교 통화" if span == "trading_currency(AUM 비교 통화)" else span
        for span in coverage.ambiguity_spans
    )
    semantic_unsupported_spans.extend(coverage.unsupported_spans)

    exact_lookup = any(item["field"] in {"ticker", "product_id", "isin"} for item in required)
    if (
        family == "fund"
        and not rankings
        and not exact_lookup
        and not any(phrase in question for phrase in unsupported_patterns)
        and not any(phrase in question for phrase in ambiguity_patterns)
        and not semantic_unsupported_spans
        and not semantic_ambiguity_spans
    ):
        rankings.append({"field": "product_name", "direction": "asc", "nulls": "last"})

    labeled_limit_matches = re.findall(
        r"(?:개수|제한)\s*[:：]\s*(\d+)",
        question,
        flags=re.IGNORECASE,
    )
    limit_matches = re.findall(r"(\d+)\s*(?:개(?!월)|건)", question)
    if labeled_limit_matches:
        limit = int(labeled_limit_matches[-1])
    else:
        limit = int(limit_matches[-1]) if limit_matches else (1 if exact_lookup else 5)
    unsupported_spans = [
        *[phrase for phrase in unsupported_patterns if phrase in question],
        *semantic_unsupported_spans,
    ]
    if family == "fund" and unsupported_spans:
        rankings = []
    if family == "bond":
        rating_token = re.search(
            r"신용\s*등급(?:이|은)?\s*([A-Z]{1,5}(?:[+0-])?)(?=인|\s|$)",
            question,
        )
        valid_ratings = set(
            load_field_registry().require_field("credit_rating", ["bond"]).enum_values
        )
        if rating_token and rating_token.group(1) not in valid_ratings:
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            unsupported_spans.append(rating_token.group(0))
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
            if vague_ordered_rating:
                required[:] = [item for item in required if item["field"] != "credit_rating"]
                unsupported_spans.append(vague_ordered_rating.group(0))
    ambiguity_spans = [
        *[phrase for phrase in ambiguity_patterns if phrase in question],
        *semantic_ambiguity_spans,
    ]
    if family == "fund":
        aum_requested = any(item["field"] == "aum" for item in required) or any(
            item["field"] == "aum" for item in rankings
        )
        currency_locked = any(
            item["field"] == "trading_currency" and item["operator"] == "eq" for item in required
        )
        if aum_requested and not currency_locked:
            ambiguity_spans.append("AUM 비교 통화")
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
