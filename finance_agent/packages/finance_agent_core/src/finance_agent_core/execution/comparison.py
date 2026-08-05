from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from finance_agent_core.config import ComparisonMode, QualityStatus, load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.domain import (
    ComparisonCellEvidence,
    ComparisonEvidence,
    FieldEvidence,
    NormalizedProductRecord,
    ProductEvidence,
    VerifiedSearch,
)
from finance_agent_core.execution.policy import (
    comparison_product_ids,
    require_comparison_contract,
)

type ComparisonStatus = Literal[
    "numeric_delta",
    "value_only",
    "currency_mismatch",
    "as_of_mismatch",
    "stale_input",
    "unavailable",
    "incomplete",
]

_USABLE_QUALITIES = {
    QualityStatus.VALID,
    QualityStatus.PARTIAL,
    QualityStatus.STALE,
}
_STALE_BOND_FIELDS = {
    "buy_yield_pct",
    "after_tax_yield_pct",
    "buyable_quantity",
    "duration_years",
}


def _evidence_value_matches(value: object | None, normalized: object | None) -> bool:
    if isinstance(value, Decimal) and normalized is not None:
        try:
            return value == Decimal(str(normalized))
        except (InvalidOperation, ValueError):
            return False
    if isinstance(value, date):
        return normalized == value.isoformat()
    return value == normalized


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
class ProductComparison:
    requested_product_ids: tuple[str, str]
    found_product_ids: tuple[str, ...]
    missing_product_ids: tuple[str, ...]
    verified: VerifiedSearch
    products: tuple[ProductEvidence, ...]
    fields: tuple[FieldComparison, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_product_ids


# Compatibility alias for the existing public-fund evaluation API.
FundComparison = ProductComparison


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
    family: str,
    requested_ids: tuple[str, str],
    records_by_id: dict[str, NormalizedProductRecord],
    products_by_id: dict[str, ProductEvidence],
) -> FieldComparison:
    registry = load_field_registry()
    definition = registry.require_field(field_name, [family])
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
    if "trading_currency" in definition.comparison_scope and any(
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
        "trading_currency" in definition.comparison_scope
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
    if definition.comparison_mode is ComparisonMode.VALUE_ONLY:
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
    first_evidence = pair[0].evidence
    second_evidence = pair[1].evidence
    assert first_evidence is not None and second_evidence is not None
    if (
        definition.as_of_basis.value in {"dynamic", "snapshot"}
        and first_evidence.as_of != second_evidence.as_of
    ):
        return FieldComparison(
            canonical_field=field_name,
            label=definition.label,
            unit=definition.unit,
            cells=pair,
            status="as_of_mismatch",
            delta=None,
            delta_basis=None,
            reason="field_as_of_mismatch",
        )
    first = Decimal(str(pair[0].value))
    second = Decimal(str(pair[1].value))
    stale = any(
        cell.evidence is not None and cell.evidence.quality is QualityStatus.STALE for cell in pair
    )
    if family == "bond" and field_name in _STALE_BOND_FIELDS:
        stale = stale or any(
            cell.evidence is not None
            and records_by_id[cell.product_id].source_snapshot_date > cell.evidence.as_of
            for cell in pair
        )
    return FieldComparison(
        canonical_field=field_name,
        label=definition.label,
        unit=definition.unit,
        cells=pair,
        status="stale_input" if stale else "numeric_delta",
        delta=second - first,
        delta_basis="second_minus_first",
        reason="one_or_more_values_stale" if stale else None,
    )


class ComparisonResultVerifier:
    """Recheck request order, evidence links, and exact deltas."""

    def verify(self, plan: QueryPlan, comparison: ProductComparison) -> ProductComparison:
        require_comparison_contract(plan)
        requested = tuple(comparison_product_ids(plan))
        if comparison.requested_product_ids != requested:
            raise ValueError("comparison changed requested product order")
        if set(comparison.found_product_ids) & set(comparison.missing_product_ids):
            raise ValueError("found and missing comparison products overlap")
        if set(comparison.found_product_ids) | set(comparison.missing_product_ids) != set(
            requested
        ):
            raise ValueError("comparison product coverage differs from the request")
        if tuple(field.canonical_field for field in comparison.fields) != tuple(
            plan.intent_payload.comparison_fields
        ):
            raise ValueError("comparison field order differs from the QueryPlan")
        product_ids = {product.product_id for product in comparison.products}
        if product_ids != set(comparison.found_product_ids):
            raise ValueError("comparison products and found IDs differ")
        records_by_id = {record.product_id: record for record in comparison.verified.records}
        products_by_id = {product.product_id: product for product in comparison.products}
        family = plan.product_families[0].value
        for field in comparison.fields:
            if tuple(cell.product_id for cell in field.cells) != requested:
                raise ValueError("comparison cell order differs from the request")
            if field.delta is not None:
                if field.delta_basis != "second_minus_first":
                    raise ValueError("comparison delta basis is invalid")
                first, second = field.cells
                if first.value is None or second.value is None:
                    raise ValueError("comparison delta cannot use a missing value")
                expected = Decimal(str(second.value)) - Decimal(str(first.value))
                if field.delta != expected:
                    raise ValueError("comparison delta differs from source values")
            elif field.delta_basis is not None:
                raise ValueError("comparison without delta cannot expose a delta basis")
            for cell in field.cells:
                evidence = cell.evidence
                if evidence is None:
                    continue
                if evidence.canonical_field != field.canonical_field:
                    raise ValueError("comparison cell points to a different evidence field")
                if not _evidence_value_matches(cell.value, evidence.normalized_value):
                    raise ValueError("comparison cell and product evidence values differ")
            expected_field = _field_comparison(
                field.canonical_field,
                family,
                requested,
                records_by_id,
                products_by_id,
            )
            if field != expected_field:
                raise ValueError(
                    f"comparison field {field.canonical_field!r} differs from evidence"
                )
        return comparison


def build_product_comparison(
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
) -> ProductComparison:
    """Build a request-ordered, field-level deterministic comparison."""

    require_comparison_contract(plan)
    requested = tuple(comparison_product_ids(plan))
    assert len(requested) == 2
    family = plan.product_families[0].value
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
            family,
            requested,
            records_by_id,
            products_by_id,
        )
        for field_name in plan.intent_payload.comparison_fields
    )
    comparison = ProductComparison(
        requested_product_ids=requested,
        found_product_ids=tuple(record.product_id for record in ordered_records),
        missing_product_ids=missing,
        verified=ordered_verified,
        products=tuple(ordered_products),
        fields=fields,
    )
    return ComparisonResultVerifier().verify(plan, comparison)


