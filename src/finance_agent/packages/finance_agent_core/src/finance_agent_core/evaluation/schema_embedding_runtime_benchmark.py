from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactGateEvidence,
    load_verified_schema_embedding_cpu_provider,
)

_DEFAULT_QUERIES = (
    "최근 한 달 성과가 높은 국내 ETF",
    "운용 비용률이 낮은 미국 상장 ETF",
    "잔존 기간이 짧고 매수 가능한 국내채권",
    "순자산 규모가 큰 공모펀드",
    "해외 ETF의 동적 지표 기준일",
    "연금 거래 가능한 국내 ETF의 일간 거래대금",
    "표면금리가 높은 채권",
    "환헤지를 하는 공모펀드",
)


class RuntimeBenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeLatency(RuntimeBenchmarkModel):
    sample_count: int = Field(ge=1)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    maximum_ms: float = Field(ge=0)


class RuntimeConcurrencyObservation(RuntimeBenchmarkModel):
    concurrency: int = Field(ge=1, le=64)
    request_count: int = Field(ge=1)
    max_inflight: int = Field(ge=1, le=64)
    error_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    throughput_requests_per_second: float = Field(ge=0)
    latency: RuntimeLatency


class RuntimeActivationGate(RuntimeBenchmarkModel):
    evaluated_concurrency_levels: tuple[int, ...]
    maximum_gated_concurrency: int = Field(ge=1, le=64)
    maximum_p95_ms: float = Field(gt=0)
    maximum_peak_memory_bytes: int = Field(gt=0)
    latency_passed: bool
    memory_passed: bool
    container_metrics_present: bool
    zero_errors: bool
    passed: bool
    scope: Literal["runtime_prerequisite_only_not_activation_approval"] = (
        "runtime_prerequisite_only_not_activation_approval"
    )

    @model_validator(mode="after")
    def require_conjunctive_gate(self) -> RuntimeActivationGate:
        expected = (
            self.latency_passed
            and self.memory_passed
            and self.container_metrics_present
            and self.zero_errors
        )
        if self.passed != expected:
            raise ValueError("runtime gate must be the conjunction of all prerequisites")
        return self


class SchemaEmbeddingRuntimeReport(RuntimeBenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: Literal["schema-embedding-docker-concurrency-v1"] = (
        "schema-embedding-docker-concurrency-v1"
    )
    generated_at_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    model_alias: Literal["bge-m3", "kure-v1"]
    model_id: str
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    device: Literal["cpu"] = "cpu"
    runtime_image_reference: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$"
    )
    platform: Literal["linux/amd64", "linux/arm64"]
    library_versions: dict[str, str]
    workload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_gate: SchemaEmbeddingArtifactGateEvidence
    cpu_threads: int = Field(ge=1, le=256)
    max_inflight: int = Field(ge=1, le=64)
    model_load_ms: float = Field(ge=0)
    process_peak_rss_bytes: int = Field(ge=0)
    cgroup_memory_peak_bytes: int | None = Field(default=None, ge=0)
    cgroup_memory_limit_bytes: int | None = Field(default=None, ge=0)
    observations: tuple[RuntimeConcurrencyObservation, ...] = Field(min_length=1)
    activation_gate: RuntimeActivationGate

    @model_validator(mode="after")
    def require_unique_ordered_levels(self) -> SchemaEmbeddingRuntimeReport:
        levels = [item.concurrency for item in self.observations]
        if levels != sorted(set(levels)):
            raise ValueError("concurrency levels must be unique and sorted")
        if self.artifact_gate.candidate.alias != self.model_alias:
            raise ValueError("artifact gate candidate and benchmark model differ")
        if self.artifact_gate.candidate.model_id != self.model_id:
            raise ValueError("artifact gate repository and benchmark model differ")
        if self.artifact_gate.candidate.revision != self.model_revision:
            raise ValueError("artifact gate revision and benchmark model differ")
        return self


class QueryEmbeddingProvider(Protocol):
    @property
    def library_versions(self) -> dict[str, str]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _latency(values: Sequence[float]) -> RuntimeLatency:
    if not values:
        raise ValueError("runtime latency requires at least one successful request")
    return RuntimeLatency(
        sample_count=len(values),
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        p99_ms=_percentile(values, 0.99),
        maximum_ms=round(max(values), 6),
    )


