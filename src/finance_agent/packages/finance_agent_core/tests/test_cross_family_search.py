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
from finance_agent_core.contracts.backend import BackendStatus, routed_result_to_backend
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
    ordinal = router.route(
        "첫째, 국내 ETF를 1개월 수익률 순으로 2개 보여줘. 둘째, 해외 ETF를 AUM 순으로 3개 보여줘.",
        "cross-route-005",
    )
    global_ranking = router.route(
        "국내 ETF와 해외 ETF의 1일 수익률을 직접 비교해 낮은 순 4개를 보여줘.",
        "cross-route-006",
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
    assert ordinal.disposition is RouteDisposition.EXECUTE
    assert ordinal.query_plan_intent is Intent.SEARCH
    assert global_ranking.disposition is RouteDisposition.EXECUTE
    assert global_ranking.query_plan_intent is Intent.SEARCH


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


def test_cross_family_global_top_k_uses_one_common_metric_and_unit(
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
        "국내 ETF와 해외 ETF 가운데 1일 성과를 직접 비교하려고 합니다. "
        "같은 퍼센트 단위로 낮은 순 3개를 보여줘",
        "cross-global-top-k-001",
    )

    assert result.status == "executed"
    assert len(result.products) == 3
    assert sum(len(item.products) for item in result.family_searches) == 3
    ranking_values = [
        next(
            field.normalized_value
            for field in product.fields
            if field.canonical_field == "one_day_return_pct"
        )
        for product in result.products
    ]
    assert ranking_values == sorted(ranking_values)
    assert "통합 정렬했습니다" in result.answer
    assert "결측·UNKNOWN 값은 순위에서 제외했습니다" in result.answer
    assert "global TOP-K" in result.warnings[0]
    response = routed_result_to_backend(result)
    assert response.status is BackendStatus.SUCCESS
    assert [product.product_id for product in response.products] == [
        product.product_id for product in result.products
    ]


def test_cross_family_global_money_ranking_requires_one_currency(
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
        "국내 ETF와 해외 ETF 중 AUM이 높은 상위 3개를 보여줘",
        "cross-global-top-k-002",
    )

    assert result.status == "clarify"
    assert "동일 거래통화 조건" in result.answer


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


def test_cross_family_search_executes_deterministic_plans_concurrently(
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
    agent = _cross_family_agent(sample_database, domestic_sample_database)
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


def test_cross_family_search_executes_explicit_ordinal_family_conditions(
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
        "첫째, 국내 ETF는 1개월 수익률 높은 순으로 2개 보여줘. "
        "둘째, 해외 ETF는 AUM 높은 순으로 3개 보여줘.",
        "cross-search-006",
    )

    assert result.status == "executed"
    assert [len(item.products) for item in result.family_searches] == [2, 3]
    assert [item.query_plan.limit for item in result.family_searches] == [2, 3]
    assert [item.query_plan.ranking[0].field for item in result.family_searches] == [
        "one_month_return_pct",
        "aum",
    ]


def test_cross_family_search_executes_family_topic_asymmetric_conditions(
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
        "cross-search-unscoped-asymmetric",
    )

    assert result.status == "executed"
    assert [item.query_plan.ranking[0].field for item in result.family_searches] == [
        "one_month_return_pct",
        "aum",
    ]


def test_cross_family_search_executes_family_suffix_asymmetric_conditions(
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
        "1개월 수익률 높은 국내 ETF와 AUM이 큰 해외 ETF를 각각 2개 보여줘",
        "cross-search-family-suffix",
    )

    assert result.status == "executed"
    assert [item.query_plan.ranking[0].field for item in result.family_searches] == [
        "one_month_return_pct",
        "aum",
    ]
    assert [item.query_plan.limit for item in result.family_searches] == [2, 2]


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
