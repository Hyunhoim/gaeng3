from __future__ import annotations

from decimal import Decimal

from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import AggregateFunction, Intent, QueryPlan
from finance_agent_core.domain import (
    AggregateEvidence,
    NormalizedBondRecord,
    NormalizedDomesticEtpRecord,
    NormalizedOverseasEtpRecord,
    NormalizedPublicFundRecord,
    ProductEvidence,
    VerifiedAggregation,
    VerifiedSearch,
)
from finance_agent_core.execution.comparison import (
    ComparisonCell,
    FieldComparison,
    build_product_comparison,
)
from finance_agent_core.execution.evidence import build_product_evidence

WARNING_MESSAGES = {
    "provisional_trading_status_mapping": (
        "판매·거래 상태 코드 의미는 공식 코드북 확인 전 잠정 매핑입니다."
    ),
    "unknown_zero_expense_ratio": (
        "총보수 0인 상품은 의미가 확인되지 않아 UNKNOWN으로 제외했습니다."
    ),
    "unknown_zero_aum": (
        "AUM 결측 또는 0은 유효한 비교·정렬·집계값으로 간주하지 않고 null로 처리했습니다."
    ),
    "historical_return_not_forecast": (
        "수익률은 원천 제공 기준일의 과거 성과이며 미래 수익을 의미하지 않습니다."
    ),
    "stale_bond_dynamic_values": (
        "채권 매수수량·수익률·듀레이션은 2026-02-24 기준으로 "
        "파일 스냅샷 2026-07-11보다 137일 오래되었습니다."
    ),
    "bond_yield_not_forecast": (
        "채권 수익률은 원천 기준일의 조회값이며 미래 수익이나 실현 수익을 보장하지 않습니다."
    ),
    "partial_credit_rating": (
        "채권 신용등급은 전체 마스터의 58.3809%에만 제공되며 결측을 무등급으로 해석하지 않습니다."
    ),
    "unconfirmed_bond_risk_code": (
        "채권 원천 위험코드는 공식 코드북 확인 전 숫자의 위험 순서를 해석하지 않습니다."
    ),
    "after_tax_yield_assumptions": (
        "세후수익률의 개인별 세제 적용 조건은 제공 데이터만으로 확정할 수 없습니다."
    ),
    "fund_public_scope_locked": (
        "공모펀드 기본 범위를 적용해 사모 및 공·사모 구분 미확인 상품을 제외했습니다."
    ),
    "fund_snapshot_level_returns": (
        "공모펀드 수익률은 개별 갱신일이 없어 파일 스냅샷 2026-07-11을 "
        "기준일 한계와 함께 표시합니다."
    ),
    "unknown_fund_management_attribute": (
        "펀드 운용 속성의 결측 및 의미 미확인 코드 06은 UNKNOWN으로 제외했습니다."
    ),
    "fund_class_level_results": (
        "공모펀드 결과는 itm_no로 식별되는 클래스 단위이며 같은 대표 펀드의 "
        "여러 클래스가 함께 표시될 수 있습니다."
    ),
    "aggregate_missing_values_excluded": (
        "집계 필드의 결측·UNKNOWN·INVALID 값은 0으로 바꾸지 않고 계산에서 제외했습니다."
    ),
    "aggregate_mixed_as_of_dates": (
        "집계에 서로 다른 필드 기준일이 포함되어 최솟값과 최댓값 기준일을 함께 표시합니다."
    ),
    "aggregate_groups_truncated": (
        "그룹 수가 표시 한도를 넘어 행 수가 많은 그룹부터 일부만 표시했습니다."
    ),
    "aggregate_average_rounded": (
        "평균은 유효값만 사용해 계산하고 소수점 이하 12자리에서 반올림했습니다."
    ),
    "aggregate_class_level_sum": (
        "공모펀드 합계는 클래스 단위 행을 더한 값이므로 동일 대표 펀드의 여러 클래스가 "
        "중복 포함될 수 있습니다."
    ),
}