def _read_cgroup_integer(name: str) -> int | None:
    path = Path("/sys/fs/cgroup") / name
    try:
        value = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _run_concurrency_level(
    provider: QueryEmbeddingProvider,
    queries: Sequence[str],
    *,
    concurrency: int,
    request_count: int,
    inflight_gate: threading.BoundedSemaphore,
    max_inflight: int,
) -> RuntimeConcurrencyObservation:
    latencies: list[float] = []
    error_count = 0
    started = time.perf_counter()

    def execute(query: str, barrier: threading.Barrier) -> float:
        barrier.wait()
        request_started = time.perf_counter()
        with inflight_gate:
            vector = provider.embed_query(query)
            if not vector:
                raise RuntimeError("embedding provider returned an empty vector")
        return (time.perf_counter() - request_started) * 1000

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        completed = 0
        while completed < request_count:
            wave_size = min(concurrency, request_count - completed)
            barrier = threading.Barrier(wave_size)
            futures = [
                executor.submit(
                    execute,
                    queries[(completed + offset) % len(queries)],
                    barrier,
                )
                for offset in range(wave_size)
            ]
            for future in futures:
                try:
                    latencies.append(future.result())
                except Exception:  # noqa: BLE001 - errors are counted in the report
                    error_count += 1
            completed += wave_size

    elapsed_ms = (time.perf_counter() - started) * 1000
    return RuntimeConcurrencyObservation(
        concurrency=concurrency,
        request_count=request_count,
        max_inflight=max_inflight,
        error_count=error_count,
        elapsed_ms=round(elapsed_ms, 6),
        throughput_requests_per_second=round(request_count / (elapsed_ms / 1000), 6),
        latency=_latency(latencies),
    )


