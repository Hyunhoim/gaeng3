from __future__ import annotations

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    ConstraintStrength,
    Ranking,
)
from finance_agent_core.evaluation.models import (
    EvaluationCase,
    EvaluationModel,
    EvaluationSplit,
    ExpectedBlocker,
    ExpectedConstraint,
    ExpectedDisposition,
    OracleExpectation,
)

BLIND_SUITE_ID = "fund-blind-v1.1-100"
BLIND_CASE_COUNT = 100
BLIND_CATEGORY_QUOTAS = {
    "scope_status": 10,
    "classification": 14,
    "risk_hedge": 10,
    "return": 14,
    "aum_currency": 10,
    "lookup": 8,
    "compound": 16,
    "safety": 18,
}
BLIND_LANGUAGE_PROFILE_QUOTAS = {
    "explicit": 20,
    "paraphrase": 25,
    "implicit_public_scope": 20,
    "colloquial_ellipsis": 15,
    "noisy_surface": 10,
    "adversarial": 10,
}
BLIND_DISPOSITION_QUOTAS = {
    "execute": 72,
    "ambiguity": 12,
    "unsupported": 16,
}
BLIND_CATEGORY_DISPOSITION_QUOTAS = {
    "scope_status": {"execute": 8, "ambiguity": 1, "unsupported": 1},
    "classification": {"execute": 12, "ambiguity": 1, "unsupported": 1},
    "risk_hedge": {"execute": 8, "ambiguity": 1, "unsupported": 1},
    "return": {"execute": 12, "ambiguity": 1, "unsupported": 1},
    "aum_currency": {"execute": 8, "ambiguity": 2, "unsupported": 0},
    "lookup": {"execute": 8, "ambiguity": 0, "unsupported": 0},
    "compound": {"execute": 14, "ambiguity": 1, "unsupported": 1},
    "safety": {"execute": 2, "ambiguity": 5, "unsupported": 11},
}
BLIND_LANGUAGE_DISPOSITION_QUOTAS = {
    "explicit": {"execute": 16, "ambiguity": 2, "unsupported": 2},
    "paraphrase": {"execute": 19, "ambiguity": 3, "unsupported": 3},
    "implicit_public_scope": {"execute": 15, "ambiguity": 2, "unsupported": 3},
    "colloquial_ellipsis": {"execute": 11, "ambiguity": 2, "unsupported": 2},
    "noisy_surface": {"execute": 7, "ambiguity": 1, "unsupported": 2},
    "adversarial": {"execute": 4, "ambiguity": 2, "unsupported": 4},
}


class BlindCategory(StrEnum):
    SCOPE_STATUS = "scope_status"
    CLASSIFICATION = "classification"
    RISK_HEDGE = "risk_hedge"
    RETURN = "return"
    AUM_CURRENCY = "aum_currency"
    LOOKUP = "lookup"
    COMPOUND = "compound"
    SAFETY = "safety"


class BlindLanguageProfile(StrEnum):
    EXPLICIT = "explicit"
    PARAPHRASE = "paraphrase"
    IMPLICIT_PUBLIC_SCOPE = "implicit_public_scope"
    COLLOQUIAL_ELLIPSIS = "colloquial_ellipsis"
    NOISY_SURFACE = "noisy_surface"
    ADVERSARIAL = "adversarial"


class BlindQuestion(EvaluationModel):
    id: str = Field(pattern=r"^fund-blind-v1\.1-\d{3}$")
    question: str = Field(min_length=5, max_length=1000)
    category: BlindCategory
    language_profile: BlindLanguageProfile


