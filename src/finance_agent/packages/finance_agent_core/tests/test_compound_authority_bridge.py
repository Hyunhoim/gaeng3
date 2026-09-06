from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from finance_agent_core.agent.backend_adapter import execute_answer_request
from finance_agent_core.agent.compound_query import (
    CompoundExecution,
    NamedComparisonIncompleteError,
    TypedCompoundQueryEngine,
)
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentExecutionClause,
    SemanticFrameDirection,
    SemanticFrameOperation,
    SemanticFrameOperator,
    SemanticFrameStatus,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)


class _FixedSemanticFrameProvider:
    provider_name = "fixed-semantic-frame"
    model_name = "fixed-semantic-frame"

    def __init__(self, draft: IntentAnalysisDraft) -> None:
        self.draft = draft

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft:
        assert question
        assert question_id
        return self.draft


def _compile_without_reading(question: str, request_id: str):
    engine = TypedCompoundQueryEngine(
        {
            ProductFamily.BOND: Path("/approved/bond.sqlite3"),
            ProductFamily.DOMESTIC_ETP: Path("/approved/domestic.sqlite3"),
            ProductFamily.OVERSEAS_ETP: Path("/approved/overseas.sqlite3"),
            ProductFamily.FUND: Path("/approved/fund.sqlite3"),
        },
        record_cache=object(),  # type: ignore[arg-type]
    )
    trace = IntentRouter().route_with_planning(question, request_id)
    plan = engine.compile(question, request_id)
    assert plan is not None
    return engine, trace, plan


def test_semantic_coverage_gap_can_open_only_a_fully_typed_derived_metric() -> None:
    question = (
        "장내 국내채권 중 총발행금액 대비 발행잔액 비율이 "
        "큰 순으로 네 개만 보여주세요"
    )
    engine, trace, plan = _compile_without_reading(question, "derived-authority")

    decision, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )

    assert trace.route_decision.reason_code == "semantic_coverage_incomplete"
    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.reason_code == "typed_compound_plan_authorized"
    assert receipt is not None
    assert receipt.covered_unresolved_spans == ("큰",)
    assert set(receipt.validated_field_ids) == {
        "bond_market",
        "issue_amount",
        "total_issue_amount",
    }


def test_subjective_semantics_and_safety_terminals_never_gain_compound_authority() -> None:
    base = (
        "장내 국내채권 중 총발행금액 대비 발행잔액 비율이 "
        "큰 순으로 네 개만 보여주세요"
    )
    engine, _trace, plan = _compile_without_reading(base, "terminal-authority")
    subjective = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="terminal-authority",
            question=base,
            intent=InteractionIntent.CLARIFY,
            product_families=[ProductFamily.BOND],
            product_mentions=[],
            requested_limit=4,
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="subjective_condition",
        reason="좋다는 기준이 정의되지 않음",
        query_plan_intent=None,
        capability_matrix_version="1.0",
    )
    blocked, receipt = engine.authorize_decision(
        subjective,
        plan,
        unresolved_spans=("좋은",),
    )
    assert blocked == subjective
    assert receipt is None

    unsafe_question = base + " 그리고 실제 주문도 넣어줘"
    unsafe_engine = TypedCompoundQueryEngine(
        {
            ProductFamily.BOND: Path("/approved/bond.sqlite3"),
            ProductFamily.DOMESTIC_ETP: Path("/approved/domestic.sqlite3"),
            ProductFamily.OVERSEAS_ETP: Path("/approved/overseas.sqlite3"),
            ProductFamily.FUND: Path("/approved/fund.sqlite3"),
        },
        record_cache=object(),  # type: ignore[arg-type]
    )
    unsafe_trace = IntentRouter().route_with_planning(
        unsafe_question,
        "safety-terminal",
    )
    assert unsafe_engine.compile(unsafe_question, "safety-terminal") is None
    assert unsafe_trace.route_decision.reason_code.startswith("safety_")
    assert unsafe_trace.route_decision.disposition is RouteDisposition.UNSUPPORTED


def test_multi_family_direction_is_proven_only_inside_its_own_clause() -> None:
    question = (
        "국내 ETF에서 일거래대금 높은 순으로 두 개, 해외 ETF에서 AUM 낮은 "
        "순으로 두 개를 상품군별로 보여줘"
    )
    engine, trace, plan = _compile_without_reading(question, "clause-local-direction")
    clarify = RouteDecision(
        draft=trace.route_decision.draft.model_copy(
            update={"intent": InteractionIntent.CLARIFY}
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="semantic_coverage_incomplete",
        reason="두 절의 미해결 방향 표현",
        query_plan_intent=None,
        capability_matrix_version=trace.route_decision.capability_matrix_version,
    )

    authorized, receipt = engine.authorize_decision(
        clarify,
        plan,
        unresolved_spans=("높은",),
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == ("높은",)


def test_cross_family_gap_does_not_erase_an_unproven_residual() -> None:
    question = (
        "국내 ETF에서 일거래대금 높은 순으로 두 개, 해외 ETF에서 AUM 낮은 "
        "순으로 두 개를 상품군별로 보여주고 인기도 반영해줘"
    )
    engine, trace, plan = _compile_without_reading(question, "cross-gap-residual")
    clarify = RouteDecision(
        draft=trace.route_decision.draft,
        disposition=RouteDisposition.CLARIFY,
        reason_code="ambiguous_product_family",
        reason="복수 상품군",
        query_plan_intent=None,
        capability_matrix_version=trace.route_decision.capability_matrix_version,
    )

    blocked, receipt = engine.authorize_decision(
        clarify,
        plan,
        unresolved_spans=("인기",),
    )

    assert blocked is clarify
    assert receipt is None


def test_caller_mutated_plan_is_rejected_before_authorization() -> None:
    question = (
        "국내 ETF에서 일거래대금 700000 이상이고 해외 ETF에서 일거래대금 "
        "900 이상인 상품을 각 상품군에서 일거래대금 높은 순으로 3개씩 알려줘"
    )
    engine, trace, plan = _compile_without_reading(question, "mutated-plan")
    mutated_family = replace(plan.family_plans[0], limit=100)
    mutated = replace(plan, family_plans=(mutated_family, *plan.family_plans[1:]))

    with pytest.raises(ValueError, match="not reproducible"):
        engine.authorize_decision(
            trace.route_decision,
            mutated,
            unresolved_spans=trace.planning_decision.unresolved_spans,
        )


def test_forged_compound_receipt_is_rejected_before_any_database_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "장내 국내채권 중 총발행금액 대비 발행잔액 비율이 "
        "큰 순으로 네 개만 보여주세요"
    )
    engine, trace, plan = _compile_without_reading(question, "forged-receipt")
    _decision, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )
    assert receipt is not None
    forged = replace(receipt, authority_mac="0" * 64)

    def fail_if_read(*_args, **_kwargs):
        raise AssertionError("a forged receipt must fail before SQLite")

    monkeypatch.setattr(engine, "_load_records", fail_if_read)
    with pytest.raises(ValueError, match="MAC is invalid"):
        engine.execute_authorized(plan, forged)


def test_compound_public_query_plan_canonicalizes_code_mapped_risk_grade() -> None:
    question = (
        "미국 주식형 국내 ETF에서는 순자산 큰 순으로 두 개, 위험등급 2등급인 "
        "공모펀드에서는 1년 수익률 큰 순으로 두 개를 각각 보여 주세요."
    )
    engine = TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]
    plan = engine.compile(question, "canonical-risk")
    assert plan is not None
    fund = next(
        item for item in plan.family_plans if item.product_family is ProductFamily.FUND
    )

    query_plan = engine._query_plan(plan, fund)

    risk = next(item for item in query_plan.constraints if item.field == "risk_level")
    assert risk.value == "높은위험(2등급)"


