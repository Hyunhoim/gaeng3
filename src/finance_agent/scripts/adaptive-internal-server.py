#!/usr/bin/env python3
"""Run a loopback-only Adaptive Semantic evaluation server.

This helper is not a deployment entrypoint. It assembles the production KURE
artifact and the real HCLX Semantic Resolver behind the normal official GET
adapter so a frozen internal suite can measure the feature-enabled path without
touching the shared 18001 service.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from app.config import Settings
from app.main import create_app
from fastapi.responses import JSONResponse
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.providers import (
    HyperClovaXCallRecord,
    HyperClovaXHTTPTransport,
    HyperClovaXSemanticResolverProvider,
    HyperClovaXSettings,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.schema_dense_hclx_ab import _load_key_file
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.retrieval.schema_adaptive import ProductionHybridSchemaLinker
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    EmbeddingProviderMetadata,
    SchemaDenseActivationPolicy,
    load_dense_schema_index_artifact,
    p1_precision_field_thresholds,
)


class _CountingEmbeddingProvider:
    def __init__(self, provider: object) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self.query_calls = 0

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._provider.metadata  # type: ignore[no-any-return,union-attr]

    @property
    def artifact_gate_evidence(self) -> object:
        return self._provider.artifact_gate_evidence  # type: ignore[union-attr]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._provider.embed_documents(texts)  # type: ignore[no-any-return,union-attr]

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            self.query_calls += 1
        return self._provider.embed_query(text)  # type: ignore[no-any-return,union-attr]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run loopback-only Adaptive internal server.")
    for family in ProductFamily:
        parser.add_argument(f"--{family.value.replace('_', '-')}-db", type=Path, required=True)
    parser.add_argument("--schema-index", type=Path, required=True)
    parser.add_argument("--schema-index-sha256", required=True)
    parser.add_argument("--calibration-report-sha256", required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--trusted-cache-root", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18003)
    parser.add_argument("--hclx-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    print("[adaptive-eval] load policy and index", file=sys.stderr, flush=True)
    policy = SchemaDenseActivationPolicy(
        dense_min_score=0.340015,
        hclx_candidate_min_score=0.30,
        minimum_margin=0.024703,
        top_k=10,
        calibration_report_sha256=arguments.calibration_report_sha256,
        field_thresholds=p1_precision_field_thresholds(),
    )
    artifact = load_dense_schema_index_artifact(
        arguments.schema_index,
        expected_file_sha256=arguments.schema_index_sha256,
    )
    print("[adaptive-eval] verify KURE snapshot", file=sys.stderr, flush=True)
    provider = _CountingEmbeddingProvider(
        load_verified_schema_embedding_cpu_provider(
            alias="kure-v1",
            snapshot_dir=arguments.snapshot_dir,
            manifest_path=arguments.snapshot_manifest,
            trusted_cache_root=arguments.trusted_cache_root,
            mode="production",
            batch_size=arguments.batch_size,
            cpu_threads=arguments.cpu_threads,
        )
    )
    print("[adaptive-eval] bind KURE index", file=sys.stderr, flush=True)
    index = DenseSchemaIndex(artifact, provider)
    linker = ProductionHybridSchemaLinker(index, policy)
    hclx_records: list[HyperClovaXCallRecord] = []
    hclx_provider = HyperClovaXSemanticResolverProvider(
        HyperClovaXSettings(
            model="HCX-007",
            timeout_seconds=arguments.hclx_timeout_seconds,
        ),
        HyperClovaXHTTPTransport(api_key=_load_key_file(arguments.api_key_file)),
        on_call=hclx_records.append,
    )
    print("[adaptive-eval] assemble four-dataset Agent", file=sys.stderr, flush=True)
    database_paths = {
        family: getattr(arguments, f"{family.value}_db") for family in ProductFamily
    }
    agent = RoutedFinanceAgent(
        database_paths,
        capability_execution_overrides={ProductFamily.FUND},
        require_approved_databases=True,
        adaptive_semantic_resolver=AdaptiveSemanticResolver(
            linker,
            hclx_provider=hclx_provider,
            execution_enabled=True,
        ),
    )
    settings = Settings(
        app_env="development",
        official_answer_timeout_seconds=270.0,
        official_answer_max_inflight=1,
        fund_execution_policy="public_fund_v1_approved",
        bond_db=database_paths[ProductFamily.BOND],
        domestic_etp_db=database_paths[ProductFamily.DOMESTIC_ETP],
        overseas_etp_db=database_paths[ProductFamily.OVERSEAS_ETP],
        fund_db=database_paths[ProductFamily.FUND],
    )
    application = create_app(settings=settings, agent=agent)

    @application.get("/__internal_adaptive_stats", include_in_schema=False)
    def internal_adaptive_stats() -> JSONResponse:
        return JSONResponse(
            {
                "schema_dense_query_calls": provider.query_calls,
                "hclx_semantic_calls": len(hclx_records),
                "hclx_semantic_successes": sum(
                    item.outcome == "success" for item in hclx_records
                ),
                "hclx_semantic_timeouts": sum(
                    item.outcome == "timed_out" for item in hclx_records
                ),
            }
        )

    print(f"[adaptive-eval] listen {arguments.host}:{arguments.port}", file=sys.stderr, flush=True)
    uvicorn.run(
        application,
        host=arguments.host,
        port=arguments.port,
        workers=1,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
