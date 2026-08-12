from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from finance_agent_core.deadline import current_request_deadline

_HASH_CHUNK_BYTES = 1024 * 1024
_SQLITE_OPEN_LOCK = threading.Lock()


class PinnedSQLiteError(RuntimeError):
    """Fail-closed error for an SQLite file that cannot be bound to its open inode."""


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_descriptor_fingerprints() -> dict[int, tuple[int, int]]:
    try:
        numbers = [int(item) for item in os.listdir("/proc/self/fd") if item.isdigit()]
    except OSError as error:
        raise PinnedSQLiteError("Linux /proc file-descriptor binding is unavailable") from error
    result: dict[int, tuple[int, int]] = {}
    for descriptor in numbers:
        try:
            value = os.fstat(descriptor)
        except OSError:
            # The descriptor used internally by procfs iteration may already
            # have closed.  It was never an application-open file.
            continue
        result[descriptor] = (value.st_dev, value.st_ino)
    return result


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, _HASH_CHUNK_BYTES, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


class PinnedSQLiteArtifact:
    """An immutable SQLite authority anchored to one already-open Linux inode.

    SQLite accepts a path rather than an existing descriptor.  A normal path can
    therefore be replaced between approval and ``sqlite3.connect``.  This guard
    keeps the approved inode open and, for every connection, confirms that the
    descriptor SQLite actually opened is the same inode.  A replace-and-restore
    race can consequently fail or keep reading the approved inode, but it cannot
    make an unapproved inode look authoritative.
    """

    def __init__(self, path: str | Path) -> None:
        try:
            resolved = Path(path).resolve(strict=True)
            descriptor = os.open(
                resolved,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise PinnedSQLiteError("database artifact is unavailable") from error
        self.path = resolved
        self._descriptor = descriptor
        self._closed = False
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PinnedSQLiteError("database artifact is not a regular file")
            self.stat_fingerprint = _fingerprint(before)
            self.database_sha256 = _sha256_descriptor(descriptor)
            after = os.fstat(descriptor)
            if _fingerprint(after) != self.stat_fingerprint:
                raise PinnedSQLiteError("database artifact changed while hashing")
            self.assert_current_path()
        except BaseException:
            os.close(descriptor)
            self._closed = True
            raise

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def assert_unchanged(self) -> None:
        if self._closed:
            raise PinnedSQLiteError("database artifact guard is closed")
        try:
            current = os.fstat(self._descriptor)
        except OSError as error:
            raise PinnedSQLiteError("database artifact descriptor is unavailable") from error
        if _fingerprint(current) != self.stat_fingerprint:
            raise PinnedSQLiteError("database artifact changed after approval")

    def assert_current_path(self) -> None:
        self.assert_unchanged()
        try:
            current = self.path.stat(follow_symlinks=False)
        except OSError as error:
            raise PinnedSQLiteError(
                "database path no longer names the approved artifact"
            ) from error
        if not stat.S_ISREG(current.st_mode) or _fingerprint(current) != self.stat_fingerprint:
            raise PinnedSQLiteError("database path no longer names the approved artifact")

    @contextmanager
    def connect_read_only(self) -> Iterator[sqlite3.Connection]:
        """Open SQLite and prove its private descriptor is this guard's inode."""

        self.assert_unchanged()
        connection: sqlite3.Connection | None = None
        sqlite_descriptor: int | None = None
        with _SQLITE_OPEN_LOCK:
            before = _open_descriptor_fingerprints()
            try:
                uri = f"file:/proc/self/fd/{self._descriptor}?mode=ro&immutable=1"
                connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
                after = _open_descriptor_fingerprints()
                matches: list[int] = []
                for candidate, fingerprint in after.items():
                    if candidate not in before and fingerprint == self.stat_fingerprint[:2]:
                        matches.append(candidate)
                if len(matches) != 1:
                    raise PinnedSQLiteError(
                        "SQLite connection did not open the approved database inode"
                    )
                sqlite_descriptor = matches[0]
            except sqlite3.Error as error:
                if connection is not None:
                    connection.close()
                raise PinnedSQLiteError(
                    "SQLite could not open the pinned database artifact"
                ) from error
            except BaseException:
                if connection is not None:
                    connection.close()
                raise

        assert connection is not None
        assert sqlite_descriptor is not None
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        deadline = current_request_deadline()
        if deadline is not None:
            connection.set_progress_handler(lambda: int(deadline.should_stop()), 1_000)
        try:
            yield connection
        finally:
            # Descriptor discovery and descriptor release share one lock.  If a
            # prior connection could close while another thread snapshots
            # /proc/self/fd, Linux may immediately reuse that descriptor number
            # and make an otherwise valid pinned open indistinguishable from the
            # old entry.  Serializing both lifecycle edges removes that ABA race.
            with _SQLITE_OPEN_LOCK:
                try:
                    self.assert_unchanged()
                    opened = os.fstat(sqlite_descriptor)
                    if _fingerprint(opened) != self.stat_fingerprint:
                        raise PinnedSQLiteError("SQLite database inode changed during execution")
                finally:
                    connection.close()
