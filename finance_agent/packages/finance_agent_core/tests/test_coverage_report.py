from __future__ import annotations

import pytest

from finance_agent_core.evaluation.coverage_ablation import (
    CoverageAblationReport,
    CoverageBucketDelta,
    CoveragePairwiseDelta,
    CoverageProfileSnapshot,
    CoverageProviderCallDelta,
)
from finance_agent_core.evaluation.coverage_questions import (
    CoverageQuestionBatch,
    CoverageQuestionCandidate,
)
from finance_agent_core.evaluation.coverage_report import (
    _accepted_source_sha256,
    render_coverage_experiment_markdown,
)
from finance_agent_core.evaluation.metamorphic import MutationAxis, MutationValidation
from finance_agent_core.evaluation.red_team_e2e import ProviderCallSnapshot

_HASH = "a" * 64


def _calls(*, plans: int = 0) -> ProviderCallSnapshot:
    return ProviderCallSnapshot(
        query_plan_calls=plans,
        query_plan_errors=0,
        query_plan_latency_ms=float(plans * 100),
        answer_calls=0,
        answer_errors=0,
        answer_latency_ms=0,
    )


def _profile(
    label: str,
    *,
    passed: int,
    plans: int,
    p95: float,
) -> CoverageProfileSnapshot:
    return CoverageProfileSnapshot(
        label=label,
        agent_profile=label,
        agent_model=None if plans == 0 else "qwen3-local-test",
        total=3,
        passed=passed,
        strict_accuracy=round(passed / 3, 6),
        strict_accuracy_ci95=[0.1, 0.9],
        plan_semantic_passed=passed,
        plan_semantic_rate=round(passed / 3, 6),
        evidence_semantic_passed=passed,
        evidence_semantic_rate=round(passed / 3, 6),
        fallback_count=0,
        first_failure_stages={} if passed == 3 else {"planning": 3 - passed},
        latency_ms={"min": 1.0, "p50": 2.0, "p95": p95, "max": p95 + 1},
        provider_calls=_calls(plans=plans),
    )


def _report(*, source_hash: str = _HASH) -> CoverageAblationReport:
    bucket = CoverageBucketDelta(
        value="search_constraint",
        total=3,
        baseline_passed=1,
        candidate_passed=2,
        baseline_accuracy=0.333333,
        candidate_accuracy=0.666667,
        accuracy_delta=0.333334,
        rescued=1,
        regressed=0,
        net_rescued=1,
    )
    return CoverageAblationReport(
        ablation_id="coverage-naturalized-ablation-v1",
        generated_at_utc="2026-08-08T00:00:00+00:00",
        source_kind="naturalized",
        source_semantic_sha256=source_hash,
        statistical_unit="source_plan_cluster",
        statistical_unit_count=1,
        baseline_label="expected",
        profiles=[
            _profile("expected", passed=1, plans=0, p95=10),
            _profile("local_test_grounded_plan_only", passed=2, plans=3, p95=310),
        ],
        pairwise_deltas=[
            CoveragePairwiseDelta(
                baseline_label="expected",
                candidate_label="local_test_grounded_plan_only",
                total=3,
                strict_accuracy_delta=0.333334,
                strict_accuracy_delta_ci95=[0.0, 0.666667],
                plan_semantic_rate_delta=0.333334,
                evidence_semantic_rate_delta=0.333334,
                rescued=1,
                regressed=0,
                unchanged_pass=1,
                unchanged_fail=1,
                plan_rescued=1,
                plan_regressed=0,
                evidence_rescued=1,
                evidence_regressed=0,
                rescued_case_ids=["question-2"],
                regressed_case_ids=[],
                stage_transitions={"planning->pass": 1},
                mcnemar_exact_p_value=1.0,
                holm_adjusted_p_value=1.0,
                statistically_significant_after_holm=False,
                zero_strict_regression=True,
                breakdowns={"kind": [bucket]},
                provider_call_delta=CoverageProviderCallDelta(
                    query_plan_calls=3,
                    query_plan_errors=0,
                    query_plan_latency_ms=300,
                    answer_calls=0,
                    answer_errors=0,
                    answer_latency_ms=0,
                ),
                latency_delta_ms={"min": 0.0, "p50": 100.0, "p95": 300.0, "max": 300.0},
            )
        ],
        interpretation_limits=["내부 synthetic 평가임"],
    )


def _question_batch() -> CoverageQuestionBatch:
    candidates = [
        CoverageQuestionCandidate.model_construct(
            id=f"question-{index}",
            source_case_id="source-1",
            cell=None,
            axis=axis,
            question=f"질문 {index}",
            hard_literals=[],
            validation=MutationValidation(
                passed=True,
                checks={"meaning": True},
                violations=[],
            ),
        )
        for index, axis in enumerate(
            [
                MutationAxis.SEMANTIC_FORMAL,
                MutationAxis.SEMANTIC_COLLOQUIAL,
                MutationAxis.SEMANTIC_TELEGRAPHIC,
            ],
            start=1,
        )
    ]
    return CoverageQuestionBatch.model_construct(
        schema_version="1.0",
        batch_id="questions",
        generated_at_utc="2026-08-08T00:00:00+00:00",
        status="internal_synthetic_not_blind",
        protocol_id="coverage-guided-question-v1",
        screen_version="coverage-question-screen-v1",
        plan_suite_id="coverage-guided-plan-v1",
        plan_suite_semantic_sha256=_HASH,
        generator="local_test",
        model="qwen3-local-test",
        axes=[
            MutationAxis.SEMANTIC_FORMAL,
            MutationAxis.SEMANTIC_COLLOQUIAL,
            MutationAxis.SEMANTIC_TELEGRAPHIC,
        ],
        selected_source_count=1,
        requested_count=3,
        generated_count=3,
        accepted_count=3,
        rejected_count=0,
        generation_failure_count=0,
        candidates=candidates,
        generation_failures=[],
        interpretation_limits=["test"],
    )


def test_coverage_report_renders_generation_ablation_and_limits() -> None:
    batch = _question_batch()
    rendered = render_coverage_experiment_markdown(
        _report(source_hash=_accepted_source_sha256(batch)),
        question_batch=batch,
    )

    assert "기계 선별 통과" in rendered
    assert "2/3 (66.7%)" in rendered
    assert "구제" in rendered
    assert "퇴행" in rendered
    assert "+300.0ms" in rendered
    assert "search_constraint" in rendered
    assert "사람이 확인할 변화 문항" in rendered
    assert "source_plan_cluster" in rendered
    assert "질문 2" in rendered
    assert "내부 synthetic 평가임" in rendered


def test_coverage_report_rejects_mismatched_questions() -> None:
    with pytest.raises(ValueError, match="questions differ"):
        render_coverage_experiment_markdown(
            _report(source_hash="b" * 64),
            question_batch=_question_batch(),
        )


def test_coverage_report_rejects_non_positive_top_changes() -> None:
    with pytest.raises(ValueError, match="positive"):
        render_coverage_experiment_markdown(_report(), top_changes=0)


def test_coverage_report_rejects_non_positive_review_examples() -> None:
    with pytest.raises(ValueError, match="review_examples"):
        render_coverage_experiment_markdown(_report(), review_examples=0)
