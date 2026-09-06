from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Protocol, cast

from fastapi import Request
from finance_agent_core.agent.adaptive_semantic import AdaptiveSemanticResolver
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.external_knowledge_router import ExternalKnowledgeRouter
from finance_agent_core.agent.external_search_planning import (
    ExternalSearchPlanCompiler,
    ProvidedProductUniverseBuilder,
)
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.knowledge_service import KnowledgeAgent
from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXFinalAnswerProvider,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXSettings,
    HyperClovaXTransport,
)
from finance_agent_core.agent.providers.hyperclova_http import HyperClovaXHTTPTransport
from finance_agent_core.agent.providers.hyperclova_observability import HyperClovaXCallObserver
from finance_agent_core.agent.routed_service import RoutedAgentResult, RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.config.knowledge_coverage import (
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.schema_embedding_artifacts import (
    load_verified_schema_embedding_cpu_provider,
)
from finance_agent_core.execution import PlanAuthorityGate
from finance_agent_core.execution.external_knowledge_runtime import ExternalKnowledgeRuntime
from finance_agent_core.execution.official_product_facts import (
    SqliteOfficialProductFactProvider,
)
from finance_agent_core.observability import (
    AppendOnlyJsonlAuditSink,
    BoundedAsyncAuditSink,
    RequestAuditRecorder,
)
from finance_agent_core.release import (
    ExternalKnowledgeArtifactRelease,
    PublicDocumentRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    RelationRetrievalArtifactRelease,
    ResolvedAgentRelease,
    RuntimeReleaseInputs,
    external_knowledge_verifier_contract_sha256,
    load_external_knowledge_artifact_release,
    load_relation_retrieval_artifact_release,
    resolve_agent_release,
)
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
    build_release_pinned_cache_first_discovery,
)
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialConnectorError,
    OfficialHttpResponse,
    OfficialSourceConnector,
    UrllibOfficialHttpTransport,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    NaverApiHubWebSearchTransport,
)
from finance_agent_core.retrieval.schema_adaptive import ProductionHybridSchemaLinker
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    DenseSchemaIndexArtifact,
    EmbeddingProvider,
    SchemaDenseActivationPolicy,
    load_dense_schema_index_artifact,
    p1_precision_field_thresholds,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationStore,
    load_verified_temporal_relation_bundle,
)
from finance_agent_core.storage import ProductIdentitySnapshotCache, RecordSnapshotCache

from app.config import Settings
from app.http_audit import request_agent_audit
from app.request_execution import IdempotentRequestCoordinator

_MAX_HCX_API_KEY_FILE_BYTES = 4096
_RELATION_ARTIFACT_SHA256_FILE_BYTES = 65


class _DisabledOfficialHttpTransport:
    """Fail closed if a sealed evaluation cache ever attempts an HTTP fetch."""

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        del url, timeout_seconds, max_bytes
        raise OfficialConnectorError("network fetch is disabled for approved corpus cache-only")


class AgentService(Protocol):
    """Small interface used by the HTTP layer and replaceable in tests."""

    router: IntentRouter

    def answer(self, question: str, request_id: str) -> RoutedAgentResult: ...


def _schema_dense_policy(settings: Settings) -> SchemaDenseActivationPolicy | None:
    if not settings.adaptive_semantic_enabled:
        return None
    assert settings.schema_dense_calibration_report_sha256 is not None
    assert settings.schema_dense_min_score is not None
    assert settings.schema_dense_hclx_candidate_min_score is not None
    assert settings.schema_dense_minimum_margin is not None
    return SchemaDenseActivationPolicy(
        dense_min_score=settings.schema_dense_min_score,
        hclx_candidate_min_score=settings.schema_dense_hclx_candidate_min_score,
        minimum_margin=settings.schema_dense_minimum_margin,
        top_k=settings.schema_dense_top_k,
        calibration_report_sha256=(settings.schema_dense_calibration_report_sha256),
        field_thresholds=p1_precision_field_thresholds(),
    )


