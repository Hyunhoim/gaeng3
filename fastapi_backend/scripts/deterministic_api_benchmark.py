from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from scripts.smoke import (
    OFFICIAL_CASES,
    smoke_cases,
    validate_answer,
    validate_health,
    validate_official_answer,
)

_OFFICIAL_FIELDS = {
    "question_id",
    "question",
    "retrieved_context",
    "think_trace",
    "answer",
}
_SAFE_ADMISSION_CONTROL_CODES = {"request_overloaded", "request_timeout"}
_FORBIDDEN_PUBLIC_FRAGMENTS = (
    "/home/",
    "system prompt:",
    "api_key",
    "authorization: bearer",
    "select * from",
)
_MEMORY_FILES = ("memory.current", "memory.peak", "memory.max", "memory.events")
_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpExchange:
    http_status: int
    body: dict[str, Any]
    response_bytes: int
    latency_ms: float


class BenchmarkTransportError(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class HttpClient(Protocol):
    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: Literal["GET", "POST"] = "GET",
        payload: Mapping[str, str] | None = None,
    ) -> HttpExchange: ...


class StdlibHttpClient:
    """Small dependency-free client that never stores raw response bodies in reports."""

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        method: Literal["GET", "POST"] = "GET",
        payload: Mapping[str, str] | None = None,
    ) -> HttpExchange:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status = response.status
                raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            raw = error.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        except TimeoutError as error:
            raise BenchmarkTransportError("client_timeout") from error
        except URLError as error:
            reason = error.reason
            kind = (
                "client_timeout"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "connect"
            )
            raise BenchmarkTransportError(kind) from error
        except OSError as error:
            raise BenchmarkTransportError("io") from error
        if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
            raise BenchmarkTransportError("response_oversized")
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BenchmarkTransportError("invalid_json") from error
        if not isinstance(body, dict):
            raise BenchmarkTransportError("non_object_json")
        return HttpExchange(
            http_status=status,
            body=body,
            response_bytes=len(raw),
            latency_ms=latency_ms,
        )


@dataclass(frozen=True, slots=True)
class OfficialExpectation:
    status: str
    intent: str
    product_families: tuple[str, ...]
    answer_mode: str = "deterministic"


@dataclass(frozen=True, slots=True)
class RequestResult:
    http_status: int | None
    response_bytes: int | None
    latency_ms: float | None
    response_sha256: str | None
    status: str | None
    control_code: str | None
    contract_violations: tuple[str, ...]
    semantic_violations: tuple[str, ...]
    transport_error: str | None

    @property
    def passed(self) -> bool:
        return (
            self.transport_error is None
            and not self.contract_violations
            and not self.semantic_violations
        )


@dataclass(frozen=True, slots=True)
class CgroupMemorySnapshot:
    configured: bool
    collected_at_utc: str
    current_bytes: int | None
    peak_bytes: int | None
    max_bytes: int | Literal["max"] | None
    events: dict[str, int]
    collection_errors: tuple[str, ...]


class MemoryCollector(Protocol):
    def collect(self) -> CgroupMemorySnapshot: ...


class NullMemoryCollector:
    def collect(self) -> CgroupMemorySnapshot:
        return CgroupMemorySnapshot(
            configured=False,
            collected_at_utc=datetime.now(UTC).isoformat(),
            current_bytes=None,
            peak_bytes=None,
            max_bytes=None,
            events={},
            collection_errors=(),
        )


class CgroupV2MemoryCollector:
    """Read only the bounded cgroup-v2 memory files explicitly needed by the report."""

    def __init__(self, cgroup_path: Path) -> None:
        self._path = cgroup_path

    @staticmethod
    def _read_bounded(path: Path) -> str:
        with path.open("r", encoding="utf-8") as stream:
            value = stream.read(65_537)
        if len(value) > 65_536:
            raise ValueError("oversized")
        return value.strip()

    def collect(self) -> CgroupMemorySnapshot:
        values: dict[str, str] = {}
        errors: list[str] = []
        for name in _MEMORY_FILES:
            try:
                values[name] = self._read_bounded(self._path / name)
            except FileNotFoundError:
                errors.append(f"{name}:missing")
            except PermissionError:
                errors.append(f"{name}:permission_denied")
            except (OSError, UnicodeError, ValueError):
                errors.append(f"{name}:unreadable")

        def parse_nonnegative(name: str) -> int | None:
            raw = values.get(name)
            if raw is None:
                return None
            try:
                parsed = int(raw)
            except ValueError:
                errors.append(f"{name}:invalid")
                return None
            if parsed < 0:
                errors.append(f"{name}:invalid")
                return None
            return parsed

        max_raw = values.get("memory.max")
        max_bytes: int | Literal["max"] | None
        if max_raw == "max":
            max_bytes = "max"
        else:
            max_bytes = parse_nonnegative("memory.max")

        events: dict[str, int] = {}
        raw_events = values.get("memory.events")
        if raw_events is not None:
            for line in raw_events.splitlines():
                parts = line.split()
                if len(parts) != 2 or not parts[0].replace("_", "").isalnum() or len(parts[0]) > 64:
                    errors.append("memory.events:invalid")
                    events = {}
                    break
                try:
                    count = int(parts[1])
                except ValueError:
                    errors.append("memory.events:invalid")
                    events = {}
                    break
                if count < 0:
                    errors.append("memory.events:invalid")
                    events = {}
                    break
                events[parts[0]] = count
        return CgroupMemorySnapshot(
            configured=True,
            collected_at_utc=datetime.now(UTC).isoformat(),
            current_bytes=parse_nonnegative("memory.current"),
            peak_bytes=parse_nonnegative("memory.peak"),
            max_bytes=max_bytes,
            events=dict(sorted(events.items())),
            collection_errors=tuple(sorted(set(errors))),
        )


