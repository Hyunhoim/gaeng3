from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finance_agent_core.audit_validation import (
    AuditValidationInputError,
    AuditValidationPolicy,
    AuditValidationStatus,
    ExpectedAuditReleaseLinkage,
    ExpectedDatasetFingerprint,
    audit_validation_report_bytes,
    load_expected_audit_release_linkage,
    validate_audit_jsonl,
)
from finance_agent_core.audit_validation_cli import run
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.observability import AuditEvent, AuditOutcome, AuditStage, sha256_text
from finance_agent_core.release import (
    DeploymentBinding,
    RollbackRelease,
    RuntimeReleaseInputs,
    build_agent_release_manifest,
    deployment_binding_file_bytes,
    manifest_file_bytes,
)

_OBSERVED_AT = datetime(2026, 8, 14, tzinfo=UTC)
_PLAN_SHA256 = "a" * 64
_DATABASE_MANIFEST_SHA256 = "b" * 64


def _expected_release() -> ExpectedAuditReleaseLinkage:
    return ExpectedAuditReleaseLinkage(
        release_id="local-evaluation-release-v1",
        agent_release_id_sha256=sha256_text("local-evaluation-release-v1"),
        agent_release_manifest_sha256="c" * 64,
        deployment_binding_sha256="d" * 64,
        release_context_sha256="e" * 64,
        dataset_release_id="approved-dataset-release-v1",
        dataset_release_id_sha256=sha256_text("approved-dataset-release-v1"),
        approved_dataset_manifest_sha256="f" * 64,
        datasets={
            family: ExpectedDatasetFingerprint(
                database_snapshot_sha256=(str(index) * 64),
                source_snapshot_sha256=(format(index + 4, "x") * 64),
            )
            for index, family in enumerate(ProductFamily, start=1)
        },
        binding_trust_anchor_verified=True,
    )


def _event(
    expected: ExpectedAuditReleaseLinkage,
    *,
    invocation_id: str,
    sequence: int,
    stage: AuditStage,
    outcome: AuditOutcome,
    reason_code: str,
    request_started: bool = False,
    route: bool = False,
    plan: bool = False,
    dataset: bool = False,
    answer_families: tuple[ProductFamily, ...] | None = None,
) -> AuditEvent:
    family = ProductFamily.BOND
    families = answer_families if answer_families is not None else ((family,) if route else ())
    fingerprint = expected.datasets[family]
    return AuditEvent.redacted(
        stage=stage,
        outcome=outcome,
        reason_code=reason_code,
        duration_ms=float(sequence),
        request_id="" if request_started else f"request-{invocation_id}",
        question="" if request_started else "synthetic validation question",
        invocation_id=invocation_id,
        event_sequence=sequence,
        observed_at_utc=_OBSERVED_AT,
        route_disposition=RouteDisposition.EXECUTE if route else None,
        interaction_intent=InteractionIntent.SEARCH if route else None,
        product_families=families,
        agent_release_id=expected.release_id,
        agent_release_manifest_sha256=expected.agent_release_manifest_sha256,
        deployment_binding_sha256=expected.deployment_binding_sha256,
        release_context_sha256=expected.release_context_sha256,
        dataset_release_id=expected.dataset_release_id if dataset else None,
        approved_dataset_manifest_sha256=(
            expected.approved_dataset_manifest_sha256 if dataset else None
        ),
        database_manifest_sha256=_DATABASE_MANIFEST_SHA256 if dataset else None,
        database_snapshot_sha256=(fingerprint.database_snapshot_sha256 if dataset else None),
        source_snapshot_sha256=(fingerprint.source_snapshot_sha256 if dataset else None),
        plan_sha256=_PLAN_SHA256 if plan else None,
    )


