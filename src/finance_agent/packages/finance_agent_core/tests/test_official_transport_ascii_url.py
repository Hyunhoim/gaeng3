"""The urllib transport must send ASCII request lines and fail closed.

Official examples E2/E4 returned HTTP 503 on the evaluation host on 2026-09-05:
NAVER discovery handed the connector a publisher URL with raw Hangul in its
path, ``http.client`` raised ``UnicodeEncodeError`` while writing the request
line, and that exception was neither encoded away nor mapped to a connector
error, so the whole request failed instead of skipping one candidate.
"""

from __future__ import annotations

import io
from email.message import Message

import pytest

from finance_agent_core.retrieval.official_connector import (
    OfficialConnectorError,
    UrllibOfficialHttpTransport,
    ascii_request_url,
)


def test_hangul_path_and_query_are_percent_encoded_once() -> None:
    url = "https://www.hanaroetf.com/뉴스룸/우주항공 테마?q=우주항공&page=2&x=a%20b"

    encoded = ascii_request_url(url)

    encoded.encode("ascii")
    assert encoded == (
        "https://www.hanaroetf.com/%EB%89%B4%EC%8A%A4%EB%A3%B8/"
        "%EC%9A%B0%EC%A3%BC%ED%95%AD%EA%B3%B5%20%ED%85%8C%EB%A7%88"
        "?q=%EC%9A%B0%EC%A3%BC%ED%95%AD%EA%B3%B5&page=2&x=a%20b"
    )
    assert ascii_request_url(encoded) == encoded  # idempotent
    assert ascii_request_url("https://a.b/c?d=1#frag") == "https://a.b/c?d=1"


class _Headers(Message):
    def get_content_type(self) -> str:  # type: ignore[override]
        return "text/html"


class _Response(io.BytesIO):
    status = 200
    headers = _Headers()

    def geturl(self) -> str:
        return "https://www.hanaroetf.com/x"

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_transport_sends_ascii_url_and_wraps_encoding_failures() -> None:
    transport = UrllibOfficialHttpTransport()
    seen: list[str] = []

    class _Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            request.full_url.encode("ascii")
            return _Response(b"<html>ok</html>")

    transport._opener = _Opener()  # type: ignore[assignment]
    response = transport.fetch(
        "https://www.hanaroetf.com/뉴스룸/우주항공", timeout_seconds=1.0, max_bytes=1024
    )
    assert response.status_code == 200
    assert seen == ["https://www.hanaroetf.com/%EB%89%B4%EC%8A%A4%EB%A3%B8/%EC%9A%B0%EC%A3%BC%ED%95%AD%EA%B3%B5"]

    class _Broken:
        def open(self, request, timeout):
            raise UnicodeEncodeError("ascii", "우주", 0, 2, "ordinal not in range(128)")

    transport._opener = _Broken()  # type: ignore[assignment]
    with pytest.raises(OfficialConnectorError):
        transport.fetch("https://www.hanaroetf.com/x", timeout_seconds=1.0, max_bytes=1024)
