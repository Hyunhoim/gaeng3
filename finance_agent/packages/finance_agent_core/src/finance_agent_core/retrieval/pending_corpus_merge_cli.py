from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.pending_corpus import merge_pending_corpus_roots


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge verified pending corpus roots without granting approval authority."
    )
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = merge_pending_corpus_roots(
        arguments.output_root,
        tuple(arguments.source_root),
        registry=load_official_source_registry(),
    )
    print(
        json.dumps(
            {
                "status": manifest.status,
                "catalog_id": manifest.catalog_id,
                "document_count": manifest.document_count,
                "failure_count": len(manifest.failures),
                "requested_document_count": manifest.requested_document_count,
                "manifest_sha256": manifest.canonical_sha256,
                "source_registry_sha256": manifest.source_registry_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
