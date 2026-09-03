from __future__ import annotations

import pytest

from finance_agent_core.agent.product_identity import unresolved_identity_like_spans
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


def test_restrictive_unknown_alphanumeric_product_is_kept_as_a_hard_identity() -> None:
    question = "해외 ETF 오로라후보77 상품만 구성종목을 확인해 주세요."

    assert _product_mentions(question) == ["오로라후보77 상품"]


def test_rank_limit_is_not_misread_as_a_restrictive_product_identity() -> None:
    question = "국내 ETF 중 수익률 TOP10 상품만 알려 주세요."

    assert "TOP10 상품" not in _product_mentions(question)


def test_two_bare_overseas_tickers_are_bound_only_in_metric_comparison() -> None:
    assert _product_mentions("SPY와 QQQ 중 AUM이 어느 쪽이 큰지 비교해줘") == [
        "SPY",
        "QQQ",
    ]


@pytest.mark.parametrize(
    "question",
    [
        "AI와 ESG 중 좋은 것을 설명해줘",
        "AA와 AAA 중 신용등급이 높은 채권을 알려줘",
        "SPY와 QQQ에 대해 알려줘",
    ],
)
def test_unscoped_uppercase_pairs_do_not_acquire_ticker_identity(question: str) -> None:
    assert _product_mentions(question) == []


@pytest.mark.parametrize(
    "question",
    [
        "공모펀드를 국내외 구분별·펀드 운용속성별로 나눠 개수를 알려줘.",
        "공모펀드를 통화별, 펀드 유형별로 묶어 집계해줘.",
        "공모펀드의 위험등급별·펀드 운용속성별 평균을 알려줘.",
    ],
)
def test_aggregate_group_grammar_is_not_a_named_fund(question: str) -> None:
    assert _product_mentions(question) == []


def test_krw_bond_family_scope_is_not_an_unresolved_product_identity() -> None:
    question = (
        "원화채권의 표면이율 상위 2개와 "
        "공모형 펀드의 석 달 수익 상위 2개를 따로 알려 주세요."
    )

    assert unresolved_identity_like_spans(question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "상품명에 '미국'이 들어간 국내 ETF를 순자산 큰 순으로 보여줘.",
        "위험등급이 '매우높은위험(1등급)'인 국내 ETF 수를 알려줘.",
        "운용속성이 '주식형'인 공모펀드를 1년 수익률 순으로 보여줘.",
        "투자지역이 '미국'으로 표시된 해외 ETF를 찾아줘.",
    ],
)
def test_quoted_field_values_do_not_become_product_identities(question: str) -> None:
    assert _product_mentions(question) == []


def test_exact_quoted_product_name_remains_an_identity() -> None:
    question = "상품명 'TIGER 미국S&P500'의 순자산을 알려줘."

    assert _product_mentions(question) == ["TIGER 미국S&P500"]
