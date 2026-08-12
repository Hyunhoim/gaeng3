from finance_agent_core.evaluation.dense_schema_linker import (
    FakeHashEmbeddingProvider,
    _fuse_fields,
    run_dense_schema_linker_evaluation,
)


def test_fake_dense_schema_evaluation_is_safe_but_never_adoption_evidence() -> None:
    report = run_dense_schema_linker_evaluation()

    assert report.suite_case_count == 200
    assert report.status == "public_offline_component_not_blind"
    assert report.evaluation_scope == "schema_field_linking_with_gold_product_family"
    assert not report.routing_quality_included
    assert report.safety.legacy_suite_blocked_cases == 20
    assert report.safety.versioned_policy_migration_count == 1
    assert (
        report.safety.policy_migration_review_status
        == "developer_authored_pending_finance_domain_review"
    )
    assert report.safety.current_policy_execute_cases == 181
    assert report.safety.current_policy_blocked_cases == 19
    assert report.safety.blocked_no_call_rate == 1
    assert report.safety.out_of_registry_candidate_count == 0
    assert report.safety.out_of_family_candidate_count == 0
    assert not report.safety.production_feature_enabled
    assert report.safety.production_probe_status == "disabled"
    assert report.safety.production_probe_provider_query_calls == 0
    assert report.index_manifest.scope == "offline_evaluation_only"
    assert not report.index_manifest.production_enabled
    assert report.lexical.micro_precision_among_returned_at_5 == 0.994163
    assert report.lexical.fixed_k_micro_precision_at_5 == 0.564641
    assert report.hybrid.fixed_k_micro_precision_at_5 == 0.561326
    assert set(report.metrics_by_family) == {
        "bond",
        "domestic_etp",
        "fund",
        "overseas_etp",
    }
    assert set(report.metrics_by_split) == {"development", "holdout"}
    assert sum(
        item.hybrid.scored_cases for item in report.metrics_by_family.values()
    ) == report.hybrid.scored_cases
    assert sum(
        item.hybrid.gold_field_count for item in report.metrics_by_family.values()
    ) == report.hybrid.gold_field_count
    assert len(report.case_diagnostics) == report.hybrid.scored_cases
    assert all(
        len(item.dense_fields) == len(item.dense_scores)
        and item.dense_top_1_score == item.dense_scores[0]
        and item.dense_margin_top_1_top_2 is not None
        and item.dense_margin_top_1_top_2 >= 0
        for item in report.case_diagnostics
    )
    assert all(item.case_id and item.question for item in report.hybrid_failure_cases)
    assert all(
        (not item.lexical_exact and item.hybrid_exact)
        or len(item.hybrid_missing_at_5) < len(item.lexical_missing_at_5)
        for item in report.lexical_recovery_cases
    )
    assert report.decision.evidence_status == "insufficient_evidence"
    assert report.decision.production_adoption == "rejected_for_now"
    assert report.decision.product_semantic_search == "deferred"
    assert report.decision.abstention_policy_status == "not_calibrated"


def test_lexical_first_fusion_preserves_the_lexical_priority_contract() -> None:
    assert _fuse_fields(
        ("fee", "aum"),
        ("aum", "inception_date"),
        strategy="lexical_first",
    ) == ("fee", "aum", "inception_date")

    report = run_dense_schema_linker_evaluation(
        FakeHashEmbeddingProvider(),
        fusion_strategy="lexical_first",
    )

    assert report.hybrid.exact_at_gold_cardinality >= report.lexical.exact_at_gold_cardinality
    assert report.hybrid.micro_recall_at_5 >= report.lexical.micro_recall_at_5
