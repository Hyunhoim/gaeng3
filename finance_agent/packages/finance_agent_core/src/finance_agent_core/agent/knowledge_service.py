from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.answering.claims import (
    KnowledgeAnswerComposition,
    KnowledgeClaimProvider,
    build_knowledge_answer_context,
    compose_knowledge_answer,
)
from finance_agent_core.contracts.knowledge import (
    DocumentKnowledgeOperation,
    KnowledgePlanAuthorityGate,
    KnowledgePlanAuthorityReceipt,
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
    canonical_knowledge_plan_sha256,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.release import KnowledgeRetrievalRelease
from finance_agent_core.retrieval.models import (
    DocumentFilters,
    DocumentSearchRequest,
    DocumentSearchResponse,
)
from finance_agent_core.retrieval.relations import (
    ProductDatabaseVerifier,
    RelationSearchRequest,
    RelationSearchResponse,
    SQLiteRelationIndex,
)
from finance_agent_core.retrieval.sqlite_fts import SQLiteDocumentIndex
from finance_agent_core.storage import connect_read_only
from finance_agent_core.storage.approval import sha256_file

_MAX_INDEX_BYTES = 512 * 1024 * 1024


class KnowledgeServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeAgentResult(KnowledgeServiceModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["found", "not_found"]
    plan: KnowledgeQueryPlan
    authority: KnowledgePlanAuthorityReceipt
    release_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int = Field(ge=0, le=20)
    relation_response: RelationSearchResponse | None = None
    document_response: DocumentSearchResponse | None = None
    answer: KnowledgeAnswerComposition

    @model_validator(mode="after")
    def validate_result(self) -> KnowledgeAgentResult:
        if self.authority.plan_sha256 != canonical_knowledge_plan_sha256(self.plan):
            raise ValueError("knowledge result and authority plan hash differ")
        if isinstance(self.plan.operation, RelationKnowledgeOperation):
            if self.relation_response is None or self.document_response is not None:
                raise ValueError("relation result requires only relation evidence")
            evidence_count = len(self.relation_response.evidence)
            response_status = self.relation_response.status
        else:
            if self.document_response is None or self.relation_response is not None:
                raise ValueError("document result requires only document evidence")
            evidence_count = len(self.document_response.evidence)
            response_status = self.document_response.status
        if self.status != response_status:
            raise ValueError("knowledge result and retrieval status differ")
        if self.candidate_count != evidence_count:
            raise ValueError("knowledge result count differs from evidence")
        return self


class KnowledgeServiceError(RuntimeError):
    """Raised when a knowledge retrieval request cannot cross a trust boundary."""


def _reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise KnowledgeServiceError("knowledge index path contains a symbolic link")
        if current.parent == current:
            return
        current = current.parent


def _file_version(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_release_file(
    path: Path,
    expected_sha256: str,
) -> tuple[Path, tuple[int, int, int, int, int]]:
    _reject_symlink_chain(path)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise KnowledgeServiceError("knowledge index is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise KnowledgeServiceError("knowledge index must be a regular file")
    if metadata.st_nlink != 1:
        raise KnowledgeServiceError("knowledge index must have one hard link")
    if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise KnowledgeServiceError("knowledge index must be read-only")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_INDEX_BYTES:
        raise KnowledgeServiceError("knowledge index size is outside the approved range")
    if any(os.path.lexists(Path(f"{resolved}{suffix}")) for suffix in ("-wal", "-shm", "-journal")):
        raise KnowledgeServiceError("knowledge index has an unexpected SQLite sidecar")
    before = _file_version(resolved)
    try:
        digest = sha256_file(resolved)
    except OSError as error:
        raise KnowledgeServiceError("knowledge index cannot be hashed") from error
    if digest != expected_sha256:
        raise KnowledgeServiceError("knowledge index differs from its release SHA-256")
    if _file_version(resolved) != before:
        raise KnowledgeServiceError("knowledge index changed during release verification")
    return resolved, before


def _verify_document_integrity(path: Path) -> None:
    try:
        with connect_read_only(path) as connection:
            required_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            integrity = connection.execute("PRAGMA quick_check").fetchone()
    except Exception as error:
        raise KnowledgeServiceError("approved document index cannot be inspected") from error
    if not {"documents", "document_chunks", "document_chunks_fts"} <= required_tables:
        raise KnowledgeServiceError("approved document index schema is incomplete")
    if integrity is None or integrity[0] != "ok":
        raise KnowledgeServiceError("approved document index integrity check failed")


class KnowledgeAgent:
    """Internal P0-7 Agent for exact-plan relation/document retrieval and claims."""

    def __init__(
        self,
        *,
        release: KnowledgeRetrievalRelease,
        relation_index_path: str | Path | None = None,
        relation_database_paths: Mapping[ProductFamily | str, str | Path] | None = None,
        relation_verifier: ProductDatabaseVerifier | None = None,
        document_index_path: str | Path | None = None,
        claim_provider: KnowledgeClaimProvider | None = None,
        plan_gate: KnowledgePlanAuthorityGate | None = None,
    ) -> None:
        if type(release) is not KnowledgeRetrievalRelease:
            raise TypeError("release must be a KnowledgeRetrievalRelease")
        if (release.relation is None) != (relation_index_path is None):
            raise ValueError("relation release and relation index path must agree")
        if release.relation is not None and not relation_database_paths:
            raise ValueError("relation release requires approved product database paths")
        if release.relation is None and relation_database_paths:
            raise ValueError("product database paths require a relation release")
        if (release.document is None) != (document_index_path is None):
            raise ValueError("document release and document index path must agree")
        self.release = KnowledgeRetrievalRelease.model_validate_json(release.model_dump_json())
        self.relation_index_path = (
            None if relation_index_path is None else Path(relation_index_path)
        )
        self.relation_database_paths = {
            ProductFamily(key): Path(value)
            for key, value in (relation_database_paths or {}).items()
        }
        self.relation_verifier = relation_verifier
        self.document_index_path = (
            None if document_index_path is None else Path(document_index_path)
        )
        self.claim_provider = claim_provider
        self.plan_gate = plan_gate or KnowledgePlanAuthorityGate()

    def execute(
        self,
        server_plan: KnowledgeQueryPlan,
        proposal: KnowledgeQueryPlan | None = None,
        *,
        proposal_provider_name: str | None = None,
        proposal_model_name: str | None = None,
    ) -> KnowledgeAgentResult:
        validated = self.plan_gate.authorize(
            server_plan,
            proposal,
            proposal_provider_name=proposal_provider_name,
            proposal_model_name=proposal_model_name,
        )
        plan = validated.plan
        if isinstance(plan.operation, RelationKnowledgeOperation):
            response = self._execute_relation(plan.operation)
            context = build_knowledge_answer_context(plan, response)
            composition = compose_knowledge_answer(context, self.claim_provider)
            return KnowledgeAgentResult(
                status=response.status,
                plan=plan,
                authority=validated.receipt,
                release_contract_sha256=self.release.contract_sha256,
                candidate_count=len(response.evidence),
                relation_response=response,
                answer=composition,
            )
        response = self._execute_document(plan.operation)
        context = build_knowledge_answer_context(plan, response)
        composition = compose_knowledge_answer(context, self.claim_provider)
        return KnowledgeAgentResult(
            status=response.status,
            plan=plan,
            authority=validated.receipt,
            release_contract_sha256=self.release.contract_sha256,
            candidate_count=len(response.evidence),
            document_response=response,
            answer=composition,
        )

    def _execute_relation(
        self,
        operation: RelationKnowledgeOperation,
    ) -> RelationSearchResponse:
        artifact = self.release.relation
        if artifact is None or self.relation_index_path is None:
            raise KnowledgeServiceError("relation retrieval is not present in this release")
        resolved, before = _verify_release_file(
            self.relation_index_path,
            artifact.index_sha256,
        )
        index = SQLiteRelationIndex(resolved)
        manifest = index.manifest()
        if (
            manifest.approval_manifest_sha256 != artifact.approval_manifest_sha256
            or manifest.relation_set_sha256 != artifact.relation_set_sha256
        ):
            raise KnowledgeServiceError("relation manifest differs from its release binding")
        response = index.search(
            RelationSearchRequest(
                query=operation.query,
                top_k=operation.top_k,
                product_families=operation.product_families,
                relation_types=operation.relation_types,
                as_of_on_or_before=operation.as_of_on_or_before,
            ),
            self.relation_database_paths,
            verifier=self.relation_verifier,
        )
        if response.relation_index_sha256 != artifact.index_sha256:
            raise KnowledgeServiceError("relation response index hash differs from release")
        if any(
            evidence.approval_manifest_sha256 != artifact.approval_manifest_sha256
            or evidence.product_family not in operation.product_families
            or evidence.relation_type not in operation.relation_types
            for evidence in response.evidence
        ):
            raise KnowledgeServiceError("relation evidence exceeds the authorized plan")
        if _file_version(resolved) != before:
            raise KnowledgeServiceError("relation index changed during Agent execution")
        _verify_release_file(resolved, artifact.index_sha256)
        return response

    def _execute_document(
        self,
        operation: DocumentKnowledgeOperation,
    ) -> DocumentSearchResponse:
        artifact = self.release.document
        if artifact is None or self.document_index_path is None:
            raise KnowledgeServiceError("document retrieval is not present in this release")
        resolved, before = _verify_release_file(
            self.document_index_path,
            artifact.index_sha256,
        )
        _verify_document_integrity(resolved)
        response = SQLiteDocumentIndex(resolved).search(
            DocumentSearchRequest(
                query=operation.query,
                top_k=operation.top_k,
                filters=DocumentFilters(
                    source_kinds=list(operation.source_kinds),
                    document_ids=list(operation.document_ids),
                    as_of_on_or_before=operation.as_of_on_or_before,
                    metadata_equals=operation.metadata_equals,
                ),
            )
        )
        if any(
            evidence.source_kind not in operation.source_kinds
            or (operation.document_ids and evidence.document_id not in operation.document_ids)
            for evidence in response.evidence
        ):
            raise KnowledgeServiceError("document evidence exceeds the authorized plan")
        if _file_version(resolved) != before:
            raise KnowledgeServiceError("document index changed during Agent execution")
        _verify_release_file(resolved, artifact.index_sha256)
        return response


__all__ = [
    "KnowledgeAgent",
    "KnowledgeAgentResult",
    "KnowledgeServiceError",
]