def _format_decimal(value: Decimal, places: int = 6) -> str:
    rendered = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _record_line(index: int, record: NormalizedOverseasEtpRecord) -> str:
    fee = _format_decimal(record.total_expense_ratio_pct)
    aum = "확인 불가" if record.aum is None else f"{record.aum:,.2f} {record.trading_currency}"
    return (
        f"{index}. {record.product_name} ({record.ticker}) — "
        f"총보수 {fee}%, AUM {aum}, AUM 기준일 {record.dynamic_as_of.isoformat()}"
    )


def _format_value(value: object, unit: str) -> str:
    if isinstance(value, Decimal):
        rendered = _format_decimal(value)
        if unit == "pct_point":
            return f"{rendered}%"
        if unit == "source_currency_amount":
            return f"{value:,.2f}"
        if unit == "source_quantity":
            return f"{value:,.0f}"
        if unit == "day":
            return f"{rendered}일"
        if unit == "year":
            return f"{rendered}년"
        return rendered
    if isinstance(value, int) and not isinstance(value, bool):
        if unit == "source_quantity":
            return f"{value:,}"
        if unit == "day":
            return f"{value}일"
        if unit == "year":
            return f"{value}년"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    return str(value)


def _domestic_record_line(
    index: int,
    record: NormalizedDomesticEtpRecord,
    plan: QueryPlan,
) -> str:
    registry = load_field_registry()
    details: list[str] = []
    skipped = {"product_id", "product_name", "ticker"}
    for field_name in plan.projection:
        if field_name in skipped:
            continue
        definition = registry.require_field(field_name, ["domestic_etp"])
        quality, _ = record.row_level_quality(field_name)
        value = record.canonical_value(field_name)
        rendered = (
            "확인 불가"
            if value is None
            or quality
            in {
                QualityStatus.UNKNOWN,
                QualityStatus.INVALID,
                QualityStatus.UNSUPPORTED,
            }
            else _format_value(value, definition.unit)
        )
        details.append(f"{definition.label} {rendered}")
    suffix = f" — {', '.join(details)}" if details else ""
    return f"{index}. {record.product_name} ({record.ticker}){suffix}"


def _bond_record_line(
    index: int,
    record: NormalizedBondRecord,
    plan: QueryPlan,
) -> str:
    registry = load_field_registry()
    details: list[str] = []
    skipped = {"product_id", "product_name", "ticker"}
    for field_name in plan.projection:
        if field_name in skipped:
            continue
        definition = registry.require_field(field_name, ["bond"])
        quality, _ = record.row_level_quality(field_name)
        value = record.canonical_value(field_name)
        rendered = (
            "확인 불가"
            if value is None
            or quality
            in {
                QualityStatus.UNKNOWN,
                QualityStatus.INVALID,
                QualityStatus.UNSUPPORTED,
            }
            else _format_value(value, definition.unit)
        )
        details.append(f"{definition.label} {rendered}")
    suffix = f" — {', '.join(details)}" if details else ""
    return f"{index}. {record.product_name} ({record.ticker}){suffix}"


def _fund_record_line(
    index: int,
    record: NormalizedPublicFundRecord,
    plan: QueryPlan,
) -> str:
    registry = load_field_registry()
    details: list[str] = []
    skipped = {"product_id", "product_name", "short_name"}
    for field_name in plan.projection:
        if field_name in skipped:
            continue
        definition = registry.require_field(field_name, ["fund"])
        quality, _ = record.row_level_quality(field_name)
        value = record.canonical_value(field_name)
        rendered = (
            "확인 불가"
            if value is None
            or quality
            in {
                QualityStatus.UNKNOWN,
                QualityStatus.INVALID,
                QualityStatus.UNSUPPORTED,
            }
            else _format_value(value, definition.unit)
        )
        details.append(f"{definition.label} {rendered}")
    suffix = f" — {', '.join(details)}" if details else ""
    return f"{index}. {record.product_name} ({record.product_id}){suffix}"


def _comparison_cell_value(cell: ComparisonCell, field: FieldComparison) -> str:
    evidence = cell.evidence
    if (
        evidence is None
        or evidence.normalized_value is None
        or evidence.quality
        in {
            QualityStatus.UNKNOWN,
            QualityStatus.INVALID,
            QualityStatus.UNSUPPORTED,
        }
    ):
        return "확인 불가"
    rendered = _format_value(cell.value, field.unit)
    if field.unit == "source_currency_amount" and cell.trading_currency:
        return f"{rendered} {cell.trading_currency}"
    return rendered


