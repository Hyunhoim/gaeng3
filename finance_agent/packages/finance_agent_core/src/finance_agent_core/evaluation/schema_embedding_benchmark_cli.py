from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_benchmark import (
    run_schema_embedding_benchmark,
    schema_embedding_report_fingerprint,
)
from finance_agent_core.evaluation.schema_embedding_models import (
    SentenceTransformerCpuProvider,
    load_schema_embedding_model_registry,
    render_model_registry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a frozen Schema Dense embedding model on CPU against the public suite."
    )
    parser.add_argument("--model", help="Pinned candidate alias from the model registry")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--allow-remote-code", action="store_true")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--cpu-threads", type=int, default=12)
    parser.add_argument(
        "--fusion-strategy",
        choices=("rrf", "lexical_first"),
        default="rrf",
    )
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--dense-weight", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-contract", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    registry = load_schema_embedding_model_registry()
    if arguments.list_models:
        print(render_model_registry())
        return 0
    if not arguments.model:
        raise SystemExit("--model is required unless --list-models is used")
    spec = registry.require(arguments.model)
    if spec.trust_remote_code and not arguments.allow_remote_code:
        raise SystemExit(
            f"{spec.alias} requires pinned remote code; review it and pass --allow-remote-code"
        )
    fusion_suffix = "" if arguments.fusion_strategy == "rrf" else "-lexical-first"
    output = arguments.output or Path(
        f"artifacts/evaluation/schema-embedding/{spec.alias}-cpu-public-v1{fusion_suffix}.json"
    )
    provider = SentenceTransformerCpuProvider(
        spec,
        batch_size=arguments.batch_size,
        cpu_threads=arguments.cpu_threads,
        cache_dir=arguments.cache_dir,
        local_files_only=arguments.local_files_only,
    )
    smoke = provider.smoke_probe()
    if arguments.smoke_only:
        payload = {
            "status": "smoke_only",
            "model": spec.model_dump(mode="json"),
            "model_load_ms": round(provider.model_load_ms, 6),
            "smoke_probe": smoke,
            "library_versions": provider.library_versions,
            "device": "cpu",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
            encoding="utf-8",
        )
        print(json.dumps({**payload, "output": str(output)}, ensure_ascii=False, indent=2))
        return 0 if smoke["finite"] else 2

    report = run_schema_embedding_benchmark(
        provider,
        smoke_probe=smoke,
        fusion_strategy=arguments.fusion_strategy,
        lexical_weight=arguments.lexical_weight,
        dense_weight=arguments.dense_weight,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    summary = {
        "evaluation_id": report.evaluation_id,
        "model": report.model.alias,
        "revision": report.model.revision,
        "fusion": report.fusion.model_dump(mode="json"),
        "report_sha256_without_timestamp": schema_embedding_report_fingerprint(report),
        "lexical_recall_at_5": report.lexical.micro_recall_at_5,
        "dense_recall_at_5": report.dense.micro_recall_at_5,
        "hybrid_recall_at_5": report.lexical_plus_dense_rrf.micro_recall_at_5,
        "hybrid_exact": report.lexical_plus_dense_rrf.exact_at_gold_cardinality,
        "hybrid_recall_delta": report.hybrid_minus_lexical.micro_recall_at_5,
        "hybrid_exact_delta": report.hybrid_minus_lexical.exact_at_gold_cardinality,
        "dense_recovery_rate_at_5": report.missed_field_recovery.dense_recovery_rate_at_5,
        "dense_p95_ms": report.runtime.dense_latency.p95_ms,
        "eligible_for_blind_evaluation": report.decision.eligible_for_blind_evaluation,
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if arguments.require_contract and not report.decision.eligible_for_blind_evaluation:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
