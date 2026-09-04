import pytest

from finance_agent_core.evaluation.schema_embedding_models import (
    SchemaEmbeddingModelRegistry,
    SchemaEmbeddingModelSpec,
    load_schema_embedding_model_registry,
)


def test_schema_embedding_registry_pins_seven_reproducible_candidates() -> None:
    registry = load_schema_embedding_model_registry()

    assert len(registry.candidates) == 7
    assert [item.alias for item in registry.candidates[:3]] == [
        "kure-v1",
        "bge-m3",
        "qwen3-embedding-0.6b",
    ]
    assert all(len(item.revision) == 40 for item in registry.candidates)
    assert all(item.license_id in {"mit", "apache-2.0"} for item in registry.candidates)
    assert all(not item.trust_remote_code for item in registry.candidates[:6])
    assert registry.candidates[-1].phase == "extended_remote_code"
    assert registry.candidates[-1].trust_remote_code
    assert registry.candidates[-1].remote_code_model_id == "nomic-ai/nomic-bert-2048"
    assert len(registry.candidates[-1].remote_code_revision or "") == 40


def test_model_specific_query_and_document_templates_are_preserved() -> None:
    registry = load_schema_embedding_model_registry()

    assert registry.require("kure-v1").prepare_query("총보수") == "총보수"
    assert registry.require("koe5").prepare_query("총보수") == "query: 총보수"
    assert registry.require("koe5").prepare_document("보수율") == "passage: 보수율"
    assert registry.require("nomic-v2-moe").prepare_document("보수율") == (
        "search_document: 보수율"
    )
    assert "Instruct:" in registry.require("qwen3-embedding-0.6b").prepare_query("총보수")


def test_registry_rejects_duplicate_aliases() -> None:
    candidate = SchemaEmbeddingModelSpec(
        alias="duplicate-model",
        phase="core",
        model_id="example/one",
        revision="a" * 40,
        license_id="mit",
        dimension=128,
        pooling="mean",
        query_template="query: {text}",
        document_template="passage: {text}",
        max_sequence_length=128,
        rationale="test candidate",
    )

    with pytest.raises(ValueError, match="aliases must be unique"):
        SchemaEmbeddingModelRegistry(candidates=(candidate, candidate))


def test_remote_code_requires_dedicated_review_phase() -> None:
    with pytest.raises(ValueError, match="dedicated review phase"):
        SchemaEmbeddingModelSpec(
            alias="unsafe-model",
            phase="core",
            model_id="example/unsafe",
            revision="b" * 40,
            license_id="apache-2.0",
            dimension=128,
            pooling="mean",
            query_template="query: {text}",
            document_template="passage: {text}",
            trust_remote_code=True,
            remote_code_model_id="example/remote-code",
            remote_code_revision="c" * 40,
            max_sequence_length=128,
            rationale="test candidate",
        )
