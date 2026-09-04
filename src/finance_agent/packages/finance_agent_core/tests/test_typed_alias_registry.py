from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_aliases import (
    FILTER_ALIAS_RULES,
    SEMANTIC_ALIAS_RULES,
    TYPED_ALIAS_POLICY_VERSION,
    typed_alias_matches,
)
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.agent.semantic_ledger import (
    SpanRole,
    build_resolved_span_ledger,
)
from finance_agent_core.config.registry import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition


def test_typed_alias_registry_is_unique_versioned_and_field_valid() -> None:
    registry = load_field_registry()
    all_rules = (*SEMANTIC_ALIAS_RULES, *FILTER_ALIAS_RULES)

    assert len({rule.rule_id for rule in all_rules}) == len(all_rules)
    for rule in FILTER_ALIAS_RULES:
        assert rule.policy_version == TYPED_ALIAS_POLICY_VERSION
        assert rule.evidence_source
        for family in rule.families:
            definition = registry.require_field(rule.field_id, [family.value])
            assert definition.queryable is True
    for rule in SEMANTIC_ALIAS_RULES:
        for family in rule.families:
            definition = registry.require_field(rule.field_id, [family.value])
            assert definition.sortable is True


def test_filter_alias_forbidden_contexts_prevent_overlapping_meanings() -> None:
    global_ex_us = typed_alias_matches(
        "미국 제외 글로벌 해외 ETF를 보여줘",
        ProductFamily.OVERSEAS_ETP,
    )
    assert [
        (match.field_id, match.value) for match in global_ex_us if match.operation == "filter"
    ] == [("investment_region", "Global Ex US")]

    securitized_bond = typed_alias_matches(
        "유동화회사채를 보여줘",
        ProductFamily.BOND,
    )
    assert [
        (match.field_id, match.value) for match in securitized_bond if match.operation == "filter"
    ] == [("bond_type", "유동화회사채")]


def test_unavailable_overseas_long_return_is_not_authorized_by_registry() -> None:
    question = "해외 ETF 중 1년 성과가 좋은 상품 3개를 보여줘"

    assert not any(
        match.field_id == "one_year_return_pct"
        for match in typed_alias_matches(question, ProductFamily.OVERSEAS_ETP)
    )
    route = IntentRouter().route(question, "typed-alias-overseas-return")
    assert route.disposition is RouteDisposition.UNSUPPORTED


def test_router_compiler_and_coverage_share_one_typed_alias_result() -> None:
    question = "대체투자 자산군 국내 ETF를 보여줘"
    expected = ("asset_type", "eq", "대체투자")

    aliases = typed_alias_matches(question, ProductFamily.DOMESTIC_ETP)
    alias = next(match for match in aliases if match.operation == "filter")
    assert (alias.field_id, alias.operator, alias.value) == expected

    hints = build_lexical_hints(question)
    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } >= {expected}

    compiled = canonicalize_query_plan_payload(
        question,
        {"product_families": [ProductFamily.DOMESTIC_ETP.value]},
    )
    assert {
        (item["field"], item["operator"], item["value"]) for item in compiled["constraints"]
    } >= {expected}

    coverage = SemanticCoverageGate(schema_link_gaps_enabled=True).evaluate(
        question,
        interaction_intent=InteractionIntent.SEARCH.value,
    )
    assert coverage.schema_link_gap_spans == ()
    assert coverage.ambiguity_spans == ()
    assert coverage.unsupported_spans == ()

    ledger = build_resolved_span_ledger(
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        product_families=(ProductFamily.DOMESTIC_ETP,),
        coverage=coverage,
    )
    assert any(
        span.role is SpanRole.HARD_FILTER and span.field_id == expected[0]
        for span in ledger.resolved_spans
    )

    route = IntentRouter().route(question, "typed-alias-shared-contract")
    assert route.disposition is RouteDisposition.EXECUTE
    assert route.draft.product_families == [ProductFamily.DOMESTIC_ETP]
