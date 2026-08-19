from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from time import perf_counter
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
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.deadline import RequestDeadlineExceeded, raise_if_request_stopped
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    current_request_audit,
)
from finance_agent_core.release import (
    DocumentRetrievalArtifactRelease,
    KnowledgeRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    RelationRetrievalArtifactRelease,
)
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
type _ReadyFile = tuple[Path, Path, tuple[int, int, int, int, int]]


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


def _capture_ready_file(path: Path) -> _ReadyFile:
    _reject_symlink_chain(path)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise KnowledgeServiceError("knowledge runtime artifact is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise KnowledgeServiceError("knowledge runtime artifact must be a single-linked file")
    return path, resolved, _file_version(resolved)


def _assert_ready_file_current(ready_file: _ReadyFile) -> None:
    configured, resolved, expected = ready_file
    _reject_symlink_chain(configured)
    try:
        current_resolved = configured.resolve(strict=True)
    except OSError as error:
        raise KnowledgeServiceError("knowledge runtime artifact is unavailable") from error
    if current_resolved != resolved or _file_version(current_resolved) != expected:
        raise KnowledgeServiceError("knowledge runtime artifact changed after readiness")


class KnowledgeAgent:
    """Exact-plan relation/document retrieval bound to one verified release."""

    def __init__(
        self,
        *,
        release: KnowledgeRetrievalRelease | PublicKnowledgeRetrievalRelease,
        relation_index_path: str | Path | None = None,
        relation_database_paths: Mapping[ProductFamily | str, str | Path] | None = None,
        relation_verifier: ProductDatabaseVerifier | None = None,
        document_index_path: str | Path | None = None,
        claim_provider: KnowledgeClaimProvider | None = None,
        plan_gate: KnowledgePlanAuthorityGate | None = None,
    ) -> None:
        if type(release) not in {
            KnowledgeRetrievalRelease,
            PublicKnowledgeRetrievalRelease,
        }:
            raise TypeError("release must be an internal or public knowledge release")
        if type(release) is PublicKnowledgeRetrievalRelease and claim_provider is not None:
            raise ValueError(
                "the public knowledge release keeps claim generation disabled until "
                "a provider contract is signed"
            )
        if type(release) is PublicKnowledgeRetrievalRelease:
            public_relation = release.relation
            relation_artifact = (
                public_relation.artifact if public_relation.status == "activated" else None
            )
            document_artifact = None
        else:
            relation_artifact = release.relation
            document_artifact = release.document
        if (relation_artifact is None) != (relation_index_path is None):
            raise ValueError("relation release and relation index path must agree")
        if relation_artifact is not None and not relation_database_paths:
            raise ValueError("relation release requires approved product database paths")
        if relation_artifact is None and relation_database_paths:
            raise ValueError("product database paths require a relation release")
        if (document_artifact is None) != (document_index_path is None):
            raise ValueError("document release and document index path must agree")
        self.release = type(release).model_validate_json(release.model_dump_json())
        self._relation_artifact: RelationRetrievalArtifactRelease | None = relation_artifact
        self._document_artifact: DocumentRetrievalArtifactRelease | None = document_artifact
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
        self._ready_files: tuple[_ReadyFile, ...] | None = None

    @property
    def relation_set_sha256(self) -> str | None:
        artifact = self._relation_artifact
        return None if artifact is None else artifact.relation_set_sha256

    def verify_ready(self) -> None:
        """Eagerly verify every configured public artifact and official DB binding."""

        ready_files: list[_ReadyFile] = []
        artifact = self._relation_artifact
        if artifact is not None:
            assert self.relation_index_path is not None
            resolved, before = _verify_release_file(
                self.relation_index_path,
                artifact.index_sha256,
            )
            manifest, digest = SQLiteRelationIndex(resolved).verify_runtime(
                self.relation_database_paths,
                verifier=self.relation_verifier,
            )
            if (
                digest != artifact.index_sha256
                or manifest.approval_manifest_sha256 != artifact.approval_manifest_sha256
                or manifest.relation_set_sha256 != artifact.relation_set_sha256
            ):
                raise KnowledgeServiceError("relation runtime differs from its release binding")
            if _file_version(resolved) != before:
                raise KnowledgeServiceError("relation index changed during readiness verification")
            ready_files.append(_capture_ready_file(self.relation_index_path))
            ready_files.extend(
                _capture_ready_file(path) for path in self.relation_database_paths.values()
            )
        document_artifact = self._document_artifact
        if document_artifact is not None:
            assert self.document_index_path is not None
            resolved, before = _verify_release_file(
                self.document_index_path,
                document_artifact.index_sha256,
            )
            _verify_document_integrity(resolved)
            if _file_version(resolved) != before:
                raise KnowledgeServiceError("document index changed during readiness verification")
            ready_files.append(_capture_ready_file(self.document_index_path))
        for ready_file in ready_files:
            _assert_ready_file_current(ready_file)
        self._ready_files = tuple(ready_files)

    def assert_ready_current(self) -> None:
        """Cheap readiness probe that detects post-start path or inode drift."""

        if self._ready_files is None:
            raise KnowledgeServiceError("knowledge runtime readiness was not verified")
        for ready_file in self._ready_files:
            _assert_ready_file_current(ready_file)

    def execute(
        self,
        server_plan: KnowledgeQueryPlan,
        proposal: KnowledgeQueryPlan | None = None,
        *,
        proposal_provider_name: str | None = None,
        proposal_model_name: str | None = None,
    ) -> KnowledgeAgentResult:
        plan_sha256 = canonical_knowledge_plan_sha256(server_plan)
        authority_started = perf_counter()
        try:
            raise_if_request_stopped()
            validated = self.plan_gate.authorize(
                server_plan,
                proposal,
                proposal_provider_name=proposal_provider_name,
                proposal_model_name=proposal_model_name,
            )
            raise_if_request_stopped()
        except RequestDeadlineExceeded:
            self._emit_relation_audit(
                server_plan,
                stage=AuditStage.AUTHORITY,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="knowledge_authority_timed_out",
                duration_ms=(perf_counter() - authority_started) * 1000,
                plan_sha256=plan_sha256,
            )
            raise
        except Exception:
            self._emit_relation_audit(
                server_plan,
                stage=AuditStage.AUTHORITY,
                outcome=AuditOutcome.FAILED,
                reason_code="knowledge_authority_rejected",
                duration_ms=(perf_counter() - authority_started) * 1000,
                plan_sha256=plan_sha256,
            )
            raise
        plan = validated.plan
        plan_sha256 = validated.receipt.plan_sha256
        self._emit_relation_audit(
            plan,
            stage=AuditStage.AUTHORITY,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="knowledge_authority_granted",
            duration_ms=(perf_counter() - authority_started) * 1000,
            plan_sha256=plan_sha256,
        )
        if isinstance(plan.operation, RelationKnowledgeOperation):
            response = self._execute_relation(
                plan,
                plan_sha256=plan_sha256,
            )
            context = build_knowledge_answer_context(plan, response)
            composition_started = perf_counter()
            try:
                raise_if_request_stopped()
                composition = compose_knowledge_answer(context, self.claim_provider)
                raise_if_request_stopped()
            except RequestDeadlineExceeded:
                provider_used = self.claim_provider is not None and bool(response.evidence)
                self._emit_relation_audit(
                    plan,
                    stage=(AuditStage.HCLX if provider_used else AuditStage.RENDERER),
                    outcome=AuditOutcome.TIMED_OUT,
                    reason_code=(
                        "knowledge_generation_timed_out"
                        if provider_used
                        else "knowledge_rendering_timed_out"
                    ),
                    duration_ms=(perf_counter() - composition_started) * 1000,
                    plan_sha256=plan_sha256,
                    candidate_count=len(response.evidence),
                )
                raise
            self._emit_relation_composition_audit(
                plan,
                composition,
                candidate_count=len(response.evidence),
                plan_sha256=plan_sha256,
            )
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
        plan: KnowledgeQueryPlan,
        *,
        plan_sha256: str,
    ) -> RelationSearchResponse:
        operation = plan.operation
        assert isinstance(operation, RelationKnowledgeOperation)
        lookup_started = perf_counter()
        try:
            raise_if_request_stopped()
            artifact = self._relation_artifact
            if artifact is None or self.relation_index_path is None:
                raise KnowledgeServiceError("relation retrieval is not present in this release")
            resolved, before = _verify_release_file(
                self.relation_index_path,
                artifact.index_sha256,
            )
            raise_if_request_stopped()
            index = SQLiteRelationIndex(resolved)
            manifest = index.manifest()
            raise_if_request_stopped()
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
            raise_if_request_stopped()
        except RequestDeadlineExceeded:
            self._emit_relation_audit(
                plan,
                stage=AuditStage.SQL,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="relation_lookup_timed_out",
                duration_ms=(perf_counter() - lookup_started) * 1000,
                plan_sha256=plan_sha256,
            )
            raise
        except sqlite3.OperationalError as error:
            try:
                raise_if_request_stopped()
            except RequestDeadlineExceeded as timeout:
                self._emit_relation_audit(
                    plan,
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.TIMED_OUT,
                    reason_code="relation_lookup_timed_out",
                    duration_ms=(perf_counter() - lookup_started) * 1000,
                    plan_sha256=plan_sha256,
                )
                raise timeout from error
            self._emit_relation_audit(
                plan,
                stage=AuditStage.SQL,
                outcome=AuditOutcome.FAILED,
                reason_code="relation_lookup_failed",
                duration_ms=(perf_counter() - lookup_started) * 1000,
                plan_sha256=plan_sha256,
            )
            raise
        except Exception:
            self._emit_relation_audit(
                plan,
                stage=AuditStage.SQL,
                outcome=AuditOutcome.FAILED,
                reason_code="relation_lookup_failed",
                duration_ms=(perf_counter() - lookup_started) * 1000,
                plan_sha256=plan_sha256,
            )
            raise
        self._emit_relation_audit(
            plan,
            stage=AuditStage.SQL,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="relation_lookup_completed",
            duration_ms=(perf_counter() - lookup_started) * 1000,
            plan_sha256=plan_sha256,
            candidate_count=len(response.evidence),
            result_count=len(response.evidence),
        )
        verification_started = perf_counter()
        try:
            raise_if_request_stopped()
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
            raise_if_request_stopped()
        except RequestDeadlineExceeded:
            self._emit_relation_audit(
                plan,
                stage=AuditStage.VERIFIER,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="relation_verification_timed_out",
                duration_ms=(perf_counter() - verification_started) * 1000,
                plan_sha256=plan_sha256,
                candidate_count=len(response.evidence),
            )
            raise
        except Exception:
            self._emit_relation_audit(
                plan,
                stage=AuditStage.VERIFIER,
                outcome=AuditOutcome.FAILED,
                reason_code="relation_evidence_rejected",
                duration_ms=(perf_counter() - verification_started) * 1000,
                plan_sha256=plan_sha256,
                candidate_count=len(response.evidence),
            )
            raise
        self._emit_relation_audit(
            plan,
            stage=AuditStage.VERIFIER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="relation_evidence_verified",
            duration_ms=(perf_counter() - verification_started) * 1000,
            plan_sha256=plan_sha256,
            candidate_count=len(response.evidence),
            result_count=len(response.evidence),
            evidence_count=len(response.evidence),
            evidence_ids=(item.evidence_id for item in response.evidence),
        )
        return response

    def _emit_relation_composition_audit(
        self,
        plan: KnowledgeQueryPlan,
        composition: KnowledgeAnswerComposition,
        *,
        candidate_count: int,
        plan_sha256: str,
    ) -> None:
        if composition.provider_name is not None:
            self._emit_relation_audit(
                plan,
                stage=AuditStage.HCLX,
                outcome=(
                    AuditOutcome.SUCCEEDED if composition.draft is not None else AuditOutcome.FAILED
                ),
                reason_code=(
                    "knowledge_generation_completed"
                    if composition.draft is not None
                    else "knowledge_generation_failed"
                ),
                duration_ms=composition.generation_latency_ms,
                plan_sha256=plan_sha256,
                candidate_count=candidate_count,
            )
        self._emit_relation_audit(
            plan,
            stage=AuditStage.VERIFIER,
            outcome=(
                AuditOutcome.SUCCEEDED if composition.verification.passed else AuditOutcome.FAILED
            ),
            reason_code=(
                "knowledge_claims_verified"
                if composition.verification.passed
                else "knowledge_claims_rejected"
            ),
            duration_ms=0,
            plan_sha256=plan_sha256,
            candidate_count=candidate_count,
            result_count=(candidate_count if composition.verification.passed else 0),
        )
        self._emit_relation_audit(
            plan,
            stage=AuditStage.RENDERER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="knowledge_rendering_completed",
            duration_ms=0,
            plan_sha256=plan_sha256,
            candidate_count=candidate_count,
            result_count=candidate_count,
        )

    def _emit_relation_audit(
        self,
        plan: KnowledgeQueryPlan,
        *,
        stage: AuditStage,
        outcome: AuditOutcome,
        reason_code: str,
        duration_ms: float,
        plan_sha256: str,
        candidate_count: int = 0,
        result_count: int = 0,
        evidence_count: int = 0,
        evidence_ids: Iterable[str] = (),
    ) -> None:
        operation = plan.operation
        if not isinstance(operation, RelationKnowledgeOperation):
            return
        audit = current_request_audit()
        if audit is None:
            return
        audit.emit(
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=duration_ms,
            route_disposition=RouteDisposition.EXECUTE,
            interaction_intent=InteractionIntent.SEARCH,
            product_families=tuple(operation.product_families),
            plan_sha256=plan_sha256,
            relation_set_sha256=self.relation_set_sha256,
            candidate_count=candidate_count,
            result_count=result_count,
            evidence_count=evidence_count,
            evidence_ids=evidence_ids,
        )

    def _execute_document(
        self,
        operation: DocumentKnowledgeOperation,
    ) -> DocumentSearchResponse:
        raise_if_request_stopped()
        artifact = self._document_artifact
        if artifact is None or self.document_index_path is None:
            raise KnowledgeServiceError("document retrieval is not present in this release")
        resolved, before = _verify_release_file(
            self.document_index_path,
            artifact.index_sha256,
        )
        _verify_document_integrity(resolved)
        raise_if_request_stopped()
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
        raise_if_request_stopped()
        return response


__all__ = [
    "KnowledgeAgent",
    "KnowledgeAgentResult",
    "KnowledgeServiceError",
]
