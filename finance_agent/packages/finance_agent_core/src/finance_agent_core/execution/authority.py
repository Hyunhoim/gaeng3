from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from finance_agent_core import __version__
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import Intent, ProductFamily, QueryPlan
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.deadline import (
    current_request_deadline,
    raise_if_request_stopped,
)
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.execution.policy import (
    PlanExecutionBlockedError,
    require_executable_aggregation,
    require_executable_comparison,
    require_executable_search,
    require_internal_evaluation_aggregation,
    require_internal_evaluation_comparison,
    require_internal_evaluation_search,
)
from finance_agent_core.ontology import (
    ONTOLOGY_RENDERER_VERSION,
    ontology_bundle_sha256,
)
from finance_agent_core.release import ResolvedAgentRelease
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database,
)
from finance_agent_core.storage.pinned_sqlite import (
    ConnectionAuditReason,
    PinnedSQLiteArtifact,
    PinnedSQLiteError,
)
from finance_agent_core.storage.sqlite import load_manifest

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_AUTHORITY_KEY = secrets.token_bytes(32)
_AUTHORITY_EPOCH_SHA256 = hashlib.sha256(_AUTHORITY_KEY).hexdigest()

PLAN_AUTHORITY_VERSION = "plan-authority-v1"
SERVER_COMPILER_VERSION = "server-queryplan-compiler-v1"
GROUNDED_PLAN_GATE_VERSION = "grounded-plan-gate-v1"
LEGACY_PROVIDER_COMPILER_VERSION = "legacy-provider-queryplan-v1"
INTERNAL_EVALUATION_COMPILER_VERSION = "internal-evaluation-queryplan-v1"
RESULT_VERIFIER_VERSION = "result-verifier-v1"
INTERNAL_EVALUATION_POLICY_VERSION = "internal-evaluation-v1"
LEGACY_PROVIDER_POLICY_VERSION = "legacy-provider-safety-v1"
ADAPTIVE_SHADOW_POLICY_VERSION = "adaptive-shadow-v1"
ADAPTIVE_SEMANTIC_POLICY_VERSION = "adaptive-semantic-v2"


class PlanAuthorityCode(StrEnum):
    INVALID_PROPOSAL = "invalid_proposal"
    ROUTE_MISMATCH = "route_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    EXECUTION_POLICY_BLOCKED = "execution_policy_blocked"
    DATASET_NOT_CONFIGURED = "dataset_not_configured"
    DATASET_MISMATCH = "dataset_mismatch"
    UNAUTHORIZED_PLAN_TYPE = "unauthorized_plan_type"
    INVALID_AUTHORITY_SEAL = "invalid_authority_seal"
    STALE_AUTHORITY_CONTEXT = "stale_authority_context"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    ORACLE_MODE_MISMATCH = "oracle_mode_mismatch"
    ROW_BUDGET_EXCEEDED = "row_budget_exceeded"
    RELEASE_MISMATCH = "release_mismatch"


