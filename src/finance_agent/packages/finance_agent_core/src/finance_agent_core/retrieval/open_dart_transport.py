from __future__ import annotations

import http.client
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from finance_agent_core.retrieval.open_dart import (
    OpenDartCollectionError,
    OpenDartHttpResponse,
)

_OPEN_DART_ORIGIN = "https://opendart.fss.or.kr"
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "/api/corpCode.xml": frozenset(),
    "/api/list.json": frozenset(
        {
            "bgn_de",
            "corp_code",
            "end_de",
            "last_reprt_at",
            "page_count",
            "page_no",
        }
    ),
    "/api/document.xml": frozenset({"rcept_no"}),
}
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")
_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{40}$")
_MAX_HTTP_BYTES = 32 * 1024 * 1024
_MAX_HTTP_ATTEMPTS = 3
_MAX_REQUEST_BUDGET = 5000
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the query-string credential to another location."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _default_opener() -> Callable[..., Any]:
    return urllib.request.build_opener(_NoRedirectHandler()).open


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def load_open_dart_api_key(path: str | Path) -> str:
    """Read one Open DART key without following links or accepting shared access."""

    key_path = Path(path)
    if key_path.is_symlink():
        raise OpenDartCollectionError("Open DART API key file must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as error:
        raise OpenDartCollectionError("Open DART API key file is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OpenDartCollectionError("Open DART API key must be stored in a regular file")
        if stat.S_IMODE(before.st_mode) not in {0o400, 0o600}:
            raise OpenDartCollectionError("Open DART API key file mode must be 0400 or 0600")
        if hasattr(os, "geteuid") and before.st_uid != os.geteuid():
            raise OpenDartCollectionError("Open DART API key file must be owned by this process")
        if before.st_nlink != 1:
            raise OpenDartCollectionError("Open DART API key file must have exactly one link")
        if before.st_size != 40:
            raise OpenDartCollectionError("Open DART API key must contain exactly 40 characters")
        data = os.read(descriptor, 41)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or len(data) != before.st_size:
            raise OpenDartCollectionError("Open DART API key file changed while being read")
    finally:
        os.close(descriptor)
    try:
        key = data.decode("ascii")
    except UnicodeDecodeError:
        raise OpenDartCollectionError("Open DART API key has an invalid shape") from None
    if _API_KEY_PATTERN.fullmatch(key) is None:
        raise OpenDartCollectionError("Open DART API key has an invalid shape")
    return key


def _validated_params(endpoint: str, params: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    allowed = _ALLOWED_PARAMS.get(endpoint)
    if allowed is None:
        raise OpenDartCollectionError("Open DART endpoint is not allowed")
    if set(params) != allowed or "crtfc_key" in params:
        raise OpenDartCollectionError(
            "Open DART request parameters differ from the endpoint contract"
        )
    normalized: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        if not isinstance(value, str) or _PARAMETER_PATTERN.fullmatch(value) is None:
            raise OpenDartCollectionError("Open DART request parameter has an invalid shape")
        normalized.append((key, value))
    return tuple(normalized)


def _close_quietly(response: object | None) -> None:
    if response is None:
        return
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:  # noqa: BLE001 - cleanup must not replace the sanitized result
        pass


class UrllibOpenDartTransport:
    """Bounded HTTPS adapter for the three official Open DART collection APIs."""

    __slots__ = (
        "_api_key",
        "_max_requests",
        "_opener",
        "_request_count",
        "_retry_delays",
        "_sleeper",
    )

    def __init__(
        self,
        api_key: str,
        *,
        opener: Callable[..., Any] | None = None,
        retry_delays: Sequence[float] = (0.25, 0.5),
        sleeper: Callable[[float], None] = time.sleep,
        max_requests: int = 2000,
    ) -> None:
        if _API_KEY_PATTERN.fullmatch(api_key) is None:
            raise OpenDartCollectionError("Open DART API key has an invalid shape")
        delays = tuple(float(value) for value in retry_delays)
        if (
            len(delays) >= _MAX_HTTP_ATTEMPTS
            or any(value < 0.0 or value > 2.0 for value in delays)
            or sum(delays) > 3.0
        ):
            raise OpenDartCollectionError("Open DART retry policy is outside the safety bound")
        if not 1 <= max_requests <= _MAX_REQUEST_BUDGET:
            raise OpenDartCollectionError("Open DART request budget is outside the safety bound")
        self._api_key = api_key
        self._opener = _default_opener() if opener is None else opener
        self._retry_delays = delays
        self._sleeper = sleeper
        self._max_requests = max_requests
        self._request_count = 0

    def __repr__(self) -> str:
        return (
            "UrllibOpenDartTransport(api_key=<redacted>, "
            f"origin={_OPEN_DART_ORIGIN}, requests={self._request_count}/{self._max_requests})"
        )

    def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> OpenDartHttpResponse:
        if not 0.1 <= timeout_seconds <= 30.0:
            raise OpenDartCollectionError("Open DART timeout is outside the safety bound")
        if not 1 <= max_bytes <= _MAX_HTTP_BYTES:
            raise OpenDartCollectionError("Open DART response budget is outside the safety bound")
        safe_params = _validated_params(endpoint, params)
        query = urllib.parse.urlencode((("crtfc_key", self._api_key), *safe_params))
        url = f"{_OPEN_DART_ORIGIN}{endpoint}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,application/zip,application/octet-stream;q=0.9",
                "User-Agent": "MiraeAssetFinanceAgentOpenDartCollector/1.0",
            },
            method="GET",
        )
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                return self._attempt(request, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            except _RetryableOpenDartError:
                if attempt + 1 >= attempts:
                    raise OpenDartCollectionError(
                        "Open DART request failed after bounded retries"
                    ) from None
                self._sleeper(self._retry_delays[attempt])
        raise AssertionError("unreachable Open DART retry state")  # pragma: no cover

    def _attempt(
        self,
        request: urllib.request.Request,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OpenDartHttpResponse:
        if self._request_count >= self._max_requests:
            raise OpenDartCollectionError("Open DART request budget is exhausted")
        self._request_count += 1
        response: Any | None = None
        try:
            response = self._opener(request, timeout=timeout_seconds)
            status_code = int(response.status)
            if status_code in _RETRYABLE_STATUS_CODES:
                raise _RetryableOpenDartError
            if status_code != 200:
                raise OpenDartCollectionError("Open DART endpoint did not return HTTP 200")
            final_url = str(response.geturl())
            if final_url != request.full_url:
                raise OpenDartCollectionError("Open DART endpoint redirected unexpectedly")
            body = response.read(max_bytes + 1)
            if not isinstance(body, bytes) or not body or len(body) > max_bytes:
                raise OpenDartCollectionError("Open DART response size is outside the bound")
            content_type = response.headers.get_content_type().casefold()
            return OpenDartHttpResponse(status_code, content_type, body)
        except urllib.error.HTTPError as error:
            try:
                if int(error.code) in _RETRYABLE_STATUS_CODES:
                    raise _RetryableOpenDartError from None
                raise OpenDartCollectionError("Open DART endpoint returned an HTTP error") from None
            finally:
                _close_quietly(error)
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, OSError)):
                raise _RetryableOpenDartError from None
            raise OpenDartCollectionError("Open DART connection failed") from None
        except (TimeoutError, http.client.HTTPException, OSError):
            raise _RetryableOpenDartError from None
        finally:
            _close_quietly(response)


class _RetryableOpenDartError(Exception):
    """Internal marker whose message deliberately carries no remote data."""


__all__ = [
    "UrllibOpenDartTransport",
    "load_open_dart_api_key",
]
