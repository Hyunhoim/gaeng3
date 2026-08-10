from __future__ import annotations

from pathlib import Path

from finance_agent_core.agent.providers import QueryPlanProvider
from finance_agent_core.agent.safety import SafetyEnvelope
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.answering import (
    AnswerComposition,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.contracts.queryplan import Intent
from finance_agent_core.domain import AgentResponse
from finance_agent_core.execution import (
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteOracle,
    build_product_comparison,
    build_product_evidence,
    render_verified_search,
    require_executable_comparison,
    require_executable_search,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)
from finance_agent_core.storage import RecordSnapshotCache


class FinanceAgent:
    def __init__(
        self,
        database_path: str | Path,
        provider: QueryPlanProvider,
        answer_provider: GroundedAnswerProvider | None = None,
        record_cache: RecordSnapshotCache | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.provider = provider
        self.answer_provider = answer_provider
        self.record_cache = record_cache or RecordSnapshotCache(max_entries=1)
        self._record_cache_enabled = record_cache is not None
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        self.safety_envelope = SafetyEnvelope()
        self.semantic_coverage_gate = SemanticCoverageGate()

    def answer(self, question: str, request_id: str) -> AgentResponse:
        response, _ = self.answer_with_composition(question, request_id)
        return response

    def answer_with_composition(
        self,
        question: str,
        request_id: str,
    ) -> tuple[AgentResponse, AnswerComposition | None]:
        safety = self.safety_envelope.evaluate(question)
        question = safety.normalized_question
        if not question:
            raise ValueError("question cannot be blank")
        if not request_id.strip():
            raise ValueError("request_id cannot be blank")
        if safety.blocked:
            raise PlanExecutionBlockedError(
                f"safety envelope blocked {safety.gate.value}: {safety.reason}"
            )
        # The legacy agent has no deterministic compare/explain parser to
        # resolve otherwise ambiguous wording, so it must apply the complete
        # semantic gate before delegating to an unconstrained plan provider.
        coverage = self.semantic_coverage_gate.evaluate(question)
        if coverage.blocked:
            spans = [*coverage.unsupported_spans, *coverage.ambiguity_spans]
            raise PlanExecutionBlockedError(
                "semantic coverage gate blocked unsupported/ambiguous request spans: "
                + ", ".join(spans)
            )
        plan = self.provider.generate_query_plan(question, request_id)
        if plan.question_id != request_id:
            raise ValueError("provider changed the trusted request_id")
        if plan.intent is Intent.COMPARE:
            require_executable_comparison(plan)
        else:
            require_executable_search(plan)
        executed = self.oracle.execute(plan)
        universe = (
            None
            if plan.intent is Intent.COMPARE
            else (
                self.record_cache.get(self.database_path).records
                if self._record_cache_enabled
                else load_projected_verifier_records(self.database_path, plan)
            )
        )
        verified = self.verifier.verify(plan, executed, universe)
        products = build_product_evidence(plan, verified)
        if plan.intent is Intent.COMPARE:
            comparison = build_product_comparison(plan, verified, products)
            verified = comparison.verified
            products = list(comparison.products)
        answer, warnings = render_verified_search(plan, verified, products)
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