def _valid_trace(
    expected: ExpectedAuditReleaseLinkage,
    *,
    invocation_id: str = "validation-invocation-1",
    fallback: bool = False,
) -> list[AuditEvent]:
    specifications = (
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received", True, False, False, False),
        (AuditStage.ROUTE, AuditOutcome.SUCCEEDED, "routed_execute", False, True, False, False),
        (AuditStage.COMPILER, AuditOutcome.SUCCEEDED, "plan_compiled", False, True, True, False),
        # Repeated allowlisted stages model finer-grained instrumentation.
        (AuditStage.COMPILER, AuditOutcome.SUCCEEDED, "plan_compiled", False, True, True, False),
        (
            AuditStage.AUTHORITY,
            AuditOutcome.SUCCEEDED,
            "authority_granted",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.SQL,
            AuditOutcome.SUCCEEDED,
            "parameterized_statement_completed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.SQL,
            AuditOutcome.SUCCEEDED,
            "parameterized_statement_completed",
            False,
            True,
            True,
            True,
        ),
        (AuditStage.ORACLE, AuditOutcome.SUCCEEDED, "oracle_completed", False, True, True, True),
        (AuditStage.ORACLE, AuditOutcome.SUCCEEDED, "oracle_completed", False, True, True, True),
        (
            AuditStage.VERIFIER,
            AuditOutcome.SUCCEEDED,
            "verification_passed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.VERIFIER,
            AuditOutcome.SUCCEEDED,
            "verification_passed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.RENDERER,
            AuditOutcome.SUCCEEDED,
            "rendering_completed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.RENDERER,
            AuditOutcome.SUCCEEDED,
            "rendering_completed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.ANSWER,
            AuditOutcome.SUCCEEDED,
            "execution_fallback" if fallback else "execution_completed",
            False,
            True,
            True,
            True,
        ),
        (
            AuditStage.REQUEST,
            AuditOutcome.SUCCEEDED,
            "response_completed",
            False,
            False,
            False,
            False,
        ),
    )
    return [
        _event(
            expected,
            invocation_id=invocation_id,
            sequence=sequence,
            stage=stage,
            outcome=outcome,
            reason_code=reason,
            request_started=request_started,
            route=route,
            plan=plan,
            dataset=dataset,
        )
        for sequence, (
            stage,
            outcome,
            reason,
            request_started,
            route,
            plan,
            dataset,
        ) in enumerate(specifications, start=1)
    ]