def _comparison_evidence(cell: ComparisonCell) -> str:
    evidence = cell.evidence
    if evidence is None:
        return "근거 없음"
    source_columns = "/".join(evidence.source_columns) or "constant"
    return (
        f"{evidence.source_id} 원본 행 {evidence.source_row}, "
        f"{source_columns}, 기준일 {evidence.as_of.isoformat()}"
    )


def _comparison_delta(field: FieldComparison) -> str:
    if field.status in {"numeric_delta", "stale_input"}:
        assert field.delta is not None
        if field.unit == "pct_point":
            value = f"{_format_decimal(field.delta)}%p"
        elif field.unit == "source_currency_amount":
            currency = field.cells[0].trading_currency or "통화 미확인"
            value = f"{field.delta:,.2f} {currency}"
        else:
            value = _format_value(field.delta, field.unit)
        suffix = " (오래된 입력값)" if field.status == "stale_input" else ""
        return f"차이(두 번째-첫 번째) {value}{suffix}"
    if field.status == "currency_mismatch":
        return "거래 통화가 달라 금액 차이를 계산하지 않음"
    if field.status == "as_of_mismatch":
        return "필드 기준일이 달라 차이를 계산하지 않음"
    if field.status == "unavailable":
        if field.reason == "trading_currency_unavailable":
            return "거래 통화를 확인할 수 없어 금액 차이를 계산하지 않음"
        return "하나 이상의 값이 없어 차이를 계산하지 않음"
    if field.status == "incomplete":
        return "비교 대상이 모두 확인되지 않아 차이를 계산하지 않음"
    return "숫자 순서를 부여하지 않고 원천값만 대조"


def render_verified_comparison(
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence],
) -> tuple[str, list[str]]:
    comparison = build_product_comparison(plan, verified, products)
    warning_codes = warning_codes_for_search(plan, comparison.verified)
    warnings = [WARNING_MESSAGES[code] for code in warning_codes]
    family_label = {
        "overseas_etp": "해외 ETP",
        "domestic_etp": "국내 ETP",
        "bond": "국내채권",
        "fund": "공모펀드",
    }[verified.manifest.dataset]
    lines = [
        (
            f"요청한 {family_label} {len(comparison.requested_product_ids)}개 중 "
            f"{len(comparison.found_product_ids)}개를 확인했습니다."
        ),
        "비교 대상:",
    ]
    records_by_id = {record.product_id: record for record in comparison.verified.records}
    for index, product_id in enumerate(comparison.requested_product_ids, start=1):
        record = records_by_id.get(product_id)
        if record is None:
            lines.append(f"{index}. {product_id} — 제공 데이터에서 확인되지 않음")
        else:
            lines.append(f"{index}. {record.product_name} ({record.product_id})")
    if comparison.missing_product_ids:
        lines.append("확인되지 않은 상품: " + ", ".join(comparison.missing_product_ids))
    lines.append("항목별 비교:")
    for field in comparison.fields:
        first, second = field.cells
        lines.extend(
            [
                f"- {field.label}",
                f"  - 첫 번째: {_comparison_cell_value(first, field)}",
                f"  - 두 번째: {_comparison_cell_value(second, field)}",
                f"  - {_comparison_delta(field)}",
                (
                    "  - 근거: "
                    f"첫 번째 {_comparison_evidence(first)}; "
                    f"두 번째 {_comparison_evidence(second)}"
                ),
            ]
        )
    lines.append(
        "원천: "
        f"{verified.manifest.source_file_name} "
        f"(스냅샷 {verified.manifest.source_snapshot_date.isoformat()})"
    )
    answer = "\n".join(lines)
    if warnings:
        answer = f"{answer}\n주의: " + " ".join(warnings)
    answer += (
        "\n차이는 두 번째 상품 값에서 첫 번째 상품 값을 뺀 값입니다. "
        "이 결과는 제공 데이터 조회 결과이며 수익을 보장하거나 투자 판단을 대신하지 않습니다."
    )
    return answer, warnings


