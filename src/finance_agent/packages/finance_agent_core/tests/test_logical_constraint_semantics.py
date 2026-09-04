from copy import deepcopy

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import IntentRouter
from finance_agent_core.agent.grounded_planning import (
    GroundedConstraintProposal,
    GroundedPlanGate,
    GroundedPlanProposal,
    GroundedPlanRejectedError,
)
from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.semantic_resolution import (
    HardFilterLock,
    SemanticResolutionError,
)
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    Intent,
    ProductFamily,
)
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
)
from finance_agent_core.execution.oracle import compile_search_sql
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)


def _search_plan(question: str, question_id: str = "logical-constraint") -> QueryPlan:
    payload = canonicalize_query_plan_payload(
        question,
        {
            "question_id": question_id,
            "product_families": ["domestic_etp"],
        },
        force_product_family_hint=True,
    )
    return QueryPlan.model_validate(payload)


def _constraint_atoms(question: str) -> set[tuple[str, str, object]]:
    hints = build_lexical_hints(question)
    return {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    }


def test_cross_field_or_is_an_explicit_boolean_expression() -> None:
    plan = _search_plan(
        "국내 ETF 중 3개월 수익률 5% 이상 또는 AUM 100억 이상인 상품을 보여줘"
    )

    assert plan.constraint_expression is not None
    assert plan.constraint_expression.operator.value == "any"
    assert len(plan.constraint_expression.constraint_keys) == 2
    _, count_sql, parameters = compile_search_sql(plan)
    assert " OR " in count_sql
    assert "three_month_return_micro_pct >= ?" in count_sql
    assert "aum_minor_units >= ?" in count_sql
    assert parameters == ["ETF", 5_000_000, 1_000_000_000_000]


def test_same_field_disjoint_ranges_are_not_silently_anded() -> None:
    plan = _search_plan(
        "국내 ETF 중 1년 수익률 10% 이상 또는 -5% 이하인 상품을 보여줘"
    )

    assert plan.constraint_expression is not None
    assert {
        (item.field, item.operator.value, item.value) for item in plan.constraints
    } >= {
        ("one_year_return_pct", "gte", 10),
        ("one_year_return_pct", "lte", -5),
    }
    _, count_sql, parameters = compile_search_sql(plan)
    assert "one_year_return_micro_pct >= ?" in count_sql
    assert " OR " in count_sql
    assert "one_year_return_micro_pct <= ?" in count_sql
    assert parameters == ["ETF", 10_000_000, -5_000_000]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "국내 ETF 중 20일·60일 변동성이 모두 10% 이하인 상품을 보여줘",
            {
                ("twenty_day_volatility_pct", "lte", 10),
                ("sixty_day_volatility_pct", "lte", 10),
            },
        ),
        (
            "국내 ETF 중 1개월·3개월 수익률이 각각 0% 이상인 상품을 보여줘",
            {
                ("one_month_return_pct", "gte", 0),
                ("three_month_return_pct", "gte", 0),
            },
        ),
    ],
)
def test_shared_threshold_is_copied_to_every_explicit_field(
    question: str,
    expected: set[tuple[str, str, object]],
) -> None:
    assert _constraint_atoms(question) >= expected
    plan = _search_plan(question)
    assert plan.constraint_expression is None
    _, count_sql, _ = compile_search_sql(plan)
    assert " OR " not in count_sql


def test_oracle_and_independent_verifier_agree_on_or_semantics(
    domestic_sample_database,
) -> None:
    path, _, _ = domestic_sample_database
    plan = _search_plan(
        "국내 ETF 중 1개월 수익률 50% 이상 또는 "
        "3개월 수익률 999% 이상인 상품을 보여줘",
        "logical-or-execution",
    )
    validated = authorize_internal_evaluation_plan(plan, path)
    executed = SQLiteOracle(path).execute(validated)
    universe = load_projected_verifier_records(path, validated)
    verified = ResultVerifier().verify(plan, executed, universe)

    assert verified.candidate_count == 2
    assert [record.ticker for record in verified.records] == ["A000005", "A000007"]


