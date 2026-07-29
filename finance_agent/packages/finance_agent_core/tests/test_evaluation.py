from collections import Counter

import pytest

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import first_vertical_slice_plan
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.evaluation import load_core_evaluation_suite
from finance_agent_core.evaluation.models import ExpectedDisposition
from finance_agent_core.evaluation.scoring import semantic_checks
from finance_agent_core.execution import PlanExecutionBlockedError


def test_core_suite_is_frozen_and_balanced() -> None:
    loaded = load_core_evaluation_suite()
    suite = loaded.suite

    assert len(suite.cases) == 50
    assert Counter(case.split.value for case in suite.cases) == {
        "development": 40,
        "holdout": 10,
    }
    assert Counter(case.disposition.value for case in suite.cases) == {
        "execute": 42,
        "block": 8,
    }
    assert len(loaded.suite_sha256) == 64


def test_semantic_scoring_ignores_constraint_order() -> None:
    case = load_core_evaluation_suite().suite.cases[0]
    expected = case.expected_plan()
    reordered = expected.model_copy(update={"constraints": list(reversed(expected.constraints))})

    checks = semantic_checks(case, reordered)

    assert checks["constraints"]
    assert checks["plan_exact"]


def test_agent_blocks_unsupported_plan_before_oracle(
    sample_database: tuple[
        object,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database

    class UnsupportedProvider:
        provider_name = "mock"

        def generate_query_plan(self, question: str, question_id: str):
            plan = first_vertical_slice_plan(question_id)
            return plan.model_copy(
                update={
                    "unsupported_conditions": [
                        {
                            "span": "배당수익률",
                            "reason": "지원하지 않음",
                        }
                    ]
                }
            )

    with pytest.raises(PlanExecutionBlockedError, match="unsupported"):
        FinanceAgent(path, UnsupportedProvider()).answer(
            "배당수익률이 높은 ETF",
            "blocked-001",
        )


def test_all_executable_cases_build_valid_expected_plans() -> None:
    suite = load_core_evaluation_suite().suite

    plans = [
        case.expected_plan()
        for case in suite.cases
        if case.disposition is ExpectedDisposition.EXECUTE
    ]

    assert len(plans) == 42
    assert all(plan.question_id.startswith("etp-core-") for plan in plans)


def test_domestic_core_suite_is_frozen_balanced_and_valid() -> None:
    loaded = load_core_evaluation_suite("domestic_etp")
    suite = loaded.suite

    assert suite.suite_id == "domestic-etp-core-50"
    assert Counter(case.split.value for case in suite.cases) == {
        "development": 40,
        "holdout": 10,
    }
    assert Counter(case.disposition.value for case in suite.cases) == {
        "execute": 47,
        "block": 3,
    }
    plans = [
        case.expected_plan("domestic_etp")
        for case in suite.cases
        if case.disposition is ExpectedDisposition.EXECUTE
    ]
    assert len(plans) == 47
    assert all(plan.product_families[0].value == "domestic_etp" for plan in plans)


def test_bond_core_suite_is_frozen_balanced_and_valid() -> None:
    loaded = load_core_evaluation_suite("bond")
    suite = loaded.suite

    assert suite.suite_id == "bond-core-50"
    assert Counter(case.split.value for case in suite.cases) == {
        "development": 40,
        "holdout": 10,
    }
    assert Counter(case.disposition.value for case in suite.cases) == {
        "execute": 47,
        "block": 3,
    }
    plans = [
        case.expected_plan("bond")
        for case in suite.cases
        if case.disposition is ExpectedDisposition.EXECUTE
    ]
    assert len(plans) == 47
    assert all(plan.product_families[0].value == "bond" for plan in plans)


def test_fund_core_suite_is_frozen_balanced_scoped_and_valid() -> None:
    loaded = load_core_evaluation_suite("fund")
    suite = loaded.suite

    assert suite.suite_id == "fund-core-50"
    assert Counter(case.split.value for case in suite.cases) == {
        "development": 40,
        "holdout": 10,
    }
    assert Counter(case.disposition.value for case in suite.cases) == {
        "execute": 44,
        "block": 6,
    }
    plans = [case.expected_plan("fund") for case in suite.cases]
    assert all(plan.product_families[0].value == "fund" for plan in plans)
    for plan in plans:
        public_scope = [
            constraint for constraint in plan.constraints if constraint.field == "public_offering"
        ]
        assert len(public_scope) == 1
        assert public_scope[0].value is True
        aum_fields = {constraint.field for constraint in plan.constraints} | {
            ranking.field for ranking in plan.ranking
        }
        if "aum" in aum_fields:
            assert any(
                constraint.field == "trading_currency" and constraint.operator.value == "eq"
                for constraint in plan.constraints
            )