def warning_codes_for_search(
    plan: QueryPlan,
    verified: VerifiedSearch,
) -> list[str]:
    codes: list[str] = []
    constrained_fields = {constraint.field for constraint in plan.constraints}
    ranked_fields = {ranking.field for ranking in plan.ranking}
    compared_fields = set(plan.intent_payload.comparison_fields)
    domestic = verified.manifest.dataset == "domestic_etp"
    bond = verified.manifest.dataset == "bond"
    fund = verified.manifest.dataset == "fund"

    if not fund and {"sellable", "trading_suspended"} & constrained_fields:
        codes.append("provisional_trading_status_mapping")
    if "total_expense_ratio_pct" in constrained_fields:
        codes.append("unknown_zero_expense_ratio")
    if "aum" in ranked_fields | compared_fields:
        codes.append("unknown_zero_aum")
    return_fields = {
        "one_week_return_pct",
        "one_day_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "ytd_return_pct",
    }
    if (domestic or fund) and return_fields & (
        constrained_fields | ranked_fields | compared_fields
    ):
        codes.append("historical_return_not_forecast")
    used_fields = constrained_fields | ranked_fields | compared_fields | set(plan.projection)
    bond_dynamic_fields = {
        "currently_buyable",
        "buyable_quantity",
        "buy_yield_pct",
        "after_tax_yield_pct",
        "duration_years",
        "dynamic_as_of",
    }
    if bond and bond_dynamic_fields & used_fields:
        codes.append("stale_bond_dynamic_values")
    if bond and {"buy_yield_pct", "after_tax_yield_pct"} & used_fields:
        codes.append("bond_yield_not_forecast")
    if bond and "credit_rating" in used_fields:
        codes.append("partial_credit_rating")
    if bond and "bond_risk_code" in used_fields:
        codes.append("unconfirmed_bond_risk_code")
    if bond and "after_tax_yield_pct" in used_fields:
        codes.append("after_tax_yield_assumptions")
    if fund:
        codes.append("fund_public_scope_locked")
        codes.append("fund_class_level_results")
    if fund and (
        {
            "one_week_return_pct",
            "one_month_return_pct",
            "three_month_return_pct",
            "six_month_return_pct",
        }
        & used_fields
    ):
        codes.append("fund_snapshot_level_returns")
    if fund and "fund_management_attribute" in used_fields:
        codes.append("unknown_fund_management_attribute")
    return codes


def warning_codes_for_aggregation(
    plan: QueryPlan,
    verified: VerifiedAggregation,
) -> list[str]:
    fields = {aggregation.field for aggregation in plan.intent_payload.aggregations}
    fields.update(plan.intent_payload.group_by)
    fields.update(constraint.field for constraint in plan.constraints)
    codes: list[str] = []
    if "total_expense_ratio_pct" in fields:
        codes.append("unknown_zero_expense_ratio")
    if "aum" in fields:
        codes.append("unknown_zero_aum")
    return_fields = {
        "one_week_return_pct",
        "one_day_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "ytd_return_pct",
    }
    if return_fields & fields:
        codes.append("historical_return_not_forecast")
    if verified.manifest.dataset == "bond":
        if {
            "buyable_quantity",
            "buy_yield_pct",
            "after_tax_yield_pct",
            "duration_years",
        } & fields:
            codes.append("stale_bond_dynamic_values")
        if {"buy_yield_pct", "after_tax_yield_pct"} & fields:
            codes.append("bond_yield_not_forecast")
        if "after_tax_yield_pct" in fields:
            codes.append("after_tax_yield_assumptions")
    if verified.manifest.dataset == "fund":
        codes.extend(["fund_public_scope_locked", "fund_class_level_results"])
        if return_fields & fields:
            codes.append("fund_snapshot_level_returns")
        if "fund_management_attribute" in fields:
            codes.append("unknown_fund_management_attribute")
        if any(
            aggregation.function is AggregateFunction.SUM
            for aggregation in plan.intent_payload.aggregations
        ):
            codes.append("aggregate_class_level_sum")
    metrics = [metric for group in verified.groups for metric in group.metrics]
    if any(metric.missing_count for metric in metrics):
        codes.append("aggregate_missing_values_excluded")
    if any(
        metric.as_of_start is not None and metric.as_of_start != metric.as_of_end
        for metric in metrics
    ):
        codes.append("aggregate_mixed_as_of_dates")
    if verified.total_group_count > len(verified.groups):
        codes.append("aggregate_groups_truncated")
    if any(metric.function is AggregateFunction.AVG for metric in metrics):
        codes.append("aggregate_average_rounded")
    return list(dict.fromkeys(codes))


