from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.external_knowledge_router import ExternalKnowledgeRouter
from finance_agent_core.agent.external_search_planning import (
    ExternalSearchPlanCompiler,
    ProvidedProductUniverseBuilder,
)
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.agent.providers import (
    HyperClovaXCallRecord,
    HyperClovaXFinalAnswerProvider,
    HyperClovaXHTTPTransport,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSemanticResolverProvider,
    HyperClovaXSettings,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.evaluation.schema_dense_hclx_ab import _load_key_file
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.execution.external_knowledge_runtime import ExternalKnowledgeRuntime
from finance_agent_core.execution.official_product_facts import (
    SqliteOfficialProductFactProvider,
)
from finance_agent_core.retrieval.document_dense import (
    OfflineHybridDocumentIndex,
    load_document_dense_artifact,
)
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
)
from finance_agent_core.retrieval.external_knowledge_release import (
    build_live_external_knowledge_bootstrap,
)
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    NaverApiHubWebSearchTransport,
)
from finance_agent_core.retrieval.pending_corpus import load_pending_corpus_manifest
from finance_agent_core.retrieval.schema_adaptive import ProductionHybridSchemaLinker
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    SchemaDenseActivationPolicy,
    load_dense_schema_index_artifact,
    p1_precision_field_thresholds,
)
from finance_agent_core.retrieval.temporal_relations import TemporalRelationStore
from finance_agent_core.storage.approval import require_approved_database_paths

from app.config import Settings
from app.dependencies import _build_knowledge_agent

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EXPECTED_SUITE_ID = "finance-agent-independent-holdout-v2-300"
_EXPECTED_SUITE_STATUS = "sealed_question_only_not_used_for_development"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: str, *, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_question_only(path: Path, expected_sha256: str) -> list[dict[str, str]]:
    if _sha256(path) != expected_sha256:
        raise RuntimeError("question-only SHA-256 differs from the sealed value")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise RuntimeError("question-only file is invalid") from error
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if (
        payload.get("suite_id") != _EXPECTED_SUITE_ID
        or payload.get("status") != _EXPECTED_SUITE_STATUS
        or payload.get("case_count") != 300
        or not isinstance(cases, list)
        or len(cases) != 300
    ):
        raise RuntimeError("question-only suite contract differs")
    expected_ids = [f"IH-{index:03d}" for index in range(1, 301)]
    if [item.get("id") for item in cases] != expected_ids:
        raise RuntimeError("question-only IDs differ")
    if any(
        not isinstance(item, dict)
        or set(item) != {"id", "question"}
        or not isinstance(item["question"], str)
        or not item["question"].strip()
        or len(item["question"]) > 2_000
        for item in cases
    ):
        raise RuntimeError("question-only case contains hidden labels or invalid input")
    return cases


def _write_new_read_only(path: Path, payload: object) -> None:
    if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("blind output must be a safe absolute path")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o400,
    )
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_new_journal(path: Path):
    if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("blind journal must be a safe absolute path")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _database_paths(arguments: argparse.Namespace) -> dict[ProductFamily, Path]:
    paths = {
        ProductFamily.BOND: arguments.bond_db,
        ProductFamily.DOMESTIC_ETP: arguments.domestic_etp_db,
        ProductFamily.OVERSEAS_ETP: arguments.overseas_etp_db,
        ProductFamily.FUND: arguments.fund_db,
    }
    if not arguments.development_unapproved_databases:
        require_approved_database_paths(paths)
    return paths


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be a JSON object")
    return payload


