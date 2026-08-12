from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from finance_agent_core.agent.backend_adapter import execute_answer_request
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
    PlanningDecisionStatus,
    PlanningPath,
    PlanningSemanticIssue,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_gate import (
    SemanticCoverageDecision,
    SemanticCoverageGate,
)
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition


class _FixedCoverageGate:
    def __init__(self, decision: SemanticCoverageDecision) -> None:
        self.decision = decision

    def evaluate(
        self,
        question: str,
        *,
        interaction_intent: str | None = None,
        check_exclusions: bool = True,
    ) -> SemanticCoverageDecision:
        del question, interaction_intent, check_exclusions
        return self.decision


class _ExplodingPlanningPolicy:
    def decide(self, *_args: object, **_kwargs: object) -> PlanningDecision:
        raise RuntimeError("secret-that-must-not-leak")


class _InvalidReturnPolicy:
    def decide(self, *_args: object, **_kwargs: object) -> object:
        return object()


class _EscalatingPolicy:
    def decide(self, route_decision: object, _coverage: object) -> PlanningDecision:
        route = route_decision
        return PlanningDecision(
            path=PlanningPath.DETERMINISTIC_FAST,
            semantic_issue=PlanningSemanticIssue.NONE,
            product_families=tuple(route.draft.product_families),
            route_reason_code=route.reason_code,
            reason_code="malicious_escalation",
            sql_allowed=True,
            compiler_allowed=True,
            oracle_allowed=True,
        )


class _CopyEscalatingPolicy:
    def decide(self, route_decision: object, _coverage: object) -> PlanningDecision:
        route = route_decision
        valid_control = PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            product_families=tuple(route.draft.product_families),
            route_reason_code=route.reason_code,
            reason_code="copied_escalation",
        )
        return valid_control.model_copy(update={"sql_allowed": True})


class _FailThenSucceedPlanningPolicy:
    def __init__(self) -> None:
        self.calls = 0
        self.fallback = AdaptiveShadowPlanningPolicy()

    def decide(self, route_decision: object, coverage: object) -> PlanningDecision:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first routing decision must stay closed")
        return self.fallback.decide(route_decision, coverage)


