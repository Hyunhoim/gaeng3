from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_agent_core.agent.compiler import _ranked_identity_requests_single_winner
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.product_identity import DatabaseProductIdentityMatcher
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent


def _overseas_identity_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE overseas_etp_products (
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                ticker TEXT,
                isin TEXT,
                is_quarantined INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            "INSERT INTO overseas_etp_products "
            "(product_id, product_name, ticker, isin) VALUES (?, ?, ?, ?)",
            [
                ("AMX:VOO", "Vanguard 500 Index Fund;ETF", "VOO", "US9229083632"),
                ("AMX:IVV", "iShares Core S&P 500 ETF", "IVV", "US4642872000"),
                ("AMX:SPY", "SPDR S&P 500 ETF Trust", "SPY", "US78462F1030"),
            ],
        )
    return path


def test_closed_identifier_set_keeps_identity_before_kkiri_suffix(tmp_path: Path) -> None:
    matcher = DatabaseProductIdentityMatcher(
        {
            ProductFamily.OVERSEAS_ETP: _overseas_identity_database(
                tmp_path / "overseas.sqlite3"
            )
        }
    )

    matches = matcher.find("VOO, IVV, SPY끼리만 총보수가 낮은 순으로 3개 보여줘")

    assert [item.candidates[0].canonical_literal for item in matches] == [
        "VOO",
        "IVV",
        "SPY",
    ]


@pytest.mark.parametrize(
    "question",
    [
        "VOO, IVV, SPY끼리만 총보수가 낮은 순으로 3개 보여줘",
        "VOO, IVV, SPY 중 총보수가 가장 낮은 순으로 세 개 비교해줘",
    ],
)
def test_explicit_closed_set_limit_is_not_downgraded_to_one_winner(
    question: str,
) -> None:
    assert _ranked_identity_requests_single_winner(question) is False


def test_candidate_set_count_does_not_hide_a_real_single_winner_request() -> None:
    assert (
        _ranked_identity_requests_single_winner(
            "VOO와 IVV 두 상품 중 총보수가 더 낮은 쪽을 알려줘"
        )
        is True
    )


@pytest.mark.parametrize(
    ("question", "subject", "relation"),
    [
        ("애플을 많이 보유한 해외 ETF를 찾아줘", "애플", "많이 보유한"),
        ("삼성전자를 가장 많이 담은 국내 ETF 세 개", "삼성전자", "가장 많이 담은"),
        ("Apple을 높은 비중으로 포함한 해외 ETF", "Apple", "높은 비중으로 포함한"),
    ],
)
def test_holding_quantity_modifier_stays_on_relation_not_entity(
    question: str,
    subject: str,
    relation: str,
) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ],
        evidence_spans=[],
        needs_clarification=False,
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=draft.product_families,
        product_mentions=(),
    )

    holding = [
        item
        for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    assert [(item.subject_span, item.relation_span) for item in holding] == [
        (subject, relation)
    ]
