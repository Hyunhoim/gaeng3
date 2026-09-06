import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.compare_sealed_blind as sealed_comparator
import scripts.run_sealed_lab_blind as sealed_runner
from scripts.compare_sealed_blind import (
    _compare_phase2,
    _external_evidence_required,
    _external_exact_metrics,
    _gold_product_identity,
    _hclx_final_grounding_violations,
    _normalize_filter,
    _phase2_plan_metrics,
    _source_field_map,
)
from scripts.run_sealed_lab_blind import (
    _SELECTION_ARTIFACT_KIND,
    _SELECTION_METHOD,
    _build_approved_cache_only_agent,
    _canonical_sha256,
    _case_result,
    _representative_case_ids,
    _runtime_source_provenance,
    _validate_case_selection,
    _validate_document_calibration,
    _validate_mode_arguments,
    _validate_question_only,
    _verify_pre_gold_source_state,
)
from tests.conftest import FakeAgentService


def test_runtime_source_provenance_matches_mounted_core() -> None:
    provenance = _runtime_source_provenance()

    assert set(provenance) == {
        "finance_agent_core_init_sha256",
        "routed_service_sha256",
        "finance_agent_core_tree_sha256",
        "fastapi_backend_app_tree_sha256",
    }
    assert all(len(value) == 64 for value in provenance.values())


def test_runtime_source_provenance_rejects_legacy_import_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_init = tmp_path / "legacy" / "finance_agent_core" / "__init__.py"
    legacy_init.parent.mkdir(parents=True)
    legacy_init.write_text("# stale package\n", encoding="utf-8")
    monkeypatch.setattr(sealed_runner.finance_agent_core, "__file__", str(legacy_init))

    with pytest.raises(RuntimeError, match="differs from mounted source"):
        _runtime_source_provenance()


def test_blind_runner_does_not_retry_a_non_retryable_failure() -> None:
    agent = FakeAgentService(error=RuntimeError("private failure"))

    result = _case_result(
        case={"id": "P5-A-01", "question": "해외 ETF 조회"},
        agent=agent,
        calls=[],
        max_retries=2,
    )

    assert len(result["attempts"]) == 1
    assert agent.calls == [("해외 ETF 조회", "P5-A-01")]


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


def _case_selection_payload(question_sha256: str, ids: list[str]) -> dict[str, object]:
    selected = _representative_case_ids(
        questions_sha256=question_sha256,
        case_ids=ids,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "artifact_kind": _SELECTION_ARTIFACT_KIND,
        "suite_id": "finance-agent-final-rehearsal-phase5-v1.1-105",
        "questions_sha256": question_sha256,
        "selection_method": _SELECTION_METHOD,
        "case_count": 100,
        "selected_ids": selected,
    }
    payload["selection_sha256"] = _canonical_sha256(payload)
    return payload


def test_post_gold_case_selection_is_deterministic_and_exact(tmp_path: Path) -> None:
    question_sha256 = "89fb1f174d2da2aa96ffab60ddc0ebd8fdef3b044548f229096c33a8cd8bae70"
    ids = [
        f"P5-{set_id}-{index:02d}"
        for set_id in ("A", "B", "C")
        for index in range(1, 36)
    ]
    payload = _case_selection_payload(question_sha256, ids)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = _validate_case_selection(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        suite_id="finance-agent-final-rehearsal-phase5-v1.1-105",
        questions_sha256=question_sha256,
        available_case_ids=ids,
    )

    assert loaded["case_count"] == 100
    assert len(loaded["selected_ids"]) == 100
    assert loaded["selected_ids"] == [
        case_id for case_id in ids if case_id in loaded["selected_ids"]
    ]
    assert set(ids).difference(loaded["selected_ids"]) == {
        "P5-A-04",
        "P5-B-12",
        "P5-C-03",
        "P5-C-16",
        "P5-C-26",
    }


def test_post_gold_case_selection_rejects_a_different_100_case_subset(
    tmp_path: Path,
) -> None:
    question_sha256 = "8" * 64
    ids = [
        f"P5-{set_id}-{index:02d}"
        for set_id in ("A", "B", "C")
        for index in range(1, 36)
    ]
    payload = _case_selection_payload(question_sha256, ids)
    selected = list(payload["selected_ids"])
    omitted = next(case_id for case_id in ids if case_id not in selected)
    selected[-1] = omitted
    payload["selected_ids"] = selected
    canonical = dict(payload)
    canonical.pop("selection_sha256")
    payload["selection_sha256"] = _canonical_sha256(canonical)
    path = tmp_path / "mutated-selection.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="deterministic sample"):
        _validate_case_selection(
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            suite_id="finance-agent-final-rehearsal-phase5-v1.1-105",
            questions_sha256=question_sha256,
            available_case_ids=ids,
        )


