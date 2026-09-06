from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import CompositeKnowledgeQueryPlan
from finance_agent_core.contracts.routing import InteractionIntent


class _AcquisitionSummary(Protocol):
    search_query_count: int
    discovered_candidate_count: int
    failure_codes: Sequence[str]


ExternalFailureKind = Literal[
    "cache_miss",
    "exact_evidence_missing",
    "dependency_incomplete",
    "provided_data_basis_missing",
    "bounded_retrieval_incomplete",
    "release_evidence_unavailable",
    "general_incomplete",
]


_CAPABILITY_LABELS: dict[KnowledgeCapability, str] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: "구성종목·편입 관계",
    KnowledgeCapability.CORPORATE_SUBSIDIARY: "기업·자회사 관계",
    KnowledgeCapability.SECTOR_THEME_HISTORY: "테마·연결 이력",
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: "펀드 구조·투자전략·동향",
    KnowledgeCapability.PRODUCT_RISK_FACTORS: "상품 위험요인",
}

_RELEASE_EVIDENCE_CODES = frozenset(
    {
        "approved_evidence_required",
        "external_knowledge_runtime_unavailable",
        "sealed_evidence_not_release_authorized",
    }
)
_PROVIDED_DATA_BASIS_CODES = frozenset(
    {
        "provided_db_numeric_ranking_basis_empty",
        "provided_db_ranking_unavailable",
        "verified_evidence_date_ordering_unavailable",
    }
)
_DEPENDENCY_CODES = frozenset(
    {
        "dependency_targets_partially_verified",
        "verified_dependency_entity_missing",
        "verified_relation_chain_incomplete",
    }
)
_BOUNDED_RETRIEVAL_CODES = frozenset(
    {
        "acquisition_deadline_exceeded",
        "credential_unavailable",
        "deadline_exceeded",
        "external_search_budget_exhausted",
        "official_discovery_failed",
        "provider_error",
        "provider_response_invalid",
        "result_window_incomplete",
    }
)
_EXACT_EVIDENCE_CODES = frozenset(
    {
        "all_candidates_rejected",
        "cached_snapshot_contract_rejected",
        "live_exact_evidence_span_missing",
        "linked_document_fetch_failed",
        "linked_document_source_not_selected",
        "linked_document_source_rejected",
    }
)


@dataclass(frozen=True, slots=True)
class ExternalCannotVerifyNarrative:
    """Safe public explanation for one incomplete external evidence chain.

    Internal failure codes select a bounded explanation, but are never copied
    into the public answer.  Every subject and relation label comes from the
    already validated typed plan, so this layer cannot invent a product or an
    external fact.
    """

    failure_kind: ExternalFailureKind
    requested_scope: str
    answer: str


def _requested_scope(plan: CompositeKnowledgeQueryPlan) -> str:
    steps: list[str] = []
    previous_subject: str | None = None
    for requirement in plan.requirements:
        subject = " ".join(requirement.subject.split())
        label = _CAPABILITY_LABELS[requirement.capability]
        if subject == previous_subject and steps:
            step = label
        else:
            step = f"‘{subject}’의 {label}"
        if step not in steps:
            steps.append(step)
        previous_subject = subject

    scope = " → ".join(steps) if steps else "요청하신 외부 관계"
    ranges = {
        (
            requirement.time_range.start,
            requirement.time_range.end,
        )
        for requirement in plan.requirements
        if requirement.time_range is not None
    }
    if len(ranges) == 1:
        start, end = next(iter(ranges))
        period = (
            f"{start.isoformat()}~{end.isoformat()}"
            if start is not None
            else f"{end.isoformat()} 기준"
        )
        scope = f"{scope}({period})"
    return scope