def _write_events(path: Path, events: list[AuditEvent]) -> None:
    payload = b"".join(
        (
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        for event in events
    )
    path.write_bytes(payload)
    path.chmod(0o600)


def _resequence(events: list[AuditEvent]) -> list[AuditEvent]:
    return [
        event.model_copy(update={"event_sequence": sequence})
        for sequence, event in enumerate(events, start=1)
    ]


def test_validates_repeated_stages_and_complete_release_dataset_path(tmp_path: Path) -> None:
    expected = _expected_release()
    audit_path = tmp_path / "events.jsonl"
    events = _valid_trace(expected)
    _write_events(audit_path, events)

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.PASSED
    assert report.issue_count == 0
    assert report.record_count == len(events)
    assert report.lifecycle_complete_invocation_count == 1
    assert report.executable_success_invocation_count == 1
    assert report.execution_path_complete_invocation_count == 1
    assert report.release_linked_event_count == len(events)
    assert report.dataset_linked_event_count == 10
    assert report.database_fingerprint_linked_event_count == 10


@pytest.mark.parametrize(
    ("case", "expected_issue"),
    [
        ("answer_dataset_missing", "answer_dataset_linkage_missing"),
        ("sql_missing", "execution_sql_missing"),
        ("renderer_missing", "execution_renderer_missing"),
        ("renderer_out_of_order", "execution_stage_order_invalid"),
    ],
)
def test_success_path_requires_answer_dataset_sql_and_ordered_renderer(
    tmp_path: Path,
    case: str,
    expected_issue: str,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    if case == "answer_dataset_missing":
        answer_index = next(
            index for index, event in enumerate(events) if event.stage is AuditStage.ANSWER
        )
        events[answer_index] = events[answer_index].model_copy(
            update={
                "dataset_release_id_sha256": None,
                "approved_dataset_manifest_sha256": None,
                "database_manifest_sha256": None,
                "database_snapshot_sha256": None,
                "source_snapshot_sha256": None,
            }
        )
    elif case == "sql_missing":
        events = [event for event in events if event.stage is not AuditStage.SQL]
    elif case == "renderer_missing":
        events = [event for event in events if event.stage is not AuditStage.RENDERER]
    else:
        renderers = [event for event in events if event.stage is AuditStage.RENDERER]
        events = [event for event in events if event.stage is not AuditStage.RENDERER]
        verifier_index = next(
            index for index, event in enumerate(events) if event.stage is AuditStage.VERIFIER
        )
        events[verifier_index:verifier_index] = renderers
    audit_path = tmp_path / f"{case}.jsonl"
    _write_events(audit_path, _resequence(events))

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts[expected_issue] >= 1
    assert report.execution_path_complete_invocation_count == 0


@pytest.mark.parametrize(
    ("stage", "replacement_reason", "expected_issue"),
    [
        (AuditStage.ROUTE, "routed_clarify", "execution_route_missing"),
        (AuditStage.COMPILER, "plan_blocked", "execution_queryplan_missing"),
        (AuditStage.AUTHORITY, "authority_connection_opened", "execution_authority_missing"),
        (AuditStage.SQL, "oracle_connection_opened", "execution_sql_missing"),
        (AuditStage.ORACLE, "oracle_connection_opened", "execution_oracle_missing"),
        (AuditStage.VERIFIER, "verifier_universe_loaded", "execution_verifier_missing"),
        (AuditStage.RENDERER, "backend_dto_built", "execution_renderer_missing"),
    ],
)
def test_success_path_requires_canonical_completion_reason_codes(
    tmp_path: Path,
    stage: AuditStage,
    replacement_reason: str,
    expected_issue: str,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    events = [
        event.model_copy(update={"reason_code": replacement_reason})
        if event.stage is stage
        else event
        for event in events
    ]
    audit_path = tmp_path / f"noncanonical-{stage.value}.jsonl"
    _write_events(audit_path, events)

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts[expected_issue] >= 1
    assert report.execution_path_complete_invocation_count == 0


def test_reports_sequence_schema_and_sensitive_key_failures_without_raw_values(
    tmp_path: Path,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    payloads = [event.model_dump(mode="json") for event in events]
    payloads[4]["prompt"] = "synthetic-sensitive-value-never-rendered"
    del payloads[7]
    audit_path = tmp_path / "invalid-events.jsonl"
    audit_path.write_bytes(
        b"".join(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
            for payload in payloads
        )
    )
    audit_path.chmod(0o600)

    report = validate_audit_jsonl(audit_path, expected_release=expected)
    rendered = audit_validation_report_bytes(report)

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts["prompt_material_exposed"] == 1
    assert report.issue_counts["schema_violation"] == 1
    assert report.issue_counts["event_sequence_gap"] == 1
    assert report.issue_counts["execution_authority_missing"] == 1
    assert b"synthetic-sensitive-value-never-rendered" not in rendered


@pytest.mark.parametrize(
    ("case", "expected_issue"),
    [
        ("duplicate_key", "duplicate_json_key"),
        ("incomplete_record", "incomplete_final_record"),
        ("oversized_record", "record_too_large"),
        ("credential_key", "credential_material_exposed"),
        ("response_key", "response_material_exposed"),
    ],
)
def test_rejects_corrupt_or_sensitive_jsonl_records(
    tmp_path: Path,
    case: str,
    expected_issue: str,
) -> None:
    expected = _expected_release()
    event = _valid_trace(expected)[0]
    payload = event.model_dump(mode="json")
    base = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if case == "duplicate_key":
        record = (
            base.replace(
                b'"schema_version":"1.2"',
                b'"schema_version":"1.2","schema_version":"1.2"',
                1,
            )
            + b"\n"
        )
    elif case == "incomplete_record":
        record = base
    elif case == "oversized_record":
        record = b'{"padding":"' + (b"x" * (70 * 1024)) + b'"}\n'
    else:
        payload["credential" if case == "credential_key" else "response"] = (
            "synthetic-material-never-rendered"
        )
        record = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    audit_path = tmp_path / f"{case}.jsonl"
    audit_path.write_bytes(record)
    audit_path.chmod(0o600)

    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(
            require_request_lifecycle=False,
            require_execution_path=False,
        ),
    )

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts[expected_issue] >= 1
    assert b"synthetic-material-never-rendered" not in audit_validation_report_bytes(report)


@pytest.mark.parametrize(
    ("case", "expected_issue"),
    [
        ("duplicate_sequence", "event_sequence_duplicate"),
        ("sequence_gap", "event_sequence_gap"),
        ("terminal_missing", "request_terminal_missing"),
    ],
)
def test_rejects_broken_sequence_or_request_lifecycle(
    tmp_path: Path,
    case: str,
    expected_issue: str,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    if case == "duplicate_sequence":
        events[6] = events[6].model_copy(update={"event_sequence": events[5].event_sequence})
    elif case == "sequence_gap":
        events = [
            event.model_copy(
                update={
                    "event_sequence": (
                        event.event_sequence + 1
                        if event.event_sequence is not None and event.event_sequence >= 6
                        else event.event_sequence
                    )
                }
            )
            for event in events
        ]
    else:
        events = events[:-1]
    audit_path = tmp_path / f"{case}.jsonl"
    _write_events(audit_path, events)

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts[expected_issue] >= 1


def test_aggregates_timeout_overload_and_fallback_per_invocation(tmp_path: Path) -> None:
    expected = _expected_release()
    timeout = [
        _event(
            expected,
            invocation_id="timeout-invocation",
            sequence=1,
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.STARTED,
            reason_code="received",
            request_started=True,
        ),
        _event(
            expected,
            invocation_id="timeout-invocation",
            sequence=2,
            stage=AuditStage.ANSWER,
            outcome=AuditOutcome.TIMED_OUT,
            reason_code="deadline_exceeded",
            route=True,
        ),
        _event(
            expected,
            invocation_id="timeout-invocation",
            sequence=3,
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.TIMED_OUT,
            reason_code="deadline_exceeded",
        ),
    ]
    overload = [
        _event(
            expected,
            invocation_id="overload-invocation",
            sequence=1,
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.STARTED,
            reason_code="received",
            request_started=True,
        ),
        _event(
            expected,
            invocation_id="overload-invocation",
            sequence=2,
            stage=AuditStage.REQUEST,
            outcome=AuditOutcome.BLOCKED,
            reason_code="admission_rejected",
        ),
    ]
    events = [*_valid_trace(expected, invocation_id="fallback-invocation", fallback=True)]
    events.extend(timeout)
    events.extend(overload)
    audit_path = tmp_path / "incidents.jsonl"
    _write_events(audit_path, events)

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.PASSED
    assert report.incident_counts.timed_out_invocations == 1
    assert report.incident_counts.overload_invocations == 1
    assert report.incident_counts.fallback_invocations == 1
    assert report.incident_counts.blocked_invocations == 1


def test_require_release_linkage_fails_unlinked_but_schema_valid_trace(tmp_path: Path) -> None:
    expected = _expected_release()
    event = AuditEvent.redacted(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.STARTED,
        reason_code="received",
        duration_ms=0,
        request_id="",
        question="",
        invocation_id="unlinked-invocation",
        event_sequence=1,
        observed_at_utc=_OBSERVED_AT,
    )
    terminal = AuditEvent.redacted(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.SUCCEEDED,
        reason_code="response_completed",
        duration_ms=1,
        request_id="request-unlinked",
        question="synthetic validation question",
        invocation_id="unlinked-invocation",
        event_sequence=2,
        observed_at_utc=_OBSERVED_AT,
    )
    audit_path = tmp_path / "unlinked.jsonl"
    _write_events(audit_path, [event, terminal])

    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(require_release_linkage=True),
    )

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts["release_linkage_missing"] == 2
    assert report.release_linked_event_count == 0
    assert expected.agent_release_manifest_sha256 not in report.model_dump_json()


def test_development_trace_can_validate_without_dataset_linkage_policy(tmp_path: Path) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    unlinked_events = [
        event.model_copy(
            update={
                "agent_release_id_sha256": None,
                "agent_release_manifest_sha256": None,
                "deployment_binding_sha256": None,
                "release_context_sha256": None,
                "dataset_release_id_sha256": None,
                "approved_dataset_manifest_sha256": None,
                "database_manifest_sha256": None,
                "database_snapshot_sha256": None,
                "source_snapshot_sha256": None,
            }
        )
        for event in events
    ]
    audit_path = tmp_path / "development-events.jsonl"
    _write_events(audit_path, unlinked_events)

    report = validate_audit_jsonl(audit_path)

    assert report.status is AuditValidationStatus.PASSED
    assert report.execution_path_complete_invocation_count == 1
    assert report.release_linked_event_count == 0
    assert report.dataset_linked_event_count == 0


def test_dataset_linkage_policy_rejects_unlinked_development_answer(tmp_path: Path) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    unlinked_events = [
        event.model_copy(
            update={
                "agent_release_id_sha256": None,
                "agent_release_manifest_sha256": None,
                "deployment_binding_sha256": None,
                "release_context_sha256": None,
                "dataset_release_id_sha256": None,
                "approved_dataset_manifest_sha256": None,
                "database_manifest_sha256": None,
                "database_snapshot_sha256": None,
                "source_snapshot_sha256": None,
            }
        )
        for event in events
    ]
    audit_path = tmp_path / "required-dataset-events.jsonl"
    _write_events(audit_path, unlinked_events)

    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(require_dataset_linkage=True),
    )

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts["answer_dataset_linkage_missing"] == 1
    assert report.execution_path_complete_invocation_count == 0


def test_dataset_linkage_policy_rejects_inconsistent_family_fingerprint(
    tmp_path: Path,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    sql_index = next(
        index
        for index, event in enumerate(events)
        if event.stage is AuditStage.SQL and event.database_snapshot_sha256 is not None
    )
    events[sql_index] = events[sql_index].model_copy(update={"source_snapshot_sha256": "9" * 64})
    audit_path = tmp_path / "inconsistent-dataset-events.jsonl"
    _write_events(audit_path, events)

    report = validate_audit_jsonl(
        audit_path,
        policy=AuditValidationPolicy(require_dataset_linkage=True),
    )

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts["dataset_linkage_inconsistent"] == 1


def test_allows_only_correlated_schema_shadow_events_after_request_terminal(
    tmp_path: Path,
) -> None:
    expected = _expected_release()
    events = _valid_trace(expected)
    terminal = events.pop()
    late_shadow = _event(
        expected,
        invocation_id="validation-invocation-1",
        sequence=terminal.event_sequence or 1,
        stage=AuditStage.SCHEMA_LINK_SHADOW,
        outcome=AuditOutcome.SUCCEEDED,
        reason_code="shadow_candidate_observed",
        route=True,
    )
    events.extend((terminal, late_shadow))
    audit_path = tmp_path / "late-shadow.jsonl"
    _write_events(audit_path, _resequence(events))

    report = validate_audit_jsonl(audit_path, expected_release=expected)

    assert report.status is AuditValidationStatus.PASSED
    assert report.lifecycle_complete_invocation_count == 1


def _control_trace_with_boundary_event(
    expected: ExpectedAuditReleaseLinkage,
    *,
    stage: AuditStage,
    outcome: AuditOutcome,
    reason_code: str,
) -> list[AuditEvent]:
    invocation_id = "control-boundary-invocation"
    specifications = (
        (AuditStage.REQUEST, AuditOutcome.STARTED, "received", True, False, False),
        (AuditStage.ROUTE, AuditOutcome.SUCCEEDED, "routed_execute", False, True, False),
        (AuditStage.COMPILER, AuditOutcome.SUCCEEDED, "plan_compiled", False, True, True),
        (stage, outcome, reason_code, False, True, True),
        (AuditStage.ANSWER, AuditOutcome.CLARIFIED, "execution_clarified", False, True, True),
        (AuditStage.REQUEST, AuditOutcome.SUCCEEDED, "response_completed", False, False, False),
    )
    return [
        _event(
            expected,
            invocation_id=invocation_id,
            sequence=sequence,
            stage=event_stage,
            outcome=event_outcome,
            reason_code=event_reason,
            request_started=request_started,
            route=route,
            plan=plan,
        )
        for sequence, (
            event_stage,
            event_outcome,
            event_reason,
            request_started,
            route,
            plan,
        ) in enumerate(specifications, start=1)
    ]


def test_control_answer_allows_blocked_authority_denial_before_database_access(
    tmp_path: Path,
) -> None:
    expected = _expected_release()
    audit_path = tmp_path / "control-authority-denied.jsonl"
    _write_events(
        audit_path,
        _control_trace_with_boundary_event(
            expected,
            stage=AuditStage.AUTHORITY,
            outcome=AuditOutcome.BLOCKED,
            reason_code="authority_denied",
        ),
    )

    report = validate_audit_jsonl(audit_path)

    assert report.status is AuditValidationStatus.PASSED
    assert report.issue_counts.get("control_path_executed", 0) == 0


@pytest.mark.parametrize(
    ("stage", "outcome", "reason_code"),
    (
        (AuditStage.AUTHORITY, AuditOutcome.SUCCEEDED, "authority_granted"),
        (AuditStage.AUTHORITY, AuditOutcome.FAILED, "authority_denied"),
        (AuditStage.AUTHORITY, AuditOutcome.TIMED_OUT, "deadline_exceeded"),
        (AuditStage.EXECUTION, AuditOutcome.STARTED, "execution_started"),
        (AuditStage.SQL, AuditOutcome.SUCCEEDED, "parameterized_statement_completed"),
        (AuditStage.ORACLE, AuditOutcome.FAILED, "oracle_failed"),
        (AuditStage.VERIFIER, AuditOutcome.SUCCEEDED, "verification_passed"),
        (AuditStage.RENDERER, AuditOutcome.SUCCEEDED, "rendering_completed"),
    ),
)
def test_control_answer_rejects_authority_or_database_execution_attempt(
    tmp_path: Path,
    stage: AuditStage,
    outcome: AuditOutcome,
    reason_code: str,
) -> None:
    expected = _expected_release()
    audit_path = tmp_path / f"control-executed-{stage.value}-{outcome.value}.jsonl"
    _write_events(
        audit_path,
        _control_trace_with_boundary_event(
            expected,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
        ),
    )

    report = validate_audit_jsonl(audit_path)

    assert report.status is AuditValidationStatus.FAILED
    assert report.issue_counts["control_path_executed"] == 1


def _write_release_artifacts(tmp_path: Path) -> tuple[Path, Path, str]:
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (backend_root / "runtime.py").write_text("VERSION = 'test'\n", encoding="utf-8")
    source_commit = "1" * 40
    image_reference = "registry.example/finance-agent@sha256:" + "2" * 64
    inputs = RuntimeReleaseInputs(
        environment="evaluation",
        source_commit=source_commit,
        image_reference=image_reference,
        backend_version="0.1.0",
        backend_root=backend_root,
        answer_provider="deterministic",
        hcx_queryplan_enabled=False,
        hcx_model=None,
        fund_execution_policy="locked",
    )
    manifest = build_agent_release_manifest(
        inputs,
        release_id="local-audit-evaluation-v1",
        generated_at_utc=_OBSERVED_AT,
    )
    manifest_data = manifest_file_bytes(manifest)
    binding = DeploymentBinding(
        release_id=manifest.release_id,
        environment=manifest.environment,
        source_commit=manifest.source_commit,
        release_manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        image_reference=image_reference,
        platform="linux/amd64",
        activation_generation=1,
        rollback=RollbackRelease(mode="initial_bootstrap"),
    )
    binding_data = deployment_binding_file_bytes(binding)
    manifest_path = tmp_path / "agent-release-manifest.json"
    binding_path = tmp_path / "deployment-binding.json"
    manifest_path.write_bytes(manifest_data)
    binding_path.write_bytes(binding_data)
    return manifest_path, binding_path, hashlib.sha256(binding_data).hexdigest()


def test_cli_binds_report_to_audit_and_deterministic_local_release(tmp_path: Path) -> None:
    manifest_path, binding_path, binding_sha256 = _write_release_artifacts(tmp_path)
    expected = load_expected_audit_release_linkage(
        manifest_path=manifest_path,
        binding_path=binding_path,
        expected_binding_sha256=binding_sha256,
    )
    audit_path = tmp_path / "events.jsonl"
    _write_events(audit_path, _valid_trace(expected))
    report_path = tmp_path / "report.json"
    commitment_path = tmp_path / "report.commitment.json"

    exit_code = run(
        [
            "--audit",
            str(audit_path),
            "--report",
            str(report_path),
            "--commitment",
            str(commitment_path),
            "--release-manifest",
            str(manifest_path),
            "--deployment-binding",
            str(binding_path),
            "--expected-binding-sha256",
            binding_sha256,
        ]
    )

    assert exit_code == 0
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
    assert report_payload["status"] == "passed"
    assert report_payload["binding_trust_anchor_verified"] is True
    assert commitment == {
        "schema_version": "1.0",
        "audit_file_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "report_file_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }


def test_release_linkage_rejects_binding_trust_anchor_mismatch(tmp_path: Path) -> None:
    manifest_path, binding_path, _ = _write_release_artifacts(tmp_path)

    with pytest.raises(AuditValidationInputError) as raised:
        load_expected_audit_release_linkage(
            manifest_path=manifest_path,
            binding_path=binding_path,
            expected_binding_sha256="0" * 64,
        )

    assert raised.value.code == "binding_trust_anchor_mismatch"


def test_cli_dataset_linkage_requires_trusted_release_inputs(tmp_path: Path) -> None:
    expected = _expected_release()
    audit_path = tmp_path / "events.jsonl"
    _write_events(audit_path, _valid_trace(expected))
    report_path = tmp_path / "report.json"
    commitment_path = tmp_path / "report.commitment.json"

    exit_code = run(
        [
            "--audit",
            str(audit_path),
            "--report",
            str(report_path),
            "--commitment",
            str(commitment_path),
            "--require-dataset-linkage",
        ]
    )

    assert exit_code == 2
    assert not report_path.exists()
    assert not commitment_path.exists()
