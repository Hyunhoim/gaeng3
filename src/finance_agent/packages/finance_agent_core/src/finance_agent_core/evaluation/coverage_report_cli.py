from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_ablation import CoverageAblationReport
from finance_agent_core.evaluation.coverage_questions import CoverageQuestionBatch
from finance_agent_core.evaluation.coverage_report import render_coverage_experiment_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Render a concise Markdown report from a coverage ablation and optional "
            "Qwen question-generation batch."
        )
    )
    parser.add_argument("--ablation", type=Path, required=True)
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-changes", type=int, default=15)
    parser.add_argument("--review-examples", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = CoverageAblationReport.model_validate_json(
        arguments.ablation.read_text(encoding="utf-8")
    )
    question_batch = (
        None
        if arguments.questions is None
        else CoverageQuestionBatch.model_validate_json(
            arguments.questions.read_text(encoding="utf-8")
        )
    )
    rendered = render_coverage_experiment_markdown(
        report,
        question_batch=question_batch,
        top_changes=arguments.top_changes,
        review_examples=arguments.review_examples,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "source_kind": report.source_kind,
                "baseline": report.baseline_label,
                "profiles": [profile.label for profile in report.profiles],
                "question_generation_included": question_batch is not None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
