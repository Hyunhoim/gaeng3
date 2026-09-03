import hashlib
import json

import pytest

import scripts.compare_sealed_blind as sealed_comparator
from scripts.compare_sealed_blind import (
    _compare_phase2,
    _external_evidence_required,
    _gold_product_identity,
    _normalize_filter,
    _phase2_plan_metrics,
    _source_field_map,
)
from scripts.run_sealed_lab_blind import (
    _validate_document_calibration,
    _validate_question_only,
    _verify_pre_gold_source_state,
)


def _phase2_question_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "suite_id": "finance-agent-independent-holdout-phase2-v1-300",
        "status": "sealed_question_only_not_used_for_development",
        "phase": "phase2",
        "phase1_reference": {"preserved_without_modification": True},
        "case_count": 300,
        "cases": [
            {"id": f"P2-{index:03d}", "question": f"질문 {index}"} for index in range(1, 301)
        ],
    }


def test_phase2_question_only_contract_is_supported(tmp_path) -> None:
    payload = _phase2_question_payload()
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    metadata, cases = _validate_question_only(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert metadata["suite_id"] == "finance-agent-independent-holdout-phase2-v1-300"
    assert metadata["phase"] == "phase2"
    assert len(cases) == 300
    assert cases[0]["id"] == "P2-001"
    assert cases[-1]["id"] == "P2-300"


def test_phase3_600_case_question_only_contract_is_supported(tmp_path) -> None:
    payload = {
        "schema_version": "1.0",
        "suite_id": "finance-agent-development-phase3-v1-600",
        "status": "sealed_question_only",
        "phase": "phase3",
        "purpose": "error_discovery_and_knowledge_expansion",
        "predecessor_question_sha256": {
            "phase1": "080e00c13b37be45d060f1f17f2ac60c899b9b0e0ffb4d2b023b2bdad3918578",
            "phase2": "2132e13e5c1cb9f820bd097f17f534dcd05e0c3eaf59821b4493cf014f0ad5d9",
        },
        "case_count": 600,
        "cases": [
            {"id": f"P3-{index:03d}", "question": f"질문 {index}"} for index in range(1, 601)
        ],
    }
    path = tmp_path / "phase3-questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    metadata, cases = _validate_question_only(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert metadata["suite_id"] == "finance-agent-development-phase3-v1-600"
    assert metadata["phase"] == "phase3"
    assert metadata["suite_case_count"] == 600
    assert len(cases) == 600
    assert cases[0]["id"] == "P3-001"
    assert cases[-1]["id"] == "P3-600"


def test_phase4_1000_case_question_only_contract_is_supported(tmp_path) -> None:
    payload = {
        "schema_version": "1.0",
        "suite_id": "finance-agent-independent-holdout-phase4-v1-1000",
        "status": "sealed_question_only_not_used_for_development",
        "phase": 4,
        "case_count": 1000,
        "cases": [
            {"id": f"P4-{index:04d}", "question": f"질문 {index}"} for index in range(1, 1001)
        ],
    }
    path = tmp_path / "phase4-questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    metadata, cases = _validate_question_only(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert metadata["suite_id"] == "finance-agent-independent-holdout-phase4-v1-1000"
    assert metadata["phase"] == 4
    assert metadata["suite_case_count"] == 1000
    assert len(cases) == 1000
    assert cases[0]["id"] == "P4-0001"
    assert cases[-1]["id"] == "P4-1000"


def test_phase5_105_case_question_only_contract_is_supported(tmp_path) -> None:
    sets = ("A", "B", "C")
    payload = {
        "schema_version": "1.0",
        "suite_id": "finance-agent-final-rehearsal-phase5-v1.1-105",
        "phase": 5,
        "revision": "v1.1-official-examples",
        "status": "sealed_question_only_not_used_for_development",
        "case_count": 105,
        "set_count": 3,
        "cases_per_set": 35,
        "sets": [
            {
                "set_id": set_id,
                "case_count": 35,
                "id_range": [f"P5-{set_id}-01", f"P5-{set_id}-35"],
            }
            for set_id in sets
        ],
        "cases": [
            {"id": f"P5-{set_id}-{index:02d}", "question": f"질문 {set_id}-{index}"}
            for set_id in sets
            for index in range(1, 36)
        ],
    }
    path = tmp_path / "phase5-questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    metadata, cases = _validate_question_only(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert metadata["suite_id"] == "finance-agent-final-rehearsal-phase5-v1.1-105"
    assert metadata["phase"] == 5
    assert metadata["suite_case_count"] == 105
    assert len(cases) == 105
    assert cases[0]["id"] == "P5-A-01"
    assert cases[-1]["id"] == "P5-C-35"


def test_phase6_150_case_question_only_contract_is_supported(tmp_path) -> None:
    payload = {
        "schema_version": "1.0",
        "suite_id": "finance-agent-generalization-phase6-v1-150",
        "phase": 6,
        "status": "sealed_question_only_not_used_for_development_before_run",
        "case_count": 150,
        "cases": [
            {"id": f"P6-{index:03d}", "question": f"질문 {index}"}
            for index in range(1, 151)
        ],
    }
    path = tmp_path / "phase6-questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    metadata, cases = _validate_question_only(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    assert metadata["suite_id"] == "finance-agent-generalization-phase6-v1-150"
    assert metadata["phase"] == 6
    assert metadata["suite_case_count"] == 150
    assert len(cases) == 150
    assert cases[0]["id"] == "P6-001"
    assert cases[-1]["id"] == "P6-150"


def test_pre_gold_source_diff_file_is_verified(tmp_path) -> None:
    diff_file = tmp_path / "source.diff"
    diff_file.write_bytes(b"diff --git a/runtime.py b/runtime.py\n")
    expected = hashlib.sha256(diff_file.read_bytes()).hexdigest()
    arguments = type(
        "Arguments",
        (),
        {
            "pre_gold_development_diff_file": diff_file,
            "pre_gold_development_diff_sha256": expected,
        },
    )()

    assert _verify_pre_gold_source_state(arguments) == expected

    diff_file.write_bytes(b"changed after sealing\n")
    with pytest.raises(RuntimeError, match="SHA-256 differs"):
        _verify_pre_gold_source_state(arguments)


def test_pre_gold_source_diff_file_and_hash_are_required_together(tmp_path) -> None:
    arguments = type(
        "Arguments",
        (),
        {
            "pre_gold_development_diff_file": tmp_path / "source.diff",
            "pre_gold_development_diff_sha256": None,
        },
    )()

    with pytest.raises(RuntimeError, match="must be set together"):
        _verify_pre_gold_source_state(arguments)


def test_document_dense_gate_matches_hybrid_candidate_runtime_path(tmp_path) -> None:
    report = {
        "report_kind": "document_dense_public_calibration_not_blind",
        "artifact_sha256": "a" * 64,
        "metrics": {
            "bm25": {"top1_accuracy": 0.57, "recall_at_5": 0.84},
            "dense": {"top1_accuracy": 0.84, "recall_at_5": 0.96},
            "hybrid_rrf": {"top1_accuracy": 0.80, "recall_at_5": 1.0},
        },
    }
    path = tmp_path / "document-calibration.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    arguments = type(
        "Arguments",
        (),
        {
            "document_dense_calibration_file": path,
            "document_dense_calibration_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_dense_index_sha256": "a" * 64,
        },
    )()

    _validate_document_calibration(arguments)