class BlindQuestionSet(EvaluationModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["fund-blind-v1.1-100"]
    dataset: Literal["fund"]
    author_role: Literal["financial_domain"]
    cases: list[BlindQuestion] = Field(min_length=BLIND_CASE_COUNT, max_length=BLIND_CASE_COUNT)

    @model_validator(mode="after")
    def validate_questions(self) -> BlindQuestionSet:
        expected_ids = [f"fund-blind-v1.1-{index:03d}" for index in range(1, 101)]
        ids = [case.id for case in self.cases]
        if ids != expected_ids:
            raise ValueError("blind question ids must be ordered from 001 through 100")
        normalized = [_normalize_question(case.question) for case in self.cases]
        if len(normalized) != len(set(normalized)):
            raise ValueError("blind questions must be unique after normalization")
        _require_exact_counts(
            "category",
            Counter(case.category.value for case in self.cases),
            BLIND_CATEGORY_QUOTAS,
        )
        _require_exact_counts(
            "language profile",
            Counter(case.language_profile.value for case in self.cases),
            BLIND_LANGUAGE_PROFILE_QUOTAS,
        )
        for case in self.cases:
            if (
                case.language_profile is BlindLanguageProfile.EXPLICIT
                and "공모펀드" not in case.question
            ):
                raise ValueError(f"{case.id}: explicit profile must contain 공모펀드")
            if (
                case.language_profile is BlindLanguageProfile.IMPLICIT_PUBLIC_SCOPE
                and "공모펀드" in case.question
            ):
                raise ValueError(
                    f"{case.id}: implicit_public_scope must omit the exact 공모펀드 phrase"
                )
        return self


class BlindAnswer(EvaluationModel):
    id: str = Field(pattern=r"^fund-blind-v1\.1-\d{3}$")
    constraints: list[ExpectedConstraint] = Field(max_length=20)
    ranking: list[Ranking] = Field(max_length=5)
    limit: int = Field(ge=1, le=100)
    disposition: ExpectedDisposition
    blocker: ExpectedBlocker | None = None
    oracle: OracleExpectation | None = None
    rationale: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def validate_answer(self) -> BlindAnswer:
        if self.disposition is ExpectedDisposition.EXECUTE:
            if self.blocker is not None or self.oracle is None:
                raise ValueError("executable blind answers require oracle and no blocker")
            if len(self.oracle.top_product_ids) > self.limit:
                raise ValueError("oracle result cannot exceed the answer limit")
            if len(self.oracle.top_product_ids) > self.oracle.candidate_count:
                raise ValueError("oracle result cannot exceed candidate_count")
        elif self.blocker is None or self.oracle is not None:
            raise ValueError("blocked blind answers require blocker and no oracle")
        if self.disposition is ExpectedDisposition.BLOCK and self.ranking:
            raise ValueError("blocked blind answers must not retain executable ranking")
        return self


class BlindAnswerKey(EvaluationModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["fund-blind-v1.1-100"]
    dataset: Literal["fund"]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[BlindAnswer] = Field(min_length=BLIND_CASE_COUNT, max_length=BLIND_CASE_COUNT)

    @model_validator(mode="after")
    def validate_answers(self) -> BlindAnswerKey:
        expected_ids = [f"fund-blind-v1.1-{index:03d}" for index in range(1, 101)]
        ids = [case.id for case in self.cases]
        if ids != expected_ids:
            raise ValueError("blind answer ids must be ordered from 001 through 100")
        blocker_counts = Counter(
            case.blocker.value if case.blocker is not None else "execute" for case in self.cases
        )
        _require_exact_counts(
            "disposition",
            blocker_counts,
            BLIND_DISPOSITION_QUOTAS,
        )
        return self


class BlindCommitment(EvaluationModel):
    protocol_version: Literal["1.0"]
    suite_id: Literal["fund-blind-v1.1-100"]
    dataset: Literal["fund"]
    status: Literal["sealed_before_reveal"]
    author_role: Literal["financial_domain"]
    parser_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answers_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    question_count: Literal[100]
    created_at_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _require_exact_counts(
    label: str,
    actual: Counter[str],
    expected: dict[str, int],
) -> None:
    if dict(actual) != expected:
        raise ValueError(f"blind {label} quotas differ: expected {expected}, got {dict(actual)}")


def _require_exact_matrix(
    label: str,
    actual: Counter[tuple[str, str]],
    expected: dict[str, dict[str, int]],
) -> None:
    expected_flat = {
        (row, column): count
        for row, columns in expected.items()
        for column, count in columns.items()
    }
    actual_flat = {key: actual.get(key, 0) for key in expected_flat}
    if actual_flat != expected_flat or any(key not in expected_flat for key in actual):
        raise ValueError(
            f"blind {label} matrix differs: expected {expected_flat}, got {dict(actual)}"
        )


def _normalize_question(question: str) -> str:
    return re.sub(r"[\W_]+", "", question.casefold())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_blind_bundle(
    questions: BlindQuestionSet,
    answers: BlindAnswerKey,
) -> dict[str, Any]:
    if questions.suite_id != answers.suite_id or questions.dataset != answers.dataset:
        raise ValueError("blind question and answer metadata must match")
    question_ids = [case.id for case in questions.cases]
    answer_ids = [case.id for case in answers.cases]
    if question_ids != answer_ids:
        raise ValueError("blind question and answer ids must match in the same order")

    for question, answer in zip(questions.cases, answers.cases, strict=True):
        case = EvaluationCase(
            id=question.id,
            split=EvaluationSplit.HOLDOUT,
            category=question.category.value,
            question=question.question,
            constraints=answer.constraints,
            ranking=answer.ranking,
            limit=answer.limit,
            disposition=answer.disposition,
            blocker=answer.blocker,
            oracle=answer.oracle,
        )
        plan = case.expected_plan("fund")
        public_scope = [
            constraint for constraint in plan.constraints if constraint.field == "public_offering"
        ]
        if (
            len(public_scope) != 1
            or public_scope[0].operator is not ConstraintOperator.EQ
            or public_scope[0].value is not True
            or public_scope[0].strength is not ConstraintStrength.LOCKED
        ):
            raise ValueError(f"{question.id}: public_offering=true locked must appear exactly once")
        aum_fields = {constraint.field for constraint in plan.constraints} | {
            ranking.field for ranking in plan.ranking
        }
        if answer.disposition is ExpectedDisposition.EXECUTE and "aum" in aum_fields:
            currency_scope = [
                constraint
                for constraint in plan.constraints
                if constraint.field == "trading_currency"
                and constraint.operator is ConstraintOperator.EQ
            ]
            if len(currency_scope) != 1:
                raise ValueError(
                    f"{question.id}: executable AUM cases require one equality currency"
                )

    category_dispositions = Counter(
        (
            question.category.value,
            answer.blocker.value if answer.blocker is not None else "execute",
        )
        for question, answer in zip(questions.cases, answers.cases, strict=True)
    )
    language_dispositions = Counter(
        (
            question.language_profile.value,
            answer.blocker.value if answer.blocker is not None else "execute",
        )
        for question, answer in zip(questions.cases, answers.cases, strict=True)
    )
    _require_exact_matrix(
        "category/disposition",
        category_dispositions,
        BLIND_CATEGORY_DISPOSITION_QUOTAS,
    )
    _require_exact_matrix(
        "language/disposition",
        language_dispositions,
        BLIND_LANGUAGE_DISPOSITION_QUOTAS,
    )

    return {
        "suite_id": questions.suite_id,
        "question_count": len(questions.cases),
        "categories": dict(Counter(case.category.value for case in questions.cases)),
        "language_profiles": dict(Counter(case.language_profile.value for case in questions.cases)),
        "dispositions": dict(
            Counter(
                case.blocker.value if case.blocker is not None else "execute"
                for case in answers.cases
            )
        ),
        "category_dispositions": {
            category: dict(counts) for category, counts in BLIND_CATEGORY_DISPOSITION_QUOTAS.items()
        },
        "language_dispositions": {
            profile: dict(counts) for profile, counts in BLIND_LANGUAGE_DISPOSITION_QUOTAS.items()
        },
    }


def build_blind_evaluation_cases(
    questions: BlindQuestionSet,
    answers: BlindAnswerKey,
) -> list[EvaluationCase]:
    validate_blind_bundle(questions, answers)
    return [
        EvaluationCase(
            id=question.id,
            split=EvaluationSplit.HOLDOUT,
            category=question.category.value,
            question=question.question,
            constraints=answer.constraints,
            ranking=answer.ranking,
            limit=answer.limit,
            disposition=answer.disposition,
            blocker=answer.blocker,
            oracle=answer.oracle,
        )
        for question, answer in zip(questions.cases, answers.cases, strict=True)
    ]


def reject_near_duplicates(
    questions: BlindQuestionSet,
    reference_questions: list[str],
    *,
    max_similarity: float = 0.84,
) -> None:
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
        joined = "; ".join(violations[:10])
        raise ValueError(f"blind questions are too similar to the frozen reference: {joined}")


def create_blind_commitment(
    question_path: Path,
    answer_path: Path,
    *,
    parser_commit: str,
    created_at_utc: str,
    reference_questions: list[str] | None = None,
) -> BlindCommitment:
    questions = BlindQuestionSet.model_validate_json(question_path.read_text(encoding="utf-8"))
    answers = BlindAnswerKey.model_validate_json(answer_path.read_text(encoding="utf-8"))
    validate_blind_bundle(questions, answers)
    if reference_questions is not None:
        reject_near_duplicates(questions, reference_questions)
    return BlindCommitment(
        protocol_version="1.0",
        suite_id=BLIND_SUITE_ID,
        dataset="fund",
        status="sealed_before_reveal",
        author_role=questions.author_role,
        parser_commit=parser_commit,
        questions_sha256=sha256_file(question_path),
        answers_sha256=sha256_file(answer_path),
        question_count=BLIND_CASE_COUNT,
        created_at_utc=created_at_utc,
    )


def verify_blind_commitment(
    commitment: BlindCommitment,
    question_path: Path,
    answer_path: Path,
    *,
    parser_commit: str,
) -> None:
    if parser_commit != commitment.parser_commit:
        raise ValueError(
            f"parser commit differs: expected {commitment.parser_commit}, got {parser_commit}"
        )
    if sha256_file(question_path) != commitment.questions_sha256:
        raise ValueError("blind question hash differs from the sealed commitment")
    if sha256_file(answer_path) != commitment.answers_sha256:
        raise ValueError("blind answer hash differs from the sealed commitment")


def blind_bundle_sha256(commitment: BlindCommitment) -> str:
    payload = f"{commitment.questions_sha256}:{commitment.answers_sha256}".encode()
    return hashlib.sha256(payload).hexdigest()
