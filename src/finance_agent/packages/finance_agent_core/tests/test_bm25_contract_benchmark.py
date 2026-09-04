from finance_agent_core.evaluation.bm25_contract_benchmark import (
    run_bm25_contract_benchmark,
)


def test_bm25_contract_benchmark_is_explicitly_synthetic() -> None:
    report = run_bm25_contract_benchmark(repetitions=1)

    assert report.summary.cases == 6
    assert report.summary.passed == 6
    assert report.summary.contract_pass_rate == 1
    assert report.summary.positive_retrieval_cases == 4
    assert report.summary.positive_top1_exact == 4
    assert report.summary.positive_top1_exact_rate == 1
    assert report.summary.negative_control_cases == 2
    assert report.summary.negative_control_passed == 2
    assert report.summary.negative_control_pass_rate == 1
    assert report.summary.warm_search_count == 6
    assert not report.approved_real_corpus_present
    assert not report.actual_corpus_quality_measured
    assert report.actual_corpus_quality_status == "not_measurable"