def test_document_dense_gate_rejects_low_dense_recall_or_hybrid_regression(tmp_path) -> None:
    report = {
        "report_kind": "document_dense_public_calibration_not_blind",
        "artifact_sha256": "a" * 64,
        "metrics": {
            "bm25": {"top1_accuracy": 0.8, "recall_at_5": 0.9},
            "dense": {"top1_accuracy": 0.7, "recall_at_5": 0.9},
            "hybrid_rrf": {"top1_accuracy": 0.8, "recall_at_5": 0.8},
        },
    }
    path = tmp_path / "document-calibration.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    arguments = type(
        "Arguments",
        (),
        {
            "document_dense_calibration_file": path,
            "document_dense_calibration_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_dense_index_sha256": "a" * 64,
        },
    )()

    with pytest.raises(RuntimeError, match="quality gate failed"):
        _validate_document_calibration(arguments)


def test_phase3_600_case_gold_comparator_contract_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_hash = "a" * 64
    predictions = {
        "suite_id": "finance-agent-development-phase3-v1-600",
        "suite_phase": "phase3",
        "answer_key_opened": False,
        "question_count": 600,
        "questions_sha256": question_hash,
        "results": [{"id": f"P3-{index:03d}"} for index in range(1, 601)],
    }
    gold = {
        "suite_id": predictions["suite_id"],
        "questions_sha256": question_hash,
        "cases": [{"id": f"P3-{index:03d}"} for index in range(1, 601)],
    }
    marker = {"phase3": "accepted"}
    monkeypatch.setattr(sealed_comparator, "_compare_phase2", lambda *args, **kwargs: marker)

    assert sealed_comparator.compare(predictions, gold) is marker


