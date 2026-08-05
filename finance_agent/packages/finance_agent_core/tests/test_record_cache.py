from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
    load_record_snapshot_uncached,
)


def test_record_cache_reuses_immutable_snapshot(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, records, _ = sample_database
    cache = RecordSnapshotCache(max_entries=1)

    first = cache.get(path)
    second = cache.get(path)

    assert first is second
    assert isinstance(first.records, tuple)
    assert len(first.records) == len(records)
    assert all(not record.source_values for record in first.records)
    assert cache.stats().hits == 1
    assert cache.stats().misses == 1
    assert cache.stats().loads == 1


def test_record_cache_prevents_concurrent_load_stampede(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    load_calls: list[Path] = []

    def counting_loader(database_path: Path):
        load_calls.append(database_path)
        return load_record_snapshot_uncached(database_path)

    cache = RecordSnapshotCache(max_entries=1, loader=counting_loader)
    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(executor.map(cache.get, [path] * 8))

    assert len({id(snapshot) for snapshot in snapshots}) == 1
    assert len(load_calls) == 1
    assert cache.stats().hits == 7
    assert cache.stats().loads == 1


def test_record_cache_invalidates_when_database_file_changes(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    cache = RecordSnapshotCache(max_entries=1)
    first = cache.get(path)
    stat = path.stat()
    os.utime(
        path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
    )

    second = cache.get(path)

    assert second is not first
    assert second.records == first.records
    assert cache.stats().invalidations == 1
    assert cache.stats().misses == 2
    assert cache.stats().loads == 2


def test_record_cache_evicts_least_recently_used_database(
    sample_database: tuple[Path, list[object], object],
    domestic_sample_database: tuple[Path, list[object], object],
) -> None:
    overseas_path, _, _ = sample_database
    domestic_path, _, _ = domestic_sample_database
    cache = RecordSnapshotCache(max_entries=1)

    cache.get(overseas_path)
    cache.get(domestic_path)
    cache.get(overseas_path)

    stats = cache.stats()
    assert stats.entries == 1
    assert stats.loads == 3
    assert stats.evictions == 2


def test_identity_cache_reuses_compact_snapshot_concurrently(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, records, _ = sample_database
    cache = ProductIdentitySnapshotCache(max_entries=1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(executor.map(cache.get, [path] * 8))

    assert len({id(snapshot) for snapshot in snapshots}) == 1
    assert len(snapshots[0].records) == len(records)
    assert snapshots[0].records[0].product_id
    assert cache.stats().hits == 7
    assert cache.stats().misses == 1
    assert cache.stats().loads == 1


def test_identity_cache_invalidates_when_database_file_changes(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    cache = ProductIdentitySnapshotCache(max_entries=1)
    first = cache.get(path)
    stat = path.stat()
    os.utime(
        path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
    )

    second = cache.get(path)

    assert second is not first
    assert second.records == first.records
    assert cache.stats().invalidations == 1
    assert cache.stats().loads == 2


def test_routed_comparison_uses_identity_cache_without_full_record_cache(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    record_cache = RecordSnapshotCache(max_entries=1)
    identity_cache = ProductIdentitySnapshotCache(max_entries=1)
    agent = RoutedFinanceAgent(
        {"overseas_etp": path},
        record_cache=record_cache,
        identity_cache=identity_cache,
    )
    question = "해외 ETF AMX:B1과 AMX:B2의 AUM을 비교해줘"

    first = agent.answer(question, "cache-agent-001")
    second = agent.answer(question, "cache-agent-002")

    assert first.status == second.status == "executed"
    assert first.comparisons[0].delta == second.comparisons[0].delta
    identity_stats = identity_cache.stats()
    assert identity_stats.misses == 1
    assert identity_stats.loads == 1
    assert identity_stats.hits == 1
    assert record_cache.stats().loads == 0


def test_record_cache_requires_positive_capacity() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RecordSnapshotCache(max_entries=0)
