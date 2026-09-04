from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.routed_service import IntentRouter, ServerQueryPlanCompiler
from finance_agent_core.agent.semantic_aliases import filter_alias_matches
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.execution.sql_schema import SQL_FIELDS_BY_FAMILY
from finance_agent_core.normalization.overseas_etp import (
    SOURCE_COLUMNS,
    normalize_overseas_etp_row,
)


def test_overseas_normalization_retains_official_text_source_columns() -> None:
    values = dict.fromkeys(SOURCE_COLUMNS)
    values.update(
        {
            "pd_exg_mkt_cd": "NAS",
            "pd_itm_no": "TEST.O",
            "pd_grp_no": "ETF",
            "pd_nm": "Test Overseas ETF",
            "pd_trd_ccy": "USD",
            "cu_charge_rt": "0.12",
            "cu_base_index": "S&P 500 TR",
            "cu_strtegy": "The Fund seeks to track the index.",
            "cu_index_repl_mthd": "Full",
            "du_opr": 764.0700000000001,
            "cu_upt_dt": "20260824",
            "du_upt_dt": "20260824",
        }
    )

    record = normalize_overseas_etp_row(
        source_row=2,
        values=values,
        present_source_fields=sum(value is not None for value in values.values()),
        source_snapshot_date=dt.date(2026, 8, 24),
    )

    assert {"cu_base_index", "cu_strtegy", "cu_index_repl_mthd"} <= set(SOURCE_COLUMNS)
    assert record.source_values["cu_base_index"] == "S&P 500 TR"
    assert record.source_values["cu_strtegy"] == "The Fund seeks to track the index."
    assert record.source_values["cu_index_repl_mthd"] == "Full"
    assert record.canonical_value("open_price") == Decimal("764.07000000")


def test_overseas_sql_projects_all_three_fields_from_source_json() -> None:
    fields = SQL_FIELDS_BY_FAMILY["overseas_etp"]

    assert fields["base_index"].column == ("json_extract(source_values_json, '$.cu_base_index')")
    assert fields["strategy_description"].column == (
        "json_extract(source_values_json, '$.cu_strtegy')"
    )
    assert fields["index_replication_method"].column == (
        "json_extract(source_values_json, '$.cu_index_repl_mthd')"
    )
    assert "strategy_description" not in SQL_FIELDS_BY_FAMILY["domestic_etp"]
    assert "index_replication_method" not in SQL_FIELDS_BY_FAMILY["domestic_etp"]


def test_registry_v18_exposes_audited_overseas_text_contracts() -> None:
    registry = load_field_registry()
    base_index = registry.require_field("base_index", ["overseas_etp"])
    strategy = registry.require_field("strategy_description", ["overseas_etp"])
    replication = registry.require_field(
        "index_replication_method",
        ["overseas_etp"],
    )

    assert registry.schema_version == "1.8"
    assert base_index.source.columns == ["cu_base_index"]
    assert base_index.quality is QualityStatus.PARTIAL
    assert base_index.coverage_pct == pytest.approx(51.4494)
    assert base_index.allowed_operators == ["eq", "contains"]
    assert strategy.source.columns == ["cu_strtegy"]
    assert strategy.coverage_pct == pytest.approx(99.8178)
    assert strategy.allowed_operators == ["eq", "contains"]
    assert replication.source.columns == ["cu_index_repl_mthd"]
    assert replication.coverage_pct == pytest.approx(39.8708)
    assert replication.enum_values == ["Optimized", "Swap", "Full", "Other"]
    assert replication.allowed_operators == ["eq", "in"]


def test_explicit_overseas_field_names_are_projected() -> None:
    payload = canonicalize_query_plan_payload(
        "해외 ETF의 기초지수, 운용전략, 복제방식을 원천값 그대로 보여 주세요.",
        {"question_id": "overseas-schema-projection"},
    )

    assert {
        "base_index",
        "strategy_description",
        "index_replication_method",
    } <= set(payload["projection"])


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "기초지수에 S&P 500이 포함된 해외 ETF를 보여 주세요.",
            ("base_index", "contains", "S&P 500"),
        ),
        (
            "공식 DB에서 해외 ETF 중 S&P 500 TR을 기초지수로 하는 해외 ETF를 보여 주세요.",
            ("base_index", "eq", "S&P 500 TR"),
        ),
        (
            "운용전략에 clean energy라는 문구가 포함된 해외 ETF를 보여 주세요.",
            ("strategy_description", "contains", "clean energy"),
        ),
    ],
)
def test_linker_requires_explicit_field_labels_for_overseas_text_filters(
    question: str,
    expected: tuple[str, str, str],
) -> None:
    hints = build_lexical_hints(question)
    observed = {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }
    decision = IntentRouter().route(question, "overseas-schema-completion")

    assert expected in observed
    assert not hints["ambiguity_spans"]
    assert decision.disposition.value == "execute"
    plan = ServerQueryPlanCompiler({}).compile(decision)
    assert expected in {(item.field, item.operator.value, item.value) for item in plan.constraints}


@pytest.mark.parametrize(
    ("surface", "canonical"),
    [
        ("최적화복제", "Optimized"),
        ("합성복제", "Swap"),
        ("완전복제", "Full"),
        ("기타", "Other"),
    ],
)
def test_typed_aliases_canonicalize_explicit_replication_methods(
    surface: str,
    canonical: str,
) -> None:
    question = f"지수 복제방식이 {surface}인 해외 ETF를 보여 주세요."
    matches = filter_alias_matches(question, "overseas_etp")
    hints = build_lexical_hints(question)

    assert [(item.field_id, item.operator, item.value) for item in matches] == [
        ("index_replication_method", "eq", canonical)
    ]
    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } >= {("index_replication_method", "eq", canonical)}


@pytest.mark.parametrize(
    "question",
    [
        "복제 방식이 Physical인 해외 ETF를 보여 주세요.",
        "복제 방식이 Full이 아닌 해외 ETF를 보여 주세요.",
        "운용전략에 AI가 포함되지 않은 해외 ETF를 보여 주세요.",
    ],
)
def test_unknown_or_negated_overseas_text_filters_fail_closed(question: str) -> None:
    hints = build_lexical_hints(question)

    assert hints["ambiguity_spans"]
