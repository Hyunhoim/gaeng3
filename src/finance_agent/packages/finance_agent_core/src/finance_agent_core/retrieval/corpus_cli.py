from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.retrieval.corpus import (
    ApprovedCorpusManifest,
    CorpusApprovalError,
    ExternalCorpusIntakeSpec,
    approved_corpus_manifest_bytes,
    build_approved_corpus_index,
    corpus_receipt_bytes,
    load_external_corpus_intake_spec,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
    write_new_read_only_file,
)
from finance_agent_core.retrieval.corpus_review_bridge import (
    ExternalCorpusHumanApproval,
    ExternalCorpusReviewWorksheet,
    bridge_pending_corpus_to_reviewed_intake,
    build_external_corpus_review_worksheet,
    corpus_review_worksheet_bytes,
    external_corpus_intake_spec_bytes,
    load_external_corpus_human_approval,
)
from finance_agent_core.retrieval.open_dart import load_open_dart_filing_whitelist
from finance_agent_core.retrieval.open_dart_pending_bridge import (
    OpenDartPendingBridgeError,
    OpenDartPendingBridgeReceipt,
    bridge_open_dart_pending_corpus,
)


def _review_registry(profile: str) -> OfficialSourceRegistry:
    if profile == "pending-intake":
        return load_pending_intake_source_registry()
    return load_official_source_registry()


def _seal(arguments: argparse.Namespace) -> None:
    spec = load_external_corpus_intake_spec(arguments.spec)
    manifest = seal_approved_corpus_manifest(spec, arguments.corpus_root)
    digest = write_new_read_only_file(
        arguments.output,
        approved_corpus_manifest_bytes(manifest),
    )
    print(f"approved_corpus_manifest_sha256={digest}")


def _verify(arguments: argparse.Namespace) -> None:
    verified = verify_approved_corpus(arguments.manifest, arguments.corpus_root)
    receipt = verified.receipt
    data = corpus_receipt_bytes(receipt)
    if arguments.output_receipt is not None:
        digest = write_new_read_only_file(arguments.output_receipt, data)
        print(f"corpus_verification_receipt_sha256={digest}")
    print(data.decode("utf-8"), end="")


def _build_index(arguments: argparse.Namespace) -> None:
    verified = verify_approved_corpus(arguments.manifest, arguments.corpus_root)
    receipt = build_approved_corpus_index(verified, arguments.output_database)
    data = corpus_receipt_bytes(receipt)
    if arguments.output_receipt is not None:
        digest = write_new_read_only_file(arguments.output_receipt, data)
        print(f"corpus_index_receipt_sha256={digest}")
    print(data.decode("utf-8"), end="")


def _schema(arguments: argparse.Namespace) -> None:
    models = {
        "approval": ExternalCorpusHumanApproval,
        "intake": ExternalCorpusIntakeSpec,
        "manifest": ApprovedCorpusManifest,
        "open-dart-bridge-receipt": OpenDartPendingBridgeReceipt,
        "worksheet": ExternalCorpusReviewWorksheet,
    }
    model = models[arguments.kind]
    print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True))


def _prepare_review(arguments: argparse.Namespace) -> None:
    worksheet = build_external_corpus_review_worksheet(
        arguments.pending_root,
        registry=_review_registry(arguments.registry_profile),
    )
    digest = write_new_read_only_file(
        arguments.output,
        corpus_review_worksheet_bytes(worksheet),
    )
    print(f"external_corpus_review_worksheet_sha256={digest}")


def _bridge_pending(arguments: argparse.Namespace) -> None:
    approval = load_external_corpus_human_approval(arguments.approval_input)
    intake = bridge_pending_corpus_to_reviewed_intake(
        arguments.pending_root,
        approval=approval,
        registry=_review_registry(arguments.registry_profile),
    )
    digest = write_new_read_only_file(
        arguments.output,
        external_corpus_intake_spec_bytes(intake),
    )
    print(f"external_corpus_intake_spec_sha256={digest}")


def _bridge_open_dart(arguments: argparse.Namespace) -> None:
    result = bridge_open_dart_pending_corpus(
        arguments.open_dart_root,
        arguments.output_root,
        registry=load_pending_intake_source_registry(),
        policy=load_open_dart_filing_whitelist(),
    )
    print(f"pending_corpus_manifest_sha256={result.manifest_sha256}")
    print(f"open_dart_bridge_receipt_sha256={result.receipt_sha256}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal and verify manually approved external text snapshots without downloading data."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_review = commands.add_parser(
        "prepare-review",
        help="create a hash-bound worksheet without granting corpus authority",
    )
    prepare_review.add_argument("--pending-root", type=Path, required=True)
    prepare_review.add_argument("--output", type=Path, required=True)
    prepare_review.add_argument(
        "--registry-profile",
        choices=("production", "pending-intake"),
        default="production",
    )
    prepare_review.set_defaults(handler=_prepare_review)

    bridge = commands.add_parser(
        "bridge-pending",
        help="build an intake spec from externally supplied reviews and rights",
    )
    bridge.add_argument("--pending-root", type=Path, required=True)
    bridge.add_argument("--approval-input", type=Path, required=True)
    bridge.add_argument("--output", type=Path, required=True)
    bridge.add_argument(
        "--registry-profile",
        choices=("production", "pending-intake"),
        default="production",
    )
    bridge.set_defaults(handler=_bridge_pending)

    bridge_open_dart = commands.add_parser(
        "bridge-open-dart",
        help="convert a verified Open DART collection into the non-authoritative review queue",
    )
    bridge_open_dart.add_argument("--open-dart-root", type=Path, required=True)
    bridge_open_dart.add_argument("--output-root", type=Path, required=True)
    bridge_open_dart.set_defaults(handler=_bridge_open_dart)

    seal = commands.add_parser("seal", help="hash reviewed snapshots into an immutable manifest")
    seal.add_argument("--spec", type=Path, required=True)
    seal.add_argument("--corpus-root", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.set_defaults(handler=_seal)

    verify = commands.add_parser("verify", help="fail closed unless every approved byte matches")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--corpus-root", type=Path, required=True)
    verify.add_argument("--output-receipt", type=Path)
    verify.set_defaults(handler=_verify)

    build = commands.add_parser(
        "build-index",
        help="build a new BM25 index from a verified approved corpus",
    )
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--corpus-root", type=Path, required=True)
    build.add_argument("--output-database", type=Path, required=True)
    build.add_argument("--output-receipt", type=Path)
    build.set_defaults(handler=_build_index)

    schema = commands.add_parser("schema", help="print the strict JSON schema")
    schema.add_argument(
        "--kind",
        choices=(
            "approval",
            "intake",
            "manifest",
            "open-dart-bridge-receipt",
            "worksheet",
        ),
        default="intake",
    )
    schema.set_defaults(handler=_schema)
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    try:
        handler(arguments)
    except (CorpusApprovalError, OpenDartPendingBridgeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":  # pragma: no cover
    main()
