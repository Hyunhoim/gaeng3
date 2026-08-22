from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import stat
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, perf_counter
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_PRODUCT_LINKS = 100
_MAX_EVIDENCE_LINKS = 2_000
_MAX_AUDIT_EVENT_BYTES = 64 * 1024
_TOKEN_PATTERN = re.compile(r"(?i)(?:bearer\s+|api[-_ ]?key\s*[:=]\s*|authorization\s*[:=]\s*)")
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s]*")
_SENSITIVE_REASON_PATTERN = re.compile(
    r"(?i)(?:question|prompt|answer|gold|expected|chain.?of.?thought|cot|reasoning|"
    r"authorization|cookie|secret|token|api.?key|database|sqlite|path|header|body)"
)


class ObservabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditStage(StrEnum):
    REQUEST = "request"
    ROUTE = "route"
    SAFETY = "safety"
    PLAN = "plan"
    PLANNING = "planning"
    LEXICAL = "lexical"
    DENSE = "dense"
    SCHEMA_LINK_SHADOW = "schema_link_shadow"
    HCLX = "hclx"
    COMPILER = "compiler"
    AUTHORITY = "authority"
    EXECUTION = "execution"
    ORACLE = "oracle"
    SQL = "sql"
    VERIFIER = "verifier"
    RENDERER = "renderer"
    SERIALIZATION = "serialization"
    ANSWER = "answer"


class AuditOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    CLARIFIED = "clarified"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class MetricCounter(StrEnum):
    REQUESTS = "requests_total"
    ROUTE_EXECUTIONS = "route_executions_total"
    CLARIFICATIONS = "clarifications_total"
    UNSUPPORTED = "unsupported_total"
    SAFETY_BLOCKS = "safety_blocks_total"
    LEXICAL_CALLS = "lexical_calls_total"
    DENSE_CALLS = "dense_calls_total"
    HCLX_CALLS = "hclx_calls_total"
    HCLX_SUCCESSES = "hclx_successes_total"
    HCLX_AUTHENTICATION_FAILURES = "hclx_authentication_failures_total"
    HCLX_RATE_LIMITS = "hclx_rate_limits_total"
    HCLX_SERVICE_FAILURES = "hclx_service_failures_total"
    HCLX_TIMEOUTS = "hclx_timeouts_total"
    HCLX_TRANSPORT_FAILURES = "hclx_transport_failures_total"
    HCLX_RESPONSE_FAILURES = "hclx_response_failures_total"
    HCLX_INPUT_TOKENS = "hclx_input_tokens_total"
    HCLX_OUTPUT_TOKENS = "hclx_output_tokens_total"
    ORACLE_CALLS = "oracle_calls_total"
    SQL_EXECUTIONS = "sql_executions_total"
    AUDIT_SINK_FAILURES = "audit_sink_failures_total"
    AUDIT_EVENTS_DROPPED = "audit_events_dropped_total"
    AUDIT_EVENTS_ACCEPTED = "audit_events_accepted_total"
    AUDIT_DOWNSTREAM_FAILURES = "audit_downstream_failures_total"
    AUDIT_FLUSH_FAILURES = "audit_flush_failures_total"
    SHADOW_OBSERVATIONS = "shadow_observations_total"
    BLOCKED_MODEL_CALLS_EXPECTED = "blocked_model_calls_expected_total"
    BLOCKED_MODEL_CALLS_ACTUAL = "blocked_model_calls_actual_total"
    BLOCKED_DENSE_CALLS_ACTUAL = "blocked_dense_calls_actual_total"
    BLOCKED_HCLX_CALLS_ACTUAL = "blocked_hclx_calls_actual_total"
    BLOCKED_ORACLE_CALLS_ACTUAL = "blocked_oracle_calls_actual_total"
    EVIDENCE_EXPECTED = "evidence_expected_total"
    EVIDENCE_PRESENT = "evidence_present_total"
    EVIDENCE_INCOMPLETE = "evidence_incomplete_total"
    TIMEOUTS = "timeouts_total"
    FALLBACKS = "fallbacks_total"
    VERIFIER_FAILURES = "verifier_failures_total"
    QUEUE_DROPS = "queue_drops_total"