def test_unknown_compound_enum_is_rejected_during_authorization_preflight() -> None:
    question = (
        "투자지역이 존재하지않는지역인 해외 ETF에서는 AUM 큰 순으로 두 개, "
        "국내 ETF에서는 AUM 큰 순으로 두 개를 각각 보여줘"
    )
    engine = TypedCompoundQueryEngine(
        {
            family: Path(f"/approved/{family.value}.sqlite3")
            for family in ProductFamily
        },
        record_cache=object(),  # type: ignore[arg-type]
    )
    plan = engine.compile(question, "unknown-compound-enum")
    assert plan is not None

    with pytest.raises(ValueError, match="unknown canonical value"):
        engine._validate_compound_plan(plan)


def test_identity_collision_across_bound_families_never_selects_first_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = TypedCompoundQueryEngine(
        {
            ProductFamily.DOMESTIC_ETP: Path("/approved/domestic.sqlite3"),
            ProductFamily.OVERSEAS_ETP: Path("/approved/overseas.sqlite3"),
        },
        record_cache=object(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(engine, "_resolve_identity", lambda _product_id: None)

    assert engine._identity_family("DUPLICATE") is None


def test_independent_post_execution_check_rejects_changed_result(monkeypatch) -> None:
    question = (
        "장내 국내채권 중 총발행금액 대비 발행잔액 비율이 "
        "큰 순으로 네 개만 보여주세요"
    )
    engine, trace, plan = _compile_without_reading(question, "post-verify")
    _decision, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )
    assert receipt is not None
    results = iter(
        (
            CompoundExecution(plan=plan, families=(), answer="first"),
            CompoundExecution(plan=plan, families=(), answer="changed"),
        )
    )
    monkeypatch.setattr(engine, "execute", lambda _plan: next(results))

    with pytest.raises(ValueError, match="independent verification"):
        engine.execute_authorized(plan, receipt)


def test_public_agent_executes_fresh_cross_family_variant_through_receipt(
    sample_database,
    domestic_sample_database,
) -> None:
    overseas_path = sample_database[0]
    domestic_path = domestic_sample_database[0]
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_path,
            ProductFamily.OVERSEAS_ETP: overseas_path,
        }
    )
    question = (
        "국내 ETF에서는 일거래대금 500000 이상, 해외 ETF에서는 일거래대금 "
        "100 이상인 상품을 상품군별로 일거래대금 큰 순서대로 두 개씩 보여주세요"
    )

    result = agent.answer(question, "fresh-cross-family")

    assert result.status == "executed", (
        result.decision.reason_code,
        result.decision.reason,
        result.decision.draft,
    )
    assert result.decision.disposition is RouteDisposition.EXECUTE
    assert result.decision.query_plan_intent is Intent.SEARCH
    assert [item.product_family for item in result.family_searches] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert all(len(item.products) <= 2 for item in result.family_searches)
    for family_result in result.family_searches:
        for product in family_result.products:
            trading_value = next(
                item
                for item in product.fields
                if item.canonical_field == "daily_trading_value"
            )
            assert trading_value.unit == "source_currency_amount"
            assert trading_value.as_of is not None
            assert trading_value.quality.value in {"VALID", "PARTIAL"}


def test_parallel_family_ledger_accepts_only_fully_owned_hclx_clauses(
    sample_database,
    domestic_sample_database,
) -> None:
    domestic_clause = (
        "국내 ETF는 3개월 수익률 0 이상에서 순자산 상위 2개"
    )
    overseas_clause = (
        "해외 ETF는 연간보수율 1 이하에서 순자산 상위 2개"
    )
    question = (
        "두 상품군을 섞지 말고 각각 계산해 주세요. "
        f"{domestic_clause}; {overseas_clause}를 보여주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=index,
                role="ranking",
                text_span=span,
                product_families=[family],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            )
            for index, (span, family) in enumerate(
                (
                    (domestic_clause, ProductFamily.DOMESTIC_ETP),
                    (overseas_clause, ProductFamily.OVERSEAS_ETP),
                ),
                start=1,
            )
        ],
    )
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        },
        intent_analysis_provider=_FixedSemanticFrameProvider(draft),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="parallel-family-hclx-ledger",
            question=question,
        ),
    ).response

    assert response.status is BackendStatus.SUCCESS, response.clarification
    assert [item.product_family for item in response.family_searches] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert all(item.query_plan.limit == 2 for item in response.family_searches)


def test_parallel_family_ledger_never_hides_subjective_hclx_clause(
    sample_database,
    domestic_sample_database,
) -> None:
    domestic_clause = (
        "국내 ETF는 3개월 수익률 0 이상에서 순자산 상위 2개이되 "
        "인기도도 반영"
    )
    overseas_clause = (
        "해외 ETF는 연간보수율 1 이하에서 순자산 상위 2개"
    )
    question = (
        "두 상품군을 섞지 말고 각각 계산해 주세요. "
        f"{domestic_clause}; {overseas_clause}를 보여주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span=domestic_clause,
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            )
        ],
    )
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        },
        intent_analysis_provider=_FixedSemanticFrameProvider(draft),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="parallel-subjective-hclx-ledger",
            question=question,
        ),
    ).response

    assert response.status is BackendStatus.CLARIFICATION
    assert response.query_plan is None
    assert response.products == []


def test_public_agent_executes_multi_family_counts_with_per_family_evidence(
    sample_database,
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )

    result = agent.answer(
        "국내 ETF와 해외 ETF의 상품 수를 상품군별로 알려줘",
        "family-count-e2e",
    )

    assert result.status == "executed", result.decision
    assert result.decision.query_plan_intent is Intent.AGGREGATE
    assert [(item.source_dataset, item.value) for item in result.aggregates] == [
        ("domestic_etp", 6),
        ("overseas_etp", 10),
    ]
    assert [item.query_plan.intent for item in result.family_searches] == [
        Intent.AGGREGATE,
        Intent.AGGREGATE,
    ]
    backend = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="family-count-backend",
            question="국내 ETF와 해외 ETF의 상품 수를 상품군별로 알려줘",
        ),
    )
    assert backend.http_status_code == 200
    assert backend.response.status is BackendStatus.SUCCESS
    assert backend.response.intent is InteractionIntent.AGGREGATE
    assert [(item.source_dataset, item.value) for item in backend.response.aggregates] == [
        ("domestic_etp", 6),
        ("overseas_etp", 10),
    ]


def test_public_agent_executes_complete_two_stage_and_blocks_missing_first_direction(
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]}
    )

    complete = agent.answer(
        "국내 ETF 중 순자산이 큰 다섯 개를 먼저 뽑고 그중 "
        "거래대금이 많은 세 개",
        "two-stage-complete-e2e",
    )
    incomplete = agent.answer(
        "우선 거래대금 기준으로 국내 ETF 5개 후보를 만들고, "
        "이 안에서 보수율이 낮은 3개",
        "two-stage-incomplete-e2e",
    )

    assert complete.status == "executed", complete.decision
    assert complete.query_plan is not None
    assert complete.query_plan.ranking[0].field == "daily_trading_value"
    assert len(complete.products) == 3
    assert incomplete.status == "clarify"
    assert incomplete.decision.reason_code == "compound_plan_required"
    assert incomplete.query_plan is None


