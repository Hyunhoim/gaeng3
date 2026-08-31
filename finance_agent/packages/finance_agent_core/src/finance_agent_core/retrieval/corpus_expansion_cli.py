from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.corpus_expansion import (
    build_corpus_expansion_queries,
    discover_corpus_candidates,
    materialize_corpus_catalog,
    merge_pending_catalogs,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    NaverApiHubWebSearchTransport,
)
from finance_agent_core.retrieval.pending_corpus import load_pending_corpus_catalog
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
    parser.add_argument("--output-catalog", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--max-products-per-source", type=int, default=24)
    parser.add_argument("--max-queries", type=int, default=600)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-documents", type=int, default=1000)
    parser.add_argument("--request-interval-seconds", type=float, default=0.05)
    parser.add_argument("--fetch-interval-seconds", type=float, default=0.05)
    return parser


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    queries = build_corpus_expansion_queries(
        registry,
        records_by_family,
        max_products_per_source=arguments.max_products_per_source,
        max_queries=arguments.max_queries,
    )
    discovery = BoundedOfficialDiscoveryProvider(
        NaverApiHubWebSearchTransport(
            client_id_file=arguments.naver_client_id_file,
            client_secret_file=arguments.naver_client_secret_file,
        ),
        registry,
    )
    candidates, discovery_failure_count = discover_corpus_candidates(
        discovery,
        registry,
        queries,
        top_k=arguments.top_k,
        request_interval_seconds=arguments.request_interval_seconds,
    )
    discovered_catalog, stats = materialize_corpus_catalog(
        candidates,
        connector=OfficialSourceConnector(UrllibOfficialHttpTransport(), registry),
        registry=registry,
        max_documents=arguments.max_documents,
        fetch_interval_seconds=arguments.fetch_interval_seconds,
    )
    packaged_catalog = load_pending_corpus_catalog()
    catalog = merge_pending_catalogs(
        registry,
        (discovered_catalog, packaged_catalog),
    )
    catalog_bytes = (catalog.model_dump_json(indent=2) + "\n").encode("utf-8")
    report = {
        "schema_version": "1.0",
        "status": "pending_independent_review_not_authorized_for_runtime",
        "evidence_cutoff": registry.evidence_cutoff.isoformat(),
        "source_registry_sha256": registry.canonical_sha256,
        "catalog_id": catalog.catalog_id,
        "query_count": len(queries),
        "discovery_failure_count": discovery_failure_count,
        "discovered_url_count": stats.discovered_url_count,
        "fetched_url_count": stats.fetched_url_count,
        "fetch_failure_count": stats.fetch_failure_count,
        "navigation_only_count": stats.navigation_only_count,
        "discovered_document_count": stats.catalog_document_count,
        "packaged_seed_count": len(packaged_catalog.documents),
        "catalog_document_count": len(catalog.documents),
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    _write_exclusive(arguments.output_catalog, catalog_bytes)
    _write_exclusive(arguments.output_report, report_bytes)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
