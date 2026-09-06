from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.corpus_coverage import GapCollectionQueue
from finance_agent_core.retrieval.corpus_expansion import (
    CorpusExpansionQuery,
    build_corpus_expansion_queries,
    build_gap_corpus_expansion_plan,
    discover_corpus_candidates,
    exclude_existing_corpus_candidates,
    materialize_corpus_catalog,
    merge_pending_catalogs,
    offline_snapshot_discovery_registry,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    NaverApiHubWebSearchTransport,
    canonical_official_candidate_url,
)
from finance_agent_core.retrieval.pending_corpus import (
    load_pending_corpus_catalog,
    load_pending_corpus_manifest,
    rebind_pending_corpus_registry,
)
from finance_agent_core.storage.approval import require_approved_database_paths
from finance_agent_core.storage.identity_cache import ProductIdentitySnapshotCache


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover dated official documents from provided-DB identities and Source Registry "
            "metadata. Output remains pending and cannot activate production retrieval."
        )
    )
    parser.add_argument("--db-bond", type=Path, required=True)
    parser.add_argument("--db-domestic-etp", type=Path, required=True)
    parser.add_argument("--db-overseas-etp", type=Path, required=True)
    parser.add_argument("--db-fund", type=Path, required=True)
    parser.add_argument("--naver-client-id-file", type=Path, required=True)
    parser.add_argument("--naver-client-secret-file", type=Path, required=True)
    parser.add_argument(
        "--gap-queue",
        type=Path,
        help="Coverage gap queue; enables exact-product gap-only collection.",
    )
    parser.add_argument(
        "--existing-pending-manifest",
        action="append",
        default=[],
        type=Path,
        help="Repeatable pending manifest whose URLs must not be fetched again.",
    )
    parser.add_argument("--output-catalog", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--max-products-per-source", type=int, default=24)
    parser.add_argument("--max-queries", type=int, default=600)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-documents", type=int, default=1000)
    parser.add_argument("--request-interval-seconds", type=float, default=0.05)
    parser.add_argument("--fetch-interval-seconds", type=float, default=0.05)
    return parser


def _write_descriptor(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("corpus expansion output write made no progress")
        view = view[written:]
    os.fsync(descriptor)


def _fsync_directories(paths: Sequence[Path]) -> None:
    for parent in sorted({path.parent.resolve() for path in paths}):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_exclusive_bundle(outputs: Sequence[tuple[Path, bytes]]) -> None:
    """Stage complete files, publish create-if-absent, and roll back a partial bundle."""

    if not outputs:
        raise ValueError("corpus expansion output bundle cannot be empty")
    targets = tuple(path for path, _ in outputs)
    canonical_targets = tuple(path.parent.resolve() / path.name for path in targets)
    if len(canonical_targets) != len(set(canonical_targets)):
        raise ValueError("corpus expansion output paths must be distinct")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    if any(os.path.lexists(target) for target in targets):
        raise FileExistsError("corpus expansion output already exists")

    staged: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for target, data in outputs:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            staged.append((temporary, target))
            try:
                _write_descriptor(descriptor, data)
            finally:
                os.close(descriptor)
        if any(os.path.lexists(target) for target in targets):
            raise FileExistsError("corpus expansion output appeared during staging")
        for temporary, target in staged:
            os.link(temporary, target, follow_symlinks=False)
            published.append(target)
        _fsync_directories(targets)
    except BaseException:
        for target in reversed(published):
            target.unlink(missing_ok=True)
        if published:
            _fsync_directories(published)
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _write_exclusive(path: Path, data: bytes) -> None:
    _write_exclusive_bundle(((path, data),))


def _selected_targets_from_queries(
    queries: Sequence[CorpusExpansionQuery],
) -> tuple[dict[str, object], ...]:
    selected: dict[str, dict[str, object]] = {}
    for query in queries:
        if (
            query.task_id is None
            or query.product_family is None
            or query.product_id is None
            or query.product_name is None
            or query.source_owner is None
        ):
            raise RuntimeError("gap query is missing its product provenance")
        target = selected.setdefault(
            query.task_id,
            {
                "task_id": query.task_id,
                "product_family": query.product_family.value,
                "product_id": query.product_id,
                "product_name": query.product_name,
                "source_owner": query.source_owner,
                "capability": query.capability.value,
                "source_registry_ids": [],
                "queries": [],
                "query_targets": [],
            },
        )
        identity = (
            target["product_family"],
            target["product_id"],
            target["product_name"],
            target["source_owner"],
            target["capability"],
        )
        if identity != (
            query.product_family.value,
            query.product_id,
            query.product_name,
            query.source_owner,
            query.capability.value,
        ):
            raise RuntimeError("gap task ID maps to conflicting product provenance")
        source_ids = target["source_registry_ids"]
        query_values = target["queries"]
        query_targets = target["query_targets"]
        assert isinstance(source_ids, list)
        assert isinstance(query_values, list)
        assert isinstance(query_targets, list)
        if query.source_registry_id not in source_ids:
            source_ids.append(query.source_registry_id)
        if query.query not in query_values:
            query_values.append(query.query)
        query_targets.append(
            {
                "source_registry_id": query.source_registry_id,
                "query": query.query,
            }
        )
    return tuple(selected.values())


def _returned_action_counts(queue: GapCollectionQueue) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                item.action.value
                for item in (
                    *queue.items,
                    *queue.document_review_items,
                    *queue.manual_review_items,
                )
            ).items()
        )
    )