def run_schema_embedding_runtime_benchmark(
    provider: QueryEmbeddingProvider,
    *,
    artifact_gate: SchemaEmbeddingArtifactGateEvidence,
    model_load_ms: float,
    generated_at_utc: str,
    runtime_image_reference: str,
    platform: Literal["linux/amd64", "linux/arm64"],
    cpu_threads: int,
    concurrency_levels: Sequence[int] = (1, 2, 4),
    request_count_per_level: int = 24,
    max_inflight: int = 1,
    queries: Sequence[str] = _DEFAULT_QUERIES,
    maximum_gated_concurrency: int = 2,
    maximum_p95_ms: float = 250.0,
    maximum_peak_memory_bytes: int = 4 * 1024**3,
) -> SchemaEmbeddingRuntimeReport:
    if not queries or any(not query.strip() for query in queries):
        raise ValueError("runtime benchmark queries cannot be empty")
    levels = tuple(concurrency_levels)
    if levels != tuple(sorted(set(levels))) or any(not 1 <= item <= 64 for item in levels):
        raise ValueError("concurrency levels must be unique, sorted, and between 1 and 64")
    if not 1 <= request_count_per_level <= 10_000:
        raise ValueError("request_count_per_level must be between 1 and 10000")
    if not 1 <= max_inflight <= max(levels):
        raise ValueError("max_inflight must be between 1 and the maximum concurrency")
    if maximum_gated_concurrency not in levels:
        raise ValueError("maximum gated concurrency must be one measured level")

    # Warm the tokenizer and kernels before timed observations. The same single
    # semaphore is shared across all levels to model the bounded Shadow worker.
    provider.embed_query(queries[0])
    inflight_gate = threading.BoundedSemaphore(max_inflight)
    observations = tuple(
        _run_concurrency_level(
            provider,
            queries,
            concurrency=level,
            request_count=request_count_per_level,
            inflight_gate=inflight_gate,
            max_inflight=max_inflight,
        )
        for level in levels
    )
    process_peak_rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    cgroup_peak = _read_cgroup_integer("memory.peak")
    cgroup_limit = _read_cgroup_integer("memory.max")
    container_metrics_present = cgroup_peak is not None and cgroup_limit is not None
    observed_peak = max(value for value in (process_peak_rss_bytes, cgroup_peak or 0))
    gated = tuple(item for item in observations if item.concurrency <= maximum_gated_concurrency)
    latency_passed = all(item.latency.p95_ms <= maximum_p95_ms for item in gated)
    zero_errors = all(item.error_count == 0 for item in observations)
    memory_passed = observed_peak <= maximum_peak_memory_bytes
    activation_gate = RuntimeActivationGate(
        evaluated_concurrency_levels=tuple(item.concurrency for item in gated),
        maximum_gated_concurrency=maximum_gated_concurrency,
        maximum_p95_ms=maximum_p95_ms,
        maximum_peak_memory_bytes=maximum_peak_memory_bytes,
        latency_passed=latency_passed,
        memory_passed=memory_passed,
        container_metrics_present=container_metrics_present,
        zero_errors=zero_errors,
        passed=(latency_passed and memory_passed and container_metrics_present and zero_errors),
    )
    candidate = artifact_gate.candidate
    workload_sha256 = _canonical_sha256(
        {
            "queries": list(queries),
            "concurrency_levels": list(levels),
            "request_count_per_level": request_count_per_level,
            "max_inflight": max_inflight,
            "cpu_threads": cpu_threads,
        }
    )
    return SchemaEmbeddingRuntimeReport(
        generated_at_utc=generated_at_utc,
        model_alias=candidate.alias,
        model_id=candidate.model_id,
        model_revision=candidate.revision,
        runtime_image_reference=runtime_image_reference,
        platform=platform,
        library_versions=provider.library_versions,
        workload_sha256=workload_sha256,
        artifact_gate=artifact_gate,
        cpu_threads=cpu_threads,
        max_inflight=max_inflight,
        model_load_ms=model_load_ms,
        process_peak_rss_bytes=process_peak_rss_bytes,
        cgroup_memory_peak_bytes=cgroup_peak,
        cgroup_memory_limit_bytes=cgroup_limit,
        observations=observations,
        activation_gate=activation_gate,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark a verified Schema Dense snapshot in a bounded CPU container."
    )
    parser.add_argument("--model", choices=("bge-m3", "kure-v1"), required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-image-reference", required=True)
    parser.add_argument(
        "--platform",
        choices=("linux/amd64", "linux/arm64"),
        required=True,
    )
    parser.add_argument("--cpu-threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--concurrency", type=int, nargs="+", default=(1, 2, 4))
    parser.add_argument("--requests-per-level", type=int, default=24)
    parser.add_argument("--max-inflight", type=int, default=1)
    parser.add_argument("--maximum-gated-concurrency", type=int, default=2)
    parser.add_argument("--maximum-p95-ms", type=float, default=250.0)
    parser.add_argument("--maximum-peak-memory-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--require-gate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    provider = load_verified_schema_embedding_cpu_provider(
        alias=arguments.model,
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
    )
    artifact_gate = provider.artifact_gate_evidence
    if artifact_gate is None:
        raise RuntimeError("verified runtime provider lost its artifact gate evidence")
    report = run_schema_embedding_runtime_benchmark(
        provider,
        artifact_gate=artifact_gate,
        model_load_ms=provider.model_load_ms,
        generated_at_utc=arguments.generated_at_utc,
        runtime_image_reference=arguments.runtime_image_reference,
        platform=arguments.platform,
        cpu_threads=arguments.cpu_threads,
        concurrency_levels=arguments.concurrency,
        request_count_per_level=arguments.requests_per_level,
        max_inflight=arguments.max_inflight,
        maximum_gated_concurrency=arguments.maximum_gated_concurrency,
        maximum_p95_ms=arguments.maximum_p95_ms,
        maximum_peak_memory_bytes=arguments.maximum_peak_memory_bytes,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "evaluation_id": report.evaluation_id,
                "model": report.model_alias,
                "revision": report.model_revision,
                "observations": [
                    {
                        "concurrency": item.concurrency,
                        "p95_ms": item.latency.p95_ms,
                        "throughput_rps": item.throughput_requests_per_second,
                        "errors": item.error_count,
                    }
                    for item in report.observations
                ],
                "process_peak_rss_bytes": report.process_peak_rss_bytes,
                "cgroup_memory_peak_bytes": report.cgroup_memory_peak_bytes,
                "runtime_gate_passed": report.activation_gate.passed,
                "output": str(arguments.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not arguments.require_gate or report.activation_gate.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QueryEmbeddingProvider",
    "RuntimeActivationGate",
    "RuntimeConcurrencyObservation",
    "RuntimeLatency",
    "SchemaEmbeddingRuntimeReport",
    "run_schema_embedding_runtime_benchmark",
]
