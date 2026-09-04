from __future__ import annotations

import json
from collections import Counter, defaultdict
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HumanRubricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RubricScale(HumanRubricModel):
    minimum: Literal[1]
    maximum: Literal[5]
    anchors: dict[Literal["1", "2", "3", "4", "5"], str]


class RubricCriterion(HumanRubricModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    label: str
    description: str
    weight: float = Field(gt=0, le=1)
    critical_minimum: int = Field(ge=1, le=5)


class HumanAnswerRubric(HumanRubricModel):
    schema_version: Literal["1.0"]
    rubric_id: Literal["human-answer-rubric-v1"]
    scale: RubricScale
    minimum_reviewers_per_case: int = Field(ge=2, le=5)
    criteria: list[RubricCriterion] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_rubric(self) -> HumanAnswerRubric:
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric criterion IDs must be unique")
        if round(sum(criterion.weight for criterion in self.criteria), 10) != 1.0:
            raise ValueError("rubric weights must sum to 1.0")
        return self


class CriterionScore(HumanRubricModel):
    criterion_id: str
    score: int = Field(ge=1, le=5)
    note: str = Field(min_length=5, max_length=1000)


class HumanScorecard(HumanRubricModel):
    schema_version: Literal["1.0"]
    rubric_id: Literal["human-answer-rubric-v1"]
    case_id: str = Field(min_length=1, max_length=128)
    evaluator_id: str = Field(pattern=r"^[A-Za-z0-9._-]{2,64}$")
    evaluator_role: Literal[
        "financial_domain",
        "product",
        "frontend",
        "backend",
        "ai",
    ]
    scores: list[CriterionScore]
    preference: Literal["agent", "deterministic", "tie"]
    overall_note: str = Field(min_length=5, max_length=2000)


class HumanEvaluationBatch(HumanRubricModel):
    schema_version: Literal["1.0"]
    rubric_id: Literal["human-answer-rubric-v1"]
    scorecards: list[HumanScorecard] = Field(min_length=1)

    def validate_against(self, rubric: HumanAnswerRubric) -> None:
        expected = [criterion.criterion_id for criterion in rubric.criteria]
        reviewers: dict[str, list[str]] = defaultdict(list)
        for scorecard in self.scorecards:
            actual = [score.criterion_id for score in scorecard.scores]
            if actual != expected:
                raise ValueError(
                    f"{scorecard.case_id}/{scorecard.evaluator_id}: "
                    "criterion order or coverage differs from rubric"
                )
            reviewers[scorecard.case_id].append(scorecard.evaluator_id)
        for case_id, evaluator_ids in reviewers.items():
            if len(evaluator_ids) != len(set(evaluator_ids)):
                raise ValueError(f"{case_id}: duplicate evaluator scorecard")
            if len(evaluator_ids) < rubric.minimum_reviewers_per_case:
                raise ValueError(f"{case_id}: not enough independent reviewers")


class HumanCaseSummary(HumanRubricModel):
    case_id: str
    reviewer_count: int
    weighted_mean: float
    critical_gate_passed: bool
    criterion_means: dict[str, float]
    preferences: dict[str, int]


def load_human_answer_rubric() -> HumanAnswerRubric:
    resource = files("finance_agent_core.evaluation.rubrics").joinpath(
        "human_answer_rubric_v1.json"
    )
    return HumanAnswerRubric.model_validate(json.loads(resource.read_text(encoding="utf-8")))


def summarize_human_batch(
    batch: HumanEvaluationBatch,
    rubric: HumanAnswerRubric | None = None,
) -> list[HumanCaseSummary]:
    actual_rubric = rubric or load_human_answer_rubric()
    batch.validate_against(actual_rubric)
    by_case: dict[str, list[HumanScorecard]] = defaultdict(list)
    for scorecard in batch.scorecards:
        by_case[scorecard.case_id].append(scorecard)
    summaries: list[HumanCaseSummary] = []
    for case_id, scorecards in sorted(by_case.items()):
        criterion_means = {
            criterion.criterion_id: round(
                sum(
                    next(
                        score.score
                        for score in scorecard.scores
                        if score.criterion_id == criterion.criterion_id
                    )
                    for scorecard in scorecards
                )
                / len(scorecards),
                3,
            )
            for criterion in actual_rubric.criteria
        }
        weighted_mean = round(
            sum(
                criterion_means[criterion.criterion_id] * criterion.weight
                for criterion in actual_rubric.criteria
            ),
            3,
        )
        critical_gate_passed = all(
            criterion_means[criterion.criterion_id] >= criterion.critical_minimum
            for criterion in actual_rubric.criteria
        )
        summaries.append(
            HumanCaseSummary(
                case_id=case_id,
                reviewer_count=len(scorecards),
                weighted_mean=weighted_mean,
                critical_gate_passed=critical_gate_passed,
                criterion_means=criterion_means,
                preferences=dict(Counter(scorecard.preference for scorecard in scorecards)),
            )
        )
    return summaries