@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    configured: bool
    collected_at_utc: str
    status: str | None
    running: bool | None
    restarting: bool | None
    dead: bool | None
    exit_code: int | None
    oom_killed: bool | None
    runtime_error_present: bool | None
    started_at: str | None
    finished_at: str | None
    health_status: str | None
    container_id: str | None
    image_id: str | None
    collection_error: str | None


class ContainerCollector(Protocol):
    def collect(self) -> ContainerSnapshot: ...


class NullContainerCollector:
    def collect(self) -> ContainerSnapshot:
        return ContainerSnapshot(
            configured=False,
            collected_at_utc=datetime.now(UTC).isoformat(),
            status=None,
            running=None,
            restarting=None,
            dead=None,
            exit_code=None,
            oom_killed=None,
            runtime_error_present=None,
            started_at=None,
            finished_at=None,
            health_status=None,
            container_id=None,
            image_id=None,
            collection_error=None,
        )


def container_snapshot_from_inspect(payload: object) -> ContainerSnapshot:
    now = datetime.now(UTC).isoformat()
    if isinstance(payload, list):
        payload = payload[0] if len(payload) == 1 else None
    if not isinstance(payload, dict):
        return _container_collection_error("invalid_inspect_shape", collected_at=now)
    state = payload.get("State")
    if not isinstance(state, dict):
        return _container_collection_error("missing_container_state", collected_at=now)
    health = state.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    image_id = payload.get("Image")
    container_id = payload.get("Id")
    runtime_error = state.get("Error")
    return ContainerSnapshot(
        configured=True,
        collected_at_utc=now,
        status=state.get("Status") if isinstance(state.get("Status"), str) else None,
        running=state.get("Running") if isinstance(state.get("Running"), bool) else None,
        restarting=(state.get("Restarting") if isinstance(state.get("Restarting"), bool) else None),
        dead=state.get("Dead") if isinstance(state.get("Dead"), bool) else None,
        exit_code=state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None,
        oom_killed=(state.get("OOMKilled") if isinstance(state.get("OOMKilled"), bool) else None),
        runtime_error_present=(bool(runtime_error) if isinstance(runtime_error, str) else None),
        started_at=state.get("StartedAt") if isinstance(state.get("StartedAt"), str) else None,
        finished_at=(state.get("FinishedAt") if isinstance(state.get("FinishedAt"), str) else None),
        health_status=health_status if isinstance(health_status, str) else None,
        container_id=container_id if isinstance(container_id, str) else None,
        image_id=image_id if isinstance(image_id, str) else None,
        collection_error=None,
    )


def _container_collection_error(code: str, *, collected_at: str | None = None) -> ContainerSnapshot:
    return ContainerSnapshot(
        configured=True,
        collected_at_utc=collected_at or datetime.now(UTC).isoformat(),
        status=None,
        running=None,
        restarting=None,
        dead=None,
        exit_code=None,
        oom_killed=None,
        runtime_error_present=None,
        started_at=None,
        finished_at=None,
        health_status=None,
        container_id=None,
        image_id=None,
        collection_error=code,
    )


class DockerInspectCollector:
    def __init__(self, container_name: str, *, docker_binary: str = "docker") -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_name) is None:
            raise ValueError("container name has an invalid Docker identifier shape")
        self._container_name = container_name
        self._docker_binary = docker_binary

    def collect(self) -> ContainerSnapshot:
        try:
            completed = subprocess.run(
                [self._docker_binary, "inspect", "--type", "container", self._container_name],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError:
            return _container_collection_error("docker_not_found")
        except subprocess.TimeoutExpired:
            return _container_collection_error("docker_inspect_timeout")
        except OSError:
            return _container_collection_error("docker_inspect_failed")
        if completed.returncode != 0:
            return _container_collection_error("docker_inspect_failed")
        if len(completed.stdout) > 4 * 1024 * 1024:
            return _container_collection_error("docker_inspect_oversized")
        try:
            payload = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _container_collection_error("docker_inspect_invalid_json")
        return container_snapshot_from_inspect(payload)


class JsonFileContainerCollector:
    def __init__(self, path: Path) -> None:
        self._path = path

    def collect(self) -> ContainerSnapshot:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return _container_collection_error("inspect_file_missing")
        except OSError:
            return _container_collection_error("inspect_file_unreadable")
        if len(raw) > 4 * 1024 * 1024:
            return _container_collection_error("inspect_file_oversized")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _container_collection_error("inspect_file_invalid_json")
        return container_snapshot_from_inspect(payload)


def canonical_json_sha256(body: Mapping[str, Any]) -> str:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _numeric_summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "min": round(min(values), 3),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 3),
    }


