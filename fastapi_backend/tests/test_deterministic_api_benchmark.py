from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import pytest

from scripts.deterministic_api_benchmark import (
    _MAX_HTTP_RESPONSE_BYTES,
    BenchmarkTransportError,
    CgroupV2MemoryCollector,
    DeterministicApiBenchmark,
    HttpExchange,
    OfficialExpectation,
    StdlibHttpClient,
    _rfc3339_epoch_ms,
    canonical_json_sha256,
    container_snapshot_from_inspect,
    evaluate_official_exchange,
    memory_delta,
    normalize_base_url,
    percentile,
)


def _official_body(
    *,
    question_id: str = "BENCH-001",
    question: str = "채권 세 개 보여줘",
    status: str = "success",
    control_code: str | None = None,
) -> dict[str, str]:
    if control_code is None:
        context = {
            "citations": [
                {
                    "citation_id": "bond:one:name",
                    "evidence_refs": ["bond:one:name"],
                }
            ]
        }
        trace = {
            "status": status,
            "intent": "search",
            "product_families": ["bond"],
            "answer_mode": "deterministic",
            "fallback_used": False,
            "control_code": None,
        }
    else:
        context = {"citations": [], "reason": "실행하지 않음"}
        trace = {
            "status": "error",
            "execution_steps": ["admission_control", "safe_control_response"],
            "control_code": control_code,
        }
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "think_trace": json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "answer": "근거가 있는 안전한 응답입니다.",
    }


def _exchange(body: dict[str, Any], *, status: int = 200, latency_ms: float = 7.5):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HttpExchange(
        http_status=status,
        body=body,
        response_bytes=len(raw),
        latency_ms=latency_ms,
    )


def _expectation() -> OfficialExpectation:
    return OfficialExpectation(
        status="success",
        intent="search",
        product_families=("bond",),
    )


def test_percentile_and_canonical_hash_are_order_independent() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 3.85
    assert canonical_json_sha256({"b": 2, "a": 1}) == canonical_json_sha256({"a": 1, "b": 2})
    assert _rfc3339_epoch_ms("2026-08-13T00:00:00.123456789Z") is not None


@pytest.mark.parametrize("control_code", ["request_overloaded", "request_timeout"])
def test_official_admission_controls_require_http_200_and_safe_empty_context(
    control_code: str,
) -> None:
    result = evaluate_official_exchange(
        _exchange(_official_body(control_code=control_code)),
        question_id="BENCH-001",
        question="채권 세 개 보여줘",
        expectation=_expectation(),
        accepted_control_codes=frozenset({"request_overloaded", "request_timeout"}),
    )

    assert result.passed is True
    assert result.control_code == control_code
    assert result.http_status == 200


def test_official_control_is_rejected_when_warm_or_non_200() -> None:
    result = evaluate_official_exchange(
        _exchange(_official_body(control_code="request_timeout"), status=504),
        question_id="BENCH-001",
        question="채권 세 개 보여줘",
        expectation=_expectation(),
        accepted_control_codes=frozenset(),
    )

    assert "official_http_status_not_200" in result.contract_violations
    assert "unexpected_control:request_timeout" in result.semantic_violations
    assert result.passed is False


def test_official_success_checks_semantics_and_does_not_store_raw_body() -> None:
    body = _official_body()
    result = evaluate_official_exchange(
        _exchange(body),
        question_id="BENCH-001",
        question="채권 세 개 보여줘",
        expectation=_expectation(),
        accepted_control_codes=frozenset(),
    )

    assert result.passed is True
    assert result.response_sha256 == canonical_json_sha256(body)
    assert not hasattr(result, "body")


def test_stdlib_client_rejects_oversized_body_before_json_decode(monkeypatch) -> None:
    class OversizedResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, amount: int) -> bytes:
            assert amount == _MAX_HTTP_RESPONSE_BYTES + 1
            return b"x" * amount

    monkeypatch.setattr(
        "scripts.deterministic_api_benchmark.urlopen",
        lambda request, timeout: OversizedResponse(),
    )

    with pytest.raises(BenchmarkTransportError, match="response_oversized"):
        StdlibHttpClient().request("http://127.0.0.1:19001/health", timeout_seconds=1)


