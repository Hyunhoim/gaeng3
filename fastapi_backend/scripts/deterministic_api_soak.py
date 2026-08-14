from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.deterministic_api_benchmark import (
    BenchmarkTransportError,
    ContainerCollector,
    ContainerSnapshot,
    DockerInspectCollector,
    HttpClient,
    OfficialExpectation,
    StdlibHttpClient,
    canonical_json_sha256,
    evaluate_official_exchange,
    require_isolated_base_url,
)
from scripts.deterministic_performance_analysis import numeric_summary
from scripts.smoke import validate_health

_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_LATENCY_SAMPLES = 100_000
_SHARED_PORTS = frozenset({18_001, 18_002})

_CONTAINER_PROBE = r"""
import json, os

def number(path):
    try:
        raw = open(path, encoding="ascii").read(128).strip()
        return int(raw)
    except (OSError, ValueError):
        return None

pids = []
try:
    pids = [name for name in os.listdir("/proc") if name.isdigit()]
except OSError:
    pass
processes = threads = descriptors = 0
for pid in pids:
    try:
        threads += len([name for name in os.listdir(f"/proc/{pid}/task") if name.isdigit()])
        descriptors += len(os.listdir(f"/proc/{pid}/fd"))
        processes += 1
    except OSError:
        continue
print(json.dumps({
    "memory_current_bytes": number("/sys/fs/cgroup/memory.current"),
    "memory_peak_bytes": number("/sys/fs/cgroup/memory.peak"),
    "pids_current": number("/sys/fs/cgroup/pids.current"),
    "process_count": processes,
    "thread_count": threads,
    "file_descriptor_count": descriptors,
}, sort_keys=True, separators=(",", ":")))
"""


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    elapsed_seconds: float
    memory_current_bytes: int
    memory_peak_bytes: int
    pids_current: int
    process_count: int
    thread_count: int
    file_descriptor_count: int


