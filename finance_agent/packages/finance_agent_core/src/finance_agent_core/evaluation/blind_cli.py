"""CLI for validating, sealing, and running the public-fund blind suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.evaluation import load_core_evaluation_suite
from finance_agent_core.evaluation.blind import (
    BlindAnswerKey,
    BlindCommitment,
    BlindQuestionSet,
    blind_bundle_sha256,
    build_blind_evaluation_cases,
    create_blind_commitment,
    reject_near_duplicates,
    validate_blind_bundle,
    verify_blind_commitment,
)
from finance_agent_core.evaluation.runner import (
    EvaluationRunner,
    ExpectedPlanProvider,
    build_report,
    sha256_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and seal the independently authored public-fund blind suite."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "commit"):
        command = subparsers.add_parser(name)
        command.add_argument("--questions", type=Path, required=True)
        command.add_argument("--answers", type=Path, required=True)
    commit = subparsers.choices["commit"]
    commit.add_argument("--parser-commit", required=True)
    commit.add_argument("--output", type=Path, required=True)

    oracle = subparsers.add_parser("oracle-check")
    oracle.add_argument("--questions", type=Path, required=True)
    oracle.add_argument("--answers", type=Path, required=True)
    oracle.add_argument("--database", type=Path, default=Path("artifacts/normalized/fund.sqlite3"))
    oracle.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/normalized/fund.sqlite3.manifest.json"),
    )
    oracle.add_argument("--workers", type=int, default=4)
    oracle.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--questions", type=Path, required=True)
    verify.add_argument("--answers", type=Path, required=True)
    verify.add_argument("--commitment", type=Path, required=True)
    verify.add_argument("--parser-commit", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--questions", type=Path, required=True)
    run.add_argument("--answers", type=Path, required=True)
    run.add_argument("--commitment", type=Path, required=True)
    run.add_argument("--parser-commit", required=True)
    run.add_argument("--database", type=Path, default=Path("artifacts/normalized/fund.sqlite3"))
    run.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/normalized/fund.sqlite3.manifest.json"),
    )
    run.add_argument("--provider", choices=("expected", "local_test"), required=True)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--first-run-state", type=Path)
    run.add_argument("--confirm-first-run", action="store_true")
    return parser


def _load_and_validate(
    question_path: Path,
    answer_path: Path,
) -> tuple[BlindQuestionSet, BlindAnswerKey, dict[str, object]]:
    questions = BlindQuestionSet.model_validate_json(question_path.read_text(encoding="utf-8"))
    answers = BlindAnswerKey.model_validate_json(answer_path.read_text(encoding="utf-8"))
    summary = validate_blind_bundle(questions, answers)
    references = [case.question for case in load_core_evaluation_suite("fund").suite.cases]
    reject_near_duplicates(questions, references)
    return questions, answers, summary


def _claim_first_run(
    path: Path,
    commitment: BlindCommitment,
    model: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "started",
        "suite_id": commitment.suite_id,
        "questions_sha256": commitment.questions_sha256,
        "answers_sha256": commitment.answers_sha256,
        "parser_commit": commitment.parser_commit,
        "model": model,
        "started_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _complete_first_run(path: Path, output: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "completed",
            "completed_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "report_name": output.name,
            "report_sha256": sha256_file(output),
        }
    )
    path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )


def _path_bundle_sha256(question_path: Path, answer_path: Path) -> str:
    payload = f"{sha256_file(question_path)}:{sha256_file(answer_path)}".encode()
    return hashlib.sha256(payload).hexdigest()


def _require_frozen_checkout(parser_commit: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != parser_commit:
        raise RuntimeError(f"checkout differs: expected {parser_commit}, got {head}")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("blind run requires a clean worktree")


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "validate":
        _, _, summary = _load_and_validate(arguments.questions, arguments.answers)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "oracle-check":
        if arguments.output.exists():
            raise RuntimeError(f"blind output already exists: {arguments.output}")
        questions, answers, summary = _load_and_validate(
            arguments.questions,
            arguments.answers,
        )
        database_sha256 = sha256_file(arguments.database)
        manifest_sha256 = sha256_file(arguments.manifest)
        if database_sha256 != answers.database_sha256:
            raise RuntimeError("blind answer key database hash differs")
        if manifest_sha256 != answers.manifest_sha256:
            raise RuntimeError("blind answer key manifest hash differs")
        cases = build_blind_evaluation_cases(questions, answers)
        runner = EvaluationRunner(
            arguments.database,
            ExpectedPlanProvider(cases, "fund"),
            allow_internal_disabled_dataset=True,
        )
        report = build_report(
            suite_id=questions.suite_id,
            suite_version="1.1",
            suite_sha256=_path_bundle_sha256(
                arguments.questions,
                arguments.answers,
            ),
            database_sha256=database_sha256,
            manifest_sha256=manifest_sha256,
            provider="expected",
            model=None,
            split="holdout",
            workers=arguments.workers,
            results=runner.run(cases, arguments.workers),
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            f"{report.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "validation": summary,
                    "summary": report.summary.model_dump(mode="json"),
                    "output": str(arguments.output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report.summary.passed == report.summary.total else 2
    if arguments.command == "commit":
        if arguments.output.exists():
            raise RuntimeError(f"blind commitment already exists: {arguments.output}")
        _load_and_validate(arguments.questions, arguments.answers)
        commitment = create_blind_commitment(
            arguments.questions,
            arguments.answers,
            parser_commit=arguments.parser_commit,
            created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            f"{commitment.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
        print(commitment.model_dump_json(indent=2))
        return 0

    commitment = BlindCommitment.model_validate_json(
        arguments.commitment.read_text(encoding="utf-8")
    )
    questions, answers, summary = _load_and_validate(
        arguments.questions,
        arguments.answers,
    )
    verify_blind_commitment(
        commitment,
        arguments.questions,
        arguments.answers,
        parser_commit=arguments.parser_commit,
    )
    if arguments.command == "run":
        _require_frozen_checkout(arguments.parser_commit)
        if arguments.output.exists():
            raise RuntimeError(f"blind output already exists: {arguments.output}")
        database_sha256 = sha256_file(arguments.database)
        manifest_sha256 = sha256_file(arguments.manifest)
        if database_sha256 != answers.database_sha256:
            raise RuntimeError("blind answer key database hash differs")
        if manifest_sha256 != answers.manifest_sha256:
            raise RuntimeError("blind answer key manifest hash differs")
        cases = build_blind_evaluation_cases(questions, answers)
        model: str | None = None
        first_run_state: Path | None = None
        if arguments.provider == "local_test":
            if not arguments.confirm_first_run or arguments.first_run_state is None:
                raise RuntimeError(
                    "local blind run requires --confirm-first-run and --first-run-state"
                )
            settings = LocalTestSettings.from_environment()
            provider = LocalTestProvider(
                settings,
                internal_evaluation_family="fund",
            )
            provider.healthcheck()
            model = settings.model
            first_run_state = arguments.first_run_state
        else:
            provider = ExpectedPlanProvider(cases, "fund")
        runner = EvaluationRunner(
            arguments.database,
            provider,
            allow_internal_disabled_dataset=True,
        )
        if first_run_state is not None:
            assert model is not None
            _claim_first_run(first_run_state, commitment, model)
        results = runner.run(cases, arguments.workers)
        report = build_report(
            suite_id=commitment.suite_id,
            suite_version="1.1",
            suite_sha256=blind_bundle_sha256(commitment),
            database_sha256=database_sha256,
            manifest_sha256=manifest_sha256,
            provider=arguments.provider,
            model=model,
            split="holdout",
            workers=arguments.workers,
            results=results,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            f"{report.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
        if first_run_state is not None:
            _complete_first_run(first_run_state, arguments.output)
        print(
            json.dumps(
                {
                    "commitment": commitment.model_dump(mode="json"),
                    "validation": summary,
                    "provider": report.provider,
                    "model": report.model,
                    "summary": report.summary.model_dump(mode="json"),
                    "output": str(arguments.output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "status": "verified",
                "suite_id": commitment.suite_id,
                "parser_commit": commitment.parser_commit,
                "question_count": commitment.question_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