def test_cgroup_v2_collector_reads_memory_and_computes_event_delta(tmp_path: Path) -> None:
    (tmp_path / "memory.current").write_text("1024\n", encoding="utf-8")
    (tmp_path / "memory.peak").write_text("2048\n", encoding="utf-8")
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")
    (tmp_path / "memory.events").write_text(
        "low 1\nhigh 2\nmax 3\noom 0\noom_kill 0\n",
        encoding="utf-8",
    )
    collector = CgroupV2MemoryCollector(tmp_path)
    before = collector.collect()
    (tmp_path / "memory.current").write_text("1536\n", encoding="utf-8")
    (tmp_path / "memory.peak").write_text("4096\n", encoding="utf-8")
    (tmp_path / "memory.events").write_text(
        "low 2\nhigh 2\nmax 4\noom 1\noom_kill 1\n",
        encoding="utf-8",
    )
    after = collector.collect()

    assert before.current_bytes == 1024
    assert before.peak_bytes == 2048
    assert before.max_bytes == "max"
    assert before.collection_errors == ()
    assert memory_delta(before, after) == {
        "current_bytes_delta": 512,
        "peak_bytes_delta": 2048,
        "event_deltas": {
            "high": 0,
            "low": 1,
            "max": 1,
            "oom": 1,
            "oom_kill": 1,
        },
    }


def test_container_inspect_allowlists_exit_oom_health_and_identity_metadata() -> None:
    secret = "DO_NOT_COPY_ENV_SECRET"
    snapshot = container_snapshot_from_inspect(
        [
            {
                "Id": "container-id",
                "Image": "sha256:image-id",
                "Config": {"Env": [f"SECRET={secret}"]},
                "State": {
                    "Status": "exited",
                    "Running": False,
                    "Restarting": False,
                    "Dead": False,
                    "ExitCode": 137,
                    "OOMKilled": True,
                    "Error": "runtime detail must not be copied",
                    "StartedAt": "2026-08-13T00:00:00Z",
                    "FinishedAt": "2026-08-13T00:01:00Z",
                    "Health": {"Status": "unhealthy", "Log": [{"Output": secret}]},
                },
            }
        ]
    )

    assert snapshot.container_id == "container-id"
    assert snapshot.image_id == "sha256:image-id"
    assert snapshot.exit_code == 137
    assert snapshot.oom_killed is True
    assert snapshot.runtime_error_present is True
    assert snapshot.health_status == "unhealthy"
    assert secret not in repr(snapshot)


class StableHttpClient:
    def __init__(self) -> None:
        self._lock = Lock()
        self.calls = 0

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: Literal["GET", "POST"] = "GET",
        payload: Mapping[str, str] | None = None,
    ) -> HttpExchange:
        del timeout_seconds, method, payload
        with self._lock:
            self.calls += 1
        if url.endswith("/health"):
            return _exchange(
                {
                    "status": "ok",
                    "configured_product_families": [
                        "bond",
                        "domestic_etp",
                        "overseas_etp",
                        "fund",
                    ],
                    "ready_product_families": [
                        "bond",
                        "domestic_etp",
                        "overseas_etp",
                        "fund",
                    ],
                    "missing_product_families": [],
                    "unavailable_product_families": [],
                    "fund_execution_policy": "locked",
                    "audit_status": "disabled",
                },
                latency_ms=2.0,
            )
        return _exchange(_official_body(), latency_ms=8.0)


def _benchmark(
    client: StableHttpClient,
    *,
    required: frozenset[str] = frozenset(),
    require_runtime_metrics: bool = False,
):
    return DeterministicApiBenchmark(
        base_url="http://127.0.0.1:19001/",
        question_id="BENCH-001",
        question="채권 세 개 보여줘",
        expectation=_expectation(),
        request_timeout_seconds=1.0,
        ready_timeout_seconds=1.0,
        health_poll_interval_seconds=0.01,
        expected_fund_execution_policy="locked",
        warm_requests=3,
        stress_requests=5,
        required_control_codes=required,
        run_smoke_suite=False,
        require_runtime_metrics=require_runtime_metrics,
        http_client=client,
    )


def test_runner_separates_cold_warm_and_stress_and_hashes_responses() -> None:
    client = StableHttpClient()
    report = _benchmark(client).run()

    assert report["passed"] is True
    assert report["health_ready"]["passed"] is True
    assert report["health_ready"]["http_status"] == 200
    assert report["post_load_health"] == {
        "passed": True,
        "http_status": 200,
        "service_status": "ok",
        "audit_status": "disabled",
        "request_latency_ms": 2.0,
        "response_bytes": report["post_load_health"]["response_bytes"],
        "response_sha256": report["health_ready"]["response_sha256"],
        "violations": [],
    }
    assert {
        "origin",
        "attempts",
        "time_to_first_http_ms",
        "time_to_ready_ms",
        "last_transport_error",
    }.isdisjoint(report["post_load_health"])
    assert report["cold_start"]["origin"] == "benchmark_invocation"
    assert [phase["name"] for phase in report["phases"]] == [
        "warm_c1",
        "warm_c2",
        "admission_c4",
        "admission_c8",
    ]
    assert [phase["actual_count"] for phase in report["phases"]] == [3, 4, 8, 8]
    for phase in report["phases"]:
        summary = phase["summary"]
        assert summary["latency_ms"] == {
            "min": 8.0,
            "p50": 8.0,
            "p95": 8.0,
            "p99": 8.0,
            "max": 8.0,
        }
        assert summary["body_bytes"]["total"] > 0
        assert summary["rps"] > 0
        assert summary["deterministic"] is True
        assert len(summary["outcomes"]["status:success"]["canonical_response_sha256"]) == 1
        assert summary["contract_failed"] == 0
        assert summary["semantic_failed"] == 0