def test_case_selection_is_forbidden_without_post_gold_mode() -> None:
    arguments = SimpleNamespace(
        case_selection_file=Path("/tmp/selection.json"),
        case_selection_sha256="a" * 64,
        post_gold_development_regression=False,
        development_case_ids=None,
        official_answer_max_inflight=2,
        external_mode="approved-cache-only",
        development_unapproved_databases=False,
    )

    with pytest.raises(SystemExit, match="post-Gold"):
        _validate_mode_arguments(arguments)


def test_approved_cache_only_runner_rejects_live_credentials_before_assembly() -> None:
    arguments = SimpleNamespace(
        naver_client_id_file=Path("/run/secrets/naver-id"),
        naver_client_secret_file=None,
        pending_corpus_manifest=None,
        pending_corpus_manifest_sha256=None,
        relation_artifact_sha256_file=None,
    )

    with pytest.raises(RuntimeError, match="forbids live"):
        _build_approved_cache_only_agent(arguments)


def test_approved_cache_only_runner_uses_production_assembly_without_live_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_index = tmp_path / "document-dense.json"
    document_index.write_bytes(b"approved index")
    captured: dict[str, object] = {}

    class FakeSettings:
        audit_shutdown_timeout_seconds = 5.0

    class FakeAuditSink:
        def close(self, *, timeout_seconds: float) -> bool:
            return timeout_seconds == 5.0

    class FakeDiscovery:
        production_approved = True
        live_fallback_enabled = False
        fallback = None
        activation_binding_sha256 = "d" * 64

    class FakeRouted:
        def __init__(self) -> None:
            self.external_knowledge_runtime = SimpleNamespace(
                acquisition=SimpleNamespace(discovery_provider=FakeDiscovery())
            )

    signed = SimpleNamespace(
        status="activated",
        artifact=SimpleNamespace(
            retrieval_mode="approved_corpus_cache_only",
            document_activation_receipt_sha256="d" * 64,
        ),
    )
    release = SimpleNamespace(
        manifest=SimpleNamespace(
            components=SimpleNamespace(
                knowledge_retrieval=SimpleNamespace(external=signed)
            )
        )
    )
    sink = FakeAuditSink()

    def fake_settings(**values: object) -> FakeSettings:
        captured.update(values)
        return FakeSettings()

    monkeypatch.setattr(sealed_runner, "Settings", fake_settings)
    monkeypatch.setattr(sealed_runner, "RoutedFinanceAgent", FakeRouted)
    monkeypatch.setattr(sealed_runner, "CacheFirstOfficialDiscoveryProvider", FakeDiscovery)
    monkeypatch.setattr(sealed_runner, "resolve_runtime_release", lambda settings: release)
    monkeypatch.setattr(sealed_runner, "build_audit_sink", lambda settings: sink)
    monkeypatch.setattr(
        sealed_runner,
        "build_production_agent",
        lambda settings, *, release_guard, audit_sink: FakeRouted(),
    )
    monkeypatch.setattr(
        sealed_runner,
        "require_approval_guard",
        lambda service, settings, release_guard, audit_sink: service,
    )
    arguments = SimpleNamespace(
        naver_client_id_file=None,
        naver_client_secret_file=None,
        pending_corpus_manifest=None,
        pending_corpus_manifest_sha256=None,
        relation_artifact_sha256_file=None,
        runtime_audit_file=tmp_path / "audit" / "events.jsonl",
        relation_artifact_sha256="a" * 64,
        document_dense_index=document_index,
        document_dense_index_sha256=hashlib.sha256(document_index.read_bytes()).hexdigest(),
        backend_version="0.1.0",
        hclx_key_file=tmp_path / "hcx-key",
        official_answer_max_inflight=2,
        release_manifest_file=tmp_path / "release.json",
        deployment_binding_file=tmp_path / "binding.json",
        deployment_binding_sha256="b" * 64,
        source_commit="c" * 40,
        runtime_image_reference="registry/image@sha256:" + "e" * 64,
        runtime_platform="linux/amd64",
        schema_dense_index=tmp_path / "schema.json",
        schema_dense_index_sha256="f" * 64,
        schema_dense_calibration_sha256="1" * 64,
        schema_dense_min_score=0.34,
        schema_dense_hclx_candidate_min_score=0.3,
        schema_dense_minimum_margin=0.03,
        kure_snapshot_dir=tmp_path / "snapshot",
        kure_snapshot_manifest=tmp_path / "snapshot.json",
        kure_cache_root=tmp_path / "cache",
        kure_cpu_threads=2,
        relation_artifact=tmp_path / "relations.json",
        relation_index=tmp_path / "relations.sqlite3",
        external_knowledge_artifact_file=tmp_path / "external.json",
        external_knowledge_artifact_sha256="2" * 64,
        external_temporal_relation_file=tmp_path / "temporal.json",
        external_temporal_relation_sha256="3" * 64,
        document_cache_activation_file=tmp_path / "activation.json",
        approved_corpus_manifest=tmp_path / "corpus.json",
        approved_corpus_root=tmp_path / "corpus",
        bond_db=tmp_path / "bond.sqlite3",
        domestic_etp_db=tmp_path / "domestic.sqlite3",
        overseas_etp_db=tmp_path / "overseas.sqlite3",
        fund_db=tmp_path / "fund.sqlite3",
    )

    service, calls, discovery, returned_sink = _build_approved_cache_only_agent(arguments)

    assert isinstance(service, FakeRouted)
    assert calls == []
    assert isinstance(discovery, FakeDiscovery)
    assert returned_sink is sink
    assert captured["app_env"] == "evaluation"
    assert captured["naver_api_hub_client_id_file"] is None
    assert captured["naver_api_hub_client_secret_file"] is None
    assert captured["external_document_corpus_root"] == arguments.approved_corpus_root


