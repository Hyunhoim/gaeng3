from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.evaluation.external_holdout import (
    EXTERNAL_FAMILY_QUOTAS,
    EXTERNAL_INTENT_QUOTAS,
)
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_schema_embedding_candidate_link,
)
from finance_agent_core.evaluation.schema_embedding_external_v2 import (
    CandidateExecutionLock,
    ExternalBlindExecutionAuthorization,
    ExternalBlindPredictionReceipt,
    ExternalBlindPrivateAnswer,
    ExternalBlindPrivateAnswerKey,
    ExternalBlindQuestionOnlyCase,
    ExternalBlindQuestionOnlySet,
    ExternalBlindV2Commitment,
    ExternalCasePrediction,
    FrozenModelArtifactBinding,
    ModelCasePrediction,
    OfflineOodProbe,
    QuestionOnlyPredictionArtifact,
    RankedField,
    _sha256_bytes,
    _sha256_text,
    _write_new_json_atomically,
    score_revealed_external_bundle,
)

_IMPLEMENTATION_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_REFERENCE = "registry.invalid/internal-synthetic-not-blind@sha256:" + "a" * 64
_PROTOCOL_SHA256 = _sha256_text("internal-synthetic-not-blind:protocol")
_REFERENCE_CORPUS_SHA256 = _sha256_text("internal-synthetic-not-blind:reference-corpus")
_REFERENCE_REPORT_SHA256 = _sha256_text("internal-synthetic-not-blind:reference-report")
_TIMES = {
    "commitment": "2026-08-13T00:00:00Z",
    "authorization": "2026-08-13T00:10:00Z",
    "prediction": "2026-08-13T00:20:00Z",
    "receipt": "2026-08-13T00:30:00Z",
    "score": "2026-08-13T00:40:00Z",
}


class SyntheticRehearsalIntegrityError(RuntimeError):
    """Raised when a mechanics-only rehearsal artifact no longer matches its report."""


class SyntheticRehearsalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


SyntheticArtifactKind = Literal[
    "questions",
    "answers",
    "commitment",
    "authorization",
    "predictions",
    "receipt",
]


