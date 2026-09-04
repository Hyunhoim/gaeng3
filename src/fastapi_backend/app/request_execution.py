from __future__ import annotations

import asyncio
import contextvars
import hashlib
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock
from time import monotonic

from finance_agent_core.deadline import RequestDeadline, bind_request_deadline

# Each accepted cross-family request can temporarily create up to four
# family-search threads inside the Agent.  Keep the public process admission
# cap small enough that the combined worst case remains bounded and observable.
MAX_PROCESS_REQUEST_WORKERS = 8


class RequestOverloadedError(RuntimeError):
    """Raised before submission when the process-wide request budget is full."""


class RequestExecutionTimeoutError(TimeoutError):
    """Raised when the public request budget expires before worker completion."""


class RequestIdentityConflictError(RuntimeError):
    """Raised when one public request key is reused for different input."""


class RequestExecutionDisposition(StrEnum):
    """Describe whether a caller started, joined, or replayed Agent work."""

    EXECUTED = "executed"
    JOINED = "joined"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class IdempotentExecutionResult[ResultT]:
    value: ResultT
    disposition: RequestExecutionDisposition


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


class _PendingAuditTerminals:
    """Track terminal callbacks that must enqueue before Audit sink shutdown."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._idle = Event()
        self._idle.set()
        self._active = 0

    def acquire(self) -> None:
        with self._lock:
            if self._active == 0:
                self._idle.clear()
            self._active += 1

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("pending Audit terminal counter underflow")
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        if not self._idle.wait(timeout_seconds):
            return False
        with self._lock:
            return self._active == 0


_PENDING_AUDIT_TERMINALS = _PendingAuditTerminals()


class RequestAuditWorkerBarrier:
    """Defer one transport terminal until its accepted workers have settled.

    The ASGI caller can disappear before synchronous Agent work notices its
    cooperative deadline.  Every worker still owns the copied request Audit
    context, so emitting the transport terminal immediately would allow later
    Agent events to appear after that terminal.  A middleware-owned instance is
    copied by reference into the worker context and keeps the terminal callback
    pending until all workers observed by that transport attempt are done.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: set[Future[object]] = set()
        self._terminal_callback: Callable[[], None] | None = None
        self._terminal_dispatched = False
        self._drain_registered = False

    def track(self, future: Future[object]) -> None:
        """Track a worker or shared single-flight future exactly once."""

        with self._lock:
            if future in self._pending:
                return
            if self._terminal_dispatched or self._terminal_callback is not None:
                raise RuntimeError("request Audit terminal was already requested")
            if not self._drain_registered:
                _PENDING_AUDIT_TERMINALS.acquire()
                self._drain_registered = True
            self._pending.add(future)
        # add_done_callback invokes immediately when completion won the race.
        # Register outside the lock so that immediate invocation cannot deadlock.
        future.add_done_callback(self._worker_settled)

    def defer_terminal(self, callback: Callable[[], None]) -> None:
        """Run the terminal callback now or after every tracked worker settles."""

        dispatch = False
        with self._lock:
            if self._terminal_dispatched or self._terminal_callback is not None:
                return
            if self._pending:
                self._terminal_callback = callback
            else:
                self._terminal_dispatched = True
                dispatch = True
        if dispatch:
            self._dispatch_terminal(callback)

    def _worker_settled(self, future: Future[object]) -> None:
        callback: Callable[[], None] | None = None
        with self._lock:
            self._pending.discard(future)
            if not self._pending and self._terminal_callback is not None:
                callback = self._terminal_callback
                self._terminal_callback = None
                self._terminal_dispatched = True
        if callback is not None:
            self._dispatch_terminal(callback)

    def _dispatch_terminal(self, callback: Callable[[], None]) -> None:
        try:
            callback()
        finally:
            release_drain = False
            with self._lock:
                if self._drain_registered:
                    self._drain_registered = False
                    release_drain = True
            if release_drain:
                _PENDING_AUDIT_TERMINALS.release()


_CURRENT_AUDIT_WORKER_BARRIER: contextvars.ContextVar[RequestAuditWorkerBarrier | None] = (
    contextvars.ContextVar(
        "finance_agent_request_audit_worker_barrier",
        default=None,
    )
)


@contextmanager
def bind_request_audit_worker_barrier(
    barrier: RequestAuditWorkerBarrier,
) -> Iterator[RequestAuditWorkerBarrier]:
    """Bind the middleware barrier across async routing and copied worker context."""

    token = _CURRENT_AUDIT_WORKER_BARRIER.set(barrier)
    try:
        yield barrier
    finally:
        _CURRENT_AUDIT_WORKER_BARRIER.reset(token)


def _track_request_audit_worker(future: Future[object]) -> None:
    barrier = _CURRENT_AUDIT_WORKER_BARRIER.get()
    if barrier is not None:
        barrier.track(future)


@dataclass(slots=True)
class _IdempotentEntry:
    input_sha256: str
    deadline: RequestDeadline
    future: Future[object]
    cache_result: Callable[[object], bool]
    completed_at: float | None = None


