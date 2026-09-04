from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from finance_agent_core.observability import AuditEvent, AuditOutcome, AuditStage, sha256_text

from scripts.deterministic_performance_analysis import (
    PhaseSlice,
    analyze,
    compare_benchmark_fingerprints,
    load_audit_invocations,
    load_phase_map,
    main,
    most_common_enriched_request_id,
)


def _write_invocation(
    path: Path,
    *,
    invocation_id: str,
    request_id: str,
    verifier_ms: float,
    include_verifier_universe: bool = True,
) -> None:
    events: list[AuditEvent] = []

    def append(
        stage: AuditStage,
        reason: str,
        duration_ms: float,
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
    ) -> None:
        events.append(
            AuditEvent.redacted(
                stage=stage,
                outcome=outcome,
                reason_code=reason,
                duration_ms=duration_ms,
                request_id="" if reason == "received" else request_id,
                question="" if reason == "received" else "고정 성능 질문",
                invocation_id=invocation_id,
                event_sequence=len(events) + 1,
            )
        )

    append(AuditStage.REQUEST, "received", 0, outcome=AuditOutcome.STARTED)
    append(AuditStage.ROUTE, "routed_execute", 1)
    append(AuditStage.COMPILER, "plan_compiled", 2)
    append(AuditStage.AUTHORITY, "authority_granted", 3)
    append(AuditStage.SQL, "authority_connection_opened", 0.5)
    append(AuditStage.SQL, "oracle_connection_opened", 0.6)
    append(AuditStage.SQL, "oracle_statements_completed", 0.7)
    append(AuditStage.SQL, "parameterized_statement_completed", 2)
    append(AuditStage.ORACLE, "oracle_completed", 3)
    append(AuditStage.SQL, "verifier_projection_connection_opened", 0.8)
    append(AuditStage.SQL, "verifier_projection_fetched", verifier_ms * 0.2)
    append(AuditStage.VERIFIER, "verifier_rows_materialized", verifier_ms * 0.7)
    if include_verifier_universe:
        append(AuditStage.VERIFIER, "verifier_universe_loaded", verifier_ms * 0.9)
    append(AuditStage.VERIFIER, "pure_verification_passed", verifier_ms * 0.1)
    append(AuditStage.VERIFIER, "verification_passed", verifier_ms)
    append(AuditStage.RENDERER, "rendering_completed", 4)
    append(AuditStage.ANSWER, "execution_completed", verifier_ms + 20)
    append(AuditStage.SERIALIZATION, "citations_built", 0.2)
    append(AuditStage.SERIALIZATION, "backend_dto_built", 0.3)
    append(AuditStage.SERIALIZATION, "official_dto_built", 0.4)
    append(AuditStage.SERIALIZATION, "http_response_serialized", 0.5)
    append(AuditStage.REQUEST, "response_completed", verifier_ms + 25)

    with path.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(event.model_dump_json() + "\n")
    path.chmod(0o600)


def _benchmark_report(
    *,
    question_id: str = "performance-request",
    question: str = "고정 성능 질문",
    second_case_hash: str = "b" * 64,
) -> dict[str, object]:
    phase_specs = (
        ("warm_c1", "warm", 1, 1, 1, []),
        ("warm_c2", "warm", 2, 1, 2, []),
        ("warm_c4", "warm", 4, 1, 4, []),
        (
            "admission_c8",
            "admission_stress",
            8,
            1,
            8,
            ["request_overloaded", "request_timeout"],
        ),
    )
    phases = []
    for index, (name, kind, concurrency, requested, actual, controls) in enumerate(
        phase_specs,
        start=1,
    ):
        phases.append(
            {
                "name": name,
                "kind": kind,
                "concurrency": concurrency,
                "requested_count": requested,
                "actual_count": actual,
                "accepted_control_codes": controls,
                "passed": True,
                "summary": {
                    "requests": actual,
                    "completed_http": actual,
                    "passed": actual,
                    "failed": 0,
                    "deterministic": True,
                    "transport_error_counts": {},
                    "violation_counts": {},
                    "control_code_counts": {},
                    "outcomes": {
                        "status:success": {
                            "count": actual,
                            "canonical_response_sha256": [f"{index:x}" * 64],
                            "deterministic": True,
                        }
                    },
                },
            }
        )
    return {
        "schema_version": "1.0",
        "suite_id": "deterministic-api-baseline-v1",
        "passed": True,
        "target": {
            "question_sha256": sha256_text(question),
            "question_id_sha256": sha256_text(question_id),
            "expected_status": "success",
            "expected_intent": "search",
            "expected_product_families": ["bond"],
            "expected_answer_mode": "deterministic",
            "expected_fund_execution_policy": "locked",
            "llm_expected": False,
            "dense_expected": False,
        },
        "semantic_contract": {
            "passed": True,
            "cases": [
                {"case_id": "case-1", "passed": True, "response_sha256": "a" * 64},
                {
                    "case_id": "case-2",
                    "passed": True,
                    "response_sha256": second_case_hash,
                },
            ],
        },
        "phases": phases,
    }


