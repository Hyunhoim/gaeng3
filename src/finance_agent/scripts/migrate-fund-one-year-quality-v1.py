#!/usr/bin/env python3
"""Create the approved public-fund one-year-return database successor.

This data-preparation tool never mutates the approved input database.  It first
verifies the exact Generation 4 database and every preserved raw one-year
return, then changes only the executable quality metadata introduced by the
public-fund one-year-return v1 policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
from decimal import Decimal
from pathlib import Path

EXPECTED_INPUT_SHA256 = "299a70baee4123360d9f61040ed834cd06d72d4ce4dbaf7aa87e3d7bc0b96518"
EXPECTED_SOURCE_SHA256 = "140d1ef0cec918d0b3f7c52c107cb123395594eb089b0cd70bb305709b0f44eb"
EXPECTED_PRODUCTS = 11_138
EXPECTED_NON_NULL = 7_017
EXPECTED_MISSING = 4_121
EXPECTED_OVER_500 = 15
EXPECTED_MIN_MICRO_PCT = -92_570_000
EXPECTED_MAX_MICRO_PCT = 975_100_000
RETURN_SCALE = Decimal("1000000")

OLD_PRESENT_REASON = "one_year_return_pct_execution_disabled_outlier_policy_unconfirmed"
OLD_MISSING_REASON = "one_year_return_pct_missing_and_execution_disabled"
NEW_PRESENT_REASON = "raw_source_value_preserved_without_outlier_capping_uses_file_snapshot"
NEW_MISSING_REASON = "one_year_return_pct_missing"

CHANGED_SCALAR_COLUMNS = frozenset({"one_year_return_quality"})
CHANGED_JSON_COLUMNS = frozenset({"field_quality_json", "field_quality_reasons_json"})


class MigrationError(RuntimeError):
    """Raised when the input or successor violates the frozen migration contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for row in connection.execute("SELECT key, value FROM metadata"):
        metadata[str(row[0])] = str(row[1])
    return metadata


