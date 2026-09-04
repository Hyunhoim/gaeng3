from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.schema_dense_residual_holdout import (
    evaluate_residual_holdout,
    finalize_report,
)
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate Schema Dense corpus v1/v2/v3 on the developer residual holdout."
    )
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    provider = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.snapshot_manifest,
        trusted_cache_root=arguments.trusted_cache_root,
        mode="shadow",
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
    )
    report = finalize_report(evaluate_residual_holdout(provider))
    serialized = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if arguments.output is not None:
        if not arguments.output.is_absolute() or not arguments.output.parent.is_dir():
            raise SystemExit("output must be an absolute path in an existing directory")
        try:
            with arguments.output.open("x", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
        except FileExistsError:
            raise SystemExit("output already exists") from None
        print(
            json.dumps(
                {
                    "activation_decision": report["activation_decision"],
                    "output": str(arguments.output),
                    "report_sha256": report["report_sha256"],
                },
                sort_keys=True,
            )
        )
    else:
        print(serialized)


if __name__ == "__main__":
    main()
