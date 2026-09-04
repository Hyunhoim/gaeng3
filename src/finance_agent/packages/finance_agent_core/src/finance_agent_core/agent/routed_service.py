from __future__ import annotations

import contextvars
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.compiler import (
    CompiledFamilySearch,
    CrossFamilyGlobalRanking,
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
    resolve_cross_family_global_ranking,
)
from finance_agent_core.agent.compound_query import (
    AggregateThresholdUnavailableError,
    CompoundAuthorizationReceipt,
    CompoundExecution,
    NamedComparisonIncompleteError,
    TypedCompoundPlan,
    TypedCompoundQueryEngine,
    requires_typed_two_stage_plan,
)
from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDecision,
    ExternalKnowledgeRouteDisposition,
    ExternalKnowledgeRouter,
)
from finance_agent_core.agent.external_search_planning import (
    ExternalSearchPlanCompiler,
    ProvidedProductUniverseBuilder,
)
from finance_agent_core.agent.field_language import registry_field_mentions
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentAnalysisProvider,
    has_external_knowledge_surface,
    has_negated_external_relation,
    has_server_authorized_knowledge_requirements,
    sanitize_server_authorized_knowledge_draft,
    verify_intent_analysis,
)
from finance_agent_core.agent.knowledge_router import (
    DeterministicKnowledgeRouter,
    KnowledgeRouteDecision,
    KnowledgeRoutedExecutionError,
    KnowledgeRouteDisposition,
    product_search_has_precedence,
)
from finance_agent_core.agent.knowledge_service import (
    KnowledgeAgent,
    KnowledgeAgentResult,
    KnowledgeServiceError,
)
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
    PlanningPath,
    PlanningTrace,
)
from finance_agent_core.agent.product_comparison import normalize_product_mention
from finance_agent_core.agent.product_identity import (
    DatabaseProductIdentityMatcher,
    extract_plain_labeled_product_lookup,
    normalize_database_identity,
    unresolved_identity_like_spans,
)
from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXConfigurationError,
    HyperClovaXTimeoutError,
    hyperclova_failure_reason,
)
from finance_agent_core.agent.router import IntentRouter, extract_product_routing_hints
from finance_agent_core.agent.safety import SafetyDisposition
from finance_agent_core.agent.semantic_aliases import ranking_intent_present
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.answering.final_generation import FinalAnswerProvider
from finance_agent_core.answering.models import AnswerComposition
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.knowledge import (
    KnowledgePlanAuthorityError,
    canonical_knowledge_plan_sha256,
)
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    ProductFamily,
    Unit,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RoutedExecutionError,
)
from finance_agent_core.deadline import (
    RequestDeadlineExceeded,
    current_request_deadline,
    raise_if_request_stopped,
)
from finance_agent_core.domain import (
    AggregateEvidence,
    ComparisonEvidence,
    DatabaseManifest,
    ProductEvidence,
    VerifiedAggregation,
    VerifiedSearch,
)
from finance_agent_core.execution import (
    AggregateResultVerifier,
    PlanAuthorityCode,
    PlanAuthorityError,
    PlanAuthorityGate,
    PlanCompilerKind,
    PlanExecutionBlockedError,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    ValidatedPlan,
    build_aggregate_evidence,
    build_comparison_evidence,
    build_product_comparison,
    build_product_evidence,
    render_blocked_plan,
    render_verified_aggregation,
    render_verified_search,
    require_executable_aggregation,
    require_executable_comparison,
    require_executable_search,
    require_internal_evaluation_aggregation,
    require_internal_evaluation_comparison,
    require_internal_evaluation_search,
)
from finance_agent_core.execution.authority import (
    query_plan_authority_sha256,
    require_manifest_binding,
    require_validated_plan,
    require_verifier_budget,
)
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntime,
    ExternalKnowledgeRuntimeResult,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
    record_cache_covers_verifier_projection,
)
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    BoundedAsyncAuditSink,
    MetricCounter,
    RequestAuditRecorder,
    bind_request_audit,
    current_request_audit,
)
from finance_agent_core.release import (
    AgentReleaseCode,
    AgentReleaseError,
    PublicKnowledgeRetrievalRelease,
    ResolvedAgentRelease,
)
from finance_agent_core.retrieval import DocumentEvidence
from finance_agent_core.retrieval.relations import RelationIndexError
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
    load_approved_dataset_manifest,
    require_approved_database_paths,
)

_PUBLIC_INTENT_ANALYSIS: contextvars.ContextVar[IntentAnalysisDraft | None] = (
    contextvars.ContextVar("finance_public_intent_analysis", default=None)
)
_MULTI_FAMILY_COMPOUND_REQUIRED = re.compile(
    r"(?:각각|각\s*(?:상품군|시장|유형|종류)(?:에서|별로)?|"
    r"상품군별(?:로)?|따로|나란히|구분해서|"
    r"한\s*(?:목록|리스트|표)(?:으로|에서|에)?|직접\s*(?:정렬|순위)|"
    r"합치지\s*말고|섞지\s*말고|"
    r"(?:먼저|우선).{0,80}(?:추리|추린|고르|고른|선별|남기|남긴)"
    r".{0,40}(?:다음|뒤|후|이후))",
    re.IGNORECASE,
)
_MULTI_FAMILY_COUNT_REQUIRED = re.compile(
    r"(?:(?:상품|종목|펀드)\s*(?:수|개수|건수).{0,24}상품군별|"
    r"상품군별.{0,24}(?:상품|종목|펀드)\s*(?:수|개수|건수))",
    re.IGNORECASE,
)
_SINGLE_FAMILY_COMPOUND_REQUIRED = re.compile(
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|[A-Z][A-Z0-9.]{1,19})"
    r"(?:\)|\s)*(?:보다|대비|의\s*.{1,64}?(?:넘는|웃도는|초과하는|밑도는|미만인))|"
    r"(?:상위|하위|최상단|최하단).{0,18}(?:개|종목|상품).{0,72}?"
    r"(?:먼저|우선|1\s*차|후보|남긴|고른|선별|줄이고|만든\s*뒤|안에서|그중)|"
    # ``metric 기준 N개로 1차 선별`` is staged even when its first-stage
    # direction is missing.  The compound compiler must reject it and the
    # service must preserve that rejection as CLARIFY instead of falling back
    # to a broad one-stage SEARCH that silently drops the shortlist.
    r"기준(?:으로)?\s*(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
    r"(?:개|종목|상품)(?:로|를)?\s*(?:1\s*(?:차|단계)|예선|후보|선별)|"
    r"(?:평균(?:값|치)?|중앙\s*값|중위수|메디안|최빈값)\s*"
    r"(?:(?:보다|이상|이하|초과|미만)|(?:을|를)?\s*"
    r"(?:넘는|웃도는|밑도는))|"
    # Two explicitly named metrics compared row-by-row must never fall back
    # to an ordinary one-metric SEARCH if the compound grammar rejects it.
    r"(?:보다|에\s*비해).{0,32}(?:높은|낮은|큰|작은|많은|적은|긴|짧은)"
    r".{0,80}(?:차이|격차)",
    re.IGNORECASE,
)

_EXTERNAL_CONTROL_JOINERS = frozenset(
    {
        "의",
        "이",
        "가",
        "을",
        "를",
        "은",
        "는",
        "와",
        "과",
        "및",
        "중",
        "에서",
        "가운데",
    }
)
_EXTERNAL_CONTROL_FAMILY_SCOPES = frozenset(
    {
        "국내etf",
        "해외etf",
        "국내etn",
        "해외etn",
        "국내etp",
        "해외etp",
        "공모펀드",
        "펀드",
        "원화채권",
        "국내채권",
        "채권",
    }
)


def _external_control_span_is_authorized(span: str, requirements: tuple) -> bool:
    """Return whether one compiler control span is wholly external semantics.

    The ordinary DB compiler deliberately marks relation verbs as unsupported.
    They may be removed from a *prefilter-only* plan only when the independently
    compiled external plan owns every character through literal subject or
    relation evidence.  DB fields, values and ranking words can therefore never
    disappear through this bridge.
    """

    normalized = normalize_database_identity(span).replace(" ", "")
    if not normalized:
        return False
    relation_atoms = {
        normalize_database_identity(requirement.relation_evidence_span).replace(" ", "")
        for requirement in requirements
    }
    subject_atoms = {
        normalize_database_identity(requirement.subject_evidence_span).replace(" ", "")
        for requirement in requirements
    }
    relation_atoms.discard("")
    subject_atoms.discard("")
    # The DB compiler can report only the relation-bearing suffix of a longer
    # server-authorized evidence span (for example ``테마와 연결`` from
    # ``심해로봇 테마와 연결 이력이 있는``).  It is safe to remove that
    # control span from this *prefilter-only* plan because the complete
    # relation span was independently grounded in the literal question and
    # validated by the capability-specific sanitizer.  The reverse containment
    # is deliberately not accepted: a broad compiler span may still contain a
    # DB field, threshold or ranking expression that must remain authoritative.
    if any(normalized in relation_atom for relation_atom in relation_atoms):
        return True
    atoms = tuple(
        sorted(
            (
                *relation_atoms,
                *subject_atoms,
                *_EXTERNAL_CONTROL_JOINERS,
                *_EXTERNAL_CONTROL_FAMILY_SCOPES,
            ),
            key=lambda value: (-len(value), value),
        )
    )
    reachable: dict[int, bool] = {0: False}
    for offset in range(len(normalized) + 1):
        if offset not in reachable:
            continue
        used_relation = reachable[offset]
        for atom in atoms:
            if normalized.startswith(atom, offset):
                next_offset = offset + len(atom)
                reachable[next_offset] = reachable.get(next_offset, False) or (
                    used_relation or atom in relation_atoms
                )
    return reachable.get(len(normalized), False)


def _external_identity_control_span_is_authorized(
    span: str,
    identity_surfaces: tuple[str, ...],
) -> bool:
    """Allow only a compiler suffix wholly owned by one explicit identity.

    Unknown literal product names are resolved to an empty universe by
    ``ProvidedProductUniverseBuilder``.  The DB prefilter must not convert that
    deterministic NOT_FOUND into CLARIFY, but it also must not discard a broad
    span containing an unrelated DB condition.  Therefore containment is
    intentionally one-way: the compiler span must fit inside the product
    surface extracted by the server Router.
    """

    normalized = normalize_database_identity(span).replace(" ", "")
    if not normalized:
        return False
    return any(
        normalized
        in normalize_database_identity(surface).replace(" ", "")
        for surface in identity_surfaces
    )


class RoutedServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FamilySearchResult(RoutedServiceModel):
    product_family: ProductFamily
    status: Literal["success", "not_found"]
    answer: str
    query_plan: QueryPlan
    candidate_count: int = Field(ge=0)
    products: list[ProductEvidence]
    warnings: list[str]
    source_manifest: DatabaseManifest

    @model_validator(mode="after")
    def validate_family_result(self) -> FamilySearchResult:
        if self.query_plan.intent not in {Intent.SEARCH, Intent.AGGREGATE}:
            raise ValueError("family result requires a SEARCH or AGGREGATE QueryPlan")
        if self.query_plan.intent is Intent.AGGREGATE and (
            self.products
            or not self.query_plan.intent_payload.aggregations
            or any(
                item.function.value
                not in {"count", "avg", "median", "sum", "min", "max"}
                for item in self.query_plan.intent_payload.aggregations
            )
        ):
            raise ValueError(
                "family aggregate result requires approved measures and no products"
            )
        if self.query_plan.product_families != [self.product_family]:
            raise ValueError("family search result and QueryPlan family differ")
        if (self.status == "not_found") != (self.candidate_count == 0):
            raise ValueError("family search status and candidate count disagree")
        if self.status == "not_found" and self.products:
            raise ValueError("not_found family search cannot contain products")
        return self


@dataclass(frozen=True)
class _ExecutedFamilySearch:
    grounded_question: str
    result: FamilySearchResult
    verified: VerifiedSearch


