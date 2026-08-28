from __future__ import annotations

import calendar
import re
from datetime import date

from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeProductSelection,
    KnowledgeRequirement,
    KnowledgeTimeRange,
    predicate_for_capability,
)
from finance_agent_core.contracts.queryplan import SortDirection
from finance_agent_core.contracts.routing import InteractionIntent


class CompositeKnowledgePlanningError(RuntimeError):
    """Raised when HCX evidence cannot be compiled into a server-owned plan."""


_CAPABILITY_SIGNAL: dict[KnowledgeCapability, re.Pattern[str]] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"편입|보유|구성\s*종목|담(?:은|고\s*있는)", re.IGNORECASE
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|가족사", re.IGNORECASE
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"테마|섹터|산업|연결\s*이력|연관\s*이력", re.IGNORECASE
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"구조|투자\s*전략|운용\s*동향|판매|향후\s*일정", re.IGNORECASE
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험\s*요인|리스크\s*요인", re.IGNORECASE
    ),
}
_RECENT_MONTHS = re.compile(r"최근\s*(\d{1,2})\s*개월", re.IGNORECASE)
_LIMIT = re.compile(r"(?:TOP\s*)?(\d{1,2})\s*(?:개|건)?", re.IGNORECASE)
_AUM = re.compile(r"AUM|순\s*자산|운용\s*자산", re.IGNORECASE)
_ONE_YEAR_RETURN = re.compile(
    r"(?:최근\s*)?(?:1\s*년|일\s*년|12\s*개월|열두\s*달)\s*(?:수익률|성과|실적)",
    re.IGNORECASE,
)
_DESCENDING = re.compile(r"큰|높|많|상위|TOP|내림차순", re.IGNORECASE)
_CAPABILITY_ORDER = {
    KnowledgeCapability.CORPORATE_SUBSIDIARY: 0,
    KnowledgeCapability.PORTFOLIO_HOLDINGS: 1,
    KnowledgeCapability.SECTOR_THEME_HISTORY: 2,
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: 3,
    KnowledgeCapability.PRODUCT_RISK_FACTORS: 4,
}


def _subtract_months(value: date, months: int) -> date:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class CompositeKnowledgePlanCompiler:
    """Validate HCX span hints and compile a non-executable candidate plan."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()

    def _source_ids(self, capability: KnowledgeCapability) -> tuple[str, ...]:
        source_ids = tuple(
            entry.source_id
            for entry in self.registry.entries
            if capability in entry.capabilities
        )
        if not source_ids:
            raise CompositeKnowledgePlanningError(
                f"no approved snapshot-intake source for {capability.value}"
            )
        return source_ids

    def compile(
        self,
        question: str,
        question_id: str,
        intent: IntentAnalysisDraft,
    ) -> CompositeKnowledgeQueryPlan:
        if not question.strip() or not question_id.strip():
            raise ValueError("question and question_id cannot be blank")
        if intent.needs_clarification or not intent.knowledge_requirements:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning requires explicit HCX requirement spans"
            )
        if intent.intent not in {
            InteractionIntent.SEARCH,
            InteractionIntent.EXPLAIN,
        }:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning supports only SEARCH or EXPLAIN"
            )
        unique_keys: set[tuple[KnowledgeCapability, str, str]] = set()
        raw_requirements = []
        for item in intent.knowledge_requirements:
            if item.subject_span not in question or item.relation_span not in question:
                raise CompositeKnowledgePlanningError(
                    "HCX knowledge evidence spans must occur verbatim in the question"
                )
            if not _CAPABILITY_SIGNAL[item.capability].search(item.relation_span):
                raise CompositeKnowledgePlanningError(
                    "HCX knowledge capability is not supported by its relation span"
                )
            key = (item.capability, item.subject_span, item.relation_span)
            if key in unique_keys:
                raise CompositeKnowledgePlanningError("duplicate external knowledge requirement")
            unique_keys.add(key)
            raw_requirements.append(item)
        raw_requirements.sort(key=lambda item: _CAPABILITY_ORDER[item.capability])

        recent = _RECENT_MONTHS.search(question)
        time_range = None
        if recent is not None:
            months = int(recent.group(1))
            if not 1 <= months <= 60:
                raise CompositeKnowledgePlanningError("recent month range is out of bounds")
            time_range = KnowledgeTimeRange(
                start=_subtract_months(self.registry.evidence_cutoff, months),
                end=self.registry.evidence_cutoff,
            )

        families = tuple(sorted(set(intent.product_families), key=lambda item: item.value))
        capability_id = {
            item.capability: f"need_{index}"
            for index, item in enumerate(raw_requirements, start=1)
        }
        compiled_requirements = []
        for index, item in enumerate(raw_requirements, start=1):
            dependencies: tuple[str, ...] = ()
            if (
                item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                and KnowledgeCapability.CORPORATE_SUBSIDIARY in capability_id
            ):
                dependencies = (
                    capability_id[KnowledgeCapability.CORPORATE_SUBSIDIARY],
                )
            elif (
                item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and KnowledgeCapability.PORTFOLIO_HOLDINGS in capability_id
            ):
                dependencies = (capability_id[KnowledgeCapability.PORTFOLIO_HOLDINGS],)
            compiled_requirements.append(
                KnowledgeRequirement(
                    requirement_id=f"need_{index}",
                    capability=item.capability,
                    predicate=predicate_for_capability(item.capability),
                    subject=item.subject_span.strip(),
                    subject_evidence_span=item.subject_span,
                    relation_evidence_span=item.relation_span,
                    product_families=families,
                    time_range=(
                        time_range
                        if item.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
                        else None
                    ),
                    source_registry_ids=self._source_ids(item.capability),
                    depends_on=dependencies,
                )
            )
        requirements = tuple(compiled_requirements)

        product_selection = None
        ranking_fields = [
            field
            for field, pattern in (
                ("aum", _AUM),
                ("one_year_return_pct", _ONE_YEAR_RETURN),
            )
            if pattern.search(question)
        ]
        if len(ranking_fields) > 1:
            raise CompositeKnowledgePlanningError(
                "external product selection contains multiple ranking metrics"
            )
        if ranking_fields:
            if not _DESCENDING.search(question):
                raise CompositeKnowledgePlanningError(
                    "external product selection requires an explicit ranking direction"
                )
            requested = [int(value) for value in _LIMIT.findall(question)]
            limit = requested[-1] if requested else 1
            if not 1 <= limit <= 20:
                raise CompositeKnowledgePlanningError("knowledge result limit is out of bounds")
            product_selection = KnowledgeProductSelection(
                rank_field=ranking_fields[0],
                direction=SortDirection.DESC,
                limit=limit,
            )

        return CompositeKnowledgeQueryPlan(
            question_id=question_id,
            question=question,
            interaction_intent=intent.intent,
            evidence_cutoff=self.registry.evidence_cutoff,
            requirements=requirements,
            product_selection=product_selection,
            join_order=tuple(
                [item.requirement_id for item in requirements]
                + ["official_product_db"]
            ),
        )


__all__ = [
    "CompositeKnowledgePlanCompiler",
    "CompositeKnowledgePlanningError",
]
