from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from finance_agent_core.evaluation import kure_shadow_smoke as smoke_module
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactGateEvidence,
    load_schema_embedding_candidate_link,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata


class _FakeVerifiedKureProvider:
    def __init__(self) -> None:
        self.metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="verified_sentence_transformers_kure_v1",
            model_id="nlpai-lab/KURE-v1",
            model_revision="d14c8a9423946e268a0c9952fecf3a7aabd73bd9",
            license_id="mit",
            dimension=1024,
            pooling="cls",
        )
        self.artifact_gate_evidence = SchemaEmbeddingArtifactGateEvidence(
            mode="shadow",
            candidate=load_schema_embedding_candidate_link("kure-v1"),
            snapshot_file_manifest_sha256="a" * 64,
            manifest_file_sha256="b" * 64,
        )
        self.model_load_ms = 1.25
        self.document_calls = 0
        self.document_text_count = 0
        self.query_calls = 0
        self.query_text_count = 0
        self._target: list[float] | None = None

    @staticmethod
    def _one_hot(index: int) -> list[float]:
        vector = [0.0] * 1024
        vector[index] = 1.0
        return vector

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.document_text_count += len(texts)
        vectors = [self._one_hot(index) for index, _ in enumerate(texts)]
        for text, vector in zip(texts, vectors, strict=True):
            if "total_expense_ratio_pct" in text and self._target is None:
                self._target = vector
        assert self._target is not None
        return vectors

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        self.query_text_count += 1
        assert text == "운용 비용률"
        assert self._target is not None
        return list(self._target)


def test_kure_shadow_smoke_is_one_call_non_authoritative_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    domestic_sample_database: tuple[object, object, object],
) -> None:
    database_path = Path(domestic_sample_database[0])
    snapshot_dir = tmp_path / "snapshot"
    trusted_root = tmp_path / "cache"
    manifest_path = tmp_path / "kure-manifest.json"
    snapshot_dir.mkdir()
    trusted_root.mkdir()
    manifest_path.write_text("{}", encoding="utf-8")
    provider = _FakeVerifiedKureProvider()
    loader_arguments: dict[str, object] = {}

    def fake_loader(**kwargs: object) -> _FakeVerifiedKureProvider:
        loader_arguments.update(kwargs)
        return provider

    monkeypatch.setattr(smoke_module, "load_verified_schema_embedding_cpu_provider", fake_loader)
    monkeypatch.setattr(smoke_module, "require_approved_database", lambda *_args: None)

    report = smoke_module.run_kure_shadow_smoke(
        database_path=database_path,
        snapshot_dir=snapshot_dir,
        trusted_cache_root=trusted_root,
        manifest_path=manifest_path,
        cpu_threads=2,
        batch_size=16,
        shadow_timeout_seconds=2,
    )

    assert report.status == "passed"
    assert report.mode == "test_only_shadow"
    assert report.external_blind_used is False
    assert report.call.baseline_result_unchanged is True
    assert report.call.baseline_result_sha256 == report.call.observed_result_sha256
    assert report.call.query_plan_unchanged is True
    assert report.call.document_call_count == 1
    assert report.call.document_text_count == report.artifact.field_key_count == 100
    assert report.call.query_call_count == report.call.query_text_count == 1
    assert report.call.blocked_control_embedding_calls == 0
    assert report.call.ood_control_embedding_calls == 0
    assert report.call.shadow_status.value == "found"
    assert report.call.shadow_reason_code == "shadow_candidate_found"
    assert report.artifact.model_dimension == 1024
    assert report.artifact.production_enabled is False
    assert report.artifact.index_scope == "offline_evaluation_only"
    assert report.runtime.queue_drop_count == 0
    assert report.runtime.operational_failure_count == 0
    assert report.runtime.audit_emit_failure_count == 0
    assert report.runtime.shutdown_completed is True
    assert report.runtime.shutdown_succeeded is True
    assert loader_arguments == {
        "alias": "kure-v1",
        "snapshot_dir": snapshot_dir,
        "manifest_path": manifest_path,
        "trusted_cache_root": trusted_root,
        "batch_size": 16,
        "cpu_threads": 2,
    }
    serialized = report.model_dump_json()
    assert "운용 비용률" not in serialized
    assert "파스타" not in serialized
    assert "수익률이 높은" not in serialized
    assert str(database_path) not in serialized


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cpu_threads", 0, "cpu_threads"),
        ("batch_size", 65, "batch_size"),
        ("shadow_timeout_seconds", 0, "timeout"),
    ],
)
def test_kure_shadow_smoke_rejects_unbounded_runtime_settings_before_file_access(
    field: str,
    value: int,
    message: str,
) -> None:
    arguments: dict[str, object] = {
        "database_path": Path("/does/not/exist"),
        "snapshot_dir": Path("/does/not/exist"),
        "trusted_cache_root": Path("/does/not/exist"),
        "manifest_path": Path("/does/not/exist"),
        field: value,
    }

    with pytest.raises(ValueError, match=message):
        smoke_module.run_kure_shadow_smoke(**arguments)  # type: ignore[arg-type]
