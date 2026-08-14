from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    assert_safe_audit_payload,
    sha256_text,
)
from finance_agent_core.release import (
    AgentReleaseManifest,
    DeploymentBinding,
    canonical_sha256,
    deployment_binding_file_bytes,
    manifest_file_bytes,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_AUDIT_RECORD_BYTES = 64 * 1024
_MAX_AUDIT_FILE_BYTES = 512 * 1024 * 1024
_MAX_AUDIT_RECORDS = 1_000_000
_MAX_AUDIT_INVOCATIONS = 100_000
_MAX_RELEASE_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_ISSUE_EXAMPLES = 500
_EMPTY_SHA256 = sha256_text("")
_CREDENTIAL_VALUE_PATTERN = re.compile(
    r"(?i)(?:bearer\s+\S+|authorization\s*[:=]|api[-_ ]?key\s*[:=]|"
    r"password\s*[:=]|secret\s*[:=]|token\s*[:=])"
)
_SENSITIVE_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "question",
        re.compile(r"(?i)(?:^|[_-])(?:question|user[_-]?query)(?:$|[_-])"),
    ),
    (
        "prompt",
        re.compile(r"(?i)(?:^|[_-])(?:prompt|instruction|reasoning|chain[_-]?of[_-]?thought)"),
    ),
    (
        "credential",
        re.compile(r"(?i)(?:authorization|cookie|credential|password|secret|token|api[_-]?key)"),
    ),
    (
        "response",
        re.compile(
            r"(?i)(?:^|[_-])(?:answer|response|request[_-]?body|response[_-]?body|"
            r"raw[_-]?(?:request|response|content)|content|message)(?:$|[_-])"
        ),
    ),
)


class AuditValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class AuditValidationIssue(AuditValidationModel):
    """A bounded diagnostic that can never carry an Audit record value."""

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    line_number: int | None = Field(default=None, ge=1)
    invocation_id_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    stage: AuditStage | None = None


class AuditIncidentCounts(AuditValidationModel):
    timed_out_invocations: int = Field(ge=0)
    overload_invocations: int = Field(ge=0)
    fallback_invocations: int = Field(ge=0)
    failed_invocations: int = Field(ge=0)
    blocked_invocations: int = Field(ge=0)
    response_aborted_invocations: int = Field(ge=0)


class AuditValidationReport(AuditValidationModel):
    """Deterministic, redacted report for one immutable Audit byte stream."""

    schema_version: Literal["1.0"] = "1.0"
    status: AuditValidationStatus
    audit_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    audit_file_size_bytes: int = Field(ge=0)
    record_count: int = Field(ge=0)
    valid_event_count: int = Field(ge=0)
    invalid_event_count: int = Field(ge=0)
    invocation_count: int = Field(ge=0)
    lifecycle_complete_invocation_count: int = Field(ge=0)
    executable_success_invocation_count: int = Field(ge=0)
    execution_path_complete_invocation_count: int = Field(ge=0)
    release_linked_event_count: int = Field(ge=0)
    dataset_linked_event_count: int = Field(ge=0)
    database_fingerprint_linked_event_count: int = Field(ge=0)
    expected_release_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    expected_deployment_binding_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    binding_trust_anchor_verified: bool
    stage_event_counts: dict[str, int]
    outcome_event_counts: dict[str, int]
    incident_counts: AuditIncidentCounts
    issue_count: int = Field(ge=0)
    issue_counts: dict[str, int]
    issue_examples: tuple[AuditValidationIssue, ...] = Field(
        default=(),
        max_length=_MAX_ISSUE_EXAMPLES,
    )
    issue_examples_truncated: bool


class AuditValidationCommitment(AuditValidationModel):
    """Small sidecar fixing both the source Audit and rendered report bytes."""

    schema_version: Literal["1.0"] = "1.0"
    audit_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_file_sha256: str = Field(pattern=_SHA256_PATTERN)


class ExpectedDatasetFingerprint(AuditValidationModel):
    database_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)


@dataclass(frozen=True, slots=True)
class ExpectedAuditReleaseLinkage:
    """Trusted hashes derived from a canonical manifest and deployment binding."""

    release_id: str = field(repr=False)
    agent_release_id_sha256: str
    agent_release_manifest_sha256: str
    deployment_binding_sha256: str
    release_context_sha256: str
    dataset_release_id: str = field(repr=False)
    dataset_release_id_sha256: str
    approved_dataset_manifest_sha256: str
    datasets: Mapping[ProductFamily, ExpectedDatasetFingerprint]
    binding_trust_anchor_verified: bool