def test_phase4_1000_case_gold_comparator_contract_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_hash = "b" * 64
    predictions = {
        "suite_id": "finance-agent-independent-holdout-phase4-v1-1000",
        "suite_phase": 4,
        "answer_key_opened": False,
        "question_count": 1000,
        "questions_sha256": question_hash,
        "results": [{"id": f"P4-{index:04d}"} for index in range(1, 1001)],
    }
    gold = {
        "suite_id": predictions["suite_id"],
        "questions_sha256": question_hash,
        "cases": [{"id": f"P4-{index:04d}"} for index in range(1, 1001)],
    }
    marker = {"phase4": "accepted"}
    monkeypatch.setattr(sealed_comparator, "_compare_phase2", lambda *args, **kwargs: marker)

    assert sealed_comparator.compare(predictions, gold) is marker


def test_phase5_105_case_gold_comparator_contract_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_hash = "c" * 64
    ids = [
        f"P5-{set_id}-{index:02d}"
        for set_id in ("A", "B", "C")
        for index in range(1, 36)
    ]
    predictions = {
        "suite_id": "finance-agent-final-rehearsal-phase5-v1.1-105",
        "suite_phase": 5,
        "answer_key_opened": False,
        "question_count": 105,
        "questions_sha256": question_hash,
        "results": [{"id": case_id} for case_id in ids],
    }
    gold = {
        "suite_id": predictions["suite_id"],
        "questions_sha256": question_hash,
        "cases": [{"id": case_id} for case_id in ids],
    }
    marker = {"phase5": "accepted"}
    monkeypatch.setattr(sealed_comparator, "_compare_phase2", lambda *args, **kwargs: marker)

    assert sealed_comparator.compare(predictions, gold) is marker


def test_external_evidence_required_supports_phase4_top_level_contract() -> None:
    assert _external_evidence_required({"external_evidence_required": True}) is True
    assert _external_evidence_required({"external_evidence_required": False}) is False


def test_external_evidence_required_supports_legacy_nested_contract() -> None:
    assert (
        _external_evidence_required(
            {"expected_status_basis": {"external_evidence_required": True}}
        )
        is True
    )
    assert _external_evidence_required({}) is False


def test_question_only_rejects_unregistered_suite(tmp_path) -> None:
    payload = _phase2_question_payload()
    payload["suite_id"] = "unregistered-suite"
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="suite contract differs"):
        _validate_question_only(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_official_source_column_mapping_is_case_insensitive() -> None:
    source_fields = _source_field_map()

    assert source_fields[("bond", "pd_no")] == "ticker"
    assert _normalize_filter(
        {"field": "PD_NO", "operator": "eq", "value": "KRTEST"},
        family="bond",
        source_fields=source_fields,
    ) == ("product_identity", "eq", "krtest")


def test_fund_risk_name_and_code_are_one_canonical_source_field() -> None:
    source_fields = _source_field_map()

    assert source_fields[("fund", "zrin_fd_ivst_risk_gcd")] == "risk_level"
    assert source_fields[("fund", "zrin_fd_ivst_risk_grd_nm")] == "risk_level"


def test_runtime_composite_identity_filters_match_official_public_keys() -> None:
    source_fields = _source_field_map()

    assert _normalize_filter(
        {
            "field": "product_id",
            "operator": "in",
            "value": ["KR111:장내:20260821:1", "KR222:장외:20260821:1"],
        },
        family="bond",
        source_fields=source_fields,
    ) == ("product_identity", "in", ("kr111", "kr222"))
    assert _normalize_filter(
        {"field": "product_id", "operator": "eq", "value": "AMX:DXD"},
        family="overseas_etp",
        source_fields=source_fields,
    ) == ("product_identity", "eq", "dxd")
    assert _normalize_filter(
        {"field": "issue_date", "operator": "gte", "value": "20250101"},
        family="bond",
        source_fields=source_fields,
    ) == ("issue_date", "gte", "2025-01-01")


def test_plan_metrics_do_not_hide_unrequested_currency_constraints() -> None:
    source_fields = _source_field_map()
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "overseas_etp",
                "filters": [
                    {"field": "pd_grp_no", "operator": "eq", "value": "ETF"},
                    {"field": "pd_itm_no", "operator": "eq", "value": "DXD"},
                ],
                "ranking_field": "aum",
                "direction": "desc",
                "limit": 1,
            }
        ],
    }
    response = {
        "query_plan": {
            "product_families": ["overseas_etp"],
            "constraints": [
                {"field": "product_id", "operator": "eq", "value": "AMX:DXD"},
                {"field": "trading_currency", "operator": "eq", "value": "USD"},
            ],
            "ranking": [{"field": "aum", "direction": "desc"}],
            "limit": 1,
        }
    }

    assert _phase2_plan_metrics(
        expected,
        response,
        source_fields=source_fields,
    ) == {
        "family_exact": True,
        "filters_exact": False,
        "ranking_exact": True,
        "limit_exact": True,
    }


