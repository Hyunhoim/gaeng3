from __future__ import annotations

import hashlib
import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.evaluation.dense_schema_linker import (
    FieldRetrievalMetrics,
    MissedFieldRecovery,
    SchemaRuntimeMetrics,
    SchemaSafetyMetrics,
    run_dense_schema_linker_evaluation,
)
from finance_agent_core.evaluation.schema_embedding_models import (
    SchemaEmbeddingModelSpec,
    SentenceTransformerCpuProvider,
)
from finance_agent_core.retrieval.schema_dense import DenseSchemaIndexManifest


class SchemaEmbeddingBenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CpuExecutionEnvironment(SchemaEmbeddingBenchmarkModel):
    python_version: str
    platform: str
    cpu_model: str
    logical_cpu_count: int = Field(ge=1)
    configured_cpu_threads: int = Field(ge=1)
    device: Literal["cpu"] = "cpu"
    library_versions: dict[str, str]


class RetrievalMetricDeltas(SchemaEmbeddingBenchmarkModel):
    exact_at_gold_cardinality: float
    micro_recall_at_3: float
    micro_recall_at_5: float
    micro_recall_at_10: float
    full_recall_case_rate_at_5: float
    mrr: float
    ndcg_at_5: float


class SchemaFusionConfiguration(SchemaEmbeddingBenchmarkModel):
    strategy: Literal["rrf", "lexical_first"]
    lexical_weight: float = Field(gt=0)
    dense_weight: float = Field(gt=0)


class SchemaEmbeddingCandidateDecision(SchemaEmbeddingBenchmarkModel):
    public_quality_gate_delta: float = 0.02
    latency_gate_p95_ms: float = 250.0
    public_quality_gate_passed: bool
    safety_contract_passed: bool
    latency_gate_passed: bool
    eligible_for_blind_evaluation: bool
    production_adoption: Literal["blocked_pending_blind_and_abstention"] = (
        "blocked_pending_blind_and_abstention"
    )
    reason: str


