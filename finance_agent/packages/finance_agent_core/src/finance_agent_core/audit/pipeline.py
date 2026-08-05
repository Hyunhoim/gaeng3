from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.audit.registry import DatasetSpec, resolve_inputs
from finance_agent_core.audit.xlsx import XlsxStream, index_to_column

FUND_NUMERIC_FIELDS = (
    "fd_nast_suma",
    "fd_wk1_ern_r",
    "fd_mm1_ern_r",
    "fd_mm3_ern_r",
    "fd_mm6_ern_r",
    "fd_mm18_ern_r",
    "fd_yr1_ern_r",
    "fd_yr2_ern_r",
    "fd_yr3_ern_r",
    "fd_yr5_ern_r",
)
FUND_RETURN_FIELDS = tuple(name for name in FUND_NUMERIC_FIELDS if name != "fd_nast_suma")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def _semantic_kind(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "blank"
        if stripped.upper() == "NULL":
            return "literal_null"
    return "value"


def _is_present(value: Any) -> bool:
    return _semantic_kind(value) == "value"


def _decimal(value: Any) -> Decimal | None:
    if not _is_present(value):
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _date(value: Any) -> dt.date | None:
    if not _is_present(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_manifest(path: Path) -> dict[str, Any]:
    return {"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


@dataclass
class FieldStats:
    numeric: bool = False
    categorical: bool = False
    total: int = 0
    missing: int = 0
    blank: int = 0
    literal_null: int = 0
    present: int = 0
    numeric_parse_fail: int = 0
    zero: int = 0
    nonzero: int = 0
    negative: int = 0
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    values: Counter[str] = field(default_factory=Counter)

    def add(self, value: Any) -> None:
        self.total += 1
        kind = _semantic_kind(value)
        if kind == "missing":
            self.missing += 1
            return
        if kind == "blank":
            self.blank += 1
            return
        if kind == "literal_null":
            self.literal_null += 1
            return
        self.present += 1
        if self.categorical:
            self.values[_text(value)] += 1
        if not self.numeric:
            return
        number = _decimal(value)
        if number is None:
            self.numeric_parse_fail += 1
            return
        if number == 0:
            self.zero += 1
        else:
            self.nonzero += 1
        if number < 0:
            self.negative += 1
        if self.minimum is None or number < self.minimum:
            self.minimum = number
        if self.maximum is None or number > self.maximum:
            self.maximum = number

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "total": self.total,
            "missing": self.missing,
            "blank": self.blank,
            "literal_null": self.literal_null,
            "present": self.present,
            "coverage_pct": round(self.present * 100 / self.total, 4) if self.total else None,
        }
        if self.numeric:
            result.update(
                {
                    "numeric_parse_fail": self.numeric_parse_fail,
                    "zero": self.zero,
                    "nonzero": self.nonzero,
                    "negative": self.negative,
                    "min": format(self.minimum, "f") if self.minimum is not None else None,
                    "max": format(self.maximum, "f") if self.maximum is not None else None,
                }
            )
        if self.categorical:
            result["value_counts"] = dict(
                sorted(self.values.items(), key=lambda item: (-item[1], item[0]))
            )
        return result


def _load_schema_columns(schema_path: Path) -> tuple[list[str], list[str]]:
    with XlsxStream(schema_path) as workbook:
        columns: list[str] = []
        for row_number, cells in workbook.iter_rows(0):
            if row_number < 3:
                continue
            name = _text(cells.get(0))
            if name:
                columns.append(name)
        return columns, [name for name, _ in workbook.sheets]


def _required_fields(spec: DatasetSpec) -> set[str]:
    return set(spec.primary_key_fields) | {spec.validation_key_field} | set(spec.metric_fields)


def _primary_key(
    row: dict[int, Any], index: dict[str, int], fields: Iterable[str]
) -> tuple[str, ...]:
    return tuple(_text(row.get(index[name])) for name in fields)


def _category_counts(stats: dict[str, FieldStats], field_name: str) -> dict[str, int]:
    return dict(sorted(stats[field_name].values.items(), key=lambda item: (-item[1], item[0])))


def _product_coverage(
    products: dict[str, dict[str, Any]], field_names: Iterable[str]
) -> dict[str, dict[str, Any]]:
    total = len(products)
    coverage: dict[str, dict[str, Any]] = {}
    for field_name in field_names:
        present = sum(_is_present(product[field_name]) for product in products.values())
        coverage[field_name] = {
            "present": present,
            "total": total,
            "coverage_pct": round(present * 100 / total, 4) if total else None,
        }
    return coverage


def audit_dataset(
    spec: DatasetSpec,
    data_path: Path,
    schema_path: Path,
    snapshot_date: dt.date,
) -> dict[str, Any]:
    schema_columns, schema_sheets = _load_schema_columns(schema_path)
    with XlsxStream(data_path) as workbook:
        rows = workbook.iter_rows(0)
        try:
            header_row_number, header_cells = next(rows)
        except StopIteration as error:
            raise ValueError(f"Workbook has no header row: {data_path}") from error
        max_header_column = max(header_cells, default=-1)
        header = [_text(header_cells.get(index)) for index in range(max_header_column + 1)]
        if not header or any(not name for name in header):
            raise ValueError(f"Workbook has blank header names: {data_path}")
        if len(set(header)) != len(header):
            raise ValueError(f"Workbook has duplicate header names: {data_path}")
        index = {name: position for position, name in enumerate(header)}
        missing_required = sorted(_required_fields(spec) - set(header))
        if missing_required:
            raise ValueError(f"Missing required fields in {data_path.name}: {missing_required}")

        field_stats = {
            name: FieldStats(
                numeric=name in spec.numeric_fields,
                categorical=name in spec.categorical_fields,
            )
            for name in spec.metric_fields
        }
        primary_seen: set[tuple[str, ...]] = set()
        primary_duplicates = 0
        primary_null = 0
        invalid_key_rows: list[int] = []
        extra_column_rows: list[int] = []
        sparse_rows: list[int] = []
        row_present_histogram: Counter[int] = Counter()
        data_rows = 0
        max_row = header_row_number
        max_column = max_header_column

        bond_counts: Counter[str] = Counter()
        isin_seen: set[str] = set()
        isin_duplicate_rows = 0
        isin_missing_rows = 0
        slice_counts: Counter[str] = Counter()
        fund_products: dict[str, dict[str, Any]] = {}
        fund_conflicts: Counter[str] = Counter()
        fund_rows_per_product: Counter[str] = Counter()
        fund_attribute_codes: dict[str, set[str]] = {}
        fund_product_fields = tuple(name for name in header if name != "prfd_attr_cd")

        for row_number, cells in rows:
            data_rows += 1
            max_row = max(max_row, row_number)
            if cells:
                max_column = max(max_column, max(cells))
                if max(cells) >= len(header):
                    extra_column_rows.append(row_number)
            present_count = sum(
                _is_present(cells.get(column_index)) for column_index in range(len(header))
            )
            row_present_histogram[present_count] += 1
            if spec.name == "overseas_etp" and present_count < 40:
                sparse_rows.append(row_number)

            for name, stats in field_stats.items():
                stats.add(cells.get(index[name]))

            primary = _primary_key(cells, index, spec.primary_key_fields)
            if any(not value or value.upper() == "NULL" for value in primary):
                primary_null += 1
            elif primary in primary_seen:
                primary_duplicates += 1
            else:
                primary_seen.add(primary)

            validation_key = _text(cells.get(index[spec.validation_key_field]))
            valid_key = spec.validation_key_pattern.fullmatch(validation_key) is not None
            if not valid_key:
                invalid_key_rows.append(row_number)

            if spec.name == "bond":
                quantity = _decimal(cells.get(index["BUYABLE_QUANTITY"]))
                if quantity is not None:
                    bond_counts["buyable_quantity_present"] += 1
                    if quantity > 0:
                        bond_counts["buyable_quantity_gt_zero"] += 1
                        maturity = _date(cells.get(index["MAT_DT"]))
                        if maturity is not None and maturity >= snapshot_date:
                            bond_counts[
                                "buyable_quantity_gt_zero_maturity_on_or_after_snapshot"
                            ] += 1
                    elif quantity == 0:
                        bond_counts["buyable_quantity_eq_zero"] += 1

            elif spec.name == "overseas_etp":
                isin = _text(cells.get(index["pd_isin_cd"]))
                if not isin or isin.upper() == "NULL":
                    isin_missing_rows += 1
                elif isin in isin_seen:
                    isin_duplicate_rows += 1
                else:
                    isin_seen.add(isin)

                if (
                    _text(cells.get(index["pd_grp_no"])) == "ETF"
                    and _text(cells.get(index["wu_inv_ast_type"])) == "Bond"
                    and _text(cells.get(index["wu_inv_rgn"])) == "United States of America"
                    and _text(cells.get(index["pd_sale_yn"])) == "1"
                    and _text(cells.get(index["pd_tr_yn"])) == "0"
                ):
                    fee = _decimal(cells.get(index["cu_charge_rt"]))
                    if fee is not None and fee <= Decimal("0.20"):
                        slice_counts["fee_lte_0_20_all"] += 1
                        if fee == 0:
                            slice_counts["fee_lte_0_20_zero"] += 1
                        elif fee > 0:
                            slice_counts["fee_lte_0_20_positive"] += 1
                        if _is_present(cells.get(index["du_last_aum"])):
                            slice_counts["fee_lte_0_20_aum_present"] += 1

            elif spec.name == "fund" and valid_key:
                item_number = validation_key
                values = {name: cells.get(index[name]) for name in fund_product_fields}
                fund_rows_per_product[item_number] += 1
                fund_attribute_codes.setdefault(item_number, set()).add(
                    _text(cells.get(index["prfd_attr_cd"]))
                )
                existing = fund_products.get(item_number)
                if existing is None:
                    fund_products[item_number] = values
                else:
                    for name, value in values.items():
                        if _text(existing[name]) != _text(value):
                            fund_conflicts[name] += 1

        fields_output = {name: stats.to_dict() for name, stats in field_stats.items()}
        domain: dict[str, Any] = {}
        if spec.name == "bond":
            domain = dict(bond_counts)
        elif spec.name == "domestic_etp":
            domain = {
                "product_group_counts": _category_counts(field_stats, "pd_grp_no"),
                "sale_status_counts": _category_counts(field_stats, "pd_sale_yn"),
                "trading_status_counts": _category_counts(field_stats, "pd_tr_yn"),
            }
        elif spec.name == "overseas_etp":
            domain = {
                "product_group_counts": _category_counts(field_stats, "pd_grp_no"),
                "sale_status_counts": _category_counts(field_stats, "pd_sale_yn"),
                "trading_status_counts": _category_counts(field_stats, "pd_tr_yn"),
                "isin": {
                    "non_null_unique": len(isin_seen),
                    "duplicate_rows": isin_duplicate_rows,
                    "null_or_blank_rows": isin_missing_rows,
                },
                "first_vertical_slice": dict(slice_counts),
            }
        elif spec.name == "fund":
            product_stats = {
                name: FieldStats(
                    numeric=name in spec.numeric_fields,
                    categorical=name in spec.categorical_fields,
                )
                for name in spec.metric_fields
                if name != "prfd_attr_cd"
            }
            for product in fund_products.values():
                for name, stats in product_stats.items():
                    stats.add(product[name])
            product_fields = {name: stats.to_dict() for name, stats in product_stats.items()}
            rows_histogram = Counter(fund_rows_per_product.values())
            attribute_count_mismatches = sum(
                len(fund_attribute_codes[item_number]) != row_count
                for item_number, row_count in fund_rows_per_product.items()
            )
            scope_counts = Counter()
            for product in fund_products.values():
                offering_scope = _text(product["prvo_pbff_desc"])
                sale_status = _text(product["sale_yn"])
                company_sale = _text(product["thco_sale_yn"])
                scope_counts["valid_products"] += 1
                if offering_scope == "공모":
                    scope_counts["public_products"] += 1
                elif offering_scope == "사모":
                    scope_counts["private_products"] += 1
                else:
                    scope_counts["unknown_offering_scope"] += 1
                if sale_status == "판매중":
                    scope_counts["sale_open_products"] += 1
                if company_sale == "Y":
                    scope_counts["company_sale_y_products"] += 1
                if offering_scope == "공모" and sale_status == "판매중":
                    scope_counts["public_sale_open_products"] += 1
                if offering_scope == "공모" and company_sale == "Y":
                    scope_counts["public_company_sale_y_products"] += 1
                if offering_scope == "공모" and sale_status == "판매중" and company_sale == "Y":
                    scope_counts["public_sale_open_company_y_products"] += 1

            return_outliers: dict[str, dict[str, int]] = {}
            for name in FUND_RETURN_FIELDS:
                below_minus_100 = 0
                above_500 = 0
                for product in fund_products.values():
                    value = _decimal(product[name])
                    if value is None:
                        continue
                    below_minus_100 += value < Decimal("-100")
                    above_500 += value > Decimal("500")
                return_outliers[name] = {
                    "below_minus_100": below_minus_100,
                    "above_500": above_500,
                }

            domain = {
                "product_grain": {
                    "valid_logical_products": len(fund_products),
                    "primary_key": ["itm_no"],
                    "raw_primary_key": ["itm_no", "prfd_attr_cd"],
                    "rows_per_product": {
                        "min": min(fund_rows_per_product.values()),
                        "max": max(fund_rows_per_product.values()),
                        "histogram": {
                            str(row_count): product_count
                            for row_count, product_count in sorted(rows_histogram.items())
                        },
                    },
                    "attribute_code_count_mismatch_products": attribute_count_mismatches,
                    "coverage": _product_coverage(fund_products, product_stats),
                    "fields": product_fields,
                    "field_conflict_counts": dict(sorted(fund_conflicts.items())),
                    "scope_counts": dict(sorted(scope_counts.items())),
                    "return_outliers": return_outliers,
                }
            }

        return {
            "schema_version": "1.0",
            "dataset": spec.name,
            "snapshot_date": snapshot_date.isoformat(),
            "inputs": {
                "data": _file_manifest(data_path),
                "schema": _file_manifest(schema_path),
            },
            "structure": {
                "data_rows": data_rows,
                "columns": len(header),
                "header_row": header_row_number,
                "actual_dimension": (
                    f"A{header_row_number}:{index_to_column(max_column)}{max_row}"
                ),
                "data_sheets": [name for name, _ in workbook.sheets],
                "schema_sheets": schema_sheets,
                "header_schema_exact_match": header == schema_columns,
                "missing_in_data_header": [name for name in schema_columns if name not in header],
                "extra_in_data_header": [name for name in header if name not in schema_columns],
                "row_present_count_histogram": {
                    str(count): rows for count, rows in sorted(row_present_histogram.items())
                },
            },
            "keys": {
                "primary": {
                    "fields": list(spec.primary_key_fields),
                    "non_null_unique": len(primary_seen),
                    "duplicate_rows": primary_duplicates,
                    "null_or_blank_rows": primary_null,
                }
            },
            "quality": {
                "invalid_key_row_count": len(invalid_key_rows),
                "invalid_key_rows": invalid_key_rows,
                "extra_column_row_count": len(extra_column_rows),
                "extra_column_rows": extra_column_rows,
                "sparse_row_count": len(sparse_rows),
                "sparse_rows": sparse_rows,
            },
            "fields": fields_output,
            "domain": domain,
        }


def audit_all(
    data_dir: Path,
    specs: Iterable[DatasetSpec],
    snapshot_date: dt.date,
) -> dict[str, Any]:
    datasets: dict[str, Any] = {}
    for spec in specs:
        data_path, schema_path = resolve_inputs(data_dir, spec)
        datasets[spec.name] = audit_dataset(
            spec=spec,
            data_path=data_path,
            schema_path=schema_path,
            snapshot_date=snapshot_date,
        )
    return {
        "schema_version": "1.0",
        "snapshot_date": snapshot_date.isoformat(),
        "datasets": datasets,
        "summary": {
            "dataset_count": len(datasets),
            "raw_data_rows": sum(
                dataset["structure"]["data_rows"] for dataset in datasets.values()
            ),
        },
    }