def test_required_control_coverage_fails_when_stress_does_not_observe_it() -> None:
    report = _benchmark(StableHttpClient(), required=frozenset({"request_overloaded"})).run()

    assert report["passed"] is False
    assert report["control_contract"]["missing_required_codes"] == ["request_overloaded"]


def test_required_runtime_metrics_fail_when_collectors_are_not_configured() -> None:
    report = _benchmark(StableHttpClient(), require_runtime_metrics=True).run()

    assert report["passed"] is False
    assert report["runtime_metrics"] == {
        "required": True,
        "complete": False,
        "passed": False,
    }


class RetryHealthClient(StableHttpClient):
    def __init__(self) -> None:
        super().__init__()
        self.health_attempts = 0

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: Literal["GET", "POST"] = "GET",
        payload: Mapping[str, str] | None = None,
    ) -> HttpExchange:
        if url.endswith("/health"):
            self.health_attempts += 1
            if self.health_attempts == 1:
                raise BenchmarkTransportError("connect")
        return super().request(
            url,
            timeout_seconds=timeout_seconds,
            method=method,
            payload=payload,
        )


def test_health_ready_probe_records_transport_retry_separately() -> None:
    report = _benchmark(RetryHealthClient()).run()

    assert report["cold_start"]["attempts"] == 2
    assert report["cold_start"]["last_transport_error"] == "connect"
    assert report["passed"] is True


class FailedPostLoadHealthClient(StableHttpClient):
    def __init__(self, *, transport_failure: bool) -> None:
        super().__init__()
        self.health_attempts = 0
        self.transport_failure = transport_failure

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: Literal["GET", "POST"] = "GET",
        payload: Mapping[str, str] | None = None,
    ) -> HttpExchange:
        if url.endswith("/health"):
            self.health_attempts += 1
            if self.health_attempts == 2:
                if self.transport_failure:
                    raise BenchmarkTransportError("connect")
                return _exchange(
                    {
                        "status": "degraded",
                        "configured_product_families": [
                            "bond",
                            "domestic_etp",
                            "overseas_etp",
                            "fund",
                        ],
                        "ready_product_families": [
                            "bond",
                            "domestic_etp",
                            "overseas_etp",
                            "fund",
                        ],
                        "missing_product_families": [],
                        "unavailable_product_families": [],
                        "fund_execution_policy": "locked",
                        "audit_status": "degraded",
                        "detail": "DO_NOT_RETAIN_POST_LOAD_HEALTH_BODY",
                    },
                    status=503,
                    latency_ms=3.0,
                )
        return super().request(
            url,
            timeout_seconds=timeout_seconds,
            method=method,
            payload=payload,
        )


def test_post_load_health_degradation_fails_report_without_retaining_body() -> None:
    report = _benchmark(FailedPostLoadHealthClient(transport_failure=False)).run()

    assert report["passed"] is False
    assert report["post_load_health"]["passed"] is False
    assert report["post_load_health"]["http_status"] == 503
    assert report["post_load_health"]["service_status"] == "degraded"
    assert report["post_load_health"]["audit_status"] == "degraded"
    assert "expected HTTP 200, got 503" in report["post_load_health"]["violations"]
    assert "health status must be ok" in report["post_load_health"]["violations"]
    assert "DO_NOT_RETAIN_POST_LOAD_HEALTH_BODY" not in json.dumps(report)


def test_post_load_health_transport_failure_is_bounded_and_fails_report() -> None:
    report = _benchmark(FailedPostLoadHealthClient(transport_failure=True)).run()

    assert report["passed"] is False
    assert report["post_load_health"] == {
        "passed": False,
        "http_status": None,
        "service_status": None,
        "audit_status": None,
        "request_latency_ms": None,
        "response_bytes": None,
        "response_sha256": None,
        "violations": ["health_transport:connect"],
    }


def test_normalize_base_url_forbids_credentials_query_and_fragment() -> None:
    assert normalize_base_url("http://127.0.0.1:18001/") == "http://127.0.0.1:18001"
    with pytest.raises(ValueError, match="credentials"):
        normalize_base_url("http://user:secret@127.0.0.1:18001")
    with pytest.raises(ValueError, match="query and fragment"):
        normalize_base_url("http://127.0.0.1:18001?token=secret")
