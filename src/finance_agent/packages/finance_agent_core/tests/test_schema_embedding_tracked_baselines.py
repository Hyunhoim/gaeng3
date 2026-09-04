import hashlib
import json
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_analysis import (
    SchemaEmbeddingStatisticalAnalysis,
)


def _finance_agent_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_schema_embedding_tracked_baselines_are_internally_consistent() -> None:
    baseline_dir = _finance_agent_root() / "evaluation" / "baselines"
    summary = json.loads(
        (baseline_dir / "schema-embedding-cpu-public-v1.json").read_text(encoding="utf-8")
    )
    statistics_path = (
        _finance_agent_root()
        / "evaluation"
        / "analysis"
        / "schema-embedding-cpu-public-v1-statistics.json"
    )
    statistics = SchemaEmbeddingStatisticalAnalysis.model_validate_json(
        statistics_path.read_text(encoding="utf-8")
    )

    selected = summary["metrics"]
    selected_model = summary["lexical_first_all_models"][0]
    assert summary["status"] == statistics.status
    assert summary["selection"]["production_adoption"].startswith("blocked_")
    assert len(summary["lexical_first_all_models"]) == 7
    assert len(summary["selected_breakdown_by_family"]) == 4
    assert selected["strict_exact_passed"] == 175
    assert selected["hits_at_5"] == statistics.selected_hits_at_5
    assert selected["maximum_possible_hits_at_5"] == statistics.maximum_possible_hits_at_5
    assert selected["capacity_adjusted_recall_at_5"] == statistics.capacity_adjusted_recall_at_5
    assert selected_model["report_fingerprint"] == statistics.selected_report_fingerprint
    assert len(statistics.exact_failure_cases) == 6
    assert all(not item.missing_at_5 for item in statistics.exact_failure_cases)
    assert len(statistics.top_5_capacity_limited_case_ids) == 4

    reports = {item["role"]: item for item in summary["reports"]}
    assert (
        reports["paired_statistics"]["sha256"]
        == hashlib.sha256(statistics_path.read_bytes()).hexdigest()
    )
    selected_artifact = (
        _finance_agent_root()
        / "artifacts"
        / "evaluation"
        / "schema-embedding"
        / reports["selected_bge_m3_lexical_first_public_report"]["artifact_name"]
    )
    if selected_artifact.is_file():
        assert (
            reports["selected_bge_m3_lexical_first_public_report"]["sha256"]
            == hashlib.sha256(selected_artifact.read_bytes()).hexdigest()
        )