class _ExplodingAllModelProviders:
    provider_name = "must-not-run"
    model_name = "must-not-run"

    def generate_query_plan(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("QueryPlan provider must not run")

    def generate_grounded_plan(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("grounded plan provider must not run")

    def generate_grounded_answer(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("grounded answer provider must not run")


class _CountingPlanningProviders:
    provider_name = "counting-test"
    model_name = "counting-test-model"

    def __init__(self, plan: QueryPlan | None = None) -> None:
        self.plan = plan
        self.query_plan_calls = 0
        self.grounded_plan_calls = 0

    def generate_query_plan(self, *_args: object, **_kwargs: object) -> QueryPlan:
        self.query_plan_calls += 1
        if self.plan is None:
            raise AssertionError("QueryPlan provider must not run")
        return self.plan

    def generate_grounded_plan(self, *_args: object, **_kwargs: object) -> object:
        self.grounded_plan_calls += 1
        raise AssertionError("grounded plan provider must not run")


class _FailingQueryPlanProvider:
    provider_name = "failing-test"
    model_name = "failing-test-model"

    def __init__(self) -> None:
        self.calls = 0

    def generate_query_plan(self, *_args: object, **_kwargs: object) -> QueryPlan:
        self.calls += 1
        raise RuntimeError("simulated advisory transport failure")


def _assert_new_calls_are_closed(decision: PlanningDecision) -> None:
    assert decision.dense_allowed is False
    assert decision.hclx_allowed is False


def _assert_all_calls_are_closed(decision: PlanningDecision) -> None:
    _assert_new_calls_are_closed(decision)
    assert decision.sql_allowed is False
    assert decision.compiler_allowed is False
    assert decision.oracle_allowed is False


def _agent_with_exploding_boundaries(
    router: IntentRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> RoutedFinanceAgent:
    providers = _ExplodingAllModelProviders()
    agent = RoutedFinanceAgent(
        {},
        router=router,
        query_plan_provider=providers,
        grounded_plan_provider=providers,
        answer_provider=providers,
    )

    def fail_boundary(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Compiler or Oracle boundary must not run")

    monkeypatch.setattr(agent.compiler, "compile", fail_boundary)
    monkeypatch.setattr(agent.compiler, "compile_search_plans", fail_boundary)
    monkeypatch.setattr(agent.compiler, "compile_family_searches", fail_boundary)
    monkeypatch.setattr(agent, "_record_universe", fail_boundary)
    monkeypatch.setattr(
        "finance_agent_core.agent.routed_service.SQLiteOracle",
        fail_boundary,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.routed_service.SQLiteAggregateOracle",
        fail_boundary,
    )
    return agent


def test_adaptive_shadow_classifies_clear_question_as_deterministic_fast_path() -> None:
    trace = IntentRouter().route_with_planning(
        "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        "planning-clear",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.planning_decision.path is PlanningPath.DETERMINISTIC_FAST
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.NONE
    assert trace.planning_decision.product_families == (ProductFamily.DOMESTIC_ETP,)
    _assert_new_calls_are_closed(trace.planning_decision)
    assert trace.planning_decision.sql_allowed is True
    assert trace.planning_decision.compiler_allowed is True
    assert trace.planning_decision.oracle_allowed is True


def test_explicit_server_opt_in_authorizes_hclx_only_on_clear_fast_path() -> None:
    trace = IntentRouter(hclx_planning_enabled=True).route_with_planning(
        "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        "planning-clear-hclx-enabled",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.planning_decision.path is PlanningPath.DETERMINISTIC_FAST
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.NONE
    assert trace.planning_decision.hclx_allowed is True


@pytest.mark.parametrize(
    ("question", "expected_issue"),
    [
        ("수익률이 높은 국내 ETF를 보여줘.", PlanningSemanticIssue.TRUE_AMBIGUITY),
        ("국내 ETF 매수 주문을 실행해 줘.", PlanningSemanticIssue.UNSUPPORTED),
    ],
)
def test_server_hclx_opt_in_never_opens_control_semantics(
    question: str,
    expected_issue: PlanningSemanticIssue,
) -> None:
    trace = IntentRouter(hclx_planning_enabled=True).route_with_planning(
        question,
        f"planning-hclx-control-{expected_issue.value}",
    )

    assert trace.planning_decision.path is PlanningPath.CONTROL
    assert trace.planning_decision.semantic_issue is expected_issue
    assert trace.planning_decision.hclx_allowed is False
    _assert_all_calls_are_closed(trace.planning_decision)


def test_cross_family_search_remains_on_the_existing_deterministic_path() -> None:
    trace = IntentRouter().route_with_planning(
        "국내 ETF와 해외 ETF를 각각 AUM 큰 순으로 2개씩 보여줘.",
        "planning-cross-family",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.route_decision.reason_code == "cross_family_search_executable"
    assert trace.planning_decision.path is PlanningPath.DETERMINISTIC_FAST
    assert trace.planning_decision.product_families == (
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    )
    _assert_new_calls_are_closed(trace.planning_decision)


def test_cross_family_schema_gap_remains_a_non_enforcing_shadow() -> None:
    gate = _FixedCoverageGate(SemanticCoverageDecision(schema_link_gap_spans=("운용 비용률",)))
    trace = IntentRouter(semantic_coverage_gate=gate).route_with_planning(
        "국내 ETF와 해외 ETF를 각각 운용 비용률 낮은 순으로 보여줘.",
        "planning-cross-family-gap",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.route_decision.reason_code == "cross_family_search_executable"
    assert trace.planning_decision.path is PlanningPath.SCHEMA_LINK_SHADOW
    assert trace.planning_decision.product_families == (
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    )
    _assert_all_calls_are_closed(trace.planning_decision)


def test_adaptive_shadow_has_no_cross_request_mutable_state() -> None:
    router = IntentRouter()
    cases = [
        ("1개월 수익률이 높은 국내 ETF 3개를 보여줘.", PlanningSemanticIssue.NONE),
        ("수익률이 높은 국내 ETF를 보여줘.", PlanningSemanticIssue.TRUE_AMBIGUITY),
        ("국내 ETF 매수 주문을 실행해 줘.", PlanningSemanticIssue.UNSUPPORTED),
    ]

    def evaluate(index: int) -> tuple[str, PlanningSemanticIssue]:
        question, expected = cases[index % len(cases)]
        trace = router.route_with_planning(question, f"parallel-{index}")
        return trace.route_decision.draft.request_id, trace.planning_decision.semantic_issue

    with ThreadPoolExecutor(max_workers=8) as executor:
        actual = list(executor.map(evaluate, range(120)))

    assert [request_id for request_id, _ in actual] == [f"parallel-{index}" for index in range(120)]
    assert [issue for _, issue in actual] == [cases[index % len(cases)][1] for index in range(120)]


def test_adaptive_shadow_keeps_true_ambiguity_closed() -> None:
    trace = IntentRouter().route_with_planning(
        "수익률이 높은 국내 ETF를 보여줘.",
        "planning-ambiguity",
    )

    assert trace.route_decision.disposition is RouteDisposition.CLARIFY
    assert trace.route_decision.reason_code == "semantic_coverage_incomplete"
    assert trace.planning_decision.path is PlanningPath.CONTROL
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.TRUE_AMBIGUITY
    assert "수익률" in trace.planning_decision.unresolved_spans
    _assert_all_calls_are_closed(trace.planning_decision)


def test_adaptive_shadow_keeps_unsupported_request_closed() -> None:
    trace = IntentRouter().route_with_planning(
        "국내 ETF 매수 주문을 실행해 줘.",
        "planning-unsupported",
    )

    assert trace.route_decision.disposition is RouteDisposition.UNSUPPORTED
    assert trace.planning_decision.path is PlanningPath.CONTROL
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.UNSUPPORTED
    _assert_all_calls_are_closed(trace.planning_decision)


@pytest.mark.parametrize(
    ("question", "expected_status"),
    [
        ("수익률이 높은 국내 ETF를 보여줘.", "clarify"),
        ("국내 ETF 매수 주문을 실행해 줘.", "unsupported"),
    ],
)
def test_control_routes_never_call_model_compiler_or_oracle_boundaries(
    question: str,
    expected_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = IntentRouter()
    agent = _agent_with_exploding_boundaries(router, monkeypatch)

    result = agent.answer(question, f"planning-no-call-{expected_status}")

    assert result.status == expected_status
    assert result.query_plan is None


def test_provider_injection_without_server_permission_keeps_clear_search_deterministic(
    sample_database,
) -> None:
    path, _, _ = sample_database
    providers = _CountingPlanningProviders()
    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=providers,
    ).answer(
        "미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘",
        "planning-provider-default-off",
    )

    assert result.status == "executed"
    assert providers.query_plan_calls == 0


def test_explicit_server_permission_allows_clear_query_plan_provider_call(
    sample_database,
) -> None:
    path, _, _ = sample_database
    question = "미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘"
    request_id = "planning-provider-explicit-on"
    route = IntentRouter().route(question, request_id)
    plan = ServerQueryPlanCompiler({"overseas_etp": path}).compile(route)
    providers = _CountingPlanningProviders(plan)

    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=providers,
        hclx_planning_enabled=True,
    ).answer(question, request_id)

    assert result.status == "executed"
    assert result.query_plan == plan
    assert providers.query_plan_calls == 1


def test_query_plan_provider_failure_retains_the_deterministic_server_plan(
    sample_database,
) -> None:
    path, _, _ = sample_database
    provider = _FailingQueryPlanProvider()

    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        query_plan_provider=provider,
        hclx_planning_enabled=True,
    ).answer(
        "미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘",
        "planning-provider-fallback",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert provider.calls == 1


@pytest.mark.parametrize(
    ("question", "expected_status"),
    [
        ("수익률이 높은 국내 ETF를 보여줘.", "clarify"),
        ("국내 ETF 매수 주문을 실행해 줘.", "unsupported"),
        ("B2 vs B3, 총보수율과 AUM 비교", "clarify"),
    ],
)
def test_explicit_server_permission_still_blocks_all_model_planning_on_controls(
    question: str,
    expected_status: str,
) -> None:
    providers = _CountingPlanningProviders()
    result = RoutedFinanceAgent(
        {},
        query_plan_provider=providers,
        grounded_plan_provider=providers,
        hclx_planning_enabled=True,
    ).answer(question, f"planning-provider-control-{expected_status}")

    assert result.status == expected_status
    assert result.query_plan is None
    assert providers.query_plan_calls == 0
    assert providers.grounded_plan_calls == 0


def test_schema_link_shadow_blocks_all_model_planning_despite_server_opt_in(
    sample_database,
) -> None:
    path, _, _ = sample_database
    providers = _CountingPlanningProviders()
    router = IntentRouter(
        semantic_coverage_gate=_FixedCoverageGate(
            SemanticCoverageDecision(schema_link_gap_spans=("AUM",))
        ),
        hclx_planning_enabled=True,
    )
    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        router=router,
        query_plan_provider=providers,
        grounded_plan_provider=providers,
        hclx_planning_enabled=True,
    ).answer(
        "미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘",
        "planning-provider-schema-shadow",
    )

    assert result.status == "executed"
    assert providers.query_plan_calls == 0
    assert providers.grounded_plan_calls == 0


def test_schema_link_gap_requires_a_trusted_separate_signal() -> None:
    gate = _FixedCoverageGate(SemanticCoverageDecision(schema_link_gap_spans=("운용 비용률",)))
    trace = IntentRouter(semantic_coverage_gate=gate).route_with_planning(
        "운용 비용률이 낮은 국내 ETF 3개를 보여줘.",
        "planning-schema-gap",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.route_decision.reason_code == "capability_executable"
    assert trace.planning_decision.path is PlanningPath.SCHEMA_LINK_SHADOW
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.SCHEMA_LINK_GAP
    assert trace.planning_decision.unresolved_spans == ("운용 비용률",)
    assert trace.planning_decision.reason_code == "schema_link_gap_observed"
    _assert_all_calls_are_closed(trace.planning_decision)


def test_server_hclx_opt_in_never_opens_schema_link_shadow() -> None:
    gate = _FixedCoverageGate(SemanticCoverageDecision(schema_link_gap_spans=("운용 비용률",)))
    trace = IntentRouter(
        semantic_coverage_gate=gate,
        hclx_planning_enabled=True,
    ).route_with_planning(
        "운용 비용률이 낮은 국내 ETF 3개를 보여줘.",
        "planning-schema-gap-hclx-enabled",
    )

    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.planning_decision.path is PlanningPath.SCHEMA_LINK_SHADOW
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.SCHEMA_LINK_GAP
    assert trace.planning_decision.hclx_allowed is False
    _assert_all_calls_are_closed(trace.planning_decision)


def test_policy_cannot_ignore_schema_gap_and_escalate_to_sql() -> None:
    gate = _FixedCoverageGate(SemanticCoverageDecision(schema_link_gap_spans=("운용 비용률",)))
    trace = IntentRouter(
        semantic_coverage_gate=gate,
        planning_policy=_EscalatingPolicy(),
    ).route_with_planning(
        "운용 비용률이 낮은 국내 ETF 3개를 보여줘.",
        "planning-schema-gap-escalation",
    )

    assert trace.route_decision.disposition is RouteDisposition.UNSUPPORTED
    assert trace.route_decision.reason_code == "planning_policy_error"
    assert trace.route_decision.draft.intent is InteractionIntent.UNSUPPORTED
    assert trace.planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
    _assert_all_calls_are_closed(trace.planning_decision)


def test_true_ambiguity_wins_over_schema_link_gap() -> None:
    gate = _FixedCoverageGate(
        SemanticCoverageDecision(
            ambiguity_spans=("수익률 기간",),
            schema_link_gap_spans=("운용 비용률",),
        )
    )
    trace = IntentRouter(semantic_coverage_gate=gate).route_with_planning(
        "수익률과 운용 비용률이 높은 국내 ETF를 보여줘.",
        "planning-precedence",
    )

    assert trace.route_decision.reason_code == "semantic_coverage_incomplete"
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.TRUE_AMBIGUITY
    assert trace.planning_decision.unresolved_spans == ("수익률 기간",)
    _assert_all_calls_are_closed(trace.planning_decision)


def test_unsupported_wins_over_true_ambiguity_and_schema_link_gap() -> None:
    gate = _FixedCoverageGate(
        SemanticCoverageDecision(
            ambiguity_spans=("수익률 기간",),
            unsupported_spans=("매수 주문",),
            schema_link_gap_spans=("운용 비용률",),
        )
    )
    trace = IntentRouter(semantic_coverage_gate=gate).route_with_planning(
        "수익률과 운용 비용률 기준으로 국내 ETF 매수 주문을 실행해줘.",
        "planning-unsupported-precedence",
    )

    assert trace.route_decision.disposition is RouteDisposition.UNSUPPORTED
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.UNSUPPORTED
    assert trace.planning_decision.unresolved_spans == ("매수 주문",)
    _assert_all_calls_are_closed(trace.planning_decision)


def test_planning_only_scan_finds_unsupported_action_inside_clarify_intent() -> None:
    trace = IntentRouter().route_with_planning(
        "괜찮은 국내 ETF 결과를 CSV 파일로 저장해줘.",
        "planning-control-rescan",
    )

    assert trace.route_decision.disposition is RouteDisposition.CLARIFY
    assert trace.route_decision.reason_code == "subjective_condition"
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.UNSUPPORTED
    assert any("CSV" in span for span in trace.planning_decision.unresolved_spans)
    _assert_all_calls_are_closed(trace.planning_decision)


@pytest.mark.parametrize(
    "question",
    [
        "수익률이 높은 국내 ETF",
        "가격이 낮은 국내 ETF",
        "유동성이 좋은 국내 ETF",
        '"가상의 상품" 국내 ETF를 찾아줘',
    ],
)
def test_default_lexical_gate_never_invents_schema_link_gap(question: str) -> None:
    decision = SemanticCoverageGate().evaluate(
        question,
        interaction_intent="search",
        check_exclusions=False,
    )

    assert decision.schema_link_gap_spans == ()


def test_planning_policy_failure_returns_closed_shadow_without_leaking_error() -> None:
    router = IntentRouter(planning_policy=_ExplodingPlanningPolicy())
    trace = router.route_with_planning(
        "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        "planning-error",
    )

    assert trace.route_decision.disposition is RouteDisposition.UNSUPPORTED
    assert trace.route_decision.reason_code == "planning_policy_error"
    assert trace.route_decision.draft.intent is InteractionIntent.UNSUPPORTED
    assert trace.planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
    assert trace.planning_decision.semantic_issue is PlanningSemanticIssue.NONE
    assert trace.planning_decision.reason_code == "planning_policy_error"
    _assert_all_calls_are_closed(trace.planning_decision)
    assert "secret-that-must-not-leak" not in trace.model_dump_json()


def test_planning_policy_failure_blocks_agent_execution_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = IntentRouter(planning_policy=_ExplodingPlanningPolicy())
    agent = _agent_with_exploding_boundaries(router, monkeypatch)

    result = agent.answer(
        "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        "planning-error-no-call",
    )

    assert result.status == "unsupported"
    assert result.decision.reason_code == "planning_policy_error"
    assert result.decision.draft.intent is InteractionIntent.UNSUPPORTED
    assert result.query_plan is None


def test_backend_reuses_first_fail_closed_decision_for_intermittent_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _FailThenSucceedPlanningPolicy()
    router = IntentRouter(planning_policy=policy)
    agent = _agent_with_exploding_boundaries(router, monkeypatch)

    result = execute_answer_request(
        agent,
        BackendAgentRequest(
            request_id="planning-single-route",
            question="1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        ),
    )

    assert policy.calls == 1
    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.UNSUPPORTED
    assert result.response.intent is InteractionIntent.UNSUPPORTED
    assert result.response.query_plan is None


@pytest.mark.parametrize(
    "policy",
    [_InvalidReturnPolicy(), _EscalatingPolicy(), _CopyEscalatingPolicy()],
)
def test_malformed_or_escalating_policy_is_contained(policy: object) -> None:
    trace = IntentRouter(planning_policy=policy).route_with_planning(
        "수익률이 높은 국내 ETF를 보여줘.",
        "planning-invalid-policy",
    )

    assert trace.route_decision.disposition is RouteDisposition.CLARIFY
    assert trace.route_decision.reason_code == "semantic_coverage_incomplete"
    assert trace.planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
    _assert_all_calls_are_closed(trace.planning_decision)


def test_injected_policy_cannot_mutate_the_authoritative_route_snapshot() -> None:
    class MutatingPolicy:
        def decide(self, route_decision, coverage):
            route_decision.draft.product_families[0] = ProductFamily.OVERSEAS_ETP
            return AdaptiveShadowPlanningPolicy().decide(route_decision, coverage)

    trace = IntentRouter(planning_policy=MutatingPolicy()).route_with_planning(
        "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
        "planning-route-mutation",
    )

    assert trace.route_decision.disposition is RouteDisposition.UNSUPPORTED
    assert trace.route_decision.reason_code == "planning_policy_error"
    assert trace.route_decision.draft.product_families == [ProductFamily.DOMESTIC_ETP]
    assert trace.planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
    _assert_all_calls_are_closed(trace.planning_decision)


def test_exact_identity_resolution_recomputes_planning_for_the_final_route(
    sample_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": path})
    original_validate = agent.plan_authority_gate.validate_routed
    observed: dict[str, object] = {}

    def capture(proposal, route_decision, **kwargs):
        observed["route"] = route_decision
        observed["planning"] = kwargs["planning_decision"]
        return original_validate(proposal, route_decision, **kwargs)

    monkeypatch.setattr(agent.plan_authority_gate, "validate_routed", capture)

    result = agent.answer(
        "종목코드 B2의 상세 정보를 알려줘",
        "planning-exact-identity",
    )

    route = observed["route"]
    planning = observed["planning"]
    assert result.status == "executed"
    assert route.reason_code == "exact_identity_family_resolved"
    assert planning.path is PlanningPath.DETERMINISTIC_FAST
    assert planning.product_families == (ProductFamily.OVERSEAS_ETP,)
    assert planning.route_reason_code == route.reason_code


def test_public_route_decision_contract_is_unchanged_by_shadow_metadata() -> None:
    router = IntentRouter()
    question = "1개월 수익률이 높은 국내 ETF 3개를 보여줘."
    trace = router.route_with_planning(question, "planning-contract")
    public_route = router.route(question, "planning-contract")

    assert public_route == trace.route_decision
    assert set(public_route.model_dump(mode="json")) == {
        "schema_version",
        "draft",
        "disposition",
        "reason_code",
        "reason",
        "query_plan_intent",
        "capability_matrix_version",
    }
    assert "planning_decision" not in public_route.model_dump(mode="json")


@pytest.mark.parametrize(
    ("question", "request_id", "expected"),
    [
        (
            "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
            "snapshot-clear",
            {
                "schema_version": "1.0",
                "draft": {
                    "request_id": "snapshot-clear",
                    "question": "1개월 수익률이 높은 국내 ETF 3개를 보여줘.",
                    "intent": "search",
                    "product_families": ["domestic_etp"],
                    "product_mentions": [],
                    "requested_limit": 3,
                },
                "disposition": "execute",
                "reason_code": "capability_executable",
                "reason": "조건 검색 Oracle과 verifier를 사용",
                "query_plan_intent": "search",
                "capability_matrix_version": "2026-07-30.3",
            },
        ),
        (
            "수익률이 높은 국내 ETF를 보여줘.",
            "snapshot-clarify",
            {
                "schema_version": "1.0",
                "draft": {
                    "request_id": "snapshot-clarify",
                    "question": "수익률이 높은 국내 ETF를 보여줘.",
                    "intent": "clarify",
                    "product_families": ["domestic_etp"],
                    "product_mentions": [],
                    "requested_limit": None,
                },
                "disposition": "clarify",
                "reason_code": "semantic_coverage_incomplete",
                "reason": "필드나 기준을 하나로 확정할 수 없는 조건: 수익률, 높은",
                "query_plan_intent": None,
                "capability_matrix_version": "2026-07-30.3",
            },
        ),
        (
            "국내 ETF 매수 주문을 실행해 줘.",
            "snapshot-unsupported",
            {
                "schema_version": "1.0",
                "draft": {
                    "request_id": "snapshot-unsupported",
                    "question": "국내 ETF 매수 주문을 실행해 줘.",
                    "intent": "unsupported",
                    "product_families": ["domestic_etp"],
                    "product_mentions": [],
                    "requested_limit": None,
                },
                "disposition": "unsupported",
                "reason_code": "safety_transaction",
                "reason": "계좌 접근이나 실제 주문·거래 실행은 지원하지 않습니다.",
                "query_plan_intent": None,
                "capability_matrix_version": "2026-07-30.3",
            },
        ),
    ],
)
def test_stage1_shadow_preserves_frozen_legacy_route_payloads(
    question: str,
    request_id: str,
    expected: dict[str, object],
) -> None:
    actual = IntentRouter().route(question, request_id).model_dump(mode="json")

    assert actual == expected


def test_planning_decision_rejects_contradictory_shadow_permissions() -> None:
    common = {
        "product_families": (ProductFamily.DOMESTIC_ETP,),
        "route_reason_code": "semantic_coverage_incomplete",
        "reason_code": "route_requires_clarification",
    }
    with pytest.raises(ValidationError, match="execution authority"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            sql_allowed=True,
            **common,
        )
    with pytest.raises(ValidationError, match="Dense calls"):
        PlanningDecision(
            path=PlanningPath.SCHEMA_LINK_SHADOW,
            semantic_issue=PlanningSemanticIssue.SCHEMA_LINK_GAP,
            unresolved_spans=("운용 비용률",),
            dense_allowed=True,
            **common,
        )
    with pytest.raises(ValidationError, match="deterministic fast path"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            hclx_allowed=True,
            **common,
        )
    allowed_hclx = PlanningDecision(
        path=PlanningPath.DETERMINISTIC_FAST,
        semantic_issue=PlanningSemanticIssue.NONE,
        product_families=(ProductFamily.DOMESTIC_ETP,),
        route_reason_code="capability_executable",
        reason_code="deterministic_route_executable",
        hclx_allowed=True,
        sql_allowed=True,
        compiler_allowed=True,
        oracle_allowed=True,
    )
    assert allowed_hclx.hclx_allowed is True
    with pytest.raises(ValidationError, match="unresolved_spans must be unique"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            unresolved_spans=("수익률", "수익률"),
            **common,
        )
    with pytest.raises(ValidationError, match="requires an unresolved span"):
        PlanningDecision(
            path=PlanningPath.SCHEMA_LINK_SHADOW,
            semantic_issue=PlanningSemanticIssue.SCHEMA_LINK_GAP,
            **common,
        )
    with pytest.raises(ValidationError, match="cannot contain blank"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            unresolved_spans=("   ",),
            **common,
        )
    with pytest.raises(ValidationError, match="cannot emit the grounded model"):
        PlanningDecision(
            path=PlanningPath.GROUNDED_MODEL,
            semantic_issue=PlanningSemanticIssue.NONE,
            **common,
        )
    with pytest.raises(ValidationError, match="resolved planning decision"):
        PlanningDecision(
            path=PlanningPath.DETERMINISTIC_FAST,
            semantic_issue=PlanningSemanticIssue.NONE,
            unresolved_spans=("남은 표현",),
            sql_allowed=True,
            compiler_allowed=True,
            oracle_allowed=True,
            **common,
        )
    with pytest.raises(ValidationError, match="Input should be False"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            enforced=True,
            **common,
        )
    with pytest.raises(ValidationError, match="valid boolean"):
        PlanningDecision(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
            dense_allowed="false",
            **common,
        )
