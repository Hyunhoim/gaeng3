from __future__ import annotations

import os
from pathlib import Path

import pytest

from finance_agent_core.agent.semantic_resolution import ResolutionOperation
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.schema_adaptive import ProductionHybridSchemaLinker
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    SchemaDenseActivationPolicy,
    load_dense_schema_index_artifact,
    p1_precision_field_thresholds,
)

_ENABLED = os.environ.get("FINANCE_RUN_PRODUCTION_KURE_OFFLINE_SMOKE") == "1"
_MODEL_REVISION = "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
_INDEX_SHA256 = "2807195ef8c233ab3218ff742a4f53e11728aa63b56bbeb6dce62bb178f6ae57"
_CALIBRATION_REPORT_SHA256 = "8c41295bf0746bf395b0898863b406f18f6cbd8a42a4cc379069e88ab85c3a09"


def _path_from_environment(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, default)).resolve()


@pytest.mark.skipif(
    not _ENABLED,
    reason="set FINANCE_RUN_PRODUCTION_KURE_OFFLINE_SMOKE=1 for the 2.3 GB local snapshot",
)
def test_production_kure_snapshot_index_and_candidate_lookup_are_offline() -> None:
    """Opt-in production artifact smoke; the runner must additionally use --network none."""

    repository = Path(__file__).resolve().parents[5]
    cache_root = _path_from_environment(
        "FINANCE_KURE_SMOKE_CACHE_ROOT",
        repository / "finance_agent/hf-cache",
    )
    snapshot = _path_from_environment(
        "FINANCE_KURE_SMOKE_SNAPSHOT_DIR",
        cache_root / "models--nlpai-lab--KURE-v1/snapshots" / _MODEL_REVISION,
    )
    snapshot_manifest = _path_from_environment(
        "FINANCE_KURE_SMOKE_SNAPSHOT_MANIFEST_FILE",
        repository
        / "src/finance_agent/evaluation/protocols/schema-embedding-kure-v1-snapshot-v2.json",
    )
    index_file = _path_from_environment(
        "FINANCE_KURE_SMOKE_INDEX_FILE",
        repository / "src/finance_agent/artifacts/runtime/schema-dense/"
        "kure-schema-dense-production-v8-registry18-calibrated-20260905.json",
    )
    policy = SchemaDenseActivationPolicy(
        dense_min_score=0.340015,
        hclx_candidate_min_score=0.3,
        minimum_margin=0.034684,
        top_k=10,
        calibration_report_sha256=_CALIBRATION_REPORT_SHA256,
        field_thresholds=p1_precision_field_thresholds(),
    )

    artifact = load_dense_schema_index_artifact(
        index_file,
        expected_file_sha256=_INDEX_SHA256,
    )
    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=snapshot,
        manifest_path=snapshot_manifest,
        trusted_cache_root=cache_root,
        mode="production",
        batch_size=1,
        cpu_threads=2,
    )

    assert provider.metadata.model_revision == _MODEL_REVISION
    assert provider.metadata.dimension == 1024
    assert artifact.manifest.provider == provider.metadata
    assert artifact.manifest.activation_policy_sha256 == policy.policy_sha256

    result = ProductionHybridSchemaLinker(
        DenseSchemaIndex(artifact, provider),
        policy,
    ).link(
        residual_span="발행사가 찍어낸 전체 물량",
        product_family=ProductFamily.BOND,
        interaction_intent=InteractionIntent.SEARCH,
        operation=ResolutionOperation.RANK,
    )

    assert result.candidates
    assert result.candidates[0].field_id == "issue_amount"
