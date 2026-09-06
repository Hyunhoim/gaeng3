from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path

from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactError,
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.corpus import CorpusApprovalError, verify_approved_corpus
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseError,
    build_approved_document_dense_artifact,
    build_document_cache_activation_receipt,
    load_approved_document_dense_artifact,
    write_approved_document_dense_artifact,
    write_document_cache_activation_receipt,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MODEL_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_MAX_CALIBRATION_REPORT_BYTES = 16 * 1024 * 1024


def _require_absolute_path(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")


def _require_digest(value: str, *, label: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lower-case SHA-256")
    return value


def _read_exact_regular_file(
    path: Path,
    *,
    expected_sha256: str,
    label: str,
    max_bytes: int,
) -> bytes:
    _require_absolute_path(path, label=label)
    _require_digest(expected_sha256, label=f"{label} expected digest")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise ValueError(f"{label} must be one bounded regular file")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} changed while reading")
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError(f"{label} changed while reading")
        data = b"".join(chunks)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(f"{label} differs from its expected SHA-256")
    return data


def _verify_calibration_report(data: bytes) -> tuple[str, float, float]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("calibration report contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except ValueError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("calibration report is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("calibration report must be a JSON object")
    if payload.get("report_kind") != "document_dense_public_calibration_not_blind":
        raise ValueError("calibration report is not the public Document Dense calibration")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("hybrid_rrf"), dict):
        raise ValueError("calibration report lacks hybrid retrieval metrics")
    subject = payload.get("artifact_sha256")
    if not isinstance(subject, str) or _SHA256_PATTERN.fullmatch(subject) is None:
        raise ValueError("calibration report lacks its evaluated artifact SHA-256")
    policy = payload.get("selected_policy")
    if not isinstance(policy, dict):
        raise ValueError("calibration report lacks its selected admission policy")
    minimum_dense_score = policy.get("minimum_dense_score")
    minimum_dense_margin = policy.get("minimum_dense_margin")
    if not isinstance(minimum_dense_score, (int, float)) or isinstance(
        minimum_dense_score, bool
    ):
        raise ValueError("calibration report dense score policy is invalid")
    if not isinstance(minimum_dense_margin, (int, float)) or isinstance(
        minimum_dense_margin, bool
    ):
        raise ValueError("calibration report dense margin policy is invalid")
    gate = payload.get("activation_gate")
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        raise ValueError("calibration report did not pass its activation gate")
    return subject, float(minimum_dense_score), float(minimum_dense_margin)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an immutable KURE Document Dense artifact and activation receipt from "
            "an already approved, independently reviewed corpus."
        )
    )
    parser.add_argument("--approved-corpus-manifest", type=Path, required=True)
    parser.add_argument("--approved-corpus-manifest-sha256", required=True)
    parser.add_argument("--approved-corpus-root", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--snapshot-manifest-sha256", required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--expected-model-revision", required=True)
    parser.add_argument("--source-registry-sha256", required=True)
    parser.add_argument("--calibration-report", type=Path)
    parser.add_argument("--calibration-report-sha256")
    parser.add_argument("--minimum-dense-score", type=float)
    parser.add_argument("--minimum-dense-margin", type=float)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=800)
    parser.add_argument("--overlap-chars", type=int, default=80)
    parser.add_argument("--artifact-output", type=Path)
    parser.add_argument("--activation-output", type=Path)
    parser.add_argument(
        "--candidate-only",
        action="store_true",
        help="Write the immutable approved artifact before calibration; never activate it.",
    )
    parser.add_argument("--candidate-artifact", type=Path)
    parser.add_argument("--candidate-artifact-sha256")
    return parser


def _require_argument(value, *, label: str):
    if value is None:
        raise ValueError(f"{label} is required for the selected operation")
    return value


