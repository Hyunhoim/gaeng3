from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.domain_qa import (
    DomainQAReport,
    DomainQARunner,
    DomainQASpec,
    DomainQASuite,
    load_domain_qa_suite,
    verify_domain_qa_databases,
    verify_domain_qa_search_gold,
)
from finance_agent_core.storage import ProductIdentitySnapshotCache, RecordSnapshotCache


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily.OVERSEAS_ETP: database_dir / "overseas_etp.sqlite3",
        ProductFamily.DOMESTIC_ETP: database_dir / "domestic_etp.sqlite3",
        ProductFamily.BOND: database_dir / "bond.sqlite3",
        ProductFamily.FUND: database_dir / "fund.sqlite3",
    }


def _write_json(path: Path, payload: Any) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate(args: argparse.Namespace) -> int:
    loaded = load_domain_qa_suite(args.questions_csv, args.review_csv)
    suite = loaded.suite
    print(
        json.dumps(
            {
                "suite_id": suite.suite_id,
                "case_count": len(suite.cases),
                "spec_sha256": loaded.spec_sha256,
                "source_questions_sha256": suite.source_questions_sha256,
                "review_csv_sha256": suite.review_csv_sha256,
                "status": suite.status,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    loaded = load_domain_qa_suite(args.questions_csv, args.review_csv)
    database_paths = _database_paths(args.database_dir)
    database_hashes = verify_domain_qa_databases(loaded.spec, database_paths)
    verify_domain_qa_search_gold(loaded.suite, database_paths)
    service = RoutedFinanceAgent(
        database_paths,
        allow_internal_disabled_dataset=True,
        record_cache=RecordSnapshotCache(max_entries=4),
        identity_cache=ProductIdentitySnapshotCache(max_entries=4),
    )
    generated_at_utc = args.generated_at_utc or (
        datetime.now(UTC).replace(microsecond=0).isoformat()
    )
    report = DomainQARunner(
        loaded_suite=loaded,
        service=service,
        database_sha256_by_family=database_hashes,
        generated_at_utc=generated_at_utc,
        report_id=args.report_id,
    ).run()
    output = args.output or Path("artifacts/evaluation/domain-qa-dev-v1-current.json")
    _write_json(output, report)
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": report.summary.passed,
                "total": report.summary.total,
                "strict_accuracy": report.summary.strict_accuracy,
                "route_pass_rate": report.summary.route_pass_rate,
                "safety_pass_rate": report.summary.safety_pass_rate,
                "dependency_pending": report.summary.dependency_pending,
                "oracle_gold_pending": report.summary.oracle_gold_pending,
                "search_gold_complete": report.summary.search_gold_complete,
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    if args.require_perfect and not report.summary.perfect:
        return 1
    if args.require_safe and report.summary.safety_passed != report.summary.total:
        return 1
    return 0


def _schema(args: argparse.Namespace) -> int:
    models = {
        "spec": DomainQASpec,
        "suite": DomainQASuite,
        "report": DomainQAReport,
    }
    _write_json(args.output, models[args.kind].model_json_schema())
    print(args.output)
    return 0


def _add_csv_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--questions-csv", type=Path, required=True)
    parser.add_argument("--review-csv", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and run the financial-domain QA development experiment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    _add_csv_arguments(validate)
    validate.set_defaults(handler=_validate)

    run = subparsers.add_parser("run")
    _add_csv_arguments(run)
    run.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    run.add_argument("--output", type=Path)
    run.add_argument("--generated-at-utc")
    run.add_argument(
        "--report-id",
        default="domain-qa-dev-v1-current",
        help="Stable experiment ID; use a new ID for every post-fix run.",
    )
    run.add_argument("--require-perfect", action="store_true")
    run.add_argument("--require-safe", action="store_true")
    run.set_defaults(handler=_run)

    schema = subparsers.add_parser("schema")
    schema.add_argument("--kind", choices=["spec", "suite", "report"], required=True)
    schema.add_argument("--output", type=Path, required=True)
    schema.set_defaults(handler=_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
