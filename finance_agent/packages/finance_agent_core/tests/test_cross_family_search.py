from __future__ import annotations

from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest

from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent
from finance_agent_core.answering import (
    ExpectedGroundedAnswerProvider,
    GroundedAnswerContext,
    GroundedAnswerDraft,
)
from finance_agent_core.contracts.backend import (
    BackendAnswerMode,
    BackendStatus,
    routed_result_to_backend,
)
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.deadline import (
    RequestDeadline,
    bind_request_deadline,
    current_request_deadline,
)
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedDomesticEtpRecord,
    NormalizedOverseasEtpRecord,
)
from finance_agent_core.evaluation.cross_family_search import (
    load_cross_family_search_suite,
)
from finance_agent_core.execution import PlanAuthorityCode, PlanAuthorityError
from finance_agent_core.observability import (
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    MetricCounter,
)


def _cross_family_agent(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
    **kwargs: object,
) -> RoutedFinanceAgent:
    overseas_path, _, _ = sample_database
    domestic_path, _, _ = domestic_sample_database
    return RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_path,
            ProductFamily.OVERSEAS_ETP: overseas_path,
        },
        **kwargs,
    )


class RecordingAnswerProvider:
    def __init__(self) -> None:
        self.contexts: list[GroundedAnswerContext] = []
        self._delegate = ExpectedGroundedAnswerProvider()

    @property
    def provider_name(self) -> str:
        return "recording_expected"

    @property
    def model_name(self) -> str:
        return "qwen-test"

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft:
        self.contexts.append(context)
        return self._delegate.generate_grounded_answer(context)


def test_cross_family_public_regression_suite_covers_safety_states() -> None:
    loaded = load_cross_family_search_suite()

    assert loaded.suite.suite_id == "cross-family-search-v1-4"
    assert {case.category for case in loaded.suite.cases} == {
        "all_success",
        "partial_success",
        "all_not_found",
        "forbidden_cross_family_comparison",
    }
    assert set(loaded.suite.data) == {
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }


def test_router_executes_only_independent_multi_family_search() -> None:
    router = IntentRouter()

    search = router.route(
        "국내 ETF와 해외 ETF를 각각 3개 보여줘",
        "cross-route-001",
    )
    comparison = router.route(
        "국내 ETF와 해외 ETF의 수익률을 비교해줘",
        "cross-route-002",
    )
    domestic_region = router.route(
        "미국에 투자하는 국내 ETF를 3개 보여줘",
        "cross-route-003",
    )
    bond_and_etp = router.route(
        "국내채권과 해외 ETF를 각각 3개 보여줘",
        "cross-route-004",
    )

    assert search.disposition is RouteDisposition.EXECUTE
    assert search.query_plan_intent is Intent.SEARCH
    assert search.draft.product_families == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert comparison.disposition is RouteDisposition.CLARIFY
    assert comparison.reason_code == "ambiguous_product_family"
    assert domestic_region.draft.product_families == [ProductFamily.DOMESTIC_ETP]
    assert bond_and_etp.draft.product_families == [
        ProductFamily.BOND,
        ProductFamily.OVERSEAS_ETP,
    ]


def test_cross_family_search_preserves_verified_family_boundaries_and_backend_dto(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    memory = InMemoryAuditSink(max_events=1_000)
    audit = BoundedAsyncAuditSink(memory, queue_capacity=256)
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        audit_sink=audit,
    ).answer(
        "국내 ETF와 해외 ETF를 각각 3개 보여줘",
        "cross-search-001",
    )
    assert audit.close(timeout_seconds=2)

    assert result.status == "executed"
    assert result.query_plan is None
    assert result.source_manifest is None
    assert result.candidate_count == 16
    assert [item.product_family for item in result.family_searches] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert [item.status for item in result.family_searches] == ["success", "success"]
    assert [item.candidate_count for item in result.family_searches] == [6, 10]
    assert [len(item.products) for item in result.family_searches] == [3, 3]
    assert len(result.products) == 6
    assert "직접 비교·합산·우열 판단은 수행하지 않았습니다" in result.answer

    response = routed_result_to_backend(result)

    assert response.status is BackendStatus.SUCCESS
    assert response.query_plan is None
    assert response.source_manifest is None
    assert response.candidate_count == 16
    assert [item.candidate_count for item in response.family_searches] == [6, 10]
    assert [item.dataset for item in response.source_manifests] == [
        "domestic_etp",
        "overseas_etp",
    ]
    assert len(response.products) == 6
    assert response.citations
    counters = audit.metrics.snapshot().counters
    assert counters[MetricCounter.EVIDENCE_EXPECTED.value] == 1
    assert counters[MetricCounter.EVIDENCE_PRESENT.value] == 1
    assert counters.get(MetricCounter.EVIDENCE_INCOMPLETE.value, 0) == 0


