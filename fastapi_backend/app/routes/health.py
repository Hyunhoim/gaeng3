from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage import connect_read_only, load_manifest

from app.config import Settings
from app.dependencies import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Readiness summary that intentionally excludes filesystem paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "degraded"]
    service: str
    configured_product_families: list[ProductFamily]
    ready_product_families: list[ProductFamily]
    missing_product_families: list[ProductFamily]
    unavailable_product_families: list[ProductFamily]


def _database_is_ready(family: ProductFamily, path: Path) -> bool:
    """Verify that the configured file is a readable DB for the expected family."""

    try:
        with closing(connect_read_only(path)) as connection:
            manifest = load_manifest(connection)
    except (OSError, sqlite3.Error, ValueError):
        return False
    return manifest.dataset == family.value


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    database_paths = settings.database_paths
    configured_families = [family for family in ProductFamily if family in database_paths]
    missing_families = [family for family in ProductFamily if family not in database_paths]
    ready_families = [
        family
        for family in configured_families
        if _database_is_ready(family, database_paths[family])
    ]
    unavailable_families = [
        family for family in configured_families if family not in ready_families
    ]
    is_ready = len(ready_families) == len(ProductFamily)
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if is_ready else "degraded",
        service=settings.app_name,
        configured_product_families=configured_families,
        ready_product_families=ready_families,
        missing_product_families=missing_families,
        unavailable_product_families=unavailable_families,
    )
