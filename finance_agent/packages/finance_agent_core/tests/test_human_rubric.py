from __future__ import annotations

import pytest

from finance_agent_core.evaluation.human_rubric import (
    CriterionScore,
    HumanEvaluationBatch,
    HumanScorecard,
    load_human_answer_rubric,
    summarize_human_batch,
)


def _scorecard(
    evaluator_id: str,
    evaluator_role: str,
    *,
    factual_score: int = 4,
) -> HumanScorecard:
    rubric = load_human_answer_rubric()
    return HumanScorecard(
        schema_version="1.0",
        rubric_id=rubric.rubric_id,
        case_id="human-case-001",
        evaluator_id=evaluator_id,
        evaluator_role=evaluator_role,
        scores=[
            CriterionScore(
                criterion_id=criterion.criterion_id,
                score=(factual_score if criterion.criterion_id == "factual_grounding" else 4),
                note=f"{criterion.label} 기준에 따라 근거를 확인함",
            )
            for criterion in rubric.criteria
        ],
        preference="agent",
        overall_note="근거와 답변 구조를 함께 검토한 독립 평가",
    )


def test_human_rubric_is_weighted_and_requires_independent_reviewers() -> None:
    rubric = load_human_answer_rubric()
    batch = HumanEvaluationBatch(
        schema_version="1.0",
        rubric_id=rubric.rubric_id,
        scorecards=[
            _scorecard("finance-reviewer", "financial_domain"),
            _scorecard("product-reviewer", "product"),
        ],
    )

    summary = summarize_human_batch(batch, rubric)[0]

    assert sum(item.weight for item in rubric.criteria) == 1.0
    assert summary.reviewer_count == 2
    assert summary.weighted_mean == 4.0
    assert summary.critical_gate_passed
    assert summary.preferences == {"agent": 2}


def test_human_rubric_fails_critical_gate_even_when_mean_is_acceptable() -> None:
    rubric = load_human_answer_rubric()
    batch = HumanEvaluationBatch(
        schema_version="1.0",
        rubric_id=rubric.rubric_id,
        scorecards=[
            _scorecard("finance-reviewer", "financial_domain", factual_score=3),
            _scorecard("product-reviewer", "product", factual_score=3),
        ],
    )

    summary = summarize_human_batch(batch, rubric)[0]

    assert summary.weighted_mean >= 3
    assert not summary.critical_gate_passed


def test_human_rubric_rejects_single_or_incomplete_review() -> None:
    rubric = load_human_answer_rubric()
    single = HumanEvaluationBatch(
        schema_version="1.0",
        rubric_id=rubric.rubric_id,
        scorecards=[_scorecard("only-reviewer", "financial_domain")],
    )
    with pytest.raises(ValueError, match="not enough independent reviewers"):
        single.validate_against(rubric)

    first = _scorecard("finance-reviewer", "financial_domain")
    incomplete = HumanEvaluationBatch(
        schema_version="1.0",
        rubric_id=rubric.rubric_id,
        scorecards=[
            first.model_copy(update={"scores": first.scores[:-1]}),
            _scorecard("product-reviewer", "product"),
        ],
    )
    with pytest.raises(ValueError, match="criterion order or coverage differs"):
        incomplete.validate_against(rubric)
