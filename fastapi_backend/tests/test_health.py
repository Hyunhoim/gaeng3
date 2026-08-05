import json
import sqlite3
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest

from app.config import Settings
from app.main import create_app
from tests.conftest import FakeAgentService


def _create_manifest_database(path: Path, family: ProductFamily) -> None:
    manifest_values: dict[str, object] = {
        "dataset": family.value,
        "registry_schema_version": "test-v1",
        "source_file_name": f"{family.value}.xlsx",
        "source_file_sha256": "0" * 64,
        "source_file_size_bytes": 1,
        "source_snapshot_date": date(2026, 8, 1),
        "total_rows": 1,
        "searchable_rows": 1,
        "quarantined_rows": 0,
    }
    if family is ProductFamily.FUND:
        manifest_values.update(
            schema_version="1.1",
            logical_product_rows=1,
            attribute_rows=1,
            scope_excluded_rows=0,
        )
    manifest = DatabaseManifest.model_validate(manifest_values)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False))
                for key, value in manifest.model_dump(mode="json").items()
            ],
        )


def test_health_reports_configured_and_missing_families_without_paths() -> None:
    settings = Settings(
        app_name="test-finance-agent",
        overseas_etp_db=Path("/private/data/overseas-secret.sqlite3"),
        bond_db=Path("/private/data/bond-secret.sqlite3"),
    )
    application = create_app(settings=settings, agent=FakeAgentService())

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "service": "test-finance-agent",
        "configured_product_families": ["bond", "overseas_etp"],
        "ready_product_families": [],
        "missing_product_families": ["domestic_etp", "fund"],
        "unavailable_product_families": ["bond", "overseas_etp"],
    }
    assert "private" not in response.text
    assert "sqlite3" not in response.text


def test_health_is_ok_when_every_database_manifest_is_ready(tmp_path: Path) -> None:
    paths = {family: tmp_path / f"{family.value}.sqlite3" for family in ProductFamily}
    for family, path in paths.items():
        _create_manifest_database(path, family)

    settings = Settings(
        overseas_etp_db=paths[ProductFamily.OVERSEAS_ETP],
        domestic_etp_db=paths[ProductFamily.DOMESTIC_ETP],
        bond_db=paths[ProductFamily.BOND],
        fund_db=paths[ProductFamily.FUND],
    )
    application = create_app(settings=settings, agent=FakeAgentService())

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["ready_product_families"] == [
        "bond",
        "domestic_etp",
        "overseas_etp",
        "fund",
    ]
    assert response.json()["missing_product_families"] == []
    assert response.json()["unavailable_product_families"] == []
