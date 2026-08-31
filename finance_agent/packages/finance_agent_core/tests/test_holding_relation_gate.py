from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_gate import _UNAPPROVED_RELATION_FILTER


@pytest.mark.parametrize(
    "question",
    [
        "삼성전자를 보유한 국내 ETF 3개",
        "삼성전자가 편입된 국내 ETF 3개",
        "삼성전자를 담은 국내 ETF 3개",
        "삼성전자를 담고 있는 국내 ETF 3개",
        "삼성전자를 포함한 국내 ETF 3개",
        "삼성전자 비중이 높은 국내 ETF 3개",
        "삼성전자 비중이 많은 국내 ETF 3개",
        "SK하이닉스를 담은 해외 ETF를 AUM 큰 순으로 5개",
    ],
)
def test_constituent_holding_questions_fail_closed(question: str) -> None:
    """The approved relation index carries no constituent holdings.

    Answering these by silently dropping the company condition returns products
    that have nothing to do with the question, so they must be refused instead.
    """

    assert _UNAPPROVED_RELATION_FILTER.search(question) is not None


@pytest.mark.parametrize(
    "question",
    [
        "짧은 이름에 미래에셋이 들어간 공모펀드를 이름 순으로 5개 찾아줘",
        "정식 상품명에 인도가 포함된 공모펀드를 이름 순으로 5개 보여줘",
        "운용사에 미래에셋이 포함된 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "약어명에 KODEX가 들어간 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        "기초지수에 MSCI가 포함된 국내 ETF를 AUM 큰 순으로 찾아줘",
        "상품명에 Vanguard가 들어간 해외 ETF를 AUM 큰 순으로 5개 보여줘.",
        "이름에 iShares Core가 포함된 해외 ETF를 보수가 낮은 순으로 10개 찾아줘.",
        "발행사에 메리츠가 포함된 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "상품명에 신종자본증권이 포함된 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
    ],
)
def test_product_field_text_search_stays_executable(question: str) -> None:
    """`이름에 X가 포함된` searches a product field and is fully supported."""

    assert _UNAPPROVED_RELATION_FILTER.search(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "국내주식 비중이 높은 공모펀드 3개",
        "국내채권 비중이 큰 공모펀드 3개",
        "해외주식 비중이 많은 공모펀드 3개",
        "해외채권 비중이 높은 공모펀드 3개",
    ],
)
def test_approved_public_fund_allocation_queries_stay_executable(
    question: str,
) -> None:
    """Approved allocation fields are metrics, not constituent relations."""

    assert _UNAPPROVED_RELATION_FILTER.search(question) is None


@pytest.mark.parametrize(
    ("question", "ranking_field"),
    [
        ("국내주식 비중이 높은 공모펀드 3개", "domestic_stock_ratio_pct"),
        ("국내채권 비중이 큰 공모펀드 3개", "domestic_bond_ratio_pct"),
        ("해외주식 비중이 많은 공모펀드 3개", "overseas_stock_ratio_pct"),
        ("해외채권 비중이 높은 공모펀드 3개", "overseas_bond_ratio_pct"),
    ],
)
def test_approved_public_fund_allocation_queries_compile_to_the_metric(
    question: str,
    ranking_field: str,
) -> None:
    decision = IntentRouter().route(question, f"fund-allocation-{ranking_field}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert [item.value for item in decision.draft.product_families] == ["fund"]
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (ranking_field, "desc")
    ]


@pytest.mark.parametrize(
    "question",
    [
        # "경과이자를 포함한 단가" defines a metric, not a holding.
        "공모 회사채만 놓고 경과이자를 포함한 단가가 낮은 것부터 5개 보여 주세요.",
        "세금을 포함한 수익률이 높은 국내채권 5개",
    ],
)
def test_metric_definitions_are_not_read_as_holdings(question: str) -> None:
    assert _UNAPPROVED_RELATION_FILTER.search(question) is None
