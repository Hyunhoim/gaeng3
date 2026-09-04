from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXConfigurationError,
    HyperClovaXStructuredRequest,
    HyperClovaXTokenUsage,
    HyperClovaXTransportResponse,
)

_CHAT_COMPLETIONS_V3_BASE_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions"
_MAX_RESPONSE_BYTES = 2_000_000
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep credentials on the one fixed CLOVA Studio origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _build_default_opener() -> Callable[..., Any]:
    return urllib.request.build_opener(_NoRedirectHandler()).open


class HyperClovaXHTTPTransport:
    """Direct, non-streaming adapter for the official Chat Completions v3 API."""

    __slots__ = ("_api_key", "_opener", "_request_id_factory")

    def __init__(
        self,
        api_key: str,
        *,
        opener: Callable[..., Any] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not _is_safe_api_key(api_key):
            raise HyperClovaXConfigurationError(
                "CLOVA Studio API key must be 1-2048 printable ASCII characters without whitespace"
            )
        self._api_key = api_key
        self._opener = _build_default_opener() if opener is None else opener
        self._request_id_factory = (
            (lambda: str(uuid.uuid4())) if request_id_factory is None else request_id_factory
        )

    def __repr__(self) -> str:
        return "HyperClovaXHTTPTransport(api_key=<redacted>, endpoint=official-v3)"

    def complete(self, request: HyperClovaXStructuredRequest) -> HyperClovaXTransportResponse:
        request_id = self._new_request_id()
        url = f"{_CHAT_COMPLETIONS_V3_BASE_URL}/{urllib.parse.quote(request.model, safe='')}"
        body = _encode_request_body(request)
        http_request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-NCP-CLOVASTUDIO-REQUEST-ID": request_id,
            },
            method="POST",
        )

        response: Any | None = None
        try:
            response = self._opener(http_request, timeout=request.timeout_seconds)
            status_code = _response_status(response)
            if not 200 <= status_code < 300:
                return _invalid_response(status_code, request_id)
            raw_body = response.read(_MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw_body, bytes) or len(raw_body) > _MAX_RESPONSE_BYTES:
                return _invalid_response(status_code, request_id)
            return _decode_success_response(raw_body, status_code, request_id)
        except urllib.error.HTTPError as error:
            try:
                status_code = _safe_http_error_status(error)
                return _invalid_response(status_code, request_id)
            finally:
                _close_quietly(error)
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError("HyperCLOVA X HTTP request timed out") from None
            raise ConnectionError("HyperCLOVA X HTTP connection failed") from None
        except TimeoutError:
            raise TimeoutError("HyperCLOVA X HTTP request timed out") from None
        except http.client.HTTPException:
            raise ConnectionError("HyperCLOVA X HTTP connection failed") from None
        except OSError:
            raise ConnectionError("HyperCLOVA X HTTP connection failed") from None
        finally:
            _close_quietly(response)

    def _new_request_id(self) -> str:
        try:
            request_id = self._request_id_factory()
        except Exception:  # noqa: BLE001 - injected boundary must be sanitized
            raise OSError("HyperCLOVA X request id generation failed") from None
        if not isinstance(request_id, str) or _REQUEST_ID_PATTERN.fullmatch(request_id) is None:
            raise OSError("HyperCLOVA X request id generation failed")
        return request_id


def _is_safe_api_key(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    return all(0x21 <= ord(character) <= 0x7E for character in value)


def _encode_request_body(request: HyperClovaXStructuredRequest) -> bytes:
    payload = {
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ],
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": request.max_output_tokens,
        "temperature": 0.0,
        "repetitionPenalty": 1.0,
        "stop": [],
        "seed": 1,
        "thinking": {"effort": "none"},
        "responseFormat": {
            "type": "json",
            "schema": request.response_schema,
        },
    }
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise OSError("HyperCLOVA X request encoding failed") from None


def _response_status(response: Any) -> int:
    status_code = getattr(response, "status", None)
    if status_code is None:
        getcode = getattr(response, "getcode", None)
        status_code = None if getcode is None else getcode()
    if type(status_code) is not int or not 100 <= status_code <= 599:
        raise ConnectionError("HyperCLOVA X HTTP response status is invalid")
    return status_code


def _safe_http_error_status(error: urllib.error.HTTPError) -> int:
    if type(error.code) is int and 100 <= error.code <= 599:
        return error.code
    raise ConnectionError("HyperCLOVA X HTTP response status is invalid")


def _decode_success_response(
    raw_body: bytes,
    status_code: int,
    request_id: str,
) -> HyperClovaXTransportResponse:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_response(status_code, request_id)
    if not isinstance(payload, Mapping):
        return _invalid_response(status_code, request_id)

    service_status = payload.get("status")
    if not isinstance(service_status, Mapping):
        return _invalid_response(status_code, request_id)
    service_code = service_status.get("code")
    if type(service_code) not in {str, int} or str(service_code) != "20000":
        return _invalid_response(status_code, request_id)

    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("finishReason") != "stop":
        return _invalid_response(status_code, request_id)
    message = result.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return _invalid_response(status_code, request_id)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return _invalid_response(status_code, request_id)

    usage = _decode_usage(result.get("usage"))
    if usage is None:
        return _invalid_response(status_code, request_id)
    return HyperClovaXTransportResponse(
        status_code=status_code,
        content=content,
        request_id=request_id,
        usage=usage,
    )


def _decode_usage(value: object) -> HyperClovaXTokenUsage | None:
    if not isinstance(value, Mapping):
        return None
    prompt_tokens = value.get("promptTokens")
    completion_tokens = value.get("completionTokens")
    total_tokens = value.get("totalTokens")
    if not all(
        type(token_count) is int and token_count >= 0
        for token_count in (prompt_tokens, completion_tokens, total_tokens)
    ):
        return None
    if total_tokens != prompt_tokens + completion_tokens:
        return None
    return HyperClovaXTokenUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _invalid_response(status_code: int, request_id: str) -> HyperClovaXTransportResponse:
    return HyperClovaXTransportResponse(
        status_code=status_code,
        content=None,
        request_id=request_id,
        usage=None,
    )


def _close_quietly(response: Any | None) -> None:
    if response is None:
        return
    close = getattr(response, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - cleanup must not replace the normalized result
        return
