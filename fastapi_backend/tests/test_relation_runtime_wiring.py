from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent_core.release import (
    PublicDocumentRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    RelationRetrievalArtifactRelease,
)
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import (
    _relation_artifact_trust_sha256,
    build_agent,
    require_approval_guard,
)
from tests.conftest import stub_resolved_release


def _relation_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE": tmp_path / "relation-release.json",
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256": "a" * 64,
        "FINANCE_RELATION_INDEX_FILE": tmp_path / "relations.sqlite3",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "provided",
    [
        {"FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE": "/data/relation-release.json"},
        {"FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256": "a" * 64},
        {"FINANCE_RELATION_INDEX_FILE": "/data/relations.sqlite3"},
        {
            "FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE": "/data/relation-release.json",
            "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256": "a" * 64,
        },
    ],
)
def test_relation_runtime_settings_require_artifacts_and_one_trust_source(
    provided: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="both artifacts and exactly one"):
        Settings(**provided)


@pytest.mark.parametrize(
    "relative_field",
    [
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE",
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE",
        "FINANCE_RELATION_INDEX_FILE",
    ],
)
def test_relation_runtime_settings_require_absolute_files(
    tmp_path: Path,
    relative_field: str,
) -> None:
    values: dict[str, object] = {
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE": tmp_path / "relation-release.json",
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256": "a" * 64,
        "FINANCE_RELATION_INDEX_FILE": tmp_path / "relations.sqlite3",
    }
    if relative_field == "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE":
        values.pop("FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256")
    values[relative_field] = "relative-file"

    with pytest.raises(ValidationError, match="absolute paths"):
        Settings(**values)


def test_relation_runtime_settings_reject_two_trust_sources(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="exactly one SHA-256 trust source"):
        _relation_settings(
            tmp_path,
            FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE=tmp_path / "relation.sha256",
        )


def test_deployment_rejects_runtime_generated_relation_trust_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires the explicit relation artifact SHA-256"):
        Settings(
            APP_ENV="evaluation",
            FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE=tmp_path / "relation-release.json",
            FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE=tmp_path / "relation.sha256",
            FINANCE_RELATION_INDEX_FILE=tmp_path / "relations.sqlite3",
        )


@pytest.mark.parametrize("mode", [0o400, 0o444, 0o600])
def test_development_reads_exact_read_only_relation_trust_file(
    tmp_path: Path,
    mode: int,
) -> None:
    trust_file = tmp_path / "relation.sha256"
    trust_file.write_text("b" * 64 + "\n", encoding="ascii")
    trust_file.chmod(mode)
    settings = Settings(
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE=tmp_path / "relation-release.json",
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE=trust_file,
        FINANCE_RELATION_INDEX_FILE=tmp_path / "relations.sqlite3",
    )

    assert _relation_artifact_trust_sha256(settings) == "b" * 64


@pytest.mark.parametrize("mode", [0o622, 0o666])
def test_development_rejects_writable_relation_trust_file(
    tmp_path: Path,
    mode: int,
) -> None:
    trust_file = tmp_path / "relation.sha256"
    trust_file.write_text("b" * 64 + "\n", encoding="ascii")
    trust_file.chmod(mode)
    settings = Settings(
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE=tmp_path / "relation-release.json",
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE=trust_file,
        FINANCE_RELATION_INDEX_FILE=tmp_path / "relations.sqlite3",
    )

    with pytest.raises(RuntimeError, match="trust file is insecure"):
        _relation_artifact_trust_sha256(settings)


def test_development_rejects_malformed_relation_trust_file(tmp_path: Path) -> None:
    trust_file = tmp_path / "relation.sha256"
    trust_file.write_text("z" * 64 + "\n", encoding="ascii")
    trust_file.chmod(0o600)
    settings = Settings(
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE=tmp_path / "relation-release.json",
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE=trust_file,
        FINANCE_RELATION_INDEX_FILE=tmp_path / "relations.sqlite3",
    )

    with pytest.raises(RuntimeError, match="trust file is invalid"):
        _relation_artifact_trust_sha256(settings)


def test_development_default_keeps_relation_router_and_execution_disabled() -> None:
    agent = build_agent(Settings())

    assert agent.knowledge_router is None
    assert agent.knowledge_agent is None


def test_activated_release_rejects_missing_runtime_relation_files() -> None:
    release = stub_resolved_release()
    release.manifest.components.knowledge_retrieval = PublicKnowledgeRetrievalRelease(
        relation=PublicRelationRetrievalRelease(
            status="activated",
            artifact=RelationRetrievalArtifactRelease(
                index_sha256="b" * 64,
                approval_manifest_sha256="c" * 64,
                relation_set_sha256="d" * 64,
            ),
            artifact_file_sha256="e" * 64,
        ),
        document=PublicDocumentRetrievalRelease(),
    )

    with pytest.raises(
        RuntimeError,
        match="activated relation release requires configured runtime artifacts",
    ):
        build_agent(Settings(APP_ENV="evaluation"), release_guard=release)


def test_disabled_release_rejects_unreleased_runtime_relation_configuration(
    tmp_path: Path,
) -> None:
    settings = _relation_settings(tmp_path, APP_ENV="evaluation")
    release = stub_resolved_release()
    service = build_agent(settings, release_guard=release)

    assert service.knowledge_agent is None
    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent assembly"):
        require_approval_guard(service, settings, release_guard=release)
