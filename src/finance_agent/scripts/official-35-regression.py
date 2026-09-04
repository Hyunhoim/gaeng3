#!/usr/bin/env python3
"""Run the public 35-case deterministic contract regression on approved DBs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 3)


def _metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item["duration_ms"]) for item in items]
    payloads = [float(item["payload_bytes"]) for item in items]
    return {
        "case_count": len(items),
        "contract_pass_count": sum(bool(item["contract_passed"]) for item in items),
        "contract_pass_rate": round(
            sum(bool(item["contract_passed"]) for item in items) / len(items), 6
        ),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "mean_ms": round(statistics.fmean(durations), 3),
        "payload_p95_bytes": _percentile(payloads, 0.95),
        "error_count": sum(item["http_status"] != 200 for item in items),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for family in ProductFamily:
        parser.add_argument(f"--{family.value.replace('_', '-')}-db", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if not arguments.output.is_absolute() or not arguments.output.parent.is_dir():
        raise SystemExit("--output must be absolute and its parent must exist")
    suite = json.loads(arguments.suite.read_text(encoding="utf-8"))
    cases = suite.get("cases")
    if not isinstance(cases, list) or len(cases) != 35:
        raise SystemExit("official regression suite must contain exactly 35 cases")
    agent = RoutedFinanceAgent(
        {
            family: getattr(arguments, f"{family.value}_db") for family in ProductFamily
        },
        capability_execution_overrides={ProductFamily.FUND},
        require_approved_databases=True,
    )
    results: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        with bind_request_deadline(RequestDeadline.after(270.0)):
            adapter = execute_answer_request(
                agent,
                BackendAgentRequest(request_id=case["id"], question=case["question"]),
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response = adapter.response
        actual_families = [family.value for family in response.product_families]
        expected_families = list(case["expected_families"])
        base_match = (
            adapter.http_status_code == 200
            and response.status.value == case["expected_status"]
            and response.intent.value == case["expected_intent"]
            and set(actual_families) == set(expected_families)
        )
        evidence_ok = (
            response.status.value not in {"success", "not_found"}
            or (
                bool(response.as_of_dates)
                and bool(response.citations)
                and response.answer_mode.value == "deterministic"
            )
        )
        control_ok = (
            response.status.value not in {"clarification", "unsupported"}
            or (
                response.query_plan is None
                and not response.products
                and response.answer_mode.value == "control"
            )
        )
        serialized = response.model_dump_json()
        results.append(
            {
                "id": case["id"],
                "difficulty": case["difficulty"],
                "question": case["question"],
                "expected_status": case["expected_status"],
                "actual_status": response.status.value,
                "expected_intent": case["expected_intent"],
                "actual_intent": response.intent.value,
                "expected_families": expected_families,
                "actual_families": actual_families,
                "http_status": adapter.http_status_code,
                "candidate_count": response.candidate_count,
                "result_count": len(response.products),
                "citation_count": len(response.citations),
                "answer_mode": response.answer_mode.value,
                "control_answer": (
                    response.answer
                    if response.status.value in {"clarification", "unsupported", "error"}
                    else None
                ),
                "duration_ms": duration_ms,
                "payload_bytes": len(serialized.encode("utf-8")),
                "base_contract_match": base_match,
                "evidence_contract_match": evidence_ok,
                "control_contract_match": control_ok,
                "contract_passed": base_match and evidence_ok and control_ok,
                "known_gap": case.get("known_gap"),
            }
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[item["difficulty"]].append(item)
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "suite_id": suite["suite_id"],
        "suite_version": suite["suite_version"],
        "evaluation_kind": "public_contract_regression_not_external_blind",
        "semantic_exactness_limitation": (
            "The server verifier proves results match the compiled plan; independent human gold "
            "is still required to prove that every compiled plan matches user intent."
        ),
        "overall": _metrics(results),
        "by_difficulty": {
            difficulty: _metrics(items) for difficulty, items in sorted(grouped.items())
        },
        "results": results,
    }
    canonical = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    report["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        with arguments.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        raise SystemExit("output already exists") from None
    print(
        json.dumps(
            {
                "overall": report["overall"],
                "by_difficulty": report["by_difficulty"],
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
