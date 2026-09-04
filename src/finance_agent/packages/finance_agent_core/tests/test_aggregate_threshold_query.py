from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_agent_core.agent.compound_query import (
    AggregateThresholdUnavailableError,
    TypedCompoundQueryEngine,
    _compound_plan_sha256,
    _SourceRecord,
)
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    ConstraintOperator,
    Intent,
    ProductFamily,
    SortDirection,
)


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


def _record(
    product_id: str,
    *,
    family: ProductFamily,
    values: dict[str, str | int | bool | None],
    source_row: int,
) -> _SourceRecord:
    return _SourceRecord(
        product_id=product_id,
        product_name=f"가상상품-{product_id}",
        ticker=product_id if family is ProductFamily.BOND else None,
        source_id="SYNTHETIC-AGGREGATE-THRESHOLD",
        source_row=source_row,
        dynamic_as_of=date(2026, 8, 22),
        source_values=values,
    )


def test_mean_threshold_and_final_ranking_are_both_explicit_in_plan() -> None:
    plan = _engine().compile(
        "국내 ETF 가운데 AUM이 전체 평균보다 큰 상품을 AUM 순으로 3개",
        "aggregate-mean",
    )

    assert plan is not None
    assert plan.kind == "aggregate_threshold"
    family = plan.family_plans[0]
    assert family.product_family is ProductFamily.DOMESTIC_ETP
    assert family.aggregate_threshold is not None
    assert family.aggregate_threshold.metric.source_fields == ("du_last_aum",)
    assert family.aggregate_threshold.function is AggregateFunction.AVG
    assert family.aggregate_threshold.operator == "gt"
    assert family.metric.source_fields == ("du_last_aum",)
    assert family.direction is SortDirection.DESC
    assert family.limit == 3


def test_median_threshold_supports_count_before_independent_final_order() -> None:
    plan = _engine().compile(
        "국내채권 중 표면금리가 전체 중앙값보다 높은 종목 3개를 "
        "만기 빠른 순으로",
        "aggregate-median",
    )

    assert plan is not None
    family = plan.family_plans[0]
    assert family.aggregate_threshold is not None
    assert family.aggregate_threshold.metric.source_fields == ("srfc_irt",)
    assert family.aggregate_threshold.function is AggregateFunction.MEDIAN
    assert family.aggregate_threshold.operator == "gt"
    assert family.metric.source_fields == ("remaining_days",)
    assert family.direction is SortDirection.ASC
    assert family.limit == 3


@pytest.mark.parametrize(
    ("question", "family", "threshold_field", "function", "operator", "rank_field"),
    [
        (
            "국내 ETF 중 순자산이 해당 상품군 평균을 웃도는 종목을 "
            "순자산 높은 순 5개",
            ProductFamily.DOMESTIC_ETP,
            "du_last_aum",
            AggregateFunction.AVG,
            "gt",
            "du_last_aum",
        ),
        (
            "공모펀드 가운데 1년 수익률이 평균치 초과인 상품을 AUM 큰 순 4개",
            ProductFamily.FUND,
            "fd_yr1_ern_r",
            AggregateFunction.AVG,
            "gt",
            "fd_nast_suma",
        ),
        (
            "해외 ETF 중 보수율이 중앙값 이하인 상품을 AUM 큰 순 5개",
            ProductFamily.OVERSEAS_ETP,
            "cu_charge_rt",
            AggregateFunction.MEDIAN,
            "lte",
            "du_last_aum",
        ),
        (
            "국내채권 중 표면이율이 중위수 미만인 2개를 잔존기간 짧은 순으로",
            ProductFamily.BOND,
            "srfc_irt",
            AggregateFunction.MEDIAN,
            "lt",
            "remaining_days",
        ),
        (
            "국내 ETF 가운데 일거래대금이 메디안보다 적은 상품을 "
            "AUM 내림차순 6개",
            ProductFamily.DOMESTIC_ETP,
            "du_val_1d",
            AggregateFunction.MEDIAN,
            "lt",
            "du_last_aum",
        ),
    ],
)
def test_aggregate_threshold_grammar_generalizes_across_families_and_wording(
    question: str,
    family: ProductFamily,
    threshold_field: str,
    function: AggregateFunction,
    operator: str,
    rank_field: str,
) -> None:
    engine = _engine()
    plan = engine.compile(question, "aggregate-language-variant")

    assert plan is not None
    family_plan = plan.family_plans[0]
    assert family_plan.product_family is family
    assert family_plan.aggregate_threshold is not None
    assert family_plan.aggregate_threshold.metric.source_fields == (threshold_field,)
    assert family_plan.aggregate_threshold.function is function
    assert family_plan.aggregate_threshold.operator == operator
    assert family_plan.metric.source_fields == (rank_field,)
    engine.database_paths[family] = Path("/tmp/not-read.sqlite")
    assert engine._validate_compound_plan(plan)  # noqa: SLF001


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF 중 AUM 최빈값보다 큰 상품을 AUM 높은 순 3개",
        "국내 ETF 중 인기 점수가 평균보다 큰 상품을 AUM 높은 순 3개",
        "해외 ETF 중 1년 수익률이 평균보다 큰 상품을 AUM 높은 순 3개",
        "국내 ETF 중 AUM 평균보다 큰 상품 3개",
        "국내 ETF와 공모펀드 중 AUM 평균보다 큰 상품을 AUM 높은 순 3개",
        "국내 ETF 중 AUM 평균보다 큰 상품을 만기 빠른 순 3개",
    ],
)
def test_unknown_incomplete_or_cross_family_aggregate_threshold_stays_uncompiled(
    question: str,
) -> None:
    assert _engine().compile(question, "aggregate-reject") is None


