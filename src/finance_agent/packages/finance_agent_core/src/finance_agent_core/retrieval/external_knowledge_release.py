from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from finance_agent_core.config.knowledge_coverage import (
    KnowledgeCapability,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
    load_official_source_registry,
)
from finance_agent_core.release import (
    ExternalKnowledgeArtifactRelease,
    external_knowledge_artifact_file_bytes,
    external_knowledge_verifier_contract_sha256,
)
from finance_agent_core.retrieval.corpus import VerifiedApprovedCorpus, verify_approved_corpus
from finance_agent_core.retrieval.document_dense import (
    ApprovedDocumentDenseArtifact,
    DocumentCacheActivationReceipt,
    approved_document_dense_artifact_bytes,
    document_cache_activation_receipt_bytes,
    load_approved_document_dense_artifact,
    load_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.sqlite_fts import normalize_document_text
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationStore,
    VerifiedTemporalRelationBundle,
    load_verified_temporal_relation_bundle,
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


@dataclass(frozen=True)
class CacheFirstExternalKnowledgeBootstrap:
    """Canonical, release-ready copies of one independently approved cache set."""

    artifact: ExternalKnowledgeArtifactRelease
    artifact_bytes: bytes
    artifact_file_sha256: str
    document_dense: ApprovedDocumentDenseArtifact
    document_dense_bytes: bytes
    document_dense_file_sha256: str
    activation: DocumentCacheActivationReceipt
    activation_bytes: bytes
    activation_file_sha256: str
    relation_bundle: VerifiedTemporalRelationBundle
    relation_bundle_bytes: bytes
    relation_bundle_file_sha256: str


def _empty_live_corpus_sha256() -> str:
    payload = b'{"documents":[],"mode":"verified_live_official","schema_version":"1.0"}\n'
    return hashlib.sha256(payload).hexdigest()


def live_corpus_manifest_sha256() -> str:
    """Corpus hash every live-mode relation bundle must carry (there is no document corpus)."""

    return _empty_live_corpus_sha256()


def _activation_capabilities(
    activation: DocumentCacheActivationReceipt,
) -> frozenset[KnowledgeCapability]:
    return frozenset(
        capability for document in activation.documents for capability in document.capabilities
    )


def _verify_cache_capability_boundary(
    *,
    activation: DocumentCacheActivationReceipt,
    relation_bundle: VerifiedTemporalRelationBundle,
) -> frozenset[KnowledgeCapability]:
    """Return capabilities genuinely represented by the sealed cache inputs."""

    capabilities = _activation_capabilities(activation)
    if not capabilities:
        raise ValueError("document activation receipt has no cache-search capability")
    relation_capabilities = {item.capability for item in relation_bundle.relations}
    if not relation_capabilities.issubset(capabilities):
        raise ValueError("temporal relations claim a capability absent from the document cache")
    return capabilities


def _verify_activation_against_corpus(
    *,
    activation: DocumentCacheActivationReceipt,
    corpus: VerifiedApprovedCorpus,
    registry: OfficialSourceRegistry,
) -> None:
    """Re-prove that receipt metadata and authorities name the verified snapshots."""

    approved = {item.approval.document_id: item.approval for item in corpus.documents}
    activated = {item.document_id: item for item in activation.documents}
    if set(approved) != set(activated):
        raise ValueError("document activation and approved corpus document sets differ")
    for document_id, source in approved.items():
        receipt_source = activated[document_id]
        if (
            receipt_source.title != source.title
            or receipt_source.publisher != source.publisher
            or receipt_source.source_registry_id != source.source_registry_id
            or receipt_source.source_uri != source.source_uri
            or receipt_source.capabilities != source.capabilities
            or receipt_source.published_at != source.published_at
            or receipt_source.as_of != source.as_of
            or receipt_source.content_sha256 != source.content_sha256
            or receipt_source.normalized_text_sha256 != source.normalized_text_sha256
        ):
            raise ValueError("document activation metadata differs from approved corpus")
        try:
            authorities = tuple(
                registry.authorize_url(source.source_uri, capability)
                for capability in receipt_source.capabilities
            )
        except OfficialSourceRegistryError as error:
            raise ValueError("document activation exceeds Source Registry authority") from error
        if any(
            item.source_id != receipt_source.source_registry_id
            or item.publisher != source.publisher
            for item in authorities
        ):
            raise ValueError("document activation Source Registry identity differs")


def _verify_relations_against_corpus(
    *,
    relation_bundle: VerifiedTemporalRelationBundle,
    corpus: VerifiedApprovedCorpus,
) -> None:
    """Bind every relation claim to one exact approved snapshot and excerpt."""

    documents = {item.approval.document_id: item for item in corpus.documents}
    normalized_text = {
        item.approval.document_id: normalize_document_text(item.text)
        for item in corpus.documents
    }
    for relation in relation_bundle.relations:
        for evidence in relation.evidence:
            verified = documents.get(evidence.document_id)
            if verified is None:
                raise ValueError("temporal relation cites a document absent from the corpus")
            source = verified.approval
            if (
                relation.capability not in source.capabilities
                or evidence.source_registry_id != source.source_registry_id
                or evidence.title != source.title
                or evidence.publisher != source.publisher
                or evidence.source_uri != source.source_uri
                or evidence.published_at != source.published_at
                or evidence.as_of != source.as_of
                or evidence.content_sha256 != source.normalized_text_sha256
            ):
                raise ValueError("temporal relation evidence differs from the approved corpus")
            excerpt = normalize_document_text(evidence.excerpt)
            if not excerpt or excerpt not in normalized_text[evidence.document_id]:
                raise ValueError("temporal relation excerpt is absent from the approved corpus")


def _verified_relation_graph(
    relation_graph: VerifiedTemporalRelationBundle,
    *,
    registry: OfficialSourceRegistry,
    corpus_sha256: str,
) -> VerifiedTemporalRelationBundle:
    """Admit a prebuilt relation graph only inside the live release trust boundary.

    The graph is built ahead of time from the same approved official resources
    the live path fetches (for example a KODEX product page plus its historical
    holdings JSON), so every relation still carries source-registry evidence
    with content hashes and dates.  Re-verifying through ``TemporalRelationStore``
    keeps the release fail-closed: a relation from an unapproved host, after the
    cutoff, or with a mismatched publisher rejects the whole bootstrap.
    """

    if not relation_graph.relations:
        raise ValueError("relation graph mode requires at least one verified relation")
    if (
        relation_graph.manifest.corpus_manifest_sha256 != corpus_sha256
        or relation_graph.manifest.source_registry_sha256 != registry.canonical_sha256
        or relation_graph.manifest.evidence_cutoff != registry.evidence_cutoff
    ):
        raise ValueError("relation graph does not share the live release trust boundary")
    TemporalRelationStore(relation_graph, registry)
    return relation_graph


def build_live_external_knowledge_bootstrap(
    relation_graph: VerifiedTemporalRelationBundle | None = None,
) -> LiveExternalKnowledgeBootstrap:
    """Build a bootstrap for release-authorized official live search.

    The bootstrap deliberately claims no BM25 or Document Dense index.  Every
    cache miss must pass the source registry, exact-span extractor, provided-DB
    intersection, and evidence verifier during the request that uses it.

    Without ``relation_graph`` the relation cache is empty
    (``verified_live_official``).  With one, the sealed prebuilt graph becomes
    the cache the live path consults first
    (``verified_live_official_with_relation_graph``); misses still go to live
    official search under the same budget and verifier.
    """

    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    if not all(
        entry.status == "approved_live_official" and entry.runtime_execution_enabled
        for entry in coverage.capabilities
    ):
        raise ValueError("every external capability must be approved for official live search")
    corpus_sha256 = _empty_live_corpus_sha256()
    if relation_graph is None:
        retrieval_mode = "verified_live_official"
        relation_bundle = seal_temporal_relation_bundle(
            (),
            corpus_manifest_sha256=corpus_sha256,
            registry=registry,
        )
    else:
        retrieval_mode = "verified_live_official_with_relation_graph"
        relation_bundle = _verified_relation_graph(
            relation_graph,
            registry=registry,
            corpus_sha256=corpus_sha256,
        )
    artifact = ExternalKnowledgeArtifactRelease(
        retrieval_mode=retrieval_mode,
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


def build_cache_first_external_knowledge_artifact(
    *,
    activation: DocumentCacheActivationReceipt,
    relation_bundle: VerifiedTemporalRelationBundle,
    retrieval_mode: Literal[
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    ] = "approved_corpus_cache_first",
) -> ExternalKnowledgeArtifactRelease:
    """Bind an approved Document Dense cache to the existing external runtime."""

    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    if (
        activation.source_registry_sha256 != registry.canonical_sha256
        or activation.evidence_cutoff != registry.evidence_cutoff
        or relation_bundle.manifest.source_registry_sha256 != registry.canonical_sha256
        or relation_bundle.manifest.corpus_manifest_sha256
        != activation.approved_corpus_manifest_sha256
    ):
        raise ValueError("cache-first external artifacts do not share one trust boundary")
    cache_capabilities = _verify_cache_capability_boundary(
        activation=activation,
        relation_bundle=relation_bundle,
    )
    if retrieval_mode == "approved_corpus_cache_first":
        if not all(
            entry.status == "approved_live_official" and entry.runtime_execution_enabled
            for entry in coverage.capabilities
        ):
            raise ValueError("every external capability must remain approved for live fallback")
    elif retrieval_mode == "approved_corpus_cache_only":
        coverage.require_cache_only_binding(
            capabilities=cache_capabilities,
            corpus_manifest_sha256=activation.approved_corpus_manifest_sha256,
            document_dense_artifact_sha256=activation.document_dense_artifact_sha256,
        )
    else:  # pragma: no cover - type checkers prevent unsupported callers
        raise ValueError("unsupported approved corpus retrieval mode")
    return ExternalKnowledgeArtifactRelease(
        retrieval_mode=retrieval_mode,
        source_registry_sha256=registry.canonical_sha256,
        coverage_manifest_sha256=coverage.canonical_sha256,
        corpus_manifest_sha256=activation.approved_corpus_manifest_sha256,
        document_bm25_sha256=activation.document_bm25_sha256,
        document_dense_sha256=activation.document_dense_artifact_sha256,
        document_activation_receipt_sha256=activation.canonical_sha256,
        temporal_relation_sha256=relation_bundle.manifest.relation_set_sha256,
        verifier_contract_sha256=external_knowledge_verifier_contract_sha256(),
        embedding_model_revision=activation.provider.model_revision,
    )


def build_cache_first_external_knowledge_bootstrap(
    *,
    approved_corpus_manifest_path: str | Path,
    approved_corpus_root: str | Path,
    expected_approved_corpus_manifest_sha256: str,
    document_dense_artifact_path: str | Path,
    expected_document_dense_file_sha256: str,
    activation_receipt_path: str | Path,
    expected_activation_receipt_file_sha256: str,
    relation_bundle_path: str | Path,
    expected_relation_bundle_file_sha256: str,
    retrieval_mode: Literal[
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    ] = "approved_corpus_cache_first",
) -> CacheFirstExternalKnowledgeBootstrap:
    """Verify and bind a complete approved corpus cache without changing approval state.

    The approval manifest is intentionally required in addition to the runtime
    artifact and receipt.  A syntactically valid receipt alone must never be
    able to turn pending corpus bytes into production retrieval authority.
    """

    corpus = verify_approved_corpus(
        approved_corpus_manifest_path,
        approved_corpus_root,
    )
    if corpus.manifest_sha256 != expected_approved_corpus_manifest_sha256:
        raise ValueError("approved corpus manifest differs from its trusted SHA-256")

    dense = load_approved_document_dense_artifact(
        document_dense_artifact_path,
        expected_file_sha256=expected_document_dense_file_sha256,
    )
    activation = load_document_cache_activation_receipt(
        activation_receipt_path,
        expected_file_sha256=expected_activation_receipt_file_sha256,
    )
    registry = load_official_source_registry()
    relation_bundle = load_verified_temporal_relation_bundle(
        relation_bundle_path,
        expected_file_sha256=expected_relation_bundle_file_sha256,
        registry=registry,
    )
    _verify_activation_against_corpus(
        activation=activation,
        corpus=corpus,
        registry=registry,
    )
    _verify_relations_against_corpus(
        relation_bundle=relation_bundle,
        corpus=corpus,
    )

    if (
        dense.manifest.source_manifest_sha256 != corpus.manifest_sha256
        or dense.manifest.source_text_manifest_sha256 != corpus.manifest.file_manifest_sha256
        or activation.approved_corpus_manifest_sha256 != corpus.manifest_sha256
        or activation.approved_corpus_file_manifest_sha256 != corpus.manifest.file_manifest_sha256
        or activation.document_dense_artifact_sha256 != dense.canonical_sha256
        or activation.document_dense_vector_payload_sha256 != dense.manifest.vector_payload_sha256
        or activation.embedding_snapshot_manifest_sha256
        != dense.manifest.embedding_snapshot_manifest_sha256
        or activation.provider != dense.manifest.provider
        or relation_bundle.manifest.corpus_manifest_sha256 != corpus.manifest_sha256
    ):
        raise ValueError("cache-first inputs do not describe one approved corpus release")

    artifact = build_cache_first_external_knowledge_artifact(
        activation=activation,
        relation_bundle=relation_bundle,
        retrieval_mode=retrieval_mode,
    )
    artifact_bytes = external_knowledge_artifact_file_bytes(artifact)
    dense_bytes = approved_document_dense_artifact_bytes(dense)
    activation_bytes = document_cache_activation_receipt_bytes(activation)
    relation_bytes = temporal_relation_bundle_bytes(relation_bundle)
    return CacheFirstExternalKnowledgeBootstrap(
        artifact=artifact,
        artifact_bytes=artifact_bytes,
        artifact_file_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        document_dense=dense,
        document_dense_bytes=dense_bytes,
        document_dense_file_sha256=hashlib.sha256(dense_bytes).hexdigest(),
        activation=activation,
        activation_bytes=activation_bytes,
        activation_file_sha256=hashlib.sha256(activation_bytes).hexdigest(),
        relation_bundle=relation_bundle,
        relation_bundle_bytes=relation_bytes,
        relation_bundle_file_sha256=hashlib.sha256(relation_bytes).hexdigest(),
    )


def _write_new_read_only_files(targets: tuple[tuple[Path, bytes], ...]) -> None:
    if len({path for path, _data in targets}) != len(targets):
        raise ValueError("external release outputs must be distinct")
    for path, _data in targets:
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink() or path.exists():
            raise ValueError("external release output must be a new safe absolute path")

    created: list[Path] = []
    try:
        for path, data in targets:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags, 0o444)
            created.append(path)
            try:
                os.fchmod(descriptor, 0o444)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("external release output write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        for path in created:
            try:
                path.chmod(0o600)
                path.unlink()
            except OSError:
                pass
        raise


def write_live_external_knowledge_bootstrap(
    *,
    artifact_path: str | Path,
    relation_bundle_path: str | Path,
    relation_graph_path: str | Path | None = None,
    expected_relation_graph_file_sha256: str | None = None,
) -> LiveExternalKnowledgeBootstrap:
    if (relation_graph_path is None) != (expected_relation_graph_file_sha256 is None):
        raise ValueError("relation graph path and its trusted SHA-256 must be given together")
    relation_graph = None
    if relation_graph_path is not None:
        assert expected_relation_graph_file_sha256 is not None
        relation_graph = load_verified_temporal_relation_bundle(
            relation_graph_path,
            expected_file_sha256=expected_relation_graph_file_sha256,
        )
    bootstrap = build_live_external_knowledge_bootstrap(relation_graph)
    _write_new_read_only_files(
        (
            (Path(artifact_path), bootstrap.artifact_bytes),
            (Path(relation_bundle_path), bootstrap.relation_bundle_bytes),
        )
    )
    return bootstrap


def write_cache_first_external_knowledge_bootstrap(
    *,
    artifact_path: str | Path,
    relation_bundle_output_path: str | Path,
    document_dense_output_path: str | Path,
    activation_receipt_output_path: str | Path,
    approved_corpus_manifest_path: str | Path,
    approved_corpus_root: str | Path,
    expected_approved_corpus_manifest_sha256: str,
    document_dense_artifact_path: str | Path,
    expected_document_dense_file_sha256: str,
    activation_receipt_path: str | Path,
    expected_activation_receipt_file_sha256: str,
    relation_bundle_path: str | Path,
    expected_relation_bundle_file_sha256: str,
    retrieval_mode: Literal[
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    ] = "approved_corpus_cache_first",
) -> CacheFirstExternalKnowledgeBootstrap:
    bootstrap = build_cache_first_external_knowledge_bootstrap(
        approved_corpus_manifest_path=approved_corpus_manifest_path,
        approved_corpus_root=approved_corpus_root,
        expected_approved_corpus_manifest_sha256=(expected_approved_corpus_manifest_sha256),
        document_dense_artifact_path=document_dense_artifact_path,
        expected_document_dense_file_sha256=expected_document_dense_file_sha256,
        activation_receipt_path=activation_receipt_path,
        expected_activation_receipt_file_sha256=(expected_activation_receipt_file_sha256),
        relation_bundle_path=relation_bundle_path,
        expected_relation_bundle_file_sha256=expected_relation_bundle_file_sha256,
        retrieval_mode=retrieval_mode,
    )
    _write_new_read_only_files(
        (
            (Path(artifact_path), bootstrap.artifact_bytes),
            (Path(relation_bundle_output_path), bootstrap.relation_bundle_bytes),
            (Path(document_dense_output_path), bootstrap.document_dense_bytes),
            (Path(activation_receipt_output_path), bootstrap.activation_bytes),
        )
    )
    return bootstrap


__all__ = [
    "CacheFirstExternalKnowledgeBootstrap",
    "LiveExternalKnowledgeBootstrap",
    "build_live_external_knowledge_bootstrap",
    "build_cache_first_external_knowledge_artifact",
    "build_cache_first_external_knowledge_bootstrap",
    "write_cache_first_external_knowledge_bootstrap",
    "write_live_external_knowledge_bootstrap",
]
