from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from finance_agent_core.storage import require_approved_database_paths

from app.audit_runtime import AuditRuntimeState
from app.config import Settings
from app.dependencies import (
    AgentService,
    build_agent,
    build_audit_sink,
    require_approval_guard,
    resolve_runtime_release,
)
from app.errors import request_validation_error_response
from app.http_audit import AnswerHttpAuditMiddleware
from app.request_execution import wait_for_request_workers
from app.routes.answer import router as answer_router
from app.routes.health import router as health_router


class _OuterAuditedFastAPI(FastAPI):
    """Keep transport audit outside FastAPI's ServerErrorMiddleware."""

    def build_middleware_stack(self):
        return AnswerHttpAuditMiddleware(super().build_middleware_stack())


def create_app(
    *,
    settings: Settings | None = None,
    agent: AgentService | None = None,
) -> FastAPI:
    """Application factory with explicit seams for configuration and Agent tests."""

    resolved_settings = settings or Settings()
    resolved_release = None
    if resolved_settings.app_env in {"evaluation", "production"}:
        if agent is not None:
            raise RuntimeError("evaluation/production forbids externally injected Agent services")
        resolved_release = resolve_runtime_release(resolved_settings)
        require_approved_database_paths(resolved_settings.database_paths)
    resolved_audit_sink = build_audit_sink(resolved_settings)
    audit_runtime = AuditRuntimeState(resolved_audit_sink)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            audit_runtime.start()
        except BaseException:
            _application.state.audit_shutdown_drained = audit_runtime.close(
                timeout_seconds=resolved_settings.audit_shutdown_timeout_seconds,
            )
            raise
        try:
            yield
        finally:
            shutdown_deadline = monotonic() + resolved_settings.audit_shutdown_timeout_seconds
            request_workers_drained = await asyncio.to_thread(
                wait_for_request_workers,
                timeout_seconds=resolved_settings.audit_shutdown_timeout_seconds,
            )
            remaining_audit_seconds = max(0.000_001, shutdown_deadline - monotonic())
            _application.state.audit_shutdown_drained = audit_runtime.close(
                timeout_seconds=remaining_audit_seconds,
                upstream_drained=request_workers_drained,
            )

    application = _OuterAuditedFastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.release_guard = resolved_release
    application.state.audit_sink = resolved_audit_sink
    application.state.audit_runtime = audit_runtime
    application.state.audit_shutdown_drained = None
    try:
        if agent is not None:
            resolved_agent = agent
        elif resolved_audit_sink is not None:
            resolved_agent = build_agent(
                resolved_settings,
                release_guard=resolved_release,
                audit_sink=resolved_audit_sink,
            )
        else:
            resolved_agent = build_agent(
                resolved_settings,
                release_guard=resolved_release,
            )
        application.state.agent = require_approval_guard(
            resolved_agent,
            resolved_settings,
            release_guard=resolved_release,
            audit_sink=resolved_audit_sink,
        )
    except BaseException:
        application.state.audit_shutdown_drained = audit_runtime.close(
            timeout_seconds=resolved_settings.audit_shutdown_timeout_seconds,
        )
        raise
    application.add_exception_handler(
        RequestValidationError,
        request_validation_error_response,
    )
    application.include_router(health_router)
    application.include_router(answer_router)
    return application


app = create_app()
