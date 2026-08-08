from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_question_runner import (
    load_coverage_question_batch,
)
from finance_agent_core.evaluation.coverage_questions import (
    coverage_question_batch_semantic_sha256,
    merge_coverage_question_batches,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge non-overlapping coverage question checkpoints after validating "
            "their plan suite, generator, model, screen, and axes."
        )
    )
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    batches = [load_coverage_question_batch(path) for path in arguments.input]
    merged = merge_coverage_question_batches(batches)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{merged.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "shards": len(batches),
                "selected_sources": merged.selected_source_count,
                "requested": merged.requested_count,
                "generated": merged.generated_count,
                "accepted": merged.accepted_count,
                "rejected": merged.rejected_count,
                "generation_failures": merged.generation_failure_count,
                "semantic_sha256": coverage_question_batch_semantic_sha256(merged),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
