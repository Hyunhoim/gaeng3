#!/usr/bin/env python3
"""Run bounded live HCX Intent/final-generation checks without retaining secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.providers import (
    HyperClovaXCallRecord,
    HyperClovaXFinalAnswerProvider,
    HyperClovaXHTTPTransport,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSettings,
)
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline

_MAX_KEY_BYTES = 4096


def _load_key_file(path: Path) -> str:
    if not path.is_absolute():
        raise RuntimeError("HCLX key file must be absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 0 < before.st_size <= _MAX_KEY_BYTES
        ):
            raise RuntimeError("HCLX key file is insecure")
        payload = os.read(descriptor, before.st_size)
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        identity = lambda item: (  # noqa: E731 - immutable file identity tuple
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise RuntimeError("HCLX key file changed while loading")
        value = payload.decode("utf-8").rstrip("\r\n")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not value:
        raise RuntimeError("HCLX key file is empty")
    return value


def _load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("questions file must contain a non-empty cases list")
    normalized: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict):
            raise RuntimeError("each case must be an object")
        case_id = item.get("id")
        question = item.get("question")
        if not isinstance(case_id, str) or not case_id.strip():
            raise RuntimeError("each case requires a non-blank id")
        if not isinstance(question, str) or not question.strip():
            raise RuntimeError("each case requires a non-blank question")
        expected_status = item.get("expected_status")
        expected_intent = item.get("expected_intent")
        expected_families = item.get("expected_families")
        has_expectation = any(
            value is not None
            for value in (expected_status, expected_intent, expected_families)
        )
        if has_expectation and not (
            isinstance(expected_status, str)
            and isinstance(expected_intent, str)
            and isinstance(expected_families, list)
            and all(isinstance(value, str) for value in expected_families)
        ):
            raise RuntimeError("case expectations must be complete when provided")
        normalized.append(
            {
                "id": case_id,
                "question": question,
                "expected_status": expected_status,
                "expected_intent": expected_intent,
                "expected_families": expected_families,
            }
        )
    if len({item["id"] for item in normalized}) != len(normalized):
        raise RuntimeError("case ids must be unique")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for family in ProductFamily:
        parser.add_argument(f"--{family.value.replace('_', '-')}-db", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--maximum-cases", type=int, default=2)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.start_index < 0:
        raise SystemExit("--start-index cannot be negative")
    if arguments.maximum_cases < 1:
        raise SystemExit("--maximum-cases must be positive")
    if not arguments.output.is_absolute() or not arguments.output.parent.is_dir():
        raise SystemExit("--output must be an absolute path in an existing directory")
    settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=arguments.timeout_seconds)
    calls: list[HyperClovaXCallRecord] = []
    transport = HyperClovaXHTTPTransport(api_key=_load_key_file(arguments.api_key_file))
    agent = RoutedFinanceAgent(
        {
            family: getattr(arguments, f"{family.value}_db") for family in ProductFamily
        },
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(
            settings,
            transport,
            on_call=calls.append,
            require_execution_structure=True,
        ),
        official_answer_provider=HyperClovaXFinalAnswerProvider(
            settings,
            transport,
            on_call=calls.append,
        ),
        require_official_hclx=True,
        capability_execution_overrides={ProductFamily.FUND},
        require_approved_databases=True,
    )
    results: list[dict[str, Any]] = []
    all_cases = _load_cases(arguments.questions)
    cases = all_cases[
        arguments.start_index : arguments.start_index + arguments.maximum_cases
    ]
    if not cases:
        raise SystemExit("selected question range is empty")
    for item in cases:
        call_start = len(calls)
        started = time.perf_counter()
        with bind_request_deadline(RequestDeadline.after(270.0)):
            adapter = execute_answer_request(
                agent,
                BackendAgentRequest(request_id=item["id"], question=item["question"]),
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response = adapter.response
        case_calls = calls[call_start:]
        serialized = response.model_dump_json()
        runtime_verified = (
            adapter.http_status_code == 200
            and response.error is None
            and (
                (
                    response.status.value in {"success", "not_found"}
                    and response.provider_model == "HCX-007"
                    and response.answer_mode.value == "llm_grounded"
                )
                or (
                    response.status.value in {"clarification", "unsupported"}
                    and response.provider_model is None
                    and response.answer_mode.value == "control"
                )
            )
            and [record.operation for record in case_calls]
            == ["intent_analysis", "final_answer"]
            and all(record.outcome == "success" for record in case_calls)
        )
        expected_contract_matched = None
        if item["expected_status"] is not None:
            expected_contract_matched = (
                response.status.value == item["expected_status"]
                and response.intent.value == item["expected_intent"]
                and set(family.value for family in response.product_families)
                == set(item["expected_families"])
            )
        results.append(
            {
                "id": item["id"],
                "question_sha256": hashlib.sha256(
                    item["question"].encode("utf-8")
                ).hexdigest(),
                "http_status": adapter.http_status_code,
                "status": response.status.value,
                "intent": response.intent.value,
                "product_families": [family.value for family in response.product_families],
                "answer_mode": response.answer_mode.value,
                "provider_model": response.provider_model,
                "candidate_count": response.candidate_count,
                "result_count": len(response.products),
                "citation_count": len(response.citations),
                "payload_bytes": len(serialized.encode("utf-8")),
                "answer_sha256": hashlib.sha256(response.answer.encode("utf-8")).hexdigest(),
                "duration_ms": duration_ms,
                "provider_calls": [
                    {
                        "operation": record.operation,
                        "outcome": record.outcome,
                        "status_code": record.status_code,
                        "latency_ms": record.latency_ms,
                        "input_tokens": (
                            None if record.usage is None else record.usage.input_tokens
                        ),
                        "output_tokens": (
                            None if record.usage is None else record.usage.output_tokens
                        ),
                    }
                    for record in case_calls
                ],
                "verifier_passed": runtime_verified,
                "expected_contract_matched": expected_contract_matched,
            }
        )
    evaluated_contracts = [
        item["expected_contract_matched"]
        for item in results
        if item["expected_contract_matched"] is not None
    ]
    all_runtime_verified = all(item["verifier_passed"] for item in results)
    all_expected_contracts_matched = (
        None if not evaluated_contracts else all(evaluated_contracts)
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "model": "HCX-007",
        "secret_retention": "forbidden",
        "raw_provider_response_retention": "forbidden",
        "case_count": len(results),
        "all_runtime_verified": all_runtime_verified,
        "all_expected_contracts_matched": all_expected_contracts_matched,
        "expected_contract_match_count": sum(bool(value) for value in evaluated_contracts),
        "expected_contract_count": len(evaluated_contracts),
        "all_verified": all_runtime_verified
        and all_expected_contracts_matched is not False,
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
                "all_verified": report["all_verified"],
                "all_runtime_verified": report["all_runtime_verified"],
                "all_expected_contracts_matched": report[
                    "all_expected_contracts_matched"
                ],
                "case_count": report["case_count"],
                "expected_contract_match_count": report[
                    "expected_contract_match_count"
                ],
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
