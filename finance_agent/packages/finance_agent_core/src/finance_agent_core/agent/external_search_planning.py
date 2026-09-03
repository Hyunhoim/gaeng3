from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from finance_agent_core.agent.source_selection import (
    SourceSelectionSignals,
    normalize_selection_surface,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    ExternalKnowledgeSearchPlan,
    ExternalSearchBudget,
    ExternalSearchExecutionReceipt,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage.approval import (
    load_approved_dataset_manifest,
    require_approved_database_paths,
    sha256_file,
)
from finance_agent_core.storage.identity_cache import (
    ProductIdentityRecord,
    ProductIdentitySnapshotCache,
)


def _canonical_sha256(payload: object) -> str:
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def normalize_product_identity(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


_GENERIC_PRODUCT_SUFFIXES = (
    "상장지수투자신탁",
    "상장지수펀드",
    "etf",
    "etp",
    "펀드",
    "채권",
    "상품",
)

_GENERIC_ASCII_IDENTITY_BASES = frozenset(
    {
        "AI",
        "ALL",
        "AND",
        "ETF",
        "ETN",
        "ETP",
        "FUND",
        "KR",
        "TOP",
        "US",
        "USD",
    }
)


def _literal_ascii_identity_tokens(value: str) -> tuple[str, ...]:
    """Return bounded identifier-like tokens exactly as written by the user.

    The provided overseas table stores Reuters-style tickers such as ``BUG.O``
    while users commonly type the public base ticker ``BUG``.  Normalized
    substring matching cannot distinguish that from ordinary prose.  We only
    admit an uppercase/digit token with an unambiguous base in the approved DB;
    short generic finance words are never treated as identities.
    """

    return tuple(
        token
        for token in re.findall(r"(?<![0-9A-Za-z])[A-Z0-9]{2,15}(?![0-9A-Za-z])", value)
        if token not in _GENERIC_ASCII_IDENTITY_BASES
    )


def _ric_base(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?P<base>[A-Z0-9]{2,15})\.[A-Z]{1,5}", value.strip().upper())
    return None if match is None else match.group("base")


_HOLDING_OBJECT_AFTER_ASCII_IDENTITY = re.compile(
    r"^\s*(?:"
    # An accusative entity is the constituent being held, not the product
    # whose identity should narrow the provided-DB candidate universe.
    r"(?:을|를)\s*(?:편입|보유|담|넣|포함|구성\s*종목)"
    r"|"
    # Passive/nominative forms carry the same unambiguous direction.
    r"(?:이|가|은|는)\s*(?:편입\s*된|포함\s*된|담긴|들어\s*있|보유\s*된)"
    r"|"
    # Headline-style Korean often drops the particle: `NVDA 편입 ETF`.
    r"(?:편입|보유|포함)\s*(?:된|한)?\s*(?:국내\s*|해외\s*)?"
    r"(?:ETF|ETN|ETP|펀드)"
    r"|(?:담긴|들어간)\s*(?:국내\s*|해외\s*)?"
    r"(?:ETF|ETN|ETP|펀드)"
    r"|(?:\s*[- ]?(?:containing|held\s+by)\b)"
    r")",
    re.IGNORECASE,
)
_HOLDING_OBJECT_BEFORE_ASCII_IDENTITY = re.compile(
    r"(?:"
    r"(?:편입|보유|포함|구성\s*종목)(?:한|된|하는|된\s*종목)?"
    r"|\b(?:holding|holds|containing|contains|including|includes|exposure\s+to)"
    r")\s*$",
    re.IGNORECASE,
)


def _is_holding_object_identity(question: str, *, start: int, end: int) -> bool:
    """Reject a constituent surface from provided-product identity binding.

    A company symbol or even an ETF name can legally collide with a provided
    product identity.  Syntax such as ``IBM을 편입한 ETF`` or ``KODEX 200이
    편입된 ETF`` identifies the preceding surface as a holding, whereas
    ``KODEX 200의 구성종목`` identifies the product itself.  This bounded,
    direction-only check prevents the former from shrinking the DB universe
    without maintaining any company or product-name list.
    """

    return (
        _HOLDING_OBJECT_AFTER_ASCII_IDENTITY.search(question[end : end + 48]) is not None
        or _HOLDING_OBJECT_BEFORE_ASCII_IDENTITY.search(question[max(0, start - 48) : start])
        is not None
    )


# Backwards-compatible private name for older callers/tests.  The predicate is
# deliberately no longer ASCII-specific: Korean product names can also occur as
# constituents in a fund-of-funds or ETF holdings question.
_is_holding_object_ascii_identity = _is_holding_object_identity


def _first_non_holding_identity_match(
    pattern: str,
    question: str,
    *,
    flags: int = 0,
) -> re.Match[str] | None:
    """Return the first occurrence that denotes a queried product.

    A surface may occur twice with different grammatical roles, for example
    ``KODEX 200을 담은 ETF와 KODEX 200의 구성종목``.  Inspecting only the
    first occurrence would incorrectly erase the explicit second product.
    """

    return next(
        (
            match
            for match in re.finditer(pattern, question, flags)
            if not _is_holding_object_identity(
                question,
                start=match.start(),
                end=match.end(),
            )
        ),
        None,
    )


def _literal_identity_matches(question: str, value: str) -> tuple[re.Match[str], ...]:
    """Find punctuation-tolerant literal DB identity occurrences."""

    direct = tuple(re.finditer(re.escape(value), question, re.IGNORECASE))
    if direct:
        return direct
    compact = re.sub(
        r"[^0-9A-Za-z가-힣]+",
        r"[^0-9A-Za-z가-힣]*",
        re.escape(value),
    )
    return tuple(re.finditer(compact, question, re.IGNORECASE))


def _selection_alias_occurs_outside_spans(
    question: str,
    alias: str,
    excluded_spans: Sequence[tuple[int, int]],
) -> bool:
    """Require a whole alias token that is not part of a held product name."""

    escaped = re.sub(
        r"[^0-9A-Za-z가-힣]+",
        r"[^0-9A-Za-z가-힣]*",
        re.escape(alias),
    )
    pattern = re.compile(
        rf"(?<![0-9A-Za-z가-힣]){escaped}(?![0-9A-Za-z가-힣])",
        re.IGNORECASE,
    )
    return any(
        not any(start <= match.start() and match.end() <= end for start, end in excluded_spans)
        for match in pattern.finditer(question)
    )


def normalized_product_surface_core(value: str) -> str | None:
    """Return a bounded DB-linking core only for an explicit product surface.

    This is intentionally not a general substring matcher. A company or theme
    name such as ``에코프로`` cannot narrow the DB universe merely because it
    appears in a product label. Only a question surface that explicitly ends
    in a generic product type may expose its sufficiently long core.
    """

    normalized = normalize_product_identity(value)
    for suffix in _GENERIC_PRODUCT_SUFFIXES:
        if normalized.endswith(suffix):
            core = normalized[: -len(suffix)]
            return core if len(core) >= 4 else None
    return None


def normalized_fund_programme_core(value: str) -> str | None:
    """Return a sale-class binding core for one explicit fund programme name.

    An official policy document commonly names the umbrella programme while
    the provided table stores multiple purchasable sale classes.  Only a
    literal surface ending in ``fund``/``펀드`` may create this weaker binding;
    company, theme and generic ``product`` surfaces can never narrow the DB by
    substring.  Exact product IDs/names still take precedence downstream.
    """

    normalized = normalize_product_identity(value)
    for suffix in ("fund", "펀드"):
        if normalized.endswith(suffix):
            core = normalized[: -len(suffix)]
            return core if len(core) >= 4 else None
    return None


def _candidate_from_record(record: ProductIdentityRecord) -> ProvidedProductCandidate:
    aliases = tuple(
        sorted(
            {
                value.strip()
                for value in (record.ticker, record.isin, record.short_name)
                if value is not None and value.strip() and value.strip() != record.product_name
            }
        )
    )
    return ProvidedProductCandidate(
        product_family=ProductFamily(record.product_family),
        product_id=record.product_id,
        product_name=record.product_name,
        aliases=aliases,
    )


def resolve_explicit_candidate_ids(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    identity_surfaces: Sequence[str],
) -> dict[ProductFamily, set[str]]:
    """Resolve explicit question identities without inventing an external product.

    Every exact DB match is retained.  Unknown surfaces do not widen or shrink
    the universe by themselves because quoted company/theme names may also be
    present in an external-knowledge question.
    """

    normalized_surfaces = {
        normalize_product_identity(value)
        for value in identity_surfaces
        if normalize_product_identity(value)
    }
    if not normalized_surfaces:
        return {}
    fund_programme_cores = {
        core
        for value in identity_surfaces
        if (core := normalized_fund_programme_core(value)) is not None
    }
    literal_ascii_tokens = {
        token for value in identity_surfaces for token in _literal_ascii_identity_tokens(value)
    }
    ric_base_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
    for family, records in records_by_family.items():
        for record in records:
            if record.is_quarantined:
                continue
            for identity in (record.product_id, record.ticker):
                base = _ric_base(identity)
                if base is not None:
                    ric_base_owners.setdefault(base, set()).add((family, record.product_id))
    unique_requested_ric_owners = {
        next(iter(owners))
        for base, owners in ric_base_owners.items()
        if base in literal_ascii_tokens and len(owners) == 1
    }
    # Identity strength is ordered. An exact DB identity must not be widened
    # again by a generic programme surface in the same question. Multiple
    # exact identities are retained for compare requests; weaker matches are
    # used only when no stronger match exists in the authorized family scope.
    exact_resolved: dict[ProductFamily, set[str]] = {}
    contained_resolved: dict[ProductFamily, set[str]] = {}
    core_resolved: dict[ProductFamily, set[str]] = {}
    for family, records in records_by_family.items():
        for record in records:
            if record.is_quarantined:
                continue
            exact_identities = (
                record.product_id,
                record.ticker,
                record.isin,
                record.product_name,
                record.short_name,
            )
            name_identities = (record.product_name, record.short_name)
            exact_match = any(
                value is not None and normalize_product_identity(value) in normalized_surfaces
                for value in exact_identities
            )
            contained_name_match = any(
                value is not None
                and len(normalize_product_identity(value)) >= 5
                and any(
                    normalize_product_identity(value) in surface for surface in normalized_surfaces
                )
                for value in name_identities
            )
            product_core_match = family is ProductFamily.FUND and any(
                core in normalize_product_identity(value)
                for core in fund_programme_cores
                for value in name_identities
                if value is not None
            )
            if exact_match:
                exact_resolved.setdefault(family, set()).add(record.product_id)
            if contained_name_match:
                contained_resolved.setdefault(family, set()).add(record.product_id)
            if product_core_match:
                core_resolved.setdefault(family, set()).add(record.product_id)
            if (family, record.product_id) in unique_requested_ric_owners:
                exact_resolved.setdefault(family, set()).add(record.product_id)
    return exact_resolved or contained_resolved or core_resolved


def resolve_question_identity_surfaces(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    question: str,
) -> tuple[str, ...]:
    """Locate explicit provided-product identities without treating entities as products.

    Full names/IDs are accepted only when their literal normalized surface occurs
    in the question.  Public RIC base tickers are accepted only when the base is
    unique in the approved candidate tables.  This lets ``BUG, CLOU`` scope a
    document query while preventing a holding company such as ``IBM`` from
    silently becoming an ETF unless it is itself an unambiguous provided ticker.
    """

    normalized_question = normalize_product_identity(question)
    tokens = set(_literal_ascii_identity_tokens(question))
    ric_owners: dict[str, list[ProductIdentityRecord]] = {}
    exact_ascii_owners: dict[str, list[ProductIdentityRecord]] = {}
    records = [
        record
        for family_records in records_by_family.values()
        for record in family_records
        if not record.is_quarantined
    ]
    for record in records:
        for value in (record.product_id, record.ticker):
            if (base := _ric_base(value)) is not None:
                ric_owners.setdefault(base, []).append(record)
            if value is not None and re.fullmatch(r"[A-Z0-9]{2,15}", value.strip()):
                exact_ascii_owners.setdefault(value.strip(), []).append(record)

    surfaces: list[tuple[int, str]] = []
    seen: set[str] = set()
    for token in tokens:
        owners = {
            (record.product_family, record.product_id)
            for record in (*ric_owners.get(token, ()), *exact_ascii_owners.get(token, ()))
        }
        if len(owners) != 1:
            continue
        match = _first_non_holding_identity_match(
            rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])",
            question,
        )
        if match is not None and token not in seen:
            surfaces.append((match.start(), token))
            seen.add(token)

    for record in records:
        for value in (
            record.product_id,
            record.ticker,
            record.isin,
            record.product_name,
            record.short_name,
        ):
            if value is None or value in seen:
                continue
            normalized = normalize_product_identity(value)
            if len(normalized) < 5 or normalized not in normalized_question:
                continue
            # Preserve an actual question span for downstream evidence checks.
            literal = _first_non_holding_identity_match(
                re.escape(value),
                question,
                flags=re.IGNORECASE,
            )
            if literal is not None:
                surfaces.append((literal.start(), literal.group(0)))
                seen.add(value)
                continue
            compact = re.sub(r"[^0-9A-Za-z가-힣]+", r"[^0-9A-Za-z가-힣]*", re.escape(value))
            literal = _first_non_holding_identity_match(
                compact,
                question,
                flags=re.IGNORECASE,
            )
            if literal is not None:
                surfaces.append((literal.start(), literal.group(0)))
                seen.add(value)
    return tuple(value for _, value in sorted(surfaces, key=lambda item: (item[0], item[1])))


