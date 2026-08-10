from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-case localhost HTTP adapter for the sealed v2 evaluator."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    args = parser.parse_args()
    parsed_url = urllib.parse.urlparse(args.url)
    if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname not in ALLOWED_HOSTS:
        parser.error("the sealed adapter permits only localhost HTTP(S) targets")
    if not 0 < args.timeout_seconds <= 60:
        parser.error("timeout must be in (0, 60] seconds")
    try:
        request_object = json.loads(args.request_json)
    except json.JSONDecodeError as error:
        parser.error(f"request JSON is invalid: {error}")
    if not isinstance(request_object, dict) or set(request_object) != {
        "schema_version",
        "request_id",
        "question",
        "locale",
    }:
        parser.error("request JSON does not match the public request contract")
    if "SBV2-SECRET-" in args.request_json:
        parser.error("sealed material must never enter a target request")
    request_bytes = json.dumps(
        request_object,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        args.url,
        data=request_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        sys.stderr.write(f"adapter transport error: {type(error).__name__}\n")
        return 2
    if len(body) > MAX_RESPONSE_BYTES:
        sys.stderr.write("adapter response exceeded the byte limit\n")
        return 3
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        sys.stderr.write("adapter response was not UTF-8 JSON\n")
        return 4
    if not isinstance(decoded, dict):
        sys.stderr.write("adapter response was not a JSON object\n")
        return 5
    os.write(sys.stdout.fileno(), body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