def _validate_schema_calibration(
    arguments: argparse.Namespace,
    *,
    schema_artifact: Any,
) -> None:
    if _sha256(arguments.schema_dense_calibration_file) != (
        arguments.schema_dense_calibration_file_sha256
    ):
        raise RuntimeError("Schema Dense calibration file SHA-256 differs")
    report = _load_json_object(
        arguments.schema_dense_calibration_file,
        label="Schema Dense calibration report",
    )
    if (
        report.get("report_sha256") != arguments.schema_dense_calibration_sha256
        or report.get("activation_decision")
        != "internal_selective_gate_passed_independent_blind_pending"
    ):
        raise RuntimeError("Schema Dense calibration decision differs")
    gates = report.get("activation_gates")
    if not isinstance(gates, dict) or any(
        value is not True for key, value in gates.items() if key != "independent_blind_completed"
    ):
        raise RuntimeError("Schema Dense calibration quality gate failed")
    prototype = report.get("corpora", {}).get("schema-field-prototype-v3", {}).get("manifest", {})
    manifest = schema_artifact.manifest
    for key in (
        "corpus_sha256",
        "field_keys_sha256",
        "field_registry_sha256",
        "field_registry_schema_version",
        "vector_artifact_sha256",
    ):
        if prototype.get(key) != getattr(manifest, key):
            raise RuntimeError(f"Schema Dense calibration {key} differs from index")


def _validate_document_calibration(arguments: argparse.Namespace) -> None:
    if _sha256(arguments.document_dense_calibration_file) != (
        arguments.document_dense_calibration_sha256
    ):
        raise RuntimeError("Document Dense calibration SHA-256 differs")
    report = _load_json_object(
        arguments.document_dense_calibration_file,
        label="Document Dense calibration report",
    )
    metrics = report.get("metrics", {})
    dense = metrics.get("dense", {}) if isinstance(metrics, dict) else {}
    hybrid = metrics.get("hybrid_rrf", {}) if isinstance(metrics, dict) else {}
    if (
        report.get("report_kind") != "document_dense_public_calibration_not_blind"
        or report.get("artifact_sha256") != arguments.document_dense_index_sha256
        or dense.get("recall_at_5") != 1
        or hybrid.get("recall_at_5") != 1
    ):
        raise RuntimeError("Document Dense public calibration quality gate failed")


