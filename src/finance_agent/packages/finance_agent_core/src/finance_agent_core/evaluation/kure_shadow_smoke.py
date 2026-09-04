from __future__ import annotations

import argparse
import hashlib
import resource
import sys
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.execution import query_plan_authority_sha256
from finance_agent_core.observability import (
    AuditStage,
    FaultTolerantAuditSink,
    InMemoryAuditSink,
)
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    build_schema_field_entries,
)
from finance_agent_core.retrieval.schema_shadow import (
    AsyncSchemaLinkShadowObserver,
    HybridSchemaLinkShadow,
    SchemaLinkStatus,
    SchemaShadowMode,
    SchemaShadowQueueSettings,
    SchemaShadowSettings,
)
from finance_agent_core.storage import require_approved_database

_ALIAS = "kure-v1"
_QUESTION = "운용 비용률이 낮은 국내 ETF 3개를 보여줘."
_UNRESOLVED_SPAN = "운용 비용률"
_BLOCKED_QUESTION = "수익률이 높은 국내 ETF를 보여줘."
_OOD_QUESTION = "파스타 만드는 법을 알려줘."
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class KureShadowSmokeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KureShadowArtifactReport(KureShadowSmokeModel):
    alias: Literal["kure-v1"] = _ALIAS
    model_id: Literal["nlpai-lab/KURE-v1"]
    model_revision: Literal["d14c8a9423946e268a0c9952fecf3a7aabd73bd9"]
    model_dimension: Literal[1024]
    snapshot_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    index_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    vector_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_key_count: int = Field(gt=0)
    index_scope: Literal["offline_evaluation_only"]
    production_enabled: Literal[False]


class KureShadowCallReport(KureShadowSmokeModel):
    fixed_question_sha256: str = Field(pattern=_SHA256_PATTERN)
    baseline_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    query_plan_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    query_plan_unchanged: Literal[True]
    baseline_result_unchanged: Literal[True]
    shadow_status: SchemaLinkStatus
    shadow_reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    shadow_candidate_count: int = Field(ge=0, le=20)
    document_call_count: Literal[1]
    document_text_count: int = Field(gt=0)
    query_call_count: Literal[1]
    query_text_count: Literal[1]
    blocked_control_embedding_calls: Literal[0]
    ood_control_embedding_calls: Literal[0]


class KureShadowRuntimeReport(KureShadowSmokeModel):
    artifact_gate_and_model_load_ms: float = Field(ge=0)
    provider_model_load_ms: float = Field(ge=0)
    index_build_ms: float = Field(ge=0)
    baseline_answer_ms: float = Field(ge=0)
    observed_answer_ms: float = Field(ge=0)
    shadow_drain_ms: float = Field(ge=0)
    peak_rss_bytes: int = Field(ge=0)
    peak_rss_delta_bytes: int = Field(ge=0)
    queue_drop_count: Literal[0]
    operational_failure_count: Literal[0]
    correlation_failure_count: Literal[0]
    audit_emit_failure_count: Literal[0]
    shutdown_completed: Literal[True]
    shutdown_succeeded: Literal[True]


