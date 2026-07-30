from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from finance_agent_core.domain import NormalizedProductRecord
from finance_agent_core.storage import ProductIdentityRecord

type FundMentionStatus = Literal[
    "resolved",
    "ambiguous",
    "not_found",
    "out_of_scope",
]

_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}


def strip_fund_mention_quotes(value: str) -> str:
    """Remove balanced outer quote delimiters while preserving identity syntax."""

    stripped = value.strip()
    while (
        len(stripped) >= 2
        and stripped[0] in _QUOTE_PAIRS
        and stripped[-1] == _QUOTE_PAIRS[stripped[0]]
    ):
        stripped = stripped[1:-1].strip()
    return stripped


def normalize_fund_mention(value: str) -> str:
    """Normalize harmless display differences without erasing class punctuation."""

    normalized = strip_fund_mention_quotes(unicodedata.normalize("NFKC", value))
    return re.sub(r"\s+", "", normalized).casefold()


@dataclass(frozen=True)
class FundResolutionCandidate:
    product_id: str
    product_name: str
    short_name: str
    public_offering: bool | None

    @property
    def option_label(self) -> str:
        return f"{self.product_name} ({self.product_id})"


@dataclass(frozen=True)
class FundMentionResolution:
    mention: str
    normalized_mention: str
    status: FundMentionStatus
    candidates: tuple[FundResolutionCandidate, ...]

    @property
    def product_id(self) -> str | None:
        if self.status == "resolved":
            return self.candidates[0].product_id
        return None


class FundProductResolver:
    """Resolve exact public-fund identities and surface every collision."""

    def __init__(
        self,
        records: Sequence[NormalizedProductRecord | ProductIdentityRecord],
    ) -> None:
        fund_records = [
            record
            for record in records
            if record.product_family == "fund" and not record.is_quarantined
        ]
        if not fund_records:
            raise ValueError("fund resolver requires at least one public-fund record")
        by_id: dict[str, FundResolutionCandidate] = {}
        all_product_ids: dict[str, set[str]] = defaultdict(set)
        public_product_ids: dict[str, set[str]] = defaultdict(set)
        all_name_aliases: dict[str, set[str]] = defaultdict(set)
        public_name_aliases: dict[str, set[str]] = defaultdict(set)
        for record in fund_records:
            candidate = FundResolutionCandidate(
                product_id=record.product_id,
                product_name=record.product_name,
                short_name=record.short_name,
                public_offering=record.public_offering,
            )
            if record.product_id in by_id:
                raise ValueError(f"duplicate fund product ID: {record.product_id}")
            by_id[record.product_id] = candidate
            product_id_key = normalize_fund_mention(record.product_id)
            if product_id_key in all_product_ids:
                raise ValueError(f"duplicate normalized fund product ID: {record.product_id}")
            all_product_ids[product_id_key].add(record.product_id)
            if record.public_offering is True:
                public_product_ids[product_id_key].add(record.product_id)
            for alias in (record.product_name, record.short_name):
                key = normalize_fund_mention(alias)
                all_name_aliases[key].add(record.product_id)
                if record.public_offering is True:
                    public_name_aliases[key].add(record.product_id)
        self._by_id = by_id
        self._all_product_ids = dict(all_product_ids)
        self._public_product_ids = dict(public_product_ids)
        self._all_name_aliases = dict(all_name_aliases)
        self._public_name_aliases = dict(public_name_aliases)
        self._identity_alias_keys = tuple(
            sorted(
                set(self._all_product_ids) | set(self._all_name_aliases),
                key=lambda value: (-len(value), value),
            )
        )

    @property
    def product_count(self) -> int:
        return len(self._by_id)

    @property
    def public_product_count(self) -> int:
        return sum(candidate.public_offering is True for candidate in self._by_id.values())

    @property
    def public_alias_count(self) -> int:
        return len(set(self._public_product_ids) | set(self._public_name_aliases))

    @property
    def ambiguous_public_alias_count(self) -> int:
        return sum(
            key not in self._public_product_ids and len(product_ids) > 1
            for key, product_ids in self._public_name_aliases.items()
        )

    @property
    def identity_alias_keys(self) -> tuple[str, ...]:
        """Return normalized exact aliases for deterministic question scanning."""

        return self._identity_alias_keys

    def _candidates(self, product_ids: set[str]) -> tuple[FundResolutionCandidate, ...]:
        return tuple(self._by_id[product_id] for product_id in sorted(product_ids))

    def resolve(self, mention: str) -> FundMentionResolution:
        if not mention.strip():
            return FundMentionResolution(
                mention=mention,
                normalized_mention="",
                status="not_found",
                candidates=(),
            )
        key = normalize_fund_mention(mention)
        public_ids = self._public_product_ids.get(key, set())
        if len(public_ids) == 1:
            return FundMentionResolution(
                mention=mention,
                normalized_mention=key,
                status="resolved",
                candidates=self._candidates(public_ids),
            )
        all_ids = self._all_product_ids.get(key, set())
        if all_ids:
            return FundMentionResolution(
                mention=mention,
                normalized_mention=key,
                status="out_of_scope",
                candidates=self._candidates(all_ids),
            )
        public_ids = self._public_name_aliases.get(key, set())
        if len(public_ids) == 1:
            return FundMentionResolution(
                mention=mention,
                normalized_mention=key,
                status="resolved",
                candidates=self._candidates(public_ids),
            )
        if len(public_ids) > 1:
            return FundMentionResolution(
                mention=mention,
                normalized_mention=key,
                status="ambiguous",
                candidates=self._candidates(public_ids),
            )
        all_ids = self._all_name_aliases.get(key, set())
        if all_ids:
            return FundMentionResolution(
                mention=mention,
                normalized_mention=key,
                status="out_of_scope",
                candidates=self._candidates(all_ids),
            )
        return FundMentionResolution(
            mention=mention,
            normalized_mention=key,
            status="not_found",
            candidates=(),
        )
