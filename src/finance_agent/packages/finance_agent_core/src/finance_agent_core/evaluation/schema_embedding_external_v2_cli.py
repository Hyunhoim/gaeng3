from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finance_agent_core.evaluation.schema_embedding_external_v2 import (
    CandidateExecutionLock,
    ExternalBlindExecutionAuthorization,
    ExternalBlindPredictionReceipt,
    ExternalBlindPrivateAnswerKey,
    ExternalBlindQuestionOnlySet,
    ExternalBlindV2Commitment,
    ExternalBlindV2ReferenceReport,
    ExternalBlindV2ScoreReport,
    ExternalBundleUnavailableError,
    ExternalSchemaBlindV2Error,
    FrozenModelArtifactManifest,
    QuestionOnlyPredictionArtifact,
    run_and_freeze_question_only_predictions,
    run_external_blind_v2_reference_gate,
    score_revealed_bundle_files,
)
from finance_agent_core.evaluation.schema_embedding_external_v2_rehearsal import (
    ExternalBlindV2SyntheticRehearsalReport,
    SyntheticRehearsalIntegrityError,
    run_synthetic_external_blind_v2_rehearsal,
)


def _run(arguments: argparse.Namespace) -> int:
    try:
        artifact = run_and_freeze_question_only_predictions(
            question_path=arguments.questions,
            commitment_path=arguments.commitment,
            execution_authorization_path=arguments.execution_authorization,
            protocol_path=arguments.protocol,
            near_duplicate_report_path=arguments.near_duplicate_report,
            bge_manifest_path=arguments.bge_manifest,
            bge_snapshot_dir=arguments.bge_snapshot,
            bge_trusted_cache_root=arguments.bge_cache_root,
            kure_manifest_path=arguments.kure_manifest,
            kure_snapshot_dir=arguments.kure_snapshot,
            kure_trusted_cache_root=arguments.kure_cache_root,
            output_path=arguments.output,
            implementation_commit=arguments.implementation_commit,
            created_at_utc=arguments.created_at_utc,
        )
    except (
        ExternalSchemaBlindV2Error,
        FileExistsError,
        OSError,
        ValueError,
    ) as error:
        print(f"Schema Dense external blind v2 run refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "protocol_id": artifact.protocol_id,
                "status": artifact.status,
                "output": str(arguments.output),
                "case_count": len(artifact.cases),
                "answer_key_opened": artifact.answer_key_opened,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _reference(arguments: argparse.Namespace) -> int:
    try:
        report = run_external_blind_v2_reference_gate(
            question_path=arguments.questions,
            protocol_path=arguments.protocol,
            output_path=arguments.output,
        )
    except (
        ExternalSchemaBlindV2Error,
        FileExistsError,
        OSError,
        ValueError,
    ) as error:
        print(f"Schema Dense external blind v2 reference gate refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "protocol_id": report.protocol_id,
                "status": report.status,
                "output": str(arguments.output),
                "reference_question_count": report.reference_question_count,
                "maximum_observed_similarity": report.maximum_observed_similarity,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _score(arguments: argparse.Namespace) -> int:
    try:
        report = score_revealed_bundle_files(
            prediction_path=arguments.predictions,
            question_path=arguments.questions,
            answer_path=arguments.answers,
            commitment_path=arguments.commitment,
            prediction_receipt_path=arguments.prediction_receipt,
            output_path=arguments.output,
            scored_at_utc=arguments.scored_at_utc,
        )
    except (ExternalBundleUnavailableError, FileExistsError, OSError, ValueError) as error:
        print(f"Schema Dense external blind v2 scoring refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "protocol_id": report.protocol_id,
                "status": report.status,
                "output": str(arguments.output),
                "disposition_accuracy": report.routing.disposition_accuracy,
                "family_accuracy": report.routing.family_accuracy,
                "interaction_intent_accuracy": report.routing.interaction_intent_accuracy,
                "gold_control_dense_safety": {
                    "control_case_count": report.routing.control_case_count,
                    "operational_dense_provider_call_count": (
                        report.routing.control_operational_dense_call_count
                    ),
                    "no_operational_dense_case_rate": (
                        report.routing.control_no_operational_dense_rate
                    ),
                    "gate_passed": report.routing.control_operational_dense_gate_passed,
                },
                "field_scores": {
                    item.candidate: {
                        "exact": item.exact_at_gold_cardinality,
                        "recall_at_5": item.micro_recall_at_5,
                    }
                    for item in report.field_scores
                },
                "ood_test_gates": {
                    item.alias: item.test_gate_passed for item in report.ood_thresholds
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _rehearse(arguments: argparse.Namespace) -> int:
    try:
        report = run_synthetic_external_blind_v2_rehearsal(
            output_dir=arguments.output_dir,
            implementation_commit=arguments.implementation_commit,
        )
    except (
        ExternalSchemaBlindV2Error,
        SyntheticRehearsalIntegrityError,
        FileExistsError,
        OSError,
        ValueError,
    ) as error:
        print(
            f"Schema Dense external blind v2 synthetic rehearsal refused: {error}",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "protocol_id": report.protocol_id,
                "status": report.status,
                "never_model_selection_evidence": report.never_model_selection_evidence,
                "output_dir": str(arguments.output_dir),
                "case_count": report.case_count,
                "control_operational_dense_call_count": (
                    report.mechanics.control_operational_dense_call_count
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _schema(arguments: argparse.Namespace) -> int:
    models = {
        "artifact-manifest": FrozenModelArtifactManifest,
        "candidate-lock": CandidateExecutionLock,
        "question-only-set": ExternalBlindQuestionOnlySet,
        "private-answer-key": ExternalBlindPrivateAnswerKey,
        "commitment": ExternalBlindV2Commitment,
        "execution-authorization": ExternalBlindExecutionAuthorization,
        "near-duplicate-report": ExternalBlindV2ReferenceReport,
        "prediction-receipt": ExternalBlindPredictionReceipt,
        "predictions": QuestionOnlyPredictionArtifact,
        "score-report": ExternalBlindV2ScoreReport,
        "synthetic-rehearsal-report": ExternalBlindV2SyntheticRehearsalReport,
    }
    payload = models[arguments.kind].model_json_schema()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    print(arguments.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Score the standalone two-phase Schema Dense external blind v2 protocol. "
            "The run command internally loads both exact frozen snapshots and canonical indexes."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--questions", type=Path, required=True)
    run.add_argument("--commitment", type=Path, required=True)
    run.add_argument("--execution-authorization", type=Path, required=True)
    run.add_argument("--protocol", type=Path, required=True)
    run.add_argument("--near-duplicate-report", type=Path, required=True)
    run.add_argument("--bge-manifest", type=Path, required=True)
    run.add_argument("--bge-snapshot", type=Path, required=True)
    run.add_argument("--bge-cache-root", type=Path, required=True)
    run.add_argument("--kure-manifest", type=Path, required=True)
    run.add_argument("--kure-snapshot", type=Path, required=True)
    run.add_argument("--kure-cache-root", type=Path, required=True)
    run.add_argument("--implementation-commit", required=True)
    run.add_argument("--created-at-utc", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)

    reference = subparsers.add_parser(
        "reference",
        help="compare label-free questions with the frozen public/development corpus",
    )
    reference.add_argument("--questions", type=Path, required=True)
    reference.add_argument("--protocol", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    reference.set_defaults(handler=_reference)

    score = subparsers.add_parser("score")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--questions", type=Path, required=True)
    score.add_argument("--answers", type=Path, required=True)
    score.add_argument("--commitment", type=Path, required=True)
    score.add_argument("--prediction-receipt", type=Path, required=True)
    score.add_argument("--scored-at-utc", required=True)
    score.add_argument("--output", type=Path, required=True)
    score.set_defaults(handler=_score)

    rehearse = subparsers.add_parser(
        "rehearse",
        help="run public synthetic mechanics only; never valid as blind/model-selection evidence",
    )
    rehearse.add_argument("--output-dir", type=Path, required=True)
    rehearse.add_argument("--implementation-commit", required=True)
    rehearse.set_defaults(handler=_rehearse)

    schema = subparsers.add_parser("schema")
    schema.add_argument(
        "--kind",
        choices=(
            "artifact-manifest",
            "question-only-set",
            "private-answer-key",
            "commitment",
            "execution-authorization",
            "near-duplicate-report",
            "prediction-receipt",
            "candidate-lock",
            "predictions",
            "score-report",
            "synthetic-rehearsal-report",
        ),
        required=True,
    )
    schema.add_argument("--output", type=Path, required=True)
    schema.set_defaults(handler=_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
