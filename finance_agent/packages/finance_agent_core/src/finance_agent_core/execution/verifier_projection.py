from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_agent_core.config import QualityStatus, ValueType, load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.execution.sql_schema import (
    SQL_FIELDS_BY_FAMILY,
    TABLE_BY_FAMILY,
)
from finance_agent_core.storage import connect_read_only, load_manifest


@dataclass(frozen=True, slots=True)
class VerifierProjection:
    fields: tuple[str, ...]
    field_positions: dict[str, int]


@dataclass(frozen=True, slots=True)
class ProjectedVerifierRecord:
    projection: VerifierProjection
    product_id: str
    product_family: str
    is_quarantined: bool
    source_snapshot_date: date
    static_as_of: date
    dynamic_as_of: date
    values: tuple[object | None, ...]
    qualities: tuple[QualityStatus | None, ...]

    def canonical_value(self, field_name: str) -> object | None:
        try:
            position = self.projection.field_positions[field_name]
        except KeyError as error:
            raise KeyError(f"field {field_name!r} is outside the verifier projection") from error
        return self.values[position]

    def row_level_quality(
        self,
        field_name: str,
    ) -> tuple[QualityStatus | None, str | None]:
        try:
            position = self.projection.field_positions[field_name]
        except KeyError:
            return None, None
        return self.qualities[position], None


def verifier_projection_fields(plan: QueryPlan) -> tuple[str, ...]:
    fields: list[str] = ["product_id"]
    for field_name in [
        *(constraint.field for constraint in plan.constraints),
        *(ranking.field for ranking in plan.ranking),
        *plan.intent_payload.group_by,
        *(aggregation.field for aggregation in plan.intent_payload.aggregations),
    ]:
        if field_name not in fields:
            fields.append(field_name)
    return tuple(fields)


def _date_value(value: object) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"verifier date must be ISO text: {value!r}")
    return date.fromisoformat(value)


def _canonical_value(
    *,
    field_name: str,
    raw_value: object,
    value_type: ValueType,
    scale: Decimal | None,
) -> object | None:
    if raw_value is None:
        return None
    if scale is not None:
        if field_name == "remaining_days":
            return int(raw_value)
        return Decimal(raw_value) / scale
    if value_type is ValueType.BOOLEAN:
        return bool(raw_value)
    if value_type is ValueType.DATE:
        return _date_value(raw_value)
    if value_type is ValueType.NUMBER:
        if isinstance(raw_value, bool):
            raise TypeError(f"numeric verifier field is boolean: {field_name}")
        return raw_value if isinstance(raw_value, (int, Decimal)) else Decimal(str(raw_value))
    return str(raw_value)


def project_verifier_rows(
    rows: Sequence[sqlite3.Row],
    *,
    family: str,
    fields: tuple[str, ...],
) -> list[ProjectedVerifierRecord]:
    sql_fields = SQL_FIELDS_BY_FAMILY[family]
    projection = VerifierProjection(
        fields=fields,
        field_positions={name: index for index, name in enumerate(fields)},
    )
    registry = load_field_registry()
    value_types = tuple(
        registry.require_field(field_name, [family]).value_type for field_name in fields
    )
    scales = tuple(sql_fields[field_name].scale for field_name in fields)
    quality_columns = tuple(sql_fields[field_name].quality_column for field_name in fields)
    records: list[ProjectedVerifierRecord] = []
    for row in rows:
        values = tuple(
            _canonical_value(
                field_name=field_name,
                raw_value=row[f"v_{index}"],
                value_type=value_types[index],
                scale=scales[index],
            )
            for index, field_name in enumerate(fields)
        )
        qualities = tuple(
            None
            if quality_columns[index] is None or row[f"q_{index}"] is None
            else QualityStatus(row[f"q_{index}"])
            for index in range(len(fields))
        )
        records.append(
            ProjectedVerifierRecord(
                projection=projection,
                product_id=str(row["_product_id"]),
                product_family=str(row["_product_family"]),
                is_quarantined=bool(row["_is_quarantined"]),
                source_snapshot_date=_date_value(row["_source_snapshot_date"]),
                static_as_of=_date_value(row["_static_as_of"]),
                dynamic_as_of=_date_value(row["_dynamic_as_of"]),
                values=values,
                qualities=qualities,
            )
        )
    return records


def verifier_select_columns(
    family: str,
    fields: tuple[str, ...],
) -> list[str]:
    try:
        sql_fields = SQL_FIELDS_BY_FAMILY[family]
        specs = [sql_fields[field_name] for field_name in fields]
    except KeyError as error:
        raise ValueError(f"no verifier projection for {family}.{error.args[0]}") from error
    projected_columns: list[str] = []
    for index, spec in enumerate(specs):
        projected_columns.append(f"{spec.column} AS v_{index}")
        quality = "NULL" if spec.quality_column is None else spec.quality_column
        projected_columns.append(f"{quality} AS q_{index}")
    return [
        "product_id AS _product_id",
        "product_family AS _product_family",
        "is_quarantined AS _is_quarantined",
        "source_snapshot_date AS _source_snapshot_date",
        "static_as_of AS _static_as_of",
        "dynamic_as_of AS _dynamic_as_of",
        *projected_columns,
    ]


def load_projected_verifier_records(
    database_path: str | Path,
    plan: QueryPlan,
) -> list[ProjectedVerifierRecord]:
    family = plan.product_families[0].value
    fields = verifier_projection_fields(plan)
    try:
        table = TABLE_BY_FAMILY[family]
    except KeyError as error:
        raise ValueError(f"no verifier table for {family}") from error
    select_columns = verifier_select_columns(family, fields)
    with connect_read_only(database_path) as connection:
        manifest = load_manifest(connection)
        if manifest.dataset != family:
            raise ValueError(f"plan requests {family}, but database contains {manifest.dataset}")
        rows = connection.execute(
            f"SELECT {', '.join(select_columns)} FROM {table} ORDER BY product_id"
        ).fetchall()
    return project_verifier_rows(rows, family=family, fields=fields)
