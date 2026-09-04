from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from finance_agent_core.config import QualityStatus, load_field_registry
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


@dataclass(frozen=True)
class _FactQuery:
    source_column: str
    scale: float
    unit: str
    quality_column: str | None = None
    currency_required: bool = False
    as_of_column: str = "dynamic_as_of"


_RANK_QUERY: dict[str, dict[ProductFamily, _FactQuery]] = {
    "aum": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "aum_minor_units", 100.0, "source_currency_amount", "aum_quality", True
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "aum_minor_units", 100.0, "source_currency_amount", "aum_quality", True
        ),
        ProductFamily.FUND: _FactQuery(
            "aum_ten_thousandth_units",
            10_000.0,
            "source_currency_amount",
            "aum_quality",
            True,
        ),
    },
    "total_expense_ratio_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "total_expense_ratio_micro_pct",
            1_000_000.0,
            "pct_point",
            "total_expense_ratio_quality",
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "total_expense_ratio_micro_pct",
            1_000_000.0,
            "pct_point",
            "total_expense_ratio_quality",
        ),
    },
    "one_day_return_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "one_day_return_micro_pct", 1_000_000.0, "pct_point", "one_day_return_quality"
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "one_day_return_micro_pct", 1_000_000.0, "pct_point", "one_day_return_quality"
        ),
    },
    "one_month_return_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "one_month_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "one_month_return_quality",
        ),
        ProductFamily.FUND: _FactQuery(
            "one_month_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "one_month_return_quality",
        ),
    },
    "three_month_return_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "three_month_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "three_month_return_quality",
        ),
        ProductFamily.FUND: _FactQuery(
            "three_month_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "three_month_return_quality",
        ),
    },
    "one_year_return_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "one_year_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "one_year_return_quality",
        ),
        ProductFamily.FUND: _FactQuery(
            "one_year_return_micro_pct",
            1_000_000.0,
            "pct_point",
            "one_year_return_quality",
        ),
    },
    "daily_volume": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "daily_volume_hundredth_units",
            100.0,
            "source_quantity",
            "daily_volume_quality",
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "daily_volume_eight_decimals",
            100_000_000.0,
            "source_quantity",
            "daily_volume_quality",
        ),
    },
    "daily_trading_value": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "daily_trading_value_minor_units",
            100.0,
            "source_currency_amount",
            "daily_trading_value_quality",
            True,
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "daily_trading_value_eight_decimals",
            100_000_000.0,
            "source_currency_amount",
            "daily_trading_value_quality",
            True,
        ),
    },
    "twenty_day_volatility_pct": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "twenty_day_volatility_eight_decimal_pct",
            100_000_000.0,
            "pct_point",
            "twenty_day_volatility_quality",
        ),
    },
}
_PRODUCT_FIELD_QUERY: dict[str, dict[ProductFamily, _FactQuery]] = {
    "product_name": {
        family: _FactQuery("product_name", 1.0, "text", as_of_column="static_as_of")
        for family in ProductFamily
    },
    "ticker": {
        ProductFamily.BOND: _FactQuery("ticker", 1.0, "text", as_of_column="static_as_of"),
        ProductFamily.DOMESTIC_ETP: _FactQuery("ticker", 1.0, "text", as_of_column="static_as_of"),
        ProductFamily.OVERSEAS_ETP: _FactQuery("ticker", 1.0, "text", as_of_column="static_as_of"),
    },
    "risk_level": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "risk_level", 1.0, "text", as_of_column="static_as_of"
        ),
        ProductFamily.FUND: _FactQuery(
            "risk_level", 1.0, "text", "risk_level_quality", as_of_column="static_as_of"
        ),
    },
    "manager": {
        ProductFamily.DOMESTIC_ETP: _FactQuery("manager", 1.0, "text", as_of_column="static_as_of"),
        ProductFamily.OVERSEAS_ETP: _FactQuery("manager", 1.0, "text", as_of_column="static_as_of"),
    },
    "investment_region": {
        ProductFamily.DOMESTIC_ETP: _FactQuery(
            "investment_region", 1.0, "text", as_of_column="static_as_of"
        ),
        ProductFamily.OVERSEAS_ETP: _FactQuery(
            "investment_region", 1.0, "text", as_of_column="static_as_of"
        ),
        ProductFamily.FUND: _FactQuery(
            "investment_region",
            1.0,
            "text",
            "investment_region_quality",
            as_of_column="static_as_of",
        ),
    },
    **_RANK_QUERY,
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
                query = family_query[family]
                fact = self._fetch_field(
                    family,
                    product_id,
                    field=selection.rank_field,
                    query=query,
                    exclude_numeric_zero=True,
                )
                if fact is not None:
                    rank_facts.append(fact)
            if keys and not rank_facts:
                # A verified external relation still exists, but the provided
                # DB cannot support the requested ordering.  This is an
                # unavailable calculation, not an empty product intersection.
                raise ValueError("requested ranking field has no usable provided-DB values")
            if rank_facts and any(
                _RANK_QUERY[selection.rank_field][item.product_family].currency_required
                for item in rank_facts
            ):
                currencies = {item.trading_currency for item in rank_facts}
                if None in currencies or len(currencies) != 1:
                    raise ValueError("currency-valued ranking requires one provided DB currency")
            # Preserve the canonical product-key order for equal values while
            # applying the requested numeric direction as the stable primary key.
            rank_facts.sort(key=lambda item: (item.product_family.value, item.product_id))
            rank_facts.sort(
                key=lambda item: float(item.normalized_value),
                reverse=selection.direction is SortDirection.DESC,
            )
            rank_facts = rank_facts[: selection.limit]
            selected_keys = tuple((item.product_family, item.product_id) for item in rank_facts)

        # Without ranking facts, product identity is the mandatory provided-DB
        # fact.  Otherwise a valid relation whose optional requested field is
        # absent can be mislabeled as NOT_FOUND downstream.  Ranking facts
        # already carry canonical product identity, so avoid a duplicate name
        # field on that path.
        requested = set(requested_fields)
        if selection is None:
            requested.add("product_name")
        if selection is not None:
            requested.discard(selection.rank_field)
        extra_facts: list[OfficialProductFact] = []
        for family, product_id in selected_keys:
            for field in sorted(requested):
                family_query = _PRODUCT_FIELD_QUERY.get(field, {})
                if family not in family_query:
                    continue
                query = family_query[family]
                fact = self._fetch_field(
                    family,
                    product_id,
                    field=field,
                    query=query,
                    exclude_numeric_zero=query.unit != "text",
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
        query: _FactQuery,
        exclude_numeric_zero: bool,
    ) -> OfficialProductFact | None:
        if family not in self.database_paths:
            raise ValueError("product fact family is not configured")
        table, _, _ = _IDENTITY_QUERY[family]
        currency_sql = (
            "trading_currency AS trading_currency"
            if query.currency_required
            else "NULL AS trading_currency"
        )
        quality_sql = (
            f"{query.quality_column} AS field_quality"
            if query.quality_column is not None
            else "'VALID' AS field_quality"
        )
        with connect_read_only(self.database_paths[family]) as connection:
            row = connection.execute(
                f"""
                SELECT product_id, product_name, source_row,
                       {query.source_column} AS value, {query.as_of_column} AS as_of,
                       {currency_sql}, {quality_sql}
                FROM {table}
                WHERE product_id = ? AND is_quarantined = 0
                """,
                (product_id,),
            ).fetchone()
        if (
            row is None
            or row["value"] is None
            or row["as_of"] is None
            or row["field_quality"] not in {"VALID", "PARTIAL"}
        ):
            return None
        if query.unit == "text":
            value: str | float = str(row["value"])
            if not value.strip() or value.strip() == "0":
                return None
        else:
            value = float(row["value"]) / query.scale
            if not math.isfinite(value) or (exclude_numeric_zero and value == 0):
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
            source_columns=(query.source_column,),
            unit=query.unit,
            quality=QualityStatus(str(row["field_quality"])),
            trading_currency=(
                None if row["trading_currency"] is None else str(row["trading_currency"])
            ),
            source_database_sha256=self.database_sha256s[family],
        )


__all__ = ["SqliteOfficialProductFactProvider"]