def _load_schema_dense_release(
    settings: Settings,
) -> tuple[DenseSchemaIndexArtifact, SchemaDenseActivationPolicy] | None:
    policy = _schema_dense_policy(settings)
    if policy is None:
        return None
    assert settings.schema_dense_index_file is not None
    assert settings.schema_dense_index_sha256 is not None
    artifact = load_dense_schema_index_artifact(
        settings.schema_dense_index_file,
        expected_file_sha256=settings.schema_dense_index_sha256,
    )
    if artifact.manifest.activation_policy_sha256 != policy.policy_sha256:
        raise RuntimeError("Schema Dense index differs from the runtime activation policy")
    return artifact, policy


def get_request_coordinator(request: Request) -> IdempotentRequestCoordinator:
    """Return the application-owned evaluator retry coordinator."""

    coordinator = getattr(request.app.state, "request_coordinator", None)
    if type(coordinator) is not IdempotentRequestCoordinator:
        raise RuntimeError("request coordinator is unavailable")
    return coordinator


def _provider_assembly_matches(
    service: RoutedFinanceAgent,
    settings: Settings,
    audit_sink: BoundedAsyncAuditSink | None,
) -> bool:
    if (
        service.query_plan_provider is not None
        or service.grounded_plan_provider is not None
        or service.answer_provider is not None
        or service.hclx_planning_enabled
    ):
        return False
    transports: list[object] = []

    if settings.hcx_intent_analysis_enabled:
        provider = service.intent_analysis_provider
        if type(provider) is not HyperClovaXIntentAnalysisProvider:
            return False
        if (
            provider.model_name != settings.hcx_model
            or not provider.require_execution_structure
            or provider.allow_entity_alias_repair != settings.hcx_semantic_resolver_enabled
            or provider._client.settings.timeout_seconds != settings.hcx_timeout_seconds
            or type(provider._client.on_call) is not HyperClovaXCallObserver
            or provider._client.on_call.expected_audit_sink is not audit_sink
        ):
            return False
        transports.append(provider._client.transport)
    elif service.intent_analysis_provider is not None:
        return False

    if settings.answer_provider == "hyperclova":
        answer_provider = service.official_answer_provider
        if type(answer_provider) is not HyperClovaXFinalAnswerProvider:
            return False
        if (
            answer_provider.model_name != settings.hcx_model
            or answer_provider._client.settings.timeout_seconds != settings.hcx_timeout_seconds
            or type(answer_provider._client.on_call) is not HyperClovaXCallObserver
            or answer_provider._client.on_call.expected_audit_sink is not audit_sink
        ):
            return False
        transports.append(answer_provider._client.transport)
    elif service.official_answer_provider is not None:
        return False
    if any(type(transport) is not HyperClovaXHTTPTransport for transport in transports):
        return False
    return len(transports) < 2 or all(item is transports[0] for item in transports[1:])


def _adaptive_assembly_matches(
    service: RoutedFinanceAgent,
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
) -> bool:
    resolver = service.adaptive_semantic_resolver
    if not settings.adaptive_semantic_enabled:
        return resolver is None and not service.router.adaptive_semantic_enabled
    if (
        type(resolver) is not AdaptiveSemanticResolver
        or not service.router.adaptive_semantic_enabled
        or resolver.execution_enabled is not settings.hcx_semantic_resolver_enabled
        or type(resolver.schema_linker) is not ProductionHybridSchemaLinker
        or type(release_guard) is not ResolvedAgentRelease
    ):
        return False
    policy = _schema_dense_policy(settings)
    if policy is None or resolver.schema_linker.policy != policy:
        return False
    manifest = resolver.schema_linker.index.manifest
    release_retrieval = release_guard.manifest.components.runtime_features.retrieval
    return (
        manifest.production_enabled
        and manifest.scope == "production_candidate"
        and manifest.activation_policy_sha256 == policy.policy_sha256
        and manifest.provider.model_id == policy.model_id
        and manifest.provider.model_revision == policy.model_revision
        and release_retrieval.schema_dense == "activated_kure_candidate_only"
        and release_retrieval.embedding_model_revision == policy.model_revision
    )


