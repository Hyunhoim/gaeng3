from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class IntegrityError(RuntimeError):
    """Raised when a sealed-suite integrity invariant does not hold."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrityError(f"invalid UTF-8 JSON: {error}") from error


def load_json(path: Path) -> Any:
    return parse_json_bytes(path.read_bytes())


def require_mode(path: Path, expected: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected:
        raise IntegrityError(
            f"unsafe mode for {path}: expected {expected:04o}, got {actual:04o}"
        )


def validate_receipt_chain(path: Path) -> list[dict[str, Any]]:
    require_mode(path, 0o600)
    entries: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw_line in enumerate(path.read_bytes().splitlines(), start=1):
        parsed = parse_json_bytes(raw_line)
        if not isinstance(parsed, dict):
            raise IntegrityError(f"receipt line {line_number} is not an object")
        entry_hash = parsed.get("entry_hash")
        body = {key: value for key, value in parsed.items() if key != "entry_hash"}
        if parsed.get("sequence") != line_number:
            raise IntegrityError(f"receipt sequence mismatch at line {line_number}")
        if parsed.get("previous_entry_hash") != previous:
            raise IntegrityError(f"receipt predecessor mismatch at line {line_number}")
        expected_hash = sha256_bytes(canonical_bytes(body))
        if entry_hash != expected_hash:
            raise IntegrityError(f"receipt hash mismatch at line {line_number}")
        previous = entry_hash
        entries.append(parsed)
    if not entries:
        raise IntegrityError("chronology receipt is empty")
    return entries


def append_receipt(
    private_dir: Path,
    *,
    event: str,
    payload: dict[str, Any],
    create: bool = False,
    expected_existing_events: list[str] | None = None,
) -> dict[str, Any]:
    """Append one fsync'd hash-chained entry using an O_APPEND descriptor."""

    if create:
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    require_mode(private_dir, 0o700)
    receipt_path = private_dir / "chronology.jsonl"
    flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(receipt_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        if create:
            entries: list[dict[str, Any]] = []
        else:
            entries = validate_receipt_chain(receipt_path)
        if expected_existing_events is not None and [
            entry["event"] for entry in entries
        ] != expected_existing_events:
            raise IntegrityError(
                "chronology state does not permit this append: "
                f"expected {expected_existing_events}, got "
                f"{[entry['event'] for entry in entries]}"
            )
        previous = entries[-1]["entry_hash"] if entries else "0" * 64
        body = {
            "sequence": len(entries) + 1,
            "event": event,
            "at_utc": utc_now(),
            "previous_entry_hash": previous,
            "payload": payload,
        }
        entry = {**body, "entry_hash": sha256_bytes(canonical_bytes(body))}
        os.write(fd, canonical_bytes(entry) + b"\n")
        os.fsync(fd)
        directory_fd = os.open(private_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return entry
    finally:
        os.close(fd)
