from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import Intent, ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.evaluation.external_holdout import (
    EXTERNAL_FAMILY_QUOTAS,
    EXTERNAL_INTENT_QUOTAS,
    assess_external_near_duplicates,
)
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactError,
    SchemaEmbeddingArtifactGateEvidence,
    SchemaEmbeddingSnapshotManifestV2,
    VerifiedSentenceTransformerCpuProvider,
    load_schema_embedding_candidate_link,
    load_verified_schema_embedding_cpu_provider,
    require_schema_embedding_artifact_gate,
)
from finance_agent_core.retrieval.schema_dense import DenseSchemaIndex, build_schema_field_entries

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_PROTOCOL_ID = "schema-embedding-external-blind-v2"
_CANDIDATE_ORDER = ("lexical", "bge-m3", "kure-v1")
_MODEL_ORDER = ("bge-m3", "kure-v1")
_OOD_SPLIT_PREFIX = f"{_PROTOCOL_ID}:"
_TOP_K = 10
_BOOTSTRAP_ITERATIONS = 10_000
_BOOTSTRAP_SEED = 20_260_812
_IMAGE_REFERENCE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$"
_REFERENCE_ALGORITHM = "normalized_sequence_matcher_v1"
_NEAR_DUPLICATE_MAX_SIMILARITY = 0.84
_MAX_PROTOCOL_BYTES = 2 * 1024 * 1024
_MAX_QUESTION_BUNDLE_BYTES = 4 * 1024 * 1024
_MAX_PRIVATE_ANSWER_BYTES = 16 * 1024 * 1024
_MAX_COMMITMENT_BYTES = 2 * 1024 * 1024
_MAX_AUTHORIZATION_BYTES = 2 * 1024 * 1024
_MAX_PREDICTION_BYTES = 32 * 1024 * 1024
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_MAX_REFERENCE_SUITE_BYTES = 8 * 1024 * 1024
_MAX_MODEL_MANIFEST_BYTES = 2 * 1024 * 1024
_TRACKED_PROTOCOL_SHA256 = "23652fd23696aa5885ec096da3d8aba0fc1063a677e3010012211ab62e554346"


@lru_cache(maxsize=1)
def _registry_datasets_by_field() -> dict[str, frozenset[str]]:
    registry = load_field_registry()
    return {field_id: frozenset(field.datasets) for field_id, field in registry.fields.items()}


class ExternalSchemaBlindV2Error(RuntimeError):
    """Base error for the standalone two-phase blind protocol."""


class ExternalBundleUnavailableError(ExternalSchemaBlindV2Error):
    """Raised before scoring when the complete external bundle is unavailable."""


class CandidateLockError(ExternalSchemaBlindV2Error):
    """Raised before prediction when either frozen model artifact is not usable."""


class ExternalSchemaBlindV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalBlindProtocolModel(BaseModel):
    """Strict nested protocol records while permitting unrelated top-level sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtocolLexicalCandidate(ExternalBlindProtocolModel):
    priority: Literal[0]
    alias: Literal["lexical"]
    algorithm: Literal["server_build_lexical_hints_v1"]
    role: Literal["frozen_baseline"]


class ProtocolModelCandidate(ExternalBlindProtocolModel):
    priority: Literal[1, 2]
    alias: Literal["bge-m3", "kure-v1"]
    repository: str = Field(min_length=3, max_length=256)
    revision: str = Field(pattern=_COMMIT_PATTERN)
    snapshot_manifest_path: str = Field(min_length=3, max_length=500)
    snapshot_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    weights_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    tokenizer_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    config_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    other_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)


class ProtocolReferenceSuite(ExternalBlindProtocolModel):
    resource_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,127}\.json$")
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_count: Literal[50]


class ProtocolReferenceCorpus(ExternalBlindProtocolModel):
    algorithm: Literal["normalized_sequence_matcher_v1"] = _REFERENCE_ALGORITHM
    near_duplicate_max_similarity: Literal[0.84] = _NEAR_DUPLICATE_MAX_SIMILARITY
    source_evaluation_id: Literal["schema-embedding-cpu-public-v1"]
    source_manifest_resource_name: Literal["schema_embedding_cpu_public_v1.json"]
    source_manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_migration_resource_name: Literal["schema_linker_policy_migrations_v1.json"]
    policy_migration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_question_count: Literal[200]
    reference_corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    suites: tuple[ProtocolReferenceSuite, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def require_unique_reference_suites(self) -> ProtocolReferenceCorpus:
        names = tuple(item.resource_name for item in self.suites)
        if len(names) != len(set(names)):
            raise ValueError("external blind reference suite resources must be unique")
        return self


class ExternalBlindV2ProtocolLock(BaseModel):
    """Security-relevant subset read from the tracked v2 protocol file."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal["2.0"]
    protocol_id: Literal["schema-embedding-external-blind-v2"]
    candidate_lock: tuple[ProtocolLexicalCandidate | ProtocolModelCandidate, ...] = Field(
        min_length=3,
        max_length=3,
    )
    reference_corpus: ProtocolReferenceCorpus

    @model_validator(mode="after")
    def require_frozen_candidate_order(self) -> ExternalBlindV2ProtocolLock:
        aliases = tuple(item.alias for item in self.candidate_lock)
        if aliases != _CANDIDATE_ORDER:
            raise ValueError("tracked protocol candidate order differs")
        priorities = tuple(item.priority for item in self.candidate_lock)
        if priorities != (0, 1, 2):
            raise ValueError("tracked protocol candidate priorities differ")
        return self

    @property
    def model_candidates(self) -> tuple[ProtocolModelCandidate, ProtocolModelCandidate]:
        values = self.candidate_lock[1:]
        if not all(isinstance(item, ProtocolModelCandidate) for item in values):
            raise CandidateLockError("tracked protocol model candidate shape differs")
        return values[0], values[1]  # type: ignore[return-value]


class ExternalBlindQuestionOnlyCase(ExternalSchemaBlindV2Model):
    """Phase-1 input with no family, intent, disposition, or author rationale."""

    id: str = Field(pattern=r"^external-blind-v1-\d{3}$")
    question: str = Field(min_length=5, max_length=2000)


class ExternalBlindQuestionOnlySet(ExternalSchemaBlindV2Model):
    schema_version: Literal["2.0"] = "2.0"
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["question_only_without_gold_labels"]
    cases: tuple[ExternalBlindQuestionOnlyCase, ...] = Field(min_length=100, max_length=100)

    @model_validator(mode="after")
    def require_ordered_unique_questions(self) -> ExternalBlindQuestionOnlySet:
        expected = [f"external-blind-v1-{index:03d}" for index in range(1, 101)]
        if [item.id for item in self.cases] != expected:
            raise ValueError("question-only blind IDs must be ordered 001 through 100")
        normalized = ["".join(item.question.casefold().split()) for item in self.cases]
        if len(normalized) != len(set(normalized)):
            raise ValueError("question-only blind questions must be unique")
        return self


class ExternalBlindV2ReferenceReport(ExternalSchemaBlindV2Model):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["schema-embedding-external-blind-v2"] = _PROTOCOL_ID
    status: Literal["passed_zero_near_duplicates"] = "passed_zero_near_duplicates"
    algorithm: Literal["normalized_sequence_matcher_v1"] = _REFERENCE_ALGORITHM
    near_duplicate_max_similarity: Literal[0.84] = _NEAR_DUPLICATE_MAX_SIMILARITY
    reference_corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_question_count: Literal[200]
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_question_count: Literal[100]
    maximum_observed_similarity: float = Field(ge=0, lt=_NEAR_DUPLICATE_MAX_SIMILARITY)
    violation_count: Literal[0] = 0


class ExternalBlindV2Commitment(ExternalSchemaBlindV2Model):
    schema_version: Literal["2.0"] = "2.0"
    protocol_id: Literal["schema-embedding-external-blind-v2"] = _PROTOCOL_ID
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["sealed_external_before_reveal"]
    author_role: Literal["financial_domain"]
    implementation_commit: str = Field(pattern=_COMMIT_PATTERN)
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    answers_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_count: Literal[100]
    created_at_utc: str = Field(pattern=_UTC_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    near_duplicate_max_similarity: Literal[0.84] = _NEAR_DUPLICATE_MAX_SIMILARITY
    near_duplicate_report_sha256: str = Field(pattern=_SHA256_PATTERN)


class ExternalBlindPrivateAnswer(ExternalSchemaBlindV2Model):
    """All routing and execution labels remain in the private phase-2 key."""

    id: str = Field(pattern=r"^external-blind-v1-\d{3}$")
    expected_product_family: ProductFamily
    expected_interaction_intent: InteractionIntent
    expected_disposition: RouteDisposition
    expected_query_plan_intent: Intent | None
    expected_query_plan: dict[str, Any] | None
    gold_schema_field_ids: tuple[str, ...] = Field(max_length=_TOP_K)
    expected_candidate_count: int | None = Field(default=None, ge=0)
    expected_product_ids: tuple[str, ...] = Field(default=(), max_length=100)
    required_answer_checks: tuple[str, ...] = Field(default=(), max_length=30)
    rationale: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def validate_private_answer(self) -> ExternalBlindPrivateAnswer:
        if self.expected_disposition is RouteDisposition.EXECUTE:
            if (
                self.expected_query_plan_intent is None
                or self.expected_query_plan is None
                or self.expected_candidate_count is None
            ):
                raise ValueError("executable private answers require plan and Oracle gold")
            if not self.gold_schema_field_ids:
                raise ValueError("executable private answers require explicit schema-field gold")
            if len(self.gold_schema_field_ids) != len(set(self.gold_schema_field_ids)):
                raise ValueError("schema-field gold IDs must be unique")
            plan = QueryPlan.model_validate(self.expected_query_plan)
            if plan.intent is not self.expected_query_plan_intent:
                raise ValueError("private QueryPlan intent differs")
            if plan.product_families != [self.expected_product_family]:
                raise ValueError("private QueryPlan product family differs")
            if len(self.expected_product_ids) > self.expected_candidate_count:
                raise ValueError("private product IDs exceed candidate count")
            registry_datasets = _registry_datasets_by_field()
            for field_id in self.gold_schema_field_ids:
                datasets = registry_datasets.get(field_id)
                if datasets is None:
                    raise ValueError(f"unknown schema-field gold ID: {field_id}")
                if self.expected_product_family.value not in datasets:
                    raise ValueError(
                        f"schema-field gold ID is outside the expected product family: {field_id}"
                    )
        elif (
            self.expected_query_plan_intent is not None
            or self.expected_query_plan is not None
            or self.gold_schema_field_ids
            or self.expected_candidate_count is not None
            or self.expected_product_ids
        ):
            raise ValueError("control private answers cannot contain executable gold")
        return self


class ExternalBlindPrivateAnswerKey(ExternalSchemaBlindV2Model):
    schema_version: Literal["2.0"] = "2.0"
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["private_labels_and_gold_before_reveal"]
    reviewer_role: Literal["financial_domain"]
    database_sha256_by_family: dict[ProductFamily, str]
    cases: tuple[ExternalBlindPrivateAnswer, ...] = Field(min_length=100, max_length=100)

    @model_validator(mode="after")
    def validate_private_key(self) -> ExternalBlindPrivateAnswerKey:
        expected = [f"external-blind-v1-{index:03d}" for index in range(1, 101)]
        if [item.id for item in self.cases] != expected:
            raise ValueError("private blind IDs must be ordered 001 through 100")
        if set(self.database_sha256_by_family) != set(ProductFamily):
            raise ValueError("private key requires four database SHA-256 values")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.database_sha256_by_family.values()
        ):
            raise ValueError("private database hashes must be lowercase SHA-256")
        family_counts = Counter(item.expected_product_family.value for item in self.cases)
        intent_counts = Counter(item.expected_interaction_intent.value for item in self.cases)
        if dict(family_counts) != EXTERNAL_FAMILY_QUOTAS:
            raise ValueError("private answer family quotas differ")
        if dict(intent_counts) != EXTERNAL_INTENT_QUOTAS:
            raise ValueError("private answer intent quotas differ")
        return self


