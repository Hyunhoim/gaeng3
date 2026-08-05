from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import DatabaseManifest, NormalizedBondRecord
from finance_agent_core.normalization import iter_normalized_bonds
from finance_agent_core.storage.sqlite import (
    _scaled_integer,
    _sha256,
    _validate_output_path,
    _write_manifest_sidecar,
    _write_metadata,
)

RATE_SCALE = Decimal("1000000")
MONEY_SCALE = Decimal("100")
QUANTITY_SCALE = Decimal("1")
DURATION_SCALE = Decimal("1000000")

SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE bond_products (
    product_id TEXT PRIMARY KEY,
    source_row INTEGER NOT NULL UNIQUE,
    source_snapshot_date TEXT NOT NULL,
    present_source_fields INTEGER NOT NULL,
    is_quarantined INTEGER NOT NULL CHECK (is_quarantined IN (0, 1)),
    quarantine_reason TEXT,
    row_quality TEXT NOT NULL,
    source_values_json TEXT NOT NULL,
    field_quality_json TEXT NOT NULL,
    field_quality_reasons_json TEXT NOT NULL,
    product_family TEXT NOT NULL CHECK (product_family = 'bond'),
    product_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    short_name TEXT,
    short_name_quality TEXT NOT NULL,
    bond_market TEXT NOT NULL CHECK (bond_market IN ('장내', '장외')),
    issuer TEXT,
    issuer_quality TEXT NOT NULL,
    bond_major_class TEXT NOT NULL,
    bond_subclass TEXT,
    bond_subclass_quality TEXT NOT NULL,
    bond_type TEXT,
    bond_type_quality TEXT NOT NULL,
    trading_currency TEXT NOT NULL,
    trading_currency_quality TEXT NOT NULL,
    issue_amount_minor_units INTEGER NOT NULL,
    issue_date TEXT,
    issue_date_quality TEXT NOT NULL,
    maturity_date TEXT,
    maturity_date_quality TEXT NOT NULL,
    coupon_rate_micro_pct INTEGER,
    coupon_rate_quality TEXT NOT NULL,
    credit_rating TEXT,
    credit_rating_quality TEXT NOT NULL,
    bond_risk_code TEXT NOT NULL,
    bond_risk_code_quality TEXT NOT NULL,
    buy_yield_micro_pct INTEGER,
    buy_yield_quality TEXT NOT NULL,
    after_tax_yield_micro_pct INTEGER,
    after_tax_yield_quality TEXT NOT NULL,
    buyable_quantity_units INTEGER,
    buyable_quantity_quality TEXT NOT NULL,
    currently_buyable INTEGER
        CHECK (currently_buyable IN (0, 1) OR currently_buyable IS NULL),
    currently_buyable_quality TEXT NOT NULL,
    remaining_days INTEGER,
    remaining_days_quality TEXT NOT NULL,
    duration_micro_years INTEGER,
    duration_quality TEXT NOT NULL,
    static_as_of TEXT NOT NULL,
    static_as_of_quality TEXT NOT NULL,
    dynamic_as_of TEXT NOT NULL,
    dynamic_as_of_quality TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_bond_current_yield
ON bond_products (
    currently_buyable,
    buy_yield_micro_pct DESC,
    product_id
);

CREATE INDEX idx_bond_class_maturity
ON bond_products (
    bond_major_class,
    bond_subclass,
    maturity_date,
    product_id
);

CREATE INDEX idx_bond_issuer
ON bond_products (issuer, product_id);
"""

INSERT_SQL = """
INSERT INTO bond_products (
    product_id, source_row, source_snapshot_date, present_source_fields,
    is_quarantined, quarantine_reason, row_quality, source_values_json,
    field_quality_json, field_quality_reasons_json, product_family, product_name,
    ticker, short_name, short_name_quality, bond_market, issuer, issuer_quality,
    bond_major_class, bond_subclass, bond_subclass_quality, bond_type,
    bond_type_quality, trading_currency, trading_currency_quality,
    issue_amount_minor_units, issue_date, issue_date_quality, maturity_date,
    maturity_date_quality, coupon_rate_micro_pct, coupon_rate_quality,
    credit_rating, credit_rating_quality, bond_risk_code, bond_risk_code_quality,
    buy_yield_micro_pct, buy_yield_quality, after_tax_yield_micro_pct,
    after_tax_yield_quality, buyable_quantity_units, buyable_quantity_quality,
    currently_buyable, currently_buyable_quality, remaining_days,
    remaining_days_quality, duration_micro_years, duration_quality, static_as_of,
    static_as_of_quality, dynamic_as_of, dynamic_as_of_quality
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?
)
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quality(record: NormalizedBondRecord, field: str) -> str:
    return record.field_quality[field].value


def _database_values(record: NormalizedBondRecord) -> tuple[object, ...]:
    return (
        record.product_id,
        record.source_row,
        record.source_snapshot_date.isoformat(),
        record.present_source_fields,
        int(record.is_quarantined),
        record.quarantine_reason,
        record.row_quality.value,
        _json(record.source_values),
        _json({name: value.value for name, value in record.field_quality.items()}),
        _json(record.field_quality_reasons),
        record.product_family,
        record.product_name,
        record.ticker,
        record.short_name,
        _quality(record, "short_name"),
        record.bond_market,
        record.issuer,
        _quality(record, "issuer"),
        record.bond_major_class,
        record.bond_subclass,
        _quality(record, "bond_subclass"),
        record.bond_type,
        _quality(record, "bond_type"),
        record.trading_currency,
        _quality(record, "trading_currency"),
        _scaled_integer(record.issue_amount, MONEY_SCALE, "issue_amount"),
        None if record.issue_date is None else record.issue_date.isoformat(),
        _quality(record, "issue_date"),
        None if record.maturity_date is None else record.maturity_date.isoformat(),
        _quality(record, "maturity_date"),
        _scaled_integer(record.coupon_rate_pct, RATE_SCALE, "coupon_rate_pct"),
        _quality(record, "coupon_rate_pct"),
        record.credit_rating,
        _quality(record, "credit_rating"),
        record.bond_risk_code,
        _quality(record, "bond_risk_code"),
        _scaled_integer(record.buy_yield_pct, RATE_SCALE, "buy_yield_pct"),
        _quality(record, "buy_yield_pct"),
        _scaled_integer(record.after_tax_yield_pct, RATE_SCALE, "after_tax_yield_pct"),
        _quality(record, "after_tax_yield_pct"),
        _scaled_integer(record.buyable_quantity, QUANTITY_SCALE, "buyable_quantity"),
        _quality(record, "buyable_quantity"),
        None if record.currently_buyable is None else int(record.currently_buyable),
        _quality(record, "currently_buyable"),
        record.remaining_days,
        _quality(record, "remaining_days"),
        _scaled_integer(record.duration_years, DURATION_SCALE, "duration_years"),
        _quality(record, "duration_years"),
        record.static_as_of.isoformat(),
        _quality(record, "static_as_of"),
        record.dynamic_as_of.isoformat(),
        _quality(record, "dynamic_as_of"),
    )


def write_bond_database(
    path: str | Path,
    records: Iterable[NormalizedBondRecord],
    manifest: DatabaseManifest,
) -> None:
    if manifest.dataset != "bond":
        raise ValueError("bond writer requires a bond manifest")
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


def build_bond_database(
    data_dir: str | Path,
    output_path: str | Path,
) -> DatabaseManifest:
    source_dir = Path(data_dir)
    destination = Path(output_path)
    _validate_output_path(source_dir, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_path, _ = resolve_inputs(source_dir, DATASET_BY_NAME["bond"])
    registry = load_field_registry()
    dataset = registry.datasets["bond"]
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version=registry.schema_version,
        source_file_name=data_path.name,
        source_file_sha256=_sha256(data_path),
        source_file_size_bytes=data_path.stat().st_size,
        source_snapshot_date=dataset.snapshot_date,
        total_rows=dataset.row_count,
        searchable_rows=dataset.row_count,
        quarantined_rows=0,
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
        write_bond_database(
            temporary_path,
            iter_normalized_bonds(data_path),
            manifest,
        )
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _write_manifest_sidecar(destination, manifest)
    return manifest


def row_to_bond_record(
    row: sqlite3.Row,
    *,
    include_source_values: bool = True,
) -> NormalizedBondRecord:
    qualities = {
        name: QualityStatus(value) for name, value in json.loads(row["field_quality_json"]).items()
    }
    return NormalizedBondRecord(
        source_row=row["source_row"],
        source_snapshot_date=row["source_snapshot_date"],
        present_source_fields=row["present_source_fields"],
        is_quarantined=bool(row["is_quarantined"]),
        quarantine_reason=row["quarantine_reason"],
        row_quality=QualityStatus(row["row_quality"]),
        source_values=(json.loads(row["source_values_json"]) if include_source_values else {}),
        product_id=row["product_id"],
        product_name=row["product_name"],
        ticker=row["ticker"],
        short_name=row["short_name"],
        bond_market=row["bond_market"],
        issuer=row["issuer"],
        bond_major_class=row["bond_major_class"],
        bond_subclass=row["bond_subclass"],
        bond_type=row["bond_type"],
        trading_currency=row["trading_currency"],
        issue_amount=Decimal(row["issue_amount_minor_units"]) / MONEY_SCALE,
        issue_date=row["issue_date"],
        maturity_date=row["maturity_date"],
        coupon_rate_pct=(
            None
            if row["coupon_rate_micro_pct"] is None
            else Decimal(row["coupon_rate_micro_pct"]) / RATE_SCALE
        ),
        credit_rating=row["credit_rating"],
        bond_risk_code=row["bond_risk_code"],
        buy_yield_pct=(
            None
            if row["buy_yield_micro_pct"] is None
            else Decimal(row["buy_yield_micro_pct"]) / RATE_SCALE
        ),
        after_tax_yield_pct=(
            None
            if row["after_tax_yield_micro_pct"] is None
            else Decimal(row["after_tax_yield_micro_pct"]) / RATE_SCALE
        ),
        buyable_quantity=(
            None
            if row["buyable_quantity_units"] is None
            else Decimal(row["buyable_quantity_units"]) / QUANTITY_SCALE
        ),
        currently_buyable=(
            None if row["currently_buyable"] is None else bool(row["currently_buyable"])
        ),
        remaining_days=row["remaining_days"],
        duration_years=(
            None
            if row["duration_micro_years"] is None
            else Decimal(row["duration_micro_years"]) / DURATION_SCALE
        ),
        static_as_of=row["static_as_of"],
        dynamic_as_of=row["dynamic_as_of"],
        field_quality=qualities,
        field_quality_reasons=json.loads(row["field_quality_reasons_json"]),
    )


def load_all_bond_records(
    connection: sqlite3.Connection,
    *,
    include_source_values: bool = True,
) -> list[NormalizedBondRecord]:
    rows = connection.execute("SELECT * FROM bond_products ORDER BY product_id").fetchall()
    return [row_to_bond_record(row, include_source_values=include_source_values) for row in rows]
