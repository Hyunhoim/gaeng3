"""A relation is observed no earlier than the document that states it was published.

Official example 4 (``우주항공 테마``) fetched the PLUS 우주항공&UAM factsheet whose PDF
metadata date (``as_of`` 2015-10-30) predates its publication (2026-04-30).  The
extractor found the relation but ``TemporalRelationRecord`` rejected
``observed_at < valid_from`` and the runtime swallowed that as
``live_exact_evidence_span_missing``.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import finance_agent_core.agent  # noqa: F401 - resolves the retrieval import cycle
from finance_agent_core.retrieval.official_live_evidence import _observed_at


def test_metadata_date_before_publication_uses_publication_date() -> None:
    snapshot = SimpleNamespace(published_at=date(2026, 4, 30), as_of=date(2015, 10, 30))

    assert _observed_at(snapshot) == date(2026, 4, 30)


def test_as_of_after_publication_is_kept() -> None:
    snapshot = SimpleNamespace(published_at=date(2026, 4, 30), as_of=date(2026, 8, 24))

    assert _observed_at(snapshot) == date(2026, 8, 24)


def test_missing_publication_date_falls_back_to_as_of() -> None:
    snapshot = SimpleNamespace(published_at=None, as_of=date(2026, 6, 15))

    assert _observed_at(snapshot) == date(2026, 6, 15)