def build_fund_comparison(
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
) -> FundComparison:
    """Backward-compatible alias for the generalized comparison builder."""

    if plan.product_families[0].value != "fund":
        raise ValueError("build_fund_comparison requires product family fund")
    return build_product_comparison(plan, verified, products)


def build_comparison_evidence(
    comparison: ProductComparison,
) -> list[ComparisonEvidence]:
    """Convert the verified comparison into the Backend-safe field DTO."""

    result: list[ComparisonEvidence] = []
    for field in comparison.fields:
        cells: list[ComparisonCellEvidence] = []
        for cell in field.cells:
            evidence = cell.evidence
            cells.append(
                ComparisonCellEvidence(
                    target_index=cell.target_index,
                    product_id=cell.product_id,
                    product_name=cell.product_name,
                    value=None if evidence is None else evidence.normalized_value,
                    trading_currency=cell.trading_currency,
                    quality=None if evidence is None else evidence.quality,
                    quality_reason=None if evidence is None else evidence.quality_reason,
                    as_of=None if evidence is None else evidence.as_of,
                    source_dataset=None if evidence is None else evidence.source_dataset,
                    source_id=None if evidence is None else evidence.source_id,
                    source_row=None if evidence is None else evidence.source_row,
                    source_columns=[] if evidence is None else evidence.source_columns,
                    evidence_ref=(
                        None if evidence is None else f"{cell.product_id}:{field.canonical_field}"
                    ),
                )
            )
        result.append(
            ComparisonEvidence(
                canonical_field=field.canonical_field,
                label=field.label,
                unit=field.unit,
                status=field.status,
                delta=None if field.delta is None else str(field.delta),
                delta_basis=field.delta_basis,
                reason=field.reason,
                cells=cells,
            )
        )
    return result
