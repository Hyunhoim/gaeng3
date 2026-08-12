from __future__ import annotations

import argparse
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_analysis import (
    build_schema_embedding_statistical_analysis,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze paired uncertainty and failures in Schema Dense CPU reports."
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20_260_812)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = build_schema_embedding_statistical_analysis(
        arguments.artifact_dir,
        iterations=arguments.iterations,
        seed=arguments.seed,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