class AuditEvent(ObservabilityModel):
    """Allowlisted, redacted Stage 4 event. It cannot carry arbitrary payloads."""

    schema_version: Literal["1.1", "1.2"] = "1.2"
    observed_at_utc: datetime
    stage: AuditStage
    outcome: AuditOutcome
    reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    duration_ms: float = Field(ge=0, le=3_600_000)
    request_id_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    # Optional defaults retain read compatibility with AuditEvent v1.1 records
    # written before invocation correlation was introduced. All new events made
    # through ``redacted`` or ``RequestAuditRecorder`` populate both fields.
    invocation_id_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    event_sequence: int | None = Field(default=None, ge=1, le=9_223_372_036_854_775_807)
    route_disposition: RouteDisposition | None = None
    interaction_intent: InteractionIntent | None = None
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    agent_release_id_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    agent_release_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    deployment_binding_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    release_context_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    dataset_release_id_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    approved_dataset_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    database_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    database_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    plan_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    plan_bundle_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    dataset_bundle_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_revision_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_snapshot_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    index_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    relation_set_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    product_family_count: int = Field(default=0, ge=0, le=4)
    candidate_count: int = Field(default=0, ge=0, le=1_000_000)
    result_count: int = Field(default=0, ge=0, le=100_000)
    evidence_count: int = Field(default=0, ge=0, le=100_000)
    shadow_candidate_count: int = Field(default=0, ge=0, le=100_000)
    product_id_sha256s: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_PRODUCT_LINKS,
    )
    evidence_id_sha256s: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_EVIDENCE_LINKS,
    )

    @field_validator("product_id_sha256s", "evidence_id_sha256s")
    @classmethod
    def require_unique_sha256_links(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if re.fullmatch(_SHA256_PATTERN, item) is None:
                raise ValueError("audit linkage values must be lowercase SHA-256")
        if len(value) != len(set(value)):
            raise ValueError("audit linkage hashes must be unique")
        return value

    @field_validator("observed_at_utc")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason_code")
    @classmethod
    def reject_sensitive_reason_codes(cls, value: str) -> str:
        if _SENSITIVE_REASON_PATTERN.search(value):
            raise ValueError("reason_code must be a stable non-sensitive code")
        return value

    @model_validator(mode="after")
    def validate_linkage_consistency(self) -> AuditEvent:
        if self.schema_version == "1.1" and self.stage is AuditStage.SERIALIZATION:
            raise ValueError("serialization audit events require schema version 1.2")
        if (self.invocation_id_sha256 is None) != (self.event_sequence is None):
            raise ValueError("invocation hash and event sequence must be present together")
        if self.product_family_count != len(self.product_families):
            raise ValueError("product_family_count must match product_families")
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("product_families must be unique")

        route_values = (self.route_disposition, self.interaction_intent)
        if any(value is not None for value in route_values) != all(
            value is not None for value in route_values
        ):
            raise ValueError("route disposition and interaction intent must be present together")
        if self.route_disposition is RouteDisposition.EXECUTE:
            if not self.product_families:
                raise ValueError("executable audit routes require a product family")
            if self.interaction_intent in {
                InteractionIntent.CLARIFY,
                InteractionIntent.UNSUPPORTED,
            }:
                raise ValueError("executable audit routes require an executable intent")
        elif self.route_disposition is RouteDisposition.UNSUPPORTED:
            if self.interaction_intent is not InteractionIntent.UNSUPPORTED:
                raise ValueError("unsupported audit routes require unsupported intent")
        if self.route_disposition is not RouteDisposition.EXECUTE and self.plan_sha256 is not None:
            raise ValueError("non-executable audit routes cannot link an executable plan")

        release_values = (
            self.agent_release_id_sha256,
            self.agent_release_manifest_sha256,
            self.deployment_binding_sha256,
            self.release_context_sha256,
        )
        if any(value is not None for value in release_values) != all(
            value is not None for value in release_values
        ):
            raise ValueError("agent release linkage fields must be present together")

        dataset_values = (
            self.dataset_release_id_sha256,
            self.approved_dataset_manifest_sha256,
            self.database_manifest_sha256,
            self.database_snapshot_sha256,
            self.source_snapshot_sha256,
        )
        if any(value is not None for value in dataset_values) != all(
            value is not None for value in dataset_values
        ):
            raise ValueError("dataset linkage fields must be present together")

        if self.result_count > self.candidate_count:
            raise ValueError("result_count cannot exceed candidate_count")
        if self.product_id_sha256s and len(self.product_id_sha256s) != self.result_count:
            raise ValueError("product linkage count must match result_count")
        if self.evidence_id_sha256s and len(self.evidence_id_sha256s) != self.evidence_count:
            raise ValueError("evidence linkage count must match evidence_count")
        return self

    @classmethod
    def redacted(
        cls,
        *,
        stage: AuditStage,
        outcome: AuditOutcome,
        reason_code: str,
        duration_ms: float,
        request_id: str,
        question: str,
        invocation_id: str | None = None,
        event_sequence: int = 1,
        observed_at_utc: datetime | None = None,
        route_disposition: RouteDisposition | None = None,
        interaction_intent: InteractionIntent | None = None,
        product_families: Iterable[ProductFamily] = (),
        agent_release_id: str | None = None,
        agent_release_manifest_sha256: str | None = None,
        deployment_binding_sha256: str | None = None,
        release_context_sha256: str | None = None,
        dataset_release_id: str | None = None,
        approved_dataset_manifest_sha256: str | None = None,
        database_manifest_sha256: str | None = None,
        database_snapshot_sha256: str | None = None,
        source_snapshot_sha256: str | None = None,
        plan_sha256: str | None = None,
        plan_bundle_sha256: str | None = None,
        dataset_bundle_sha256: str | None = None,
        model_revision: str | None = None,
        model_revision_sha256: str | None = None,
        model_snapshot_manifest_sha256: str | None = None,
        index_manifest_sha256: str | None = None,
        relation_set_sha256: str | None = None,
        product_family_count: int | None = None,
        candidate_count: int = 0,
        result_count: int = 0,
        evidence_count: int = 0,
        shadow_candidate_count: int = 0,
        product_ids: Iterable[str] = (),
        evidence_ids: Iterable[str] = (),
    ) -> AuditEvent:
        families = tuple(product_families)
        resolved_invocation_id = str(uuid4()) if invocation_id is None else invocation_id
        if not resolved_invocation_id.strip() or len(resolved_invocation_id) > 128:
            raise ValueError(
                "invocation_id must be a non-empty server identifier up to 128 characters"
            )
        if model_revision is not None and model_revision_sha256 is not None:
            raise ValueError("model revision must use either raw or pre-hashed linkage")
        if (
            model_revision_sha256 is not None
            and re.fullmatch(
                _SHA256_PATTERN,
                model_revision_sha256,
            )
            is None
        ):
            raise ValueError("pre-hashed model revision linkage must be SHA-256")
        return cls(
            observed_at_utc=observed_at_utc or datetime.now(UTC),
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=round(duration_ms, 6),
            request_id_sha256=sha256_text(request_id),
            question_sha256=sha256_text(question),
            invocation_id_sha256=sha256_text(resolved_invocation_id),
            event_sequence=event_sequence,
            route_disposition=route_disposition,
            interaction_intent=interaction_intent,
            product_families=families,
            agent_release_id_sha256=(
                sha256_text(agent_release_id) if agent_release_id is not None else None
            ),
            agent_release_manifest_sha256=agent_release_manifest_sha256,
            deployment_binding_sha256=deployment_binding_sha256,
            release_context_sha256=release_context_sha256,
            dataset_release_id_sha256=(
                sha256_text(dataset_release_id) if dataset_release_id is not None else None
            ),
            approved_dataset_manifest_sha256=approved_dataset_manifest_sha256,
            database_manifest_sha256=database_manifest_sha256,
            database_snapshot_sha256=database_snapshot_sha256,
            source_snapshot_sha256=source_snapshot_sha256,
            plan_sha256=plan_sha256,
            plan_bundle_sha256=plan_bundle_sha256,
            dataset_bundle_sha256=dataset_bundle_sha256,
            model_revision_sha256=(
                model_revision_sha256
                if model_revision_sha256 is not None
                else (sha256_text(model_revision) if model_revision is not None else None)
            ),
            model_snapshot_manifest_sha256=model_snapshot_manifest_sha256,
            index_manifest_sha256=index_manifest_sha256,
            relation_set_sha256=relation_set_sha256,
            product_family_count=(
                len(families) if product_family_count is None else product_family_count
            ),
            candidate_count=candidate_count,
            result_count=result_count,
            evidence_count=evidence_count,
            shadow_candidate_count=shadow_candidate_count,
            product_id_sha256s=_sha256_identifiers(product_ids, kind="product"),
            evidence_id_sha256s=_sha256_identifiers(evidence_ids, kind="evidence"),
        )


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    def emit(self, event: AuditEvent) -> None:
        del event


class InMemoryAuditSink:
    """Thread-safe bounded sink for tests and non-durable local inspection."""

    def __init__(self, *, max_events: int = 1_000) -> None:
        if not 1 <= max_events <= 100_000:
            raise ValueError("max_events must be between 1 and 100000")
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def emit(self, event: AuditEvent) -> None:
        detached = AuditEvent.model_validate_json(event.model_dump_json())
        with self._lock:
            self._events.append(detached)

    def snapshot(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


class LatencySnapshot(ObservabilityModel):
    sample_count: int = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)


class MetricsSnapshot(ObservabilityModel):
    schema_version: Literal["1.0"] = "1.0"
    observed_at_utc: datetime
    counters: dict[str, int]
    latency_by_stage: dict[str, LatencySnapshot]
    queue_depth: int = Field(ge=0)
    inflight: int = Field(ge=0)
    peak_queue_depth: int = Field(ge=0)
    peak_inflight: int = Field(ge=0)


class AuditSinkSnapshot(ObservabilityModel):
    """Bounded, caller-readable durability state with no request payloads."""

    schema_version: Literal["1.0"] = "1.0"
    observed_at_utc: datetime
    accepting: bool
    started: bool
    closed: bool
    worker_alive: bool
    sentinel_enqueued: bool
    flush_completed: bool
    flush_succeeded: bool | None
    queue_depth: int = Field(ge=0)
    inflight: int = Field(ge=0, le=1)
    pending_event_count: int = Field(ge=0, le=100_001)
    oldest_pending_age_seconds: float | None = Field(default=None, ge=0)
    no_progress_age_seconds: float | None = Field(default=None, ge=0)
    stall_timeout_seconds: float = Field(gt=0, le=60)
    downstream_progress_count: int = Field(ge=0)
    accepted_event_count: int = Field(ge=0)
    successful_downstream_emit_count: int = Field(ge=0)
    dropped_event_count: int = Field(ge=0)
    downstream_failure_count: int = Field(ge=0)
    flush_failure_count: int = Field(ge=0)
    sink_failure_count: int = Field(ge=0)


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _latency_snapshot(values: tuple[float, ...]) -> LatencySnapshot:
    return LatencySnapshot(
        sample_count=len(values),
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        p99_ms=_percentile(values, 0.99),
    )


class BoundedMetrics:
    """Process-local metrics with bounded cardinality and bounded latency samples."""

    def __init__(self, *, max_latency_samples_per_stage: int = 1_024) -> None:
        if not 1 <= max_latency_samples_per_stage <= 100_000:
            raise ValueError("latency sample capacity must be between 1 and 100000")
        self._capacity = max_latency_samples_per_stage
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._latencies: dict[AuditStage, deque[float]] = {}
        self._queue_depth = 0
        self._inflight = 0
        self._peak_queue_depth = 0
        self._peak_inflight = 0

    def observe_event(self, event: AuditEvent) -> None:
        with self._lock:
            self._counters["audit_events_total"] += 1
            self._counters[f"stage.{event.stage.value}.total"] += 1
            self._counters[f"outcome.{event.outcome.value}.total"] += 1
            samples = self._latencies.setdefault(
                event.stage,
                deque(maxlen=self._capacity),
            )
            samples.append(event.duration_ms)

    def increment(self, counter: MetricCounter | str, amount: int = 1) -> None:
        try:
            normalized = MetricCounter(counter).value
        except ValueError as error:
            raise ValueError("counter is outside the bounded metrics contract") from error
        if amount < 0:
            raise ValueError("counter increments cannot be negative")
        with self._lock:
            self._counters[normalized] += amount

    def set_gauges(self, *, queue_depth: int, inflight: int) -> None:
        if queue_depth < 0 or inflight < 0:
            raise ValueError("observability gauges cannot be negative")
        with self._lock:
            self._queue_depth = queue_depth
            self._inflight = inflight
            self._peak_queue_depth = max(self._peak_queue_depth, queue_depth)
            self._peak_inflight = max(self._peak_inflight, inflight)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            latencies = {
                stage.value: _latency_snapshot(tuple(values))
                for stage, values in sorted(self._latencies.items(), key=lambda item: item[0].value)
            }
            return MetricsSnapshot(
                observed_at_utc=datetime.now(UTC),
                counters=counters,
                latency_by_stage=latencies,
                queue_depth=self._queue_depth,
                inflight=self._inflight,
                peak_queue_depth=self._peak_queue_depth,
                peak_inflight=self._peak_inflight,
            )


class FaultTolerantAuditSink:
    """Catches telemetry errors; latency isolation belongs at the wiring boundary."""

    def __init__(self, sink: AuditSink, metrics: BoundedMetrics | None = None) -> None:
        self._sink = sink
        self.metrics = metrics or BoundedMetrics()

    def emit(self, event: AuditEvent) -> bool:
        try:
            safe_event = AuditEvent.model_validate_json(event.model_dump_json())
            assert_safe_audit_payload(safe_event.model_dump(mode="python"))
        except Exception:  # noqa: BLE001 - telemetry is explicitly non-authoritative
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return False
        try:
            self._sink.emit(safe_event)
        except Exception:  # noqa: BLE001 - telemetry is explicitly non-authoritative
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            self._safe_increment(MetricCounter.AUDIT_DOWNSTREAM_FAILURES)
            return False
        # A successful-event metric must never claim an event that the
        # downstream sink rejected. Metrics remain non-authoritative even after
        # a successful write, so a metrics fault cannot change this return value.
        try:
            self.metrics.observe_event(safe_event)
        except Exception:  # noqa: BLE001 - metrics cannot acquire authority
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
        return True

    def emit_lazy(self, event_factory: Callable[[], AuditEvent]) -> bool:
        """Construct and emit without allowing telemetry data errors to escape."""

        try:
            event = event_factory()
        except Exception:  # noqa: BLE001 - event construction is non-authoritative telemetry
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return False
        return self.emit(event)

    def _safe_increment(self, counter: MetricCounter) -> None:
        try:
            self.metrics.increment(counter)
        except Exception:  # noqa: BLE001 - the failure counter is telemetry too
            pass


class AppendOnlyJsonlAuditSink:
    """Owner-only JSONL sink whose own descriptor writes with ``O_APPEND``.

    This sink is intentionally synchronous.  Production request paths must put
    :class:`BoundedAsyncAuditSink` in front of it so disk latency never becomes
    Agent latency. ``O_APPEND`` and fsync improve ordering and durability; they
    do not prevent owner/root rewrite or provide tamper evidence. Network
    filesystems are outside this contract.
    """

    def __init__(self, path: str | Path, *, fsync_each_event: bool = True) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("audit file path must be absolute")
        try:
            parent = target.parent.resolve(strict=True)
            parent_stat = target.parent.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError("audit file parent must be an existing local directory") from error
        if (
            parent != target.parent
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise RuntimeError("audit file parent must be an owner-only local directory")
        flags = (
            # Startup validates the existing tail through this same descriptor
            # before any append is accepted. O_APPEND still makes every write
            # land at EOF; pread below never changes the descriptor offset.
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        existed_before_open = target.exists()
        try:
            descriptor = os.open(target, flags, 0o600)
            opened = os.fstat(descriptor)
            current = target.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
                or opened.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
                or stat.S_ISLNK(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise RuntimeError("audit file is not a secure owner-only regular file")
            _validate_existing_audit_tail(descriptor, opened)
            validated = os.fstat(descriptor)
            current = target.stat(follow_symlinks=False)
            if (
                _audit_file_identity(validated) != _audit_file_identity(opened)
                or not stat.S_ISREG(validated.st_mode)
                or validated.st_nlink != 1
                or validated.st_uid != os.geteuid()
                or validated.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
                or stat.S_ISLNK(current.st_mode)
                or (validated.st_dev, validated.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise RuntimeError("audit file changed during startup validation")
            if not existed_before_open:
                # Persist the newly created directory entry as well as event
                # contents. Without a parent fsync, a power loss can discard
                # the filename even after the file descriptor itself was
                # fsynced successfully.
                parent_descriptor = os.open(
                    parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._fsync_each_event = fsync_each_event
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event: AuditEvent) -> None:
        payload = (
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > _MAX_AUDIT_EVENT_BYTES:
            raise ValueError("audit event exceeds the bounded JSONL record size")
        with self._lock:
            if self._closed:
                raise RuntimeError("audit sink is closed")
            remaining = memoryview(payload)
            while remaining:
                try:
                    written = os.write(self._descriptor, remaining)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("audit event append made no progress")
                remaining = remaining[written:]
            if self._fsync_each_event:
                os.fsync(self._descriptor)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._descriptor)


def _audit_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return content/security identity fields unaffected by an ordinary read."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _pread_exact(descriptor: int, *, offset: int, size: int) -> bytes:
    """Read a bounded descriptor range without changing its append offset."""

    chunks: list[bytes] = []
    remaining = size
    cursor = offset
    while remaining:
        try:
            chunk = os.pread(descriptor, remaining, cursor)
        except InterruptedError:
            continue
        if not chunk:
            raise RuntimeError("audit file changed during startup validation")
        chunks.append(chunk)
        cursor += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_existing_audit_tail(descriptor: int, metadata: os.stat_result) -> None:
    """Validate only the bounded final JSONL record before resuming appends.

    Earlier records are immutable historical input to this process. The final
    record is the crash boundary: it must be newline-terminated, fit the same
    maximum as newly emitted events, and satisfy the closed AuditEvent schema.
    No raw record content is included in refusal messages.
    """

    if metadata.st_size == 0:
        return
    read_size = min(metadata.st_size, _MAX_AUDIT_EVENT_BYTES + 1)
    tail = _pread_exact(
        descriptor,
        offset=metadata.st_size - read_size,
        size=read_size,
    )
    if not tail.endswith(b"\n"):
        raise RuntimeError("existing audit file has an incomplete final record")
    previous_newline = tail.rfind(b"\n", 0, len(tail) - 1)
    if previous_newline < 0:
        if read_size != metadata.st_size:
            raise RuntimeError("existing audit file final record exceeds the size limit")
        record = tail
    else:
        record = tail[previous_newline + 1 :]
    if len(record) > _MAX_AUDIT_EVENT_BYTES:
        raise RuntimeError("existing audit file final record exceeds the size limit")
    try:
        event = AuditEvent.model_validate_json(record[:-1])
        assert_safe_audit_payload(event.model_dump(mode="python"))
    except Exception:  # noqa: BLE001 - never surface existing raw audit bytes
        raise RuntimeError("existing audit file has an invalid final record") from None


_AUDIT_SENTINEL = object()


class BoundedAsyncAuditSink:
    """Non-authoritative, bounded request-to-sink isolation boundary."""

    def __init__(
        self,
        sink: AuditSink,
        *,
        queue_capacity: int = 2_048,
        metrics: BoundedMetrics | None = None,
        start_worker: bool = True,
        stall_timeout_seconds: float = 5.0,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not 1 <= queue_capacity <= 100_000:
            raise ValueError("audit queue capacity must be between 1 and 100000")
        if not 0 < stall_timeout_seconds <= 60:
            raise ValueError("audit stall timeout must be in (0, 60]")
        initial_monotonic = float(monotonic_clock())
        if not math.isfinite(initial_monotonic):
            raise ValueError("audit monotonic clock must return a finite value")
        self._fault_tolerant = FaultTolerantAuditSink(sink, metrics)
        self.metrics = self._fault_tolerant.metrics
        self._queue: queue.Queue[AuditEvent | object] = queue.Queue(maxsize=queue_capacity)
        self._stall_timeout_seconds = stall_timeout_seconds
        self._monotonic_clock = monotonic_clock
        self._last_monotonic = initial_monotonic
        # The queue holds at most ``queue_capacity`` events while the worker can
        # hold one additional in-flight event. This payload-free timestamp deque
        # therefore remains bounded by ``queue_capacity + 1``.
        self._pending_enqueued_at: deque[float] = deque()
        self._last_downstream_progress_at = initial_monotonic
        self._downstream_progress_count = 0
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._accepting = True
        self._closed = False
        self._started = False
        self._sentinel_enqueued = False
        self._flush_completed = False
        self._flush_succeeded: bool | None = None
        self._inflight = 0
        self._delivery_failed = False
        self._worker = threading.Thread(
            target=self._run,
            name="finance-audit-writer",
            daemon=True,
        )
        if start_worker:
            self.start()

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("audit sink is closed")
            self._start_worker_locked()

    def _start_worker_locked(self) -> None:
        if self._started:
            return
        self._worker.start()
        self._started = True

    def emit(self, event: AuditEvent) -> bool:
        try:
            detached = AuditEvent.model_validate_json(event.model_dump_json())
            assert_safe_audit_payload(detached.model_dump(mode="python"))
        except Exception:  # noqa: BLE001 - telemetry cannot alter the Agent result
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return False
        with self._state_lock:
            if not self._accepting or not self._started:
                self._safe_increment(MetricCounter.AUDIT_EVENTS_DROPPED)
                return False
            try:
                self._queue.put_nowait(detached)
            except queue.Full:
                self._safe_increment(MetricCounter.AUDIT_EVENTS_DROPPED)
                return False
            accepted_at = self._read_monotonic_locked()
            if not self._pending_enqueued_at:
                # Idle time must not make the first new event look stalled.
                self._last_downstream_progress_at = accepted_at
            self._pending_enqueued_at.append(accepted_at)
            self._safe_increment(MetricCounter.AUDIT_EVENTS_ACCEPTED)
            self._safe_gauges(queue_depth=self._queue.qsize(), inflight=0)
        return True

    def emit_lazy(self, event_factory: Callable[[], AuditEvent]) -> bool:
        try:
            return self.emit(event_factory())
        except Exception:  # noqa: BLE001 - telemetry factory is non-authoritative
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return False

    def close(self, *, timeout_seconds: float = 5.0) -> bool:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("audit shutdown timeout must be in (0, 60]")
        deadline = monotonic() + timeout_seconds
        if not self._close_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
            return False
        try:
            with self._state_lock:
                if self._closed:
                    return self._flush_succeeded is True
                self._accepting = False
                # Even a deliberately non-started sink closes through its worker
                # so an arbitrary downstream close cannot exceed this caller's
                # bounded join. The daemon may finish after a timed-out caller.
                self._start_worker_locked()
                sentinel_enqueued = self._sentinel_enqueued

            if not sentinel_enqueued:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
                    return False
                try:
                    self._queue.put(_AUDIT_SENTINEL, timeout=remaining)
                except queue.Full:
                    self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
                    return False
                with self._state_lock:
                    self._sentinel_enqueued = True
                self._safe_gauges(queue_depth=self._queue.qsize(), inflight=self._inflight)

            remaining = deadline - monotonic()
            if remaining > 0:
                self._worker.join(remaining)
            if self._worker.is_alive():
                self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
                return False
            with self._state_lock:
                flush_completed = self._flush_completed
                flush_succeeded = self._flush_succeeded is True
                if not flush_completed:
                    # A dead worker that did not execute its terminal section is
                    # irrecoverable; retain a stable failure for future callers.
                    self._flush_completed = True
                    self._flush_succeeded = False
                    self._closed = True
            if not flush_completed:
                self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
                return False
            return flush_succeeded
        finally:
            self._close_lock.release()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _AUDIT_SENTINEL:
                    self._finish_downstream()
                    return
                assert isinstance(item, AuditEvent)
                with self._state_lock:
                    self._inflight = 1
                self._safe_gauges(queue_depth=self._queue.qsize(), inflight=1)
                if not self._fault_tolerant.emit(item):
                    with self._state_lock:
                        self._delivery_failed = True
            finally:
                self._queue.task_done()
                with self._state_lock:
                    self._inflight = 0
                    if item is not _AUDIT_SENTINEL:
                        if self._pending_enqueued_at:
                            self._pending_enqueued_at.popleft()
                        else:
                            # This invariant failure is fail-closed through the
                            # existing bounded sink failure signal.
                            self._delivery_failed = True
                            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
                        self._last_downstream_progress_at = self._read_monotonic_locked()
                        self._downstream_progress_count += 1
                self._safe_gauges(queue_depth=self._queue.qsize(), inflight=0)

    def _finish_downstream(self) -> None:
        close_failed = False
        downstream_close = getattr(self._fault_tolerant._sink, "close", None)
        if callable(downstream_close):
            try:
                downstream_close()
            except Exception:  # noqa: BLE001 - shutdown telemetry remains isolated
                close_failed = True
                self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
        with self._state_lock:
            dropped_event_count = self.metrics.snapshot().counters.get(
                MetricCounter.AUDIT_EVENTS_DROPPED.value,
                0,
            )
            flush_succeeded = (
                not close_failed and not self._delivery_failed and dropped_event_count == 0
            )
            if not flush_succeeded:
                self._safe_increment(MetricCounter.AUDIT_FLUSH_FAILURES)
            self._flush_completed = True
            self._flush_succeeded = flush_succeeded
            self._closed = True
            self._accepting = False

    def snapshot(self) -> AuditSinkSnapshot:
        """Return bounded durability state without exposing any event payload."""

        with self._state_lock:
            accepting = self._accepting
            started = self._started
            closed = self._closed
            sentinel_enqueued = self._sentinel_enqueued
            flush_completed = self._flush_completed
            flush_succeeded = self._flush_succeeded
            inflight = self._inflight
            worker_alive = self._worker.is_alive() if self._started else False
            downstream_progress_count = self._downstream_progress_count
            observed_at_monotonic = self._read_monotonic_locked()
            pending_event_count = len(self._pending_enqueued_at)
            if pending_event_count:
                oldest_pending_age_seconds = max(
                    0.0,
                    observed_at_monotonic - self._pending_enqueued_at[0],
                )
                no_progress_age_seconds = max(
                    0.0,
                    observed_at_monotonic - self._last_downstream_progress_at,
                )
            else:
                oldest_pending_age_seconds = None
                no_progress_age_seconds = None
            # State-changing paths use the same state -> metrics lock order.
            # Keeping both snapshots in one critical section avoids reporting a
            # completed failed flush before its bounded failure counter exists.
            metrics = self.metrics.snapshot()
            counters = metrics.counters
            queue_depth = self._queue.qsize()
        return AuditSinkSnapshot(
            observed_at_utc=datetime.now(UTC),
            accepting=accepting,
            started=started,
            closed=closed,
            worker_alive=worker_alive,
            sentinel_enqueued=sentinel_enqueued,
            flush_completed=flush_completed,
            flush_succeeded=flush_succeeded,
            queue_depth=queue_depth,
            inflight=inflight,
            pending_event_count=pending_event_count,
            oldest_pending_age_seconds=oldest_pending_age_seconds,
            no_progress_age_seconds=no_progress_age_seconds,
            stall_timeout_seconds=self._stall_timeout_seconds,
            downstream_progress_count=downstream_progress_count,
            accepted_event_count=counters.get(MetricCounter.AUDIT_EVENTS_ACCEPTED.value, 0),
            successful_downstream_emit_count=counters.get("audit_events_total", 0),
            dropped_event_count=counters.get(MetricCounter.AUDIT_EVENTS_DROPPED.value, 0),
            downstream_failure_count=counters.get(
                MetricCounter.AUDIT_DOWNSTREAM_FAILURES.value,
                0,
            ),
            flush_failure_count=counters.get(MetricCounter.AUDIT_FLUSH_FAILURES.value, 0),
            sink_failure_count=counters.get(MetricCounter.AUDIT_SINK_FAILURES.value, 0),
        )

    def _read_monotonic_locked(self) -> float:
        """Read an injected monotonic clock while tolerating backward movement."""

        try:
            observed = float(self._monotonic_clock())
        except Exception:  # noqa: BLE001 - telemetry clocks cannot gain authority
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return self._last_monotonic
        if not math.isfinite(observed):
            # A runtime clock contract failure must not alter the Agent result.
            # Freezing time is conservative for the current emit; the sink
            # failure counter makes readiness fail closed.
            self._safe_increment(MetricCounter.AUDIT_SINK_FAILURES)
            return self._last_monotonic
        self._last_monotonic = max(self._last_monotonic, observed)
        return self._last_monotonic

    def _safe_increment(self, counter: MetricCounter) -> None:
        try:
            self.metrics.increment(counter)
        except Exception:  # noqa: BLE001 - metrics cannot acquire authority
            pass

    def _safe_gauges(self, *, queue_depth: int, inflight: int) -> None:
        try:
            self.metrics.set_gauges(queue_depth=queue_depth, inflight=inflight)
        except Exception:  # noqa: BLE001 - metrics cannot acquire authority
            pass


@dataclass(slots=True)
class _AuditInvocationState:
    """Mutable state shared only by recorder views for one server invocation."""

    invocation_id: str = field(default_factory=lambda: str(uuid4()))
    event_sequence: int = 0
    emit_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True, slots=True)
class RequestAuditRecorder:
    """Short-lived request context; only redacted values leave this object."""

    request_id: str = field(repr=False)
    question: str = field(repr=False)
    sink: BoundedAsyncAuditSink
    agent_release_id: str | None = None
    agent_release_manifest_sha256: str | None = None
    deployment_binding_sha256: str | None = None
    release_context_sha256: str | None = None
    _invocation: _AuditInvocationState = field(
        default_factory=_AuditInvocationState,
        repr=False,
        compare=False,
    )

    @property
    def invocation_id_sha256(self) -> str:
        """Expose only the one-way correlation identifier, never the UUID."""

        return sha256_text(self._invocation.invocation_id)

    @property
    def last_event_sequence(self) -> int:
        with self._invocation.emit_lock:
            return self._invocation.event_sequence

    def with_request(self, *, request_id: str, question: str) -> RequestAuditRecorder:
        """Enrich a middleware recorder while retaining its invocation chain.

        The returned frozen view shares the same private UUID and sequence lock.
        This lets middleware emit a payload-free START event, then bind an
        enriched recorder before executor ``copy_context`` without splitting the
        audit chain.
        """

        return replace(self, request_id=request_id, question=question)

    def emit(
        self,
        *,
        stage: AuditStage,
        outcome: AuditOutcome,
        reason_code: str,
        duration_ms: float,
        **fields: object,
    ) -> bool:
        # Hold the per-invocation lock through the bounded enqueue. Concurrent
        # cross-family workers therefore cannot reorder sequence allocation and
        # queue insertion; a rejected event leaves an intentional sequence gap.
        with self._invocation.emit_lock:
            self._invocation.event_sequence += 1
            event_sequence = self._invocation.event_sequence
            return self.sink.emit_lazy(
                lambda: AuditEvent.redacted(
                    stage=stage,
                    outcome=outcome,
                    reason_code=reason_code,
                    duration_ms=duration_ms,
                    request_id=self.request_id,
                    question=self.question,
                    invocation_id=self._invocation.invocation_id,
                    event_sequence=event_sequence,
                    agent_release_id=self.agent_release_id,
                    agent_release_manifest_sha256=self.agent_release_manifest_sha256,
                    deployment_binding_sha256=self.deployment_binding_sha256,
                    release_context_sha256=self.release_context_sha256,
                    **fields,
                )
            )


_CURRENT_AUDIT_RECORDER: ContextVar[RequestAuditRecorder | None] = ContextVar(
    "finance_agent_request_audit_recorder",
    default=None,
)


@contextmanager
def bind_request_audit(recorder: RequestAuditRecorder):
    token: Token[RequestAuditRecorder | None] = _CURRENT_AUDIT_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT_AUDIT_RECORDER.reset(token)


def current_request_audit() -> RequestAuditRecorder | None:
    return _CURRENT_AUDIT_RECORDER.get()


def observe_call[ResultT](
    operation: Callable[[], ResultT],
    *,
    sink: FaultTolerantAuditSink,
    event_factory: Callable[[AuditOutcome, float], AuditEvent],
    success_outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
) -> ResultT:
    """Observe an operation without replacing its return value or exception."""

    started = perf_counter()
    try:
        result = operation()
    except Exception:
        sink.emit_lazy(
            lambda: event_factory(
                AuditOutcome.FAILED,
                (perf_counter() - started) * 1000,
            )
        )
        raise
    sink.emit_lazy(
        lambda: event_factory(
            success_outcome,
            (perf_counter() - started) * 1000,
        )
    )
    return result


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_identifiers(values: Iterable[str], *, kind: str) -> tuple[str, ...]:
    identifiers = tuple(values)
    for value in identifiers:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"{kind} identifiers must be non-empty strings up to 512 characters")
    return tuple(sha256_text(value) for value in identifiers)


def assert_safe_audit_payload(payload: Mapping[str, object]) -> None:
    """Fail closed when a future adapter tries to add non-allowlisted telemetry."""

    allowed = set(AuditEvent.model_fields)
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError(f"audit payload contains forbidden fields: {sorted(unexpected)}")
    serialized = repr(dict(payload))
    if _TOKEN_PATTERN.search(serialized) or _PATH_PATTERN.search(serialized):
        raise ValueError("audit payload contains secret-like or path-like material")
    AuditEvent.model_validate(dict(payload))


__all__ = [
    "AppendOnlyJsonlAuditSink",
    "AuditEvent",
    "AuditOutcome",
    "AuditSink",
    "AuditSinkSnapshot",
    "AuditStage",
    "BoundedAsyncAuditSink",
    "BoundedMetrics",
    "FaultTolerantAuditSink",
    "InMemoryAuditSink",
    "LatencySnapshot",
    "MetricCounter",
    "MetricsSnapshot",
    "NullAuditSink",
    "RequestAuditRecorder",
    "assert_safe_audit_payload",
    "bind_request_audit",
    "current_request_audit",
    "observe_call",
    "sha256_text",
]
