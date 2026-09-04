from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent_core.audit_validation import (
    AuditValidationInputError,
    AuditValidationReport,
    AuditValidationStatus,
    validate_audit_jsonl,
)
from finance_agent_core.observability import AuditEvent, AuditOutcome, AuditStage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_AUDIT_BYTES = 512 * 1024 * 1024
_MAX_AUDIT_EVENTS = 1_000_000
_BENCHMARK_PHASE_CONTRACT = (
    ("warm_c1", "warm", 1, ()),
    ("warm_c2", "warm", 2, ()),
    ("warm_c4", "warm", 4, ()),
    (
        "admission_c8",
        "admission_stress",
        8,
        ("request_overloaded", "request_timeout"),
    ),
)

_CANONICAL_STAGE_REASONS: dict[AuditStage, frozenset[str]] = {
    AuditStage.ROUTE: frozenset(
        {"routed_execute", "routed_clarify", "routed_unsupported", "routing_failed"}
    ),
    AuditStage.COMPILER: frozenset(
        {
            "plan_compiled",
            "plan_blocked",
            "plan_unresolved",
            "family_plan_compiled",
            "family_plan_failed",
        }
    ),
    AuditStage.AUTHORITY: frozenset({"authority_granted", "authority_denied"}),
    AuditStage.SQL: frozenset({"parameterized_statement_completed", "statement_failed"}),
    AuditStage.ORACLE: frozenset({"oracle_completed", "oracle_failed"}),
    AuditStage.VERIFIER: frozenset({"verification_passed", "verification_failed"}),
    AuditStage.RENDERER: frozenset({"rendering_completed", "rendering_failed"}),
    AuditStage.ANSWER: frozenset(
        {
            "execution_completed",
            "execution_clarified",
            "execution_unsupported",
            "execution_fallback",
            "execution_failed",
            "deadline_exceeded",
            "completed_after_deadline",
        }
    ),
}

_SEGMENTS: dict[str, tuple[AuditStage, frozenset[str] | None]] = {
    "router": (AuditStage.ROUTE, _CANONICAL_STAGE_REASONS[AuditStage.ROUTE]),
    "queryplan_compiler": (
        AuditStage.COMPILER,
        _CANONICAL_STAGE_REASONS[AuditStage.COMPILER],
    ),
    "authority_total": (
        AuditStage.AUTHORITY,
        _CANONICAL_STAGE_REASONS[AuditStage.AUTHORITY],
    ),
    "authority_connection": (
        AuditStage.SQL,
        frozenset({"authority_connection_opened", "authority_connection_failed"}),
    ),
    "oracle_connection": (
        AuditStage.SQL,
        frozenset({"oracle_connection_opened", "oracle_connection_failed"}),
    ),
    "oracle_statements": (
        AuditStage.SQL,
        frozenset({"oracle_statements_completed", "oracle_statements_failed"}),
    ),
    "oracle_total": (AuditStage.ORACLE, _CANONICAL_STAGE_REASONS[AuditStage.ORACLE]),
    "verifier_projection_connection": (
        AuditStage.SQL,
        frozenset(
            {
                "verifier_projection_connection_opened",
                "verifier_projection_connection_failed",
            }
        ),
    ),
    "verifier_projection_fetch": (
        AuditStage.SQL,
        frozenset({"verifier_projection_fetched", "verifier_projection_failed"}),
    ),
    "verifier_row_materialization": (
        AuditStage.VERIFIER,
        frozenset({"verifier_rows_materialized", "verifier_materialization_failed"}),
    ),
    "verifier_universe_total": (
        AuditStage.VERIFIER,
        frozenset({"verifier_universe_loaded", "verifier_universe_failed"}),
    ),
    "pure_verifier": (
        AuditStage.VERIFIER,
        frozenset({"pure_verification_passed", "pure_verification_failed"}),
    ),
    "verifier_total": (
        AuditStage.VERIFIER,
        _CANONICAL_STAGE_REASONS[AuditStage.VERIFIER],
    ),
    "evidence_and_answer_renderer": (
        AuditStage.RENDERER,
        _CANONICAL_STAGE_REASONS[AuditStage.RENDERER],
    ),
    "citation_generation": (AuditStage.SERIALIZATION, frozenset({"citations_built"})),
    "backend_dto_build": (AuditStage.SERIALIZATION, frozenset({"backend_dto_built"})),
    "official_dto_build": (AuditStage.SERIALIZATION, frozenset({"official_dto_built"})),
    "http_response_serialization": (
        AuditStage.SERIALIZATION,
        frozenset({"http_response_serialized"}),
    ),
    "answer_total": (AuditStage.ANSWER, _CANONICAL_STAGE_REASONS[AuditStage.ANSWER]),
}

