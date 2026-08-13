from __future__ import annotations

import pytest

from finance_agent_core.evaluation import schema_embedding_runtime_benchmark as module
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactGateEvidence,
    load_schema_embedding_candidate_link,
)
from finance_agent_core.evaluation.schema_embedding_runtime_benchmark import (
    run_schema_embedding_runtime_benchmark,
)


class _FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, text: str) -> tuple[float, ...]:
        assert text
        self.calls += 1
        return (1.0, 0.0)

    @property
    def library_versions(self) -> dict[str, str]:
        return {"fake-runtime": "1.0"}


def _gate() -> SchemaEmbeddingArtifactGateEvidence:
    return SchemaEmbeddingArtifactGateEvidence(
        mode="shadow",
        candidate=load_schema_embedding_candidate_link("bge-m3"),
        snapshot_file_manifest_sha256="a" * 64,
        manifest_file_sha256="b" * 64,
    )


def test_runtime_benchmark_measures_bounded_concurrency_and_conjunctive_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_read_cgroup_integer",
        lambda name: {"memory.peak": 512 * 1024**2, "memory.max": 6 * 1024**3}[name],
    )
    provider = _FakeProvider()

    report = run_schema_embedding_runtime_benchmark(
        provider,
        artifact_gate=_gate(),
        model_load_ms=12.0,
        generated_at_utc="2026-08-13T04:00:00Z",
        runtime_image_reference="registry.example/schema-eval@sha256:" + "c" * 64,
        platform="linux/amd64",
        cpu_threads=2,
        concurrency_levels=(1, 2, 4),
        request_count_per_level=8,
        max_inflight=1,
        maximum_gated_concurrency=2,
        maximum_p95_ms=1_000,
        maximum_peak_memory_bytes=1024**4,
    )

    assert provider.calls == 25  # one warm request plus three levels of eight
    assert [item.concurrency for item in report.observations] == [1, 2, 4]
    assert all(item.request_count == 8 for item in report.observations)
    assert all(item.max_inflight == 1 for item in report.observations)
    assert all(item.error_count == 0 for item in report.observations)
    assert report.activation_gate.evaluated_concurrency_levels == (1, 2)
    assert report.activation_gate.latency_passed is True
    assert report.activation_gate.memory_passed is True
    assert report.activation_gate.container_metrics_present is True
    assert report.activation_gate.zero_errors is True
    assert report.activation_gate.passed is True
    assert report.activation_gate.scope == "runtime_prerequisite_only_not_activation_approval"


def test_runtime_gate_does_not_treat_diagnostic_c4_latency_as_c2_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_read_cgroup_integer",
        lambda name: {"memory.peak": 512 * 1024**2, "memory.max": 6 * 1024**3}[name],
    )
    provider = _FakeProvider()
    report = run_schema_embedding_runtime_benchmark(
        provider,
        artifact_gate=_gate(),
        model_load_ms=1.0,
        generated_at_utc="2026-08-13T04:00:00Z",
        runtime_image_reference="registry.example/schema-eval@sha256:" + "c" * 64,
        platform="linux/amd64",
        cpu_threads=1,
        concurrency_levels=(1, 2, 4),
        request_count_per_level=4,
        max_inflight=1,
        maximum_gated_concurrency=2,
        maximum_p95_ms=1_000,
        maximum_peak_memory_bytes=1024**4,
    )

    assert report.activation_gate.evaluated_concurrency_levels == (1, 2)
    assert report.activation_gate.passed is True


def test_runtime_gate_fails_closed_without_container_memory_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_read_cgroup_integer", lambda _name: None)

    report = run_schema_embedding_runtime_benchmark(
        _FakeProvider(),
        artifact_gate=_gate(),
        model_load_ms=1.0,
        generated_at_utc="2026-08-13T04:00:00Z",
        runtime_image_reference="registry.example/schema-eval@sha256:" + "c" * 64,
        platform="linux/amd64",
        cpu_threads=1,
        concurrency_levels=(1, 2),
        request_count_per_level=2,
        max_inflight=1,
        maximum_gated_concurrency=2,
        maximum_p95_ms=1_000,
        maximum_peak_memory_bytes=1024**4,
    )

    assert report.activation_gate.latency_passed is True
    assert report.activation_gate.memory_passed is True
    assert report.activation_gate.container_metrics_present is False
    assert report.activation_gate.passed is False
