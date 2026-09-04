from __future__ import annotations

from decimal import Decimal

from finance_agent_core.answering.models import (
    AnswerWarning,
    GroundedAnswerContext,
)
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    Intent,
    QueryPlan,
    Ranking,
    SortDirection,
)
from finance_agent_core.domain import ProductEvidence, VerifiedSearch
from finance_agent_core.execution.renderer import (
    WARNING_MESSAGES,
    render_verified_search,
    warning_codes_for_search,
)


def _format_scalar(value: object, unit: str) -> str:
    if isinstance(value, Decimal):
        rendered = format(value, "f").rstrip("0").rstrip(".")
        return rendered or "0"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, list):
        return " ~ ".join(_format_scalar(item, unit) for item in value)
    return str(value)


def _constraint_text(constraint: Constraint, product_family: str) -> str:
    registry = load_field_registry()
    definition = registry.require_field(constraint.field, [product_family])
    value = _format_scalar(constraint.value, definition.unit)
    suffix = "%" if definition.unit == "pct_point" else ""
    rendered_value = f"{value}{suffix}"
    if constraint.operator is ConstraintOperator.EQ:
        return f"{definition.label} = {rendered_value}"
    if constraint.operator is ConstraintOperator.NEQ:
        return f"{definition.label} ≠ {rendered_value}"
    if constraint.operator is ConstraintOperator.IN:
        return f"{definition.label}: {rendered_value} 중 하나"
    if constraint.operator is ConstraintOperator.NOT_IN:
        return f"{definition.label}: {rendered_value} 제외"
    if constraint.operator is ConstraintOperator.CONTAINS:
        return f"{definition.label}에 {rendered_value} 포함"
    operators = {
        ConstraintOperator.LT: "미만",
        ConstraintOperator.LTE: "이하",
        ConstraintOperator.GT: "초과",
        ConstraintOperator.GTE: "이상",
        ConstraintOperator.BETWEEN: "사이",
    }
    return f"{definition.label} {rendered_value} {operators[constraint.operator]}"


def _ranking_text(ranking: Ranking, product_family: str) -> str:
    registry = load_field_registry()
    definition = registry.require_field(ranking.field, [product_family])
    direction = "높은 순" if ranking.direction is SortDirection.DESC else "낮은 순"
    return f"{definition.label} {direction}"


def render_query_contract(plan: QueryPlan) -> str:
    product_family = plan.product_families[0].value
    lines: list[str] = []
    if plan.constraints:
        lines.append(
            "적용 조건: "
            + "; ".join(
                _constraint_text(constraint, product_family) for constraint in plan.constraints
            )
        )
    if plan.ranking:
        lines.append(
            "정렬 기준: "
            + ", ".join(_ranking_text(ranking, product_family) for ranking in plan.ranking)
        )
    if plan.intent is Intent.COMPARE:
        registry = load_field_registry()
        labels = [
            registry.require_field(field, [product_family]).label
            for field in plan.intent_payload.comparison_fields
        ]
        lines.append("비교 항목: " + ", ".join(labels))
        lines.append(f"비교 대상 수: {plan.limit}개")
    else:
        lines.append(f"최대 표시 개수: {plan.limit}개")
    return "\n".join(lines)


def build_grounded_answer_context(
    *,
    question: str,
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
) -> GroundedAnswerContext:
    rendered, _ = render_verified_search(plan, verified, products)
    deterministic_answer = f"{render_query_contract(plan)}\n{rendered}"
    warnings = [
        AnswerWarning(code=code, message=WARNING_MESSAGES[code])
        for code in warning_codes_for_search(plan, verified)
    ]
    return GroundedAnswerContext(
        question=question,
        query_plan=plan,
        candidate_count=verified.candidate_count,
        products=products,
        warnings=warnings,
        source_manifest=verified.manifest,
        deterministic_answer=deterministic_answer,
    )


def required_evidence_fields(context: GroundedAnswerContext) -> list[str]:
    if context.query_plan.intent is Intent.COMPARE:
        return context.query_plan.intent_payload.comparison_fields
    ranked = [ranking.field for ranking in context.query_plan.ranking]
    if ranked:
        return ranked
    return [constraint.field for constraint in context.query_plan.constraints]
