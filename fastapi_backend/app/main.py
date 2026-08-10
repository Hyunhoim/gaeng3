from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from finance_agent_core.storage import require_approved_database_paths

from app.config import Settings
from app.dependencies import AgentService, build_agent, require_approval_guard
from app.errors import request_validation_error_response
from app.routes.answer import router as answer_router
from app.routes.health import router as health_router


def create_app(
    *,
    settings: Settings | None = None,
    agent: AgentService | None = None,
) -> FastAPI:
    """Application factory with explicit seams for configuration and Agent tests."""

    resolved_settings = settings or Settings()
    if resolved_settings.app_env in {"evaluation", "production"}:
        require_approved_database_paths(resolved_settings.database_paths)
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
    )
    application.state.settings = resolved_settings
    resolved_agent = agent or build_agent(resolved_settings)
    application.state.agent = require_approval_guard(resolved_agent, resolved_settings)
    application.add_exception_handler(
        RequestValidationError,
        request_validation_error_response,
    )
    application.include_router(health_router)
    application.include_router(answer_router)
    return application


app = create_app()
