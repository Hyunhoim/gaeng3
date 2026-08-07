from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import Intent, ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition

EXTERNAL_BLIND_SUITE_ID = "external-blind-v1-100"
EXTERNAL_BLIND_CASE_COUNT = 100
EXTERNAL_FAMILY_QUOTAS = {
    "overseas_etp": 25,
    "domestic_etp": 25,
    "bond": 25,
    "fund": 25,
}
EXTERNAL_INTENT_QUOTAS = {
    "search": 24,
    "detail": 12,
    "compare": 16,
    "aggregate": 12,
    "explain": 12,
    "clarify": 12,
    "unsupported": 12,
}


class ExternalHoldoutModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalBlindQuestion(ExternalHoldoutModel):
    id: str = Field(pattern=r"^external-blind-v1-\d{3}$")
    product_family: ProductFamily
    intent: InteractionIntent
    question: str = Field(min_length=5, max_length=2000)
    author_note: str = Field(min_length=5, max_length=1000)


class ExternalBlindQuestionSet(ExternalHoldoutModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["authored_externally_before_reveal"]
    author_role: Literal["financial_domain"]
    cases: list[ExternalBlindQuestion] = Field(
        min_length=EXTERNAL_BLIND_CASE_COUNT,
        max_length=EXTERNAL_BLIND_CASE_COUNT,
    )

    @model_validator(mode="after")
    def validate_distribution(self) -> ExternalBlindQuestionSet:
        expected_ids = [f"external-blind-v1-{index:03d}" for index in range(1, 101)]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("external blind ids must be ordered from 001 through 100")
        normalized = ["".join(case.question.casefold().split()) for case in self.cases]
        if len(normalized) != len(set(normalized)):
            raise ValueError("external blind questions must be unique")
        family_counts = Counter(case.product_family.value for case in self.cases)
        intent_counts = Counter(case.intent.value for case in self.cases)
        if dict(family_counts) != EXTERNAL_FAMILY_QUOTAS:
            raise ValueError(
                f"family quotas differ: expected {EXTERNAL_FAMILY_QUOTAS}, "
                f"got {dict(family_counts)}"
            )
        if dict(intent_counts) != EXTERNAL_INTENT_QUOTAS:
            raise ValueError(
                f"intent quotas differ: expected {EXTERNAL_INTENT_QUOTAS}, "
                f"got {dict(intent_counts)}"
            )
        return self


class ExternalBlindAnswer(ExternalHoldoutModel):
    id: str = Field(pattern=r"^external-blind-v1-\d{3}$")
    expected_disposition: RouteDisposition
    expected_query_plan_intent: Intent | None
    expected_query_plan: dict[str, Any] | None
    expected_candidate_count: int | None = Field(default=None, ge=0)
    expected_product_ids: list[str] = Field(max_length=100)
    required_answer_checks: list[str] = Field(max_length=30)
    rationale: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def validate_answer_shape(self) -> ExternalBlindAnswer:
        if self.expected_disposition is RouteDisposition.EXECUTE:
            if (
                self.expected_query_plan_intent is None
                or self.expected_query_plan is None
                or self.expected_candidate_count is None
            ):
                raise ValueError("executable answers require plan and Oracle expectations")
            plan = QueryPlan.model_validate(self.expected_query_plan)
            if plan.intent is not self.expected_query_plan_intent:
                raise ValueError("expected QueryPlan intent differs from answer metadata")
            if len(self.expected_product_ids) > self.expected_candidate_count:
                raise ValueError("expected products cannot exceed candidate_count")
        elif (
            self.expected_query_plan_intent is not None
            or self.expected_query_plan is not None
            or self.expected_candidate_count is not None
            or self.expected_product_ids
        ):
            raise ValueError("control answers must not contain executable expectations")
        return self


class ExternalBlindAnswerKey(ExternalHoldoutModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["private_answer_key_before_reveal"]
    reviewer_role: Literal["financial_domain"]
    database_sha256_by_family: dict[ProductFamily, str]
    cases: list[ExternalBlindAnswer] = Field(
        min_length=EXTERNAL_BLIND_CASE_COUNT,
        max_length=EXTERNAL_BLIND_CASE_COUNT,
    )

    @model_validator(mode="after")
    def validate_answer_key(self) -> ExternalBlindAnswerKey:
        expected_ids = [f"external-blind-v1-{index:03d}" for index in range(1, 101)]
        if [case.id for case in self.cases] != expected_ids:
            raise ValueError("external blind answer ids must be ordered from 001 through 100")
        if set(self.database_sha256_by_family) != set(ProductFamily):
            raise ValueError("answer key requires one database hash per product family")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.database_sha256_by_family.values()
        ):
            raise ValueError("database hashes must be lowercase SHA-256")
        return self


class ExternalBlindCommitment(ExternalHoldoutModel):
    protocol_version: Literal["1.0"]
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["sealed_external_before_reveal"]
    author_role: Literal["financial_domain"]
    implementation_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answers_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    question_count: Literal[100]
    created_at_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ExternalBlindFirstRunState(ExternalHoldoutModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: Literal["external-blind-v1-100"]
    status: Literal["started", "completed"]
    implementation_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answers_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    started_at_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    completed_at_utc: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
    )
    report_name: str | None = Field(default=None, min_length=1, max_length=255)
    report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_completion(self) -> ExternalBlindFirstRunState:
        completion = (self.completed_at_utc, self.report_name, self.report_sha256)
        if self.status == "started" and any(value is not None for value in completion):
            raise ValueError("started first-run state cannot contain completion fields")
        if self.status == "completed" and any(value is None for value in completion):
            raise ValueError("completed first-run state requires report metadata")
        if self.report_name is not None and Path(self.report_name).name != self.report_name:
            raise ValueError("first-run report_name must be a filename")
        return self


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_question(question: str) -> str:
    return re.sub(r"[\W_]+", "", question.casefold())


def reject_external_near_duplicates(
    questions: ExternalBlindQuestionSet,
    reference_questions: list[str],
    *,
    max_similarity: float = 0.84,
) -> None:
    if not 0 < max_similarity <= 1:
        raise ValueError("max_similarity must be in (0, 1]")
    normalized_references = [
        (_normalize_question(question), question) for question in reference_questions
    ]
    violations: list[str] = []
    for case in questions.cases:
        normalized = _normalize_question(case.question)
        for reference, reference_raw in normalized_references:
            similarity = SequenceMatcher(None, normalized, reference).ratio()
            if similarity >= max_similarity:
                violations.append(
                    f"{case.id} similarity={similarity:.3f} reference={reference_raw[:80]!r}"
                )
                break
    if violations:
        raise ValueError(
            "external blind questions are too similar to frozen references: "
            + "; ".join(violations[:10])
        )


def validate_external_blind_bundle(
    questions: ExternalBlindQuestionSet,
    answers: ExternalBlindAnswerKey,
) -> dict[str, Any]:
    if questions.suite_id != answers.suite_id:
        raise ValueError("question and answer suite IDs differ")
    question_ids = [case.id for case in questions.cases]
    answer_ids = [case.id for case in answers.cases]
    if question_ids != answer_ids:
        raise ValueError("question and answer IDs differ")
    for question, answer in zip(questions.cases, answers.cases, strict=True):
        if answer.expected_query_plan is not None:
            plan = QueryPlan.model_validate(answer.expected_query_plan)
            if plan.question_id != question.id:
                raise ValueError(f"{question.id}: QueryPlan question_id differs")
            if plan.product_families != [question.product_family]:
                raise ValueError(f"{question.id}: QueryPlan product family differs")
    return {
        "suite_id": questions.suite_id,
        "question_count": len(questions.cases),
        "family_counts": dict(Counter(case.product_family.value for case in questions.cases)),
        "intent_counts": dict(Counter(case.intent.value for case in questions.cases)),
        "disposition_counts": dict(
            Counter(case.expected_disposition.value for case in answers.cases)
        ),
    }


def create_external_blind_commitment(
    question_path: Path,
    answer_path: Path,
    *,
    implementation_commit: str,
    created_at_utc: str,
    reference_questions: list[str] | None = None,
) -> ExternalBlindCommitment:
    questions = ExternalBlindQuestionSet.model_validate_json(
        question_path.read_text(encoding="utf-8")
    )
    answers = ExternalBlindAnswerKey.model_validate_json(answer_path.read_text(encoding="utf-8"))
    validate_external_blind_bundle(questions, answers)
    if reference_questions is not None:
        reject_external_near_duplicates(questions, reference_questions)
    return ExternalBlindCommitment(
        protocol_version="1.0",
        suite_id=questions.suite_id,
        status="sealed_external_before_reveal",
        author_role=questions.author_role,
        implementation_commit=implementation_commit,
        questions_sha256=_sha256(question_path),
        answers_sha256=_sha256(answer_path),
        question_count=len(questions.cases),
        created_at_utc=created_at_utc,
    )


def verify_external_blind_commitment(
    commitment: ExternalBlindCommitment,
    question_path: Path,
    answer_path: Path,
    *,
    implementation_commit: str,
) -> None:
    if implementation_commit != commitment.implementation_commit:
        raise ValueError("implementation commit differs from the sealed commitment")
    if _sha256(question_path) != commitment.questions_sha256:
        raise ValueError("question hash differs from the sealed commitment")
    if _sha256(answer_path) != commitment.answers_sha256:
        raise ValueError("answer hash differs from the sealed commitment")
    questions = ExternalBlindQuestionSet.model_validate_json(
        question_path.read_text(encoding="utf-8")
    )
    answers = ExternalBlindAnswerKey.model_validate_json(answer_path.read_text(encoding="utf-8"))
    validate_external_blind_bundle(questions, answers)


def claim_external_blind_first_run(
    state_path: Path,
    commitment: ExternalBlindCommitment,
    *,
    provider: str,
    model: str | None,
    started_at_utc: str,
) -> ExternalBlindFirstRunState:
    state = ExternalBlindFirstRunState(
        suite_id=commitment.suite_id,
        status="started",
        implementation_commit=commitment.implementation_commit,
        questions_sha256=commitment.questions_sha256,
        answers_sha256=commitment.answers_sha256,
        provider=provider,
        model=model,
        started_at_utc=started_at_utc,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("x", encoding="utf-8") as stream:
        stream.write(f"{state.model_dump_json(indent=2)}\n")
    return state


def complete_external_blind_first_run(
    state_path: Path,
    commitment: ExternalBlindCommitment,
    report_path: Path,
    *,
    completed_at_utc: str,
) -> ExternalBlindFirstRunState:
    state = ExternalBlindFirstRunState.model_validate_json(state_path.read_text(encoding="utf-8"))
    if state.status != "started":
        raise ValueError("external blind first run is already completed")
    expected = (
        commitment.suite_id,
        commitment.implementation_commit,
        commitment.questions_sha256,
        commitment.answers_sha256,
    )
    observed = (
        state.suite_id,
        state.implementation_commit,
        state.questions_sha256,
        state.answers_sha256,
    )
    if observed != expected:
        raise ValueError("first-run state differs from the sealed commitment")
    if not report_path.is_file():
        raise ValueError("first-run report does not exist")
    payload = state.model_dump(mode="json")
    payload.update(
        status="completed",
        completed_at_utc=completed_at_utc,
        report_name=report_path.name,
        report_sha256=_sha256(report_path),
    )
    completed = ExternalBlindFirstRunState.model_validate(payload)
    temporary_path = state_path.with_name(f".{state_path.name}.completing")
    with temporary_path.open("x", encoding="utf-8") as stream:
        json.dump(completed.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary_path.replace(state_path)
    return completed