class KureShadowSmokeReport(KureShadowSmokeModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["passed"] = "passed"
    mode: Literal["test_only_shadow"] = "test_only_shadow"
    authority: Literal["non_authoritative_never_used_for_queryplan_or_answer"] = (
        "non_authoritative_never_used_for_queryplan_or_answer"
    )
    external_blind_used: Literal[False] = False
    artifact: KureShadowArtifactReport
    call: KureShadowCallReport
    runtime: KureShadowRuntimeReport

    @model_validator(mode="after")
    def require_exact_shadow_observation(self) -> KureShadowSmokeReport:
        if self.call.shadow_status is SchemaLinkStatus.DISABLED:
            raise ValueError("KURE Shadow smoke must produce a non-disabled observation")
        if self.call.document_text_count != self.artifact.field_key_count:
            raise ValueError("KURE Shadow document and index field counts differ")
        return self


class _OneShotSchemaGapGate:
    """Local smoke seam; it is never wired into the public Agent runtime."""

    def evaluate(self, question: str, **_kwargs: object) -> SemanticCoverageDecision:
        if question == _QUESTION:
            return SemanticCoverageDecision(schema_link_gap_spans=(_UNRESOLVED_SPAN,))
        return SemanticCoverageDecision()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _peak_rss_bytes() -> int:
    observed = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return observed if sys.platform == "darwin" else observed * 1024


def _result_sha256(result: object) -> str:
    serializer = getattr(result, "model_dump_json", None)
    if serializer is None:
        raise TypeError("Agent result does not support canonical serialization")
    return _sha256_text(serializer())


def _answer_timed(agent: RoutedFinanceAgent, question: str, request_id: str):
    started = perf_counter()
    result = agent.answer(question, request_id)
    return result, (perf_counter() - started) * 1000


def run_kure_shadow_smoke(
    *,
    database_path: Path,
    snapshot_dir: Path,
    trusted_cache_root: Path,
    manifest_path: Path,
    cpu_threads: int = 2,
    batch_size: int = 16,
    shadow_timeout_seconds: float = 30.0,
) -> KureShadowSmokeReport:
    """Run one exact KURE query without granting it user-visible authority."""

    if not 1 <= cpu_threads <= 8:
        raise ValueError("KURE Shadow smoke cpu_threads must be between 1 and 8")
    if not 1 <= batch_size <= 64:
        raise ValueError("KURE Shadow smoke batch_size must be between 1 and 64")
    if not 0 < shadow_timeout_seconds <= 120:
        raise ValueError("KURE Shadow smoke timeout must be in (0, 120]")

    approved_database = database_path.resolve(strict=True)
    require_approved_database(ProductFamily.DOMESTIC_ETP.value, approved_database)
    initial_peak_rss = _peak_rss_bytes()

    provider_started = perf_counter()
    provider = load_verified_schema_embedding_cpu_provider(
        alias=_ALIAS,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
        trusted_cache_root=trusted_cache_root,
        batch_size=batch_size,
        cpu_threads=cpu_threads,
    )
    provider_elapsed_ms = (perf_counter() - provider_started) * 1000

    index_started = perf_counter()
    index = DenseSchemaIndex.build(build_schema_field_entries(), provider)
    index_elapsed_ms = (perf_counter() - index_started) * 1000
    evidence = provider.artifact_gate_evidence
    if evidence.mode != "shadow":
        raise RuntimeError("KURE artifact is not admitted for test-only Shadow")

    baseline_router = IntentRouter(semantic_coverage_gate=_OneShotSchemaGapGate())
    observed_router = IntentRouter(semantic_coverage_gate=_OneShotSchemaGapGate())
    baseline_agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: approved_database},
        router=baseline_router,
    )

    memory_sink = InMemoryAuditSink(max_events=4)
    shadow_worker = HybridSchemaLinkShadow(
        settings=SchemaShadowSettings(mode=SchemaShadowMode.SHADOW),
        index=index,
        # The exact snapshot was verified before and after local-only model load.
        # A read-only cache mount is the external boundary after construction; do
        # not hash a 2.3 GiB snapshot again for every observed request.
        artifact_precondition=lambda: evidence,
        audit_sink=FaultTolerantAuditSink(memory_sink),
    )
    observer = AsyncSchemaLinkShadowObserver(
        shadow_worker,
        settings=SchemaShadowQueueSettings(queue_capacity=2),
    )
    observed_agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: approved_database},
        router=observed_router,
        schema_link_shadow_observer=observer,
    )

    drain_elapsed_ms = 0.0
    try:
        baseline, baseline_ms = _answer_timed(
            baseline_agent,
            _QUESTION,
            "kure-shadow-smoke-primary",
        )
        observed, observed_ms = _answer_timed(
            observed_agent,
            _QUESTION,
            "kure-shadow-smoke-primary",
        )
        drain_started = perf_counter()
        if not observer.drain(timeout_seconds=shadow_timeout_seconds):
            raise RuntimeError("KURE Shadow observation did not drain within its timeout")
        drain_elapsed_ms = (perf_counter() - drain_started) * 1000

        baseline_json = baseline.model_dump_json()
        observed_json = observed.model_dump_json()
        if observed_json != baseline_json:
            raise RuntimeError("KURE Shadow changed the deterministic Agent result")
        if (baseline.query_plan is None) != (observed.query_plan is None):
            raise RuntimeError("KURE Shadow changed QueryPlan presence")
        baseline_plan_sha256: str | None = None
        if baseline.query_plan is not None:
            if observed.query_plan is None:
                raise RuntimeError("KURE Shadow changed QueryPlan presence")
            baseline_plan_sha256 = query_plan_authority_sha256(baseline.query_plan)
            if query_plan_authority_sha256(observed.query_plan) != baseline_plan_sha256:
                raise RuntimeError("KURE Shadow changed the deterministic QueryPlan")

        query_calls_after_primary = provider.query_calls
        observed_agent.answer(_BLOCKED_QUESTION, "kure-shadow-smoke-blocked")
        blocked_call_delta = provider.query_calls - query_calls_after_primary
        observed_agent.answer(_OOD_QUESTION, "kure-shadow-smoke-ood")
        ood_call_delta = provider.query_calls - query_calls_after_primary - blocked_call_delta
        if blocked_call_delta or ood_call_delta:
            raise RuntimeError("control questions reached the KURE embedding provider")
        if provider.query_calls != 1 or provider.query_text_count != 1:
            raise RuntimeError("KURE Shadow smoke must call embed_query exactly once")
        if provider.document_calls != 1:
            raise RuntimeError("KURE Shadow smoke must build the canonical index exactly once")

        events = tuple(
            event
            for event in memory_sink.snapshot()
            if event.stage is AuditStage.SCHEMA_LINK_SHADOW
        )
        if len(events) != 1:
            raise RuntimeError("KURE Shadow smoke requires exactly one redacted audit observation")
        shadow_event = events[0]
        if (
            shadow_event.model_revision_sha256 != _sha256_text(provider.metadata.model_revision)
            or shadow_event.model_snapshot_manifest_sha256 != evidence.snapshot_file_manifest_sha256
            or shadow_event.index_manifest_sha256 is None
        ):
            raise RuntimeError("KURE Shadow audit identities differ from the loaded artifacts")
        serialized_shadow_event = shadow_event.model_dump_json()
        if _QUESTION in serialized_shadow_event or _UNRESOLVED_SPAN in serialized_shadow_event:
            raise RuntimeError("KURE Shadow audit exposed raw request text")

        snapshot_before_shutdown = observer.snapshot()
    finally:
        shutdown_succeeded = observer.shutdown(
            timeout_seconds=shadow_timeout_seconds,
            drain=True,
        )
    snapshot_after_shutdown = observer.snapshot()
    if not shutdown_succeeded or not snapshot_after_shutdown.shutdown_completed:
        raise RuntimeError("KURE Shadow worker did not shut down cleanly")

    peak_rss = _peak_rss_bytes()
    return KureShadowSmokeReport(
        artifact=KureShadowArtifactReport(
            model_id=provider.metadata.model_id,
            model_revision=provider.metadata.model_revision,
            model_dimension=provider.metadata.dimension,
            snapshot_file_manifest_sha256=evidence.snapshot_file_manifest_sha256,
            snapshot_manifest_file_sha256=evidence.manifest_file_sha256,
            index_manifest_sha256=shadow_event.index_manifest_sha256,
            vector_artifact_sha256=index.manifest.vector_artifact_sha256,
            field_registry_sha256=index.manifest.field_registry_sha256,
            field_key_count=index.manifest.field_key_count,
            index_scope=index.manifest.scope,
            production_enabled=index.manifest.production_enabled,
        ),
        call=KureShadowCallReport(
            fixed_question_sha256=_sha256_text(_QUESTION),
            baseline_result_sha256=_result_sha256(baseline),
            observed_result_sha256=_result_sha256(observed),
            query_plan_sha256=baseline_plan_sha256,
            query_plan_unchanged=True,
            baseline_result_unchanged=True,
            shadow_status={
                "shadow_candidate_found": SchemaLinkStatus.FOUND,
                "shadow_lexical_dense_conflict": SchemaLinkStatus.CONFLICT,
            }.get(shadow_event.reason_code, SchemaLinkStatus.ABSTAIN),
            shadow_reason_code=shadow_event.reason_code,
            shadow_candidate_count=shadow_event.shadow_candidate_count,
            document_call_count=provider.document_calls,
            document_text_count=provider.document_text_count,
            query_call_count=provider.query_calls,
            query_text_count=provider.query_text_count,
            blocked_control_embedding_calls=blocked_call_delta,
            ood_control_embedding_calls=ood_call_delta,
        ),
        runtime=KureShadowRuntimeReport(
            artifact_gate_and_model_load_ms=round(provider_elapsed_ms, 6),
            provider_model_load_ms=round(provider.model_load_ms, 6),
            index_build_ms=round(index_elapsed_ms, 6),
            baseline_answer_ms=round(baseline_ms, 6),
            observed_answer_ms=round(observed_ms, 6),
            shadow_drain_ms=round(drain_elapsed_ms, 6),
            peak_rss_bytes=peak_rss,
            peak_rss_delta_bytes=max(0, peak_rss - initial_peak_rss),
            queue_drop_count=snapshot_before_shutdown.queue_drop_count,
            operational_failure_count=snapshot_before_shutdown.operational_failure_count,
            correlation_failure_count=snapshot_before_shutdown.correlation_failure_count,
            audit_emit_failure_count=snapshot_before_shutdown.audit_emit_failure_count,
            shutdown_completed=snapshot_after_shutdown.shutdown_completed,
            shutdown_succeeded=snapshot_after_shutdown.shutdown_succeeded is True,
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load the exact local KURE-v1 snapshot and run one non-authoritative Schema Shadow "
            "smoke without External Blind inputs."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--shadow-timeout-seconds", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    report = run_kure_shadow_smoke(
        database_path=arguments.database,
        snapshot_dir=arguments.snapshot_dir,
        trusted_cache_root=arguments.trusted_cache_root,
        manifest_path=arguments.manifest,
        cpu_threads=arguments.cpu_threads,
        batch_size=arguments.batch_size,
        shadow_timeout_seconds=arguments.shadow_timeout_seconds,
    )
    rendered = f"{report.model_dump_json(indent=2)}\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with arguments.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["KureShadowSmokeReport", "main", "run_kure_shadow_smoke"]
