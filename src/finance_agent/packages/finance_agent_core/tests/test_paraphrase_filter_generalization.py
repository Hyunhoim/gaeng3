from finance_agent_core.agent.linker import build_lexical_hints


def _constraints(question: str, family: str) -> set[tuple[str, str, object]]:
    hints = build_lexical_hints(question, family, force_product_family_hint=True)
    return {
        (
            item["field"],
            item["operator"],
            tuple(item["value"]) if isinstance(item["value"], list) else item["value"],
        )
        for item in hints["required_constraints"]
    }


def test_overseas_colloquial_filters_keep_metric_ownership() -> None:
    question = (
        "블랙록이 운용하는 전세계 노출 해외 ETF 중 관리비가 연 0.4% 안쪽인 "
        "상품을 몸집 큰 순으로 보여줘"
    )
    hints = build_lexical_hints(question)

    assert ("manager", "contains", "BlackRock") in _constraints(
        question, "overseas_etp"
    )
    assert ("investment_region", "eq", "Global") in _constraints(
        question, "overseas_etp"
    )
    assert ("total_expense_ratio_pct", "lte", 0.4) in _constraints(
        question, "overseas_etp"
    )
    assert hints["required_rankings"] == [
        {"field": "aum", "direction": "desc", "nulls": "last"}
    ]


def test_coordinated_official_values_compile_to_in() -> None:
    constraints = _constraints(
        "나스닥과 뉴욕 양쪽의 주식 또는 채권 자산 해외 ETF를 찾아줘",
        "overseas_etp",
    )

    assert ("exchange_code", "in", ("NAS", "NYS")) in constraints
    assert ("asset_type", "in", ("Equity", "Bond")) in constraints


def test_numeric_presence_never_turns_observed_return_into_positive_return() -> None:
    aum = _constraints("AUM 데이터가 있는 해외 ETF", "overseas_etp")
    returns = _constraints("1일 수익률 데이터가 있는 해외 ETF", "overseas_etp")

    # ``is_present`` is the typed availability operator.  The Oracle and
    # Verifier apply the organizer's rule by excluding both NULL and numeric
    # zero, while preserving valid negative returns instead of turning this
    # wording into a positive-return predicate.
    assert ("aum", "is_present", True) in aum
    assert ("one_day_return_pct", "is_present", True) in returns


def test_schema_noun_is_not_captured_as_manager() -> None:
    constraints = _constraints("단기자금 성격의 해외 ETF", "overseas_etp")

    assert ("asset_type", "eq", "Money Market") in constraints
    assert not any(field == "manager" for field, _, _ in constraints)


def test_korean_count_and_boundary_are_preserved() -> None:
    constraints = _constraints("연간 네 번 이상 분배하는 국내 ETF", "domestic_etp")

    assert ("distribution_payments_per_year", "gte", 4) in constraints


def test_explicit_risk_grade_and_pension_wording_are_preserved() -> None:
    constraints = _constraints(
        "위험 2등급이면서 연금 거래가 되는 국내 ETF", "domestic_etp"
    )

    assert ("risk_level", "eq", "높은위험(2등급)") in constraints
    assert ("pension_eligible", "eq", True) in constraints


def test_leverage_boundary_does_not_also_create_equality() -> None:
    constraints = _constraints(
        "레버리지 배수가 1.5배 이상인 국내 ETF", "domestic_etp"
    )

    assert ("leverage_factor", "gte", 1.5) in constraints
    assert ("leverage_factor", "eq", 1.5) not in constraints


def test_fund_channel_and_fee_paraphrases_are_typed() -> None:
    question = "개인이 온라인으로 가입하는 펀드를 판매회사 몫 수수료가 낮은 순으로"
    hints = build_lexical_hints(question)
    constraints = _constraints(question, "fund")

    assert ("investor_type", "eq", "개인") in constraints
    assert ("sales_channel", "contains", "온라인") in constraints
    assert hints["required_rankings"] == [
        {"field": "sale_company_fee_pct", "direction": "asc", "nulls": "last"}
    ]