@dataclass(frozen=True, slots=True)
class _QuestionIdentityIndex:
    aliases: Mapping[str, tuple[str, ...]]
    ric_base_owners: Mapping[str, frozenset[tuple[ProductFamily, str]]]
    ascii_identity_owners: Mapping[str, frozenset[tuple[ProductFamily, str]]]
    max_alias_length: int


def _build_question_identity_index(
    family: ProductFamily,
    records: Sequence[ProductIdentityRecord],
) -> _QuestionIdentityIndex:
    aliases: dict[str, set[str]] = {}
    ric_base_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
    ascii_identity_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
    for record in records:
        if record.is_quarantined:
            continue
        for identity in (record.product_id, record.ticker):
            if (base := _ric_base(identity)) is not None:
                ric_base_owners.setdefault(base, set()).add((family, record.product_id))
            if identity is not None and re.fullmatch(
                r"[A-Z0-9]{2,15}",
                identity.strip(),
            ):
                ascii_identity_owners.setdefault(identity.strip(), set()).add(
                    (family, record.product_id)
                )
        for value in (
            record.product_id,
            record.ticker,
            record.isin,
            record.product_name,
            record.short_name,
        ):
            if value is None:
                continue
            normalized = normalize_product_identity(value)
            if len(normalized) >= 5:
                aliases.setdefault(normalized, set()).add(value)
    frozen_aliases = {
        key: tuple(sorted(values, key=lambda value: (-len(value), value)))
        for key, values in aliases.items()
    }
    return _QuestionIdentityIndex(
        aliases=frozen_aliases,
        ric_base_owners={
            key: frozenset(values) for key, values in ric_base_owners.items()
        },
        ascii_identity_owners={
            key: frozenset(values) for key, values in ascii_identity_owners.items()
        },
        max_alias_length=max((len(key) for key in frozen_aliases), default=0),
    )


