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
        if value.casefold() not in _GENERIC_FUND_NAMES:
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
    "extract_named_product_mentions",
    "is_domestic_etp_brand_question",
    "is_named_product_mention",
    "is_quoted_product_mention",
]