class RoutedAgentResult(RoutedServiceModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    status: Literal["executed", "clarify", "unsupported"]
    decision: RouteDecision
    answer: str
    query_plan: QueryPlan | None
    candidate_count: int | None
    products: list[ProductEvidence]
    aggregates: list[AggregateEvidence] = Field(default_factory=list)
    comparisons: list[ComparisonEvidence] = Field(default_factory=list)
    documents: list[DocumentEvidence] = Field(default_factory=list)
    warnings: list[str]
    source_manifest: DatabaseManifest | None
    answer_composition: AnswerComposition | None
    family_searches: list[FamilySearchResult] = Field(default_factory=list, max_length=4)
    verified_document_intersection_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_result_state(self) -> RoutedAgentResult:
        if self.request_id != self.decision.draft.request_id:
            raise ValueError("result and route request IDs differ")
        if self.status == "executed":
            if self.verified_document_intersection_count and not self.documents:
                raise ValueError(
                    "verified document intersections require official document evidence"
                )
            if self.family_searches:
                if len(self.family_searches) < 2:
                    raise ValueError("compound result requires at least two family searches")
                family_intents = {item.query_plan.intent for item in self.family_searches}
                if family_intents not in ({Intent.SEARCH}, {Intent.AGGREGATE}):
                    raise ValueError("multi-family result cannot mix query intents")
                aggregate_bundle = family_intents == {Intent.AGGREGATE}
                if (
                    self.query_plan is not None
                    or self.source_manifest is not None
                    or self.comparisons
                    or (self.aggregates and not aggregate_bundle)
                    or (aggregate_bundle and not self.aggregates)
                ):
                    raise ValueError(
                        "multi-family result must keep compatible evidence per family"
                    )
                if aggregate_bundle and self.products:
                    raise ValueError("multi-family aggregate cannot return product rows")
                if self.candidate_count != sum(
                    item.candidate_count for item in self.family_searches
                ):
                    raise ValueError("multi-family candidate count must equal the family sum")
                family_products = [
                    product for item in self.family_searches for product in item.products
                ]
                if self.products != family_products:
                    # A verified global TOP-K may interleave products from multiple
                    # families.  Permit only a stable interleaving of the exact
                    # per-family evidence: no product may be added, removed, or
                    # reordered inside its own family result.
                    unmatched = list(family_products)
                    for product in self.products:
                        try:
                            unmatched.remove(product)
                        except ValueError as error:
                            raise ValueError(
                                "multi-family products must contain only family evidence"
                            ) from error
                    if unmatched:
                        raise ValueError(
                            "multi-family products must contain every family evidence item"
                        )
                    for family_search in self.family_searches:
                        positions = [
                            self.products.index(product) for product in family_search.products
                        ]
                        if positions != sorted(positions):
                            raise ValueError(
                                "multi-family products must preserve each family result order"
                            )
                bundle_families = list(
                    dict.fromkeys(item.product_family for item in self.family_searches)
                )
                if bundle_families != self.decision.draft.product_families:
                    raise ValueError("compound result order must match the routed family order")
                if (
                    self.answer_composition is not None
                    and self.answer_composition.answer != self.answer
                ):
                    raise ValueError("multi-family composition and served answer differ")
            elif (
                self.query_plan is None
                or self.candidate_count is None
                or self.source_manifest is None
            ):
                raise ValueError("executed result requires plan, count, and source manifest")
        elif (
            self.query_plan is not None
            or self.candidate_count is not None
            or self.products
            or self.aggregates
            or self.comparisons
            or self.documents
            or self.warnings
            or self.source_manifest is not None
            or self.answer_composition is not None
            or self.family_searches
            or self.verified_document_intersection_count
        ):
            raise ValueError("control result must not contain executed evidence")
        return self


class ExactIdentityNotFoundResult(RoutedServiceModel):
    """Absence proven across every configured provided-product database."""

    request_id: str = Field(min_length=1, max_length=128)
    identity: str = Field(min_length=2, max_length=48)
    checked_families: list[ProductFamily] = Field(min_length=1, max_length=4)
    answer: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_checked_scope(self) -> ExactIdentityNotFoundResult:
        if len(self.checked_families) != len(set(self.checked_families)):
            raise ValueError("checked product families must be unique")
        return self


def _control_answer(disposition: RouteDisposition, reason: str) -> str:
    if disposition is RouteDisposition.CLARIFY:
        return (
            f"질문을 실행하지 않았습니다. {reason} "
            "확인할 상품군·상품 식별자·수치 기준을 구체적으로 알려주세요."
        )
    return (
        f"요청을 실행하지 않았습니다. {reason} "
        "제공 데이터에 근거한 조회·상세 설명 범위로 질문을 바꿔주세요."
    )


_FAMILY_LABELS = {
    ProductFamily.BOND: "국내채권",
    ProductFamily.DOMESTIC_ETP: "국내 ETP",
    ProductFamily.OVERSEAS_ETP: "해외 ETP",
    ProductFamily.FUND: "공모펀드",
}
_CROSS_FAMILY_SAFETY_NOTICE = (
    "상품군별로 독립 검색했으며, 상품군 간 수치의 직접 비교·합산·우열 판단은 수행하지 않았습니다."
)
_CROSS_FAMILY_ALL_REQUIRED = re.compile(
    r"각각|나란히|한\s*번에|모두|정확한\s*(?:이름|상품명)|상품군별",
    re.IGNORECASE,
)
_CROSS_FAMILY_EXACT_FIELDS = frozenset(
    {"product_id", "product_name", "ticker", "isin", "credit_rating"}
)
_EXACT_IDENTITY_READ_CUE = re.compile(
    r"상세|정보|원천\s*(?:값|데이터)|제공\s*필드|수치|값|"
    r"조회|확인|알려|보여|말해|적어|나열|궁금",
    re.IGNORECASE,
)
_CLOSED_IDENTITY_COMPARISON_CUE = re.compile(
    r"비교|대조|나란히|(?:입력|질문|적은|쓴)\s*(?:한)?\s*순서|"
    r"순서(?:를)?\s*(?:유지|대로)|차례(?:대로|로)|각각|표(?:로|에서)",
    re.IGNORECASE,
)


def _requires_complete_exact_cross_family_match(
    question: str,
    searches: list[CompiledFamilySearch],
) -> bool:
    """Keep an explicitly conjunctive exact request atomic across families."""

    if _CROSS_FAMILY_ALL_REQUIRED.search(question) is None:
        return False
    exact_families = 0
    for search in searches:
        if any(
            constraint.field in _CROSS_FAMILY_EXACT_FIELDS
            and constraint.operator.value in {"eq", "in"}
            and constraint.strength.value == "locked"
            for constraint in search.plan.constraints
        ):
            exact_families += 1
    return exact_families >= 2


@dataclass(frozen=True)
class _GlobalRankedProduct:
    product_family: ProductFamily
    product: ProductEvidence
    value: Decimal


def _ranking_value(product: ProductEvidence, field_name: str) -> Decimal | None:
    matches = [field for field in product.fields if field.canonical_field == field_name]
    if len(matches) != 1:
        return None
    evidence = matches[0]
    if (
        evidence.normalized_value is None
        or evidence.quality not in {QualityStatus.VALID, QualityStatus.PARTIAL}
        or isinstance(evidence.normalized_value, bool)
    ):
        return None
    try:
        return Decimal(str(evidence.normalized_value))
    except InvalidOperation:
        return None


def _rank_global_products(
    family_searches: list[FamilySearchResult],
    ranking: CrossFamilyGlobalRanking,
) -> tuple[list[FamilySearchResult], list[_GlobalRankedProduct]]:
    family_order = {result.product_family: index for index, result in enumerate(family_searches)}
    candidates = [
        _GlobalRankedProduct(result.product_family, product, value)
        for result in family_searches
        for product in result.products
        if (value := _ranking_value(product, ranking.field)) is not None
    ]
    direction_factor = Decimal("-1") if ranking.direction.value == "desc" else Decimal("1")
    ordered = sorted(
        candidates,
        key=lambda item: (
            direction_factor * item.value,
            family_order[item.product_family],
            item.product.product_id,
        ),
    )[: ranking.limit]
    selected = {
        (item.product_family, item.product.product_id): position
        for position, item in enumerate(ordered)
    }
    filtered: list[FamilySearchResult] = []
    for result in family_searches:
        products = sorted(
            (
                product
                for product in result.products
                if (result.product_family, product.product_id) in selected
            ),
            key=lambda product: selected[(result.product_family, product.product_id)],
        )
        filtered.append(
            result.model_copy(
                update={
                    "products": products,
                    "answer": (
                        f"전체 순위 {ranking.limit}개 중 "
                        f"{_FAMILY_LABELS[result.product_family]} {len(products)}개가 포함됐습니다."
                    ),
                }
            )
        )
    return filtered, ordered


def _compile_global_cross_family_answer(
    ranked: list[_GlobalRankedProduct],
    ranking: CrossFamilyGlobalRanking,
    *,
    candidate_count: int,
) -> str:
    registry = load_field_registry()
    first_family = ranked[0].product_family.value if ranked else None
    label = (
        registry.require_field(ranking.field, [first_family]).label
        if first_family is not None
        else ranking.field
    )
    direction = "높은 순" if ranking.direction.value == "desc" else "낮은 순"
    lines = [
        f"동일 canonical field(공통 표준 필드)인 {label}을 {direction}으로 "
        f"통합 정렬했습니다. 검증된 후보는 총 {candidate_count:,}개입니다."
    ]
    for position, item in enumerate(ranked, start=1):
        evidence = next(
            field for field in item.product.fields if field.canonical_field == ranking.field
        )
        suffix = "%" if evidence.unit == "pct_point" else ""
        identifier = item.product.ticker or item.product.product_id
        columns = "/".join(evidence.source_columns) or "constant"
        lines.append(
            f"{position}. [{_FAMILY_LABELS[item.product_family]}] "
            f"{item.product.product_name} ({identifier}) — {label} {item.value}{suffix} "
            f"[근거: {evidence.source_id} 원본 행 {evidence.source_row}, "
            f"{columns}, 기준일 {evidence.as_of.isoformat()}]"
        )
    if not ranked:
        lines.append("요청한 공통 지표에 사용 가능한 원천값이 없어 결과를 표시하지 않습니다.")
    lines.append(
        "상품군별 Oracle(결정론적 검색기) 결과를 같은 필드·단위에서만 합쳤으며, "
        "결측·UNKNOWN 값은 순위에서 제외했습니다."
    )
    return "\n".join(lines)


# AuditEvent's owner-only O_APPEND JSONL boundary is 64 KiB. A serialized SHA-256
# linkage entry costs 67 bytes (quotes, 64 hex characters, and a separator),
# so retaining at most 768 hashes leaves more than 12 KiB for the fixed event,
# release/dataset provenance, and JSON field names. Counts always describe the
# complete result; an optional linkage list is emitted only when the complete
# list fits both its model cardinality and this shared byte-safe budget.
_AUDIT_PRODUCT_LINK_LIMIT = 100
_AUDIT_EVIDENCE_LINK_LIMIT = 2_000
_AUDIT_TOTAL_LINK_HASH_BUDGET = 768


@dataclass(frozen=True)
class _AuditLinkage:
    result_count: int
    evidence_count: int
    product_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def _bounded_audit_linkage(
    *,
    product_ids: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (),
) -> _AuditLinkage:
    """Keep exact counts while bounding optional identifier linkage payloads."""

    # Evidence references can legitimately repeat (for example, comparison
    # cells can point at product field evidence already present in the result).
    # AuditEvent requires unique hashes, so its evidence count is the exact
    # number of distinct evidence records/references.
    unique_evidence_ids = tuple(dict.fromkeys(evidence_ids))
    result_count = len(product_ids)
    evidence_count = len(unique_evidence_ids)

    bounded_product_ids: tuple[str, ...] = ()
    if (
        result_count <= _AUDIT_PRODUCT_LINK_LIMIT
        and result_count <= _AUDIT_TOTAL_LINK_HASH_BUDGET
        and len(set(product_ids)) == result_count
    ):
        bounded_product_ids = product_ids

    remaining_hash_budget = _AUDIT_TOTAL_LINK_HASH_BUDGET - len(bounded_product_ids)
    bounded_evidence_ids = (
        unique_evidence_ids
        if evidence_count <= _AUDIT_EVIDENCE_LINK_LIMIT and evidence_count <= remaining_hash_budget
        else ()
    )
    return _AuditLinkage(
        result_count=result_count,
        evidence_count=evidence_count,
        product_ids=bounded_product_ids,
        evidence_ids=bounded_evidence_ids,
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _aggregate_response_candidate_count(
    plan: QueryPlan,
    verified: VerifiedAggregation,
    question: str,
) -> int:
    """Expose the complete usable population for a single aggregate metric."""

    aggregations = plan.intent_payload.aggregations
    if len(aggregations) != 1 or verified.total_group_count != len(verified.groups):
        return verified.candidate_count
    if not plan.intent_payload.group_by and re.search(
        r"0\s*(?:이|가)?\s*아닌|0\s*(?:값|수치)?\s*(?:제외|빼고)",
        question,
        re.IGNORECASE,
    ) is None:
        return verified.candidate_count
    requested = aggregations[0]
    if requested.function.value == "count" and requested.field != "product_id":
        # COUNT(field) availability reports partition the same verified cohort
        # into present/missing groups.  The public candidate count is therefore
        # the whole cohort, not only the non-null side's valid_count.
        return verified.candidate_count
    valid_count = 0
    for group in verified.groups:
        metric = next(
            (
                item
                for item in group.metrics
                if item.function is requested.function and item.field == requested.field
            ),
            None,
        )
        if metric is None:
            return verified.candidate_count
        valid_count += metric.valid_count
    return valid_count


def _lock_approved_fund_aggregate_scope(plan: QueryPlan) -> QueryPlan:
    """Attach the immutable public-fund row scope before authority validation.

    The lexical compiler deliberately treats ``공모펀드`` as the supplied
    dataset name, so its standalone parser output does not duplicate that word
    as a row predicate.  At the executable server boundary, however, every
    fund aggregate must carry exactly the same locked ``public_offering=true``
    constraint as SEARCH and COMPARE.  An explicit conflicting predicate is
    preserved and rejected by ``require_fund_public_scope`` rather than being
    silently overwritten.
    """

    if plan.intent is not Intent.AGGREGATE or plan.product_families != [ProductFamily.FUND]:
        return plan
    if any(item.field == "public_offering" for item in plan.constraints):
        return plan
    return plan.model_copy(
        update={
            "constraints": [
                *plan.constraints,
                Constraint(
                    field="public_offering",
                    operator=ConstraintOperator.EQ,
                    value=True,
                    unit=Unit.BOOLEAN,
                    strength=ConstraintStrength.LOCKED,
                ),
            ]
        }
    )


def _compile_cross_family_answer(
    family_searches: list[FamilySearchResult],
) -> str:
    sections = [
        f"[{_FAMILY_LABELS[item.product_family]}]\n{item.answer}" for item in family_searches
    ]
    return "\n\n".join([*sections, _CROSS_FAMILY_SAFETY_NOTICE])


class RoutedFinanceAgent:
    """Shared fail-closed path for routing, compilation, Oracle, evidence and answer."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        router: IntentRouter | None = None,
        query_plan_provider: object | None = None,
        grounded_plan_provider: object | None = None,
        answer_provider: object | None = None,
        intent_analysis_provider: IntentAnalysisProvider | None = None,
        official_answer_provider: FinalAnswerProvider | None = None,
        require_official_hclx: bool = False,
        hclx_planning_enabled: bool = False,
        allow_internal_disabled_dataset: bool = False,
        capability_execution_overrides: set[ProductFamily | str] | None = None,
        require_approved_databases: bool = False,
        release_guard: ResolvedAgentRelease | None = None,
        require_agent_release: bool = False,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
        adaptive_semantic_resolver: AdaptiveSemanticResolver | None = None,
        schema_link_shadow_observer: object | None = None,
        audit_sink: BoundedAsyncAuditSink | None = None,
        knowledge_router: DeterministicKnowledgeRouter | None = None,
        knowledge_agent: KnowledgeAgent | None = None,
        external_knowledge_router: ExternalKnowledgeRouter | None = None,
        external_knowledge_runtime: ExternalKnowledgeRuntime | None = None,
        external_universe_builder: ProvidedProductUniverseBuilder | None = None,
        external_search_plan_compiler: ExternalSearchPlanCompiler | None = None,
    ) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
        if (
            query_plan_provider is not None
            or grounded_plan_provider is not None
            or answer_provider is not None
            or hclx_planning_enabled
        ):
            raise ValueError(
                "legacy QueryPlan and grounded-answer providers are retired from Runtime"
            )
        if schema_link_shadow_observer is not None:
            raise ValueError("Schema Shadow execution is retired from Runtime")
        # Retired seams stay as immutable-by-contract sentinels so the Backend
        # approval guard can detect post-construction monkeypatching/tampering.
        # No execution method reads these attributes.
        self.query_plan_provider = None
        self.grounded_plan_provider = None
        self.answer_provider = None
        self.hclx_planning_enabled = False
        self.schema_link_shadow_observer = None
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.record_cache = record_cache or RecordSnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )
        self._record_cache_enabled = record_cache is not None
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )
        self.database_identity_matcher = DatabaseProductIdentityMatcher(self.database_paths)
        self.router = router or IntentRouter(
            adaptive_semantic_enabled=adaptive_semantic_resolver is not None,
        )
        self.compiler = ServerQueryPlanCompiler(
            self.database_paths,
            record_cache=self.record_cache,
            identity_cache=self.identity_cache,
        )
        self.compound_query_engine = TypedCompoundQueryEngine(
            self.database_paths,
            record_cache=self.record_cache,
        )
        self.intent_analysis_provider = intent_analysis_provider
        self.official_answer_provider = official_answer_provider
        self.require_official_hclx = require_official_hclx
        if require_official_hclx and (
            intent_analysis_provider is None
            or official_answer_provider is None
            or getattr(intent_analysis_provider, "require_execution_structure", False)
            is not True
        ):
            raise ValueError(
                "official HCLX mode requires semantic-structure Intent analysis and "
                "final answer providers"
            )
        if (
            adaptive_semantic_resolver is not None
            and type(adaptive_semantic_resolver) is not AdaptiveSemanticResolver
        ):
            raise TypeError("adaptive_semantic_resolver must be the approved server resolver")
        self.adaptive_semantic_resolver = adaptive_semantic_resolver
        if adaptive_semantic_resolver is not None and not self.router.adaptive_semantic_enabled:
            raise ValueError("adaptive semantic resolver requires an explicitly enabled router")
        if (
            knowledge_router is not None
            and type(knowledge_router) is not DeterministicKnowledgeRouter
        ):
            raise TypeError("knowledge_router must be the deterministic public router")
        if knowledge_agent is not None and type(knowledge_agent) is not KnowledgeAgent:
            raise TypeError("knowledge_agent must be a KnowledgeAgent")
        if knowledge_agent is not None and knowledge_router is None:
            raise ValueError("knowledge execution requires the public knowledge router")
        self.knowledge_router = knowledge_router
        self.knowledge_agent = knowledge_agent
        if (
            external_knowledge_router is not None
            and type(external_knowledge_router) is not ExternalKnowledgeRouter
        ):
            raise TypeError("external_knowledge_router must be the approved server router")
        self.external_knowledge_router = external_knowledge_router
        external_parts = (
            external_knowledge_runtime,
            external_universe_builder,
            external_search_plan_compiler,
        )
        if any(item is not None for item in external_parts) and not all(
            item is not None for item in external_parts
        ):
            raise ValueError("external search runtime requires runtime, universe and compiler")
        if (
            external_knowledge_runtime is not None
            and type(external_knowledge_runtime) is not ExternalKnowledgeRuntime
        ):
            raise TypeError("external knowledge runtime has an invalid type")
        if (
            external_universe_builder is not None
            and type(external_universe_builder) is not ProvidedProductUniverseBuilder
        ):
            raise TypeError("external universe builder has an invalid type")
        if (
            external_search_plan_compiler is not None
            and type(external_search_plan_compiler) is not ExternalSearchPlanCompiler
        ):
            raise TypeError("external search plan compiler has an invalid type")
        if require_agent_release and release_guard is None:
            raise ValueError("public RoutedFinanceAgent requires a resolved Agent release")
        if release_guard is not None and type(release_guard) is not ResolvedAgentRelease:
            raise TypeError("release_guard must be a ResolvedAgentRelease")
        if require_agent_release:
            assert release_guard is not None
            signed_external = release_guard.manifest.components.knowledge_retrieval.external
            if external_knowledge_runtime is None:
                if signed_external.status == "activated":
                    raise ValueError("signed external knowledge release requires its runtime")
            elif (
                signed_external.status != "activated"
                or external_knowledge_runtime.release_artifact != signed_external.artifact
                or external_knowledge_runtime.release_artifact_file_sha256
                != signed_external.artifact_file_sha256
            ):
                raise ValueError("external knowledge runtime differs from signed release")
        self.external_knowledge_runtime = external_knowledge_runtime
        self.external_universe_builder = external_universe_builder
        self.external_search_plan_compiler = external_search_plan_compiler
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset
        self.capability_execution_overrides = frozenset(
            ProductFamily(family) for family in capability_execution_overrides or set()
        )
        self.require_approved_databases = require_approved_databases
        if require_agent_release:
            assert release_guard is not None
            signed_knowledge = release_guard.manifest.components.knowledge_retrieval
            relation_activated = signed_knowledge.relation.status == "activated"
            if relation_activated:
                if knowledge_router is None or knowledge_agent is None:
                    raise ValueError(
                        "the signed relation release requires its public router and Agent"
                    )
                if (
                    type(knowledge_agent.release) is not PublicKnowledgeRetrievalRelease
                    or knowledge_agent.release != signed_knowledge
                ):
                    raise ValueError(
                        "the knowledge Agent must equal the signed public knowledge release"
                    )
                if knowledge_agent.claim_provider is not None:
                    raise ValueError(
                        "the signed public knowledge release keeps claim generation disabled"
                    )
            elif knowledge_router is not None or knowledge_agent is not None:
                raise ValueError(
                    "a disabled signed relation release cannot attach a knowledge router or Agent"
                )
        self.release_guard = release_guard
        self.require_agent_release = require_agent_release
        if audit_sink is not None and type(audit_sink) is not BoundedAsyncAuditSink:
            raise TypeError("audit_sink must be the bounded async audit sink")
        self.audit_sink = audit_sink
        self.plan_authority_gate = PlanAuthorityGate(
            self.database_paths,
            require_approved_databases=require_approved_databases,
            allow_internal_disabled_dataset=allow_internal_disabled_dataset,
            capability_execution_overrides=self.capability_execution_overrides,
            release_guard=release_guard,
            require_agent_release=require_agent_release,
        )

    def _record_universe(self, database_path: Path, validated_plan: ValidatedPlan):
        require_validated_plan(validated_plan, database_path)
        if self._record_cache_enabled and record_cache_covers_verifier_projection(
            validated_plan.canonical_plan
        ):
            snapshot = self.record_cache.get(database_path)
            require_manifest_binding(validated_plan, snapshot.manifest)
            require_verifier_budget(validated_plan, len(snapshot.records))
            require_validated_plan(validated_plan, database_path)
            return snapshot.records
        return load_projected_verifier_records(database_path, validated_plan)

    def _external_db_prefilter_candidate_ids(
        self,
        *,
        question: str,
        request_id: str,
        external_plan,
        families: tuple[ProductFamily, ...],
        identity_surfaces: tuple[str, ...] = (),
    ) -> dict[ProductFamily, set[str]] | None:
        """Apply ordinary DB constraints before any external evidence lookup.

        The returned IDs are produced by the same QueryPlan compiler, public
        authority gate and read-only verifier projection as an ordinary SEARCH.
        Ranking and limit intentionally do not narrow this set: external
        relations are intersected first, then the external executor applies
        the requested global/product selection.
        """

        candidate_ids: dict[ProductFamily, set[str]] = {}
        scoped_questions = self.compiler.scope_family_questions(question, families)
        for family in families:
            scoped_question = scoped_questions[family]
            decision = RouteDecision(
                draft=MinimalQueryDraft(
                    request_id=request_id,
                    question=scoped_question,
                    intent=InteractionIntent.SEARCH,
                    product_families=[family],
                    # Exact product identities are intersected independently
                    # by ProvidedProductUniverseBuilder.  Keeping them out of
                    # this filter-only plan prevents one family's named product
                    # from leaking into another family slot.
                    product_mentions=[],
                    requested_limit=None,
                ),
                disposition=RouteDisposition.EXECUTE,
                reason_code="external_db_prefilter",
                reason="외부 근거 검색 전 제공 DB 조건을 고정",
                query_plan_intent=Intent.SEARCH,
                capability_matrix_version=self.router.matrix.matrix_version,
            )
            plan = self.compiler.compile(decision)
            remaining_ambiguities = [
                item
                for item in plan.ambiguities
                if not _external_control_span_is_authorized(
                    item.span,
                    external_plan.requirements,
                )
                and not _external_identity_control_span_is_authorized(
                    item.span,
                    identity_surfaces,
                )
            ]
            remaining_unsupported = [
                item
                for item in plan.unsupported_conditions
                if not _external_control_span_is_authorized(
                    item.span,
                    external_plan.requirements,
                )
                and not _external_identity_control_span_is_authorized(
                    item.span,
                    identity_surfaces,
                )
            ]
            if remaining_ambiguities or remaining_unsupported:
                spans = tuple(
                    item.span for item in (*remaining_ambiguities, *remaining_unsupported)
                )
                raise PlanCompilationBlockedError(
                    "외부 관계와 함께 요청한 DB 조건을 모두 확정할 수 없습니다: "
                    + ", ".join(spans)
                )
            plan = QueryPlan.model_validate(
                {
                    **plan.model_dump(mode="python"),
                    "ambiguities": [],
                    "unsupported_conditions": [],
                }
            )
            if not plan.constraints:
                continue
            planning = AdaptiveShadowPlanningPolicy().decide(
                decision,
                SemanticCoverageDecision(),
            )
            validated = self.plan_authority_gate.validate_routed(
                plan,
                decision,
                planning_decision=planning,
            )
            candidate_ids[family] = set(
                SQLiteOracle(self.database_paths[family]).candidate_product_ids(validated)
            )
        return candidate_ids or None

    def _assert_compound_authority_current(self) -> None:
        """Apply the ordinary immutable DB/release boundary to compound plans."""

        raise_if_request_stopped()
        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        raise_if_request_stopped()

    @staticmethod
    def _compound_control_decision(
        decision: RouteDecision,
        *,
        reason_code: str,
        reason: str,
    ) -> RouteDecision:
        """Fail closed when compound syntax cannot produce execution authority.

        Safety and unsupported routes are terminal and must never be rewritten
        to a weaker disposition.  Every other compound representation or
        authorization failure becomes a non-executable CLARIFY decision.
        """

        if (
            decision.disposition is RouteDisposition.UNSUPPORTED
            or decision.reason_code.startswith("safety_")
        ):
            return decision
        return RouteDecision(
            draft=decision.draft.model_copy(update={"intent": InteractionIntent.CLARIFY}),
            disposition=RouteDisposition.CLARIFY,
            reason_code=reason_code,
            reason=reason,
            query_plan_intent=None,
            capability_matrix_version=decision.capability_matrix_version,
        )

    @staticmethod
    def _requires_multi_family_compound_plan(decision: RouteDecision) -> bool:
        """Detect explicit per-family/multi-stage grammar that may not degrade.

        Simple cross-family searches remain on the existing family-search
        executor.  Requests that explicitly demand separate family clauses or
        a staged shortlist require a Typed Compound Plan; a compiler miss must
        not silently discard one clause and run a narrower ordinary search.
        """

        return (
            decision.disposition is not RouteDisposition.UNSUPPORTED
            and len(set(decision.draft.product_families)) > 1
            and (
                (
                    _MULTI_FAMILY_COMPOUND_REQUIRED.search(decision.draft.question)
                    is not None
                    and ranking_intent_present(decision.draft.question)
                )
                or _MULTI_FAMILY_COUNT_REQUIRED.search(decision.draft.question) is not None
            )
        )

    @staticmethod
    def _requires_single_family_compound_plan(decision: RouteDecision) -> bool:
        """Keep a failed relative/staged parse from degrading to DETAIL/SEARCH."""

        return (
            decision.disposition is not RouteDisposition.UNSUPPORTED
            and (
                _SINGLE_FAMILY_COMPOUND_REQUIRED.search(decision.draft.question)
                is not None
                or requires_typed_two_stage_plan(decision.draft.question)
            )
        )

    def _assert_signed_knowledge_current(self) -> None:
        """Recheck the in-process knowledge assembly at every public boundary."""

        if not self.require_agent_release:
            return
        assert self.release_guard is not None
        signed = self.release_guard.manifest.components.knowledge_retrieval
        activated = signed.relation.status == "activated"
        agent = self.knowledge_agent
        if activated:
            matches = (
                type(self.knowledge_router) is DeterministicKnowledgeRouter
                and type(agent) is KnowledgeAgent
                and type(agent.release) is PublicKnowledgeRetrievalRelease
                and agent.release == signed
                and agent.claim_provider is None
            )
        else:
            matches = self.knowledge_router is None and agent is None
        signed_external = signed.external
        if signed_external.status == "activated":
            matches = matches and (
                type(self.external_knowledge_runtime) is ExternalKnowledgeRuntime
                and self.external_knowledge_runtime.release_artifact == signed_external.artifact
                and self.external_knowledge_runtime.release_artifact_file_sha256
                == signed_external.artifact_file_sha256
            )
        else:
            matches = matches and self.external_knowledge_runtime is None
        if not matches:
            raise AgentReleaseError(
                AgentReleaseCode.RUNTIME_MISMATCH,
                "knowledge runtime differs from the signed public release",
            )

    def _assert_public_knowledge_boundary_current(self) -> None:
        """Apply the same immutable boundary to every public relation outcome."""

        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        self._assert_signed_knowledge_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        if self.require_agent_release and self.knowledge_agent is not None:
            # Production assembly verifies the expensive hashes at startup. This
            # request check is the cheap inode/path fingerprint used by readiness.
            self.knowledge_agent.assert_ready_current()

    def assert_public_response_boundary_current(self) -> None:
        """Recheck every immutable public input immediately before HTTP return.

        HyperCLOVA X final generation can take materially longer than the
        deterministic DB path.  A release binding or approved database that
        changes during that call invalidates the already-built answer, so the
        adapter calls this boundary after generation and discards the result.
        """

        self._assert_public_knowledge_boundary_current()

    def answer(self, question: str, request_id: str) -> RoutedAgentResult:
        return self._execute_audited(
            question,
            request_id,
            atomic=False,
        )

    def _answer_checked(self, question: str, request_id: str) -> RoutedAgentResult:
        # Evaluation/production code lives in the digest-pinned read-only image.
        # Startup and readiness perform the expensive deep tree hash; request
        # boundaries recheck the detached immutable release files only.
        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        try:
            return self._answer_once(question, request_id)
        finally:
            # Detect a path replacement that occurred after the pre-execution
            # approval check.  A changed inode/ctime invalidates the cached
            # approval and the result is discarded before it can be served.
            if self.require_approved_databases:
                require_approved_database_paths(self.database_paths)
            if self.release_guard is not None:
                self.release_guard.assert_request_current()

    def _answer_atomically(self, question: str, request_id: str) -> RoutedAgentResult:
        """Internal adapter seam: route and execute exactly once inside this service."""

        return self._execute_audited(
            question,
            request_id,
            atomic=True,
        )

    def _answer_public_atomically(
        self,
        question: str,
        request_id: str,
    ) -> (
        RoutedAgentResult
        | KnowledgeAgentResult
        | KnowledgeRouteDecision
        | ExternalKnowledgeRouteDecision
        | ExternalKnowledgeRuntimeResult
        | ExactIdentityNotFoundResult
    ):
        """Route one public request without bypassing the established atomic boundary."""

        self._assert_public_knowledge_boundary_current()
        safety = self.router.safety_envelope.evaluate(question)
        # SafetyEnvelope is a deterministic pre-provider boundary.  A terminal
        # safety decision never sends an attack, secret-exfiltration attempt, or
        # other blocked text to HCLX.  The official HCLX Intent requirement still
        # applies to every evaluation query that passes this boundary.
        if safety.disposition is not SafetyDisposition.ALLOW:
            return self._answer_atomically(question, request_id)
        intent_draft = self._analyze_public_intent(question, request_id)
        token = _PUBLIC_INTENT_ANALYSIS.set(intent_draft)
        try:
            # Negative relationship predicates require proof of absence across
            # a complete official-document universe.  The current external
            # runtime verifies positive evidence only, so it must never turn
            # ``보유하지 않은`` or ``자회사가 아닌`` into a positive relation
            # after lexical sanitization.  HCX has no authority to override
            # this deterministic, pre-sanitizer control decision.
            if has_negated_external_relation(safety.normalized_question):
                self._assert_public_knowledge_boundary_current()
                return ExternalKnowledgeRouteDecision(
                    disposition=ExternalKnowledgeRouteDisposition.CLARIFY,
                    reason_code="negated_external_relation_unverifiable",
                    reason=(
                        "외부 공식 자료만으로 관계가 존재하지 않는다는 사실을 "
                        "완전하게 검증할 수 없어 조건을 확정할 수 없음"
                    ),
                )
            labeled_lookup = extract_plain_labeled_product_lookup(
                safety.normalized_question
            )
            if (
                labeled_lookup is not None
                and self.database_identity_matcher.resolve_literal(labeled_lookup)
                is None
            ):
                # The request asks only whether one explicit code exists.  A
                # full exact-identity index over every configured provided DB
                # proves the empty result without guessing a product family.
                # Fabrication/insertion, ranking, comparison, and external
                # relation requests are excluded by the extractor (and unsafe
                # mutations are already terminal in SafetyEnvelope).
                checked_families = sorted(
                    self.database_paths,
                    key=lambda family: family.value,
                )
                self._assert_public_knowledge_boundary_current()
                return ExactIdentityNotFoundResult(
                    request_id=request_id,
                    identity=labeled_lookup,
                    checked_families=checked_families,
                    answer=(
                        f"요청한 상품 식별자 {labeled_lookup}는 제공 데이터의 "
                        "국내채권·국내/해외 ETP·공모펀드 범위에서 찾을 수 없습니다."
                    ),
                )
            lexical_families, lexical_product_mentions = extract_product_routing_hints(
                safety.normalized_question
            )
            db_product_mentions: tuple[str, ...] = ()
            if self.external_universe_builder is not None and has_external_knowledge_surface(
                question
            ):
                identity_families = lexical_families
                if not identity_families:
                    if re.search(r"(?<![A-Za-z])(?:ETF|ETN|ETP)(?![A-Za-z])", question, re.I):
                        identity_families = (
                            ProductFamily.DOMESTIC_ETP,
                            ProductFamily.OVERSEAS_ETP,
                        )
                    elif re.search(r"(?:공모\s*)?펀드", question, re.I):
                        identity_families = (ProductFamily.FUND,)
                    elif re.search(r"(?:원화|국내)?\s*채권", question, re.I):
                        identity_families = (ProductFamily.BOND,)
                db_product_mentions = (
                    self.external_universe_builder.question_identity_surfaces(
                        families=identity_families,
                        question=question,
                    )
                )
            product_mentions = tuple(
                dict.fromkeys((*lexical_product_mentions, *db_product_mentions))
            )
            # Development/offline profiles may intentionally omit HCX, but a
            # literal server-owned external relation must still never fall
            # through to a broad SQL search.  Seed a zero-authority draft and
            # let the same deterministic sanitizer derive the relation.  In
            # evaluation/production ``require_official_hclx`` still makes the
            # real Intent call mandatory before reaching this point.
            knowledge_seed = intent_draft
            if knowledge_seed is None and has_external_knowledge_surface(question):
                knowledge_seed = IntentAnalysisDraft(
                    intent=InteractionIntent.SEARCH,
                    product_families=list(lexical_families),
                    evidence_spans=[],
                    needs_clarification=False,
                )
            knowledge_draft = (
                None
                if knowledge_seed is None
                else sanitize_server_authorized_knowledge_draft(
                    question,
                    knowledge_seed,
                    fallback_families=lexical_families,
                    product_mentions=product_mentions,
                )
            )
            has_external_requirements = (
                knowledge_draft is not None
                and has_server_authorized_knowledge_requirements(question, knowledge_draft)
            )
            public_coverage = self.router.semantic_coverage_gate.evaluate(
                safety.normalized_question,
                interaction_intent=None,
                check_exclusions=True,
            )
            # A rejected HCX relation proposal has no authority.  The server
            # sanitizer independently derives every explicit external
            # relation from the question; if it finds none, continue through
            # the ordinary SemanticCoverageGate and Typed QueryPlan path.  The
            # previous model-proposal veto falsely blocked pure DB questions
            # such as multi-stage ranking whenever HCX mislabeled one phrase
            # as external knowledge.
            # Exact product IDs and fully specified DB filters outrank a model
            # hint only when the server found no literal external relation.
            # A query such as "자회사를 편입한 ETF 중 순자산 큰 상품" needs
            # both the verified relation and the DB ranking; letting the metric
            # pre-empt the relation silently drops the user's main condition.
            if (
                not has_external_requirements
                and product_search_has_precedence(safety.normalized_question)
            ):
                return self._answer_atomically(question, request_id)
            # External retrieval must not bypass the same ambiguity boundary
            # that protects the SQL path.  Examples include a missing holding
            # subject ("최근에 편입된") or an undefined ranking metric such as
            # "가격 매력".  Route those through the authoritative server gate,
            # which returns CLARIFY without opening the network.
            externally_covered_ambiguities = {
                span
                for span in public_coverage.ambiguity_spans
                if has_external_requirements
                and any(
                    span in requirement.relation_span
                    or requirement.relation_span in span
                    for requirement in knowledge_draft.knowledge_requirements
                )
            }
            unresolved_public_ambiguities = tuple(
                span
                for span in public_coverage.ambiguity_spans
                if span not in externally_covered_ambiguities
            )
            if unresolved_public_ambiguities:
                if has_external_requirements:
                    self._assert_public_knowledge_boundary_current()
                    return ExternalKnowledgeRouteDecision(
                        disposition=ExternalKnowledgeRouteDisposition.CLARIFY,
                        reason_code="external_relation_semantically_ambiguous",
                        reason="외부 관계의 대상·시점·정렬 조건을 하나로 확정할 수 없음",
                    )
                return self._answer_atomically(question, request_id)
            if has_external_requirements:
                assert knowledge_draft is not None
                knowledge_identity_surfaces = tuple(
                    dict.fromkeys(product_mentions)
                )
                if self.external_knowledge_router is None:
                    # A valid holdings/theme/corporate/risk/fund-document
                    # question does not become an unsupported action merely
                    # because this runtime profile has no live evidence
                    # executor. Compile the same server-owned typed plan, then
                    # remove execution authority and expose KNOWLEDGE_SEARCH.
                    # The public adapter maps that state to CANNOT_VERIFY and
                    # never falls through to a broad SQL search or the legacy
                    # relation router.
                    unavailable_route = ExternalKnowledgeRouter().route(
                        question,
                        request_id,
                        knowledge_draft,
                    )
                    if (
                        unavailable_route.disposition
                        is ExternalKnowledgeRouteDisposition.EXECUTE
                    ):
                        assert unavailable_route.plan is not None
                        candidate_plan = type(unavailable_route.plan).model_validate(
                            {
                                **unavailable_route.plan.model_dump(mode="python"),
                                "execution_authorized": False,
                            }
                        )
                        unavailable_route = ExternalKnowledgeRouteDecision(
                            disposition=(
                                ExternalKnowledgeRouteDisposition.KNOWLEDGE_SEARCH
                            ),
                            reason_code="external_knowledge_runtime_unavailable",
                            reason=(
                                "정상 금융상품 지식 질문이지만 이 실행 환경에는 "
                                "검증된 공식 외부 근거 실행기가 조립되지 않음"
                            ),
                            plan=candidate_plan,
                        )
                    self._assert_public_knowledge_boundary_current()
                    return unavailable_route
                source_selection_signals = None
                if self.external_universe_builder is not None:
                    source_selection_signals = (
                        self.external_universe_builder.source_selection_signals(
                            families=tuple(
                                sorted(
                                    set(knowledge_draft.product_families),
                                    key=lambda item: item.value,
                                )
                            ),
                            question=question,
                            identity_surfaces=knowledge_identity_surfaces,
                        )
                    )
                external_decision = self.external_knowledge_router.route(
                    question,
                    request_id,
                    knowledge_draft,
                    source_selection_signals=source_selection_signals,
                )
                if external_decision.disposition is not (
                    ExternalKnowledgeRouteDisposition.NOT_APPLICABLE
                ):
                    if (
                        external_decision.plan is not None
                        and self.external_knowledge_runtime is not None
                    ):
                        assert self.external_universe_builder is not None
                        assert self.external_search_plan_compiler is not None
                        families = tuple(
                            sorted(
                                {
                                    family
                                    for requirement in external_decision.plan.requirements
                                    for family in requirement.product_families
                                },
                                key=lambda item: item.value,
                            )
                        )
                        try:
                            candidate_ids_by_family = (
                                self._external_db_prefilter_candidate_ids(
                                    question=question,
                                    request_id=request_id,
                                    external_plan=external_decision.plan,
                                    families=families,
                                    identity_surfaces=knowledge_identity_surfaces,
                                )
                            )
                        except PlanCompilationBlockedError:
                            self._assert_public_knowledge_boundary_current()
                            return ExternalKnowledgeRouteDecision(
                                disposition=ExternalKnowledgeRouteDisposition.CLARIFY,
                                reason_code="external_db_prefilter_incomplete",
                                reason=(
                                    "외부 관계와 함께 요청한 제공 DB 조건을 모두 "
                                    "안전하게 확정할 수 없음"
                                ),
                            )
                        universe = self.external_universe_builder.build(
                            families=families,
                            candidate_ids_by_family=candidate_ids_by_family,
                            identity_surfaces=knowledge_identity_surfaces,
                        )
                        search_plan = self.external_search_plan_compiler.compile(
                            external_decision.plan,
                            universe,
                        )
                        result = self.external_knowledge_runtime.execute(
                            search_plan,
                            universe,
                            evidence_execution_authorized=(
                                external_decision.disposition
                                is ExternalKnowledgeRouteDisposition.EXECUTE
                            ),
                        )
                        self._assert_public_knowledge_boundary_current()
                        return result
                    if external_decision.disposition is ExternalKnowledgeRouteDisposition.EXECUTE:
                        raise AgentReleaseError(
                            AgentReleaseCode.RUNTIME_MISMATCH,
                            "external knowledge execution has no signed public executor",
                        )
                    self._assert_public_knowledge_boundary_current()
                    return external_decision
            if self.knowledge_router is None:
                return self._answer_atomically(question, request_id)
            decision = self.knowledge_router.route_after_safety_gate(
                safety.normalized_question,
                request_id,
                safety_gate_passed=True,
            )
            if decision.disposition is KnowledgeRouteDisposition.NOT_APPLICABLE:
                return self._answer_atomically(question, request_id)
            if not self._knowledge_intent_is_verified(decision, intent_draft):
                decision = KnowledgeRouteDecision(
                    disposition=KnowledgeRouteDisposition.CLARIFY,
                    reason_code="hclx_intent_conflict",
                    reason="HCLX Intent 분석과 서버 해석이 일치하지 않아 조건 확인이 필요함",
                )
            try:
                # A control decision performs no retrieval, so this is its post-route
                # release check. Executable found/not-found paths additionally guard
                # both sides of KnowledgeAgent execution below.
                if decision.disposition is not KnowledgeRouteDisposition.EXECUTE:
                    self._assert_public_knowledge_boundary_current()
                return self._execute_knowledge_audited(decision, question, request_id)
            except KnowledgeRoutedExecutionError:
                raise
            except Exception as error:
                raise KnowledgeRoutedExecutionError(decision, error) from error
        finally:
            _PUBLIC_INTENT_ANALYSIS.reset(token)

    def _analyze_public_intent(
        self,
        question: str,
        request_id: str,
    ) -> IntentAnalysisDraft | None:
        provider = self.intent_analysis_provider
        if provider is None:
            if self.require_official_hclx:
                raise HyperClovaXConfigurationError(
                    "official request requires HyperCLOVA X Intent analysis"
                )
            return None
        audit = current_request_audit()
        started = perf_counter()
        self._increment_audit_metric(MetricCounter.HCLX_CALLS)
        try:
            draft = provider.analyze_intent(question, request_id)
        except Exception as error:
            if audit is not None:
                failure_reason = hyperclova_failure_reason(error)
                audit.emit(
                    stage=AuditStage.HCLX,
                    outcome=(
                        AuditOutcome.TIMED_OUT
                        if failure_reason == "timed_out"
                        else AuditOutcome.FAILED
                    ),
                    reason_code=f"intent_{failure_reason}",
                    duration_ms=(perf_counter() - started) * 1000,
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.HCLX,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="intent_analysis_completed",
                duration_ms=(perf_counter() - started) * 1000,
            )
        return draft

    @staticmethod
    def _knowledge_intent_is_verified(
        decision: KnowledgeRouteDecision,
        draft: IntentAnalysisDraft | None,
    ) -> bool:
        if draft is None or decision.disposition is not KnowledgeRouteDisposition.EXECUTE:
            return True
        assert decision.plan is not None
        # HCX supplies the mandatory Intent receipt, but the deterministic
        # relation router owns operation and family scope.  Intent/family label
        # drift is therefore advisory just like the SQL path; only invented
        # evidence may veto an otherwise verified server plan.
        return all(span in decision.plan.question for span in draft.evidence_spans)

    def _execute_knowledge_audited(
        self,
        decision: KnowledgeRouteDecision,
        question: str,
        request_id: str,
    ) -> KnowledgeAgentResult | KnowledgeRouteDecision:
        if self.audit_sink is None:
            return self._execute_knowledge_route_checked(decision)
        inherited = current_request_audit()
        if inherited is not None and inherited.sink is self.audit_sink:
            recorder = inherited.with_request(request_id=request_id, question=question)
        else:
            release = self.release_guard
            recorder = RequestAuditRecorder(
                request_id=request_id,
                question=question,
                sink=self.audit_sink,
                agent_release_id=(release.release_id if release is not None else None),
                agent_release_manifest_sha256=(
                    release.manifest_file_sha256 if release is not None else None
                ),
                deployment_binding_sha256=(
                    release.binding_file_sha256 if release is not None else None
                ),
                release_context_sha256=(
                    release.release_context_sha256 if release is not None else None
                ),
            )
        started = perf_counter()
        with bind_request_audit(recorder):
            try:
                result = self._execute_knowledge_route_checked(decision)
            except Exception as error:
                self._emit_knowledge_terminal_audit(
                    recorder,
                    started=started,
                    result=None,
                    decision=decision,
                    error=error,
                )
                raise
            self._emit_knowledge_terminal_audit(
                recorder,
                started=started,
                result=result,
                decision=result if isinstance(result, KnowledgeRouteDecision) else decision,
                error=None,
            )
            return result

    def _execute_knowledge_route_checked(
        self,
        decision: KnowledgeRouteDecision,
    ) -> KnowledgeAgentResult | KnowledgeRouteDecision:
        resolved = decision
        if (
            decision.disposition is KnowledgeRouteDisposition.EXECUTE
            and self.knowledge_agent is None
        ):
            resolved = KnowledgeRouteDecision(
                disposition=KnowledgeRouteDisposition.UNSUPPORTED,
                reason_code="knowledge_retrieval_not_activated",
                reason="관계 검색이 현재 공개 릴리스에서 활성화되지 않음",
            )
        self._emit_knowledge_route_audit(resolved)
        if resolved.disposition is not KnowledgeRouteDisposition.EXECUTE:
            return resolved
        try:
            raise_if_request_stopped()
            if self.release_guard is not None:
                self.release_guard.assert_request_current()
            self._assert_signed_knowledge_current()
            if self.require_approved_databases:
                require_approved_database_paths(self.database_paths)
            raise_if_request_stopped()
        except RequestDeadlineExceeded:
            self._emit_knowledge_boundary_failure(
                resolved,
                stage=AuditStage.AUTHORITY,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="knowledge_authority_timed_out",
            )
            raise
        except Exception:
            self._emit_knowledge_boundary_failure(
                resolved,
                stage=AuditStage.AUTHORITY,
                reason_code="knowledge_authority_rejected",
            )
            raise
        try:
            assert resolved.plan is not None
            assert self.knowledge_agent is not None
            result = self.knowledge_agent.execute(resolved.plan)
        except (
            KnowledgeServiceError,
            KnowledgePlanAuthorityError,
            RelationIndexError,
            RequestDeadlineExceeded,
            sqlite3.Error,
        ):
            raise
        except Exception:
            # KnowledgeAgent owns its SQL/verifier boundary events.  An
            # unexpected exception is deliberately left without a fabricated
            # boundary so the audit validator can reject an impossible trace.
            raise
        try:
            if self.require_approved_databases:
                require_approved_database_paths(self.database_paths)
            self._assert_signed_knowledge_current()
            if self.release_guard is not None:
                self.release_guard.assert_request_current()
            raise_if_request_stopped()
            return result
        except RequestDeadlineExceeded:
            self._emit_knowledge_boundary_failure(
                resolved,
                stage=AuditStage.AUTHORITY,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="knowledge_authority_timed_out",
            )
            raise
        except Exception:
            self._emit_knowledge_boundary_failure(
                resolved,
                stage=AuditStage.AUTHORITY,
                reason_code="knowledge_post_authority_rejected",
            )
            raise

    def _emit_knowledge_boundary_failure(
        self,
        decision: KnowledgeRouteDecision,
        *,
        stage: AuditStage,
        outcome: AuditOutcome = AuditOutcome.FAILED,
        reason_code: str,
    ) -> None:
        audit = current_request_audit()
        if audit is None or decision.plan is None:
            return
        operation = decision.plan.operation
        audit.emit(
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=0,
            route_disposition=RouteDisposition.EXECUTE,
            interaction_intent=InteractionIntent.SEARCH,
            product_families=tuple(operation.product_families),
            plan_sha256=canonical_knowledge_plan_sha256(decision.plan),
            relation_set_sha256=(
                self.knowledge_agent.relation_set_sha256
                if self.knowledge_agent is not None
                else None
            ),
        )

    def _emit_knowledge_route_audit(self, decision: KnowledgeRouteDecision) -> None:
        audit = current_request_audit()
        if audit is None:
            return
        operation = decision.plan.operation if decision.plan is not None else None
        families = tuple(operation.product_families) if operation is not None else ()
        disposition = {
            KnowledgeRouteDisposition.EXECUTE: RouteDisposition.EXECUTE,
            KnowledgeRouteDisposition.CLARIFY: RouteDisposition.CLARIFY,
            KnowledgeRouteDisposition.UNSUPPORTED: RouteDisposition.UNSUPPORTED,
        }.get(decision.disposition)
        if disposition is None:
            raise ValueError("non-applicable knowledge route cannot be audited as public")
        outcome = {
            KnowledgeRouteDisposition.EXECUTE: AuditOutcome.SUCCEEDED,
            KnowledgeRouteDisposition.CLARIFY: AuditOutcome.CLARIFIED,
            KnowledgeRouteDisposition.UNSUPPORTED: AuditOutcome.UNSUPPORTED,
        }[decision.disposition]
        intent = {
            KnowledgeRouteDisposition.EXECUTE: InteractionIntent.SEARCH,
            KnowledgeRouteDisposition.CLARIFY: InteractionIntent.CLARIFY,
            KnowledgeRouteDisposition.UNSUPPORTED: InteractionIntent.UNSUPPORTED,
        }[decision.disposition]
        audit.emit(
            stage=AuditStage.SAFETY,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="guard_allowed",
            duration_ms=0,
        )
        audit.emit(
            stage=AuditStage.ROUTE,
            outcome=outcome,
            reason_code={
                KnowledgeRouteDisposition.EXECUTE: "knowledge_routed_execute",
                KnowledgeRouteDisposition.CLARIFY: "knowledge_routed_clarify",
                KnowledgeRouteDisposition.UNSUPPORTED: "knowledge_routed_unsupported",
            }[decision.disposition],
            duration_ms=0,
            route_disposition=disposition,
            interaction_intent=intent,
            product_families=families,
            relation_set_sha256=(
                self.knowledge_agent.relation_set_sha256
                if decision.disposition is KnowledgeRouteDisposition.EXECUTE
                and self.knowledge_agent is not None
                else None
            ),
        )
        if decision.plan is not None:
            audit.emit(
                stage=AuditStage.COMPILER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="knowledge_plan_compiled",
                duration_ms=0,
                route_disposition=RouteDisposition.EXECUTE,
                interaction_intent=InteractionIntent.SEARCH,
                product_families=families,
                plan_sha256=canonical_knowledge_plan_sha256(decision.plan),
                relation_set_sha256=(
                    self.knowledge_agent.relation_set_sha256
                    if self.knowledge_agent is not None
                    else None
                ),
            )

    def _emit_knowledge_terminal_audit(
        self,
        recorder: RequestAuditRecorder,
        *,
        started: float,
        result: KnowledgeAgentResult | KnowledgeRouteDecision | None,
        decision: KnowledgeRouteDecision,
        error: Exception | None,
    ) -> None:
        duration_ms = (perf_counter() - started) * 1000
        operation = decision.plan.operation if decision.plan is not None else None
        families = tuple(operation.product_families) if operation is not None else ()
        plan_sha256 = (
            canonical_knowledge_plan_sha256(decision.plan) if decision.plan is not None else None
        )
        relation_set_sha256 = (
            self.knowledge_agent.relation_set_sha256 if self.knowledge_agent is not None else None
        )
        if result is None:
            timed_out = isinstance(
                error,
                HyperClovaXTimeoutError | RequestDeadlineExceeded | TimeoutError,
            )
            recorder.emit(
                stage=AuditStage.ANSWER,
                outcome=(AuditOutcome.TIMED_OUT if timed_out else AuditOutcome.FAILED),
                reason_code=(
                    "knowledge_deadline_exceeded" if timed_out else "knowledge_execution_failed"
                ),
                duration_ms=duration_ms,
                route_disposition=RouteDisposition.EXECUTE,
                interaction_intent=InteractionIntent.SEARCH,
                product_families=families,
                plan_sha256=plan_sha256,
                relation_set_sha256=relation_set_sha256,
            )
            self._increment_audit_metric(MetricCounter.REQUESTS)
            if timed_out:
                self._increment_audit_metric(MetricCounter.TIMEOUTS)
            return
        if isinstance(result, KnowledgeRouteDecision):
            clarify = result.disposition is KnowledgeRouteDisposition.CLARIFY
            recorder.emit(
                stage=AuditStage.ANSWER,
                outcome=(AuditOutcome.CLARIFIED if clarify else AuditOutcome.UNSUPPORTED),
                reason_code=(
                    "knowledge_execution_clarified"
                    if clarify
                    else "knowledge_execution_unsupported"
                ),
                duration_ms=duration_ms,
                route_disposition=(
                    RouteDisposition.CLARIFY if clarify else RouteDisposition.UNSUPPORTED
                ),
                interaction_intent=(
                    InteractionIntent.CLARIFY if clarify else InteractionIntent.UNSUPPORTED
                ),
            )
            self._increment_audit_metric(MetricCounter.REQUESTS)
            self._increment_audit_metric(
                MetricCounter.CLARIFICATIONS if clarify else MetricCounter.UNSUPPORTED
            )
            return
        assert decision.plan is not None
        evidence = result.relation_response.evidence if result.relation_response is not None else ()
        reason_code = {
            "deterministic_fallback": "knowledge_execution_fallback",
            "structured_grounded": "knowledge_execution_completed",
            "deterministic": (
                "knowledge_execution_not_found"
                if result.status == "not_found"
                else "knowledge_execution_completed"
            ),
        }[result.answer.mode]
        recorder.emit(
            stage=AuditStage.ANSWER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code=reason_code,
            duration_ms=duration_ms,
            route_disposition=RouteDisposition.EXECUTE,
            interaction_intent=InteractionIntent.SEARCH,
            product_families=families,
            plan_sha256=plan_sha256,
            relation_set_sha256=relation_set_sha256,
            candidate_count=result.candidate_count,
            result_count=len(evidence),
            evidence_count=len(evidence),
            evidence_ids=(item.evidence_id for item in evidence),
        )
        self._increment_audit_metric(MetricCounter.REQUESTS)
        self._increment_audit_metric(MetricCounter.ROUTE_EXECUTIONS)
        self._increment_audit_metric(MetricCounter.EVIDENCE_EXPECTED)
        self._increment_audit_metric(MetricCounter.EVIDENCE_PRESENT)
        if result.answer.mode == "deterministic_fallback":
            self._increment_audit_metric(MetricCounter.FALLBACKS)

    def _answer_atomically_checked(self, question: str, request_id: str) -> RoutedAgentResult:
        """Run atomic checks; audit correlation is owned by the outer wrapper."""

        if self.release_guard is not None:
            self.release_guard.assert_request_current()
        if self.require_approved_databases:
            require_approved_database_paths(self.database_paths)
        try:
            result = self._answer_once(
                question,
                request_id,
                capture_execution_error=True,
            )
        except RoutedExecutionError as error:
            if self.require_approved_databases:
                try:
                    require_approved_database_paths(self.database_paths)
                except Exception as approval_error:  # noqa: BLE001 - replace stale result cause
                    raise RoutedExecutionError(error.decision, approval_error) from approval_error
            if self.release_guard is not None:
                try:
                    self.release_guard.assert_request_current()
                except Exception as release_error:  # noqa: BLE001 - replace stale result cause
                    raise RoutedExecutionError(error.decision, release_error) from release_error
            raise
        if self.require_approved_databases:
            try:
                require_approved_database_paths(self.database_paths)
            except Exception as error:  # noqa: BLE001 - retain the trusted routed scope
                raise RoutedExecutionError(result.decision, error) from error
        if self.release_guard is not None:
            try:
                self.release_guard.assert_request_current()
            except Exception as error:  # noqa: BLE001 - retain trusted routed scope
                raise RoutedExecutionError(result.decision, error) from error
        return result

    def _execute_audited(
        self,
        question: str,
        request_id: str,
        *,
        atomic: bool,
    ) -> RoutedAgentResult:
        operation = self._answer_atomically_checked if atomic else self._answer_checked
        if self.audit_sink is None:
            return operation(question, request_id)
        inherited = current_request_audit()
        if inherited is not None and inherited.sink is self.audit_sink:
            # The HTTP middleware owns the transport-wide invocation ID. Reuse
            # its shared sequence state so REQUEST -> Core -> REQUEST is one
            # ordered audit chain rather than two unrelated traces.
            recorder = inherited.with_request(request_id=request_id, question=question)
        else:
            release = self.release_guard
            recorder = RequestAuditRecorder(
                request_id=request_id,
                question=question,
                sink=self.audit_sink,
                agent_release_id=(release.release_id if release is not None else None),
                agent_release_manifest_sha256=(
                    release.manifest_file_sha256 if release is not None else None
                ),
                deployment_binding_sha256=(
                    release.binding_file_sha256 if release is not None else None
                ),
                release_context_sha256=(
                    release.release_context_sha256 if release is not None else None
                ),
            )
        started = perf_counter()
        with bind_request_audit(recorder):
            try:
                result = operation(question, request_id)
            except Exception as error:
                decision = error.decision if isinstance(error, RoutedExecutionError) else None
                self._emit_terminal_audit(
                    recorder,
                    started=started,
                    result=None,
                    decision=decision,
                    error=error,
                )
                raise
            self._emit_terminal_audit(
                recorder,
                started=started,
                result=result,
                decision=result.decision,
                error=None,
            )
            return result

    @staticmethod
    def _decision_audit_fields(decision: RouteDecision) -> dict[str, object]:
        return {
            "route_disposition": decision.disposition,
            "interaction_intent": (
                InteractionIntent.UNSUPPORTED
                if decision.disposition is RouteDisposition.UNSUPPORTED
                else decision.draft.intent
            ),
            "product_families": tuple(decision.draft.product_families),
        }

    @staticmethod
    def _validated_audit_fields(validated_plan: ValidatedPlan) -> dict[str, object]:
        receipt = validated_plan.receipt
        fields: dict[str, object] = {
            "route_disposition": RouteDisposition.EXECUTE,
            "interaction_intent": receipt.capability_interaction_intent,
            "product_families": (receipt.dataset,),
            "plan_sha256": receipt.plan_sha256,
        }
        if receipt.approved_manifest_sha256 is not None:
            fields.update(
                dataset_release_id=receipt.dataset_release_id,
                approved_dataset_manifest_sha256=receipt.approved_manifest_sha256,
                database_manifest_sha256=receipt.database_manifest_sha256,
                database_snapshot_sha256=receipt.database_sha256,
                source_snapshot_sha256=receipt.source_file_sha256,
            )
        return fields

    @staticmethod
    def _result_audit_links(
        result: RoutedAgentResult,
    ) -> _AuditLinkage:
        if result.family_searches:
            product_ids = tuple(
                f"{family.product_family.value}:{product.product_id}"
                for family in result.family_searches
                for product in family.products
            )
        else:
            product_ids = tuple(product.product_id for product in result.products)
        evidence_ids: list[str] = []
        if result.family_searches:
            for family in result.family_searches:
                for product in family.products:
                    for field in product.fields:
                        evidence_ids.append(
                            f"{family.product_family.value}:{product.product_id}:"
                            f"{field.canonical_field}"
                        )
        else:
            for product in result.products:
                for field in product.fields:
                    evidence_ids.append(f"{product.product_id}:{field.canonical_field}")
        evidence_ids.extend(item.evidence_id for item in result.aggregates)
        evidence_ids.extend(
            cell.evidence_ref
            for comparison in result.comparisons
            for cell in comparison.cells
            if cell.evidence_ref is not None
        )
        return _bounded_audit_linkage(
            product_ids=product_ids,
            evidence_ids=tuple(evidence_ids),
        )

    def _terminal_audit_provenance(self, result: RoutedAgentResult) -> dict[str, object]:
        """Link the terminal execution to its ordered plans and approved datasets."""

        fields: dict[str, object] = {}
        if result.family_searches:
            plan_links = [
                {
                    "product_family": item.product_family.value,
                    "plan_sha256": query_plan_authority_sha256(item.query_plan),
                }
                for item in result.family_searches
            ]
            fields["plan_bundle_sha256"] = _canonical_sha256(plan_links)
        elif result.query_plan is not None:
            fields["plan_sha256"] = query_plan_authority_sha256(result.query_plan)

        if not self.require_approved_databases or result.status != "executed":
            return fields
        approval = load_approved_dataset_manifest()
        approved_manifest_sha256 = approval.canonical_sha256
        if result.family_searches:
            family_manifests = [
                (item.product_family, item.source_manifest) for item in result.family_searches
            ]
        else:
            if result.source_manifest is None:
                raise RuntimeError("executed result lost its approved source manifest")
            family_manifests = [
                (ProductFamily(result.source_manifest.dataset), result.source_manifest)
            ]
        dataset_links = []
        for family, source_manifest in family_manifests:
            approved = approval.datasets[family.value]
            dataset_links.append(
                {
                    "product_family": family.value,
                    "dataset_release_id": approval.release_id,
                    "approved_dataset_manifest_sha256": approved_manifest_sha256,
                    "database_manifest_sha256": _canonical_sha256(
                        source_manifest.model_dump(mode="json")
                    ),
                    "database_snapshot_sha256": approved.database_sha256,
                    "source_snapshot_sha256": approved.data_file_sha256,
                }
            )
        if len(dataset_links) == 1:
            item = dataset_links[0]
            fields.update(
                dataset_release_id=item["dataset_release_id"],
                approved_dataset_manifest_sha256=item["approved_dataset_manifest_sha256"],
                database_manifest_sha256=item["database_manifest_sha256"],
                database_snapshot_sha256=item["database_snapshot_sha256"],
                source_snapshot_sha256=item["source_snapshot_sha256"],
            )
        else:
            fields["dataset_bundle_sha256"] = _canonical_sha256(dataset_links)
        return fields

    def _emit_terminal_audit(
        self,
        recorder: RequestAuditRecorder,
        *,
        started: float,
        result: RoutedAgentResult | None,
        decision: RouteDecision | None,
        error: Exception | None,
    ) -> None:
        duration_ms = (perf_counter() - started) * 1000
        if result is None:
            audit_error = error.cause if isinstance(error, RoutedExecutionError) else error
            timed_out = isinstance(
                audit_error,
                HyperClovaXTimeoutError | RequestDeadlineExceeded | TimeoutError,
            )
            recorder.emit(
                stage=AuditStage.ANSWER,
                outcome=(AuditOutcome.TIMED_OUT if timed_out else AuditOutcome.FAILED),
                reason_code=("deadline_exceeded" if timed_out else "execution_failed"),
                duration_ms=duration_ms,
                **(self._decision_audit_fields(decision) if decision is not None else {}),
            )
            self._increment_audit_metric(MetricCounter.REQUESTS)
            if timed_out:
                self._increment_audit_metric(MetricCounter.TIMEOUTS)
            return

        linkage = self._result_audit_links(result)
        try:
            provenance = self._terminal_audit_provenance(result)
        except Exception:  # noqa: BLE001 - telemetry linkage cannot alter the Agent result
            self._increment_audit_metric(MetricCounter.AUDIT_SINK_FAILURES)
            provenance = (
                {"plan_sha256": query_plan_authority_sha256(result.query_plan)}
                if result.query_plan is not None
                else {}
            )
        deadline = current_request_deadline()
        completed_after_deadline = deadline is not None and deadline.should_stop()
        outcome = {
            "executed": AuditOutcome.SUCCEEDED,
            "clarify": AuditOutcome.CLARIFIED,
            "unsupported": AuditOutcome.UNSUPPORTED,
        }[result.status]
        reason_code = {
            "executed": "execution_completed",
            "clarify": "execution_clarified",
            "unsupported": "execution_unsupported",
        }[result.status]
        if completed_after_deadline:
            outcome = AuditOutcome.TIMED_OUT
            reason_code = "completed_after_deadline"
            self._increment_audit_metric(MetricCounter.TIMEOUTS)
        if (
            not completed_after_deadline
            and result.answer_composition is not None
            and result.answer_composition.mode == "deterministic_fallback"
        ):
            reason_code = "execution_fallback"
            self._increment_audit_metric(MetricCounter.FALLBACKS)
        recorder.emit(
            stage=AuditStage.ANSWER,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=duration_ms,
            candidate_count=result.candidate_count or 0,
            result_count=linkage.result_count,
            evidence_count=linkage.evidence_count,
            product_ids=linkage.product_ids,
            evidence_ids=linkage.evidence_ids,
            **provenance,
            **self._decision_audit_fields(result.decision),
        )
        self._increment_audit_metric(MetricCounter.REQUESTS)
        if result.status == "executed":
            self._increment_audit_metric(MetricCounter.ROUTE_EXECUTIONS)
            self._increment_audit_metric(MetricCounter.EVIDENCE_EXPECTED)
            if linkage.evidence_count > 0 or result.candidate_count == 0:
                self._increment_audit_metric(MetricCounter.EVIDENCE_PRESENT)
            else:
                self._increment_audit_metric(MetricCounter.EVIDENCE_INCOMPLETE)
        elif result.status == "clarify":
            self._increment_audit_metric(MetricCounter.CLARIFICATIONS)
        else:
            self._increment_audit_metric(MetricCounter.UNSUPPORTED)

    def _increment_audit_metric(self, counter: MetricCounter) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink.metrics.increment(counter)
        except Exception:
            pass

    def _answer_once(
        self,
        question: str,
        request_id: str,
        *,
        capture_execution_error: bool = False,
    ) -> RoutedAgentResult:
        route_started = perf_counter()
        audit = current_request_audit()
        try:
            trace = self.router.route_with_planning(question, request_id)
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.ROUTE,
                    outcome=AuditOutcome.FAILED,
                    reason_code="routing_failed",
                    duration_ms=(perf_counter() - route_started) * 1000,
                )
            raise
        decision = trace.route_decision
        route_emitted = False
        try:
            decision = self._bind_database_product_identities(decision)
            decision = self._resolve_exact_identity_family(decision)
            intent_draft = _PUBLIC_INTENT_ANALYSIS.get()
            hclx_structure = (
                ()
                if intent_draft is None
                else tuple(
                    (
                        item.text_span,
                        tuple(item.product_families),
                        item.role,
                    )
                    for item in intent_draft.execution_clauses
                )
            )
            compound_plan: TypedCompoundPlan | None = None
            compound_authorization: CompoundAuthorizationReceipt | None = None
            compound_terminal = (
                decision.reason_code.startswith("safety_")
                or (
                    decision.disposition is RouteDisposition.UNSUPPORTED
                    and decision.reason_code != "semantic_unmapped_action"
                )
            )
            if not compound_terminal:
                try:
                    compound_plan = self.compound_query_engine.compile(
                        question,
                        request_id,
                        hclx_structure=hclx_structure,
                    )
                except ValueError:
                    decision = self._compound_control_decision(
                        decision,
                        reason_code="compound_plan_invalid",
                        reason=(
                            "복합 조건의 상품군·필터·정렬 구조를 안전한 "
                            "실행 계획으로 확정하지 못했습니다."
                        ),
                    )
                if (
                    compound_plan is None
                    and decision.disposition is RouteDisposition.EXECUTE
                    # Check the pre-resolution route as well: exact-identity
                    # family resolution must not collapse an originally
                    # multi-family compound request into one ordinary family.
                    and self._requires_multi_family_compound_plan(trace.route_decision)
                ):
                    decision = self._compound_control_decision(
                        trace.route_decision,
                        reason_code="compound_plan_required",
                        reason=(
                            "상품군별 복합 조건을 모두 보존하는 실행 계획을 "
                            "확정하지 못했습니다. 상품군별 조건을 다시 명시해 주세요."
                        ),
                    )
                elif (
                    compound_plan is None
                    and decision.disposition is RouteDisposition.EXECUTE
                    and self._requires_single_family_compound_plan(trace.route_decision)
                ):
                    decision = self._compound_control_decision(
                        trace.route_decision,
                        reason_code="compound_plan_required",
                        reason=(
                            "상대 비교 또는 단계별 후보 조건을 모두 보존하는 실행 계획을 "
                            "확정하지 못했습니다. 기준 상품과 각 조건을 다시 확인해 주세요."
                        ),
                    )
            if compound_plan is not None:
                # Compound grammar is not authority by itself.  Bind it to the
                # exact routed request only while the same approved DB and
                # immutable release checks used by the normal Oracle path are
                # current.  Safety/unsupported routes remain terminal inside
                # authorize_decision().
                self._assert_compound_authority_current()
                try:
                    decision, compound_authorization = (
                        self.compound_query_engine.authorize_decision(
                            decision,
                            compound_plan,
                            unresolved_spans=trace.planning_decision.unresolved_spans,
                        )
                    )
                except ValueError:
                    decision = self._compound_control_decision(
                        decision,
                        reason_code="compound_authorization_invalid",
                        reason=(
                            "복합 실행 계획을 현재 요청·상품군·승인 데이터와 "
                            "일치하는 권한으로 검증하지 못했습니다."
                        ),
                    )
                    compound_plan = None
                    compound_authorization = None
                else:
                    self._assert_compound_authority_current()
                    if (
                        decision.disposition is RouteDisposition.EXECUTE
                        and compound_authorization is None
                    ):
                        decision = self._compound_control_decision(
                            decision,
                            reason_code="compound_authorization_missing",
                            reason=(
                                "복합 실행 계획의 서버 권한 증명을 완성하지 "
                                "못해 조건을 다시 확인해야 합니다."
                            ),
                        )
                        compound_plan = None
            planning_decision = trace.planning_decision
            if decision != trace.route_decision:
                # Exact product identity is a server-owned DB resolution, not
                # a caller-provided RouteDecision. Recompute the shadow record
                # from the final route so CONTROL metadata cannot silently
                # accompany an executable plan.
                planning_started = perf_counter()
                planning_decision = AdaptiveShadowPlanningPolicy().decide(
                    decision,
                    SemanticCoverageDecision(),
                )
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.PLANNING,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="policy_recomputed",
                        duration_ms=(perf_counter() - planning_started) * 1000,
                        **self._decision_audit_fields(decision),
                    )
            safety_terminal = (
                decision.disposition is not RouteDisposition.EXECUTE
                and decision.reason_code.startswith("safety_")
            )
            if self.require_official_hclx and intent_draft is None and not safety_terminal:
                raise HyperClovaXConfigurationError(
                    "official request lost its HyperCLOVA X Intent analysis receipt"
                )
            # A server-owned SafetyEnvelope terminal decision has the highest
            # precedence and is completed without sending blocked text to HCLX.
            if intent_draft is not None and not decision.reason_code.startswith("safety_"):
                intent_verification = verify_intent_analysis(
                    decision.draft.question,
                    decision,
                    intent_draft,
                    require_execution_structure=self.require_official_hclx,
                )
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=(
                            AuditOutcome.SUCCEEDED
                            if intent_verification.passed
                            else AuditOutcome.BLOCKED
                        ),
                        reason_code=intent_verification.reason_code,
                        duration_ms=0,
                        **self._decision_audit_fields(decision),
                    )
                if not intent_verification.passed:
                    return self._control_result(
                        decision,
                        disposition=RouteDisposition.CLARIFY,
                        reason=(
                            "HCLX Intent 분석과 서버의 안전한 해석이 일치하지 않아 "
                            "상품군과 요청 기준을 다시 확인해야 합니다."
                        ),
                    )
            if audit is not None:
                audit.emit(
                    stage=AuditStage.ROUTE,
                    outcome={
                        RouteDisposition.EXECUTE: AuditOutcome.SUCCEEDED,
                        RouteDisposition.CLARIFY: AuditOutcome.CLARIFIED,
                        RouteDisposition.UNSUPPORTED: AuditOutcome.UNSUPPORTED,
                    }[decision.disposition],
                    reason_code={
                        RouteDisposition.EXECUTE: "routed_execute",
                        RouteDisposition.CLARIFY: "routed_clarify",
                        RouteDisposition.UNSUPPORTED: "routed_unsupported",
                    }[decision.disposition],
                    duration_ms=(perf_counter() - route_started) * 1000,
                    **self._decision_audit_fields(decision),
                )
                route_emitted = True
            return self._answer_from_decision(
                decision,
                planning_decision,
                trace,
                compound_plan=compound_plan,
                compound_authorization=compound_authorization,
            )
        except Exception as error:  # noqa: BLE001 - private atomic error transport
            if audit is not None and not route_emitted:
                audit.emit(
                    stage=AuditStage.ROUTE,
                    outcome=AuditOutcome.FAILED,
                    reason_code="routing_failed",
                    duration_ms=(perf_counter() - route_started) * 1000,
                    **self._decision_audit_fields(decision),
                )
            if capture_execution_error:
                raise RoutedExecutionError(decision, error) from error
            raise

    def _answer_from_decision(
        self,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
        trace: PlanningTrace,
        *,
        compound_plan: TypedCompoundPlan | None = None,
        compound_authorization: CompoundAuthorizationReceipt | None = None,
    ) -> RoutedAgentResult:
        request_id = decision.draft.request_id
        semantic_plan: QueryPlan | None = None
        semantic_planning_decision: BaseModel | None = None
        semantic_authority_receipts: tuple[BaseModel, ...] = ()
        if compound_plan is not None and decision.disposition is RouteDisposition.EXECUTE:
            if compound_authorization is None:
                raise PlanExecutionBlockedError(
                    "compound execution requires a server authorization receipt"
                )
            # HAVING-like valid-value thresholds and aggregate-output ordering
            # are sealed in TypedCompoundPlan but cannot be represented by the
            # public QueryPlan DTO.  Execute every compound shape through its
            # dedicated deterministic executor so those semantics cannot be
            # dropped while adapting to the ordinary aggregate Oracle.
            return self._answer_compound_query(
                decision,
                compound_plan,
                compound_authorization=compound_authorization,
            )
        if (
            semantic_plan is None
            and planning_decision.path is PlanningPath.SCHEMA_LINK_SHADOW
            and self.adaptive_semantic_resolver is not None
        ):
            semantic_started = perf_counter()
            outcome = self.adaptive_semantic_resolver.resolve(trace)
            if outcome.dense_attempted:
                self._increment_audit_metric(MetricCounter.DENSE_CALLS)
            if outcome.hclx_attempted:
                self._increment_audit_metric(MetricCounter.HCLX_CALLS)
            semantic_audit = current_request_audit()
            if semantic_audit is not None and outcome.dense_attempted:
                semantic_audit.emit(
                    stage=AuditStage.DENSE,
                    outcome={
                        "resolved": AuditOutcome.SUCCEEDED,
                        "clarify": AuditOutcome.CLARIFIED,
                        "unsupported": AuditOutcome.UNSUPPORTED,
                    }[outcome.status],
                    reason_code=outcome.reason_code,
                    duration_ms=(perf_counter() - semantic_started) * 1000,
                    route_disposition=decision.disposition,
                    interaction_intent=decision.draft.intent,
                    product_families=decision.draft.product_families,
                    candidate_count=outcome.candidate_count,
                    index_manifest_sha256=outcome.index_manifest_sha256,
                    model_revision_sha256=outcome.model_revision_sha256,
                    model_snapshot_manifest_sha256=(outcome.model_snapshot_manifest_sha256),
                )
            if outcome.status != "resolved":
                disposition = (
                    RouteDisposition.UNSUPPORTED
                    if outcome.status == "unsupported"
                    else RouteDisposition.CLARIFY
                )
                return self._control_result(
                    decision,
                    disposition=disposition,
                    reason=(
                        "제공 데이터의 필드로 지원할 수 없는 의미입니다."
                        if disposition is RouteDisposition.UNSUPPORTED
                        else "의미가 모호한 조건의 데이터 필드를 하나로 확정해 주세요."
                    ),
                )
            assert outcome.hard_filter_lock is not None
            assert outcome.receipt is not None
            try:
                semantic_plan = self.compiler.compile_with_semantic_resolution(
                    decision,
                    hard_filter_lock=outcome.hard_filter_lock,
                    receipts=(outcome.receipt,),
                )
                semantic_planning_decision = outcome.planning_decision
                semantic_authority_receipts = (outcome.receipt,)
            except PlanCompilationBlockedError as error:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.CLARIFY,
                    reason=str(error),
                )
        if (
            decision.disposition is RouteDisposition.EXECUTE
            and ProductFamily.FUND in decision.draft.product_families
            and not self._fund_execution_enabled()
        ):
            return self._control_result(
                decision,
                disposition=RouteDisposition.UNSUPPORTED,
                reason=(
                    "공모펀드 공식 실행은 승인 플래그가 잠겨 있어 현재 공개 경로에서 "
                    "처리할 수 없습니다."
                ),
            )
        if len(decision.draft.product_families) > 1:
            if decision.disposition is not RouteDisposition.EXECUTE:
                return self._control_result(decision)
            return self._answer_cross_family_search(decision, planning_decision)
        audit = current_request_audit()
        compiler_started = perf_counter()
        try:
            compiled = (
                (decision, semantic_plan)
                if semantic_plan is not None
                else self._compile_server_plan(decision)
            )
        except PlanCompilationBlockedError as error:
            if audit is not None and decision.disposition is RouteDisposition.EXECUTE:
                audit.emit(
                    stage=AuditStage.COMPILER,
                    outcome=AuditOutcome.BLOCKED,
                    reason_code="plan_blocked",
                    duration_ms=(perf_counter() - compiler_started) * 1000,
                    **self._decision_audit_fields(decision),
                )
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason=str(error),
            )
        if compiled is None:
            if audit is not None and decision.disposition is RouteDisposition.EXECUTE:
                audit.emit(
                    stage=AuditStage.COMPILER,
                    outcome=AuditOutcome.BLOCKED,
                    reason_code="plan_unresolved",
                    duration_ms=(perf_counter() - compiler_started) * 1000,
                    **self._decision_audit_fields(decision),
                )
            if decision.disposition is not RouteDisposition.EXECUTE:
                return self._control_result(decision)
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason="질문의 실행 조건을 안전한 계획으로 확정하지 못했습니다.",
            )
        decision, plan = compiled
        plan = _lock_approved_fund_aggregate_scope(plan)
        if audit is not None:
            audit.emit(
                stage=AuditStage.COMPILER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="plan_compiled",
                duration_ms=(perf_counter() - compiler_started) * 1000,
                plan_sha256=query_plan_authority_sha256(plan),
                **self._decision_audit_fields(decision),
            )
        # A blocked server plan must stop before any advisory provider call.
        # The same contract is checked again after provider admission below.
        try:
            self._require_execution_authority(decision, plan)
            self._require_execution(plan)
        except PlanExecutionBlockedError as error:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.AUTHORITY,
                    outcome=AuditOutcome.BLOCKED,
                    reason_code="authority_denied",
                    duration_ms=0,
                    plan_sha256=query_plan_authority_sha256(plan),
                    **self._decision_audit_fields(decision),
                )
            disposition = (
                RouteDisposition.UNSUPPORTED
                if plan.unsupported_conditions
                else RouteDisposition.CLARIFY
            )
            answer = render_blocked_plan(plan, plan.product_families[0].value)
            return self._control_result(
                decision,
                disposition=disposition,
                reason=f"{answer} {error}",
                plan=plan,
            )

        # Preserve the existing user-facing CLARIFY/UNSUPPORTED classification
        # before resolving infrastructure. PlanAuthorityGate repeats these
        # checks on the final canonical proposal immediately before DB access.
        try:
            self._require_execution_authority(decision, plan)
            self._require_execution(plan)
        except PlanExecutionBlockedError as error:
            disposition = (
                RouteDisposition.UNSUPPORTED
                if plan.unsupported_conditions
                else RouteDisposition.CLARIFY
            )
            answer = render_blocked_plan(plan, plan.product_families[0].value)
            return self._control_result(
                decision,
                disposition=disposition,
                reason=f"{answer} {error}",
                plan=plan,
            )

        family = plan.product_families[0]
        try:
            database_path = self.database_paths[family]
        except KeyError:
            return self._control_result(
                decision,
                disposition=RouteDisposition.UNSUPPORTED,
                reason=f"{family.value} database path is not configured",
                plan=plan,
            )
        try:
            authority_started = perf_counter()
            validated_plan = self.plan_authority_gate.validate_routed(
                plan,
                decision,
                planning_decision=(semantic_planning_decision or planning_decision),
                semantic_receipts=semantic_authority_receipts,
                compiler_kind=PlanCompilerKind.SERVER_QUERY_PLAN,
                proposal_provider_name=None,
                proposal_model_name=None,
            )
            plan = validated_plan.canonical_plan
        except PlanAuthorityError as error:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.AUTHORITY,
                    outcome=(
                        AuditOutcome.TIMED_OUT
                        if error.code is PlanAuthorityCode.DEADLINE_EXCEEDED
                        else AuditOutcome.BLOCKED
                    ),
                    reason_code=(
                        "deadline_exceeded"
                        if error.code is PlanAuthorityCode.DEADLINE_EXCEEDED
                        else "authority_denied"
                    ),
                    duration_ms=(perf_counter() - authority_started) * 1000,
                    plan_sha256=query_plan_authority_sha256(plan),
                    **self._decision_audit_fields(decision),
                )
            if error.code is not PlanAuthorityCode.EXECUTION_POLICY_BLOCKED:
                raise
            disposition = (
                RouteDisposition.UNSUPPORTED
                if plan.unsupported_conditions
                else RouteDisposition.CLARIFY
            )
            answer = render_blocked_plan(plan, plan.product_families[0].value)
            return self._control_result(
                decision,
                disposition=disposition,
                reason=f"{answer} {error}",
                plan=plan,
            )
        except PlanExecutionBlockedError as error:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.AUTHORITY,
                    outcome=AuditOutcome.BLOCKED,
                    reason_code="authority_denied",
                    duration_ms=(perf_counter() - authority_started) * 1000,
                    plan_sha256=query_plan_authority_sha256(plan),
                    **self._decision_audit_fields(decision),
                )
            disposition = (
                RouteDisposition.UNSUPPORTED
                if plan.unsupported_conditions
                else RouteDisposition.CLARIFY
            )
            answer = render_blocked_plan(plan, plan.product_families[0].value)
            return self._control_result(
                decision,
                disposition=disposition,
                reason=f"{answer} {error}",
                plan=plan,
            )
        if audit is not None:
            audit.emit(
                stage=AuditStage.AUTHORITY,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="authority_granted",
                duration_ms=(perf_counter() - authority_started) * 1000,
                **self._validated_audit_fields(validated_plan),
            )
        if plan.intent is Intent.AGGREGATE:
            oracle_started = perf_counter()
            self._increment_audit_metric(MetricCounter.ORACLE_CALLS)
            self._increment_audit_metric(MetricCounter.SQL_EXECUTIONS)
            try:
                executed_aggregation = SQLiteAggregateOracle(database_path).execute(validated_plan)
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.ORACLE,
                        outcome=AuditOutcome.FAILED,
                        reason_code="oracle_failed",
                        duration_ms=(perf_counter() - oracle_started) * 1000,
                        **self._validated_audit_fields(validated_plan),
                    )
                raise
            if audit is not None:
                audit.emit(
                    stage=AuditStage.ORACLE,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="oracle_completed",
                    duration_ms=(perf_counter() - oracle_started) * 1000,
                    candidate_count=executed_aggregation.candidate_count,
                    **self._validated_audit_fields(validated_plan),
                )
            verifier_started = perf_counter()
            try:
                universe_started = perf_counter()
                try:
                    universe = self._record_universe(database_path, validated_plan)
                except Exception:
                    if audit is not None:
                        audit.emit(
                            stage=AuditStage.VERIFIER,
                            outcome=AuditOutcome.FAILED,
                            reason_code="verifier_universe_failed",
                            duration_ms=(perf_counter() - universe_started) * 1000,
                            candidate_count=executed_aggregation.candidate_count,
                            **self._validated_audit_fields(validated_plan),
                        )
                    raise
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="verifier_universe_loaded",
                        duration_ms=(perf_counter() - universe_started) * 1000,
                        candidate_count=len(universe),
                        **self._validated_audit_fields(validated_plan),
                    )
                pure_verifier_started = perf_counter()
                try:
                    verified_aggregation = AggregateResultVerifier().verify(
                        plan,
                        executed_aggregation,
                        universe,
                    )
                except Exception:
                    if audit is not None:
                        audit.emit(
                            stage=AuditStage.VERIFIER,
                            outcome=AuditOutcome.FAILED,
                            reason_code="pure_verification_failed",
                            duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                            candidate_count=executed_aggregation.candidate_count,
                            **self._validated_audit_fields(validated_plan),
                        )
                    raise
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="pure_verification_passed",
                        duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                        candidate_count=verified_aggregation.candidate_count,
                        **self._validated_audit_fields(validated_plan),
                    )
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.FAILED,
                        reason_code="verification_failed",
                        duration_ms=(perf_counter() - verifier_started) * 1000,
                        candidate_count=executed_aggregation.candidate_count,
                        **self._validated_audit_fields(validated_plan),
                    )
                    self._increment_audit_metric(MetricCounter.VERIFIER_FAILURES)
                raise
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="verification_passed",
                    duration_ms=(perf_counter() - verifier_started) * 1000,
                    candidate_count=verified_aggregation.candidate_count,
                    **self._validated_audit_fields(validated_plan),
                )
            response_candidate_count = _aggregate_response_candidate_count(
                plan,
                verified_aggregation,
                decision.draft.question,
            )
            renderer_started = perf_counter()
            try:
                aggregates = build_aggregate_evidence(plan, verified_aggregation)
                answer, warnings = render_verified_aggregation(
                    plan,
                    verified_aggregation,
                    aggregates,
                )
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.RENDERER,
                        outcome=AuditOutcome.FAILED,
                        reason_code="rendering_failed",
                        duration_ms=(perf_counter() - renderer_started) * 1000,
                        **self._validated_audit_fields(validated_plan),
                    )
                raise
            if audit is not None:
                linkage = _bounded_audit_linkage(
                    evidence_ids=tuple(item.evidence_id for item in aggregates),
                )
                audit.emit(
                    stage=AuditStage.RENDERER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="rendering_completed",
                    duration_ms=(perf_counter() - renderer_started) * 1000,
                    candidate_count=verified_aggregation.candidate_count,
                    evidence_count=linkage.evidence_count,
                    evidence_ids=linkage.evidence_ids,
                    **self._validated_audit_fields(validated_plan),
                )
            return RoutedAgentResult(
                request_id=request_id,
                status="executed",
                decision=decision,
                answer=answer,
                query_plan=plan,
                candidate_count=response_candidate_count,
                products=[],
                aggregates=aggregates,
                comparisons=[],
                warnings=warnings,
                source_manifest=verified_aggregation.manifest,
                answer_composition=None,
            )
        oracle_started = perf_counter()
        self._increment_audit_metric(MetricCounter.ORACLE_CALLS)
        self._increment_audit_metric(MetricCounter.SQL_EXECUTIONS)
        try:
            oracle = SQLiteOracle(database_path)
            executed = oracle.execute(validated_plan)
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.ORACLE,
                    outcome=AuditOutcome.FAILED,
                    reason_code="oracle_failed",
                    duration_ms=(perf_counter() - oracle_started) * 1000,
                    **self._validated_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.ORACLE,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="oracle_completed",
                duration_ms=(perf_counter() - oracle_started) * 1000,
                candidate_count=executed.candidate_count,
                result_count=len(executed.records),
                **self._validated_audit_fields(validated_plan),
            )
        verifier_started = perf_counter()
        try:
            universe = None
            if plan.intent is not Intent.COMPARE:
                universe_started = perf_counter()
                try:
                    universe = self._record_universe(database_path, validated_plan)
                except Exception:
                    if audit is not None:
                        audit.emit(
                            stage=AuditStage.VERIFIER,
                            outcome=AuditOutcome.FAILED,
                            reason_code="verifier_universe_failed",
                            duration_ms=(perf_counter() - universe_started) * 1000,
                            candidate_count=executed.candidate_count,
                            **self._validated_audit_fields(validated_plan),
                        )
                    raise
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="verifier_universe_loaded",
                        duration_ms=(perf_counter() - universe_started) * 1000,
                        candidate_count=len(universe),
                        **self._validated_audit_fields(validated_plan),
                    )
            pure_verifier_started = perf_counter()
            try:
                verified = ResultVerifier().verify(plan, executed, universe)
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.FAILED,
                        reason_code="pure_verification_failed",
                        duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                        candidate_count=executed.candidate_count,
                        **self._validated_audit_fields(validated_plan),
                    )
                raise
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="pure_verification_passed",
                    duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                    candidate_count=verified.candidate_count,
                    result_count=len(verified.records),
                    **self._validated_audit_fields(validated_plan),
                )
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.FAILED,
                    reason_code="verification_failed",
                    duration_ms=(perf_counter() - verifier_started) * 1000,
                    candidate_count=executed.candidate_count,
                    **self._validated_audit_fields(validated_plan),
                )
                self._increment_audit_metric(MetricCounter.VERIFIER_FAILURES)
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.VERIFIER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="verification_passed",
                duration_ms=(perf_counter() - verifier_started) * 1000,
                candidate_count=verified.candidate_count,
                result_count=len(verified.records),
                **self._validated_audit_fields(validated_plan),
            )
        renderer_started = perf_counter()
        try:
            products = build_product_evidence(plan, verified)
            comparisons: list[ComparisonEvidence] = []
            if plan.intent is Intent.COMPARE:
                comparison = build_product_comparison(plan, verified, products)
                verified = comparison.verified
                products = list(comparison.products)
                comparisons = build_comparison_evidence(comparison)
            answer, warnings = render_verified_search(plan, verified, products)
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.RENDERER,
                    outcome=AuditOutcome.FAILED,
                    reason_code="rendering_failed",
                    duration_ms=(perf_counter() - renderer_started) * 1000,
                    **self._validated_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            evidence_ids = tuple(
                f"{product.product_id}:{field.canonical_field}"
                for product in products
                for field in product.fields
            )
            linkage = _bounded_audit_linkage(
                product_ids=tuple(product.product_id for product in products),
                evidence_ids=evidence_ids,
            )
            audit.emit(
                stage=AuditStage.RENDERER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="rendering_completed",
                duration_ms=(perf_counter() - renderer_started) * 1000,
                candidate_count=verified.candidate_count,
                result_count=linkage.result_count,
                evidence_count=linkage.evidence_count,
                product_ids=linkage.product_ids,
                evidence_ids=linkage.evidence_ids,
                **self._validated_audit_fields(validated_plan),
            )
        return RoutedAgentResult(
            request_id=request_id,
            status="executed",
            decision=decision,
            answer=answer,
            query_plan=plan,
            candidate_count=verified.candidate_count,
            products=products,
            aggregates=[],
            comparisons=comparisons,
            warnings=warnings,
            source_manifest=verified.manifest,
            answer_composition=None,
        )

    def _answer_compound_query(
        self,
        decision: RouteDecision,
        compound_plan: TypedCompoundPlan,
        *,
        compound_authorization: CompoundAuthorizationReceipt,
        execution: CompoundExecution | None = None,
    ) -> RoutedAgentResult:
        self._assert_compound_authority_current()
        if execution is None:
            try:
                execution = self.compound_query_engine.execute_authorized(
                    compound_plan,
                    compound_authorization,
                )
            except NamedComparisonIncompleteError:
                self._assert_compound_authority_current()
                reason = (
                    "비교 대상으로 지정한 모든 상품에서 요청 지표의 유효한 값을 "
                    "확인할 수 없어 일부 상품만으로 비교하지 않았습니다. 다른 지표를 "
                    "지정하거나 비교 대상을 다시 확인해 주세요."
                )
                control = self._compound_control_decision(
                    decision,
                    reason_code="named_comparison_metric_unavailable",
                    reason=reason,
                )
                return self._control_result(control)
            except AggregateThresholdUnavailableError:
                self._assert_compound_authority_current()
                reason = (
                    "같은 후보군에서 집계 기준을 계산할 유효한 0이 아닌 수치가 없어 "
                    "평균·중앙값 조건을 실행하지 않았습니다. 다른 지표나 범위를 "
                    "지정해 주세요."
                )
                control = self._compound_control_decision(
                    decision,
                    reason_code="aggregate_threshold_unavailable",
                    reason=reason,
                )
                return self._control_result(control)
        else:
            self.compound_query_engine.verify_authorized_execution(
                execution,
                compound_authorization,
            )
        self._assert_compound_authority_current()
        family_results: list[FamilySearchResult] = []
        compound_aggregates: list[AggregateEvidence] = []
        registry = load_field_registry()
        for item in execution.families:
            manifest = self.record_cache.get(
                self.database_paths[item.plan.product_family]
            ).manifest
            family_results.append(
                FamilySearchResult(
                    product_family=item.plan.product_family,
                    status="success" if item.candidate_count else "not_found",
                    answer=execution.answer,
                    query_plan=item.query_plan,
                    candidate_count=item.candidate_count,
                    products=list(item.products),
                    warnings=[
                        "Typed Compound Plan으로 원천 필터·중간 shortlist·"
                        "최종 계산을 순서대로 실행했습니다."
                    ],
                    source_manifest=manifest,
                )
            )
            if execution.plan.kind in {
                "grouped_aggregate",
                "family_count_aggregate",
                "family_summary_aggregate",
            }:
                aggregate_specs = item.query_plan.intent_payload.aggregations
                group_fields = item.query_plan.intent_payload.group_by
                if not item.grouped_aggregates or len(group_fields) > 2:
                    raise ValueError("compound aggregate execution lost its evidence")
                for group_index, group in enumerate(item.grouped_aggregates, start=1):
                    if len(group.aggregates) != len(aggregate_specs):
                        raise ValueError("compound aggregate measure count changed")
                    if not group_fields:
                        group_values = {}
                    else:
                        raw_group_values = (
                            group.group_value
                            if isinstance(group.group_value, tuple)
                            else (group.group_value,)
                        )
                        if len(raw_group_values) != len(group_fields):
                            raise ValueError("compound aggregate group axes changed")
                        group_values = dict(
                            zip(group_fields, raw_group_values, strict=True)
                        )
                    group_source_columns = {
                        field: registry.require_field(
                            field, [item.plan.product_family.value]
                        ).source.columns
                        for field in group_fields
                    }
                    for value_index, (value, specification) in enumerate(
                        zip(group.aggregates, aggregate_specs, strict=True), start=1
                    ):
                        canonical = specification.field
                        if value.function is not specification.function:
                            raise ValueError("compound aggregate function changed")
                        definition = registry.require_field(
                            canonical,
                            [item.plan.product_family.value],
                        )
                        compound_aggregates.append(
                            AggregateEvidence(
                                evidence_id=(
                                    f"aggregate_{item.plan.product_family.value}_"
                                    f"{group_index}_{value_index}_"
                                    f"{value.function.value}_{canonical}"
                                ),
                                function=value.function,
                                field=canonical,
                                label=(
                                    f"{item.plan.product_family.value} 상품 수"
                                    if value.function.value == "count"
                                    else f"{item.plan.product_family.value} "
                                    f"{definition.label} {value.function.value}"
                                ),
                                value=(
                                    str(value.value)
                                    if isinstance(value.value, Decimal)
                                    else value.value
                                ),
                                unit=definition.unit,
                                group_values=group_values,
                                group_source_columns=group_source_columns,
                                row_count=group.row_count,
                                valid_count=value.valid_count,
                                missing_count=value.missing_count,
                                source_dataset=item.plan.product_family.value,
                                source_id=group.source_id,
                                source_columns=definition.source.columns,
                                source_snapshot_date=manifest.source_snapshot_date,
                                as_of_start=group.as_of,
                                as_of_end=group.as_of,
                                quality=(
                                    QualityStatus.VALID
                                    if value.value is not None
                                    else QualityStatus.UNKNOWN
                                ),
                                quality_reason=(
                                    None
                                    if value.value is not None
                                    else "all_values_missing_or_unusable"
                                ),
                            )
                        )
        products = list(execution.ordered_products) or [
            product for item in family_results for product in item.products
        ]
        candidate_count = sum(item.candidate_count for item in family_results)
        use_family_bundle = len(family_results) > 1
        if use_family_bundle:
            bundle_warning = (
                "동일 canonical field와 단위가 검증된 상품군만 global TOP-K에 "
                "포함했습니다."
                if execution.plan.kind == "global_ranking"
                else "복수 하위 검색의 계산 결과를 각각 독립 검증했습니다."
            )
            exclusion_warnings = [
                (
                    f"{item.product_family.value}은 제공 데이터 schema에 "
                    f"{item.canonical_field} 필드가 없어 통합 순위에서 제외했습니다."
                )
                for item in execution.plan.excluded_families
            ]
            return RoutedAgentResult(
                request_id=decision.draft.request_id,
                status="executed",
                decision=decision,
                answer=execution.answer,
                query_plan=None,
                candidate_count=candidate_count,
                products=products,
                aggregates=compound_aggregates,
                comparisons=[],
                warnings=[bundle_warning, *exclusion_warnings],
                source_manifest=None,
                answer_composition=None,
                family_searches=family_results,
            )
        first = family_results[0]
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status="executed",
            decision=decision,
            answer=execution.answer,
            query_plan=first.query_plan,
            candidate_count=candidate_count,
            products=products,
            aggregates=compound_aggregates,
            comparisons=[],
            warnings=[
                "Typed Compound Plan으로 모든 수치 입력과 계산 순서를 결정론적으로 검증했습니다."
            ],
            source_manifest=first.source_manifest,
            answer_composition=None,
            family_searches=[],
        )

    def _bind_database_product_identities(self, decision: RouteDecision) -> RouteDecision:
        """Replace heuristic name spans with longest exact approved-DB identities.

        The lexical router intentionally has no access to product rows.  This
        server boundary enriches its draft only with exact DB aliases, before
        either the compound compiler or ordinary QueryPlan compiler can run.
        Duplicate names/share classes/venues never acquire execution authority.
        """

        if (
            decision.disposition is RouteDisposition.UNSUPPORTED
            and decision.reason_code != "safety_scope"
        ):
            return decision
        matches = self.database_identity_matcher.find(decision.draft.question)
        unresolved_identities = unresolved_identity_like_spans(
            decision.draft.question,
            matches,
        )
        if unresolved_identities and decision.disposition is RouteDisposition.EXECUTE:
            return self._compound_control_decision(
                decision,
                reason_code="unresolved_product_identity_span",
                reason=(
                    "상품명처럼 보이는 표현을 제공 데이터의 상품 식별자와 유일하게 "
                    "연결하지 못했습니다. 정확한 상품명이나 종목코드를 확인해 주세요."
                ),
            )
        if not matches:
            return decision
        if any(not match.is_unique for match in matches):
            return self._compound_control_decision(
                decision,
                reason_code="ambiguous_product_identity",
                reason=(
                    "같은 상품명 또는 식별자가 제공 데이터의 여러 상품·시장·클래스와 "
                    "일치합니다. 종목코드나 시장을 함께 지정해 주세요."
                ),
            )

        # ``safety_scope`` here is the lexical Router's lack-of-finance-signal
        # fallback, not a SafetyEnvelope policy violation (those use their
        # own ``safety_<gate>`` reason codes).  A unique exact identity from
        # the approved DB is stronger positive scope evidence, so let the
        # normal exact-family resolver re-authorize DETAIL/COMPARE below.
        if (
            decision.disposition is RouteDisposition.UNSUPPORTED
            and decision.reason_code == "safety_scope"
        ):
            decision = decision.model_copy(
                update={
                    "disposition": RouteDisposition.CLARIFY,
                    "reason_code": "ambiguous_product_family",
                    "reason": "정확한 제공 DB 상품 식별자의 상품군을 확인 중",
                    "query_plan_intent": None,
                }
            )

        exact_aliases = tuple(match.normalized_alias for match in matches)
        retained_mentions: list[str] = []
        for mention in decision.draft.product_mentions:
            normalized = normalize_database_identity(mention)
            # The old branded-name regex can greedily absorb request grammar
            # (``KODEX 200보다 ...``).  An exact DB span inside it is stronger
            # and replaces the whole heuristic surface.
            if any(alias in normalized or normalized in alias for alias in exact_aliases):
                continue
            retained_mentions.append(mention)
        # A question commonly supplies both the official name and its ticker
        # in parentheses.  Those are two aliases for one DB row, not two
        # products.  Collapse by family+product_id and pass the canonical
        # executable literal to downstream DETAIL/COMPARE compilers.
        resolved_mentions: list[str] = []
        resolved_products: set[tuple[ProductFamily, str]] = set()
        for match in matches:
            candidate = match.candidates[0]
            key = (candidate.product_family, candidate.canonical_literal)
            if key in resolved_products:
                continue
            resolved_products.add(key)
            resolved_mentions.append(candidate.canonical_literal)
        product_mentions = list(
            dict.fromkeys([*resolved_mentions, *retained_mentions])
        )
        matched_families = list(
            dict.fromkeys(match.product_families[0] for match in matches)
        )
        exact_identity_detail = False
        if (
            len(resolved_products) == 1
            and len(product_mentions) == 1
            and len(matched_families) == 1
        ):
            identity_end = max(match.end for match in matches)
            tail = decision.draft.question[identity_end:]
            explicit_detail_marker = re.match(
                r"^\s*(?:의|에\s*대한)?\s*.{0,50}?"
                r"(?:상세|정보|원천\s*(?:값|데이터)|제공\s*필드|수치|값)",
                tail,
                re.IGNORECASE,
            ) is not None
            explicit_field = any(
                mention.start >= identity_end
                for capability in ("queryable", "comparable", "sortable")
                for mention in registry_field_mentions(
                    decision.draft.question,
                    matched_families[0].value,
                    capability,
                )
            )
            exact_identity_detail = (
                (
                    explicit_detail_marker
                    or explicit_field
                    or _EXACT_IDENTITY_READ_CUE.search(tail) is not None
                )
                # Ranking words can legitimately be part of the official
                # product name (for example ``...TOP2+...``).  Only the
                # request tail outside every exact DB identity may decide
                # whether this is a screen rather than a detail lookup.
                and ranking_intent_present(tail) is False
                and re.search(r"(?:보다|대비)", tail) is None
                and re.search(
                    r"(?:편입|보유|구성\s*종목|자회사|계열사|테마|"
                    r"연결\s*이력|동향|위험\s*요인)",
                    tail,
                )
                is None
            )
        if (
            exact_identity_detail
            and decision.disposition is RouteDisposition.CLARIFY
            and decision.reason_code
            in {
                "missing_product_identity",
                "multiple_product_identities_unbound",
                "unresolved_product_identity_span",
                "semantic_coverage_incomplete",
            }
        ):
            decision = decision.model_copy(
                update={
                    "draft": decision.draft.model_copy(
                        update={"intent": InteractionIntent.DETAIL}
                    ),
                    "reason_code": "ambiguous_product_family",
                    "reason": "정확한 제공 DB 상품 식별자의 상품군을 확인 중",
                }
            )
        updates: dict[str, object] = {"product_mentions": product_mentions}
        if exact_identity_detail:
            # A unique, approved DB identity followed by requested fields is a
            # DETAIL even when the lexical Router originally saw only the
            # dataset noun inside the long official name and chose SEARCH.
            updates["intent"] = InteractionIntent.DETAIL
        exact_closed_comparison = (
            2 <= len(resolved_products) <= 3
            and len(matched_families) == 1
            and len(resolved_products) == len(product_mentions)
            and _CLOSED_IDENTITY_COMPARISON_CUE.search(decision.draft.question)
            is not None
        )
        if exact_closed_comparison:
            # The DB matcher has now supplied the evidence that the lexical
            # Router lacked.  Re-enter the normal family capability check; do
            # not preserve heuristic missing/counterpart errors after every
            # target is uniquely bound to one approved dataset.
            updates["intent"] = InteractionIntent.COMPARE
            if decision.disposition is RouteDisposition.CLARIFY:
                decision = decision.model_copy(
                    update={
                        "reason_code": "ambiguous_product_family",
                        "reason": "정확한 제공 DB 비교 대상을 한 상품군으로 확인 중",
                        "query_plan_intent": None,
                    }
                )
        if any(
            re.match(r"\s*(?:보다|대비)", decision.draft.question[match.end :])
            is not None
            for match in matches
        ):
            # ``기준상품보다 ... 상품 N개`` is a universe SEARCH.  The
            # heuristic Router sees one exact name first and may label it
            # DETAIL; exact DB span ownership makes the relative grammar
            # deterministic and prevents that downgrade.
            updates["intent"] = InteractionIntent.SEARCH
        explicit_families = decision.draft.product_families
        name_corroborated = all(
            match.candidates[0].alias_kind
            in {"product_name", "short_name", "derived_product_name"}
            for match in matches
        )
        if len(matched_families) == 1 and (
            not explicit_families
            or explicit_families == matched_families
            or decision.reason_code == "ambiguous_product_family"
            or name_corroborated
        ):
            updates["product_families"] = matched_families
        return decision.model_copy(
            update={"draft": decision.draft.model_copy(update=updates)}
        )

    def _resolve_exact_identity_family(self, decision: RouteDecision) -> RouteDecision:
        if not decision.draft.product_mentions:
            return decision
        if decision.disposition is RouteDisposition.UNSUPPORTED:
            return decision
        if (
            decision.disposition is RouteDisposition.CLARIFY
            and decision.reason_code != "ambiguous_product_family"
        ):
            return decision
        # The lexical router cannot know every official product name.  Once a
        # single identity has been proven uniquely against the approved DB,
        # bind the common possessive detail form (``<name>(<ticker>)의
        # <field>``) as DETAIL.  Relative/ranking screens deliberately stay
        # SEARCH and relationship/document questions are handled elsewhere.
        if (
            decision.draft.intent is InteractionIntent.SEARCH
            and len(decision.draft.product_mentions) == 1
            and not ranking_intent_present(decision.draft.question)
            and re.search(r"(?:보다|대비)", decision.draft.question) is None
            and re.search(
                r"(?:\([^)]{1,40}\))?\s*(?:의|에\s*대한)\s*"
                r"(?:상세|정보|수치|값|[가-힣A-Za-z0-9_%·/ -]{1,40})",
                decision.draft.question,
            )
            is not None
            and re.search(
                r"(?:편입|보유|구성\s*종목|자회사|계열사|테마|연결\s*이력|동향|위험\s*요인)",
                decision.draft.question,
            )
            is None
        ):
            decision = decision.model_copy(
                update={
                    "draft": decision.draft.model_copy(
                        update={"intent": InteractionIntent.DETAIL}
                    )
                }
            )
        if decision.draft.intent not in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
            InteractionIntent.COMPARE,
        } and not (
            decision.draft.intent is InteractionIntent.SEARCH
            and (
                len(decision.draft.product_mentions) >= 2
                or re.search(r"(?:보다|대비)", decision.draft.question) is not None
            )
        ):
            return decision
        resolved_families: set[ProductFamily] = set()
        corroborated_identity_name = False
        normalized_question = normalize_product_mention(decision.draft.question)
        identity_records_by_family = {
            family: self.identity_cache.get(path).records
            for family, path in self.database_paths.items()
        }
        for mention in decision.draft.product_mentions:
            matches: set[ProductFamily] = set()
            normalized_mention = normalize_product_mention(mention)
            for family, records in identity_records_by_family.items():
                for record in records:
                    identities = (
                        record.product_id,
                        record.product_name,
                        record.short_name,
                        record.ticker,
                        record.isin,
                    )
                    if any(
                        value is not None and normalize_product_mention(value) == normalized_mention
                        for value in identities
                    ):
                        matches.add(family)
                        corroborated_identity_name = corroborated_identity_name or any(
                            value is not None
                            and normalize_product_mention(value) in normalized_question
                            for value in (record.product_name, record.short_name)
                        )
            if len(matches) != 1:
                return decision
            resolved_families.update(matches)
        if len(resolved_families) != 1:
            return decision
        family = next(iter(resolved_families))
        if (
            decision.disposition is RouteDisposition.EXECUTE
            and decision.draft.product_families != [family]
            and not corroborated_identity_name
        ):
            # An explicit family and an unrelated code conflict must remain
            # fail-closed.  Family correction is allowed only when the same
            # approved DB record also corroborates the product name printed in
            # the question (words inside that name caused the lexical error).
            return decision
        capability = self.router.matrix.require(family, decision.draft.intent)
        if capability.status != "executable" or capability.query_plan_intent is None:
            return decision
        if (
            decision.disposition is RouteDisposition.EXECUTE
            and decision.draft.product_families == [family]
            and decision.query_plan_intent is capability.query_plan_intent
        ):
            return decision
        draft = decision.draft.model_copy(update={"product_families": [family]})
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="exact_identity_family_resolved",
            reason="정확한 상품 식별자를 제공 데이터에서 유일한 상품군으로 확인",
            query_plan_intent=capability.query_plan_intent,
            capability_matrix_version=decision.capability_matrix_version,
        )

    def _compile_server_plan(
        self,
        decision: RouteDecision,
    ) -> tuple[RouteDecision, QueryPlan] | None:
        """Compile the only executable QueryPlan: the server-owned deterministic plan."""

        if decision.disposition is not RouteDisposition.EXECUTE:
            return None
        return decision, self.compiler.compile(decision)

    def _answer_cross_family_search(
        self,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
    ) -> RoutedAgentResult:
        audit = current_request_audit()
        compiler_started = perf_counter()
        try:
            searches = self.compiler.compile_family_searches(decision)
        except PlanCompilationBlockedError as error:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.COMPILER,
                    outcome=AuditOutcome.BLOCKED,
                    reason_code="plan_blocked",
                    duration_ms=(perf_counter() - compiler_started) * 1000,
                    **self._decision_audit_fields(decision),
                )
            return self._control_result(
                decision,
                disposition=RouteDisposition.CLARIFY,
                reason=str(error),
            )
        plans = [search.plan for search in searches]
        global_ranking = resolve_cross_family_global_ranking(
            decision.draft.question,
            searches,
        )
        if audit is not None:
            elapsed_ms = (perf_counter() - compiler_started) * 1000
            for plan in plans:
                audit.emit(
                    stage=AuditStage.COMPILER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="family_plan_compiled",
                    duration_ms=elapsed_ms,
                    route_disposition=RouteDisposition.EXECUTE,
                    interaction_intent=InteractionIntent.SEARCH,
                    product_families=tuple(plan.product_families),
                    plan_sha256=query_plan_authority_sha256(plan),
                )
        for plan in plans:
            family = plan.product_families[0]
            if family not in self.database_paths:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.UNSUPPORTED,
                    reason=f"{family.value} database path is not configured",
                )
        validated_searches: list[tuple[CompiledFamilySearch, ValidatedPlan]] = []
        try:
            # Authorize the complete ordered batch before starting any worker.
            # A failure in child N therefore cannot leave children 0..N-1
            # partially executed.
            for index, search in enumerate(searches):
                authority_started = perf_counter()
                try:
                    validated = self.plan_authority_gate.validate_routed(
                        search.plan,
                        decision,
                        planning_decision=planning_decision,
                        cross_family_index=index,
                        cross_family_total=len(searches),
                    )
                except Exception:
                    if audit is not None:
                        audit.emit(
                            stage=AuditStage.AUTHORITY,
                            outcome=AuditOutcome.BLOCKED,
                            reason_code="authority_denied",
                            duration_ms=(perf_counter() - authority_started) * 1000,
                            route_disposition=RouteDisposition.EXECUTE,
                            interaction_intent=InteractionIntent.SEARCH,
                            product_families=tuple(search.plan.product_families),
                            plan_sha256=query_plan_authority_sha256(search.plan),
                        )
                    raise
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.AUTHORITY,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="authority_granted",
                        duration_ms=(perf_counter() - authority_started) * 1000,
                        **self._validated_audit_fields(validated),
                    )
                validated_searches.append((search, validated))
        except PlanAuthorityError as error:
            if error.code is not PlanAuthorityCode.EXECUTION_POLICY_BLOCKED:
                raise
            disposition = (
                RouteDisposition.UNSUPPORTED
                if any(plan.unsupported_conditions for plan in plans)
                else RouteDisposition.CLARIFY
            )
            return self._control_result(
                decision,
                disposition=disposition,
                reason=str(error),
            )
        except PlanExecutionBlockedError as error:
            disposition = (
                RouteDisposition.UNSUPPORTED
                if any(plan.unsupported_conditions for plan in plans)
                else RouteDisposition.CLARIFY
            )
            return self._control_result(
                decision,
                disposition=disposition,
                reason=str(error),
            )

        with ThreadPoolExecutor(max_workers=len(searches)) as executor:
            futures = [
                executor.submit(
                    contextvars.copy_context().run,
                    self._execute_family_search,
                    search,
                    validated,
                )
                for search, validated in validated_searches
            ]
            executions = [future.result() for future in futures]

        deterministic_searches = [execution.result for execution in executions]
        if _requires_complete_exact_cross_family_match(decision.draft.question, searches) and any(
            item.candidate_count == 0 for item in deterministic_searches
        ):
            atomic_not_found = [
                item.model_copy(
                    update={
                        "status": "not_found",
                        "answer": (
                            "교차 상품군에 명시한 정확 조건 중 하나 이상을 제공 데이터에서 "
                            "찾지 못해 부분 결과를 반환하지 않았습니다."
                        ),
                        "candidate_count": 0,
                        "products": [],
                    }
                )
                for item in deterministic_searches
            ]
            return RoutedAgentResult(
                request_id=decision.draft.request_id,
                status="executed",
                decision=decision,
                answer=(
                    "각 상품군에 명시한 정확 조건을 모두 만족하는 조합을 제공 데이터에서 "
                    "찾지 못했습니다. 충족되지 않은 조건을 자동으로 완화하거나 다른 상품군의 "
                    "부분 결과만 반환하지 않았습니다."
                ),
                query_plan=None,
                candidate_count=0,
                products=[],
                aggregates=[],
                comparisons=[],
                warnings=["교차 상품군의 정확 조건을 하나의 원자적 요청으로 검증했습니다."],
                source_manifest=None,
                answer_composition=None,
                family_searches=atomic_not_found,
            )
        family_searches = deterministic_searches
        global_ranked: list[_GlobalRankedProduct] = []
        if global_ranking is not None:
            family_searches, global_ranked = _rank_global_products(
                deterministic_searches,
                global_ranking,
            )

        products = (
            [item.product for item in global_ranked]
            if global_ranking is not None
            else [
                product for family_search in family_searches for product in family_search.products
            ]
        )
        candidate_count = sum(family_search.candidate_count for family_search in family_searches)
        cross_family_notice = (
            "동일 canonical field와 단위가 검증된 상품군만 global TOP-K에 포함했습니다."
            if global_ranking is not None
            else _CROSS_FAMILY_SAFETY_NOTICE
        )
        warnings = [
            cross_family_notice,
            *(
                f"{item.product_family.value}: {warning}"
                for item in family_searches
                for warning in item.warnings
            ),
        ]
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status="executed",
            decision=decision,
            answer=(
                _compile_global_cross_family_answer(
                    global_ranked,
                    global_ranking,
                    candidate_count=candidate_count,
                )
                if global_ranking is not None
                else _compile_cross_family_answer(family_searches)
            ),
            query_plan=None,
            candidate_count=candidate_count,
            products=products,
            aggregates=[],
            comparisons=[],
            warnings=warnings,
            source_manifest=None,
            answer_composition=None,
            family_searches=family_searches,
        )

    def _execute_family_search(
        self,
        search: CompiledFamilySearch,
        validated_plan: ValidatedPlan,
    ) -> _ExecutedFamilySearch:
        plan = validated_plan.canonical_plan
        family = plan.product_families[0]
        database_path = self.database_paths[family]
        audit = current_request_audit()
        oracle_started = perf_counter()
        self._increment_audit_metric(MetricCounter.ORACLE_CALLS)
        self._increment_audit_metric(MetricCounter.SQL_EXECUTIONS)
        try:
            executed = SQLiteOracle(database_path).execute(validated_plan)
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.ORACLE,
                    outcome=AuditOutcome.FAILED,
                    reason_code="oracle_failed",
                    duration_ms=(perf_counter() - oracle_started) * 1000,
                    **self._validated_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.ORACLE,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="oracle_completed",
                duration_ms=(perf_counter() - oracle_started) * 1000,
                candidate_count=executed.candidate_count,
                result_count=len(executed.records),
                **self._validated_audit_fields(validated_plan),
            )
        verifier_started = perf_counter()
        try:
            universe_started = perf_counter()
            try:
                universe = self._record_universe(database_path, validated_plan)
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.FAILED,
                        reason_code="verifier_universe_failed",
                        duration_ms=(perf_counter() - universe_started) * 1000,
                        candidate_count=executed.candidate_count,
                        **self._validated_audit_fields(validated_plan),
                    )
                raise
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="verifier_universe_loaded",
                    duration_ms=(perf_counter() - universe_started) * 1000,
                    candidate_count=len(universe),
                    **self._validated_audit_fields(validated_plan),
                )
            pure_verifier_started = perf_counter()
            try:
                verified = ResultVerifier().verify(plan, executed, universe)
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.VERIFIER,
                        outcome=AuditOutcome.FAILED,
                        reason_code="pure_verification_failed",
                        duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                        candidate_count=executed.candidate_count,
                        **self._validated_audit_fields(validated_plan),
                    )
                raise
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.SUCCEEDED,
                    reason_code="pure_verification_passed",
                    duration_ms=(perf_counter() - pure_verifier_started) * 1000,
                    candidate_count=verified.candidate_count,
                    result_count=len(verified.records),
                    **self._validated_audit_fields(validated_plan),
                )
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.VERIFIER,
                    outcome=AuditOutcome.FAILED,
                    reason_code="verification_failed",
                    duration_ms=(perf_counter() - verifier_started) * 1000,
                    candidate_count=executed.candidate_count,
                    **self._validated_audit_fields(validated_plan),
                )
                self._increment_audit_metric(MetricCounter.VERIFIER_FAILURES)
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.VERIFIER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="verification_passed",
                duration_ms=(perf_counter() - verifier_started) * 1000,
                candidate_count=verified.candidate_count,
                result_count=len(verified.records),
                **self._validated_audit_fields(validated_plan),
            )
        renderer_started = perf_counter()
        try:
            products = build_product_evidence(plan, verified)
            answer, warnings = render_verified_search(plan, verified, products)
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.RENDERER,
                    outcome=AuditOutcome.FAILED,
                    reason_code="rendering_failed",
                    duration_ms=(perf_counter() - renderer_started) * 1000,
                    **self._validated_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            evidence_ids = tuple(
                f"{family.value}:{product.product_id}:{field.canonical_field}"
                for product in products
                for field in product.fields
            )
            linkage = _bounded_audit_linkage(
                product_ids=tuple(f"{family.value}:{item.product_id}" for item in products),
                evidence_ids=evidence_ids,
            )
            audit.emit(
                stage=AuditStage.RENDERER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="rendering_completed",
                duration_ms=(perf_counter() - renderer_started) * 1000,
                candidate_count=verified.candidate_count,
                result_count=linkage.result_count,
                evidence_count=linkage.evidence_count,
                product_ids=linkage.product_ids,
                evidence_ids=linkage.evidence_ids,
                **self._validated_audit_fields(validated_plan),
            )
        return _ExecutedFamilySearch(
            grounded_question=search.grounded_question,
            result=FamilySearchResult(
                product_family=family,
                status="not_found" if verified.candidate_count == 0 else "success",
                answer=answer,
                query_plan=plan,
                candidate_count=verified.candidate_count,
                products=products,
                warnings=warnings,
                source_manifest=verified.manifest,
            ),
            verified=verified,
        )

    def _require_execution(self, plan: QueryPlan) -> None:
        uses_approved_override = bool(plan.product_families) and set(
            plan.product_families
        ).issubset(
            self.capability_execution_overrides,
        )
        use_internal_contract = self.allow_internal_disabled_dataset or uses_approved_override
        if plan.intent is Intent.AGGREGATE:
            if use_internal_contract:
                require_internal_evaluation_aggregation(plan)
            else:
                require_executable_aggregation(plan)
        elif plan.intent is Intent.COMPARE:
            if use_internal_contract:
                require_internal_evaluation_comparison(plan)
            else:
                require_executable_comparison(plan)
        elif use_internal_contract:
            require_internal_evaluation_search(plan)
        else:
            require_executable_search(plan)

    def _fund_execution_enabled(self) -> bool:
        return self.allow_internal_disabled_dataset or (
            ProductFamily.FUND in self.capability_execution_overrides
        )

    @staticmethod
    def _require_execution_authority(decision: RouteDecision, plan: QueryPlan) -> None:
        """Recheck server-owned authority immediately before an Oracle boundary."""

        if decision.disposition is not RouteDisposition.EXECUTE:
            raise PlanExecutionBlockedError("control disposition cannot reach an Oracle")
        if decision.query_plan_intent is not plan.intent:
            raise PlanExecutionBlockedError("route and QueryPlan intents do not match")
        if plan.ambiguities or plan.unsupported_conditions:
            raise PlanExecutionBlockedError(
                "ambiguous or unsupported QueryPlan cannot reach an Oracle"
            )

    def _control_result(
        self,
        decision: RouteDecision,
        *,
        disposition: RouteDisposition | None = None,
        reason: str | None = None,
        plan: QueryPlan | None = None,
    ) -> RoutedAgentResult:
        actual_disposition = disposition or decision.disposition
        status: Literal["clarify", "unsupported"] = (
            "clarify" if actual_disposition is RouteDisposition.CLARIFY else "unsupported"
        )
        control_intent = (
            InteractionIntent.CLARIFY
            if actual_disposition is RouteDisposition.CLARIFY
            else InteractionIntent.UNSUPPORTED
        )
        control_reason = reason or decision.reason
        preserve_original_clarification = (
            actual_disposition is RouteDisposition.CLARIFY
            and decision.disposition is RouteDisposition.CLARIFY
            and decision.query_plan_intent is None
            and decision.reason_code
            in {"missing_product_identity", "ambiguous_product_family"}
        )
        if not preserve_original_clarification and (
            decision.disposition is not actual_disposition
            or decision.query_plan_intent is not None
            or decision.draft.intent is not control_intent
        ):
            decision = RouteDecision(
                draft=decision.draft.model_copy(update={"intent": control_intent}),
                disposition=actual_disposition,
                reason_code=f"{control_intent.value}_execution_blocked",
                reason=control_reason,
                query_plan_intent=None,
                capability_matrix_version=decision.capability_matrix_version,
            )
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status=status,
            decision=decision,
            answer=_control_answer(actual_disposition, control_reason),
            # A control response never exports a partially compiled plan.  This
            # keeps execution authority and evidence unambiguously absent at
            # every public boundary, even when compilation found a later issue.
            query_plan=None,
            candidate_count=None,
            products=[],
            aggregates=[],
            comparisons=[],
            warnings=[],
            source_manifest=None,
            answer_composition=None,
        )