def test_constraint_expression_rejects_unknown_leaf() -> None:
    plan = _search_plan(
        "국내 ETF 중 1년 수익률 10% 이상 또는 -5% 이하인 상품을 보여줘"
    )
    payload = plan.model_dump(mode="json")
    assert "constraint_expression" in payload
    tampered = deepcopy(payload)
    tampered["constraint_expression"]["constraint_keys"][0] = "0" * 64

    with pytest.raises(ValidationError, match="unknown or duplicate leaf"):
        QueryPlan.model_validate(tampered)


def test_semantic_resolver_cannot_drop_locked_or_connective() -> None:
    plan = _search_plan(
        "국내 ETF 중 1년 수익률 10% 이상 또는 -5% 이하인 상품을 보여줘"
    )
    lock = HardFilterLock.from_plan(plan)
    changed = plan.model_copy(update={"constraint_expression": None})

    with pytest.raises(SemanticResolutionError, match="constraint logic"):
        lock.require_preserved(changed)


def _grounded_or_proposal(question_id: str, *, include_second_branch: bool = True):
    constraints = [
        GroundedConstraintProposal(
            field="product_type",
            operator=ConstraintOperator.EQ,
            value="ETF",
            evidence_span="ETF",
        ),
        GroundedConstraintProposal(
            field="one_month_return_pct",
            operator=ConstraintOperator.GTE,
            value=5,
            evidence_span="1개월 수익률 5% 이상",
        ),
    ]
    if include_second_branch:
        constraints.append(
            GroundedConstraintProposal(
                field="three_month_return_pct",
                operator=ConstraintOperator.GTE,
                value=10,
                evidence_span="3개월 수익률 10% 이상",
            )
        )
    return GroundedPlanProposal(
        schema_version="1.0",
        question_id=question_id,
        intent=Intent.SEARCH,
        product_family=ProductFamily.DOMESTIC_ETP,
        family_evidence_span="국내 ETF",
        constraints=constraints,
        ranking=[],
        limit=5,
        limit_evidence_span="",
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        ambiguities=[],
        unsupported_conditions=[],
    )


def test_grounded_hclx_plan_inherits_server_owned_or_expression(
    domestic_sample_database,
) -> None:
    path, _, _ = domestic_sample_database
    question_id = "grounded-logical-or"
    question = (
        "국내 ETF 중 1개월 수익률 5% 이상 또는 "
        "3개월 수익률 10% 이상인 상품을 보여줘"
    )
    trusted = _search_plan(question, question_id)
    decision = IntentRouter().route(question, question_id)

    plan = GroundedPlanGate({"domestic_etp": path}).compile(
        question,
        decision,
        _grounded_or_proposal(question_id),
        trusted_plan=trusted,
    )

    assert plan.constraint_expression == trusted.constraint_expression


def test_grounded_hclx_plan_cannot_omit_one_or_branch(
    domestic_sample_database,
) -> None:
    path, _, _ = domestic_sample_database
    question_id = "grounded-logical-or-missing"
    question = (
        "국내 ETF 중 1개월 수익률 5% 이상 또는 "
        "3개월 수익률 10% 이상인 상품을 보여줘"
    )
    trusted = _search_plan(question, question_id)
    decision = IntentRouter().route(question, question_id)

    with pytest.raises(
        GroundedPlanRejectedError,
        match="did not preserve the server constraint expression",
    ):
        GroundedPlanGate({"domestic_etp": path}).compile(
            question,
            decision,
            _grounded_or_proposal(question_id, include_second_branch=False),
            trusted_plan=trusted,
        )


def test_legacy_all_constraints_plan_keeps_original_json_shape() -> None:
    plan = _search_plan("국내 ETF 중 AUM 100억 이상인 상품을 보여줘")

    assert plan.constraint_expression is None
    assert "constraint_expression" not in plan.model_dump(mode="json")
    assert QueryPlan.model_validate_json(plan.model_dump_json()) == plan


def test_remaining_day_between_converts_year_units() -> None:
    hints = build_lexical_hints("잔존기간 1년부터 2년 사이인 채권을 보여줘")

    assert any(
        item == {
            "field": "remaining_days",
            "operator": "between",
            "value": [365, 730],
        }
        for item in hints["required_constraints"]
    )