_NON_OVERLAPPING_BOTTLENECK_SEGMENTS = (
    "router",
    "queryplan_compiler",
    "authority_total",
    "oracle_total",
    "verifier_total",
    "evidence_and_answer_renderer",
    "citation_generation",
    "backend_dto_build",
    "official_dto_build",
    "http_response_serialization",
)


@dataclass(frozen=True, slots=True)
class PhaseSlice:
    name: str
    request_id_sha256: str
    skip_invocations: int
    invocation_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def numeric_summary(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {
            "sample_count": 0,
            "min": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }
    return {
        "sample_count": len(values),
        "min": round(min(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 6),
    }


def load_audit_invocations(path: Path) -> dict[str, tuple[AuditEvent, ...]]:
    size = path.stat().st_size
    if size > _MAX_AUDIT_BYTES:
        raise ValueError("audit JSONL exceeds the bounded analysis size")
    grouped: dict[str, list[AuditEvent]] = defaultdict(list)
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, start=1):
            if line_number > _MAX_AUDIT_EVENTS:
                raise ValueError("audit JSONL exceeds the bounded event count")
            if not line.endswith(b"\n"):
                raise ValueError("audit JSONL has an incomplete final record")
            try:
                event = AuditEvent.model_validate_json(line)
            except Exception as error:
                raise ValueError(f"audit JSONL record {line_number} is invalid") from error
            if event.invocation_id_sha256 is None or event.event_sequence is None:
                raise ValueError("performance analysis requires correlated AuditEvent v1.1 records")
            grouped[event.invocation_id_sha256].append(event)
    return {
        invocation_id: tuple(sorted(events, key=lambda item: item.event_sequence or 0))
        for invocation_id, events in grouped.items()
    }


def _final_request_id_sha256(events: Sequence[AuditEvent]) -> str:
    request_events = [event for event in events if event.stage is AuditStage.REQUEST]
    if request_events:
        return request_events[-1].request_id_sha256
    answer_events = [event for event in events if event.stage is AuditStage.ANSWER]
    return answer_events[-1].request_id_sha256 if answer_events else events[-1].request_id_sha256


def _final_question_sha256(events: Sequence[AuditEvent]) -> str:
    request_events = [event for event in events if event.stage is AuditStage.REQUEST]
    if request_events:
        return request_events[-1].question_sha256
    answer_events = [event for event in events if event.stage is AuditStage.ANSWER]
    return answer_events[-1].question_sha256 if answer_events else events[-1].question_sha256


def most_common_enriched_request_id(
    invocations: Mapping[str, Sequence[AuditEvent]],
) -> str:
    counts = Counter(_final_request_id_sha256(events) for events in invocations.values())
    if not counts:
        raise ValueError("audit JSONL contains no invocations")
    request_id_sha256, _count = counts.most_common(1)[0]
    return request_id_sha256


def _invocation_started_at(events: Sequence[AuditEvent]) -> datetime:
    return min(event.observed_at_utc for event in events)


def select_phase_invocations(
    invocations: Mapping[str, Sequence[AuditEvent]],
    phase: PhaseSlice,
) -> tuple[tuple[AuditEvent, ...], ...]:
    matching = [
        tuple(events)
        for events in invocations.values()
        if _final_request_id_sha256(events) == phase.request_id_sha256
    ]
    matching.sort(key=_invocation_started_at)
    end = phase.skip_invocations + phase.invocation_count
    if end > len(matching):
        raise ValueError(
            f"phase {phase.name!r} requires {end} matching invocations; found {len(matching)}"
        )
    return tuple(matching[phase.skip_invocations : end])


def _selected_durations(
    invocations: Sequence[Sequence[AuditEvent]],
    *,
    stage: AuditStage,
    reasons: frozenset[str] | None,
) -> list[float]:
    values: list[float] = []
    for events in invocations:
        selected = [
            event.duration_ms
            for event in events
            if event.stage is stage
            and event.outcome is not AuditOutcome.STARTED
            and (reasons is None or event.reason_code in reasons)
        ]
        if selected:
            values.append(sum(selected))
    return values


def summarize_phase(
    phase: PhaseSlice,
    invocations: Sequence[Sequence[AuditEvent]],
) -> dict[str, Any]:
    segment_summaries: dict[str, dict[str, float | int]] = {}
    for name, (stage, reasons) in _SEGMENTS.items():
        summary = numeric_summary(_selected_durations(invocations, stage=stage, reasons=reasons))
        summary["invocation_coverage"] = round(int(summary["sample_count"]) / len(invocations), 6)
        segment_summaries[name] = summary

    reason_values: dict[str, list[float]] = defaultdict(list)
    for events in invocations:
        per_invocation: dict[str, float] = defaultdict(float)
        for event in events:
            if event.outcome is not AuditOutcome.STARTED:
                per_invocation[f"{event.stage.value}:{event.reason_code}"] += event.duration_ms
        for key, value in per_invocation.items():
            reason_values[key].append(value)

    candidates = {
        name: float(segment_summaries[name]["p95"])
        for name in _NON_OVERLAPPING_BOTTLENECK_SEGMENTS
        if int(segment_summaries[name]["sample_count"]) > 0
    }
    bottleneck = max(candidates, key=candidates.get) if candidates else None
    terminal_request_events = [
        event
        for events in invocations
        for event in events
        if event.stage is AuditStage.REQUEST and event.outcome is not AuditOutcome.STARTED
    ]
    error_count = sum(
        event.outcome in {AuditOutcome.FAILED, AuditOutcome.TIMED_OUT, AuditOutcome.BLOCKED}
        for event in terminal_request_events
    )
    return {
        "name": phase.name,
        "request_id_sha256": phase.request_id_sha256,
        "invocation_count": len(invocations),
        "terminal_request_count": len(terminal_request_events),
        "terminal_error_or_timeout_count": error_count,
        "segments_ms": segment_summaries,
        "reason_code_ms": {
            key: numeric_summary(values) for key, values in sorted(reason_values.items())
        },
        "dominant_non_overlapping_segment_by_p95": bottleneck,
        "dominant_segment_p95_ms": candidates.get(bottleneck) if bottleneck else None,
    }


def instrumentation_coverage(phases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    required = {
        "router",
        "queryplan_compiler",
        "authority_total",
        "authority_connection",
        "oracle_connection",
        "oracle_statements",
        "oracle_total",
        "verifier_projection_connection",
        "verifier_projection_fetch",
        "verifier_row_materialization",
        "verifier_universe_total",
        "pure_verifier",
        "verifier_total",
        "evidence_and_answer_renderer",
        "citation_generation",
        "backend_dto_build",
        "official_dto_build",
        "http_response_serialization",
        "answer_total",
    }
    phase_coverage: dict[str, Any] = {}
    observed: set[str] = set()
    for phase in phases:
        invocation_count = int(phase["invocation_count"])
        summaries = phase["segments_ms"]
        observed_in_phase = {name for name in required if int(summaries[name]["sample_count"]) > 0}
        complete_in_phase = {
            name for name in required if int(summaries[name]["sample_count"]) == invocation_count
        }
        observed.update(observed_in_phase)
        phase_coverage[str(phase["name"])] = {
            "invocation_count": invocation_count,
            "missing_segments": sorted(required - observed_in_phase),
            "partial_segments": sorted(observed_in_phase - complete_in_phase),
            "complete": complete_in_phase == required,
        }
    globally_missing = sorted(required - observed)
    return {
        "required_segments": sorted(required),
        "observed_segments": sorted(required & observed),
        "missing_segments": globally_missing,
        "phases": phase_coverage,
        "complete": bool(phases)
        and not globally_missing
        and all(item["complete"] for item in phase_coverage.values()),
        "audit_fsync_latency_source": "separate same-filesystem fsync probe required",
    }


def phase_execution_gate(phases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    results = {
        str(phase["name"]): {
            "invocation_count": int(phase["invocation_count"]),
            "terminal_request_count": int(phase["terminal_request_count"]),
            "terminal_error_or_timeout_count": int(phase["terminal_error_or_timeout_count"]),
            "passed": (
                int(phase["terminal_request_count"]) == int(phase["invocation_count"])
                and int(phase["terminal_error_or_timeout_count"]) == 0
            ),
        }
        for phase in phases
    }
    return {
        "phases": results,
        "passed": bool(phases) and all(item["passed"] for item in results.values()),
    }


def validate_audit_for_analysis(path: Path) -> AuditValidationReport:
    try:
        report = validate_audit_jsonl(path)
    except AuditValidationInputError as error:
        raise ValueError("Audit lifecycle/path validation could not read the input") from error
    if report.status is not AuditValidationStatus.PASSED:
        raise ValueError("Audit lifecycle/path validation failed")
    return report


def _audit_validation_summary(report: AuditValidationReport) -> dict[str, Any]:
    return {
        "status": report.status.value,
        "audit_file_sha256": report.audit_file_sha256,
        "record_count": report.record_count,
        "valid_event_count": report.valid_event_count,
        "invalid_event_count": report.invalid_event_count,
        "invocation_count": report.invocation_count,
        "lifecycle_complete_invocation_count": (report.lifecycle_complete_invocation_count),
        "execution_path_complete_invocation_count": (
            report.execution_path_complete_invocation_count
        ),
        "incident_counts": report.incident_counts.model_dump(mode="json"),
        "issue_count": report.issue_count,
    }


def _benchmark_fingerprints(
    report: Mapping[str, Any],
) -> dict[str, tuple[str, ...]] | None:
    fingerprints: dict[str, tuple[str, ...]] = {}
    smoke = report.get("semantic_contract")
    cases = smoke.get("cases") if isinstance(smoke, Mapping) else None
    if not isinstance(cases, list) or not cases:
        return None
    for item in cases:
        if not isinstance(item, Mapping) or item.get("passed") is not True:
            return None
        case_id = item.get("case_id")
        digest = item.get("response_sha256")
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            return None
        key = f"semantic_contract:{case_id}"
        if key in fingerprints:
            return None
        fingerprints[key] = (digest,)

    phases = report.get("phases")
    if not isinstance(phases, list) or not phases:
        return None
    phase_names: set[str] = set()
    for phase in phases:
        if not isinstance(phase, Mapping) or not isinstance(phase.get("name"), str):
            return None
        phase_name = phase["name"]
        if not phase_name or phase_name in phase_names:
            return None
        phase_names.add(phase_name)
        actual_count = phase.get("actual_count")
        summary = phase.get("summary")
        outcomes = summary.get("outcomes") if isinstance(summary, Mapping) else None
        if (
            not isinstance(actual_count, int)
            or isinstance(actual_count, bool)
            or actual_count < 1
            or not isinstance(outcomes, Mapping)
            or not outcomes
        ):
            return None
        observed_count = 0
        for outcome, value in outcomes.items():
            if not isinstance(outcome, str) or not outcome or not isinstance(value, Mapping):
                return None
            hashes = value.get("canonical_response_sha256")
            count = value.get("count")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 1
                or value.get("deterministic") is not True
                or not isinstance(hashes, list)
                or len(hashes) != 1
                or not isinstance(hashes[0], str)
                or _SHA256.fullmatch(hashes[0]) is None
            ):
                return None
            key = f"phase:{phase_name}:{outcome}"
            if key in fingerprints:
                return None
            fingerprints[key] = (hashes[0],)
            observed_count += count
        if observed_count != actual_count:
            return None
    return fingerprints or None


def _benchmark_comparison_contract(report: Mapping[str, Any]) -> dict[str, Any] | None:
    if (
        report.get("schema_version") != "1.0"
        or report.get("suite_id") != "deterministic-api-baseline-v1"
        or report.get("passed") is not True
        or _benchmark_fingerprints(report) is None
    ):
        return None
    target = report.get("target")
    target_fields = (
        "question_sha256",
        "question_id_sha256",
        "expected_status",
        "expected_intent",
        "expected_product_families",
        "expected_answer_mode",
        "expected_fund_execution_policy",
        "llm_expected",
        "dense_expected",
    )
    if not isinstance(target, Mapping) or any(name not in target for name in target_fields):
        return None
    if any(
        not isinstance(target.get(name), str) or _SHA256.fullmatch(target[name]) is None
        for name in ("question_sha256", "question_id_sha256")
    ):
        return None
    semantic = report.get("semantic_contract")
    if not isinstance(semantic, Mapping) or semantic.get("passed") is not True:
        return None
    phases = report.get("phases")
    phase_fields = (
        "name",
        "kind",
        "concurrency",
        "requested_count",
        "actual_count",
        "accepted_control_codes",
    )
    if (
        not isinstance(phases, list)
        or not phases
        or any(
            not isinstance(phase, Mapping) or any(name not in phase for name in phase_fields)
            for phase in phases
        )
    ):
        return None
    names = [phase["name"] for phase in phases if isinstance(phase, Mapping)]
    if len(set(names)) != len(names) or len(phases) != len(_BENCHMARK_PHASE_CONTRACT):
        return None
    for phase, expected_phase in zip(phases, _BENCHMARK_PHASE_CONTRACT, strict=True):
        if not isinstance(phase, Mapping) or phase.get("passed") is not True:
            return None
        expected_name, expected_kind, expected_concurrency, expected_controls = expected_phase
        requested_count = phase.get("requested_count")
        actual_count = phase.get("actual_count")
        if (
            phase.get("name") != expected_name
            or phase.get("kind") != expected_kind
            or phase.get("concurrency") != expected_concurrency
            or phase.get("accepted_control_codes") != list(expected_controls)
            or not isinstance(requested_count, int)
            or isinstance(requested_count, bool)
            or requested_count < 1
            or not isinstance(actual_count, int)
            or isinstance(actual_count, bool)
            or actual_count
            != math.ceil(requested_count / expected_concurrency) * expected_concurrency
        ):
            return None
        summary = phase.get("summary")
        if (
            not isinstance(summary, Mapping)
            or summary.get("requests") != phase.get("actual_count")
            or summary.get("completed_http") != phase.get("actual_count")
            or summary.get("passed") != phase.get("actual_count")
            or summary.get("failed") != 0
            or summary.get("deterministic") is not True
            or summary.get("transport_error_counts") != {}
            or summary.get("violation_counts") != {}
        ):
            return None
        if not expected_controls and summary.get("control_code_counts") != {}:
            return None
    return {
        "schema_version": report["schema_version"],
        "suite_id": report["suite_id"],
        "target": {name: target[name] for name in target_fields},
        "phase_specs": [
            {name: phase[name] for name in phase_fields}
            for phase in phases
            if isinstance(phase, Mapping)
        ],
    }


def compare_benchmark_fingerprints(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    raw_left = _benchmark_fingerprints(baseline)
    raw_right = _benchmark_fingerprints(candidate)
    fingerprints_valid = raw_left is not None and raw_right is not None
    left = raw_left or {}
    right = raw_right or {}
    left_contract = _benchmark_comparison_contract(baseline)
    right_contract = _benchmark_comparison_contract(candidate)
    contract_match = left_contract is not None and left_contract == right_contract
    shared = sorted(set(left) & set(right))
    changed = [key for key in shared if left[key] != right[key]]
    missing = sorted(set(left) - set(right))
    added = sorted(set(right) - set(left))
    return {
        "baseline_fingerprint_count": len(left),
        "candidate_fingerprint_count": len(right),
        "shared_fingerprint_count": len(shared),
        "changed_keys": changed,
        "missing_candidate_keys": missing,
        "candidate_only_keys": added,
        "comparison_contract_complete": (left_contract is not None and right_contract is not None),
        "comparison_contract_match": contract_match,
        "fingerprint_sets_valid": fingerprints_valid,
        "exact_match": (
            bool(left)
            and fingerprints_valid
            and contract_match
            and not changed
            and not missing
            and not added
        ),
    }


def _benchmark_audit_binding(
    benchmark: Mapping[str, Any],
    selected_phases: Sequence[tuple[PhaseSlice, Sequence[Sequence[AuditEvent]]]],
) -> dict[str, Any]:
    contract = _benchmark_comparison_contract(benchmark)
    mismatch_codes: set[str] = set()
    if contract is None:
        return {
            "passed": False,
            "selected_phase_count": len(selected_phases),
            "matched_phase_count": 0,
            "mismatch_codes": ["benchmark_contract_incomplete"],
        }

    target = contract["target"]
    benchmark_phases = {str(phase["name"]): phase for phase in contract["phase_specs"]}
    expected_offsets: dict[str, int] = {}
    expected_offset = 0
    for benchmark_phase in contract["phase_specs"]:
        expected_offsets[str(benchmark_phase["name"])] = expected_offset
        expected_offset += int(benchmark_phase["actual_count"])
    selected_names = [phase.name for phase, _invocations in selected_phases]
    expected_order = [
        str(phase["name"]) for phase in contract["phase_specs"] if phase["name"] in selected_names
    ]
    required_warm_names = {"warm_c1", "warm_c2", "warm_c4"}
    if not required_warm_names.issubset(selected_names):
        mismatch_codes.add("required_warm_phases_missing")
    if selected_names != expected_order:
        mismatch_codes.add("phase_order_mismatch")

    matched_phase_count = 0
    for phase, invocations in selected_phases:
        benchmark_phase = benchmark_phases.get(phase.name)
        phase_matches = True
        if benchmark_phase is None:
            mismatch_codes.add("phase_not_in_benchmark")
            continue
        if benchmark_phase["actual_count"] != phase.invocation_count:
            mismatch_codes.add("phase_invocation_count_mismatch")
            phase_matches = False
        if expected_offsets[phase.name] != phase.skip_invocations:
            mismatch_codes.add("phase_offset_mismatch")
            phase_matches = False
        if phase.request_id_sha256 != target["question_id_sha256"]:
            mismatch_codes.add("phase_request_id_mismatch")
            phase_matches = False
        if any(
            _final_request_id_sha256(events) != target["question_id_sha256"]
            for events in invocations
        ):
            mismatch_codes.add("audit_request_id_mismatch")
            phase_matches = False
        if any(
            _final_question_sha256(events) != target["question_sha256"] for events in invocations
        ):
            mismatch_codes.add("audit_question_mismatch")
            phase_matches = False
        if phase_matches:
            matched_phase_count += 1

    return {
        "passed": not mismatch_codes and bool(selected_phases),
        "selected_phase_count": len(selected_phases),
        "matched_phase_count": matched_phase_count,
        "mismatch_codes": sorted(mismatch_codes),
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError(f"{path.name} must contain one JSON object")
    return payload


def _validate_phase_slices(phases: Sequence[PhaseSlice]) -> tuple[PhaseSlice, ...]:
    if not phases:
        raise ValueError("at least one phase is required")

    ranges_by_request: dict[str, list[PhaseSlice]] = defaultdict(list)
    phase_names: set[str] = set()
    for phase in phases:
        if (
            not isinstance(phase.name, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", phase.name) is None
            or not isinstance(phase.request_id_sha256, str)
            or _SHA256.fullmatch(phase.request_id_sha256) is None
            or not isinstance(phase.skip_invocations, int)
            or isinstance(phase.skip_invocations, bool)
            or phase.skip_invocations < 0
            or not isinstance(phase.invocation_count, int)
            or isinstance(phase.invocation_count, bool)
            or phase.invocation_count < 1
        ):
            raise ValueError("phase values are invalid")
        if phase.name in phase_names:
            raise ValueError("phase names must be unique")
        phase_names.add(phase.name)
        ranges_by_request[phase.request_id_sha256].append(phase)

    for request_phases in ranges_by_request.values():
        ordered = sorted(request_phases, key=lambda phase: phase.skip_invocations)
        previous_end = -1
        for phase in ordered:
            if phase.skip_invocations < previous_end:
                raise ValueError("phase ranges must not overlap for one request hash")
            previous_end = phase.skip_invocations + phase.invocation_count
    return tuple(phases)


def load_phase_map(path: Path) -> list[PhaseSlice]:
    payload = _load_json_object(path)
    if payload.get("schema_version") != "1.0" or not isinstance(payload.get("phases"), list):
        raise ValueError("phase map must use schema_version 1.0 and a phases list")
    phases: list[PhaseSlice] = []
    for raw in payload["phases"]:
        if not isinstance(raw, dict) or set(raw) != {
            "name",
            "request_id_sha256",
            "skip_invocations",
            "invocation_count",
        }:
            raise ValueError("phase map entries have an invalid field set")
        name = raw["name"]
        request_hash = raw["request_id_sha256"]
        skip = raw["skip_invocations"]
        count = raw["invocation_count"]
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) is None
            or not isinstance(request_hash, str)
            or _SHA256.fullmatch(request_hash) is None
            or not isinstance(skip, int)
            or isinstance(skip, bool)
            or skip < 0
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise ValueError("phase map entry values are invalid")
        phases.append(PhaseSlice(name, request_hash, skip, count))
    return list(_validate_phase_slices(phases))


def _legacy_phase_slices(
    specifications: Iterable[str],
    *,
    request_id_sha256: str,
) -> list[PhaseSlice]:
    phases: list[PhaseSlice] = []
    offset = 0
    for value in specifications:
        name, separator, raw_count = value.partition("=")
        if separator != "=" or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) is None:
            raise ValueError("legacy phases must use NAME=COUNT")
        count = int(raw_count)
        if count < 1:
            raise ValueError("legacy phase counts must be positive")
        phases.append(PhaseSlice(name, request_id_sha256, offset, count))
        offset += count
    return list(_validate_phase_slices(phases))


def analyze(
    *,
    audit_path: Path,
    phases: Sequence[PhaseSlice],
    stage4_baseline_summary: Mapping[str, Any] | None = None,
    baseline_benchmark: Mapping[str, Any] | None = None,
    candidate_benchmark: Mapping[str, Any] | None = None,
    validated_audit: AuditValidationReport | None = None,
) -> dict[str, Any]:
    validated_phases = _validate_phase_slices(phases)
    audit_validation = validated_audit or validate_audit_for_analysis(audit_path)
    invocations = load_audit_invocations(audit_path)
    audit_sha256 = _sha256_file(audit_path)
    audit_size_bytes = audit_path.stat().st_size
    event_count = sum(len(events) for events in invocations.values())
    if (
        audit_validation.audit_file_sha256 != audit_sha256
        or audit_validation.audit_file_size_bytes != audit_size_bytes
        or audit_validation.valid_event_count != event_count
        or audit_validation.invocation_count != len(invocations)
    ):
        raise ValueError("Audit changed after lifecycle/path validation")
    selected_phases = [
        (phase, select_phase_invocations(invocations, phase)) for phase in validated_phases
    ]
    phase_reports = [
        summarize_phase(phase, phase_invocations) for phase, phase_invocations in selected_phases
    ]
    baseline_reference = None
    if stage4_baseline_summary is not None:
        metrics = stage4_baseline_summary.get("metrics", {})
        source = stage4_baseline_summary.get("source", {})
        baseline_reference = {
            "baseline_id": stage4_baseline_summary.get("baseline_id"),
            "base_commit": source.get("base_commit") if isinstance(source, Mapping) else None,
            "c1_p95_ms": metrics.get("c1_p95_ms") if isinstance(metrics, Mapping) else None,
            "c2_p95_ms": metrics.get("c2_p95_ms") if isinstance(metrics, Mapping) else None,
            "recorded_passed": metrics.get("benchmark_passed")
            if isinstance(metrics, Mapping)
            else None,
            "note": (
                "summary has no raw response fingerprints; replay the same benchmark against "
                "a clean Stage 4 image and the candidate image for the exact fingerprint gate"
            ),
        }
    fingerprint_comparison = None
    if baseline_benchmark is not None and candidate_benchmark is not None:
        fingerprint_comparison = compare_benchmark_fingerprints(
            baseline_benchmark,
            candidate_benchmark,
        )
    benchmark_audit_binding = (
        _benchmark_audit_binding(candidate_benchmark, selected_phases)
        if candidate_benchmark is not None
        else None
    )
    coverage = instrumentation_coverage(phase_reports)
    execution_gate = phase_execution_gate(phase_reports)
    return {
        "schema_version": "1.0",
        "suite_id": "deterministic-performance-analysis-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "audit": {
            "sha256": audit_sha256,
            "size_bytes": audit_size_bytes,
            "event_count": event_count,
            "invocation_count": len(invocations),
        },
        "audit_lifecycle_path_validation": _audit_validation_summary(audit_validation),
        "phases": phase_reports,
        "instrumentation_coverage": coverage,
        "phase_execution_gate": execution_gate,
        "stage4_baseline_reference": baseline_reference,
        "response_fingerprint_comparison": fingerprint_comparison,
        "benchmark_audit_binding": benchmark_audit_binding,
    }


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate redacted deterministic API AuditEvent timings by load phase."
    )
    parser.add_argument("--audit-jsonl", type=Path, required=True)
    phase_group = parser.add_mutually_exclusive_group(required=True)
    phase_group.add_argument("--phase-map", type=Path)
    phase_group.add_argument("--phase", action="append", default=[])
    parser.add_argument("--request-id-sha256")
    parser.add_argument("--stage4-baseline-summary", type=Path)
    parser.add_argument("--baseline-benchmark-report", type=Path)
    parser.add_argument("--candidate-benchmark-report", type=Path)
    parser.add_argument("--require-complete-instrumentation", action="store_true")
    parser.add_argument("--require-fingerprint-match", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        validated_audit = validate_audit_for_analysis(arguments.audit_jsonl)
        invocations = load_audit_invocations(arguments.audit_jsonl)
        if arguments.phase_map is not None:
            phases = load_phase_map(arguments.phase_map)
        else:
            request_hash = arguments.request_id_sha256 or most_common_enriched_request_id(
                invocations
            )
            if _SHA256.fullmatch(request_hash) is None:
                raise ValueError("request ID hash must be lowercase SHA-256")
            phases = _legacy_phase_slices(arguments.phase, request_id_sha256=request_hash)
        stage4 = (
            _load_json_object(arguments.stage4_baseline_summary)
            if arguments.stage4_baseline_summary is not None
            else None
        )
        baseline = (
            _load_json_object(arguments.baseline_benchmark_report)
            if arguments.baseline_benchmark_report is not None
            else None
        )
        candidate = (
            _load_json_object(arguments.candidate_benchmark_report)
            if arguments.candidate_benchmark_report is not None
            else None
        )
        if (baseline is None) != (candidate is None):
            raise ValueError("baseline and candidate benchmark reports must be supplied together")
        report = analyze(
            audit_path=arguments.audit_jsonl,
            phases=phases,
            stage4_baseline_summary=stage4,
            baseline_benchmark=baseline,
            candidate_benchmark=candidate,
            validated_audit=validated_audit,
        )
        _write_new(arguments.output, report)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"deterministic performance analysis failed: {error}", file=sys.stderr)
        return 2
    passed = True
    if arguments.require_complete_instrumentation:
        passed = (
            report["instrumentation_coverage"]["complete"]
            and report["phase_execution_gate"]["passed"]
        )
    if arguments.require_fingerprint_match:
        comparison = report["response_fingerprint_comparison"]
        binding = report["benchmark_audit_binding"]
        passed = (
            passed
            and comparison is not None
            and comparison["exact_match"]
            and binding is not None
            and binding["passed"]
        )
    print(
        json.dumps(
            {
                "suite_id": report["suite_id"],
                "output": str(arguments.output),
                "passed": passed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
