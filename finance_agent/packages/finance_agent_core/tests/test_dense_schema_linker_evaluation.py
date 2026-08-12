from finance_agent_core.evaluation.dense_schema_linker import (
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
    assert report.decision.evidence_status == "insufficient_evidence"
    assert report.decision.production_adoption == "rejected_for_now"
    assert report.decision.product_semantic_search == "deferred"
    assert report.decision.abstention_policy_status == "not_calibrated"
