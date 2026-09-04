from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from finance_agent_core.agent.product_identity import (
    DatabaseProductIdentityMatcher,
    unresolved_identity_like_spans,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import ProductFamily


def _identity_database(
    path: Path,
    *,
    table: str,
    rows: list[tuple[str, str, str | None, str | None, str | None]],
) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""
            CREATE TABLE {table} (
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                short_name TEXT,
                ticker TEXT,
                isin TEXT,
                is_quarantined INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            f"INSERT INTO {table} "
            "(product_id, product_name, short_name, ticker, isin) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return path


def test_database_identity_uses_longest_exact_name_and_korean_particle(tmp_path: Path) -> None:
    path = _identity_database(
        tmp_path / "domestic.sqlite3",
        table="domestic_etp_products",
        rows=[
            ("KR7000000001", "KODEX 200", "KODEX 200", "A000001", "KR7000000001"),
            (
                "KR7000000002",
                "KODEX 200선물인버스2X",
                "KODEX 200선물인버스2X",
                "A000002",
                "KR7000000002",
            ),
        ],
    )
    matcher = DatabaseProductIdentityMatcher({ProductFamily.DOMESTIC_ETP: path})

    matches = matcher.find("KODEX 200보다 1년 성과가 좋은 국내 ETF 네 개 찾아줘")

    assert len(matches) == 1
    assert matches[0].surface == "KODEX 200"
    assert matches[0].product_ids == ("KR7000000001",)
    assert matches[0].is_unique is True


def test_database_identity_does_not_authorize_invented_name_continuation(tmp_path: Path) -> None:
    path = _identity_database(
        tmp_path / "domestic.sqlite3",
        table="domestic_etp_products",
        rows=[("KR7000000001", "KODEX 200", "KODEX 200", "A000001", "KR7000000001")],
    )
    matcher = DatabaseProductIdentityMatcher({ProductFamily.DOMESTIC_ETP: path})

    matches = matcher.find("KODEX 200 가짜 ETF의 상세 정보를 보여줘")

    assert matches == ()


def test_database_identity_preserves_exact_alias_order_across_and_conjunction(
    tmp_path: Path,
) -> None:
    path = _identity_database(
        tmp_path / "overseas.sqlite3",
        table="overseas_etp_products",
        rows=[
            ("AMX:B1", "Atlas One ETF", None, "B1", "US0000000001"),
            ("AMX:B2", "Atlas Two ETF", None, "B2", "US0000000002"),
        ],
    )
    matcher = DatabaseProductIdentityMatcher({ProductFamily.OVERSEAS_ETP: path})

    matches = matcher.find("상품 ID AMX:B1 및 AMX:B2의 AUM을 입력 순서로 보여 줘")

    assert [match.surface for match in matches] == ["AMX:B1", "AMX:B2"]


def test_adjacent_exact_id_disambiguates_same_name_across_families(tmp_path: Path) -> None:
    domestic = _identity_database(
        tmp_path / "domestic.sqlite3",
        table="domestic_etp_products",
        rows=[
            (
                "KR7000000001",
                "공통 이름 장기성장 상품",
                "공통 이름 장기성장 상품",
                "A000001",
                "KR7000000001",
            )
        ],
    )
    fund = _identity_database(
        tmp_path / "fund.sqlite3",
        table="fund_products",
        rows=[
            (
                "KR5000000001",
                "공통 이름 장기성장 상품",
                "공통 이름 장기성장 상품",
                None,
                None,
            )
        ],
    )
    matcher = DatabaseProductIdentityMatcher(
        {
            ProductFamily.DOMESTIC_ETP: domestic,
            ProductFamily.FUND: fund,
        }
    )

    matches = matcher.find(
        "‘공통 이름 장기성장 상품’(A000001, ID KR7000000001)의 AUM을 알려줘"
    )

    assert matches
    assert all(match.is_unique for match in matches)
    assert {
        candidate.product_family
        for match in matches
        for candidate in match.candidates
    } == {ProductFamily.DOMESTIC_ETP}


def test_unrelated_exact_id_does_not_disambiguate_distant_colliding_name(
    tmp_path: Path,
) -> None:
    domestic = _identity_database(
        tmp_path / "domestic.sqlite3",
        table="domestic_etp_products",
        rows=[
            (
                "KR7000000001",
                "공통 이름 장기성장 상품",
                "공통 이름 장기성장 상품",
                "A000001",
                "KR7000000001",
            )
        ],
    )
    fund = _identity_database(
        tmp_path / "fund.sqlite3",
        table="fund_products",
        rows=[
            (
                "KR5000000001",
                "공통 이름 장기성장 상품",
                "공통 이름 장기성장 상품",
                None,
                None,
            )
        ],
    )
    matcher = DatabaseProductIdentityMatcher(
        {
            ProductFamily.DOMESTIC_ETP: domestic,
            ProductFamily.FUND: fund,
        }
    )

    matches = matcher.find(
        "공통 이름 장기성장 상품의 AUM과, 별도 비교 대상으로 ID KR7000000001을 확인해줘"
    )

    name_match = next(match for match in matches if match.surface == "공통 이름 장기성장 상품")
    assert name_match.is_unique is False


def test_database_identity_coalesces_same_logical_bond_across_venues(tmp_path: Path) -> None:
    path = _identity_database(
        tmp_path / "bond.sqlite3",
        table="bond_products",
        rows=[
            (
                "KR101501DB80:장내:20260821:1",
                "국민주택1종채권 21-08",
                "국민주택1종채권 21-08",
                "KR101501DB80",
                None,
            ),
            (
                "KR101501DB80:장외:20260821:1",
                "국민주택1종채권 21-08",
                "국민주택1종채권 21-08",
                "KR101501DB80",
                None,
            ),
        ],
    )
    matcher = DatabaseProductIdentityMatcher({ProductFamily.BOND: path})

    matches = matcher.find("국민주택1종채권 21-08의 만기일과 표면금리를 알려줘")

    assert len(matches) == 1
    assert matches[0].surface == "국민주택1종채권 21-08"
    assert len(matches[0].product_ids) == 2
    assert matches[0].is_unique is True

    agent = RoutedFinanceAgent({ProductFamily.BOND: path})
    routed = agent.router.route_with_planning(
        "국민주택1종채권 21-08의 만기일과 표면금리를 알려줘",
        "identity-collision-001",
    ).route_decision
    bound = agent._bind_database_product_identities(routed)
    assert bound.reason_code != "ambiguous_product_identity"
    assert bound.draft.product_mentions == ["KR101501DB80"]


def test_unknown_identity_like_surface_is_not_silently_ignored() -> None:
    assert unresolved_identity_like_spans(
        "존재하지않는채권 99-99의 만기일을 알려줘"
    ) == ("존재하지않는채권 99-99",)
    assert unresolved_identity_like_spans("국내 ETF의 AUM을 높은 순으로 보여줘") == ()


def test_named_domestic_anchor_executes_relative_search(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = domestic_sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            ("KODEX 200", "KODEX 200", "KR7000000002"),
        )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})

    question = "KODEX 200보다 1년 수익률이 높은 국내 ETF를 네 개만 골라줘"
    result = agent.answer(
        question,
        "identity-relative-001",
    )

    assert result.status == "executed", (
        result.decision.model_dump(mode="json"),
        result.answer,
    )
    assert result.candidate_count == 0
    assert result.query_plan is not None
    assert result.query_plan.limit == 4
    assert result.query_plan.ranking[0].field == "one_year_return_pct"
    assert any(
        item.field == "one_year_return_pct" and item.operator.value == "gt"
        for item in result.query_plan.constraints
    )