def _knowledge_assembly_matches(
    service: RoutedFinanceAgent,
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
) -> bool:
    if type(release_guard) is not ResolvedAgentRelease:
        return False
    knowledge_release = release_guard.manifest.components.knowledge_retrieval
    if knowledge_release.relation.status != "activated":
        return (
            service.knowledge_router is None
            and service.knowledge_agent is None
            and not settings.relation_retrieval_configured
        )
    if type(service.knowledge_router) is not DeterministicKnowledgeRouter:
        return False
    agent = service.knowledge_agent
    if type(agent) is not KnowledgeAgent or not settings.relation_retrieval_configured:
        return False
    assert settings.relation_index_file is not None
    relation_families = {
        ProductFamily.BOND,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }
    return (
        agent.release == knowledge_release
        and agent.relation_index_path == settings.relation_index_file
        and agent.relation_database_paths
        == {
            family: settings.database_paths[family]
            for family in relation_families
            if family in settings.database_paths
        }
        and agent.document_index_path is None
        and agent.claim_provider is None
    )


def _external_assembly_matches(
    service: RoutedFinanceAgent,
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
) -> bool:
    if type(release_guard) is not ResolvedAgentRelease:
        return False
    signed = release_guard.manifest.components.knowledge_retrieval.external
    runtime = service.external_knowledge_runtime
    if not settings.external_knowledge_enabled:
        return (
            signed.status == "disabled_not_activated"
            and runtime is None
            and service.external_universe_builder is None
            and service.external_search_plan_compiler is None
        )
    if runtime is None:
        return False
    discovery = runtime.acquisition.discovery_provider
    if signed.artifact is None:
        return False
    if signed.artifact.retrieval_mode in {
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    }:
        if (
            type(discovery) is not CacheFirstOfficialDiscoveryProvider
            or not discovery.production_approved
            or discovery.activation_binding_sha256
            != signed.artifact.document_activation_receipt_sha256
            or not settings.document_cache_configured
        ):
            return False
        connector_transport = runtime.acquisition.connector.transport
        if signed.artifact.retrieval_mode == "approved_corpus_cache_only":
            if type(connector_transport) is not _DisabledOfficialHttpTransport:
                return False
        elif type(connector_transport) is not UrllibOfficialHttpTransport:
            return False
    elif type(discovery) is not BoundedOfficialDiscoveryProvider:
        return False
    return (
        signed.status == "activated"
        and type(runtime) is ExternalKnowledgeRuntime
        and runtime.release_artifact == signed.artifact
        and runtime.release_artifact_file_sha256 == signed.artifact_file_sha256
        and type(service.external_universe_builder) is ProvidedProductUniverseBuilder
        and type(service.external_search_plan_compiler) is ExternalSearchPlanCompiler
    )


def require_approval_guard(
    service: AgentService,
    settings: Settings,
    release_guard: ResolvedAgentRelease | None = None,
    audit_sink: BoundedAsyncAuditSink | None = None,
) -> AgentService:
    """Accept only the internally assembled authority-aware production service."""

    if settings.app_env not in {"evaluation", "production"}:
        return service
    if type(service) is not RoutedFinanceAgent:
        raise RuntimeError(
            "evaluation/production requires the approved RoutedFinanceAgent assembly"
        )
    gate = service.plan_authority_gate
    if (
        not service.require_approved_databases
        or service.database_paths != settings.database_paths
        or type(gate) is not PlanAuthorityGate
        or not gate.require_approved_databases
        or not gate.require_request_deadline
        or gate.database_paths != settings.database_paths
        or gate.allow_internal_disabled_dataset
        or gate.allow_internal_evaluation_issuance
        or gate.capability_execution_overrides != frozenset(settings.capability_execution_overrides)
        or type(service.router) is not IntentRouter
        or type(service.compiler) is not ServerQueryPlanCompiler
        or service._record_cache_enabled
        or type(service.record_cache) is not RecordSnapshotCache
        or type(service.identity_cache) is not ProductIdentitySnapshotCache
        or service.compiler.record_cache is not service.record_cache
        or service.compiler.identity_cache is not service.identity_cache
        or service.allow_internal_disabled_dataset
        or service.capability_execution_overrides
        != frozenset(settings.capability_execution_overrides)
        or service.schema_link_shadow_observer is not None
        or service.require_official_hclx
        != (settings.hcx_intent_analysis_enabled and settings.answer_provider == "hyperclova")
        or (
            settings.has_release_configuration
            and (
                type(audit_sink) is not BoundedAsyncAuditSink
                or service.audit_sink is not audit_sink
            )
        )
        or not _provider_assembly_matches(service, settings, audit_sink)
        or not _adaptive_assembly_matches(service, settings, release_guard)
        or not _knowledge_assembly_matches(service, settings, release_guard)
        or not _external_assembly_matches(service, settings, release_guard)
        or type(release_guard) is not ResolvedAgentRelease
        or service.release_guard is not release_guard
        or not service.require_agent_release
        or gate.release_guard is not release_guard
        or not gate.require_agent_release
    ):
        raise RuntimeError(
            "evaluation/production requires the approved RoutedFinanceAgent assembly"
        )
    return service


