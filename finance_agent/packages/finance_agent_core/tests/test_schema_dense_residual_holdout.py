from __future__ import annotations

from finance_agent_core.agent.semantic_resolution import (
    ResolutionDecision,
    ResolutionOperation,
    SemanticResolutionDraft,
    SemanticResolutionRequest,
)
from finance_agent_core.contracts.queryplan import SortDirection
from finance_agent_core.evaluation.dense_schema_linker import FakeHashEmbeddingProvider
from finance_agent_core.evaluation.schema_dense_hclx_ab import (
    evaluate_hclx_candidate_selection,
)
from finance_agent_core.evaluation.schema_dense_residual_holdout import (
    evaluate_residual_holdout,
    finalize_report,
    load_residual_holdout,
)


def test_residual_holdout_is_explicitly_not_an_independent_blind() -> None:
    suite, suite_sha256 = load_residual_holdout()

    assert suite["status"] == "developer_authored_not_independent_blind"
    assert suite["scope"] == "single_residual_ranking_field_linking_only"
    assert len(suite["cases"]) == 85
    assert len(suite_sha256) == 64


def test_residual_holdout_report_compares_v1_v2_v3_and_stays_shadow_only() -> None:
    report = finalize_report(evaluate_residual_holdout(FakeHashEmbeddingProvider(dimension=64)))

    assert set(report["corpora"]) == {
        "schema-field-text-v1",
        "schema-field-text-v2",
        "schema-field-prototype-v3",
    }
    assert report["activation_gates"]["independent_blind_completed"] is False
    assert str(report["activation_decision"]).startswith("shadow_only_")
    assert report["report_sha256"] != "filled_after_canonicalization"
    assert len(report["report_sha256"]) == 64


class _ClarifyingSemanticProvider:
    provider_name = "hyperclova"
    model_name = "HCX-CONTRACT-TEST"

    def resolve_semantics(self, request: SemanticResolutionRequest) -> SemanticResolutionDraft:
        del request
        return SemanticResolutionDraft(
            decision=ResolutionDecision.CLARIFY,
            selected_field_id="__none__",
            operation=ResolutionOperation.RANK,
            direction=SortDirection.DESC,
            reason_code="multiple_interpretations",
            ambiguity_reason="후보를 하나로 확정할 수 없음",
        )


def test_hclx_ab_uses_a_bounded_candidate_only_subset() -> None:
    dense_report = finalize_report(
        evaluate_residual_holdout(FakeHashEmbeddingProvider(dimension=64))
    )

    report = evaluate_hclx_candidate_selection(
        _ClarifyingSemanticProvider(),  # type: ignore[arg-type]
        dense_report,
        maximum_calls=4,
    )

    assert 0 <= report["case_count"] <= 4
    assert report["scope"] == "candidate_selection_only_no_sql_or_answer_generation"
    assert report["activation_decision"].startswith("shadow_only_")
    assert all(len(case["candidate_field_ids"]) <= 10 for case in report["cases"])
