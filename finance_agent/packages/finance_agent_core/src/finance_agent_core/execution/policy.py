from __future__ import annotations

from finance_agent_core.contracts.queryplan import Intent, QueryPlan


class PlanExecutionBlockedError(ValueError):
    """Raised when a valid QueryPlan still requires clarification or unsupported work."""


def require_executable_search(plan: QueryPlan) -> None:
    blockers: list[str] = []
    if plan.intent is not Intent.SEARCH:
        blockers.append(f"intent {plan.intent.value!r} is not executable yet")
    if plan.ambiguities:
        blockers.append(f"{len(plan.ambiguities)} ambiguity item(s) require clarification")
    if plan.unsupported_conditions:
        blockers.append(
            f"{len(plan.unsupported_conditions)} unsupported condition(s) require a safe refusal"
        )
    if blockers:
        raise PlanExecutionBlockedError("; ".join(blockers))
