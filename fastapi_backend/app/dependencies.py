from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Protocol, cast

from fastapi import Request
from finance_agent_core.agent import IntentRouter, RoutedAgentResult, RoutedFinanceAgent
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.grounded_planning import GroundedPlanGate
from finance_agent_core.agent.knowledge_router import DeterministicKnowledgeRouter
from finance_agent_core.agent.knowledge_service import KnowledgeAgent
from finance_agent_core.agent.providers import (
    HyperClovaXHTTPTransport,
    HyperClovaXQueryPlanProvider,
    HyperClovaXSettings,
    HyperClovaXTransport,
    LocalTestSettings,
)
from finance_agent_core.answering import (
    HyperClovaXGroundedAnswerProvider,
    LocalGroundedAnswerProvider,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.execution import PlanAuthorityGate
from finance_agent_core.observability import (
    AppendOnlyJsonlAuditSink,
    BoundedAsyncAuditSink,
    RequestAuditRecorder,
)
from finance_agent_core.release import (
    PublicDocumentRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    RelationRetrievalArtifactRelease,
    ResolvedAgentRelease,
    RuntimeReleaseInputs,
    load_relation_retrieval_artifact_release,
    resolve_agent_release,
)
from finance_agent_core.storage import ProductIdentitySnapshotCache, RecordSnapshotCache

from app.config import Settings
from app.http_audit import request_agent_audit
from app.request_execution import IdempotentRequestCoordinator

_MAX_HCX_API_KEY_FILE_BYTES = 4096
_RELATION_ARTIFACT_SHA256_FILE_BYTES = 65


class AgentService(Protocol):
    """Small interface used by the HTTP layer and replaceable in tests."""

    router: IntentRouter

    def answer(self, question: str, request_id: str) -> RoutedAgentResult: ...


def get_request_coordinator(request: Request) -> IdempotentRequestCoordinator:
    """Return the application-owned evaluator retry coordinator."""

    coordinator = getattr(request.app.state, "request_coordinator", None)
    if type(coordinator) is not IdempotentRequestCoordinator:
        raise RuntimeError("request coordinator is unavailable")
    return coordinator


def _provider_assembly_matches(service: RoutedFinanceAgent, settings: Settings) -> bool:
    if service.grounded_plan_provider is not None:
        return False
    transports: list[object] = []
    if settings.hcx_query_plan_enabled:
        provider = service.query_plan_provider
        if type(provider) is not HyperClovaXQueryPlanProvider:
            return False
        if (
            provider.model_name != settings.hcx_model
            or provider._client.settings.timeout_seconds != settings.hcx_timeout_seconds
        ):
            return False
        transports.append(provider._client.transport)
    elif service.query_plan_provider is not None:
        return False

    if settings.answer_provider == "hyperclova":
        answer_provider = service.answer_provider
        if type(answer_provider) is not HyperClovaXGroundedAnswerProvider:
            return False
        if (
            answer_provider.model_name != settings.hcx_model
            or answer_provider._client.settings.timeout_seconds != settings.hcx_timeout_seconds
        ):
            return False
        transports.append(answer_provider._client.transport)
    elif service.answer_provider is not None:
        return False
    if any(type(transport) is not HyperClovaXHTTPTransport for transport in transports):
        return False
    return len(transports) < 2 or transports[0] is transports[1]


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
        or type(service.grounded_plan_gate) is not GroundedPlanGate
        or service._record_cache_enabled
        or type(service.record_cache) is not RecordSnapshotCache
        or type(service.identity_cache) is not ProductIdentitySnapshotCache
        or service.compiler.record_cache is not service.record_cache
        or service.compiler.identity_cache is not service.identity_cache
        or service.grounded_plan_gate.identity_cache is not service.identity_cache
        or service.allow_internal_disabled_dataset
        or service.capability_execution_overrides
        != frozenset(settings.capability_execution_overrides)
        or service.hclx_planning_enabled != settings.hcx_query_plan_enabled
        # Stage 5 is intentionally not part of the approved evaluation or
        # production assembly yet.  An arbitrary observer receives a detached
        # trace containing the raw question, so accepting an injected observer
        # here would bypass both the release profile and the audit boundary.
        or service.schema_link_shadow_observer is not None
        or (
            settings.has_release_configuration
            and (
                type(audit_sink) is not BoundedAsyncAuditSink
                or service.audit_sink is not audit_sink
            )
        )
        or not _provider_assembly_matches(service, settings)
        or not _knowledge_assembly_matches(service, settings, release_guard)
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
    inputs = RuntimeReleaseInputs(
        environment=settings.app_env,
        source_commit=settings.source_commit,
        image_reference=settings.runtime_image_reference,
        backend_version=settings.app_version,
        backend_root=Path(__file__).resolve().parent,
        answer_provider=settings.answer_provider,
        hcx_queryplan_enabled=settings.hcx_query_plan_enabled,
        hcx_model=settings.hcx_model,
        fund_execution_policy=settings.fund_execution_policy,
        schema_dense_enabled=settings.dense_schema_linker_enabled,
        product_dense_enabled=settings.product_dense_enabled,
        relation_retrieval_artifact=relation_artifact,
        relation_retrieval_artifact_file_sha256=relation_artifact_sha256,
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
        fingerprint = lambda item: (
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
    answer_provider = None
    query_plan_provider = None
    if settings.answer_provider == "local_test":
        answer_provider = LocalGroundedAnswerProvider(LocalTestSettings.from_environment())
        answer_provider.healthcheck()
    if settings.uses_hyperclova:
        hcx_settings = HyperClovaXSettings(
            model=settings.hcx_model or "",
            timeout_seconds=settings.hcx_timeout_seconds,
        )
        transport = HyperClovaXHTTPTransport(api_key=_load_hcx_api_key(settings))
        if settings.hcx_query_plan_enabled:
            query_plan_provider = HyperClovaXQueryPlanProvider(hcx_settings, transport)
        if settings.answer_provider == "hyperclova":
            answer_provider = HyperClovaXGroundedAnswerProvider(hcx_settings, transport)
    knowledge_agent = _build_knowledge_agent(settings, release_guard)
    knowledge_router = DeterministicKnowledgeRouter() if knowledge_agent is not None else None
    return RoutedFinanceAgent(
        settings.database_paths,
        query_plan_provider=query_plan_provider,
        answer_provider=answer_provider,
        hclx_planning_enabled=settings.hcx_query_plan_enabled,
        capability_execution_overrides=settings.capability_execution_overrides,
        require_approved_databases=settings.app_env in {"evaluation", "production"},
        release_guard=release_guard,
        require_agent_release=settings.app_env in {"evaluation", "production"},
        audit_sink=audit_sink,
        knowledge_router=knowledge_router,
        knowledge_agent=knowledge_agent,
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