class ExternalBlindExecutionAuthorization(ExternalSchemaBlindV2Model):
    """Evaluator-owned pre-run trust anchor; code validates binding, not authorship."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["authorized_by_independent_evaluator"]
    evaluator_role: Literal["independent_external_evaluator"]
    implementation_commit: str = Field(pattern=_COMMIT_PATTERN)
    image_reference: str = Field(pattern=_IMAGE_REFERENCE_PATTERN)
    platform: Literal["linux/amd64", "linux/arm64"]
    clean_source_tree: Literal[True]
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    near_duplicate_max_similarity: Literal[0.84] = _NEAR_DUPLICATE_MAX_SIMILARITY
    near_duplicate_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    bge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    kure_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at_utc: str = Field(pattern=_UTC_PATTERN)
    external_authorization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)


class ExternalBlindPredictionReceipt(ExternalSchemaBlindV2Model):
    """Append-only receipt created outside the evaluated image after prediction."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["prediction_hash_recorded_externally_before_answer_reveal"]
    evaluator_role: Literal["independent_external_evaluator"]
    prediction_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation_commit: str = Field(pattern=_COMMIT_PATTERN)
    image_reference: str = Field(pattern=_IMAGE_REFERENCE_PATTERN)
    recorded_at_utc: str = Field(pattern=_UTC_PATTERN)
    external_locator: str = Field(min_length=8, max_length=500)


def validate_external_blind_v2_bundle(
    questions: ExternalBlindQuestionOnlySet,
    answers: ExternalBlindPrivateAnswerKey,
) -> None:
    if questions.suite_id != answers.suite_id:
        raise ValueError("question and private-answer suite IDs differ")
    if [item.id for item in questions.cases] != [item.id for item in answers.cases]:
        raise ValueError("question and private-answer IDs differ")
    for question, answer in zip(questions.cases, answers.cases, strict=True):
        if answer.expected_query_plan is not None:
            plan = QueryPlan.model_validate(answer.expected_query_plan)
            if plan.question_id != question.id:
                raise ValueError(f"{question.id}: private QueryPlan question_id differs")


# Public compatibility name for the artifact contract consumed by this runner.
# The byte-level scanner and validator live in schema_embedding_artifacts; this
# module only binds both verified manifests into the blind execution lock.
FrozenModelArtifactManifest = SchemaEmbeddingSnapshotManifestV2


class FrozenModelArtifactBinding(ExternalSchemaBlindV2Model):
    priority: int = Field(ge=1, le=2)
    alias: Literal["bge-m3", "kure-v1"]
    repository: str
    revision: str = Field(pattern=_COMMIT_PATTERN)
    manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    weights_sha256: str = Field(pattern=_SHA256_PATTERN)
    tokenizer_sha256: str = Field(pattern=_SHA256_PATTERN)
    config_sha256: str = Field(pattern=_SHA256_PATTERN)
    other_sha256: str = Field(pattern=_SHA256_PATTERN)
    verification_mode: Literal["shadow"] = "shadow"
    verification_status: Literal["verified_prerequisite"] = "verified_prerequisite"
    approval_scope: Literal["artifact_identity_only_not_activation_approval"] = (
        "artifact_identity_only_not_activation_approval"
    )


class CandidateExecutionLock(ExternalSchemaBlindV2Model):
    protocol_id: Literal["schema-embedding-external-blind-v2"] = _PROTOCOL_ID
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation_commit: str = Field(pattern=_COMMIT_PATTERN)
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    answer_key_sha256_commitment: str = Field(pattern=_SHA256_PATTERN)
    reference_corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    near_duplicate_max_similarity: Literal[0.84] = _NEAR_DUPLICATE_MAX_SIMILARITY
    near_duplicate_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_reference: str = Field(pattern=_IMAGE_REFERENCE_PATTERN)
    platform: Literal["linux/amd64", "linux/arm64"]
    external_authorization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    commitment_created_at_utc: str = Field(pattern=_UTC_PATTERN)
    authorization_issued_at_utc: str = Field(pattern=_UTC_PATTERN)
    candidate_order: tuple[Literal["lexical", "bge-m3", "kure-v1"], ...]
    fusion_strategy: Literal["lexical_first"] = "lexical_first"
    top_k: Literal[10] = 10
    lexical_algorithm: Literal["server_build_lexical_hints_v1"] = "server_build_lexical_hints_v1"
    model_artifacts: tuple[FrozenModelArtifactBinding, ...]
    bootstrap_iterations: Literal[10000] = _BOOTSTRAP_ITERATIONS
    bootstrap_seed: Literal[20260812] = _BOOTSTRAP_SEED
    ood_split: Literal["sha256_ordered_half_split_v1"] = "sha256_ordered_half_split_v1"

    @model_validator(mode="after")
    def require_candidate_and_artifact_order(self) -> CandidateExecutionLock:
        if tuple(self.candidate_order) != _CANDIDATE_ORDER:
            raise ValueError("blind v2 candidate order must be lexical, BGE-M3, KURE-v1")
        observed = tuple((item.priority, item.alias) for item in self.model_artifacts)
        if observed != ((1, "bge-m3"), (2, "kure-v1")):
            raise ValueError("blind v2 requires BGE-M3 then KURE-v1 artifact bindings")
        if _utc_instant(self.commitment_created_at_utc) > _utc_instant(
            self.authorization_issued_at_utc
        ):
            raise ValueError("blind commitment must not be after execution authorization")
        return self


class RankedField(ExternalSchemaBlindV2Model):
    product_family: ProductFamily
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    score: float = Field(ge=-1.000001, le=1.000001)
    rank: int = Field(ge=1, le=_TOP_K)


class OfflineOodProbe(ExternalSchemaBlindV2Model):
    """Dense confidence produced outside the operational execution path."""

    isolation: Literal["offline_only_no_execution_authority"] = (
        "offline_only_no_execution_authority"
    )
    family_source: Literal["all_four_approved_families_without_hidden_label_access"] = (
        "all_four_approved_families_without_hidden_label_access"
    )
    candidates: tuple[RankedField, ...] = Field(min_length=2, max_length=_TOP_K)
    top_1_score: float = Field(ge=-1.000001, le=1.000001)
    top_1_top_2_margin: float = Field(ge=0, le=2.000002)

    @model_validator(mode="after")
    def require_score_summary(self) -> OfflineOodProbe:
        if self.top_1_score != self.candidates[0].score:
            raise ValueError("OOD top-1 score differs from the first candidate")
        expected_margin = round(self.candidates[0].score - self.candidates[1].score, 9)
        if self.top_1_top_2_margin != expected_margin:
            raise ValueError("OOD top-1/top-2 margin differs from candidates")
        return self


class ModelCasePrediction(ExternalSchemaBlindV2Model):
    alias: Literal["bge-m3", "kure-v1"]
    operational_dense_called: bool
    dense_candidates: tuple[RankedField, ...] = Field(max_length=_TOP_K)
    fused_fields: tuple[str, ...] = Field(max_length=_TOP_K)
    offline_ood_probe: OfflineOodProbe

    @model_validator(mode="after")
    def require_operational_call_shape(self) -> ModelCasePrediction:
        if self.operational_dense_called != bool(self.dense_candidates):
            raise ValueError("operational Dense call marker and candidates differ")
        if not self.operational_dense_called and self.fused_fields:
            raise ValueError("control or unroutable cases cannot expose fused fields")
        return self


class ExternalCasePrediction(ExternalSchemaBlindV2Model):
    case_id: str = Field(pattern=r"^external-blind-v1-\d{3}$")
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    route_disposition: RouteDisposition
    route_interaction_intent: InteractionIntent
    route_product_families: tuple[ProductFamily, ...] = Field(max_length=4)
    route_query_plan_intent: Intent | None
    route_reason_code: str = Field(min_length=1, max_length=100)
    lexical_fields: tuple[str, ...] = Field(max_length=_TOP_K)
    models: tuple[ModelCasePrediction, ...]

    @model_validator(mode="after")
    def validate_operational_isolation(self) -> ExternalCasePrediction:
        if tuple(item.alias for item in self.models) != _MODEL_ORDER:
            raise ValueError("case model predictions must preserve BGE-M3, KURE-v1 order")
        operational_allowed = (
            self.route_disposition is RouteDisposition.EXECUTE
            and len(self.route_product_families) == 1
        )
        if bool(self.lexical_fields) and not operational_allowed:
            raise ValueError("control or unroutable cases cannot expose lexical candidates")
        if any(item.operational_dense_called != operational_allowed for item in self.models):
            raise ValueError(
                "operational Dense calls must exactly follow the executable route gate"
            )
        if self.route_disposition is not RouteDisposition.EXECUTE:
            if any(item.operational_dense_called for item in self.models):
                raise ValueError("control routes must not call operational Dense providers")
        return self


