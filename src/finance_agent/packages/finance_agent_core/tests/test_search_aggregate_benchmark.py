from finance_agent_core.evaluation.search_aggregate_benchmark import (
    SearchAggregateCaseResult,
    build_search_aggregate_benchmark_report,
    load_search_aggregate_benchmark_suite,
)


def test_search_aggregate_benchmark_suite_has_complete_four_family_coverage() -> None:
    loaded = load_search_aggregate_benchmark_suite()

    assert len(loaded.suite.cases) == 8
    assert {(case.product_family.value, case.intent) for case in loaded.suite.cases} == {
        (family, intent)
        for family in ("overseas_etp", "domestic_etp", "bond", "fund")
        for intent in ("search", "aggregate")
    }


def test_search_aggregate_benchmark_report_preserves_perfect_summary() -> None:
    loaded = load_search_aggregate_benchmark_suite()
    results = [
        SearchAggregateCaseResult(
            case_id=case.id,
            product_family=case.product_family,
            intent=case.intent,
            status="executed",
            passed=True,
            latency_ms=index + 1,
            max_rss_delta_kib=(index + 1) * 100,
            candidate_count=case.expected.candidate_count,
            fingerprint={
                "candidate_count": case.expected.candidate_count,
                "top_product_ids": case.expected.top_product_ids,
                "aggregates": case.expected.aggregates,
            },
            checks={"fingerprint": True},
        )
        for index, case in enumerate(loaded.suite.cases)
    ]

    report = build_search_aggregate_benchmark_report(loaded, results)

    assert report.summary.total == 8
    assert report.summary.passed == 8
    assert report.summary.strict_accuracy == 1
    assert report.summary.latency_ms_p50 == 4
    assert report.summary.latency_ms_p95 == 8
    assert report.summary.max_rss_delta_kib_max == 800
