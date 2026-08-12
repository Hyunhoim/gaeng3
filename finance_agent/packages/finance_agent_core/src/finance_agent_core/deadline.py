from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from time import monotonic


class RequestDeadlineExceeded(TimeoutError):
    """Raised when work reaches the caller's monotonic request deadline."""


@dataclass(frozen=True, slots=True)
class RequestDeadline:
    """A process-local deadline and cooperative cancellation signal."""

    expires_at: float
    cancel_event: Event = field(default_factory=Event, compare=False, repr=False)

    @classmethod
    def after(cls, timeout_seconds: float) -> RequestDeadline:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("request timeout must be a positive finite number")
        return cls(expires_at=monotonic() + timeout_seconds)

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - monotonic())

    def cancel(self) -> None:
        self.cancel_event.set()

    def should_stop(self) -> bool:
        return self.cancel_event.is_set() or self.remaining_seconds() <= 0


_CURRENT_REQUEST_DEADLINE: ContextVar[RequestDeadline | None] = ContextVar(
    "finance_agent_request_deadline",
    default=None,
)


@contextmanager
def bind_request_deadline(deadline: RequestDeadline) -> Iterator[None]:
    """Bind a deadline for synchronous Agent/provider/SQLite work in this context."""

    token = _CURRENT_REQUEST_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _CURRENT_REQUEST_DEADLINE.reset(token)


def current_request_deadline() -> RequestDeadline | None:
    return _CURRENT_REQUEST_DEADLINE.get()


def raise_if_request_stopped() -> None:
    """Cooperatively stop CPU work after request cancellation or expiry."""

    deadline = current_request_deadline()
    if deadline is not None and deadline.should_stop():
        raise RequestDeadlineExceeded("request deadline exceeded")


def remaining_request_timeout(configured_timeout_seconds: float) -> float:
    """Clamp a nested operation timeout to the active request's remaining budget."""

    if not math.isfinite(configured_timeout_seconds) or configured_timeout_seconds <= 0:
        raise ValueError("configured timeout must be a positive finite number")
    deadline = current_request_deadline()
    if deadline is None:
        return configured_timeout_seconds
    remaining = deadline.remaining_seconds()
    if deadline.cancel_event.is_set() or remaining <= 0:
        raise RequestDeadlineExceeded("request deadline exceeded")
    return min(configured_timeout_seconds, remaining)