class QuestionOnlyPredictionArtifact(ExternalSchemaBlindV2Model):
    schema_version: Literal["2.0"] = "2.0"
    protocol_id: Literal["schema-embedding-external-blind-v2"] = _PROTOCOL_ID
    status: Literal["local_predictions_awaiting_external_receipt"] = (
        "local_predictions_awaiting_external_receipt"
    )
    answer_key_opened: Literal[False] = False
    created_at_utc: str = Field(pattern=_UTC_PATTERN)
    lock: CandidateExecutionLock
    cases: tuple[ExternalCasePrediction, ...]
    predicted_non_execute_operational_dense_call_count: Literal[0] = 0
    offline_ood_probe_scope: Literal["all_external_cases"] = "all_external_cases"

    @model_validator(mode="after")
    def validate_prediction_population(self) -> QuestionOnlyPredictionArtifact:
        if len(self.cases) != 100:
            raise ValueError("blind v2 requires predictions for all 100 external cases")
        expected_ids = [f"external-blind-v1-{index:03d}" for index in range(1, 101)]
        if [item.case_id for item in self.cases] != expected_ids:
            raise ValueError("blind v2 predictions must preserve external case order")
        control_dense_calls = sum(
            model.operational_dense_called
            for case in self.cases
            if case.route_disposition is not RouteDisposition.EXECUTE
            for model in case.models
        )
        if control_dense_calls:
            raise ValueError("control routes reached an operational Dense provider")
        if _utc_instant(self.lock.authorization_issued_at_utc) > _utc_instant(self.created_at_utc):
            raise ValueError("execution authorization must not be after prediction")
        return self


class RoutingCaseScore(ExternalSchemaBlindV2Model):
    case_id: str
    expected_disposition: RouteDisposition
    actual_disposition: RouteDisposition
    disposition_exact: bool
    expected_family: ProductFamily | None
    actual_families: tuple[ProductFamily, ...]
    family_exact: bool | None
    expected_interaction_intent: InteractionIntent
    actual_interaction_intent: InteractionIntent
    interaction_intent_exact: bool
    expected_query_plan_intent: Intent | None
    actual_query_plan_intent: Intent | None
    query_plan_intent_exact: bool


class RoutingScoreSummary(ExternalSchemaBlindV2Model):
    case_count: int = Field(ge=1)
    disposition_exact_count: int = Field(ge=0)
    disposition_accuracy: float = Field(ge=0, le=1)
    family_scored_case_count: int = Field(ge=0)
    family_exact_count: int = Field(ge=0)
    family_accuracy: float = Field(ge=0, le=1)
    interaction_intent_exact_count: int = Field(ge=0)
    interaction_intent_accuracy: float = Field(ge=0, le=1)
    query_plan_intent_exact_count: int = Field(ge=0)
    query_plan_intent_accuracy: float = Field(ge=0, le=1)
    control_case_count: int = Field(ge=0)
    control_operational_dense_call_count: int = Field(ge=0)
    control_no_operational_dense_case_count: int = Field(ge=0)
    control_no_operational_dense_rate: float = Field(ge=0, le=1)
    control_operational_dense_gate_passed: bool

    @model_validator(mode="after")
    def validate_control_dense_metrics(self) -> RoutingScoreSummary:
        if self.control_no_operational_dense_case_count > self.control_case_count:
            raise ValueError("control Dense no-call count exceeds the control population")
        called_case_count = self.control_case_count - self.control_no_operational_dense_case_count
        expected_calls = called_case_count * len(_MODEL_ORDER)
        if self.control_operational_dense_call_count != expected_calls:
            raise ValueError(
                "control Dense provider calls differ from the frozen per-case model calls"
            )
        expected_rate = (
            round(self.control_no_operational_dense_case_count / self.control_case_count, 6)
            if self.control_case_count
            else 1.0
        )
        if self.control_no_operational_dense_rate != expected_rate:
            raise ValueError("control Dense no-call rate differs from the measured counts")
        expected_gate = (
            self.control_operational_dense_call_count == 0
            and self.control_no_operational_dense_case_count == self.control_case_count
        )
        if self.control_operational_dense_gate_passed is not expected_gate:
            raise ValueError("control Dense gate differs from the measured calls")
        return self


class FieldScore(ExternalSchemaBlindV2Model):
    candidate: Literal["lexical", "bge-m3", "kure-v1"]
    scored_case_count: int = Field(ge=0)
    gold_field_count: int = Field(ge=0)
    exact_at_gold_cardinality_count: int = Field(ge=0)
    exact_at_gold_cardinality: float = Field(ge=0, le=1)
    hits_at_5: int = Field(ge=0)
    micro_recall_at_5: float = Field(ge=0, le=1)
    hits_at_10: int = Field(ge=0)
    micro_recall_at_10: float = Field(ge=0, le=1)


class BootstrapDelta(ExternalSchemaBlindV2Model):
    selected_value: float = Field(ge=0, le=1)
    comparator_value: float = Field(ge=0, le=1)
    observed_delta: float = Field(ge=-1, le=1)
    ci95_lower: float = Field(ge=-1, le=1)
    ci95_upper: float = Field(ge=-1, le=1)
    probability_selected_greater: float = Field(ge=0, le=1)


class BlindPairedComparison(ExternalSchemaBlindV2Model):
    selected: Literal["bge-m3", "kure-v1"]
    comparator: Literal["lexical", "bge-m3", "kure-v1"]
    case_count: int = Field(ge=1)
    exact: BootstrapDelta
    recall_at_5: BootstrapDelta


class OodPopulationScore(ExternalSchemaBlindV2Model):
    case_count: int = Field(ge=0)
    execute_count: int = Field(ge=0)
    control_count: int = Field(ge=0)
    true_accept_count: int = Field(ge=0)
    false_accept_count: int = Field(ge=0)
    false_reject_count: int = Field(ge=0)
    execute_false_reject_rate: float = Field(ge=0, le=1)


class OodThresholdEvaluation(ExternalSchemaBlindV2Model):
    alias: Literal["bge-m3", "kure-v1"]
    split_method: Literal["sha256_ordered_half_split_v1"] = "sha256_ordered_half_split_v1"
    calibration_case_ids: tuple[str, ...]
    test_case_ids: tuple[str, ...]
    threshold_status: Literal["selected", "rejected_no_usable_threshold"]
    score_threshold: float | None = None
    margin_threshold: float | None = None
    calibration: OodPopulationScore
    test: OodPopulationScore | None
    test_gate_passed: bool

    @model_validator(mode="after")
    def validate_threshold_state(self) -> OodThresholdEvaluation:
        selected = self.threshold_status == "selected"
        if selected != (self.score_threshold is not None and self.margin_threshold is not None):
            raise ValueError("OOD threshold status and threshold values differ")
        if selected != (self.test is not None):
            raise ValueError("OOD test may run only for a selected calibration threshold")
        if not selected and self.test_gate_passed:
            raise ValueError("a rejected OOD threshold cannot pass the test gate")
        return self


class ExternalBlindV2ScoreReport(ExternalSchemaBlindV2Model):
    schema_version: Literal["2.0"] = "2.0"
    protocol_id: Literal["schema-embedding-external-blind-v2"] = _PROTOCOL_ID
    status: Literal["revealed_external_bundle_scored_once"] = "revealed_external_bundle_scored_once"
    scored_at_utc: str = Field(pattern=_UTC_PATTERN)
    implementation_commit: str = Field(pattern=_COMMIT_PATTERN)
    questions_sha256: str = Field(pattern=_SHA256_PATTERN)
    answers_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_receipt_locator: str = Field(min_length=8, max_length=500)
    candidate_order: tuple[Literal["lexical", "bge-m3", "kure-v1"], ...]
    routing: RoutingScoreSummary
    routing_cases: tuple[RoutingCaseScore, ...]
    field_scores: tuple[FieldScore, ...]
    paired_bootstrap: tuple[BlindPairedComparison, ...]
    ood_thresholds: tuple[OodThresholdEvaluation, ...]

    @model_validator(mode="after")
    def preserve_report_order(self) -> ExternalBlindV2ScoreReport:
        if tuple(self.candidate_order) != _CANDIDATE_ORDER:
            raise ValueError("scored candidate order differs from the frozen prediction")
        if tuple(item.candidate for item in self.field_scores) != _CANDIDATE_ORDER:
            raise ValueError("field score order differs from the frozen prediction")
        if tuple(item.alias for item in self.ood_thresholds) != _MODEL_ORDER:
            raise ValueError("OOD result order must be BGE-M3 then KURE-v1")
        return self


class ArtifactBoundSchemaCandidateProvider:
    """Exact Schema Dense index bound to verified model artifact evidence."""

    def __init__(self, loaded: _LoadedArtifact, index: DenseSchemaIndex) -> None:
        evidence = loaded.gate_evidence
        provider = index.manifest.provider
        if evidence is None:
            raise CandidateLockError("candidate provider requires verified artifact evidence")
        if provider.provider_kind != "frozen_model":
            raise CandidateLockError("blind candidates require a frozen-model provider")
        if type(index) is not DenseSchemaIndex:
            raise CandidateLockError("blind candidates require the canonical DenseSchemaIndex")
        if type(index.provider) is not VerifiedSentenceTransformerCpuProvider:
            raise CandidateLockError(
                "blind candidates require the internally verified CPU provider"
            )
        runtime_evidence = index.provider.artifact_gate_evidence
        if (
            evidence.candidate.model_id != provider.model_id
            or evidence.candidate.revision != provider.model_revision
            or evidence.manifest_file_sha256 != loaded.raw_sha256
            or runtime_evidence != evidence
        ):
            raise CandidateLockError("candidate index and verified model artifact differ")
        self._loaded = loaded
        self._index = index

    @property
    def alias(self) -> Literal["bge-m3", "kure-v1"]:
        return self._loaded.manifest.candidate.alias

    @property
    def artifact_manifest_sha256(self) -> str:
        return self._loaded.raw_sha256

    def rank_fields(
        self,
        question: str,
        product_family: ProductFamily,
        *,
        top_k: int,
        purpose: Literal["operational_candidate", "offline_ood_probe"],
    ) -> Sequence[RankedField]:
        del purpose
        return tuple(
            RankedField(
                product_family=item.product_family,
                field_id=item.field_id,
                score=item.score,
                rank=item.rank,
            )
            for item in self._index.search(question, product_family, top_k=top_k)
        )


LexicalRanker = Callable[[str, ProductFamily, int], Sequence[str]]


@dataclass(frozen=True)
class _LoadedArtifact:
    manifest: SchemaEmbeddingSnapshotManifestV2
    raw_sha256: str
    gate_evidence: SchemaEmbeddingArtifactGateEvidence | None = None


@dataclass(frozen=True)
class _LoadedProtocol:
    contract: ExternalBlindV2ProtocolLock
    raw_sha256: str


@dataclass(frozen=True)
class _ReferenceCorpus:
    questions: tuple[str, ...]
    corpus_sha256: str