def test_unique_overseas_product_name_resolves_detail_family(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Global X Solar ETF", "B1"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    result = agent.answer(
        "Global X Solar ETF의 상세 정보를 보여줘",
        "identity-detail-001",
    )

    assert result.status == "executed"
    assert result.candidate_count == 1
    assert result.products[0].product_id == "B1"


def test_named_overseas_anchor_is_not_confused_with_generic_etf_token(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Global X Solar ETF", "B1"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    result = agent.answer(
        "Global X Solar ETF보다 총보수가 낮은 해외 ETF 세 개 찾아줘",
        "identity-relative-overseas-001",
    )

    assert result.status == "executed"
    assert result.candidate_count == 1
    # Compound execution preserves the physical exchange-qualified row ID;
    # the public QueryPlan still binds the logical ticker.
    assert [item.product_id for item in result.products] == ["AMX:B4"]
    assert result.query_plan is not None
    assert any(
        item.field == "total_expense_ratio_pct"
        and item.operator.value == "lt"
        and item.value == 0.1
        for item in result.query_plan.constraints
    )


def test_official_name_and_parenthesized_ticker_bind_as_one_product(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Global X Solar ETF", "B1"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    result = agent.answer(
        "Global X Solar ETF(B1)의 연간보수율을 알려줘",
        "identity-name-ticker-detail",
    )

    assert result.status == "executed"
    assert result.decision.draft.product_mentions == ["B1"]
    assert [item.product_id for item in result.products] == ["B1"]


