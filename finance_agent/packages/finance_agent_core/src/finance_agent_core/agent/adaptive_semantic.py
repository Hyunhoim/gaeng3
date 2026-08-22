from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.planning_policy import (
    PlanningPath,
    PlanningTrace,
)
from finance_agent_core.agent.semantic_resolution import (
    AdaptivePlanningDecisionV2,
    AdaptivePlanningPolicyV2,
    HardFilterLock,
    ResolutionDecision,
    ResolutionOperation,
    SchemaFieldCandidate,
    SemanticResolutionDraft,
    SemanticResolutionError,
    SemanticResolutionGate,
    SemanticResolutionReceipt,
    SemanticResolutionRequest,
    SemanticResolverProvider,
    SpanSource,
    canonical_sha256,
)
from finance_agent_core.contracts.queryplan import QueryPlan, SortDirection
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.deadline import RequestDeadlineExceeded
from finance_agent_core.retrieval.schema_adaptive import (
    AdaptiveSchemaLinkStatus,
    AdaptiveSchemaLinkUnavailable,
    ProductionHybridSchemaLinker,
)

_ASCENDING = re.compile(r"낮|작|적|하위|오름차순|ascending", re.IGNORECASE)
_DESCENDING = re.compile(r"높|큰|크|많|상위|내림차순|descending", re.IGNORECASE)


class AdaptiveSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveSemanticOutcome(AdaptiveSemanticModel):
    status: Literal["resolved", "clarify", "unsupported"]
    reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    hard_filter_lock: HardFilterLock | None = None
    receipt: SemanticResolutionReceipt | None = None
    planning_decision: AdaptivePlanningDecisionV2
    dense_attempted: bool = False
    hclx_attempted: bool = False
    candidate_count: int = Field(default=0, ge=0, le=10)
    index_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    model_revision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    model_snapshot_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_outcome(self) -> AdaptiveSemanticOutcome:
        resolved = self.status == "resolved"
        if resolved != (self.hard_filter_lock is not None and self.receipt is not None):
            raise ValueError("resolved semantic outcome requires lock and receipt")
        if resolved != self.planning_decision.compiler_allowed:
            raise ValueError("semantic outcome and planning authority disagree")
        if self.hclx_attempted and not self.dense_attempted:
            raise ValueError("HCLX Semantic Resolver requires one preceding Dense attempt")
        audit_values = (
            self.index_manifest_sha256,
            self.model_revision_sha256,
            self.model_snapshot_manifest_sha256,
        )
        if self.dense_attempted != all(value is not None for value in audit_values):
            raise ValueError("Dense attempt requires complete redacted artifact linkage")
        return self


def _control(
    status: Literal["clarify", "unsupported"],
    reason_code: str,
    **observation: object,
) -> AdaptiveSemanticOutcome:
    return AdaptiveSemanticOutcome(
        status=status,
        reason_code=reason_code,
        planning_decision=AdaptivePlanningPolicyV2.control(),
        **observation,
    )


def _expected_direction(question: str, start: int, end: int) -> SortDirection | None:
    surface = question[max(0, start - 8) : min(len(question), end + 24)]
    ascending = _ASCENDING.search(surface) is not None
    descending = _DESCENDING.search(surface) is not None
    if ascending == descending:
        return None
    return SortDirection.ASC if ascending else SortDirection.DESC