class PlanAuthorityError(ValueError):
    """Stable fail-closed error raised at the plan-to-database boundary."""

    def __init__(self, code: PlanAuthorityCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class PlanAuthorityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoritySource(StrEnum):
    ROUTED_AGENT = "routed_agent"
    LEGACY_PROVIDER = "legacy_provider"
    INTERNAL_EVALUATION = "internal_evaluation"


class DatasetAuthorityStatus(StrEnum):
    OFFICIAL_APPROVED = "official_competition_data_approved"
    TEST_FIXTURE = "test_fixture_unapproved"


class ExecutionProfile(StrEnum):
    PUBLIC = "public"
    PUBLIC_CAPABILITY_OVERRIDE = "public_capability_override"
    INTERNAL_DISABLED_DATASET = "internal_disabled_dataset"
    INTERNAL_EVALUATION = "internal_evaluation"


class PlanCompilerKind(StrEnum):
    SERVER_QUERY_PLAN = "server_queryplan_compiler"
    GROUNDED_PLAN_GATE = "grounded_plan_gate"
    LEGACY_PROVIDER = "legacy_provider"
    INTERNAL_EVALUATION = "internal_evaluation"


def _compiler_version(kind: PlanCompilerKind) -> str:
    return {
        PlanCompilerKind.SERVER_QUERY_PLAN: SERVER_COMPILER_VERSION,
        PlanCompilerKind.GROUNDED_PLAN_GATE: GROUNDED_PLAN_GATE_VERSION,
        PlanCompilerKind.LEGACY_PROVIDER: LEGACY_PROVIDER_COMPILER_VERSION,
        PlanCompilerKind.INTERNAL_EVALUATION: INTERNAL_EVALUATION_COMPILER_VERSION,
    }[kind]


class ValidationReceipt(PlanAuthorityModel):
    """Serializable audit receipt; it is not execution authority by itself."""

    schema_version: Literal["1.0"] = "1.0"
    authority_version: Literal["plan-authority-v1"] = PLAN_AUTHORITY_VERSION
    authority_epoch_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_source: AuthoritySource
    execution_profile: ExecutionProfile
    issued_at_utc: datetime
    request_id: str = Field(min_length=1, max_length=128)
    question_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    request_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    route_decision_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    planning_decision_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    planning_policy_version: str = Field(min_length=1, max_length=64)
    capability_matrix_version: str = Field(min_length=1, max_length=32)
    capability_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_interaction_intent: InteractionIntent
    capability_query_plan_intent: Intent
    capability_oracle_mode: Literal["search", "compare", "fund_compare", "aggregate"]
    oracle_kind: Literal["search", "aggregate"]
    registry_schema_version: str = Field(min_length=1, max_length=32)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    ontology_renderer_version: Literal["registry-derived-turtle-v1"] = ONTOLOGY_RENDERER_VERSION
    ontology_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    dataset: ProductFamily
    dataset_authority_status: DatasetAuthorityStatus
    dataset_release_id: str = Field(min_length=1, max_length=128)
    approved_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    database_manifest_schema_version: Literal["1.0", "1.1"]
    database_registry_schema_version: str = Field(min_length=1, max_length=32)
    database_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_date: date
    agent_release_id: str | None = Field(default=None, min_length=8, max_length=128)
    agent_release_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    deployment_binding_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    release_context_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    compiler_kind: PlanCompilerKind
    compiler_version: str = Field(min_length=1, max_length=64)
    proposal_provider_name: str | None = Field(default=None, min_length=1, max_length=64)
    proposal_model_name: str | None = Field(default=None, min_length=1, max_length=128)
    verifier_version: Literal["result-verifier-v1"] = RESULT_VERIFIER_VERSION
    core_version: str = Field(min_length=1, max_length=32)
    deadline_budget_ms: StrictInt | None = Field(default=None, ge=1)
    max_candidate_rows: StrictInt = Field(ge=0)
    max_verifier_rows: StrictInt = Field(ge=1)
    max_result_rows: StrictInt = Field(ge=1, le=100)
    cross_family_index: StrictInt | None = Field(default=None, ge=0, le=3)
    cross_family_total: StrictInt | None = Field(default=None, ge=2, le=4)
    execution_context_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_cross_family_slot(self) -> ValidationReceipt:
        release_values = (
            self.agent_release_id,
            self.agent_release_manifest_sha256,
            self.deployment_binding_sha256,
            self.release_context_sha256,
        )
        if any(value is not None for value in release_values) != all(
            value is not None for value in release_values
        ):
            raise ValueError("agent release receipt fields must be present together")
        if (self.cross_family_index is None) != (self.cross_family_total is None):
            raise ValueError("cross-family index and total must be present together")
        if (
            self.cross_family_index is not None
            and self.cross_family_total is not None
            and self.cross_family_index >= self.cross_family_total
        ):
            raise ValueError("cross-family index must be smaller than total")
        if self.compiler_version != _compiler_version(self.compiler_kind):
            raise ValueError("compiler kind and version differ")
        if self.compiler_kind is PlanCompilerKind.GROUNDED_PLAN_GATE:
            if self.proposal_provider_name is None or self.proposal_model_name is None:
                raise ValueError("grounded planning requires provider and model provenance")
        elif self.compiler_kind is PlanCompilerKind.LEGACY_PROVIDER:
            if self.proposal_provider_name is None:
                raise ValueError("legacy provider planning requires provider provenance")
        elif self.proposal_provider_name is not None or self.proposal_model_name is not None:
            raise ValueError("deterministic/internal plans cannot claim provider provenance")
        return self


class ValidatedPlan(PlanAuthorityModel):
    """Nominal Oracle capability containing an immutable canonical plan snapshot.

    ``authority_seal`` is intentionally excluded from dumps. A serialized
    receipt can reproduce the execution conditions, but cannot be deserialized
    back into executable authority.
    """

    canonical_plan_json: str = Field(min_length=2)
    receipt: ValidationReceipt
    authority_seal: str = Field(
        pattern=_SHA256_PATTERN,
        exclude=True,
        repr=False,
    )
    release_guard: object | None = Field(default=None, exclude=True, repr=False)
    database_guard: object | None = Field(default=None, exclude=True, repr=False)

    @property
    def canonical_plan(self) -> QueryPlan:
        return QueryPlan.model_validate_json(self.canonical_plan_json)

    @model_validator(mode="after")
    def validate_plan_receipt_alignment(self) -> ValidatedPlan:
        try:
            plan = QueryPlan.model_validate_json(self.canonical_plan_json)
        except Exception as error:  # noqa: BLE001 - normalize an untrusted wrapper
            raise ValueError("canonical plan JSON is invalid") from error
        if _canonical_query_plan_json(plan) != self.canonical_plan_json:
            raise ValueError("canonical plan JSON is not canonical")
        if _sha256_text(self.canonical_plan_json) != self.receipt.plan_sha256:
            raise ValueError("canonical plan and receipt hash differ")
        if plan.question_id != self.receipt.request_id:
            raise ValueError("canonical plan and receipt request IDs differ")
        if plan.product_families != [self.receipt.dataset]:
            raise ValueError("canonical plan and receipt datasets differ")
        expected_kind = "aggregate" if plan.intent is Intent.AGGREGATE else "search"
        if self.receipt.oracle_kind != expected_kind:
            raise ValueError("canonical plan and receipt Oracle kinds differ")
        release_values = (
            self.receipt.agent_release_id,
            self.receipt.agent_release_manifest_sha256,
            self.receipt.deployment_binding_sha256,
            self.receipt.release_context_sha256,
        )
        if all(value is None for value in release_values):
            if self.release_guard is not None:
                raise ValueError("unbound plan cannot carry an Agent release guard")
        else:
            if type(self.release_guard) is not ResolvedAgentRelease:
                raise ValueError("release-bound plan requires the resolved Agent release")
            guard = self.release_guard
            if (
                guard.release_id != self.receipt.agent_release_id
                or guard.manifest_file_sha256 != self.receipt.agent_release_manifest_sha256
                or guard.binding_file_sha256 != self.receipt.deployment_binding_sha256
                or guard.release_context_sha256 != self.receipt.release_context_sha256
            ):
                raise ValueError("ValidatedPlan and Agent release guard differ")
        if type(self.database_guard) is not PinnedSQLiteArtifact:
            raise ValueError("ValidatedPlan requires its server-opened database guard")
        database_guard = self.database_guard
        if database_guard.database_sha256 != self.receipt.database_sha256:
            raise ValueError("ValidatedPlan and database guard differ")
        return self


# A proposal is deliberately only a migration-friendly alias. It never gains
# Oracle authority until PlanAuthorityGate returns a nominal ValidatedPlan.
ProposedQueryPlan = QueryPlan


@dataclass(frozen=True, slots=True)
class _AuthorityContext:
    capability_matrix_version: str
    capability_matrix_sha256: str
    registry_schema_version: str
    registry_sha256: str
    ontology_bundle_sha256: str


@dataclass(frozen=True, slots=True)
class _DatabaseContext:
    path: Path
    stat_fingerprint: tuple[int, int, int, int, int]
    manifest: DatabaseManifest
    manifest_sha256: str
    database_sha256: str
    authority_status: DatasetAuthorityStatus
    release_id: str
    approved_manifest_sha256: str | None
    guard: PinnedSQLiteArtifact


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_model_sha256(model: BaseModel) -> str:
    return _sha256_text(_canonical_json(model.model_dump(mode="json")))


def _canonical_query_plan_json(plan: QueryPlan) -> str:
    return _canonical_json(plan.model_dump(mode="json"))


def query_plan_authority_sha256(plan: QueryPlan) -> str:
    """Hash every execution-relevant field, including request ID and order."""

    return _sha256_text(_canonical_query_plan_json(plan))


def _database_stat(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@lru_cache(maxsize=1)
def _current_authority_context() -> _AuthorityContext:
    # Local import avoids an execution.oracle <-> capability import cycle.
    from finance_agent_core.config.capability import load_capability_matrix

    registry = load_field_registry()
    capability = load_capability_matrix()
    return _AuthorityContext(
        capability_matrix_version=capability.matrix_version,
        capability_matrix_sha256=_canonical_model_sha256(capability),
        registry_schema_version=registry.schema_version,
        registry_sha256=_canonical_model_sha256(registry),
        ontology_bundle_sha256=ontology_bundle_sha256(registry),
    )


def _canonical_route_sha256(route: RouteDecision) -> str:
    return _canonical_model_sha256(route)


def _canonical_planning_sha256(planning_payload: BaseModel | None) -> str | None:
    if planning_payload is None:
        return None
    return _canonical_model_sha256(planning_payload)


def _inferred_interaction_intent(plan: QueryPlan) -> InteractionIntent:
    return {
        Intent.SEARCH: InteractionIntent.SEARCH,
        Intent.COMPARE: InteractionIntent.COMPARE,
        Intent.AGGREGATE: InteractionIntent.AGGREGATE,
        Intent.EXPLAIN: InteractionIntent.EXPLAIN,
    }[plan.intent]


def _deadline_values() -> tuple[int | None, float | None]:
    deadline = current_request_deadline()
    if deadline is None:
        return None, None
    if deadline.should_stop():
        raise PlanAuthorityError(
            PlanAuthorityCode.DEADLINE_EXCEEDED,
            "request deadline expired before plan authorization",
        )
    remaining_ms = max(1, math.ceil(deadline.remaining_seconds() * 1000))
    return remaining_ms, deadline.expires_at


def _seal_payload(
    canonical_plan_json: str,
    receipt: ValidationReceipt,
    *,
    path: Path,
    stat_fingerprint: tuple[int, int, int, int, int],
    deadline_expires_at: float | None,
) -> bytes:
    payload = {
        "canonical_plan_json": canonical_plan_json,
        "receipt": receipt.model_dump(mode="json"),
        "database_path": str(path),
        "database_stat": stat_fingerprint,
        "deadline_expires_at": deadline_expires_at,
    }
    return _canonical_json(payload).encode("utf-8")


def _seal(
    canonical_plan_json: str,
    receipt: ValidationReceipt,
    *,
    path: Path,
    stat_fingerprint: tuple[int, int, int, int, int],
    deadline_expires_at: float | None,
) -> str:
    return hmac.new(
        _AUTHORITY_KEY,
        _seal_payload(
            canonical_plan_json,
            receipt,
            path=path,
            stat_fingerprint=stat_fingerprint,
            deadline_expires_at=deadline_expires_at,
        ),
        hashlib.sha256,
    ).hexdigest()


class PlanAuthorityGate:
    """Server-owned issuer between a proposed QueryPlan and every DB read."""

    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
        *,
        require_approved_databases: bool,
        allow_internal_disabled_dataset: bool = False,
        allow_internal_evaluation_issuance: bool = False,
        require_request_deadline: bool = False,
        release_guard: ResolvedAgentRelease | None = None,
        require_agent_release: bool = False,
        capability_execution_overrides: set[ProductFamily | str]
        | frozenset[ProductFamily]
        | None = None,
    ) -> None:
        self.database_paths = {
            ProductFamily(key): Path(value) for key, value in database_paths.items()
        }
        self.require_approved_databases = require_approved_databases
        self.allow_internal_disabled_dataset = allow_internal_disabled_dataset
        self.allow_internal_evaluation_issuance = allow_internal_evaluation_issuance
        self.require_request_deadline = require_request_deadline or require_approved_databases
        if release_guard is not None and type(release_guard) is not ResolvedAgentRelease:
            raise TypeError("release_guard must be a ResolvedAgentRelease")
        if require_agent_release and release_guard is None:
            raise ValueError("public Agent release authority requires a resolved release")
        self.release_guard = release_guard
        self.require_agent_release = require_agent_release
        self.capability_execution_overrides = frozenset(
            ProductFamily(value) for value in capability_execution_overrides or set()
        )
        self._database_guards: dict[ProductFamily, PinnedSQLiteArtifact] = {}
        self._database_guard_lock = threading.Lock()

    def validate_routed(
        self,
        proposal: ProposedQueryPlan,
        route_decision: RouteDecision,
        *,
        planning_decision: BaseModel,
        semantic_receipts: tuple[BaseModel, ...] = (),
        compiler_kind: PlanCompilerKind = PlanCompilerKind.SERVER_QUERY_PLAN,
        proposal_provider_name: str | None = None,
        proposal_model_name: str | None = None,
        cross_family_index: int | None = None,
        cross_family_total: int | None = None,
    ) -> ValidatedPlan:
        route = self._canonical_route(route_decision)
        plan = self._canonical_plan(proposal)
        planning, admitted_semantic_receipts = self._canonical_planning_decision(
            planning_decision,
            route,
            compiler_kind=compiler_kind,
            semantic_receipts=semantic_receipts,
        )
        if compiler_kind not in {
            PlanCompilerKind.SERVER_QUERY_PLAN,
            PlanCompilerKind.GROUNDED_PLAN_GATE,
        }:
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "routed plan has an invalid compiler provenance",
            )
        self._require_route_alignment(
            plan,
            route,
            cross_family_index=cross_family_index,
            cross_family_total=cross_family_total,
        )
        self._require_semantic_authority_alignment(
            plan,
            route,
            planning,
            admitted_semantic_receipts,
        )
        return self._issue(
            plan,
            authority_source=AuthoritySource.ROUTED_AGENT,
            planning_policy_version=planning.policy_version,
            route=route,
            planning_decision=planning,
            interaction_intent=route.draft.intent,
            compiler_kind=compiler_kind,
            proposal_provider_name=proposal_provider_name,
            proposal_model_name=proposal_model_name,
            cross_family_index=cross_family_index,
            cross_family_total=cross_family_total,
        )

    def validate_legacy_provider(
        self,
        proposal: ProposedQueryPlan,
        *,
        request_id: str,
        normalized_question: str,
        proposal_provider_name: str,
        proposal_model_name: str | None = None,
    ) -> ValidatedPlan:
        plan = self._canonical_plan(proposal)
        if plan.question_id != request_id:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "legacy provider changed the trusted request ID",
            )
        return self._issue(
            plan,
            authority_source=AuthoritySource.LEGACY_PROVIDER,
            planning_policy_version=LEGACY_PROVIDER_POLICY_VERSION,
            route=None,
            planning_decision=None,
            interaction_intent=_inferred_interaction_intent(plan),
            question_sha256=_sha256_text(normalized_question),
            compiler_kind=PlanCompilerKind.LEGACY_PROVIDER,
            proposal_provider_name=proposal_provider_name,
            proposal_model_name=proposal_model_name,
        )

    def validate_internal_evaluation(
        self,
        proposal: ProposedQueryPlan,
    ) -> ValidatedPlan:
        if not self.allow_internal_evaluation_issuance:
            raise PlanAuthorityError(
                PlanAuthorityCode.EXECUTION_POLICY_BLOCKED,
                "internal evaluation authority is disabled for this gate",
            )
        plan = self._canonical_plan(proposal)
        return self._issue(
            plan,
            authority_source=AuthoritySource.INTERNAL_EVALUATION,
            planning_policy_version=INTERNAL_EVALUATION_POLICY_VERSION,
            route=None,
            planning_decision=None,
            interaction_intent=_inferred_interaction_intent(plan),
            compiler_kind=PlanCompilerKind.INTERNAL_EVALUATION,
        )

    @staticmethod
    def _canonical_plan(proposal: ProposedQueryPlan) -> QueryPlan:
        if not isinstance(proposal, QueryPlan):
            raise PlanAuthorityError(
                PlanAuthorityCode.INVALID_PROPOSAL,
                "plan proposal must use the QueryPlan contract",
            )
        try:
            # Never trust a Pydantic instance: model_copy/model_construct may
            # have skipped validators. JSON round-trip also severs mutable lists.
            payload = _canonical_json(proposal.model_dump(mode="json"))
            return QueryPlan.model_validate_json(payload)
        except Exception as error:  # noqa: BLE001 - normalize untrusted proposals
            raise PlanAuthorityError(
                PlanAuthorityCode.INVALID_PROPOSAL,
                "plan proposal failed canonical QueryPlan validation",
            ) from error

    @staticmethod
    def _canonical_route(route_decision: RouteDecision) -> RouteDecision:
        try:
            payload = _canonical_json(route_decision.model_dump(mode="json"))
            return RouteDecision.model_validate_json(payload)
        except Exception as error:  # noqa: BLE001 - normalize untrusted route copies
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "route decision failed canonical validation",
            ) from error

    @staticmethod
    def _canonical_planning_decision(
        planning_decision: BaseModel,
        route: RouteDecision,
        *,
        compiler_kind: PlanCompilerKind,
        semantic_receipts: tuple[BaseModel, ...],
    ) -> tuple[BaseModel, tuple[BaseModel, ...]]:
        # Local import keeps the execution boundary independent from the
        # agent package at module import time while still validating the exact
        # nominal Stage 1 contract and its route alignment.
        from finance_agent_core.agent.planning_policy import (
            PlanningDecision,
            PlanningDecisionStatus,
            PlanningTrace,
        )
        from finance_agent_core.agent.semantic_resolution import (
            AdaptivePlanningDecisionV2,
            ResolutionPath,
            SemanticResolutionReceipt,
            SpanSource,
        )

        if type(planning_decision) is AdaptivePlanningDecisionV2:
            if compiler_kind is not PlanCompilerKind.SERVER_QUERY_PLAN:
                raise PlanAuthorityError(
                    PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                    "adaptive semantic authority requires the server compiler",
                )
            if len(semantic_receipts) != 1 or type(semantic_receipts[0]) is not (
                SemanticResolutionReceipt
            ):
                raise PlanAuthorityError(
                    PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                    "adaptive semantic authority requires one exact receipt",
                )
            try:
                planning_payload = _canonical_json(planning_decision.model_dump(mode="json"))
                planning = AdaptivePlanningDecisionV2.model_validate_json(planning_payload)
                receipt_payload = _canonical_json(semantic_receipts[0].model_dump(mode="json"))
                receipt = SemanticResolutionReceipt.model_validate_json(receipt_payload)
            except Exception as error:  # noqa: BLE001 - stable authority boundary
                raise PlanAuthorityError(
                    PlanAuthorityCode.ROUTE_MISMATCH,
                    "adaptive semantic authority failed canonical validation",
                ) from error
            expected_path = {
                SpanSource.SCHEMA_DENSE: ResolutionPath.SCHEMA_DENSE,
                SpanSource.HCLX: ResolutionPath.HCLX,
            }[receipt.source]
            if (
                planning.policy_version != ADAPTIVE_SEMANTIC_POLICY_VERSION
                or planning.path is not expected_path
                or planning.receipt_sha256 != receipt.receipt_sha256
            ):
                raise PlanAuthorityError(
                    PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                    "adaptive planning decision differs from its semantic receipt",
                )
            return planning, (receipt,)

        if type(planning_decision) is not PlanningDecision:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "routed authority requires the server PlanningDecision contract",
            )
        if semantic_receipts:
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "legacy planning authority cannot carry semantic receipts",
            )
        try:
            payload = _canonical_json(planning_decision.model_dump(mode="json"))
            planning = PlanningDecision.model_validate_json(payload)
            PlanningTrace(
                route_decision=route,
                planning_decision=planning,
            )
        except Exception as error:  # noqa: BLE001 - stable authority boundary
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "planning decision failed canonical route alignment",
            ) from error
        if (
            planning.policy_version != ADAPTIVE_SHADOW_POLICY_VERSION
            or planning.decision_status is not PlanningDecisionStatus.OK
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "planning decision is not valid for the pinned Stage 1 policy",
            )
        if compiler_kind is PlanCompilerKind.GROUNDED_PLAN_GATE and not planning.hclx_allowed:
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "grounded model planning lacks explicit PlanningDecision authority",
            )
        return planning, ()

    @staticmethod
    def _require_semantic_authority_alignment(
        plan: QueryPlan,
        route: RouteDecision,
        planning_decision: BaseModel,
        semantic_receipts: tuple[BaseModel, ...],
    ) -> None:
        """Bind v2 authority to this request and its compiler-admitted field."""

        from finance_agent_core.agent.semantic_resolution import (
            AdaptivePlanningDecisionV2,
            ResolutionOperation,
            SemanticResolutionReceipt,
        )

        if type(planning_decision) is not AdaptivePlanningDecisionV2:
            if semantic_receipts:
                raise PlanAuthorityError(
                    PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                    "non-adaptive planning cannot carry semantic authority",
                )
            return
        if len(semantic_receipts) != 1 or type(semantic_receipts[0]) is not (
            SemanticResolutionReceipt
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "adaptive semantic receipt is unavailable",
            )
        receipt = semantic_receipts[0]
        if (
            route.disposition is not RouteDisposition.EXECUTE
            or route.query_plan_intent is not Intent.SEARCH
            or len(route.draft.product_families) != 1
            or receipt.request_id_sha256
            != hashlib.sha256(route.draft.request_id.encode("utf-8")).hexdigest()
            or receipt.product_family is not route.draft.product_families[0]
            or plan.product_families != [receipt.product_family]
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "semantic receipt is outside this routed request",
            )
        if receipt.operation is ResolutionOperation.RANK:
            admitted = any(
                ranking.field == receipt.field_id and ranking.direction is receipt.direction
                for ranking in plan.ranking
            )
        elif receipt.operation is ResolutionOperation.PROJECT:
            admitted = receipt.field_id in plan.projection
        else:
            admitted = False
        if not admitted:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "compiled plan does not contain the admitted semantic field",
            )

    @staticmethod
    def _require_route_alignment(
        plan: QueryPlan,
        route: RouteDecision,
        *,
        cross_family_index: int | None,
        cross_family_total: int | None,
    ) -> None:
        if route.disposition is not RouteDisposition.EXECUTE:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "control route cannot acquire plan execution authority",
            )
        if plan.question_id != route.draft.request_id:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "route and plan request IDs differ",
            )
        if plan.intent is not route.query_plan_intent:
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "route and plan intents differ",
            )
        families = route.draft.product_families
        if len(families) == 1:
            if cross_family_index is not None or cross_family_total is not None:
                raise PlanAuthorityError(
                    PlanAuthorityCode.ROUTE_MISMATCH,
                    "single-family route cannot carry a cross-family slot",
                )
            if plan.product_families != families:
                raise PlanAuthorityError(
                    PlanAuthorityCode.ROUTE_MISMATCH,
                    "route and plan product families differ",
                )
            return
        if (
            route.draft.intent is not InteractionIntent.SEARCH
            or plan.intent is not Intent.SEARCH
            or cross_family_index is None
            or cross_family_total != len(families)
            or cross_family_index >= len(families)
            or plan.product_families != [families[cross_family_index]]
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.ROUTE_MISMATCH,
                "cross-family plan slot differs from the routed family order",
            )

    def _issue(
        self,
        plan: QueryPlan,
        *,
        authority_source: AuthoritySource,
        planning_policy_version: str,
        route: RouteDecision | None,
        planning_decision: BaseModel | None,
        interaction_intent: InteractionIntent,
        compiler_kind: PlanCompilerKind,
        proposal_provider_name: str | None = None,
        proposal_model_name: str | None = None,
        question_sha256: str | None = None,
        cross_family_index: int | None = None,
        cross_family_total: int | None = None,
    ) -> ValidatedPlan:
        if len(plan.product_families) != 1:
            raise PlanAuthorityError(
                PlanAuthorityCode.INVALID_PROPOSAL,
                "one ValidatedPlan must target exactly one product family",
            )
        if plan.ambiguities or plan.unsupported_conditions:
            raise PlanAuthorityError(
                PlanAuthorityCode.EXECUTION_POLICY_BLOCKED,
                "ambiguous or unsupported plan cannot acquire execution authority",
            )
        if (proposal_provider_name is not None and not proposal_provider_name.strip()) or (
            proposal_model_name is not None and not proposal_model_name.strip()
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.INVALID_PROPOSAL,
                "compiler provenance cannot contain blank values",
            )
        if compiler_kind is PlanCompilerKind.GROUNDED_PLAN_GATE:
            if proposal_provider_name is None or proposal_model_name is None:
                raise PlanAuthorityError(
                    PlanAuthorityCode.INVALID_PROPOSAL,
                    "grounded planning requires provider and model provenance",
                )
        elif compiler_kind is PlanCompilerKind.LEGACY_PROVIDER:
            if proposal_provider_name is None:
                raise PlanAuthorityError(
                    PlanAuthorityCode.INVALID_PROPOSAL,
                    "legacy provider planning requires provider provenance",
                )
        elif proposal_provider_name is not None or proposal_model_name is not None:
            raise PlanAuthorityError(
                PlanAuthorityCode.INVALID_PROPOSAL,
                "deterministic/internal plans cannot claim provider provenance",
            )

        family = plan.product_families[0]
        internal_disabled = self.allow_internal_disabled_dataset
        public_capability_override = family in self.capability_execution_overrides
        uses_disabled_capability_policy = internal_disabled or public_capability_override
        try:
            if plan.intent is Intent.AGGREGATE:
                policy = (
                    require_internal_evaluation_aggregation
                    if uses_disabled_capability_policy
                    else require_executable_aggregation
                )
            elif plan.intent is Intent.COMPARE:
                policy = (
                    require_internal_evaluation_comparison
                    if uses_disabled_capability_policy
                    else require_executable_comparison
                )
            elif plan.intent is Intent.SEARCH:
                policy = (
                    require_internal_evaluation_search
                    if uses_disabled_capability_policy
                    else require_executable_search
                )
            else:
                raise ValueError("no Oracle execution policy for plan intent")
            policy(plan)
        except PlanExecutionBlockedError as error:
            raise PlanAuthorityError(
                PlanAuthorityCode.EXECUTION_POLICY_BLOCKED,
                str(error),
            ) from error
        except Exception as error:  # noqa: BLE001 - stable authority boundary
            raise PlanAuthorityError(
                PlanAuthorityCode.EXECUTION_POLICY_BLOCKED,
                "plan failed the deterministic execution policy",
            ) from error

        capability_entry, authority_context = self._require_capability(
            family,
            interaction_intent,
            plan.intent,
            route,
        )
        database = self._database_context(family)
        if self.require_agent_release:
            if self.release_guard is None:
                raise PlanAuthorityError(
                    PlanAuthorityCode.RELEASE_MISMATCH,
                    "public plan authority has no resolved Agent release",
                )
            try:
                self.release_guard.assert_request_current()
            except Exception as error:  # noqa: BLE001 - stable execution boundary
                raise PlanAuthorityError(
                    PlanAuthorityCode.RELEASE_MISMATCH,
                    "active Agent release is stale",
                ) from error
            if database.authority_status is DatasetAuthorityStatus.OFFICIAL_APPROVED:
                datasets = self.release_guard.manifest.components.approved_datasets
                snapshot = datasets.snapshots[family.value]
                if (
                    datasets.release_id != database.release_id
                    or datasets.manifest.contract_sha256 != database.approved_manifest_sha256
                    or snapshot.database_sha256 != database.database_sha256
                    or snapshot.data_file_sha256 != database.manifest.source_file_sha256
                    or snapshot.source_snapshot_date
                    != database.manifest.source_snapshot_date.isoformat()
                    or snapshot.manifest_schema_version != database.manifest.schema_version
                ):
                    raise PlanAuthorityError(
                        PlanAuthorityCode.RELEASE_MISMATCH,
                        "approved database differs from the active Agent release",
                    )
        deadline_budget_ms, deadline_expires_at = _deadline_values()
        if self.require_request_deadline and deadline_budget_ms is None:
            raise PlanAuthorityError(
                PlanAuthorityCode.DEADLINE_EXCEEDED,
                "request deadline is required before public plan authorization",
            )
        compiler_version = _compiler_version(compiler_kind)
        canonical_plan_json = _canonical_query_plan_json(plan)
        plan_sha256 = _sha256_text(canonical_plan_json)
        route_sha256 = None if route is None else _canonical_route_sha256(route)
        planning_sha256 = _canonical_planning_sha256(planning_decision)
        actual_question_sha256 = question_sha256
        if actual_question_sha256 is None and route is not None:
            actual_question_sha256 = _sha256_text(route.draft.question)

        execution_profile = ExecutionProfile.PUBLIC
        if authority_source is AuthoritySource.INTERNAL_EVALUATION:
            execution_profile = ExecutionProfile.INTERNAL_EVALUATION
        elif internal_disabled:
            execution_profile = ExecutionProfile.INTERNAL_DISABLED_DATASET
        elif public_capability_override:
            execution_profile = ExecutionProfile.PUBLIC_CAPABILITY_OVERRIDE

        approval_context = {
            "authority_status": database.authority_status.value,
            "release_id": database.release_id,
            "approved_manifest_sha256": database.approved_manifest_sha256,
            "database_sha256": database.database_sha256,
            "manifest_sha256": database.manifest_sha256,
        }
        execution_context = {
            "authority_version": PLAN_AUTHORITY_VERSION,
            "authority_epoch_sha256": _AUTHORITY_EPOCH_SHA256,
            "authority_source": authority_source.value,
            "execution_profile": execution_profile.value,
            "planning_policy_version": planning_policy_version,
            "capability_matrix_sha256": authority_context.capability_matrix_sha256,
            "registry_sha256": authority_context.registry_sha256,
            "ontology_bundle_sha256": authority_context.ontology_bundle_sha256,
            "approval": approval_context,
            "compiler_kind": compiler_kind.value,
            "compiler_version": compiler_version,
            "proposal_provider_name": proposal_provider_name,
            "proposal_model_name": proposal_model_name,
            "verifier_version": RESULT_VERIFIER_VERSION,
            "capability_overrides": sorted(
                item.value for item in self.capability_execution_overrides
            ),
            "allow_internal_disabled_dataset": self.allow_internal_disabled_dataset,
            "require_request_deadline": self.require_request_deadline,
            "agent_release_id": (
                None if self.release_guard is None else self.release_guard.release_id
            ),
            "agent_release_manifest_sha256": (
                None if self.release_guard is None else self.release_guard.manifest_file_sha256
            ),
            "deployment_binding_sha256": (
                None if self.release_guard is None else self.release_guard.binding_file_sha256
            ),
            "release_context_sha256": (
                None if self.release_guard is None else self.release_guard.release_context_sha256
            ),
        }
        request_scope = {
            "request_id": plan.question_id,
            "question_sha256": actual_question_sha256,
            "route_sha256": route_sha256,
            "planning_sha256": planning_sha256,
            "plan_sha256": plan_sha256,
            "cross_family_index": cross_family_index,
            "cross_family_total": cross_family_total,
        }
        receipt = ValidationReceipt(
            authority_epoch_sha256=_AUTHORITY_EPOCH_SHA256,
            authority_source=authority_source,
            execution_profile=execution_profile,
            issued_at_utc=datetime.now(UTC),
            request_id=plan.question_id,
            question_sha256=actual_question_sha256,
            request_scope_sha256=_sha256_text(_canonical_json(request_scope)),
            route_decision_sha256=route_sha256,
            planning_decision_sha256=planning_sha256,
            plan_sha256=plan_sha256,
            planning_policy_version=planning_policy_version,
            capability_matrix_version=authority_context.capability_matrix_version,
            capability_matrix_sha256=authority_context.capability_matrix_sha256,
            capability_interaction_intent=interaction_intent,
            capability_query_plan_intent=plan.intent,
            capability_oracle_mode=capability_entry.oracle_mode,
            oracle_kind="aggregate" if plan.intent is Intent.AGGREGATE else "search",
            registry_schema_version=authority_context.registry_schema_version,
            registry_sha256=authority_context.registry_sha256,
            ontology_bundle_sha256=authority_context.ontology_bundle_sha256,
            dataset=family,
            dataset_authority_status=database.authority_status,
            dataset_release_id=database.release_id,
            approved_manifest_sha256=database.approved_manifest_sha256,
            database_manifest_schema_version=database.manifest.schema_version,
            database_registry_schema_version=database.manifest.registry_schema_version,
            database_manifest_sha256=database.manifest_sha256,
            database_sha256=database.database_sha256,
            source_file_sha256=database.manifest.source_file_sha256,
            source_snapshot_date=database.manifest.source_snapshot_date,
            agent_release_id=(
                None if self.release_guard is None else self.release_guard.release_id
            ),
            agent_release_manifest_sha256=(
                None if self.release_guard is None else self.release_guard.manifest_file_sha256
            ),
            deployment_binding_sha256=(
                None if self.release_guard is None else self.release_guard.binding_file_sha256
            ),
            release_context_sha256=(
                None if self.release_guard is None else self.release_guard.release_context_sha256
            ),
            compiler_kind=compiler_kind,
            compiler_version=compiler_version,
            proposal_provider_name=proposal_provider_name,
            proposal_model_name=proposal_model_name,
            core_version=__version__,
            deadline_budget_ms=deadline_budget_ms,
            max_candidate_rows=database.manifest.searchable_rows,
            max_verifier_rows=(
                database.manifest.logical_product_rows or database.manifest.total_rows
            ),
            max_result_rows=plan.limit,
            cross_family_index=cross_family_index,
            cross_family_total=cross_family_total,
            execution_context_sha256=_sha256_text(_canonical_json(execution_context)),
        )
        authority_seal = _seal(
            canonical_plan_json,
            receipt,
            path=database.path,
            stat_fingerprint=database.stat_fingerprint,
            deadline_expires_at=deadline_expires_at,
        )
        return ValidatedPlan(
            canonical_plan_json=canonical_plan_json,
            receipt=receipt,
            authority_seal=authority_seal,
            release_guard=self.release_guard,
            database_guard=database.guard,
        )

    @staticmethod
    def _require_capability(
        family: ProductFamily,
        interaction_intent: InteractionIntent,
        query_plan_intent: Intent,
        route: RouteDecision | None,
    ):
        from finance_agent_core.config.capability import load_capability_matrix

        matrix = load_capability_matrix()
        context = _current_authority_context()
        if route is not None and route.capability_matrix_version != matrix.matrix_version:
            raise PlanAuthorityError(
                PlanAuthorityCode.CAPABILITY_MISMATCH,
                "route capability matrix version is stale",
            )
        entry = matrix.require(family, interaction_intent)
        if (
            entry.status != "executable"
            or entry.query_plan_intent is not query_plan_intent
            or entry.oracle_mode == "none"
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.CAPABILITY_MISMATCH,
                "route is outside the pinned capability matrix",
            )
        return entry, context

    def _database_context(self, family: ProductFamily) -> _DatabaseContext:
        try:
            configured_path = self.database_paths[family]
            path = configured_path.resolve(strict=True)
            initial_stat_fingerprint = _database_stat(path)
        except (KeyError, OSError) as error:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_NOT_CONFIGURED,
                f"{family.value} database is not configured",
            ) from error

        try:
            with self._database_guard_lock:
                guard = self._database_guards.get(family)
                if guard is None:
                    guard = PinnedSQLiteArtifact(path)
                    self._database_guards[family] = guard
                elif guard.path != path:
                    raise PinnedSQLiteError("configured database path changed after pinning")
            guard.assert_current_path()
            if guard.stat_fingerprint != initial_stat_fingerprint:
                raise PinnedSQLiteError("configured database inode differs from its guard")
        except PinnedSQLiteError as error:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_MISMATCH,
                "database could not be pinned to one approved artifact",
            ) from error

        approved_manifest_sha256: str | None = None
        if self.require_approved_databases:
            approval = load_approved_dataset_manifest()
            try:
                approved_manifest = require_approved_database(family.value, path)
                with guard.connect_read_only(
                    audit_reason_prefix="authority_connection"
                ) as connection:
                    manifest = load_manifest(connection)
                    integrity = connection.execute("PRAGMA quick_check").fetchone()
            except Exception as error:  # noqa: BLE001 - normalize approval failures
                raise PlanAuthorityError(
                    PlanAuthorityCode.DATASET_MISMATCH,
                    "database is not the approved competition dataset",
                ) from error
            approved = approval.datasets[family.value]
            if (
                manifest != approved_manifest
                or integrity is None
                or integrity[0] != "ok"
                or guard.stat_fingerprint[2] != approved.database_file_size_bytes
                or guard.database_sha256 != approved.database_sha256
            ):
                raise PlanAuthorityError(
                    PlanAuthorityCode.DATASET_MISMATCH,
                    "opened database artifact differs from the approved competition dataset",
                )
            database_sha256 = approved.database_sha256
            authority_status = DatasetAuthorityStatus.OFFICIAL_APPROVED
            release_id = approval.release_id
            approved_manifest_sha256 = approval.canonical_sha256
        else:
            try:
                with guard.connect_read_only(
                    audit_reason_prefix="authority_connection"
                ) as connection:
                    manifest = load_manifest(connection)
            except Exception as error:  # noqa: BLE001 - normalize fixture failures
                raise PlanAuthorityError(
                    PlanAuthorityCode.DATASET_MISMATCH,
                    "database manifest is unavailable",
                ) from error
            if manifest.dataset != family.value:
                raise PlanAuthorityError(
                    PlanAuthorityCode.DATASET_MISMATCH,
                    "plan family differs from the database manifest",
                )
            authority_status = DatasetAuthorityStatus.TEST_FIXTURE
            release_id = "internal-test-fixture-v1"
            database_sha256 = ""

        if manifest.dataset != family.value:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_MISMATCH,
                "plan family differs from the database manifest",
            )
        try:
            stat_fingerprint = _database_stat(path)
        except OSError as error:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_MISMATCH,
                "database changed while validation was in progress",
            ) from error
        if stat_fingerprint != initial_stat_fingerprint:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_MISMATCH,
                "database changed while validation was in progress",
            )
        if guard.stat_fingerprint != stat_fingerprint:
            raise PlanAuthorityError(
                PlanAuthorityCode.DATASET_MISMATCH,
                "database changed while validation was in progress",
            )
        if not database_sha256:
            database_sha256 = guard.database_sha256
        return _DatabaseContext(
            path=path,
            stat_fingerprint=stat_fingerprint,
            manifest=manifest,
            manifest_sha256=_canonical_model_sha256(manifest),
            database_sha256=database_sha256,
            authority_status=authority_status,
            release_id=release_id,
            approved_manifest_sha256=approved_manifest_sha256,
            guard=guard,
        )


