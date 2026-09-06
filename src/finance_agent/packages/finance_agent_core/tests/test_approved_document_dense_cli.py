from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.approved_document_dense_cli import main
from finance_agent_core.retrieval.corpus import (
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    ExternalCorpusIntakeSpec,
    approved_corpus_manifest_bytes,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
)
from finance_agent_core.retrieval.document_dense import (
    build_approved_document_dense_artifact,
    load_approved_document_dense_artifact,
    load_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata

_MODEL_REVISION = "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"


class _FakeKureProvider:
    def __init__(self, *, manifest_sha256: str) -> None:
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="verified_sentence_transformers_kure_v1",
            model_id="nlpai-lab/KURE-v1",
            model_revision=_MODEL_REVISION,
            license_id="cc-by-nc-sa-4.0",
            dimension=8,
            pooling="cls",
        )
        self.artifact_gate_evidence = SimpleNamespace(
            manifest_file_sha256=manifest_sha256,
            snapshot_file_manifest_sha256="a" * 64,
        )

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    @staticmethod
    def _embed(text: str) -> list[float]:
        values = [float(item + 1) for item in hashlib.sha256(text.encode()).digest()[:8]]
        norm = math.sqrt(sum(item * item for item in values))
        return [item / norm for item in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def _approved_corpus(tmp_path):
    root = tmp_path / "approved-corpus"
    documents = root / "documents"
    documents.mkdir(parents=True)
    (documents / "fsc.txt").write_text(
        "국민성장펀드는 정책금융과 민간자금이 함께 참여하는 투자구조입니다.",
        encoding="utf-8",
    )
    spec = ExternalCorpusIntakeSpec(
        corpus_id="approved-document-dense-cli-test",
        reviews=(
            CorpusReview(
                reviewer_role="data_rights",
                reviewer_id="rights-reviewer",
                reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                note="공식 문서의 저장 및 평가 사용 권리를 검토함",
            ),
            CorpusReview(
                reviewer_role="finance_domain",
                reviewer_id="finance-reviewer",
                reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                note="펀드 구조 설명의 금융 근거 적합성을 검토함",
            ),
        ),
        documents=(
            CorpusDocumentSpec(
                document_id="national-growth-fund-structure",
                relative_path="documents/fsc.txt",
                title="국민성장펀드 투자구조",
                publisher="금융위원회",
                source_registry_id="fsc-policy",
                source_uri="https://www.fsc.go.kr/po010103/86950",
                capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                collected_at_utc=datetime(2026, 8, 20, tzinfo=UTC),
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
                language="ko",
                media_type="text/plain",
                purposes=("product_structure",),
                license=CorpusLicenseApproval(
                    license_id="test-open-license",
                    license_uri="https://www.fsc.go.kr/copyright",
                    storage_allowed=True,
                    retrieval_allowed=True,
                    competition_use_allowed=True,
                    deployment_bundle_allowed=True,
                    attribution_text="금융위원회 공식 공개 자료",
                ),
            ),
        ),
    )
    manifest = seal_approved_corpus_manifest(spec, root)
    manifest_path = root / "approved-manifest.json"
    manifest_data = approved_corpus_manifest_bytes(manifest)
    manifest_path.write_bytes(manifest_data)
    return root, manifest_path, hashlib.sha256(manifest_data).hexdigest(), spec


def _inputs(tmp_path, monkeypatch):
    corpus_root, corpus_manifest, corpus_sha256, spec = _approved_corpus(tmp_path)
    snapshot_root = tmp_path / "kure-cache"
    snapshot_dir = snapshot_root / "snapshot"
    snapshot_dir.mkdir(parents=True)
    snapshot_manifest = tmp_path / "kure-snapshot-manifest.json"
    snapshot_manifest.write_text('{"fixture":"snapshot"}\n', encoding="utf-8")
    snapshot_sha256 = hashlib.sha256(snapshot_manifest.read_bytes()).hexdigest()
    provider = _FakeKureProvider(manifest_sha256=snapshot_sha256)
    approved = verify_approved_corpus(corpus_manifest, corpus_root)
    calibrated_artifact = build_approved_document_dense_artifact(
        approved,
        provider=provider,
        embedding_snapshot_manifest_sha256=snapshot_sha256,
        max_chars=800,
        overlap_chars=80,
    )
    calibration = tmp_path / "document-dense-calibration.json"
    calibration.write_text(
        json.dumps(
            {
                "artifact_sha256": calibrated_artifact.canonical_sha256,
                "activation_gate": {
                    "minimum_hybrid_recall_at_5": 0.8,
                    "passed": True,
                },
                "metrics": {"hybrid_rrf": {"recall_at_5": 1.0}},
                "report_kind": "document_dense_public_calibration_not_blind",
                "selected_policy": {
                    "minimum_dense_margin": 0.03,
                    "minimum_dense_score": 0.55,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    calibration_sha256 = hashlib.sha256(calibration.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "finance_agent_core.retrieval.approved_document_dense_cli."
        "load_verified_schema_embedding_cpu_provider",
        lambda **_kwargs: provider,
    )
    artifact_output = tmp_path / "approved-document-dense.json"
    activation_output = tmp_path / "document-cache-activation.json"
    arguments = [
        "--approved-corpus-manifest",
        str(corpus_manifest),
        "--approved-corpus-manifest-sha256",
        corpus_sha256,
        "--approved-corpus-root",
        str(corpus_root),
        "--snapshot-dir",
        str(snapshot_dir),
        "--snapshot-manifest",
        str(snapshot_manifest),
        "--snapshot-manifest-sha256",
        snapshot_sha256,
        "--trusted-cache-root",
        str(snapshot_root),
        "--expected-model-revision",
        _MODEL_REVISION,
        "--source-registry-sha256",
        load_official_source_registry().canonical_sha256,
        "--calibration-report",
        str(calibration),
        "--calibration-report-sha256",
        calibration_sha256,
        "--minimum-dense-score",
        "0.55",
        "--minimum-dense-margin",
        "0.03",
        "--artifact-output",
        str(artifact_output),
        "--activation-output",
        str(activation_output),
    ]
    return arguments, artifact_output, activation_output, corpus_sha256, snapshot_sha256, spec


def test_cli_builds_exact_approved_kure_artifact_and_activation(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    arguments, artifact_path, activation_path, corpus_sha, snapshot_sha, _ = _inputs(
        tmp_path,
        monkeypatch,
    )

    assert main(arguments) == 0

    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    activation_sha = hashlib.sha256(activation_path.read_bytes()).hexdigest()
    artifact = load_approved_document_dense_artifact(
        artifact_path,
        expected_file_sha256=artifact_sha,
    )
    receipt = load_document_cache_activation_receipt(
        activation_path,
        expected_file_sha256=activation_sha,
    )
    assert artifact.manifest.source_manifest_sha256 == corpus_sha
    assert artifact.manifest.embedding_snapshot_manifest_sha256 == snapshot_sha
    assert artifact.manifest.provider.model_id == "nlpai-lab/KURE-v1"
    assert receipt.document_dense_artifact_sha256 == artifact_sha
    assert receipt.approved_corpus_manifest_sha256 == corpus_sha
    assert receipt.embedding_snapshot_manifest_sha256 == snapshot_sha
    assert receipt.provider.model_revision == _MODEL_REVISION
    assert receipt.minimum_dense_score == 0.55
    assert receipt.minimum_dense_margin == 0.03
    assert stat_mode(artifact_path) == 0o444
    assert stat_mode(activation_path) == 0o444
    output = capsys.readouterr().out
    assert f"document_dense_artifact_sha256={artifact_sha}" in output
    assert f"document_cache_activation_sha256={activation_sha}" in output


def stat_mode(path) -> int:
    return path.stat().st_mode & 0o777


def _without_options(arguments: list[str], *names: str) -> list[str]:
    result = list(arguments)
    for name in names:
        index = result.index(name)
        del result[index : index + 2]
    return result


def test_cli_separates_candidate_calibration_and_activation(
    tmp_path,
    monkeypatch,
) -> None:
    arguments, artifact_path, activation_path, _, _, _ = _inputs(tmp_path, monkeypatch)
    candidate_arguments = _without_options(
        arguments,
        "--calibration-report",
        "--calibration-report-sha256",
        "--minimum-dense-score",
        "--minimum-dense-margin",
        "--activation-output",
    )
    candidate_arguments.append("--candidate-only")

    assert main(candidate_arguments) == 0
    assert artifact_path.exists()
    assert not activation_path.exists()
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    activation_arguments = _without_options(arguments, "--artifact-output")
    activation_arguments.extend(
        (
            "--candidate-artifact",
            str(artifact_path),
            "--candidate-artifact-sha256",
            artifact_sha256,
        )
    )
    assert main(activation_arguments) == 0
    assert activation_path.exists()
    receipt_sha256 = hashlib.sha256(activation_path.read_bytes()).hexdigest()
    receipt = load_document_cache_activation_receipt(
        activation_path,
        expected_file_sha256=receipt_sha256,
    )
    assert receipt.document_dense_artifact_sha256 == artifact_sha256


def test_cli_rejects_unsealed_intake_in_place_of_approved_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    arguments, artifact_path, activation_path, _, _, spec = _inputs(tmp_path, monkeypatch)
    manifest_index = arguments.index("--approved-corpus-manifest") + 1
    sha_index = arguments.index("--approved-corpus-manifest-sha256") + 1
    intake_path = tmp_path / "reviewed-intake-not-sealed.json"
    intake_path.write_text(spec.model_dump_json() + "\n", encoding="utf-8")
    arguments[manifest_index] = str(intake_path)
    arguments[sha_index] = hashlib.sha256(intake_path.read_bytes()).hexdigest()

    with pytest.raises(SystemExit, match="approved corpus manifest violates the schema"):
        main(arguments)

    assert not artifact_path.exists()
    assert not activation_path.exists()


def test_cli_rejects_any_explicit_trust_anchor_mismatch(tmp_path, monkeypatch) -> None:
    arguments, artifact_path, activation_path, _, _, _ = _inputs(tmp_path, monkeypatch)
    sha_index = arguments.index("--calibration-report-sha256") + 1
    arguments[sha_index] = "0" * 64

    with pytest.raises(SystemExit, match="calibration report differs from its expected SHA-256"):
        main(arguments)

    assert not artifact_path.exists()
    assert not activation_path.exists()