def resolve_runtime_release(settings: Settings) -> ResolvedAgentRelease | None:
    """Resolve the immutable evaluation/production release before Agent assembly."""

    if settings.app_env not in {"evaluation", "production"}:
        return None
    if settings.web_concurrency != 1:
        raise RuntimeError(
            "evaluation/production requires one web worker until audit aggregation exists"
        )
    if not settings.has_release_configuration:
        raise RuntimeError("evaluation/production requires a complete Agent release configuration")
    assert settings.release_manifest_file is not None
    assert settings.deployment_binding_file is not None
    assert settings.deployment_binding_sha256 is not None
    assert settings.source_commit is not None
    assert settings.runtime_image_reference is not None
    relation_binding = _load_relation_retrieval_artifact(settings)
    relation_artifact = relation_binding[0] if relation_binding is not None else None
    relation_artifact_sha256 = relation_binding[1] if relation_binding is not None else None
    external_binding = _load_external_knowledge_artifact(settings)
    external_artifact = external_binding[0] if external_binding is not None else None
    external_artifact_sha256 = external_binding[1] if external_binding is not None else None
    schema_dense_binding = _load_schema_dense_release(settings)
    schema_dense_artifact = schema_dense_binding[0] if schema_dense_binding is not None else None
    schema_dense_policy = schema_dense_binding[1] if schema_dense_binding is not None else None
    inputs = RuntimeReleaseInputs(
        environment=settings.app_env,
        source_commit=settings.source_commit,
        image_reference=settings.runtime_image_reference,
        backend_version=settings.app_version,
        backend_root=Path(__file__).resolve().parent,
        answer_provider=settings.answer_provider,
        hcx_queryplan_enabled=settings.hcx_query_plan_enabled,
        hcx_intent_analysis_enabled=settings.hcx_intent_analysis_enabled,
        hcx_semantic_resolver_enabled=settings.hcx_semantic_resolver_enabled,
        hcx_model=settings.hcx_model,
        fund_execution_policy=settings.fund_execution_policy,
        schema_dense_enabled=settings.dense_schema_linker_enabled,
        schema_dense_artifact=schema_dense_artifact,
        schema_dense_artifact_file_sha256=settings.schema_dense_index_sha256,
        schema_dense_policy=schema_dense_policy,
        product_dense_enabled=settings.product_dense_enabled,
        relation_retrieval_artifact=relation_artifact,
        relation_retrieval_artifact_file_sha256=relation_artifact_sha256,
        external_knowledge_artifact=external_artifact,
        external_knowledge_artifact_file_sha256=external_artifact_sha256,
        platform=settings.runtime_platform,
        hcx_timeout_seconds=settings.hcx_timeout_seconds,
        official_answer_timeout_seconds=settings.official_answer_timeout_seconds,
        official_answer_max_inflight=settings.official_answer_max_inflight,
        worker_count=settings.web_concurrency,
        audit_queue_capacity=settings.audit_queue_capacity,
        audit_shutdown_timeout_seconds=settings.audit_shutdown_timeout_seconds,
        audit_fsync_each_event=settings.audit_fsync_each_event,
    )
    return resolve_agent_release(
        manifest_path=settings.release_manifest_file,
        binding_path=settings.deployment_binding_file,
        expected_binding_sha256=settings.deployment_binding_sha256,
        runtime_inputs=inputs,
    )


def _load_relation_retrieval_artifact(
    settings: Settings,
) -> tuple[RelationRetrievalArtifactRelease, str] | None:
    if not settings.relation_retrieval_configured:
        return None
    assert settings.relation_retrieval_artifact_file is not None
    expected_file_sha256 = _relation_artifact_trust_sha256(settings)
    artifact = load_relation_retrieval_artifact_release(
        artifact_path=settings.relation_retrieval_artifact_file,
        expected_file_sha256=expected_file_sha256,
    )
    return artifact, expected_file_sha256


