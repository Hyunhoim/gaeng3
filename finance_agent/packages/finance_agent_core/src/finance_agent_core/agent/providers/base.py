from __future__ import annotations

from typing import Literal, Protocol

from finance_agent_core.contracts.queryplan import QueryPlan


class QueryPlanProvider(Protocol):
    @property
    def provider_name(self) -> Literal["mock", "local_test", "hyperclova"]: ...

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan: ...
