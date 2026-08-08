from __future__ import annotations

import pytest

from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    NullPlacement,
    ProductFamily,
    QueryPlan,
    Ranking,
    SortDirection,
    Unit,
)
from finance_agent_core.evaluation.coverage_ablation import (
    _apply_holm_correction,
    _mcnemar_exact_p_value,
    _paired_bootstrap_ci95,
    _wilson_ci95,
    compare_coverage_profiles,
)
from finance_agent_core.evaluation.coverage_analysis import plan_delta_codes
from finance_agent_core.evaluation.coverage_plan import (
    CoverageCell,
    CoverageCellKind,
    CoverageOutcome,
    CoveragePlanCase,
    CoveragePlanSuite,
    CoveragePlanSummary,
    coverage_plan_suite_semantic_sha256,
    render_canonical_question,
    rerender_coverage_plan_suite,
)
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionRunner,
    merge_coverage_question_run_reports,
)
from finance_agent_core.evaluation.coverage_questions import (
    _boolean_value_present,
    coverage_question_batch_semantic_sha256,
    generate_coverage_question_batch,
    merge_coverage_question_batches,
    validate_coverage_question,
)
from finance_agent_core.evaluation.coverage_runner import CoverageRunner
from finance_agent_core.evaluation.metamorphic import (
    GeneratedMutation,
    MutationAxis,
    MutationValidation,
)
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderCallSnapshot,
    ProviderTelemetry,
)
from finance_agent_core.evaluation.semantic_roundtrip import build_semantic_plan_spec

_HASH = "a" * 64


