from __future__ import annotations

import hashlib
import json

from finance_agent_core.evaluation.external_knowledge_preblind import (
    build_external_knowledge_preblind_report,
)
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseArtifact,
    DocumentDenseChunk,
    DocumentDenseManifest,
    DocumentDenseVector,
    document_dense_artifact_bytes,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata


def test_preblind_gate_accepts_release_approved_live_activation(tmp_path) -> None:
    text = "공식 문서의 공개 calibration 전용 chunk"
    vector = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    chunk = DocumentDenseChunk(
        chunk_id="official-document:0000",
        document_id="official-document",
        ordinal=0,
        title="official",
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    record = DocumentDenseVector(chunk=chunk, vector=vector)
    records = (record,)
    vector_hash = hashlib.sha256(
        (
            json.dumps(
                [record.model_dump(mode="json")],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    ).hexdigest()
    artifact = DocumentDenseArtifact(
        manifest=DocumentDenseManifest(
            source_manifest_sha256="a" * 64,
            source_text_manifest_sha256="b" * 64,
            embedding_snapshot_manifest_sha256="c" * 64,
            max_chars=800,
            overlap_chars=80,
            document_count=1,
            chunk_count=1,
            provider=EmbeddingProviderMetadata(
                provider_id="verified_sentence_transformers_kure_v1",
                provider_kind="frozen_model",
                model_id="nlpai-lab/KURE-v1",
                model_revision="d14c8a9423946e268a0c9952fecf3a7aabd73bd9",
                dimension=8,
                pooling="cls",
                normalization="l2",
                similarity="cosine",
                license_id="mit",
            ),
            vector_payload_sha256=vector_hash,
        ),
        records=records,
    )
    artifact_path = tmp_path / "document-dense.json"
    artifact_path.write_bytes(document_dense_artifact_bytes(artifact))
    calibration_path = tmp_path / "public-calibration.json"
    calibration_path.write_text(
        json.dumps(
            {
                "report_kind": "document_dense_public_calibration_not_blind",
                "artifact_sha256": artifact.canonical_sha256,
                "metrics": {"dense": {"recall_at_5": 1.0}},
            }
        )
    )

    report = build_external_knowledge_preblind_report(
        document_dense_artifact_path=artifact_path,
        public_calibration_report_path=calibration_path,
    )

    assert report.status == "ready_for_independent_blind"
    assert report.independent_blind_executed is False
    assert report.production_execution_enabled is True
    assert report.checks["coverage_execution_is_approved_live_official"]
    assert report.blockers == ()