def build_provided_candidate_universe(
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    *,
    database_sha256s: Mapping[ProductFamily, str],
    candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
    explicit_identity_bound: bool = False,
) -> ProvidedProductCandidateUniverse:
    """Freeze SQL-produced candidates before external evidence can affect results."""

    candidates_by_family = {
        family: tuple(
            _candidate_from_record(record)
            for record in family_records
            if not record.is_quarantined
            and not (family is ProductFamily.FUND and record.public_offering is False)
        )
        for family, family_records in records_by_family.items()
    }
    return _build_provided_candidate_universe_from_candidates(
        candidates_by_family,
        database_sha256s=database_sha256s,
        candidate_ids_by_family=candidate_ids_by_family,
        explicit_identity_bound=explicit_identity_bound,
    )


def _build_provided_candidate_universe_from_candidates(
    candidates_by_family: Mapping[ProductFamily, Sequence[ProvidedProductCandidate]],
    *,
    database_sha256s: Mapping[ProductFamily, str],
    candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
    explicit_identity_bound: bool = False,
) -> ProvidedProductCandidateUniverse:
    """Assemble a request universe from immutable version-bound candidates."""

    allowed_ids = candidate_ids_by_family or {}
    candidates: list[ProvidedProductCandidate] = []
    for family in sorted(candidates_by_family, key=lambda item: item.value):
        if family not in database_sha256s:
            raise ValueError("candidate family lacks an approved database SHA-256")
        family_allowed = allowed_ids.get(family)
        for candidate in candidates_by_family[family]:
            if family_allowed is not None and candidate.product_id not in family_allowed:
                continue
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.product_family.value, item.product_id))
    filter_payload = {
        family.value: sorted(values)
        for family, values in sorted(allowed_ids.items(), key=lambda item: item[0].value)
    }
    return ProvidedProductCandidateUniverse(
        candidates=tuple(candidates),
        database_sha256s=dict(database_sha256s),
        candidate_filter_sha256=_canonical_sha256(filter_payload),
        explicit_identity_bound=explicit_identity_bound,
    )