def _decode_json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def evaluate_official_exchange(
    exchange: HttpExchange,
    *,
    question_id: str,
    question: str,
    expectation: OfficialExpectation,
    accepted_control_codes: frozenset[str],
) -> RequestResult:
    body = exchange.body
    contract: list[str] = []
    semantic: list[str] = []
    if exchange.http_status != 200:
        contract.append("official_http_status_not_200")
    if set(body) != _OFFICIAL_FIELDS:
        contract.append("official_fields_not_exact")
    if not all(isinstance(body.get(field), str) for field in _OFFICIAL_FIELDS):
        contract.append("official_fields_not_all_strings")
    if body.get("question_id") != question_id:
        contract.append("question_id_not_preserved")
    if body.get("question") != question:
        contract.append("question_not_preserved")
    context = _decode_json_object(body.get("retrieved_context"))
    trace = _decode_json_object(body.get("think_trace"))
    if context is None:
        contract.append("retrieved_context_not_json_object")
    if trace is None:
        contract.append("think_trace_not_json_object")
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        contract.append("answer_empty")
    serialized = json.dumps(body, ensure_ascii=False).casefold()
    if any(fragment in serialized for fragment in _FORBIDDEN_PUBLIC_FRAGMENTS):
        contract.append("forbidden_public_fragment")

    status = trace.get("status") if trace is not None else None
    control = trace.get("control_code") if trace is not None else None
    status = status if isinstance(status, str) else None
    control = control if isinstance(control, str) else None
    if control in _SAFE_ADMISSION_CONTROL_CODES:
        if control not in accepted_control_codes:
            semantic.append(f"unexpected_control:{control}")
        if status != "error":
            contract.append("admission_control_status_not_error")
        citations = context.get("citations") if context is not None else None
        if citations != []:
            contract.append("admission_control_contains_citations")
    else:
        if status != expectation.status:
            semantic.append("status_differs")
        actual_intent = trace.get("intent") if trace is not None else None
        if actual_intent != expectation.intent:
            semantic.append("intent_differs")
        actual_families = trace.get("product_families") if trace is not None else None
        if actual_families != list(expectation.product_families):
            semantic.append("product_families_differ")
        if trace is None or trace.get("answer_mode") != expectation.answer_mode:
            semantic.append("answer_mode_differs")
        if trace is None or trace.get("fallback_used") is not False:
            semantic.append("fallback_flag_differs")
        if expectation.status == "success":
            citations = context.get("citations") if context is not None else None
            if not isinstance(citations, list) or not citations:
                semantic.append("success_citations_missing")
    return RequestResult(
        http_status=exchange.http_status,
        response_bytes=exchange.response_bytes,
        latency_ms=round(exchange.latency_ms, 3),
        response_sha256=canonical_json_sha256(body),
        status=status,
        control_code=control,
        contract_violations=tuple(sorted(set(contract))),
        semantic_violations=tuple(sorted(set(semantic))),
        transport_error=None,
    )


def _transport_failure(kind: str) -> RequestResult:
    return RequestResult(
        http_status=None,
        response_bytes=None,
        latency_ms=None,
        response_sha256=None,
        status=None,
        control_code=None,
        contract_violations=(),
        semantic_violations=(),
        transport_error=kind,
    )


def _outcome_key(result: RequestResult) -> str:
    if result.transport_error is not None:
        return f"transport:{result.transport_error}"
    if result.control_code in _SAFE_ADMISSION_CONTROL_CODES:
        return f"control:{result.control_code}"
    return f"status:{result.status or 'unknown'}"


