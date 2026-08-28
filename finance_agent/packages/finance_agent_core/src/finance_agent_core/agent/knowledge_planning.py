from __future__ import annotations

import calendar
import re
from datetime import date

from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.agent.source_selection import (
    GeneralizedOfficialSourceSelector,
    SourceSelectionSignals,
)
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
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(r"자회사|계열사|가족사", re.IGNORECASE),
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
_EXPLICIT_MONTH_RANGE = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*[~～-]\s*(\d{1,2})\s*월",
    re.IGNORECASE,
)
_EXPLICIT_MONTH = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월(?:\s*말)?",
    re.IGNORECASE,
)
_LIMIT = re.compile(
    r"(?:TOP\s*(?P<top>\d{1,2})|(?P<count>\d{1,2})\s*(?:개|건))",
    re.IGNORECASE,
)
_AUM = re.compile(r"AUM|순\s*자산|운용\s*자산", re.IGNORECASE)
_ONE_YEAR_RETURN = re.compile(
    r"(?:최근\s*)?(?:1\s*년|일\s*년|12\s*개월|열두\s*달)\s*(?:수익률|성과|실적)",
    re.IGNORECASE,
)
_DESCENDING = re.compile(r"큰|높|많|상위|TOP|내림차순", re.IGNORECASE)
_REQUESTED_PRODUCT_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "aum": _AUM,
    "investment_region": re.compile(r"투자\s*지역|지역", re.IGNORECASE),
    "manager": re.compile(r"운용사|자산\s*운용사|발행사", re.IGNORECASE),
    "product_name": re.compile(r"상품명|상품\s*이름|상품\s*정보", re.IGNORECASE),
    "risk_level": re.compile(r"위험\s*등급", re.IGNORECASE),
    "ticker": re.compile(r"티커|ticker", re.IGNORECASE),
}
_CORPORATE_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80})\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)(?:인|인\s*)?\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80})",
    re.IGNORECASE,
)
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
        self.source_selector = GeneralizedOfficialSourceSelector(self.registry)

    def _source_ids(
        self,
        capability: KnowledgeCapability,
        *,
        question: str,
        subject: str,
        signals: SourceSelectionSignals | None,
    ) -> tuple[str, ...]:
        try:
            return self.source_selector.select_source_ids(
                capability,
                question=question,
                subject=subject,
                signals=signals,
            )
        except ValueError as error:
            raise CompositeKnowledgePlanningError(
                f"no approved snapshot-intake source for {capability.value}"
            ) from error

    def compile(
        self,
        question: str,
        question_id: str,
        intent: IntentAnalysisDraft,
        *,
        source_selection_signals: SourceSelectionSignals | None = None,
    ) -> CompositeKnowledgeQueryPlan:
        if not question.strip() or not question_id.strip():
            raise ValueError("question and question_id cannot be blank")
        if intent.needs_clarification or not intent.knowledge_requirements:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning requires explicit HCX requirement spans"
            )
        if intent.intent not in {
            InteractionIntent.SEARCH,
            InteractionIntent.DETAIL,
            InteractionIntent.COMPARE,
            InteractionIntent.EXPLAIN,
        }:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning supports only SEARCH, DETAIL, COMPARE, or EXPLAIN"
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
        else:
            explicit_range = _EXPLICIT_MONTH_RANGE.search(question)
            explicit_month = _EXPLICIT_MONTH.search(question)
            if explicit_range is not None:
                year = int(explicit_range.group(1))
                start_month = int(explicit_range.group(2))
                end_month = int(explicit_range.group(3))
                if not 1 <= start_month <= end_month <= 12:
                    raise CompositeKnowledgePlanningError(
                        "explicit evidence month range is invalid"
                    )
                time_range = KnowledgeTimeRange(
                    start=date(year, start_month, 1),
                    end=date(year, end_month, calendar.monthrange(year, end_month)[1]),
                )
            elif explicit_month is not None:
                year = int(explicit_month.group(1))
                month = int(explicit_month.group(2))
                if not 1 <= month <= 12:
                    raise CompositeKnowledgePlanningError("explicit evidence month is invalid")
                time_range = KnowledgeTimeRange(
                    start=date(year, month, 1),
                    end=date(year, month, calendar.monthrange(year, month)[1]),
                )
        if time_range is not None and time_range.end > self.registry.evidence_cutoff:
            time_range = KnowledgeTimeRange(
                start=time_range.start,
                end=self.registry.evidence_cutoff,
            )

        families = tuple(sorted(set(intent.product_families), key=lambda item: item.value))
        capability_id = {
            item.capability: f"need_{index}" for index, item in enumerate(raw_requirements, start=1)
        }
        compiled_requirements = []
        for index, item in enumerate(raw_requirements, start=1):
            subject_span = item.subject_span
            relation_span = item.relation_span
            if item.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY:
                corporate_pair = _CORPORATE_PAIR.search(question)
                if corporate_pair is not None:
                    subject_span = corporate_pair.group("parent")
                    relation_span = corporate_pair.group(0)
            dependencies: tuple[str, ...] = ()
            if (
                item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                and KnowledgeCapability.CORPORATE_SUBSIDIARY in capability_id
            ):
                dependencies = (capability_id[KnowledgeCapability.CORPORATE_SUBSIDIARY],)
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
                    subject=subject_span.strip(),
                    search_aliases=tuple(sorted(item.search_aliases)),
                    subject_evidence_span=subject_span,
                    relation_evidence_span=relation_span,
                    product_families=families,
                    time_range=(
                        time_range
                        if item.capability
                        in {
                            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                            KnowledgeCapability.PORTFOLIO_HOLDINGS,
                            KnowledgeCapability.SECTOR_THEME_HISTORY,
                        }
                        else None
                    ),
                    source_registry_ids=self._source_ids(
                        item.capability,
                        question=question,
                        subject=subject_span,
                        signals=source_selection_signals,
                    ),
                    depends_on=dependencies,
                )
            )
        requirements = tuple(compiled_requirements)

        product_selection = None
        ranking_fields = (
            [
                field
                for field, pattern in (
                    ("aum", _AUM),
                    ("one_year_return_pct", _ONE_YEAR_RETURN),
                )
                if pattern.search(question)
            ]
            if _DESCENDING.search(question)
            else []
        )
        if len(ranking_fields) > 1:
            raise CompositeKnowledgePlanningError(
                "external product selection contains multiple ranking metrics"
            )
        if ranking_fields:
            if not _DESCENDING.search(question):
                raise CompositeKnowledgePlanningError(
                    "external product selection requires an explicit ranking direction"
                )
            requested = [
                int(match.group("top") or match.group("count"))
                for match in _LIMIT.finditer(question)
            ]
            limit = requested[-1] if requested else 1
            if not 1 <= limit <= 20:
                raise CompositeKnowledgePlanningError("knowledge result limit is out of bounds")
            product_selection = KnowledgeProductSelection(
                rank_field=ranking_fields[0],
                direction=SortDirection.DESC,
                limit=limit,
            )
        requested_product_fields = tuple(
            sorted(
                field
                for field, pattern in _REQUESTED_PRODUCT_FIELD_PATTERNS.items()
                if pattern.search(question)
            )
        )

        return CompositeKnowledgeQueryPlan(
            question_id=question_id,
            question=question,
            interaction_intent=intent.intent,
            evidence_cutoff=self.registry.evidence_cutoff,
            requirements=requirements,
            product_selection=product_selection,
            requested_product_fields=requested_product_fields,
            join_order=tuple(
                [item.requirement_id for item in requirements] + ["official_product_db"]
            ),
        )


__all__ = [
    "CompositeKnowledgePlanCompiler",
    "CompositeKnowledgePlanningError",
]