def _require_metadata(connection: sqlite3.Connection) -> None:
    metadata = _metadata(connection)
    expected = {
        "dataset": '"fund"',
        "schema_version": '"1.1"',
        "registry_schema_version": '"1.3"',
        "source_file_sha256": json.dumps(EXPECTED_SOURCE_SHA256),
        "source_snapshot_date": '"2026-07-11"',
        "total_rows": "95619",
        "logical_product_rows": str(EXPECTED_PRODUCTS),
        "searchable_rows": "11115",
        "quarantined_rows": "1",
        "attribute_rows": "95618",
        "scope_excluded_rows": "23",
    }
    mismatches = {
        key: {"expected": value, "actual": metadata.get(key)}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if mismatches:
        raise MigrationError(f"input metadata mismatch: {mismatches}")


def _scaled_raw_value(raw: object) -> int | None:
    if raw is None:
        return None
    scaled = Decimal(str(raw)) * RETURN_SCALE
    if scaled != scaled.to_integral_value():
        raise MigrationError(f"raw one-year return is not exactly representable: {raw!r}")
    return int(scaled)


def _require_input_rows(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {
        "products": 0,
        "non_null": 0,
        "missing": 0,
        "over_500": 0,
    }
    minimum: int | None = None
    maximum: int | None = None
    rows = connection.execute(
        """
        SELECT product_id, source_values_json, one_year_return_micro_pct,
               one_year_return_quality, field_quality_json,
               field_quality_reasons_json
        FROM fund_products
        ORDER BY product_id
        """
    )
    for row in rows:
        counts["products"] += 1
        source_values = json.loads(str(row["source_values_json"]))
        qualities = json.loads(str(row["field_quality_json"]))
        reasons = json.loads(str(row["field_quality_reasons_json"]))
        stored = row["one_year_return_micro_pct"]
        expected_stored = _scaled_raw_value(source_values.get("fd_yr1_ern_r"))
        if stored != expected_stored:
            raise MigrationError(
                f"raw/stored one-year return mismatch for {row['product_id']}: "
                f"raw={expected_stored!r}, stored={stored!r}"
            )
        if row["one_year_return_quality"] != "UNKNOWN":
            raise MigrationError("input one-year scalar quality is not frozen UNKNOWN")
        if qualities.get("one_year_return_pct") != "UNKNOWN":
            raise MigrationError("input one-year JSON quality is not frozen UNKNOWN")
        expected_reason = OLD_MISSING_REASON if stored is None else OLD_PRESENT_REASON
        if reasons.get("one_year_return_pct") != expected_reason:
            raise MigrationError(f"input one-year quality reason mismatch for {row['product_id']}")
        if stored is None:
            counts["missing"] += 1
            continue
        counts["non_null"] += 1
        minimum = stored if minimum is None else min(minimum, stored)
        maximum = stored if maximum is None else max(maximum, stored)
        if stored > 500 * int(RETURN_SCALE):
            counts["over_500"] += 1

    expected_counts = {
        "products": EXPECTED_PRODUCTS,
        "non_null": EXPECTED_NON_NULL,
        "missing": EXPECTED_MISSING,
        "over_500": EXPECTED_OVER_500,
    }
    if counts != expected_counts:
        raise MigrationError(f"input one-year counts mismatch: {counts}")
    if minimum != EXPECTED_MIN_MICRO_PCT or maximum != EXPECTED_MAX_MICRO_PCT:
        raise MigrationError(f"input one-year range mismatch: minimum={minimum}, maximum={maximum}")
    return counts


def _require_unchanged_rows(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    columns = [str(row[1]) for row in source.execute("PRAGMA table_info(fund_products)").fetchall()]
    source_rows = source.execute("SELECT * FROM fund_products ORDER BY product_id")
    target_rows = target.execute("SELECT * FROM fund_products ORDER BY product_id")
    for source_row, target_row in zip(source_rows, target_rows, strict=True):
        for column in columns:
            if column in CHANGED_SCALAR_COLUMNS or column in CHANGED_JSON_COLUMNS:
                continue
            if source_row[column] != target_row[column]:
                raise MigrationError(
                    f"unauthorized field change for {source_row['product_id']}: {column}"
                )
        source_quality = json.loads(str(source_row["field_quality_json"]))
        target_quality = json.loads(str(target_row["field_quality_json"]))
        source_reason = json.loads(str(source_row["field_quality_reasons_json"]))
        target_reason = json.loads(str(target_row["field_quality_reasons_json"]))
        source_quality.pop("one_year_return_pct", None)
        target_quality.pop("one_year_return_pct", None)
        source_reason.pop("one_year_return_pct", None)
        target_reason.pop("one_year_return_pct", None)
        if source_quality != target_quality or source_reason != target_reason:
            raise MigrationError(
                f"unauthorized quality metadata change for {source_row['product_id']}"
            )


def _require_successor(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    _require_metadata(target)
    _require_unchanged_rows(source, target)
    counts = target.execute(
        """
        SELECT COUNT(*),
               SUM(one_year_return_quality = 'PARTIAL'),
               SUM(one_year_return_quality = 'UNKNOWN'),
               SUM(one_year_return_micro_pct > 500000000),
               MIN(one_year_return_micro_pct),
               MAX(one_year_return_micro_pct)
        FROM fund_products
        """
    ).fetchone()
    expected = (
        EXPECTED_PRODUCTS,
        EXPECTED_NON_NULL,
        EXPECTED_MISSING,
        EXPECTED_OVER_500,
        EXPECTED_MIN_MICRO_PCT,
        EXPECTED_MAX_MICRO_PCT,
    )
    if tuple(counts) != expected:
        raise MigrationError(f"successor one-year distribution mismatch: {tuple(counts)}")
    bad_quality = target.execute(
        """
        SELECT COUNT(*)
        FROM fund_products
        WHERE json_extract(field_quality_json, '$.one_year_return_pct')
                  != CASE WHEN one_year_return_micro_pct IS NULL THEN 'UNKNOWN' ELSE 'PARTIAL' END
           OR json_extract(field_quality_reasons_json, '$.one_year_return_pct')
                  != CASE WHEN one_year_return_micro_pct IS NULL THEN ? ELSE ? END
        """,
        (NEW_MISSING_REASON, NEW_PRESENT_REASON),
    ).fetchone()[0]
    if bad_quality:
        raise MigrationError(f"successor quality metadata mismatch: {bad_quality} rows")
    integrity = target.execute("PRAGMA integrity_check").fetchone()
    foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
    if integrity is None or integrity[0] != "ok" or foreign_keys:
        raise MigrationError("successor SQLite integrity verification failed")


def migrate(source_path: Path, output_path: Path) -> dict[str, object]:
    source = source_path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise MigrationError("input database must be a regular non-symlink file")
    if sha256_file(source) != EXPECTED_INPUT_SHA256:
        raise MigrationError("input database SHA-256 differs from frozen Generation 4")
    output_parent = output_path.parent.resolve(strict=True)
    output = output_parent / output_path.name
    if output.exists() or output.is_symlink():
        raise MigrationError("refusing to overwrite successor database")
    temporary = output_parent / f".{output.name}.{secrets.token_hex(8)}.tmp"

    source_connection = _connect_read_only(source)
    try:
        _require_metadata(source_connection)
        source_counts = _require_input_rows(source_connection)
        target_connection = sqlite3.connect(temporary)
        target_connection.row_factory = sqlite3.Row
        try:
            source_connection.backup(target_connection)
            target_connection.execute("BEGIN IMMEDIATE")
            cursor = target_connection.execute(
                """
                UPDATE fund_products
                SET one_year_return_quality =
                        CASE WHEN one_year_return_micro_pct IS NULL
                             THEN 'UNKNOWN' ELSE 'PARTIAL' END,
                    field_quality_json = json_set(
                        field_quality_json,
                        '$.one_year_return_pct',
                        CASE WHEN one_year_return_micro_pct IS NULL
                             THEN 'UNKNOWN' ELSE 'PARTIAL' END
                    ),
                    field_quality_reasons_json = json_set(
                        field_quality_reasons_json,
                        '$.one_year_return_pct',
                        CASE WHEN one_year_return_micro_pct IS NULL THEN ? ELSE ? END
                    )
                """,
                (NEW_MISSING_REASON, NEW_PRESENT_REASON),
            )
            if cursor.rowcount != EXPECTED_PRODUCTS:
                raise MigrationError(f"unexpected migrated row count: {cursor.rowcount}")
            target_connection.commit()
            target_connection.execute("VACUUM")
            _require_successor(source_connection, target_connection)
        finally:
            target_connection.close()
        os.chmod(temporary, 0o600)
        os.link(temporary, output, follow_symlinks=False)
        temporary.unlink()
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    finally:
        source_connection.close()

    return {
        "schema_version": "1.0",
        "migration": "public_fund_one_year_quality_v1",
        "input_database_sha256": EXPECTED_INPUT_SHA256,
        "output_database_sha256": sha256_file(output),
        "output_database_size_bytes": output.stat().st_size,
        "source_file_sha256": EXPECTED_SOURCE_SHA256,
        "source_counts": source_counts,
        "snapshot_date": "2026-07-11",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the immutable public-fund one-year-return v1 DB successor."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    report = migrate(arguments.input, arguments.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