class IdempotentRequestCoordinator:
    """Process-local single-flight and bounded replay cache for evaluator retries.

    Raw request IDs and questions never enter the registry. Only SHA-256 values
    are retained. Running duplicate requests share one worker. A completed safe
    result can be replayed briefly, while retryable failures are removed so the
    evaluator's next attempt can execute again.
    """

    def __init__(
        self,
        *,
        replay_ttl_seconds: float = 300.0,
        max_replay_entries: int = 2_048,
    ) -> None:
        if replay_ttl_seconds <= 0:
            raise ValueError("replay_ttl_seconds must be positive")
        if max_replay_entries < 1:
            raise ValueError("max_replay_entries must be positive")
        self._replay_ttl_seconds = replay_ttl_seconds
        self._max_replay_entries = max_replay_entries
        self._lock = Lock()
        self._entries: dict[str, _IdempotentEntry] = {}

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.completed_at is not None
            and now - entry.completed_at >= self._replay_ttl_seconds
        ]
        for key in expired:
            del self._entries[key]

        overflow = len(self._entries) - self._max_replay_entries
        if overflow <= 0:
            return
        completed = sorted(
            (
                (entry.completed_at, key)
                for key, entry in self._entries.items()
                if entry.completed_at is not None
            ),
            key=lambda item: item[0],
        )
        for _completed_at, key in completed[:overflow]:
            del self._entries[key]

    def _complete(self, key_sha256: str, entry: _IdempotentEntry) -> None:
        cacheable = False
        if not entry.future.cancelled():
            try:
                cacheable = entry.cache_result(entry.future.result())
            except BaseException:  # noqa: BLE001 - callback must never escape
                cacheable = False
        with self._lock:
            if self._entries.get(key_sha256) is not entry:
                return
            if cacheable:
                entry.completed_at = monotonic()
                self._prune_locked(entry.completed_at)
            else:
                del self._entries[key_sha256]

    def _entry_for(
        self,
        operation: Callable[[], object],
        *,
        request_key: str,
        request_input: str,
        timeout_seconds: float,
        max_inflight: int,
        cache_result: Callable[[object], bool],
    ) -> tuple[_IdempotentEntry, RequestExecutionDisposition]:
        if not 1 <= max_inflight <= MAX_PROCESS_REQUEST_WORKERS:
            raise ValueError(f"max_inflight must be in [1, {MAX_PROCESS_REQUEST_WORKERS}]")
        key_sha256 = self._sha256(request_key)
        input_sha256 = self._sha256(request_input)
        now = monotonic()
        with self._lock:
            self._prune_locked(now)
            existing = self._entries.get(key_sha256)
            if existing is not None:
                if existing.input_sha256 != input_sha256:
                    raise RequestIdentityConflictError(
                        "public request key was reused for different input"
                    )
                disposition = (
                    RequestExecutionDisposition.REPLAYED
                    if existing.future.done()
                    else RequestExecutionDisposition.JOINED
                )
                return existing, disposition

            deadline = RequestDeadline.after(timeout_seconds)
            if not _PROCESS_ADMISSION.try_acquire(max_inflight):
                raise RequestOverloadedError("request execution capacity is full")
            context = contextvars.copy_context()
            try:
                future = _PROCESS_EXECUTOR.submit(
                    _run_with_deadline,
                    context,
                    deadline,
                    operation,
                )
            except BaseException:
                _PROCESS_ADMISSION.release()
                raise
            entry = _IdempotentEntry(
                input_sha256=input_sha256,
                deadline=deadline,
                future=future,
                cache_result=cache_result,
            )
            self._entries[key_sha256] = entry

        future.add_done_callback(_release_admission)
        future.add_done_callback(lambda _future: self._complete(key_sha256, entry))
        return entry, RequestExecutionDisposition.EXECUTED

    async def execute[ResultT](
        self,
        operation: Callable[[], ResultT],
        *,
        request_key: str,
        request_input: str,
        timeout_seconds: float,
        max_inflight: int,
        cache_result: Callable[[ResultT], bool],
    ) -> IdempotentExecutionResult[ResultT]:
        """Execute once per active identity and replay only safe completions."""

        entry, disposition = self._entry_for(
            operation,
            request_key=request_key,
            request_input=request_input,
            timeout_seconds=timeout_seconds,
            max_inflight=max_inflight,
            cache_result=cache_result,  # type: ignore[arg-type]
        )
        _track_request_audit_worker(entry.future)
        if entry.future.done():
            return IdempotentExecutionResult(
                value=entry.future.result(),  # type: ignore[arg-type]
                disposition=disposition,
            )

        async_future = asyncio.wrap_future(entry.future)
        async_future.add_done_callback(_consume_future_exception)
        try:
            value = await asyncio.wait_for(
                asyncio.shield(async_future),
                timeout=entry.deadline.remaining_seconds(),
            )
        except TimeoutError:
            entry.deadline.cancel()
            entry.future.cancel()
            raise RequestExecutionTimeoutError("request execution timed out") from None
        except asyncio.CancelledError:
            # Keep the one shared execution alive. A transport retry can join it
            # without duplicating provider calls, SQL work, Audit, or cost.
            raise
        return IdempotentExecutionResult(
            value=value,  # type: ignore[arg-type]
            disposition=disposition,
        )


def request_execution_stats() -> RequestExecutionStats:
    """Expose aggregate counters for deterministic tests and process telemetry."""

    return _PROCESS_ADMISSION.stats()


def wait_for_request_workers(*, timeout_seconds: float) -> bool:
    """Drain workers and their deferred Audit terminals before sink shutdown."""

    deadline = monotonic() + timeout_seconds
    if not _PROCESS_ADMISSION.wait_until_idle(timeout_seconds):
        return False
    return _PENDING_AUDIT_TERMINALS.wait_until_idle(max(0.0, deadline - monotonic()))


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
    _track_request_audit_worker(worker_future)

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
