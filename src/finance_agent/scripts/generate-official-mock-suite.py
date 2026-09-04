#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = (
    PROJECT_ROOT
    / "packages"
    / "finance_agent_core"
    / "src"
    / "finance_agent_core"
    / "evaluation"
    / "suites"
)
OUTPUT = SUITE_ROOT / "official_mock_v1_30.json"


def _read(name: str) -> dict[str, Any]:
    return json.loads((SUITE_ROOT / name).read_text(encoding="utf-8"))


def _red_case(
    source: dict[str, Any],
    source_id: str,
    *,
    difficulty: str,
    answerability: str,
) -> dict[str, Any]:
    indexed = {case["id"]: case for case in source["cases"]}
    original = indexed[source_id]
    return {
        "difficulty": difficulty,
        "answerability": answerability,
        "coverage_family": original["coverage_family"],
        "attack_class": original["attack_class"],
        "source_case_id": source_id,
        "question": original["question"],
        "expectation": deepcopy(original["expectation"]),
    }


def _performance_case(
    source: dict[str, Any],
    source_id: str,
    *,
    difficulty: str,
) -> dict[str, Any]:
    indexed = {case["id"]: case for case in source["cases"]}
    original = indexed[source_id]
    family = original["product_family"]
    expected = original["expected"]
    aggregates = expected["aggregates"]
    if aggregates:
        interaction_intent = "aggregate"
        query_plan_intent = "aggregate"
        product_ids: list[str] = []
        aggregate_functions = [item["function"] for item in aggregates]
        evidence_kind = "aggregate"
        llm_answer_eligible = False
        attack_class = "aggregate_boundary"
    else:
        interaction_intent = "search"
        query_plan_intent = "search"
        product_ids = expected["top_product_ids"]
        aggregate_functions = []
        evidence_kind = "product"
        llm_answer_eligible = True
        attack_class = "adversarial_wording"
    return {
        "difficulty": difficulty,
        "answerability": "answerable",
        "coverage_family": family,
        "attack_class": attack_class,
        "source_case_id": source_id,
        "question": original["question"],
        "expectation": {
            "backend_status": "success",
            "interaction_intent": interaction_intent,
            "product_families": [family],
            "query_plan_intent": query_plan_intent,
            "candidate_count": expected["candidate_count"],
            "product_ids": product_ids,
            "comparison_fields": [],
            "aggregate_functions": aggregate_functions,
            "evidence_kind": evidence_kind,
            "llm_answer_eligible": llm_answer_eligible,
            "forbidden_answer_fragments": [],
        },
    }


def build_suite() -> dict[str, Any]:
    red = _read("internal_red_team_v1.json")
    performance = _read("search_aggregate_performance_8.json")
    briefing = _read("briefing_examples_v1.json")
    cases = [
        _red_case(
            red,
            "internal-red-team-v1-002",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-012",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-022",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-032",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-004",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-014",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-024",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-034",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-001",
            difficulty="low",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-017",
            difficulty="low",
            answerability="unanswerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-003",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-013",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-023",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-033",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-011",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-021",
            difficulty="medium",
            answerability="answerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-031",
            difficulty="medium",
            answerability="answerable",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-bond-search",
            difficulty="medium",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-fund-search",
            difficulty="medium",
        ),
        _red_case(
            red,
            "internal-red-team-v1-006",
            difficulty="medium",
            answerability="unanswerable",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-overseas-etp-search",
            difficulty="high",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-domestic-etp-search",
            difficulty="high",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-overseas-etp-aggregate",
            difficulty="high",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-domestic-etp-aggregate",
            difficulty="high",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-bond-aggregate",
            difficulty="high",
        ),
        _performance_case(
            performance,
            "search-aggregate-perf-fund-aggregate",
            difficulty="high",
        ),
        {
            "difficulty": "high",
            "answerability": "answerable",
            "coverage_family": "overseas_etp",
            "attack_class": "cross_family",
            "source_case_id": "cross-family-search-v1-001",
            "question": "국내 ETF와 해외 ETF를 각각 3개 보여줘",
            "expectation": {
                "backend_status": "success",
                "interaction_intent": "search",
                "product_families": ["domestic_etp", "overseas_etp"],
                "query_plan_intent": None,
                "candidate_count": 6778,
                "product_ids": [
                    "KR70000D0009",
                    "KR70000H0005",
                    "KR70000J0003",
                    "101:IVEG.O",
                    "101:IWTR.O",
                    "101:MNTL.O",
                ],
                "comparison_fields": [],
                "aggregate_functions": [],
                "evidence_kind": "product",
                "llm_answer_eligible": True,
                "forbidden_answer_fragments": [],
            },
        },
        _red_case(
            red,
            "internal-red-team-v1-005",
            difficulty="high",
            answerability="unanswerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-028",
            difficulty="high",
            answerability="unanswerable",
        ),
        _red_case(
            red,
            "internal-red-team-v1-040",
            difficulty="high",
            answerability="unanswerable",
        ),
    ]
    for index, case in enumerate(cases, start=1):
        case["id"] = f"official-mock-v1-{index:03d}"
    return {
        "schema_version": "1.0",
        "suite_id": "official-mock-v1-30",
        "suite_version": "1.0",
        "status": "public_official_shape_mock_not_blind",
        "is_blind": False,
        "author_role": "ai_engineering",
        "source": {
            "title": briefing["source"]["title"],
            "artifact_name": briefing["source"]["artifact_name"],
            "sha256": briefing["source"]["sha256"],
            "interpretation": "official_distribution_only_not_evaluation_items",
        },
        "source_suite_ids": [
            "internal-red-team-v1",
            "search-aggregate-performance-8",
            "cross-family-search-v1-4",
        ],
        "data": deepcopy(red["data"]),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the frozen official-shaped mock suite."
    )
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    rendered = f"{json.dumps(build_suite(), ensure_ascii=False, indent=2)}\n"
    if arguments.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "official mock suite differs from the deterministic generator"
            )
        print(f"Official mock suite is reproducible: {OUTPUT}")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
