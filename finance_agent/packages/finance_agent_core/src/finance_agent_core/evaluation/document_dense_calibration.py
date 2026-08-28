from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.document_dense import (
    OfflineHybridDocumentIndex,
    load_document_dense_artifact,
)


class CalibrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentCalibrationCase(CalibrationModel):
    id: str = Field(pattern=r"^DOC-PUB-[0-9]{3}$")
    question: str = Field(min_length=3, max_length=500)
    expected_document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")


class DocumentCalibrationSuite(CalibrationModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: Literal["document-dense-public-calibration-v1"]
    suite_kind: Literal["public_calibration_not_blind"]
    cases: tuple[DocumentCalibrationCase, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_cases(self) -> DocumentCalibrationSuite:
        ids = [item.id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("document calibration case IDs must be unique")
        return self


def _metrics(ranks: list[int | None]) -> dict[str, float]:
    total = len(ranks)
    return {
        "top1_accuracy": sum(rank == 1 for rank in ranks) / total,
        "recall_at_3": sum(rank is not None and rank <= 3 for rank in ranks) / total,
        "recall_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / total,
        "mrr": sum(0.0 if rank is None else 1 / rank for rank in ranks) / total,
    }


def _rank(hits, expected_document_id: str) -> int | None:
    seen: set[str] = set()
    document_rank = 0
    for hit in hits:
        if hit.document_id in seen:
            continue
        seen.add(hit.document_id)
        document_rank += 1
        if hit.document_id == expected_document_id:
            return document_rank
    return None


def evaluate_document_dense_public_calibration(
    suite: DocumentCalibrationSuite,
    index: OfflineHybridDocumentIndex,
) -> dict[str, object]:
    ranks: dict[str, list[int | None]] = {"bm25": [], "dense": [], "hybrid_rrf": []}
    results: list[dict[str, object]] = []
    for case in suite.cases:
        channel_hits = {
            "bm25": index.bm25_search(case.question, top_k=20),
            "dense": index.dense_search(case.question, top_k=20),
            "hybrid_rrf": index.hybrid_search(case.question, top_k=20),
        }
        case_ranks = {
            channel: _rank(hits, case.expected_document_id)
            for channel, hits in channel_hits.items()
        }
        for channel, rank in case_ranks.items():
            ranks[channel].append(rank)
        results.append(
            {
                "id": case.id,
                "expected_document_id": case.expected_document_id,
                "document_ranks": case_ranks,
            }
        )
    return {
        "schema_version": "1.0",
        "report_kind": "document_dense_public_calibration_not_blind",
        "suite_id": suite.suite_id,
        "case_count": len(suite.cases),
        "metrics": {channel: _metrics(values) for channel, values in ranks.items()},
        "cases": results,
    }


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o444)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run public, non-blind Document Dense calibration."
    )
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu-threads", type=int, default=2)
    arguments = parser.parse_args()
    suite = DocumentCalibrationSuite.model_validate_json(arguments.suite.read_bytes())
    artifact = load_document_dense_artifact(arguments.artifact)
    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.snapshot_manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        mode="shadow",
        batch_size=8,
        cpu_threads=arguments.cpu_threads,
    )
    report = evaluate_document_dense_public_calibration(
        suite,
        OfflineHybridDocumentIndex(artifact, provider),
    )
    report["artifact_sha256"] = artifact.canonical_sha256
    data = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _write_new(arguments.output, data)
    print(json.dumps(report["metrics"], ensure_ascii=False, sort_keys=True))
    print(f"report_sha256={hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
