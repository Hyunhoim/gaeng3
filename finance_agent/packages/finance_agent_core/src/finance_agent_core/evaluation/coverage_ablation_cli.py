from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_ablation import (
    ComparableCoverageReport,
    compare_coverage_profiles,
)
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionCampaignReport,
    CoverageQuestionRunReport,
)
from finance_agent_core.evaluation.coverage_runner import CoverageRunReport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare identical coverage questions across deterministic and local-Qwen "
            "Agent profiles, including rescues, regressions, stages, calls, and latency."
        )
    )
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="LABEL=REPORT_JSON; the first input is the baseline",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _load(path: Path) -> ComparableCoverageReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "campaign_id" in payload:
        return CoverageQuestionCampaignReport.model_validate(payload)
    if "variants" in payload:
        return CoverageQuestionRunReport.model_validate(payload)
    if "cases" in payload:
        return CoverageRunReport.model_validate(payload)
    raise ValueError(f"unsupported coverage report shape: {path}")


def _inputs(values: list[str]) -> dict[str, ComparableCoverageReport]:
    reports: dict[str, ComparableCoverageReport] = {}
    for value in values:
        label, separator, raw_path = value.partition("=")
        if not separator or not label or not raw_path:
            raise ValueError("coverage ablation input must use LABEL=REPORT_JSON")
        if label in reports:
            raise ValueError(f"duplicate coverage ablation label: {label}")
        reports[label] = _load(Path(raw_path))
    return reports


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    comparison = compare_coverage_profiles(_inputs(arguments.input))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{comparison.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "source_kind": comparison.source_kind,
                "baseline": comparison.baseline_label,
                "profiles": [
                    {
                        "label": item.label,
                        "agent_profile": item.agent_profile,
                        "passed": item.passed,
                        "total": item.total,
                        "strict_accuracy": item.strict_accuracy,
                        "strict_accuracy_ci95": item.strict_accuracy_ci95,
                        "plan_semantic_rate": item.plan_semantic_rate,
                        "evidence_semantic_rate": item.evidence_semantic_rate,
                        "fallback_count": item.fallback_count,
                        "latency_ms": item.latency_ms,
                    }
                    for item in comparison.profiles
                ],
                "pairwise_deltas": [
                    {
                        "candidate": item.candidate_label,
                        "strict_accuracy_delta": item.strict_accuracy_delta,
                        "strict_accuracy_delta_ci95": item.strict_accuracy_delta_ci95,
                        "rescued": item.rescued,
                        "regressed": item.regressed,
                        "zero_strict_regression": item.zero_strict_regression,
                        "plan_rescued": item.plan_rescued,
                        "plan_regressed": item.plan_regressed,
                        "evidence_rescued": item.evidence_rescued,
                        "evidence_regressed": item.evidence_regressed,
                        "mcnemar_exact_p_value": item.mcnemar_exact_p_value,
                        "holm_adjusted_p_value": item.holm_adjusted_p_value,
                        "statistically_significant_after_holm": (
                            item.statistically_significant_after_holm
                        ),
                        "top_net_rescues": {
                            dimension: [
                                bucket.model_dump(mode="json")
                                for bucket in sorted(
                                    buckets,
                                    key=lambda bucket: (
                                        -bucket.net_rescued,
                                        -bucket.total,
                                        bucket.value,
                                    ),
                                )[:10]
                            ]
                            for dimension, buckets in item.breakdowns.items()
                            if buckets
                        },
                        "provider_call_delta": item.provider_call_delta.model_dump(mode="json"),
                        "latency_delta_ms": item.latency_delta_ms,
                    }
                    for item in comparison.pairwise_deltas
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