def test_exact_name_resolved_to_one_db_identity_is_filter_equivalent() -> None:
    source_fields = _source_field_map()
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "fund",
                "filters": [
                    {"field": "prvo_pbff_desc", "operator": "eq", "value": "공모"},
                    {"field": "itm_abrv_nm", "operator": "eq", "value": "정확한 펀드명"},
                ],
                "ranking_field": None,
                "direction": None,
                "limit": 1,
            }
        ],
    }
    response = {
        "query_plan": {
            "product_families": ["fund"],
            "constraints": [
                {"field": "public_offering", "operator": "eq", "value": True},
                {"field": "product_id", "operator": "eq", "value": "KR5000000001"},
            ],
            "ranking": [],
            "limit": 1,
        },
        "candidate_count": 1,
        "products": [{"product_id": "KR5000000001"}],
    }

    assert (
        _phase2_plan_metrics(
            expected,
            response,
            source_fields=source_fields,
        )["filters_exact"]
        is True
    )


def test_gold_labels_and_typed_filters_share_semantic_normal_form() -> None:
    source_fields = _source_field_map()

    assert _normalize_filter(
        {"field": "public_offering", "operator": "eq", "value": "공모"},
        family="fund",
        source_fields=source_fields,
    ) == _normalize_filter(
        {"field": "public_offering", "operator": "eq", "value": True},
        family="fund",
        source_fields=source_fields,
    )
    assert _normalize_filter(
        {"field": "sellable", "operator": "eq", "value": "판매중"},
        family="fund",
        source_fields=source_fields,
    ) == _normalize_filter(
        {"field": "sellable", "operator": "eq", "value": True},
        family="fund",
        source_fields=source_fields,
    )


def test_gold_identity_uses_each_official_table_public_key() -> None:
    product = {"product_id": "INTERNAL-ROW", "ticker": "PUBLIC-TICKER"}

    assert _gold_product_identity(product, "bond") == "PUBLIC-TICKER"
    assert _gold_product_identity(product, "overseas_etp") == "PUBLIC-TICKER"
    assert _gold_product_identity(product, "domestic_etp") == "INTERNAL-ROW"
    assert _gold_product_identity(product, "fund") == "INTERNAL-ROW"


def test_external_product_identity_reads_verified_ticker_field_evidence() -> None:
    product = {
        "product_id": "AMX:KSTR.K",
        "fields": [
            {
                "canonical_field": "ticker",
                "normalized_value": "KSTR.K",
            }
        ],
    }

    assert _gold_product_identity(product, "overseas_etp") == "KSTR.K"


def test_phase2_composite_plan_metrics_validate_each_public_family_subplan() -> None:
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "bond",
                "filters": [{"field": "pd_risk_nm", "operator": "eq", "value": "보통"}],
                "ranking_field": "remaining_days",
                "direction": "asc",
                "limit": 2,
            },
            {
                "product_family": "fund",
                "filters": [{"field": "prvo_pbff_desc", "operator": "eq", "value": "공모"}],
                "ranking_field": "fd_yr1_ern_r",
                "direction": "desc",
                "limit": 3,
            },
        ],
    }
    response = {
        "query_plan": None,
        "family_searches": [
            {
                "query_plan": {
                    "product_families": ["bond"],
                    "constraints": [{"field": "risk_level", "operator": "eq", "value": "보통"}],
                    "ranking": [{"field": "remaining_days", "direction": "asc"}],
                    "limit": 2,
                },
                "candidate_count": 10,
                "returned_product_ids": ["B1", "B2"],
            },
            {
                "query_plan": {
                    "product_families": ["fund"],
                    "constraints": [{"field": "public_offering", "operator": "eq", "value": True}],
                    "ranking": [{"field": "one_year_return_pct", "direction": "desc"}],
                    "limit": 3,
                },
                "candidate_count": 20,
                "returned_product_ids": ["F1", "F2", "F3"],
            },
        ],
    }

    assert _phase2_plan_metrics(
        expected,
        response,
        source_fields=_source_field_map(),
    ) == {
        "family_exact": True,
        "filters_exact": True,
        "ranking_exact": True,
        "limit_exact": True,
    }