def _build_agent(
    arguments: argparse.Namespace,
) -> tuple[RoutedFinanceAgent, list[HyperClovaXCallRecord], CacheFirstOfficialDiscoveryProvider]:
    database_paths = _database_paths(arguments)
    hcx_settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=45.0)
    calls: list[HyperClovaXCallRecord] = []
    transport = HyperClovaXHTTPTransport(api_key=_load_key_file(arguments.hclx_key_file))

    policy = SchemaDenseActivationPolicy(
        dense_min_score=arguments.schema_dense_min_score,
        hclx_candidate_min_score=arguments.schema_dense_hclx_candidate_min_score,
        minimum_margin=arguments.schema_dense_minimum_margin,
        top_k=10,
        calibration_report_sha256=arguments.schema_dense_calibration_sha256,
        field_thresholds=p1_precision_field_thresholds(),
    )
    schema_artifact = load_dense_schema_index_artifact(
        arguments.schema_dense_index,
        expected_file_sha256=arguments.schema_dense_index_sha256,
    )
    _validate_schema_calibration(arguments, schema_artifact=schema_artifact)
    if schema_artifact.manifest.activation_policy_sha256 != policy.policy_sha256:
        raise RuntimeError("Schema Dense index and activation policy differ")
    embedding = load_verified_schema_embedding_cpu_provider(
        alias="kure-v1",
        snapshot_dir=arguments.kure_snapshot_dir,
        manifest_path=arguments.kure_snapshot_manifest,
        trusted_cache_root=arguments.kure_cache_root,
        mode="production",
        batch_size=16,
        cpu_threads=arguments.kure_cpu_threads,
    )
    schema_linker = ProductionHybridSchemaLinker(
        DenseSchemaIndex(schema_artifact, embedding),
        policy,
    )
    semantic_provider = HyperClovaXSemanticResolverProvider(
        hcx_settings,
        transport,
        on_call=calls.append,
    )

    knowledge_settings = Settings(
        app_env="development",
        fund_execution_policy="public_fund_v1_approved",
        bond_db=database_paths[ProductFamily.BOND],
        domestic_etp_db=database_paths[ProductFamily.DOMESTIC_ETP],
        overseas_etp_db=database_paths[ProductFamily.OVERSEAS_ETP],
        fund_db=database_paths[ProductFamily.FUND],
        relation_retrieval_artifact_file=arguments.relation_artifact,
        relation_retrieval_artifact_sha256_file=arguments.relation_artifact_sha256_file,
        relation_index_file=arguments.relation_index,
    )
    knowledge_agent = _build_knowledge_agent(knowledge_settings, None)
    if knowledge_agent is None:
        raise RuntimeError("provided relation knowledge path did not activate")

    registry = load_official_source_registry()
    live_bootstrap = build_live_external_knowledge_bootstrap()
    relation_store = TemporalRelationStore(live_bootstrap.relation_bundle, registry)
    live_discovery = BoundedOfficialDiscoveryProvider(
        NaverApiHubWebSearchTransport(
            client_id_file=arguments.naver_client_id_file,
            client_secret_file=arguments.naver_client_secret_file,
            timeout_seconds=arguments.discovery_timeout_seconds,
        ),
        registry,
    )
    document_artifact = load_document_dense_artifact(arguments.document_dense_index)
    if _sha256(arguments.document_dense_index) != arguments.document_dense_index_sha256:
        raise RuntimeError("Document Dense artifact SHA-256 differs")
    _validate_document_calibration(arguments)
    pending_manifest = load_pending_corpus_manifest(arguments.pending_corpus_manifest)
    if _sha256(arguments.pending_corpus_manifest) != arguments.pending_corpus_manifest_sha256:
        raise RuntimeError("pending corpus manifest SHA-256 differs")
    document_discovery = CacheFirstOfficialDiscoveryProvider(
        index=OfflineHybridDocumentIndex(document_artifact, embedding),
        artifact=document_artifact,
        corpus_manifest=pending_manifest,
        corpus_manifest_path=arguments.pending_corpus_manifest,
        fallback=live_discovery,
        registry=registry,
        minimum_dense_score=arguments.document_dense_min_score,
        minimum_dense_margin=arguments.document_dense_minimum_margin,
    )
    acquisition = OfficialEvidenceAcquisitionCoordinator(
        relation_store=relation_store,
        discovery_provider=document_discovery,
        connector=OfficialSourceConnector(UrllibOfficialHttpTransport(), registry),
    )
    external_runtime = ExternalKnowledgeRuntime(
        acquisition=acquisition,
        product_fact_provider=SqliteOfficialProductFactProvider(
            database_paths,
            verify_approved_databases=not arguments.development_unapproved_databases,
        ),
        release_artifact=live_bootstrap.artifact,
        release_artifact_file_sha256=live_bootstrap.artifact_file_sha256,
    )
    agent = RoutedFinanceAgent(
        database_paths,
        intent_analysis_provider=HyperClovaXIntentAnalysisProvider(
            hcx_settings,
            transport,
            on_call=calls.append,
        ),
        official_answer_provider=HyperClovaXFinalAnswerProvider(
            hcx_settings,
            transport,
            on_call=calls.append,
        ),
        require_official_hclx=True,
        capability_execution_overrides={ProductFamily.FUND},
        require_approved_databases=not arguments.development_unapproved_databases,
        knowledge_router=DeterministicKnowledgeRouter(),
        knowledge_agent=knowledge_agent,
        external_knowledge_router=ExternalKnowledgeRouter(),
        external_knowledge_runtime=external_runtime,
        external_universe_builder=ProvidedProductUniverseBuilder(
            database_paths,
            verify_approved_databases=not arguments.development_unapproved_databases,
        ),
        external_search_plan_compiler=ExternalSearchPlanCompiler(registry),
        adaptive_semantic_resolver=AdaptiveSemanticResolver(
            schema_linker,
            hclx_provider=semantic_provider,
            execution_enabled=True,
        ),
    )
    return agent, calls, document_discovery


