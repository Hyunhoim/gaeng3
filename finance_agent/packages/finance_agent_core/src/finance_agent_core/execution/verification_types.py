from __future__ import annotations

from datetime import date
from typing import Protocol

from finance_agent_core.config import QualityStatus


class VerifierRecord(Protocol):
    product_id: str
    product_family: str
    is_quarantined: bool
    source_snapshot_date: date
    static_as_of: date
    dynamic_as_of: date

    def canonical_value(self, field_name: str) -> object | None: ...

    def row_level_quality(
        self,
        field_name: str,
    ) -> tuple[QualityStatus | None, str | None]: ...
