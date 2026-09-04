from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from finance_agent_core.retrieval.external_knowledge_release import (
    write_cache_first_external_knowledge_bootstrap,
    write_live_external_knowledge_bootstrap,
)


def _required_cache_arguments(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    required = (
        "approved_corpus_manifest",
        "approved_corpus_root",
        "approved_corpus_manifest_sha256",
        "approved_document_dense",
        "approved_document_dense_sha256",
        "document_dense_output",
        "document_cache_activation",
        "document_cache_activation_sha256",
        "document_cache_activation_output",
        "approved_temporal_relations",
        "approved_temporal_relations_sha256",
    )
    missing = [name for name in required if getattr(arguments, name) is None]
    if missing:
        parser.error(
            "approved_corpus_cache_first requires: "
            + ", ".join(name.replace("_", "-") for name in missing)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a canonical, fail-closed external-knowledge release artifact."
    )
    parser.add_argument(
        "--mode",
        choices=(
            "verified_live_official",
            "verified_live_official_with_relation_graph",
            "approved_corpus_cache_first",
        ),
        default="verified_live_official",
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--temporal-relations", type=Path, required=True)
    parser.add_argument("--approved-corpus-manifest", type=Path)
    parser.add_argument("--approved-corpus-root", type=Path)
    parser.add_argument("--approved-corpus-manifest-sha256")
    parser.add_argument("--approved-document-dense", type=Path)
    parser.add_argument("--approved-document-dense-sha256")
    parser.add_argument("--document-dense-output", type=Path)
    parser.add_argument("--document-cache-activation", type=Path)
    parser.add_argument("--document-cache-activation-sha256")
    parser.add_argument("--document-cache-activation-output", type=Path)
    parser.add_argument("--approved-temporal-relations", type=Path)
    parser.add_argument("--approved-temporal-relations-sha256")
    parser.add_argument("--approved-relation-graph", type=Path)
    parser.add_argument("--approved-relation-graph-sha256")
    arguments = parser.parse_args(argv)
    cache_values = (
        arguments.approved_corpus_manifest,
        arguments.approved_corpus_root,
        arguments.approved_corpus_manifest_sha256,
        arguments.approved_document_dense,
        arguments.approved_document_dense_sha256,
        arguments.document_dense_output,
        arguments.document_cache_activation,
        arguments.document_cache_activation_sha256,
        arguments.document_cache_activation_output,
        arguments.approved_temporal_relations,
        arguments.approved_temporal_relations_sha256,
    )
    graph_values = (
        arguments.approved_relation_graph,
        arguments.approved_relation_graph_sha256,
    )
    if arguments.mode == "verified_live_official":
        if any(value is not None for value in (*cache_values, *graph_values)):
            parser.error("live-only mode rejects approved cache inputs")
        bootstrap = write_live_external_knowledge_bootstrap(
            artifact_path=arguments.artifact,
            relation_bundle_path=arguments.temporal_relations,
        )
    elif arguments.mode == "verified_live_official_with_relation_graph":
        if any(value is not None for value in cache_values):
            parser.error("relation graph mode rejects approved document cache inputs")
        if any(value is None for value in graph_values):
            parser.error(
                "verified_live_official_with_relation_graph requires: "
                "approved-relation-graph, approved-relation-graph-sha256"
            )
        bootstrap = write_live_external_knowledge_bootstrap(
            artifact_path=arguments.artifact,
            relation_bundle_path=arguments.temporal_relations,
            relation_graph_path=arguments.approved_relation_graph,
            expected_relation_graph_file_sha256=arguments.approved_relation_graph_sha256,
        )
    else:
        _required_cache_arguments(parser, arguments)
        bootstrap = write_cache_first_external_knowledge_bootstrap(
            artifact_path=arguments.artifact,
            relation_bundle_output_path=arguments.temporal_relations,
            document_dense_output_path=arguments.document_dense_output,
            activation_receipt_output_path=(arguments.document_cache_activation_output),
            approved_corpus_manifest_path=arguments.approved_corpus_manifest,
            approved_corpus_root=arguments.approved_corpus_root,
            expected_approved_corpus_manifest_sha256=(arguments.approved_corpus_manifest_sha256),
            document_dense_artifact_path=arguments.approved_document_dense,
            expected_document_dense_file_sha256=(arguments.approved_document_dense_sha256),
            activation_receipt_path=arguments.document_cache_activation,
            expected_activation_receipt_file_sha256=(arguments.document_cache_activation_sha256),
            relation_bundle_path=arguments.approved_temporal_relations,
            expected_relation_bundle_file_sha256=(arguments.approved_temporal_relations_sha256),
        )
    print(f"external_knowledge_artifact_sha256={bootstrap.artifact_file_sha256}")
    print(f"external_temporal_relation_sha256={bootstrap.relation_bundle_file_sha256}")
    print(
        "external_temporal_relation_set_sha256="
        f"{bootstrap.relation_bundle.manifest.relation_set_sha256}"
    )
    if arguments.mode == "approved_corpus_cache_first":
        print(f"external_document_dense_sha256={bootstrap.document_dense_file_sha256}")
        print(f"external_document_cache_activation_sha256={bootstrap.activation_file_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
