from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentExecutionClause,
    SemanticFrameDirection,
    SemanticFrameOperation,
    SemanticFrameStatus,
    semantic_frame_clause_is_schema_admissible,
)
from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningPath,
    PlanningTrace,
)
from finance_agent_core.agent.semantic_admission import (
    is_generic_semantic_ambiguity,
    semantic_field_evidence,
)
from finance_agent_core.agent.semantic_aliases import typed_alias_matches
from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.agent.semantic_ledger import build_resolved_span_ledger
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
    SpanSource,
    canonical_sha256,
)
from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    ProductFamily,
    QueryPlan,
    SortDirection,
    Unit,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.deadline import RequestDeadlineExceeded
from finance_agent_core.retrieval.schema_adaptive import (
    AdaptiveSchemaCandidateSet,
    AdaptiveSchemaLinkUnavailable,
    ProductionHybridSchemaLinker,
)

_ASCENDING = re.compile(
    r"낮(?:은|게|다|고|으며|아|을수록)|"
    r"작(?:은|게|다|고|으며|아|을수록)|"
    r"적(?:은|게|다|고|으며|어|을수록)|"
    r"하위|오름차순|ascending",
    re.IGNORECASE,
)
_DESCENDING = re.compile(
    r"높(?:은|게|다|고|으며|아|을수록)|"
    r"큰|크(?:게|다|고|며)|클수록|"
    r"많(?:은|게|다|고|으며|아|을수록)|"
    r"상위|내림차순|descending",
    re.IGNORECASE,
)
_FRAME_NUMBER = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:천억|백억|십억|조|억|만|천)?\s*"
    r"(?:퍼센트|%|원|달러|USD|KRW|개월|년|일)?",
    re.IGNORECASE,
)
_FRAME_SCALES = {
    "천억": Decimal("100000000000"),
    "백억": Decimal("10000000000"),
    "십억": Decimal("1000000000"),
    "조": Decimal("1000000000000"),
    "억": Decimal("100000000"),
    "만": Decimal("10000"),
    "천": Decimal("1000"),
}
_FRAME_OPERATOR_CUES = {
    ConstraintOperator.GTE: re.compile(r"이상|적어도|최소|≥|(?<![<>=])>=(?!=)"),
    ConstraintOperator.GT: re.compile(r"초과|넘는|보다\s*(?:높|크|많)|(?<![<>=])>(?!=)"),
    ConstraintOperator.LTE: re.compile(r"이하|최대|많아야|넘지\s*않|≤|(?<![<>=])<=(?!=)"),
    ConstraintOperator.LT: re.compile(r"미만|보다\s*(?:낮|작|적)|(?<![<>=])<(?!=)"),
    ConstraintOperator.BETWEEN: re.compile(r"사이|범위|부터.+까지"),
    ConstraintOperator.EQ: re.compile(r"정확히|같은|동일|(?<![<>=])=(?!=)"),
}


class AdaptiveSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveSemanticOutcome(AdaptiveSemanticModel):
    status: Literal["resolved", "clarify", "unsupported"]
    reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    hard_filter_lock: HardFilterLock | None = None
    receipt: SemanticResolutionReceipt | None = None
    semantic_constraints: tuple[Constraint, ...] = Field(default=(), max_length=4)
    planning_decision: AdaptivePlanningDecisionV2
    dense_attempted: bool = False
    hclx_frame_used: bool = False
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
        if not resolved and self.semantic_constraints:
            raise ValueError("control semantic outcome cannot carry constraints")
        if self.semantic_constraints and (
            self.receipt is None
            or self.receipt.operation is not ResolutionOperation.FILTER
            or len(self.semantic_constraints) != 1
            or canonical_sha256(self.semantic_constraints[0].model_dump(mode="json"))
            != self.receipt.constraint_sha256
        ):
            raise ValueError("semantic constraint differs from its authority receipt")
        audit_values = (
            self.index_manifest_sha256,
            self.model_revision_sha256,
            self.model_snapshot_manifest_sha256,
        )
        if self.dense_attempted != all(value is not None for value in audit_values):
            raise ValueError("Dense attempt requires complete redacted artifact linkage")
        return self


@dataclass(frozen=True, slots=True)
class AdaptiveSemanticPreparation:
    """Server-owned input frozen before the primary HCX analysis is consumed.

    This object is request-local and deliberately contains the literal residual
    span.  It is never emitted to Audit.  ``binding_sha256`` proves that a later,
    conditional KURE fallback still belongs to the exact Router ledger and
    HardFilterLock that are about to acquire Compiler authority.
    """

    request_id: str
    question_sha256: str
    product_family: ProductFamily
    interaction_intent: InteractionIntent
    residual_text: str
    residual_start: int
    residual_end: int
    operation: ResolutionOperation
    expected_direction: SortDirection | None
    base_plan: QueryPlan
    hard_filter_lock: HardFilterLock
    binding_sha256: str