def test_analysis_slices_repeated_request_id_and_identifies_verifier_bottleneck(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "events.jsonl"
    for index, verifier_ms in enumerate((100.0, 120.0, 300.0, 360.0), start=1):
        _write_invocation(
            audit_path,
            invocation_id=f"invocation-{index}",
            request_id="performance-request",
            verifier_ms=verifier_ms,
        )

    invocations = load_audit_invocations(audit_path)
    request_hash = sha256_text("performance-request")
    assert most_common_enriched_request_id(invocations) == request_hash
    report = analyze(
        audit_path=audit_path,
        phases=(
            PhaseSlice("warm_c1", request_hash, 0, 2),
            PhaseSlice("warm_c2", request_hash, 2, 2),
        ),
    )

    c1, c2 = report["phases"]
    assert c1["segments_ms"]["verifier_total"]["p95"] == 119.0
    assert c2["segments_ms"]["verifier_total"]["p95"] == 357.0
    assert c2["dominant_non_overlapping_segment_by_p95"] == "verifier_total"
    assert c2["terminal_error_or_timeout_count"] == 0
    assert report["instrumentation_coverage"]["complete"] is True
    assert report["instrumentation_coverage"]["phases"] == {
        "warm_c1": {
            "invocation_count": 2,
            "missing_segments": [],
            "partial_segments": [],
            "complete": True,
        },
        "warm_c2": {
            "invocation_count": 2,
            "missing_segments": [],
            "partial_segments": [],
            "complete": True,
        },
    }
    assert report["phase_execution_gate"]["passed"] is True
    assert report["audit_lifecycle_path_validation"]["status"] == "passed"
    assert report["audit"]["invocation_count"] == 4


def test_analysis_rejects_audit_without_terminal_lifecycle_event(tmp_path: Path) -> None:
    audit_path = tmp_path / "events.jsonl"
    _write_invocation(
        audit_path,
        invocation_id="invocation-1",
        request_id="performance-request",
        verifier_ms=100,
    )
    lines = audit_path.read_text(encoding="utf-8").splitlines(keepends=True)
    audit_path.write_text("".join(lines[:-1]), encoding="utf-8")

    with pytest.raises(ValueError, match="lifecycle/path validation failed"):
        analyze(
            audit_path=audit_path,
            phases=(
                PhaseSlice(
                    "warm_c1",
                    sha256_text("performance-request"),
                    0,
                    1,
                ),
            ),
        )


def test_instrumentation_coverage_requires_verifier_universe_total(tmp_path: Path) -> None:
    audit_path = tmp_path / "events.jsonl"
    _write_invocation(
        audit_path,
        invocation_id="invocation-1",
        request_id="performance-request",
        verifier_ms=100,
        include_verifier_universe=False,
    )

    report = analyze(
        audit_path=audit_path,
        phases=(
            PhaseSlice(
                "warm_c1",
                sha256_text("performance-request"),
                0,
                1,
            ),
        ),
    )

    coverage = report["instrumentation_coverage"]
    assert coverage["complete"] is False
    assert coverage["missing_segments"] == ["verifier_universe_total"]
    assert coverage["phases"]["warm_c1"]["missing_segments"] == ["verifier_universe_total"]


def test_benchmark_fingerprint_comparison_requires_same_cases_and_hashes() -> None:
    baseline = _benchmark_report()
    candidate = _benchmark_report(second_case_hash="d" * 64)

    same = compare_benchmark_fingerprints(baseline, baseline)
    changed = compare_benchmark_fingerprints(baseline, candidate)

    assert same["exact_match"] is True
    assert same["comparison_contract_match"] is True
    assert changed["exact_match"] is False
    assert changed["changed_keys"] == ["semantic_contract:case-2"]


@pytest.mark.parametrize("missing", ["semantic_cases", "phase", "outcomes"])
def test_benchmark_fingerprint_comparison_fails_closed_on_missing_inputs(
    missing: str,
) -> None:
    baseline = _benchmark_report()
    candidate = copy.deepcopy(baseline)
    if missing == "semantic_cases":
        candidate["semantic_contract"]["cases"] = []
    elif missing == "phase":
        candidate["phases"].pop()
    else:
        candidate["phases"][0]["summary"].pop("outcomes")

    comparison = compare_benchmark_fingerprints(baseline, candidate)

    assert comparison["exact_match"] is False
    assert comparison["comparison_contract_complete"] is False
    if missing != "phase":
        assert comparison["fingerprint_sets_valid"] is False


def test_analysis_binds_benchmark_question_and_warm_phase_slices(tmp_path: Path) -> None:
    audit_path = tmp_path / "events.jsonl"
    for index in range(7):
        _write_invocation(
            audit_path,
            invocation_id=f"invocation-{index}",
            request_id="performance-request",
            verifier_ms=100,
        )
    request_hash = sha256_text("performance-request")
    phases = (
        PhaseSlice("warm_c1", request_hash, 0, 1),
        PhaseSlice("warm_c2", request_hash, 1, 2),
        PhaseSlice("warm_c4", request_hash, 3, 4),
    )
    benchmark = _benchmark_report()

    report = analyze(
        audit_path=audit_path,
        phases=phases,
        baseline_benchmark=benchmark,
        candidate_benchmark=benchmark,
    )

    assert report["response_fingerprint_comparison"]["exact_match"] is True
    assert report["benchmark_audit_binding"] == {
        "passed": True,
        "selected_phase_count": 3,
        "matched_phase_count": 3,
        "mismatch_codes": [],
    }


def test_analysis_rejects_unbound_benchmark_question_even_when_reports_match(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "events.jsonl"
    for index in range(7):
        _write_invocation(
            audit_path,
            invocation_id=f"invocation-{index}",
            request_id="performance-request",
            verifier_ms=100,
        )
    request_hash = sha256_text("performance-request")
    benchmark = _benchmark_report(question="다른 질문")

    report = analyze(
        audit_path=audit_path,
        phases=(
            PhaseSlice("warm_c1", request_hash, 0, 1),
            PhaseSlice("warm_c2", request_hash, 1, 2),
            PhaseSlice("warm_c4", request_hash, 3, 4),
        ),
        baseline_benchmark=benchmark,
        candidate_benchmark=benchmark,
    )

    assert report["response_fingerprint_comparison"]["exact_match"] is True
    assert report["benchmark_audit_binding"]["passed"] is False
    assert report["benchmark_audit_binding"]["mismatch_codes"] == ["audit_question_mismatch"]


def test_analysis_rejects_phase_count_that_does_not_bind_to_benchmark(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "events.jsonl"
    for index in range(6):
        _write_invocation(
            audit_path,
            invocation_id=f"invocation-{index}",
            request_id="performance-request",
            verifier_ms=100,
        )
    request_hash = sha256_text("performance-request")
    benchmark = _benchmark_report()

    report = analyze(
        audit_path=audit_path,
        phases=(
            PhaseSlice("warm_c1", request_hash, 0, 1),
            PhaseSlice("warm_c2", request_hash, 1, 2),
            PhaseSlice("warm_c4", request_hash, 3, 3),
        ),
        baseline_benchmark=benchmark,
        candidate_benchmark=benchmark,
    )

    assert report["benchmark_audit_binding"]["passed"] is False
    assert report["benchmark_audit_binding"]["mismatch_codes"] == [
        "phase_invocation_count_mismatch"
    ]


def test_fingerprint_cli_gate_requires_benchmark_audit_binding(tmp_path: Path) -> None:
    audit_path = tmp_path / "events.jsonl"
    for index in range(7):
        _write_invocation(
            audit_path,
            invocation_id=f"invocation-{index}",
            request_id="performance-request",
            verifier_ms=100,
        )
    benchmark = _benchmark_report(question="다른 질문")
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(benchmark), encoding="utf-8")
    candidate_path.write_text(json.dumps(benchmark), encoding="utf-8")
    output_path = tmp_path / "analysis.json"

    exit_code = main(
        [
            "--audit-jsonl",
            str(audit_path),
            "--phase",
            "warm_c1=1",
            "--phase",
            "warm_c2=2",
            "--phase",
            "warm_c4=4",
            "--baseline-benchmark-report",
            str(baseline_path),
            "--candidate-benchmark-report",
            str(candidate_path),
            "--require-fingerprint-match",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["response_fingerprint_comparison"]["exact_match"] is True
    assert output["benchmark_audit_binding"]["passed"] is False


def test_phase_map_rejects_overlapping_ranges_for_same_request_hash(
    tmp_path: Path,
) -> None:
    phase_map_path = tmp_path / "phase-map.json"
    phase_map_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "phases": [
                    {
                        "name": "warm_c1",
                        "request_id_sha256": "a" * 64,
                        "skip_invocations": 0,
                        "invocation_count": 2,
                    },
                    {
                        "name": "warm_c2",
                        "request_id_sha256": "a" * 64,
                        "skip_invocations": 1,
                        "invocation_count": 2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not overlap"):
        load_phase_map(phase_map_path)
