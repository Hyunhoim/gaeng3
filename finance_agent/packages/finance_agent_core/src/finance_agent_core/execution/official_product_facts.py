from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from finance_agent_core.contracts.external_knowledge import KnowledgeProductSelection
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.execution.external_knowledge_verifier import OfficialProductFact
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database_paths,
)
from finance_agent_core.storage.sqlite import connect_read_only

_IDENTITY_QUERY = {
    ProductFamily.BOND: (
        "bond_products",
        "product_name",
        "static_as_of",
    ),
    ProductFamily.DOMESTIC_ETP: (
        "domestic_etp_products",
        "product_name",
        "static_as_of",
    ),
    ProductFamily.OVERSEAS_ETP: (
        "overseas_etp_products",
        "product_name",
        "static_as_of",
    ),
    ProductFamily.FUND: (
        "fund_products",
        "product_name",
        "static_as_of",
    ),
}
_RANK_QUERY = {
    "aum": {
        ProductFamily.DOMESTIC_ETP: (
            "aum_minor_units",
            100.0,
            "source_currency_amount",
            True,
        ),
        ProductFamily.OVERSEAS_ETP: (
            "aum_minor_units",
            100.0,
            "source_currency_amount",
            True,
        ),
        ProductFamily.FUND: (
            "aum_ten_thousandth_units",
            10_000.0,
            "source_currency_amount",
            True,
        ),
    },
    "one_year_return_pct": {
        ProductFamily.DOMESTIC_ETP: (
            "one_year_return_pct",
            1.0,
            "pct_point",
            False,
        ),
        ProductFamily.FUND: (
            "one_year_return_pct",
            1.0,
            "pct_point",
            False,
        ),
    },
}


class SqliteOfficialProductFactProvider:
    """Read product identity and ranking values only from approved read-only DBs."""

    def __init__(
        self,
        database_paths: Mapping[ProductFamily | str, str | Path],
        *,
        verify_approved_databases: bool = True,
    ) -> None:
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        if verify_approved_databases:
            require_approved_database_paths(self.database_paths)
        approval = load_approved_dataset_manifest()
        self.database_sha256s = {
            family: approval.datasets[family.value].database_sha256
            for family in self.database_paths
        }
        self.source_ids = {
            family: approval.datasets[family.value].source_id
            for family in self.database_paths
        }

    def fetch(
        self,
        product_keys: tuple[tuple[ProductFamily, str], ...],
        selection: KnowledgeProductSelection | None,
    ) -> tuple[OfficialProductFact, ...]:
        rows: list[OfficialProductFact] = []
        for family, product_id in sorted(
            set(product_keys), key=lambda item: (item[0].value, item[1])
        ):
            table, name_column, as_of_column = _IDENTITY_QUERY[family]
            if family not in self.database_paths:
                raise ValueError("product fact family is not configured")
            if selection is None:
                value_sql = f"{name_column} AS value"
                field = "product_name"
                source_column = "product_name"
                scale = 1.0
                unit = "text"
                currency_sql = "NULL AS trading_currency"
                evidence_as_of_column = as_of_column
            else:
                family_query = _RANK_QUERY[selection.rank_field]
                if family not in family_query:
                    continue
                value_column, scale, unit, currency_required = family_query[family]
                value_sql = f"{value_column} AS value"
                field = selection.rank_field
                source_column = value_column
                evidence_as_of_column = "dynamic_as_of"
                currency_sql = (
                    "trading_currency AS trading_currency"
                    if currency_required
                    else "NULL AS trading_currency"
                )
            with connect_read_only(self.database_paths[family]) as connection:
                row = connection.execute(
                    f"""
                    SELECT product_id, product_name, source_row, {value_sql},
                           {evidence_as_of_column} AS as_of, {currency_sql}
                    FROM {table}
                    WHERE product_id = ? AND is_quarantined = 0
                    """,
                    (product_id,),
                ).fetchone()
            if row is None or row["value"] is None:
                continue
            value: str | float
            if selection is None:
                value = str(row["value"])
            else:
                value = float(row["value"]) / scale
            rows.append(
                OfficialProductFact(
                    evidence_id=f"provided:{family.value}:{product_id}:{field}",
                    product_family=family,
                    product_id=str(row["product_id"]),
                    product_name=str(row["product_name"]),
                    canonical_field=field,
                    normalized_value=value,
                    as_of=date.fromisoformat(str(row["as_of"])),
                    source_dataset=family.value,
                    source_id=self.source_ids[family],
                    source_row=int(row["source_row"]),
                    source_columns=(source_column,),
                    unit=unit,
                    trading_currency=(
                        None
                        if row["trading_currency"] is None
                        else str(row["trading_currency"])
                    ),
                    source_database_sha256=self.database_sha256s[family],
                )
            )
        if selection is None:
            return tuple(rows)
        ordered = sorted(
            rows,
            key=lambda item: float(item.normalized_value),
            reverse=selection.direction is SortDirection.DESC,
        )
        return tuple(ordered[: selection.limit])


__all__ = ["SqliteOfficialProductFactProvider"]
