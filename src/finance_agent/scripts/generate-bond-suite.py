from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from finance_agent_core.evaluation.models import EvaluationCase
from finance_agent_core.evaluation.runner import sha256_file
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
)
from finance_agent_core.storage import connect_read_only, load_all_records


def constraint(field: str, operator: str, value: object) -> dict[str, object]:
    return {
        "field": field,
        "operator": operator,
        "value": value,
        "strength": "locked",
    }


def ranking(field: str, direction: str) -> list[dict[str, str]]:
    return [{"field": field, "direction": direction, "nulls": "last"}]


AVAILABLE = constraint("currently_buyable", "eq", True)


def available_with(*items: dict[str, object]) -> list[dict[str, object]]:
    return [AVAILABLE, *items]


CASES: list[dict[str, Any]] = [
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "현재 매수 가능한 국내채권을 매수 가능 수량 큰 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("buyable_quantity", "desc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 잔존일수 낮은 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("remaining_days", "asc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 만기일 오름차순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("maturity_date", "asc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 상품명 오름차순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 종목코드 오름차순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("ticker", "asc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 듀레이션 낮은 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("duration_years", "asc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 표면이율 높은 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("coupon_rate_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "availability",
        "question": "매수 가능한 국내채권을 세후수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(),
        "ranking": ranking("after_tax_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "class",
        "question": "매수 가능한 회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_major_class", "eq", "회사채")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "class",
        "question": "매수 가능한 국공채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_major_class", "eq", "국공채")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "class",
        "question": "매수 가능한 특수채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_major_class", "eq", "특수채")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "class",
        "question": "매수 가능한 개인투자용국채를 상품명 오름차순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "개인투자용국채")
        ),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "잔존일수 365일 이하인 매수 가능한 회사채를 매수수익률 높은 순으로 3개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("remaining_days", "lte", 365),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 3,
    },
    {
        "category": "numeric",
        "question": "잔존일수 100일에서 500일 사이인 매수 가능한 회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("remaining_days", "between", [100, 500]),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "매수수익률 4% 이상인 매수 가능한 국내채권을 수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("buy_yield_pct", "gte", 4)),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "매수수익률 3%에서 4% 사이인 매수 가능한 국내채권을 수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("buy_yield_pct", "between", [3, 4])),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "표면이율 4% 이상인 매수 가능한 국내채권을 표면이율 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("coupon_rate_pct", "gte", 4)),
        "ranking": ranking("coupon_rate_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "표면이율 2%에서 3% 사이인 매수 가능한 국내채권을 표면이율 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("coupon_rate_pct", "between", [2, 3])),
        "ranking": ranking("coupon_rate_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "매수 가능 수량 1억원 이상인 매수 가능한 국내채권을 수량 큰 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("buyable_quantity", "gte", 100_000_000)
        ),
        "ranking": ranking("buyable_quantity", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "듀레이션 1년 이하인 매수 가능한 국내채권을 듀레이션 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("duration_years", "lte", 1)),
        "ranking": ranking("duration_years", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "듀레이션 1년에서 3년 사이인 매수 가능한 국내채권을 듀레이션 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("duration_years", "between", [1, 3])),
        "ranking": ranking("duration_years", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "발행잔액 1조원 이상인 매수 가능한 국내채권을 발행잔액 큰 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("issue_amount", "gte", 1_000_000_000_000)
        ),
        "ranking": ranking("issue_amount", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "발행잔액 100억원에서 1조원 사이인 매수 가능한 국내채권을 발행잔액 큰 순으로 5개 보여줘",
        "constraints": available_with(
            constraint(
                "issue_amount",
                "between",
                [10_000_000_000, 1_000_000_000_000],
            )
        ),
        "ranking": ranking("issue_amount", "desc"),
        "limit": 5,
    },
    {
        "category": "market",
        "question": "장내에서 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_market", "eq", "장내")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "market",
        "question": "장외에서 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_market", "eq", "장외")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "market",
        "question": "장내 매수 가능한 회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("bond_market", "eq", "장내"),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 국고채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_subclass", "eq", "국고채")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 지역개발채를 잔존일수 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_type", "eq", "지역개발채")),
        "ranking": ranking("remaining_days", "asc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 도시철도공채를 잔존일수 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_type", "eq", "도시철도공채")),
        "ranking": ranking("remaining_days", "asc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 일반회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("bond_type", "eq", "일반회사채"),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 할부금융채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("bond_type", "eq", "할부금융채")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 보험회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("bond_type", "eq", "보험회사채"),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "매수 가능한 금융지주회사채를 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("bond_major_class", "eq", "회사채"),
            constraint("bond_type", "eq", "금융지주회사채"),
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "발행사에 메리츠가 포함된 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("issuer", "contains", "메리츠")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "발행사 한국투자캐피탈의 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("issuer", "contains", "한국투자캐피탈")
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "상품명에 신종자본증권이 포함된 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint("product_name", "contains", "신종자본증권")
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "종목코드 KR6157181C34인 국내채권을 보여줘",
        "constraints": [constraint("ticker", "eq", "KR6157181C34")],
        "ranking": [],
        "limit": 1,
    },
    {
        "category": "rating",
        "question": "신용등급이 AA-인 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("credit_rating", "eq", "AA-")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "rating",
        "question": "신용등급이 AAA인 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("credit_rating", "eq", "AAA")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "date",
        "question": "발행일 2025-01-01 이후인 국내채권을 상품명 오름차순으로 5개 보여줘",
        "constraints": [constraint("issue_date", "gt", "2025-01-01")],
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "date",
        "question": "만기일 2027-01-01 이전인 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("maturity_date", "lt", "2027-01-01")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "date",
        "question": "만기일 2027-01-01에서 2028-12-31 사이인 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(
            constraint(
                "maturity_date",
                "between",
                ["2027-01-01", "2028-12-31"],
            )
        ),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "currency",
        "question": "원화 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("trading_currency", "eq", "KRW")),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "currency",
        "question": "달러 국내채권을 상품명 오름차순으로 5개 보여줘",
        "constraints": [constraint("trading_currency", "eq", "USD")],
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "매수수익률 3% 이하인 매수 가능한 국내채권을 수익률 낮은 순으로 5개 보여줘",
        "constraints": available_with(constraint("buy_yield_pct", "lte", 3)),
        "ranking": ranking("buy_yield_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "잔존일수 1000일 이상인 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘",
        "constraints": available_with(constraint("remaining_days", "gte", 1000)),
        "ranking": ranking("buy_yield_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "safety",
        "question": "안전한 매수 가능한 국내채권을 5개 추천해줘",
        "constraints": available_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "ambiguity",
    },
    {
        "category": "safety",
        "question": "신용등급 AA- 이상인 국내채권을 찾아줘",
        "constraints": [],
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "safety",
        "question": "매수 가능한 국내채권의 가격 전망을 알려줘",
        "constraints": available_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze the domestic bond core-50 suite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/bond.sqlite3"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/normalized/bond.sqlite3.manifest.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "packages/finance_agent_core/src/finance_agent_core/"
            "evaluation/suites/bond_core_50.json"
        ),
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if len(CASES) != 50:
        raise ValueError(f"expected 50 cases, found {len(CASES)}")
    oracle = SQLiteOracle(arguments.database)
    verifier = ResultVerifier()
    with connect_read_only(arguments.database) as connection:
        universe = load_all_records(connection)
    payload_cases: list[dict[str, Any]] = []
    for index, spec in enumerate(CASES, start=1):
        disposition = spec.get("disposition", "execute")
        payload: dict[str, Any] = {
            "id": f"bond-{index:03d}",
            "split": "development" if index <= 40 else "holdout",
            "category": spec["category"],
            "question": spec["question"],
            "constraints": spec["constraints"],
            "ranking": spec["ranking"],
            "limit": spec["limit"],
            "disposition": disposition,
            "blocker": spec.get("blocker"),
            "oracle": (
                {"candidate_count": 0, "top_product_ids": []}
                if disposition == "execute"
                else None
            ),
        }
        case = EvaluationCase.model_validate(payload)
        if disposition == "execute":
            plan = case.expected_plan("bond")
            validated_plan = authorize_internal_evaluation_plan(
                plan, arguments.database
            )
            executed = oracle.execute(validated_plan)
            verified = verifier.verify(plan, executed, universe)
            payload["oracle"] = {
                "candidate_count": verified.candidate_count,
                "top_product_ids": [record.product_id for record in verified.records],
            }
        payload_cases.append(payload)
    suite = {
        "suite_id": "bond-core-50",
        "suite_version": "1.0",
        "dataset": "bond",
        "database_sha256": sha256_file(arguments.database),
        "manifest_sha256": sha256_file(arguments.manifest),
        "cases": payload_cases,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(suite, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "cases": len(payload_cases),
                "development": sum(
                    case["split"] == "development" for case in payload_cases
                ),
                "holdout": sum(case["split"] == "holdout" for case in payload_cases),
                "database_sha256": suite["database_sha256"],
                "manifest_sha256": suite["manifest_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
