from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.planning_policy import (
    PlanningPath,
    PlanningSemanticIssue,
    PlanningTrace,
)
from finance_agent_core.agent.safety import normalize_user_question
from finance_agent_core.config import FieldRegistry, load_field_registry
from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    FaultTolerantAuditSink,
    RequestAuditRecorder,
    current_request_audit,
)
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    packaged_field_registry_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TOKEN = re.compile(r"[0-9a-zA-Z가-힣]+")


class SchemaShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaShadowMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"


class SchemaLinkStatus(StrEnum):
    FOUND = "found"
    CONFLICT = "conflict"
    ABSTAIN = "abstain"
    DISABLED = "disabled"


class SchemaFieldCapability(StrEnum):
    QUERYABLE = "queryable"
    SELECTABLE = "selectable"
    SORTABLE = "sortable"
    AGGREGATABLE = "aggregatable"
    COMPARABLE = "comparable"


class SchemaLinkCandidateV2(SchemaShadowModel):
    product_family: ProductFamily
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    lexical_rank: int | None = Field(default=None, ge=1, le=20)
    dense_score: float | None = Field(default=None, ge=-1.000001, le=1.000001)
    fused_rank: int = Field(ge=1, le=20)
    capabilities: tuple[SchemaFieldCapability, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def require_unique_capabilities(self) -> SchemaLinkCandidateV2:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("schema field capabilities must be unique")
        return self


class SchemaLinkDecision(SchemaShadowModel):
    """Non-authoritative Schema Dense observation.

    This DTO deliberately contains only hashes, canonical IDs, bounded scores,
    and frozen manifest identities. It cannot carry the raw question or raw
    unresolved span and cannot be compiled into SQL.
    """

    schema_version: Literal["2.0"] = "2.0"
    mode: SchemaShadowMode
    status: SchemaLinkStatus
    reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    unresolved_span_sha256: str = Field(pattern=_SHA256_PATTERN)
    product_family: ProductFamily | None = None
    intent: InteractionIntent | None = None
    candidates: tuple[SchemaLinkCandidateV2, ...] = Field(default=(), max_length=20)
    margin: float | None = Field(default=None, ge=0, le=2.000002)
    field_registry_schema_version: str | None = Field(default=None, max_length=32)
    field_registry_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    index_manifest_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    provider_manifest_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_snapshot_manifest_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_state(self) -> SchemaLinkDecision:
        ranks = [candidate.fused_rank for candidate in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("fused candidate ranks must be contiguous")
        if self.status is SchemaLinkStatus.DISABLED:
            if self.mode is not SchemaShadowMode.OFF and self.reason_code == "shadow_mode_off":
                raise ValueError("off reason requires off mode")
            if self.candidates or self.margin is not None:
                raise ValueError("disabled schema link cannot contain candidates or a margin")
        elif self.mode is not SchemaShadowMode.SHADOW:
            raise ValueError("active schema-link observations require shadow mode")
        if self.status is SchemaLinkStatus.FOUND and not self.candidates:
            raise ValueError("found schema link requires a candidate")
        if self.candidates and self.product_family is None:
            raise ValueError("schema candidates require a product family")
        if self.candidates and any(
            item.product_family is not self.product_family for item in self.candidates
        ):
            raise ValueError("schema candidates must stay inside the approved family")
        manifest_values = (
            self.field_registry_schema_version,
            self.field_registry_sha256,
            self.index_manifest_id,
            self.provider_manifest_id,
            self.model_snapshot_manifest_id,
        )
        if self.candidates and any(value is None for value in manifest_values):
            raise ValueError("schema candidates require all registry and manifest identities")
        return self


class SchemaShadowSettings(SchemaShadowModel):
    mode: SchemaShadowMode = SchemaShadowMode.OFF
    top_k: int = Field(default=5, ge=1, le=20)
    dense_min_score: float = Field(default=0.55, ge=-1, le=1)
    minimum_margin: float = Field(default=0.05, ge=0, le=2)
    max_inflight: Literal[1] = 1


class SchemaShadowQueueSettings(SchemaShadowModel):
    worker_count: Literal[1] = 1
    queue_capacity: int = Field(default=8, ge=1, le=128)
    stall_timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class SchemaShadowRuntimeSnapshot(SchemaShadowModel):
    """Payload-free process-local state for the optional Shadow worker."""

    schema_version: Literal["1.0"] = "1.0"
    enabled: bool
    mode: SchemaShadowMode
    started: bool
    accepting: bool
    worker_alive: bool
    shutdown_started: bool
    shutdown_completed: bool
    shutdown_succeeded: bool | None
    queue_depth: int = Field(ge=0)
    inflight: int = Field(ge=0, le=1)
    peak_queue_depth: int = Field(ge=0)
    peak_inflight: int = Field(ge=0, le=1)
    pending_count: int = Field(ge=0)
    oldest_pending_age_seconds: float | None = Field(default=None, ge=0)
    no_progress_age_seconds: float | None = Field(default=None, ge=0)
    stall_timeout_seconds: float = Field(gt=0, le=60)
    stalled: bool
    accepted_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    queue_drop_count: int = Field(ge=0)
    operational_failure_count: int = Field(ge=0)
    correlation_failure_count: int = Field(ge=0)
    audit_emit_attempt_count: int = Field(ge=0)
    audit_emit_success_count: int = Field(ge=0)
    audit_emit_failure_count: int = Field(ge=0)


class SchemaLinkShadowObserver(Protocol):
    """Request-path seam: submission must be bounded and non-blocking."""

    def submit(self, trace: PlanningTrace) -> bool: ...


class SchemaEmbeddingCandidateEvidence(Protocol):
    model_id: str
    revision: str


class SchemaEmbeddingArtifactEvidence(Protocol):
    mode: str
    status: str
    candidate: SchemaEmbeddingCandidateEvidence
    snapshot_file_manifest_sha256: str
    manifest_file_sha256: str


ArtifactPrecondition = Callable[[], SchemaEmbeddingArtifactEvidence]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return "".join(_TOKEN.findall(value.casefold()))


def _capabilities(
    registry: FieldRegistry, family: ProductFamily, field_id: str
) -> tuple[SchemaFieldCapability, ...]:
    definition = registry.require_field(field_id, [family.value])
    ordered = (
        (SchemaFieldCapability.QUERYABLE, definition.queryable),
        (SchemaFieldCapability.SELECTABLE, definition.selectable),
        (SchemaFieldCapability.SORTABLE, definition.sortable),
        (SchemaFieldCapability.AGGREGATABLE, definition.aggregatable),
        (SchemaFieldCapability.COMPARABLE, definition.comparable),
    )
    return tuple(capability for capability, enabled in ordered if enabled)


def _intent_allows(
    intent: InteractionIntent,
    capabilities: tuple[SchemaFieldCapability, ...],
) -> bool:
    available = set(capabilities)
    required_any = {
        InteractionIntent.SEARCH: {
            SchemaFieldCapability.QUERYABLE,
            SchemaFieldCapability.SELECTABLE,
            SchemaFieldCapability.SORTABLE,
        },
        InteractionIntent.DETAIL: {SchemaFieldCapability.SELECTABLE},
        InteractionIntent.COMPARE: {SchemaFieldCapability.COMPARABLE},
        InteractionIntent.AGGREGATE: {SchemaFieldCapability.AGGREGATABLE},
        InteractionIntent.EXPLAIN: {SchemaFieldCapability.SELECTABLE},
    }
    return bool(available & required_any.get(intent, set()))


def _eligible_fields(
    registry: FieldRegistry,
    family: ProductFamily,
    intent: InteractionIntent,
) -> dict[str, tuple[SchemaFieldCapability, ...]]:
    eligible: dict[str, tuple[SchemaFieldCapability, ...]] = {}
    for field_id, definition in registry.fields.items():
        if family.value not in definition.datasets:
            continue
        capabilities = _capabilities(registry, family, field_id)
        if capabilities and _intent_allows(intent, capabilities):
            eligible[field_id] = capabilities
    return eligible


def _lexical_ranking(
    span: str,
    registry: FieldRegistry,
    family: ProductFamily,
    eligible: set[str],
) -> tuple[list[str], bool]:
    query = _normalized(span)
    if not query:
        return [], False
    scored: list[tuple[int, int, str]] = []
    for field_id in eligible:
        definition = registry.require_field(field_id, [family.value])
        terms = (field_id, definition.label, *definition.aliases)
        normalized_terms = tuple(filter(None, (_normalized(term) for term in terms)))
        exact = any(query == term for term in normalized_terms)
        contained = max(
            (
                min(len(query), len(term))
                for term in normalized_terms
                if min(len(query), len(term)) >= 2 and (query in term or term in query)
            ),
            default=0,
        )
        if exact:
            exact_length = max(len(term) for term in normalized_terms if query == term)
            scored.append((2, exact_length, field_id))
        elif contained:
            scored.append((1, contained, field_id))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    if not scored:
        return [], False
    top_key = scored[0][:2]
    ambiguous_top = sum(item[:2] == top_key for item in scored) > 1
    return [field_id for _, _, field_id in scored[:20]], ambiguous_top


def _manifest_context(
    index: DenseSchemaIndex,
    evidence: SchemaEmbeddingArtifactEvidence,
) -> dict[str, str]:
    provider = index.manifest.provider
    if evidence.mode != "shadow" or evidence.status != "verified_prerequisite":
        raise ValueError("schema embedding artifact is not approved for shadow")
    if (
        provider.provider_kind != "frozen_model"
        or evidence.candidate.model_id != provider.model_id
        or evidence.candidate.revision != provider.model_revision
        or getattr(index.provider, "artifact_gate_evidence", None) != evidence
    ):
        raise ValueError("schema embedding artifact and index provider differ")
    if index.manifest.field_registry_sha256 != packaged_field_registry_sha256():
        raise ValueError("schema embedding index and packaged field registry differ")
    return {
        "field_registry_schema_version": index.manifest.field_registry_schema_version,
        "field_registry_sha256": index.manifest.field_registry_sha256,
        "index_manifest_id": _canonical_sha256(index.manifest.model_dump(mode="json")),
        "provider_manifest_id": _canonical_sha256(provider.model_dump(mode="json")),
        "model_snapshot_manifest_id": evidence.snapshot_file_manifest_sha256,
    }


class HybridSchemaLinkShadow:
    """Exact-cosine + lexical-first Schema Linker with no execution authority."""

    def __init__(
        self,
        *,
        settings: SchemaShadowSettings | None = None,
        index: DenseSchemaIndex | None = None,
        artifact_precondition: ArtifactPrecondition | None = None,
        registry: FieldRegistry | None = None,
        capability_matrix: CapabilityMatrix | None = None,
        audit_sink: FaultTolerantAuditSink | BoundedAsyncAuditSink | None = None,
    ) -> None:
        self.settings = settings or SchemaShadowSettings()
        self.index = index
        self.artifact_precondition = artifact_precondition
        self.registry = registry or load_field_registry()
        self.capability_matrix = capability_matrix or load_capability_matrix()
        self.audit_sink = audit_sink
        self._inflight = threading.BoundedSemaphore(self.settings.max_inflight)

    @property
    def expected_audit_sink(self) -> BoundedAsyncAuditSink | None:
        """Return the server-owned sink that requires request correlation."""

        if type(self.audit_sink) is BoundedAsyncAuditSink:
            return self.audit_sink
        return None

    @staticmethod
    def audit_recorder_matches_trace(
        recorder: RequestAuditRecorder | None,
        trace: PlanningTrace,
        expected_sink: BoundedAsyncAuditSink,
    ) -> bool:
        """Bind a queued task to its exact request, not merely a shared sink."""

        draft = trace.route_decision.draft
        return bool(
            recorder is not None
            and recorder.sink is expected_sink
            and recorder.request_id == draft.request_id
            and normalize_user_question(recorder.question) == draft.question
        )

    def should_enqueue(self, trace: PlanningTrace) -> bool:
        """Cheap request-thread guard; it never verifies files or calls a model."""

        planning = trace.planning_decision
        route = trace.route_decision
        return bool(
            self.settings.mode is SchemaShadowMode.SHADOW
            and route.disposition is RouteDisposition.EXECUTE
            and planning.path is PlanningPath.SCHEMA_LINK_SHADOW
            and planning.semantic_issue is PlanningSemanticIssue.SCHEMA_LINK_GAP
            and len(planning.product_families) == 1
            and planning.unresolved_spans
            and route.draft.intent not in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
        )

    def observe(
        self,
        trace: PlanningTrace,
        *,
        audit_recorder: RequestAuditRecorder | None = None,
    ) -> tuple[SchemaLinkDecision, ...]:
        decisions, _ = self.observe_with_audit(trace, audit_recorder=audit_recorder)
        return decisions

    def observe_with_audit(
        self,
        trace: PlanningTrace,
        *,
        audit_recorder: RequestAuditRecorder | None = None,
    ) -> tuple[tuple[SchemaLinkDecision, ...], bool | None]:
        started = perf_counter()
        spans = trace.planning_decision.unresolved_spans or ("disabled",)
        expected_sink = self.expected_audit_sink
        if expected_sink is not None and not self.audit_recorder_matches_trace(
            audit_recorder,
            trace,
            expected_sink,
        ):
            return (
                tuple(
                    self._abstain(span, trace, "shadow_audit_correlation_failure") for span in spans
                ),
                None,
            )
        try:
            decisions = self._observe_safely(trace)
        except Exception:  # noqa: BLE001 - a shadow observer can never alter the Agent result
            decisions = tuple(
                self._abstain(span, trace, "shadow_internal_failure") for span in spans
            )
        emitted = self._emit_audit(
            trace,
            decisions,
            (perf_counter() - started) * 1000,
            audit_recorder=audit_recorder,
        )
        return decisions, emitted

    def _observe_safely(self, trace: PlanningTrace) -> tuple[SchemaLinkDecision, ...]:
        planning = trace.planning_decision
        route = trace.route_decision
        spans = planning.unresolved_spans or ("disabled",)
        if self.settings.mode is SchemaShadowMode.OFF:
            return tuple(self._disabled(span, trace, "shadow_mode_off") for span in spans)
        if (
            route.disposition is not RouteDisposition.EXECUTE
            or planning.path is not PlanningPath.SCHEMA_LINK_SHADOW
            or planning.semantic_issue is not PlanningSemanticIssue.SCHEMA_LINK_GAP
            or len(planning.product_families) != 1
            or not planning.unresolved_spans
            or route.draft.intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
        ):
            return tuple(self._disabled(span, trace, "shadow_not_eligible") for span in spans)

        family = planning.product_families[0]
        try:
            self.registry.require_dataset(family.value)
            capability = self.capability_matrix.require(family, route.draft.intent)
            if capability.status != "executable":
                raise ValueError("family intent is not executable")
        except Exception:
            return tuple(
                self._abstain(span, trace, "shadow_family_not_approved")
                for span in planning.unresolved_spans
            )

        if self.index is None or self.artifact_precondition is None:
            return tuple(
                self._abstain(span, trace, "shadow_artifact_missing")
                for span in planning.unresolved_spans
            )
        try:
            evidence = self.artifact_precondition()
            manifest_context = _manifest_context(self.index, evidence)
        except Exception:
            return tuple(
                self._abstain(span, trace, "shadow_artifact_unverified")
                for span in planning.unresolved_spans
            )

        if not self._inflight.acquire(blocking=False):
            return tuple(
                self._abstain(span, trace, "shadow_inflight_busy", manifest_context)
                for span in planning.unresolved_spans
            )
        try:
            return tuple(
                self._link_span(
                    span,
                    family=family,
                    intent=route.draft.intent,
                    manifest_context=manifest_context,
                )
                for span in planning.unresolved_spans
            )
        finally:
            self._inflight.release()

    def _link_span(
        self,
        span: str,
        *,
        family: ProductFamily,
        intent: InteractionIntent,
        manifest_context: dict[str, str],
    ) -> SchemaLinkDecision:
        assert self.index is not None
        eligible = _eligible_fields(self.registry, family, intent)
        if not eligible:
            return self._decision(
                span,
                status=SchemaLinkStatus.ABSTAIN,
                reason_code="shadow_no_capable_fields",
                family=family,
                intent=intent,
                manifest_context=manifest_context,
            )
        try:
            dense = self.index.search(span, family, top_k=20)
        except Exception:
            return self._decision(
                span,
                status=SchemaLinkStatus.ABSTAIN,
                reason_code="shadow_embedding_failure",
                family=family,
                intent=intent,
                manifest_context=manifest_context,
            )
        dense = [
            candidate
            for candidate in dense
            if candidate.product_family is family and candidate.field_id in eligible
        ]
        if not dense:
            return self._decision(
                span,
                status=SchemaLinkStatus.ABSTAIN,
                reason_code="shadow_no_registry_candidate",
                family=family,
                intent=intent,
                manifest_context=manifest_context,
            )

        lexical, lexical_tie = _lexical_ranking(
            span,
            self.registry,
            family,
            set(eligible),
        )
        dense_by_field = {candidate.field_id: candidate for candidate in dense}
        lexical_in_dense = [field_id for field_id in lexical if field_id in dense_by_field]
        fused_fields = [
            *lexical_in_dense,
            *(
                candidate.field_id
                for candidate in dense
                if candidate.field_id not in lexical_in_dense
            ),
        ][: self.settings.top_k]
        lexical_rank = {field_id: rank for rank, field_id in enumerate(lexical, start=1)}
        candidates = tuple(
            SchemaLinkCandidateV2(
                product_family=family,
                field_id=field_id,
                lexical_rank=lexical_rank.get(field_id),
                dense_score=dense_by_field[field_id].score,
                fused_rank=rank,
                capabilities=eligible[field_id],
            )
            for rank, field_id in enumerate(fused_fields, start=1)
        )
        top_dense = dense[0]
        second_score = dense[1].score if len(dense) > 1 else -1.0
        margin = round(max(0.0, top_dense.score - second_score), 9)

        lexical_top = lexical_in_dense[0] if lexical_in_dense else None
        if lexical_tie or (lexical_top is not None and lexical_top != top_dense.field_id):
            status = SchemaLinkStatus.CONFLICT
            reason_code = "shadow_lexical_dense_conflict"
        elif top_dense.score < self.settings.dense_min_score:
            status = SchemaLinkStatus.ABSTAIN
            reason_code = "shadow_low_dense_score"
        elif margin < self.settings.minimum_margin:
            status = SchemaLinkStatus.ABSTAIN
            reason_code = "shadow_low_margin"
        else:
            status = SchemaLinkStatus.FOUND
            reason_code = "shadow_candidate_found"
        return self._decision(
            span,
            status=status,
            reason_code=reason_code,
            family=family,
            intent=intent,
            candidates=candidates,
            margin=margin,
            manifest_context=manifest_context,
        )

    def _decision(
        self,
        span: str,
        *,
        status: SchemaLinkStatus,
        reason_code: str,
        family: ProductFamily | None = None,
        intent: InteractionIntent | None = None,
        candidates: Sequence[SchemaLinkCandidateV2] = (),
        margin: float | None = None,
        manifest_context: dict[str, str] | None = None,
    ) -> SchemaLinkDecision:
        return SchemaLinkDecision(
            mode=self.settings.mode,
            status=status,
            reason_code=reason_code,
            unresolved_span_sha256=_text_sha256(span),
            product_family=family,
            intent=intent,
            candidates=tuple(candidates),
            margin=margin,
            **(manifest_context or {}),
        )

    def _disabled(
        self,
        span: str,
        trace: PlanningTrace,
        reason_code: str,
    ) -> SchemaLinkDecision:
        family = (
            trace.planning_decision.product_families[0]
            if len(trace.planning_decision.product_families) == 1
            else None
        )
        return self._decision(
            span,
            status=SchemaLinkStatus.DISABLED,
            reason_code=reason_code,
            family=family,
            intent=trace.route_decision.draft.intent,
        )

    def _abstain(
        self,
        span: str,
        trace: PlanningTrace,
        reason_code: str,
        manifest_context: dict[str, str] | None = None,
    ) -> SchemaLinkDecision:
        family = (
            trace.planning_decision.product_families[0]
            if len(trace.planning_decision.product_families) == 1
            else None
        )
        return self._decision(
            span,
            status=SchemaLinkStatus.ABSTAIN,
            reason_code=reason_code,
            family=family,
            intent=trace.route_decision.draft.intent,
            manifest_context=manifest_context,
        )

    def _emit_audit(
        self,
        trace: PlanningTrace,
        decisions: tuple[SchemaLinkDecision, ...],
        duration_ms: float,
        *,
        audit_recorder: RequestAuditRecorder | None,
    ) -> bool | None:
        if self.audit_sink is None:
            return None
        priority = {
            SchemaLinkStatus.CONFLICT: 3,
            SchemaLinkStatus.ABSTAIN: 2,
            SchemaLinkStatus.FOUND: 1,
            SchemaLinkStatus.DISABLED: 0,
        }
        selected = max(decisions, key=lambda decision: priority[decision.status])
        outcome = {
            SchemaLinkStatus.FOUND: AuditOutcome.SUCCEEDED,
            SchemaLinkStatus.CONFLICT: AuditOutcome.BLOCKED,
            SchemaLinkStatus.ABSTAIN: AuditOutcome.BLOCKED,
            SchemaLinkStatus.DISABLED: AuditOutcome.BLOCKED,
        }[selected.status]
        model_revision = self.index.manifest.provider.model_revision if self.index else None
        fields = {
            "model_revision": model_revision,
            "model_snapshot_manifest_sha256": selected.model_snapshot_manifest_id,
            "index_manifest_sha256": selected.index_manifest_id,
            "route_disposition": trace.route_decision.disposition,
            "interaction_intent": trace.route_decision.draft.intent,
            "product_families": trace.planning_decision.product_families,
            "shadow_candidate_count": sum(len(item.candidates) for item in decisions),
        }
        if audit_recorder is not None:
            return audit_recorder.emit(
                stage=AuditStage.SCHEMA_LINK_SHADOW,
                outcome=outcome,
                reason_code=selected.reason_code,
                duration_ms=duration_ms,
                **fields,
            )
        if type(self.audit_sink) is BoundedAsyncAuditSink:
            return False
        return self.audit_sink.emit_lazy(
            lambda: AuditEvent.redacted(
                stage=AuditStage.SCHEMA_LINK_SHADOW,
                outcome=outcome,
                reason_code=selected.reason_code,
                duration_ms=duration_ms,
                request_id=trace.route_decision.draft.request_id,
                question=trace.route_decision.draft.question,
                **fields,
            )
        )


@dataclass(frozen=True, slots=True)
class _QueuedShadowTask:
    trace: PlanningTrace
    audit_recorder: RequestAuditRecorder | None
    enqueued_at: float


_OPERATIONAL_REASON_CODES = frozenset(
    {
        "shadow_artifact_missing",
        "shadow_artifact_unverified",
        "shadow_embedding_failure",
        "shadow_inflight_busy",
        "shadow_internal_failure",
    }
)


class AsyncSchemaLinkShadowObserver:
    """Single-worker, bounded Shadow adapter for the Agent request seam.

    `submit` never waits for an embedding. Queue overflow is fail-closed and
    observable; it cannot apply backpressure to the user-facing request.
    Lifecycle owners must call `shutdown` before process teardown.
    """

    def __init__(
        self,
        worker: HybridSchemaLinkShadow,
        *,
        settings: SchemaShadowQueueSettings | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if type(worker) is not HybridSchemaLinkShadow:
            raise TypeError("shadow worker must be the trusted HybridSchemaLinkShadow")
        self._worker = worker
        self.settings = settings or SchemaShadowQueueSettings()
        self._clock = monotonic_clock
        initial = float(self._clock())
        if not math.isfinite(initial) or initial < 0:
            raise ValueError("shadow monotonic clock must be finite and non-negative")
        self._last_clock = initial
        self._queue: queue.Queue[_QueuedShadowTask] = queue.Queue(
            maxsize=self.settings.queue_capacity
        )
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._shutdown_lock = threading.Lock()
        self._accepting = True
        self._thread: threading.Thread | None = None
        self._unfinished = 0
        self._inflight_count = 0
        self._pending_enqueued_at: deque[float] = deque()
        self._last_progress_at = initial
        self._peak_queue_depth = 0
        self._peak_inflight = 0
        self._accepted_count = 0
        self._completed_count = 0
        self._queue_drop_count = 0
        self._operational_failure_count = 0
        self._correlation_failure_count = 0
        self._audit_emit_attempt_count = 0
        self._audit_emit_success_count = 0
        self._audit_emit_failure_count = 0
        self._shutdown_started = False
        self._shutdown_completed = False
        self._shutdown_succeeded: bool | None = None

    @property
    def enabled(self) -> bool:
        return self._worker.settings.mode is SchemaShadowMode.SHADOW

    @property
    def expected_audit_sink(self) -> BoundedAsyncAuditSink | None:
        return self._worker.expected_audit_sink

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _ensure_started_locked(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run,
            name="schema-link-shadow-worker",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def submit(self, trace: PlanningTrace) -> bool:
        if not self._worker.should_enqueue(trace):
            return False
        recorder = current_request_audit()
        expected_sink = self._worker.expected_audit_sink
        with self._condition:
            if not self._accepting:
                return False
            if expected_sink is not None and not self._worker.audit_recorder_matches_trace(
                recorder,
                trace,
                expected_sink,
            ):
                self._correlation_failure_count += 1
                return False
            if self._thread is not None and not self._thread.is_alive():
                self._operational_failure_count += 1
                return False
            self._ensure_started_locked()
            accepted_at = self._read_clock_locked()
            task = _QueuedShadowTask(
                trace=trace,
                audit_recorder=recorder,
                enqueued_at=accepted_at,
            )
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                self._queue_drop_count += 1
                self._condition.notify_all()
                return False
            else:
                self._unfinished += 1
                self._accepted_count += 1
                if not self._pending_enqueued_at:
                    self._last_progress_at = accepted_at
                self._pending_enqueued_at.append(accepted_at)
            self._update_peaks_locked()
            self._condition.notify_all()
        return True

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                task = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._condition:
                self._inflight_count = 1
                self._update_peaks_locked()
            operational = False
            audit_attempted = False
            emitted: bool | None = None
            try:
                decisions, emitted = self._worker.observe_with_audit(
                    task.trace,
                    audit_recorder=task.audit_recorder,
                )
                operational = any(
                    decision.reason_code in _OPERATIONAL_REASON_CODES for decision in decisions
                )
                audit_attempted = self._worker.audit_sink is not None
            except Exception:
                # HybridSchemaLinkShadow already isolates failures, and this
                # second boundary protects the process from injected workers.
                operational = True
                audit_attempted = self._worker.audit_sink is not None
                emitted = False if audit_attempted else None
            finally:
                self._queue.task_done()
                with self._condition:
                    self._inflight_count = 0
                    self._unfinished -= 1
                    self._completed_count += 1
                    if operational:
                        self._operational_failure_count += 1
                    if audit_attempted:
                        self._audit_emit_attempt_count += 1
                        if emitted:
                            self._audit_emit_success_count += 1
                        else:
                            self._audit_emit_failure_count += 1
                    if self._pending_enqueued_at:
                        self._pending_enqueued_at.popleft()
                    self._last_progress_at = self._read_clock_locked()
                    self._condition.notify_all()

    def _update_peaks_locked(self) -> None:
        self._peak_queue_depth = max(self._peak_queue_depth, self._queue.qsize())
        self._peak_inflight = max(self._peak_inflight, self._inflight_count)

    def _read_clock_locked(self) -> float:
        try:
            observed = float(self._clock())
        except Exception:  # noqa: BLE001 - telemetry cannot alter the Agent result
            self._operational_failure_count += 1
            return self._last_clock
        if not math.isfinite(observed):
            self._operational_failure_count += 1
            return self._last_clock
        self._last_clock = max(self._last_clock, observed)
        return self._last_clock

    def snapshot(self) -> SchemaShadowRuntimeSnapshot:
        with self._condition:
            now = self._read_clock_locked()
            pending = self._unfinished
            oldest_age = (
                max(0.0, now - self._pending_enqueued_at[0]) if self._pending_enqueued_at else None
            )
            no_progress_age = max(0.0, now - self._last_progress_at) if pending else None
            stalled = bool(
                pending
                and no_progress_age is not None
                and no_progress_age >= self.settings.stall_timeout_seconds
            )
            thread = self._thread
            return SchemaShadowRuntimeSnapshot(
                enabled=self.enabled,
                mode=self._worker.settings.mode,
                started=thread is not None,
                accepting=self._accepting,
                worker_alive=bool(thread is not None and thread.is_alive()),
                shutdown_started=self._shutdown_started,
                shutdown_completed=self._shutdown_completed,
                shutdown_succeeded=self._shutdown_succeeded,
                queue_depth=self._queue.qsize(),
                inflight=self._inflight_count,
                peak_queue_depth=self._peak_queue_depth,
                peak_inflight=self._peak_inflight,
                pending_count=pending,
                oldest_pending_age_seconds=oldest_age,
                no_progress_age_seconds=no_progress_age,
                stall_timeout_seconds=self.settings.stall_timeout_seconds,
                stalled=stalled,
                accepted_count=self._accepted_count,
                completed_count=self._completed_count,
                queue_drop_count=self._queue_drop_count,
                operational_failure_count=self._operational_failure_count,
                correlation_failure_count=self._correlation_failure_count,
                audit_emit_attempt_count=self._audit_emit_attempt_count,
                audit_emit_success_count=self._audit_emit_success_count,
                audit_emit_failure_count=self._audit_emit_failure_count,
            )

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("drain timeout cannot be negative")
        deadline = monotonic() + timeout_seconds
        return self._drain_until(deadline)

    def _drain_until(self, deadline: float) -> bool:
        with self._condition:
            while self._unfinished:
                thread = self._thread
                if thread is not None and not thread.is_alive():
                    return False
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.05))
        return True

    def shutdown(
        self,
        *,
        timeout_seconds: float = 5.0,
        drain: bool = True,
    ) -> bool:
        if timeout_seconds < 0:
            raise ValueError("shutdown timeout cannot be negative")
        deadline = monotonic() + timeout_seconds
        if not self._shutdown_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            return False
        try:
            with self._condition:
                if self._shutdown_completed:
                    return self._shutdown_succeeded is True
                self._shutdown_started = True
                self._accepting = False
                thread = self._thread
            drained = not drain or self._drain_until(deadline)
            self._stop.set()
            remaining = deadline - monotonic()
            if thread is not None and remaining > 0:
                thread.join(timeout=remaining)
            stopped = thread is None or not thread.is_alive()
            succeeded = drained and stopped
            with self._condition:
                self._shutdown_completed = True
                self._shutdown_succeeded = succeeded
                self._condition.notify_all()
            return succeeded
        finally:
            self._shutdown_lock.release()

    def __enter__(self) -> AsyncSchemaLinkShadowObserver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.shutdown()


__all__ = [
    "ArtifactPrecondition",
    "AsyncSchemaLinkShadowObserver",
    "HybridSchemaLinkShadow",
    "SchemaFieldCapability",
    "SchemaEmbeddingArtifactEvidence",
    "SchemaEmbeddingCandidateEvidence",
    "SchemaLinkCandidateV2",
    "SchemaLinkDecision",
    "SchemaLinkShadowObserver",
    "SchemaLinkStatus",
    "SchemaShadowMode",
    "SchemaShadowQueueSettings",
    "SchemaShadowRuntimeSnapshot",
    "SchemaShadowSettings",
]