def test_derived_metric_ranking_is_not_forced_into_one_standard_field() -> None:
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "fund",
                "filters": [],
                "ranking_field": None,
                "derived_metric": {
                    "kind": "subtract",
                    "fields": ["fd_yr1_ern_r", "fd_mm3_ern_r"],
                },
                "direction": "asc",
                "limit": 5,
            }
        ],
    }
    response = {
        "query_plan": {
            "product_families": ["fund"],
            "constraints": [],
            "ranking": [],
            "limit": 5,
        },
        "candidate_count": 10,
        "products": [],
    }

    assert _phase2_plan_metrics(
        expected,
        response,
        source_fields=_source_field_map(),
    ) == {
        "family_exact": True,
        "filters_exact": True,
        "ranking_exact": None,
        "limit_exact": True,
    }


def test_empty_identity_intersection_uses_zero_result_receipt_instead_of_empty_in() -> None:
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "domestic_etp",
                "filters": [
                    {"field": "pd_grp_no", "operator": "eq", "value": "ETF"},
                    {"field": "pd_itm_no", "operator": "in", "value": []},
                ],
                "ranking_field": "pd_net_tamt",
                "direction": "desc",
                "limit": 3,
            },
            {
                "product_family": "overseas_etp",
                "filters": [
                    {"field": "pd_grp_no", "operator": "eq", "value": "ETF"},
                    {"field": "pd_itm_no", "operator": "in", "value": []},
                ],
                "ranking_field": "du_last_aum",
                "direction": "desc",
                "limit": 3,
            },
        ],
    }
    response = {
        "query_plan": None,
        "family_searches": [
            {
                "query_plan": {
                    "product_families": ["domestic_etp"],
                    "constraints": [
                        {"field": "product_type", "operator": "eq", "value": "ETF"}
                    ],
                    "ranking": [{"field": "net_asset_total", "direction": "desc"}],
                    "limit": 3,
                },
                "candidate_count": 0,
                "returned_product_ids": [],
            },
            {
                "query_plan": {
                    "product_families": ["overseas_etp"],
                    "constraints": [
                        {"field": "product_type", "operator": "eq", "value": "ETF"}
                    ],
                    "ranking": [{"field": "aum", "direction": "desc"}],
                    "limit": 3,
                },
                "candidate_count": 0,
                "returned_product_ids": [],
            },
        ],
    }

    assert _phase2_plan_metrics(
        expected,
        response,
        source_fields=_source_field_map(),
    ) == {
        "family_exact": True,
        "filters_exact": True,
        "ranking_exact": True,
        "limit_exact": None,
    }


def test_composite_external_plan_is_not_scored_as_a_standard_sql_plan() -> None:
    expected = {
        "expected_disposition": "execute",
        "expected_subqueries": [
            {
                "product_family": "domestic_etp",
                "filters": [],
                "ranking_field": "pd_net_tamt",
                "direction": "desc",
                "limit": 3,
            }
        ],
    }
    response = {
        "query_plan": {
            "plan_kind": "composite_external_knowledge",
            "interaction_intent": "search",
            "requirements": [],
        },
        "candidate_count": 1,
        "products": [],
    }

    assert _phase2_plan_metrics(
        expected,
        response,
        source_fields=_source_field_map(),
    ) == {
        "family_exact": None,
        "filters_exact": None,
        "ranking_exact": None,
        "limit_exact": None,
    }