def test_server_owned_derived_metric_outranks_advisory_hclx_gap(
    domestic_sample_database,
) -> None:
    """HCX cannot veto a complete formula the positive ledger reproduced."""

    question = (
        "국내 ETF 중 순자산이 큰 다섯 개를 먼저 뽑고 그중 1년 수익률에서 "
        "3개월 수익률을 뺀 차이(%p)가 큰 순으로 세 개를 골라주세요."
    )
    derived_span = "1년 수익률에서 3개월 수익률을 뺀 차이(%p)가 큰 순으로 세 개"
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span=derived_span,
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            )
        ],
    )
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]},
        intent_analysis_provider=_FixedSemanticFrameProvider(draft),
    )

    result = agent.answer(question, "server-owned-derived-hclx-gap")
    plan = agent.compound_query_engine.compile(
        question,
        "server-owned-derived-hclx-gap-proof",
    )

    assert result.status == "executed", result.decision
    assert plan is not None
    assert plan.family_plans[0].metric.kind == "subtract"
    assert plan.family_plans[0].metric.source_fields == ("du_er_1y", "du_er_3m")
    assert plan.family_plans[0].direction.value == "desc"
    assert plan.family_plans[0].limit == 3


def test_public_agent_executes_monthly_turnover_then_one_year_return_shortlist(
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]}
    )
    question = (
        "국내 ETF를 1개월 평균 거래대금이 큰 순서로 30개 먼저 추린 다음, "
        "그 30개 안에서 1년 수익률이 높은 5개를 다시 골라주세요. "
        "수익률 자료가 없는 상품은 30개 후보를 정한 다음에 빼주세요."
    )

    result = agent.answer(question, "two-stage-monthly-turnover-e2e")

    assert result.status == "executed", result.decision
    assert result.decision.reason_code != "compound_plan_required"
    assert result.query_plan is not None
    assert result.query_plan.ranking[0].field == "one_year_return_pct"
    assert len(result.products) <= 5


def test_canonical_anchor_ledger_corrects_name_induced_family_noise(
    domestic_sample_database,
) -> None:
    """An unseen DB name may contain another family word without changing scope."""

    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    unseen_name = "오로라 채권혼합 전략 인덱스"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (unseen_name, unseen_name, anchor.product_id),
        )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    question = (
        f"테스트운용 운용 국내 ETF에서 {unseen_name}({anchor.product_id})를 "
        "기준점으로 삼아 주세요. 이 상품 이상인 3개월 수익률과 이 상품 "
        "이하인 총보수를 동시에 만족하는 다른 상품을 순자산 높은 순으로 "
        "2개 찾아주세요."
    )

    result = agent.answer(question, "canonical-anchor-unseen-name")

    assert result.status == "executed", result.decision
    assert result.decision.draft.product_families == [ProductFamily.DOMESTIC_ETP]
    assert result.query_plan is not None
    assert {
        (item.field, item.operator.value, item.value)
        for item in result.query_plan.constraints
    } >= {
        ("manager", "eq", "테스트운용"),
        ("three_month_return_pct", "gte", 2),
        ("total_expense_ratio_pct", "lte", 0.4),
    }
    assert result.query_plan.ranking[0].field == "aum"
    assert result.query_plan.ranking[0].direction.value == "desc"
    assert result.query_plan.limit == 2
    assert all(item.product_id != anchor.product_id for item in result.products)

    plan = agent.compound_query_engine.compile(
        question,
        "canonical-anchor-noisy-route",
    )
    assert plan is not None
    routed = IntentRouter().route(question, "canonical-anchor-noisy-route")
    noisy_route = routed.model_copy(
        update={
            "draft": routed.draft.model_copy(
                update={
                    "product_families": [
                        ProductFamily.DOMESTIC_ETP,
                        ProductFamily.BOND,
                    ]
                }
            ),
            "reason_code": "cross_family_search_executable",
        }
    )

    corrected, receipt = agent.compound_query_engine.authorize_decision(
        noisy_route,
        plan,
    )

    assert corrected.reason_code == "typed_anchor_relative_authorized"
    assert corrected.draft.product_families == [ProductFamily.DOMESTIC_ETP]
    assert receipt is not None
    assert len(
        [item for item in receipt.canonical_clause_ids if item.startswith("relative:")]
    ) == 2
    assert "direction:desc" in receipt.canonical_clause_ids
    assert "limit:2" in receipt.canonical_clause_ids
    assert any(
        item.startswith("exclude_anchor:") for item in receipt.canonical_clause_ids
    )


def test_inclusive_anchor_thresholds_require_explicit_anchor_exclusion(
    domestic_sample_database,
) -> None:
    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    unseen_name = "노바 멀티에셋 인덱스"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (unseen_name, unseen_name, anchor.product_id),
        )
    engine = TypedCompoundQueryEngine(
        {ProductFamily.DOMESTIC_ETP: database_path},
        record_cache=object(),  # type: ignore[arg-type]
    )
    question = (
        f"국내 ETF에서 {unseen_name}({anchor.product_id})를 기준점으로 삼아 "
        "주세요. 이 상품 이상인 3개월 수익률과 이 상품 이하인 총보수를 "
        "동시에 만족하는 상품을 순자산 높은 순으로 2개 찾아주세요."
    )
    trace = IntentRouter().route_with_planning(question, "missing-anchor-exclusion")
    plan = engine.compile(question, "missing-anchor-exclusion")

    assert plan is not None
    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )

    assert authorized is trace.route_decision
    assert receipt is None


def test_closed_named_set_ledger_ignores_only_db_owned_name_noise(
    domestic_sample_database,
) -> None:
    """Unseen names may contain metric/count/family words without widening scope."""

    database_path, records, _manifest = domestic_sample_database
    names = (
        "오로라 채권혼합 TOP10 인덱스",
        "노바 1개월 성장전략 인덱스",
        "루멘 일거래대금 혁신 인덱스",
    )
    selected = records[:3]
    with sqlite3.connect(database_path) as connection:
        for record, name in zip(selected, names, strict=True):
            connection.execute(
                "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
                "WHERE product_id = ?",
                (name, name, record.product_id),
            )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    identities = ", ".join(
        f"{name}({record.product_id})"
        for name, record in zip(names, selected, strict=True)
    )
    question = (
        f"후보를 이 세 상품으로만 한정할게요: {identities}. "
        "1개월 수익률이 높은 순서로 셋을 나열하고 각 상품의 "
        "일거래대금도 같이 보여주세요."
    )

    result = agent.answer(question, "closed-named-set-unseen")

    assert result.status == "executed", result.decision
    assert result.decision.reason_code == "typed_named_set_authorized"
    assert result.decision.draft.product_families == [ProductFamily.DOMESTIC_ETP]
    assert result.decision.draft.requested_limit == 3
    assert result.query_plan is not None
    assert result.query_plan.ranking[0].field == "one_month_return_pct"
    assert result.query_plan.ranking[0].direction.value == "desc"
    assert result.query_plan.limit == 3
    assert [item.product_id for item in result.products] == [
        selected[1].product_id,
        selected[2].product_id,
        selected[0].product_id,
    ]

    plan = agent.compound_query_engine.compile(question, "closed-named-set-ledger")
    assert plan is not None
    trace = IntentRouter().route_with_planning(question, "closed-named-set-ledger")
    _authorized, receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )
    assert receipt is not None
    assert len(
        [item for item in receipt.canonical_clause_ids if item.startswith("named_product:")]
    ) == 3
    assert "direction:desc" in receipt.canonical_clause_ids
    assert "limit:3" in receipt.canonical_clause_ids
    assert "projection:du_val_1d" in receipt.canonical_clause_ids
    assert "identity_scope:closed" in receipt.canonical_clause_ids