class ProvidedProductUniverseBuilder:
    """Load only approved, non-quarantined identities from the four official DBs."""

    def __init__(
        self,
        database_paths: Mapping[ProductFamily | str, str | Path],
        *,
        identity_cache: ProductIdentitySnapshotCache | None = None,
        verify_approved_databases: bool = True,
    ) -> None:
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        if verify_approved_databases:
            require_approved_database_paths(self.database_paths)
        self.verify_approved_databases = verify_approved_databases
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache(
            max_entries=max(1, len(self.database_paths))
        )
        self._candidate_cache_lock = RLock()
        self._candidate_cache: dict[
            ProductFamily,
            tuple[object, tuple[ProvidedProductCandidate, ...]],
        ] = {}
        self._question_identity_index_cache: dict[
            ProductFamily,
            tuple[object, _QuestionIdentityIndex],
        ] = {}

    def _snapshot_version(self, family: ProductFamily, snapshot: object) -> object:
        version = getattr(snapshot, "version", None)
        if version is not None:
            return version
        stat = self.database_paths[family].stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _cached_candidates(
        self,
        family: ProductFamily,
        snapshot: object,
    ) -> tuple[ProvidedProductCandidate, ...]:
        """Reuse candidate DTOs until the immutable DB snapshot changes."""

        version = self._snapshot_version(family, snapshot)
        with self._candidate_cache_lock:
            cached = self._candidate_cache.get(family)
            if cached is not None and cached[0] == version:
                return cached[1]
            records = snapshot.records
            candidates = tuple(
                _candidate_from_record(record)
                for record in records
                if not record.is_quarantined
                and not (
                    family is ProductFamily.FUND and record.public_offering is False
                )
            )
            self._candidate_cache[family] = (version, candidates)
            return candidates

    def _cached_question_identity_index(
        self,
        family: ProductFamily,
        snapshot: object,
    ) -> _QuestionIdentityIndex:
        version = self._snapshot_version(family, snapshot)
        with self._candidate_cache_lock:
            cached = self._question_identity_index_cache.get(family)
            if cached is not None and cached[0] == version:
                return cached[1]
            index = _build_question_identity_index(family, snapshot.records)
            self._question_identity_index_cache[family] = (version, index)
            return index

    def _indexed_question_identity_surfaces(
        self,
        snapshots: Mapping[ProductFamily, object],
        question: str,
    ) -> tuple[str, ...]:
        indexes = tuple(
            self._cached_question_identity_index(family, snapshot)
            for family, snapshot in snapshots.items()
        )
        normalized_question = normalize_product_identity(question)
        matched_values = self._indexed_alias_values(indexes, normalized_question)

        surfaces: list[tuple[int, str]] = []
        seen: set[str] = set()
        ric_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
        for index in indexes:
            for base, owners in index.ric_base_owners.items():
                ric_owners.setdefault(base, set()).update(owners)
            for identity, owners in index.ascii_identity_owners.items():
                ric_owners.setdefault(identity, set()).update(owners)
        for token in _literal_ascii_identity_tokens(question):
            if len(ric_owners.get(token, ())) != 1:
                continue
            literal = _first_non_holding_identity_match(
                rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])",
                question,
            )
            if literal is None:
                continue
            surfaces.append((literal.start(), literal.group(0)))
            seen.add(normalize_product_identity(literal.group(0)))

        for value in sorted(matched_values, key=lambda item: (-len(item), item)):
            normalized = normalize_product_identity(value)
            if normalized in seen:
                continue
            literal = _first_non_holding_identity_match(
                re.escape(value),
                question,
                flags=re.IGNORECASE,
            )
            if literal is None:
                compact = re.sub(
                    r"[^0-9A-Za-z가-힣]+",
                    r"[^0-9A-Za-z가-힣]*",
                    re.escape(value),
                )
                literal = _first_non_holding_identity_match(
                    compact,
                    question,
                    flags=re.IGNORECASE,
                )
            if literal is None:
                continue
            surfaces.append((literal.start(), literal.group(0)))
            seen.add(normalized)
        return tuple(
            value for _, value in sorted(surfaces, key=lambda item: (item[0], -len(item[1])))
        )

    @staticmethod
    def _indexed_alias_values(
        indexes: Sequence[_QuestionIdentityIndex],
        normalized_question: str,
    ) -> set[str]:
        maximum = max((index.max_alias_length for index in indexes), default=0)
        matched_values: set[str] = set()
        for start in range(len(normalized_question)):
            for end in range(min(len(normalized_question), start + maximum), start + 4, -1):
                surface = normalized_question[start:end]
                for index in indexes:
                    matched_values.update(index.aliases.get(surface, ()))
        return matched_values

    def _indexed_holding_product_spans(
        self,
        snapshots: Mapping[ProductFamily, object],
        question: str,
    ) -> tuple[tuple[int, int], ...]:
        """Locate only DB-indexed identities used as relation objects."""

        indexes = tuple(
            self._cached_question_identity_index(family, snapshot)
            for family, snapshot in snapshots.items()
        )
        matched_values = self._indexed_alias_values(
            indexes,
            normalize_product_identity(question),
        )
        ascii_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
        for index in indexes:
            for identity, owners in (
                *index.ric_base_owners.items(),
                *index.ascii_identity_owners.items(),
            ):
                ascii_owners.setdefault(identity, set()).update(owners)
        matched_values.update(
            token
            for token in _literal_ascii_identity_tokens(question)
            if len(ascii_owners.get(token, ())) == 1
        )
        spans = {
            (match.start(), match.end())
            for value in matched_values
            for match in _literal_identity_matches(question, value)
            if _is_holding_object_identity(
                question,
                start=match.start(),
                end=match.end(),
            )
        }
        return tuple(sorted(spans))

    def build(
        self,
        *,
        families: tuple[ProductFamily, ...],
        candidate_ids_by_family: Mapping[ProductFamily, set[str]] | None = None,
        identity_surfaces: Sequence[str] = (),
        question: str | None = None,
    ) -> ProvidedProductCandidateUniverse:
        if not families:
            raise ValueError("external search requires at least one product family")
        approval = load_approved_dataset_manifest() if self.verify_approved_databases else None
        snapshots = {
            family: self.identity_cache.get(self.database_paths[family])
            for family in sorted(set(families), key=lambda item: item.value)
        }
        records = {family: snapshot.records for family, snapshot in snapshots.items()}
        effective_surfaces = tuple(identity_surfaces)
        if question is not None:
            effective_surfaces = tuple(
                dict.fromkeys(
                    (*effective_surfaces, *resolve_question_identity_surfaces(records, question))
                )
            )
        explicit_identity_requested = bool(effective_surfaces)
        explicit_ids = resolve_explicit_candidate_ids(records, effective_surfaces)
        if candidate_ids_by_family is not None and explicit_identity_requested:
            explicit_ids = {
                family: values.intersection(candidate_ids_by_family.get(family, values))
                for family, values in explicit_ids.items()
            }
        if explicit_identity_requested:
            # An explicit identity is a hard DB boundary.  An unknown branded
            # product must produce an empty universe rather than silently
            # widening to every product in the requested family.
            effective_ids = {
                family: explicit_ids.get(family, set()) for family in records
            }
        else:
            effective_ids = candidate_ids_by_family
        if self.verify_approved_databases:
            assert approval is not None
            hashes = {family: approval.datasets[family.value].database_sha256 for family in records}
        else:
            hashes = {family: sha256_file(self.database_paths[family]) for family in records}
        return _build_provided_candidate_universe_from_candidates(
            {
                family: self._cached_candidates(family, snapshot)
                for family, snapshot in snapshots.items()
            },
            database_sha256s=hashes,
            candidate_ids_by_family=effective_ids,
            explicit_identity_bound=explicit_identity_requested,
        )

    def question_identity_surfaces(
        self,
        *,
        families: tuple[ProductFamily, ...],
        question: str,
    ) -> tuple[str, ...]:
        """Expose only literal, DB-confirmed product surfaces for intent sanitizing."""

        selected_families = tuple(sorted(set(families), key=lambda item: item.value))
        if not selected_families:
            selected_families = tuple(sorted(self.database_paths, key=lambda item: item.value))
        snapshots = {
            family: self.identity_cache.get(self.database_paths[family])
            for family in selected_families
            if family in self.database_paths
        }
        return self._indexed_question_identity_surfaces(snapshots, question)

    def source_selection_signals(
        self,
        *,
        families: tuple[ProductFamily, ...],
        question: str,
        identity_surfaces: Sequence[str] = (),
        registry: OfficialSourceRegistry | None = None,
    ) -> SourceSelectionSignals:
        """Derive brand/owner signals from approved DB identities and registry metadata."""

        if not families:
            raise ValueError("source selection requires at least one product family")
        effective_registry = registry or load_official_source_registry()
        snapshots = {
            family: self.identity_cache.get(self.database_paths[family])
            for family in sorted(set(families), key=lambda item: item.value)
        }
        records_by_family = {
            family: snapshot.records for family, snapshot in snapshots.items()
        }
        inferred_identity_surfaces = self._indexed_question_identity_surfaces(
            snapshots,
            question,
        )
        effective_identity_surfaces = tuple(
            dict.fromkeys((*identity_surfaces, *inferred_identity_surfaces))
        )
        explicit_ids = resolve_explicit_candidate_ids(
            records_by_family,
            effective_identity_surfaces,
        )
        matched_records: list[ProductIdentityRecord] = []
        all_records = [
            record
            for family in sorted(records_by_family, key=lambda item: item.value)
            for record in records_by_family[family]
            if not record.is_quarantined
        ]
        for record in all_records:
            family = ProductFamily(record.product_family)
            if record.product_id in explicit_ids.get(family, set()):
                matched_records.append(record)

        held_product_spans = self._indexed_holding_product_spans(snapshots, question)

        registry_aliases = {
            alias
            for entry in effective_registry.entries
            for alias in (entry.publisher, *entry.selection_aliases)
            if normalize_selection_surface(alias)
        }
        confirmed_aliases = {
            alias
            for alias in registry_aliases
            if _selection_alias_occurs_outside_spans(
                question,
                alias,
                held_product_spans,
            )
            and any(
                normalize_selection_surface(alias)
                in normalize_selection_surface(
                    " ".join(
                        value
                        for value in (record.product_name, record.source_owner)
                        if value is not None
                    )
                )
                for record in all_records
            )
        }
        return SourceSelectionSignals(
            product_families=tuple(sorted(set(families), key=lambda item: item.value)),
            db_confirmed_aliases=tuple(sorted(confirmed_aliases, key=str.casefold)),
            matched_product_names=tuple(
                sorted({record.product_name for record in matched_records}, key=str.casefold)
            ),
            matched_source_owners=tuple(
                sorted(
                    {
                        record.source_owner
                        for record in matched_records
                        if record.source_owner is not None
                    },
                    key=str.casefold,
                )
            ),
            explicit_identity_bound=bool(explicit_ids),
        )