def test_composed_targeted_regression_requires_explicit_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_hash = "d" * 64
    predictions = {
        "suite_id": "finance-agent-independent-holdout-phase4-v1-1000",
        "suite_phase": 4,
        "answer_key_opened": True,
        "evaluation_kind": "post_gold_development_regression",
        "artifact_kind": "post_gold_composed_targeted_regression_predictions",
        "question_count": 1000,
        "questions_sha256": question_hash,
        "results": [{"id": f"P4-{index:04d}"} for index in range(1, 1001)],
        "regression_composition": {
            "unchanged_full_run_cases": 986,
            "actual_targeted_rerun_cases": 14,
        },
        "targeted_regression_source_sha256s": ["e" * 64],
    }
    gold = {
        "suite_id": predictions["suite_id"],
        "questions_sha256": question_hash,
        "cases": [{"id": f"P4-{index:04d}"} for index in range(1, 1001)],
    }
    marker = {"composed": "accepted"}
    monkeypatch.setattr(sealed_comparator, "_compare_phase2", lambda *args, **kwargs: marker)

    assert (
        sealed_comparator.compare(
            predictions,
            gold,
            allow_post_gold_development=True,
        )
        is marker
    )


def test_phase2_post_gold_report_is_never_labeled_as_first_blind() -> None:
    gold_cases = []
    prediction_results = []
    for index in range(1, 301):
        case_id = f"P2-{index:03d}"
        gold_cases.append(
            {
                "id": case_id,
                "category": "clarify",
                "query_scope": "single_family",
                "expected_disposition": "clarify",
                "expected_result_status": "CLARIFY",
                "expected_product_families": [],
                "expected_top_products": [],
                "expected_candidate_count_total": 0,
                "expected_query_plan": None,
                "expected_status_basis": {"external_evidence_required": False},
            }
        )
        prediction_results.append(
            {
                "id": case_id,
                "http_status": 200,
                "duration_ms": 1.0,
                "provider_calls": [],
                "backend_response": {
                    "status": "clarification",
                    "products": [],
                    "product_families": [],
                    "candidate_count": 0,
                    "documents": [],
                    "query_plan": None,
                },
            }
        )
    report = _compare_phase2(
        {
            "suite_id": "finance-agent-independent-holdout-phase2-v1-300",
            "suite_phase": "phase2",
            "canonical_prediction_sha256": "a" * 64,
            "questions_sha256": "b" * 64,
            "implementation_commit": "c" * 40,
            "image_id": "sha256:" + "d" * 64,
            "results": prediction_results,
        },
        {"cases": gold_cases},
        allow_post_gold_development=True,
    )

    assert report["artifact_kind"] == "post_gold_development_regression_comparison"
    assert report["evaluation_kind"] == "post_gold_development_regression"


def test_terminal_lookup_results_are_execute_dispositions_but_keep_distinct_statuses() -> None:
    gold_cases = []
    prediction_results = []
    for index in range(1, 301):
        case_id = f"P2-{index:03d}"
        if index <= 2:
            expected_disposition = "execute"
            expected_status = "NOT_FOUND"
            predicted_status = "not_found" if index == 1 else "cannot_verify"
        else:
            expected_disposition = "clarify"
            expected_status = "CLARIFY"
            predicted_status = "clarification"
        gold_cases.append(
            {
                "id": case_id,
                "category": "resolvable" if index <= 2 else "clarify",
                "query_scope": "single_family",
                "expected_disposition": expected_disposition,
                "expected_result_status": expected_status,
                "expected_product_families": [],
                "expected_top_products": [],
                "expected_candidate_count_total": 0,
                "expected_subqueries": [],
                "expected_status_basis": {"external_evidence_required": False},
            }
        )
        prediction_results.append(
            {
                "id": case_id,
                "http_status": 200,
                "duration_ms": 1.0,
                "provider_calls": [],
                "backend_response": {
                    "status": predicted_status,
                    "products": [],
                    "product_families": [],
                    "candidate_count": 0,
                    "documents": [],
                    "query_plan": None,
                },
            }
        )

    report = _compare_phase2(
        {
            "suite_id": "finance-agent-independent-holdout-phase2-v1-300",
            "suite_phase": "phase2",
            "canonical_prediction_sha256": "a" * 64,
            "questions_sha256": "b" * 64,
            "implementation_commit": "c" * 40,
            "image_id": "sha256:" + "d" * 64,
            "results": prediction_results,
        },
        {"cases": gold_cases},
        allow_post_gold_development=True,
    )

    assert report["metrics"]["disposition_accuracy"] == 1.0
    assert report["metrics"]["result_status_accuracy"] == 0.996667
    assert report["metrics"]["outcome_exact_accuracy"] == 0.996667
    assert report["metrics"]["false_block_rate"] == 0.0
    assert report["metrics"]["product_macro_f1"] == 1.0