def _aggregate_currency(plan: QueryPlan, evidence: AggregateEvidence) -> str | None:
    grouped = evidence.group_values.get("trading_currency")
    if isinstance(grouped, str):
        return grouped
    for constraint in plan.constraints:
        if (
            constraint.field == "trading_currency"
            and constraint.operator.value == "eq"
            and isinstance(constraint.value, str)
        ):
            return constraint.value
    return None


def _aggregate_value(plan: QueryPlan, evidence: AggregateEvidence) -> str:
    if evidence.value is None:
        return "확인 불가"
    if evidence.function is AggregateFunction.COUNT:
        return f"{evidence.value:,}개"
    value = Decimal(evidence.value)
    rendered = _format_value(value, evidence.unit)
    if evidence.unit == "source_currency_amount":
        currency = _aggregate_currency(plan, evidence)
        return f"{rendered} {currency}" if currency else f"{rendered} (통화 미확인)"
    return rendered


def _aggregate_as_of(evidence: AggregateEvidence) -> str:
    if evidence.as_of_start is None:
        return "유효값 기준일 없음"
    if evidence.as_of_start == evidence.as_of_end:
        return f"기준일 {evidence.as_of_start.isoformat()}"
    return f"기준일 범위 {evidence.as_of_start.isoformat()}~{evidence.as_of_end.isoformat()}"


def render_verified_aggregation(
    plan: QueryPlan,
    verified: VerifiedAggregation,
    aggregates: list[AggregateEvidence],
) -> tuple[str, list[str]]:
    warning_codes = warning_codes_for_aggregation(plan, verified)
    warnings = [WARNING_MESSAGES[code] for code in warning_codes]
    family_label = {
        "domestic_etp": "국내 ETP",
        "overseas_etp": "해외 ETP",
        "bond": "국내채권",
        "fund": "공모펀드",
    }[verified.manifest.dataset]
    if verified.candidate_count == 0:
        answer = (
            f"잠긴 조건을 모두 만족하고 품질 검증을 통과한 {family_label}가 없어 "
            "집계값을 계산하지 않았습니다. 조건을 자동으로 완화하지 않았습니다."
        )
    else:
        function_labels = {
            AggregateFunction.COUNT: "개수",
            AggregateFunction.MIN: "최솟값",
            AggregateFunction.MAX: "최댓값",
            AggregateFunction.AVG: "평균",
            AggregateFunction.SUM: "합계",
        }
        lines = [
            (
                f"검증된 {family_label} 후보 {verified.candidate_count:,}개를 "
                f"{verified.total_group_count:,}개 그룹으로 집계했습니다."
            )
        ]
        for evidence in aggregates:
            if evidence.group_values:
                registry = load_field_registry()
                group_text = ", ".join(
                    (
                        f"{registry.require_field(field, [verified.manifest.dataset]).label}"
                        f"={value if value is not None else '확인 불가'}"
                    )
                    for field, value in evidence.group_values.items()
                )
            else:
                group_text = "전체"
            metric_label = (
                "상품 수"
                if evidence.function is AggregateFunction.COUNT and evidence.field == "product_id"
                else f"{evidence.label} {function_labels[evidence.function]}"
            )
            share = ""
            if (
                evidence.function is AggregateFunction.COUNT
                and evidence.group_values
                and verified.candidate_count
                and isinstance(evidence.value, int)
            ):
                share_pct = (
                    Decimal(evidence.value) * Decimal("100") / Decimal(verified.candidate_count)
                )
                share = f", 전체 후보의 {_format_decimal(share_pct, places=2)}%"
            columns = "/".join(evidence.source_columns) or "constant"
            lines.append(
                f"- [{group_text}] {metric_label}: {_aggregate_value(plan, evidence)} "
                f"(행 {evidence.row_count:,}개, 유효 {evidence.valid_count:,}개, "
                f"제외 {evidence.missing_count:,}개{share}; {_aggregate_as_of(evidence)}) "
                f"[근거: {evidence.source_id}, {columns}, "
                f"스냅샷 {evidence.source_snapshot_date.isoformat()}]"
            )
        lines.append(
            "원천: "
            f"{verified.manifest.source_file_name} "
            f"(스냅샷 {verified.manifest.source_snapshot_date.isoformat()})"
        )
        answer = "\n".join(lines)
    if warnings:
        answer = f"{answer}\n주의: " + " ".join(warnings)
    answer += (
        "\n이 집계는 제공 데이터의 상품 행을 기준으로 계산했으며 수익을 보장하거나 "
        "투자 판단을 대신하지 않습니다."
    )
    return answer, warnings


