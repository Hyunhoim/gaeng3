from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedPublicFundAttribute,
    NormalizedPublicFundRecord,
    QuarantinedPublicFundRow,
)
from finance_agent_core.normalization import (
    PublicFundNormalizationResult,
    normalize_public_fund_workbook,
)
from finance_agent_core.storage.sqlite import (
    _scaled_integer,
    _sha256,
    _validate_output_path,
    _write_manifest_sidecar,
    _write_metadata,
)

FUND_AUM_SCALE = Decimal("10000")
FUND_RETURN_SCALE = Decimal("1000000")

SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE fund_products (
    product_id TEXT PRIMARY KEY,
    source_row INTEGER NOT NULL UNIQUE,
    source_snapshot_date TEXT NOT NULL,
    present_source_fields INTEGER NOT NULL,
    is_quarantined INTEGER NOT NULL DEFAULT 0 CHECK (is_quarantined = 0),
    quarantine_reason TEXT CHECK (quarantine_reason IS NULL),
    row_quality TEXT NOT NULL CHECK (row_quality = 'VALID'),
    source_values_json TEXT NOT NULL,
    field_quality_json TEXT NOT NULL,
    field_quality_reasons_json TEXT NOT NULL,
    attribute_count INTEGER NOT NULL CHECK (attribute_count > 0),
    product_family TEXT NOT NULL CHECK (product_family = 'fund'),
    product_name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    public_offering INTEGER CHECK (public_offering IN (0, 1) OR public_offering IS NULL),
    public_offering_quality TEXT NOT NULL,
    sellable INTEGER NOT NULL CHECK (sellable IN (0, 1)),
    sellable_quality TEXT NOT NULL,
    company_sellable INTEGER
        CHECK (company_sellable IN (0, 1) OR company_sellable IS NULL),
    company_sellable_quality TEXT NOT NULL,
    trading_currency TEXT NOT NULL CHECK (trading_currency IN ('KRW', 'USD')),
    trading_currency_quality TEXT NOT NULL,
    investment_region TEXT,
    investment_region_quality TEXT NOT NULL,
    fund_geography_scope TEXT,
    fund_geography_scope_quality TEXT NOT NULL,
    fund_management_attribute TEXT,
    fund_management_attribute_quality TEXT NOT NULL,
    investor_type TEXT,
    investor_type_quality TEXT NOT NULL,
    currency_hedged INTEGER
        CHECK (currency_hedged IN (0, 1) OR currency_hedged IS NULL),
    currency_hedged_quality TEXT NOT NULL,
    risk_level TEXT,
    risk_level_quality TEXT NOT NULL,
    aum_ten_thousandth_units INTEGER,
    aum_quality TEXT NOT NULL,
    base_index TEXT,
    base_index_quality TEXT NOT NULL,
    one_week_return_micro_pct INTEGER,
    one_week_return_quality TEXT NOT NULL,
    one_month_return_micro_pct INTEGER,
    one_month_return_quality TEXT NOT NULL,
    three_month_return_micro_pct INTEGER,
    three_month_return_quality TEXT NOT NULL,
    six_month_return_micro_pct INTEGER,
    six_month_return_quality TEXT NOT NULL,
    eighteen_month_return_micro_pct INTEGER,
    eighteen_month_return_quality TEXT NOT NULL,
    one_year_return_micro_pct INTEGER,
    one_year_return_quality TEXT NOT NULL,
    two_year_return_micro_pct INTEGER,
    two_year_return_quality TEXT NOT NULL,
    three_year_return_micro_pct INTEGER,
    three_year_return_quality TEXT NOT NULL,
    five_year_return_micro_pct INTEGER,
    five_year_return_quality TEXT NOT NULL,
    static_as_of TEXT NOT NULL,
    static_as_of_quality TEXT NOT NULL,
    dynamic_as_of TEXT NOT NULL,
    dynamic_as_of_quality TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE fund_attributes (
    product_id TEXT NOT NULL,
    attribute_code TEXT NOT NULL,
    source_row INTEGER NOT NULL UNIQUE,
    quality TEXT NOT NULL CHECK (quality = 'UNKNOWN'),
    quality_reason TEXT NOT NULL,
    PRIMARY KEY (product_id, attribute_code),
    FOREIGN KEY (product_id) REFERENCES fund_products(product_id)
) WITHOUT ROWID;

CREATE TABLE fund_quarantine (
    source_row INTEGER PRIMARY KEY,
    source_snapshot_date TEXT NOT NULL,
    present_source_fields INTEGER NOT NULL,
    raw_item_number TEXT,
    raw_attribute_code TEXT,
    quarantine_reason TEXT NOT NULL,
    row_quality TEXT NOT NULL CHECK (row_quality = 'INVALID'),
    source_values_json TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_fund_default_scope
ON fund_products (
    public_offering,
    sellable,
    company_sellable,
    product_id
);

CREATE INDEX idx_fund_classification
ON fund_products (
    investment_region,
    fund_geography_scope,
    fund_management_attribute,
    risk_level
);

CREATE INDEX idx_fund_short_returns
ON fund_products (
    one_month_return_micro_pct DESC,
    three_month_return_micro_pct DESC,
    six_month_return_micro_pct DESC
);

CREATE INDEX idx_fund_aum
ON fund_products (trading_currency, aum_ten_thousandth_units DESC);
"""

PRODUCT_INSERT_SQL = """
INSERT INTO fund_products (
    product_id, source_row, source_snapshot_date, present_source_fields,
    is_quarantined, quarantine_reason, row_quality, source_values_json,
    field_quality_json, field_quality_reasons_json, attribute_count,
    product_family, product_name, short_name, public_offering,
    public_offering_quality, sellable, sellable_quality, company_sellable,
    company_sellable_quality, trading_currency, trading_currency_quality,
    investment_region, investment_region_quality, fund_geography_scope,
    fund_geography_scope_quality, fund_management_attribute,
    fund_management_attribute_quality, investor_type, investor_type_quality,
    currency_hedged, currency_hedged_quality, risk_level, risk_level_quality,
    aum_ten_thousandth_units, aum_quality, base_index, base_index_quality,
    one_week_return_micro_pct, one_week_return_quality,
    one_month_return_micro_pct, one_month_return_quality,
    three_month_return_micro_pct, three_month_return_quality,
    six_month_return_micro_pct, six_month_return_quality,
    eighteen_month_return_micro_pct, eighteen_month_return_quality,
    one_year_return_micro_pct, one_year_return_quality,
    two_year_return_micro_pct, two_year_return_quality,
    three_year_return_micro_pct, three_year_return_quality,
    five_year_return_micro_pct, five_year_return_quality,
    static_as_of, static_as_of_quality, dynamic_as_of, dynamic_as_of_quality
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
"""

ATTRIBUTE_INSERT_SQL = """
INSERT INTO fund_attributes (
    product_id, attribute_code, source_row, quality, quality_reason
) VALUES (?, ?, ?, ?, ?)
"""

QUARANTINE_INSERT_SQL = """
INSERT INTO fund_quarantine (
    source_row, source_snapshot_date, present_source_fields, raw_item_number,
    raw_attribute_code, quarantine_reason, row_quality, source_values_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quality(record: NormalizedPublicFundRecord, field: str) -> str:
    return record.field_quality[field].value


def _optional_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _product_values(record: NormalizedPublicFundRecord) -> tuple[object, ...]:
    return (
        record.product_id,
        record.source_row,
        record.source_snapshot_date.isoformat(),
        record.present_source_fields,
        0,
        None,
        record.row_quality.value,
        _json(record.source_values),
        _json({name: value.value for name, value in record.field_quality.items()}),
        _json(record.field_quality_reasons),
        record.attribute_count,
        record.product_family,
        record.product_name,
        record.short_name,
        _optional_bool(record.public_offering),
        _quality(record, "public_offering"),
        int(record.sellable),
        _quality(record, "sellable"),
        _optional_bool(record.company_sellable),
        _quality(record, "company_sellable"),
        record.trading_currency,
        _quality(record, "trading_currency"),
        record.investment_region,
        _quality(record, "investment_region"),
        record.fund_geography_scope,
        _quality(record, "fund_geography_scope"),
        record.fund_management_attribute,
        _quality(record, "fund_management_attribute"),
        record.investor_type,
        _quality(record, "investor_type"),
        _optional_bool(record.currency_hedged),
        _quality(record, "currency_hedged"),
        record.risk_level,
        _quality(record, "risk_level"),
        _scaled_integer(record.aum, FUND_AUM_SCALE, "aum"),
        _quality(record, "aum"),
        record.base_index,
        _quality(record, "base_index"),
        _scaled_integer(
            record.one_week_return_pct,
            FUND_RETURN_SCALE,
            "one_week_return_pct",
        ),
        _quality(record, "one_week_return_pct"),
        _scaled_integer(
            record.one_month_return_pct,
            FUND_RETURN_SCALE,
            "one_month_return_pct",
        ),
        _quality(record, "one_month_return_pct"),
        _scaled_integer(
            record.three_month_return_pct,
            FUND_RETURN_SCALE,
            "three_month_return_pct",
        ),
        _quality(record, "three_month_return_pct"),
        _scaled_integer(
            record.six_month_return_pct,
            FUND_RETURN_SCALE,
            "six_month_return_pct",
        ),
        _quality(record, "six_month_return_pct"),
        _scaled_integer(
            record.eighteen_month_return_pct,
            FUND_RETURN_SCALE,
            "eighteen_month_return_pct",
        ),
        _quality(record, "eighteen_month_return_pct"),
        _scaled_integer(
            record.one_year_return_pct,
            FUND_RETURN_SCALE,
            "one_year_return_pct",
        ),
        _quality(record, "one_year_return_pct"),
        _scaled_integer(
            record.two_year_return_pct,
            FUND_RETURN_SCALE,
            "two_year_return_pct",
        ),
        _quality(record, "two_year_return_pct"),
        _scaled_integer(
            record.three_year_return_pct,
            FUND_RETURN_SCALE,
            "three_year_return_pct",
        ),
        _quality(record, "three_year_return_pct"),
        _scaled_integer(
            record.five_year_return_pct,
            FUND_RETURN_SCALE,
            "five_year_return_pct",
        ),
        _quality(record, "five_year_return_pct"),
        record.static_as_of.isoformat(),
        _quality(record, "static_as_of"),
        record.dynamic_as_of.isoformat(),
        _quality(record, "dynamic_as_of"),
    )


def _attribute_values(record: NormalizedPublicFundAttribute) -> tuple[object, ...]:
    return (
        record.product_id,
        record.attribute_code,
        record.source_row,
        record.quality.value,
        record.quality_reason,
    )


def _quarantine_values(record: QuarantinedPublicFundRow) -> tuple[object, ...]:
    return (
        record.source_row,
        record.source_snapshot_date.isoformat(),
        record.present_source_fields,
        record.raw_item_number,
        record.raw_attribute_code,
        record.quarantine_reason,
        record.row_quality.value,
        _json(record.source_values),
    )


def write_public_fund_database(
    path: str | Path,
    result: PublicFundNormalizationResult,
    manifest: DatabaseManifest,
) -> None:
    if manifest.dataset != "fund":
        raise ValueError("public-fund writer requires a fund manifest")
    output_path = Path(path)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite database: {output_path}")

    with closing(sqlite3.connect(output_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.executescript(SCHEMA_SQL)
        connection.executemany(
            PRODUCT_INSERT_SQL,
            (_product_values(record) for record in result.products),
        )
        connection.executemany(
            ATTRIBUTE_INSERT_SQL,
            (_attribute_values(record) for record in result.attributes),
        )
        connection.executemany(
            QUARANTINE_INSERT_SQL,
            (_quarantine_values(record) for record in result.quarantine),
        )

        actual_products = connection.execute("SELECT COUNT(*) FROM fund_products").fetchone()[0]
        actual_attributes = connection.execute("SELECT COUNT(*) FROM fund_attributes").fetchone()[0]
        actual_quarantine = connection.execute("SELECT COUNT(*) FROM fund_quarantine").fetchone()[0]
        actual_searchable = connection.execute(
            """
            SELECT COUNT(*)
            FROM fund_products
            WHERE public_offering = 1
              AND public_offering_quality IN ('VALID', 'PARTIAL')
            """
        ).fetchone()[0]
        expected = (
            manifest.logical_product_rows,
            manifest.attribute_rows,
            manifest.quarantined_rows,
            manifest.searchable_rows,
        )
        actual = (
            actual_products,
            actual_attributes,
            actual_quarantine,
            actual_searchable,
        )
        if actual != expected:
            raise ValueError(f"fund manifest counts differ; expected={expected}, actual={actual}")

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError(f"fund foreign-key check failed: {foreign_key_errors[:5]}")
        _write_metadata(connection, manifest)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        connection.commit()


def build_public_fund_database(
    data_dir: str | Path,
    output_path: str | Path,
) -> DatabaseManifest:
    source_dir = Path(data_dir)
    destination = Path(output_path)
    _validate_output_path(source_dir, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    data_path, _ = resolve_inputs(source_dir, DATASET_BY_NAME["fund"])
    registry = load_field_registry()
    dataset = registry.datasets["fund"]
    result = normalize_public_fund_workbook(data_path)
    searchable = sum(record.public_offering is True for record in result.products)
    manifest = DatabaseManifest(
        schema_version="1.1",
        dataset="fund",
        registry_schema_version=registry.schema_version,
        source_file_name=data_path.name,
        source_file_sha256=_sha256(data_path),
        source_file_size_bytes=data_path.stat().st_size,
        source_snapshot_date=dataset.snapshot_date,
        total_rows=result.raw_rows,
        searchable_rows=searchable,
        quarantined_rows=len(result.quarantine),
        logical_product_rows=len(result.products),
        attribute_rows=len(result.attributes),
        scope_excluded_rows=len(result.products) - searchable,
    )
    expected_counts = (
        dataset.row_count,
        dataset.logical_row_count,
        dataset.quarantined_rows,
    )
    actual_counts = (
        manifest.total_rows,
        manifest.logical_product_rows,
        manifest.quarantined_rows,
    )
    if actual_counts != expected_counts:
        raise ValueError(
            f"fund registry counts differ; expected={expected_counts}, actual={actual_counts}"
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
        write_public_fund_database(temporary_path, result, manifest)
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _write_manifest_sidecar(destination, manifest)
    return manifest


def row_to_public_fund_record(row: sqlite3.Row) -> NormalizedPublicFundRecord:
    qualities = {
        name: QualityStatus(value) for name, value in json.loads(row["field_quality_json"]).items()
    }

    def decimal_value(column: str, scale: Decimal) -> Decimal | None:
        value = row[column]
        return None if value is None else Decimal(value) / scale

    return NormalizedPublicFundRecord(
        source_row=row["source_row"],
        source_snapshot_date=row["source_snapshot_date"],
        present_source_fields=row["present_source_fields"],
        source_values=json.loads(row["source_values_json"]),
        attribute_count=row["attribute_count"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        short_name=row["short_name"],
        public_offering=(None if row["public_offering"] is None else bool(row["public_offering"])),
        sellable=bool(row["sellable"]),
        company_sellable=(
            None if row["company_sellable"] is None else bool(row["company_sellable"])
        ),
        trading_currency=row["trading_currency"],
        investment_region=row["investment_region"],
        fund_geography_scope=row["fund_geography_scope"],
        fund_management_attribute=row["fund_management_attribute"],
        investor_type=row["investor_type"],
        currency_hedged=(None if row["currency_hedged"] is None else bool(row["currency_hedged"])),
        risk_level=row["risk_level"],
        aum=decimal_value("aum_ten_thousandth_units", FUND_AUM_SCALE),
        base_index=row["base_index"],
        one_week_return_pct=decimal_value(
            "one_week_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        one_month_return_pct=decimal_value(
            "one_month_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        three_month_return_pct=decimal_value(
            "three_month_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        six_month_return_pct=decimal_value(
            "six_month_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        eighteen_month_return_pct=decimal_value(
            "eighteen_month_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        one_year_return_pct=decimal_value(
            "one_year_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        two_year_return_pct=decimal_value(
            "two_year_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        three_year_return_pct=decimal_value(
            "three_year_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        five_year_return_pct=decimal_value(
            "five_year_return_micro_pct",
            FUND_RETURN_SCALE,
        ),
        static_as_of=row["static_as_of"],
        dynamic_as_of=row["dynamic_as_of"],
        field_quality=qualities,
        field_quality_reasons=json.loads(row["field_quality_reasons_json"]),
    )


def load_all_public_fund_records(
    connection: sqlite3.Connection,
) -> list[NormalizedPublicFundRecord]:
    rows = connection.execute("SELECT * FROM fund_products ORDER BY product_id").fetchall()
    return [row_to_public_fund_record(row) for row in rows]


def load_public_fund_attributes(
    connection: sqlite3.Connection,
) -> list[NormalizedPublicFundAttribute]:
    rows = connection.execute(
        """
        SELECT product_id, attribute_code, source_row, quality, quality_reason
        FROM fund_attributes
        ORDER BY product_id, attribute_code
        """
    ).fetchall()
    return [
        NormalizedPublicFundAttribute(
            product_id=row["product_id"],
            attribute_code=row["attribute_code"],
            source_row=row["source_row"],
            quality=QualityStatus(row["quality"]),
            quality_reason=row["quality_reason"],
        )
        for row in rows
    ]


def load_public_fund_quarantine(
    connection: sqlite3.Connection,
) -> list[QuarantinedPublicFundRow]:
    rows = connection.execute("SELECT * FROM fund_quarantine ORDER BY source_row").fetchall()
    return [
        QuarantinedPublicFundRow(
            source_row=row["source_row"],
            source_snapshot_date=row["source_snapshot_date"],
            present_source_fields=row["present_source_fields"],
            raw_item_number=row["raw_item_number"],
            raw_attribute_code=row["raw_attribute_code"],
            quarantine_reason=row["quarantine_reason"],
            row_quality=QualityStatus(row["row_quality"]),
            source_values=json.loads(row["source_values_json"]),
        )
        for row in rows
    ]