def test_closed_named_set_ledger_outranks_advisory_hclx_family_drift(
    domestic_sample_database,
) -> None:
    """A model may misread words inside names, but cannot widen a DB-bound set."""

    database_path, records, _manifest = domestic_sample_database
    names = (
        "아틀라스 채권혼합 전략 지수",
        "베가 해외성장 전략 지수",
        "시리우스 펀드 혁신 지수",
    )
    selected = records[:3]
    with sqlite3.connect(database_path) as connection:
        for record, name in zip(selected, names, strict=True):
            connection.execute(
                "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
                "WHERE product_id = ?",
                (name, name, record.product_id),
            )
    identity_surfaces = tuple(
        f"{name}({record.product_id})"
        for name, record in zip(names, selected, strict=True)
    )
    question = (
        f"후보를 이 세 상품으로만 한정할게요: {', '.join(identity_surfaces)}. "
        "1개월 수익률이 큰 순서로 셋을 나열하고 각 상품의 "
        "일거래대금도 같이 보여주세요."
    )
    drifted_families = [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.FUND,
    ]
    clauses = [
        IntentExecutionClause(
            sequence=index,
            role="filter",
            text_span=surface,
            product_families=[family],
            semantic_status=SemanticFrameStatus.UNRESOLVED,
        )
        for index, (surface, family) in enumerate(
            zip(identity_surfaces, drifted_families, strict=True),
            start=1,
        )
    ]
    clauses.extend(
        (
            IntentExecutionClause(
                sequence=4,
                role="ranking",
                text_span="1개월 수익률이 큰 순서로 셋을 나열",
                product_families=drifted_families,
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="one_month_return_pct",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="1개월 수익률",
            ),
            IntentExecutionClause(
                sequence=5,
                role="projection",
                text_span="각 상품의 일거래대금도 같이 보여주세요.",
                product_families=drifted_families,
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="daily_trading_value",
                operation=SemanticFrameOperation.PROJECT,
                field_span="일거래대금",
            ),
        )
    )
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: database_path},
        intent_analysis_provider=_FixedSemanticFrameProvider(
            IntentAnalysisDraft(
                intent=InteractionIntent.SEARCH,
                product_families=drifted_families,
                evidence_spans=[],
                needs_clarification=False,
                execution_clauses=clauses,
            )
        ),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="advisory-family-drift", question=question),
    ).response

    assert response.status is BackendStatus.SUCCESS, response.clarification
    assert response.product_families == [ProductFamily.DOMESTIC_ETP]
    assert response.candidate_count == 3
    assert {item.product_id for item in response.products} == {
        item.product_id for item in selected
    }
    assert response.query_plan is not None
    assert response.query_plan.ranking[0].field == "one_month_return_pct"
    assert response.query_plan.ranking[0].direction.value == "desc"
    assert response.query_plan.limit == 3


def test_anchor_ledger_outranks_advisory_unresolved_hclx_clauses(
    domestic_sample_database,
) -> None:
    """A complete server ledger, not HCX restatement quality, owns execution."""

    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    anchor_name = "헤르메스 멀티에셋 기준 지수"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (anchor_name, anchor_name, anchor.product_id),
        )
    anchor_surface = f"{anchor_name}({anchor.product_id})"
    clause_surfaces = (
        ("scope", "테스트운용 운용 국내 ETF"),
        ("filter", anchor_surface),
        ("filter", "이 상품 이상인 3개월 수익률"),
        ("filter", "이 상품 이하인 총보수"),
        ("ranking", "순자산 높은 순으로 2개"),
    )
    question = (
        f"테스트운용 운용 국내 ETF에서 {anchor_surface}를 기준점으로 삼아 "
        "주세요. 이 상품 이상인 3개월 수익률과 이 상품 이하인 총보수를 "
        "동시에 만족하는 다른 상품을 순자산 높은 순으로 2개 찾아주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=index,
                role=role,
                text_span=surface,
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            )
            for index, (role, surface) in enumerate(clause_surfaces, start=1)
        ],
    )
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: database_path},
        intent_analysis_provider=_FixedSemanticFrameProvider(draft),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="advisory-unresolved-anchor", question=question),
    ).response

    assert response.status is BackendStatus.SUCCESS, response.clarification
    assert response.product_families == [ProductFamily.DOMESTIC_ETP]
    assert response.query_plan is not None
    assert {
        (item.field, item.operator.value)
        for item in response.query_plan.constraints
    } >= {
        ("manager", "eq"),
        ("three_month_return_pct", "gte"),
        ("total_expense_ratio_pct", "lte"),
    }
    assert response.query_plan.ranking[0].field == "aum"
    assert response.query_plan.ranking[0].direction.value == "desc"
    assert response.query_plan.limit == 2
    assert all(item.product_id != anchor.product_id for item in response.products)

    proof_request_id = "advisory-unresolved-anchor-proof"
    trace = agent.router.route_with_planning(question, proof_request_id)
    plan = agent.compound_query_engine.compile(question, proof_request_id)
    assert plan is not None
    authorized, receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=tuple(surface for _role, surface in clause_surfaces),
    )
    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == tuple(
        surface for _role, surface in clause_surfaces
    )

    merged_declaration = (
        f"{anchor_surface}를 기준점으로 삼아 주세요. "
        "이 상품 이상인 3개월 수익률"
    )
    merged_authorized, merged_receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(merged_declaration,),
    )
    assert merged_authorized.disposition is RouteDisposition.EXECUTE
    assert merged_receipt is not None
    assert merged_receipt.covered_unresolved_spans == (merged_declaration,)


def test_anchor_ledger_owns_venue_annotation_declaration_and_exact_projection(
    domestic_sample_database,
) -> None:
    """One model clause may wrap a DB identity, but cannot add query meaning."""

    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    anchor_name = "아르테미스 균형전략 기준 지수"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (anchor_name, anchor_name, anchor.product_id),
        )
    anchor_surface = f"{anchor_name}({anchor.product_id}, 장내)"
    declaration = f"{anchor_surface}를 기준점으로 삼아 주세요"
    projection = "기준 상품의 두 수치도 함께 적어주세요."
    question = (
        f"테스트운용 운용 국내 ETF에서 {declaration}. 이 상품 이상인 "
        "3개월 수익률과 이 상품 이하인 총보수를 동시에 만족하는 다른 "
        f"상품을 순자산 낮은 순으로 3개 찾아주세요. {projection}"
    )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    request_id = "anchor-annotation-declaration-projection"
    trace = agent.router.route_with_planning(question, request_id)
    plan = agent.compound_query_engine.compile(question, request_id)

    assert plan is not None
    authorized, receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(declaration, projection),
    )
    coverage = agent.compound_query_engine._anchor_relative_clause_coverage(plan)
    assert coverage is not None, {
        "kind": plan.kind,
        "exclude_product_ids": plan.family_plans[0].exclude_product_ids,
        "relative_filters": plan.family_plans[0].relative_filters,
        "projection_fields": plan.family_plans[0].projection_fields,
        "anchor": agent.compound_query_engine._database_relative_anchor_context(
            question,
            family=ProductFamily.DOMESTIC_ETP,
        ),
    }
    proofs = {
        span: agent.compound_query_engine._canonical_ledger_proves_unresolved_span(
            span,
            plan,
            anchor_clause_coverage=coverage,
            named_set_clause_coverage=None,
        )
        for span in (declaration, projection)
    }

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None, proofs
    assert receipt.covered_unresolved_spans == (declaration, projection)
    assert "projection:du_er_3m" in receipt.canonical_clause_ids
    assert "projection:cu_charge_rt" in receipt.canonical_clause_ids

    noun_projection = "기준 상품의 두 수치"
    noun_authorized, noun_receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(noun_projection,),
    )
    assert noun_authorized.disposition is RouteDisposition.EXECUTE
    assert noun_receipt is not None
    assert noun_receipt.covered_unresolved_spans == (noun_projection,)

    short_declaration = f"{anchor_surface}를 기준으로"
    short_question = question.replace(
        declaration,
        f"{short_declaration} 비교해 볼게요",
        1,
    )
    short_request_id = "anchor-short-declaration"
    short_trace = agent.router.route_with_planning(short_question, short_request_id)
    short_plan = agent.compound_query_engine.compile(
        short_question,
        short_request_id,
    )
    assert short_plan is not None

    short_authorized, short_receipt = agent.compound_query_engine.authorize_decision(
        short_trace.route_decision,
        short_plan,
        unresolved_spans=(short_declaration,),
    )
    short_coverage = agent.compound_query_engine._anchor_relative_clause_coverage(
        short_plan
    )
    short_proof = (
        short_coverage is not None
        and agent.compound_query_engine._canonical_ledger_proves_unresolved_span(
            short_declaration,
            short_plan,
            anchor_clause_coverage=short_coverage,
            named_set_clause_coverage=None,
        )
    )
    assert short_authorized.disposition is RouteDisposition.EXECUTE
    assert short_receipt is not None, {
        "coverage": short_coverage,
        "proof": short_proof,
        "reason": short_trace.route_decision.reason_code,
    }
    assert short_receipt.covered_unresolved_spans == (short_declaration,)


