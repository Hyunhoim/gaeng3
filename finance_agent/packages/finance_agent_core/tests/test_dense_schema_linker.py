from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.dense_schema_linker import FakeHashEmbeddingProvider
from finance_agent_core.retrieval import (
    DenseSchemaIndex,
    DenseSchemaIndexArtifact,
    DenseSchemaLinkerSettings,
    EmbeddingProviderMetadata,
    FeatureGatedDenseSchemaLinker,
    SchemaDenseContractError,
    build_schema_field_entries,
    packaged_field_registry_sha256,
)


def _index() -> tuple[DenseSchemaIndex, FakeHashEmbeddingProvider]:
    provider = FakeHashEmbeddingProvider(dimension=64)
    return DenseSchemaIndex.build(build_schema_field_entries(), provider), provider


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_schema_dense_manifest_pins_registry_provider_and_offline_scope() -> None:
    index, provider = _index()

    assert index.manifest.scope == "offline_evaluation_only"
    assert not index.manifest.production_enabled
    assert index.manifest.abstention_policy == "not_calibrated"
    assert index.manifest.field_registry_sha256 == packaged_field_registry_sha256()
    assert index.manifest.provider == provider.metadata
    assert index.manifest.vector_count == index.manifest.field_key_count
    assert provider.document_calls == 1


def test_schema_dense_search_returns_only_exact_registry_fields_for_family() -> None:
    index, _ = _index()

    candidates = index.search(
        "총보수가 낮은 해외 ETF",
        ProductFamily.OVERSEAS_ETP,
        top_k=5,
    )
    allowed = {
        item.entry.field_id
        for item in index.artifact.records
        if item.entry.product_family is ProductFamily.OVERSEAS_ETP
    }

    assert len(candidates) == 5
    assert all(item.product_family is ProductFamily.OVERSEAS_ETP for item in candidates)
    assert all(item.field_id in allowed for item in candidates)
    assert [item.rank for item in candidates] == [1, 2, 3, 4, 5]


def test_disabled_schema_dense_feature_never_calls_query_provider() -> None:
    index, provider = _index()
    linker = FeatureGatedDenseSchemaLinker(index, DenseSchemaLinkerSettings(enabled=False))
    before = provider.query_calls

    response = linker.link("AUM이 큰 해외 ETF", ProductFamily.OVERSEAS_ETP)

    assert response.status == "disabled"
    assert response.candidates == ()
    assert provider.query_calls == before


def test_offline_manifest_fails_closed_if_feature_is_forced_on() -> None:
    index, provider = _index()
    linker = FeatureGatedDenseSchemaLinker(index, DenseSchemaLinkerSettings(enabled=True))
    before = provider.query_calls

    with pytest.raises(SchemaDenseContractError, match="offline-only"):
        linker.link("AUM이 큰 해외 ETF", ProductFamily.OVERSEAS_ETP)

    assert provider.query_calls == before


def test_schema_dense_artifact_rejects_vector_tampering() -> None:
    index, provider = _index()
    record = index.artifact.records[0]
    tampered_record = record.model_copy(update={"vector": (*record.vector[:-1], 0.125)})
    tampered = DenseSchemaIndexArtifact(
        manifest=index.manifest,
        records=[tampered_record, *index.artifact.records[1:]],
    )

    with pytest.raises(SchemaDenseContractError, match="vector SHA-256"):
        DenseSchemaIndex.from_artifact(tampered, provider)


def test_schema_dense_artifact_rejects_provider_metadata_mismatch() -> None:
    index, _ = _index()
    provider = FakeHashEmbeddingProvider(dimension=128)

    with pytest.raises(SchemaDenseContractError, match="provider metadata"):
        DenseSchemaIndex.from_artifact(index.artifact, provider)


def test_schema_dense_artifact_rejects_self_consistent_incomplete_corpus() -> None:
    index, provider = _index()
    incomplete_records = index.artifact.records[:-1]
    entries = [record.entry.model_dump(mode="json") for record in incomplete_records]
    keys = [record.entry.document_id for record in incomplete_records]
    records = [record.model_dump(mode="json") for record in incomplete_records]
    forged_manifest = index.manifest.model_copy(
        update={
            "field_key_count": len(keys),
            "vector_count": len(keys),
            "field_keys_sha256": _canonical_sha256(keys),
            "corpus_sha256": _canonical_sha256(entries),
            "vector_artifact_sha256": _canonical_sha256(records),
        }
    )
    forged = DenseSchemaIndexArtifact(
        manifest=forged_manifest,
        records=incomplete_records,
    )

    with pytest.raises(SchemaDenseContractError, match="canonical complete field corpus"):
        DenseSchemaIndex.from_artifact(forged, provider)


def test_schema_dense_artifact_nested_collections_are_immutable() -> None:
    index, _ = _index()

    assert isinstance(index.artifact.records, tuple)
    assert isinstance(index.artifact.records[0].vector, tuple)
    with pytest.raises(TypeError):
        index.artifact.records[0].vector[0] = 0.0


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [(None, False), ("false", False), ("0", False), ("true", True), ("1", True)],
)
def test_schema_dense_feature_flag_has_strict_off_default(
    raw: str | None,
    enabled: bool,
) -> None:
    environment = {} if raw is None else {"FINANCE_DENSE_SCHEMA_LINKER_ENABLED": raw}

    assert DenseSchemaLinkerSettings.from_environment(environment).enabled is enabled


def test_schema_dense_feature_flag_rejects_ambiguous_value() -> None:
    with pytest.raises(ValueError, match="explicit boolean"):
        DenseSchemaLinkerSettings.from_environment({"FINANCE_DENSE_SCHEMA_LINKER_ENABLED": "maybe"})


def test_explicit_empty_environment_does_not_fall_back_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINANCE_DENSE_SCHEMA_LINKER_ENABLED", "true")

    assert not DenseSchemaLinkerSettings.from_environment({}).enabled


def test_embedding_manifest_rejects_mutable_model_revision() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="example_provider",
            model_id="example/model",
            model_revision="latest",
            license_id="MIT",
            dimension=64,
            pooling="mean",
        )


def test_real_embedding_manifest_requires_commit_digest_revision() -> None:
    with pytest.raises(ValidationError, match="commit digest"):
        EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="example_provider",
            model_id="example/model",
            model_revision="release-v1",
            license_id="MIT",
            dimension=64,
            pooling="mean",
        )

    metadata = EmbeddingProviderMetadata(
        provider_kind="frozen_model",
        provider_id="example_provider",
        model_id="example/model",
        model_revision="a" * 40,
        license_id="MIT",
        dimension=64,
        pooling="mean",
    )
    assert metadata.model_revision == "a" * 40
