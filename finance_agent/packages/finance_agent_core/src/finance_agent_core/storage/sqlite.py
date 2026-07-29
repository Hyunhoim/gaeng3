from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedOverseasEtpRecord,
    NormalizedProductRecord,
)
from finance_agent_core.normalization import iter_normalized_overseas_etp

FEE_SCALE = Decimal("1000000")
AUM_SCALE = Decimal("100")

SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE overseas_etp_products (
    product_id TEXT PRIMARY KEY,
    source_row INTEGER NOT NULL UNIQUE,
    source_snapshot_date TEXT NOT NULL,
    present_source_fields INTEGER NOT NULL,
    is_quarantined INTEGER NOT NULL CHECK (is_quarantined IN (0, 1)),
    quarantine_reason TEXT,
    row_quality TEXT NOT NULL,
    source_values_json TEXT NOT NULL,
    product_family TEXT NOT NULL CHECK (product_family = 'overseas_etp'),
    product_type TEXT NOT NULL CHECK (product_type IN ('ETF', 'ETN')),
    product_name TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    ticker TEXT NOT NULL,
    isin TEXT,
    sellable INTEGER CHECK (sellable IN (0, 1) OR sellable IS NULL),
    trading_suspended INTEGER
        CHECK (trading_suspended IN (0, 1) OR trading_suspended IS NULL),
    asset_type TEXT,
    investment_region TEXT,
    total_expense_ratio_micro_pct INTEGER NOT NULL,
    total_expense_ratio_quality TEXT NOT NULL,
    total_expense_ratio_quality_reason TEXT,
    aum_minor_units INTEGER,
    aum_quality TEXT NOT NULL,
    aum_quality_reason TEXT,
    trading_currency TEXT NOT NULL,
    static_as_of TEXT NOT NULL,
    dynamic_as_of TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_overseas_etp_slice
ON overseas_etp_products (
    product_type,
    investment_region,
    asset_type,
    sellable,
    trading_suspended,
    total_expense_ratio_micro_pct
);

CREATE INDEX idx_overseas_etp_aum
ON overseas_etp_products (aum_minor_units DESC);
"""

INSERT_SQL = """
INSERT INTO overseas_etp_products (
    product_id,
    source_row,
    source_snapshot_date,
    present_source_fields,
    is_quarantined,
    quarantine_reason,
    row_quality,
    source_values_json,
    product_family,
    product_type,
    product_name,
    exchange_code,
    ticker,
    isin,
    sellable,
    trading_suspended,
    asset_type,
    investment_region,
    total_expense_ratio_micro_pct,
    total_expense_ratio_quality,
    total_expense_ratio_quality_reason,
    aum_minor_units,
    aum_quality,
    aum_quality_reason,
    trading_currency,
    static_as_of,
    dynamic_as_of
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scaled_integer(value: Decimal | None, scale: Decimal, field: str) -> int | None:
    if value is None:
        return None
    scaled = value * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} exceeds supported precision: {value}")
    return int(scaled)


def _database_values(record: NormalizedOverseasEtpRecord) -> tuple[object, ...]:
    return (
        record.product_id,
        record.source_row,
        record.source_snapshot_date.isoformat(),
        record.present_source_fields,
        int(record.is_quarantined),
        record.quarantine_reason,
        record.row_quality.value,
        json.dumps(
            record.source_values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        record.product_family,
        record.product_type,
        record.product_name,
        record.exchange_code,
        record.ticker,
        record.isin,
        None if record.sellable is None else int(record.sellable),
        None if record.trading_suspended is None else int(record.trading_suspended),
        record.asset_type,
        record.investment_region,
        _scaled_integer(
            record.total_expense_ratio_pct,
            FEE_SCALE,
            "total_expense_ratio_pct",
        ),
        record.total_expense_ratio_quality.value,
        record.total_expense_ratio_quality_reason,
        _scaled_integer(record.aum, AUM_SCALE, "aum"),
        record.aum_quality.value,
        record.aum_quality_reason,
        record.trading_currency,
        record.static_as_of.isoformat(),
        record.dynamic_as_of.isoformat(),
    )


def _write_metadata(connection: sqlite3.Connection, manifest: DatabaseManifest) -> None:
    payload = manifest.model_dump(mode="json")
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            for key, value in sorted(payload.items())
        ],
    )