def _case_result(
    *,
    case: dict[str, str],
    agent: RoutedFinanceAgent,
    calls: list[HyperClovaXCallRecord],
    max_retries: int,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    final_adapter = None
    case_calls_start = len(calls)
    case_started = time.perf_counter()
    for attempt_number in range(1, max_retries + 2):
        attempt_started = time.perf_counter()
        with bind_request_deadline(RequestDeadline.after(270.0)):
            adapter = execute_answer_request(
                agent,
                BackendAgentRequest(
                    request_id=case["id"],
                    question=case["question"],
                ),
            )
        attempts.append(
            {
                "attempt": attempt_number,
                "http_status": adapter.http_status_code,
                "duration_ms": round((time.perf_counter() - attempt_started) * 1000, 3),
            }
        )
        final_adapter = adapter
        if adapter.http_status_code == 200:
            break
        if attempt_number <= max_retries:
            time.sleep(0.25 * attempt_number)
    assert final_adapter is not None
    official = official_response_from_backend(
        question_id=case["id"],
        question=case["question"],
        response=final_adapter.response,
    )
    case_calls = calls[case_calls_start:]
    return {
        "id": case["id"],
        "question_sha256": hashlib.sha256(case["question"].encode("utf-8")).hexdigest(),
        "duration_ms": round((time.perf_counter() - case_started) * 1000, 3),
        "attempts": attempts,
        "http_status": final_adapter.http_status_code,
        "backend_response": final_adapter.response.model_dump(mode="json"),
        "official_get_response": official.model_dump(mode="json"),
        "provider_calls": [record.model_dump(mode="json") for record in case_calls],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the sealed 300-case lab-only all-on blind evaluation."
    )
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument(
        "--questions-sha256", type=lambda v: _require_sha256(v, label="questions"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--source-commit", type=str, required=True)
    parser.add_argument(
        "--development-diff-sha256",
        type=lambda v: _require_sha256(v, label="development diff"),
        help=(
            "required for a post-Gold regression that mounts source newer than the "
            "recorded clean commit"
        ),
    )
    parser.add_argument("--image-id", type=str, required=True)
    parser.add_argument(
        "--runner-sha256", type=lambda v: _require_sha256(v, label="runner"), required=True
    )
    parser.add_argument("--hclx-key-file", type=Path, required=True)
    parser.add_argument("--naver-client-id-file", type=Path, required=True)
    parser.add_argument("--naver-client-secret-file", type=Path, required=True)
    parser.add_argument("--bond-db", type=Path, required=True)
    parser.add_argument("--domestic-etp-db", type=Path, required=True)
    parser.add_argument("--overseas-etp-db", type=Path, required=True)
    parser.add_argument("--fund-db", type=Path, required=True)
    parser.add_argument(
        "--development-unapproved-databases",
        action="store_true",
        help="post-Gold development rerun only; never valid for release or NCP deployment",
    )
    parser.add_argument("--relation-artifact", type=Path, required=True)
    parser.add_argument("--relation-artifact-sha256-file", type=Path, required=True)
    parser.add_argument("--relation-index", type=Path, required=True)
    parser.add_argument("--schema-dense-index", type=Path, required=True)
    parser.add_argument(
        "--schema-dense-index-sha256",
        type=lambda v: _require_sha256(v, label="Schema Dense index"),
        required=True,
    )
    parser.add_argument(
        "--schema-dense-calibration-sha256",
        type=lambda v: _require_sha256(v, label="Schema Dense calibration"),
        required=True,
    )
    parser.add_argument("--schema-dense-calibration-file", type=Path, required=True)
    parser.add_argument(
        "--schema-dense-calibration-file-sha256",
        type=lambda v: _require_sha256(v, label="Schema Dense calibration file"),
        required=True,
    )
    parser.add_argument("--schema-dense-min-score", type=float, default=0.382017)
    parser.add_argument("--schema-dense-hclx-candidate-min-score", type=float, default=0.3)
    parser.add_argument("--schema-dense-minimum-margin", type=float, default=0.007688)
    parser.add_argument("--kure-snapshot-dir", type=Path, required=True)
    parser.add_argument("--kure-snapshot-manifest", type=Path, required=True)
    parser.add_argument("--kure-cache-root", type=Path, required=True)
    parser.add_argument("--kure-cpu-threads", type=int, default=2)
    parser.add_argument("--document-dense-index", type=Path, required=True)
    parser.add_argument(
        "--document-dense-index-sha256",
        type=lambda v: _require_sha256(v, label="Document Dense index"),
        required=True,
    )
    parser.add_argument("--document-dense-calibration-file", type=Path, required=True)
    parser.add_argument(
        "--document-dense-calibration-sha256",
        type=lambda v: _require_sha256(v, label="Document Dense calibration"),
        required=True,
    )
    parser.add_argument("--pending-corpus-manifest", type=Path, required=True)
    parser.add_argument(
        "--pending-corpus-manifest-sha256",
        type=lambda v: _require_sha256(v, label="pending corpus manifest"),
        required=True,
    )
    parser.add_argument("--document-dense-min-score", type=float, default=0.55)
    parser.add_argument("--document-dense-minimum-margin", type=float, default=0.03)
    parser.add_argument("--discovery-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="assemble every trusted runtime input without opening the question-only file",
    )
    parser.add_argument(
        "--post-gold-development-regression",
        action="store_true",
        help=(
            "label the output as a post-Gold development regression; this can never "
            "be represented as the first independent Blind prediction"
        ),
    )
    parser.add_argument(
        "--development-case-ids",
        help=(
            "comma-separated IH IDs for a post-Gold smoke regression; the full suite "
            "remains the default"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if _COMMIT.fullmatch(arguments.source_commit) is None:
        raise SystemExit("source commit must be a 40-character lowercase Git SHA")
    if (
        not arguments.image_id.startswith("sha256:")
        or _SHA256.fullmatch(arguments.image_id.removeprefix("sha256:")) is None
    ):
        raise SystemExit("image ID must be a sha256 digest")
    if _sha256(Path(__file__)) != arguments.runner_sha256:
        raise SystemExit("runner SHA-256 differs")
    if not 0 <= arguments.max_retries <= 2:
        raise SystemExit("max retries must be between zero and two")
    if arguments.post_gold_development_regression != bool(arguments.development_diff_sha256):
        raise SystemExit(
            "post-Gold development regression and development diff SHA-256 must be set together"
        )
    if arguments.development_case_ids and not arguments.post_gold_development_regression:
        raise SystemExit("development case selection is forbidden for the first Blind run")
    if arguments.output.exists() or arguments.journal.exists():
        raise SystemExit("blind output or journal already exists")

    # The question-only file is the last input opened. All executable code,
    # models, indexes, credentials and DB trust boundaries must assemble first.
    agent, calls, document_discovery = _build_agent(arguments)
    if arguments.preflight_only:
        print(
            json.dumps(
                {
                    "preflight": "passed_without_opening_question_only",
                    "source_commit": arguments.source_commit,
                    "development_diff_sha256": arguments.development_diff_sha256,
                    "image_id": arguments.image_id,
                    "hclx_model": "HCX-007",
                    "schema_dense_policy_sha256": (
                        agent.adaptive_semantic_resolver.schema_linker.policy.policy_sha256
                    ),
                    "source_registry_sha256": (load_official_source_registry().canonical_sha256),
                    "document_cache_queries": document_discovery.cache_queries,
                    "hclx_calls": len(calls),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    cases = _validate_question_only(arguments.questions, arguments.questions_sha256)
    if arguments.development_case_ids:
        requested_ids = [
            value.strip() for value in arguments.development_case_ids.split(",") if value.strip()
        ]
        if not requested_ids or len(requested_ids) != len(set(requested_ids)):
            raise SystemExit("development case IDs must be a non-empty unique list")
        by_id = {case["id"]: case for case in cases}
        if any(case_id not in by_id for case_id in requested_ids):
            raise SystemExit("development case ID is outside the sealed suite")
        cases = [by_id[case_id] for case_id in requested_ids]
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    journal = _open_new_journal(arguments.journal)
    try:
        for index, case in enumerate(cases, start=1):
            result = _case_result(
                case=case,
                agent=agent,
                calls=calls,
                max_retries=arguments.max_retries,
            )
            results.append(result)
            journal.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            backend = result["backend_response"]
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(cases)}",
                        "id": case["id"],
                        "http_status": result["http_status"],
                        "status": backend["status"],
                        "duration_ms": result["duration_ms"],
                        "provider_operations": [
                            item["operation"] for item in result["provider_calls"]
                        ],
                        "provider_outcomes": [item["outcome"] for item in result["provider_calls"]],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        journal.close()
        if arguments.journal.exists():
            arguments.journal.chmod(0o400)

    database_paths = _database_paths(arguments)
    post_gold = arguments.post_gold_development_regression
    body: dict[str, Any] = {
        "schema_version": "2.0",
        "artifact_kind": (
            "post_gold_development_regression_predictions"
            if post_gold
            else "independent_blind_question_only_first_predictions"
        ),
        "status": (
            "post_gold_development_regression_frozen"
            if post_gold
            else "predictions_frozen_awaiting_gold_reveal"
        ),
        "evaluation_kind": (
            "post_gold_development_regression" if post_gold else "first_independent_blind"
        ),
        "answer_key_opened": post_gold,
        "created_at_utc": created_at,
        "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "implementation_commit": arguments.source_commit,
        "development_diff_sha256": arguments.development_diff_sha256,
        "image_id": arguments.image_id,
        "runner_sha256": arguments.runner_sha256,
        "questions_sha256": arguments.questions_sha256,
        "question_count": len(cases),
        "suite_question_count": 300,
        "database_sha256s": {
            family.value: _sha256(path)
            for family, path in sorted(database_paths.items(), key=lambda item: item[0].value)
        },
        "schema_dense_index_sha256": arguments.schema_dense_index_sha256,
        "schema_dense_activation_policy_sha256": agent.adaptive_semantic_resolver.schema_linker.policy.policy_sha256,
        "schema_dense_calibration_sha256": arguments.schema_dense_calibration_sha256,
        "schema_dense_calibration_file_sha256": (arguments.schema_dense_calibration_file_sha256),
        "kure_snapshot_manifest_sha256": _sha256(arguments.kure_snapshot_manifest),
        "document_dense_index_sha256": arguments.document_dense_index_sha256,
        "document_dense_calibration_sha256": (arguments.document_dense_calibration_sha256),
        "pending_corpus_manifest_sha256": arguments.pending_corpus_manifest_sha256,
        "relation_artifact_sha256": _sha256(arguments.relation_artifact),
        "relation_artifact_sha256_file_sha256": _sha256(arguments.relation_artifact_sha256_file),
        "relation_index_sha256": _sha256(arguments.relation_index),
        "source_registry_sha256": load_official_source_registry().canonical_sha256,
        "hclx_model": "HCX-007",
        "feature_state": {
            "hclx_intent": "on",
            "ontology_lexical": "on",
            "schema_dense": "on_candidate_only",
            "hclx_semantic_resolver": "on_bounded_candidate_selection",
            "sql_python_verifier": "on",
            "official_external_live_search": "on",
            "document_dense": "on_url_candidate_cache_only",
            "provided_db_intersection": "on",
            "hclx_grounded_answer": "on",
            "qwen": "absent",
            "ncp_deployment": "not_performed",
        },
        "document_discovery_stats": {
            "cache_queries": document_discovery.cache_queries,
            "cache_hits": document_discovery.cache_hits,
            "live_fallback_queries": document_discovery.live_fallback_queries,
        },
        "results": results,
    }
    body["canonical_prediction_sha256"] = _canonical_sha256(body)
    _write_new_read_only(arguments.output, body)
    print(
        json.dumps(
            {
                "completed": True,
                "case_count": len(cases),
                "output": str(arguments.output),
                "output_sha256": _sha256(arguments.output),
                "canonical_prediction_sha256": body["canonical_prediction_sha256"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
