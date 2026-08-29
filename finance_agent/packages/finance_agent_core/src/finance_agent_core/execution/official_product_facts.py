from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.external_knowledge import KnowledgeProductSelection
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.execution.external_knowledge_verifier import OfficialProductFact
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database_paths,
    sha256_file,
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
_PRODUCT_FIELD_QUERY = {
    "product_name": {
        family: ("product_name", 1.0, "text", False, "static_as_of") for family in ProductFamily
    },
    "ticker": {
        ProductFamily.BOND: ("ticker", 1.0, "text", False, "static_as_of"),
        ProductFamily.DOMESTIC_ETP: (
            "ticker",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
        ProductFamily.OVERSEAS_ETP: (
            "ticker",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
    },
    "risk_level": {
        ProductFamily.DOMESTIC_ETP: (
            "risk_level",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
        ProductFamily.FUND: (
            "risk_level",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
    },
    "manager": {
        ProductFamily.DOMESTIC_ETP: (
            "manager",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
    },
    "investment_region": {
        ProductFamily.DOMESTIC_ETP: (
            "investment_region",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
        ProductFamily.OVERSEAS_ETP: (
            "investment_region",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
        ProductFamily.FUND: (
            "investment_region",
            1.0,
            "text",
            False,
            "static_as_of",
        ),
    },
    "aum": {
        ProductFamily.DOMESTIC_ETP: (
            "aum_minor_units",
            100.0,
            "source_currency_amount",
            True,
            "dynamic_as_of",
        ),
        ProductFamily.OVERSEAS_ETP: (
            "aum_minor_units",
            100.0,
            "source_currency_amount",
            True,
            "dynamic_as_of",
        ),
        ProductFamily.FUND: (
            "aum_ten_thousandth_units",
            10_000.0,
            "source_currency_amount",
            True,
            "dynamic_as_of",
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
        if verify_approved_databases:
            approval = load_approved_dataset_manifest()
            self.database_sha256s = {
                family: approval.datasets[family.value].database_sha256
                for family in self.database_paths
            }
            self.source_ids = {
                family: approval.datasets[family.value].source_id for family in self.database_paths
            }
        else:
            registry = load_field_registry()
            self.database_sha256s = {
                family: sha256_file(path) for family, path in self.database_paths.items()
            }
            self.source_ids = {
                family: registry.datasets[family.value].source_id for family in self.database_paths
            }

    def fetch(
        self,
        product_keys: tuple[tuple[ProductFamily, str], ...],
        selection: KnowledgeProductSelection | None,
        requested_fields: tuple[str, ...] = (),
    ) -> tuple[OfficialProductFact, ...]:
        keys = tuple(sorted(set(product_keys), key=lambda item: (item[0].value, item[1])))
        rank_facts: list[OfficialProductFact] = []
        selected_keys = keys
        if selection is not None:
            for family, product_id in keys:
                family_query = _RANK_QUERY[selection.rank_field]
                if family not in family_query:
                    continue
                value_column, scale, unit, currency_required = family_query[family]
                fact = self._fetch_field(
                    family,
                    product_id,
                    field=selection.rank_field,
                    source_column=value_column,
                    scale=scale,
                    unit=unit,
                    currency_required=currency_required,
                    as_of_column="dynamic_as_of",
                    exclude_numeric_zero=True,
                )
                if fact is not None:
                    rank_facts.append(fact)
            rank_facts.sort(
                key=lambda item: float(item.normalized_value),
                reverse=selection.direction is SortDirection.DESC,
            )
            rank_facts = rank_facts[: selection.limit]
            selected_keys = tuple((item.product_family, item.product_id) for item in rank_facts)

        requested = set(requested_fields)
        if selection is None and not requested:
            requested.add("product_name")
        if selection is not None:
            requested.discard(selection.rank_field)
        extra_facts: list[OfficialProductFact] = []
        for family, product_id in selected_keys:
            for field in sorted(requested):
                family_query = _PRODUCT_FIELD_QUERY.get(field, {})
                if family not in family_query:
                    continue
                source_column, scale, unit, currency_required, as_of_column = family_query[family]
                fact = self._fetch_field(
                    family,
                    product_id,
                    field=field,
                    source_column=source_column,
                    scale=scale,
                    unit=unit,
                    currency_required=currency_required,
                    as_of_column=as_of_column,
                    exclude_numeric_zero=field == "aum",
                )
                if fact is not None:
                    extra_facts.append(fact)
        return tuple([*rank_facts, *extra_facts])

    def _fetch_field(
        self,
        family: ProductFamily,
        product_id: str,
        *,
        field: str,
        source_column: str,
        scale: float,
        unit: str,
        currency_required: bool,
        as_of_column: str,
        exclude_numeric_zero: bool,
    ) -> OfficialProductFact | None:
        if family not in self.database_paths:
            raise ValueError("product fact family is not configured")
        table, _, _ = _IDENTITY_QUERY[family]
        currency_sql = (
            "trading_currency AS trading_currency"
            if currency_required
            else "NULL AS trading_currency"
        )
        with connect_read_only(self.database_paths[family]) as connection:
            row = connection.execute(
                f"""
                SELECT product_id, product_name, source_row,
                       {source_column} AS value, {as_of_column} AS as_of,
                       {currency_sql}
                FROM {table}
                WHERE product_id = ? AND is_quarantined = 0
                """,
                (product_id,),
            ).fetchone()
        if row is None or row["value"] is None:
            return None
        if unit == "text":
            value: str | float = str(row["value"])
            if not value.strip() or value.strip() == "0":
                return None
        else:
            value = float(row["value"]) / scale
            if exclude_numeric_zero and value == 0:
                return None
        return OfficialProductFact(
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
                None if row["trading_currency"] is None else str(row["trading_currency"])
            ),
            source_database_sha256=self.database_sha256s[family],
        )


__all__ = ["SqliteOfficialProductFactProvider"]
