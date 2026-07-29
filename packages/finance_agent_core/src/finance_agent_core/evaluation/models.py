from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    Ambiguity,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    IntentPayload,
    QueryPlan,
    Ranking,
    UnsupportedCondition,
)

type EvaluationScalar = StrictBool | StrictInt | StrictFloat | StrictStr
type EvaluationValue = EvaluationScalar | list[EvaluationScalar]

CANONICAL_SEARCH_PROJECTION = [
    "product_id",
    "product_name",
    "ticker",
    "total_expense_ratio_pct",
    "aum",
    "trading_currency",
    "dynamic_as_of",
]


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvaluationSplit(StrEnum):
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class ExpectedDisposition(StrEnum):
    EXECUTE = "execute"
    BLOCK = "block"


class ExpectedBlocker(StrEnum):
    AMBIGUITY = "ambiguity"
    UNSUPPORTED = "unsupported"


class ExpectedConstraint(EvaluationModel):
    field: str
    operator: ConstraintOperator
    value: EvaluationValue
    strength: ConstraintStrength = ConstraintStrength.LOCKED

    def to_constraint(self, product_family: str = "overseas_etp") -> Constraint:
        definition = load_field_registry().require_field(self.field, [product_family])
        return Constraint.model_validate(
            {
                "field": self.field,
                "operator": self.operator,
                "value": self.value,
                "unit": definition.unit,
                "strength": self.strength,
            }
        )


class OracleExpectation(EvaluationModel):
    candidate_count: int = Field(ge=0)
    top_product_ids: list[str] = Field(max_length=100)


class EvaluationCase(EvaluationModel):
    id: str = Field(min_length=1, max_length=128)
    split: EvaluationSplit
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1000)
    constraints: list[ExpectedConstraint] = Field(max_length=20)
    ranking: list[Ranking] = Field(max_length=5)
    limit: int = Field(ge=1, le=100)
    disposition: ExpectedDisposition
    blocker: ExpectedBlocker | None = None
    oracle: OracleExpectation | None = None

    @model_validator(mode="after")
    def validate_expectation(self) -> EvaluationCase:
        if self.disposition is ExpectedDisposition.EXECUTE:
            if self.blocker is not None or self.oracle is None:
                raise ValueError("executable cases require oracle and no blocker")
            if len(self.oracle.top_product_ids) > self.limit:
                raise ValueError("oracle result cannot exceed the case limit")
            if len(self.oracle.top_product_ids) > self.oracle.candidate_count:
                raise ValueError("oracle result cannot exceed candidate_count")
        elif self.blocker is None or self.oracle is not None:
            raise ValueError("blocked cases require blocker and no oracle")
        return self

    def expected_plan(self, product_family: str = "overseas_etp") -> QueryPlan:
        ambiguities: list[Ambiguity] = []
        unsupported: list[UnsupportedCondition] = []
        if self.blocker is ExpectedBlocker.AMBIGUITY:
            ambiguities.append(
                Ambiguity(
                    span=self.question[:200],
                    reason="평가 세트가 명시한 사용자 확인 필요 조건",
                    options=["조건을 구체화한다"],
                )
            )
        elif self.blocker is ExpectedBlocker.UNSUPPORTED:
            unsupported.append(
                UnsupportedCondition(
                    span=self.question[:200],
                    reason=f"현재 동결된 {product_family} field registry에서 지원하지 않는 조건",
                )
            )
        return QueryPlan(
            schema_version="1.0",
            question_id=self.id,
            intent="search",
            product_families=[product_family],
            constraints=[item.to_constraint(product_family) for item in self.constraints],
            ranking=self.ranking,
            projection=SEARCH_PROJECTION_BY_FAMILY[product_family],
            limit=self.limit,
            intent_payload=IntentPayload(
                comparison_fields=[],
                group_by=[],
                aggregations=[],
                explain_product_ids=[],
            ),
            ambiguities=ambiguities,
            unsupported_conditions=unsupported,
        )


class EvaluationSuite(EvaluationModel):
    suite_id: Literal["overseas-etp-core-50", "domestic-etp-core-50", "bond-core-50"]
    suite_version: Literal["1.0"]
    dataset: Literal["overseas_etp", "domestic_etp", "bond"]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_core_suite(self) -> EvaluationSuite:
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case ids must be unique")
        if len(self.cases) != 50:
            raise ValueError("a core suite must contain exactly 50 cases")
        development = sum(case.split is EvaluationSplit.DEVELOPMENT for case in self.cases)
        holdout = sum(case.split is EvaluationSplit.HOLDOUT for case in self.cases)
        if (development, holdout) != (40, 10):
            raise ValueError("core suite must contain 40 development and 10 holdout cases")
        return self
