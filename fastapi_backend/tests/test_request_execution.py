from __future__ import annotations

import asyncio
from threading import Event
from time import monotonic, sleep

import pytest

from app import request_execution as execution_module
from app.request_execution import (
    IdempotentRequestCoordinator,
    RequestExecutionDisposition,
    RequestIdentityConflictError,
    execute_bounded_request,
    request_execution_stats,
    wait_for_request_workers,
)


def _wait_for_active(expected: int, timeout_seconds: float = 1.0) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if request_execution_stats().active == expected:
            return
        sleep(0.001)
    assert request_execution_stats().active == expected


def test_operation_exception_releases_admission_capacity() -> None:
    _wait_for_active(0)

    def fail() -> None:
        raise RuntimeError("operation failed")

    with pytest.raises(RuntimeError, match="operation failed"):
        asyncio.run(
            execute_bounded_request(
                fail,
                timeout_seconds=1,
                max_inflight=1,
            )
        )

    _wait_for_active(0)


def test_executor_submission_failure_releases_admission_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wait_for_active(0)

    class RejectingExecutor:
        @staticmethod
        def submit(*_args: object, **_kwargs: object):
            raise RuntimeError("submit rejected")

    monkeypatch.setattr(execution_module, "_PROCESS_EXECUTOR", RejectingExecutor())

    with pytest.raises(RuntimeError, match="submit rejected"):
        asyncio.run(
            execute_bounded_request(
                lambda: None,
                timeout_seconds=1,
                max_inflight=1,
            )
        )

    assert request_execution_stats().active == 0


def test_async_caller_cancellation_holds_permit_until_worker_cleanup() -> None:
    _wait_for_active(0)
    started = Event()
    release = Event()
    cleaned = Event()

    def blocking_operation() -> None:
        started.set()
        try:
            assert release.wait(1)
        finally:
            cleaned.set()

    async def scenario() -> None:
        task = asyncio.create_task(
            execute_bounded_request(
                blocking_operation,
                timeout_seconds=5,
                max_inflight=1,
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert request_execution_stats().active == 1
        release.set()
        assert await asyncio.to_thread(cleaned.wait, 1)

    asyncio.run(scenario())
    _wait_for_active(0)


def test_shutdown_wait_tracks_timed_out_worker_until_cleanup() -> None:
    _wait_for_active(0)
    started = Event()
    release = Event()

    def blocking_operation() -> None:
        started.set()
        assert release.wait(1)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await execute_bounded_request(
                blocking_operation,
                timeout_seconds=0.01,
                max_inflight=1,
            )

    asyncio.run(scenario())
    assert started.is_set()
    assert wait_for_request_workers(timeout_seconds=0.001) is False
    release.set()
    assert wait_for_request_workers(timeout_seconds=1) is True
    _wait_for_active(0)


def test_idempotent_concurrent_callers_share_one_worker() -> None:
    _wait_for_active(0)
    coordinator = IdempotentRequestCoordinator()
    started = Event()
    release = Event()
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        assert release.wait(1)
        return "shared-result"

    async def scenario() -> None:
        first = asyncio.create_task(
            coordinator.execute(
                operation,
                request_key="Q-SINGLE-FLIGHT",
                request_input="동일한 질문",
                timeout_seconds=1,
                max_inflight=1,
                cache_result=lambda _result: True,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(
            coordinator.execute(
                operation,
                request_key="Q-SINGLE-FLIGHT",
                request_input="동일한 질문",
                timeout_seconds=1,
                max_inflight=1,
                cache_result=lambda _result: True,
            )
        )
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.value == second_result.value == "shared-result"
        assert first_result.disposition is RequestExecutionDisposition.EXECUTED
        assert second_result.disposition is RequestExecutionDisposition.JOINED

    asyncio.run(scenario())
    assert call_count == 1
    _wait_for_active(0)


def test_idempotent_safe_completion_is_replayed_without_execution() -> None:
    coordinator = IdempotentRequestCoordinator()
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        return "safe-result"

    async def scenario() -> None:
        first = await coordinator.execute(
            operation,
            request_key="Q-REPLAY",
            request_input="재전송 질문",
            timeout_seconds=1,
            max_inflight=1,
            cache_result=lambda _result: True,
        )
        second = await coordinator.execute(
            operation,
            request_key="Q-REPLAY",
            request_input="재전송 질문",
            timeout_seconds=1,
            max_inflight=1,
            cache_result=lambda _result: True,
        )
        assert first.disposition is RequestExecutionDisposition.EXECUTED
        assert second.disposition is RequestExecutionDisposition.REPLAYED

    asyncio.run(scenario())
    assert call_count == 1


def test_idempotent_retryable_completion_is_not_replayed() -> None:
    coordinator = IdempotentRequestCoordinator()
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        return "transient-failure"

    async def scenario() -> None:
        first = await coordinator.execute(
            operation,
            request_key="Q-RETRY",
            request_input="재시도 질문",
            timeout_seconds=1,
            max_inflight=1,
            cache_result=lambda _result: False,
        )
        second = await coordinator.execute(
            operation,
            request_key="Q-RETRY",
            request_input="재시도 질문",
            timeout_seconds=1,
            max_inflight=1,
            cache_result=lambda _result: False,
        )
        assert first.disposition is RequestExecutionDisposition.EXECUTED
        assert second.disposition is RequestExecutionDisposition.EXECUTED

    asyncio.run(scenario())
    assert call_count == 2


def test_idempotent_request_key_cannot_be_reused_for_different_input() -> None:
    coordinator = IdempotentRequestCoordinator()
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        return "safe-result"

    async def scenario() -> None:
        await coordinator.execute(
            operation,
            request_key="Q-CONFLICT",
            request_input="첫 번째 질문",
            timeout_seconds=1,
            max_inflight=1,
            cache_result=lambda _result: True,
        )
        with pytest.raises(RequestIdentityConflictError):
            await coordinator.execute(
                operation,
                request_key="Q-CONFLICT",
                request_input="다른 질문",
                timeout_seconds=1,
                max_inflight=1,
                cache_result=lambda _result: True,
            )

    asyncio.run(scenario())
    assert call_count == 1
