from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock

from finance_agent_core.deadline import RequestDeadline, bind_request_deadline

# Each accepted cross-family request can temporarily create up to four
# family-search threads inside the Agent.  Keep the public process admission
# cap small enough that the combined worst case remains bounded and observable.
MAX_PROCESS_REQUEST_WORKERS = 8


class RequestOverloadedError(RuntimeError):
    """Raised before submission when the process-wide request budget is full."""


class RequestExecutionTimeoutError(TimeoutError):
    """Raised when the public request budget expires before worker completion."""


@dataclass(frozen=True, slots=True)
class RequestExecutionStats:
    active: int
    peak_active: int
    accepted: int
    rejected: int


class _ProcessAdmission:
    def __init__(self) -> None:
        self._lock = Lock()
        self._idle = Event()
        self._idle.set()
        self._active = 0
        self._peak_active = 0
        self._accepted = 0
        self._rejected = 0

    def try_acquire(self, limit: int) -> bool:
        with self._lock:
            if self._active >= limit:
                self._rejected += 1
                return False
            if self._active == 0:
                self._idle.clear()
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            self._accepted += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("request admission counter underflow")
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    def stats(self) -> RequestExecutionStats:
        with self._lock:
            return RequestExecutionStats(
                active=self._active,
                peak_active=self._peak_active,
                accepted=self._accepted,
                rejected=self._rejected,
            )

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        """Wait for already accepted workers without accepting audit authority.

        Uvicorn finishes ASGI traffic before lifespan shutdown, so no new public
        request should be admitted while this wait runs. Rechecking the counter
        after the Event closes the small defensive race with direct test callers.
        """

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        if not self._idle.wait(timeout_seconds):
            return False
        with self._lock:
            return self._active == 0


_PROCESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_PROCESS_REQUEST_WORKERS,
    thread_name_prefix="finance-answer",
)
_PROCESS_ADMISSION = _ProcessAdmission()


def request_execution_stats() -> RequestExecutionStats:
    """Expose aggregate counters for deterministic tests and process telemetry."""

    return _PROCESS_ADMISSION.stats()


def wait_for_request_workers(*, timeout_seconds: float) -> bool:
    """Bound shutdown until timed-out synchronous workers finish cleanup."""

    return _PROCESS_ADMISSION.wait_until_idle(timeout_seconds)


def _run_with_deadline[ResultT](
    context: contextvars.Context,
    deadline: RequestDeadline,
    operation: Callable[[], ResultT],
) -> ResultT:
    def invoke() -> ResultT:
        with bind_request_deadline(deadline):
            return operation()

    return context.run(invoke)


def _release_admission(_future: Future[object]) -> None:
    _PROCESS_ADMISSION.release()


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    future.exception()


async def execute_bounded_request[ResultT](
    operation: Callable[[], ResultT],
    *,
    timeout_seconds: float,
    max_inflight: int,
) -> ResultT:
    """Run sync Agent work with non-queuing admission and a cooperative deadline."""

    if not 1 <= max_inflight <= MAX_PROCESS_REQUEST_WORKERS:
        raise ValueError(f"max_inflight must be in [1, {MAX_PROCESS_REQUEST_WORKERS}]")
    deadline = RequestDeadline.after(timeout_seconds)
    if not _PROCESS_ADMISSION.try_acquire(max_inflight):
        raise RequestOverloadedError("request execution capacity is full")

    context = contextvars.copy_context()
    try:
        worker_future = _PROCESS_EXECUTOR.submit(
            _run_with_deadline,
            context,
            deadline,
            operation,
        )
    except BaseException:
        _PROCESS_ADMISSION.release()
        raise
    worker_future.add_done_callback(_release_admission)

    async_future = asyncio.wrap_future(worker_future)
    async_future.add_done_callback(_consume_future_exception)
    try:
        return await asyncio.wait_for(
            asyncio.shield(async_future),
            timeout=deadline.remaining_seconds(),
        )
    except TimeoutError:
        deadline.cancel()
        worker_future.cancel()
        raise RequestExecutionTimeoutError("request execution timed out") from None
    except asyncio.CancelledError:
        deadline.cancel()
        worker_future.cancel()
        raise
