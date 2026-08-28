from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.document_dense import (
    build_pending_document_dense_artifact,
    write_document_dense_artifact,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a KURE Document Dense artifact that remains blocked from production "
            "until corpus approval and independent calibration."
        )
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=800)
    parser.add_argument("--overlap-chars", type=int, default=80)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    snapshot_manifest_sha256 = hashlib.sha256(arguments.snapshot_manifest.read_bytes()).hexdigest()
    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.snapshot_manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        mode="shadow",
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
    )
    artifact = build_pending_document_dense_artifact(
        arguments.corpus_root,
        provider=provider,
        embedding_snapshot_manifest_sha256=snapshot_manifest_sha256,
        max_chars=arguments.max_chars,
        overlap_chars=arguments.overlap_chars,
    )
    write_document_dense_artifact(artifact, arguments.output)
    print(f"document_dense_artifact_sha256={artifact.canonical_sha256}")
    print(f"source_manifest_sha256={artifact.manifest.source_manifest_sha256}")
    print(f"document_count={artifact.manifest.document_count}")
    print(f"chunk_count={artifact.manifest.chunk_count}")
    print("production_enabled=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