def _load_external_knowledge_artifact(
    settings: Settings,
) -> tuple[ExternalKnowledgeArtifactRelease, str] | None:
    if not settings.external_knowledge_enabled:
        return None
    assert settings.external_knowledge_artifact_file is not None
    assert settings.external_knowledge_artifact_sha256 is not None
    artifact = load_external_knowledge_artifact_release(
        artifact_path=settings.external_knowledge_artifact_file,
        expected_file_sha256=settings.external_knowledge_artifact_sha256,
    )
    return artifact, settings.external_knowledge_artifact_sha256


def _relation_artifact_trust_sha256(settings: Settings) -> str:
    if settings.relation_retrieval_artifact_sha256 is not None:
        return settings.relation_retrieval_artifact_sha256
    path = settings.relation_retrieval_artifact_sha256_file
    if path is None or settings.app_env in {"evaluation", "production"}:
        raise RuntimeError("relation retrieval artifact trust anchor is unavailable")
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
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size != _RELATION_ARTIFACT_SHA256_FILE_BYTES
        ):
            raise RuntimeError("relation retrieval artifact trust file is insecure")
        chunks: list[bytes] = []
        remaining = _RELATION_ARTIFACT_SHA256_FILE_BYTES
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise RuntimeError("relation retrieval artifact trust file changed while loading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError("relation retrieval artifact trust file changed while loading")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)

        def fingerprint(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise RuntimeError("relation retrieval artifact trust file changed while loading")
        value = payload.decode("ascii")
    except RuntimeError:
        raise
    except (OSError, UnicodeError):
        raise RuntimeError("relation retrieval artifact trust file is unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        len(value) != _RELATION_ARTIFACT_SHA256_FILE_BYTES
        or not value.endswith("\n")
        or any(character not in "0123456789abcdef" for character in value[:-1])
    ):
        raise RuntimeError("relation retrieval artifact trust file is invalid")
    return value[:-1]


def _knowledge_release(
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
) -> PublicKnowledgeRetrievalRelease:
    if release_guard is not None:
        return release_guard.manifest.components.knowledge_retrieval
    relation_binding = _load_relation_retrieval_artifact(settings)
    artifact = relation_binding[0] if relation_binding is not None else None
    artifact_file_sha256 = relation_binding[1] if relation_binding is not None else None
    return PublicKnowledgeRetrievalRelease(
        relation=(
            PublicRelationRetrievalRelease(status="disabled_not_activated")
            if artifact is None
            else PublicRelationRetrievalRelease(
                status="activated",
                artifact=artifact,
                artifact_file_sha256=artifact_file_sha256,
            )
        ),
        document=PublicDocumentRetrievalRelease(),
    )


def _build_knowledge_agent(
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
) -> KnowledgeAgent | None:
    release = _knowledge_release(settings, release_guard)
    if release.relation.status != "activated":
        return None
    if not settings.relation_retrieval_configured:
        raise RuntimeError("activated relation release requires configured runtime artifacts")
    assert settings.relation_index_file is not None
    required_families = {
        family
        for family in (
            ProductFamily.BOND,
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        )
    }
    relation_database_paths = {
        family: path
        for family, path in settings.database_paths.items()
        if family in required_families
    }
    if set(relation_database_paths) != required_families:
        raise RuntimeError("relation retrieval requires all three approved product databases")
    agent = KnowledgeAgent(
        release=release,
        relation_index_path=settings.relation_index_file,
        relation_database_paths=relation_database_paths,
    )
    agent.verify_ready()
    return agent


def _load_hcx_api_key(settings: Settings) -> str:
    key_file = settings.clovastudio_api_key_file
    if key_file is None:
        raise RuntimeError("HyperCLOVA X credential is unavailable")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(key_file, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 0 < before.st_size <= _MAX_HCX_API_KEY_FILE_BYTES
        ):
            raise RuntimeError("HyperCLOVA X credential file is insecure")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise RuntimeError("HyperCLOVA X credential file changed while loading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError("HyperCLOVA X credential file changed while loading")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = key_file.stat(follow_symlinks=False)

        def fingerprint(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise RuntimeError("HyperCLOVA X credential file changed while loading")
        value = payload.decode("utf-8")
    except RuntimeError:
        raise
    except (OSError, UnicodeError):
        raise RuntimeError("HyperCLOVA X credential file is unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    value = value.rstrip("\r\n")
    if not value:
        raise RuntimeError("HyperCLOVA X credential file is invalid")
    return value


def _build_adaptive_semantic_resolver(
    settings: Settings,
    *,
    embedding_provider: EmbeddingProvider | None,
) -> AdaptiveSemanticResolver | None:
    binding = _load_schema_dense_release(settings)
    if binding is None:
        return None
    artifact, policy = binding
    if embedding_provider is None:
        raise RuntimeError("adaptive semantics requires the verified KURE provider")
    provider = embedding_provider
    evidence = getattr(provider, "artifact_gate_evidence", None)
    if (
        evidence.snapshot_file_manifest_sha256 != policy.snapshot_file_manifest_sha256
        or provider.metadata.model_id != policy.model_id
        or provider.metadata.model_revision != policy.model_revision
        or provider.metadata.dimension != policy.dimension
        or provider.metadata.pooling != policy.pooling
    ):
        raise RuntimeError("loaded KURE provider differs from the activation policy")
    index = DenseSchemaIndex(artifact, provider)
    linker = ProductionHybridSchemaLinker(index, policy)
    return AdaptiveSemanticResolver(
        linker,
        execution_enabled=settings.hcx_semantic_resolver_enabled,
    )


def _build_external_knowledge_runtime(
    settings: Settings,
    release_guard: ResolvedAgentRelease | None,
    *,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[
    ExternalKnowledgeRuntime | None,
    ProvidedProductUniverseBuilder | None,
    ExternalSearchPlanCompiler | None,
]:
    if not settings.external_knowledge_enabled:
        return None, None, None
    if release_guard is None:
        raise RuntimeError("external knowledge requires a resolved Agent release")
    assert settings.external_knowledge_artifact_file is not None
    assert settings.external_knowledge_artifact_sha256 is not None
    assert settings.external_temporal_relation_file is not None
    assert settings.external_temporal_relation_sha256 is not None
    artifact = load_external_knowledge_artifact_release(
        artifact_path=settings.external_knowledge_artifact_file,
        expected_file_sha256=settings.external_knowledge_artifact_sha256,
    )
    signed = release_guard.manifest.components.knowledge_retrieval.external
    if (
        signed.status != "activated"
        or signed.artifact != artifact
        or signed.artifact_file_sha256 != settings.external_knowledge_artifact_sha256
    ):
        raise RuntimeError("external knowledge artifact differs from the signed release")
    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    verifier_contract_sha256 = external_knowledge_verifier_contract_sha256()
    if artifact.source_registry_sha256 != registry.canonical_sha256:
        raise RuntimeError("external source registry differs from its release artifact")
    if artifact.coverage_manifest_sha256 != coverage.canonical_sha256:
        raise RuntimeError("external coverage manifest differs from its release artifact")
    if artifact.verifier_contract_sha256 != verifier_contract_sha256:
        raise RuntimeError("external verifier contract differs from its release artifact")
    bundle = load_verified_temporal_relation_bundle(
        settings.external_temporal_relation_file,
        expected_file_sha256=settings.external_temporal_relation_sha256,
        registry=registry,
    )
    if (
        bundle.manifest.relation_set_sha256 != artifact.temporal_relation_sha256
        or bundle.manifest.corpus_manifest_sha256 != artifact.corpus_manifest_sha256
    ):
        raise RuntimeError("external temporal relations differ from the release artifact")
    if artifact.retrieval_mode == "verified_live_official" and bundle.relations:
        raise RuntimeError("live-only external knowledge requires an empty relation cache")
    if (
        artifact.retrieval_mode == "verified_live_official_with_relation_graph"
        and not bundle.relations
    ):
        raise RuntimeError("relation graph external knowledge requires a non-empty graph")
    relation_store = TemporalRelationStore(bundle, registry)
    cache_only = artifact.retrieval_mode == "approved_corpus_cache_only"
    if cache_only:
        if (
            settings.naver_api_hub_client_id_file is not None
            or settings.naver_api_hub_client_secret_file is not None
        ):
            raise RuntimeError("approved corpus cache-only mode forbids NAVER API HUB credentials")
        live_discovery = None
    else:
        if (
            settings.naver_api_hub_client_id_file is None
            or settings.naver_api_hub_client_secret_file is None
        ):
            raise RuntimeError("live external knowledge requires NAVER API HUB credentials")
        live_discovery = BoundedOfficialDiscoveryProvider(
            NaverApiHubWebSearchTransport(
                client_id_file=settings.naver_api_hub_client_id_file,
                client_secret_file=settings.naver_api_hub_client_secret_file,
            ),
            registry,
        )
    if artifact.retrieval_mode in {
        "approved_corpus_cache_only",
        "approved_corpus_cache_first",
    }:
        if not settings.document_cache_configured or embedding_provider is None:
            raise RuntimeError("cache-first external knowledge requires approved Document Dense")
        assert settings.external_document_dense_artifact_file is not None
        assert settings.external_document_cache_activation_file is not None
        assert settings.external_document_corpus_manifest_file is not None
        assert settings.external_document_corpus_root is not None
        assert artifact.document_activation_receipt_sha256 is not None
        assert artifact.document_dense_sha256 is not None
        assert artifact.document_bm25_sha256 is not None
        assert artifact.embedding_model_revision is not None
        discovery = build_release_pinned_cache_first_discovery(
            activation_receipt_path=settings.external_document_cache_activation_file,
            expected_activation_receipt_sha256=(artifact.document_activation_receipt_sha256),
            document_dense_artifact_path=(settings.external_document_dense_artifact_file),
            expected_document_dense_sha256=artifact.document_dense_sha256,
            expected_corpus_manifest_sha256=artifact.corpus_manifest_sha256,
            expected_document_bm25_sha256=artifact.document_bm25_sha256,
            expected_embedding_model_revision=artifact.embedding_model_revision,
            embedding=embedding_provider,
            fallback=live_discovery,
            registry=registry,
            approved_corpus_manifest_path=(settings.external_document_corpus_manifest_file),
            approved_corpus_root=settings.external_document_corpus_root,
            cache_only_coverage_manifest=coverage if cache_only else None,
        )
    else:
        if settings.document_cache_configured:
            raise RuntimeError("live-only release cannot mount Document Dense cache artifacts")
        assert live_discovery is not None
        discovery = live_discovery
    connector = OfficialSourceConnector(
        _DisabledOfficialHttpTransport() if cache_only else UrllibOfficialHttpTransport(),
        registry,
    )
    acquisition = OfficialEvidenceAcquisitionCoordinator(
        relation_store=relation_store,
        discovery_provider=discovery,
        connector=connector,
    )
    product_fact_provider = SqliteOfficialProductFactProvider(settings.database_paths)
    runtime = ExternalKnowledgeRuntime(
        acquisition=acquisition,
        product_fact_provider=product_fact_provider,
        release_artifact=artifact,
        release_artifact_file_sha256=settings.external_knowledge_artifact_sha256,
    )
    return (
        runtime,
        ProvidedProductUniverseBuilder(settings.database_paths),
        ExternalSearchPlanCompiler(registry),
    )


def build_agent(
    settings: Settings,
    *,
    hcx_transport: HyperClovaXTransport | None = None,
    release_guard: ResolvedAgentRelease | None = None,
    audit_sink: BoundedAsyncAuditSink | None = None,
) -> RoutedFinanceAgent:
    """Create the core Agent without making an eager database connection."""

    if hcx_transport is not None:
        if settings.app_env in {"evaluation", "production"}:
            raise RuntimeError(
                "evaluation/production forbids caller-injected HyperCLOVA transport; "
                "use CLOVASTUDIO_API_KEY_FILE"
            )
        raise RuntimeError("caller-injected HyperCLOVA transport is unsupported")
    if (
        settings.app_env in {"evaluation", "production"}
        and type(release_guard) is not ResolvedAgentRelease
    ):
        raise RuntimeError("evaluation/production Agent assembly requires a resolved release")
    intent_analysis_provider = None
    official_answer_provider = None
    if settings.uses_hyperclova:
        hcx_settings = HyperClovaXSettings(
            model=settings.hcx_model or "",
            timeout_seconds=settings.hcx_timeout_seconds,
        )
        transport = HyperClovaXHTTPTransport(api_key=_load_hcx_api_key(settings))
        call_observer = HyperClovaXCallObserver(audit_sink) if audit_sink is not None else None
        if settings.hcx_intent_analysis_enabled:
            intent_analysis_provider = HyperClovaXIntentAnalysisProvider(
                hcx_settings,
                transport,
                on_call=call_observer,
                require_execution_structure=True,
                allow_entity_alias_repair=settings.hcx_semantic_resolver_enabled,
            )
        if settings.answer_provider == "hyperclova":
            official_answer_provider = HyperClovaXFinalAnswerProvider(
                hcx_settings,
                transport,
                on_call=call_observer,
            )
    embedding_provider = None
    if settings.adaptive_semantic_enabled:
        assert settings.kure_snapshot_dir is not None
        assert settings.kure_snapshot_manifest_file is not None
        assert settings.kure_trusted_cache_root is not None
        embedding_provider = load_verified_schema_embedding_cpu_provider(
            alias="kure-v1",
            snapshot_dir=settings.kure_snapshot_dir,
            manifest_path=settings.kure_snapshot_manifest_file,
            trusted_cache_root=settings.kure_trusted_cache_root,
            mode="production",
            batch_size=settings.kure_batch_size,
            cpu_threads=settings.kure_cpu_threads,
        )
    adaptive_semantic_resolver = _build_adaptive_semantic_resolver(
        settings,
        embedding_provider=embedding_provider,
    )
    knowledge_agent = _build_knowledge_agent(settings, release_guard)
    knowledge_router = DeterministicKnowledgeRouter() if knowledge_agent is not None else None
    (
        external_knowledge_runtime,
        external_universe_builder,
        external_search_plan_compiler,
    ) = _build_external_knowledge_runtime(
        settings,
        release_guard,
        embedding_provider=embedding_provider,
    )
    # Keep route authority and the signed executor atomic.  With external
    # knowledge disabled, RoutedFinanceAgent's explicit unavailable branch
    # compiles a zero-authority plan and returns CANNOT_VERIFY.  Supplying an
    # executable router without its runtime would instead turn an ordinary
    # evidence question into a release-mismatch 503.
    external_knowledge_router = (
        ExternalKnowledgeRouter() if external_knowledge_runtime is not None else None
    )
    return RoutedFinanceAgent(
        settings.database_paths,
        intent_analysis_provider=intent_analysis_provider,
        official_answer_provider=official_answer_provider,
        require_official_hclx=(
            settings.hcx_intent_analysis_enabled and settings.answer_provider == "hyperclova"
        ),
        capability_execution_overrides=settings.capability_execution_overrides,
        require_approved_databases=settings.app_env in {"evaluation", "production"},
        release_guard=release_guard,
        require_agent_release=settings.app_env in {"evaluation", "production"},
        audit_sink=audit_sink,
        knowledge_router=knowledge_router,
        knowledge_agent=knowledge_agent,
        external_knowledge_router=external_knowledge_router,
        external_knowledge_runtime=external_knowledge_runtime,
        external_universe_builder=external_universe_builder,
        external_search_plan_compiler=external_search_plan_compiler,
        adaptive_semantic_resolver=adaptive_semantic_resolver,
    )


def build_audit_sink(settings: Settings) -> BoundedAsyncAuditSink | None:
    """Build the only approved non-blocking durable audit boundary."""

    if settings.audit_mode == "disabled":
        return None
    if settings.audit_mode != "jsonl" or settings.audit_file is None:
        raise RuntimeError("unsupported audit runtime configuration")
    downstream = AppendOnlyJsonlAuditSink(
        settings.audit_file,
        fsync_each_event=settings.audit_fsync_each_event,
    )
    return BoundedAsyncAuditSink(
        downstream,
        queue_capacity=settings.audit_queue_capacity,
        start_worker=False,
        stall_timeout_seconds=settings.audit_shutdown_timeout_seconds,
    )


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_agent(request: Request) -> AgentService:
    return cast(AgentService, request.app.state.agent)


def request_audit_recorder(
    service: AgentService,
    *,
    request: Request,
    request_id: str,
    question: str,
) -> RequestAuditRecorder | None:
    if type(service) is not RoutedFinanceAgent or service.audit_sink is None:
        return None
    recorder = request_agent_audit(
        request,
        request_id=request_id,
        question=question,
    )
    if recorder is None or recorder.sink is not service.audit_sink:
        return None
    return recorder