def test_anchor_projection_count_mismatch_remains_fail_closed(
    domestic_sample_database,
) -> None:
    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    anchor_surface = f"{anchor.product_name}({anchor.product_id})"
    projection = "기준 상품의 세 수치도 함께 적어주세요."
    question = (
        f"테스트운용 운용 국내 ETF에서 {anchor_surface}를 기준점으로 삼아 "
        "주세요. 이 상품 이상인 3개월 수익률과 이 상품 이하인 총보수를 "
        "동시에 만족하는 다른 상품을 순자산 낮은 순으로 3개 찾아주세요. "
        f"{projection}"
    )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    request_id = "anchor-projection-count-mismatch"
    trace = agent.router.route_with_planning(question, request_id)
    plan = agent.compound_query_engine.compile(question, request_id)

    assert plan is not None
    authorized, receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(projection,),
    )

    assert authorized is trace.route_decision
    assert receipt is None


def test_anchor_ledger_outranks_advisory_wrong_hclx_execution_meaning(
    domestic_sample_database,
) -> None:
    """Wrong model operators/direction cannot veto or alter a complete ledger."""

    database_path, records, _manifest = domestic_sample_database
    anchor = records[0]
    anchor_name = "셀레네 균형전략 기준 지수"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (anchor_name, anchor_name, anchor.product_id),
        )
    anchor_surface = f"{anchor_name}({anchor.product_id})"
    question = (
        f"테스트운용 운용 국내 ETF에서 {anchor_surface}를 기준점으로 삼아 주세요. "
        "이 상품 이상인 3개월 수익률과 이 상품 이하인 총보수를 동시에 "
        "만족하는 다른 상품을 순자산 낮은 순으로 3개 찾아주세요."
    )
    clauses = [
        IntentExecutionClause(
            sequence=1,
            role="scope",
            text_span="테스트운용 운용 국내 ETF",
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.UNRESOLVED,
        ),
        IntentExecutionClause(
            sequence=2,
            role="filter",
            text_span=anchor_surface,
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.UNRESOLVED,
        ),
        IntentExecutionClause(
            sequence=3,
            role="filter",
            text_span="이 상품 이상인 3개월 수익률",
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.RESOLVED,
            field_id="three_month_return_pct",
            operation=SemanticFrameOperation.FILTER,
            operator=SemanticFrameOperator.GT,
            field_span="3개월 수익률",
            value_span=anchor_surface,
        ),
        IntentExecutionClause(
            sequence=4,
            role="filter",
            text_span="이 상품 이하인 총보수",
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.RESOLVED,
            field_id="total_expense_ratio_pct",
            operation=SemanticFrameOperation.FILTER,
            operator=SemanticFrameOperator.LTE,
            field_span="총보수",
            value_span=anchor_surface,
        ),
        IntentExecutionClause(
            sequence=5,
            role="ranking",
            text_span="순자산 낮은 순으로 3개",
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.RESOLVED,
            field_id="aum",
            operation=SemanticFrameOperation.RANK,
            direction=SemanticFrameDirection.DESC,
            field_span="순자산",
            limit_span="3",
        ),
    ]
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: database_path},
        intent_analysis_provider=_FixedSemanticFrameProvider(
            IntentAnalysisDraft(
                intent=InteractionIntent.SEARCH,
                product_families=[ProductFamily.DOMESTIC_ETP],
                evidence_spans=[],
                needs_clarification=False,
                execution_clauses=clauses,
            )
        ),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="advisory-wrong-anchor-frame", question=question),
    ).response

    assert response.status is BackendStatus.SUCCESS, response.clarification
    assert response.query_plan is not None
    assert {
        (item.field, item.operator.value)
        for item in response.query_plan.constraints
    } >= {
        ("manager", "eq"),
        ("three_month_return_pct", "gte"),
        ("total_expense_ratio_pct", "lte"),
    }
    assert response.query_plan.ranking[0].field == "aum"
    assert response.query_plan.ranking[0].direction.value == "asc"
    assert response.query_plan.limit == 3


def test_two_stage_ledger_proves_explicit_complete_calculation_inputs() -> None:
    """Availability prose is owned only when every metric input is sealed."""

    question = (
        "채권종류가 일반회사채인 장내 국내채권 중 잔존일수가 411 이상이고 "
        "계산 입력값이 모두 있는 상품에서 듀레이션 상위 8개를 먼저 추린 "
        "다음, 총발행금액 대비 발행잔액 비율이 큰 순으로 3개를 골라주세요."
    )
    engine, trace, plan = _compile_without_reading(question, "complete-input-ledger")
    family_plan = plan.family_plans[0]
    required_fields = {
        *family_plan.metric.source_fields,
        *family_plan.shortlist_metric.source_fields,
    }
    assert required_fields == {"dur", "isu_bal_amt", "bd_tisu_a"}
    assert all(
        any(
            item.source_field == source
            and item.operator == "neq"
            and item.value == 0
            for item in family_plan.filters
        )
        for source in required_fields
    )

    unresolved = tuple(
        dict.fromkeys(
            (
                *trace.planning_decision.unresolved_spans,
                "계산 입력값이 모두 있는",
            )
        )
    )
    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=unresolved,
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == unresolved


def test_two_stage_ledger_proves_individual_server_compiled_filter_spans() -> None:
    """HCX segmentation cannot veto filters rebuilt in the canonical plan."""

    question = (
        "채권종류가 일반회사채인 장내 국내채권 중 잔존일수가 509 이상이고 "
        "계산 입력값이 모두 있는 상품에서 듀레이션 상위 9개를 먼저 추린 "
        "다음, 총발행금액 대비 발행잔액 비율이 큰 순으로 4개를 골라주세요."
    )
    engine, trace, plan = _compile_without_reading(question, "filter-span-ledger")
    unresolved = (
        "채권종류가 일반회사채인",
        "장내 국내채권",
        "잔존일수가 509 이상",
    )
    proofs = {
        span: engine._canonical_ledger_proves_unresolved_span(
            span,
            plan,
            anchor_clause_coverage=None,
            named_set_clause_coverage=None,
        )
        for span in unresolved
    }

    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=unresolved,
    )

    assert authorized.disposition is RouteDisposition.EXECUTE, proofs
    assert receipt is not None
    assert receipt.covered_unresolved_spans == unresolved