def authorize_internal_evaluation_plan(
    plan: ProposedQueryPlan,
    database_path: str | Path,
) -> ValidatedPlan:
    """Explicit issuer for regression/evaluation code; never used by FastAPI."""

    family = PlanAuthorityGate._canonical_plan(plan).product_families[0]
    return PlanAuthorityGate(
        {family: database_path},
        require_approved_databases=False,
        allow_internal_disabled_dataset=True,
        allow_internal_evaluation_issuance=True,
    ).validate_internal_evaluation(plan)


def _require_current_context(receipt: ValidationReceipt) -> None:
    context = _current_authority_context()
    if (
        receipt.authority_version != PLAN_AUTHORITY_VERSION
        or receipt.authority_epoch_sha256 != _AUTHORITY_EPOCH_SHA256
        or receipt.capability_matrix_version != context.capability_matrix_version
        or receipt.capability_matrix_sha256 != context.capability_matrix_sha256
        or receipt.registry_schema_version != context.registry_schema_version
        or receipt.registry_sha256 != context.registry_sha256
        or receipt.ontology_renderer_version != ONTOLOGY_RENDERER_VERSION
        or receipt.ontology_bundle_sha256 != context.ontology_bundle_sha256
        or receipt.compiler_version != _compiler_version(receipt.compiler_kind)
        or receipt.verifier_version != RESULT_VERIFIER_VERSION
        or receipt.core_version != __version__
    ):
        raise PlanAuthorityError(
            PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
            "validated plan authority context is stale",
        )
    if receipt.dataset_authority_status is DatasetAuthorityStatus.OFFICIAL_APPROVED:
        approval = load_approved_dataset_manifest()
        approved = approval.datasets[receipt.dataset.value]
        if (
            receipt.dataset_release_id != approval.release_id
            or receipt.approved_manifest_sha256 != approval.canonical_sha256
            or receipt.database_sha256 != approved.database_sha256
            or receipt.source_file_sha256 != approved.data_file_sha256
            or receipt.source_snapshot_date != approved.source_snapshot_date
        ):
            raise PlanAuthorityError(
                PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
                "validated plan approved dataset context is stale",
            )


