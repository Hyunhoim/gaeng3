from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.retrieval.corpus import (
    VerifiedApprovedCorpus,
    approved_corpus_manifest_bytes,
)
from finance_agent_core.retrieval.pending_corpus import load_pending_corpus_manifest
from finance_agent_core.retrieval.sqlite_fts import (
    normalize_document_text,
    sha256_document_text,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationRecord,
    VerifiedTemporalRelationBundle,
    seal_temporal_relation_bundle,
)


class PendingRelationError(RuntimeError):
    """Raised when an extracted relation is not verbatim-grounded in pending text."""


class PendingRelationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


class PendingTemporalRelationBundle(PendingRelationModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["temporal_relation_candidates_pending_review"] = (
        "temporal_relation_candidates_pending_review"
    )
    status: Literal["not_authorized_for_retrieval_or_release"] = (
        "not_authorized_for_retrieval_or_release"
    )
    production_enabled: StrictBool = False
    corpus_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_count: int = Field(gt=0, le=100000)
    relations: tuple[TemporalRelationRecord, ...] = Field(min_length=1, max_length=100000)

    @model_validator(mode="after")
    def validate_bundle(self) -> PendingTemporalRelationBundle:
        ids = [item.relation_id for item in self.relations]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("pending relations must be unique and sorted")
        if self.relation_count != len(self.relations):
            raise ValueError("pending relation count differs")
        digest = hashlib.sha256(
            _canonical_json_bytes([item.model_dump(mode="json") for item in self.relations])
        ).hexdigest()
        if digest != self.relation_set_sha256:
            raise ValueError("pending relation set SHA-256 differs")
        if self.production_enabled:
            raise ValueError("pending relations can never be production enabled")
        return self


def verify_pending_relation_candidates(
    corpus_root: str | Path,
    relations: tuple[TemporalRelationRecord, ...],
) -> PendingTemporalRelationBundle:
    root = Path(corpus_root).resolve(strict=True)
    manifest_path = root / "pending-manifest.json"
    manifest = load_pending_corpus_manifest(manifest_path)
    corpus_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    documents = {item.document_id: item for item in manifest.documents}
    text_cache: dict[str, str] = {}
    ordered = tuple(sorted(relations, key=lambda item: item.relation_id))
    for relation in ordered:
        if relation.observed_at > manifest.evidence_cutoff:
            raise PendingRelationError("pending relation exceeds the evidence cutoff")
        for evidence in relation.evidence:
            document = documents.get(evidence.document_id)
            if document is None:
                raise PendingRelationError("relation cites an uncollected pending document")
            if (
                evidence.source_registry_id != document.source_registry_id
                or evidence.source_uri != document.source_uri
                or evidence.title != document.title
                or evidence.publisher != document.publisher
                or evidence.as_of != document.as_of
                or evidence.published_at != document.published_at
                or evidence.content_sha256 != document.text_sha256
            ):
                raise PendingRelationError("relation evidence metadata differs from snapshot")
            if evidence.document_id not in text_cache:
                candidate = (root / document.text_relative_path).resolve(strict=True)
                try:
                    candidate.relative_to(root)
                except ValueError as error:
                    raise PendingRelationError("pending relation snapshot escaped root") from error
                content = candidate.read_bytes()
                if hashlib.sha256(content).hexdigest() != document.text_sha256:
                    raise PendingRelationError("pending relation snapshot SHA-256 differs")
                text_cache[evidence.document_id] = normalize_document_text(content.decode("utf-8"))
            excerpt = normalize_document_text(evidence.excerpt)
            if not excerpt or excerpt not in text_cache[evidence.document_id]:
                raise PendingRelationError("relation excerpt is not verbatim snapshot text")
    digest = hashlib.sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in ordered])
    ).hexdigest()
    return PendingTemporalRelationBundle(
        corpus_manifest_sha256=corpus_manifest_sha256,
        relation_set_sha256=digest,
        relation_count=len(ordered),
        relations=ordered,
    )


