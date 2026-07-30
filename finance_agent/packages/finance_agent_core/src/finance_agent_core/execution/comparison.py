from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from finance_agent_core.config import QualityStatus, ValueType, load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.domain import (
    FieldEvidence,
    NormalizedProductRecord,
    ProductEvidence,
    VerifiedSearch,
)
from finance_agent_core.execution.policy import (
    fund_comparison_product_ids,
    require_fund_comparison_contract,
)

type ComparisonStatus = Literal[
    "numeric_delta",
    "value_only",
    "currency_mismatch",
    "unavailable",
    "incomplete",
]

_USABLE_QUALITIES = {QualityStatus.VALID, QualityStatus.PARTIAL}


@dataclass(frozen=True)
class ComparisonCell:
    target_index: int
    product_id: str
    product_name: str | None
    value: object | None
    evidence: FieldEvidence | None
    trading_currency: str | None


@dataclass(frozen=True)
class FieldComparison:
    canonical_field: str
    label: str
    unit: str
    cells: tuple[ComparisonCell, ComparisonCell]
    status: ComparisonStatus
    delta: Decimal | None
    delta_basis: Literal["second_minus_first"] | None
    reason: str | None


@dataclass(frozen=True)
class FundComparison:
    requested_product_ids: tuple[str, str]
    found_product_ids: tuple[str, ...]
    missing_product_ids: tuple[str, ...]
    verified: VerifiedSearch
    products: tuple[ProductEvidence, ...]
    fields: tuple[FieldComparison, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_product_ids


def _field_evidence(
    product: ProductEvidence | None,
    field_name: str,
) -> FieldEvidence | None:
    if product is None:
        return None
    return next(
        (field for field in product.fields if field.canonical_field == field_name),
        None,
    )


def _usable(evidence: FieldEvidence | None) -> bool:
    return (
        evidence is not None
        and evidence.normalized_value is not None
        and evidence.quality in _USABLE_QUALITIES
    )


def _field_comparison(
    field_name: str,
    requested_ids: tuple[str, str],
    records_by_id: dict[str, NormalizedProductRecord],
    products_by_id: dict[str, ProductEvidence],
) -> FieldComparison:
    registry = load_field_registry()
    definition = registry.require_field(field_name, ["fund"])
    cells: list[ComparisonCell] = []
    for target_index, product_id in enumerate(requested_ids, start=1):
        record = records_by_id.get(product_id)
        product = products_by_id.get(product_id)
        evidence = _field_evidence(product, field_name)
        cells.append(
            ComparisonCell(
                target_index=target_index,
                product_id=product_id,
                product_name=None if record is None else record.product_name,
                value=None if record is None else record.canonical_value(field_name),
                evidence=evidence,
                trading_currency=(
                    None if record is None else getattr(record, "trading_currency", None)
                ),
            )
        )
    pair = (cells[0], cells[1])
    if any(cell.product_name is None for cell in pair):
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="incomplete",
            delta=None,
            delta_basis=None,
            reason="requested_product_missing",
        )
    if not all(_usable(cell.evidence) for cell in pair):
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="unavailable",
            delta=None,
            delta_basis=None,
            reason="one_or_more_values_unavailable",
        )
    if definition.comparison_scope == "same_trading_currency" and any(
        cell.trading_currency is None for cell in pair
    ):
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="unavailable",
            delta=None,
            delta_basis=None,
            reason="trading_currency_unavailable",
        )
    if (
        definition.comparison_scope == "same_trading_currency"
        and pair[0].trading_currency != pair[1].trading_currency
    ):
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="currency_mismatch",
            delta=None,
            delta_basis=None,
            reason="trading_currency_mismatch",
        )
    if definition.value_type is not ValueType.NUMBER:
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="value_only",
            delta=None,
            delta_basis=None,
            reason="non_numeric_field",
        )
    first = Decimal(str(pair[0].value))
    second = Decimal(str(pair[1].value))
    return FieldComparison(
        canonical_field=field_name,
        label=definition.label,
        unit=definition.unit,
        cells=pair,
        status="numeric_delta",
        delta=second - first,
        delta_basis="second_minus_first",
        reason=None,
    )


def build_fund_comparison(
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
) -> FundComparison:
    """Build a request-ordered, field-level deterministic comparison."""

    require_fund_comparison_contract(plan)
    requested = tuple(fund_comparison_product_ids(plan))
    assert len(requested) == 2
    records_by_id = {record.product_id: record for record in verified.records}
    products_by_id = {product.product_id: product for product in products}
    if len(records_by_id) != len(verified.records):
        raise ValueError("verified comparison contains duplicate product records")
    if len(products_by_id) != len(products):
        raise ValueError("comparison evidence contains duplicate product IDs")
    if set(products_by_id) != set(records_by_id):
        raise ValueError("comparison evidence and verified products differ")

    ordered_records = [
        records_by_id[product_id] for product_id in requested if product_id in records_by_id
    ]
    ordered_products = [
        products_by_id[product_id] for product_id in requested if product_id in products_by_id
    ]
    ordered_verified = VerifiedSearch(
        question_id=verified.question_id,
        candidate_count=verified.candidate_count,
        records=ordered_records,
        manifest=verified.manifest,
    )
    missing = tuple(product_id for product_id in requested if product_id not in records_by_id)
    fields = tuple(
        _field_comparison(
            field_name,
            requested,
            records_by_id,
            products_by_id,
        )
        for field_name in plan.intent_payload.comparison_fields
    )
    return FundComparison(
        requested_product_ids=requested,
        found_product_ids=tuple(record.product_id for record in ordered_records),
        missing_product_ids=missing,
        verified=ordered_verified,
        products=tuple(ordered_products),
        fields=fields,
    )
