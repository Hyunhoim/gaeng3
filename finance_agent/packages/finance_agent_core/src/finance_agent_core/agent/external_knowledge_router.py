from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanCompiler,
    CompositeKnowledgePlanningError,
)
from finance_agent_core.agent.source_selection import SourceSelectionSignals
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.contracts.external_knowledge import CompositeKnowledgeQueryPlan


class ExternalKnowledgeRouteDisposition(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    KNOWLEDGE_SEARCH = "knowledge_search"
    EXECUTE = "execute"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class ExternalKnowledgeRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    disposition: ExternalKnowledgeRouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    plan: CompositeKnowledgeQueryPlan | None = None

    @model_validator(mode="after")
    def validate_route(self) -> ExternalKnowledgeRouteDecision:
        carries_plan = self.disposition in {
            ExternalKnowledgeRouteDisposition.KNOWLEDGE_SEARCH,
            ExternalKnowledgeRouteDisposition.EXECUTE,
        }
        if carries_plan != (self.plan is not None):
            raise ValueError("external knowledge search/execute routes require a plan")
        if self.disposition is ExternalKnowledgeRouteDisposition.EXECUTE:
            assert self.plan is not None
            if not self.plan.execution_authorized:
                raise ValueError("executable external knowledge route lacks authority")
        elif self.plan is not None and self.plan.execution_authorized:
            raise ValueError("candidate knowledge search cannot carry execution authority")
        return self


class ExternalKnowledgeRouter:
    """Separate valid knowledge questions from unsupported/out-of-domain requests."""

    def __init__(
        self,
        *,
        compiler: CompositeKnowledgePlanCompiler | None = None,
        coverage: ApprovedKnowledgeCoverageManifest | None = None,
    ) -> None:
        self.compiler = compiler or CompositeKnowledgePlanCompiler()
        self.coverage = coverage or load_approved_knowledge_coverage_manifest()

    def route(
        self,
        question: str,
        question_id: str,
        intent: IntentAnalysisDraft,
        *,
        source_selection_signals: SourceSelectionSignals | None = None,
    ) -> ExternalKnowledgeRouteDecision:
        if not intent.knowledge_requirements:
            return ExternalKnowledgeRouteDecision(
                disposition=ExternalKnowledgeRouteDisposition.NOT_APPLICABLE,
                reason_code="not_external_knowledge",
                reason="외부 공식 근거가 필요한 지식 요구가 없음",
            )
        try:
            candidate = self.compiler.compile(
                question,
                question_id,
                intent,
                source_selection_signals=source_selection_signals,
            )
        except CompositeKnowledgePlanningError as error:
            return ExternalKnowledgeRouteDecision(
                disposition=ExternalKnowledgeRouteDisposition.CLARIFY,
                reason_code="external_knowledge_plan_incomplete",
                reason=str(error)[:500],
            )

        entries = [
            self.coverage.require(requirement.capability) for requirement in candidate.requirements
        ]
        executable = all(
            entry.runtime_execution_enabled
            and (
                entry.status == "approved_live_official"
                or (
                    entry.status == "approved"
                    and entry.corpus_sha256 is not None
                    and entry.index_sha256 is not None
                )
            )
            for entry in entries
        )
        if not executable:
            return ExternalKnowledgeRouteDecision(
                disposition=ExternalKnowledgeRouteDisposition.KNOWLEDGE_SEARCH,
                reason_code="approved_evidence_required",
                reason=(
                    "정상 금융상품 지식 질문이지만 승인 corpus·관계 index 또는 "
                    "검증된 공식 live source 권한이 없어 아직 실행하지 않음"
                ),
                plan=candidate,
            )
        authorized = CompositeKnowledgeQueryPlan(
            **candidate.model_dump(mode="python", exclude={"execution_authorized"}),
            execution_authorized=True,
        )
        return ExternalKnowledgeRouteDecision(
            disposition=ExternalKnowledgeRouteDisposition.EXECUTE,
            reason_code="external_knowledge_authorized",
            reason="승인 artifact 또는 검증된 공식 live source에 묶인 복합 지식 계획을 실행",
            plan=authorized,
        )


__all__ = [
    "ExternalKnowledgeRouteDecision",
    "ExternalKnowledgeRouteDisposition",
    "ExternalKnowledgeRouter",
]