class SyntheticArtifactEnvelope(SyntheticRehearsalModel):
    """A persisted wrapper that official blind-v2 loaders must reject."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    never_model_selection_evidence: Literal[True] = True
    artifact_kind: SyntheticArtifactKind
    payload: dict[str, object]


class SyntheticArtifactDigest(SyntheticRehearsalModel):
    filename: str = Field(pattern=r"^internal-synthetic-not-blind-[a-z-]+\.json$")
    artifact_kind: SyntheticArtifactKind
    sha256: str = Field(pattern=_SHA256_PATTERN)


class SyntheticMechanicsSummary(SyntheticRehearsalModel):
    disposition_accuracy: float = Field(ge=0, le=1)
    family_accuracy: float = Field(ge=0, le=1)
    interaction_intent_accuracy: float = Field(ge=0, le=1)
    control_operational_dense_call_count: int = Field(ge=0)
    field_recall_at_5: dict[Literal["lexical", "bge-m3", "kure-v1"], float]
    ood_test_gate_by_model: dict[Literal["bge-m3", "kure-v1"], bool]

    @model_validator(mode="after")
    def require_candidate_populations(self) -> SyntheticMechanicsSummary:
        if set(self.field_recall_at_5) != {"lexical", "bge-m3", "kure-v1"}:
            raise ValueError("synthetic field summary requires all frozen candidates")
        if set(self.ood_test_gate_by_model) != {"bge-m3", "kure-v1"}:
            raise ValueError("synthetic OOD summary requires both frozen models")
        return self


class ExternalBlindV2SyntheticRehearsalReport(SyntheticRehearsalModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["schema-embedding-external-blind-v2"] = (
        "schema-embedding-external-blind-v2"
    )
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    never_model_selection_evidence: Literal[True] = True
    external_independence_present: Literal[False] = False
    real_model_inference_performed: Literal[False] = False
    purpose: Literal["mechanics_and_tamper_contract_rehearsal_only"] = (
        "mechanics_and_tamper_contract_rehearsal_only"
    )
    implementation_commit: str = Field(pattern=_IMPLEMENTATION_COMMIT_PATTERN)
    case_count: Literal[100] = 100
    family_counts: dict[str, int]
    intent_counts: dict[str, int]
    chronology_utc: dict[
        Literal["commitment", "authorization", "prediction", "receipt", "score"], str
    ]
    artifacts: tuple[SyntheticArtifactDigest, ...]
    mechanics: SyntheticMechanicsSummary

    @model_validator(mode="after")
    def require_expected_population_and_artifacts(
        self,
    ) -> ExternalBlindV2SyntheticRehearsalReport:
        if self.family_counts != EXTERNAL_FAMILY_QUOTAS:
            raise ValueError("synthetic rehearsal family quotas differ")
        if self.intent_counts != EXTERNAL_INTENT_QUOTAS:
            raise ValueError("synthetic rehearsal intent quotas differ")
        filenames = tuple(item.filename for item in self.artifacts)
        if len(filenames) != len(set(filenames)):
            raise ValueError("synthetic rehearsal artifact names must be unique")
        if {item.artifact_kind for item in self.artifacts} != {
            "questions",
            "answers",
            "commitment",
            "authorization",
            "predictions",
            "receipt",
        }:
            raise ValueError("synthetic rehearsal requires all mechanics artifact kinds")
        return self


def _json_bytes(model: BaseModel) -> bytes:
    return f"{model.model_dump_json(indent=2)}\n".encode()


def _write_synthetic_envelope(
    path: Path,
    *,
    artifact_kind: SyntheticArtifactKind,
    payload: BaseModel,
) -> None:
    _write_new_json_atomically(
        path,
        SyntheticArtifactEnvelope(
            artifact_kind=artifact_kind,
            payload=payload.model_dump(mode="json"),
        ),
    )


def _labels() -> tuple[tuple[ProductFamily, InteractionIntent], ...]:
    intents = (
        *(InteractionIntent.SEARCH for _ in range(24)),
        *(InteractionIntent.DETAIL for _ in range(12)),
        *(InteractionIntent.COMPARE for _ in range(16)),
        *(InteractionIntent.AGGREGATE for _ in range(12)),
        *(InteractionIntent.EXPLAIN for _ in range(12)),
        *(InteractionIntent.CLARIFY for _ in range(12)),
        *(InteractionIntent.UNSUPPORTED for _ in range(12)),
    )
    families = (
        *(ProductFamily.OVERSEAS_ETP for _ in range(25)),
        *(ProductFamily.DOMESTIC_ETP for _ in range(25)),
        *(ProductFamily.BOND for _ in range(25)),
        *(ProductFamily.FUND for _ in range(25)),
    )
    return tuple(zip(families, intents, strict=True))


def _question_set() -> ExternalBlindQuestionOnlySet:
    return ExternalBlindQuestionOnlySet(
        suite_id="external-blind-v1-100",
        status="question_only_without_gold_labels",
        cases=tuple(
            ExternalBlindQuestionOnlyCase(
                id=f"external-blind-v1-{index:03d}",
                question=(
                    "공개 내부 synthetic mechanics rehearsal 질문 "
                    f"{index:03d}: 상품명에 synthetic-alpha 포함"
                ),
            )
            for index in range(1, 101)
        ),
    )


def _query_plan(case_id: str, family: ProductFamily) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "question_id": case_id,
        "intent": "search",
        "product_families": [family.value],
        "constraints": [
            {
                "field": "product_name",
                "operator": "contains",
                "value": "synthetic-alpha",
                "unit": "none",
                "strength": "locked",
            }
        ],
        "ranking": [],
        "projection": ["product_name"],
        "limit": 5,
        "intent_payload": {
            "comparison_fields": [],
            "group_by": [],
            "aggregations": [],
            "explain_product_ids": [],
        },
        "ambiguities": [],
        "unsupported_conditions": [],
    }


def _answer_key() -> ExternalBlindPrivateAnswerKey:
    cases: list[ExternalBlindPrivateAnswer] = []
    for index, (family, interaction_intent) in enumerate(_labels(), start=1):
        case_id = f"external-blind-v1-{index:03d}"
        disposition = (
            RouteDisposition.CLARIFY
            if interaction_intent is InteractionIntent.CLARIFY
            else RouteDisposition.UNSUPPORTED
            if interaction_intent is InteractionIntent.UNSUPPORTED
            else RouteDisposition.EXECUTE
        )
        executable = disposition is RouteDisposition.EXECUTE
        cases.append(
            ExternalBlindPrivateAnswer(
                id=case_id,
                expected_product_family=family,
                expected_interaction_intent=interaction_intent,
                expected_disposition=disposition,
                expected_query_plan_intent="search" if executable else None,
                expected_query_plan=_query_plan(case_id, family) if executable else None,
                gold_schema_field_ids=("product_name",) if executable else (),
                expected_candidate_count=0 if executable else None,
                expected_product_ids=(),
                required_answer_checks=(),
                rationale=("공식 blind 증거가 아닌 공개 synthetic mechanics rehearsal 정답입니다."),
            )
        )
    return ExternalBlindPrivateAnswerKey(
        suite_id="external-blind-v1-100",
        status="private_labels_and_gold_before_reveal",
        reviewer_role="financial_domain",
        database_sha256_by_family={
            family: _sha256_text(f"internal-synthetic-not-blind:{family.value}")
            for family in ProductFamily
        },
        cases=tuple(cases),
    )


def _artifact_bindings() -> tuple[FrozenModelArtifactBinding, ...]:
    bindings: list[FrozenModelArtifactBinding] = []
    for priority, alias in enumerate(("bge-m3", "kure-v1"), start=1):
        candidate = load_schema_embedding_candidate_link(alias)
        bindings.append(
            FrozenModelArtifactBinding(
                priority=priority,
                alias=alias,
                repository=candidate.model_id,
                revision=candidate.revision,
                manifest_file_sha256=_sha256_text(f"synthetic:{alias}:manifest"),
                snapshot_sha256=_sha256_text(f"synthetic:{alias}:snapshot"),
                weights_sha256=_sha256_text(f"synthetic:{alias}:weights"),
                tokenizer_sha256=_sha256_text(f"synthetic:{alias}:tokenizer"),
                config_sha256=_sha256_text(f"synthetic:{alias}:config"),
                other_sha256=_sha256_text(f"synthetic:{alias}:other"),
            )
        )
    return tuple(bindings)


def _ranked_fields(
    family: ProductFamily,
    *,
    execute: bool,
) -> tuple[RankedField, RankedField]:
    scores = (0.9, 0.5) if execute else (0.2, 0.19)
    return (
        RankedField(product_family=family, field_id="product_name", score=scores[0], rank=1),
        RankedField(product_family=family, field_id="product_id", score=scores[1], rank=2),
    )


def _predictions(
    questions: ExternalBlindQuestionOnlySet,
    answers: ExternalBlindPrivateAnswerKey,
    commitment: ExternalBlindV2Commitment,
    authorization: ExternalBlindExecutionAuthorization,
    *,
    implementation_commit: str,
    questions_sha256: str,
    commitment_sha256: str,
    authorization_sha256: str,
) -> QuestionOnlyPredictionArtifact:
    cases: list[ExternalCasePrediction] = []
    for question, answer in zip(questions.cases, answers.cases, strict=True):
        execute = answer.expected_disposition is RouteDisposition.EXECUTE
        ranked = _ranked_fields(answer.expected_product_family, execute=execute)
        models = tuple(
            ModelCasePrediction(
                alias=alias,
                operational_dense_called=execute,
                dense_candidates=ranked if execute else (),
                fused_fields=("product_name", "product_id") if execute else (),
                offline_ood_probe=OfflineOodProbe(
                    candidates=ranked,
                    top_1_score=ranked[0].score,
                    top_1_top_2_margin=round(ranked[0].score - ranked[1].score, 9),
                ),
            )
            for alias in ("bge-m3", "kure-v1")
        )
        cases.append(
            ExternalCasePrediction(
                case_id=question.id,
                question_sha256=_sha256_text(question.question),
                route_disposition=answer.expected_disposition,
                route_interaction_intent=answer.expected_interaction_intent,
                route_product_families=(answer.expected_product_family,),
                route_query_plan_intent=answer.expected_query_plan_intent,
                route_reason_code="internal_synthetic_not_blind",
                lexical_fields=("product_name",) if execute else (),
                models=models,
            )
        )
    return QuestionOnlyPredictionArtifact(
        created_at_utc=_TIMES["prediction"],
        lock=CandidateExecutionLock(
            protocol_sha256=_PROTOCOL_SHA256,
            implementation_commit=implementation_commit,
            questions_sha256=questions_sha256,
            answer_key_sha256_commitment=commitment.answers_sha256,
            reference_corpus_sha256=_REFERENCE_CORPUS_SHA256,
            near_duplicate_report_sha256=_REFERENCE_REPORT_SHA256,
            external_commitment_sha256=commitment_sha256,
            execution_authorization_sha256=authorization_sha256,
            image_reference=authorization.image_reference,
            platform=authorization.platform,
            external_authorization_receipt_sha256=(
                authorization.external_authorization_receipt_sha256
            ),
            commitment_created_at_utc=commitment.created_at_utc,
            authorization_issued_at_utc=authorization.issued_at_utc,
            candidate_order=("lexical", "bge-m3", "kure-v1"),
            model_artifacts=_artifact_bindings(),
        ),
        cases=tuple(cases),
    )


def run_synthetic_external_blind_v2_rehearsal(
    *,
    output_dir: Path,
    implementation_commit: str,
) -> ExternalBlindV2SyntheticRehearsalReport:
    """Exercise blind-v2 mechanics without producing model-selection evidence.

    The caller must choose an empty artifact directory. There is intentionally
    no repository-relative default and no real model/provider is loaded.
    """

    resolved_output = output_dir.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if temporary_root not in resolved_output.parents:
        raise ValueError("synthetic rehearsal output must be a caller-owned temp subdirectory")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"synthetic rehearsal output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    questions = _question_set()
    answers = _answer_key()
    questions_path = output_dir / "internal-synthetic-not-blind-questions.json"
    answers_path = output_dir / "internal-synthetic-not-blind-answers.json"
    _write_synthetic_envelope(questions_path, artifact_kind="questions", payload=questions)
    _write_synthetic_envelope(answers_path, artifact_kind="answers", payload=answers)
    questions_raw = _json_bytes(questions)
    answers_raw = _json_bytes(answers)

    commitment = ExternalBlindV2Commitment(
        suite_id="external-blind-v1-100",
        status="sealed_external_before_reveal",
        author_role="financial_domain",
        implementation_commit=implementation_commit,
        questions_sha256=_sha256_bytes(questions_raw),
        answers_sha256=_sha256_bytes(answers_raw),
        question_count=100,
        created_at_utc=_TIMES["commitment"],
        protocol_sha256=_PROTOCOL_SHA256,
        reference_corpus_sha256=_REFERENCE_CORPUS_SHA256,
        near_duplicate_report_sha256=_REFERENCE_REPORT_SHA256,
    )
    commitment_path = output_dir / "internal-synthetic-not-blind-commitment.json"
    _write_synthetic_envelope(
        commitment_path,
        artifact_kind="commitment",
        payload=commitment,
    )
    commitment_raw = _json_bytes(commitment)

    bindings = _artifact_bindings()
    authorization = ExternalBlindExecutionAuthorization(
        status="authorized_by_independent_evaluator",
        evaluator_role="independent_external_evaluator",
        implementation_commit=implementation_commit,
        image_reference=_IMAGE_REFERENCE,
        platform="linux/amd64",
        clean_source_tree=True,
        questions_sha256=commitment.questions_sha256,
        protocol_sha256=_PROTOCOL_SHA256,
        reference_corpus_sha256=_REFERENCE_CORPUS_SHA256,
        near_duplicate_report_sha256=_REFERENCE_REPORT_SHA256,
        bge_manifest_sha256=bindings[0].manifest_file_sha256,
        kure_manifest_sha256=bindings[1].manifest_file_sha256,
        issued_at_utc=_TIMES["authorization"],
        external_authorization_receipt_sha256=_sha256_text(
            "internal-synthetic-not-blind:authorization-receipt"
        ),
    )
    authorization_path = output_dir / "internal-synthetic-not-blind-authorization.json"
    _write_synthetic_envelope(
        authorization_path,
        artifact_kind="authorization",
        payload=authorization,
    )
    authorization_raw = _json_bytes(authorization)

    predictions = _predictions(
        questions,
        answers,
        commitment,
        authorization,
        implementation_commit=implementation_commit,
        questions_sha256=_sha256_bytes(questions_raw),
        commitment_sha256=_sha256_bytes(commitment_raw),
        authorization_sha256=_sha256_bytes(authorization_raw),
    )
    predictions_path = output_dir / "internal-synthetic-not-blind-predictions.json"
    _write_synthetic_envelope(
        predictions_path,
        artifact_kind="predictions",
        payload=predictions,
    )
    predictions_raw = _json_bytes(predictions)

    receipt = ExternalBlindPredictionReceipt(
        status="prediction_hash_recorded_externally_before_answer_reveal",
        evaluator_role="independent_external_evaluator",
        prediction_artifact_sha256=_sha256_bytes(predictions_raw),
        questions_sha256=_sha256_bytes(questions_raw),
        implementation_commit=implementation_commit,
        image_reference=_IMAGE_REFERENCE,
        recorded_at_utc=_TIMES["receipt"],
        external_locator="internal-synthetic-not-blind://local-mechanics/receipt",
    )
    receipt_path = output_dir / "internal-synthetic-not-blind-receipt.json"
    _write_synthetic_envelope(receipt_path, artifact_kind="receipt", payload=receipt)
    receipt_raw = _json_bytes(receipt)

    score = score_revealed_external_bundle(
        predictions,
        questions,
        answers,
        commitment,
        receipt,
        raw_questions_sha256=_sha256_bytes(questions_raw),
        raw_answers_sha256=_sha256_bytes(answers_raw),
        raw_commitment_sha256=_sha256_bytes(commitment_raw),
        raw_predictions_sha256=_sha256_bytes(predictions_raw),
        raw_prediction_receipt_sha256=_sha256_bytes(receipt_raw),
        scored_at_utc=_TIMES["score"],
    )
    artifact_paths: tuple[tuple[SyntheticArtifactKind, Path], ...] = (
        ("questions", questions_path),
        ("answers", answers_path),
        ("commitment", commitment_path),
        ("authorization", authorization_path),
        ("predictions", predictions_path),
        ("receipt", receipt_path),
    )
    artifacts = tuple(
        SyntheticArtifactDigest(
            filename=path.name,
            artifact_kind=artifact_kind,
            sha256=_sha256_bytes(path.read_bytes()),
        )
        for artifact_kind, path in artifact_paths
    )
    report = ExternalBlindV2SyntheticRehearsalReport(
        implementation_commit=implementation_commit,
        family_counts=dict(
            Counter(answer.expected_product_family.value for answer in answers.cases)
        ),
        intent_counts=dict(
            Counter(answer.expected_interaction_intent.value for answer in answers.cases)
        ),
        chronology_utc=dict(_TIMES),
        artifacts=artifacts,
        mechanics=SyntheticMechanicsSummary(
            disposition_accuracy=score.routing.disposition_accuracy,
            family_accuracy=score.routing.family_accuracy,
            interaction_intent_accuracy=score.routing.interaction_intent_accuracy,
            control_operational_dense_call_count=(
                score.routing.control_operational_dense_call_count
            ),
            field_recall_at_5={
                item.candidate: item.micro_recall_at_5 for item in score.field_scores
            },
            ood_test_gate_by_model={
                item.alias: item.test_gate_passed for item in score.ood_thresholds
            },
        ),
    )
    report_path = output_dir / "internal-synthetic-not-blind-rehearsal-report.json"
    _write_new_json_atomically(report_path, report)
    return verify_synthetic_external_blind_v2_rehearsal(output_dir=output_dir)


def verify_synthetic_external_blind_v2_rehearsal(
    *,
    output_dir: Path,
) -> ExternalBlindV2SyntheticRehearsalReport:
    """Re-read the explicit non-blind report and verify every bound artifact byte."""

    report_path = output_dir / "internal-synthetic-not-blind-rehearsal-report.json"
    try:
        report = ExternalBlindV2SyntheticRehearsalReport.model_validate_json(
            report_path.read_bytes()
        )
    except (OSError, ValueError) as error:
        raise SyntheticRehearsalIntegrityError(
            "synthetic rehearsal report is unavailable or invalid"
        ) from error
    for artifact in report.artifacts:
        path = output_dir / artifact.filename
        if path.parent != output_dir:
            raise SyntheticRehearsalIntegrityError("synthetic artifact escaped output directory")
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise SyntheticRehearsalIntegrityError(
                f"synthetic rehearsal artifact is unavailable: {artifact.filename}"
            ) from error
        observed = _sha256_bytes(raw)
        if observed != artifact.sha256:
            raise SyntheticRehearsalIntegrityError(
                f"synthetic rehearsal artifact hash differs: {artifact.filename}"
            )
        try:
            envelope = SyntheticArtifactEnvelope.model_validate_json(raw)
        except ValueError as error:
            raise SyntheticRehearsalIntegrityError(
                f"synthetic rehearsal artifact is invalid: {artifact.filename}"
            ) from error
        if envelope.artifact_kind != artifact.artifact_kind:
            raise SyntheticRehearsalIntegrityError(
                f"synthetic rehearsal artifact kind differs: {artifact.filename}"
            )
    return report


__all__ = [
    "ExternalBlindV2SyntheticRehearsalReport",
    "SyntheticRehearsalIntegrityError",
    "run_synthetic_external_blind_v2_rehearsal",
    "verify_synthetic_external_blind_v2_rehearsal",
]