def _summarize_results(
    results: Sequence[RequestResult], *, elapsed_seconds: float
) -> dict[str, Any]:
    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
    body_sizes = [result.response_bytes for result in results if result.response_bytes is not None]
    violation_counts = Counter(
        violation
        for result in results
        for violation in (*result.contract_violations, *result.semantic_violations)
    )
    transport_errors = Counter(
        result.transport_error for result in results if result.transport_error is not None
    )
    status_counts = Counter(
        str(result.http_status) for result in results if result.http_status is not None
    )
    control_counts = Counter(
        result.control_code for result in results if result.control_code is not None
    )
    outcomes: dict[str, Any] = {}
    for key in sorted({_outcome_key(result) for result in results}):
        selected = [result for result in results if _outcome_key(result) == key]
        hashes = sorted(
            {result.response_sha256 for result in selected if result.response_sha256 is not None}
        )
        outcomes[key] = {
            "count": len(selected),
            "canonical_response_sha256": hashes,
            "deterministic": len(hashes) <= 1
            and all(result.transport_error is None for result in selected),
        }
    completed = len(results) - sum(transport_errors.values())
    deterministic = all(outcome["deterministic"] for outcome in outcomes.values())
    return {
        "requests": len(results),
        "completed_http": completed,
        "passed": sum(result.passed for result in results),
        "failed": sum(not result.passed for result in results),
        "contract_passed": sum(
            result.transport_error is None and not result.contract_violations for result in results
        ),
        "contract_failed": sum(
            result.transport_error is not None or bool(result.contract_violations)
            for result in results
        ),
        "semantic_passed": sum(
            result.transport_error is None and not result.semantic_violations for result in results
        ),
        "semantic_failed": sum(
            result.transport_error is not None or bool(result.semantic_violations)
            for result in results
        ),
        "rps": round(completed / elapsed_seconds, 3) if elapsed_seconds > 0 else 0.0,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "latency_ms": _numeric_summary([float(value) for value in latencies]),
        "body_bytes": {
            **_numeric_summary([float(value) for value in body_sizes]),
            "total": sum(body_sizes),
        },
        "http_status_counts": dict(sorted(status_counts.items())),
        "control_code_counts": dict(sorted(control_counts.items())),
        "transport_error_counts": dict(sorted(transport_errors.items())),
        "violation_counts": dict(sorted(violation_counts.items())),
        "outcomes": outcomes,
        "deterministic": deterministic,
    }


def memory_delta(before: CgroupMemorySnapshot, after: CgroupMemorySnapshot) -> dict[str, Any]:
    def difference(left: int | None, right: int | None) -> int | None:
        return right - left if left is not None and right is not None else None

    event_names = set(before.events) | set(after.events)
    return {
        "current_bytes_delta": difference(before.current_bytes, after.current_bytes),
        "peak_bytes_delta": difference(before.peak_bytes, after.peak_bytes),
        "event_deltas": {
            name: after.events.get(name, 0) - before.events.get(name, 0)
            for name in sorted(event_names)
        },
    }


def _memory_payload(snapshot: CgroupMemorySnapshot) -> dict[str, Any]:
    return asdict(snapshot)


def _container_payload(snapshot: ContainerSnapshot) -> dict[str, Any]:
    return asdict(snapshot)


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name: str
    concurrency: int
    requested_count: int
    accept_admission_controls: bool


