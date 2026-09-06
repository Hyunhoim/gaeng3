from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import finance_agent_core
from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.backend_adapter import execute_answer_request
from finance_agent_core.agent.external_knowledge_router import ExternalKnowledgeRouter
from finance_agent_core.agent.external_search_planning import (
    ExternalSearchPlanCompiler,
    ProvidedProductUniverseBuilder,
)
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXCallRecord,
    HyperClovaXFinalAnswerProvider,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSettings,
)
from finance_agent_core.agent.providers.hyperclova_http import HyperClovaXHTTPTransport
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
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
    build_cache_first_discovery_provider,
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
from app.dependencies import (
    _build_knowledge_agent,
    build_audit_sink,
    require_approval_guard,
    resolve_runtime_release,
)
from app.dependencies import build_agent as build_production_agent

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_SECRET_BYTES = 16 * 1024
_REPRESENTATIVE_CASE_COUNT = 100
_SELECTION_ARTIFACT_KIND = "post_gold_representative_case_selection"
_SELECTION_METHOD = "lowest_sha256_of_questions_sha256_colon_case_id"
_SUPPORTED_SUITES = {
    "finance-agent-independent-holdout-v2-300": {
        "id_prefix": "IH",
        "case_count": 300,
        "status": "sealed_question_only_not_used_for_development",
        "phase": "phase1",
    },
    "finance-agent-independent-holdout-phase2-v1-300": {
        "id_prefix": "P2",
        "case_count": 300,
        "status": "sealed_question_only_not_used_for_development",
        "phase": "phase2",
    },
    "finance-agent-development-phase3-v1-600": {
        "id_prefix": "P3",
        "case_count": 600,
        "status": "sealed_question_only",
        "phase": "phase3",
        "purpose": "error_discovery_and_knowledge_expansion",
        "predecessor_question_sha256": {
            "phase1": "080e00c13b37be45d060f1f17f2ac60c899b9b0e0ffb4d2b023b2bdad3918578",
            "phase2": "2132e13e5c1cb9f820bd097f17f534dcd05e0c3eaf59821b4493cf014f0ad5d9",
        },
    },
    "finance-agent-independent-holdout-phase4-v1-1000": {
        "id_prefix": "P4",
        "id_width": 4,
        "case_count": 1000,
        "status": "sealed_question_only_not_used_for_development",
        "phase": 4,
    },
    "finance-agent-final-rehearsal-phase5-v1.1-105": {
        "id_prefix": "P5",
        "id_sets": ("A", "B", "C"),
        "id_width": 2,
        "cases_per_set": 35,
        "case_count": 105,
        "status": "sealed_question_only_not_used_for_development",
        "phase": 5,
        "revision": "v1.1-official-examples",
    },
    "finance-agent-generalization-phase6-v1-150": {
        "id_prefix": "P6",
        "case_count": 150,
        "status": "sealed_question_only_not_used_for_development_before_run",
        "phase": 6,
    },
    "finance-agent-final-evaluation-phase7-v2-200": {
        "id_prefix": "P7V2",
        "case_count": 200,
        "status": "sealed_question_only_not_used_for_development",
        "phase": None,
    },
    "finance-agent-paraphrase-evaluation-phase8-200": {
        "id_prefix": "P8",
        "case_count": 200,
        "status": "sealed_question_only_not_used_for_development",
        "phase": None,
    },
}