def render_blocked_plan(plan: QueryPlan, product_family: str) -> str:
    family_label = {
        "domestic_etp": "국내 ETP",
        "overseas_etp": "해외 ETP",
        "bond": "국내채권",
        "fund": "공모펀드",
    }.get(product_family, product_family)
    reasons: list[str] = []
    if plan.ambiguities:
        reasons.append(
            "확인이 필요한 표현: " + ", ".join(ambiguity.span for ambiguity in plan.ambiguities)
        )
    if plan.unsupported_conditions:
        reasons.append(
            "현재 지원하지 않는 조건: "
            + ", ".join(condition.span for condition in plan.unsupported_conditions)
        )
    detail = " ".join(reasons) if reasons else "실행할 수 없는 계획입니다."
    operation = (
        "비교"
        if plan.intent is Intent.COMPARE
        else "집계"
        if plan.intent is Intent.AGGREGATE
        else "검색"
    )
    return (
        f"{family_label} {operation}를 실행하지 않았습니다. {detail} "
        "조건을 임의로 해석하거나 완화하지 않았습니다."
    )


def render_verified_search(
    plan: QueryPlan,
    verified: VerifiedSearch,
    products: list[ProductEvidence] | None = None,
) -> tuple[str, list[str]]:
    if plan.intent is Intent.COMPARE:
        evidence = products if products is not None else build_product_evidence(plan, verified)
        return render_verified_comparison(plan, verified, evidence)
    warning_codes = warning_codes_for_search(plan, verified)
    warnings = [WARNING_MESSAGES[code] for code in warning_codes]
    domestic = verified.manifest.dataset == "domestic_etp"
    bond = verified.manifest.dataset == "bond"
    fund = verified.manifest.dataset == "fund"

    if not verified.records:
        family_label = (
            "공모펀드" if fund else "국내채권" if bond else "국내 ETP" if domestic else "해외 ETP"
        )
        answer = (
            f"잠긴 조건을 모두 만족하고 품질 검증을 통과한 {family_label}를 찾지 "
            "못했습니다. 조건을 자동으로 완화하지 않았습니다."
        )
    else:
        lines = [
            (f"검증된 후보 {verified.candidate_count}개 중 상위 {len(verified.records)}개입니다."),
            *(
                [
                    _bond_record_line(index, record, plan)
                    for index, record in enumerate(verified.records, start=1)
                    if isinstance(record, NormalizedBondRecord)
                ]
                if bond
                else [
                    _fund_record_line(index, record, plan)
                    for index, record in enumerate(verified.records, start=1)
                    if isinstance(record, NormalizedPublicFundRecord)
                ]
                if fund
                else [
                    _domestic_record_line(index, record, plan)
                    for index, record in enumerate(verified.records, start=1)
                    if isinstance(record, NormalizedDomesticEtpRecord)
                ]
                if domestic
                else [
                    _record_line(index, record)
                    for index, record in enumerate(verified.records, start=1)
                    if isinstance(record, NormalizedOverseasEtpRecord)
                ]
            ),
            (
                "원천: "
                f"{verified.manifest.source_file_name} "
                f"(스냅샷 {verified.manifest.source_snapshot_date.isoformat()})"
            ),
        ]
        answer = "\n".join(lines)

    if warnings:
        answer = f"{answer}\n주의: " + " ".join(warnings)
    answer += (
        "\n이 결과는 제공 데이터 조회 결과이며 수익을 보장하거나 투자 판단을 대신하지 않습니다."
    )
    return answer, warnings
