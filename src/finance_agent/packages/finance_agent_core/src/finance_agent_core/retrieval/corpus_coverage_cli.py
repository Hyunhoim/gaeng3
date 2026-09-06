from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.corpus_coverage import (
    CorpusCoverageError,
    DatasetCoverageReceipt,
    RelationBundleInput,
    build_corpus_coverage_analysis,
    load_verified_pending_texts,
    write_corpus_coverage_artifacts,
)
from finance_agent_core.retrieval.pending_corpus import load_pending_corpus_manifest
from finance_agent_core.retrieval.temporal_relations import VerifiedTemporalRelationBundle
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database_paths,
)
from finance_agent_core.storage.identity_cache import load_product_identities

_MAX_RELATION_BYTES = 256 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_model(
    path: Path,
    model: type[OfficialSourceRegistry] | type[ApprovedKnowledgeCoverageManifest],
):
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise CorpusCoverageError(f"invalid coverage input: {path}") from error


def _relation_argument(value: str) -> RelationBundleInput:
    try:
        raw_path, expected_sha256 = value.rsplit("=", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError("relation input must be PATH=SHA256") from error
    path = Path(raw_path).resolve()
    if (
        len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or not path.is_file()
        or path.is_symlink()
        or path.stat().st_size <= 0
        or path.stat().st_size > _MAX_RELATION_BYTES
    ):
        raise argparse.ArgumentTypeError("relation input path or SHA-256 is invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise argparse.ArgumentTypeError("relation input SHA-256 differs")
    try:
        bundle = VerifiedTemporalRelationBundle.model_validate_json(data)
    except ValueError as error:
        raise argparse.ArgumentTypeError("relation bundle schema is invalid") from error
    return RelationBundleInput(
        source_id=str(path),
        file_sha256=expected_sha256,
        bundle=bundle,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build analysis-only product/document coverage and a gap-only queue."
    )
    parser.add_argument("--bond-db", type=Path, required=True)
    parser.add_argument("--domestic-etp-db", type=Path, required=True)
    parser.add_argument("--overseas-etp-db", type=Path, required=True)
    parser.add_argument("--fund-db", type=Path, required=True)
    parser.add_argument("--source-registry", type=Path)
    parser.add_argument("--coverage-manifest", type=Path)
    parser.add_argument("--pending-manifest", type=Path)
    parser.add_argument("--pending-root", type=Path)
    parser.add_argument(
        "--active-relation",
        action="append",
        default=[],
        type=_relation_argument,
        metavar="PATH=SHA256",
    )
    parser.add_argument(
        "--legacy-relation",
        action="append",
        default=[],
        type=_relation_argument,
        metavar="PATH=SHA256",
    )
    parser.add_argument("--max-gap-items", type=int, default=500)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if (arguments.pending_manifest is None) != (arguments.pending_root is None):
        raise SystemExit("--pending-manifest and --pending-root must be supplied together")
    registry = (
        load_official_source_registry()
        if arguments.source_registry is None
        else _load_json_model(arguments.source_registry, OfficialSourceRegistry)
    )
    coverage = (
        load_approved_knowledge_coverage_manifest()
        if arguments.coverage_manifest is None
        else _load_json_model(
            arguments.coverage_manifest,
            ApprovedKnowledgeCoverageManifest,
        )
    )
    approved_datasets = load_approved_dataset_manifest()
    database_paths = {
        ProductFamily.BOND: arguments.bond_db,
        ProductFamily.DOMESTIC_ETP: arguments.domestic_etp_db,
        ProductFamily.OVERSEAS_ETP: arguments.overseas_etp_db,
        ProductFamily.FUND: arguments.fund_db,
    }
    require_approved_database_paths(database_paths, approval=approved_datasets)
    records_by_family = {}
    datasets = []
    for family, path in database_paths.items():
        manifest, records = load_product_identities(path)
        if manifest.dataset != family.value:
            raise CorpusCoverageError(f"database family differs: {path}")
        eligible = tuple(
            record
            for record in records
            if not record.is_quarantined
            and not (family is ProductFamily.FUND and record.public_offering is False)
        )
        records_by_family[family] = records
        datasets.append(
            DatasetCoverageReceipt(
                product_family=family,
                database_sha256=_sha256(path),
                source_file_sha256=manifest.source_file_sha256,
                source_snapshot_date=manifest.source_snapshot_date,
                product_count=len(eligible),
            )
        )
    pending = (
        None
        if arguments.pending_manifest is None
        else load_pending_corpus_manifest(arguments.pending_manifest)
    )
    texts = {} if pending is None else load_verified_pending_texts(pending, arguments.pending_root)
    analysis, queue = build_corpus_coverage_analysis(
        records_by_family,
        datasets=datasets,
        registry=registry,
        coverage=coverage,
        dataset_release_id=approved_datasets.release_id,
        pending_manifest=pending,
        pending_texts=texts,
        active_relations=arguments.active_relation,
        legacy_relations=arguments.legacy_relation,
        max_gap_items=arguments.max_gap_items,
    )
    receipt, paths = write_corpus_coverage_artifacts(
        arguments.output_dir.resolve(),
        analysis,
        queue,
    )
    print(f"analysis_id={analysis.analysis_id}")
    print(f"analysis_sha256={analysis.canonical_sha256}")
    print(f"gap_count={queue.total_gap_count}")
    print(f"returned_gap_count={queue.returned_gap_count}")
    print(f"receipt_outputs={len(receipt.output_sha256s)}")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
