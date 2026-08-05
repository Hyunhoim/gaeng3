from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from finance_agent_core.evaluation.models import EvaluationCase
from finance_agent_core.evaluation.runner import sha256_file
from finance_agent_core.execution import ResultVerifier, SQLiteOracle
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


PUBLIC = constraint("public_offering", "eq", True)


def public_with(*items: dict[str, object]) -> list[dict[str, object]]:
    return [PUBLIC, *items]


CASES: list[dict[str, Any]] = [
    {
        "category": "scope_status",
        "question": "공모펀드를 상품명 오름차순으로 5개 보여줘",
        "constraints": public_with(),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "scope_status",
        "question": "현재 판매 중인 공모펀드를 상품명 순으로 5개 찾아줘",
        "constraints": public_with(constraint("sellable", "eq", True)),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "scope_status",
        "question": "미래에셋증권에서 판매 가능한 공모펀드를 상품명 순으로 5개 보여줘",
        "constraints": public_with(constraint("company_sellable", "eq", True)),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "scope_status",
        "question": "현재 판매 중이면서 당사에서도 판매 가능한 공모펀드를 5개 보여줘",
        "constraints": public_with(
            constraint("sellable", "eq", True),
            constraint("company_sellable", "eq", True),
        ),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "scope_status",
        "question": "판매가 완료된 공모펀드를 상품명 순으로 5개 찾아줘",
        "constraints": public_with(constraint("sellable", "eq", False)),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "scope_status",
        "question": "달러로 거래되는 공모펀드를 상품명 순으로 5개 보여줘",
        "constraints": public_with(constraint("trading_currency", "eq", "USD")),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "국내에 투자하는 원화 공모펀드를 AUM 큰 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("fund_geography_scope", "eq", "국내"),
            constraint("trading_currency", "eq", "KRW"),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "해외 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("fund_geography_scope", "eq", "해외")),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "국내외 혼합 공모펀드를 1개월 수익률 높은 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("fund_geography_scope", "eq", "국내외혼합")
        ),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "주식형 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("fund_management_attribute", "eq", "주식형")
        ),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "채권형 공모펀드를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("fund_management_attribute", "eq", "채권형")
        ),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "재간접 원화 공모펀드를 AUM 큰 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("fund_management_attribute", "eq", "재간접"),
            constraint("trading_currency", "eq", "KRW"),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "MMF 공모펀드를 원화 AUM 큰 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("fund_management_attribute", "eq", "MMF"),
            constraint("trading_currency", "eq", "KRW"),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "투자지역이 글로벌인 공모펀드를 6개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("investment_region", "eq", "글로벌")),
        "ranking": ranking("six_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "아시아 지역 공모펀드를 3개월 수익률 높은 순으로 5개 찾아줘",
        "constraints": public_with(constraint("investment_region", "eq", "아시아")),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "유럽 지역 공모펀드를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("investment_region", "eq", "유럽")),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "개인용으로 구분된 공모펀드를 상품명 순으로 5개 보여줘",
        "constraints": public_with(constraint("investor_type", "eq", "개인")),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "classification",
        "question": "법인용으로 구분된 공모펀드를 상품명 순으로 5개 찾아줘",
        "constraints": public_with(constraint("investor_type", "eq", "법인")),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "매우높은위험 1등급 공모펀드를 1개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("risk_level", "eq", "매우높은위험(1등급)")
        ),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "높은위험 2등급 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("risk_level", "eq", "높은위험(2등급)")),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "다소높은위험 3등급 공모펀드를 6개월 수익률 높은 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("risk_level", "eq", "다소높은위험(3등급)")
        ),
        "ranking": ranking("six_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "보통위험 4등급 원화 공모펀드를 AUM 큰 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("risk_level", "eq", "보통위험(4등급)"),
            constraint("trading_currency", "eq", "KRW"),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "낮은위험 5등급 공모펀드를 1주 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("risk_level", "eq", "낮은위험(5등급)")),
        "ranking": ranking("one_week_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "환헤지하는 공모펀드를 3개월 수익률 높은 순으로 5개 찾아줘",
        "constraints": public_with(constraint("currency_hedged", "eq", True)),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "risk_hedge",
        "question": "환헤지하지 않는 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("currency_hedged", "eq", False)),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1주 수익률이 5% 이상인 공모펀드를 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("one_week_return_pct", "gte", 5)),
        "ranking": ranking("one_week_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1주 수익률이 -1%에서 1% 사이인 공모펀드를 낮은 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("one_week_return_pct", "between", [-1, 1])
        ),
        "ranking": ranking("one_week_return_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1개월 수익률이 10% 이상인 공모펀드를 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("one_month_return_pct", "gte", 10)),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "1개월 수익률이 0%에서 5% 사이인 공모펀드를 높은 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("one_month_return_pct", "between", [0, 5])
        ),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "3개월 수익률이 20% 이상인 공모펀드를 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("three_month_return_pct", "gte", 20)),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "3개월 수익률이 -5%에서 5% 사이인 공모펀드를 낮은 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("three_month_return_pct", "between", [-5, 5])
        ),
        "ranking": ranking("three_month_return_pct", "asc"),
        "limit": 5,
    },
    {
        "category": "return",
        "question": "6개월 수익률이 50% 이상인 공모펀드를 높은 순으로 5개 보여줘",
        "constraints": public_with(constraint("six_month_return_pct", "gte", 50)),
        "ranking": ranking("six_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "compound",
        "question": "당사에서 판매 중인 해외 주식형 공모펀드 중 3개월 수익률이 높은 상품 5개",
        "constraints": public_with(
            constraint("sellable", "eq", True),
            constraint("company_sellable", "eq", True),
            constraint("fund_geography_scope", "eq", "해외"),
            constraint("fund_management_attribute", "eq", "주식형"),
        ),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "compound",
        "question": "국내 채권형이면서 낮은위험 5등급인 공모펀드를 1개월 수익률 높은 순으로 5개",
        "constraints": public_with(
            constraint("fund_geography_scope", "eq", "국내"),
            constraint("fund_management_attribute", "eq", "채권형"),
            constraint("risk_level", "eq", "낮은위험(5등급)"),
        ),
        "ranking": ranking("one_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "aum",
        "question": "AUM이 1조원 이상인 원화 공모펀드를 AUM 큰 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("trading_currency", "eq", "KRW"),
            constraint("aum", "gte", 1_000_000_000_000),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "aum",
        "question": "AUM이 1천억원에서 1조원 사이인 원화 공모펀드를 큰 순으로 5개 찾아줘",
        "constraints": public_with(
            constraint("trading_currency", "eq", "KRW"),
            constraint("aum", "between", [100_000_000_000, 1_000_000_000_000]),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "aum",
        "question": "AUM이 100만달러 이상인 달러 공모펀드를 큰 순으로 5개 보여줘",
        "constraints": public_with(
            constraint("trading_currency", "eq", "USD"),
            constraint("aum", "gte", 1_000_000),
        ),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "safety",
        "question": "운용사 이름이 미래에셋인 공모펀드를 찾아줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "safety",
        "question": "오늘 기준 최신 수익률이 높은 공모펀드를 보여줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "lookup",
        "question": "상품번호 KR5010101401 공모펀드를 조회해줘",
        "constraints": public_with(constraint("product_id", "eq", "KR5010101401")),
        "ranking": [],
        "limit": 1,
    },
    {
        "category": "lookup",
        "question": "짧은 이름에 미래에셋이 들어간 공모펀드를 이름 순으로 5개 찾아줘",
        "constraints": public_with(constraint("short_name", "contains", "미래에셋")),
        "ranking": ranking("short_name", "asc"),
        "limit": 5,
    },
    {
        "category": "lookup",
        "question": "정식 상품명에 인도가 포함된 공모펀드를 이름 순으로 5개 보여줘",
        "constraints": public_with(constraint("product_name", "contains", "인도")),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "blind_paraphrase",
        "question": "환율 변동을 막는 해외 주식형 공모펀드 가운데 석 달 성과가 좋은 것 5개",
        "constraints": public_with(
            constraint("currency_hedged", "eq", True),
            constraint("fund_geography_scope", "eq", "해외"),
            constraint("fund_management_attribute", "eq", "주식형"),
        ),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "blind_paraphrase",
        "question": "원화 공모펀드 중 돈이 가장 많이 모인 상품 다섯 개만 알려줘",
        "constraints": public_with(constraint("trading_currency", "eq", "KRW")),
        "ranking": ranking("aum", "desc"),
        "limit": 5,
    },
    {
        "category": "blind_paraphrase",
        "question": "최근 석 달 수익이 마이너스가 아닌 공모펀드를 성과순으로 5개",
        "constraints": public_with(constraint("three_month_return_pct", "gte", 0)),
        "ranking": ranking("three_month_return_pct", "desc"),
        "limit": 5,
    },
    {
        "category": "blind_paraphrase",
        "question": "개인 투자자 대상으로 분류된 판매 중 공모펀드 5개를 이름순으로 보여줘",
        "constraints": public_with(
            constraint("investor_type", "eq", "개인"),
            constraint("sellable", "eq", True),
        ),
        "ranking": ranking("product_name", "asc"),
        "limit": 5,
    },
    {
        "category": "safety",
        "question": "안전하고 괜찮은 공모펀드 5개를 추천해줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "ambiguity",
    },
    {
        "category": "safety",
        "question": "총보수와 판매수수료가 가장 낮은 공모펀드 5개를 보여줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "safety",
        "question": "1년 수익률이 높은 공모펀드를 5개 찾아줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
    {
        "category": "safety",
        "question": "클래스는 합쳐서 대표 펀드별 AUM 합계가 큰 순으로 5개 보여줘",
        "constraints": public_with(),
        "ranking": [],
        "limit": 5,
        "disposition": "block",
        "blocker": "unsupported",
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze the public fund core-50 suite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/fund.sqlite3"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/normalized/fund.sqlite3.manifest.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "packages/finance_agent_core/src/finance_agent_core/"
            "evaluation/suites/fund_core_50.json"
        ),
    )
    return parser


def _validate_case_contract(spec: dict[str, Any]) -> None:
    fields = {item["field"] for item in spec["constraints"]}
    public_scope = [
        item for item in spec["constraints"] if item["field"] == "public_offering"
    ]
    if public_scope != [PUBLIC]:
        raise ValueError("every fund case must contain exactly one locked public scope")
    ranking_fields = {item["field"] for item in spec["ranking"]}
    if "aum" in fields | ranking_fields:
        currency_scopes = [
            item
            for item in spec["constraints"]
            if item["field"] == "trading_currency" and item["operator"] == "eq"
        ]
        if len(currency_scopes) != 1:
            raise ValueError("every AUM case must lock exactly one trading currency")


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
        _validate_case_contract(spec)
        disposition = spec.get("disposition", "execute")
        payload: dict[str, Any] = {
            "id": f"fund-{index:03d}",
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
            plan = case.expected_plan("fund")
            executed = oracle.execute(plan)
            verified = verifier.verify(plan, executed, universe)
            payload["oracle"] = {
                "candidate_count": verified.candidate_count,
                "top_product_ids": [record.product_id for record in verified.records],
            }
        payload_cases.append(payload)
    suite = {
        "suite_id": "fund-core-50",
        "suite_version": "1.0",
        "dataset": "fund",
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
                "execute": sum(
                    case["disposition"] == "execute" for case in payload_cases
                ),
                "block": sum(case["disposition"] == "block" for case in payload_cases),
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