def test_bounded_ledger_proves_clause_local_filter_and_rank_tail() -> None:
    """HCX uncertainty cannot veto a fully reproduced ordinary rank clause."""

    question = (
        "한화 운용 국내 ETF 중 20거래일 연환산 변동성이 14.88134495 "
        "이상인 것 중 낮은 순서로 3개 보여주세요."
    )
    engine, trace, plan = _compile_without_reading(question, "bounded-clause-ledger")
    unresolved = (
        "한화 운용 국내 ETF",
        "20거래일 연환산 변동성이 14.88134495 이상인 것",
        "낮은 순서로 3개",
    )

    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=unresolved,
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == unresolved
    whole_authorized, whole_receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(question,),
    )
    assert whole_authorized.disposition is RouteDisposition.EXECUTE
    assert whole_receipt is not None
    assert whole_receipt.covered_unresolved_spans == (question,)


@pytest.mark.parametrize(
    "tail",
    (
        "보여주되 인기도도 반영해 주세요.",
        "보여주고 참고로 상위 99개도 고려해 주세요.",
        "보여주되 绿色 조건도 반영해 주세요.",
    ),
)
def test_bounded_ledger_rejects_unowned_semantics(tail: str) -> None:
    """Known filters never hide subjective, conflicting or Unicode criteria."""

    question = (
        "한화 운용 국내 ETF 중 20거래일 연환산 변동성이 14 이상인 것 중 "
        f"낮은 순서로 3개 {tail}"
    )
    engine, trace, plan = _compile_without_reading(question, "bounded-unowned-tail")

    blocked, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(question,),
    )

    assert blocked is trace.route_decision
    assert receipt is None


def test_derived_ledger_proves_formula_but_rejects_unowned_tail() -> None:
    """An explicit formula owns its rank, never a neighbouring criterion."""

    clean = (
        "장내 금융지주회사채 중 총발행금액 대비 발행잔액 비율(%)이 "
        "높은 순으로 3개 보여주세요."
    )
    engine, trace, plan = _compile_without_reading(clean, "derived-owned-formula")

    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(clean,),
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None

    for suffix in ("인기도도 반영해 주세요.", "绿色 조건도 반영해 주세요."):
        question = f"{clean} {suffix}"
        unsafe_engine, unsafe_trace, unsafe_plan = _compile_without_reading(
            question,
            "derived-unowned-tail",
        )
        blocked, unsafe_receipt = unsafe_engine.authorize_decision(
            unsafe_trace.route_decision,
            unsafe_plan,
            unresolved_spans=(question,),
        )
        assert blocked is unsafe_trace.route_decision
        assert unsafe_receipt is None


@pytest.mark.parametrize(
    "question",
    (
        "공모펀드 중 1년수익률이 3개월수익률보다 높은 상품 중 "
        "두 수익률 차이가 큰 순으로 3개 보여주되 친환경성도 반영해 주세요.",
        "국내 ETF 중 순자산이 평균보다 높은 상품을 일거래대금 큰 순으로 "
        "3개 보여주되 인기도도 반영해 주세요.",
        "국내 ETF의 운용사별 순자산 평균을 알려주되 인기도도 반영해 주세요.",
    ),
)
def test_atomic_fallback_cannot_hide_unowned_composite_semantics(question: str) -> None:
    """A known comparison or aggregate token cannot authorize an extra rule."""

    engine, trace, plan = _compile_without_reading(question, "atomic-fallback-tail")
    blocked, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(question,),
    )

    assert blocked is trace.route_decision
    assert receipt is None


def test_cohort_ledger_proves_exact_scopes_and_fixed_summary_contract() -> None:
    """Aggregate projection nouns are advisory after exact cohort recompilation."""

    question = (
        "키움 운용 국내 ETF와 하나 운용 국내 ETF의 일거래대금 분포를 "
        "비교해 주세요. 0·빈칸은 빼고 각각 유효 상품 수, 단순평균, "
        "최솟값과 최댓값을 계산한 뒤 각 집단의 상위 3개도 알려주세요. "
        "평균이 더 큰 집단과 평균 차이까지 부탁드립니다."
    )
    engine, trace, plan = _compile_without_reading(question, "cohort-summary-ledger")
    unresolved = (
        "키움 운용 국내 ETF",
        "하나 운용 국내 ETF",
        "일거래대금 분포를 비교",
        "유효 상품 수",
        "단순평균",
        "최솟값",
        "최댓값",
        "각 집단의 상위 3개",
    )

    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=unresolved,
    )

    assert plan.kind == "cohort"
    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == unresolved

    bundled = (
        "유효 상품 수, 단순평균, 최솟값과 최댓값을 계산한 뒤 각 집단의 "
        "상위 3개도 알려주세요.",
        "평균이 더 큰 집단과 평균 차이",
    )
    bundled_authorized, bundled_receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=bundled,
    )
    bundled_proofs = {
        span: engine._canonical_cohort_summary_proves_unresolved_span(span, plan)
        for span in bundled
    }
    assert bundled_authorized.disposition is RouteDisposition.EXECUTE
    assert bundled_receipt is not None, bundled_proofs
    assert bundled_receipt.covered_unresolved_spans == bundled


def test_cohort_ledger_rejects_untyped_extra_criterion() -> None:
    """A known cohort contract never absorbs an unrelated subjective condition."""

    question = (
        "키움 운용 국내 ETF와 하나 운용 국내 ETF의 일거래대금 분포를 "
        "비교해 주세요. 0·빈칸은 빼고 각각 유효 상품 수, 단순평균, "
        "최솟값과 최댓값을 계산한 뒤 각 집단의 상위 3개도 알려주세요. "
        "평균이 더 큰 집단과 평균 차이를 구하되 인기도도 반영해 주세요."
    )
    engine, trace, plan = _compile_without_reading(question, "cohort-untyped-extra")

    blocked, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=("인기도도 반영",),
    )

    assert blocked is trace.route_decision
    assert receipt is None


