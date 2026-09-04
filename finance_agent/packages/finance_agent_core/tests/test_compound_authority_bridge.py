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