def promote_pending_relations_to_approved_corpus(
    pending: PendingTemporalRelationBundle,
    approved_corpus: VerifiedApprovedCorpus,
    *,
    registry: OfficialSourceRegistry,
) -> VerifiedTemporalRelationBundle:
    """Rebind reviewed relation candidates to an already verified corpus.

    This function grants no document or relation approval.  It accepts only an
    existing ``VerifiedApprovedCorpus``, re-proves each pending evidence record
    against that corpus and the current Source Registry, then changes the
    evidence hash from the staged text-file bytes to the canonical normalized
    text bytes used by an approved cache-only release.
    """

    canonical_manifest_sha256 = hashlib.sha256(
        approved_corpus_manifest_bytes(approved_corpus.manifest)
    ).hexdigest()
    if canonical_manifest_sha256 != approved_corpus.manifest_sha256:
        raise PendingRelationError("approved corpus manifest trust anchor differs")

    manifest_documents = {item.document_id: item for item in approved_corpus.manifest.documents}
    verified_documents = {item.approval.document_id: item for item in approved_corpus.documents}
    if set(manifest_documents) != set(verified_documents) or any(
        verified_documents[document_id].approval != approval
        for document_id, approval in manifest_documents.items()
    ):
        raise PendingRelationError("verified approved corpus document set differs")

    normalized_documents: dict[str, str] = {}
    for document_id, verified in verified_documents.items():
        normalized = normalize_document_text(verified.text)
        if (
            not normalized
            or sha256_document_text(normalized) != verified.approval.normalized_text_sha256
        ):
            raise PendingRelationError("verified approved corpus normalized text differs")
        normalized_documents[document_id] = normalized

    promoted_relations: list[TemporalRelationRecord] = []
    for relation in pending.relations:
        promoted_evidence = []
        for evidence in relation.evidence:
            verified = verified_documents.get(evidence.document_id)
            if verified is None:
                raise PendingRelationError(
                    "relation cites a document absent from the approved corpus"
                )
            source = verified.approval
            if relation.capability not in source.capabilities:
                raise PendingRelationError(
                    "relation capability is absent from the approved corpus document"
                )
            if (
                evidence.source_registry_id != source.source_registry_id
                or evidence.document_id != source.document_id
                or evidence.title != source.title
                or evidence.publisher != source.publisher
                or evidence.source_uri != source.source_uri
                or evidence.published_at != source.published_at
                or evidence.as_of != source.as_of
            ):
                raise PendingRelationError(
                    "relation evidence metadata differs from the approved corpus"
                )
            # Pending relation evidence names the exact staged UTF-8 text file.
            # The approved release instead names its normalized canonical text.
            # Requiring the former before replacing it prevents an arbitrary
            # hash from borrowing the approved document's authority.
            if evidence.content_sha256 != source.content_sha256:
                raise PendingRelationError("pending relation evidence content SHA-256 differs")
            try:
                authority = registry.authorize_url(
                    source.source_uri,
                    relation.capability,
                )
            except OfficialSourceRegistryError as error:
                raise PendingRelationError(
                    "approved relation source exceeds the current Source Registry"
                ) from error
            if (
                authority.source_id != source.source_registry_id
                or authority.publisher != source.publisher
            ):
                raise PendingRelationError(
                    "approved relation source identity differs from the current registry"
                )
            excerpt = normalize_document_text(evidence.excerpt)
            if not excerpt or excerpt not in normalized_documents[source.document_id]:
                raise PendingRelationError("relation excerpt is absent from the approved corpus")
            promoted_evidence.append(
                evidence.model_copy(
                    update={
                        "document_id": source.document_id,
                        "content_sha256": source.normalized_text_sha256,
                    }
                )
            )
        promoted_relations.append(
            relation.model_copy(update={"evidence": tuple(promoted_evidence)})
        )

    return seal_temporal_relation_bundle(
        tuple(promoted_relations),
        corpus_manifest_sha256=approved_corpus.manifest_sha256,
        registry=registry,
    )


__all__ = [
    "PendingRelationError",
    "PendingTemporalRelationBundle",
    "promote_pending_relations_to_approved_corpus",
    "verify_pending_relation_candidates",
]
