from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedProductRecord,
)
from finance_agent_core.storage.sqlite import (
    connect_read_only,
    load_all_records,
    load_manifest,
)

type RecordSnapshotLoader = Callable[
    [Path],
    tuple[DatabaseManifest, Sequence[NormalizedProductRecord]],
]


@dataclass(frozen=True)
class DatabaseFileVersion:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True)
class RecordSnapshot:
    database_path: Path
    version: DatabaseFileVersion
    manifest: DatabaseManifest
    records: tuple[NormalizedProductRecord, ...]


@dataclass(frozen=True)
class RecordCacheStats:
    hits: int
    misses: int
    loads: int
    invalidations: int
    evictions: int
    entries: int
    records: int


def _database_version(path: Path) -> DatabaseFileVersion:
    stat = path.stat()
    if not path.is_file():
        raise FileNotFoundError(f"normalized database does not exist: {path}")
    return DatabaseFileVersion(
        device=stat.st_dev,
        inode=stat.st_ino,
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
    )


def load_record_snapshot_uncached(
    path: Path,
) -> tuple[DatabaseManifest, Sequence[NormalizedProductRecord]]:
    with connect_read_only(path) as connection:
        manifest = load_manifest(connection)
        records = load_all_records(connection, include_source_values=False)
    return manifest, records


class RecordSnapshotCache:
    """Process-local, bounded cache for immutable normalized record snapshots."""

    def __init__(
        self,
        *,
        max_entries: int = 4,
        loader: RecordSnapshotLoader | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least one")
        self.max_entries = max_entries
        self._loader = loader or load_record_snapshot_uncached
        self._entries: OrderedDict[Path, RecordSnapshot] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._invalidations = 0
        self._evictions = 0

    def get(self, path: str | Path) -> RecordSnapshot:
        database_path = Path(path).resolve()
        with self._lock:
            version = _database_version(database_path)
            cached = self._entries.get(database_path)
            if cached is not None and cached.version == version:
                self._entries.move_to_end(database_path)
                self._hits += 1
                return cached
            self._misses += 1
            if cached is not None:
                self._invalidations += 1
                del self._entries[database_path]

            for attempt in range(2):
                before = _database_version(database_path)
                manifest, records = self._loader(database_path)
                after = _database_version(database_path)
                self._loads += 1
                if before == after:
                    snapshot = RecordSnapshot(
                        database_path=database_path,
                        version=after,
                        manifest=manifest,
                        records=tuple(records),
                    )
                    self._entries[database_path] = snapshot
                    self._entries.move_to_end(database_path)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
                        self._evictions += 1
                    return snapshot
                self._invalidations += 1
                if attempt == 1:
                    break
            raise RuntimeError(
                f"normalized database changed repeatedly while loading: {database_path}"
            )

    def clear(self, path: str | Path | None = None) -> None:
        with self._lock:
            if path is None:
                self._entries.clear()
                return
            self._entries.pop(Path(path).resolve(), None)

    def stats(self) -> RecordCacheStats:
        with self._lock:
            return RecordCacheStats(
                hits=self._hits,
                misses=self._misses,
                loads=self._loads,
                invalidations=self._invalidations,
                evictions=self._evictions,
                entries=len(self._entries),
                records=sum(len(snapshot.records) for snapshot in self._entries.values()),
            )