def intersect_evidence_with_provided_products(
    universe: ProvidedProductCandidateUniverse,
    evidence_texts: Sequence[str],
) -> tuple[ProvidedProductCandidate, ...]:
    """Exact alias intersection; external documents cannot create a product."""

    source_texts = tuple(unicodedata.normalize("NFKC", text) for text in evidence_texts)
    normalized_texts = tuple(normalize_product_identity(text) for text in source_texts)
    folded_texts = tuple(text.casefold() for text in source_texts)
    ascii_alnum_tokens = {
        token for text in folded_texts for token in re.findall(r"[0-9a-z]+", text)
    }
    ric_bases_by_product: dict[tuple[ProductFamily, str], set[str]] = {}
    ric_base_owners: dict[str, set[tuple[ProductFamily, str]]] = {}
    for candidate in universe.candidates:
        product_key = (candidate.product_family, candidate.product_id)
        for alias in candidate.aliases:
            matched = re.fullmatch(r"(?P<base>[A-Z0-9]{2,15})\.[A-Z]{1,5}", alias)
            if matched is None:
                continue
            base = matched.group("base")
            ric_bases_by_product.setdefault(product_key, set()).add(base)
            ric_base_owners.setdefault(base, set()).add(product_key)

    product_context = re.compile(
        r"\b(?:ETF|ETN|ETP|fund|performance|holdings|portfolio|investment\s+strategy)\b",
        re.IGNORECASE,
    )
    # Do not uppercase prose before extracting ticker tokens. Otherwise words
    # such as ``High``, ``Year`` or ``Aims`` become valid-looking RIC bases.
    # A short base is accepted only when the official source wrote it in
    # uppercase inside a product-context unit.
    context_ticker_tokens = {
        token
        for text in source_texts
        for unit in re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", text)
        if product_context.search(unit) is not None
        for token in re.findall(r"(?<![A-Z0-9])[A-Z0-9]{2,15}(?![A-Z0-9])", unit)
    }

    def ascii_alias_matches(folded_alias: str) -> bool:
        if folded_alias.isalnum():
            return folded_alias in ascii_alnum_tokens
        for text in folded_texts:
            start = text.find(folded_alias)
            while start >= 0:
                end = start + len(folded_alias)
                left_is_alnum = (
                    start > 0 and text[start - 1] in "0123456789abcdefghijklmnopqrstuvwxyz"
                )
                right_is_alnum = (
                    end < len(text) and text[end] in "0123456789abcdefghijklmnopqrstuvwxyz"
                )
                if not left_is_alnum and not right_is_alnum:
                    return True
                start = text.find(folded_alias, start + 1)
        return False

    matches = []
    for candidate in universe.candidates:
        canonical = normalize_product_identity(candidate.product_name)
        canonical_match = len(canonical) >= 3 and any(
            canonical in text for text in normalized_texts
        )
        alias_match = False
        for alias in candidate.aliases:
            folded_alias = unicodedata.normalize("NFKC", alias).casefold().strip()
            normalized_alias = normalize_product_identity(alias)
            if len(normalized_alias) < 4:
                continue
            if folded_alias.isascii() and re.fullmatch(r"[0-9a-z._:+-]+", folded_alias):
                if ascii_alias_matches(folded_alias):
                    alias_match = True
                    break
            elif any(normalized_alias in text for text in normalized_texts):
                alias_match = True
                break
        unique_base_match = any(
            len(ric_base_owners[base]) == 1 and base in context_ticker_tokens
            for base in ric_bases_by_product.get(
                (candidate.product_family, candidate.product_id), set()
            )
        )
        if canonical_match or alias_match or unique_base_match:
            matches.append(candidate)
    return tuple(matches)