@dataclass(frozen=True)
class _FieldOutcome:
    exact: bool
    hits_at_5: int
    hits_at_10: int
    gold_count: int


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(payload: str) -> str:
    return _sha256_bytes(payload.encode("utf-8"))


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _utc_instant(value: str) -> datetime:
    """Parse the contract's canonical second-resolution UTC timestamp."""

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _require_chronology(*values: tuple[str, str]) -> None:
    """Require a non-decreasing named chronology without trusting lexical order."""

    instants = tuple((label, _utc_instant(value)) for label, value in values)
    for (left_label, left), (right_label, right) in zip(instants, instants[1:], strict=False):
        if left > right:
            raise ExternalBundleUnavailableError(
                f"blind chronology differs: {left_label} must not be after {right_label}"
            )


def _read_required(path: Path, label: str, *, maximum_bytes: int) -> bytes:
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > maximum_bytes:
                raise ExternalBundleUnavailableError(
                    f"{label} exceeds the {maximum_bytes}-byte safety limit"
                )
            payload = stream.read(maximum_bytes + 1)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as error:
        raise ExternalBundleUnavailableError(f"{label} is unavailable: {path}") from error
    if len(payload) > maximum_bytes:
        raise ExternalBundleUnavailableError(
            f"{label} exceeds the {maximum_bytes}-byte safety limit"
        )
    return payload


def _json_object_without_duplicate_keys(raw: bytes, *, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError(f"{label} contains a duplicate JSON key")
            output[key] = value
        return output

    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def load_external_blind_v2_protocol(protocol_path: Path) -> _LoadedProtocol:
    raw = _read_required(
        protocol_path,
        "tracked external blind v2 protocol",
        maximum_bytes=_MAX_PROTOCOL_BYTES,
    )
    raw_sha256 = _sha256_bytes(raw)
    if raw_sha256 != _TRACKED_PROTOCOL_SHA256:
        raise CandidateLockError("external blind v2 protocol differs from the tracked protocol")
    try:
        contract = ExternalBlindV2ProtocolLock.model_validate(
            _json_object_without_duplicate_keys(raw, label="external blind v2 protocol")
        )
    except ValueError as error:
        raise CandidateLockError("tracked external blind v2 protocol is invalid") from error
    return _LoadedProtocol(contract=contract, raw_sha256=raw_sha256)


def _read_reference_resource(resource: Any, *, label: str) -> bytes:
    try:
        with resource.open("rb") as stream:
            raw = stream.read(_MAX_REFERENCE_SUITE_BYTES + 1)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as error:
        raise CandidateLockError(f"{label} is unavailable") from error
    if len(raw) > _MAX_REFERENCE_SUITE_BYTES:
        raise CandidateLockError(f"{label} exceeds the safety limit")
    return raw


def _load_reference_corpus(protocol: _LoadedProtocol) -> _ReferenceCorpus:
    expected_corpus = protocol.contract.reference_corpus
    suite_bindings: list[dict[str, object]] = []
    reference_questions: list[str] = []
    suite_package = files("finance_agent_core.evaluation.suites")
    source_manifest_raw = _read_reference_resource(
        suite_package.joinpath(expected_corpus.source_manifest_resource_name),
        label="frozen Schema Dense selection manifest",
    )
    policy_migration_raw = _read_reference_resource(
        suite_package.joinpath(expected_corpus.policy_migration_resource_name),
        label="frozen Schema Dense policy migration provenance",
    )
    if (
        _sha256_bytes(source_manifest_raw) != expected_corpus.source_manifest_file_sha256
        or _sha256_bytes(policy_migration_raw) != expected_corpus.policy_migration_file_sha256
    ):
        raise CandidateLockError("frozen Schema Dense provenance hash differs")
    try:
        source_manifest = _json_object_without_duplicate_keys(
            source_manifest_raw,
            label="Schema Dense public selection manifest",
        )
        source_inputs = source_manifest["input_file_sha256"]
    except (KeyError, ValueError) as error:
        raise CandidateLockError("frozen Schema Dense provenance is invalid") from error
    if source_manifest.get("suite_id") != expected_corpus.source_evaluation_id or not isinstance(
        source_inputs, dict
    ):
        raise CandidateLockError("frozen Schema Dense selection manifest differs")

    for expected in expected_corpus.suites:
        resource = suite_package.joinpath(expected.resource_name)
        raw = _read_reference_resource(
            resource,
            label=f"frozen near-duplicate reference suite {expected.resource_name}",
        )
        observed_sha256 = _sha256_bytes(raw)
        if observed_sha256 != expected.file_sha256:
            raise CandidateLockError("frozen near-duplicate reference suite hash differs")
        try:
            payload = _json_object_without_duplicate_keys(
                raw,
                label=f"reference suite {expected.resource_name}",
            )
            cases = payload["cases"]
        except (KeyError, ValueError) as error:
            raise CandidateLockError("frozen near-duplicate reference suite is invalid") from error
        if not isinstance(cases, list) or len(cases) != expected.question_count:
            raise CandidateLockError("frozen near-duplicate reference question count differs")
        suite_questions: list[str] = []
        suite_ids: list[str] = []
        for case in cases:
            if not isinstance(case, dict):
                raise CandidateLockError("frozen near-duplicate reference case is invalid")
            case_id = case.get("id")
            question = case.get("question")
            if (
                not isinstance(case_id, str)
                or not isinstance(question, str)
                or not question.strip()
            ):
                raise CandidateLockError("frozen near-duplicate reference case is invalid")
            suite_ids.append(case_id)
            suite_questions.append(question)
        if len(suite_ids) != len(set(suite_ids)):
            raise CandidateLockError("frozen near-duplicate reference case IDs are not unique")
        reference_questions.extend(suite_questions)
        suite_bindings.append(
            {
                "resource_name": expected.resource_name,
                "file_sha256": observed_sha256,
                "question_count": len(suite_questions),
            }
        )
        manifest_key = next(
            (
                key
                for key in source_inputs
                if isinstance(key, str) and key.endswith(f"/suites/{expected.resource_name}")
            ),
            None,
        )
        if manifest_key is None or source_inputs[manifest_key] != observed_sha256:
            raise CandidateLockError(
                "Schema Dense selection manifest does not bind every reference suite"
            )
    migration_key = next(
        (
            key
            for key in source_inputs
            if isinstance(key, str)
            and key.endswith(f"/suites/{expected_corpus.policy_migration_resource_name}")
        ),
        None,
    )
    if (
        migration_key is None
        or source_inputs[migration_key] != expected_corpus.policy_migration_file_sha256
    ):
        raise CandidateLockError(
            "Schema Dense selection manifest does not bind the policy migration provenance"
        )
    if len(reference_questions) != expected_corpus.reference_question_count:
        raise CandidateLockError("frozen near-duplicate reference corpus size differs")
    corpus_sha256 = _canonical_sha256(
        {
            "source_evaluation_id": expected_corpus.source_evaluation_id,
            "source_manifest_file_sha256": expected_corpus.source_manifest_file_sha256,
            "policy_migration_file_sha256": expected_corpus.policy_migration_file_sha256,
            "question_suites": suite_bindings,
        }
    )
    if corpus_sha256 != expected_corpus.reference_corpus_sha256:
        raise CandidateLockError("frozen near-duplicate reference corpus hash differs")
    return _ReferenceCorpus(questions=tuple(reference_questions), corpus_sha256=corpus_sha256)


def build_external_blind_v2_reference_report(
    questions: ExternalBlindQuestionOnlySet,
    *,
    raw_questions_sha256: str,
    protocol: _LoadedProtocol,
) -> ExternalBlindV2ReferenceReport:
    reference = _load_reference_corpus(protocol)
    threshold = protocol.contract.reference_corpus.near_duplicate_max_similarity
    maximum_observed, violations = assess_external_near_duplicates(
        questions.cases,
        reference.questions,
        max_similarity=threshold,
    )
    if violations:
        raise CandidateLockError(
            "external blind questions are too similar to frozen public/development references: "
            + ", ".join(violations[:10])
        )
    return ExternalBlindV2ReferenceReport(
        reference_corpus_sha256=reference.corpus_sha256,
        reference_question_count=len(reference.questions),
        questions_sha256=raw_questions_sha256,
        external_question_count=len(questions.cases),
        maximum_observed_similarity=maximum_observed,
    )


def run_external_blind_v2_reference_gate(
    *,
    question_path: Path,
    protocol_path: Path,
    output_path: Path,
) -> ExternalBlindV2ReferenceReport:
    """Create the pre-commitment near-duplicate report with bounded inputs."""

    raw = _read_required(
        question_path,
        "external question bundle",
        maximum_bytes=_MAX_QUESTION_BUNDLE_BYTES,
    )
    try:
        questions = ExternalBlindQuestionOnlySet.model_validate_json(raw)
    except ValueError as error:
        raise ExternalBundleUnavailableError("external question bundle is invalid") from error
    protocol = load_external_blind_v2_protocol(protocol_path)
    report = build_external_blind_v2_reference_report(
        questions,
        raw_questions_sha256=_sha256_bytes(raw),
        protocol=protocol,
    )
    _write_new_json_atomically(output_path, report)
    return report


def _load_artifact(path: Path, expected_alias: str) -> _LoadedArtifact:
    try:
        raw = _read_required(
            path,
            f"{expected_alias} model artifact manifest",
            maximum_bytes=_MAX_MODEL_MANIFEST_BYTES,
        )
    except ExternalBundleUnavailableError as error:
        raise CandidateLockError(
            f"both model artifact manifests are required before prediction: {path}"
        ) from error
    try:
        manifest = SchemaEmbeddingSnapshotManifestV2.model_validate_json(raw)
    except ValueError as error:
        raise CandidateLockError(f"invalid {expected_alias} artifact manifest") from error
    if manifest.candidate.alias != expected_alias:
        raise CandidateLockError(
            f"artifact order differs: expected {expected_alias}, got {manifest.candidate.alias}"
        )
    if manifest.candidate != load_schema_embedding_candidate_link(expected_alias):
        raise CandidateLockError(
            f"{expected_alias} artifact differs from the frozen repository and revision"
        )
    return _LoadedArtifact(manifest=manifest, raw_sha256=_sha256_bytes(raw))


def _require_tracked_protocol_artifact(
    loaded: _LoadedArtifact,
    expected: ProtocolModelCandidate,
) -> None:
    manifest = loaded.manifest
    observed = (
        manifest.candidate.alias,
        manifest.candidate.model_id,
        manifest.candidate.revision,
        loaded.raw_sha256,
        manifest.snapshot_file_manifest_sha256,
        manifest.category_digests["weights"].file_manifest_sha256,
        manifest.category_digests["tokenizer"].file_manifest_sha256,
        manifest.category_digests["config"].file_manifest_sha256,
        manifest.category_digests["other"].file_manifest_sha256,
    )
    required = (
        expected.alias,
        expected.repository,
        expected.revision,
        expected.snapshot_manifest_file_sha256,
        expected.snapshot_file_manifest_sha256,
        expected.weights_file_manifest_sha256,
        expected.tokenizer_file_manifest_sha256,
        expected.config_file_manifest_sha256,
        expected.other_file_manifest_sha256,
    )
    if observed != required:
        raise CandidateLockError(
            f"{expected.alias} artifact differs from the exact tracked protocol snapshot"
        )


def load_candidate_artifact_lock(
    *,
    protocol: _LoadedProtocol,
    bge_manifest_path: Path,
    bge_snapshot_dir: Path,
    kure_manifest_path: Path,
    kure_snapshot_dir: Path,
    bge_trusted_cache_root: Path | None = None,
    kure_trusted_cache_root: Path | None = None,
) -> tuple[_LoadedArtifact, _LoadedArtifact]:
    """Verify both manifests and their snapshot bytes before provider calls."""

    # Parse both manifests before starting either byte gate.  Do not return a
    # partial lock or claim first-run state after only one manifest was found.
    bge = _load_artifact(bge_manifest_path, "bge-m3")
    kure = _load_artifact(kure_manifest_path, "kure-v1")
    protocol_candidates = protocol.contract.model_candidates
    _require_tracked_protocol_artifact(bge, protocol_candidates[0])
    _require_tracked_protocol_artifact(kure, protocol_candidates[1])
    verified: list[_LoadedArtifact] = []
    for loaded, alias, snapshot_dir, manifest_path, trusted_cache_root in (
        (
            bge,
            "bge-m3",
            bge_snapshot_dir,
            bge_manifest_path,
            bge_trusted_cache_root,
        ),
        (
            kure,
            "kure-v1",
            kure_snapshot_dir,
            kure_manifest_path,
            kure_trusted_cache_root,
        ),
    ):
        try:
            evidence = require_schema_embedding_artifact_gate(
                mode="shadow",
                alias=alias,
                snapshot_dir=snapshot_dir,
                manifest_path=manifest_path,
                trusted_cache_root=trusted_cache_root,
            )
        except SchemaEmbeddingArtifactError as error:
            raise CandidateLockError(f"{alias} snapshot bytes failed the artifact gate") from error
        if (
            evidence.candidate != loaded.manifest.candidate
            or evidence.manifest_file_sha256 != loaded.raw_sha256
            or evidence.snapshot_file_manifest_sha256
            != loaded.manifest.snapshot_file_manifest_sha256
        ):
            raise CandidateLockError(
                f"{alias} artifact gate evidence differs from the supplied manifest"
            )
        verified.append(
            _LoadedArtifact(
                manifest=loaded.manifest,
                raw_sha256=loaded.raw_sha256,
                gate_evidence=evidence,
            )
        )
    return verified[0], verified[1]


def _artifact_binding(loaded: _LoadedArtifact, priority: int) -> FrozenModelArtifactBinding:
    item = loaded.manifest
    evidence = loaded.gate_evidence
    if evidence is None:
        raise CandidateLockError("model artifact binding requires verified byte evidence")
    return FrozenModelArtifactBinding(
        priority=priority,
        alias=item.candidate.alias,
        repository=item.candidate.model_id,
        revision=item.candidate.revision,
        manifest_file_sha256=evidence.manifest_file_sha256,
        snapshot_sha256=evidence.snapshot_file_manifest_sha256,
        weights_sha256=item.category_digests["weights"].file_manifest_sha256,
        tokenizer_sha256=item.category_digests["tokenizer"].file_manifest_sha256,
        config_sha256=item.category_digests["config"].file_manifest_sha256,
        other_sha256=item.category_digests["other"].file_manifest_sha256,
        verification_mode=evidence.mode,
        verification_status=evidence.status,
        approval_scope=evidence.approval_scope,
    )


def _require_verified_artifact_lock(
    artifacts: tuple[_LoadedArtifact, _LoadedArtifact],
) -> None:
    if tuple(item.manifest.candidate.alias for item in artifacts) != _MODEL_ORDER:
        raise CandidateLockError("both model artifacts must be loaded in frozen order")
    for loaded in artifacts:
        evidence = loaded.gate_evidence
        if evidence is None:
            raise CandidateLockError("both model artifacts require verified snapshot byte evidence")
        if evidence.mode != "shadow":
            raise CandidateLockError("blind v2 model artifacts must remain shadow-only")
        if (
            evidence.candidate != loaded.manifest.candidate
            or evidence.manifest_file_sha256 != loaded.raw_sha256
            or evidence.snapshot_file_manifest_sha256
            != loaded.manifest.snapshot_file_manifest_sha256
        ):
            raise CandidateLockError("model artifact gate evidence differs from its manifest")


def _load_and_build_official_candidates(
    artifacts: tuple[_LoadedArtifact, _LoadedArtifact],
    *,
    bge_manifest_path: Path,
    bge_snapshot_dir: Path,
    bge_trusted_cache_root: Path,
    kure_manifest_path: Path,
    kure_snapshot_dir: Path,
    kure_trusted_cache_root: Path,
) -> dict[str, ArtifactBoundSchemaCandidateProvider]:
    """Load both exact snapshots, then build only canonical Schema Dense indexes."""

    _require_verified_artifact_lock(artifacts)
    load_settings = (
        (
            artifacts[0],
            bge_manifest_path,
            bge_snapshot_dir,
            bge_trusted_cache_root,
        ),
        (
            artifacts[1],
            kure_manifest_path,
            kure_snapshot_dir,
            kure_trusted_cache_root,
        ),
    )
    loaded_providers: list[tuple[_LoadedArtifact, VerifiedSentenceTransformerCpuProvider]] = []
    for loaded, manifest_path, snapshot_dir, trusted_cache_root in load_settings:
        alias = loaded.manifest.candidate.alias
        try:
            provider = load_verified_schema_embedding_cpu_provider(
                alias=alias,
                snapshot_dir=snapshot_dir,
                manifest_path=manifest_path,
                trusted_cache_root=trusted_cache_root,
            )
        except (SchemaEmbeddingArtifactError, OSError, RuntimeError, ValueError) as error:
            raise CandidateLockError(
                f"{alias} could not be loaded from the verified local snapshot"
            ) from error
        if provider.artifact_gate_evidence != loaded.gate_evidence:
            raise CandidateLockError(f"{alias} runtime evidence differs from the candidate lock")
        loaded_providers.append((loaded, provider))

    # Both heavyweight model loads must succeed before either model creates a
    # searchable index or emits a prediction. Both indexes use the same
    # canonical registry-derived entries.
    entries = tuple(build_schema_field_entries())
    candidates: dict[str, ArtifactBoundSchemaCandidateProvider] = {}
    for loaded, provider in loaded_providers:
        index = DenseSchemaIndex.build(entries, provider)
        alias = loaded.manifest.candidate.alias
        candidates[alias] = ArtifactBoundSchemaCandidateProvider(loaded, index)
    if tuple(candidates) != _MODEL_ORDER:
        raise CandidateLockError("verified candidate construction order differs")
    return candidates


def _validate_ranked_fields(
    values: Sequence[RankedField],
    *,
    require_two: bool,
    expected_family: ProductFamily | None = None,
) -> tuple[RankedField, ...]:
    candidates = tuple(RankedField.model_validate(item) for item in values)
    minimum = 2 if require_two else 1
    if not minimum <= len(candidates) <= _TOP_K:
        raise CandidateLockError(f"provider must return between {minimum} and {_TOP_K} fields")
    if [item.rank for item in candidates] != list(range(1, len(candidates) + 1)):
        raise CandidateLockError("provider ranks must be contiguous and start at one")
    if len({(item.product_family, item.field_id) for item in candidates}) != len(candidates):
        raise CandidateLockError("provider field candidates must be unique")
    if expected_family is not None and any(
        item.product_family is not expected_family for item in candidates
    ):
        raise CandidateLockError("provider returned a field outside the requested family")
    if any(
        left.score < right.score for left, right in zip(candidates, candidates[1:], strict=False)
    ):
        raise CandidateLockError("provider field scores must be non-increasing")
    return candidates


def _default_lexical_ranker(
    question: str,
    family: ProductFamily,
    top_k: int,
) -> Sequence[str]:
    hints = build_lexical_hints(question, family.value, force_product_family_hint=True)
    raw = [
        *(item["field"] for item in hints["required_constraints"]),
        *(item["field"] for item in hints["required_rankings"]),
    ]
    registry = load_field_registry()
    return tuple(
        dict.fromkeys(
            field_id
            for field_id in raw
            if field_id in registry.fields and family.value in registry.fields[field_id].datasets
        )
    )[:top_k]


def _validate_lexical_fields(values: Sequence[str], family: ProductFamily) -> tuple[str, ...]:
    fields = tuple(values)
    if len(fields) > _TOP_K or len(fields) != len(set(fields)):
        raise CandidateLockError("lexical fields must be unique and respect top-k")
    registry = load_field_registry()
    if any(
        field_id not in registry.fields or family.value not in registry.fields[field_id].datasets
        for field_id in fields
    ):
        raise CandidateLockError("lexical ranker returned a field outside the routed family")
    return fields


def _fuse_lexical_first(lexical: Sequence[str], dense: Sequence[RankedField]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*lexical, *(item.field_id for item in dense)]))[:_TOP_K]


