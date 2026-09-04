from __future__ import annotations

import re

_KOREAN_CARDINALS = {
    "하나": 1,
    "한": 1,
    "둘": 2,
    "두": 2,
    "셋": 3,
    "세": 3,
    "넷": 4,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}
_COUNTER = (
    r"(?:개(?!월)|건|종(?!가|목)|종목|상품(?!군)|"
    r"(?:국내\s*|해외\s*|원화\s*)?(?:ETF|ETN|ETP|채권)|"
    r"(?:공모\s*)?펀드)"
)


def requested_result_limit(question: str) -> int | None:
    """Extract an explicit result count without confusing periods with counts.

    The public questions mix Arabic numerals, full-width numerals normalized by
    ``SafetyEnvelope``, and native Korean counters such as ``세 개``.  Routing
    and QueryPlan linking must share one parser so the requested count cannot be
    lost between the two boundaries.
    """

    labeled = re.findall(
        r"(?:개수|제한)\s*[:：]\s*(\d+)",
        question,
        flags=re.IGNORECASE,
    )
    if labeled:
        value = int(labeled[-1])
        return value if 1 <= value <= 100 else None

    ranked = re.findall(
        r"(?:TOP|상위|하위)\s*(\d+)(?!\s*(?:일|주|개월|년))",
        question,
        flags=re.IGNORECASE,
    )
    numeric = re.findall(rf"(\d+)\s*{_COUNTER}", question, flags=re.IGNORECASE)
    ordinal = re.findall(
        r"(\d+)\s*(?:등(?!급)|위(?!험))(?:만)?",
        question,
        flags=re.IGNORECASE,
    )
    candidates = [*ranked, *numeric, *ordinal]
    if candidates:
        value = int(candidates[-1])
        return value if 1 <= value <= 100 else None

    korean = re.findall(
        rf"(?<![0-9A-Za-z가-힣])"
        rf"({'|'.join(sorted(_KOREAN_CARDINALS, key=len, reverse=True))})\s*{_COUNTER}",
        question,
    )
    if korean:
        return _KOREAN_CARDINALS[korean[-1]]

    korean_ordinal = re.findall(
        rf"(?<![0-9A-Za-z가-힣])"
        rf"(첫|{'|'.join(sorted(_KOREAN_CARDINALS, key=len, reverse=True))})\s*"
        r"번째\s*(?:까지|이내)",
        question,
    )
    if korean_ordinal:
        token = korean_ordinal[-1]
        return 1 if token == "첫" else _KOREAN_CARDINALS[token]

    if re.search(
        r"(?:^|\s)하나(?:만)?\s*(?:보여|알려|찾아|골라|선정|조회)",
        question,
        flags=re.IGNORECASE,
    ):
        return 1
    return None


__all__ = ["requested_result_limit"]