def test_two_names_with_parenthesized_tickers_bind_as_two_products(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Global X Solar ETF", "B1"),
        )
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Global X Robotics ETF", "B2"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    result = agent.answer(
        "Global X Solar ETF(B1)와 Global X Robotics ETF(B2) 중 AUM이 더 큰 상품을 알려줘",
        "identity-name-ticker-compare",
    )

    assert result.status == "executed"
    assert result.decision.draft.product_mentions == ["B1", "B2"]
    assert result.candidate_count == 2
    assert [item.product_id for item in result.products] == ["B2"]


def test_exact_db_name_proves_finance_scope_without_generic_family_word(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            ("Atlas Growth Instrument", "B1"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    result = agent.answer(
        "Atlas Growth Instrument의 상세 정보를 조회해 주세요",
        "identity-db-scope-detail",
    )

    assert result.status == "executed"
    assert [item.product_id for item in result.products] == ["B1"]


def test_ranking_token_inside_exact_product_name_is_not_request_grammar(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = domestic_sample_database
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (
                "하나 1Q K반도체TOP2+증권상장지수투자신탁(주식)",
                "1Q K반도체TOP2+",
                "KR7000000002",
            ),
        )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})

    result = agent.answer(
        "하나 1Q K반도체TOP2+증권상장지수투자신탁(주식)의 상세 정보를 조회해 주세요",
        "identity-name-ranking-token-detail",
    )

    assert result.status == "executed"
    assert [item.product_id for item in result.products] == ["KR7000000002"]


def test_exact_detail_owns_latest_basis_and_attribute_conversation_words(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _records, _manifest = sample_database
    exact_name = "Atlas Low Volatility Instrument"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE ticker = ?",
            (exact_name, "B1"),
        )
    agent = RoutedFinanceAgent({ProductFamily.OVERSEAS_ETP: path})

    for index, question in enumerate(
        (
            f"'{exact_name}'의 주요 속성과 최신 기준일을 확인해 줘.",
            f"‘{exact_name}’의 동적 기준일과 상세 정보를 보여줘.",
        )
    ):
        result = agent.answer(question, f"identity-detail-basis-{index}")
        assert result.status == "executed", result.answer
        assert [item.product_id for item in result.products] == ["B1"]
        assert result.query_plan is not None
        assert result.query_plan.ranking == []
        assert "dynamic_as_of" in result.query_plan.projection

    # "latest return" has no period field.  Exact product identity must not
    # turn that unresolved metric into a guessed one-day return.
    unresolved = agent.answer(
        f"'{exact_name}'의 최신 수익률을 알려줘.",
        "identity-detail-latest-return-control",
    )
    assert unresolved.status == "clarify"
    assert unresolved.products == []


def test_exact_detail_boolean_field_names_are_projection_not_filters(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    path, records, _manifest = domestic_sample_database
    product = records[0]
    exact_name = "Atlas Core Status Instrument"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (exact_name, exact_name, product.product_id),
        )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})

    result = agent.answer(
        f"‘{exact_name}’({product.ticker}, ID {product.product_id})의 "
        "핵심 ETF 여부와 연금 가능 여부, 거래정지 상태를 확인해 주세요.",
        "identity-detail-projection-only",
    )

    assert result.status == "executed", result.answer
    assert [item.product_id for item in result.products] == [product.product_id]
    assert result.query_plan is not None
    assert {item.field for item in result.query_plan.constraints} == {"product_id"}
    assert {
        "core_etf",
        "pension_eligible",
        "trading_suspended",
    }.issubset(result.query_plan.projection)


