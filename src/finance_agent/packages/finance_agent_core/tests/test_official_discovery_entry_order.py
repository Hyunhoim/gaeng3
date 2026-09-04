"""Discovery spends its bounded query budget on publishers that can name the entity.

Official example 5 (``에코프로의 자회사를 편입한 ETF``) reached the holdings step with
3-4 queries per subsidiary, all spent on the first registry entries in alphabetical
order (ACE, First Trust, Global X) — overseas issuers whose English factsheets never
mention ``에코프로비엠``.  Approved entries are now ordered by the entity's script;
the registry remains the only authority on which publishers exist.
"""

from __future__ import annotations

import finance_agent_core.agent  # noqa: F401 - resolves the retrieval import cycle
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.official_discovery import order_entries_for_entity_script


def _holdings_entries():
    registry = load_official_source_registry()
    return registry.entries_for_capability(
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(
            entry.source_id
            for entry in registry.entries
            if KnowledgeCapability.PORTFOLIO_HOLDINGS in entry.capabilities
        ),
    )


def test_hangul_entity_puts_korean_publishers_first() -> None:
    ordered = order_entries_for_entity_script(_holdings_entries(), entity_terms=("에코프로비엠",))

    first_families = [set(entry.product_families) for entry in ordered[:5]]
    assert all(ProductFamily.DOMESTIC_ETP in families for families in first_families)
    overseas_only = [
        entry for entry in ordered if set(entry.product_families) == {ProductFamily.OVERSEAS_ETP}
    ]
    assert overseas_only
    assert ordered.index(overseas_only[0]) >= len(ordered) - len(overseas_only)


def test_latin_entity_puts_overseas_publishers_first() -> None:
    ordered = order_entries_for_entity_script(_holdings_entries(), entity_terms=("Cambricon",))

    assert all(ProductFamily.OVERSEAS_ETP in set(entry.product_families) for entry in ordered[:5])


def test_mixed_or_empty_entity_keeps_registry_order() -> None:
    entries = _holdings_entries()

    assert order_entries_for_entity_script(
        entries, entity_terms=("캠브리콘", "Cambricon")
    ) == tuple(entries)
    assert order_entries_for_entity_script(entries, entity_terms=()) == tuple(entries)


def test_ordering_never_adds_or_drops_entries() -> None:
    entries = _holdings_entries()
    ordered = order_entries_for_entity_script(entries, entity_terms=("에코프로비엠",))

    assert sorted(entry.source_id for entry in ordered) == sorted(
        entry.source_id for entry in entries
    )


def test_universe_brand_weight_puts_issuers_with_more_candidates_first() -> None:
    surfaces = (
        "삼성 KODEX 2차전지산업증권상장지수투자신탁[주식]",
        "삼성 KODEX 2차전지핵심소재10증권상장지수투자신탁[주식]",
        "삼성 KODEX 2차전지산업레버리지증권상장지수투자신탁[주식-파생형]",
        "미래에셋 TIGER 2차전지테마증권상장지수투자신탁(주식)",
        "미래에셋 TIGER 2차전지TOP10 증권상장지수투자신탁(주식)",
        "KB RISE 2차전지액티브증권상장지수투자신탁(주식)",
    )

    ordered = order_entries_for_entity_script(
        _holdings_entries(), entity_terms=("에코프로비엠",), product_surfaces=surfaces
    )

    leaders = [entry.publisher for entry in ordered[:5]]
    assert leaders[0] == "삼성자산운용"
    assert "미래에셋자산운용" in leaders
    assert all(ProductFamily.DOMESTIC_ETP in set(entry.product_families) for entry in ordered[:5])
