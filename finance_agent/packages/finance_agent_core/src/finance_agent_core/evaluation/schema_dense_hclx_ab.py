from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict

from finance_agent_core.agent.providers import (
    HyperClovaXCallRecord,
    HyperClovaXHTTPTransport,
    HyperClovaXSemanticResolverProvider,
    HyperClovaXSettings,
    hyperclova_failure_reason,
)
from finance_agent_core.agent.semantic_resolution import (
    ResolutionDecision,
    ResolutionOperation,
    SchemaFieldCandidate,
    SemanticResolutionGate,
    SemanticResolutionRequest,
    SemanticResolverProvider,
    SpanSource,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.schema_dense_residual_holdout import (
    finalize_report,
    load_residual_holdout,
)

_MAX_KEY_BYTES = 4096


class _SelectedCase(TypedDict):
    id: str
    split: str
    product_family: str
    residual_span: str
    expected: Literal["resolve", "clarify"]
    gold_field_id: str | None
    lexical: list[str]
    dense: list[str]
    dense_scores: list[float]


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
        identity = lambda item: (  # noqa: E731 - compact immutable file identity
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if len(payload) != before.st_size or identity(before) != identity(after):
            raise RuntimeError("HCLX key file changed while loading")
        if identity(after) != identity(current):
            raise RuntimeError("HCLX key file changed while loading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        key = payload.decode("ascii").strip()
    except (UnicodeDecodeError, UnboundLocalError):
        raise RuntimeError("HCLX key file is invalid") from None
    if not key or any(character.isspace() for character in key):
        raise RuntimeError("HCLX key file is invalid")
    return key


def _load_dense_report(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Dense report must be an absolute regular file")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (
        report.get("evaluation_id") != "schema-dense-residual-developer-holdout-v1"
        or report.get("status") != "developer_authored_not_independent_blind"
        or not isinstance(report.get("test_diagnostics"), list)
        or not isinstance(report.get("selective_policy"), dict)
    ):
        raise ValueError("Dense report contract differs")
    expected = finalize_report(report)["report_sha256"]
    if report.get("report_sha256") != expected:
        raise ValueError("Dense report SHA-256 differs")
    return report


def _select_cases(
    dense_report: Mapping[str, object],
    *,
    maximum_calls: int,
) -> list[_SelectedCase]:
    suite, _ = load_residual_holdout()
    suite_cases = {item["id"]: item for item in suite["cases"]}  # type: ignore[index]
    selective: dict[str, object] = dense_report["selective_policy"]  # type: ignore[assignment]
    diagnostics: list[dict[str, object]] = selective["test_diagnostics"]  # type: ignore[assignment]
    selected = [item for item in diagnostics if item["decision"] == "hclx_review"][:maximum_calls]
    cases: list[_SelectedCase] = []
    for diagnostic in selected:
        source = suite_cases[diagnostic["id"]]
        cases.append(
            {
                "id": source["id"],
                "split": source["split"],
                "product_family": source["product_family"],
                "residual_span": source["residual_span"],
                "expected": source["expected"],
                "gold_field_id": source["gold_field_id"],
                "lexical": diagnostic["lexical"],
                "dense": diagnostic["hclx_candidate_field_ids"],
                "dense_scores": diagnostic["hclx_candidate_scores"],
            }
        )
    return cases


def evaluate_hclx_candidate_selection(
    provider: SemanticResolverProvider,
    dense_report: Mapping[str, object],
    *,
    maximum_calls: int = 20,
) -> dict[str, object]:
    if not 1 <= maximum_calls <= 100:
        raise ValueError("maximum_calls must be between 1 and 100")
    selected = _select_cases(dense_report, maximum_calls=maximum_calls)
    results: list[dict[str, object]] = []
    gate = SemanticResolutionGate()
    for case in selected:
        lexical_rank = {field_id: rank for rank, field_id in enumerate(case["lexical"], 1)}
        request = SemanticResolutionRequest(
            request_id="p1-hclx-ab-" + case["id"],
            residual_span=case["residual_span"],
            context_after=" 기준으로 내림차순 정렬",
            product_family=ProductFamily(case["product_family"]),
            interaction_intent=InteractionIntent.SEARCH,
            allowed_operations=(ResolutionOperation.RANK,),
            candidates=tuple(
                SchemaFieldCandidate(
                    field_id=field_id,
                    rank=rank,
                    dense_score=case["dense_scores"][rank - 1],
                    lexical_rank=lexical_rank.get(field_id),
                )
                for rank, field_id in enumerate(case["dense"], 1)
            ),
            expected_direction=SortDirection.DESC,
            hard_filter_lock_sha256=hashlib.sha256(
                ("p1-hclx-ab-lock|" + case["id"]).encode("utf-8")
            ).hexdigest(),
        )
        try:
            draft = provider.resolve_semantics(request)
            failure_reason = None
            decision = draft.decision.value
            selected_field_id = draft.selected_field_id
            admitted = False
            if draft.decision is ResolutionDecision.RESOLVE:
                receipt = gate.admit(draft, request, source=SpanSource.HCLX)
                admitted = receipt.field_id == draft.selected_field_id
            correct = (
                admitted
                and case["expected"] == "resolve"
                and draft.selected_field_id == case["gold_field_id"]
            ) or (draft.decision is ResolutionDecision.CLARIFY and case["expected"] == "clarify")
        except Exception as error:  # noqa: BLE001 - sanitized provider evaluation result
            failure_reason = hyperclova_failure_reason(error)
            decision = "provider_failure"
            selected_field_id = "__none__"
            admitted = False
            correct = False
        dense_correct = case["expected"] == "resolve" and case["dense"][0] == case["gold_field_id"]
        lexical_correct = (
            case["expected"] == "resolve"
            and bool(case["lexical"])
            and case["lexical"][0] == case["gold_field_id"]
        ) or (case["expected"] == "clarify" and not case["lexical"])
        results.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "gold_field_id": case["gold_field_id"],
                "candidate_field_ids": case["dense"],
                "lexical_correct": lexical_correct,
                "dense_top1_correct": dense_correct,
                "hclx_decision": decision,
                "hclx_selected_field_id": selected_field_id,
                "server_admitted": admitted,
                "hclx_correct": correct,
                "failure_reason": failure_reason,
            }
        )
    denominator = len(results) or 1
    return {
        "schema_version": "1.0",
        "evaluation_id": "schema-dense-hclx-candidate-selection-ab-v1",
        "status": "developer_authored_limited_live_hclx_not_independent_blind",
        "scope": "candidate_selection_only_no_sql_or_answer_generation",
        "dense_report_sha256": dense_report["report_sha256"],
        "case_count": len(results),
        "metrics": {
            "lexical_accuracy": round(
                sum(item["lexical_correct"] for item in results) / denominator,
                6,
            ),
            "dense_top1_accuracy": round(
                sum(item["dense_top1_correct"] for item in results) / denominator,
                6,
            ),
            "hclx_accuracy": round(sum(item["hclx_correct"] for item in results) / denominator, 6),
            "provider_failure_count": sum(item["failure_reason"] is not None for item in results),
        },
        "cases": results,
        "activation_decision": "shadow_only_until_independent_blind_and_zero_false_execution",
        "report_sha256": "filled_after_canonicalization",
    }


def _write_new(path: Path, report: Mapping[str, object]) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise SystemExit("output must be an absolute path in an existing directory")
    serialized = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except FileExistsError:
        raise SystemExit("output already exists") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded live HCLX Schema Dense A/B.")
    parser.add_argument("--dense-report", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--maximum-calls", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    call_records: list[HyperClovaXCallRecord] = []
    provider = HyperClovaXSemanticResolverProvider(
        HyperClovaXSettings(model="HCX-007", timeout_seconds=arguments.timeout_seconds),
        HyperClovaXHTTPTransport(api_key=_load_key_file(arguments.api_key_file)),
        on_call=call_records.append,
    )
    report = evaluate_hclx_candidate_selection(
        provider,
        _load_dense_report(arguments.dense_report),
        maximum_calls=arguments.maximum_calls,
    )
    report["provider_observation"] = {
        "call_count": len(call_records),
        "success_count": sum(item.outcome == "success" for item in call_records),
        "total_latency_ms": round(sum(item.latency_ms for item in call_records), 3),
        "input_tokens": sum(item.usage.input_tokens for item in call_records if item.usage),
        "output_tokens": sum(item.usage.output_tokens for item in call_records if item.usage),
    }
    report = finalize_report(report)
    _write_new(arguments.output, report)
    print(
        json.dumps(
            {
                "activation_decision": report["activation_decision"],
                "metrics": report["metrics"],
                "provider_observation": report["provider_observation"],
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
        )
    )


__all__ = ["evaluate_hclx_candidate_selection"]


if __name__ == "__main__":
    main()