class AdaptiveSemanticResolver:
    """Resolve schema gaps without granting Dense or HCLX direct plan authority."""

    def __init__(
        self,
        schema_linker: ProductionHybridSchemaLinker,
        *,
        hclx_provider: SemanticResolverProvider | None = None,
        gate: SemanticResolutionGate | None = None,
        planning_policy: AdaptivePlanningPolicyV2 | None = None,
    ) -> None:
        if type(schema_linker) is not ProductionHybridSchemaLinker:
            raise TypeError("adaptive semantic resolver requires the production schema linker")
        self.schema_linker = schema_linker
        self.hclx_provider = hclx_provider
        self.gate = gate or SemanticResolutionGate()
        if planning_policy is not None and type(planning_policy) is not AdaptivePlanningPolicyV2:
            raise TypeError("adaptive semantic resolver requires the server planning policy v2")
        self.planning_policy = planning_policy or AdaptivePlanningPolicyV2()

    def resolve(self, trace: PlanningTrace) -> AdaptiveSemanticOutcome:
        route = trace.route_decision
        planning = trace.planning_decision
        ledger = trace.semantic_ledger
        if (
            route.disposition is not RouteDisposition.EXECUTE
            or planning.path is not PlanningPath.SCHEMA_LINK_SHADOW
            or ledger is None
            or len(route.draft.product_families) != 1
            or not ledger.residual_spans
        ):
            return _control("clarify", "semantic_path_not_eligible")
        if len(ledger.residual_spans) > self.schema_linker.policy.maximum_residual_spans:
            return _control("clarify", "too_many_residual_spans")
        # v1 execution admits exactly one resolved ranking. Multiple residuals
        # are represented by the contract but remain fail-closed until ordering
        # and composition receive an independent evaluation.
        if len(ledger.residual_spans) != 1:
            return _control("clarify", "multiple_residual_spans_not_activated")

        residual = ledger.residual_spans[0]
        direction = _expected_direction(
            route.draft.question,
            residual.start,
            residual.end,
        )
        if direction is None:
            return _control("clarify", "residual_direction_unresolved")
        family = route.draft.product_families[0]
        payload = canonicalize_query_plan_payload(
            route.draft.question,
            {
                "question_id": route.draft.request_id,
                "product_families": [family.value],
            },
            force_product_family_hint=True,
        )
        residual_hashes = {item.sha256 for item in ledger.residual_spans}
        payload["ambiguities"] = [
            item
            for item in payload["ambiguities"]
            if hashlib.sha256(item["span"].encode("utf-8")).hexdigest() not in residual_hashes
        ]
        try:
            base_plan = QueryPlan.model_validate(payload)
        except ValueError:
            return _control("clarify", "hard_filter_plan_invalid")
        if base_plan.ambiguities or base_plan.unsupported_conditions:
            return _control("clarify", "hard_filter_plan_unresolved")
        hard_filter_lock = HardFilterLock.from_plan(
            base_plan,
            requested_limit=route.draft.requested_limit,
            product_mentions=tuple(route.draft.product_mentions),
        )
        dense_observation = {
            "dense_attempted": True,
            "candidate_count": 0,
            "index_manifest_sha256": canonical_sha256(
                self.schema_linker.index.manifest.model_dump(mode="json")
            ),
            "model_revision_sha256": hashlib.sha256(
                self.schema_linker.index.manifest.provider.model_revision.encode("utf-8")
            ).hexdigest(),
            "model_snapshot_manifest_sha256": (
                self.schema_linker.policy.snapshot_file_manifest_sha256
            ),
        }
        try:
            linked = self.schema_linker.link(
                residual_span=residual.text,
                product_family=family,
                interaction_intent=route.draft.intent,
                operation=ResolutionOperation.RANK,
            )
        except AdaptiveSchemaLinkUnavailable:
            return _control("clarify", "schema_dense_capacity_unavailable")
        except RequestDeadlineExceeded:
            raise
        except Exception:  # noqa: BLE001 - Dense failure cannot acquire execution authority
            return _control("clarify", "schema_dense_failed", **dense_observation)
        observation = dense_observation | {"candidate_count": len(linked.candidates)}
        if not linked.candidates:
            return _control("clarify", linked.reason_code, **observation)
        request = SemanticResolutionRequest(
            request_id=route.draft.request_id,
            residual_span=residual.text,
            product_family=family,
            interaction_intent=route.draft.intent,
            allowed_operations=(ResolutionOperation.RANK,),
            candidates=tuple(
                SchemaFieldCandidate.model_validate(item.model_dump(mode="python"))
                for item in linked.candidates
            ),
            expected_direction=direction,
            hard_filter_lock_sha256=hard_filter_lock.payload_sha256,
        )
        if linked.status is AdaptiveSchemaLinkStatus.FOUND:
            draft = SemanticResolutionDraft(
                decision=ResolutionDecision.RESOLVE,
                selected_field_id=linked.candidates[0].field_id,
                operation=ResolutionOperation.RANK,
                direction=direction,
                reason_code="candidate_context_match",
            )
            source = SpanSource.SCHEMA_DENSE
        elif linked.hclx_eligible and self.hclx_provider is not None:
            observation["hclx_attempted"] = True
            try:
                draft = self.hclx_provider.resolve_semantics(request)
            except RequestDeadlineExceeded:
                raise
            except Exception:  # noqa: BLE001 - provider failure cannot acquire execution authority
                return _control(
                    "clarify",
                    "hclx_semantic_resolver_failed",
                    **observation,
                )
            if draft.decision is ResolutionDecision.CLARIFY:
                return _control(
                    "clarify",
                    "hclx_semantic_resolver_abstained",
                    **observation,
                )
            if draft.decision is ResolutionDecision.UNSUPPORTED:
                return _control(
                    "unsupported",
                    "hclx_semantic_resolver_unsupported",
                    **observation,
                )
            source = SpanSource.HCLX
        else:
            return _control("clarify", linked.reason_code, **observation)
        try:
            receipt = self.gate.admit(draft, request, source=source)
        except SemanticResolutionError:
            return _control("clarify", "semantic_resolution_rejected", **observation)
        return AdaptiveSemanticOutcome(
            status="resolved",
            reason_code=(
                "schema_dense_resolution_admitted"
                if source is SpanSource.SCHEMA_DENSE
                else "hclx_semantic_resolution_admitted"
            ),
            hard_filter_lock=hard_filter_lock,
            receipt=receipt,
            planning_decision=self.planning_policy.admit(receipt),
            **observation,
        )


__all__ = ["AdaptiveSemanticOutcome", "AdaptiveSemanticResolver"]