class SchemaEmbeddingBenchmarkReport(SchemaEmbeddingBenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: Literal["schema-embedding-cpu-public-v1"] = "schema-embedding-cpu-public-v1"
    status: Literal["public_development_not_blind"] = "public_development_not_blind"
    evaluation_scope: Literal["schema_field_linking_with_gold_product_family"] = (
        "schema_field_linking_with_gold_product_family"
    )
    routing_quality_included: Literal[False] = False
    measured_at: datetime
    model: SchemaEmbeddingModelSpec
    fusion: SchemaFusionConfiguration
    environment: CpuExecutionEnvironment
    model_load_ms: float = Field(ge=0)
    smoke_probe: dict[str, object]
    suite_case_count: int = Field(ge=1)
    suite_sha256_by_family: dict[str, str]
    policy_migration_suite_sha256: str
    index_manifest: DenseSchemaIndexManifest
    provider_document_calls: int = Field(ge=0)
    provider_query_calls: int = Field(ge=0)
    lexical: FieldRetrievalMetrics
    dense: FieldRetrievalMetrics
    lexical_plus_dense_rrf: FieldRetrievalMetrics
    dense_minus_lexical: RetrievalMetricDeltas
    hybrid_minus_lexical: RetrievalMetricDeltas
    missed_field_recovery: MissedFieldRecovery
    safety: SchemaSafetyMetrics
    runtime: SchemaRuntimeMetrics
    decision: SchemaEmbeddingCandidateDecision


def _cpu_model_name() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8").splitlines():
            if line.casefold().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _deltas(
    candidate: FieldRetrievalMetrics,
    lexical: FieldRetrievalMetrics,
) -> RetrievalMetricDeltas:
    return RetrievalMetricDeltas(
        exact_at_gold_cardinality=round(
            candidate.exact_at_gold_cardinality - lexical.exact_at_gold_cardinality,
            6,
        ),
        micro_recall_at_3=round(candidate.micro_recall_at_3 - lexical.micro_recall_at_3, 6),
        micro_recall_at_5=round(candidate.micro_recall_at_5 - lexical.micro_recall_at_5, 6),
        micro_recall_at_10=round(candidate.micro_recall_at_10 - lexical.micro_recall_at_10, 6),
        full_recall_case_rate_at_5=round(
            candidate.full_recall_case_rate_at_5 - lexical.full_recall_case_rate_at_5,
            6,
        ),
        mrr=round(candidate.mrr - lexical.mrr, 6),
        ndcg_at_5=round(candidate.ndcg_at_5 - lexical.ndcg_at_5, 6),
    )


def run_schema_embedding_benchmark(
    provider: SentenceTransformerCpuProvider,
    *,
    smoke_probe: dict[str, object] | None = None,
    fusion_strategy: Literal["rrf", "lexical_first"] = "rrf",
    lexical_weight: float = 1.0,
    dense_weight: float = 1.0,
) -> SchemaEmbeddingBenchmarkReport:
    smoke = smoke_probe or provider.smoke_probe()
    component = run_dense_schema_linker_evaluation(
        provider,  # type: ignore[arg-type]
        fusion_strategy=fusion_strategy,
        lexical_weight=lexical_weight,
        dense_weight=dense_weight,
    )
    hybrid_delta = _deltas(component.hybrid, component.lexical)
    dense_delta = _deltas(component.fake_dense, component.lexical)
    quality_passed = (
        max(
            hybrid_delta.micro_recall_at_5,
            hybrid_delta.exact_at_gold_cardinality,
        )
        >= 0.02
    )
    safety_passed = (
        component.safety.blocked_no_call_rate == 1
        and component.safety.pre_dense_gate_false_positive_count == 0
        and component.safety.out_of_registry_candidate_count == 0
        and component.safety.out_of_family_candidate_count == 0
        and component.safety.production_probe_provider_query_calls == 0
        and not component.index_manifest.production_enabled
    )
    latency_passed = component.runtime.dense_latency.p95_ms <= 250
    eligible = quality_passed and safety_passed and latency_passed
    if eligible:
        reason = (
            "공개 개발 세트의 품질·안전·CPU 지연시간 문턱을 통과했으나, "
            "독립 blind와 OOD 기권 임계값 검증 전에는 production에 사용할 수 없습니다."
        )
    else:
        failed = [
            name
            for name, passed in (
                ("공개 품질", quality_passed),
                ("안전 계약", safety_passed),
                ("CPU p95", latency_passed),
            )
            if not passed
        ]
        reason = (
            f"{', '.join(failed)} 문턱을 통과하지 못했습니다. 공개 개발 세트 결과는 "
            "production 채택 근거가 아니며 독립 blind·OOD 검증도 남아 있습니다."
        )
    return SchemaEmbeddingBenchmarkReport(
        measured_at=datetime.now(UTC),
        model=provider.spec,
        fusion=SchemaFusionConfiguration(
            strategy=fusion_strategy,
            lexical_weight=lexical_weight,
            dense_weight=dense_weight,
        ),
        environment=CpuExecutionEnvironment(
            python_version=platform.python_version(),
            platform=platform.platform(),
            cpu_model=_cpu_model_name(),
            logical_cpu_count=os.cpu_count() or 1,
            configured_cpu_threads=provider.cpu_threads,
            library_versions=provider.library_versions,
        ),
        model_load_ms=round(provider.model_load_ms, 6),
        smoke_probe=smoke,
        suite_case_count=component.suite_case_count,
        suite_sha256_by_family=component.suite_sha256_by_family,
        policy_migration_suite_sha256=component.policy_migration_suite_sha256,
        index_manifest=component.index_manifest,
        provider_document_calls=component.provider_document_calls,
        provider_query_calls=component.provider_query_calls,
        lexical=component.lexical,
        dense=component.fake_dense,
        lexical_plus_dense_rrf=component.hybrid,
        dense_minus_lexical=dense_delta,
        hybrid_minus_lexical=hybrid_delta,
        missed_field_recovery=component.missed_field_recovery,
        safety=component.safety,
        runtime=component.runtime,
        decision=SchemaEmbeddingCandidateDecision(
            public_quality_gate_passed=quality_passed,
            safety_contract_passed=safety_passed,
            latency_gate_passed=latency_passed,
            eligible_for_blind_evaluation=eligible,
            reason=reason,
        ),
    )


def schema_embedding_report_fingerprint(report: SchemaEmbeddingBenchmarkReport) -> str:
    payload = json.dumps(
        report.model_dump(mode="json", exclude={"measured_at"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CpuExecutionEnvironment",
    "RetrievalMetricDeltas",
    "SchemaEmbeddingBenchmarkReport",
    "SchemaEmbeddingCandidateDecision",
    "SchemaFusionConfiguration",
    "run_schema_embedding_benchmark",
    "schema_embedding_report_fingerprint",
]