def require_validated_plan(
    candidate: object,
    database_path: str | Path,
    *,
    oracle_kind: Literal["search", "aggregate"] | None = None,
) -> QueryPlan:
    """Verify nominal type, full hashes, context, DB binding, deadline and seal."""

    if type(candidate) is not ValidatedPlan:
        raise PlanAuthorityError(
            PlanAuthorityCode.UNAUTHORIZED_PLAN_TYPE,
            "Oracle requires a server-issued ValidatedPlan",
        )
    validated = candidate
    try:
        # Rebuild the wrapper so model_construct/model_copy cannot skip its
        # alignment checks. model_dump excludes the proof by design, so only
        # the observed proof is copied into the independently validated model.
        rebuilt = ValidatedPlan.model_validate(
            {
                **validated.model_dump(mode="python"),
                "authority_seal": validated.authority_seal,
                "release_guard": validated.release_guard,
                "database_guard": validated.database_guard,
            }
        )
    except Exception as error:  # noqa: BLE001 - normalize forged wrappers
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "ValidatedPlan structure is invalid",
        ) from error
    if oracle_kind is not None and rebuilt.receipt.oracle_kind != oracle_kind:
        raise PlanAuthorityError(
            PlanAuthorityCode.ORACLE_MODE_MISMATCH,
            "ValidatedPlan was issued for a different Oracle kind",
        )
    _require_current_context(rebuilt.receipt)
    if rebuilt.release_guard is not None:
        try:
            rebuilt.release_guard.assert_request_current()
        except Exception as error:  # noqa: BLE001 - stable Oracle boundary
            raise PlanAuthorityError(
                PlanAuthorityCode.RELEASE_MISMATCH,
                "active Agent release changed before Oracle execution",
            ) from error
    database_guard = rebuilt.database_guard
    if type(database_guard) is not PinnedSQLiteArtifact:
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "ValidatedPlan database guard is invalid",
        )
    try:
        path = Path(database_path).resolve(strict=True)
        if path != database_guard.path:
            raise PinnedSQLiteError("Oracle path differs from the validated database guard")
        database_guard.assert_current_path()
        stat_fingerprint = database_guard.stat_fingerprint
    except (OSError, PinnedSQLiteError) as error:
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "validated database path is unavailable or stale",
        ) from error
    deadline = current_request_deadline()
    deadline_expires_at: float | None = None
    if rebuilt.receipt.deadline_budget_ms is not None:
        if deadline is None or deadline.should_stop():
            raise PlanAuthorityError(
                PlanAuthorityCode.DEADLINE_EXCEEDED,
                "validated request deadline is absent or expired",
            )
        deadline_expires_at = deadline.expires_at
    elif deadline is not None:
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "ValidatedPlan deadline scope differs from the execution scope",
        )
    expected = _seal(
        rebuilt.canonical_plan_json,
        rebuilt.receipt,
        path=path,
        stat_fingerprint=stat_fingerprint,
        deadline_expires_at=deadline_expires_at,
    )
    if not hmac.compare_digest(rebuilt.authority_seal, expected):
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "ValidatedPlan authority seal is invalid or stale",
        )
    raise_if_request_stopped()
    plan = rebuilt.canonical_plan
    if plan.limit > rebuilt.receipt.max_result_rows:
        raise PlanAuthorityError(
            PlanAuthorityCode.ROW_BUDGET_EXCEEDED,
            "plan result limit exceeds its validated row budget",
        )
    return plan


