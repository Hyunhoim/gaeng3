from __future__ import annotations

import contextvars
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.compiler import (
    CompiledFamilySearch,
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.grounded_planning import (
    GroundedPlanGate,
    GroundedPlanProvider,
    GroundedPlanRejectedError,
    grounded_plan_is_eligible,
)
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
    PlanningPath,
    PlanningTrace,
)
from finance_agent_core.agent.providers import HyperClovaXTimeoutError, QueryPlanProvider
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.answering import (
    AnswerComposition,
    CrossFamilyAnswerVerifier,
    GroundedAnswerProvider,
    compose_grounded_answer,
)
from finance_agent_core.contracts import QueryPlan, RouteDecision, RouteDisposition
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RoutedExecutionError
from finance_agent_core.deadline import RequestDeadlineExceeded, current_request_deadline
from finance_agent_core.domain import (
    AggregateEvidence,
    ComparisonEvidence,
    DatabaseManifest,
    ProductEvidence,
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
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
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
from finance_agent_core.release import ResolvedAgentRelease
from finance_agent_core.retrieval.schema_shadow import (
    AsyncSchemaLinkShadowObserver,
    SchemaLinkShadowObserver,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
    load_approved_dataset_manifest,
    require_approved_database_paths,
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
        if self.query_plan.intent is not Intent.SEARCH:
            raise ValueError("family search result requires a SEARCH QueryPlan")
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
    warnings: list[str]
    source_manifest: DatabaseManifest | None
    answer_composition: AnswerComposition | None
    family_searches: list[FamilySearchResult] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_result_state(self) -> RoutedAgentResult:
        if self.request_id != self.decision.draft.request_id:
            raise ValueError("result and route request IDs differ")
        if self.status == "executed":
            if self.family_searches:
                if len(self.family_searches) < 2:
                    raise ValueError("multi-family result requires at least two family searches")
                if (
                    self.query_plan is not None
                    or self.source_manifest is not None
                    or self.aggregates
                    or self.comparisons
                ):
                    raise ValueError(
                        "multi-family SEARCH result must keep plans and manifests per family"
                    )
                if self.candidate_count != sum(
                    item.candidate_count for item in self.family_searches
                ):
                    raise ValueError("multi-family candidate count must equal the family sum")
                if self.products != [
                    product for item in self.family_searches for product in item.products
                ]:
                    raise ValueError("multi-family products must preserve the family result order")
                if [item.product_family for item in self.family_searches] != (
                    self.decision.draft.product_families
                ):
                    raise ValueError("multi-family result order must match the routed family order")
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
            or self.warnings
            or self.source_manifest is not None
            or self.answer_composition is not None
            or self.family_searches
        ):
            raise ValueError("control result must not contain executed evidence")
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
        query_plan_provider: QueryPlanProvider | None = None,
        grounded_plan_provider: GroundedPlanProvider | None = None,
        answer_provider: GroundedAnswerProvider | None = None,
        hclx_planning_enabled: bool = False,
        allow_internal_disabled_dataset: bool = False,
        capability_execution_overrides: set[ProductFamily | str] | None = None,
        require_approved_databases: bool = False,
        release_guard: ResolvedAgentRelease | None = None,
        require_agent_release: bool = False,
        record_cache: RecordSnapshotCache | None = None,
        identity_cache: ProductIdentitySnapshotCache | None = None,
        schema_link_shadow_observer: SchemaLinkShadowObserver | None = None,
        audit_sink: BoundedAsyncAuditSink | None = None,
    ) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
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
        self.hclx_planning_enabled = hclx_planning_enabled
        self.router = router or IntentRouter(hclx_planning_enabled=hclx_planning_enabled)
        self.query_plan_provider = query_plan_provider
        self.grounded_plan_provider = grounded_plan_provider
        self.compiler = ServerQueryPlanCompiler(
            self.database_paths,
            record_cache=self.record_cache,
            identity_cache=self.identity_cache,
        )
        self.grounded_plan_gate = GroundedPlanGate(
            self.database_paths,
            identity_cache=self.identity_cache,
        )
        self.answer_provider = answer_provider
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset
        self.capability_execution_overrides = frozenset(
            ProductFamily(family) for family in capability_execution_overrides or set()
        )
        self.require_approved_databases = require_approved_databases
        if release_guard is not None and type(release_guard) is not ResolvedAgentRelease:
            raise TypeError("release_guard must be a ResolvedAgentRelease")
        if require_agent_release and release_guard is None:
            raise ValueError("public RoutedFinanceAgent requires a resolved Agent release")
        if require_agent_release and schema_link_shadow_observer is not None:
            raise ValueError(
                "the current public Agent release profile keeps Schema Dense shadow disabled"
            )
        self.release_guard = release_guard
        self.require_agent_release = require_agent_release
        if audit_sink is not None and type(audit_sink) is not BoundedAsyncAuditSink:
            raise TypeError("audit_sink must be the bounded async audit sink")
        self.audit_sink = audit_sink
        if (
            schema_link_shadow_observer is not None
            and type(schema_link_shadow_observer) is not AsyncSchemaLinkShadowObserver
        ):
            raise TypeError("schema_link_shadow_observer must be the bounded async observer")
        self.schema_link_shadow_observer = schema_link_shadow_observer
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
        if self._record_cache_enabled:
            snapshot = self.record_cache.get(database_path)
            require_manifest_binding(validated_plan, snapshot.manifest)
            require_verifier_budget(validated_plan, len(snapshot.records))
            require_validated_plan(validated_plan, database_path)
            return snapshot.records
        return load_projected_verifier_records(database_path, validated_plan)

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
                (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError),
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

    def _emit_composition_audit(
        self,
        *,
        composition: AnswerComposition,
        elapsed_ms: float,
        candidate_count: int,
        result_count: int,
        audit_fields: dict[str, object],
    ) -> None:
        audit = current_request_audit()
        if audit is None or composition.mode == "deterministic":
            return
        provider_completed = composition.draft is not None
        audit.emit(
            stage=AuditStage.HCLX,
            outcome=(AuditOutcome.SUCCEEDED if provider_completed else AuditOutcome.FAILED),
            reason_code=("generation_completed" if provider_completed else "provider_failed"),
            duration_ms=composition.generation_latency_ms,
            candidate_count=candidate_count,
            result_count=result_count,
            **audit_fields,
        )
        self._increment_audit_metric(MetricCounter.HCLX_CALLS)
        verification_passed = composition.verification.passed
        audit.emit(
            stage=AuditStage.VERIFIER,
            outcome=(AuditOutcome.SUCCEEDED if verification_passed else AuditOutcome.FAILED),
            reason_code=("composition_verified" if verification_passed else "composition_rejected"),
            duration_ms=max(0.0, elapsed_ms - composition.generation_latency_ms),
            candidate_count=candidate_count,
            result_count=result_count,
            **audit_fields,
        )
        if not verification_passed:
            self._increment_audit_metric(MetricCounter.VERIFIER_FAILURES)

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
        if (
            self.schema_link_shadow_observer is not None
            and trace.planning_decision.path is PlanningPath.SCHEMA_LINK_SHADOW
        ):
            # The observer receives a detached snapshot. Even a faulty or
            # stateful implementation cannot mutate the RouteDecision or
            # PlanningDecision used by Compiler, SQL, or the served response.
            trusted_trace = PlanningTrace.model_validate_json(trace.model_dump_json())
            observer_trace = PlanningTrace.model_validate_json(trace.model_dump_json())
            try:
                self.schema_link_shadow_observer.submit(observer_trace)
            except Exception:
                # Shadow telemetry has no authority over the Agent result.
                pass
            trace = trusted_trace
        decision = trace.route_decision
        route_emitted = False
        try:
            decision = self._resolve_exact_identity_family(decision)
            planning_decision = trace.planning_decision
            if decision != trace.route_decision:
                # Exact product identity is a server-owned DB resolution, not
                # a caller-provided RouteDecision. Recompute the shadow record
                # from the final route so CONTROL metadata cannot silently
                # accompany an executable plan.
                planning_started = perf_counter()
                planning_decision = AdaptiveShadowPlanningPolicy(
                    hclx_planning_enabled=self.hclx_planning_enabled
                ).decide(
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
            return self._answer_from_decision(decision, planning_decision)
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
    ) -> RoutedAgentResult:
        question = decision.draft.question
        request_id = decision.draft.request_id
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
            compiled = self._compile_with_optional_grounded_plan(
                question,
                decision,
                planning_decision,
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
        decision, plan, used_grounded_plan = compiled
        if audit is not None:
            audit.emit(
                stage=AuditStage.COMPILER,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="plan_compiled",
                duration_ms=(perf_counter() - compiler_started) * 1000,
                plan_sha256=query_plan_authority_sha256(plan),
                **self._decision_audit_fields(decision),
            )
        if used_grounded_plan:
            # Grounded planning is admitted only from an already executable
            # deterministic route. Recompute server-owned metadata from the
            # fully validated final decision while preserving the explicit
            # deployment-owned HCLX permission.
            planning_decision = AdaptiveShadowPlanningPolicy(
                hclx_planning_enabled=planning_decision.hclx_allowed
            ).decide(
                decision,
                SemanticCoverageDecision(),
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

        if (
            self.query_plan_provider is not None
            and self.hclx_planning_enabled
            and planning_decision.hclx_allowed
            and not used_grounded_plan
            and plan.intent is Intent.SEARCH
        ):
            try:
                plan = self._provider_search_plan(decision, plan)
            except PlanCompilationBlockedError as error:
                return self._control_result(
                    decision,
                    disposition=RouteDisposition.CLARIFY,
                    reason=str(error),
                    plan=plan,
                )
            except (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError):
                raise
            except Exception:
                # QueryPlan generation is advisory. Transport, schema, or
                # adapter failures retain the independently compiled server
                # plan; a model can never turn a valid deterministic request
                # into an infrastructure failure.
                pass

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
                planning_decision=planning_decision,
                compiler_kind=(
                    PlanCompilerKind.GROUNDED_PLAN_GATE
                    if used_grounded_plan
                    else PlanCompilerKind.SERVER_QUERY_PLAN
                ),
                proposal_provider_name=(
                    self.grounded_plan_provider.provider_name
                    if used_grounded_plan and self.grounded_plan_provider is not None
                    else None
                ),
                proposal_model_name=(
                    self.grounded_plan_provider.model_name
                    if used_grounded_plan and self.grounded_plan_provider is not None
                    else None
                ),
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
                universe = self._record_universe(database_path, validated_plan)
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
                candidate_count=verified_aggregation.candidate_count,
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
            universe = (
                None
                if plan.intent is Intent.COMPARE
                else self._record_universe(database_path, validated_plan)
            )
            verified = ResultVerifier().verify(plan, executed, universe)
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
        composition: AnswerComposition | None = None
        if self.answer_provider is not None:
            composition_started = perf_counter()
            composition = compose_grounded_answer(
                question=question,
                plan=plan,
                verified=verified,
                products=products,
                provider=self.answer_provider,
            )
            answer = composition.answer
            self._emit_composition_audit(
                composition=composition,
                elapsed_ms=(perf_counter() - composition_started) * 1000,
                candidate_count=verified.candidate_count,
                result_count=len(products),
                audit_fields=self._validated_audit_fields(validated_plan),
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
            answer_composition=composition,
        )

    def _resolve_exact_identity_family(self, decision: RouteDecision) -> RouteDecision:
        if (
            decision.disposition is not RouteDisposition.CLARIFY
            or decision.reason_code != "ambiguous_product_family"
            or not decision.draft.product_mentions
        ):
            return decision
        resolved_families: set[ProductFamily] = set()
        for mention in decision.draft.product_mentions:
            matches: set[ProductFamily] = set()
            for family, path in self.database_paths.items():
                for record in self.identity_cache.get(path).records:
                    identities = (record.product_id, record.ticker, record.isin)
                    if any(
                        value is not None and value.casefold() == mention.casefold()
                        for value in identities
                    ):
                        matches.add(family)
            if len(matches) != 1:
                return decision
            resolved_families.update(matches)
        if len(resolved_families) != 1:
            return decision
        family = next(iter(resolved_families))
        capability = self.router.matrix.require(family, decision.draft.intent)
        if capability.status != "executable" or capability.query_plan_intent is None:
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

    def _compile_with_optional_grounded_plan(
        self,
        question: str,
        decision: RouteDecision,
        planning_decision: PlanningDecision,
    ) -> tuple[RouteDecision, QueryPlan, bool] | None:
        server_plan: QueryPlan | None = None
        server_error: PlanCompilationBlockedError | None = None
        if decision.disposition is RouteDisposition.EXECUTE:
            try:
                server_plan = self.compiler.compile(decision)
            except PlanCompilationBlockedError as error:
                server_error = error

        eligible = (
            self.grounded_plan_provider is not None
            and self.hclx_planning_enabled
            and planning_decision.hclx_allowed
            and grounded_plan_is_eligible(decision)
        )
        if eligible:
            assert self.grounded_plan_provider is not None
            family_hint = (
                decision.draft.product_families[0]
                if len(decision.draft.product_families) == 1
                else None
            )
            audit = current_request_audit()
            provider_started = perf_counter()
            provider_observed = False
            self._increment_audit_metric(MetricCounter.HCLX_CALLS)
            try:
                proposal = self.grounded_plan_provider.generate_grounded_plan(
                    question,
                    decision.draft.request_id,
                    family_hint,
                )
                provider_observed = True
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.HCLX,
                        outcome=AuditOutcome.SUCCEEDED,
                        reason_code="provider_completed",
                        duration_ms=(perf_counter() - provider_started) * 1000,
                        **self._decision_audit_fields(decision),
                    )
                grounded_plan = self.grounded_plan_gate.compile(
                    question,
                    decision,
                    proposal,
                    trusted_plan=server_plan,
                )
                grounded_decision = self._grounded_execution_decision(
                    decision,
                    grounded_plan,
                )
                return grounded_decision, grounded_plan, True
            except (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError):
                if audit is not None and not provider_observed:
                    audit.emit(
                        stage=AuditStage.HCLX,
                        outcome=AuditOutcome.TIMED_OUT,
                        reason_code="deadline_exceeded",
                        duration_ms=(perf_counter() - provider_started) * 1000,
                        **self._decision_audit_fields(decision),
                    )
                raise
            except GroundedPlanRejectedError:
                if audit is not None:
                    audit.emit(
                        stage=AuditStage.COMPILER,
                        outcome=AuditOutcome.BLOCKED,
                        reason_code="grounded_plan_rejected",
                        duration_ms=(perf_counter() - provider_started) * 1000,
                        **self._decision_audit_fields(decision),
                    )
            except Exception:
                if audit is not None:
                    audit.emit(
                        stage=(AuditStage.COMPILER if provider_observed else AuditStage.HCLX),
                        outcome=AuditOutcome.FAILED,
                        reason_code=(
                            "grounded_plan_gate_failed" if provider_observed else "provider_failed"
                        ),
                        duration_ms=(perf_counter() - provider_started) * 1000,
                        **self._decision_audit_fields(decision),
                    )
                # A planning model is advisory only. Malformed output, transport
                # failure, or an adapter bug must never become an HTTP 500 or
                # acquire execution authority. Reuse the independently compiled
                # server plan when one exists; otherwise answer with the normal
                # fail-closed clarification path.
                pass
        if server_plan is None:
            if server_error is not None:
                raise server_error
            return None
        return decision, server_plan, False

    @staticmethod
    def _grounded_execution_decision(
        decision: RouteDecision,
        plan: QueryPlan,
    ) -> RouteDecision:
        if plan.intent is Intent.COMPARE:
            interaction_intent = InteractionIntent.COMPARE
        elif plan.intent is Intent.AGGREGATE:
            interaction_intent = InteractionIntent.AGGREGATE
        elif decision.draft.intent in {
            InteractionIntent.DETAIL,
            InteractionIntent.EXPLAIN,
        }:
            interaction_intent = decision.draft.intent
        else:
            interaction_intent = InteractionIntent.SEARCH
        mentions: list[str] = []
        for constraint in plan.constraints:
            if constraint.field not in {"product_id", "ticker", "isin"}:
                continue
            values = constraint.value if isinstance(constraint.value, list) else [constraint.value]
            mentions.extend(str(value) for value in values)
        draft = decision.draft.model_copy(
            update={
                "intent": interaction_intent,
                "product_families": plan.product_families,
                "product_mentions": list(dict.fromkeys(mentions)),
            }
        )
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="grounded_model_plan_accepted",
            reason="모델 계획의 모든 실행 조건을 원문 근거와 서버 계약으로 검증",
            query_plan_intent=plan.intent,
            capability_matrix_version=decision.capability_matrix_version,
        )

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
        family_searches = deterministic_searches
        answer_composition: AnswerComposition | None = None
        if self.answer_provider is not None:
            family_searches, answer_composition = self._compose_cross_family_grounded_answer(
                executions
            )

        products = [
            product for family_search in family_searches for product in family_search.products
        ]
        candidate_count = sum(family_search.candidate_count for family_search in family_searches)
        warnings = [
            _CROSS_FAMILY_SAFETY_NOTICE,
            *(
                f"{item.product_family.value}: {warning}"
                for item in family_searches
                for warning in item.warnings
            ),
        ]
        if self.query_plan_provider is not None:
            warnings.append(
                "교차 상품군 SEARCH는 QueryPlan 모델을 호출하지 않고 "
                "서버가 상품군별 실행 계획을 확정했습니다."
            )
        if answer_composition is not None and answer_composition.mode == "deterministic_fallback":
            warnings.append(
                "상품군별 생성 또는 교차 답변 검증이 실패해 전체 답변을 "
                "결정론적 결과로 교체했습니다."
            )
        return RoutedAgentResult(
            request_id=decision.draft.request_id,
            status="executed",
            decision=decision,
            answer=_compile_cross_family_answer(family_searches),
            query_plan=None,
            candidate_count=candidate_count,
            products=products,
            aggregates=[],
            comparisons=[],
            warnings=warnings,
            source_manifest=None,
            answer_composition=answer_composition,
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
            universe = self._record_universe(database_path, validated_plan)
            verified = ResultVerifier().verify(plan, executed, universe)
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

    def _compose_cross_family_grounded_answer(
        self,
        executions: list[_ExecutedFamilySearch],
    ) -> tuple[list[FamilySearchResult], AnswerComposition]:
        if self.answer_provider is None:
            raise RuntimeError("cross-family grounded composition requires an answer provider")

        family_compositions: list[tuple[ProductFamily, AnswerComposition]] = []
        generated_searches: list[FamilySearchResult] = []
        audit = current_request_audit()
        for execution in executions:
            result = execution.result
            composition_started = perf_counter()
            composition = compose_grounded_answer(
                question=execution.grounded_question,
                plan=result.query_plan,
                verified=execution.verified,
                products=result.products,
                provider=self.answer_provider,
            )
            family_compositions.append((result.product_family, composition))
            generated_searches.append(result.model_copy(update={"answer": composition.answer}))
            self._emit_composition_audit(
                composition=composition,
                elapsed_ms=(perf_counter() - composition_started) * 1000,
                candidate_count=result.candidate_count,
                result_count=len(result.products),
                audit_fields={
                    "route_disposition": RouteDisposition.EXECUTE,
                    "interaction_intent": InteractionIntent.SEARCH,
                    "product_families": (result.product_family,),
                    "plan_sha256": query_plan_authority_sha256(result.query_plan),
                },
            )

        candidate_answer = _compile_cross_family_answer(generated_searches)
        verifier_started = perf_counter()
        verification = CrossFamilyAnswerVerifier().verify(
            family_compositions=family_compositions,
            family_answers=[(item.product_family, item.answer) for item in generated_searches],
            answer=candidate_answer,
            safety_notice=_CROSS_FAMILY_SAFETY_NOTICE,
        )
        if audit is not None:
            audit.emit(
                stage=AuditStage.VERIFIER,
                outcome=(AuditOutcome.SUCCEEDED if verification.passed else AuditOutcome.FAILED),
                reason_code=(
                    "cross_composition_verified"
                    if verification.passed
                    else "cross_composition_rejected"
                ),
                duration_ms=(perf_counter() - verifier_started) * 1000,
                route_disposition=RouteDisposition.EXECUTE,
                interaction_intent=InteractionIntent.SEARCH,
                product_families=tuple(execution.result.product_family for execution in executions),
                candidate_count=sum(execution.result.candidate_count for execution in executions),
                result_count=sum(len(execution.result.products) for execution in executions),
            )
            if not verification.passed:
                self._increment_audit_metric(MetricCounter.VERIFIER_FAILURES)
        latency_ms = round(
            sum(composition.generation_latency_ms for _, composition in family_compositions),
            3,
        )
        if verification.passed:
            mode: Literal["llm_grounded", "deterministic"] = (
                "llm_grounded"
                if any(composition.mode == "llm_grounded" for _, composition in family_compositions)
                else "deterministic"
            )
            return generated_searches, AnswerComposition(
                mode=mode,
                answer=candidate_answer,
                model=self.answer_provider.model_name,
                generation_latency_ms=latency_ms,
                draft=None,
                verification=verification,
            )

        deterministic_searches = [execution.result for execution in executions]
        deterministic_answer = _compile_cross_family_answer(deterministic_searches)
        return deterministic_searches, AnswerComposition(
            mode="deterministic_fallback",
            answer=deterministic_answer,
            model=self.answer_provider.model_name,
            generation_latency_ms=latency_ms,
            draft=None,
            verification=verification,
        )

    def _provider_search_plan(
        self,
        decision: RouteDecision,
        server_plan: QueryPlan,
    ) -> QueryPlan:
        if self.query_plan_provider is None:
            return server_plan
        audit = current_request_audit()
        started = perf_counter()
        self._increment_audit_metric(MetricCounter.HCLX_CALLS)
        try:
            provider_plan = self.query_plan_provider.generate_query_plan(
                decision.draft.question,
                decision.draft.request_id,
            )
        except (HyperClovaXTimeoutError, RequestDeadlineExceeded, TimeoutError):
            if audit is not None:
                audit.emit(
                    stage=AuditStage.HCLX,
                    outcome=AuditOutcome.TIMED_OUT,
                    reason_code="deadline_exceeded",
                    duration_ms=(perf_counter() - started) * 1000,
                    plan_sha256=query_plan_authority_sha256(server_plan),
                    **self._decision_audit_fields(decision),
                )
            raise
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.HCLX,
                    outcome=AuditOutcome.FAILED,
                    reason_code="provider_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    plan_sha256=query_plan_authority_sha256(server_plan),
                    **self._decision_audit_fields(decision),
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.HCLX,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="provider_completed",
                duration_ms=(perf_counter() - started) * 1000,
                plan_sha256=query_plan_authority_sha256(server_plan),
                **self._decision_audit_fields(decision),
            )
        if provider_plan != server_plan:
            raise PlanCompilationBlockedError(
                "model QueryPlan differs from the server-compiled execution contract"
            )
        # Equality is only an admission check. Keep the server-owned object as
        # the proposal that reaches PlanAuthorityGate so a provider instance is
        # never substituted after deterministic validation.
        return server_plan

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
        if decision.disposition is not actual_disposition or decision.query_plan_intent is not None:
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
