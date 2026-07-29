from __future__ import annotations

from decimal import Decimal

from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import QueryPlan
from finance_agent_core.domain import (
    NormalizedBondRecord,
    NormalizedDomesticEtpRecord,
    NormalizedOverseasEtpRecord,
    VerifiedSearch,
)

WARNING_MESSAGES = {
    "provisional_trading_status_mapping": (
        "판매·거래 상태 코드 의미는 공식 코드북 확인 전 잠정 매핑입니다."
    ),
    "unknown_zero_expense_ratio": (
        "총보수 0인 상품은 의미가 확인되지 않아 UNKNOWN으로 제외했습니다."
    ),
    "unknown_zero_aum": ("AUM 결측 또는 0은 유효한 순위값으로 간주하지 않고 null로 처리했습니다."),
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


def warning_codes_for_search(
    plan: QueryPlan,
    verified: VerifiedSearch,
) -> list[str]:
    codes: list[str] = []
    constrained_fields = {constraint.field for constraint in plan.constraints}
    ranked_fields = {ranking.field for ranking in plan.ranking}
    domestic = verified.manifest.dataset == "domestic_etp"
    bond = verified.manifest.dataset == "bond"

    if {"sellable", "trading_suspended"} & constrained_fields:
        codes.append("provisional_trading_status_mapping")
    if "total_expense_ratio_pct" in constrained_fields:
        codes.append("unknown_zero_expense_ratio")
    if "aum" in ranked_fields:
        codes.append("unknown_zero_aum")
    return_fields = {
        "one_day_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "ytd_return_pct",
    }
    if domestic and return_fields & (constrained_fields | ranked_fields):
        codes.append("historical_return_not_forecast")
    used_fields = constrained_fields | ranked_fields | set(plan.projection)
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
    return codes


def render_blocked_plan(plan: QueryPlan, product_family: str) -> str:
    family_label = {
        "domestic_etp": "국내 ETP",
        "overseas_etp": "해외 ETP",
        "bond": "국내채권",
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
    return (
        f"{family_label} 검색을 실행하지 않았습니다. {detail} "
        "조건을 임의로 해석하거나 완화하지 않았습니다."
    )


def render_verified_search(
    plan: QueryPlan,
    verified: VerifiedSearch,
) -> tuple[str, list[str]]:
    warning_codes = warning_codes_for_search(plan, verified)
    warnings = [WARNING_MESSAGES[code] for code in warning_codes]
    domestic = verified.manifest.dataset == "domestic_etp"
    bond = verified.manifest.dataset == "bond"

    if not verified.records:
        family_label = "국내채권" if bond else "국내 ETP" if domestic else "해외 ETP"
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
