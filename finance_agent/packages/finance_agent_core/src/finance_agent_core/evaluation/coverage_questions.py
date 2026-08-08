from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

from pydantic import Field, model_validator

from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    ProductFamily,
    QueryPlan,
)
from finance_agent_core.evaluation.coverage_plan import (
    CoverageCell,
    CoverageModel,
    CoveragePlanCase,
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.metamorphic import (
    SEMANTIC_ROUNDTRIP_AXES,
    GeneratedMutation,
    MutationAxis,
    MutationValidation,
)
from finance_agent_core.evaluation.semantic_roundtrip import (
    SemanticPlanSpec,
    build_semantic_plan_spec,
    validate_semantic_question,
)
from finance_agent_core.evaluation.semantics import canonical_json_sha256

_QUESTION_PROTOCOL_ID = "coverage-guided-question-v1"
_SCREEN_VERSION = "coverage-question-screen-v1"
_NEGATION = re.compile(
    r"제외|아니|아닌|아님|않|불가|미포함|빼고|말고",
    re.IGNORECASE,
)
_CONTAINS = re.compile(r"포함|들어간|들어\s*있는|함유", re.IGNORECASE)
_BETWEEN = re.compile(r"사이|범위|부터.{0,30}까지|이상.{0,30}이하", re.IGNORECASE)
_IN_CONNECTOR = re.compile(r"또는|혹은|중|이나|거나|,", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?")
_SPECIAL_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "product_type": re.compile(r"ETF|ETN|ETP", re.IGNORECASE),
    "sellable": re.compile(r"(?:판매|거래).{0,12}(?:가능|불가|아니|않)", re.IGNORECASE),
    "trading_suspended": re.compile(r"거래.{0,12}(?:중지|정지|중단|가능)", re.IGNORECASE),
    "pension_eligible": re.compile(r"연금", re.IGNORECASE),
    "currently_buyable": re.compile(r"(?:매수|구매).{0,12}(?:가능|불가|아니)", re.IGNORECASE),
    "investment_region": re.compile(r"투자\s*지역|미국|국가", re.IGNORECASE),
    "asset_type": re.compile(r"주식\s*형?|채권\s*형?|자산", re.IGNORECASE),
    "total_expense_ratio_pct": re.compile(r"총\s*보수(?:\s*율)?|보수\s*율", re.IGNORECASE),
    "aum": re.compile(r"AUM|순자산|운용\s*자산", re.IGNORECASE),
    "trading_currency": re.compile(r"거래\s*통화|통화|KRW|USD", re.IGNORECASE),
    "risk_level": re.compile(r"위험\s*등급|위험도", re.IGNORECASE),
}


class CoverageQuestionProvider(Protocol):
    @property
    def provider_name(self) -> Literal["expected", "local_test"]: ...

    @property
    def model_name(self) -> str | None: ...

    def generate_questions(
        self,
        spec: SemanticPlanSpec,
        axes: Sequence[MutationAxis],
    ) -> list[GeneratedMutation]: ...


class CoverageQuestionCandidate(CoverageModel):
    id: str = Field(min_length=1, max_length=300)
    source_case_id: str
    cell: CoverageCell
    axis: MutationAxis
    question: str = Field(min_length=1, max_length=3000)
    hard_literals: list[str]
    validation: MutationValidation


class CoverageGenerationFailure(CoverageModel):
    source_case_id: str
    cell: CoverageCell
    error_type: str
    error_message: str


class CoverageQuestionBatch(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    protocol_id: Literal["coverage-guided-question-v1"] = _QUESTION_PROTOCOL_ID
    screen_version: Literal["coverage-question-screen-v1"] = _SCREEN_VERSION
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator: Literal["expected", "local_test"]
    model: str | None
    axes: list[MutationAxis]
    selected_source_count: int = Field(ge=1)
    requested_count: int = Field(ge=1)
    generated_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    generation_failure_count: int = Field(ge=0)
    candidates: list[CoverageQuestionCandidate]
    generation_failures: list[CoverageGenerationFailure]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_counts(self) -> CoverageQuestionBatch:
        if self.axes != list(SEMANTIC_ROUNDTRIP_AXES):
            raise ValueError("coverage question axes differ")
        if self.requested_count != self.selected_source_count * len(self.axes):
            raise ValueError("coverage requested question count differs")
        if self.generated_count != len(self.candidates):
            raise ValueError("coverage generated question count differs")
        if self.accepted_count + self.rejected_count != self.generated_count:
            raise ValueError("coverage accepted and rejected counts differ")
        if self.generation_failure_count != len(self.generation_failures):
            raise ValueError("coverage generation failure count differs")
        candidate_sources = {candidate.source_case_id for candidate in self.candidates}
        failure_sources = {failure.source_case_id for failure in self.generation_failures}
        if candidate_sources & failure_sources:
            raise ValueError("coverage source cannot have questions and a generation failure")
        if len(candidate_sources | failure_sources) != self.selected_source_count:
            raise ValueError("coverage selected source accounting differs")
        counts = Counter(candidate.source_case_id for candidate in self.candidates)
        if any(count != len(self.axes) for count in counts.values()):
            raise ValueError("successful coverage source must contain every question axis")
        if self.generated_count + self.generation_failure_count * len(self.axes) != (
            self.requested_count
        ):
            raise ValueError("coverage generated and failed source accounting differs")
        ids = [candidate.id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("coverage question IDs must be unique")
        return self


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _compact(value: str) -> str:
    return re.sub(r"[\s\"'`()\[\]{}]", "", _normalized(value))


def _field_present(question: str, field_name: str, family: ProductFamily) -> bool:
    if field_name in {"product_family", "public_offering"}:
        return True
    pattern = _SPECIAL_FIELD_PATTERNS.get(field_name)
    if pattern is not None and pattern.search(question) is not None:
        return True
    definition = load_field_registry().require_field(field_name, [family.value])
    terms = [definition.label, *definition.aliases]
    haystack = _compact(question)
    return any(len(compact := _compact(term)) >= 2 and compact in haystack for term in terms)


def _observed_numbers(question: str) -> list[Decimal]:
    observed: list[Decimal] = []
    for match in _NUMBER.findall(unicodedata.normalize("NFKC", question)):
        try:
            observed.append(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return observed


def _numeric_value_present(question: str, value: int | float) -> bool:
    expected = Decimal(str(value))
    return any(actual == expected for actual in _observed_numbers(question))


def _boolean_value_present(question: str, constraint: Constraint) -> bool:
    assert isinstance(constraint.value, bool)
    normalized = _normalized(question)
    positive_patterns = {
        "sellable": re.compile(r"판매.{0,8}가능|거래.{0,8}가능"),
        "trading_suspended": re.compile(r"거래.{0,8}(?:중지|정지|중단)"),
        "pension_eligible": re.compile(r"연금.{0,8}(?:가능|대상)"),
        "core_etf": re.compile(r"핵심.{0,4}ETF", re.IGNORECASE),
        "currently_buyable": re.compile(r"(?:매수|구매).{0,8}가능"),
        "public_offering": re.compile(r"공모.{0,12}펀드"),
        "company_sellable": re.compile(r"(?:당사|회사).{0,8}판매.{0,8}가능"),
        "currency_hedged": re.compile(r"환\s*헤지|hedg", re.IGNORECASE),
    }
    negative_patterns = {
        "sellable": re.compile(r"판매.{0,8}불가|거래.{0,8}불가|판매.{0,8}아니"),
        "trading_suspended": re.compile(
            r"거래.{0,8}(?:중지|정지|중단).{0,8}(?:아니|아닌|아님|않)|"
            r"거래.{0,8}가능"
        ),
        "pension_eligible": re.compile(r"연금.{0,8}(?:불가|제외|아니)"),
        "core_etf": re.compile(r"핵심.{0,4}ETF.{0,8}(?:아니|제외)"),
        "currently_buyable": re.compile(r"(?:매수|구매).{0,8}(?:불가|아니)"),
        "public_offering": re.compile(r"사모.{0,12}펀드"),
        "company_sellable": re.compile(r"(?:당사|회사).{0,8}판매.{0,8}불가"),
        "currency_hedged": re.compile(r"환\s*노출|비\s*헤지|헤지.{0,8}아니"),
    }
    expected_patterns = positive_patterns if constraint.value else negative_patterns
    pattern = expected_patterns.get(constraint.field)
    if pattern is None:
        return True
    if pattern.search(normalized) is None:
        return False
    if constraint.value:
        opposite = negative_patterns.get(constraint.field)
        if opposite is not None and opposite.search(normalized) is not None:
            return False
    return True


def _rendered_values_for_constraint(
    plan: QueryPlan,
    spec: SemanticPlanSpec,
    index: int,
) -> list[str]:
    if index >= len(spec.conditions):
        raise ValueError("semantic condition count differs from QueryPlan")
    rendered = spec.conditions[index].value
    return rendered if isinstance(rendered, list) else [rendered]


def _constraint_values_present(
    question: str,
    plan: QueryPlan,
    spec: SemanticPlanSpec,
) -> bool:
    normalized = _compact(question)
    for index, constraint in enumerate(plan.constraints):
        if isinstance(constraint.value, bool):
            if not _boolean_value_present(question, constraint):
                return False
            continue
        values = constraint.value if isinstance(constraint.value, list) else [constraint.value]
        rendered = _rendered_values_for_constraint(plan, spec, index)
        definition = load_field_registry().require_field(
            constraint.field,
            [plan.product_families[0].value],
        )
        for raw, display in zip(values, rendered, strict=True):
            if definition.value_type is ValueType.NUMBER:
                if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                    return False
                if not _numeric_value_present(question, raw):
                    return False
            elif definition.value_type is ValueType.DATE:
                if _compact(str(raw)) not in normalized:
                    return False
            elif _compact(display) not in normalized:
                return False
    return True


def _operators_present(question: str, plan: QueryPlan) -> bool:
    for constraint in plan.constraints:
        operator = constraint.operator
        if operator in {ConstraintOperator.NEQ, ConstraintOperator.NOT_IN}:
            if _NEGATION.search(question) is None:
                return False
        elif operator is ConstraintOperator.IN:
            if _IN_CONNECTOR.search(question) is None:
                return False
        elif operator is ConstraintOperator.BETWEEN:
            if _BETWEEN.search(question) is None:
                return False
        elif operator is ConstraintOperator.CONTAINS:
            if _CONTAINS.search(question) is None:
                return False
    return True


def _all_fields_present(question: str, plan: QueryPlan) -> bool:
    family = plan.product_families[0]
    fields = {
        *(constraint.field for constraint in plan.constraints),
        *(ranking.field for ranking in plan.ranking),
        *plan.intent_payload.comparison_fields,
        *plan.intent_payload.group_by,
        *(aggregation.field for aggregation in plan.intent_payload.aggregations),
    }
    fields.discard("product_id")
    if any(constraint.field == "product_id" for constraint in plan.constraints):
        if re.search(r"상품\s*(?:ID|아이디|식별자)|종목\s*코드", question, re.IGNORECASE) is None:
            return False
    return all(_field_present(question, field_name, family) for field_name in fields)


def _hard_literals(plan: QueryPlan, spec: SemanticPlanSpec) -> list[str]:
    literals: list[str] = []
    for index, constraint in enumerate(plan.constraints):
        if isinstance(constraint.value, bool):
            continue
        literals.extend(_rendered_values_for_constraint(plan, spec, index))
    if plan.intent.value == "search":
        literals.append(str(plan.limit))
    return list(dict.fromkeys(literals))


def validate_coverage_question(
    question: str,
    case: CoveragePlanCase,
    spec: SemanticPlanSpec,
    *,
    source_questions: Sequence[str],
    sibling_questions: Sequence[str] = (),
) -> MutationValidation:
    base = validate_semantic_question(
        question,
        spec,
        source_questions=source_questions,
        sibling_questions=sibling_questions,
    )
    checks = {
        **base.checks,
        "all_registry_fields_present": _all_fields_present(question, case.plan),
        "all_constraint_values_present": _constraint_values_present(
            question,
            case.plan,
            spec,
        ),
        "all_constraint_operators_present": _operators_present(question, case.plan),
    }
    violations = [name for name, passed in checks.items() if not passed]
    return MutationValidation(checks=checks, violations=violations, passed=not violations)


def _generate_one(
    provider: CoverageQuestionProvider,
    case: CoveragePlanCase,
) -> tuple[CoveragePlanCase, SemanticPlanSpec, list[GeneratedMutation] | Exception]:
    spec = build_semantic_plan_spec(case.plan)
    try:
        generated = provider.generate_questions(spec, SEMANTIC_ROUNDTRIP_AXES)
    except Exception as error:
        return case, spec, error
    return case, spec, generated


def _select_cases(
    suite: CoveragePlanSuite,
    *,
    families: set[ProductFamily] | None,
    kinds: set[str] | None,
    offset: int,
    limit: int | None,
) -> list[CoveragePlanCase]:
    cases = [
        case
        for case in suite.cases
        if (families is None or case.cell.product_family in families)
        and (kinds is None or case.cell.kind.value in kinds)
    ]
    if offset < 0:
        raise ValueError("coverage question offset cannot be negative")
    if limit is not None and limit < 1:
        raise ValueError("coverage question limit must be positive")
    return cases[offset:] if limit is None else cases[offset : offset + limit]


def generate_coverage_question_batch(
    provider: CoverageQuestionProvider,
    suite: CoveragePlanSuite,
    *,
    families: set[ProductFamily] | None = None,
    kinds: set[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
    workers: int = 1,
    generated_at_utc: str | None = None,
) -> CoverageQuestionBatch:
    if workers < 1:
        raise ValueError("coverage question workers must be positive")
    selected = _select_cases(
        suite,
        families=families,
        kinds=kinds,
        offset=offset,
        limit=limit,
    )
    if not selected:
        raise ValueError("coverage question selection is empty")
    generated_by_id: dict[
        str,
        tuple[CoveragePlanCase, SemanticPlanSpec, list[GeneratedMutation] | Exception],
    ] = {}
    if workers == 1:
        for case in selected:
            generated_by_id[case.id] = _generate_one(provider, case)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_generate_one, provider, case): case.id for case in selected}
            for future in as_completed(futures):
                generated_by_id[futures[future]] = future.result()

    source_questions = [case.canonical_question for case in suite.cases]
    candidates: list[CoverageQuestionCandidate] = []
    failures: list[CoverageGenerationFailure] = []
    for case in selected:
        _, spec, generated_or_error = generated_by_id[case.id]
        if isinstance(generated_or_error, Exception):
            failures.append(
                CoverageGenerationFailure(
                    source_case_id=case.id,
                    cell=case.cell,
                    error_type=type(generated_or_error).__name__,
                    error_message=str(generated_or_error),
                )
            )
            continue
        observed_axes = Counter(item.axis for item in generated_or_error)
        if observed_axes != Counter(SEMANTIC_ROUNDTRIP_AXES):
            failures.append(
                CoverageGenerationFailure(
                    source_case_id=case.id,
                    cell=case.cell,
                    error_type="AxisMismatch",
                    error_message=f"generated axes differ: {observed_axes}",
                )
            )
            continue
        siblings: list[str] = []
        for item in generated_or_error:
            validation = validate_coverage_question(
                item.question,
                case,
                spec,
                source_questions=source_questions,
                sibling_questions=siblings,
            )
            siblings.append(item.question)
            candidates.append(
                CoverageQuestionCandidate(
                    id=f"{_QUESTION_PROTOCOL_ID}-{case.id.rsplit('-', 1)[-1]}-{item.axis.value}",
                    source_case_id=case.id,
                    cell=case.cell,
                    axis=item.axis,
                    question=item.question,
                    hard_literals=_hard_literals(case.plan, spec),
                    validation=validation,
                )
            )
    accepted = sum(candidate.validation.passed for candidate in candidates)
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageQuestionBatch(
        batch_id=f"{_QUESTION_PROTOCOL_ID}-{provider.provider_name}",
        generated_at_utc=timestamp,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=coverage_plan_suite_semantic_sha256(suite),
        generator=provider.provider_name,
        model=provider.model_name,
        axes=list(SEMANTIC_ROUNDTRIP_AXES),
        selected_source_count=len(selected),
        requested_count=len(selected) * len(SEMANTIC_ROUNDTRIP_AXES),
        generated_count=len(candidates),
        accepted_count=accepted,
        rejected_count=len(candidates) - accepted,
        generation_failure_count=len(failures),
        candidates=candidates,
        generation_failures=failures,
        interpretation_limits=[
            *suite.interpretation_limits,
            "Qwen은 canonical 원문이 아니라 QueryPlan 의미 명세만 보고 질문을 생성한다.",
            "기계 검사는 필드·값·연산자·정렬·개수·의도 보존을 확인한다.",
            "기계 검사 통과는 자연스러움이나 금융 의미의 사람 검수를 대체하지 않는다.",
            "거절 질문도 삭제하지 않고 위반 항목과 함께 보존한다.",
        ],
    )


def coverage_question_batch_semantic_sha256(batch: CoverageQuestionBatch) -> str:
    payload = batch.model_dump(mode="json")
    payload.pop("generated_at_utc", None)
    return canonical_json_sha256(payload)


def merge_coverage_question_batches(
    batches: Sequence[CoverageQuestionBatch],
    *,
    generated_at_utc: str | None = None,
) -> CoverageQuestionBatch:
    if not batches:
        raise ValueError("at least one coverage question batch is required")
    first = batches[0]
    invariant_fields = (
        "schema_version",
        "status",
        "protocol_id",
        "screen_version",
        "plan_suite_id",
        "plan_suite_semantic_sha256",
        "generator",
        "model",
        "axes",
    )
    for batch in batches[1:]:
        for field_name in invariant_fields:
            if getattr(batch, field_name) != getattr(first, field_name):
                raise ValueError(f"coverage batch {field_name} differs")

    occupied_sources: set[str] = set()
    candidates: list[CoverageQuestionCandidate] = []
    failures: list[CoverageGenerationFailure] = []
    for batch in batches:
        batch_sources = {
            *(candidate.source_case_id for candidate in batch.candidates),
            *(failure.source_case_id for failure in batch.generation_failures),
        }
        overlap = occupied_sources & batch_sources
        if overlap:
            raise ValueError(f"coverage batches overlap source cases: {sorted(overlap)}")
        occupied_sources.update(batch_sources)
        candidates.extend(batch.candidates)
        failures.extend(batch.generation_failures)

    axis_order = {axis: index for index, axis in enumerate(first.axes)}
    candidates.sort(
        key=lambda candidate: (
            candidate.source_case_id,
            axis_order[candidate.axis],
        )
    )
    failures.sort(key=lambda failure: failure.source_case_id)
    limits = list(
        dict.fromkeys(
            item
            for batch in batches
            for item in batch.interpretation_limits
            if not item.startswith("merged coverage shards:")
        )
    )
    accepted = sum(candidate.validation.passed for candidate in candidates)
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageQuestionBatch(
        batch_id=f"{first.batch_id}-merged",
        generated_at_utc=timestamp,
        plan_suite_id=first.plan_suite_id,
        plan_suite_semantic_sha256=first.plan_suite_semantic_sha256,
        generator=first.generator,
        model=first.model,
        axes=first.axes,
        selected_source_count=len(occupied_sources),
        requested_count=len(occupied_sources) * len(first.axes),
        generated_count=len(candidates),
        accepted_count=accepted,
        rejected_count=len(candidates) - accepted,
        generation_failure_count=len(failures),
        candidates=candidates,
        generation_failures=failures,
        interpretation_limits=[*limits, f"merged coverage shards: {len(batches)}"],
    )