def test_cross_family_search_preserves_success_when_one_family_is_empty(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
    ).answer(
        "국내 ETF와 해외 ETF 중 채권형 상품을 각각 3개 보여줘",
        "cross-search-002",
    )

    assert result.status == "executed"
    assert [item.status for item in result.family_searches] == [
        "not_found",
        "success",
    ]
    assert [item.candidate_count for item in result.family_searches] == [0, 10]
    assert result.candidate_count == 10
    assert len(result.products) == 3
    assert result.products == result.family_searches[1].products
    assert routed_result_to_backend(result).status is BackendStatus.SUCCESS


def test_cross_family_search_returns_not_found_only_when_every_family_is_empty(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
    ).answer(
        "국내 ETF와 해외 ETF 중 유럽 상품을 각각 3개 보여줘",
        "cross-search-003",
    )
    response = routed_result_to_backend(result)

    assert result.status == "executed"
    assert result.candidate_count == 0
    assert [item.status for item in result.family_searches] == [
        "not_found",
        "not_found",
    ]
    assert result.products == []
    assert response.status is BackendStatus.NOT_FOUND
    assert response.family_searches
    assert response.source_manifests
    assert response.citations == []


def test_cross_family_partial_success_calls_only_non_empty_family_provider(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    provider = RecordingAnswerProvider()
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=provider,
    ).answer(
        "국내 ETF와 해외 ETF 중 채권형 상품을 각각 3개 보여줘",
        "cross-grounded-partial",
    )

    assert [context.source_manifest.dataset for context in provider.contexts] == ["overseas_etp"]
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer.count("상품별 근거 해설:") == 1


def test_cross_family_all_empty_skips_answer_provider(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    provider = RecordingAnswerProvider()
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=provider,
    ).answer(
        "국내 ETF와 해외 ETF 중 유럽 상품을 각각 3개 보여줘",
        "cross-grounded-empty",
    )

    assert provider.contexts == []
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "deterministic"
    assert result.answer_composition.verification.passed


def test_cross_family_search_skips_query_plan_provider_and_executes_concurrently(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    class ForbiddenQueryPlanProvider:
        def generate_query_plan(self, question: str, question_id: str):
            raise AssertionError("multi-family SEARCH must not call the QueryPlan provider")

    agent = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        query_plan_provider=ForbiddenQueryPlanProvider(),
    )
    original = agent._execute_family_search
    barrier = Barrier(2)
    lock = Lock()
    thread_ids: set[int] = set()
    deadline = RequestDeadline.after(5)

    def synchronized_execute(search, validated_plan):
        assert current_request_deadline() is deadline
        with lock:
            thread_ids.add(get_ident())
        barrier.wait(timeout=3)
        return original(search, validated_plan)

    agent._execute_family_search = synchronized_execute  # type: ignore[method-assign]
    with bind_request_deadline(deadline):
        result = agent.answer(
            "국내 ETF와 해외 ETF를 각각 2개 보여줘",
            "cross-search-004",
        )

    assert result.status == "executed"
    assert len(thread_ids) == 2
    assert any("QueryPlan 모델을 호출하지 않고" in warning for warning in result.warnings)


def test_cross_family_search_generates_only_from_isolated_family_evidence(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    provider = RecordingAnswerProvider()
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=provider,
    ).answer(
        "국내 ETF와 해외 ETF를 각각 2개 보여줘",
        "cross-grounded-001",
    )
    response = routed_result_to_backend(result)

    assert [context.source_manifest.dataset for context in provider.contexts] == [
        "domestic_etp",
        "overseas_etp",
    ]
    assert provider.contexts[0].question == "국내 ETF를 2개 보여줘"
    assert provider.contexts[1].question == "해외 ETF를 2개 보여줘"
    assert [context.query_plan.product_families[0].value for context in provider.contexts] == [
        context.source_manifest.dataset for context in provider.contexts
    ]
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer_composition.verification.passed
    assert result.answer.count("상품별 근거 해설:") == 2
    assert response.answer_mode is BackendAnswerMode.LLM_GROUNDED
    assert response.provider_model == "qwen-test"
    assert not response.fallback_used


