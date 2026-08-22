from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    SchemaDenseActivationPolicy,
    approve_schema_index_for_production,
    build_schema_field_entries,
    dense_schema_index_file_bytes,
)


def _write_immutable(path: Path, data: bytes) -> str:
    if not path.is_absolute() or not path.parent.is_dir():
        raise SystemExit("Schema Dense output requires an absolute path in an existing directory")
    current = path.parent
    while True:
        if current.is_symlink():
            raise SystemExit("Schema Dense output parent cannot contain a symbolic link")
        if current.parent == current:
            break
        current = current.parent
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o444)
    except OSError as error:
        raise SystemExit("Schema Dense output already exists or cannot be created") from error
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        raise SystemExit("cannot durably record the Schema Dense output") from error
    return hashlib.sha256(data).hexdigest()


def _create(arguments: argparse.Namespace) -> None:
    policy = SchemaDenseActivationPolicy(
        dense_min_score=arguments.dense_min_score,
        hclx_candidate_min_score=arguments.hclx_candidate_min_score,
        minimum_margin=arguments.minimum_margin,
        top_k=arguments.top_k,
        maximum_residual_spans=arguments.maximum_residual_spans,
        queue_timeout_seconds=arguments.queue_timeout_seconds,
        calibration_report_sha256=arguments.calibration_report_sha256,
    )
    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.snapshot_manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        mode="production",
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
    )
    offline = DenseSchemaIndex.build(build_schema_field_entries(), provider)
    artifact = approve_schema_index_for_production(offline, policy)
    data = dense_schema_index_file_bytes(artifact)
    file_sha256 = _write_immutable(arguments.output, data)
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            artifact.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    print(f"schema_dense_index_sha256={file_sha256}")
    print(f"schema_dense_manifest_sha256={manifest_sha256}")
    print(f"schema_dense_policy_sha256={policy.policy_sha256}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one immutable KURE Schema Dense production-candidate index offline."
    )
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--calibration-report-sha256", required=True)
    parser.add_argument("--dense-min-score", type=float, required=True)
    parser.add_argument("--hclx-candidate-min-score", type=float, required=True)
    parser.add_argument("--minimum-margin", type=float, required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--maximum-residual-spans", type=int, default=1)
    parser.add_argument("--queue-timeout-seconds", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.set_defaults(handler=_create)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