def _ood_probe(candidates: Sequence[RankedField]) -> OfflineOodProbe:
    ranked = _validate_ranked_fields(candidates, require_two=True)
    return OfflineOodProbe(
        candidates=ranked,
        top_1_score=ranked[0].score,
        top_1_top_2_margin=round(ranked[0].score - ranked[1].score, 9),
    )


def _global_ood_probe(
    provider: ArtifactBoundSchemaCandidateProvider,
    question: str,
) -> OfflineOodProbe:
    """Probe every approved family without consuming a hidden phase-2 label."""

    candidates: list[RankedField] = []
    for family in ProductFamily:
        observed = _validate_ranked_fields(
            provider.rank_fields(
                question,
                family,
                top_k=_TOP_K,
                purpose="offline_ood_probe",
            ),
            require_two=True,
            expected_family=family,
        )
        candidates.extend(observed)
    selected = sorted(
        candidates,
        key=lambda item: (-item.score, item.product_family.value, item.field_id),
    )[:_TOP_K]
    reranked = tuple(
        item.model_copy(update={"rank": rank}) for rank, item in enumerate(selected, 1)
    )
    return _ood_probe(reranked)


def _require_pre_inference_lock(
    questions: ExternalBlindQuestionOnlySet,
    commitment: ExternalBlindV2Commitment,
    authorization: ExternalBlindExecutionAuthorization,
    *,
    raw_questions_sha256: str,
    protocol: _LoadedProtocol,
    reference_report: ExternalBlindV2ReferenceReport,
    raw_reference_report_sha256: str,
    implementation_commit: str,
    prediction_created_at_utc: str,
    artifacts: tuple[_LoadedArtifact, _LoadedArtifact],
) -> None:
    if commitment.implementation_commit != implementation_commit:
        raise CandidateLockError("implementation commit differs from the external commitment")
    if commitment.questions_sha256 != raw_questions_sha256:
        raise CandidateLockError("question bytes differ from the external commitment")
    if questions.suite_id != commitment.suite_id:
        raise CandidateLockError("question suite differs from the external commitment")
    reference_binding = (
        protocol.raw_sha256,
        reference_report.reference_corpus_sha256,
        reference_report.near_duplicate_max_similarity,
        raw_reference_report_sha256,
    )
    if reference_report.questions_sha256 != raw_questions_sha256:
        raise CandidateLockError("near-duplicate report question hash differs")
    if (
        commitment.protocol_sha256,
        commitment.reference_corpus_sha256,
        commitment.near_duplicate_max_similarity,
        commitment.near_duplicate_report_sha256,
    ) != reference_binding:
        raise CandidateLockError("external commitment reference-corpus binding differs")
    if (
        authorization.protocol_sha256,
        authorization.reference_corpus_sha256,
        authorization.near_duplicate_max_similarity,
        authorization.near_duplicate_report_sha256,
    ) != reference_binding:
        raise CandidateLockError("external authorization reference-corpus binding differs")
    _require_chronology(
        ("commitment", commitment.created_at_utc),
        ("authorization", authorization.issued_at_utc),
        ("prediction", prediction_created_at_utc),
    )
    # Keep this check inside the provider-free preamble so even internal Python
    # callers cannot reach a provider with parsed-only artifact manifests.
    _require_verified_artifact_lock(artifacts)
    if (
        authorization.implementation_commit != implementation_commit
        or authorization.questions_sha256 != raw_questions_sha256
        or authorization.bge_manifest_sha256 != artifacts[0].raw_sha256
        or authorization.kure_manifest_sha256 != artifacts[1].raw_sha256
    ):
        raise CandidateLockError("external execution authorization differs from the run lock")


