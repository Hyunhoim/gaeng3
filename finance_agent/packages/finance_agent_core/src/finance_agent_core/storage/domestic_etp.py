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
from finance_agent_core.domain import DatabaseManifest, NormalizedDomesticEtpRecord
from finance_agent_core.normalization import iter_normalized_domestic_etp
from finance_agent_core.storage.sqlite import (
    AUM_SCALE,
    FEE_SCALE,
    _scaled_integer,
    _sha256,
    _validate_output_path,
    _write_manifest_sidecar,
    _write_metadata,
)

MONEY_SCALE = Decimal("100")
RETURN_SCALE = FEE_SCALE
FACTOR_SCALE = FEE_SCALE

SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE domestic_etp_products (
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
    product_family TEXT NOT NULL CHECK (product_family = 'domestic_etp'),
    product_type TEXT NOT NULL CHECK (product_type IN ('ETF', 'ETN')),
    product_name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    ticker TEXT NOT NULL,
    isin TEXT NOT NULL,
    sellable INTEGER CHECK (sellable IN (0, 1) OR sellable IS NULL),
    trading_suspended INTEGER
        CHECK (trading_suspended IN (0, 1) OR trading_suspended IS NULL),
    asset_type TEXT NOT NULL,
    investment_region TEXT NOT NULL,
    manager TEXT NOT NULL,
    base_index TEXT,
    base_index_quality TEXT NOT NULL,
    strategy TEXT,
    strategy_quality TEXT NOT NULL,
    leverage_factor_micro INTEGER,
    leverage_factor_quality TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    pension_eligible INTEGER CHECK (pension_eligible IN (0, 1) OR pension_eligible IS NULL),
    core_etf INTEGER CHECK (core_etf IN (0, 1) OR core_etf IS NULL),
    total_expense_ratio_micro_pct INTEGER,
    total_expense_ratio_quality TEXT NOT NULL,
    total_expense_ratio_quality_reason TEXT,
    aum_minor_units INTEGER,
    aum_quality TEXT NOT NULL,
    aum_quality_reason TEXT,
    trading_currency TEXT NOT NULL,
    trading_currency_quality TEXT NOT NULL,
    close_price_minor_units INTEGER,
    close_price_quality TEXT NOT NULL,
    one_day_return_micro_pct INTEGER,
    one_day_return_quality TEXT NOT NULL,
    one_month_return_micro_pct INTEGER,
    one_month_return_quality TEXT NOT NULL,
    three_month_return_micro_pct INTEGER,
    three_month_return_quality TEXT NOT NULL,
    six_month_return_micro_pct INTEGER,
    six_month_return_quality TEXT NOT NULL,
    one_year_return_micro_pct INTEGER,
    one_year_return_quality TEXT NOT NULL,
    ytd_return_micro_pct INTEGER,
    ytd_return_quality TEXT NOT NULL,
    daily_trading_value_minor_units INTEGER,
    daily_trading_value_quality TEXT NOT NULL,
    static_as_of TEXT NOT NULL,
    static_as_of_quality TEXT NOT NULL,
    dynamic_as_of TEXT NOT NULL,
    dynamic_as_of_quality TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_domestic_etp_slice
ON domestic_etp_products (
    product_type,
    investment_region,
    asset_type,
    sellable,
    trading_suspended,
    total_expense_ratio_micro_pct
);

CREATE INDEX idx_domestic_etp_returns
ON domestic_etp_products (
    one_month_return_micro_pct DESC,
    three_month_return_micro_pct DESC
);

CREATE INDEX idx_domestic_etp_aum
ON domestic_etp_products (aum_minor_units DESC);
"""

INSERT_SQL = """
INSERT INTO domestic_etp_products (
    product_id, source_row, source_snapshot_date, present_source_fields,
    is_quarantined, quarantine_reason, row_quality, source_values_json,
    field_quality_json, field_quality_reasons_json, product_family, product_type,
    product_name, short_name, exchange_code, ticker, isin, sellable,
    trading_suspended, asset_type, investment_region, manager, base_index,
    base_index_quality, strategy, strategy_quality, leverage_factor_micro,
    leverage_factor_quality, risk_level, pension_eligible, core_etf,
    total_expense_ratio_micro_pct, total_expense_ratio_quality,
    total_expense_ratio_quality_reason, aum_minor_units, aum_quality,
    aum_quality_reason, trading_currency, trading_currency_quality,
    close_price_minor_units, close_price_quality, one_day_return_micro_pct,
    one_day_return_quality, one_month_return_micro_pct, one_month_return_quality,
    three_month_return_micro_pct, three_month_return_quality,
    six_month_return_micro_pct, six_month_return_quality,
    one_year_return_micro_pct, one_year_return_quality, ytd_return_micro_pct,
    ytd_return_quality, daily_trading_value_minor_units,
    daily_trading_value_quality, static_as_of, static_as_of_quality,
    dynamic_as_of, dynamic_as_of_quality
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quality(record: NormalizedDomesticEtpRecord, field: str) -> str:
    return record.field_quality[field].value


def _database_values(record: NormalizedDomesticEtpRecord) -> tuple[object, ...]:
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
        record.product_type,
        record.product_name,
        record.short_name,
        record.exchange_code,
        record.ticker,
        record.isin,
        None if record.sellable is None else int(record.sellable),
        None if record.trading_suspended is None else int(record.trading_suspended),
        record.asset_type,
        record.investment_region,
        record.manager,
        record.base_index,
        _quality(record, "base_index"),
        record.strategy,
        _quality(record, "strategy"),
        _scaled_integer(record.leverage_factor, FACTOR_SCALE, "leverage_factor"),
        _quality(record, "leverage_factor"),
        record.risk_level,
        None if record.pension_eligible is None else int(record.pension_eligible),
        None if record.core_etf is None else int(record.core_etf),
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
        _quality(record, "trading_currency"),
        _scaled_integer(record.close_price, MONEY_SCALE, "close_price"),
        _quality(record, "close_price"),
        _scaled_integer(record.one_day_return_pct, RETURN_SCALE, "one_day_return_pct"),
        _quality(record, "one_day_return_pct"),
        _scaled_integer(
            record.one_month_return_pct,
            RETURN_SCALE,
            "one_month_return_pct",
        ),
        _quality(record, "one_month_return_pct"),
        _scaled_integer(
            record.three_month_return_pct,
            RETURN_SCALE,
            "three_month_return_pct",
        ),
        _quality(record, "three_month_return_pct"),
        _scaled_integer(
            record.six_month_return_pct,
            RETURN_SCALE,
            "six_month_return_pct",
        ),
        _quality(record, "six_month_return_pct"),
        _scaled_integer(
            record.one_year_return_pct,
            RETURN_SCALE,
            "one_year_return_pct",
        ),
        _quality(record, "one_year_return_pct"),
        _scaled_integer(record.ytd_return_pct, RETURN_SCALE, "ytd_return_pct"),
        _quality(record, "ytd_return_pct"),
        _scaled_integer(
            record.daily_trading_value,
            MONEY_SCALE,
            "daily_trading_value",
        ),
        _quality(record, "daily_trading_value"),
        record.static_as_of.isoformat(),
        _quality(record, "static_as_of"),
        record.dynamic_as_of.isoformat(),
        _quality(record, "dynamic_as_of"),
    )


def write_domestic_etp_database(
    path: str | Path,
    records: Iterable[NormalizedDomesticEtpRecord],
    manifest: DatabaseManifest,
) -> None:
    if manifest.dataset != "domestic_etp":
        raise ValueError("domestic ETP writer requires a domestic_etp manifest")
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


def build_domestic_etp_database(
    data_dir: str | Path,
    output_path: str | Path,
) -> DatabaseManifest:
    source_dir = Path(data_dir)
    destination = Path(output_path)
    _validate_output_path(source_dir, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_path, _ = resolve_inputs(source_dir, DATASET_BY_NAME["domestic_etp"])
    registry = load_field_registry()
    dataset = registry.datasets["domestic_etp"]
    records = list(iter_normalized_domestic_etp(data_path))
    quarantined = sum(record.is_quarantined for record in records)
    manifest = DatabaseManifest(
        dataset="domestic_etp",
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
        write_domestic_etp_database(temporary_path, records, manifest)
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _write_manifest_sidecar(destination, manifest)
    return manifest


def row_to_domestic_etp_record(
    row: sqlite3.Row,
    *,
    include_source_values: bool = True,
) -> NormalizedDomesticEtpRecord:
    field_quality = {
        name: QualityStatus(value) for name, value in json.loads(row["field_quality_json"]).items()
    }
    return NormalizedDomesticEtpRecord(
        source_row=row["source_row"],
        source_snapshot_date=row["source_snapshot_date"],
        present_source_fields=row["present_source_fields"],
        is_quarantined=bool(row["is_quarantined"]),
        quarantine_reason=row["quarantine_reason"],
        row_quality=QualityStatus(row["row_quality"]),
        source_values=(json.loads(row["source_values_json"]) if include_source_values else {}),
        product_id=row["product_id"],
        product_type=row["product_type"],
        product_name=row["product_name"],
        short_name=row["short_name"],
        exchange_code=row["exchange_code"],
        ticker=row["ticker"],
        isin=row["isin"],
        sellable=None if row["sellable"] is None else bool(row["sellable"]),
        trading_suspended=(
            None if row["trading_suspended"] is None else bool(row["trading_suspended"])
        ),
        asset_type=row["asset_type"],
        investment_region=row["investment_region"],
        manager=row["manager"],
        base_index=row["base_index"],
        strategy=row["strategy"],
        leverage_factor=(
            None
            if row["leverage_factor_micro"] is None
            else Decimal(row["leverage_factor_micro"]) / FACTOR_SCALE
        ),
        risk_level=row["risk_level"],
        pension_eligible=(
            None if row["pension_eligible"] is None else bool(row["pension_eligible"])
        ),
        core_etf=None if row["core_etf"] is None else bool(row["core_etf"]),
        total_expense_ratio_pct=(
            None
            if row["total_expense_ratio_micro_pct"] is None
            else Decimal(row["total_expense_ratio_micro_pct"]) / FEE_SCALE
        ),
        total_expense_ratio_quality=QualityStatus(row["total_expense_ratio_quality"]),
        total_expense_ratio_quality_reason=row["total_expense_ratio_quality_reason"],
        aum=(
            None if row["aum_minor_units"] is None else Decimal(row["aum_minor_units"]) / AUM_SCALE
        ),
        aum_quality=QualityStatus(row["aum_quality"]),
        aum_quality_reason=row["aum_quality_reason"],
        close_price=(
            None
            if row["close_price_minor_units"] is None
            else Decimal(row["close_price_minor_units"]) / MONEY_SCALE
        ),
        one_day_return_pct=(
            None
            if row["one_day_return_micro_pct"] is None
            else Decimal(row["one_day_return_micro_pct"]) / RETURN_SCALE
        ),
        one_month_return_pct=(
            None
            if row["one_month_return_micro_pct"] is None
            else Decimal(row["one_month_return_micro_pct"]) / RETURN_SCALE
        ),
        three_month_return_pct=(
            None
            if row["three_month_return_micro_pct"] is None
            else Decimal(row["three_month_return_micro_pct"]) / RETURN_SCALE
        ),
        six_month_return_pct=(
            None
            if row["six_month_return_micro_pct"] is None
            else Decimal(row["six_month_return_micro_pct"]) / RETURN_SCALE
        ),
        one_year_return_pct=(
            None
            if row["one_year_return_micro_pct"] is None
            else Decimal(row["one_year_return_micro_pct"]) / RETURN_SCALE
        ),
        ytd_return_pct=(
            None
            if row["ytd_return_micro_pct"] is None
            else Decimal(row["ytd_return_micro_pct"]) / RETURN_SCALE
        ),
        daily_trading_value=(
            None
            if row["daily_trading_value_minor_units"] is None
            else Decimal(row["daily_trading_value_minor_units"]) / MONEY_SCALE
        ),
        static_as_of=row["static_as_of"],
        dynamic_as_of=row["dynamic_as_of"],
        field_quality=field_quality,
        field_quality_reasons=json.loads(row["field_quality_reasons_json"]),
    )


def load_all_domestic_etp_records(
    connection: sqlite3.Connection,
    *,
    include_source_values: bool = True,
) -> list[NormalizedDomesticEtpRecord]:
    rows = connection.execute("SELECT * FROM domestic_etp_products ORDER BY product_id").fetchall()
    return [
        row_to_domestic_etp_record(row, include_source_values=include_source_values) for row in rows
    ]