def test_multiple_exact_products_with_presentation_cue_compare_in_input_order(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    path, records, _manifest = domestic_sample_database
    selected = records[:3]
    names = ["Atlas Alpha Instrument", "Atlas Beta Instrument", "Atlas Gamma Instrument"]
    with sqlite3.connect(path) as connection:
        for record, name in zip(selected, names, strict=True):
            connection.execute(
                "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
                "WHERE product_id = ?",
                (name, name, record.product_id),
            )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})
    targets = ", ".join(
        f"‘{name}’({record.ticker}, ID {record.product_id})"
        for name, record in zip(names, selected, strict=True)
    )

    result = agent.answer(
        f"국내 ETP 세 종목 {targets}의 AUM과 위험등급을 입력한 순서대로 조회해 주세요.",
        "identity-three-products-input-order",
    )

    assert result.status == "executed", result.answer
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "compare"
    assert result.query_plan.intent_payload.comparison_fields == ["aum", "risk_level"]
    assert [item.product_id for item in result.products] == [
        record.product_id for record in selected
    ]


def test_exact_anchor_can_copy_manager_and_apply_a_relative_fee_threshold(
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    """A DB-proven anchor owns equality and relative conditions together."""

    path, records, _manifest = domestic_sample_database
    anchor, cheaper, cheapest = records[:3]
    exact_name = "Atlas 기준 운용 상품"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (exact_name, exact_name, anchor.product_id),
        )
        for record, raw_fee, scaled_fee in (
            (cheaper, "0.30", 300000),
            (cheapest, "0.20", 200000),
        ):
            source_values = json.loads(
                connection.execute(
                    "SELECT source_values_json FROM domestic_etp_products "
                    "WHERE product_id = ?",
                    (record.product_id,),
                ).fetchone()[0]
            )
            source_values["cu_charge_rt"] = raw_fee
            connection.execute(
                "UPDATE domestic_etp_products "
                "SET source_values_json = ?, total_expense_ratio_micro_pct = ? "
                "WHERE product_id = ?",
                (json.dumps(source_values), scaled_fee, record.product_id),
            )
    agent = RoutedFinanceAgent({ProductFamily.DOMESTIC_ETP: path})

    result = agent.answer(
        f"운용사가 ‘{exact_name}’({anchor.ticker}, ID {anchor.product_id})와 같은 "
        "국내 ETP 중 이 상품보다 총보수가 낮은 것을 2개, 싼 순서로 알려주세요.",
        "identity-anchor-manager-relative-fee",
    )

    assert result.status == "executed", result.answer
    assert result.candidate_count == 2
    assert [item.product_id for item in result.products] == [
        cheapest.product_id,
        cheaper.product_id,
    ]
    assert result.query_plan is not None
    assert result.query_plan.ranking[0].field == "total_expense_ratio_pct"
    assert any(
        item.field == "manager" and item.operator.value == "eq"
        for item in result.query_plan.constraints
    )
    assert any(
        item.field == "total_expense_ratio_pct"
        and item.operator.value == "lt"
        and item.value == 0.4
        for item in result.query_plan.constraints
    )