class DockerProcCollector:
    """Collect allowlisted container-wide counters without reading argv or env."""

    def __init__(self, container_name: str, *, docker_binary: str = "docker") -> None:
        if _CONTAINER_NAME.fullmatch(container_name) is None:
            raise ValueError("container name has an invalid Docker identifier shape")
        self.container_name = container_name
        self.docker_binary = docker_binary

    def collect(self, *, elapsed_seconds: float) -> RuntimeSnapshot:
        try:
            completed = subprocess.run(
                [
                    self.docker_binary,
                    "exec",
                    self.container_name,
                    "python",
                    "-c",
                    _CONTAINER_PROBE,
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError as error:
            raise RuntimeError("docker executable is unavailable") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("container runtime probe failed") from error
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            raise RuntimeError("container runtime probe failed")
        try:
            payload = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("container runtime probe returned invalid JSON") from error
        return runtime_snapshot_from_payload(payload, elapsed_seconds=elapsed_seconds)


def runtime_snapshot_from_payload(
    payload: object,
    *,
    elapsed_seconds: float,
) -> RuntimeSnapshot:
    if not isinstance(payload, dict) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("runtime snapshot payload is invalid")
    expected = {
        "memory_current_bytes",
        "memory_peak_bytes",
        "pids_current",
        "process_count",
        "thread_count",
        "file_descriptor_count",
    }
    if set(payload) != expected:
        raise ValueError("runtime snapshot payload has an invalid field set")
    values: dict[str, int] = {}
    for name in expected:
        value = payload[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("runtime snapshot counters must be non-negative integers")
        values[name] = value
    return RuntimeSnapshot(elapsed_seconds=round(elapsed_seconds, 6), **values)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _slope(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def assess_runtime_plateau(
    snapshots: Sequence[RuntimeSnapshot],
    *,
    memory_growth_tolerance_bytes: int = 16 * 1024 * 1024,
    memory_slope_tolerance_bytes_per_second: float = 64 * 1024,
    fd_growth_tolerance: int = 4,
    thread_growth_tolerance: int = 2,
    pid_growth_tolerance: int = 2,
) -> dict[str, Any]:
    if len(snapshots) < 4:
        return {
            "sample_count": len(snapshots),
            "passed": False,
            "reason": "at_least_four_post_warmup_samples_required",
        }
    ordered = sorted(snapshots, key=lambda item: item.elapsed_seconds)
    window = max(1, len(ordered) // 4)
    first = ordered[:window]
    last = ordered[-window:]
    tail = ordered[len(ordered) // 2 :]

    def median_delta(attribute: str) -> float:
        return _median([float(getattr(item, attribute)) for item in last]) - _median(
            [float(getattr(item, attribute)) for item in first]
        )

    memory_delta = median_delta("memory_current_bytes")
    memory_slope = _slope(
        [(item.elapsed_seconds, float(item.memory_current_bytes)) for item in tail]
    )
    fd_delta = median_delta("file_descriptor_count")
    thread_delta = median_delta("thread_count")
    pid_delta = median_delta("pids_current")
    checks = {
        "memory_growth_within_tolerance": memory_delta <= memory_growth_tolerance_bytes,
        "memory_slope_within_tolerance": memory_slope <= memory_slope_tolerance_bytes_per_second,
        "fd_growth_within_tolerance": fd_delta <= fd_growth_tolerance,
        "thread_growth_within_tolerance": thread_delta <= thread_growth_tolerance,
        "pid_growth_within_tolerance": pid_delta <= pid_growth_tolerance,
    }
    return {
        "sample_count": len(ordered),
        "memory_median_growth_bytes": round(memory_delta, 3),
        "memory_tail_slope_bytes_per_second": round(memory_slope, 3),
        "fd_median_growth": round(fd_delta, 3),
        "thread_median_growth": round(thread_delta, 3),
        "pid_median_growth": round(pid_delta, 3),
        "tolerances": {
            "memory_growth_bytes": memory_growth_tolerance_bytes,
            "memory_slope_bytes_per_second": memory_slope_tolerance_bytes_per_second,
            "fd_growth": fd_growth_tolerance,
            "thread_growth": thread_growth_tolerance,
            "pid_growth": pid_growth_tolerance,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


class _SoakResults:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.passed = 0
        self.latencies: deque[float] = deque(maxlen=_MAX_LATENCY_SAMPLES)
        self.payload_bytes: deque[float] = deque(maxlen=_MAX_LATENCY_SAMPLES)
        self.evidence_counts: deque[float] = deque(maxlen=_MAX_LATENCY_SAMPLES)
        self.citation_counts: deque[float] = deque(maxlen=_MAX_LATENCY_SAMPLES)
        self.errors: Counter[str] = Counter()
        self.late_completions_excluded = 0
        self._stability_baseline: dict[str, int | str] | None = None
        self._stability_observations = 0
        self._stability_mismatches: Counter[str] = Counter()

    def record(self, *, exchange: Any | None, result: Any | None, error: str | None) -> None:
        with self.lock:
            self.requests += 1
            if error is not None:
                self.errors[error] += 1
                return
            assert exchange is not None and result is not None
            self.latencies.append(float(exchange.latency_ms))
            self.payload_bytes.append(float(exchange.response_bytes))
            citations, evidence = _evidence_counts(exchange.body)
            self.citation_counts.append(float(citations))
            self.evidence_counts.append(float(evidence))
            if result.passed and result.control_code is None:
                self.passed += 1
                fingerprint = result.response_sha256
                if isinstance(fingerprint, str):
                    observed: dict[str, int | str] = {
                        "canonical_response_sha256": fingerprint,
                        "payload_bytes": exchange.response_bytes,
                        "citation_count": citations,
                        "evidence_ref_count": evidence,
                    }
                    self._stability_observations += 1
                    if self._stability_baseline is None:
                        self._stability_baseline = observed
                    else:
                        for field, value in observed.items():
                            if value != self._stability_baseline[field]:
                                self._stability_mismatches[field] += 1
            else:
                for violation in (*result.contract_violations, *result.semantic_violations):
                    self.errors[violation] += 1
                if result.control_code is not None:
                    self.errors[f"control:{result.control_code}"] += 1

    def record_late_completion(self) -> None:
        with self.lock:
            self.late_completions_excluded += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            baseline = dict(self._stability_baseline or {})
            checks = {
                "response_fingerprint_stable": self._stability_mismatches[
                    "canonical_response_sha256"
                ]
                == 0,
                "payload_bytes_stable": self._stability_mismatches["payload_bytes"] == 0,
                "citation_count_stable": self._stability_mismatches["citation_count"] == 0,
                "evidence_ref_count_stable": self._stability_mismatches["evidence_ref_count"] == 0,
                "baseline_observed": self._stability_observations > 0,
            }
            return {
                "requests": self.requests,
                "passed": self.passed,
                "failed": self.requests - self.passed,
                "latency_ms": numeric_summary(tuple(self.latencies)),
                "payload_bytes": numeric_summary(tuple(self.payload_bytes)),
                "citation_count": numeric_summary(tuple(self.citation_counts)),
                "evidence_ref_count": numeric_summary(tuple(self.evidence_counts)),
                "error_counts": dict(sorted(self.errors.items())),
                "late_completions_excluded": self.late_completions_excluded,
                "deterministic_stability": {
                    "method": "first_successful_measured_response_baseline",
                    "observations": self._stability_observations,
                    "baseline": baseline,
                    "mismatch_counts": dict(sorted(self._stability_mismatches.items())),
                    "checks": checks,
                    "passed": all(checks.values()),
                },
            }


def _evidence_counts(body: Mapping[str, Any]) -> tuple[int, int]:
    raw = body.get("retrieved_context")
    if not isinstance(raw, str):
        return 0, 0
    try:
        context = json.loads(raw)
    except json.JSONDecodeError:
        return 0, 0
    citations = context.get("citations") if isinstance(context, dict) else None
    if not isinstance(citations, list):
        return 0, 0
    references = {
        reference
        for citation in citations
        if isinstance(citation, dict) and isinstance(citation.get("evidence_refs"), list)
        for reference in citation["evidence_refs"]
        if isinstance(reference, str)
    }
    return len(citations), len(references)


def _completion_belongs_to_measurement(
    *,
    started_while_measuring: bool,
    completed_at: float,
    measurement_deadline: float | None,
) -> bool:
    return (
        started_while_measuring
        and measurement_deadline is not None
        and completed_at <= measurement_deadline
    )


def assess_container_runtime(
    before: ContainerSnapshot,
    after: ContainerSnapshot,
) -> dict[str, Any]:
    checks = {
        "before_inspect_complete": before.configured is True and before.collection_error is None,
        "after_inspect_complete": after.configured is True and after.collection_error is None,
        "container_identity_stable": before.container_id is not None
        and before.container_id == after.container_id
        and before.image_id is not None
        and before.image_id == after.image_id
        and before.started_at is not None
        and before.started_at == after.started_at,
        "running_after_load": after.running is True and after.status == "running",
        "not_restarting_after_load": after.restarting is False,
        "not_dead_after_load": after.dead is False,
        "not_oom_killed_after_load": after.oom_killed is False,
        "no_runtime_error_after_load": after.runtime_error_present is False,
        "docker_health_not_unhealthy": after.health_status != "unhealthy",
    }
    return {"checks": checks, "passed": all(checks.values())}


class DeterministicApiSoak:
    def __init__(
        self,
        *,
        base_url: str,
        container_name: str,
        question_id: str,
        question: str,
        expectation: OfficialExpectation,
        concurrency: int,
        warmup_seconds: float,
        duration_seconds: float,
        sample_interval_seconds: float,
        request_timeout_seconds: float,
        http_client: HttpClient | None = None,
        runtime_collector: DockerProcCollector | None = None,
        container_collector: ContainerCollector | None = None,
        expected_fund_execution_policy: str = "locked",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("concurrency must be in [1, 8]")
        if not 0 <= warmup_seconds <= 86_400 or not 1 <= duration_seconds <= 86_400:
            raise ValueError("soak durations are outside the bounded range")
        if not 0.1 <= sample_interval_seconds <= duration_seconds:
            raise ValueError("sample interval must be within the measurement duration")
        if not 0 < request_timeout_seconds <= 300:
            raise ValueError("request timeout must be in (0, 300]")
        if not question_id.strip() or len(question_id) > 128:
            raise ValueError("question ID must contain 1..128 characters")
        if not question.strip() or len(question) > 2000:
            raise ValueError("question must contain 1..2000 characters")
        if not container_name.startswith("finance-perf-"):
            raise ValueError("soak container name must use the finance-perf- namespace")
        normalized_base_url = require_isolated_base_url(base_url)
        if int(normalized_base_url.rsplit(":", 1)[1]) in _SHARED_PORTS:
            raise ValueError("soak target must not use shared ports 18001 or 18002")
        if expected_fund_execution_policy not in {"locked", "public_fund_v1_approved"}:
            raise ValueError("fund execution policy must be locked or public_fund_v1_approved")
        self.base_url = normalized_base_url
        self.container_name = container_name
        self.question_id = question_id
        self.question = question
        self.expectation = expectation
        self.concurrency = concurrency
        self.warmup_seconds = warmup_seconds
        self.duration_seconds = duration_seconds
        self.sample_interval_seconds = sample_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.http_client = http_client or StdlibHttpClient()
        self.runtime_collector = runtime_collector or DockerProcCollector(container_name)
        self.container_collector = container_collector or DockerInspectCollector(container_name)
        self.expected_fund_execution_policy = expected_fund_execution_policy
        self.monotonic = monotonic

    def _post_load_health_probe(self) -> dict[str, Any]:
        try:
            exchange = self.http_client.request(
                f"{self.base_url}/health",
                timeout_seconds=self.request_timeout_seconds,
            )
        except BenchmarkTransportError as error:
            return {
                "passed": False,
                "http_status": None,
                "service_status": None,
                "audit_status": None,
                "request_latency_ms": None,
                "response_bytes": None,
                "response_sha256": None,
                "violations": [f"health_transport:{error.kind}"],
            }
        except Exception:  # noqa: BLE001 - return a bounded failure without leaking details.
            return {
                "passed": False,
                "http_status": None,
                "service_status": None,
                "audit_status": None,
                "request_latency_ms": None,
                "response_bytes": None,
                "response_sha256": None,
                "violations": ["health_probe_internal_error"],
            }
        violations = validate_health(
            exchange.http_status,
            exchange.body,
            expected_fund_execution_policy=self.expected_fund_execution_policy,
        )
        service_status = exchange.body.get("status")
        audit_status = exchange.body.get("audit_status")
        if audit_status not in {"disabled", "ok"}:
            violations.append("audit status must be disabled or ok")
        return {
            "passed": not violations,
            "http_status": exchange.http_status,
            "service_status": (service_status if service_status in {"ok", "degraded"} else None),
            "audit_status": (
                audit_status if audit_status in {"disabled", "ok", "degraded"} else None
            ),
            "request_latency_ms": round(exchange.latency_ms, 3),
            "response_bytes": exchange.response_bytes,
            "response_sha256": canonical_json_sha256(exchange.body),
            "violations": violations,
        }

    def run(self) -> dict[str, Any]:
        results = _SoakResults()
        stop = threading.Event()
        measuring = threading.Event()
        measurement_deadline: float | None = None
        url = f"{self.base_url}/answer?" + urlencode(
            {"question_id": self.question_id, "question": self.question}
        )

        def worker() -> None:
            def record_completion(
                *,
                include: bool,
                deadline: float | None,
                exchange: Any | None,
                result: Any | None,
                error: str | None,
            ) -> None:
                completed_at = self.monotonic()
                if _completion_belongs_to_measurement(
                    started_while_measuring=include,
                    completed_at=completed_at,
                    measurement_deadline=deadline,
                ):
                    results.record(exchange=exchange, result=result, error=error)
                elif include:
                    results.record_late_completion()

            while not stop.is_set():
                include = measuring.is_set()
                deadline_for_request = measurement_deadline

                try:
                    exchange = self.http_client.request(
                        url,
                        timeout_seconds=self.request_timeout_seconds,
                    )
                except BenchmarkTransportError as error:
                    record_completion(
                        include=include,
                        deadline=deadline_for_request,
                        exchange=None,
                        result=None,
                        error=f"transport:{error.kind}",
                    )
                    continue
                except Exception:  # noqa: BLE001 - keep worker failures observable in the report.
                    record_completion(
                        include=include,
                        deadline=deadline_for_request,
                        exchange=None,
                        result=None,
                        error="worker_internal_error",
                    )
                    continue
                try:
                    evaluated = evaluate_official_exchange(
                        exchange,
                        question_id=self.question_id,
                        question=self.question,
                        expectation=self.expectation,
                        accepted_control_codes=frozenset(),
                    )
                except Exception:  # noqa: BLE001 - isolate a malformed exchange to this worker.
                    record_completion(
                        include=include,
                        deadline=deadline_for_request,
                        exchange=None,
                        result=None,
                        error="worker_internal_error",
                    )
                    continue
                record_completion(
                    include=include,
                    deadline=deadline_for_request,
                    exchange=exchange,
                    result=evaluated,
                    error=None,
                )

        threads = [
            threading.Thread(target=worker, name=f"deterministic-soak-{index}", daemon=True)
            for index in range(self.concurrency)
        ]
        container_before = self.container_collector.collect()
        started = self.monotonic()
        for thread in threads:
            thread.start()
        if self.warmup_seconds:
            stop.wait(self.warmup_seconds)
        measured_started = self.monotonic()
        deadline = measured_started + self.duration_seconds
        measurement_deadline = deadline
        measuring.set()
        snapshots: list[RuntimeSnapshot] = []
        next_sample = measured_started
        try:
            while not stop.is_set() and self.monotonic() < deadline:
                now = self.monotonic()
                if now >= next_sample:
                    snapshots.append(
                        self.runtime_collector.collect(
                            elapsed_seconds=now - measured_started,
                        )
                    )
                    next_sample += self.sample_interval_seconds
                stop.wait(min(0.1, max(0.0, deadline - self.monotonic())))
            measured_ended = self.monotonic()
            snapshots.append(
                self.runtime_collector.collect(
                    elapsed_seconds=measured_ended - measured_started,
                )
            )
        finally:
            measuring.clear()
            stop.set()
            for thread in threads:
                thread.join(self.request_timeout_seconds + 1)
        worker_threads_stopped = all(not thread.is_alive() for thread in threads)
        post_load_health = self._post_load_health_probe()
        container_after = self.container_collector.collect()
        load = results.snapshot()
        plateau = assess_runtime_plateau(snapshots)
        container_runtime = assess_container_runtime(container_before, container_after)
        runtime_passed = (
            load["requests"] > 0
            and load["failed"] == 0
            and load["deterministic_stability"]["passed"]
            and plateau["passed"]
            and post_load_health["passed"]
            and container_runtime["passed"]
            and worker_threads_stopped
        )
        return {
            "schema_version": "1.0",
            "suite_id": "deterministic-api-soak-v1",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "target": {
                "base_url_origin": self.base_url,
                "container_name_sha256": hashlib.sha256(
                    self.container_name.encode("utf-8")
                ).hexdigest(),
                "question_id_sha256": hashlib.sha256(self.question_id.encode("utf-8")).hexdigest(),
                "question_sha256": hashlib.sha256(self.question.encode("utf-8")).hexdigest(),
            },
            "configuration": {
                "concurrency": self.concurrency,
                "warmup_seconds": self.warmup_seconds,
                "duration_seconds": self.duration_seconds,
                "sample_interval_seconds": self.sample_interval_seconds,
                "request_timeout_seconds": self.request_timeout_seconds,
            },
            "elapsed_seconds": round(measured_ended - measured_started, 6),
            "load": load,
            "post_load_health": post_load_health,
            "runtime_samples": [asdict(item) for item in snapshots],
            "plateau": plateau,
            "container_identity_stable": container_runtime["checks"]["container_identity_stable"],
            "container_runtime": container_runtime,
            "worker_threads_stopped": worker_threads_stopped,
            "queue_integrity": {
                "validated": False,
                "required_followup": (
                    "stop the isolated container, then require complete START/terminal and "
                    "contiguous event_sequence coverage with the audit validation CLI"
                ),
            },
            "runtime_passed": runtime_passed,
            "complete_success_gate_passed": False,
            "started_to_measurement_end_seconds": round(measured_ended - started, 6),
        }


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded deterministic HTTP soak against one isolated container."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--warmup-seconds", type=float, default=60)
    parser.add_argument("--duration-seconds", type=float, default=900)
    parser.add_argument("--sample-interval-seconds", type=float, default=5)
    parser.add_argument("--request-timeout-seconds", type=float, default=60)
    parser.add_argument("--question-id", default="deterministic-soak-bond-001")
    parser.add_argument(
        "--question",
        default="매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.",
    )
    parser.add_argument("--expected-status", default="success")
    parser.add_argument("--expected-intent", default="search")
    parser.add_argument("--expected-family", default="bond")
    parser.add_argument(
        "--expected-fund-execution-policy",
        choices=("locked", "public_fund_v1_approved"),
        default="locked",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = DeterministicApiSoak(
            base_url=arguments.base_url,
            container_name=arguments.container_name,
            question_id=arguments.question_id,
            question=arguments.question,
            expectation=OfficialExpectation(
                status=arguments.expected_status,
                intent=arguments.expected_intent,
                product_families=(arguments.expected_family,),
            ),
            concurrency=arguments.concurrency,
            warmup_seconds=arguments.warmup_seconds,
            duration_seconds=arguments.duration_seconds,
            sample_interval_seconds=arguments.sample_interval_seconds,
            request_timeout_seconds=arguments.request_timeout_seconds,
            expected_fund_execution_policy=arguments.expected_fund_execution_policy,
        ).run()
        _write_new(arguments.output, report)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"deterministic API soak failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "suite_id": report["suite_id"],
                "output": str(arguments.output),
                "runtime_passed": report["runtime_passed"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["runtime_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
