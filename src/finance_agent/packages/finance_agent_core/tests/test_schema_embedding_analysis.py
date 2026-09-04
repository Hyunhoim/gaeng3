from finance_agent_core.evaluation.schema_embedding_analysis import (
    _CaseOutcome,
    _percentile,
    paired_bootstrap_comparison,
)


def test_percentile_uses_linear_interpolation() -> None:
    assert _percentile([0.0, 10.0], 0.25) == 2.5
    assert _percentile([3.0], 0.95) == 3.0


def test_paired_bootstrap_is_deterministic_and_preserves_pairing() -> None:
    selected = [
        _CaseOutcome(exact=True, hits_at_5=2, gold_count=2),
        _CaseOutcome(exact=True, hits_at_5=1, gold_count=1),
        _CaseOutcome(exact=False, hits_at_5=1, gold_count=2),
        _CaseOutcome(exact=True, hits_at_5=2, gold_count=2),
    ]
    comparator = [
        _CaseOutcome(exact=False, hits_at_5=1, gold_count=2),
        _CaseOutcome(exact=True, hits_at_5=1, gold_count=1),
        _CaseOutcome(exact=False, hits_at_5=0, gold_count=2),
        _CaseOutcome(exact=False, hits_at_5=1, gold_count=2),
    ]

    first = paired_bootstrap_comparison(
        selected,
        comparator,
        comparator_name="baseline",
        iterations=1_000,
        seed=42,
    )
    second = paired_bootstrap_comparison(
        selected,
        comparator,
        comparator_name="baseline",
        iterations=1_000,
        seed=42,
    )

    assert first == second
    assert first.selected_only_exact_cases == 2
    assert first.comparator_only_exact_cases == 0
    assert first.exact.observed_delta == 0.5
    assert first.recall_at_5.observed_delta == round(3 / 7, 6)
