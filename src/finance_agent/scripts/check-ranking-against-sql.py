#!/usr/bin/env python
"""Compare the Agent's ranked TOP-K against an independently written SQL query.

The 2026-08-29 All-ON report asked for bond and overseas-ETP ordering to be
re-checked against an independent SQL oracle rather than the planner's own
output. This script asks a running Backend the same questions a user would ask,
then recomputes the expected order directly from the approved SQLite files with
a query written separately from the planner. A mismatch is either a ranking
defect or a tie whose ordering is not pinned by the data.

Usage:
    check-ranking-against-sql.py --database-dir DIR [--backend-url URL] [--limit N]

The approved databases are not in Git, so `--database-dir` must point at a
prepared data volume or an equivalent local build.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Identifiers are printed in parentheses after the product name. Overseas
# tickers can be as short as two characters (for example `VOO`).
_IDENTIFIER = re.compile(r"\(([A-Z0-9:._]{2,20})\)")


@dataclass(frozen=True)
class RankingCase:
    question: str
    database: str
    table: str
    column: str
    direction: str


CASES: tuple[RankingCase, ...] = (
    RankingCase(
        "국내채권을 발행잔액 큰 순으로 {limit}개 보여줘",
        "bond",
        "bond_products",
        "issue_amount_minor_units",
        "desc",
    ),
    RankingCase(
        "국내채권을 잔존일수 짧은 순으로 {limit}개 보여줘",
        "bond",
        "bond_products",
        "remaining_days",
        "asc",
    ),
    RankingCase(
        "해외 ETF를 총보수 낮은 순으로 {limit}개 보여줘",
        "overseas_etp",
        "overseas_etp_products",
        "total_expense_ratio_micro_pct",
        "asc",
    ),
    RankingCase(
        "해외 ETF를 종가 높은 순으로 {limit}개 보여줘",
        "overseas_etp",
        "overseas_etp_products",
        "close_price_eight_decimals",
        "desc",
    ),
    RankingCase(
        "해외 ETF를 거래대금 큰 순으로 {limit}개 보여줘",
        "overseas_etp",
        "overseas_etp_products",
        "daily_trading_value_eight_decimals",
        "desc",
    ),
    RankingCase(
        "해외 ETF를 거래량 많은 순으로 {limit}개 보여줘",
        "overseas_etp",
        "overseas_etp_products",
        "daily_volume_eight_decimals",
        "desc",
    ),
    RankingCase(
        "국내 ETF를 총보수 낮은 순으로 {limit}개 보여줘",
        "domestic_etp",
        "domestic_etp_products",
        "total_expense_ratio_micro_pct",
        "asc",
    ),
)


def expected_order(
    directory: Path, case: RankingCase, limit: int
) -> list[tuple[str, int]]:
    """Recompute the ranking straight from the approved database.

    A bond can occupy several rows, so the ranking is collapsed to one row per
    public identifier. Zero and missing values are excluded, matching the
    organizer's 2026-08-26 answer that a zero carries no ranking basis.
    """

    aggregate = "max" if case.direction == "desc" else "min"
    with sqlite3.connect(
        f"file:{directory / case.database}.sqlite3?mode=ro", uri=True
    ) as db:
        return db.execute(
            f"""
            SELECT ticker, {aggregate}({case.column}) AS value
            FROM {case.table}
            WHERE is_quarantined = 0
              AND {case.column} IS NOT NULL
              AND {case.column} <> 0
            GROUP BY ticker
            ORDER BY value {case.direction}, ticker ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def observed_order(
    backend_url: str, question: str, question_id: str, limit: int
) -> list[str]:
    query = urllib.parse.urlencode({"question_id": question_id, "question": question})
    with urllib.request.urlopen(f"{backend_url}?{query}", timeout=300) as response:
        payload = json.load(response)
    return _IDENTIFIER.findall(payload["answer"])[:limit]


def tied_at_boundary(rows: list[tuple[str, int]]) -> bool:
    """Report whether the cut-off value is shared, which makes the tail arbitrary."""

    return len(rows) > 1 and rows[-1][1] == rows[-2][1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", required=True, type=Path)
    parser.add_argument("--backend-url", default="http://127.0.0.1:18002/answer")
    parser.add_argument("--limit", type=int, default=5)
    arguments = parser.parse_args(argv)

    mismatches = 0
    for index, case in enumerate(CASES, start=1):
        question = case.question.format(limit=arguments.limit)
        expected_rows = expected_order(arguments.database_dir, case, arguments.limit)
        expected = [ticker for ticker, _ in expected_rows]
        observed = observed_order(
            arguments.backend_url, question, f"rank-{index}", arguments.limit
        )

        if expected == observed:
            print(f"OK    {question}")
            continue

        mismatches += 1
        note = " (경계값 동점)" if tied_at_boundary(expected_rows) else ""
        print(f"DIFF  {question}{note}")
        print(f"        독립 SQL: {expected}")
        print(f"        Agent   : {observed}")

    print(f"\n{len(CASES) - mismatches}/{len(CASES)} 일치")
    if mismatches:
        print(
            "경계값이 동점이면 어느 상품이 잘려 나가는지는 데이터가 정하지 않는다. "
            "Agent는 oracle.py의 `product_id ASC`로 순서를 고정하며, 이 스크립트는 "
            "공개 식별자 순으로 계산하므로 두 결과가 다를 수 있다."
        )
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
