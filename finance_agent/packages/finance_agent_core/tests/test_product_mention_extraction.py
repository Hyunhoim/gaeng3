from __future__ import annotations

import pytest

from finance_agent_core.agent.router import _product_mentions


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # ETF tickers carry an `A` prefix, ETN tickers a `Q` prefix. Both are
        # seven characters and both must survive an attached Korean postposition.
        ("국내 ETF A091160과 A139260의 총보수를 비교해줘", ["A091160", "A139260"]),
        ("국내 ETN Q500055와 Q500056 중 수익률이 높은 쪽은?", ["Q500055", "Q500056"]),
        ("A0000D0 상품 정보 알려줘", ["A0000D0"]),
        # Two of the three approved ticker shapes end in letters.
        ("A0000H0과 A5005AB를 비교해줘", ["A0000H0", "A5005AB"]),
    ],
)
def test_bare_domestic_etp_tickers_are_recognized(question: str, expected: list[str]) -> None:
    assert _product_mentions(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "ARIRANG 고배당주 ETF 알려줘",
        "KODEX 반도체 ETF의 총보수 알려줘",
        "KBSTAR 200 ETF 중 순자산이 큰 것",
        "HANARO 200 ETF 알려줘",
    ],
)
def test_brand_words_are_not_read_as_domestic_tickers(question: str) -> None:
    """A brand name has no four-digit block, so it must not become an identifier."""

    assert not any(
        mention in {"ARIRANG", "KODEX", "KBSTAR", "HANARO"}
        for mention in _product_mentions(question)
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("공모펀드 KR5370113938 정보 알려줘", ["KR5370113938"]),
        ('해외 ETF "101:IVEG.O"와 "101:IWTR.O"를 비교해줘', ["101:IVEG.O", "101:IWTR.O"]),
        ('국내 ETF "A091160"과 "A139260"의 총보수를 비교해줘', ["A091160", "A139260"]),
    ],
)
def test_existing_identifier_shapes_still_resolve(question: str, expected: list[str]) -> None:
    assert _product_mentions(question) == expected
