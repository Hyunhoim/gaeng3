from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

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
    model = ExternalCorpusIntakeSpec if arguments.kind == "intake" else ApprovedCorpusManifest
    print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal and verify manually approved external text snapshots without downloading data."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

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
    schema.add_argument("--kind", choices=("intake", "manifest"), default="intake")
    schema.set_defaults(handler=_schema)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    try:
        handler(arguments)
    except (CorpusApprovalError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":  # pragma: no cover
    main()
