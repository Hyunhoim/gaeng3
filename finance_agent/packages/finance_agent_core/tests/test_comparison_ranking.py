from __future__ import annotations

from decimal import Decimal

import pytest

from finance_agent_core.execution.comparison import ComparisonCell, FieldComparison
from finance_agent_core.execution.renderer import _comparison_ranking


def _field(status: str, delta: Decimal | None) -> FieldComparison:
    cells = (
        ComparisonCell(
            target_index=0,
            product_id="A091160",
            product_name="첫 번째 상품",
            value=Decimal("0.52"),
            evidence=None,
            trading_currency=None,
        ),
        ComparisonCell(
            target_index=1,
            product_id="A139260",
            product_name="두 번째 상품",
            value=Decimal("0.40"),
            evidence=None,
            trading_currency=None,
        ),
    )
    return FieldComparison(
        canonical_field="total_expense_ratio_pct",
        label="총보수율",
        unit="pct",
        cells=cells,
        status=status,  # type: ignore[arg-type]
        delta=delta,
        delta_basis="second_minus_first" if delta is not None else None,
        reason=None,
    )


def test_negative_delta_names_the_first_product_as_higher() -> None:
    ranking = _comparison_ranking(_field("numeric_delta", Decimal("-0.12")))
    assert ranking == "  - 더 높음: 첫 번째 상품 / 더 낮음: 두 번째 상품"


def test_positive_delta_names_the_second_product_as_higher() -> None:
    ranking = _comparison_ranking(_field("numeric_delta", Decimal("0.12")))
    assert ranking == "  - 더 높음: 두 번째 상품 / 더 낮음: 첫 번째 상품"


def test_equal_values_state_a_tie_instead_of_a_winner() -> None:
    ranking = _comparison_ranking(_field("numeric_delta", Decimal("0")))
    assert ranking == "  - 두 상품의 총보수율이(가) 같습니다"


def test_stale_input_still_reports_the_verified_order() -> None:
    assert _comparison_ranking(_field("stale_input", Decimal("-0.12"))) is not None


@pytest.mark.parametrize(
    "status",
    ["value_only", "currency_mismatch", "as_of_mismatch", "unavailable", "incomplete"],
)
def test_fields_without_a_verified_delta_claim_no_order(status: str) -> None:
    """Only a proven numeric difference may state which product is higher."""

    assert _comparison_ranking(_field(status, None)) is None