class DeterministicApiBenchmark:
    def __init__(
        self,
        *,
        base_url: str,
        question_id: str,
        question: str,
        expectation: OfficialExpectation,
        request_timeout_seconds: float,
        ready_timeout_seconds: float,
        health_poll_interval_seconds: float,
        expected_fund_execution_policy: str,
        warm_requests: int,
        stress_requests: int,
        required_control_codes: frozenset[str] = frozenset(),
        cold_start_epoch_ms: float | None = None,
        run_smoke_suite: bool = True,
        require_runtime_metrics: bool = False,
        http_client: HttpClient | None = None,
        memory_collector: MemoryCollector | None = None,
        container_collector: ContainerCollector | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], None] = time.sleep,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < request_timeout_seconds <= 300:
            raise ValueError("request timeout must be in (0, 300]")
        if not 0 < ready_timeout_seconds <= 900:
            raise ValueError("ready timeout must be in (0, 900]")
        if not 0 < health_poll_interval_seconds <= ready_timeout_seconds:
            raise ValueError("health poll interval must be positive and within ready timeout")
        if not 1 <= warm_requests <= 10_000 or not 1 <= stress_requests <= 10_000:
            raise ValueError("phase request counts must be in [1, 10000]")
        if not required_control_codes <= _SAFE_ADMISSION_CONTROL_CODES:
            raise ValueError("unknown required admission control code")
        if not question_id.strip() or len(question_id) > 128:
            raise ValueError("benchmark question_id must contain 1..128 characters")
        if not question.strip() or len(question) > 2000:
            raise ValueError("benchmark question must contain 1..2000 characters")
        if expectation.answer_mode != "deterministic":
            raise ValueError("deterministic baseline requires deterministic answer mode")
        known_families = {"bond", "domestic_etp", "overseas_etp", "fund"}
        if (
            not expectation.product_families
            or not set(expectation.product_families) <= known_families
            or len(set(expectation.product_families)) != len(expectation.product_families)
        ):
            raise ValueError("expected product families must use the four approved families")
        if cold_start_epoch_ms is not None and (
            not math.isfinite(cold_start_epoch_ms) or cold_start_epoch_ms < 0
        ):
            raise ValueError("cold start epoch must be a finite non-negative value")
        self.base_url = normalize_base_url(base_url)
        self.question_id = question_id
        self.question = question
        self.expectation = expectation
        self.request_timeout_seconds = request_timeout_seconds
        self.ready_timeout_seconds = ready_timeout_seconds
        self.health_poll_interval_seconds = health_poll_interval_seconds
        self.expected_fund_execution_policy = expected_fund_execution_policy
        self.warm_requests = warm_requests
        self.stress_requests = stress_requests
        self.required_control_codes = required_control_codes
        self.cold_start_epoch_ms = cold_start_epoch_ms
        self.run_smoke_suite = run_smoke_suite
        self.require_runtime_metrics = require_runtime_metrics
        self.http_client = http_client or StdlibHttpClient()
        self.memory_collector = memory_collector or NullMemoryCollector()
        self.container_collector = container_collector or NullContainerCollector()
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._wall_time = wall_time

    def _health_probe(
        self,
        *,
        observed_start_epoch_ms: float | None = None,
        observed_start_origin: str | None = None,
    ) -> tuple[HttpExchange | None, list[str], dict[str, Any]]:
        invocation_monotonic = self._monotonic()
        invocation_epoch_ms = self._wall_time() * 1000
        origin_epoch_ms = (
            observed_start_epoch_ms if observed_start_epoch_ms is not None else invocation_epoch_ms
        )
        origin = observed_start_origin or "benchmark_invocation"
        deadline = invocation_monotonic + self.ready_timeout_seconds
        attempts = 0
        first_http_ms: float | None = None
        last_transport_error: str | None = None
        last_health_errors: list[str] = []
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                errors = last_health_errors or [
                    f"health_transport:{last_transport_error or 'unavailable'}"
                ]
                return (
                    None,
                    errors,
                    {
                        "origin": origin,
                        "attempts": attempts,
                        "time_to_first_http_ms": (
                            round(first_http_ms, 3) if first_http_ms is not None else None
                        ),
                        "time_to_ready_ms": None,
                        "last_transport_error": last_transport_error,
                    },
                )
            attempts += 1
            try:
                exchange = self.http_client.request(
                    f"{self.base_url}/health",
                    timeout_seconds=min(self.request_timeout_seconds, remaining),
                )
            except BenchmarkTransportError as error:
                last_transport_error = error.kind
            else:
                now_epoch_ms = self._wall_time() * 1000
                if first_http_ms is None:
                    first_http_ms = max(0.0, now_epoch_ms - origin_epoch_ms)
                last_health_errors = validate_health(
                    exchange.http_status,
                    exchange.body,
                    expected_fund_execution_policy=self.expected_fund_execution_policy,
                )
                if not last_health_errors:
                    ready_ms = max(0.0, now_epoch_ms - origin_epoch_ms)
                    return (
                        exchange,
                        [],
                        {
                            "origin": origin,
                            "attempts": attempts,
                            "time_to_first_http_ms": (
                                round(first_http_ms, 3) if first_http_ms is not None else None
                            ),
                            "time_to_ready_ms": round(ready_ms, 3),
                            "last_transport_error": last_transport_error,
                        },
                    )
            now = self._monotonic()
            if now >= deadline:
                errors = last_health_errors or [
                    f"health_transport:{last_transport_error or 'unavailable'}"
                ]
                return (
                    None,
                    errors,
                    {
                        "origin": origin,
                        "attempts": attempts,
                        "time_to_first_http_ms": (
                            round(first_http_ms, 3) if first_http_ms is not None else None
                        ),
                        "time_to_ready_ms": None,
                        "last_transport_error": last_transport_error,
                    },
                )
            self._sleeper(min(self.health_poll_interval_seconds, deadline - now))

    def _run_smoke(self) -> dict[str, Any]:
        if not self.run_smoke_suite:
            return {"enabled": False, "passed": None, "cases": [], "metrics": {}}
        results: list[dict[str, Any]] = []
        for case in smoke_cases(self.expected_fund_execution_policy):
            try:
                exchange = self.http_client.request(
                    f"{self.base_url}/answer",
                    timeout_seconds=self.request_timeout_seconds,
                    method="POST",
                    payload=case.payload(),
                )
            except BenchmarkTransportError as error:
                violations = [f"transport:{error.kind}"]
                exchange = None
            else:
                violations = validate_answer(case, exchange.http_status, exchange.body)
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": not violations,
                    "http_status": exchange.http_status if exchange is not None else None,
                    "response_bytes": exchange.response_bytes if exchange is not None else None,
                    "latency_ms": (round(exchange.latency_ms, 3) if exchange is not None else None),
                    "response_sha256": (
                        canonical_json_sha256(exchange.body) if exchange is not None else None
                    ),
                    "violations": violations,
                }
            )
        for case in OFFICIAL_CASES:
            try:
                exchange = self.http_client.request(
                    f"{self.base_url}/answer?{case.query_string()}",
                    timeout_seconds=self.request_timeout_seconds,
                )
            except BenchmarkTransportError as error:
                violations = [f"transport:{error.kind}"]
                exchange = None
            else:
                violations = validate_official_answer(
                    exchange.http_status,
                    exchange.body,
                    question_id=case.expected_question_id,
                    question=case.expected_question,
                    expected_control_code=case.expected_control_code,
                    forbidden_output_fragments=case.forbidden_output_fragments,
                )
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": not violations,
                    "http_status": exchange.http_status if exchange is not None else None,
                    "response_bytes": exchange.response_bytes if exchange is not None else None,
                    "latency_ms": (round(exchange.latency_ms, 3) if exchange is not None else None),
                    "response_sha256": (
                        canonical_json_sha256(exchange.body) if exchange is not None else None
                    ),
                    "violations": violations,
                }
            )
        return {
            "enabled": True,
            "passed": all(result["passed"] for result in results),
            "cases": results,
            "metrics": {
                "total": len(results),
                "passed": sum(result["passed"] for result in results),
                "failed": sum(not result["passed"] for result in results),
            },
        }

    def _post_load_health_probe(self) -> dict[str, Any]:
        """Run one bounded readiness probe after load without retaining its body."""

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

        violations = validate_health(
            exchange.http_status,
            exchange.body,
            expected_fund_execution_policy=self.expected_fund_execution_policy,
        )
        service_status = exchange.body.get("status")
        audit_status = exchange.body.get("audit_status")
        return {
            "passed": not violations,
            "http_status": exchange.http_status,
            "service_status": (
                service_status
                if isinstance(service_status, str) and service_status in {"ok", "degraded"}
                else None
            ),
            "audit_status": (
                audit_status
                if isinstance(audit_status, str) and audit_status in {"disabled", "ok", "degraded"}
                else None
            ),
            "request_latency_ms": round(exchange.latency_ms, 3),
            "response_bytes": exchange.response_bytes,
            "response_sha256": canonical_json_sha256(exchange.body),
            "violations": violations,
        }

    def _single_official_request(self, *, accepted_control_codes: frozenset[str]) -> RequestResult:
        url = f"{self.base_url}/answer?" + urlencode(
            {"question_id": self.question_id, "question": self.question}
        )
        try:
            exchange = self.http_client.request(
                url,
                timeout_seconds=self.request_timeout_seconds,
            )
        except BenchmarkTransportError as error:
            return _transport_failure(error.kind)
        return evaluate_official_exchange(
            exchange,
            question_id=self.question_id,
            question=self.question,
            expectation=self.expectation,
            accepted_control_codes=accepted_control_codes,
        )

    def _run_phase(self, spec: PhaseSpec) -> dict[str, Any]:
        actual_count = math.ceil(spec.requested_count / spec.concurrency) * spec.concurrency
        accepted_controls = (
            frozenset(_SAFE_ADMISSION_CONTROL_CODES)
            if spec.accept_admission_controls
            else frozenset()
        )
        before = self.memory_collector.collect()
        results: list[RequestResult] = []
        started = self._monotonic()
        with ThreadPoolExecutor(
            max_workers=spec.concurrency,
            thread_name_prefix=f"api-benchmark-{spec.name}",
        ) as executor:
            for _wave in range(actual_count // spec.concurrency):
                barrier = Barrier(spec.concurrency)

                def invoke(active_barrier: Barrier = barrier) -> RequestResult:
                    if spec.concurrency > 1:
                        active_barrier.wait(timeout=max(5.0, self.request_timeout_seconds))
                    return self._single_official_request(accepted_control_codes=accepted_controls)

                results.extend(
                    future.result()
                    for future in [executor.submit(invoke) for _ in range(spec.concurrency)]
                )
        elapsed = self._monotonic() - started
        after = self.memory_collector.collect()
        summary = _summarize_results(results, elapsed_seconds=elapsed)
        return {
            "name": spec.name,
            "kind": "admission_stress" if spec.accept_admission_controls else "warm",
            "concurrency": spec.concurrency,
            "requested_count": spec.requested_count,
            "actual_count": actual_count,
            "accepted_control_codes": sorted(accepted_controls),
            "summary": summary,
            "memory": {
                "before": _memory_payload(before),
                "after": _memory_payload(after),
                "delta": memory_delta(before, after),
            },
            "passed": summary["failed"] == 0 and summary["deterministic"],
        }

    def run(self) -> dict[str, Any]:
        generated_at = datetime.now(UTC)
        container_before = self.container_collector.collect()
        memory_before = self.memory_collector.collect()
        observed_start_epoch_ms = self.cold_start_epoch_ms
        observed_start_origin = (
            "supplied_container_start_epoch" if observed_start_epoch_ms is not None else None
        )
        if observed_start_epoch_ms is None and container_before.started_at is not None:
            observed_start_epoch_ms = _rfc3339_epoch_ms(container_before.started_at)
            if observed_start_epoch_ms is not None:
                observed_start_origin = "container_inspect_started_at"
        health_exchange, health_errors, cold = self._health_probe(
            observed_start_epoch_ms=observed_start_epoch_ms,
            observed_start_origin=observed_start_origin,
        )
        memory_ready = self.memory_collector.collect()
        if health_exchange is None:
            container_after = self.container_collector.collect()
            return {
                "schema_version": "1.0",
                "suite_id": "deterministic-api-baseline-v1",
                "generated_at_utc": generated_at.isoformat(),
                "target": self._target_metadata(),
                "cold_start": cold,
                "health_ready": {
                    "passed": False,
                    "http_status": None,
                    "request_latency_ms": None,
                    "response_bytes": None,
                    "response_sha256": None,
                    "violations": health_errors,
                },
                "post_load_health": None,
                "semantic_contract": None,
                "phases": [],
                "control_contract": self._control_summary([]),
                "memory": {
                    "before_health": _memory_payload(memory_before),
                    "after_health": _memory_payload(memory_ready),
                    "delta": memory_delta(memory_before, memory_ready),
                },
                "container": {
                    "before": _container_payload(container_before),
                    "after": _container_payload(container_after),
                },
                "runtime_metrics": {
                    "required": self.require_runtime_metrics,
                    "complete": False,
                    "passed": not self.require_runtime_metrics,
                },
                "passed": False,
            }
        smoke = self._run_smoke()
        phase_specs = (
            PhaseSpec("warm_c1", 1, self.warm_requests, False),
            PhaseSpec("warm_c2", 2, self.warm_requests, False),
            PhaseSpec("admission_c4", 4, self.stress_requests, True),
            PhaseSpec("admission_c8", 8, self.stress_requests, True),
        )
        phases = [self._run_phase(spec) for spec in phase_specs]
        post_load_health = self._post_load_health_probe()
        memory_after = self.memory_collector.collect()
        container_after = self.container_collector.collect()
        all_controls = [
            code
            for phase in phases
            for code, count in phase["summary"]["control_code_counts"].items()
            for _ in range(count)
        ]
        control_summary = self._control_summary(all_controls)
        container_identity_stable = container_before.configured is False or (
            container_before.container_id is not None
            and container_before.container_id == container_after.container_id
            and container_before.image_id is not None
            and container_before.image_id == container_after.image_id
            and container_before.started_at is not None
            and container_before.started_at == container_after.started_at
        )
        container_safe = (
            container_after.oom_killed is not True
            and container_after.dead is not True
            and container_after.restarting is not True
            and container_after.runtime_error_present is not True
            and container_after.collection_error is None
            and (container_after.configured is False or container_after.running is True)
            and container_after.health_status not in {"unhealthy"}
            and container_identity_stable
        )
        global_memory_delta = memory_delta(memory_before, memory_after)
        memory_observed = (
            memory_before.configured
            and memory_after.configured
            and not memory_before.collection_errors
            and not memory_after.collection_errors
        )
        memory_safe = (
            global_memory_delta["event_deltas"].get("oom_kill", 0) == 0
            and global_memory_delta["event_deltas"].get("oom", 0) == 0
        )
        container_observed = (
            container_before.configured
            and container_after.configured
            and container_before.collection_error is None
            and container_after.collection_error is None
        )
        runtime_metrics_complete = memory_observed and container_observed
        runtime_metrics_passed = runtime_metrics_complete or not self.require_runtime_metrics
        passed = (
            not health_errors
            and post_load_health["passed"]
            and (smoke["passed"] is not False)
            and all(phase["passed"] for phase in phases)
            and control_summary["passed"]
            and container_safe
            and memory_safe
            and runtime_metrics_passed
        )
        return {
            "schema_version": "1.0",
            "suite_id": "deterministic-api-baseline-v1",
            "generated_at_utc": generated_at.isoformat(),
            "target": self._target_metadata(),
            "cold_start": cold,
            "health_ready": {
                "passed": True,
                "http_status": health_exchange.http_status,
                "request_latency_ms": round(health_exchange.latency_ms, 3),
                "response_bytes": health_exchange.response_bytes,
                "response_sha256": canonical_json_sha256(health_exchange.body),
                "violations": [],
            },
            "post_load_health": post_load_health,
            "semantic_contract": smoke,
            "phases": phases,
            "control_contract": control_summary,
            "memory": {
                "before_health": _memory_payload(memory_before),
                "after_health": _memory_payload(memory_ready),
                "after_all": _memory_payload(memory_after),
                "health_delta": memory_delta(memory_before, memory_ready),
                "global_delta": global_memory_delta,
                "observed": memory_observed,
                "oom_event_free": memory_safe,
            },
            "container": {
                "before": _container_payload(container_before),
                "after": _container_payload(container_after),
                "observed": container_observed,
                "identity_stable": container_identity_stable,
                "safe": container_safe,
            },
            "runtime_metrics": {
                "required": self.require_runtime_metrics,
                "complete": runtime_metrics_complete,
                "passed": runtime_metrics_passed,
            },
            "interpretation_limits": [
                ("공개 smoke와 고정 질문의 단일 호스트 관측이며 external blind 점수가 아니다."),
                (
                    "cold_start_epoch_ms와 유효한 Docker StartedAt이 모두 없으면 "
                    "time_to_ready는 runner 시작 기준이다."
                ),
                ("request_timeout은 서버가 해당 제어 응답을 실제 반환한 실행에서만 관측된다."),
                ("cgroup_path 또는 container collector 미설정 값은 성능 실패가 아니라 미관측이다."),
                ("응답 본문은 저장하지 않고 canonical SHA-256과 계약·의미 판정만 저장한다."),
            ],
            "passed": passed,
        }

    def _target_metadata(self) -> dict[str, Any]:
        parsed = urlsplit(self.base_url)
        return {
            "base_url_origin": urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
            "base_path_sha256": hashlib.sha256(parsed.path.encode("utf-8")).hexdigest(),
            "question_sha256": hashlib.sha256(self.question.encode("utf-8")).hexdigest(),
            "question_id_sha256": hashlib.sha256(self.question_id.encode("utf-8")).hexdigest(),
            "expected_status": self.expectation.status,
            "expected_intent": self.expectation.intent,
            "expected_product_families": list(self.expectation.product_families),
            "expected_answer_mode": self.expectation.answer_mode,
            "expected_fund_execution_policy": self.expected_fund_execution_policy,
            "llm_expected": False,
            "dense_expected": False,
        }

    def _control_summary(self, controls: Sequence[str]) -> dict[str, Any]:
        counts = Counter(controls)
        missing = sorted(self.required_control_codes - set(counts))
        return {
            "safe_codes": sorted(_SAFE_ADMISSION_CONTROL_CODES),
            "observed_counts": dict(sorted(counts.items())),
            "required_codes": sorted(self.required_control_codes),
            "missing_required_codes": missing,
            "passed": not missing,
        }


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL credentials are forbidden")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL query and fragment are forbidden")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _rfc3339_epoch_ms(value: str) -> float | None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    epoch_ms = parsed.timestamp() * 1000
    return epoch_ms if epoch_ms >= 0 else None