@contextmanager
def open_validated_database(
    validated: ValidatedPlan,
    database_path: str | Path,
    *,
    oracle_kind: Literal["search", "aggregate"] | None = None,
    connection_audit_reason: ConnectionAuditReason | None = None,
) -> Iterator[tuple[QueryPlan, sqlite3.Connection, DatabaseManifest]]:
    """Yield the exact SQLite inode bound to a server-issued plan.

    The hidden guard is opened by ``PlanAuthorityGate`` and is never rebuilt
    from a caller-controlled path.  Both the Oracle and verifier projection use
    this boundary so they cannot observe different files during a path race.
    """

    plan = require_validated_plan(validated, database_path, oracle_kind=oracle_kind)
    guard = validated.database_guard
    if type(guard) is not PinnedSQLiteArtifact:
        raise PlanAuthorityError(
            PlanAuthorityCode.INVALID_AUTHORITY_SEAL,
            "ValidatedPlan database guard is invalid",
        )
    try:
        with guard.connect_read_only(audit_reason_prefix=connection_audit_reason) as connection:
            manifest = load_manifest(connection)
            require_manifest_binding(validated, manifest)
            yield plan, connection, manifest
    except PlanAuthorityError:
        raise
    except PinnedSQLiteError as error:
        raise PlanAuthorityError(
            PlanAuthorityCode.DATASET_MISMATCH,
            "opened database artifact changed during execution",
        ) from error
    require_validated_plan(validated, database_path, oracle_kind=oracle_kind)