def _failure_kind(
    reason_codes: frozenset[str],
    acquisitions: tuple[_AcquisitionSummary, ...],
) -> ExternalFailureKind:
    if reason_codes & _RELEASE_EVIDENCE_CODES:
        return "release_evidence_unavailable"
    if reason_codes & _PROVIDED_DATA_BASIS_CODES:
        return "provided_data_basis_missing"
    if reason_codes & _DEPENDENCY_CODES:
        return "dependency_incomplete"
    if reason_codes & _BOUNDED_RETRIEVAL_CODES:
        return "bounded_retrieval_incomplete"

    acquisition_codes = {code for acquisition in acquisitions for code in acquisition.failure_codes}
    cache_only_miss = (
        bool(acquisitions)
        and all(acquisition.search_query_count == 0 for acquisition in acquisitions)
        and (
            "source_scope_incomplete" in acquisition_codes
            or "search_not_exhaustive" in acquisition_codes
        )
    )
    if cache_only_miss:
        return "cache_miss"
    if reason_codes & _EXACT_EVIDENCE_CODES or acquisition_codes & _EXACT_EVIDENCE_CODES:
        return "exact_evidence_missing"
    return "general_incomplete"


def _safe_terminal_sentence(plan: CompositeKnowledgeQueryPlan) -> str:
    if plan.interaction_intent is InteractionIntent.EXPLAIN and plan.product_selection is None:
        return (
            "확인되지 않은 내용을 사전학습 지식으로 보완하지 않았으며, "
            "공식 근거 없는 설명도 생성하지 않았습니다."
        )
    return (
        "따라서 제공 데이터 상품과의 교집합을 확정할 수 없어, "
        "일부 조건을 무시한 상품 검색은 수행하지 않았습니다. "
        "확인되지 않은 상품도 제시하지 않았습니다."
    )


def build_external_cannot_verify_narrative(
    plan: CompositeKnowledgeQueryPlan,
    *,
    reason_codes: Iterable[str] = (),
    acquisitions: Iterable[_AcquisitionSummary] = (),
) -> ExternalCannotVerifyNarrative:
    """Render a contextual singleton control answer without weakening status.

    A Cache Miss is deliberately not described as an empty search result.  It
    remains CANNOT_VERIFY because the approved evidence universe was
    incomplete for the requested relation.  HCLX may later add only its
    schema-approved lead; this server-owned body and its hash stay unchanged.
    """

    reasons = frozenset(reason_codes)
    acquisition_items = tuple(acquisitions)
    scope = _requested_scope(plan)
    kind = _failure_kind(reasons, acquisition_items)
    if kind == "cache_miss":
        first = (
            f"승인된 공식 문서 저장소(Cache)에서 {scope} 관련 사실을 직접 뒷받침하는 "
            "근거를 찾지 못해 확인할 수 없습니다."
        )
    elif kind == "exact_evidence_missing":
        first = (
            f"공식 문서 후보는 확인했지만 {scope} 관련 사실을 직접 뒷받침하는 원문 근거를 "
            "검증하지 못해 확인할 수 없습니다."
        )
    elif kind == "dependency_incomplete":
        first = f"{scope} 연결 단계 중 일부를 승인된 공식 근거로 검증하지 못해 확인할 수 없습니다."
    elif kind == "provided_data_basis_missing":
        first = (
            f"공식 관계 근거를 상품 데이터의 요청 수치·정렬 기준과 연결하지 못해 "
            f"{scope} 관련 결과를 확인할 수 없습니다."
        )
    elif kind == "bounded_retrieval_incomplete":
        first = (
            f"제한된 공식 근거 확인 범위 안에서 {scope} 관련 검증을 완료하지 못해 "
            "확인할 수 없습니다."
        )
    elif kind == "release_evidence_unavailable":
        first = (
            f"현재 승인된 Release(배포 단위)에 {scope} 관련 사실을 검증할 공식 근거가 "
            "활성화되지 않아 확인할 수 없습니다."
        )
    else:
        first = f"{scope} 관련 사실을 뒷받침하는 승인된 공식 외부 근거를 끝까지 검증할 수 없습니다."
    answer = f"{first} {_safe_terminal_sentence(plan)}"
    return ExternalCannotVerifyNarrative(
        failure_kind=kind,
        requested_scope=scope,
        answer=answer,
    )


__all__ = [
    "ExternalCannotVerifyNarrative",
    "build_external_cannot_verify_narrative",
]
