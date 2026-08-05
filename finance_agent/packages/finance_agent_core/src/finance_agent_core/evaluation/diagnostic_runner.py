from __future__ import annotations

from typing import Protocol

from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.config.capability import load_capability_matrix
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)


class Router(Protocol):
    def route(self, question: str, request_id: str) -> RouteDecision: ...


class PreRouterSnapshot:
    """Executable replay of the pre-router linker contract.

    Before the shared router existed, the common linker forced every recognized
    question into a SEARCH QueryPlan. This adapter preserves that behavior for
    an honest before/after diagnostic; it is not used by the Agent.
    """

    def route(self, question: str, request_id: str) -> RouteDecision:
        hints = build_lexical_hints(question)
        family_name = hints["product_family"]
        families = [] if family_name is None else [ProductFamily(family_name)]
        draft = MinimalQueryDraft(
            request_id=request_id,
            question=question,
            intent=InteractionIntent.SEARCH,
            product_families=families,
            product_mentions=[],
            requested_limit=hints["limit"],
        )
        if not families:
            return RouteDecision(
                draft=draft,
                disposition=RouteDisposition.CLARIFY,
                reason_code="pre_router_missing_family",
                reason="기존 linker가 상품군을 확정하지 못함",
                query_plan_intent=None,
                capability_matrix_version="pre-router",
            )
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="pre_router_forced_search",
            reason="기존 공통 linker가 모든 질문을 search로 강제",
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version=load_capability_matrix().matrix_version,
        )