def require_manifest_binding(
    validated: ValidatedPlan,
    manifest: DatabaseManifest,
) -> None:
    """Recheck the opened DB manifest before any plan-driven SQL executes."""

    receipt = validated.receipt
    if (
        manifest.dataset != receipt.dataset.value
        or _canonical_model_sha256(manifest) != receipt.database_manifest_sha256
        or manifest.schema_version != receipt.database_manifest_schema_version
        or manifest.registry_schema_version != receipt.database_registry_schema_version
        or manifest.source_file_sha256 != receipt.source_file_sha256
        or manifest.source_snapshot_date != receipt.source_snapshot_date
        or manifest.searchable_rows != receipt.max_candidate_rows
        or (manifest.logical_product_rows or manifest.total_rows) != receipt.max_verifier_rows
    ):
        raise PlanAuthorityError(
            PlanAuthorityCode.DATASET_MISMATCH,
            "opened database manifest differs from the validated receipt",
        )


def require_candidate_budget(validated: ValidatedPlan, candidate_count: int) -> None:
    if candidate_count > validated.receipt.max_candidate_rows:
        raise PlanAuthorityError(
            PlanAuthorityCode.ROW_BUDGET_EXCEEDED,
            "database candidate count exceeds the validated row budget",
        )


def require_verifier_budget(validated: ValidatedPlan, verifier_row_count: int) -> None:
    if verifier_row_count != validated.receipt.max_verifier_rows:
        raise PlanAuthorityError(
            PlanAuthorityCode.DATASET_MISMATCH,
            "verifier universe size differs from the validated dataset",
        )