@dataclass(frozen=True, slots=True)
class AuditValidationPolicy:
    require_request_lifecycle: bool = True
    require_execution_path: bool = True
    require_release_linkage: bool = False
    require_dataset_linkage: bool = False


@dataclass(frozen=True, slots=True)
class _LocatedEvent:
    line_number: int
    event: AuditEvent


class AuditValidationInputError(RuntimeError):
    """Stable input failure whose message never contains source bytes or paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DuplicateJsonKey(ValueError):
    pass


class _IssueAccumulator:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.examples: list[AuditValidationIssue] = []
        self.total = 0

    def add(
        self,
        code: str,
        *,
        line_number: int | None = None,
        invocation_id_sha256: str | None = None,
        stage: AuditStage | None = None,
    ) -> None:
        self.total += 1
        self.counts[code] += 1
        if len(self.examples) >= _MAX_ISSUE_EXAMPLES:
            return
        self.examples.append(
            AuditValidationIssue(
                code=code,
                line_number=line_number,
                invocation_id_sha256=invocation_id_sha256,
                stage=stage,
            )
        )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def audit_validation_report_bytes(report: AuditValidationReport) -> bytes:
    return _canonical_json_bytes(report.model_dump(mode="json"))


def audit_validation_commitment(
    report: AuditValidationReport,
) -> AuditValidationCommitment:
    report_bytes = audit_validation_report_bytes(report)
    return AuditValidationCommitment(
        audit_file_sha256=report.audit_file_sha256,
        report_file_sha256=hashlib.sha256(report_bytes).hexdigest(),
    )


def audit_validation_commitment_bytes(commitment: AuditValidationCommitment) -> bytes:
    return _canonical_json_bytes(commitment.model_dump(mode="json"))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON is forbidden")


def _strict_json(data: bytes) -> Any:
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )


def _sensitive_categories(value: object) -> set[str]:
    categories: set[str] = set()

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    for category, pattern in _SENSITIVE_KEY_PATTERNS:
                        if pattern.search(key) and key not in AuditEvent.model_fields:
                            categories.add(category)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str) and _CREDENTIAL_VALUE_PATTERN.search(item):
            categories.add("credential")

    walk(value)
    return categories


def _parse_event_record(
    record: bytes,
    *,
    line_number: int,
    issues: _IssueAccumulator,
) -> AuditEvent | None:
    if not record:
        issues.add("empty_record", line_number=line_number)
        return None
    try:
        payload = _strict_json(record)
    except UnicodeError:
        issues.add("invalid_utf8", line_number=line_number)
        return None
    except _DuplicateJsonKey:
        issues.add("duplicate_json_key", line_number=line_number)
        return None
    except (json.JSONDecodeError, ValueError):
        issues.add("invalid_json", line_number=line_number)
        return None
    for category in sorted(_sensitive_categories(payload)):
        issues.add(f"{category}_material_exposed", line_number=line_number)
    if not isinstance(payload, dict):
        issues.add("schema_violation", line_number=line_number)
        return None
    try:
        event = AuditEvent.model_validate(payload)
        assert_safe_audit_payload(event.model_dump(mode="python"))
    except Exception:  # noqa: BLE001 - raw values must never enter diagnostics
        issues.add("schema_violation", line_number=line_number)
        return None
    return event


def _read_audit_events(
    path: str | Path,
    issues: _IssueAccumulator,
) -> tuple[list[_LocatedEvent], str, int, int, int]:
    target = Path(path)
    if not target.is_absolute():
        raise AuditValidationInputError("audit_path_not_absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        raise AuditValidationInputError("audit_file_unavailable") from None
    events: list[_LocatedEvent] = []
    digest = hashlib.sha256()
    total_bytes = 0
    line_number = 0
    invalid_records = 0
    buffer = bytearray()
    oversized = False
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuditValidationInputError("audit_file_not_regular")
        if (
            before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
            or before.st_nlink != 1
        ):
            raise AuditValidationInputError("audit_file_permissions_unsafe")
        if before.st_size > _MAX_AUDIT_FILE_BYTES:
            raise AuditValidationInputError("audit_file_too_large")
        while True:
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
            cursor = 0
            while cursor < len(chunk):
                newline = chunk.find(b"\n", cursor)
                end = len(chunk) if newline < 0 else newline
                segment = chunk[cursor:end]
                if not oversized:
                    if len(buffer) + len(segment) + (1 if newline >= 0 else 0) > (
                        _MAX_AUDIT_RECORD_BYTES
                    ):
                        oversized = True
                        buffer.clear()
                    else:
                        buffer.extend(segment)
                if newline < 0:
                    break
                line_number += 1
                if line_number > _MAX_AUDIT_RECORDS:
                    raise AuditValidationInputError("audit_record_limit_exceeded")
                if oversized:
                    issues.add("record_too_large", line_number=line_number)
                    invalid_records += 1
                else:
                    event = _parse_event_record(
                        bytes(buffer),
                        line_number=line_number,
                        issues=issues,
                    )
                    if event is None:
                        invalid_records += 1
                    else:
                        events.append(_LocatedEvent(line_number=line_number, event=event))
                buffer.clear()
                oversized = False
                cursor = newline + 1
        if buffer or oversized:
            line_number += 1
            issues.add("incomplete_final_record", line_number=line_number)
            if oversized:
                issues.add("record_too_large", line_number=line_number)
            invalid_records += 1
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or before.st_size != total_bytes:
            issues.add("audit_file_changed_during_validation")
    finally:
        os.close(descriptor)
    if line_number == 0:
        issues.add("empty_audit_file")
    return events, digest.hexdigest(), total_bytes, line_number, invalid_records


def _read_bounded_artifact(path: str | Path, *, label: str) -> bytes:
    target = Path(path)
    if not target.is_absolute():
        raise AuditValidationInputError(f"{label}_path_not_absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        raise AuditValidationInputError(f"{label}_unavailable") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= (
            _MAX_RELEASE_ARTIFACT_BYTES
        ):
            raise AuditValidationInputError(f"{label}_unsafe")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            try:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            except InterruptedError:
                continue
            if not chunk:
                raise AuditValidationInputError(f"{label}_changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AuditValidationInputError(f"{label}_changed")
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise AuditValidationInputError(f"{label}_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_expected_audit_release_linkage(
    *,
    manifest_path: str | Path,
    binding_path: str | Path,
    expected_binding_sha256: str | None = None,
) -> ExpectedAuditReleaseLinkage:
    """Load canonical release artifacts without hashing runtime code or reading DB rows."""

    if (
        expected_binding_sha256 is not None
        and re.fullmatch(
            _SHA256_PATTERN,
            expected_binding_sha256,
        )
        is None
    ):
        raise AuditValidationInputError("binding_trust_anchor_invalid")
    manifest_data = _read_bounded_artifact(manifest_path, label="release_manifest")
    binding_data = _read_bounded_artifact(binding_path, label="deployment_binding")
    manifest_sha256 = hashlib.sha256(manifest_data).hexdigest()
    binding_sha256 = hashlib.sha256(binding_data).hexdigest()
    if expected_binding_sha256 is not None and binding_sha256 != expected_binding_sha256:
        raise AuditValidationInputError("binding_trust_anchor_mismatch")
    try:
        manifest_payload = _strict_json(manifest_data)
        binding_payload = _strict_json(binding_data)
        if not isinstance(manifest_payload, dict) or not isinstance(binding_payload, dict):
            raise ValueError
        manifest = AgentReleaseManifest.model_validate(manifest_payload)
        binding = DeploymentBinding.model_validate(binding_payload)
    except Exception:  # noqa: BLE001 - never expose release artifact values
        raise AuditValidationInputError("release_artifact_invalid") from None
    if manifest_data != manifest_file_bytes(
        manifest
    ) or binding_data != deployment_binding_file_bytes(binding):
        raise AuditValidationInputError("release_artifact_noncanonical")
    if (
        binding.release_manifest_sha256 != manifest_sha256
        or binding.release_id != manifest.release_id
        or binding.environment != manifest.environment
        or binding.source_commit != manifest.source_commit
    ):
        raise AuditValidationInputError("release_artifact_mismatch")
    context_sha256 = canonical_sha256(
        {
            "release_id": manifest.release_id,
            "manifest_file_sha256": manifest_sha256,
            "binding_file_sha256": binding_sha256,
            "image_reference": binding.image_reference,
            "activation_generation": binding.activation_generation,
        }
    )
    datasets = manifest.components.approved_datasets
    return ExpectedAuditReleaseLinkage(
        release_id=manifest.release_id,
        agent_release_id_sha256=sha256_text(manifest.release_id),
        agent_release_manifest_sha256=manifest_sha256,
        deployment_binding_sha256=binding_sha256,
        release_context_sha256=context_sha256,
        dataset_release_id=datasets.release_id,
        dataset_release_id_sha256=sha256_text(datasets.release_id),
        approved_dataset_manifest_sha256=datasets.manifest.contract_sha256,
        datasets={
            ProductFamily(name): ExpectedDatasetFingerprint(
                database_snapshot_sha256=snapshot.database_sha256,
                source_snapshot_sha256=snapshot.data_file_sha256,
            )
            for name, snapshot in datasets.snapshots.items()
        },
        binding_trust_anchor_verified=expected_binding_sha256 is not None,
    )


def _release_tuple(event: AuditEvent) -> tuple[str, str, str, str] | None:
    values = (
        event.agent_release_id_sha256,
        event.agent_release_manifest_sha256,
        event.deployment_binding_sha256,
        event.release_context_sha256,
    )
    if all(value is not None for value in values):
        return values  # type: ignore[return-value]
    return None


def _dataset_tuple(event: AuditEvent) -> tuple[str, str, str, str, str] | None:
    values = (
        event.dataset_release_id_sha256,
        event.approved_dataset_manifest_sha256,
        event.database_manifest_sha256,
        event.database_snapshot_sha256,
        event.source_snapshot_sha256,
    )
    if all(value is not None for value in values):
        return values  # type: ignore[return-value]
    return None


def _add_event_issue(
    issues: _IssueAccumulator,
    code: str,
    located: _LocatedEvent,
) -> None:
    issues.add(
        code,
        line_number=located.line_number,
        invocation_id_sha256=located.event.invocation_id_sha256,
        stage=located.event.stage,
    )


def _validate_release_and_dataset_linkage(
    events: Iterable[_LocatedEvent],
    *,
    expected_release: ExpectedAuditReleaseLinkage | None,
    policy: AuditValidationPolicy,
    issues: _IssueAccumulator,
) -> tuple[int, int, int]:
    expected_release_tuple = (
        (
            expected_release.agent_release_id_sha256,
            expected_release.agent_release_manifest_sha256,
            expected_release.deployment_binding_sha256,
            expected_release.release_context_sha256,
        )
        if expected_release is not None
        else None
    )
    observed_release_tuple: tuple[str, str, str, str] | None = None
    release_linked = 0
    dataset_linked = 0
    database_linked = 0
    dataset_tuple_by_family: dict[ProductFamily, tuple[str, str, str, str, str]] = {}
    data_required_stages = {
        AuditStage.AUTHORITY,
        AuditStage.SQL,
        AuditStage.ORACLE,
        AuditStage.VERIFIER,
        AuditStage.RENDERER,
    }
    for located in events:
        event = located.event
        release_values = _release_tuple(event)
        if release_values is not None:
            release_linked += 1
            if observed_release_tuple is None:
                observed_release_tuple = release_values
            elif release_values != observed_release_tuple:
                _add_event_issue(issues, "release_linkage_inconsistent", located)
        if expected_release_tuple is not None:
            if release_values is None:
                _add_event_issue(issues, "release_linkage_missing", located)
            elif release_values != expected_release_tuple:
                _add_event_issue(issues, "release_linkage_mismatch", located)
        elif policy.require_release_linkage and release_values is None:
            _add_event_issue(issues, "release_linkage_missing", located)

        dataset_values = _dataset_tuple(event)
        if dataset_values is not None:
            dataset_linked += 1
            database_linked += 1
            if len(event.product_families) != 1:
                _add_event_issue(issues, "dataset_family_ambiguous", located)
                continue
            family = event.product_families[0]
            prior_dataset = dataset_tuple_by_family.setdefault(family, dataset_values)
            if prior_dataset != dataset_values:
                _add_event_issue(issues, "dataset_linkage_inconsistent", located)
            if expected_release is not None:
                fingerprint = expected_release.datasets[family]
                if dataset_values[0] != expected_release.dataset_release_id_sha256:
                    _add_event_issue(issues, "dataset_release_mismatch", located)
                if dataset_values[1] != expected_release.approved_dataset_manifest_sha256:
                    _add_event_issue(issues, "dataset_manifest_mismatch", located)
                if dataset_values[3] != fingerprint.database_snapshot_sha256:
                    _add_event_issue(issues, "database_snapshot_mismatch", located)
                if dataset_values[4] != fingerprint.source_snapshot_sha256:
                    _add_event_issue(issues, "source_snapshot_mismatch", located)

        requires_dataset = (
            event.outcome is AuditOutcome.SUCCEEDED
            and event.route_disposition is RouteDisposition.EXECUTE
            and event.stage in data_required_stages
        )
        if (policy.require_dataset_linkage or expected_release is not None) and requires_dataset:
            if dataset_values is None:
                _add_event_issue(issues, "dataset_linkage_missing", located)
    return release_linked, dataset_linked, database_linked


def _validate_dataset_bundle(
    invocation: list[_LocatedEvent],
    answer: _LocatedEvent,
    *,
    expected_release: ExpectedAuditReleaseLinkage | None,
    issues: _IssueAccumulator,
) -> bool:
    event = answer.event
    if len(event.product_families) == 1:
        if _dataset_tuple(event) is None:
            _add_event_issue(issues, "answer_dataset_linkage_missing", answer)
            return False
        return True
    if not event.product_families:
        _add_event_issue(issues, "answer_dataset_family_missing", answer)
        return False
    if event.dataset_bundle_sha256 is None:
        _add_event_issue(issues, "dataset_bundle_missing", answer)
        return False
    if expected_release is None:
        _add_event_issue(issues, "dataset_bundle_unverifiable_without_release", answer)
        return False
    database_manifests: dict[ProductFamily, str] = {}
    for located in invocation:
        child = located.event
        if located.line_number >= answer.line_number or len(child.product_families) != 1:
            continue
        dataset_values = _dataset_tuple(child)
        if dataset_values is not None:
            database_manifests.setdefault(child.product_families[0], dataset_values[2])
    if any(family not in database_manifests for family in event.product_families):
        _add_event_issue(issues, "dataset_bundle_source_missing", answer)
        return False
    payload = []
    for family in event.product_families:
        fingerprint = expected_release.datasets[family]
        payload.append(
            {
                "product_family": family.value,
                "dataset_release_id": expected_release.dataset_release_id,
                "approved_dataset_manifest_sha256": (
                    expected_release.approved_dataset_manifest_sha256
                ),
                "database_manifest_sha256": database_manifests[family],
                "database_snapshot_sha256": fingerprint.database_snapshot_sha256,
                "source_snapshot_sha256": fingerprint.source_snapshot_sha256,
            }
        )
    if canonical_sha256(payload) != event.dataset_bundle_sha256:
        _add_event_issue(issues, "dataset_bundle_mismatch", answer)
        return False
    return True


def _validate_success_path(
    invocation: list[_LocatedEvent],
    answer: _LocatedEvent,
    *,
    expected_release: ExpectedAuditReleaseLinkage | None,
    require_dataset_linkage: bool,
    issues: _IssueAccumulator,
) -> bool:
    complete = True

    def fail(code: str, located: _LocatedEvent = answer) -> None:
        nonlocal complete
        complete = False
        _add_event_issue(issues, code, located)

    preceding = [item for item in invocation if item.line_number < answer.line_number]
    route = [
        item
        for item in preceding
        if item.event.stage is AuditStage.ROUTE
        and item.event.outcome is AuditOutcome.SUCCEEDED
        and item.event.reason_code == "routed_execute"
        and item.event.route_disposition is RouteDisposition.EXECUTE
    ]
    if not route:
        fail("execution_route_missing")
    compiler = [
        item
        for item in preceding
        if item.event.stage is AuditStage.COMPILER
        and item.event.outcome is AuditOutcome.SUCCEEDED
        and item.event.reason_code in {"plan_compiled", "family_plan_compiled"}
        and item.event.plan_sha256 is not None
    ]
    if not compiler:
        fail("execution_queryplan_missing")
        return complete
    if route and route[-1].line_number >= compiler[0].line_number:
        fail("execution_stage_order_invalid", compiler[0])

    compiled_plans: list[tuple[ProductFamily, str, _LocatedEvent]] = []
    seen_compiled_plans: set[tuple[ProductFamily, str]] = set()
    for item in compiler:
        if len(item.event.product_families) != 1 or item.event.plan_sha256 is None:
            fail("queryplan_family_ambiguous", item)
            continue
        key = (item.event.product_families[0], item.event.plan_sha256)
        if key in seen_compiled_plans:
            continue
        seen_compiled_plans.add(key)
        compiled_plans.append((*key, item))
    if len(answer.event.product_families) == 1:
        if answer.event.plan_sha256 is None:
            fail("answer_plan_link_missing")
        elif [plan for _, plan, _ in compiled_plans] != [answer.event.plan_sha256]:
            fail("answer_plan_link_mismatch")
    else:
        expected_bundle = canonical_sha256(
            [
                {"product_family": family.value, "plan_sha256": plan}
                for family, plan, _ in compiled_plans
            ]
        )
        if answer.event.plan_bundle_sha256 is None:
            fail("answer_plan_bundle_missing")
        elif answer.event.plan_bundle_sha256 != expected_bundle:
            fail("answer_plan_bundle_mismatch")
        if tuple(family for family, _, _ in compiled_plans) != answer.event.product_families:
            fail("answer_plan_family_order_mismatch")

    compiled_hashes = {plan for _, plan, _ in compiled_plans}
    downstream_stages = {
        AuditStage.AUTHORITY,
        AuditStage.SQL,
        AuditStage.ORACLE,
        AuditStage.VERIFIER,
        AuditStage.RENDERER,
    }
    for located in preceding:
        event = located.event
        if (
            event.stage in downstream_stages
            and event.outcome is AuditOutcome.SUCCEEDED
            and event.route_disposition is RouteDisposition.EXECUTE
            and event.plan_sha256 not in compiled_hashes
        ):
            fail("execution_plan_link_mismatch", located)

    for _family, plan, compiler_event in compiled_plans:
        cursor = compiler_event
        required_stages = (
            (AuditStage.AUTHORITY, "authority_granted", "execution_authority_missing"),
            (
                AuditStage.SQL,
                "parameterized_statement_completed",
                "execution_sql_missing",
            ),
            (AuditStage.ORACLE, "oracle_completed", "execution_oracle_missing"),
            (
                AuditStage.VERIFIER,
                "verification_passed",
                "execution_verifier_missing",
            ),
            (
                AuditStage.RENDERER,
                "rendering_completed",
                "execution_renderer_missing",
            ),
        )
        for stage, reason_code, missing_code in required_stages:
            candidates = [
                item
                for item in preceding
                if item.event.stage is stage
                and item.event.outcome is AuditOutcome.SUCCEEDED
                and item.event.reason_code == reason_code
                and item.event.plan_sha256 == plan
            ]
            ordered = [item for item in candidates if item.line_number > cursor.line_number]
            if ordered:
                cursor = ordered[0]
            elif candidates:
                fail("execution_stage_order_invalid", candidates[-1])
            else:
                fail(missing_code, compiler_event)
        # Repeated instrumentation is accepted: each lookup selects the first
        # matching event after the prior link and ignores equivalent repeats.

    if require_dataset_linkage or expected_release is not None:
        if not _validate_dataset_bundle(
            invocation,
            answer,
            expected_release=expected_release,
            issues=issues,
        ):
            complete = False
    return complete


def _validate_invocations(
    events: list[_LocatedEvent],
    *,
    expected_release: ExpectedAuditReleaseLinkage | None,
    policy: AuditValidationPolicy,
    issues: _IssueAccumulator,
) -> tuple[int, int, int, int, AuditIncidentCounts]:
    groups: dict[str, list[_LocatedEvent]] = {}
    for located in events:
        invocation = located.event.invocation_id_sha256
        if invocation is None or located.event.event_sequence is None:
            _add_event_issue(issues, "invocation_correlation_missing", located)
            continue
        if invocation not in groups and len(groups) >= _MAX_AUDIT_INVOCATIONS:
            raise AuditValidationInputError("audit_invocation_limit_exceeded")
        groups.setdefault(invocation, []).append(located)

    lifecycle_complete = 0
    executable_success = 0
    execution_path_complete = 0
    timeout_count = 0
    overload_count = 0
    fallback_count = 0
    failed_count = 0
    blocked_count = 0
    aborted_count = 0
    for invocation_id, invocation in groups.items():
        invocation.sort(key=lambda item: item.line_number)
        sequences = [item.event.event_sequence for item in invocation]
        assert all(sequence is not None for sequence in sequences)
        integer_sequences = [int(sequence) for sequence in sequences if sequence is not None]
        sequence_counter = Counter(integer_sequences)
        for sequence, count in sorted(sequence_counter.items()):
            if count > 1:
                duplicate = next(
                    item for item in invocation if item.event.event_sequence == sequence
                )
                _add_event_issue(issues, "event_sequence_duplicate", duplicate)
        if integer_sequences != sorted(integer_sequences):
            _add_event_issue(issues, "event_sequence_out_of_order", invocation[0])
        if integer_sequences:
            expected_sequences = set(range(1, max(integer_sequences) + 1))
            if set(integer_sequences) != expected_sequences:
                _add_event_issue(issues, "event_sequence_gap", invocation[0])

        starts = [
            item
            for item in invocation
            if item.event.stage is AuditStage.REQUEST and item.event.outcome is AuditOutcome.STARTED
        ]
        terminals = [
            item
            for item in invocation
            if item.event.stage is AuditStage.REQUEST
            and item.event.outcome is not AuditOutcome.STARTED
        ]
        lifecycle_ok = True
        if policy.require_request_lifecycle:
            if not starts:
                issues.add(
                    "request_start_missing",
                    invocation_id_sha256=invocation_id,
                )
                lifecycle_ok = False
            elif len(starts) > 1:
                _add_event_issue(issues, "request_start_duplicate", starts[1])
                lifecycle_ok = False
            else:
                start = starts[0]
                if start is not invocation[0] or start.event.event_sequence != 1:
                    _add_event_issue(issues, "request_start_not_first", start)
                    lifecycle_ok = False
                if (
                    start.event.request_id_sha256 != _EMPTY_SHA256
                    or start.event.question_sha256 != _EMPTY_SHA256
                ):
                    _add_event_issue(issues, "transport_start_payload_hash_nonempty", start)
                    lifecycle_ok = False
            if not terminals:
                issues.add(
                    "request_terminal_missing",
                    invocation_id_sha256=invocation_id,
                )
                lifecycle_ok = False
            elif len(terminals) > 1:
                _add_event_issue(issues, "request_terminal_duplicate", terminals[1])
                lifecycle_ok = False
            else:
                terminal = terminals[0]
                terminal_index = invocation.index(terminal)
                invalid_late_event = next(
                    (
                        item
                        for item in invocation[terminal_index + 1 :]
                        if item.event.stage is not AuditStage.SCHEMA_LINK_SHADOW
                    ),
                    None,
                )
                if invalid_late_event is not None:
                    _add_event_issue(
                        issues,
                        "request_terminal_not_last",
                        invalid_late_event,
                    )
                    lifecycle_ok = False
            post_start_pairs = {
                (item.event.request_id_sha256, item.event.question_sha256)
                for item in invocation[1:]
            }
            if len(post_start_pairs) > 1:
                _add_event_issue(issues, "request_hash_linkage_inconsistent", invocation[1])
                lifecycle_ok = False
        if lifecycle_ok:
            lifecycle_complete += 1

        if any(item.event.outcome is AuditOutcome.TIMED_OUT for item in invocation):
            timeout_count += 1
        if any(item.event.reason_code == "admission_rejected" for item in invocation):
            overload_count += 1
        if any(item.event.reason_code == "execution_fallback" for item in invocation):
            fallback_count += 1
        if any(item.event.outcome is AuditOutcome.FAILED for item in invocation):
            failed_count += 1
        if any(item.event.outcome is AuditOutcome.BLOCKED for item in invocation):
            blocked_count += 1
        if any(item.event.reason_code == "response_aborted" for item in invocation):
            aborted_count += 1

        successful_answers = [
            item
            for item in invocation
            if item.event.stage is AuditStage.ANSWER
            and item.event.outcome is AuditOutcome.SUCCEEDED
            and item.event.route_disposition is RouteDisposition.EXECUTE
        ]
        if successful_answers:
            executable_success += 1
            if len(successful_answers) > 1:
                _add_event_issue(issues, "answer_terminal_duplicate", successful_answers[1])
            answer = successful_answers[-1]
            if not policy.require_execution_path or _validate_success_path(
                invocation,
                answer,
                expected_release=expected_release,
                require_dataset_linkage=policy.require_dataset_linkage,
                issues=issues,
            ):
                execution_path_complete += 1

        control_answers = [
            item
            for item in invocation
            if item.event.stage is AuditStage.ANSWER
            and item.event.outcome in {AuditOutcome.CLARIFIED, AuditOutcome.UNSUPPORTED}
        ]
        for answer in control_answers:
            if any(
                item.line_number < answer.line_number
                and (
                    (
                        item.event.stage is AuditStage.AUTHORITY
                        and not (
                            item.event.outcome is AuditOutcome.BLOCKED
                            and item.event.reason_code == "authority_denied"
                        )
                    )
                    or item.event.stage
                    in {
                        AuditStage.EXECUTION,
                        AuditStage.SQL,
                        AuditStage.ORACLE,
                        AuditStage.VERIFIER,
                        AuditStage.RENDERER,
                    }
                )
                for item in invocation
            ):
                _add_event_issue(issues, "control_path_executed", answer)

    return (
        len(groups),
        lifecycle_complete,
        executable_success,
        execution_path_complete,
        AuditIncidentCounts(
            timed_out_invocations=timeout_count,
            overload_invocations=overload_count,
            fallback_invocations=fallback_count,
            failed_invocations=failed_count,
            blocked_invocations=blocked_count,
            response_aborted_invocations=aborted_count,
        ),
    )


def validate_audit_jsonl(
    audit_path: str | Path,
    *,
    expected_release: ExpectedAuditReleaseLinkage | None = None,
    policy: AuditValidationPolicy | None = None,
) -> AuditValidationReport:
    """Validate a complete JSONL stream without returning or logging record values."""

    resolved_policy = policy or AuditValidationPolicy(
        require_release_linkage=expected_release is not None,
        require_dataset_linkage=expected_release is not None,
    )
    issues = _IssueAccumulator()
    events, audit_sha256, total_bytes, record_count, invalid_records = _read_audit_events(
        audit_path,
        issues,
    )
    release_linked, dataset_linked, database_linked = _validate_release_and_dataset_linkage(
        events,
        expected_release=expected_release,
        policy=resolved_policy,
        issues=issues,
    )
    (
        invocation_count,
        lifecycle_complete,
        executable_success,
        execution_path_complete,
        incidents,
    ) = _validate_invocations(
        events,
        expected_release=expected_release,
        policy=resolved_policy,
        issues=issues,
    )
    stage_counts = Counter(item.event.stage.value for item in events)
    outcome_counts = Counter(item.event.outcome.value for item in events)
    return AuditValidationReport(
        status=(
            AuditValidationStatus.PASSED if issues.total == 0 else AuditValidationStatus.FAILED
        ),
        audit_file_sha256=audit_sha256,
        audit_file_size_bytes=total_bytes,
        record_count=record_count,
        valid_event_count=len(events),
        invalid_event_count=invalid_records,
        invocation_count=invocation_count,
        lifecycle_complete_invocation_count=lifecycle_complete,
        executable_success_invocation_count=executable_success,
        execution_path_complete_invocation_count=execution_path_complete,
        release_linked_event_count=release_linked,
        dataset_linked_event_count=dataset_linked,
        database_fingerprint_linked_event_count=database_linked,
        expected_release_manifest_sha256=(
            expected_release.agent_release_manifest_sha256 if expected_release is not None else None
        ),
        expected_deployment_binding_sha256=(
            expected_release.deployment_binding_sha256 if expected_release is not None else None
        ),
        binding_trust_anchor_verified=(
            expected_release.binding_trust_anchor_verified
            if expected_release is not None
            else False
        ),
        stage_event_counts=dict(sorted(stage_counts.items())),
        outcome_event_counts=dict(sorted(outcome_counts.items())),
        incident_counts=incidents,
        issue_count=issues.total,
        issue_counts=dict(sorted(issues.counts.items())),
        issue_examples=tuple(issues.examples),
        issue_examples_truncated=issues.total > len(issues.examples),
    )


__all__ = [
    "AuditIncidentCounts",
    "AuditValidationCommitment",
    "AuditValidationInputError",
    "AuditValidationIssue",
    "AuditValidationPolicy",
    "AuditValidationReport",
    "AuditValidationStatus",
    "ExpectedAuditReleaseLinkage",
    "ExpectedDatasetFingerprint",
    "audit_validation_commitment",
    "audit_validation_commitment_bytes",
    "audit_validation_report_bytes",
    "load_expected_audit_release_linkage",
    "validate_audit_jsonl",
]
