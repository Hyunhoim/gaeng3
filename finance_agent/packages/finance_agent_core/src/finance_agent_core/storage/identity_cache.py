from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage.record_cache import DatabaseFileVersion
from finance_agent_core.storage.sqlite import connect_read_only, load_manifest


@dataclass(frozen=True, slots=True)
class ProductIdentityRecord:
    product_family: str
    product_id: str
    product_name: str
    ticker: str | None
    isin: str | None
    short_name: str | None
    public_offering: bool | None
    is_quarantined: bool


@dataclass(frozen=True)
class ProductIdentitySnapshot:
    database_path: Path
    version: DatabaseFileVersion
    manifest: DatabaseManifest
    records: tuple[ProductIdentityRecord, ...]


@dataclass(frozen=True)
class ProductIdentityCacheStats:
    hits: int
    misses: int
    loads: int
    invalidations: int
    evictions: int
    entries: int
    records: int


_TABLE_BY_DATASET = {
    "overseas_etp": "overseas_etp_products",
    "domestic_etp": "domestic_etp_products",
    "bond": "bond_products",
    "fund": "fund_products",
}

_OPTIONAL_COLUMNS_BY_DATASET = {
    "overseas_etp": {
        "ticker": "ticker",
        "isin": "isin",
        "short_name": "NULL",
        "public_offering": "NULL",
    },
    "domestic_etp": {
        "ticker": "ticker",
        "isin": "isin",
        "short_name": "short_name",
        "public_offering": "NULL",
    },
    "bond": {
        "ticker": "ticker",
        "isin": "NULL",
        "short_name": "short_name",
        "public_offering": "NULL",
    },
    "fund": {
        "ticker": "NULL",
        "isin": "NULL",
        "short_name": "short_name",
        "public_offering": "public_offering",
    },
}


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


def load_product_identities(
    path: Path,
) -> tuple[DatabaseManifest, tuple[ProductIdentityRecord, ...]]:
    with connect_read_only(path) as connection:
        manifest = load_manifest(connection)
        try:
            table = _TABLE_BY_DATASET[manifest.dataset]
            optional = _OPTIONAL_COLUMNS_BY_DATASET[manifest.dataset]
        except KeyError as error:
            raise ValueError(f"unsupported identity dataset: {manifest.dataset}") from error
        rows = connection.execute(
            f"""
            SELECT
                product_family,
                product_id,
                product_name,
                {optional["ticker"]} AS ticker,
                {optional["isin"]} AS isin,
                {optional["short_name"]} AS short_name,
                {optional["public_offering"]} AS public_offering,
                is_quarantined
            FROM {table}
            ORDER BY product_id
            """
        ).fetchall()
    return manifest, tuple(
        ProductIdentityRecord(
            product_family=str(row["product_family"]),
            product_id=str(row["product_id"]),
            product_name=str(row["product_name"]),
            ticker=None if row["ticker"] is None else str(row["ticker"]),
            isin=None if row["isin"] is None else str(row["isin"]),
            short_name=None if row["short_name"] is None else str(row["short_name"]),
            public_offering=(
                None if row["public_offering"] is None else bool(row["public_offering"])
            ),
            is_quarantined=bool(row["is_quarantined"]),
        )
        for row in rows
    )


class ProductIdentitySnapshotCache:
    """Bounded cache for the compact fields needed by exact identity resolution."""

    def __init__(self, *, max_entries: int = 4) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least one")
        self.max_entries = max_entries
        self._entries: OrderedDict[Path, ProductIdentitySnapshot] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._invalidations = 0
        self._evictions = 0

    def get(self, path: str | Path) -> ProductIdentitySnapshot:
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
                manifest, records = load_product_identities(database_path)
                after = _database_version(database_path)
                self._loads += 1
                if before == after:
                    snapshot = ProductIdentitySnapshot(
                        database_path=database_path,
                        version=after,
                        manifest=manifest,
                        records=records,
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

    def stats(self) -> ProductIdentityCacheStats:
        with self._lock:
            return ProductIdentityCacheStats(
                hits=self._hits,
                misses=self._misses,
                loads=self._loads,
                invalidations=self._invalidations,
                evictions=self._evictions,
                entries=len(self._entries),
                records=sum(len(snapshot.records) for snapshot in self._entries.values()),
            )
