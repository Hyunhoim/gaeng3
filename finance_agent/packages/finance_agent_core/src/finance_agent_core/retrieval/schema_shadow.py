from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
from collections.abc import Callable, Sequence
from enum import StrEnum
from time import monotonic, perf_counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.planning_policy import (
    PlanningPath,
    PlanningSemanticIssue,
    PlanningTrace,
)
from finance_agent_core.config import FieldRegistry, load_field_registry
from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.observability import (
    AuditEvent,
    AuditOutcome,
    AuditStage,
    FaultTolerantAuditSink,
    MetricCounter,
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
        audit_sink: FaultTolerantAuditSink | None = None,
    ) -> None:
        self.settings = settings or SchemaShadowSettings()
        self.index = index
        self.artifact_precondition = artifact_precondition
        self.registry = registry or load_field_registry()
        self.capability_matrix = capability_matrix or load_capability_matrix()
        self.audit_sink = audit_sink
        self._inflight = threading.BoundedSemaphore(self.settings.max_inflight)

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

    def observe(self, trace: PlanningTrace) -> tuple[SchemaLinkDecision, ...]:
        started = perf_counter()
        spans = trace.planning_decision.unresolved_spans or ("disabled",)
        try:
            decisions = self._observe_safely(trace)
        except Exception:  # noqa: BLE001 - a shadow observer can never alter the Agent result
            decisions = tuple(
                self._abstain(span, trace, "shadow_internal_failure") for span in spans
            )
        self._emit_audit(trace, decisions, (perf_counter() - started) * 1000)
        return decisions

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
            if self.audit_sink is not None:
                try:
                    self.audit_sink.metrics.increment(MetricCounter.SHADOW_OBSERVATIONS)
                except Exception:
                    pass
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
    ) -> None:
        if self.audit_sink is None:
            return
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
        self.audit_sink.emit_lazy(
            lambda: AuditEvent.redacted(
                stage=AuditStage.SCHEMA_LINK_SHADOW,
                outcome=outcome,
                reason_code=selected.reason_code,
                duration_ms=duration_ms,
                request_id=trace.route_decision.draft.request_id,
                question=trace.route_decision.draft.question,
                model_revision=model_revision,
                model_snapshot_manifest_sha256=selected.model_snapshot_manifest_id,
                index_manifest_sha256=selected.index_manifest_id,
                route_disposition=trace.route_decision.disposition,
                interaction_intent=trace.route_decision.draft.intent,
                product_families=trace.planning_decision.product_families,
                shadow_candidate_count=sum(len(item.candidates) for item in decisions),
            )
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
    ) -> None:
        if type(worker) is not HybridSchemaLinkShadow:
            raise TypeError("shadow worker must be the trusted HybridSchemaLinkShadow")
        self._worker = worker
        self.settings = settings or SchemaShadowQueueSettings()
        self._queue: queue.Queue[PlanningTrace] = queue.Queue(maxsize=self.settings.queue_capacity)
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._accepting = True
        self._thread: threading.Thread | None = None
        self._unfinished = 0
        self._inflight_count = 0

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
        dropped = False
        with self._condition:
            if not self._accepting:
                return False
            self._ensure_started_locked()
            try:
                self._queue.put_nowait(trace)
            except queue.Full:
                dropped = True
            else:
                self._unfinished += 1
            self._set_gauges_locked()
            self._condition.notify_all()
        if dropped:
            self._record_drop(trace)
            return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                trace = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._condition:
                self._inflight_count = 1
                self._set_gauges_locked()
            try:
                self._worker.observe(trace)
            except Exception:
                # HybridSchemaLinkShadow already isolates failures, and this
                # second boundary protects the process from injected workers.
                pass
            finally:
                self._queue.task_done()
                with self._condition:
                    self._inflight_count = 0
                    self._unfinished -= 1
                    self._set_gauges_locked()
                    self._condition.notify_all()

    def _set_gauges_locked(self) -> None:
        if self._worker.audit_sink is None:
            return
        try:
            self._worker.audit_sink.metrics.set_gauges(
                queue_depth=self._queue.qsize(),
                inflight=self._inflight_count,
            )
        except Exception:
            pass

    def _record_drop(self, trace: PlanningTrace) -> None:
        del trace
        sink = self._worker.audit_sink
        if sink is None:
            return
        try:
            sink.metrics.increment(MetricCounter.QUEUE_DROPS)
        except Exception:
            pass
        # Queue saturation is handled on the user request thread.  Calling an
        # arbitrary durable sink here could turn a telemetry outage into user
        # latency.  The bounded in-memory counter is the only synchronous work;
        # detailed events remain a worker-thread responsibility.

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("drain timeout cannot be negative")
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while self._unfinished:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def shutdown(
        self,
        *,
        timeout_seconds: float = 5.0,
        drain: bool = True,
    ) -> bool:
        if timeout_seconds < 0:
            raise ValueError("shutdown timeout cannot be negative")
        with self._condition:
            self._accepting = False
        drained = True
        if drain:
            drained = self.drain(timeout_seconds=timeout_seconds)
        self._stop.set()
        thread = self._thread
        if thread is None:
            return drained
        thread.join(timeout=timeout_seconds)
        return drained and not thread.is_alive()

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
    "SchemaShadowSettings",
]