def _write_new_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure an isolated deterministic FastAPI container over real HTTP."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--health-poll-interval-seconds", type=float, default=0.1)
    parser.add_argument("--warm-requests", type=int, default=20)
    parser.add_argument("--stress-requests", type=int, default=24)
    parser.add_argument("--question-id", default="deterministic-api-benchmark-bond-001")
    parser.add_argument(
        "--question",
        default="매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.",
    )
    parser.add_argument(
        "--expected-status",
        choices=("success", "not_found", "clarification", "unsupported"),
        default="success",
    )
    parser.add_argument(
        "--expected-intent",
        choices=("search", "detail", "compare", "aggregate", "explain", "clarify", "unsupported"),
        default="search",
    )
    parser.add_argument(
        "--expected-family",
        action="append",
        choices=("bond", "domestic_etp", "overseas_etp", "fund"),
        default=[],
    )
    parser.add_argument(
        "--expected-answer-mode", choices=("deterministic",), default="deterministic"
    )
    parser.add_argument(
        "--expected-fund-execution-policy",
        choices=("locked", "public_fund_v1_approved"),
        default="locked",
    )
    parser.add_argument(
        "--required-control-code",
        action="append",
        choices=tuple(sorted(_SAFE_ADMISSION_CONTROL_CODES)),
        default=[],
    )
    parser.add_argument("--cold-start-epoch-ms", type=float)
    parser.add_argument("--cgroup-path", type=Path)
    container = parser.add_mutually_exclusive_group()
    container.add_argument("--container-name")
    container.add_argument("--container-inspect-json", type=Path)
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--skip-smoke-suite", action="store_true")
    parser.add_argument("--require-runtime-metrics", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    families = tuple(arguments.expected_family or ["bond"])
    memory_collector: MemoryCollector = (
        CgroupV2MemoryCollector(arguments.cgroup_path)
        if arguments.cgroup_path is not None
        else NullMemoryCollector()
    )
    if arguments.container_name is not None:
        container_collector: ContainerCollector = DockerInspectCollector(
            arguments.container_name,
            docker_binary=arguments.docker_binary,
        )
    elif arguments.container_inspect_json is not None:
        container_collector = JsonFileContainerCollector(arguments.container_inspect_json)
    else:
        container_collector = NullContainerCollector()
    try:
        benchmark = DeterministicApiBenchmark(
            base_url=arguments.base_url,
            question_id=arguments.question_id,
            question=arguments.question,
            expectation=OfficialExpectation(
                status=arguments.expected_status,
                intent=arguments.expected_intent,
                product_families=families,
                answer_mode=arguments.expected_answer_mode,
            ),
            request_timeout_seconds=arguments.request_timeout_seconds,
            ready_timeout_seconds=arguments.ready_timeout_seconds,
            health_poll_interval_seconds=arguments.health_poll_interval_seconds,
            expected_fund_execution_policy=arguments.expected_fund_execution_policy,
            warm_requests=arguments.warm_requests,
            stress_requests=arguments.stress_requests,
            required_control_codes=frozenset(arguments.required_control_code),
            cold_start_epoch_ms=arguments.cold_start_epoch_ms,
            run_smoke_suite=not arguments.skip_smoke_suite,
            require_runtime_metrics=arguments.require_runtime_metrics,
            memory_collector=memory_collector,
            container_collector=container_collector,
        )
        report = benchmark.run()
        _write_new_report(arguments.output, report)
    except (OSError, ValueError) as error:
        print(f"deterministic API benchmark failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "suite_id": report["suite_id"],
                "output": str(arguments.output),
                "passed": report["passed"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