def _load_owner_only_secret_file(path: Path) -> str:
    """Read one immutable owner-only secret without evaluation-module coupling."""

    if not path.is_absolute():
        raise RuntimeError("secret file must be absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
        ):
            raise RuntimeError("secret file is insecure")
        payload = os.read(descriptor, before.st_size)
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if len(payload) != before.st_size or identity(before) != identity(after):
            raise RuntimeError("secret file changed while loading")
        if identity(after) != identity(current):
            raise RuntimeError("secret file changed while loading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        value = payload.decode("ascii").strip()
    except (UnicodeDecodeError, UnboundLocalError):
        raise RuntimeError("secret file is invalid") from None
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("secret file is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    """Bind every runtime source file, including safe untracked development files."""

    digest = hashlib.sha256()
    for path in sorted(
        (
            item
            for item in root.rglob("*")
            if item.is_file()
            and not item.is_symlink()
            and "__pycache__" not in item.parts
            and item.suffix != ".pyc"
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(path)))
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


def _representative_case_ids(
    *,
    questions_sha256: str,
    case_ids: list[str],
    case_count: int = _REPRESENTATIVE_CASE_COUNT,
) -> list[str]:
    """Select a deterministic Gold-independent sample and preserve suite order."""

    if len(case_ids) < case_count or len(case_ids) != len(set(case_ids)):
        raise RuntimeError("case selection source IDs are invalid")
    ranked = sorted(
        case_ids,
        key=lambda case_id: (
            hashlib.sha256(f"{questions_sha256}:{case_id}".encode()).hexdigest(),
            case_id,
        ),
    )
    selected = set(ranked[:case_count])
    return [case_id for case_id in case_ids if case_id in selected]


def _validate_case_selection(
    path: Path,
    expected_file_sha256: str,
    *,
    suite_id: str,
    questions_sha256: str,
    available_case_ids: list[str],
) -> dict[str, Any]:
    """Load one canonical post-Gold sample without consulting answer labels."""

    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeError("case selection must be an absolute regular file")
    if _sha256(path) != expected_file_sha256:
        raise RuntimeError("case selection file SHA-256 differs")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise RuntimeError("case selection file is invalid") from error
    if not isinstance(payload, dict):
        raise TypeError("case selection file must be a JSON object")
    required = {
        "schema_version",
        "artifact_kind",
        "suite_id",
        "questions_sha256",
        "selection_method",
        "case_count",
        "selected_ids",
        "selection_sha256",
    }
    if set(payload) != required:
        raise RuntimeError("case selection schema differs")
    selected_ids = payload.get("selected_ids")
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("artifact_kind") != _SELECTION_ARTIFACT_KIND
        or payload.get("suite_id") != suite_id
        or payload.get("questions_sha256") != questions_sha256
        or payload.get("selection_method") != _SELECTION_METHOD
        or payload.get("case_count") != _REPRESENTATIVE_CASE_COUNT
        or not isinstance(selected_ids, list)
        or any(not isinstance(value, str) for value in selected_ids)
    ):
        raise RuntimeError("case selection contract differs")
    expected_ids = _representative_case_ids(
        questions_sha256=questions_sha256,
        case_ids=available_case_ids,
    )
    if selected_ids != expected_ids:
        raise RuntimeError("case selection IDs differ from the deterministic sample")
    canonical = dict(payload)
    claimed_sha256 = canonical.pop("selection_sha256")
    if claimed_sha256 != _canonical_sha256(canonical):
        raise RuntimeError("case selection canonical SHA-256 differs")
    return payload


def _require_sha256(value: str, *, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _verify_pre_gold_source_state(arguments: argparse.Namespace) -> str | None:
    expected = arguments.pre_gold_development_diff_sha256
    diff_file = arguments.pre_gold_development_diff_file
    if expected is None and diff_file is None:
        return None
    if expected is None or diff_file is None:
        raise RuntimeError("pre-Gold development diff file and SHA-256 must be set together")
    actual = _sha256(diff_file)
    if actual != expected:
        raise RuntimeError("pre-Gold development diff SHA-256 differs")
    return actual


def _runtime_source_provenance() -> dict[str, str]:
    """Prove that the runner imports Core from the mounted source tree.

    Recording a requested Git commit is insufficient when an old image or
    legacy ``PYTHONPATH`` actually supplies the imported package.  In a source
    checkout, refuse that mismatch before opening the sealed questions and
    bind the two execution-critical modules by SHA-256. Installed production
    images have no sibling source tree and retain the immutable-image trust
    boundary instead.
    """

    core_init = Path(finance_agent_core.__file__).resolve()
    core_root = core_init.parent
    source_root = Path(__file__).resolve().parents[2]
    expected_core_root = (
        source_root / "finance_agent" / "packages" / "finance_agent_core" / "src"
        / "finance_agent_core"
    )
    if expected_core_root.is_dir() and core_root != expected_core_root.resolve():
        raise RuntimeError("runtime finance_agent_core import differs from mounted source")
    routed_service = core_root / "agent" / "routed_service.py"
    if not routed_service.is_file():
        raise RuntimeError("runtime routed service source is unavailable")
    return {
        "finance_agent_core_init_sha256": _sha256(core_init),
        "routed_service_sha256": _sha256(routed_service),
        "finance_agent_core_tree_sha256": _source_tree_sha256(core_root),
        "fastapi_backend_app_tree_sha256": _source_tree_sha256(
            Path(__file__).resolve().parents[1] / "app"
        ),
    }


def _validate_question_only(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if _sha256(path) != expected_sha256:
        raise RuntimeError("question-only SHA-256 differs from the sealed value")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise RuntimeError("question-only file is invalid") from error
    cases = payload.get("cases") if isinstance(payload, dict) else None
    suite_id = payload.get("suite_id")
    contract = _SUPPORTED_SUITES.get(suite_id)
    if contract is None:
        raise RuntimeError("question-only suite contract differs")
    id_prefix = contract["id_prefix"]
    case_count = contract["case_count"]
    if (
        payload.get("status") != contract["status"]
        or payload.get("phase") != contract["phase"]
        or payload.get("case_count") != case_count
        or not isinstance(cases, list)
        or len(cases) != case_count
        or (contract.get("purpose") is not None and payload.get("purpose") != contract["purpose"])
        or (
            contract.get("predecessor_question_sha256") is not None
            and payload.get("predecessor_question_sha256")
            != contract["predecessor_question_sha256"]
        )
    ):
        raise RuntimeError("question-only suite contract differs")
    id_width = int(contract.get("id_width", 3))
    id_sets = contract.get("id_sets")
    if id_sets is None:
        expected_ids = [
            f"{id_prefix}-{index:0{id_width}d}" for index in range(1, case_count + 1)
        ]
    else:
        cases_per_set = int(contract["cases_per_set"])
        expected_ids = [
            f"{id_prefix}-{set_id}-{index:0{id_width}d}"
            for set_id in id_sets
            for index in range(1, cases_per_set + 1)
        ]
        expected_sets = [
            {
                "set_id": set_id,
                "case_count": cases_per_set,
                "id_range": [
                    f"{id_prefix}-{set_id}-{1:0{id_width}d}",
                    f"{id_prefix}-{set_id}-{cases_per_set:0{id_width}d}",
                ],
            }
            for set_id in id_sets
        ]
        if (
            payload.get("revision") != contract["revision"]
            or payload.get("set_count") != len(id_sets)
            or payload.get("cases_per_set") != cases_per_set
            or payload.get("sets") != expected_sets
        ):
            raise RuntimeError("question-only suite contract differs")
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
    metadata = {
        "suite_id": suite_id,
        "phase": payload.get("phase"),
        "phase1_reference": payload.get("phase1_reference"),
        "predecessor_question_sha256": payload.get("predecessor_question_sha256"),
        "suite_case_count": case_count,
    }
    return metadata, cases


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
    bm25 = metrics.get("bm25", {}) if isinstance(metrics, dict) else {}
    dense = metrics.get("dense", {}) if isinstance(metrics, dict) else {}
    hybrid = metrics.get("hybrid_rrf", {}) if isinstance(metrics, dict) else {}
    numeric_keys = ("top1_accuracy", "recall_at_5")
    if any(
        not isinstance(channel.get(key), (int, float))
        for channel in (bm25, dense, hybrid)
        for key in numeric_keys
    ):
        raise RuntimeError("Document Dense public calibration quality gate failed")
    if (
        report.get("report_kind") != "document_dense_public_calibration_not_blind"
        or report.get("artifact_sha256") != arguments.document_dense_index_sha256
        or hybrid.get("recall_at_5") != 1
        or dense.get("recall_at_5", 0) < 0.95
        or dense["top1_accuracy"] < bm25["top1_accuracy"]
        or hybrid["recall_at_5"] < max(
            bm25["recall_at_5"],
            dense["recall_at_5"],
        )
    ):
        raise RuntimeError("Document Dense public calibration quality gate failed")


def _build_lab_live_agent(
    arguments: argparse.Namespace,
) -> tuple[
    RoutedFinanceAgent,
    list[HyperClovaXCallRecord],
    CacheFirstOfficialDiscoveryProvider,
    Any | None,
]:
    database_paths = _database_paths(arguments)
    hcx_settings = HyperClovaXSettings(model="HCX-007", timeout_seconds=45.0)
    calls: list[HyperClovaXCallRecord] = []
    transport = HyperClovaXHTTPTransport(
        api_key=_load_owner_only_secret_file(arguments.hclx_key_file)
    )

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
    document_discovery = build_cache_first_discovery_provider(
        index=OfflineHybridDocumentIndex(document_artifact, embedding),
        artifact=document_artifact,
        corpus_manifest=pending_manifest,
        corpus_manifest_path=arguments.pending_corpus_manifest,
        fallback=live_discovery,
        registry=registry,
        minimum_dense_score=arguments.document_dense_min_score,
        minimum_dense_margin=arguments.document_dense_minimum_margin,
        # This runner is an explicitly post-Gold, non-release laboratory
        # regression.  It may evaluate the SHA-bound pending corpus bytes, but
        # the release constructor remains fail-closed until independent corpus
        # approval produces a DocumentCacheActivationReceipt.
        allow_pending_snapshot_evidence=True,
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
            require_execution_structure=True,
            allow_entity_alias_repair=True,
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
            execution_enabled=True,
        ),
    )
    return agent, calls, document_discovery, None


def _required_path(arguments: argparse.Namespace, name: str) -> Path:
    value = getattr(arguments, name, None)
    if not isinstance(value, Path):
        raise TypeError(f"approved cache-only mode requires --{name.replace('_', '-')}")
    return value


def _required_text(arguments: argparse.Namespace, name: str) -> str:
    value = getattr(arguments, name, None)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"approved cache-only mode requires --{name.replace('_', '-')}")
    return value


def _build_approved_cache_only_agent(
    arguments: argparse.Namespace,
) -> tuple[
    RoutedFinanceAgent,
    list[HyperClovaXCallRecord],
    CacheFirstOfficialDiscoveryProvider,
    Any,
]:
    """Assemble the signed production path while forbidding every live input."""

    forbidden = {
        "naver-client-id-file": getattr(arguments, "naver_client_id_file", None),
        "naver-client-secret-file": getattr(arguments, "naver_client_secret_file", None),
        "pending-corpus-manifest": getattr(arguments, "pending_corpus_manifest", None),
        "pending-corpus-manifest-sha256": getattr(
            arguments,
            "pending_corpus_manifest_sha256",
            None,
        ),
        "relation-artifact-sha256-file": getattr(
            arguments,
            "relation_artifact_sha256_file",
            None,
        ),
    }
    configured_forbidden = sorted(name for name, value in forbidden.items() if value is not None)
    if configured_forbidden:
        raise RuntimeError(
            "approved cache-only mode forbids live, pending, and sidecar trust inputs: "
            + ", ".join(configured_forbidden)
        )

    runtime_audit_file = _required_path(arguments, "runtime_audit_file")
    if runtime_audit_file.exists() or runtime_audit_file.is_symlink():
        raise RuntimeError("approved cache-only runtime audit file must not already exist")
    relation_artifact_sha256 = _required_text(arguments, "relation_artifact_sha256")
    _require_sha256(relation_artifact_sha256, label="relation artifact")
    if _sha256(arguments.document_dense_index) != arguments.document_dense_index_sha256:
        raise RuntimeError("approved Document Dense artifact SHA-256 differs")

    settings = Settings(
        app_env="evaluation",
        app_version=arguments.backend_version,
        answer_provider="hyperclova",
        hcx_query_plan_enabled=False,
        hcx_intent_analysis_enabled=True,
        hcx_semantic_resolver_enabled=True,
        llm_mode="evaluation",
        llm_provider="hyperclova",
        hcx_model="HCX-007",
        hcx_timeout_seconds=45.0,
        clovastudio_api_key_file=arguments.hclx_key_file,
        official_answer_timeout_seconds=270.0,
        official_answer_max_inflight=arguments.official_answer_max_inflight,
        web_concurrency=1,
        audit_mode="jsonl",
        audit_file=runtime_audit_file,
        audit_queue_capacity=2_048,
        audit_shutdown_timeout_seconds=5.0,
        audit_fsync_each_event=True,
        fund_execution_policy="public_fund_v1_approved",
        release_manifest_file=_required_path(arguments, "release_manifest_file"),
        deployment_binding_file=_required_path(arguments, "deployment_binding_file"),
        deployment_binding_sha256=_required_text(arguments, "deployment_binding_sha256"),
        source_commit=arguments.source_commit,
        runtime_image_reference=_required_text(arguments, "runtime_image_reference"),
        runtime_platform=arguments.runtime_platform,
        dense_schema_linker_enabled=True,
        adaptive_semantic_enabled=True,
        schema_dense_index_file=arguments.schema_dense_index,
        schema_dense_index_sha256=arguments.schema_dense_index_sha256,
        schema_dense_calibration_report_sha256=(arguments.schema_dense_calibration_sha256),
        schema_dense_min_score=arguments.schema_dense_min_score,
        schema_dense_hclx_candidate_min_score=(
            arguments.schema_dense_hclx_candidate_min_score
        ),
        schema_dense_minimum_margin=arguments.schema_dense_minimum_margin,
        schema_dense_top_k=10,
        kure_snapshot_dir=arguments.kure_snapshot_dir,
        kure_snapshot_manifest_file=arguments.kure_snapshot_manifest,
        kure_trusted_cache_root=arguments.kure_cache_root,
        kure_cpu_threads=arguments.kure_cpu_threads,
        kure_batch_size=16,
        product_dense_enabled=False,
        relation_retrieval_artifact_file=arguments.relation_artifact,
        relation_retrieval_artifact_sha256=relation_artifact_sha256,
        relation_index_file=arguments.relation_index,
        external_knowledge_enabled=True,
        external_knowledge_artifact_file=_required_path(
            arguments,
            "external_knowledge_artifact_file",
        ),
        external_knowledge_artifact_sha256=_required_text(
            arguments,
            "external_knowledge_artifact_sha256",
        ),
        external_temporal_relation_file=_required_path(
            arguments,
            "external_temporal_relation_file",
        ),
        external_temporal_relation_sha256=_required_text(
            arguments,
            "external_temporal_relation_sha256",
        ),
        external_document_dense_artifact_file=arguments.document_dense_index,
        external_document_cache_activation_file=_required_path(
            arguments,
            "document_cache_activation_file",
        ),
        external_document_corpus_manifest_file=_required_path(
            arguments,
            "approved_corpus_manifest",
        ),
        external_document_corpus_root=_required_path(
            arguments,
            "approved_corpus_root",
        ),
        naver_api_hub_client_id_file=None,
        naver_api_hub_client_secret_file=None,
        bond_db=arguments.bond_db,
        domestic_etp_db=arguments.domestic_etp_db,
        overseas_etp_db=arguments.overseas_etp_db,
        fund_db=arguments.fund_db,
    )
    release_guard = resolve_runtime_release(settings)
    if release_guard is None:
        raise RuntimeError("approved cache-only mode requires a resolved signed release")
    audit_sink = build_audit_sink(settings)
    if audit_sink is None:
        raise RuntimeError("approved cache-only mode requires durable JSONL audit")
    try:
        service = build_production_agent(
            settings,
            release_guard=release_guard,
            audit_sink=audit_sink,
        )
        service = require_approval_guard(
            service,
            settings,
            release_guard,
            audit_sink,
        )
        if type(service) is not RoutedFinanceAgent:
            raise RuntimeError("approved cache-only runtime Agent type differs")
        runtime = service.external_knowledge_runtime
        signed = release_guard.manifest.components.knowledge_retrieval.external
        if runtime is None or signed.artifact is None:
            raise RuntimeError("approved cache-only external Runtime is missing")
        discovery = runtime.acquisition.discovery_provider
        if (
            signed.status != "activated"
            or signed.artifact.retrieval_mode != "approved_corpus_cache_only"
            or type(discovery) is not CacheFirstOfficialDiscoveryProvider
            or not discovery.production_approved
            or discovery.live_fallback_enabled
            or discovery.fallback is not None
            or discovery.activation_binding_sha256
            != signed.artifact.document_activation_receipt_sha256
        ):
            raise RuntimeError("signed external Runtime is not approved cache-only")
    except Exception:
        audit_sink.close(timeout_seconds=settings.audit_shutdown_timeout_seconds)
        raise
    return service, [], discovery, audit_sink


def _build_agent(
    arguments: argparse.Namespace,
) -> tuple[
    RoutedFinanceAgent,
    list[HyperClovaXCallRecord],
    CacheFirstOfficialDiscoveryProvider,
    Any | None,
]:
    if arguments.external_mode == "approved-cache-only":
        return _build_approved_cache_only_agent(arguments)
    return _build_lab_live_agent(arguments)


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
        if adapter.response.error is not None and not adapter.response.error.retryable:
            # A programming/contract failure cannot heal on an immediate
            # retry.  Repeating the whole request needlessly repeats HCLX
            # Intent and the bounded Semantic Resolver.  Transient provider
            # errors keep their existing retry budget.
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
        description="Run a registered sealed lab-only all-on evaluation suite."
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
    parser.add_argument(
        "--pre-gold-development-diff-file",
        type=Path,
        help="read-only binary Git diff generated from the recorded source commit",
    )
    parser.add_argument(
        "--pre-gold-development-diff-sha256",
        type=lambda v: _require_sha256(v, label="pre-Gold development diff"),
        help=(
            "exact tracked Git diff for a first Blind run that evaluates reviewed "
            "development changes before the answer key is opened"
        ),
    )
    parser.add_argument("--image-id", type=str, required=True)
    parser.add_argument(
        "--external-mode",
        choices=("lab-live-pending", "approved-cache-only"),
        default="lab-live-pending",
        help=(
            "use the legacy post-Gold lab path or the exact signed production "
            "cache-only assembly"
        ),
    )
    parser.add_argument(
        "--runner-sha256", type=lambda v: _require_sha256(v, label="runner"), required=True
    )
    parser.add_argument("--hclx-key-file", type=Path, required=True)
    parser.add_argument("--naver-client-id-file", type=Path)
    parser.add_argument("--naver-client-secret-file", type=Path)
    parser.add_argument("--bond-db", type=Path, required=True)
    parser.add_argument("--domestic-etp-db", type=Path, required=True)
    parser.add_argument("--overseas-etp-db", type=Path, required=True)
    parser.add_argument("--fund-db", type=Path, required=True)
    parser.add_argument(
        "--development-unapproved-databases",
        action="store_true",
        help=(
            "lab-only frozen database mode; every DB SHA-256 is recorded, but the "
            "result is never valid for release or NCP deployment"
        ),
    )
    parser.add_argument("--relation-artifact", type=Path, required=True)
    parser.add_argument("--relation-artifact-sha256-file", type=Path)
    parser.add_argument(
        "--relation-artifact-sha256",
        type=lambda v: _require_sha256(v, label="relation artifact"),
    )
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
    parser.add_argument("--schema-dense-min-score", type=float, default=0.340015)
    parser.add_argument("--schema-dense-hclx-candidate-min-score", type=float, default=0.3)
    parser.add_argument("--schema-dense-minimum-margin", type=float, default=0.034684)
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
    parser.add_argument("--document-dense-calibration-file", type=Path)
    parser.add_argument(
        "--document-dense-calibration-sha256",
        type=lambda v: _require_sha256(v, label="Document Dense calibration"),
    )
    parser.add_argument("--pending-corpus-manifest", type=Path)
    parser.add_argument(
        "--pending-corpus-manifest-sha256",
        type=lambda v: _require_sha256(v, label="pending corpus manifest"),
    )
    parser.add_argument("--release-manifest-file", type=Path)
    parser.add_argument("--deployment-binding-file", type=Path)
    parser.add_argument(
        "--deployment-binding-sha256",
        type=lambda v: _require_sha256(v, label="deployment binding"),
    )
    parser.add_argument("--runtime-image-reference")
    parser.add_argument(
        "--runtime-platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    parser.add_argument("--backend-version", default="0.1.0")
    parser.add_argument("--runtime-audit-file", type=Path)
    parser.add_argument("--external-knowledge-artifact-file", type=Path)
    parser.add_argument(
        "--external-knowledge-artifact-sha256",
        type=lambda v: _require_sha256(v, label="external knowledge artifact"),
    )
    parser.add_argument("--external-temporal-relation-file", type=Path)
    parser.add_argument(
        "--external-temporal-relation-sha256",
        type=lambda v: _require_sha256(v, label="external temporal relation"),
    )
    parser.add_argument("--document-cache-activation-file", type=Path)
    parser.add_argument("--approved-corpus-manifest", type=Path)
    parser.add_argument("--approved-corpus-root", type=Path)
    parser.add_argument("--official-answer-max-inflight", type=int, default=2)
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
    parser.add_argument(
        "--case-selection-file",
        type=Path,
        help="canonical deterministic 100-case selection manifest (post-Gold only)",
    )
    parser.add_argument(
        "--case-selection-sha256",
        type=lambda v: _require_sha256(v, label="case selection"),
    )
    return parser


def _validate_mode_arguments(arguments: argparse.Namespace) -> None:
    selection_configured = (
        arguments.case_selection_file is not None,
        arguments.case_selection_sha256 is not None,
    )
    if selection_configured[0] != selection_configured[1]:
        raise SystemExit("case selection file and SHA-256 must be set together")
    if any(selection_configured) and not arguments.post_gold_development_regression:
        raise SystemExit("case selection is allowed only for a post-Gold regression")
    if arguments.development_case_ids and any(selection_configured):
        raise SystemExit("development case IDs and canonical case selection are mutually exclusive")
    if not 1 <= arguments.official_answer_max_inflight <= 8:
        raise SystemExit("official answer max inflight must be between one and eight")
    if arguments.external_mode == "lab-live-pending":
        required = {
            "naver client ID file": arguments.naver_client_id_file,
            "naver client secret file": arguments.naver_client_secret_file,
            "relation artifact SHA-256 file": arguments.relation_artifact_sha256_file,
            "pending corpus manifest": arguments.pending_corpus_manifest,
            "pending corpus manifest SHA-256": arguments.pending_corpus_manifest_sha256,
            "Document Dense calibration file": arguments.document_dense_calibration_file,
            "Document Dense calibration SHA-256": (
                arguments.document_dense_calibration_sha256
            ),
        }
        missing = sorted(label for label, value in required.items() if value is None)
        if missing:
            raise SystemExit("lab live/pending mode requires " + ", ".join(missing))
        if arguments.relation_artifact_sha256 is not None:
            raise SystemExit("lab live/pending mode requires the SHA sidecar, not a direct hash")
        return
    if arguments.development_unapproved_databases:
        raise SystemExit("approved cache-only mode forbids unapproved databases")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _validate_mode_arguments(arguments)
    if _COMMIT.fullmatch(arguments.source_commit) is None:
        raise SystemExit("source commit must be a 40-character lowercase Git SHA")
    if (
        not arguments.image_id.startswith("sha256:")
        or _SHA256.fullmatch(arguments.image_id.removeprefix("sha256:")) is None
    ):
        raise SystemExit("image ID must be a sha256 digest")
    if _sha256(Path(__file__)) != arguments.runner_sha256:
        raise SystemExit("runner SHA-256 differs")
    runtime_source_provenance = _runtime_source_provenance()
    if not 0 <= arguments.max_retries <= 2:
        raise SystemExit("max retries must be between zero and two")
    if arguments.post_gold_development_regression != bool(arguments.development_diff_sha256):
        raise SystemExit(
            "post-Gold development regression and development diff SHA-256 must be set together"
        )
    if (
        arguments.pre_gold_development_diff_sha256 is not None
        and arguments.post_gold_development_regression
    ):
        raise SystemExit("pre-Gold and post-Gold development modes are mutually exclusive")
    if arguments.development_case_ids and not arguments.post_gold_development_regression:
        raise SystemExit("development case selection is forbidden for the first Blind run")
    if arguments.output.exists() or arguments.journal.exists():
        raise SystemExit("blind output or journal already exists")

    verified_pre_gold_diff = _verify_pre_gold_source_state(arguments)

    # The question-only file is the last input opened. All executable code,
    # models, indexes, credentials and DB trust boundaries must assemble first.
    agent, calls, document_discovery, audit_sink = _build_agent(arguments)
    if arguments.preflight_only:
        try:
            print(
                json.dumps(
                    {
                        "preflight": "passed_without_opening_question_only",
                        "source_commit": arguments.source_commit,
                        "development_diff_sha256": arguments.development_diff_sha256,
                        "pre_gold_development_diff_sha256": verified_pre_gold_diff,
                        "image_id": arguments.image_id,
                        "runtime_source_provenance": runtime_source_provenance,
                        "external_mode": arguments.external_mode,
                        "document_cache_production_approved": (
                            document_discovery.production_approved
                        ),
                        "document_live_fallback_enabled": (
                            document_discovery.live_fallback_enabled
                        ),
                        "hclx_model": "HCX-007",
                        "schema_dense_policy_sha256": (
                            agent.adaptive_semantic_resolver.schema_linker.policy.policy_sha256
                        ),
                        "source_registry_sha256": (
                            load_official_source_registry().canonical_sha256
                        ),
                        "document_cache_queries": document_discovery.cache_queries,
                        "document_cache_evidence_hits": (
                            document_discovery.cache_evidence_hits
                        ),
                        "hclx_calls": len(calls),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        finally:
            if audit_sink is not None and not audit_sink.close(timeout_seconds=5.0):
                raise RuntimeError("runtime audit did not close cleanly")
        return 0
    try:
        # Re-check after all runtime inputs assemble. A concurrent edit must never
        # cross the boundary between the sealed source state and question exposure.
        verified_pre_gold_diff = _verify_pre_gold_source_state(arguments)
        if _runtime_source_provenance() != runtime_source_provenance:
            raise RuntimeError("runtime source changed during preflight")
        suite_metadata, cases = _validate_question_only(
            arguments.questions,
            arguments.questions_sha256,
        )
        case_selection: dict[str, Any] | None = None
        if arguments.case_selection_file is not None:
            case_selection = _validate_case_selection(
                arguments.case_selection_file,
                arguments.case_selection_sha256,
                suite_id=suite_metadata["suite_id"],
                questions_sha256=arguments.questions_sha256,
                available_case_ids=[case["id"] for case in cases],
            )
            selected_ids = set(case_selection["selected_ids"])
            cases = [case for case in cases if case["id"] in selected_ids]
        if arguments.development_case_ids:
            requested_ids = [
                value.strip()
                for value in arguments.development_case_ids.split(",")
                if value.strip()
            ]
            if not requested_ids or len(requested_ids) != len(set(requested_ids)):
                raise SystemExit("development case IDs must be a non-empty unique list")
            by_id = {case["id"]: case for case in cases}
            if any(case_id not in by_id for case_id in requested_ids):
                raise SystemExit("development case ID is outside the sealed suite")
            cases = [by_id[case_id] for case_id in requested_ids]
    except BaseException:
        if audit_sink is not None:
            audit_sink.close(timeout_seconds=5.0)
        raise
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    try:
        journal = _open_new_journal(arguments.journal)
        if audit_sink is not None:
            audit_sink.start()
    except BaseException:
        if audit_sink is not None:
            audit_sink.close(timeout_seconds=5.0)
        raise
    audit_flush_succeeded = True
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
        if audit_sink is not None:
            audit_flush_succeeded = audit_sink.close(timeout_seconds=5.0)
    if not audit_flush_succeeded:
        raise RuntimeError("runtime audit did not flush cleanly")

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
        "pre_gold_development_diff_sha256": verified_pre_gold_diff,
        "image_id": arguments.image_id,
        "runtime_source_provenance": runtime_source_provenance,
        "runner_sha256": arguments.runner_sha256,
        "questions_sha256": arguments.questions_sha256,
        "case_selection_file_sha256": (
            arguments.case_selection_sha256 if case_selection is not None else None
        ),
        "case_selection_sha256": (
            case_selection["selection_sha256"] if case_selection is not None else None
        ),
        "case_selection_method": (
            case_selection["selection_method"] if case_selection is not None else None
        ),
        "selected_case_ids": (
            case_selection["selected_ids"] if case_selection is not None else None
        ),
        "suite_id": suite_metadata["suite_id"],
        "suite_phase": suite_metadata["phase"],
        "phase1_reference": suite_metadata["phase1_reference"],
        "question_count": len(cases),
        "suite_question_count": suite_metadata["suite_case_count"],
        "predecessor_question_sha256": suite_metadata["predecessor_question_sha256"],
        "database_sha256s": {
            family.value: _sha256(path)
            for family, path in sorted(database_paths.items(), key=lambda item: item[0].value)
        },
        "database_approval_mode": (
            "lab_frozen_sha256_not_release_approved"
            if arguments.development_unapproved_databases
            else "release_approved"
        ),
        "schema_dense_index_sha256": arguments.schema_dense_index_sha256,
        "schema_dense_activation_policy_sha256": (
            agent.adaptive_semantic_resolver.schema_linker.policy.policy_sha256
        ),
        "schema_dense_calibration_sha256": arguments.schema_dense_calibration_sha256,
        "schema_dense_calibration_file_sha256": (arguments.schema_dense_calibration_file_sha256),
        "kure_snapshot_manifest_sha256": _sha256(arguments.kure_snapshot_manifest),
        "document_dense_index_sha256": arguments.document_dense_index_sha256,
        "document_dense_calibration_sha256": (
            arguments.document_dense_calibration_sha256
            if arguments.external_mode == "lab-live-pending"
            else None
        ),
        "pending_corpus_manifest_sha256": (
            arguments.pending_corpus_manifest_sha256
            if arguments.external_mode == "lab-live-pending"
            else None
        ),
        "approved_corpus_manifest_sha256": (
            _sha256(arguments.approved_corpus_manifest)
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "document_cache_activation_sha256": (
            _sha256(arguments.document_cache_activation_file)
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "relation_artifact_sha256": (
            arguments.relation_artifact_sha256
            if arguments.external_mode == "approved-cache-only"
            else _sha256(arguments.relation_artifact)
        ),
        "relation_artifact_sha256_file_sha256": (
            _sha256(arguments.relation_artifact_sha256_file)
            if arguments.external_mode == "lab-live-pending"
            else None
        ),
        "relation_index_sha256": _sha256(arguments.relation_index),
        "external_mode": arguments.external_mode,
        "release_manifest_sha256": (
            _sha256(arguments.release_manifest_file)
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "deployment_binding_sha256": (
            arguments.deployment_binding_sha256
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "external_knowledge_artifact_sha256": (
            arguments.external_knowledge_artifact_sha256
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "external_temporal_relation_sha256": (
            arguments.external_temporal_relation_sha256
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "runtime_audit_sha256": (
            _sha256(arguments.runtime_audit_file)
            if arguments.external_mode == "approved-cache-only"
            else None
        ),
        "source_registry_sha256": load_official_source_registry().canonical_sha256,
        "hclx_model": "HCX-007",
        "feature_state": {
            "hclx_intent": "on",
            "ontology_lexical": "on",
            "schema_dense": "on_hclx_gap_fallback_candidate",
            "hclx_semantic_frame": "on_primary_bounded_structure",
            "semantic_admission": "on_server_verified_sequential",
            "sql_python_verifier": "on",
            "official_external_live_search": (
                "off_signed_cache_only"
                if arguments.external_mode == "approved-cache-only"
                else "on"
            ),
            "document_dense": (
                "on_release_pinned_approved_cache"
                if arguments.external_mode == "approved-cache-only"
                else "on_lab_sha_bound_snapshot_evidence_pending_release_approval"
            ),
            "provided_db_intersection": "on",
            "hclx_grounded_answer": "on",
            "qwen": "absent",
            "ncp_deployment": "not_performed",
        },
        "document_discovery_stats": {
            "cache_queries": document_discovery.cache_queries,
            "cache_hits": document_discovery.cache_hits,
            "cache_evidence_hits": document_discovery.cache_evidence_hits,
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
