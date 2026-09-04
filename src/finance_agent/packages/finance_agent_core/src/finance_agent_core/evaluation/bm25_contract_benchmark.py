from __future__ import annotations

import math
import resource
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.retrieval import (
    DocumentFilters,
    DocumentInput,
    DocumentSearchRequest,
    DocumentSourceKind,
    SQLiteDocumentIndex,
)


class BM25ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BM25ContractSummary(BM25ContractModel):
    cases: int
    passed: int
    contract_pass_rate: float = Field(ge=0, le=1)
    positive_retrieval_cases: int = Field(ge=0)
    positive_top1_exact: int = Field(ge=0)
    positive_top1_exact_rate: float = Field(ge=0, le=1)
    negative_control_cases: int = Field(ge=0)
    negative_control_passed: int = Field(ge=0)
    negative_control_pass_rate: float = Field(ge=0, le=1)
    percentile_method: Literal["linear_interpolation"] = "linear_interpolation"
    warm_search_count: int
    warm_latency_ms_p50: float = Field(ge=0)
    warm_latency_ms_p95: float = Field(ge=0)
    warm_latency_ms_max: float = Field(ge=0)
    index_build_ms: float = Field(ge=0)
    process_peak_rss_kib: int = Field(ge=0)
    peak_rss_delta_kib: int = Field(ge=0)


class BM25ContractBenchmarkReport(BM25ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: Literal["bm25-document-contract-v1"] = "bm25-document-contract-v1"
    status: Literal["synthetic_contract_not_quality_evidence"] = (
        "synthetic_contract_not_quality_evidence"
    )
    retrieval_scope: Literal["caller_fed_document_rag"] = "caller_fed_document_rag"
    approved_real_corpus_present: Literal[False] = False
    actual_corpus_quality_measured: Literal[False] = False
    actual_corpus_quality_status: Literal["not_measurable"] = "not_measurable"
    summary: BM25ContractSummary
    warning: str


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    case_kind: Literal["positive_retrieval", "negative_control"]
    expected_document_id: str | None
    filters: DocumentFilters = Field(default_factory=DocumentFilters)


def _rss_kib() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024) if sys.platform == "darwin" else int(peak)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _documents() -> list[DocumentInput]:
    snapshot = date(2026, 7, 11)
    return [
        DocumentInput(
            document_id="synthetic-risk-grade",
            title="위험등급 계약 문서",
            text="위험등급 상품별 분류 기준 확인 절차",
            source_uri="synthetic://risk-grade",
            source_kind=DocumentSourceKind.PROVIDED,
            as_of=snapshot,
            metadata={"category": "risk"},
        ),
        DocumentInput(
            document_id="synthetic-expense",
            title="총보수 계약 문서",
            text="총보수 비용 항목 확인 절차",
            source_uri="synthetic://expense",
            source_kind=DocumentSourceKind.PROVIDED,
            as_of=snapshot,
            metadata={"category": "cost"},
        ),
        DocumentInput(
            document_id="synthetic-snapshot",
            title="기준일 계약 문서",
            text="기준일 스냅샷 갱신 시점 확인 절차",
            source_uri="synthetic://snapshot",
            source_kind=DocumentSourceKind.PROVIDED,
            as_of=snapshot,
            metadata={"category": "date"},
        ),
        DocumentInput(
            document_id="synthetic-yield",
            title="매수수익률 계약 문서",
            text="매수수익률 국내채권 지표 확인 절차",
            source_uri="synthetic://yield",
            source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
            as_of=snapshot,
            metadata={"category": "yield"},
        ),
    ]


def _cases() -> list[_Case]:
    return [
        _Case(
            query="위험등급 분류",
            case_kind="positive_retrieval",
            expected_document_id="synthetic-risk-grade",
        ),
        _Case(
            query="총보수 비용",
            case_kind="positive_retrieval",
            expected_document_id="synthetic-expense",
        ),
        _Case(
            query="기준일 스냅샷",
            case_kind="positive_retrieval",
            expected_document_id="synthetic-snapshot",
        ),
        _Case(
            query="매수수익률 국내채권",
            case_kind="positive_retrieval",
            expected_document_id="synthetic-yield",
        ),
        _Case(
            query="매수수익률 국내채권",
            case_kind="negative_control",
            expected_document_id=None,
            filters=DocumentFilters(source_kinds=[DocumentSourceKind.PROVIDED]),
        ),
        _Case(
            query="승인문서에없는고유질문",
            case_kind="negative_control",
            expected_document_id=None,
        ),
    ]


def _matches(index: SQLiteDocumentIndex, case: _Case) -> bool:
    result = index.search(DocumentSearchRequest(query=case.query, top_k=3, filters=case.filters))
    observed = result.evidence[0].document_id if result.evidence else None
    return observed == case.expected_document_id


def run_bm25_contract_benchmark(*, repetitions: int = 20) -> BM25ContractBenchmarkReport:
    if not 1 <= repetitions <= 1000:
        raise ValueError("BM25 benchmark repetitions must be between 1 and 1000")
    rss_before = _rss_kib()
    with tempfile.TemporaryDirectory(prefix="finance-bm25-contract-") as directory:
        index = SQLiteDocumentIndex(Path(directory) / "documents.sqlite3")
        build_started = time.perf_counter()
        index.initialize()
        for document in _documents():
            index.ingest(document)
        index_build_ms = (time.perf_counter() - build_started) * 1000
        cases = _cases()
        case_results = [(case, _matches(index, case)) for case in cases]
        passed = sum(matched for _, matched in case_results)
        positive_results = [
            matched for case, matched in case_results if case.case_kind == "positive_retrieval"
        ]
        negative_results = [
            matched for case, matched in case_results if case.case_kind == "negative_control"
        ]
        latencies: list[float] = []
        for _ in range(repetitions):
            for case in cases:
                started = time.perf_counter()
                _matches(index, case)
                latencies.append((time.perf_counter() - started) * 1000)
        rss_after = _rss_kib()
    return BM25ContractBenchmarkReport(
        summary=BM25ContractSummary(
            cases=len(cases),
            passed=passed,
            contract_pass_rate=round(passed / len(cases), 6),
            positive_retrieval_cases=len(positive_results),
            positive_top1_exact=sum(positive_results),
            positive_top1_exact_rate=round(sum(positive_results) / len(positive_results), 6),
            negative_control_cases=len(negative_results),
            negative_control_passed=sum(negative_results),
            negative_control_pass_rate=round(sum(negative_results) / len(negative_results), 6),
            warm_search_count=len(latencies),
            warm_latency_ms_p50=round(_percentile(latencies, 0.5), 6),
            warm_latency_ms_p95=round(_percentile(latencies, 0.95), 6),
            warm_latency_ms_max=round(max(latencies, default=0.0), 6),
            index_build_ms=round(index_build_ms, 6),
            process_peak_rss_kib=rss_after,
            peak_rss_delta_kib=max(0, rss_after - rss_before),
        ),
        warning=(
            "이 수치는 합성 문서에서 FTS5/BM25 계약과 실행 비용만 "
            "확인합니다. "
            "승인된 실제 금융 문서 corpus와 relevance gold가 없어 실제 검색 "
            "정확도는 측정하지 않았습니다."
        ),
    )


__all__ = ["BM25ContractBenchmarkReport", "run_bm25_contract_benchmark"]
