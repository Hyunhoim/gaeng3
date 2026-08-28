from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from finance_agent_core.retrieval.pending_corpus import load_pending_corpus_manifest
from finance_agent_core.retrieval.sqlite_fts import normalize_document_text
from finance_agent_core.retrieval.temporal_relations import TemporalRelationRecord


class PendingRelationError(RuntimeError):
    """Raised when an extracted relation is not verbatim-grounded in pending text."""


class PendingRelationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
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
            _canonical_json_bytes(
                [item.model_dump(mode="json") for item in self.relations]
            )
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
                text_cache[evidence.document_id] = normalize_document_text(
                    content.decode("utf-8")
                )
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


__all__ = [
    "PendingRelationError",
    "PendingTemporalRelationBundle",
    "verify_pending_relation_candidates",
]
