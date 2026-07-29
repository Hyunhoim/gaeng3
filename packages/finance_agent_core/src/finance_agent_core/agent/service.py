from __future__ import annotations

from pathlib import Path

from finance_agent_core.agent.providers import QueryPlanProvider
from finance_agent_core.answering import (
    AnswerComposition,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.domain import AgentResponse
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
    render_verified_search,
    require_executable_search,
)
from finance_agent_core.storage import connect_read_only, load_all_records


class FinanceAgent:
    def __init__(
        self,
        database_path: str | Path,
        provider: QueryPlanProvider,
        answer_provider: GroundedAnswerProvider | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.provider = provider
        self.answer_provider = answer_provider
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()

    def answer(self, question: str, request_id: str) -> AgentResponse:
        response, _ = self.answer_with_composition(question, request_id)
        return response

    def answer_with_composition(
        self,
        question: str,
        request_id: str,
    ) -> tuple[AgentResponse, AnswerComposition | None]:
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not request_id.strip():
            raise ValueError("request_id cannot be blank")
        plan = self.provider.generate_query_plan(question, request_id)
        if plan.question_id != request_id:
            raise ValueError("provider changed the trusted request_id")
        require_executable_search(plan)
        executed = self.oracle.execute(plan)
        with connect_read_only(self.database_path) as connection:
            universe = load_all_records(connection)
        verified = self.verifier.verify(plan, executed, universe)
        products = build_product_evidence(plan, verified)
        answer, warnings = render_verified_search(plan, verified)
        composition: AnswerComposition | None = None
        if self.answer_provider is not None:
            composition = compose_grounded_answer(
                question=question,
                plan=plan,
                verified=verified,
                products=products,
                provider=self.answer_provider,
            )
            answer = composition.answer
        response = AgentResponse(
            request_id=request_id,
            provider=self.provider.provider_name,
            answer=answer,
            query_plan=plan,
            candidate_count=verified.candidate_count,
            products=products,
            warnings=warnings,
            source_manifest=verified.manifest,
        )
        return response, composition
