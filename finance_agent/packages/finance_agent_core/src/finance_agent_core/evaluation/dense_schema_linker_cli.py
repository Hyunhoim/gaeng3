from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.dense_schema_linker import (
    report_fingerprint,
    run_dense_schema_linker_evaluation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the offline-only fake Dense schema-linker component evaluation "
            "with the gold product family."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/dense-schema-linker-component-fake-v1.json"),
    )
    parser.add_argument("--require-contract", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = run_dense_schema_linker_evaluation()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "evaluation_id": report.evaluation_id,
                "report_sha256": report_fingerprint(report),
                "suite_case_count": report.suite_case_count,
                "lexical": report.lexical.model_dump(mode="json"),
                "fake_dense": report.fake_dense.model_dump(mode="json"),
                "hybrid": report.hybrid.model_dump(mode="json"),
                "safety": report.safety.model_dump(mode="json"),
                "runtime": report.runtime.model_dump(mode="json"),
                "decision": report.decision.model_dump(mode="json"),
                "output": str(arguments.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    contract_passed = (
        report.safety.blocked_no_call_rate == 1
        and report.safety.pre_dense_gate_false_positive_count == 0
        and report.safety.out_of_registry_candidate_count == 0
        and report.safety.out_of_family_candidate_count == 0
        and report.safety.production_probe_provider_query_calls == 0
        and not report.index_manifest.production_enabled
        and report.index_manifest.abstention_policy == "not_calibrated"
    )
    if arguments.require_contract and not contract_passed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