class ExternalSearchPlanCompiler:
    """Bind a server-authorized plan to the precomputed DB candidate universe."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()

    def compile(
        self,
        plan: CompositeKnowledgeQueryPlan,
        universe: ProvidedProductCandidateUniverse,
        *,
        budget: ExternalSearchBudget | None = None,
    ) -> ExternalKnowledgeSearchPlan:
        plan_families = {
            family for requirement in plan.requirements for family in requirement.product_families
        }
        universe_families = {item.product_family for item in universe.candidates}
        if universe_families - plan_families:
            raise ValueError("provided DB candidates exceed the Typed QueryPlan families")
        if budget is None:
            # A one-document-per-product official source must still be able to
            # satisfy a requested TOP-N.  Keep two candidate slots for noisy
            # discovery results while respecting the signed global fetch cap.
            # Small/default searches retain the prior eight-candidate budget.
            requested_limit = (
                0 if plan.product_selection is None else plan.product_selection.limit
            )
            effective_budget = ExternalSearchBudget(
                max_candidates_per_requirement=min(20, max(8, requested_limit + 2))
            )
        else:
            effective_budget = budget
        universe_sha256 = universe.canonical_sha256
        source_registry_sha256 = self.registry.canonical_sha256
        execution_receipt = (
            ExternalSearchExecutionReceipt.bind(
                composite_plan=plan,
                candidate_universe_sha256=universe_sha256,
                candidate_count=len(universe.candidates),
                source_registry_sha256=source_registry_sha256,
                budget=effective_budget,
            )
            if plan.execution_authorized
            else None
        )
        return ExternalKnowledgeSearchPlan(
            composite_plan=plan,
            candidate_universe_sha256=universe_sha256,
            candidate_count=len(universe.candidates),
            source_registry_sha256=source_registry_sha256,
            budget=effective_budget,
            execution_receipt=execution_receipt,
        )


__all__ = [
    "ExternalSearchPlanCompiler",
    "ProvidedProductUniverseBuilder",
    "build_provided_candidate_universe",
    "intersect_evidence_with_provided_products",
    "normalize_product_identity",
    "normalized_fund_programme_core",
    "normalized_product_surface_core",
    "resolve_explicit_candidate_ids",
    "resolve_question_identity_surfaces",
]