def _load_gap_queue(path: Path) -> tuple[GapCollectionQueue, str]:
    raw = path.read_bytes()
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise RuntimeError("coverage gap queue is empty or oversized")
    return GapCollectionQueue.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def main() -> int:
    arguments = _parser().parse_args()
    database_paths = {
        ProductFamily.BOND: arguments.db_bond,
        ProductFamily.DOMESTIC_ETP: arguments.db_domestic_etp,
        ProductFamily.OVERSEAS_ETP: arguments.db_overseas_etp,
        ProductFamily.FUND: arguments.db_fund,
    }
    require_approved_database_paths(database_paths)
    cache = ProductIdentitySnapshotCache(max_entries=4)
    records_by_family = {family: cache.get(path).records for family, path in database_paths.items()}
    registry = load_pending_intake_source_registry()
    queue = None
    queue_sha256 = None
    gap_plan = None
    selected_targets = ()
    existing_urls: set[str] = set()
    if arguments.gap_queue is None:
        if arguments.existing_pending_manifest:
            raise SystemExit("--existing-pending-manifest requires --gap-queue")
        queries = build_corpus_expansion_queries(
            registry,
            records_by_family,
            max_products_per_source=arguments.max_products_per_source,
            max_queries=arguments.max_queries,
        )
        discovery_registry = registry
    else:
        queue, queue_sha256 = _load_gap_queue(arguments.gap_queue)
        gap_plan = build_gap_corpus_expansion_plan(
            queue,
            registry,
            records_by_family,
            max_queries=arguments.max_queries,
        )
        queries = gap_plan.queries
        if not queries:
            raise RuntimeError("coverage gap queue contains no network collection query")
        selected_targets = _selected_targets_from_queries(queries)
        for manifest_path in arguments.existing_pending_manifest:
            manifest = rebind_pending_corpus_registry(
                load_pending_corpus_manifest(manifest_path),
                registry,
            )
            existing_urls.update(document.source_uri for document in manifest.documents)
        discovery_registry = offline_snapshot_discovery_registry(
            registry,
            source_registry_ids=tuple(query.source_registry_id for query in queries),
        )
    discovery = BoundedOfficialDiscoveryProvider(
        NaverApiHubWebSearchTransport(
            client_id_file=arguments.naver_client_id_file,
            client_secret_file=arguments.naver_client_secret_file,
        ),
        discovery_registry,
    )
    candidates, discovery_failure_count = discover_corpus_candidates(
        discovery,
        registry,
        queries,
        top_k=arguments.top_k,
        request_interval_seconds=arguments.request_interval_seconds,
    )
    candidates, excluded_existing_url_count = exclude_existing_corpus_candidates(
        candidates,
        existing_urls=existing_urls,
    )
    existing_url_keys = frozenset(canonical_official_candidate_url(url) for url in existing_urls)
    discovered_catalog, stats = materialize_corpus_catalog(
        candidates,
        connector=OfficialSourceConnector(
            UrllibOfficialHttpTransport(),
            registry,
            url_is_excluded=(
                None
                if not existing_url_keys
                else lambda url: canonical_official_candidate_url(url) in existing_url_keys
            ),
        ),
        registry=registry,
        max_documents=arguments.max_documents,
        fetch_interval_seconds=arguments.fetch_interval_seconds,
    )
    excluded_existing_url_count += stats.excluded_existing_url_count
    packaged_catalog = None if queue is not None else load_pending_corpus_catalog()
    catalog = (
        discovered_catalog
        if packaged_catalog is None
        else merge_pending_catalogs(registry, (discovered_catalog, packaged_catalog))
    )
    catalog_bytes = (catalog.model_dump_json(indent=2) + "\n").encode("utf-8")
    report = {
        "schema_version": "1.0",
        "status": "pending_independent_review_not_authorized_for_runtime",
        "evidence_cutoff": registry.evidence_cutoff.isoformat(),
        "source_registry_sha256": registry.canonical_sha256,
        "catalog_id": catalog.catalog_id,
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "gap_mode": queue is not None,
        "gap_queue_file_sha256": queue_sha256,
        "gap_queue_action_counts": None if queue is None else queue.action_counts,
        "gap_returned_action_counts": (None if queue is None else _returned_action_counts(queue)),
        "selected_network_task_count": len(selected_targets),
        "selected_network_task_ids": [item["task_id"] for item in selected_targets],
        "selected_targets": selected_targets,
        "query_count": len(queries),
        "gap_total_query_count": None if gap_plan is None else gap_plan.total_query_count,
        "query_truncated": None if gap_plan is None else gap_plan.truncated,
        "query_limit": arguments.max_queries,
        "discovery_failure_count": discovery_failure_count,
        "discovered_url_count": stats.discovered_url_count,
        "fetched_url_count": stats.fetched_url_count,
        "fetch_failure_count": stats.fetch_failure_count,
        "navigation_only_count": stats.navigation_only_count,
        "excluded_existing_url_count": excluded_existing_url_count,
        "discovered_document_count": stats.catalog_document_count,
        "packaged_seed_count": 0 if packaged_catalog is None else len(packaged_catalog.documents),
        "catalog_document_count": len(catalog.documents),
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    _write_exclusive_bundle(
        (
            (arguments.output_catalog, catalog_bytes),
            (arguments.output_report, report_bytes),
        )
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
