from __future__ import annotations

import argparse
from pathlib import Path

from finance_agent_core.retrieval.external_knowledge_release import (
    write_live_external_knowledge_bootstrap,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the canonical empty-cache bootstrap for official live evidence."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--temporal-relations", type=Path, required=True)
    arguments = parser.parse_args()
    bootstrap = write_live_external_knowledge_bootstrap(
        artifact_path=arguments.artifact,
        relation_bundle_path=arguments.temporal_relations,
    )
    print(f"external_knowledge_artifact_sha256={bootstrap.artifact_file_sha256}")
    print(f"external_temporal_relation_sha256={bootstrap.relation_bundle_file_sha256}")
    print(
        "external_temporal_relation_set_sha256="
        f"{bootstrap.relation_bundle.manifest.relation_set_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