def _build_question_only_predictions(
    questions: ExternalBlindQuestionOnlySet,
    commitment: ExternalBlindV2Commitment,
    authorization: ExternalBlindExecutionAuthorization,
    *,
    raw_questions_sha256: str,
    raw_commitment_sha256: str,
    raw_authorization_sha256: str,
    protocol: _LoadedProtocol,
    reference_report: ExternalBlindV2ReferenceReport,
    raw_reference_report_sha256: str,
    implementation_commit: str,
    created_at_utc: str,
    artifacts: tuple[_LoadedArtifact, _LoadedArtifact],
    providers: Mapping[str, ArtifactBoundSchemaCandidateProvider],
    lexical_ranker: LexicalRanker = _default_lexical_ranker,
) -> QuestionOnlyPredictionArtifact:
    """Produce predictions from the official internally bound candidates."""

    _require_pre_inference_lock(
        questions,
        commitment,
        authorization,
        raw_questions_sha256=raw_questions_sha256,
        protocol=protocol,
        reference_report=reference_report,
        raw_reference_report_sha256=raw_reference_report_sha256,
        implementation_commit=implementation_commit,
        prediction_created_at_utc=created_at_utc,
        artifacts=artifacts,
    )
    if tuple(providers) != _MODEL_ORDER:
        raise CandidateLockError("exactly BGE-M3 and KURE-v1 providers are required")
    for loaded in artifacts:
        provider = providers[loaded.manifest.candidate.alias]
        if provider.alias != loaded.manifest.candidate.alias:
            raise CandidateLockError("provider alias differs from its model artifact")
        if provider.artifact_manifest_sha256 != loaded.raw_sha256:
            raise CandidateLockError("provider is not bound to the supplied artifact manifest")

    case_predictions: list[ExternalCasePrediction] = []
    router = IntentRouter()
    for case in questions.cases:
        route = RouteDecision.model_validate(router.route(case.question, case.id))
        operational_family = (
            route.draft.product_families[0]
            if route.disposition is RouteDisposition.EXECUTE
            and len(route.draft.product_families) == 1
            else None
        )
        lexical_fields: tuple[str, ...] = ()
        if operational_family is not None:
            lexical_fields = _validate_lexical_fields(
                lexical_ranker(case.question, operational_family, _TOP_K),
                operational_family,
            )

        model_predictions: list[ModelCasePrediction] = []
        for alias in _MODEL_ORDER:
            provider = providers[alias]
            operational: tuple[RankedField, ...] = ()
            if operational_family is not None:
                operational = _validate_ranked_fields(
                    provider.rank_fields(
                        case.question,
                        operational_family,
                        top_k=_TOP_K,
                        purpose="operational_candidate",
                    ),
                    require_two=False,
                    expected_family=operational_family,
                )
            # Phase 1 contains no hidden family/intent label.  The isolated OOD
            # probe searches all four approved families and has no execution
            # authority, including for CLARIFY and UNSUPPORTED cases.
            offline_probe = _global_ood_probe(provider, case.question)
            model_predictions.append(
                ModelCasePrediction(
                    alias=alias,
                    operational_dense_called=operational_family is not None,
                    dense_candidates=operational,
                    fused_fields=(
                        _fuse_lexical_first(lexical_fields, operational)
                        if operational_family is not None
                        else ()
                    ),
                    offline_ood_probe=offline_probe,
                )
            )
        case_predictions.append(
            ExternalCasePrediction(
                case_id=case.id,
                question_sha256=_sha256_text(case.question),
                route_disposition=route.disposition,
                route_interaction_intent=route.draft.intent,
                route_product_families=tuple(route.draft.product_families),
                route_query_plan_intent=route.query_plan_intent,
                route_reason_code=route.reason_code,
                lexical_fields=lexical_fields,
                models=tuple(model_predictions),
            )
        )

    return QuestionOnlyPredictionArtifact(
        created_at_utc=created_at_utc,
        lock=CandidateExecutionLock(
            protocol_sha256=protocol.raw_sha256,
            implementation_commit=implementation_commit,
            questions_sha256=raw_questions_sha256,
            answer_key_sha256_commitment=commitment.answers_sha256,
            reference_corpus_sha256=reference_report.reference_corpus_sha256,
            near_duplicate_max_similarity=reference_report.near_duplicate_max_similarity,
            near_duplicate_report_sha256=raw_reference_report_sha256,
            external_commitment_sha256=raw_commitment_sha256,
            execution_authorization_sha256=raw_authorization_sha256,
            image_reference=authorization.image_reference,
            platform=authorization.platform,
            external_authorization_receipt_sha256=(
                authorization.external_authorization_receipt_sha256
            ),
            commitment_created_at_utc=commitment.created_at_utc,
            authorization_issued_at_utc=authorization.issued_at_utc,
            candidate_order=_CANDIDATE_ORDER,
            model_artifacts=(
                _artifact_binding(artifacts[0], 1),
                _artifact_binding(artifacts[1], 2),
            ),
        ),
        cases=tuple(case_predictions),
        predicted_non_execute_operational_dense_call_count=0,
    )


