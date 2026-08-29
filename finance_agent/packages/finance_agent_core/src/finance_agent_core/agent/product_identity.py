from __future__ import annotations

import re

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


def extract_named_product_mentions(question: str) -> list[str]:
    """Extract narrow product-name surfaces without guessing a DB identity."""

    matches: list[tuple[int, str]] = []
    matches.extend(
        (match.start(), match.group("name").strip())
        for match in _BRANDED_PRODUCT.finditer(question)
    )
    for match in _NAMED_FUND.finditer(question):
        value = match.group("name").strip()
        if value.casefold() not in _GENERIC_FUND_NAMES:
            matches.append((match.start(), value))
    return list(dict.fromkeys(value for _, value in sorted(matches)))


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
    "extract_named_product_mentions",
    "is_domestic_etp_brand_question",
    "is_named_product_mention",
    "is_quoted_product_mention",
]
