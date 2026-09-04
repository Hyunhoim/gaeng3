from __future__ import annotations

import json

import pytest

from scripts.deterministic_api_benchmark import (
    ContainerSnapshot,
    HttpExchange,
    OfficialExpectation,
    RequestResult,
)
from scripts.deterministic_api_soak import (
    DeterministicApiSoak,
    RuntimeSnapshot,
    _completion_belongs_to_measurement,
    _SoakResults,
    assess_container_runtime,
    assess_runtime_plateau,
    runtime_snapshot_from_payload,
)


def _snapshot(
    elapsed_seconds: float,
    *,
    memory_current_bytes: int = 128 * 1024 * 1024,
    file_descriptor_count: int = 24,
    thread_count: int = 8,
    pids_current: int = 8,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        elapsed_seconds=elapsed_seconds,
        memory_current_bytes=memory_current_bytes,
        memory_peak_bytes=max(memory_current_bytes, 128 * 1024 * 1024),
        pids_current=pids_current,
        process_count=2,
        thread_count=thread_count,
        file_descriptor_count=file_descriptor_count,
    )


def test_runtime_snapshot_rejects_missing_or_non_integer_counters() -> None:
    payload = {
        "memory_current_bytes": 1,
        "memory_peak_bytes": 2,
        "pids_current": 3,
        "process_count": 4,
        "thread_count": 5,
        "file_descriptor_count": 6,
    }

    assert runtime_snapshot_from_payload(payload, elapsed_seconds=1.25) == RuntimeSnapshot(
        elapsed_seconds=1.25,
        memory_current_bytes=1,
        memory_peak_bytes=2,
        pids_current=3,
        process_count=4,
        thread_count=5,
        file_descriptor_count=6,
    )
    with pytest.raises(ValueError, match="field set"):
        runtime_snapshot_from_payload({**payload, "unexpected": 7}, elapsed_seconds=1)
    with pytest.raises(ValueError, match="non-negative integers"):
        runtime_snapshot_from_payload(
            {**payload, "thread_count": True},
            elapsed_seconds=1,
        )


def test_runtime_plateau_accepts_stable_post_warmup_samples() -> None:
    snapshots = [
        _snapshot(
            float(index * 5),
            memory_current_bytes=128 * 1024 * 1024 + (index % 2) * 256 * 1024,
        )
        for index in range(12)
    ]

    report = assess_runtime_plateau(snapshots)

    assert report["passed"] is True
    assert all(report["checks"].values())


def test_runtime_plateau_detects_memory_fd_thread_and_pid_growth() -> None:
    snapshots = [
        _snapshot(
            float(index * 5),
            memory_current_bytes=128 * 1024 * 1024 + index * 8 * 1024 * 1024,
            file_descriptor_count=24 + index,
            thread_count=8 + index,
            pids_current=8 + index,
        )
        for index in range(12)
    ]

    report = assess_runtime_plateau(snapshots)

    assert report["passed"] is False
    assert report["checks"] == {
        "memory_growth_within_tolerance": False,
        "memory_slope_within_tolerance": False,
        "fd_growth_within_tolerance": False,
        "thread_growth_within_tolerance": False,
        "pid_growth_within_tolerance": False,
    }


def test_runtime_plateau_requires_four_measurement_samples() -> None:
    report = assess_runtime_plateau([_snapshot(0), _snapshot(5), _snapshot(10)])

    assert report == {
        "sample_count": 3,
        "passed": False,
        "reason": "at_least_four_post_warmup_samples_required",
    }


@pytest.mark.parametrize(
    ("base_url", "container_name", "message"),
    [
        ("http://127.0.0.1:18001", "finance-perf-unit", "non-shared"),
        ("http://127.0.0.1:18002", "finance-perf-unit", "non-shared"),
        ("https://example.com:18144", "finance-perf-unit", "loopback"),
        ("http://127.0.0.1:18144", "shared-backend", "finance-perf-"),
    ],
)
def test_soak_rejects_shared_or_nonisolated_targets(
    base_url: str,
    container_name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DeterministicApiSoak(
            base_url=base_url,
            container_name=container_name,
            question_id="isolation-test",
            question="격리 대상 검증",
            expectation=OfficialExpectation(
                status="success",
                intent="search",
                product_families=("bond",),
            ),
            concurrency=1,
            warmup_seconds=0,
            duration_seconds=1,
            sample_interval_seconds=1,
            request_timeout_seconds=1,
        )


def test_soak_rejects_non_contract_fund_execution_policy() -> None:
    with pytest.raises(ValueError, match="public_fund_v1_approved"):
        DeterministicApiSoak(
            base_url="http://127.0.0.1:18144",
            container_name="finance-perf-unit",
            question_id="policy-test",
            question="격리 대상 검증",
            expectation=OfficialExpectation(
                status="success",
                intent="search",
                product_families=("bond",),
            ),
            concurrency=1,
            warmup_seconds=0,
            duration_seconds=1,
            sample_interval_seconds=1,
            request_timeout_seconds=1,
            expected_fund_execution_policy="enabled",
        )


def _container_snapshot(**overrides: object) -> ContainerSnapshot:
    values: dict[str, object] = {
        "configured": True,
        "collected_at_utc": "2026-08-14T00:00:00+00:00",
        "status": "running",
        "running": True,
        "restarting": False,
        "dead": False,
        "exit_code": 0,
        "oom_killed": False,
        "runtime_error_present": False,
        "started_at": "2026-08-14T00:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "health_status": "healthy",
        "container_id": "container-id",
        "image_id": "sha256:" + "a" * 64,
        "collection_error": None,
    }
    values.update(overrides)
    return ContainerSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    [
        ({"dead": True}, "not_dead_after_load"),
        ({"runtime_error_present": True}, "no_runtime_error_after_load"),
        ({"health_status": "unhealthy"}, "docker_health_not_unhealthy"),
        ({"running": False, "status": "exited"}, "running_after_load"),
    ],
)
def test_container_runtime_assessment_fails_closed_on_unsafe_state(
    overrides: dict[str, object],
    failed_check: str,
) -> None:
    report = assess_container_runtime(
        _container_snapshot(),
        _container_snapshot(**overrides),
    )

    assert report["passed"] is False
    assert report["checks"][failed_check] is False