def _write_new_json_atomically(path: Path, model: BaseModel) -> None:
    """Publish one immutable JSON artifact without an overwrite window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"blind artifact already exists: {path}")
    payload = f"{model.model_dump_json(indent=2)}\n".encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link is an atomic create-if-absent publication on the same
        # filesystem.  It cannot overwrite a previous first-run artifact.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def run_and_freeze_question_only_predictions(
    *,
    question_path: Path,
    commitment_path: Path,
    execution_authorization_path: Path,
    protocol_path: Path,
    near_duplicate_report_path: Path,
    bge_manifest_path: Path,
    bge_snapshot_dir: Path,
    kure_manifest_path: Path,
    kure_snapshot_dir: Path,
    output_path: Path,
    implementation_commit: str,
    created_at_utc: str,
    bge_trusted_cache_root: Path,
    kure_trusted_cache_root: Path,
) -> QuestionOnlyPredictionArtifact:
    """Validate all locks, run question-only inference, then publish once.

    Both model manifests and all actual snapshot bytes pass the shared artifact
    gate first. If either side fails, no provider is called and no first-run
    output/state is created.
    """

    if output_path.exists():
        raise FileExistsError(f"blind artifact already exists: {output_path}")
    question_raw = _read_required(
        question_path,
        "external question bundle",
        maximum_bytes=_MAX_QUESTION_BUNDLE_BYTES,
    )
    commitment_raw = _read_required(
        commitment_path,
        "external commitment",
        maximum_bytes=_MAX_COMMITMENT_BYTES,
    )
    authorization_raw = _read_required(
        execution_authorization_path,
        "external execution authorization",
        maximum_bytes=_MAX_AUTHORIZATION_BYTES,
    )
    reference_report_raw = _read_required(
        near_duplicate_report_path,
        "external near-duplicate report",
        maximum_bytes=_MAX_COMMITMENT_BYTES,
    )
    try:
        questions = ExternalBlindQuestionOnlySet.model_validate_json(question_raw)
        commitment = ExternalBlindV2Commitment.model_validate_json(commitment_raw)
        authorization = ExternalBlindExecutionAuthorization.model_validate_json(authorization_raw)
        supplied_reference_report = ExternalBlindV2ReferenceReport.model_validate_json(
            reference_report_raw
        )
    except ValueError as error:
        raise ExternalBundleUnavailableError(
            "external question or commitment is invalid"
        ) from error
    raw_questions_sha256 = _sha256_bytes(question_raw)
    protocol = load_external_blind_v2_protocol(protocol_path)
    reference_report = build_external_blind_v2_reference_report(
        questions,
        raw_questions_sha256=raw_questions_sha256,
        protocol=protocol,
    )
    if reference_report != supplied_reference_report:
        raise CandidateLockError(
            "external near-duplicate report differs from the independently recomputed result"
        )
    raw_reference_report_sha256 = _sha256_bytes(reference_report_raw)
    artifacts = load_candidate_artifact_lock(
        protocol=protocol,
        bge_manifest_path=bge_manifest_path,
        bge_snapshot_dir=bge_snapshot_dir,
        kure_manifest_path=kure_manifest_path,
        kure_snapshot_dir=kure_snapshot_dir,
        bge_trusted_cache_root=bge_trusted_cache_root,
        kure_trusted_cache_root=kure_trusted_cache_root,
    )
    _require_pre_inference_lock(
        questions,
        commitment,
        authorization,
        raw_questions_sha256=raw_questions_sha256,
        protocol=protocol,
        reference_report=reference_report,
        raw_reference_report_sha256=raw_reference_report_sha256,
        implementation_commit=implementation_commit,
        prediction_created_at_utc=created_at_utc,
        artifacts=artifacts,
    )
    providers = _load_and_build_official_candidates(
        artifacts,
        bge_manifest_path=bge_manifest_path,
        bge_snapshot_dir=bge_snapshot_dir,
        bge_trusted_cache_root=bge_trusted_cache_root,
        kure_manifest_path=kure_manifest_path,
        kure_snapshot_dir=kure_snapshot_dir,
        kure_trusted_cache_root=kure_trusted_cache_root,
    )
    artifact = _build_question_only_predictions(
        questions,
        commitment,
        authorization,
        raw_questions_sha256=raw_questions_sha256,
        raw_commitment_sha256=_sha256_bytes(commitment_raw),
        raw_authorization_sha256=_sha256_bytes(authorization_raw),
        protocol=protocol,
        reference_report=reference_report,
        raw_reference_report_sha256=raw_reference_report_sha256,
        implementation_commit=implementation_commit,
        created_at_utc=created_at_utc,
        artifacts=artifacts,
        providers=providers,
        lexical_ranker=_default_lexical_ranker,
    )
    _write_new_json_atomically(output_path, artifact)
    return artifact


def _prediction_fields(case: ExternalCasePrediction, candidate: str) -> tuple[str, ...]:
    if candidate == "lexical":
        return case.lexical_fields
    return next(item.fused_fields for item in case.models if item.alias == candidate)


def _field_outcomes(
    predictions: QuestionOnlyPredictionArtifact,
    answers: ExternalBlindPrivateAnswerKey,
    candidate: str,
) -> list[_FieldOutcome]:
    outcomes: list[_FieldOutcome] = []
    for prediction, answer in zip(predictions.cases, answers.cases, strict=True):
        if answer.expected_disposition is not RouteDisposition.EXECUTE:
            continue
        gold = frozenset(answer.gold_schema_field_ids)
        predicted = _prediction_fields(prediction, candidate)
        outcomes.append(
            _FieldOutcome(
                exact=(len(predicted) >= len(gold) and set(predicted[: len(gold)]) == gold),
                hits_at_5=len(gold & set(predicted[:5])),
                hits_at_10=len(gold & set(predicted[:10])),
                gold_count=len(gold),
            )
        )
    return outcomes


def _field_score(candidate: str, outcomes: Sequence[_FieldOutcome]) -> FieldScore:
    case_count = len(outcomes)
    gold_count = sum(item.gold_count for item in outcomes)
    exact_count = sum(item.exact for item in outcomes)
    hits_5 = sum(item.hits_at_5 for item in outcomes)
    hits_10 = sum(item.hits_at_10 for item in outcomes)
    return FieldScore(
        candidate=candidate,
        scored_case_count=case_count,
        gold_field_count=gold_count,
        exact_at_gold_cardinality_count=exact_count,
        exact_at_gold_cardinality=round(exact_count / case_count, 6) if case_count else 0.0,
        hits_at_5=hits_5,
        micro_recall_at_5=round(hits_5 / gold_count, 6) if gold_count else 0.0,
        hits_at_10=hits_10,
        micro_recall_at_10=round(hits_10 / gold_count, 6) if gold_count else 0.0,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires values")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _bootstrap_delta(
    selected_value: float,
    comparator_value: float,
    deltas: Sequence[float],
) -> BootstrapDelta:
    return BootstrapDelta(
        selected_value=round(selected_value, 6),
        comparator_value=round(comparator_value, 6),
        observed_delta=round(selected_value - comparator_value, 6),
        ci95_lower=round(_percentile(deltas, 0.025), 6),
        ci95_upper=round(_percentile(deltas, 0.975), 6),
        probability_selected_greater=round(sum(item > 1e-12 for item in deltas) / len(deltas), 6),
    )


def paired_bootstrap(
    selected_name: Literal["bge-m3", "kure-v1"],
    comparator_name: Literal["lexical", "bge-m3", "kure-v1"],
    selected: Sequence[_FieldOutcome],
    comparator: Sequence[_FieldOutcome],
    *,
    iterations: int = _BOOTSTRAP_ITERATIONS,
    seed: int = _BOOTSTRAP_SEED,
) -> BlindPairedComparison:
    if len(selected) != len(comparator) or not selected:
        raise ValueError("paired bootstrap requires aligned non-empty outcomes")
    if iterations < 1_000:
        raise ValueError("paired bootstrap requires at least 1,000 iterations")
    if any(
        left.gold_count != right.gold_count
        for left, right in zip(selected, comparator, strict=True)
    ):
        raise ValueError("paired bootstrap gold cardinalities differ")

    def metrics(indices: Sequence[int], values: Sequence[_FieldOutcome]) -> tuple[float, float]:
        exact = sum(values[index].exact for index in indices) / len(indices)
        total_gold = sum(values[index].gold_count for index in indices)
        recall = sum(values[index].hits_at_5 for index in indices) / total_gold
        return exact, recall

    all_indices = tuple(range(len(selected)))
    selected_exact, selected_recall = metrics(all_indices, selected)
    comparator_exact, comparator_recall = metrics(all_indices, comparator)
    rng = random.Random(seed)
    exact_deltas: list[float] = []
    recall_deltas: list[float] = []
    for _ in range(iterations):
        sample = tuple(rng.randrange(len(selected)) for _ in selected)
        left_exact, left_recall = metrics(sample, selected)
        right_exact, right_recall = metrics(sample, comparator)
        exact_deltas.append(left_exact - right_exact)
        recall_deltas.append(left_recall - right_recall)
    return BlindPairedComparison(
        selected=selected_name,
        comparator=comparator_name,
        case_count=len(selected),
        exact=_bootstrap_delta(selected_exact, comparator_exact, exact_deltas),
        recall_at_5=_bootstrap_delta(selected_recall, comparator_recall, recall_deltas),
    )


def _ood_split(case_ids: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ordered = sorted(case_ids, key=lambda case_id: _sha256_text(f"{_OOD_SPLIT_PREFIX}{case_id}"))
    midpoint = len(ordered) // 2
    return tuple(ordered[:midpoint]), tuple(ordered[midpoint:])


def _population_score(
    case_ids: Sequence[str],
    answers_by_id: Mapping[str, RouteDisposition],
    probes_by_id: Mapping[str, OfflineOodProbe],
    *,
    score_threshold: float,
    margin_threshold: float,
) -> OodPopulationScore:
    true_accept = false_accept = false_reject = execute_count = control_count = 0
    for case_id in case_ids:
        is_execute = answers_by_id[case_id] is RouteDisposition.EXECUTE
        execute_count += int(is_execute)
        control_count += int(not is_execute)
        probe = probes_by_id[case_id]
        accepted = (
            probe.top_1_score >= score_threshold and probe.top_1_top_2_margin >= margin_threshold
        )
        true_accept += int(is_execute and accepted)
        false_accept += int(not is_execute and accepted)
        false_reject += int(is_execute and not accepted)
    return OodPopulationScore(
        case_count=len(case_ids),
        execute_count=execute_count,
        control_count=control_count,
        true_accept_count=true_accept,
        false_accept_count=false_accept,
        false_reject_count=false_reject,
        execute_false_reject_rate=(
            round(false_reject / execute_count, 6) if execute_count else 0.0
        ),
    )


def calibrate_ood_threshold(
    alias: Literal["bge-m3", "kure-v1"],
    predictions: QuestionOnlyPredictionArtifact,
    answers: ExternalBlindPrivateAnswerKey,
) -> OodThresholdEvaluation:
    answers_by_id = {item.id: item.expected_disposition for item in answers.cases}
    probes_by_id = {
        case.case_id: next(item.offline_ood_probe for item in case.models if item.alias == alias)
        for case in predictions.cases
    }
    calibration_ids, test_ids = _ood_split(tuple(answers_by_id))
    calibration_probes = [probes_by_id[case_id] for case_id in calibration_ids]
    score_values = sorted({item.top_1_score for item in calibration_probes})
    margin_values = sorted({item.top_1_top_2_margin for item in calibration_probes})
    score_values.append(math.nextafter(max(score_values), math.inf))
    margin_values.append(math.nextafter(max(margin_values), math.inf))

    best: tuple[int, float, float, OodPopulationScore] | None = None
    for score_threshold in score_values:
        for margin_threshold in margin_values:
            result = _population_score(
                calibration_ids,
                answers_by_id,
                probes_by_id,
                score_threshold=score_threshold,
                margin_threshold=margin_threshold,
            )
            if result.false_accept_count:
                continue
            candidate = (
                result.true_accept_count,
                score_threshold,
                margin_threshold,
                result,
            )
            if best is None or candidate[:3] > best[:3]:
                best = candidate
    if best is None or best[0] == 0:
        empty = OodPopulationScore(
            case_count=len(calibration_ids),
            execute_count=sum(
                answers_by_id[case_id] is RouteDisposition.EXECUTE for case_id in calibration_ids
            ),
            control_count=sum(
                answers_by_id[case_id] is not RouteDisposition.EXECUTE
                for case_id in calibration_ids
            ),
            true_accept_count=0,
            false_accept_count=0,
            false_reject_count=sum(
                answers_by_id[case_id] is RouteDisposition.EXECUTE for case_id in calibration_ids
            ),
            execute_false_reject_rate=1.0,
        )
        return OodThresholdEvaluation(
            alias=alias,
            calibration_case_ids=calibration_ids,
            test_case_ids=test_ids,
            threshold_status="rejected_no_usable_threshold",
            calibration=empty,
            test=None,
            test_gate_passed=False,
        )
    _, score_threshold, margin_threshold, calibration = best
    test = _population_score(
        test_ids,
        answers_by_id,
        probes_by_id,
        score_threshold=score_threshold,
        margin_threshold=margin_threshold,
    )
    return OodThresholdEvaluation(
        alias=alias,
        calibration_case_ids=calibration_ids,
        test_case_ids=test_ids,
        threshold_status="selected",
        score_threshold=score_threshold,
        margin_threshold=margin_threshold,
        calibration=calibration,
        test=test,
        test_gate_passed=(test.false_accept_count == 0 and test.execute_false_reject_rate <= 0.05),
    )


def score_revealed_external_bundle(
    predictions: QuestionOnlyPredictionArtifact,
    questions: ExternalBlindQuestionOnlySet,
    answers: ExternalBlindPrivateAnswerKey,
    commitment: ExternalBlindV2Commitment,
    prediction_receipt: ExternalBlindPredictionReceipt,
    *,
    raw_questions_sha256: str,
    raw_answers_sha256: str,
    raw_commitment_sha256: str,
    raw_predictions_sha256: str,
    raw_prediction_receipt_sha256: str,
    scored_at_utc: str,
) -> ExternalBlindV2ScoreReport:
    """Score only after all revealed bytes match the pre-reveal commitment."""

    validate_external_blind_v2_bundle(questions, answers)
    _require_chronology(
        ("commitment", predictions.lock.commitment_created_at_utc),
        ("authorization", predictions.lock.authorization_issued_at_utc),
        ("prediction", predictions.created_at_utc),
        ("prediction receipt", prediction_receipt.recorded_at_utc),
        ("score", scored_at_utc),
    )
    expected = (
        predictions.lock.implementation_commit,
        predictions.lock.questions_sha256,
        predictions.lock.answer_key_sha256_commitment,
        predictions.lock.external_commitment_sha256,
        predictions.lock.protocol_sha256,
        predictions.lock.reference_corpus_sha256,
        predictions.lock.near_duplicate_max_similarity,
        predictions.lock.near_duplicate_report_sha256,
    )
    observed = (
        commitment.implementation_commit,
        raw_questions_sha256,
        raw_answers_sha256,
        raw_commitment_sha256,
        commitment.protocol_sha256,
        commitment.reference_corpus_sha256,
        commitment.near_duplicate_max_similarity,
        commitment.near_duplicate_report_sha256,
    )
    if observed != expected:
        raise ExternalBundleUnavailableError(
            "revealed question, answer, commitment, or implementation lock differs"
        )
    if commitment.questions_sha256 != raw_questions_sha256:
        raise ExternalBundleUnavailableError("revealed questions differ from commitment")
    if commitment.answers_sha256 != raw_answers_sha256:
        raise ExternalBundleUnavailableError("revealed answer key differs from commitment")
    if (
        prediction_receipt.prediction_artifact_sha256 != raw_predictions_sha256
        or prediction_receipt.questions_sha256 != raw_questions_sha256
        or prediction_receipt.implementation_commit != predictions.lock.implementation_commit
        or prediction_receipt.image_reference != predictions.lock.image_reference
    ):
        raise ExternalBundleUnavailableError(
            "external prediction receipt differs from the frozen execution"
        )
    if [item.case_id for item in predictions.cases] != [item.id for item in questions.cases]:
        raise ExternalBundleUnavailableError("prediction and question IDs differ")
    if any(
        prediction.question_sha256 != _sha256_text(question.question)
        for prediction, question in zip(predictions.cases, questions.cases, strict=True)
    ):
        raise ExternalBundleUnavailableError("prediction question hashes differ")

    routing_cases: list[RoutingCaseScore] = []
    for prediction, answer in zip(
        predictions.cases,
        answers.cases,
        strict=True,
    ):
        routing_cases.append(
            RoutingCaseScore(
                case_id=prediction.case_id,
                expected_disposition=answer.expected_disposition,
                actual_disposition=prediction.route_disposition,
                disposition_exact=prediction.route_disposition is answer.expected_disposition,
                expected_family=(
                    answer.expected_product_family
                    if answer.expected_disposition is RouteDisposition.EXECUTE
                    else None
                ),
                actual_families=prediction.route_product_families,
                family_exact=(
                    prediction.route_product_families == (answer.expected_product_family,)
                    if answer.expected_disposition is RouteDisposition.EXECUTE
                    else None
                ),
                expected_interaction_intent=answer.expected_interaction_intent,
                actual_interaction_intent=prediction.route_interaction_intent,
                interaction_intent_exact=(
                    prediction.route_interaction_intent is answer.expected_interaction_intent
                ),
                expected_query_plan_intent=answer.expected_query_plan_intent,
                actual_query_plan_intent=prediction.route_query_plan_intent,
                query_plan_intent_exact=(
                    prediction.route_query_plan_intent is answer.expected_query_plan_intent
                ),
            )
        )
    total = len(routing_cases)
    control_count = sum(
        item.expected_disposition is not RouteDisposition.EXECUTE for item in answers.cases
    )
    gold_control_predictions = tuple(
        prediction
        for prediction, answer in zip(predictions.cases, answers.cases, strict=True)
        if answer.expected_disposition is not RouteDisposition.EXECUTE
    )
    control_operational_dense_call_count = sum(
        model.operational_dense_called
        for prediction in gold_control_predictions
        for model in prediction.models
    )
    control_no_operational_dense_case_count = sum(
        not any(model.operational_dense_called for model in prediction.models)
        for prediction in gold_control_predictions
    )
    family_cases = tuple(item for item in routing_cases if item.family_exact is not None)
    family_exact_count = sum(item.family_exact is True for item in family_cases)
    routing = RoutingScoreSummary(
        case_count=total,
        disposition_exact_count=(value := sum(item.disposition_exact for item in routing_cases)),
        disposition_accuracy=round(value / total, 6),
        family_scored_case_count=len(family_cases),
        family_exact_count=family_exact_count,
        family_accuracy=(round(family_exact_count / len(family_cases), 6) if family_cases else 0.0),
        interaction_intent_exact_count=(
            value := sum(item.interaction_intent_exact for item in routing_cases)
        ),
        interaction_intent_accuracy=round(value / total, 6),
        query_plan_intent_exact_count=(
            value := sum(item.query_plan_intent_exact for item in routing_cases)
        ),
        query_plan_intent_accuracy=round(value / total, 6),
        control_case_count=control_count,
        control_operational_dense_call_count=control_operational_dense_call_count,
        control_no_operational_dense_case_count=control_no_operational_dense_case_count,
        control_no_operational_dense_rate=(
            round(control_no_operational_dense_case_count / control_count, 6)
            if control_count
            else 1.0
        ),
        control_operational_dense_gate_passed=(control_operational_dense_call_count == 0),
    )

    outcomes = {
        candidate: _field_outcomes(predictions, answers, candidate)
        for candidate in _CANDIDATE_ORDER
    }
    field_scores = tuple(
        _field_score(candidate, outcomes[candidate]) for candidate in _CANDIDATE_ORDER
    )
    comparisons = (
        paired_bootstrap(
            "bge-m3",
            "lexical",
            outcomes["bge-m3"],
            outcomes["lexical"],
            seed=_BOOTSTRAP_SEED,
        ),
        paired_bootstrap(
            "kure-v1",
            "lexical",
            outcomes["kure-v1"],
            outcomes["lexical"],
            seed=_BOOTSTRAP_SEED,
        ),
        paired_bootstrap(
            "bge-m3",
            "kure-v1",
            outcomes["bge-m3"],
            outcomes["kure-v1"],
            seed=_BOOTSTRAP_SEED,
        ),
    )
    return ExternalBlindV2ScoreReport(
        scored_at_utc=scored_at_utc,
        implementation_commit=predictions.lock.implementation_commit,
        questions_sha256=raw_questions_sha256,
        answers_sha256=raw_answers_sha256,
        external_commitment_sha256=raw_commitment_sha256,
        prediction_artifact_sha256=raw_predictions_sha256,
        prediction_receipt_sha256=raw_prediction_receipt_sha256,
        prediction_receipt_locator=prediction_receipt.external_locator,
        candidate_order=predictions.lock.candidate_order,
        routing=routing,
        routing_cases=tuple(routing_cases),
        field_scores=field_scores,
        paired_bootstrap=comparisons,
        ood_thresholds=tuple(
            calibrate_ood_threshold(alias, predictions, answers) for alias in _MODEL_ORDER
        ),
    )


def score_revealed_bundle_files(
    *,
    prediction_path: Path,
    question_path: Path,
    answer_path: Path,
    commitment_path: Path,
    prediction_receipt_path: Path,
    output_path: Path,
    scored_at_utc: str,
) -> ExternalBlindV2ScoreReport:
    """Fail closed without creating a report unless the full bundle exists."""

    # Read every required file before parsing or creating the score artifact.
    prediction_raw = _read_required(
        prediction_path,
        "question-only prediction artifact",
        maximum_bytes=_MAX_PREDICTION_BYTES,
    )
    question_raw = _read_required(
        question_path,
        "external question bundle",
        maximum_bytes=_MAX_QUESTION_BUNDLE_BYTES,
    )
    answer_raw = _read_required(
        answer_path,
        "revealed external answer key",
        maximum_bytes=_MAX_PRIVATE_ANSWER_BYTES,
    )
    commitment_raw = _read_required(
        commitment_path,
        "external commitment",
        maximum_bytes=_MAX_COMMITMENT_BYTES,
    )
    prediction_receipt_raw = _read_required(
        prediction_receipt_path,
        "external prediction receipt",
        maximum_bytes=_MAX_RECEIPT_BYTES,
    )
    try:
        predictions = QuestionOnlyPredictionArtifact.model_validate_json(prediction_raw)
        questions = ExternalBlindQuestionOnlySet.model_validate_json(question_raw)
        answers = ExternalBlindPrivateAnswerKey.model_validate_json(answer_raw)
        commitment = ExternalBlindV2Commitment.model_validate_json(commitment_raw)
        prediction_receipt = ExternalBlindPredictionReceipt.model_validate_json(
            prediction_receipt_raw
        )
    except ValueError as error:
        raise ExternalBundleUnavailableError("external blind v2 bundle is invalid") from error
    report = score_revealed_external_bundle(
        predictions,
        questions,
        answers,
        commitment,
        prediction_receipt,
        raw_questions_sha256=_sha256_bytes(question_raw),
        raw_answers_sha256=_sha256_bytes(answer_raw),
        raw_commitment_sha256=_sha256_bytes(commitment_raw),
        raw_predictions_sha256=_sha256_bytes(prediction_raw),
        raw_prediction_receipt_sha256=_sha256_bytes(prediction_receipt_raw),
        scored_at_utc=scored_at_utc,
    )
    _write_new_json_atomically(output_path, report)
    return report


__all__ = [
    "BlindPairedComparison",
    "CandidateExecutionLock",
    "CandidateLockError",
    "ExternalBlindV2Commitment",
    "ExternalBlindV2ReferenceReport",
    "ExternalBlindV2ScoreReport",
    "ExternalBlindExecutionAuthorization",
    "ExternalBlindPredictionReceipt",
    "ExternalBlindPrivateAnswerKey",
    "ExternalBlindQuestionOnlySet",
    "ExternalBundleUnavailableError",
    "ExternalCasePrediction",
    "ExternalSchemaBlindV2Error",
    "FieldScore",
    "FrozenModelArtifactManifest",
    "OfflineOodProbe",
    "OodThresholdEvaluation",
    "QuestionOnlyPredictionArtifact",
    "RankedField",
    "calibrate_ood_threshold",
    "load_candidate_artifact_lock",
    "load_external_blind_v2_protocol",
    "paired_bootstrap",
    "run_and_freeze_question_only_predictions",
    "run_external_blind_v2_reference_gate",
    "score_revealed_bundle_files",
    "score_revealed_external_bundle",
]
