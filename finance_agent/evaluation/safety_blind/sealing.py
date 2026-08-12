"""Small, dependency-free local sealing protocol for safety-blind expectations.

This is an authenticated local envelope, not a replacement for a reviewed
cryptographic storage system.  Its purpose is to keep the expected labels out
of the public question corpus while making accidental edits detectable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ALGORITHM = "hmac-sha256-xor-v1"
KEY_BYTES = 32
NONCE_BYTES = 16


class SealError(ValueError):
    """Raised when a sealed record is malformed or fails authentication."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used by the protocol."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_key(raw: bytes | str) -> bytes:
    """Parse a 32-byte key from raw bytes, hex, or ``base64:...`` text."""

    if isinstance(raw, str):
        raw_bytes = raw.strip().encode("ascii")
    else:
        raw_bytes = raw.strip()
    if raw_bytes.startswith(b"base64:"):
        try:
            key = base64.b64decode(raw_bytes[7:], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise SealError("invalid base64 safety-blind key") from exc
    elif len(raw_bytes) == KEY_BYTES:
        key = raw_bytes
    else:
        try:
            key = bytes.fromhex(raw_bytes.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SealError("key must be 32 raw bytes, 64 hex chars, or base64") from exc
    if len(key) != KEY_BYTES:
        raise SealError(f"key must decode to {KEY_BYTES} bytes")
    return key


def read_key(path: str | Path) -> bytes:
    """Read a locally held safety-blind key."""

    return parse_key(Path(path).read_bytes())


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(map(len, blocks)) < length:
        counter_bytes = counter.to_bytes(8, "big")
        blocks.append(hmac.new(key, b"stream\0" + nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1
    return b"".join(blocks)[:length]


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def seal_record(
    expectation: Mapping[str, Any],
    *,
    key: bytes,
    nonce: bytes,
) -> dict[str, str]:
    """Seal one expectation using a caller-supplied unique nonce."""

    key = parse_key(key)
    if len(nonce) != NONCE_BYTES:
        raise SealError(f"nonce must be {NONCE_BYTES} bytes")
    plaintext = canonical_json_bytes(dict(expectation))
    ciphertext = _xor(plaintext, _keystream(key, nonce, len(plaintext)))
    tag = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256).digest()
    commitment = hmac.new(key, b"commitment\0" + plaintext, hashlib.sha256).digest()
    return {
        "id": str(expectation["id"]),
        "algorithm": ALGORITHM,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
        "commitment": base64.b64encode(commitment).decode("ascii"),
    }


def open_record(record: Mapping[str, Any], *, key: bytes) -> dict[str, Any]:
    """Authenticate and open one expectation record."""

    key = parse_key(key)
    if record.get("algorithm") != ALGORITHM:
        raise SealError(f"unsupported seal algorithm: {record.get('algorithm')!r}")
    try:
        nonce = base64.b64decode(str(record["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(record["ciphertext"]), validate=True)
        tag = base64.b64decode(str(record["tag"]), validate=True)
        commitment = base64.b64decode(str(record["commitment"]), validate=True)
    except (KeyError, ValueError, base64.binascii.Error) as exc:
        raise SealError("malformed sealed expectation") from exc
    expected_tag = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise SealError("sealed expectation authentication failed")
    plaintext = _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))
    expected_commitment = hmac.new(key, b"commitment\0" + plaintext, hashlib.sha256).digest()
    if not hmac.compare_digest(commitment, expected_commitment):
        raise SealError("sealed expectation commitment failed")
    try:
        value = json.loads(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealError("sealed expectation plaintext is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("id") != record.get("id"):
        raise SealError("sealed expectation id mismatch")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())