def test_measurement_completion_excludes_post_deadline_results() -> None:
    assert _completion_belongs_to_measurement(
        started_while_measuring=True,
        completed_at=9.999,
        measurement_deadline=10.0,
    )
    assert not _completion_belongs_to_measurement(
        started_while_measuring=True,
        completed_at=10.001,
        measurement_deadline=10.0,
    )
    assert not _completion_belongs_to_measurement(
        started_while_measuring=False,
        completed_at=9.0,
        measurement_deadline=10.0,
    )


def _official_exchange(*, evidence_refs: list[str], response_bytes: int) -> HttpExchange:
    body = {
        "question_id": "SOAK-001",
        "question": "채권 세 개 보여줘",
        "retrieved_context": json.dumps(
            {
                "citations": [
                    {
                        "citation_id": "bond:one:name",
                        "evidence_refs": evidence_refs,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        "think_trace": json.dumps(
            {
                "status": "success",
                "intent": "search",
                "product_families": ["bond"],
                "answer_mode": "deterministic",
                "fallback_used": False,
                "control_code": None,
            },
            ensure_ascii=False,
        ),
        "answer": "결정론적 응답",
    }
    return HttpExchange(
        http_status=200,
        body=body,
        response_bytes=response_bytes,
        latency_ms=10.0,
    )


def _passed_result(fingerprint: str) -> RequestResult:
    return RequestResult(
        http_status=200,
        response_bytes=100,
        latency_ms=10.0,
        response_sha256=fingerprint,
        status="success",
        control_code=None,
        contract_violations=(),
        semantic_violations=(),
        transport_error=None,
    )


def test_soak_stability_detects_response_payload_and_evidence_drift() -> None:
    results = _SoakResults()
    results.record(
        exchange=_official_exchange(evidence_refs=["bond:one:name"], response_bytes=100),
        result=_passed_result("a" * 64),
        error=None,
    )
    results.record(
        exchange=_official_exchange(
            evidence_refs=["bond:one:name", "bond:one:yield"],
            response_bytes=120,
        ),
        result=_passed_result("b" * 64),
        error=None,
    )
    results.record_late_completion()

    stability = results.snapshot()["deterministic_stability"]

    assert stability["passed"] is False
    assert stability["mismatch_counts"] == {
        "canonical_response_sha256": 1,
        "evidence_ref_count": 1,
        "payload_bytes": 1,
    }
    assert results.snapshot()["late_completions_excluded"] == 1


class _HealthClient:
    def __init__(self, audit_status: str) -> None:
        self.audit_status = audit_status

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: str = "GET",
        payload: object = None,
    ) -> HttpExchange:
        del url, timeout_seconds, method, payload
        body = {
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
            "audit_status": self.audit_status,
        }
        return HttpExchange(
            http_status=200,
            body=body,
            response_bytes=len(json.dumps(body).encode("utf-8")),
            latency_ms=2.0,
        )


def _soak_for_health(client: _HealthClient) -> DeterministicApiSoak:
    return DeterministicApiSoak(
        base_url="http://127.0.0.1:18144",
        container_name="finance-perf-health-unit",
        question_id="health-test",
        question="격리 대상 검증",
        expectation=OfficialExpectation(
            status="success",
            intent="search",
            product_families=("bond",),
        ),
        concurrency=1,
        warmup_seconds=0,
        duration_seconds=1,
        sample_interval_seconds=1,
        request_timeout_seconds=1,
        http_client=client,  # type: ignore[arg-type]
    )


def test_post_load_health_is_required_and_does_not_retain_body() -> None:
    passed = _soak_for_health(_HealthClient("ok"))._post_load_health_probe()
    degraded = _soak_for_health(_HealthClient("degraded"))._post_load_health_probe()

    assert passed["passed"] is True
    assert passed["service_status"] == "ok"
    assert passed["audit_status"] == "ok"
    assert "configured_product_families" not in passed
    assert degraded["passed"] is False
    assert degraded["violations"] == ["audit status must be disabled or ok"]