def test_mean_execution_excludes_zero_and_missing_then_filters_and_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    records = tuple(
        _record(
            f"ETF{index}",
            family=ProductFamily.DOMESTIC_ETP,
            values={"pd_grp_no": "ETF", "du_last_aum": value},
            source_row=index,
        )
        for index, value in enumerate(("100", "200", "0", None, "400"), start=1)
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(
        "국내 ETF 가운데 AUM이 전체 평균보다 큰 상품을 AUM 순으로 3개",
        "aggregate-mean-execute",
    )
    assert plan is not None

    execution = engine.execute(plan)

    family = execution.families[0]
    threshold = family.aggregate_threshold_evidence
    assert threshold is not None
    assert threshold.valid_count == 3
    assert threshold.value == Decimal(700) / Decimal(3)
    assert [record.product_id for record in family.product_records] == ["ETF5"]
    assert family.metric_values == (Decimal(400),)
    constraints = [
        item
        for item in family.query_plan.constraints
        if item.field == "aum" and item.operator is ConstraintOperator.GT
    ]
    assert len(constraints) == 1
    assert Decimal(str(constraints[0].value)) == Decimal(str(float(threshold.value)))
    assert "유효값 3개의 평균" in execution.answer
    assert threshold.input_sha256 in execution.answer


def test_median_execution_uses_same_base_cohort_then_final_maturity_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    coupons = ("1", "2", "0", None, "10", "20")
    days = ("500", "400", "300", "200", "90", "30")
    records = tuple(
        _record(
            f"KR00000000{index:02d}",
            family=ProductFamily.BOND,
            values={"srfc_irt": coupon, "remaining_days": remaining},
            source_row=index,
        )
        for index, (coupon, remaining) in enumerate(zip(coupons, days, strict=True), start=1)
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(
        "국내채권 중 표면금리가 전체 중앙값보다 높은 종목 3개를 "
        "만기 빠른 순으로",
        "aggregate-median-execute",
    )
    assert plan is not None

    execution = engine.execute(plan)

    family = execution.families[0]
    threshold = family.aggregate_threshold_evidence
    assert threshold is not None
    assert threshold.valid_count == 4
    assert threshold.value == Decimal(6)
    assert [record.ticker for record in family.product_records] == [
        "KR0000000006",
        "KR0000000005",
    ]
    assert family.metric_values == (Decimal(30), Decimal(90))


def test_empty_usable_base_cohort_raises_controlled_threshold_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    records = (
        _record(
            "ETF1",
            family=ProductFamily.DOMESTIC_ETP,
            values={"pd_grp_no": "ETF", "du_last_aum": "0"},
            source_row=1,
        ),
        _record(
            "ETF2",
            family=ProductFamily.DOMESTIC_ETP,
            values={"pd_grp_no": "ETF", "du_last_aum": None},
            source_row=2,
        ),
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(
        "국내 ETF 가운데 AUM이 전체 평균보다 큰 상품을 AUM 순으로 3개",
        "aggregate-empty",
    )
    assert plan is not None

    with pytest.raises(AggregateThresholdUnavailableError):
        engine.execute(plan)


def test_plan_hash_and_independent_verifier_bind_aggregate_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    records = tuple(
        _record(
            f"ETF{index}",
            family=ProductFamily.DOMESTIC_ETP,
            values={"pd_grp_no": "ETF", "du_last_aum": str(value)},
            source_row=index,
        )
        for index, value in enumerate((100, 200, 400), start=1)
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(
        "국내 ETF 가운데 AUM이 전체 평균보다 큰 상품을 AUM 순으로 3개",
        "aggregate-binding",
    )
    assert plan is not None
    family = plan.family_plans[0]
    assert family.aggregate_threshold is not None
    mutated_threshold = replace(
        family.aggregate_threshold,
        function=AggregateFunction.MEDIAN,
    )
    mutated_plan = replace(
        plan,
        family_plans=(replace(family, aggregate_threshold=mutated_threshold),),
    )
    assert _compound_plan_sha256(mutated_plan) != _compound_plan_sha256(plan)

    # The verifier performs a second complete base-cohort aggregate and rank.
    # Altering even only the aggregate evidence must be detected.
    execution = engine.execute(plan)
    threshold = execution.families[0].aggregate_threshold_evidence
    assert threshold is not None
    altered_family = replace(
        execution.families[0],
        aggregate_threshold_evidence=replace(threshold, value=threshold.value + 1),
    )
    altered = replace(execution, families=(altered_family,))

    # Build a genuine receipt without depending on Router semantics, then
    # compare the precomputed result through the same public verifier path.
    from finance_agent_core.contracts.routing import (
        InteractionIntent,
        MinimalQueryDraft,
        RouteDecision,
        RouteDisposition,
    )

    decision = RouteDecision(
        draft=MinimalQueryDraft(
            request_id=plan.question_id,
            question=plan.question,
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP],
            product_mentions=[],
        ),
        disposition=RouteDisposition.EXECUTE,
        reason_code="test_execute",
        reason="test",
        query_plan_intent=Intent.SEARCH,
        capability_matrix_version="1.0",
    )
    engine.database_paths[ProductFamily.DOMESTIC_ETP] = Path("/tmp/not-read.sqlite")
    _authorized, receipt = engine.authorize_decision(decision, plan)
    assert receipt is not None
    with pytest.raises(ValueError, match="independent verification"):
        engine.verify_authorized_execution(altered, receipt)