def test_comparator_accepts_only_the_same_canonical_100_case_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_sha256 = "7" * 64
    all_ids = [
        f"P5-{set_id}-{index:02d}"
        for set_id in ("A", "B", "C")
        for index in range(1, 36)
    ]
    selection = _case_selection_payload(question_sha256, all_ids)
    selected_ids = list(selection["selected_ids"])
    selection_file_sha256 = "6" * 64
    predictions = {
        "artifact_kind": "post_gold_development_regression_predictions",
        "answer_key_opened": True,
        "evaluation_kind": "post_gold_development_regression",
        "suite_id": "finance-agent-final-rehearsal-phase5-v1.1-105",
        "suite_phase": 5,
        "questions_sha256": question_sha256,
        "question_count": 100,
        "suite_question_count": 105,
        "case_selection_file_sha256": selection_file_sha256,
        "case_selection_sha256": selection["selection_sha256"],
        "case_selection_method": _SELECTION_METHOD,
        "selected_case_ids": selected_ids,
        "results": [{"id": case_id} for case_id in selected_ids],
    }
    gold = {
        "suite_id": predictions["suite_id"],
        "questions_sha256": question_sha256,
        "cases": [{"id": case_id} for case_id in all_ids],
    }
    captured: dict[str, object] = {}

    def fake_compare_phase2(*args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"count": len(kwargs["selected_case_ids"])}

    monkeypatch.setattr(sealed_comparator, "_compare_phase2", fake_compare_phase2)

    report = sealed_comparator.compare(
        predictions,
        gold,
        allow_post_gold_development=True,
        case_selection=selection,
        case_selection_file_sha256=selection_file_sha256,
    )

    assert report == {"count": 100}
    assert captured["selected_case_ids"] == selected_ids

    predictions["case_selection_sha256"] = "5" * 64
    with pytest.raises(RuntimeError, match="provenance differs"):
        sealed_comparator.compare(
            predictions,
            gold,
            allow_post_gold_development=True,
            case_selection=selection,
            case_selection_file_sha256=selection_file_sha256,
        )


def test_comparator_scores_exact_external_evidence_and_grounding_fields() -> None:
    expected = {"required_evidence_ids": ["DOC-1"]}
    catalog = {
        "DOC-1": {
            "id": "DOC-1",
            "url": "https://official.example/report",
            "publication_date": "2026-08-24",
            "required_evidence_spans": ["공식 근거 문장"],
        }
    }
    response = {
        "documents": [
            {
                "source_uri": "https://official.example/report",
                "as_of": "2026-08-24",
                "text": "앞 문장. 공식 근거 문장. 뒤 문장.",
            }
        ],
        "hclx_final_grounding_violations": [],
    }

    assert _external_exact_metrics(
        expected,
        response,
        catalog_documents=catalog,
    ) == {
        "external_source_uri_exact": True,
        "external_publication_date_exact": True,
        "external_evidence_span_exact": True,
    }
    assert _hclx_final_grounding_violations({}, response) == 0

    response["documents"][0]["text"] = "근거 문장이 없음"
    assert (
        _external_exact_metrics(
            expected,
            response,
            catalog_documents=catalog,
        )["external_evidence_span_exact"]
        is False
    )


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