@dataclass(frozen=True, slots=True)
class _AdaptiveSemanticProbe:
    """Bounded KURE observation bound to one immutable preparation."""

    preparation_sha256: str
    linked: AdaptiveSchemaCandidateSet | None
    failure_reason: str | None
    index_manifest_sha256: str
    model_revision_sha256: str
    model_snapshot_manifest_sha256: str

    @property
    def candidate_count(self) -> int:
        return 0 if self.linked is None else len(self.linked.candidates)


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
    """Use a primary HCX Semantic Frame, then a bounded Schema Dense fallback.

    Lexical/Ontology-owned values remain locked before this resolver runs.  For
    one unresolved ranking or numeric-filter meaning, a fully grounded HCX frame
    is checked first.  KURE is called only when that frame is absent or rejected;
    it can propose one calibrated, server-grounded field but cannot trigger a
    second HCX call.  Numeric values, operators, units, and currency are always
    reparsed and locked from literal user spans.
    """

    def __init__(
        self,
        schema_linker: ProductionHybridSchemaLinker,
        *,
        gate: SemanticResolutionGate | None = None,
        planning_policy: AdaptivePlanningPolicyV2 | None = None,
        execution_enabled: bool = True,
    ) -> None:
        if type(schema_linker) is not ProductionHybridSchemaLinker:
            raise TypeError("adaptive semantic resolver requires the production schema linker")
        if type(execution_enabled) is not bool:
            raise TypeError("execution_enabled must be a boolean")
        self.schema_linker = schema_linker
        self.execution_enabled = execution_enabled
        self.gate = gate or SemanticResolutionGate()
        if planning_policy is not None and type(planning_policy) is not AdaptivePlanningPolicyV2:
            raise TypeError("adaptive semantic resolver requires the server planning policy v2")
        self.planning_policy = planning_policy or AdaptivePlanningPolicyV2()

    @staticmethod
    def _matching_hclx_clause(
        draft: IntentAnalysisDraft | None,
        *,
        residual_text: str,
        family: ProductFamily,
    ) -> IntentExecutionClause | None:
        if draft is None:
            return None
        matches = []
        for clause in draft.execution_clauses:
            if (
                clause.semantic_status is not SemanticFrameStatus.RESOLVED
                or not semantic_frame_clause_is_schema_admissible(draft, clause)
                or clause.operation
                not in {SemanticFrameOperation.RANK, SemanticFrameOperation.FILTER}
                or clause.field_id == "__none__"
                or family not in set(clause.product_families or draft.product_families)
                or not clause.field_span
                or not (residual_text in clause.text_span or clause.text_span in residual_text)
            ):
                continue
            matches.append(clause)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _server_numeric_constraint(
        clause: IntentExecutionClause,
        *,
        family: ProductFamily,
        base_plan: QueryPlan | None = None,
    ) -> Constraint:
        """Reparse one HCX-quoted number; never trust a model-generated value."""

        definition = load_field_registry().require_field(
            clause.field_id,
            [family.value],
        )
        if definition.value_type is not ValueType.NUMBER:
            raise SemanticResolutionError("HCLX filter value is not server-parsable")
        if clause.field_span not in clause.text_span or clause.value_span not in clause.text_span:
            raise SemanticResolutionError("HCLX filter spans do not share one literal clause")
        operator = ConstraintOperator(clause.operator.value)
        if operator.value not in definition.allowed_operators:
            raise SemanticResolutionError("HCLX filter operator is unavailable")
        cue = _FRAME_OPERATOR_CUES.get(operator)
        if cue is None or cue.search(clause.text_span) is None:
            raise SemanticResolutionError("HCLX filter operator lacks literal evidence")
        numbers = tuple(_FRAME_NUMBER.finditer(clause.value_span))
        expected_count = 2 if operator is ConstraintOperator.BETWEEN else 1
        if len(numbers) != expected_count:
            raise SemanticResolutionError("HCLX filter value lacks a numeric anchor")

        def parse_number(surface: str) -> int | float:
            raw = re.sub(r"\s+", "", surface).replace(",", "")
            percent_unit = raw.endswith(("퍼센트", "%"))
            calendar_unit = next(
                (unit for unit in ("개월", "년", "일") if raw.endswith(unit)),
                None,
            )
            currency_unit = (
                "USD"
                if raw.casefold().endswith(("달러", "usd"))
                else "KRW"
                if raw.casefold().endswith(("krw", "원"))
                else None
            )
            scale_unit = next(
                (suffix for suffix in _FRAME_SCALES if suffix in raw),
                None,
            )
            explicit_unit_groups = sum(
                (
                    percent_unit,
                    calendar_unit is not None,
                    currency_unit is not None,
                )
            )
            if explicit_unit_groups > 1:
                raise SemanticResolutionError("HCLX filter value mixes incompatible units")
            if percent_unit and definition.unit != Unit.PCT_POINT.value:
                raise SemanticResolutionError("HCLX filter percentage unit differs from Registry")
            if calendar_unit is not None and definition.unit not in {
                Unit.DAY.value,
                Unit.YEAR.value,
            }:
                raise SemanticResolutionError("HCLX filter calendar unit differs from Registry")
            if currency_unit is not None and definition.unit != Unit.SOURCE_CURRENCY_AMOUNT.value:
                raise SemanticResolutionError("HCLX filter currency unit differs from Registry")
            if scale_unit is not None and definition.unit not in {
                Unit.SOURCE_CURRENCY_AMOUNT.value,
                Unit.SOURCE_QUANTITY.value,
            }:
                raise SemanticResolutionError("HCLX filter scale unit differs from Registry")
            if currency_unit is not None and base_plan is not None:
                locked_currencies = {
                    str(item.value).upper()
                    for item in base_plan.constraints
                    if item.field == "trading_currency"
                    and item.operator is ConstraintOperator.EQ
                    and isinstance(item.value, str)
                }
                # Domestic ETP amount fields are intrinsically KRW-denominated
                # in the approved schema.  The deterministic compiler may omit
                # a redundant ``trading_currency=KRW`` constraint, so absence
                # of that lock is not loss of user meaning.  This exception is
                # deliberately narrow: an explicit conflicting lock, a foreign
                # currency, or any other product family remains fail-closed.
                intrinsic_domestic_krw = (
                    family is ProductFamily.DOMESTIC_ETP
                    and currency_unit == "KRW"
                    and not locked_currencies
                )
                if locked_currencies != {currency_unit} and not intrinsic_domestic_krw:
                    raise SemanticResolutionError(
                        "HCLX filter currency is not preserved by the hard-filter lock"
                    )
            for suffix in ("퍼센트", "%", "달러", "USD", "KRW", "원", "개월", "년", "일"):
                if raw.casefold().endswith(suffix.casefold()):
                    raw = raw[: -len(suffix)]
                    break
            scale = Decimal("1")
            for suffix, multiplier in _FRAME_SCALES.items():
                if raw.endswith(suffix):
                    raw = raw[: -len(suffix)]
                    scale = multiplier
                    break
            try:
                parsed = Decimal(raw) * scale
            except InvalidOperation as error:
                raise SemanticResolutionError("HCLX filter number is invalid") from error
            if calendar_unit is not None:
                if definition.unit == Unit.DAY.value:
                    parsed *= {"년": 365, "개월": 30, "일": 1}[calendar_unit]
                elif definition.unit == Unit.YEAR.value:
                    parsed *= {
                        "년": Decimal("1"),
                        "개월": Decimal("0.0833333333333333"),
                        "일": Decimal("0.00273972602739726"),
                    }[calendar_unit]
                else:
                    raise SemanticResolutionError(
                        "HCLX filter calendar unit differs from the Registry"
                    )
            return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)

        parsed_values = [parse_number(item.group(0)) for item in numbers]
        value: int | float | list[int | float] = (
            parsed_values if operator is ConstraintOperator.BETWEEN else parsed_values[0]
        )
        return Constraint(
            field=clause.field_id,
            operator=operator,
            value=value,
            unit=Unit(definition.unit),
            strength=ConstraintStrength.LOCKED,
        )

    @staticmethod
    def _preparation_binding(
        *,
        request_id: str,
        question_sha256: str,
        family: ProductFamily,
        interaction_intent: InteractionIntent,
        residual_text: str,
        residual_start: int,
        residual_end: int,
        operation: ResolutionOperation,
        expected_direction: SortDirection | None,
        hard_filter_lock: HardFilterLock,
    ) -> str:
        return canonical_sha256(
            {
                "request_id": request_id,
                "question_sha256": question_sha256,
                "product_family": family.value,
                "interaction_intent": interaction_intent.value,
                "residual_sha256": hashlib.sha256(residual_text.encode("utf-8")).hexdigest(),
                "residual_start": residual_start,
                "residual_end": residual_end,
                "operation": operation.value,
                "expected_direction": (
                    None if expected_direction is None else expected_direction.value
                ),
                "hard_filter_lock_sha256": hard_filter_lock.payload_sha256,
            }
        )

    @staticmethod
    def _residual_operation(question: str, start: int, end: int) -> ResolutionOperation:
        surface = question[max(0, start - 8) : min(len(question), end + 32)]
        # A literal ordering cue owned by the residual takes precedence over an
        # unrelated numeric filter in the preceding clause (for example,
        # ``총보수 0.5% 이하이면서 ... 큰``).
        if _expected_direction(question, start, end) is not None:
            return ResolutionOperation.RANK
        has_operator = any(pattern.search(surface) for pattern in _FRAME_OPERATOR_CUES.values())
        if has_operator and _FRAME_NUMBER.search(surface) is not None:
            return ResolutionOperation.FILTER
        return ResolutionOperation.RANK

    def prepare(
        self,
        trace: PlanningTrace,
    ) -> AdaptiveSemanticPreparation | AdaptiveSemanticOutcome:
        """Freeze server semantics before HCX and KURE execute.

        A CLARIFY caused by exactly one schema coverage gap is eligible because
        this method grants no execution authority.  Safety/UNSUPPORTED routes,
        multi-family questions, Fast Paths, and ambiguous residual sets stop
        here and therefore make zero Dense calls.
        """

        route = trace.route_decision
        planning = trace.planning_decision
        ledger = trace.semantic_ledger
        schema_path = (
            route.disposition is RouteDisposition.EXECUTE
            and planning.path is PlanningPath.SCHEMA_LINK_SHADOW
        )
        promotable_gap = (
            route.disposition is RouteDisposition.CLARIFY
            and route.reason_code == "semantic_coverage_incomplete"
            and planning.semantic_issue.value == "true_ambiguity"
        )
        if (
            not (schema_path or promotable_gap)
            or ledger is None
            or len(route.draft.product_families) != 1
            or not ledger.residual_spans
        ):
            return _control("clarify", "semantic_path_not_eligible")
        if len(ledger.residual_spans) > self.schema_linker.policy.maximum_residual_spans:
            return _control("clarify", "too_many_residual_spans")
        if len(ledger.residual_spans) != 1:
            return _control("clarify", "multiple_residual_spans_not_activated")

        residual = ledger.residual_spans[0]
        family = route.draft.product_families[0]
        operation = self._residual_operation(
            route.draft.question,
            residual.start,
            residual.end,
        )
        direction = _expected_direction(
            route.draft.question,
            residual.start,
            residual.end,
        )
        if operation is ResolutionOperation.RANK:
            if direction is None:
                return _control("clarify", "residual_direction_unresolved")
            if is_generic_semantic_ambiguity(
                residual.text,
                product_family=family,
                operation="rank",
            ):
                return _control("clarify", "generic_semantic_ambiguity")

        payload = canonicalize_query_plan_payload(
            route.draft.question,
            {
                "question_id": route.draft.request_id,
                "product_families": [family.value],
            },
            force_product_family_hint=True,
        )
        residual_hashes = {item.sha256 for item in ledger.residual_spans}

        def ambiguity_is_owned_by_residual(item: dict[str, object]) -> bool:
            span = item.get("span")
            if not isinstance(span, str) or not span:
                return False
            if hashlib.sha256(span.encode("utf-8")).hexdigest() in residual_hashes:
                return True
            if operation is not ResolutionOperation.RANK or span not in residual.text:
                return False
            literal_direction = _expected_direction(span, 0, len(span))
            return literal_direction is not None and literal_direction == direction

        payload["ambiguities"] = [
            item for item in payload["ambiguities"] if not ambiguity_is_owned_by_residual(item)
        ]
        try:
            base_plan = QueryPlan.model_validate(payload)
        except ValueError:
            return _control("clarify", "hard_filter_plan_invalid")
        if base_plan.ambiguities or base_plan.unsupported_conditions:
            return _control("clarify", "hard_filter_plan_unresolved")
        if base_plan.ranking and operation is not ResolutionOperation.FILTER:
            return _control("clarify", "existing_ranking_conflicts_with_residual")
        hard_filter_lock = HardFilterLock.from_plan(
            base_plan,
            requested_limit=route.draft.requested_limit,
            product_mentions=tuple(route.draft.product_mentions),
        )
        question_sha256 = hashlib.sha256(route.draft.question.encode("utf-8")).hexdigest()
        binding_sha256 = self._preparation_binding(
            request_id=route.draft.request_id,
            question_sha256=question_sha256,
            family=family,
            interaction_intent=route.draft.intent,
            residual_text=residual.text,
            residual_start=residual.start,
            residual_end=residual.end,
            operation=operation,
            expected_direction=direction,
            hard_filter_lock=hard_filter_lock,
        )
        return AdaptiveSemanticPreparation(
            request_id=route.draft.request_id,
            question_sha256=question_sha256,
            product_family=family,
            interaction_intent=route.draft.intent,
            residual_text=residual.text,
            residual_start=residual.start,
            residual_end=residual.end,
            operation=operation,
            expected_direction=direction,
            base_plan=base_plan,
            hard_filter_lock=hard_filter_lock,
            binding_sha256=binding_sha256,
        )

    def _probe(self, preparation: AdaptiveSemanticPreparation) -> _AdaptiveSemanticProbe:
        """Run one candidate-only KURE query over the server residual verbatim."""

        linked = None
        failure_reason: str | None = None
        try:
            linked = self.schema_linker.link(
                residual_span=preparation.residual_text,
                product_family=preparation.product_family,
                interaction_intent=preparation.interaction_intent,
                operation=preparation.operation,
            )
        except AdaptiveSchemaLinkUnavailable:
            failure_reason = "schema_dense_capacity_unavailable"
        except RequestDeadlineExceeded:
            raise
        except Exception:  # noqa: BLE001 - advisory Dense cannot widen authority
            failure_reason = "schema_dense_failed"
        return _AdaptiveSemanticProbe(
            preparation_sha256=preparation.binding_sha256,
            linked=linked,
            failure_reason=failure_reason,
            index_manifest_sha256=canonical_sha256(
                self.schema_linker.index.manifest.model_dump(mode="json")
            ),
            model_revision_sha256=hashlib.sha256(
                self.schema_linker.index.manifest.provider.model_revision.encode("utf-8")
            ).hexdigest(),
            model_snapshot_manifest_sha256=(
                self.schema_linker.policy.snapshot_file_manifest_sha256
            ),
        )

    def _resolve_grounded_hclx_frame(
        self,
        trace: PlanningTrace,
        *,
        preparation: AdaptiveSemanticPreparation,
        intent_draft: IntentAnalysisDraft | None,
    ) -> AdaptiveSemanticOutcome | None:
        """Grant authority to one fully server-grounded HCX frame without Dense."""

        route = trace.route_decision
        ledger = trace.semantic_ledger
        if (
            route.disposition is not RouteDisposition.EXECUTE
            or trace.planning_decision.path is not PlanningPath.SCHEMA_LINK_SHADOW
            or ledger is None
            or len(route.draft.product_families) != 1
            or len(ledger.residual_spans) != 1
        ):
            return None
        residual = ledger.residual_spans[0]
        family = route.draft.product_families[0]
        current_binding = self._preparation_binding(
            request_id=route.draft.request_id,
            question_sha256=hashlib.sha256(route.draft.question.encode("utf-8")).hexdigest(),
            family=family,
            interaction_intent=route.draft.intent,
            residual_text=residual.text,
            residual_start=residual.start,
            residual_end=residual.end,
            operation=preparation.operation,
            expected_direction=preparation.expected_direction,
            hard_filter_lock=preparation.hard_filter_lock,
        )
        if current_binding != preparation.binding_sha256:
            return _control("clarify", "semantic_preparation_binding_mismatch")
        frame_clause = self._matching_hclx_clause(
            intent_draft,
            residual_text=residual.text,
            family=family,
        )
        if frame_clause is None:
            return None
        try:
            frame_operation = ResolutionOperation(frame_clause.operation.value)
        except ValueError:
            return None
        if frame_operation is not preparation.operation:
            return None
        if not self.execution_enabled:
            return _control(
                "clarify",
                "semantic_resolution_shadow_only",
                hclx_frame_used=True,
            )
        try:
            semantic_constraint = (
                self._server_numeric_constraint(
                    frame_clause,
                    family=family,
                    base_plan=preparation.base_plan,
                )
                if frame_operation is ResolutionOperation.FILTER
                else None
            )
            if frame_operation is ResolutionOperation.RANK:
                expected_direction = preparation.expected_direction
                if (
                    expected_direction is None
                    or frame_clause.direction is SemanticFrameDirection.NONE
                ):
                    return None
                frame_direction = SortDirection(frame_clause.direction.value)
                if frame_direction is not expected_direction:
                    return None
            else:
                frame_direction = None
        except (SemanticResolutionError, ValueError):
            return None
        if (
            not frame_clause.field_span
            or frame_clause.field_span not in frame_clause.text_span
            or route.draft.question.count(frame_clause.text_span) != 1
        ):
            return None
        request = SemanticResolutionRequest(
            request_id=route.draft.request_id,
            residual_span=residual.text,
            context_before="",
            context_after="",
            product_family=family,
            interaction_intent=route.draft.intent,
            allowed_operations=(frame_operation,),
            candidates=(SchemaFieldCandidate(field_id=frame_clause.field_id, rank=1),),
            expected_direction=(
                preparation.expected_direction
                if frame_operation is ResolutionOperation.RANK
                else None
            ),
            hard_filter_lock_sha256=preparation.hard_filter_lock.payload_sha256,
        )
        draft = SemanticResolutionDraft(
            decision=ResolutionDecision.RESOLVE,
            selected_field_id=frame_clause.field_id,
            operation=frame_operation,
            direction=frame_direction,
            reason_code="candidate_context_match",
            evidence_spans=(residual.text,),
        )
        try:
            receipt = self.gate.admit_hclx_frame(
                draft,
                request,
                constraint=semantic_constraint,
            )
        except (SemanticResolutionError, ValueError):
            return None
        return AdaptiveSemanticOutcome(
            status="resolved",
            reason_code=(
                "hclx_filter_server_grounded"
                if frame_operation is ResolutionOperation.FILTER
                else "hclx_server_grounded"
            ),
            hard_filter_lock=preparation.hard_filter_lock,
            receipt=receipt,
            semantic_constraints=(() if semantic_constraint is None else (semantic_constraint,)),
            planning_decision=self.planning_policy.admit(receipt),
            hclx_frame_used=True,
        )

    def _resolve_dense_fallback(
        self,
        trace: PlanningTrace,
        *,
        preparation: AdaptiveSemanticPreparation,
        probe: _AdaptiveSemanticProbe,
    ) -> AdaptiveSemanticOutcome:
        """Try one bounded KURE fallback after HCX did not acquire authority."""

        route = trace.route_decision
        ledger = trace.semantic_ledger
        if (
            route.disposition is not RouteDisposition.EXECUTE
            or trace.planning_decision.path is not PlanningPath.SCHEMA_LINK_SHADOW
            or ledger is None
            or len(route.draft.product_families) != 1
            or len(ledger.residual_spans) != 1
        ):
            return _control("clarify", "semantic_path_not_eligible")
        residual = ledger.residual_spans[0]
        family = route.draft.product_families[0]
        current_binding = self._preparation_binding(
            request_id=route.draft.request_id,
            question_sha256=hashlib.sha256(route.draft.question.encode("utf-8")).hexdigest(),
            family=family,
            interaction_intent=route.draft.intent,
            residual_text=residual.text,
            residual_start=residual.start,
            residual_end=residual.end,
            operation=preparation.operation,
            expected_direction=preparation.expected_direction,
            hard_filter_lock=preparation.hard_filter_lock,
        )
        if (
            current_binding != preparation.binding_sha256
            or probe.preparation_sha256 != preparation.binding_sha256
        ):
            return _control("clarify", "semantic_preparation_binding_mismatch")
        direction = preparation.expected_direction
        hard_filter_lock = preparation.hard_filter_lock
        dense_observation = {
            "dense_attempted": True,
            "candidate_count": probe.candidate_count,
            "index_manifest_sha256": probe.index_manifest_sha256,
            "model_revision_sha256": probe.model_revision_sha256,
            "model_snapshot_manifest_sha256": probe.model_snapshot_manifest_sha256,
        }
        linked = probe.linked
        dense_failure_reason = probe.failure_reason
        if dense_failure_reason is not None:
            return _control(
                "clarify",
                dense_failure_reason,
                **dense_observation,
            )

        # HCX either abstained or omitted a binding for the one unresolved
        # field. Only now is Schema Dense allowed to propose bounded candidates.
        if direction is None:
            return _control("clarify", "residual_direction_unresolved")
        if is_generic_semantic_ambiguity(
            residual.text,
            product_family=family,
            operation="rank",
        ):
            return _control("clarify", "generic_semantic_ambiguity")
        if linked is None:
            return _control(
                "clarify",
                dense_failure_reason or "schema_dense_no_candidates",
                **dense_observation,
            )
        observation = dense_observation
        if not linked.candidates:
            return _control("clarify", linked.reason_code, **observation)
        if not self.execution_enabled:
            return _control("clarify", "semantic_resolution_shadow_only", **observation)
        request = SemanticResolutionRequest(
            request_id=route.draft.request_id,
            residual_span=residual.text,
            context_before=route.draft.question[max(0, residual.start - 80) : residual.start],
            context_after=route.draft.question[residual.end : residual.end + 80],
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
        supported: list[tuple[SchemaFieldCandidate, tuple[str, ...]]] = []
        for candidate in request.candidates:
            if candidate.dense_score is None:
                continue
            threshold = self.schema_linker.policy.threshold_for(
                product_family=family,
                field_id=candidate.field_id,
                operation="rank",
            )
            if candidate.dense_score < threshold.hclx_candidate_min_score:
                continue
            evidence = semantic_field_evidence(
                request.evidence_context,
                field_id=candidate.field_id,
                product_family=family,
                operation="rank",
            )
            if evidence.supported:
                supported.append((candidate, evidence.evidence_spans))

        # Dense is a fallback candidate generator only. The server's versioned
        # semantic rules grant authority when exactly one candidate is grounded
        # and the family/field/operation tuple has an explicit calibrated policy.
        # It never triggers another HCX call after the primary Semantic Frame.
        calibrated_single = len(
            supported
        ) == 1 and self.schema_linker.policy.has_explicit_threshold(
            product_family=family,
            field_id=supported[0][0].field_id,
            operation="rank",
        )
        if calibrated_single:
            candidate, evidence_spans = supported[0]
            draft = SemanticResolutionDraft(
                decision=ResolutionDecision.RESOLVE,
                selected_field_id=candidate.field_id,
                operation=ResolutionOperation.RANK,
                direction=direction,
                reason_code="candidate_context_match",
                evidence_spans=evidence_spans,
            )
            source = SpanSource.SCHEMA_DENSE
        else:
            return _control("clarify", "semantic_field_evidence_missing", **observation)
        try:
            receipt = self.gate.admit(draft, request, source=source)
        except SemanticResolutionError:
            return _control("clarify", "semantic_resolution_rejected", **observation)
        return AdaptiveSemanticOutcome(
            status="resolved",
            reason_code="schema_dense_resolution_admitted",
            hard_filter_lock=hard_filter_lock,
            receipt=receipt,
            planning_decision=self.planning_policy.admit(receipt),
            **observation,
        )

    def resolve(
        self,
        trace: PlanningTrace,
        *,
        intent_draft: IntentAnalysisDraft | None = None,
        preparation: AdaptiveSemanticPreparation | None = None,
    ) -> AdaptiveSemanticOutcome:
        """Use one HCX frame first, then one conditional KURE fallback."""

        if preparation is None:
            prepared = self.prepare(trace)
            if isinstance(prepared, AdaptiveSemanticOutcome):
                return prepared
            preparation = prepared
        primary = self._resolve_grounded_hclx_frame(
            trace,
            preparation=preparation,
            intent_draft=intent_draft,
        )
        if primary is not None:
            return primary
        if preparation.operation is ResolutionOperation.FILTER:
            # KURE links text to a field; it cannot independently prove the
            # literal number, operator, unit and currency that make a numeric
            # filter executable.  Only a fully grounded HCX frame followed by
            # the server re-parser may recover this shape.  Avoid a useless
            # embedding call when that frame is absent or rejected.
            return _control("clarify", "hclx_filter_frame_unresolved")
        probe = self._probe(preparation)
        assert probe is not None
        return self._resolve_dense_fallback(
            trace,
            preparation=preparation,
            probe=probe,
        )


def promote_hclx_semantic_gap_trace(
    decision: RouteDecision,
    trace: PlanningTrace,
    intent_draft: IntentAnalysisDraft | None,
) -> PlanningTrace:
    """Reopen only one server-observed schema gap backed by a closed HCX frame.

    Safety and UNSUPPORTED decisions are intentionally ineligible.  The
    promotion carries no SQL authority by itself: it only enters the existing
    schema-link path, where the frame, Registry, hard-filter lock and Compiler
    are independently revalidated before execution.
    """

    if (
        intent_draft is None
        or intent_draft.intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
        or intent_draft.needs_clarification
        or len(decision.draft.product_families) != 1
    ):
        return trace
    family = decision.draft.product_families[0]
    if family not in intent_draft.product_families:
        return trace
    clarify_gap = (
        decision.disposition is RouteDisposition.CLARIFY
        and decision.reason_code == "semantic_coverage_incomplete"
        and trace.planning_decision.semantic_issue.value == "true_ambiguity"
        and len(trace.planning_decision.unresolved_spans) == 1
    )
    deterministic_gap = (
        decision.disposition is RouteDisposition.EXECUTE
        and decision.draft.intent is InteractionIntent.SEARCH
        and trace.planning_decision.path is PlanningPath.DETERMINISTIC_FAST
    )
    if not (clarify_gap or deterministic_gap):
        return trace
    if deterministic_gap:
        # A complete deterministic plan already proves every executable
        # filter and ranking without model authority. HCX still participates
        # in mandatory Intent analysis, but a differently segmented or
        # mistaken frame must not reopen that plan and create a false
        # CLARIFY. Reopen only when the ordinary compiler itself detects a
        # missing executable meaning.
        from finance_agent_core.agent.compiler import (
            PlanCompilationBlockedError,
            ServerQueryPlanCompiler,
        )

        try:
            ServerQueryPlanCompiler({}).compile(decision)
        except (KeyError, OSError, PlanCompilationBlockedError, ValueError):
            pass
        else:
            return trace

    clause: IntentExecutionClause | None = None
    if clarify_gap:
        residual_text = trace.planning_decision.unresolved_spans[0]
        clause = AdaptiveSemanticResolver._matching_hclx_clause(
            intent_draft,
            residual_text=residual_text,
            family=family,
        )
    else:
        aliases = typed_alias_matches(decision.draft.question, family)
        try:
            deterministic_plan = QueryPlan.model_validate(
                canonicalize_query_plan_payload(
                    decision.draft.question,
                    {
                        "question_id": decision.draft.request_id,
                        "product_families": [family.value],
                    },
                    force_product_family_hint=True,
                )
            )
        except (ValueError, TypeError):
            # This plan is used only to detect fields already owned by the
            # deterministic parser.  Failure grants no authority and simply
            # leaves the HCX clause subject to the ordinary admission path.
            deterministic_plan = None
        unresolved_frames: list[IntentExecutionClause] = []
        for candidate in intent_draft.execution_clauses:
            if (
                candidate.semantic_status is not SemanticFrameStatus.RESOLVED
                or not semantic_frame_clause_is_schema_admissible(intent_draft, candidate)
                or candidate.operation
                not in {SemanticFrameOperation.RANK, SemanticFrameOperation.FILTER}
                or candidate.field_id == "__none__"
                or not candidate.field_span
                or family not in set(candidate.product_families or intent_draft.product_families)
            ):
                continue
            start = decision.draft.question.find(candidate.field_span)
            if start < 0 or decision.draft.question.find(candidate.field_span, start + 1) >= 0:
                continue
            end = start + len(candidate.field_span)
            owners = [item for item in aliases if item.start < end and start < item.end]
            if owners:
                # Lexical/Ontology owns known surfaces. A conflicting HCX field
                # is advisory only and can never replace the deterministic lock.
                if any(
                    item.field_id != candidate.field_id
                    or item.operation != candidate.operation.value
                    for item in owners
                ):
                    return trace
            already_owned = False
            if deterministic_plan is not None:
                if candidate.operation is SemanticFrameOperation.RANK:
                    already_owned = any(
                        item.field == candidate.field_id
                        and item.direction.value == candidate.direction.value
                        for item in deterministic_plan.ranking
                    )
                elif candidate.operation is SemanticFrameOperation.FILTER:
                    try:
                        frame_constraint = AdaptiveSemanticResolver._server_numeric_constraint(
                            candidate,
                            family=family,
                            base_plan=deterministic_plan,
                        )
                    except (SemanticResolutionError, ValueError):
                        frame_constraint = None
                    already_owned = frame_constraint is not None and any(
                        item.model_dump(mode="json") == frame_constraint.model_dump(mode="json")
                        for item in deterministic_plan.constraints
                    )
            if already_owned:
                continue
            unresolved_frames.append(candidate)
        if len(unresolved_frames) != 1:
            return trace
        clause = unresolved_frames[0]
        residual_text = clause.text_span
    if clause is None:
        return trace
    operation = ResolutionOperation(clause.operation.value)
    if is_generic_semantic_ambiguity(
        residual_text,
        product_family=family,
        operation=operation.value,
    ):
        return trace
    if operation is ResolutionOperation.RANK:
        expected = _expected_direction(
            decision.draft.question,
            decision.draft.question.find(residual_text),
            decision.draft.question.find(residual_text) + len(residual_text),
        )
        if expected is None or clause.direction.value != expected.value:
            return trace
    elif operation is ResolutionOperation.FILTER:
        try:
            AdaptiveSemanticResolver._server_numeric_constraint(clause, family=family)
        except (SemanticResolutionError, ValueError):
            return trace
    else:
        return trace

    if clarify_gap:
        promoted_draft = MinimalQueryDraft.model_validate(
            {
                **decision.draft.model_dump(mode="python"),
                "intent": InteractionIntent.SEARCH,
            }
        )
        promoted_route = RouteDecision(
            draft=promoted_draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="hclx_semantic_frame_candidate",
            reason="HCX 의미 프레임을 서버 검증 경로에서 확인 중",
            query_plan_intent=Intent.SEARCH,
            capability_matrix_version=decision.capability_matrix_version,
        )
    else:
        promoted_route = RouteDecision.model_validate_json(decision.model_dump_json())
        promoted_draft = promoted_route.draft
    coverage = SemanticCoverageDecision(schema_link_gap_spans=(residual_text,))
    planning = AdaptiveShadowPlanningPolicy().decide(promoted_route, coverage)
    ledger = build_resolved_span_ledger(
        question=promoted_draft.question,
        interaction_intent=InteractionIntent.SEARCH,
        product_families=(family,),
        coverage=coverage,
    )
    return PlanningTrace(
        route_decision=promoted_route,
        planning_decision=planning,
        semantic_ledger=ledger,
    )


__all__ = [
    "AdaptiveSemanticOutcome",
    "AdaptiveSemanticPreparation",
    "AdaptiveSemanticResolver",
    "promote_hclx_semantic_gap_trace",
]