def test_cross_family_search_falls_back_entire_answer_on_provider_failure(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    class SecondCallFails(RecordingAnswerProvider):
        def generate_grounded_answer(
            self,
            context: GroundedAnswerContext,
        ) -> GroundedAnswerDraft:
            if self.contexts:
                raise RuntimeError("second family unavailable")
            return super().generate_grounded_answer(context)

    provider = SecondCallFails()
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=provider,
    ).answer(
        "국내 ETF와 해외 ETF를 각각 2개 보여줘",
        "cross-grounded-002",
    )
    response = routed_result_to_backend(result)

    assert result.answer_composition is not None
    assert result.answer_composition.mode == "deterministic_fallback"
    assert not result.answer_composition.verification.passed
    assert "상품별 근거 해설:" not in result.answer
    assert any(
        "second family unavailable" in violation
        for violation in result.answer_composition.verification.violations
    )
    assert response.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK
    assert response.fallback_used


def test_cross_family_search_rejects_cross_family_language_from_model(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    class CrossFamilyLanguageProvider(RecordingAnswerProvider):
        def generate_grounded_answer(
            self,
            context: GroundedAnswerContext,
        ) -> GroundedAnswerDraft:
            draft = super().generate_grounded_answer(context)
            return draft.model_copy(update={"lead": "해외 ETF보다 국내 ETF가 더 높은 결과입니다."})

    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=CrossFamilyLanguageProvider(),
    ).answer(
        "국내 ETF와 해외 ETF를 각각 2개 보여줘",
        "cross-grounded-003",
    )

    assert result.answer_composition is not None
    assert result.answer_composition.mode == "deterministic_fallback"
    assert not result.answer_composition.verification.checks["family_prose_isolated"]
    assert not result.answer_composition.verification.checks["no_cross_family_operation"]
    assert "해외 ETF보다" not in result.answer


def test_cross_family_control_route_never_calls_answer_provider(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    provider = RecordingAnswerProvider()
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
        answer_provider=provider,
    ).answer(
        "국내 ETF와 해외 ETF의 수익률을 비교해줘",
        "cross-grounded-004",
    )

    assert result.status == "clarify"
    assert provider.contexts == []
    assert result.answer_composition is None


def test_cross_family_search_fails_closed_before_execution_when_database_is_missing(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    domestic_path, _, _ = domestic_sample_database
    result = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: domestic_path}).answer(
        "국내 ETF와 해외 ETF를 각각 3개 보여줘",
        "cross-search-005",
    )

    assert result.status == "unsupported"
    assert result.candidate_count is None
    assert result.family_searches == []
    assert result.products == []
    assert "database path is not configured" in result.answer


def test_cross_family_search_clarifies_asymmetric_family_conditions(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    result = _cross_family_agent(
        sample_database,
        domestic_sample_database,
    ).answer(
        "국내 ETF는 1개월 수익률 높은 순, 해외 ETF는 AUM 높은 순으로 보여줘",
        "cross-search-006",
    )

    assert result.status == "clarify"
    assert result.family_searches == []
    assert result.products == []
    assert "서로 다른 조건은 아직 지원하지 않습니다" in result.answer


def test_cross_family_authorizes_the_whole_batch_before_any_oracle_call(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _cross_family_agent(sample_database, domestic_sample_database)
    original_validate = agent.plan_authority_gate.validate_routed
    validation_calls = 0
    oracle_calls = 0

    def fail_second_validation(proposal, route_decision, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "synthetic second-child rejection",
            )
        return original_validate(proposal, route_decision, **kwargs)

    class CountingOracle:
        def __init__(self, database_path):
            del database_path

        def execute(self, validated_plan):
            nonlocal oracle_calls
            oracle_calls += 1
            raise AssertionError("Oracle must not run after partial batch authorization")

    monkeypatch.setattr(
        agent.plan_authority_gate,
        "validate_routed",
        fail_second_validation,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.routed_service.SQLiteOracle",
        CountingOracle,
    )

    with pytest.raises(PlanAuthorityError) as raised:
        agent.answer(
            "국내 ETF와 해외 ETF를 각각 2개 보여줘",
            "cross-authority-all-or-none",
        )

    assert raised.value.code is PlanAuthorityCode.ROUTE_MISMATCH
    assert validation_calls == 2
    assert oracle_calls == 0
