from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from finance_agent_core.agent import IntentRouter
from finance_agent_core.evaluation.diagnostic import (
    DiagnosticCommitment,
    DiagnosticSuite,
    create_diagnostic_commitment,
    evaluate_decisions,
    load_diagnostic_suite,
    verify_diagnostic_commitment,
)
from finance_agent_core.evaluation.diagnostic_runner import PreRouterSnapshot
from finance_agent_core.evaluation.external_holdout import (
    ExternalBlindAnswerKey,
    ExternalBlindCommitment,
    ExternalBlindFirstRunState,
    ExternalBlindQuestionSet,
    claim_external_blind_first_run,
    complete_external_blind_first_run,
    create_external_blind_commitment,
    reject_external_near_duplicates,
    validate_external_blind_bundle,
    verify_external_blind_commitment,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _diagnostic(args: argparse.Namespace) -> int:
    suite, suite_sha256 = load_diagnostic_suite()
    if args.profile == "pre_router_snapshot":
        router = PreRouterSnapshot()
        router_version = "pre-router-search-only-v1"
    else:
        router = IntentRouter()
        router_version = "intent-router-v1"
    decisions = [router.route(case.question, case.id) for case in suite.cases]
    report = evaluate_decisions(
        suite,
        decisions,
        suite_sha256=suite_sha256,
        profile=args.profile,
        router_version=router_version,
        generated_at_utc=args.generated_at_utc,
    )
    _write_json(args.output, report)
    print(
        f"{report.profile}: {report.summary.passed}/{report.summary.total} "
        f"({report.summary.strict_accuracy:.6f})"
    )
    return 0 if not args.require_perfect or report.summary.passed == report.summary.total else 1


def _seal_diagnostic(args: argparse.Namespace) -> int:
    commitment = create_diagnostic_commitment(
        args.suite,
        created_at_utc=args.created_at_utc,
    )
    _write_json(args.output, commitment)
    print(commitment.suite_sha256)
    return 0


def _verify_diagnostic(args: argparse.Namespace) -> int:
    commitment = DiagnosticCommitment.model_validate_json(
        args.commitment.read_text(encoding="utf-8")
    )
    verify_diagnostic_commitment(commitment, args.suite)
    print("diagnostic commitment verified")
    return 0


def _validate_external(args: argparse.Namespace) -> int:
    questions = ExternalBlindQuestionSet.model_validate_json(
        args.questions.read_text(encoding="utf-8")
    )
    answers = ExternalBlindAnswerKey.model_validate_json(args.answers.read_text(encoding="utf-8"))
    summary = validate_external_blind_bundle(questions, answers)
    if args.reference_suite:
        reject_external_near_duplicates(
            questions,
            _load_reference_questions(args.reference_suite),
        )
        summary["near_duplicate_reference_suites"] = len(args.reference_suite)
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _seal_external(args: argparse.Namespace) -> int:
    commitment = create_external_blind_commitment(
        args.questions,
        args.answers,
        implementation_commit=args.implementation_commit,
        created_at_utc=args.created_at_utc,
        reference_questions=(
            _load_reference_questions(args.reference_suite) if args.reference_suite else None
        ),
    )
    _write_json(args.output, commitment)
    print(commitment.questions_sha256)
    return 0


def _load_reference_questions(paths: list[Path]) -> list[str]:
    questions: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases = payload.get("cases") if isinstance(payload, dict) else None
        if not isinstance(cases, list):
            raise ValueError(f"reference suite has no cases array: {path}")
        loaded = [
            case.get("question")
            for case in cases
            if isinstance(case, dict) and isinstance(case.get("question"), str)
        ]
        if len(loaded) != len(cases):
            raise ValueError(f"reference suite contains a case without question: {path}")
        questions.extend(loaded)
    return questions


def _require_frozen_checkout(implementation_commit: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != implementation_commit:
        raise RuntimeError(f"checkout differs: expected {implementation_commit}, got {head}")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("external blind first run requires a clean worktree")


def _claim_external_first_run(args: argparse.Namespace) -> int:
    commitment = ExternalBlindCommitment.model_validate_json(
        args.commitment.read_text(encoding="utf-8")
    )
    verify_external_blind_commitment(
        commitment,
        args.questions,
        args.answers,
        implementation_commit=args.implementation_commit,
    )
    _require_frozen_checkout(args.implementation_commit)
    state = claim_external_blind_first_run(
        args.output,
        commitment,
        provider=args.provider,
        model=args.model,
        started_at_utc=args.started_at_utc,
    )
    print(state.model_dump_json(indent=2))
    return 0


def _complete_external_first_run(args: argparse.Namespace) -> int:
    commitment = ExternalBlindCommitment.model_validate_json(
        args.commitment.read_text(encoding="utf-8")
    )
    state = complete_external_blind_first_run(
        args.state,
        commitment,
        args.report,
        completed_at_utc=args.completed_at_utc,
    )
    print(state.model_dump_json(indent=2))
    return 0


def _verify_external(args: argparse.Namespace) -> int:
    commitment = ExternalBlindCommitment.model_validate_json(
        args.commitment.read_text(encoding="utf-8")
    )
    verify_external_blind_commitment(
        commitment,
        args.questions,
        args.answers,
        implementation_commit=args.implementation_commit,
    )
    print("external blind commitment verified")
    return 0


def _schema(args: argparse.Namespace) -> int:
    models = {
        "diagnostic": DiagnosticSuite,
        "external-questions": ExternalBlindQuestionSet,
        "external-answers": ExternalBlindAnswerKey,
        "external-commitment": ExternalBlindCommitment,
        "external-first-run-state": ExternalBlindFirstRunState,
    }
    _write_json(args.output, models[args.kind].model_json_schema())
    print(args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pre-HCX diagnostic and blind protocol")
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnostic = subparsers.add_parser("diagnostic")
    diagnostic.add_argument(
        "--profile",
        choices=["pre_router_snapshot", "current_router"],
        required=True,
    )
    diagnostic.add_argument("--generated-at-utc", required=True)
    diagnostic.add_argument("--output", type=Path, required=True)
    diagnostic.add_argument("--require-perfect", action="store_true")
    diagnostic.set_defaults(handler=_diagnostic)

    seal_diagnostic = subparsers.add_parser("seal-diagnostic")
    seal_diagnostic.add_argument("--suite", type=Path, required=True)
    seal_diagnostic.add_argument("--created-at-utc", required=True)
    seal_diagnostic.add_argument("--output", type=Path, required=True)
    seal_diagnostic.set_defaults(handler=_seal_diagnostic)

    verify_diagnostic = subparsers.add_parser("verify-diagnostic")
    verify_diagnostic.add_argument("--suite", type=Path, required=True)
    verify_diagnostic.add_argument("--commitment", type=Path, required=True)
    verify_diagnostic.set_defaults(handler=_verify_diagnostic)

    validate_external = subparsers.add_parser("validate-external")
    validate_external.add_argument("--questions", type=Path, required=True)
    validate_external.add_argument("--answers", type=Path, required=True)
    validate_external.add_argument(
        "--reference-suite",
        type=Path,
        action="append",
        default=[],
    )
    validate_external.set_defaults(handler=_validate_external)

    seal_external = subparsers.add_parser("seal-external")
    seal_external.add_argument("--questions", type=Path, required=True)
    seal_external.add_argument("--answers", type=Path, required=True)
    seal_external.add_argument("--implementation-commit", required=True)
    seal_external.add_argument("--created-at-utc", required=True)
    seal_external.add_argument("--output", type=Path, required=True)
    seal_external.add_argument(
        "--reference-suite",
        type=Path,
        action="append",
        default=[],
    )
    seal_external.set_defaults(handler=_seal_external)

    verify_external = subparsers.add_parser("verify-external")
    verify_external.add_argument("--questions", type=Path, required=True)
    verify_external.add_argument("--answers", type=Path, required=True)
    verify_external.add_argument("--commitment", type=Path, required=True)
    verify_external.add_argument("--implementation-commit", required=True)
    verify_external.set_defaults(handler=_verify_external)

    claim_first_run = subparsers.add_parser("claim-external-first-run")
    claim_first_run.add_argument("--questions", type=Path, required=True)
    claim_first_run.add_argument("--answers", type=Path, required=True)
    claim_first_run.add_argument("--commitment", type=Path, required=True)
    claim_first_run.add_argument("--implementation-commit", required=True)
    claim_first_run.add_argument("--provider", required=True)
    claim_first_run.add_argument("--model")
    claim_first_run.add_argument("--started-at-utc", required=True)
    claim_first_run.add_argument("--output", type=Path, required=True)
    claim_first_run.set_defaults(handler=_claim_external_first_run)

    complete_first_run = subparsers.add_parser("complete-external-first-run")
    complete_first_run.add_argument("--commitment", type=Path, required=True)
    complete_first_run.add_argument("--state", type=Path, required=True)
    complete_first_run.add_argument("--report", type=Path, required=True)
    complete_first_run.add_argument("--completed-at-utc", required=True)
    complete_first_run.set_defaults(handler=_complete_external_first_run)

    schema = subparsers.add_parser("schema")
    schema.add_argument(
        "--kind",
        choices=[
            "diagnostic",
            "external-questions",
            "external-answers",
            "external-commitment",
            "external-first-run-state",
        ],
        required=True,
    )
    schema.add_argument("--output", type=Path, required=True)
    schema.set_defaults(handler=_schema)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