def test_two_stage_ledger_accepts_complete_hclx_clauses_only_in_their_family() -> None:
    """A long model span cannot force literal equality across family clauses."""

    domestic_clause = (
        "미래에셋 운용 국내 ETF 중 순자산총액이 220,000,000,000 이하이고 "
        "계산 입력값이 모두 있는 상품에서 3개월 수익률 상위 6개를 먼저 "
        "추린 다음, 일거래대금을 1개월 평균 거래대금으로 나눈 배수가 큰 "
        "순으로 2개"
    )
    overseas_clause = (
        "투자자산군이 Alternatives인 해외 ETF 중 연간보수율이 0.51 이상이고 "
        "계산 입력값이 모두 있는 상품에서 일거래대금 상위 5개를 먼저 추린 "
        "다음, 시가 대비 종가 변화율((종가-시가)/시가×100)이 큰 순으로 3개"
    )
    question = (
        "상품군별로 서로 다른 계산을 수행해 주세요. 첫째, "
        f"{domestic_clause}. 둘째, {overseas_clause}. "
        "두 결과를 통합 순위로 합치지 마세요."
    )
    engine, trace, plan = _compile_without_reading(question, "family-local-long-clause")
    clarify = RouteDecision(
        draft=trace.route_decision.draft.model_copy(
            update={"intent": InteractionIntent.CLARIFY}
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="semantic_coverage_incomplete",
        reason="HCX가 두 실행 절을 긴 미해결 span으로 반환",
        query_plan_intent=None,
        capability_matrix_version=trace.route_decision.capability_matrix_version,
    )

    authorized, receipt = engine.authorize_decision(
        clarify,
        plan,
        unresolved_spans=(domestic_clause, overseas_clause),
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == (domestic_clause, overseas_clause)


def test_two_stage_long_hclx_clause_cannot_hide_subjective_condition() -> None:
    domestic_clause = (
        "국내 ETF 중 순자산총액이 220,000,000,000 이하이고 계산 입력값이 "
        "모두 있는 상품에서 3개월 수익률 상위 6개를 먼저 추린 다음, "
        "일거래대금을 1개월 평균 거래대금으로 나눈 배수가 큰 순으로 "
        "2개를 고르되 인기도도 반영"
    )
    question = domestic_clause + "해 주세요."
    engine, trace, plan = _compile_without_reading(question, "subjective-long-clause")
    clarify = RouteDecision(
        draft=trace.route_decision.draft.model_copy(
            update={"intent": InteractionIntent.CLARIFY}
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="semantic_coverage_incomplete",
        reason="주관 조건이 포함됨",
        query_plan_intent=None,
        capability_matrix_version=trace.route_decision.capability_matrix_version,
    )

    blocked, receipt = engine.authorize_decision(
        clarify,
        plan,
        unresolved_spans=(domestic_clause,),
    )

    assert blocked is clarify
    assert receipt is None


@pytest.mark.parametrize(
    "question",
    (
        (
            "국내 ETF 중 순자산총액이 220,000,000,000 이하인 상품에서 "
            "3개월 수익률 상위 6개를 먼저 추린 다음, 일거래대금이 "
            "큰 순으로 2개를 고르되 로고 색깔이 파란 상품만 선택해 주세요."
        ),
        (
            "국내 ETF 중 로고 색깔이 파란 상품에서 순자산총액이 "
            "큰 6개를 먼저 추린 다음, 일거래대금이 큰 순으로 "
            "2개를 골라주세요."
        ),
        (
            "국내 ETF 중 친환경 3개월 수익률 상위 6개를 먼저 추린 다음, "
            "일거래대금이 큰 순으로 2개"
        ),
    ),
)
def test_forced_long_hclx_unresolved_clause_cannot_hide_unregistered_attribute(
    domestic_sample_database,
    question: str,
) -> None:
    """One known metric never authorizes an unrelated untyped condition."""

    request_id = "forced-long-unresolved-unregistered-attribute"
    engine, trace, plan = _compile_without_reading(question, request_id)
    forced_clarify = RouteDecision(
        draft=trace.route_decision.draft.model_copy(
            update={"intent": InteractionIntent.CLARIFY}
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="semantic_coverage_incomplete",
        reason="HCX가 부가 조건을 포함한 전체 절을 미해결로 반환",
        query_plan_intent=None,
        capability_matrix_version=trace.route_decision.capability_matrix_version,
    )
    blocked, receipt = engine.authorize_decision(
        forced_clarify,
        plan,
        unresolved_spans=(question,),
    )
    assert blocked is forced_clarify
    assert receipt is None

    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="filter",
                text_span=question,
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            )
        ],
    )
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]},
        intent_analysis_provider=_FixedSemanticFrameProvider(draft),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id=request_id,
            question=question,
        ),
    ).response

    assert response.status is BackendStatus.CLARIFICATION
    assert response.query_plan is None
    assert response.products == []


def test_canonical_ledger_does_not_hide_an_extra_unresolved_criterion(
    domestic_sample_database,
) -> None:
    """HCX advisory status never turns an uncompiled subjective clause into SQL."""

    database_path, records, _manifest = domestic_sample_database
    selected = records[:2]
    identity_surfaces = tuple(
        f"{record.product_name}({record.product_id})" for record in selected
    )
    question = (
        f"{identity_surfaces[0]}와 {identity_surfaces[1]}만 놓고 1개월 수익률이 "
        "높은 순으로 비교하되 인기도도 함께 반영해 주세요."
    )
    clauses = [
        IntentExecutionClause(
            sequence=index,
            role="filter",
            text_span=surface,
            product_families=[ProductFamily.DOMESTIC_ETP],
            semantic_status=SemanticFrameStatus.UNRESOLVED,
        )
        for index, surface in enumerate(identity_surfaces, start=1)
    ]
    clauses.extend(
        (
            IntentExecutionClause(
                sequence=3,
                role="ranking",
                text_span="1개월 수익률이 높은 순으로 비교",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="one_month_return_pct",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="1개월 수익률",
            ),
            IntentExecutionClause(
                sequence=4,
                role="filter",
                text_span="인기도도 함께 반영",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.UNRESOLVED,
            ),
        )
    )
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: database_path},
        intent_analysis_provider=_FixedSemanticFrameProvider(
            IntentAnalysisDraft(
                intent=InteractionIntent.SEARCH,
                product_families=[ProductFamily.DOMESTIC_ETP],
                evidence_spans=[],
                needs_clarification=False,
                execution_clauses=clauses,
            )
        ),
    )

    response = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="unresolved-extra-criterion", question=question),
    ).response

    assert response.status is BackendStatus.CLARIFICATION
    assert response.query_plan is None
    assert response.products == []


def test_named_set_ledger_owns_only_an_exact_cardinality_declaration(
    domestic_sample_database,
) -> None:
    database_path, records, _manifest = domestic_sample_database
    selected = records[:3]
    names = ("아르고 전략 지수", "벨라 성장 지수", "키론 가치 지수")
    with sqlite3.connect(database_path) as connection:
        for record, name in zip(selected, names, strict=True):
            connection.execute(
                "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
                "WHERE product_id = ?",
                (name, name, record.product_id),
            )
    identities = ", ".join(
        f"{name}({record.product_id})"
        for name, record in zip(names, selected, strict=True)
    )
    declaration = f"후보를 이 세 상품으로만 한정할게요: {identities}"
    question = (
        f"{declaration}. 1개월 수익률이 높은 순서로 셋을 나열하고 각 "
        "상품의 일거래대금도 같이 보여주세요."
    )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    request_id = "named-set-exact-declaration"
    trace = agent.router.route_with_planning(question, request_id)
    plan = agent.compound_query_engine.compile(question, request_id)

    assert plan is not None
    authorized, receipt = agent.compound_query_engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=(declaration,),
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert receipt is not None
    assert receipt.covered_unresolved_spans == (declaration,)

    mismatched_question = question.replace("이 세 상품", "이 두 상품", 1)
    mismatched_request_id = "named-set-wrong-declaration-count"
    mismatched_trace = agent.router.route_with_planning(
        mismatched_question,
        mismatched_request_id,
    )
    mismatched_plan = agent.compound_query_engine.compile(
        mismatched_question,
        mismatched_request_id,
    )

    assert mismatched_plan is not None
    blocked, blocked_receipt = agent.compound_query_engine.authorize_decision(
        mismatched_trace.route_decision,
        mismatched_plan,
        unresolved_spans=(
            declaration.replace("이 세 상품", "이 두 상품", 1),
        ),
    )
    assert blocked is mismatched_trace.route_decision
    assert blocked_receipt is None

    expanded_declaration = declaration + " 그리고 인기도도 반영"
    expanded_question = question.replace(declaration, expanded_declaration, 1)
    expanded_request_id = "named-set-declaration-extra-condition"
    expanded_trace = agent.router.route_with_planning(
        expanded_question,
        expanded_request_id,
    )
    expanded_plan = agent.compound_query_engine.compile(
        expanded_question,
        expanded_request_id,
    )

    assert expanded_plan is not None
    expanded_blocked, expanded_receipt = agent.compound_query_engine.authorize_decision(
        expanded_trace.route_decision,
        expanded_plan,
        unresolved_spans=(expanded_declaration,),
    )
    assert expanded_blocked is expanded_trace.route_decision
    assert expanded_receipt is None


