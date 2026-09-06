from __future__ import annotations

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentExecutionClause,
    SemanticFrameDirection,
    SemanticFrameOperation,
    SemanticFrameOperator,
    SemanticFrameStatus,
    semantic_frame_clause_is_schema_admissible,
)
from finance_agent_core.agent.routed_service import (
    _external_semantic_prefilter_questions,
    _scope_external_semantic_draft,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent


def _filter_draft(
    *,
    question_span: str,
    clause_families: list[ProductFamily],
    draft_families: list[ProductFamily],
) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=draft_families,
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="filter",
                text_span=question_span,
                product_families=clause_families,
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.FILTER,
                operator=SemanticFrameOperator.GTE,
                field_span="순자산",
                value_span="100억",
            )
        ],
    )


def _project(question: str, draft: IntentAnalysisDraft):
    families = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
    return _external_semantic_prefilter_questions(
        question,
        families,
        intent_draft=draft,
        ranking_field=None,
        requested_fields=(),
        requirements=(),
        identity_surfaces=(),
    )


def test_generic_multi_family_filter_cannot_be_narrowed_by_hclx_family() -> None:
    clause = "순자산이 100억 이상인"
    question = f"{clause} ETF 중 공식 편입 근거가 있는 상품"
    draft = _filter_draft(
        question_span=clause,
        clause_families=[ProductFamily.DOMESTIC_ETP],
        draft_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
    )

    assert _project(question, draft) is None


def test_generic_multi_family_filter_is_applied_to_every_requested_family() -> None:
    clause = "순자산이 100억 이상인"
    question = f"{clause} ETF 중 공식 편입 근거가 있는 상품"
    draft = _filter_draft(
        question_span=clause,
        clause_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        draft_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
    )

    projected = _project(question, draft)

    assert projected == {
        ProductFamily.DOMESTIC_ETP: "국내 ETF 순자산이 100억 이상인",
        ProductFamily.OVERSEAS_ETP: "해외 ETF 순자산이 100억 이상인",
    }


def test_schema_invalid_advisory_clause_is_removed_from_downstream_scope() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.FUND],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span="20거래일 변동성이 큰 순",
                product_families=[ProductFamily.FUND],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="twenty_day_volatility_pct",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="20거래일 변동성",
            )
        ],
    )

    scoped = _scope_external_semantic_draft(draft, ProductFamily.FUND)

    assert scoped.execution_clauses == []


def test_literal_direction_conflict_is_removed_from_downstream_scope() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span="순자산이 낮은 순",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="순자산",
            )
        ],
    )

    scoped = _scope_external_semantic_draft(draft, ProductFamily.DOMESTIC_ETP)

    assert scoped.execution_clauses == []


def test_quantity_maximum_does_not_conflict_with_ascending_rank_direction() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span="순자산이 낮은 순으로 최대 3개",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.ASC,
                field_span="순자산",
                limit_span="3개",
            )
        ],
    )

    scoped = _scope_external_semantic_draft(draft, ProductFamily.DOMESTIC_ETP)

    assert len(scoped.execution_clauses) == 1


def test_literal_operator_conflict_cannot_become_external_db_filter() -> None:
    clause = "순자산이 100억 이하인"
    question = f"{clause} ETF 중 공식 편입 근거가 있는 상품"
    draft = _filter_draft(
        question_span=clause,
        clause_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        draft_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
    )

    assert _project(question, draft) is None


def test_bare_number_cannot_authorize_an_ordered_filter() -> None:
    clause = "순자산이 100억인"
    question = f"{clause} ETF 중 공식 편입 근거가 있는 상품"
    draft = _filter_draft(
        question_span=clause,
        clause_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        draft_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
    )

    assert _project(question, draft) is None


def test_single_field_hclx_hint_cannot_replace_a_derived_expression() -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="ranking",
                text_span="순자산을 총보수로 나눈 비율이 큰 순",
                product_families=[ProductFamily.DOMESTIC_ETP],
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="aum",
                operation=SemanticFrameOperation.RANK,
                direction=SemanticFrameDirection.DESC,
                field_span="순자산",
            )
        ],
    )

    scoped = _scope_external_semantic_draft(draft, ProductFamily.DOMESTIC_ETP)

    assert scoped.execution_clauses == []


def test_single_field_aggregate_sum_remains_an_admissible_hint() -> None:
    clause = IntentExecutionClause(
        sequence=1,
        role="projection",
        text_span="순자산 합계",
        product_families=[ProductFamily.DOMESTIC_ETP],
        semantic_status=SemanticFrameStatus.RESOLVED,
        field_id="aum",
        operation=SemanticFrameOperation.AGGREGATE,
        field_span="순자산",
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.AGGREGATE,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        execution_clauses=[clause],
    )

    assert semantic_frame_clause_is_schema_admissible(draft, clause)