def _build(arguments: argparse.Namespace) -> None:
    paths = [
        (arguments.approved_corpus_manifest, "approved corpus manifest"),
        (arguments.approved_corpus_root, "approved corpus root"),
        (arguments.snapshot_dir, "KURE snapshot"),
        (arguments.snapshot_manifest, "KURE snapshot manifest"),
        (arguments.trusted_cache_root, "trusted KURE cache root"),
    ]
    if arguments.artifact_output is not None:
        paths.append((arguments.artifact_output, "approved Document Dense output"))
    if arguments.candidate_artifact is not None:
        paths.append((arguments.candidate_artifact, "approved Document Dense candidate"))
    if arguments.calibration_report is not None:
        paths.append((arguments.calibration_report, "calibration report"))
    if arguments.activation_output is not None:
        paths.append((arguments.activation_output, "activation receipt output"))
    for path, label in paths:
        _require_absolute_path(path, label=label)
    if (
        arguments.artifact_output is not None
        and arguments.artifact_output == arguments.activation_output
    ):
        raise ValueError("artifact and activation outputs must differ")

    activation_only = arguments.candidate_artifact is not None
    if arguments.candidate_only and activation_only:
        raise ValueError("candidate-only and candidate-artifact operations are mutually exclusive")
    if arguments.candidate_only:
        _require_argument(arguments.artifact_output, label="artifact output")
        if any(
            item is not None
            for item in (
                arguments.calibration_report,
                arguments.calibration_report_sha256,
                arguments.minimum_dense_score,
                arguments.minimum_dense_margin,
                arguments.activation_output,
                arguments.candidate_artifact_sha256,
            )
        ):
            raise ValueError("candidate-only cannot receive calibration or activation arguments")
    else:
        _require_argument(arguments.calibration_report, label="calibration report")
        _require_argument(
            arguments.calibration_report_sha256,
            label="calibration report SHA-256",
        )
        _require_argument(arguments.minimum_dense_score, label="minimum dense score")
        _require_argument(arguments.minimum_dense_margin, label="minimum dense margin")
        _require_argument(arguments.activation_output, label="activation output")
        if activation_only:
            _require_argument(
                arguments.candidate_artifact_sha256,
                label="candidate artifact SHA-256",
            )
            if arguments.artifact_output is not None:
                raise ValueError("activation-only cannot write a second artifact")
        else:
            _require_argument(arguments.artifact_output, label="artifact output")

    expected_corpus_sha256 = _require_digest(
        arguments.approved_corpus_manifest_sha256,
        label="approved corpus manifest digest",
    )
    expected_snapshot_manifest_sha256 = _require_digest(
        arguments.snapshot_manifest_sha256,
        label="KURE snapshot manifest digest",
    )
    expected_registry_sha256 = _require_digest(
        arguments.source_registry_sha256,
        label="Source Registry digest",
    )
    if _MODEL_REVISION_PATTERN.fullmatch(arguments.expected_model_revision) is None:
        raise ValueError("expected KURE model revision must be a lower-case 40-hex commit")

    corpus = verify_approved_corpus(
        arguments.approved_corpus_manifest,
        arguments.approved_corpus_root,
    )
    if corpus.manifest_sha256 != expected_corpus_sha256:
        raise ValueError("approved corpus manifest differs from its expected SHA-256")

    registry = load_official_source_registry()
    if registry.canonical_sha256 != expected_registry_sha256:
        raise ValueError("Source Registry differs from its expected SHA-256")

    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.snapshot_manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        mode="production",
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
    )
    gate = provider.artifact_gate_evidence
    if gate.manifest_file_sha256 != expected_snapshot_manifest_sha256:
        raise ValueError("KURE snapshot manifest differs from its expected SHA-256")
    if provider.metadata.model_revision != arguments.expected_model_revision:
        raise ValueError("loaded KURE model revision differs from its expected revision")

    if activation_only:
        candidate_sha256 = _require_digest(
            arguments.candidate_artifact_sha256,
            label="candidate artifact digest",
        )
        artifact = load_approved_document_dense_artifact(
            arguments.candidate_artifact,
            expected_file_sha256=candidate_sha256,
        )
        if (
            artifact.manifest.source_manifest_sha256 != corpus.manifest_sha256
            or artifact.manifest.source_text_manifest_sha256
            != corpus.manifest.file_manifest_sha256
        ):
            raise ValueError("candidate artifact is not bound to this approved corpus")
        if (
            artifact.manifest.embedding_snapshot_manifest_sha256 != gate.manifest_file_sha256
            or artifact.manifest.provider != provider.metadata
        ):
            raise ValueError("candidate artifact is not bound to this frozen KURE snapshot")
    else:
        artifact = build_approved_document_dense_artifact(
            corpus,
            provider=provider,
            embedding_snapshot_manifest_sha256=gate.manifest_file_sha256,
            max_chars=arguments.max_chars,
            overlap_chars=arguments.overlap_chars,
        )

    if arguments.candidate_only:
        artifact_sha256 = write_approved_document_dense_artifact(
            artifact,
            arguments.artifact_output,
        )
        print(f"approved_corpus_manifest_sha256={corpus.manifest_sha256}")
        print(f"source_registry_sha256={registry.canonical_sha256}")
        print(f"embedding_snapshot_manifest_sha256={gate.manifest_file_sha256}")
        print(f"embedding_model_revision={provider.metadata.model_revision}")
        print(f"document_dense_artifact_sha256={artifact_sha256}")
        print(f"document_dense_vector_payload_sha256={artifact.manifest.vector_payload_sha256}")
        print("activation_status=candidate_only_not_activated")
        return

    calibration_sha256 = _require_digest(
        arguments.calibration_report_sha256,
        label="calibration report digest",
    )
    calibration_data = _read_exact_regular_file(
        arguments.calibration_report,
        expected_sha256=calibration_sha256,
        label="calibration report",
        max_bytes=_MAX_CALIBRATION_REPORT_BYTES,
    )
    (
        calibrated_artifact_sha256,
        calibrated_minimum_score,
        calibrated_minimum_margin,
    ) = _verify_calibration_report(calibration_data)
    if calibrated_artifact_sha256 != artifact.canonical_sha256:
        raise ValueError(
            "calibration report was not produced from this approved Document Dense artifact"
        )
    if (
        calibrated_minimum_score != arguments.minimum_dense_score
        or calibrated_minimum_margin != arguments.minimum_dense_margin
    ):
        raise ValueError("activation policy differs from the calibrated policy")
    activation = build_document_cache_activation_receipt(
        corpus,
        artifact,
        registry=registry,
        calibration_report_sha256=calibration_sha256,
        minimum_dense_score=arguments.minimum_dense_score,
        minimum_dense_margin=arguments.minimum_dense_margin,
        top_k=arguments.top_k,
    )
    artifact_sha256 = artifact.canonical_sha256
    if not activation_only:
        artifact_sha256 = write_approved_document_dense_artifact(
            artifact,
            arguments.artifact_output,
        )
    activation_sha256 = write_document_cache_activation_receipt(
        activation,
        arguments.activation_output,
    )

    print(f"approved_corpus_manifest_sha256={corpus.manifest_sha256}")
    print(f"approved_corpus_file_manifest_sha256={corpus.manifest.file_manifest_sha256}")
    print(f"source_registry_sha256={registry.canonical_sha256}")
    print(f"embedding_snapshot_manifest_sha256={gate.manifest_file_sha256}")
    print(f"embedding_snapshot_file_manifest_sha256={gate.snapshot_file_manifest_sha256}")
    print(f"embedding_model_id={provider.metadata.model_id}")
    print(f"embedding_model_revision={provider.metadata.model_revision}")
    print(f"calibration_report_sha256={calibration_sha256}")
    print(f"document_dense_artifact_sha256={artifact_sha256}")
    print(f"document_dense_vector_payload_sha256={artifact.manifest.vector_payload_sha256}")
    print(f"document_bm25_sha256={activation.document_bm25_sha256}")
    print(f"document_cache_activation_sha256={activation_sha256}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        _build(arguments)
    except (
        CorpusApprovalError,
        DocumentDenseError,
        SchemaEmbeddingArtifactError,
        OSError,
        ValueError,
    ) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