def test_open_named_examples_never_shrink_a_broader_cohort_to_those_ids(
    domestic_sample_database,
) -> None:
    database_path, records, _manifest = domestic_sample_database
    engine = TypedCompoundQueryEngine(
        {ProductFamily.DOMESTIC_ETP: database_path},
        record_cache=object(),  # type: ignore[arg-type]
    )
    question = (
        f"{records[0].product_id}와 {records[1].product_id}를 포함한 국내 ETF 중 "
        "1개월 수익률이 높은 순서로 2개를 보여주세요."
    )
    trace = IntentRouter().route_with_planning(question, "open-named-cohort")
    plan = engine.compile(question, "open-named-cohort")

    assert plan is not None
    assert plan.kind == "named_pair"
    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )

    assert authorized is trace.route_decision
    assert receipt is None


def test_public_agent_executes_named_pair_only_after_db_identity_resolution(
    domestic_sample_database,
) -> None:
    agent = RoutedFinanceAgent(
        {ProductFamily.DOMESTIC_ETP: domestic_sample_database[0]}
    )
    question = (
        "국내 ETF KR7000000002와 KR7000000003 중 1개월 수익률이 "
        "더 높은 상품부터 둘 다 나열해 주세요"
    )

    result = agent.answer(question, "fresh-named-pair")

    assert result.status == "executed", result.decision
    assert result.decision.draft.intent is InteractionIntent.COMPARE
    assert [product.product_id for product in result.products] == [
        "KR7000000003",
        "KR7000000002",
    ]


@pytest.mark.parametrize("unavailable_value", ["0", None])
def test_named_comparison_with_unavailable_metric_never_returns_partial_success(
    domestic_sample_database,
    unavailable_value: str | None,
) -> None:
    database_path = domestic_sample_database[0]
    unavailable_product_id = "KR7000000002"
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT source_values_json FROM domestic_etp_products WHERE product_id = ?",
            (unavailable_product_id,),
        ).fetchone()
        assert row is not None
        source_values = json.loads(str(row[0]))
        source_values["du_er_1m"] = unavailable_value
        connection.execute(
            "UPDATE domestic_etp_products SET source_values_json = ? WHERE product_id = ?",
            (
                json.dumps(source_values, ensure_ascii=False, sort_keys=True),
                unavailable_product_id,
            ),
        )

    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: database_path})
    question = (
        "국내 ETF KR7000000002와 KR7000000003 중 1개월 수익률이 "
        "더 높은 상품부터 둘 다 나열해 주세요"
    )
    plan = agent.compound_query_engine.compile(question, "incomplete-named-pair-engine")
    assert plan is not None

    with pytest.raises(NamedComparisonIncompleteError) as execution_error:
        agent.compound_query_engine.execute(plan)
    assert execution_error.value.unavailable_product_ids == (unavailable_product_id,)
    assert execution_error.value.metric_source_fields == ("du_er_1m",)

    adapted = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="incomplete-named-pair-public",
            question=question,
        ),
    )

    assert adapted.http_status_code == 200
    assert adapted.response.status is BackendStatus.CLARIFICATION
    assert adapted.response.products == []
    assert adapted.response.query_plan is None
    assert adapted.response.clarification is not None
    assert "일부 상품만으로 비교하지 않았습니다" in adapted.response.answer


def _cross_family_agent(sample_database, domestic_sample_database) -> RoutedFinanceAgent:
    return RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_sample_database[0],
            ProductFamily.OVERSEAS_ETP: sample_database[0],
        }
    )


_CROSS_FAMILY_COMPOUND_QUESTION = (
    "국내 ETF에서는 일거래대금 500000 이상, 해외 ETF에서는 일거래대금 "
    "100 이상인 상품을 상품군별로 일거래대금 큰 순서대로 두 개씩 보여주세요"
)


def test_execute_compound_without_authorization_receipt_clarifies_instead_of_raising_5xx(
    sample_database,
    domestic_sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)

    monkeypatch.setattr(
        agent.compound_query_engine,
        "authorize_decision",
        lambda decision, _plan, **_kwargs: (decision, None),
    )

    result = agent.answer(_CROSS_FAMILY_COMPOUND_QUESTION, "missing-compound-receipt")

    assert result.status == "clarify"
    assert result.decision.reason_code == "compound_authorization_missing"
    assert result.query_plan is None
    assert result.family_searches == []
    assert result.products == []


def test_compound_authorization_value_error_clarifies_instead_of_raising_5xx(
    sample_database,
    domestic_sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)

    def reject_authorization(*_args, **_kwargs):
        raise ValueError("synthetic non-reproducible compound plan")

    monkeypatch.setattr(
        agent.compound_query_engine,
        "authorize_decision",
        reject_authorization,
    )

    adapted = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="invalid-compound-receipt",
            question=_CROSS_FAMILY_COMPOUND_QUESTION,
        ),
    )
    result = adapted.response

    assert adapted.http_status_code == 200
    assert result.status is BackendStatus.CLARIFICATION
    assert result.query_plan is None
    assert result.family_searches == []
    assert result.products == []
    assert result.clarification is not None


def test_multi_family_compound_compile_miss_never_falls_back_to_partial_search(
    sample_database,
    domestic_sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)
    monkeypatch.setattr(agent.compound_query_engine, "compile", lambda *_args, **_kwargs: None)

    result = agent.answer(_CROSS_FAMILY_COMPOUND_QUESTION, "compound-compile-miss")

    assert result.status == "clarify"
    assert result.decision.reason_code == "compound_plan_required"
    assert result.query_plan is None
    assert result.family_searches == []
    assert result.products == []


def test_compound_compile_miss_preserves_original_multi_family_scope(
    sample_database,
    domestic_sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)
    monkeypatch.setattr(agent.compound_query_engine, "compile", lambda *_args, **_kwargs: None)

    def collapse_to_one_family(decision: RouteDecision) -> RouteDecision:
        return RouteDecision(
            draft=decision.draft.model_copy(
                update={"product_families": [ProductFamily.DOMESTIC_ETP]}
            ),
            disposition=RouteDisposition.EXECUTE,
            reason_code="synthetic_exact_identity_family_resolution",
            reason="synthetic single-family rewrite",
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version=decision.capability_matrix_version,
        )

    monkeypatch.setattr(agent, "_resolve_exact_identity_family", collapse_to_one_family)

    result = agent.answer(_CROSS_FAMILY_COMPOUND_QUESTION, "compound-scope-collapse")

    assert result.status == "clarify"
    assert result.decision.reason_code == "compound_plan_required"
    assert result.decision.draft.product_families == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert result.products == []


def test_safety_unsupported_skips_compound_compilation_and_remains_terminal(
    sample_database,
    domestic_sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Safety terminal must not reach compound compilation")

    monkeypatch.setattr(agent.compound_query_engine, "compile", fail_if_called)
    question = _CROSS_FAMILY_COMPOUND_QUESTION + " 그리고 실제로 매수해 주세요."

    result = agent.answer(question, "compound-safety-terminal")

    assert result.status == "unsupported"
    assert result.decision.disposition is RouteDisposition.UNSUPPORTED
    assert result.decision.reason_code == "safety_transaction"
    assert result.query_plan is None
    assert result.products == []
