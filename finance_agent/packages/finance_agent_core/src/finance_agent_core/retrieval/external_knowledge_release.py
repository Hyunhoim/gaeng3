from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from finance_agent_core.config.knowledge_coverage import (
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.release import (
    ExternalKnowledgeArtifactRelease,
    external_knowledge_artifact_file_bytes,
    external_knowledge_verifier_contract_sha256,
)
from finance_agent_core.retrieval.temporal_relations import (
    VerifiedTemporalRelationBundle,
    seal_temporal_relation_bundle,
    temporal_relation_bundle_bytes,
)


@dataclass(frozen=True)
class LiveExternalKnowledgeBootstrap:
    artifact: ExternalKnowledgeArtifactRelease
    artifact_bytes: bytes
    artifact_file_sha256: str
    relation_bundle: VerifiedTemporalRelationBundle
    relation_bundle_bytes: bytes
    relation_bundle_file_sha256: str


def _empty_live_corpus_sha256() -> str:
    payload = b'{"documents":[],"mode":"verified_live_official","schema_version":"1.0"}\n'
    return hashlib.sha256(payload).hexdigest()


def build_live_external_knowledge_bootstrap() -> LiveExternalKnowledgeBootstrap:
    """Build an empty cache bootstrap for release-authorized official live search.

    The bootstrap deliberately claims no BM25 or Document Dense index.  Every
    cache miss must pass the source registry, exact-span extractor, provided-DB
    intersection, and evidence verifier during the request that uses it.
    """

    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    if not all(
        entry.status == "approved_live_official" and entry.runtime_execution_enabled
        for entry in coverage.capabilities
    ):
        raise ValueError("every external capability must be approved for official live search")
    corpus_sha256 = _empty_live_corpus_sha256()
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus_sha256,
        registry=registry,
    )
    artifact = ExternalKnowledgeArtifactRelease(
        retrieval_mode="verified_live_official",
        source_registry_sha256=registry.canonical_sha256,
        coverage_manifest_sha256=coverage.canonical_sha256,
        corpus_manifest_sha256=corpus_sha256,
        temporal_relation_sha256=relation_bundle.manifest.relation_set_sha256,
        verifier_contract_sha256=external_knowledge_verifier_contract_sha256(),
        embedding_model_revision=None,
    )
    artifact_bytes = external_knowledge_artifact_file_bytes(artifact)
    relation_bytes = temporal_relation_bundle_bytes(relation_bundle)
    return LiveExternalKnowledgeBootstrap(
        artifact=artifact,
        artifact_bytes=artifact_bytes,
        artifact_file_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        relation_bundle=relation_bundle,
        relation_bundle_bytes=relation_bytes,
        relation_bundle_file_sha256=hashlib.sha256(relation_bytes).hexdigest(),
    )


def write_live_external_knowledge_bootstrap(
    *,
    artifact_path: str | Path,
    relation_bundle_path: str | Path,
) -> LiveExternalKnowledgeBootstrap:
    bootstrap = build_live_external_knowledge_bootstrap()
    targets = (
        (Path(artifact_path), bootstrap.artifact_bytes),
        (Path(relation_bundle_path), bootstrap.relation_bundle_bytes),
    )
    for path, data in targets:
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise ValueError("external bootstrap output must be a safe absolute path")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o444)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return bootstrap


__all__ = [
    "LiveExternalKnowledgeBootstrap",
    "build_live_external_knowledge_bootstrap",
    "write_live_external_knowledge_bootstrap",
]
