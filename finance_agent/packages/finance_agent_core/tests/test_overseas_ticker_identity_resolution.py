from __future__ import annotations

import pytest

from finance_agent_core.agent.product_comparison import (
    ProductIdentityAmbiguousError,
    ProductIdentityNotFoundError,
    resolve_exact_product_ids,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage import ProductIdentityRecord


def _record(
    product_id: str,
    ticker: str,
    *,
    product_name: str | None = None,
    quarantined: bool = False,
) -> ProductIdentityRecord:
    return ProductIdentityRecord(
        product_family=ProductFamily.OVERSEAS_ETP.value,
        product_id=product_id,
        product_name=product_name or f"해외 상품 {product_id}",
        ticker=ticker,
        isin=None,
        short_name=None,
        public_offering=None,
        is_quarantined=quarantined,
    )


def test_unique_exchange_suffix_ticker_resolves_from_exact_base() -> None:
    records = [_record("OS:QQQ.O", "QQQ.O")]

    assert resolve_exact_product_ids(
        ProductFamily.OVERSEAS_ETP,
        ["QQQ"],
        records,
    ) == ["OS:QQQ.O"]


def test_exact_alias_wins_before_base_ticker_fallback() -> None:
    records = [
        _record("OS:QQQ", "QQQ"),
        _record("OS:QQQ.O", "QQQ.O"),
    ]

    assert resolve_exact_product_ids(
        ProductFamily.OVERSEAS_ETP,
        ["qqq"],
        records,
    ) == ["OS:QQQ"]
    assert resolve_exact_product_ids(
        ProductFamily.OVERSEAS_ETP,
        ["QQQ.O"],
        records,
    ) == ["OS:QQQ.O"]


def test_duplicate_exchange_suffix_bases_remain_fail_closed() -> None:
    records = [
        _record("OS:DUP.K", "DUP.K"),
        _record("OS:DUP.O", "DUP.O"),
    ]

    with pytest.raises(ProductIdentityAmbiguousError, match="여러 상품과 일치"):
        resolve_exact_product_ids(
            ProductFamily.OVERSEAS_ETP,
            ["DUP"],
            records,
        )


@pytest.mark.parametrize("mention", ["MISSING", "QQQ.X.Y", "QQ Q"])
def test_unrecognized_or_noncanonical_ticker_does_not_fuzzy_match(mention: str) -> None:
    records = [
        _record("OS:QQQ.O", "QQQ.O", product_name="Invesco QQQ Trust"),
        _record("OS:QUAR.K", "QUAR.K", quarantined=True),
    ]

    with pytest.raises(ProductIdentityNotFoundError, match="정확히 찾지 못했습니다"):
        resolve_exact_product_ids(
            ProductFamily.OVERSEAS_ETP,
            [mention],
            records,
        )
