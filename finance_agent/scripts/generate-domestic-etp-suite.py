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


CASES: list[dict[str, Any]] = [
    {
        "category": "type",
        "question": "국내 ETF를 상품명 오름차순으로 5개 보여줘",
        "constraints": [constraint("product_type", "eq", "ETF")],
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "type",
        "question": "국내 ETN을 종목코드 순으로 5개 찾아줘",
        "constraints": [constraint("product_type", "eq", "ETN")],
        "ranking": ranking("ticker", "asc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "미국 주식형 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "미국"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "국내 채권형 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "국내"),
            constraint("asset_type", "eq", "채권"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "원자재형 국내 ETN을 1일 수익률 높은 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETN"),
            constraint("asset_type", "eq", "원자재"),
        ],
        "ranking": ranking("one_day_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "글로벌 주식형 국내 ETF를 YTD 수익률 높은 순으로 3개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "글로벌"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("ytd_return_pct", "desc"),
        "limit": 3,
    },
    {
        "category": "region_asset",
        "question": "중국 주식형 국내 ETF를 3개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "중국"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "일본 주식형 국내 ETF를 1년 수익률 높은 순으로 4개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "일본"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("one_year_return_pct", "desc"),
        "limit": 4,
    },
    {
        "category": "region_asset",
        "question": "인도 주식형 국내 ETF를 6개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "인도"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("six_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "유럽 주식형 국내 ETF를 1개월 수익률 높은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "유럽"),
            constraint("asset_type", "eq", "주식"),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "판매 가능하고 거래정지가 아닌 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("sellable", "eq", True),
            constraint("trading_suspended", "eq", False),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "거래정지된 국내 ETF를 상품명 순으로 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("trading_suspended", "eq", True),
        ],
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "판매 불가인 국내 ETP를 종목코드 순으로 5개 보여줘",
        "constraints": [constraint("sellable", "eq", False)],
        "ranking": ranking("ticker", "asc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "연금 거래 가능한 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("pension_eligible", "eq", True),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "핵심 국내 ETF만 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("core_etf", "eq", True),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "status",
        "question": "연금 거래가 불가능한 국내 ETN을 1일 수익률 높은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETN"),
            constraint("pension_eligible", "eq", False),
        ],
        "ranking": ranking("one_day_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "총보수 0.4% 이하인 국내 ETF를 보수 낮은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("total_expense_ratio_pct", "lte", 0.4),
        ],
        "ranking": ranking("total_expense_ratio_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "총보수 0.1%에서 0.5% 사이인 국내 ETF를 보수 낮은 순으로 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("total_expense_ratio_pct", "between", [0.1, 0.5]),
        ],
        "ranking": ranking("total_expense_ratio_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "AUM 1조원 이상인 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("aum", "gte", 1_000_000_000_000),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "AUM 1천억원에서 1조원 사이인 국내 ETF를 AUM 큰 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("aum", "between", [100_000_000_000, 1_000_000_000_000]),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "종가 1만원 이하인 국내 ETF를 종가 낮은 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("close_price", "lte", 10_000),
        ],
        "ranking": ranking("close_price", "asc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "일 거래대금 100억원 이상인 국내 ETF를 거래대금 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("daily_trading_value", "gte", 10_000_000_000),
        ],
        "ranking": ranking("daily_trading_value", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1일 수익률 5% 초과인 국내 ETF를 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("one_day_return_pct", "gt", 5),
        ],
        "ranking": ranking("one_day_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1개월 수익률 10% 이상인 국내 ETF를 높은 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("one_month_return_pct", "gte", 10),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "3개월 수익률 -5%에서 5% 사이인 국내 ETF를 낮은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("three_month_return_pct", "between", [-5, 5]),
        ],
        "ranking": ranking("three_month_return_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "6개월 수익률 20% 초과인 국내 ETN을 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETN"),
            constraint("six_month_return_pct", "gt", 20),
        ],
        "ranking": ranking("six_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1년 수익률이 0% 미만인 국내 ETF를 낮은 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("one_year_return_pct", "lt", 0),
        ],
        "ranking": ranking("one_year_return_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "YTD 수익률 15% 이상인 주식형 국내 ETF를 높은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("asset_type", "eq", "주식"),
            constraint("ytd_return_pct", "gte", 15),
        ],
        "ranking": ranking("ytd_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "structure",
        "question": "2배 레버리지 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("leverage_factor", "eq", 2),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "structure",
        "question": "-2배 인버스 국내 ETN을 1개월 수익률 높은 순으로 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETN"),
            constraint("leverage_factor", "eq", -2),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "structure",
        "question": "액티브 전략 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("strategy", "eq", "액티브"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "structure",
        "question": "실물복제 국내 ETF를 총보수 낮은 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("strategy", "eq", "실물복제"),
        ],
        "ranking": ranking("total_expense_ratio_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "structure",
        "question": "전략 코드 C인 국내 ETN을 1일 수익률 높은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETN"),
            constraint("strategy", "eq", "C"),
        ],
        "ranking": ranking("one_day_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk",
        "question": "매우높은위험 1등급 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("risk_level", "eq", "매우높은위험(1등급)"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "risk",
        "question": "낮은위험 5등급 국내 ETF를 AUM 큰 순으로 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("risk_level", "eq", "낮은위험(5등급)"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "운용사에 미래에셋이 포함된 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("manager", "contains", "미래에셋"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "약어명에 KODEX가 들어간 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("short_name", "contains", "KODEX"),
        ],
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "기초지수에 MSCI가 포함된 국내 ETF를 AUM 큰 순으로 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("base_index", "contains", "MSCI"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "종목코드 A305080인 국내 ETP를 보여줘",
        "constraints": [constraint("ticker", "eq", "A305080")],
        "ranking": [],
        "limit": 1,
    },
    {
        "category": "lookup",
        "question": "상품번호 KR7305080004인 국내 ETP를 찾아줘",
        "constraints": [constraint("product_id", "eq", "KR7305080004")],
        "ranking": [],
        "limit": 1,
    },
    {
        "category": "region_asset",
        "question": "판매 가능하고 거래정지가 아닌 미국 채권형 국내 ETF를 1년 수익률 높은 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "미국"),
            constraint("asset_type", "eq", "채권"),
            constraint("sellable", "eq", True),
            constraint("trading_suspended", "eq", False),
        ],
        "ranking": ranking("one_year_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "region_asset",
        "question": "연금 거래 가능한 아시아 주식형 국내 ETF를 YTD 수익률 높은 순으로 3개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "아시아"),
            constraint("asset_type", "eq", "주식"),
            constraint("pension_eligible", "eq", True),
        ],
        "ranking": ranking("ytd_return_pct", "desc"),
        "limit": 3,
    },
    {
        "category": "region_asset",
        "question": "국내 혼합자산형 ETF를 AUM 큰 순으로 4개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("investment_region", "eq", "국내"),
            constraint("asset_type", "eq", "혼합자산"),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 4,
    },
    {
        "category": "region_asset",
        "question": "단기자금형 국내 ETF를 일 거래대금 큰 순으로 5개 찾아줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("asset_type", "eq", "단기자금"),
        ],
        "ranking": ranking("daily_trading_value", "desc"),
        "limit": 5,
    },
    {
        "category": "numeric",
        "question": "총보수 0.4% 초과인 국내 ETF를 보수 높은 순으로 3개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("total_expense_ratio_pct", "gt", 0.4),
        ],
        "ranking": ranking("total_expense_ratio_pct", "desc"),
        "limit": 3,
    },
    {
        "category": "structure",
        "question": "배수가 -1배에서 1배 사이인 국내 ETF를 1일 수익률 높은 순으로 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("leverage_factor", "between", [-1, 1]),
        ],
        "ranking": ranking("one_day_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk",
        "question": "다소높은위험 3등급이고 연금 거래 가능한 국내 ETF를 AUM 큰 순으로 5개 보여줘",
        "constraints": [
            constraint("product_type", "eq", "ETF"),
            constraint("risk_level", "eq", "다소높은위험(3등급)"),
            constraint("pension_eligible", "eq", True),
        ],
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "safety",
        "question": "괜찮은 국내 ETF를 5개 추천해줘",
        "constraints": [constraint("product_type", "eq", "ETF")],
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "ambiguity",
    },
    {
        "category": "safety",
        "question": "배당수익률 높은 국내 ETF를 5개 보여줘",
        "constraints": [constraint("product_type", "eq", "ETF")],
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "safety",
        "question": "국내 ETF와 공모펀드를 함께 비교해줘",
        "constraints": [constraint("product_type", "eq", "ETF")],
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze the domestic ETP core-50 suite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/domestic_etp.sqlite3"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/normalized/domestic_etp.sqlite3.manifest.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "packages/finance_agent_core/src/finance_agent_core/"
            "evaluation/suites/domestic_etp_core_50.json"
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
            "id": f"detp-{index:03d}",
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
            plan = case.expected_plan("domestic_etp")
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
        "suite_id": "domestic-etp-core-50",
        "suite_version": "1.0",
        "dataset": "domestic_etp",
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
