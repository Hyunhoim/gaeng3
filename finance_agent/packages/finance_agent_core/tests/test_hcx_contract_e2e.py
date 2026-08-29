"""Regression guards for HCLX execution contracts retired from final Runtime.

The filename remains stable because historical release manifests reference it.
Only the blocking contract is retained; the old QueryPlan and grounded-planning
execution tests were removed with their Runtime implementations.
"""

from __future__ import annotations

import pytest

from finance_agent_core.agent import providers
from finance_agent_core.agent.routed_service import RoutedFinanceAgent


@pytest.mark.parametrize(
    "retired_seam",
    [
        "query_plan_provider",
        "grounded_plan_provider",
        "answer_provider",
        "hclx_planning_enabled",
    ],
)
def test_retired_hclx_planning_and_answer_seams_fail_closed(retired_seam: str) -> None:
    value: object = True if retired_seam == "hclx_planning_enabled" else object()
    with pytest.raises(ValueError, match="retired from Runtime"):
        RoutedFinanceAgent({}, **{retired_seam: value})


def test_retired_hclx_provider_classes_are_not_publicly_exported() -> None:
    assert not hasattr(providers, "HyperClovaXQueryPlanProvider")
    assert not hasattr(providers, "HyperClovaXFundComparisonDraftProvider")
