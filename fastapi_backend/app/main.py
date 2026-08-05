from __future__ import annotations

from fastapi import FastAPI

from app.config import Settings
from app.dependencies import AgentService, build_agent
from app.routes.answer import router as answer_router
from app.routes.health import router as health_router


def create_app(
    *,
    settings: Settings | None = None,
    agent: AgentService | None = None,
) -> FastAPI:
    """Application factory with explicit seams for configuration and Agent tests."""

    resolved_settings = settings or Settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
    )
    application.state.settings = resolved_settings
    application.state.agent = agent or build_agent(resolved_settings)
    application.include_router(health_router)
    application.include_router(answer_router)
    return application


app = create_app()
