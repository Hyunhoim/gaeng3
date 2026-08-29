from scripts.compare_sealed_blind import (
    _gold_product_identity,
    _normalize_filter,
    _source_field_map,
)


def test_official_source_column_mapping_is_case_insensitive() -> None:
    source_fields = _source_field_map()

    assert source_fields[("bond", "pd_no")] == "ticker"
    assert _normalize_filter(
        {"field": "PD_NO", "operator": "eq", "value": "KRTEST"},
        family="bond",
        source_fields=source_fields,
    ) == ("product_identity", "eq", "krtest")


def test_gold_labels_and_typed_filters_share_semantic_normal_form() -> None:
    source_fields = _source_field_map()

    assert _normalize_filter(
        {"field": "public_offering", "operator": "eq", "value": "공모"},
        family="fund",
        source_fields=source_fields,
    ) == _normalize_filter(
        {"field": "public_offering", "operator": "eq", "value": True},
        family="fund",
        source_fields=source_fields,
    )
    assert _normalize_filter(
        {"field": "sellable", "operator": "eq", "value": "판매중"},
        family="fund",
        source_fields=source_fields,
    ) == _normalize_filter(
        {"field": "sellable", "operator": "eq", "value": True},
        family="fund",
        source_fields=source_fields,
    )


def test_gold_identity_uses_each_official_table_public_key() -> None:
    product = {"product_id": "INTERNAL-ROW", "ticker": "PUBLIC-TICKER"}

    assert _gold_product_identity(product, "bond") == "PUBLIC-TICKER"
    assert _gold_product_identity(product, "overseas_etp") == "PUBLIC-TICKER"
    assert _gold_product_identity(product, "domestic_etp") == "INTERNAL-ROW"
    assert _gold_product_identity(product, "fund") == "INTERNAL-ROW"