def _search_plan() -> QueryPlan:
    family = ProductFamily.OVERSEAS_ETP
    projection = list(
        dict.fromkeys(
            [
                *SEARCH_PROJECTION_BY_FAMILY[family.value],
                "product_type",
                "sellable",
                "investment_region",
            ]
        )
    )
    return QueryPlan(
        schema_version="1.0",
        question_id="coverage-guided-plan-v1-0001",
        intent=Intent.SEARCH,
        product_families=[family],
        constraints=[
            Constraint(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="sellable",
                operator=ConstraintOperator.EQ,
                value=True,
                unit=Unit.BOOLEAN,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="investment_region",
                operator=ConstraintOperator.EQ,
                value="United States of America",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            ),
            Constraint(
                field="total_expense_ratio_pct",
                operator=ConstraintOperator.LTE,
                value=0.1,
                unit=Unit.PCT_POINT,
                strength=ConstraintStrength.LOCKED,
            ),
        ],
        ranking=[Ranking(field="aum", direction=SortDirection.DESC, nulls=NullPlacement.LAST)],
        projection=projection,
        limit=3,
        intent_payload=IntentPayload(
            comparison_fields=[],
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )


def _case() -> CoveragePlanCase:
    plan = _search_plan()
    return CoveragePlanCase(
        id=plan.question_id,
        cell=CoverageCell(
            key="overseas_etp:search_constraint:total_expense_ratio_pct:lte",
            product_family=ProductFamily.OVERSEAS_ETP,
            intent=Intent.SEARCH,
            kind=CoverageCellKind.SEARCH_CONSTRAINT,
            field="total_expense_ratio_pct",
            operator=ConstraintOperator.LTE,
        ),
        canonical_question=render_canonical_question(plan),
        plan=plan,
        outcome=CoverageOutcome(
            candidate_count=3,
            returned_product_ids=["AMX:AAA", "AMX:BBB", "AMX:CCC"],
            product_evidence_count=3,
            comparison_evidence_count=0,
            aggregate_evidence_count=0,
            query_plan_semantic_sha256=_HASH,
            evidence_semantic_sha256=_HASH,
            system_semantic_sha256=_HASH,
            source_dataset="overseas_etp",
            source_snapshot_date="2026-07-11",
            latency_ms=1.0,
        ),
    )


def _suite() -> CoveragePlanSuite:
    case = _case()
    return CoveragePlanSuite(
        generated_at_utc="2026-08-08T00:00:00+00:00",
        registry_schema_version="1.3",
        registry_sha256=_HASH,
        database_sha256_by_family={family.value: _HASH for family in ProductFamily},
        selection_contract={"search": "test"},
        summary=CoveragePlanSummary(
            attempted_cells=1,
            executable_cases=1,
            excluded_cells=0,
            execution_rate=1.0,
            by_family={"overseas_etp": 1},
            by_kind={"search_constraint": 1},
            by_operator={"lte": 1},
            by_direction={},
            by_function={},
            exclusion_reasons={},
        ),
        cases=[case],
        exclusions=[],
        interpretation_limits=["unit test"],
    )


def _two_case_suite() -> CoveragePlanSuite:
    first = _case()
    second_plan = first.plan.model_copy(update={"question_id": "coverage-guided-plan-v1-0002"})
    second = first.model_copy(
        update={
            "id": "coverage-guided-plan-v1-0002",
            "cell": first.cell.model_copy(
                update={"key": "overseas_etp:search_constraint:total_expense_ratio_pct:lte:second"}
            ),
            "plan": second_plan,
        }
    )
    return CoveragePlanSuite(
        generated_at_utc="2026-08-08T00:00:00+00:00",
        registry_schema_version="1.3",
        registry_sha256=_HASH,
        database_sha256_by_family={family.value: _HASH for family in ProductFamily},
        selection_contract={"search": "test"},
        summary=CoveragePlanSummary(
            attempted_cells=2,
            executable_cases=2,
            excluded_cells=0,
            execution_rate=1.0,
            by_family={"overseas_etp": 2},
            by_kind={"search_constraint": 2},
            by_operator={"lte": 2},
            by_direction={},
            by_function={},
            exclusion_reasons={},
        ),
        cases=[first, second],
        exclusions=[],
        interpretation_limits=["unit test"],
    )


class _QuestionProvider:
    @property
    def provider_name(self):
        return "expected"

    @property
    def model_name(self):
        return None

    def generate_questions(self, spec, axes):
        questions = {
            MutationAxis.SEMANTIC_FORMAL: (
                "판매 가능한 해외 ETF 중 투자 지역이 미국이고 총보수율이 0.1% 이하인 "
                "상품을 AUM 큰 순으로 3개 찾아주세요"
            ),
            MutationAxis.SEMANTIC_COLLOQUIAL: (
                "해외 ETF에서 판매 가능한 것만 보고 투자 지역 미국, 총보수율 0.1% 이하로 "
                "AUM 큰 순 3개 찾아줘"
            ),
            MutationAxis.SEMANTIC_TELEGRAPHIC: (
                "해외 ETF 판매 가능 투자 지역 미국 총보수율 0.1% 이하 AUM 큰 순 3개 조회"
            ),
        }
        return [GeneratedMutation(axis=axis, question=questions[axis]) for axis in axes]


class _NoCallService:
    def answer(self, question: str, request_id: str):
        raise AssertionError("rejected coverage questions must not execute")


def test_coverage_question_validator_requires_all_values_and_fields() -> None:
    case = _case()
    spec = build_semantic_plan_spec(case.plan)
    valid = _QuestionProvider().generate_questions(spec, [MutationAxis.SEMANTIC_FORMAL])[0]
    accepted = validate_coverage_question(
        valid.question,
        case,
        spec,
        source_questions=[case.canonical_question],
    )
    missing_value = validate_coverage_question(
        valid.question.replace("0.1%", "낮은"),
        case,
        spec,
        source_questions=[case.canonical_question],
    )
    assert accepted.passed
    assert not missing_value.passed
    assert "all_constraint_values_present" in missing_value.violations


def test_generate_coverage_question_batch_is_counted_and_hash_stable() -> None:
    suite = _suite()
    batch = generate_coverage_question_batch(
        _QuestionProvider(),
        suite,
        generated_at_utc="2026-08-08T01:00:00+00:00",
    )
    regenerated = batch.model_copy(update={"generated_at_utc": "2026-08-09T01:00:00+00:00"})
    assert batch.requested_count == 3
    assert batch.generated_count == 3
    assert batch.accepted_count == 3
    assert batch.rejected_count == 0
    assert batch.generation_failure_count == 0
    assert coverage_question_batch_semantic_sha256(
        batch
    ) == coverage_question_batch_semantic_sha256(regenerated)


def test_rerender_preserves_plan_outcome_and_updates_semantic_hash() -> None:
    suite = _suite()
    old_hash = coverage_plan_suite_semantic_sha256(suite)
    rerendered = rerender_coverage_plan_suite(
        suite,
        generated_at_utc="2026-08-08T02:00:00+00:00",
    )
    assert rerendered.cases[0].plan == suite.cases[0].plan
    assert rerendered.cases[0].outcome == suite.cases[0].outcome
    assert rerendered.cases[0].canonical_question == render_canonical_question(suite.cases[0].plan)
    assert coverage_plan_suite_semantic_sha256(rerendered) != old_hash


def test_coverage_question_runner_does_not_execute_rejected_candidates() -> None:
    suite = _suite()
    generated = generate_coverage_question_batch(_QuestionProvider(), suite)
    rejected_candidates = [
        candidate.model_copy(
            update={
                "validation": MutationValidation(
                    checks={"forced_rejection": False},
                    violations=["forced_rejection"],
                    passed=False,
                )
            }
        )
        for candidate in generated.candidates
    ]
    batch = generated.model_copy(
        update={
            "accepted_count": 0,
            "rejected_count": 3,
            "candidates": rejected_candidates,
        }
    )
    service = _NoCallService()
    report = CoverageQuestionRunner(
        suite=suite,
        batch=batch,
        services={family: service for family in ProductFamily},
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run(generated_at_utc="2026-08-08T03:00:00+00:00")
    assert report.summary.accepted == 0
    assert report.summary.rejected == 3
    assert report.summary.executed == 0
    assert report.summary.agent_strict_accuracy is None
    assert report.summary.first_failure_stages == {"mutation_validation": 3}


def test_merge_coverage_question_batches_is_order_stable() -> None:
    suite = _two_case_suite()
    first = generate_coverage_question_batch(
        _QuestionProvider(),
        suite,
        offset=0,
        limit=1,
        generated_at_utc="2026-08-08T01:00:00+00:00",
    )
    second = generate_coverage_question_batch(
        _QuestionProvider(),
        suite,
        offset=1,
        limit=1,
        generated_at_utc="2026-08-08T02:00:00+00:00",
    )
    forward = merge_coverage_question_batches(
        [first, second],
        generated_at_utc="2026-08-08T03:00:00+00:00",
    )
    reverse = merge_coverage_question_batches(
        [second, first],
        generated_at_utc="2026-08-08T03:00:00+00:00",
    )

    assert forward.selected_source_count == 2
    assert forward.requested_count == 6
    assert forward.generated_count == 6
    assert [item.source_case_id for item in forward.candidates] == [
        "coverage-guided-plan-v1-0001",
        "coverage-guided-plan-v1-0001",
        "coverage-guided-plan-v1-0001",
        "coverage-guided-plan-v1-0002",
        "coverage-guided-plan-v1-0002",
        "coverage-guided-plan-v1-0002",
    ]
    assert coverage_question_batch_semantic_sha256(
        forward
    ) == coverage_question_batch_semantic_sha256(reverse)


def test_merge_coverage_question_batches_rejects_overlapping_sources() -> None:
    suite = _suite()
    batch = generate_coverage_question_batch(_QuestionProvider(), suite)

    with pytest.raises(ValueError, match="overlap source cases"):
        merge_coverage_question_batches([batch, batch])


def test_merge_coverage_question_batches_rejects_generator_mismatch() -> None:
    suite = _two_case_suite()
    first = generate_coverage_question_batch(_QuestionProvider(), suite, offset=0, limit=1)
    second = generate_coverage_question_batch(
        _QuestionProvider(), suite, offset=1, limit=1
    ).model_copy(update={"generator": "local_test", "model": "different-model"})

    with pytest.raises(ValueError, match="generator differs"):
        merge_coverage_question_batches([first, second])


def test_plan_delta_uses_the_same_semantic_normalization_as_the_runner() -> None:
    expected = _search_plan()
    implicit_etp_scope = Constraint(
        field="product_type",
        operator=ConstraintOperator.IN,
        value=["ETF", "ETN"],
        unit=Unit.CODE,
        strength=ConstraintStrength.LOCKED,
    )
    equivalent = expected.model_copy(
        update={"constraints": [*expected.constraints, implicit_etp_scope]}
    )
    changed_limit = expected.model_copy(update={"limit": expected.limit + 1})

    assert plan_delta_codes(expected, equivalent) == []
    assert plan_delta_codes(expected, changed_limit) == ["limit_changed"]


def test_coverage_boolean_screen_scopes_negation_to_the_same_field() -> None:
    sellable = Constraint(
        field="sellable",
        operator=ConstraintOperator.EQ,
        value=True,
        unit=Unit.BOOLEAN,
        strength=ConstraintStrength.LOCKED,
    )
    trading_active = Constraint(
        field="trading_suspended",
        operator=ConstraintOperator.EQ,
        value=False,
        unit=Unit.BOOLEAN,
        strength=ConstraintStrength.LOCKED,
    )
    trading_suspended = trading_active.model_copy(update={"value": True})

    assert _boolean_value_present(
        "판매 가능한 해외 ETF 중 특정 운용사는 제외해줘",
        sellable,
    )
    assert _boolean_value_present("거래 중지 아닌 국내 ETF를 찾아줘", trading_active)
    assert not _boolean_value_present(
        "거래 중지 아닌 국내 ETF를 찾아줘",
        trading_suspended,
    )


def test_merge_coverage_question_run_reports_recomputes_campaign_summary() -> None:
    suite = _two_case_suite()
    first_batch = generate_coverage_question_batch(_QuestionProvider(), suite, offset=0, limit=1)
    second_batch = generate_coverage_question_batch(_QuestionProvider(), suite, offset=1, limit=1)
    services = {family: _NoCallService() for family in ProductFamily}
    first_report = CoverageQuestionRunner(
        suite=suite,
        batch=first_batch,
        services=services,
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run(generated_at_utc="2026-08-08T04:00:00+00:00")
    second_report = CoverageQuestionRunner(
        suite=suite,
        batch=second_batch,
        services=services,
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run(generated_at_utc="2026-08-08T05:00:00+00:00")

    campaign = merge_coverage_question_run_reports(
        suite=suite,
        batches=[second_batch, first_batch],
        reports=[second_report, first_report],
        generated_at_utc="2026-08-08T06:00:00+00:00",
    )

    assert len(campaign.shards) == 2
    assert campaign.summary.requested == 6
    assert campaign.summary.generated == 6
    assert campaign.summary.accepted == 6
    assert campaign.summary.executed == 6
    assert campaign.summary.passed == 0
    assert campaign.summary.first_failure_stages == {"routing": 6}
    assert [item.candidate.source_case_id for item in campaign.variants[:3]] == [
        "coverage-guided-plan-v1-0001"
    ] * 3


def test_merge_coverage_question_run_reports_rejects_wrong_batch_pair() -> None:
    suite = _two_case_suite()
    first_batch = generate_coverage_question_batch(_QuestionProvider(), suite, offset=0, limit=1)
    second_batch = generate_coverage_question_batch(_QuestionProvider(), suite, offset=1, limit=1)
    report = CoverageQuestionRunner(
        suite=suite,
        batch=first_batch,
        services={family: _NoCallService() for family in ProductFamily},
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run()

    with pytest.raises(ValueError, match="question batch SHA-256 differs"):
        merge_coverage_question_run_reports(
            suite=suite,
            batches=[second_batch],
            reports=[report],
        )


def test_compare_coverage_profiles_counts_rescues_and_call_cost() -> None:
    suite = _suite()
    baseline = CoverageRunner(
        suite=suite,
        services={family: _NoCallService() for family in ProductFamily},
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run(generated_at_utc="2026-08-08T07:00:00+00:00")
    improved_checks = {name: True for name in baseline.cases[0].checks}
    improved_case = baseline.cases[0].model_copy(
        update={
            "passed": True,
            "checks": improved_checks,
            "violations": [],
            "first_failure_stage": None,
        }
    )
    candidate = baseline.model_copy(
        update={
            "agent_profile": "local_test_grounded_plan_only",
            "agent_model": "qwen3-local-test",
            "provider_calls": ProviderCallSnapshot(
                query_plan_calls=1,
                query_plan_errors=0,
                query_plan_latency_ms=25.0,
                answer_calls=0,
                answer_errors=0,
                answer_latency_ms=0.0,
            ),
            "cases": [improved_case],
        }
    )

    comparison = compare_coverage_profiles(
        {"deterministic": baseline, "qwen_plan": candidate},
        generated_at_utc="2026-08-08T08:00:00+00:00",
    )
    delta = comparison.pairwise_deltas[0]

    assert comparison.baseline_label == "deterministic"
    assert delta.rescued == 1
    assert delta.regressed == 0
    assert delta.plan_rescued == 1
    assert delta.evidence_rescued == 1
    assert delta.provider_call_delta.query_plan_calls == 1
    assert delta.stage_transitions == {"routing->pass": 1}
    assert delta.strict_accuracy_delta_ci95 == [1.0, 1.0]
    assert delta.zero_strict_regression
    assert comparison.profiles[0].strict_accuracy_ci95[0] == 0.0
    assert comparison.profiles[1].strict_accuracy_ci95[1] == 1.0
    assert [item.model_dump() for item in delta.breakdowns["product_family"]] == [
        {
            "value": "overseas_etp",
            "total": 1,
            "baseline_passed": 0,
            "candidate_passed": 1,
            "baseline_accuracy": 0.0,
            "candidate_accuracy": 1.0,
            "accuracy_delta": 1.0,
            "rescued": 1,
            "regressed": 0,
            "net_rescued": 1,
        }
    ]
    assert delta.breakdowns["axis"] == []


def test_coverage_ablation_statistics_are_deterministic_and_exact() -> None:
    assert _wilson_ci95(50, 100) == [0.403832, 0.596168]
    assert _mcnemar_exact_p_value(10, 0) == 0.001953125
    assert _mcnemar_exact_p_value(0, 0) == 1.0

    differences = [1] * 20 + [0] * 80
    first = _paired_bootstrap_ci95(differences, seed="fixed")
    second = _paired_bootstrap_ci95(differences, seed="fixed")
    assert first == second
    assert 0 < first[0] <= 0.2 <= first[1]


def test_coverage_ablation_holm_correction_is_monotonic() -> None:
    suite = _suite()
    baseline = CoverageRunner(
        suite=suite,
        services={family: _NoCallService() for family in ProductFamily},
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run()
    comparison = compare_coverage_profiles(
        {
            "baseline": baseline,
            "candidate": baseline.model_copy(update={"agent_profile": "candidate"}),
        }
    )
    template = comparison.pairwise_deltas[0]
    corrected = _apply_holm_correction(
        [
            template.model_copy(
                update={
                    "candidate_label": "a",
                    "mcnemar_exact_p_value": 0.01,
                    "strict_accuracy_delta": 0.1,
                }
            ),
            template.model_copy(
                update={
                    "candidate_label": "b",
                    "mcnemar_exact_p_value": 0.03,
                    "strict_accuracy_delta": 0.1,
                }
            ),
            template.model_copy(
                update={
                    "candidate_label": "c",
                    "mcnemar_exact_p_value": 0.04,
                    "strict_accuracy_delta": 0.1,
                }
            ),
        ]
    )

    assert [item.holm_adjusted_p_value for item in corrected] == [0.03, 0.06, 0.06]
    assert [item.statistically_significant_after_holm for item in corrected] == [
        True,
        False,
        False,
    ]


def test_compare_coverage_profiles_rejects_different_questions() -> None:
    suite = _suite()
    baseline = CoverageRunner(
        suite=suite,
        services={family: _NoCallService() for family in ProductFamily},
        agent_profile="expected",
        agent_model=None,
        telemetry=ProviderTelemetry(),
    ).run()
    changed_case = baseline.cases[0].model_copy(update={"question": "다른 질문"})
    changed = baseline.model_copy(update={"cases": [changed_case]})

    with pytest.raises(ValueError, match="source questions differ"):
        compare_coverage_profiles({"baseline": baseline, "changed": changed})