def test_two_exact_products_across_families_compare_common_fields_in_input_order(
    domestic_sample_database: tuple[Path, list[object], object],
    sample_database: tuple[Path, list[object], object],
) -> None:
    domestic_path, domestic_records, _domestic_manifest = domestic_sample_database
    overseas_path, overseas_records, _overseas_manifest = sample_database
    domestic = domestic_records[0]
    overseas = overseas_records[0]
    domestic_name = "Atlas 국내 기준 상품"
    overseas_name = "Atlas Global Reference ETF"
    with sqlite3.connect(domestic_path) as connection:
        connection.execute(
            "UPDATE domestic_etp_products SET product_name = ?, short_name = ? "
            "WHERE product_id = ?",
            (domestic_name, domestic_name, domestic.product_id),
        )
    with sqlite3.connect(overseas_path) as connection:
        connection.execute(
            "UPDATE overseas_etp_products SET product_name = ? WHERE product_id = ?",
            (overseas_name, overseas.product_id),
        )
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_path,
            ProductFamily.OVERSEAS_ETP: overseas_path,
        }
    )

    result = agent.answer(
        f"국내 ‘{domestic_name}’({domestic.ticker}, ID {domestic.product_id})와 "
        f"해외 ‘{overseas_name}’({overseas.ticker}, ID {overseas.product_id})의 "
        "하루 수익률과 총보수를 질문 순서대로 비교해 주세요.",
        "identity-cross-family-projection-comparison",
    )

    assert result.status == "executed", result.answer
    products = [
        product
        for family_search in result.family_searches
        for product in family_search.products
    ]
    assert [item.product_id for item in products] == [
        domestic.product_id,
        overseas.product_id,
    ]
    assert result.candidate_count == 2
    assert result.query_plan is None
    assert [item.product_family for item in result.family_searches] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert all(
        {"one_day_return_pct", "total_expense_ratio_pct"}.issubset(
            item.query_plan.projection
        )
        for item in result.family_searches
    )


def test_cross_family_named_projection_does_not_discard_a_winner_request(
    domestic_sample_database: tuple[Path, list[object], object],
    sample_database: tuple[Path, list[object], object],
) -> None:
    domestic_path, domestic_records, _domestic_manifest = domestic_sample_database
    overseas_path, overseas_records, _overseas_manifest = sample_database
    domestic = domestic_records[0]
    overseas = overseas_records[0]
    agent = RoutedFinanceAgent(
        {
            ProductFamily.DOMESTIC_ETP: domestic_path,
            ProductFamily.OVERSEAS_ETP: overseas_path,
        }
    )

    result = agent.answer(
        f"국내 상품 ID {domestic.product_id}와 해외 상품 ID {overseas.product_id} 중 "
        "하루 수익률이 더 높은 상품을 골라 주세요.",
        "identity-cross-family-winner-control",
    )

    assert result.status != "executed"
    assert result.products == []
    assert result.family_searches == []


def test_suffixed_overseas_ticker_is_reachable_by_its_public_base(tmp_path: Path) -> None:
    # Blind set A: ``해외 ETF QQQ가 …`` and ``해외 ETF TLT의 …`` were asked for
    # clarification while ``SPY`` answered, because the provided table stores
    # ``QQQ.O``/``TLT.O`` but ``SPY`` without a venue suffix.
    path = _identity_database(
        tmp_path / "overseas.sqlite3",
        table="overseas_etp_products",
        rows=[
            ("NAS:QQQ.O", "Invesco QQQ Trust, Series 1", None, "QQQ.O", "US46090E1038"),
            ("NAS:TLT.O", "iShares 20+ Year Treasury Bond ETF", None, "TLT.O", "US4642874329"),
            ("AMX:SPY", "State Street SPDR S&P 500 ETF Trust", None, "SPY", "US78462F1030"),
            # The same base on two venues stays a collision, never a guess.
            ("NAS:NERD.O", "Roundhill Video Games ETF", None, "NERD.O", None),
            ("AMX:NERD.K", "Global X Video Games ETF", None, "NERD.K", None),
        ],
    )
    matcher = DatabaseProductIdentityMatcher({ProductFamily.OVERSEAS_ETP: path})

    base = matcher.find("해외 ETF QQQ가 어떤 시장에 노출되는지 설명하고 1일 수익률을 알려줘.")
    suffixed = matcher.find("해외 ETF TLT.O의 연간보수율을 알려줘.")
    plain = matcher.find("해외 ETF SPY의 기초지수를 알려줘.")
    collision = matcher.find("해외 ETF NERD의 AUM을 알려줘.")

    assert [(m.surface, m.is_unique) for m in base] == [("QQQ", True)]
    assert base[0].candidates[0].canonical_literal == "QQQ.O"
    assert [m.candidates[0].canonical_literal for m in suffixed] == ["TLT.O"]
    assert [m.candidates[0].canonical_literal for m in plain] == ["SPY"]
    assert [(m.surface, m.is_unique) for m in collision] == [("NERD", False)]
