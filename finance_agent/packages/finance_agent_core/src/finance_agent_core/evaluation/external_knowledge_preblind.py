from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.config.knowledge_coverage import (
    KnowledgeCapability,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.document_dense import load_document_dense_artifact


class ExternalPreblindModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalKnowledgePreblindReport(ExternalPreblindModel):
    schema_version: Literal["1.0"] = "1.0"
    report_kind: Literal["external_knowledge_preblind_readiness"] = (
        "external_knowledge_preblind_readiness"
    )
    status: Literal["ready_for_independent_blind", "blocked"]
    independent_blind_executed: Literal[False] = False
    production_execution_enabled: bool
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_dense_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_calibration_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: dict[str, bool]
    public_metrics: dict[str, dict[str, float]]
    blockers: tuple[str, ...]


def build_external_knowledge_preblind_report(
    *,
    document_dense_artifact_path: str | Path,
    public_calibration_report_path: str | Path,
) -> ExternalKnowledgePreblindReport:
    """Verify pre-blind mechanics without opening any private question or Gold file."""

    artifact = load_document_dense_artifact(document_dense_artifact_path)
    report_path = Path(public_calibration_report_path)
    report_bytes = report_path.read_bytes()
    try:
        calibration = json.loads(report_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("public Document Dense calibration report is invalid") from error
    metrics = calibration.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("public Document Dense calibration metrics are missing")
    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    production_execution_enabled = any(
        item.runtime_execution_enabled for item in coverage.capabilities
    )

    source_capabilities = {
        capability
        for entry in registry.entries
        if entry.status == "approved_for_live_evidence"
        for capability in entry.capabilities
    }
    dense_metrics = metrics.get("dense") if isinstance(metrics, dict) else None
    dense_recall_at_5 = (
        dense_metrics.get("recall_at_5") if isinstance(dense_metrics, dict) else None
    )
    checks = {
        "all_capabilities_have_live_source_scope": source_capabilities == set(KnowledgeCapability),
        "coverage_execution_is_approved_live_official": all(
            item.status == "approved_live_official" and item.runtime_execution_enabled
            for item in coverage.capabilities
        ),
        "document_dense_is_frozen_kure": (
            artifact.manifest.provider.model_id == "nlpai-lab/KURE-v1"
            and len(artifact.manifest.provider.model_revision) == 40
            and artifact.manifest.provider.provider_kind == "frozen_model"
        ),
        "document_dense_is_offline_only": (
            artifact.manifest.scope == "offline_evaluation_only"
            and not artifact.manifest.production_enabled
        ),
        "calibration_is_public_not_blind": calibration.get("report_kind")
        == "document_dense_public_calibration_not_blind",
        "calibration_binds_exact_artifact": calibration.get("artifact_sha256")
        == artifact.canonical_sha256,
        "public_dense_recall_at_5_meets_candidate_gate": (
            isinstance(dense_recall_at_5, int | float) and dense_recall_at_5 >= 0.98
        ),
    }
    blockers = tuple(sorted(name for name, passed in checks.items() if not passed))
    return ExternalKnowledgePreblindReport(
        status="ready_for_independent_blind" if not blockers else "blocked",
        production_execution_enabled=production_execution_enabled,
        source_registry_sha256=registry.canonical_sha256,
        coverage_manifest_sha256=coverage.canonical_sha256,
        document_dense_artifact_sha256=artifact.canonical_sha256,
        public_calibration_report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        checks=checks,
        public_metrics={
            str(channel): {
                str(metric): float(value)
                for metric, value in channel_metrics.items()
                if isinstance(value, int | float)
            }
            for channel, channel_metrics in metrics.items()
            if isinstance(channel_metrics, dict)
        },
        blockers=blockers,
    )


def _write_new(path: Path, report: ExternalKnowledgePreblindReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (report.model_dump_json(indent=2) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o444)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify external-knowledge readiness without reading the blind set."
    )
    parser.add_argument("--document-dense-artifact", type=Path, required=True)
    parser.add_argument("--public-calibration-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = build_external_knowledge_preblind_report(
        document_dense_artifact_path=arguments.document_dense_artifact,
        public_calibration_report_path=arguments.public_calibration_report,
    )
    _write_new(arguments.output, report)
    print(report.model_dump_json(indent=2))
    return 0 if report.status == "ready_for_independent_blind" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExternalKnowledgePreblindReport",
    "build_external_knowledge_preblind_report",
]
