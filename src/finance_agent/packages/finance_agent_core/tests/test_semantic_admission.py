from __future__ import annotations

import pytest

from finance_agent_core.agent.semantic_admission import (
    constrain_semantic_candidate_fields,
    deterministic_anchor_field_ids,
    is_generic_semantic_ambiguity,
    semantic_field_evidence,
)
from finance_agent_core.agent.semantic_resolution import (
    ResolutionDecision,
    ResolutionOperation,
    SchemaFieldCandidate,
    SemanticResolutionDraft,
    SemanticResolutionError,
    SemanticResolutionGate,
    SemanticResolutionRequest,
    SpanSource,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent


def test_tax_and_period_anchors_are_unique_and_family_scoped() -> None:
    assert deterministic_anchor_field_ids(
        "세전으로 매수했을 때 남는 수익",
        product_family=ProductFamily.BOND,
    ) == frozenset({"buy_yield_pct"})
    assert deterministic_anchor_field_ids(
        "최근 분기 성적이 앞선",
        product_family=ProductFamily.DOMESTIC_ETP,
    ) == frozenset({"three_month_return_pct"})
    assert deterministic_anchor_field_ids(
        "연초 이후 성과가 앞선",
        product_family=ProductFamily.DOMESTIC_ETP,
    ) == frozenset({"ytd_return_pct"})


@pytest.mark.parametrize(
    "text",
    [
        "좋은",
        "안정적인",
        "인기 있는",
        "부담이 적은",
        "알짜인",
    ],
)
def test_generic_preferences_are_never_sent_to_dense_or_hclx(text: str) -> None:
    assert is_generic_semantic_ambiguity(
        text,
        product_family=ProductFamily.OVERSEAS_ETP,
    )


def test_candidate_space_excludes_metadata_and_respects_explicit_anchor() -> None:
    eligible = {
        "static_as_of",
        "dynamic_as_of",
        "product_name",
        "buy_yield_pct",
        "after_tax_yield_pct",
        "coupon_rate_pct",
    }

    assert constrain_semantic_candidate_fields(
        "세후 기준으로 손에 남는 수익",
        product_family=ProductFamily.BOND,
        operation="rank",
        eligible_field_ids=eligible,
    ) == {"after_tax_yield_pct"}


def test_hclx_cannot_convert_body_size_evidence_into_holding_cost() -> None:
    request = SemanticResolutionRequest(
        request_id="evidence-cross-check",
        residual_span="몸집",
        context_after="이 큰 해외 ETF",
        product_family=ProductFamily.OVERSEAS_ETP,
        interaction_intent=InteractionIntent.SEARCH,
        allowed_operations=(ResolutionOperation.RANK,),
        candidates=(
            SchemaFieldCandidate(field_id="aum", rank=1, dense_score=0.61),
            SchemaFieldCandidate(
                field_id="total_expense_ratio_pct",
                rank=2,
                dense_score=0.59,
            ),
        ),
        expected_direction=SortDirection.DESC,
        hard_filter_lock_sha256="a" * 64,
    )
    draft = SemanticResolutionDraft(
        decision=ResolutionDecision.RESOLVE,
        selected_field_id="total_expense_ratio_pct",
        operation=ResolutionOperation.RANK,
        direction=SortDirection.DESC,
        reason_code="candidate_context_match",
        evidence_spans=("몸집",),
    )

    with pytest.raises(SemanticResolutionError, match="server-grounded"):
        SemanticResolutionGate().admit(draft, request, source=SpanSource.HCLX)


def test_hclx_evidence_must_be_an_exact_question_substring() -> None:
    request = SemanticResolutionRequest(
        request_id="evidence-grounding",
        residual_span="몸집",
        context_after="이 큰 해외 ETF",
        product_family=ProductFamily.OVERSEAS_ETP,
        interaction_intent=InteractionIntent.SEARCH,
        allowed_operations=(ResolutionOperation.RANK,),
        candidates=(SchemaFieldCandidate(field_id="aum", rank=1, dense_score=0.61),),
        expected_direction=SortDirection.DESC,
        hard_filter_lock_sha256="b" * 64,
    )
    draft = SemanticResolutionDraft(
        decision=ResolutionDecision.RESOLVE,
        selected_field_id="aum",
        operation=ResolutionOperation.RANK,
        direction=SortDirection.DESC,
        reason_code="candidate_context_match",
        evidence_spans=("운용자산이 크다",),
    )

    with pytest.raises(SemanticResolutionError, match="not grounded"):
        SemanticResolutionGate().admit(draft, request, source=SpanSource.HCLX)


def test_explicit_cost_language_supports_only_holding_cost() -> None:
    cost = semantic_field_evidence(
        "운영 비용 부담이 작은",
        field_id="total_expense_ratio_pct",
        product_family=ProductFamily.OVERSEAS_ETP,
    )
    price = semantic_field_evidence(
        "운영 비용 부담이 작은",
        field_id="close_price",
        product_family=ProductFamily.OVERSEAS_ETP,
    )

    assert cost.supported
    assert not price.supported