def write_database(
    path: str | Path,
    records: Iterable[NormalizedOverseasEtpRecord],
    manifest: DatabaseManifest,
) -> None:
    output_path = Path(path)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite database: {output_path}")
    with closing(sqlite3.connect(output_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.executescript(SCHEMA_SQL)
        inserted = 0
        for record in records:
            connection.execute(INSERT_SQL, _database_values(record))
            inserted += 1
        if inserted != manifest.total_rows:
            raise ValueError(f"manifest expects {manifest.total_rows} rows but inserted {inserted}")
        _write_metadata(connection, manifest)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        connection.commit()


def _validate_output_path(data_dir: Path, output_path: Path) -> None:
    resolved_data = data_dir.resolve()
    resolved_output = output_path.resolve()
    if resolved_output == resolved_data or resolved_data in resolved_output.parents:
        raise ValueError("output database cannot be inside the raw data directory")
    if output_path.suffix not in {".sqlite", ".sqlite3", ".db"}:
        raise ValueError("output database must use .sqlite, .sqlite3, or .db")


def _write_manifest_sidecar(path: Path, manifest: DatabaseManifest) -> None:
    sidecar = path.with_suffix(f"{path.suffix}.manifest.json")
    payload = manifest.model_dump_json(indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=sidecar.parent,
        prefix=f".{sidecar.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(payload)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(sidecar)


def build_overseas_etp_database(
    data_dir: str | Path,
    output_path: str | Path,
) -> DatabaseManifest:
    source_dir = Path(data_dir)
    destination = Path(output_path)
    _validate_output_path(source_dir, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_path, _ = resolve_inputs(source_dir, DATASET_BY_NAME["overseas_etp"])
    registry = load_field_registry()
    dataset = registry.datasets["overseas_etp"]
    records = list(iter_normalized_overseas_etp(data_path))
    quarantined = sum(record.is_quarantined for record in records)
    manifest = DatabaseManifest(
        registry_schema_version=registry.schema_version,
        source_file_name=data_path.name,
        source_file_sha256=_sha256(data_path),
        source_file_size_bytes=data_path.stat().st_size,
        source_snapshot_date=dataset.snapshot_date,
        total_rows=len(records),
        searchable_rows=len(records) - quarantined,
        quarantined_rows=quarantined,
    )
    if manifest.total_rows != dataset.row_count:
        raise ValueError(f"registry expects {dataset.row_count} rows, got {manifest.total_rows}")
    if manifest.quarantined_rows != dataset.quarantined_rows:
        raise ValueError(
            "registry expects "
            f"{dataset.quarantined_rows} quarantined rows, got "
            f"{manifest.quarantined_rows}"
        )

    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    temporary_path.unlink()
    try:
        write_database(temporary_path, records, manifest)
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _write_manifest_sidecar(destination, manifest)
    return manifest


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    database = Path(path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"normalized database does not exist: {database}")
    uri = f"file:{quote(str(database))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_manifest(connection: sqlite3.Connection) -> DatabaseManifest:
    rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    payload: dict[str, Any] = {str(row["key"]): json.loads(str(row["value"])) for row in rows}
    return DatabaseManifest.model_validate(payload)


def row_to_record(row: sqlite3.Row) -> NormalizedProductRecord:
    family = row["product_family"]
    if family == "bond":
        from finance_agent_core.storage.bond import row_to_bond_record

        return row_to_bond_record(row)
    if family == "domestic_etp":
        from finance_agent_core.storage.domestic_etp import row_to_domestic_etp_record

        return row_to_domestic_etp_record(row)
    if family == "fund":
        from finance_agent_core.storage.public_fund import row_to_public_fund_record

        return row_to_public_fund_record(row)
    if family != "overseas_etp":
        raise ValueError(f"unsupported normalized product family: {family}")
    return NormalizedOverseasEtpRecord(
        source_row=row["source_row"],
        source_snapshot_date=row["source_snapshot_date"],
        present_source_fields=row["present_source_fields"],
        is_quarantined=bool(row["is_quarantined"]),
        quarantine_reason=row["quarantine_reason"],
        row_quality=QualityStatus(row["row_quality"]),
        source_values=json.loads(row["source_values_json"]),
        product_id=row["product_id"],
        product_type=row["product_type"],
        product_name=row["product_name"],
        exchange_code=row["exchange_code"],
        ticker=row["ticker"],
        isin=row["isin"],
        sellable=None if row["sellable"] is None else bool(row["sellable"]),
        trading_suspended=None
        if row["trading_suspended"] is None
        else bool(row["trading_suspended"]),
        asset_type=row["asset_type"],
        investment_region=row["investment_region"],
        total_expense_ratio_pct=Decimal(row["total_expense_ratio_micro_pct"]) / FEE_SCALE,
        total_expense_ratio_quality=QualityStatus(row["total_expense_ratio_quality"]),
        total_expense_ratio_quality_reason=row["total_expense_ratio_quality_reason"],
        aum=None if row["aum_minor_units"] is None else Decimal(row["aum_minor_units"]) / AUM_SCALE,
        aum_quality=QualityStatus(row["aum_quality"]),
        aum_quality_reason=row["aum_quality_reason"],
        trading_currency=row["trading_currency"],
        static_as_of=row["static_as_of"],
        dynamic_as_of=row["dynamic_as_of"],
    )


def load_all_records(
    connection: sqlite3.Connection,
) -> list[NormalizedProductRecord]:
    manifest = load_manifest(connection)
    if manifest.dataset == "bond":
        from finance_agent_core.storage.bond import load_all_bond_records

        return load_all_bond_records(connection)
    if manifest.dataset == "domestic_etp":
        from finance_agent_core.storage.domestic_etp import load_all_domestic_etp_records

        return load_all_domestic_etp_records(connection)
    if manifest.dataset == "fund":
        from finance_agent_core.storage.public_fund import load_all_public_fund_records

        return load_all_public_fund_records(connection)
    rows = connection.execute("SELECT * FROM overseas_etp_products ORDER BY product_id").fetchall()
    return [row_to_record(row) for row in rows]
