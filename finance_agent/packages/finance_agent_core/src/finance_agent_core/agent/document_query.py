from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from finance_agent_core.agent.compound_query import (
    CompoundExecution,
    CompoundFamilyPlan,
    CompoundFilter,
    CompoundMetric,
    TypedCompoundPlan,
    TypedCompoundQueryEngine,
    _numeric_filters,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind
from finance_agent_core.storage.sqlite import connect_read_only

_PHASE4_DOCUMENT_IDS = frozenset(
    {
        *(f"phase4-globalx-{ticker}-20250331" for ticker in (
            "aiq",
            "botz",
            "bug",
            "clou",
            "copx",
            "driv",
            "finx",
            "gnom",
            "hero",
            "lit",
            "snsr",
            "ura",
        )),
        "phase4-tiger-battery-lev-20240531",
        "phase4-tiger-semi-20250630",
        "phase4-tiger-semi-lev-20250530",
        "samsungfund-kodex-pension-guide-20260416",
    }
)

_OFFICIAL_DOMAINS = frozenset(
    {
        "assets.globalxetfs.com",
        "investments.miraeasset.com",
        "www.samsungfund.com",
    }
)

_TABLE_BY_FAMILY = {
    ProductFamily.DOMESTIC_ETP: "domestic_etp_products",
    ProductFamily.OVERSEAS_ETP: "overseas_etp_products",
}

_STOPWORDS = frozenset(
    {
        "기준",
        "지난",
        "개월",
        "나온",
        "가이드북",
        "에서",
        "투자",
        "한다고",
        "소개하는",
        "찾아주세요",
        "상품",
        "산업",
        "대표",
        "기업",
        "테마",
        "연결",
        "etf",
        "kodex",
        "미국",
        "국내",
    }
)

# Official documents use exchange/vendor abbreviations while Korean questions
# normally use the issuer's public name.  This is a relation-identity registry,
# not a query-specific answer table: every alias is applied symmetrically to
# every document and every question.
_ENTITY_EQUIVALENCES = {
    "Samsung Electron": "삼성전자",
    "Nvidia Corp": "NVIDIA",
    "Microsoft Corp": "Microsoft",
    "Apple Inc": "Apple",
    "Tesla Inc": "Tesla",
    "Cameco Corp": "Cameco",
    "NuScale Power Corp": "NuScale Power",
    "Nuscale Power Corp": "NuScale Power",
    "Nuscale Power Co": "NuScale Power",
    "Oklo Inc": "Oklo",
    "Oklo Inc-a": "Oklo",
    "Hyundai Eng&cons": "현대건설",
    "Hyundai Eng&con": "현대건설",
    "Vertiv Holdings Co-a": "Vertiv Holdings",
}


@dataclass(frozen=True, slots=True)
class Holding:
    name: str
    weight_pct: float

    @property
    def normalized_name(self) -> str:
        return _normalize_name(self.name)

    @property
    def canonical_name(self) -> str:
        return canonical_relation_entity(self.name)


@dataclass(frozen=True, slots=True)
class OfficialDocumentProfile:
    profile_key: str
    document_id: str
    product_name: str
    title: str
    source_uri: str
    publisher: str
    as_of: date
    document_sha256: str
    holdings: tuple[Holding, ...]
    description: str
    risk_text: str

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.profile_key, self.product_name)))


@dataclass(frozen=True, slots=True)
class VerifiedDocumentSelection:
    profile: OfficialDocumentProfile
    product_family: ProductFamily
    product_id: str
    matched_holdings: tuple[Holding, ...]


@dataclass(frozen=True, slots=True)
class DocumentQueryExecution:
    compound: CompoundExecution
    documents: tuple[DocumentEvidence, ...]
    selections: tuple[VerifiedDocumentSelection, ...]
    relation_answer: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


_NORMALIZED_ENTITY_EQUIVALENCES = {
    _normalize_name(key): _normalize_name(value)
    for key, value in _ENTITY_EQUIVALENCES.items()
}


def canonical_relation_entity(value: str) -> str:
    normalized = _normalize_name(value)
    return _NORMALIZED_ENTITY_EQUIVALENCES.get(normalized, normalized)


def _semantic_tokens(value: str) -> set[str]:
    suffixes = (
        "에서는",
        "에서",
        "으로",
        "라고",
        "한다는",
        "한다고",
        "하는",
        "한",
        "과",
        "와",
        "을",
        "를",
        "에",
        "의",
    )
    result: set[str] = set()
    for raw in re.findall(r"[0-9A-Za-z가-힣]{2,}", value):
        token = raw.casefold()
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                token = token[: -len(suffix)]
                break
        if token and token not in _STOPWORDS:
            result.add(token)
    return result


def _numbered_holdings(text: str, marker: str) -> tuple[Holding, ...]:
    all_lines = text.splitlines()
    try:
        marker_index = next(
            index for index, line in enumerate(all_lines) if line.strip() == marker
        )
    except StopIteration:
        return ()
    lines = [line.strip() for line in all_lines[marker_index:]]
    holdings: list[Holding] = []
    current = ""
    for line in lines[1:]:
        if len(holdings) >= 10:
            break
        if re.match(r"^\d+\s+", line):
            current = line
        elif current:
            current += " " + line
        else:
            continue
        match = re.match(r"^\d+\s+(.+?)\s+(-?\d+(?:\.\d+)?)%?$", current)
        if match is None:
            continue
        holdings.append(Holding(match.group(1).strip(), float(match.group(2))))
        current = ""
    return tuple(holdings)


def _global_x_holdings(text: str) -> tuple[Holding, ...]:
    marker = "TOP 10 HOLDINGS (%)"
    position = text.find(marker)
    if position < 0:
        return ()
    lines = text[position:].splitlines()[1:]
    holdings: list[Holding] = []
    for line in lines:
        if len(holdings) >= 10:
            break
        match = re.match(r"^(.+?)\s+(-?\d+(?:\.\d+)?)%$", line.strip())
        if match is not None:
            holdings.append(Holding(match.group(1).strip(), float(match.group(2))))
    return tuple(holdings)


def _risk_excerpt(text: str) -> str:
    starts = (
        "Investing involves risk",
        "집합투자증권은 예금자보호법",
        "이 금융상품은 예금자보호법",
    )
    for marker in starts:
        position = text.find(marker)
        if position >= 0:
            return " ".join(text[position : position + 900].split())
    return "공식 문서에는 투자 원금 손실 가능성과 과거 성과의 비보장 유의사항이 적혀 있습니다."


def _profile_excerpt(profile: OfficialDocumentProfile) -> str:
    holdings = "; ".join(
        f"{item.name} {item.weight_pct:g}%" for item in profile.holdings
    )
    return (
        f"{profile.product_name} ({profile.profile_key}) · 상위 10개: {holdings}. "
        f"설명: {profile.description[:600]}. 위험 고지: {profile.risk_text[:900]}"
    )


class OfficialDocumentRelationIndex:
    """Parse approved-domain snapshots into dated product-holding profiles.

    Exact document names and product identifiers are a deterministic fast path.
    Document Dense remains the fallback for unresolved documents; this index
    never guesses a holding that is absent from the frozen official text.
    """

    def __init__(self, corpus_dir: str | Path) -> None:
        self.corpus_dir = Path(corpus_dir).resolve()
        manifest_path = self.corpus_dir / "pending-manifest.json"
        payload = json.loads(manifest_path.read_bytes())
        entries = {
            str(item["document_id"]): item
            for item in payload.get("documents", ())
            if str(item.get("document_id")) in _PHASE4_DOCUMENT_IDS
        }
        if set(entries) != _PHASE4_DOCUMENT_IDS:
            missing = sorted(_PHASE4_DOCUMENT_IDS - set(entries))
            raise ValueError(f"official document relation corpus is incomplete: {missing}")
        profiles: list[OfficialDocumentProfile] = []
        for document_id in sorted(entries):
            entry = entries[document_id]
            domain = (urlparse(str(entry["source_uri"])).hostname or "").casefold()
            if domain not in _OFFICIAL_DOMAINS:
                raise ValueError(f"unapproved official document domain: {domain}")
            text_path = self.corpus_dir / str(entry["text_relative_path"])
            if _sha256(text_path) != entry["text_sha256"]:
                raise ValueError(f"official text snapshot hash differs: {document_id}")
            text = text_path.read_text(encoding="utf-8")
            if document_id.startswith("phase4-globalx-"):
                profiles.append(self._global_x_profile(entry, text))
            elif document_id.startswith("phase4-tiger-"):
                profiles.append(self._tiger_profile(entry, text))
            else:
                profiles.extend(self._samsung_profiles(entry, text))
        if any(not item.holdings for item in profiles):
            missing = [item.profile_key for item in profiles if not item.holdings]
            raise ValueError(f"official top-10 table extraction failed: {missing}")
        self.profiles = tuple(profiles)

    @staticmethod
    def _global_x_profile(entry: dict[str, object], text: str) -> OfficialDocumentProfile:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        key = lines[0]
        holdings = _global_x_holdings(text)
        description = text[: text.find("TOP 10 HOLDINGS (%)")]
        return OfficialDocumentProfile(
            profile_key=key,
            document_id=str(entry["document_id"]),
            product_name=lines[1],
            title=str(entry["title"]),
            source_uri=str(entry["source_uri"]),
            publisher=str(entry["publisher"]),
            as_of=date.fromisoformat(str(entry["as_of"])),
            document_sha256=str(entry["text_sha256"]),
            holdings=holdings,
            description=" ".join(description.split()),
            risk_text=_risk_excerpt(text),
        )

    @staticmethod
    def _tiger_profile(entry: dict[str, object], text: str) -> OfficialDocumentProfile:
        code_match = re.search(r"\((\d{6})\)", text[:1000])
        if code_match is None:
            raise ValueError(f"TIGER code missing: {entry['document_id']}")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return OfficialDocumentProfile(
            profile_key=code_match.group(1),
            document_id=str(entry["document_id"]),
            product_name=lines[1] if len(lines) > 1 else str(entry["title"]),
            title=str(entry["title"]),
            source_uri=str(entry["source_uri"]),
            publisher=str(entry["publisher"]),
            as_of=date.fromisoformat(str(entry["as_of"])),
            document_sha256=str(entry["text_sha256"]),
            holdings=_numbered_holdings(text, "상위 10종목"),
            description=" ".join(text[:3000].split()),
            risk_text=_risk_excerpt(text),
        )

    @staticmethod
    def _samsung_profiles(
        entry: dict[str, object],
        text: str,
    ) -> tuple[OfficialDocumentProfile, ...]:
        matches = list(re.finditer(r"종목코드\s+([0-9A-Z]{6})", text))
        profiles: list[OfficialDocumentProfile] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[match.start() : end]
            holdings = _numbered_holdings(segment, "No. 종목명 비중(%)")
            if not holdings:
                continue
            name_match = re.search(r"상품\s*정보\s*\nKodex\s*\n(.+? ETF)", segment, re.DOTALL)
            name = (
                " ".join(name_match.group(1).split())
                if name_match is not None
                else f"KODEX {match.group(1)}"
            )
            profiles.append(
                OfficialDocumentProfile(
                    profile_key=match.group(1),
                    document_id=str(entry["document_id"]),
                    product_name=name,
                    title=str(entry["title"]),
                    source_uri=str(entry["source_uri"]),
                    publisher=str(entry["publisher"]),
                    as_of=date.fromisoformat(str(entry["as_of"])),
                    document_sha256=str(entry["text_sha256"]),
                    holdings=holdings,
                    description=" ".join(segment.split()),
                    risk_text=_risk_excerpt(segment),
                )
            )
        return tuple(profiles)

    def mentioned_profiles(self, question: str) -> tuple[OfficialDocumentProfile, ...]:
        located: list[tuple[int, OfficialDocumentProfile]] = []
        for profile in self.profiles:
            positions = []
            for alias in profile.aliases:
                if not alias:
                    continue
                match = re.search(
                    rf"(?<![0-9A-Z]){re.escape(alias)}(?![0-9A-Z])",
                    question,
                    re.IGNORECASE,
                )
                if match is not None:
                    positions.append(match.start())
            if positions:
                located.append((min(positions), profile))
        located.sort(key=lambda item: item[0])
        return tuple(item[1] for item in located)

    def semantic_samsung_profile(self, description: str) -> OfficialDocumentProfile | None:
        query_tokens = _semantic_tokens(description)
        candidates = tuple(
            profile
            for profile in self.profiles
            if profile.document_id == "samsungfund-kodex-pension-guide-20260416"
        )
        document_tokens_by_key = {
            profile.profile_key: _semantic_tokens(profile.description)
            for profile in candidates
        }
        document_frequency = {
            token: sum(token in values for values in document_tokens_by_key.values())
            for token in query_tokens
        }
        scored = [
            (
                sum(
                    1.0 / document_frequency[token]
                    for token in query_tokens & document_tokens_by_key[profile.profile_key]
                    if document_frequency[token]
                )
                + 10.0
                * sum(
                    token in _normalize_name(profile.product_name)
                    for token in query_tokens
                ),
                profile,
            )
            for profile in candidates
        ]
        scored.sort(key=lambda item: (-item[0], item[1].profile_key))
        if not scored or scored[0][0] <= 0:
            return None
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-12:
            return None
        return scored[0][1]


class TypedDocumentQueryEngine:
    """Execute explicit official-document relations and DB intersection only."""

    def __init__(
        self,
        database_paths: dict[ProductFamily, Path],
        *,
        compound_engine: TypedCompoundQueryEngine,
        corpus_dir: str | Path,
    ) -> None:
        self.database_paths = database_paths
        self.compound_engine = compound_engine
        self.index = OfficialDocumentRelationIndex(corpus_dir)

    def recognizes(self, question: str) -> bool:
        if "상위 10개" not in question and "상위 10종목" not in question:
            return False
        return bool(self.index.mentioned_profiles(question)) or (
            re.search(
                r"(?:지난|최근).{0,18}6개월|6개월.{0,18}거슬러\s*올라간\s*기간",
                question,
            )
            is not None
            and "가이드" in question
        )

    def execute(
        self,
        question: str,
        request_id: str,
        *,
        hclx_structure: tuple[
            tuple[str, tuple[ProductFamily, ...], str], ...
        ] = (),
    ) -> DocumentQueryExecution | None:
        if not self.recognizes(question):
            return None
        profiles = self.index.mentioned_profiles(question)
        dated = (
            re.search(
                r"(?:지난|최근).{0,18}6개월|6개월.{0,18}거슬러\s*올라간\s*기간",
                question,
            )
            is not None
            and "가이드" in question
        )
        if dated:
            description_match = re.search(
                r"가이드(?:북)?에서\s*(.+?)(?:ETF\s*(?:설명을\s*)?(?:확인|찾)|"
                r"ETF를\s*찾)",
                question,
            )
            if description_match is None:
                return None
            selected = self.index.semantic_samsung_profile(description_match.group(1))
            if selected is None:
                return None
            profiles = (selected,)
        if not profiles:
            return None

        if re.search(r"(?:겹치는\s*기업|공통\s*종목)", question) and re.search(
            r"그\s*공통\s*(?:기업|종목)", question
        ):
            matches = self._overlap_matches(question, profiles)
        else:
            entities = self._entities(question, profiles)
            if not entities:
                return None
            require_all = re.search(
                r"(?:모두가|모두\s*상위|두\s*종목이\s*모두|두\s*기업이\s*모두)",
                question,
            ) is not None
            threshold_match = re.search(r"합이\s*([0-9]+(?:\.[0-9]+)?)%\s*이상", question)
            minimum = None if threshold_match is None else float(threshold_match.group(1))
            matches = self._relation_matches(
                profiles,
                entities,
                require_all=require_all,
                minimum_sum=minimum,
            )

        family_order: list[ProductFamily] = []
        selections: list[VerifiedDocumentSelection] = []
        for profile, holdings in matches:
            resolved = self._resolve_product(profile.profile_key)
            if resolved is None:
                continue
            family, product_id = resolved
            if family not in family_order:
                family_order.append(family)
            selections.append(
                VerifiedDocumentSelection(profile, family, product_id, holdings)
            )
        for profile in profiles:
            resolved = self._resolve_product(profile.profile_key)
            if resolved is not None and resolved[0] not in family_order:
                family_order.append(resolved[0])

        limit = self._limit(question, dated=dated)
        plans: list[CompoundFamilyPlan] = []
        for family in family_order:
            product_ids = tuple(
                item.product_id for item in selections if item.product_family is family
            )
            metric = self._ranking_metric(question, family)
            plans.append(
                CompoundFamilyPlan(
                    product_family=family,
                    filters=tuple(
                        dict.fromkeys(
                            (
                                *self._identity_filters(family),
                                *_numeric_filters(
                                    self._family_numeric_surface(question, family),
                                    family,
                                ),
                            )
                        )
                    ),
                    metric=metric,
                    direction=SortDirection.DESC,
                    limit=limit,
                    projection_fields=self._projection_fields(question, family),
                    include_product_ids=product_ids,
                    identity_scope_active=True,
                )
            )

        compound_tail = (
            self.compound_engine.compile(
                question,
                request_id,
                hclx_structure=hclx_structure,
            )
            if dated
            else None
        )
        if compound_tail is not None:
            plans.extend(compound_tail.family_plans)
        if not plans:
            return None
        compound_plan = TypedCompoundPlan(
            question_id=request_id,
            question=question,
            interaction_intent=InteractionIntent.SEARCH,
            kind="document_intersection",
            family_plans=tuple(plans),
            hclx_structure_spans=tuple(
                dict.fromkeys(
                    span
                    for span, families, _role in hclx_structure
                    if span in question
                    and set(families).issubset(set(family_order))
                )
            ),
        )
        compound = self.compound_engine.execute(compound_plan)
        documents = self._documents(profiles)
        relation_lines = [
            "공식 문서의 상위 10개 표를 원문 기준일 그대로 검증한 뒤 "
            "제공 DB 상품과 교집합을 계산했습니다."
        ]
        if not selections:
            relation_lines.append("찾으시는 상품은 제공 데이터 내에 존재하지 않습니다.")
        for item in selections:
            holdings = ", ".join(
                f"{holding.name} {holding.weight_pct:g}%" for holding in item.matched_holdings
            )
            relation_lines.append(
                f"- {item.profile.product_name} ({item.product_id}): {holdings}; "
                f"문서 기준일 {item.profile.as_of.isoformat()}"
            )
        relation_lines.append(compound.answer)
        return DocumentQueryExecution(
            compound=compound,
            documents=documents,
            selections=tuple(selections),
            relation_answer="\n".join(relation_lines),
        )

    @staticmethod
    def _entities(
        question: str,
        profiles: tuple[OfficialDocumentProfile, ...],
    ) -> tuple[str, ...]:
        patterns = (
            r"에서\s*(.+?)\s*(?:중\s*하나\s*이상이|모두가)\s*상위\s*10개에",
            r"상위\s*10개\s*표에\s*(.+?)(?:\s*항목)?(?:이|가)\s*나오는",
            r"그\s*표에\s*(.+?)(?:이|가)\s*나오는",
            r"상위\s*10개\s*표에서\s*(.+?)(?:와|과)\s*(.+?)의\s*비중\s*합",
            r"대조해\s*(.+?)\s*종목이\s*상위\s*10개에\s*들어\s*있는",
            r"대조해\s*(.+?)\s*중\s*하나\s*이상이\s*상위\s*10개에\s*들어\s*있는",
            r"대조해\s*(.+?)\s*두\s*종목이\s*모두\s*상위\s*10개에\s*들어\s*있는",
            r"확인해서\s*(.+?)(?:이|가)\s*포함된",
            r"그중\s*(.+?)\s*종목이\s*상위\s*10개에\s*들어\s*있는",
        )
        for pattern in patterns:
            match = re.search(pattern, question)
            if match is not None:
                values = " 및 ".join(value.strip() for value in match.groups() if value)
                return tuple(
                    item.strip()
                    for item in re.split(
                        r"\s+및\s+|\s*,\s*|\s*[·ㆍ]\s*|(?<=[가-힣])(?:와|과)\s+",
                        values,
                    )
                    if item.strip()
                )
        known: list[str] = []
        normalized_question = _normalize_name(question)
        profile_aliases = {
            _normalize_name(alias) for profile in profiles for alias in profile.aliases
        }
        for profile in profiles:
            for holding in profile.holdings:
                if (
                    holding.normalized_name in normalized_question
                    and holding.normalized_name not in profile_aliases
                ):
                    known.append(holding.name)
        if known:
            return tuple(dict.fromkeys(known))
        return ()

    @staticmethod
    def _relation_matches(
        profiles: tuple[OfficialDocumentProfile, ...],
        entities: tuple[str, ...],
        *,
        require_all: bool,
        minimum_sum: float | None,
    ) -> tuple[tuple[OfficialDocumentProfile, tuple[Holding, ...]], ...]:
        normalized = tuple(canonical_relation_entity(item) for item in entities)
        result = []
        for profile in profiles:
            by_name = {item.canonical_name: item for item in profile.holdings}
            matched = tuple(by_name[item] for item in normalized if item in by_name)
            accepted = len(matched) == len(normalized) if require_all else bool(matched)
            if minimum_sum is not None:
                accepted = accepted and sum(item.weight_pct for item in matched) >= minimum_sum
            if accepted:
                result.append((profile, matched))
        return tuple(result)

    def _overlap_matches(
        self,
        question: str,
        profiles: tuple[OfficialDocumentProfile, ...],
    ) -> tuple[tuple[OfficialDocumentProfile, tuple[Holding, ...]], ...]:
        split_candidates = [
            position
            for marker in ("그 공통 기업", "그 공통 종목")
            if (position := question.find(marker)) >= 0
        ]
        if not split_candidates:
            return ()
        split = min(split_candidates)
        seed_text = question[:split]
        candidate_text = question[split:]
        seed = tuple(
            profile
            for profile in profiles
            if self._profile_mentioned_in_text(seed_text, profile)
        )
        candidates = tuple(
            profile
            for profile in profiles
            if self._profile_mentioned_in_text(candidate_text, profile)
        )
        if len(seed) < 2 or not candidates:
            return ()
        common = set(item.canonical_name for item in seed[0].holdings)
        for profile in seed[1:]:
            common &= {item.canonical_name for item in profile.holdings}
        result = []
        for profile in candidates:
            matched = tuple(item for item in profile.holdings if item.canonical_name in common)
            if matched:
                result.append((profile, matched))
        return tuple(result)

    @staticmethod
    def _profile_mentioned_in_text(
        text: str,
        profile: OfficialDocumentProfile,
    ) -> bool:
        return any(
            re.search(
                rf"(?<![0-9A-Z]){re.escape(alias)}(?![0-9A-Z])",
                text,
                re.IGNORECASE,
            )
            is not None
            for alias in profile.aliases
            if alias
        )

    def _resolve_product(self, profile_key: str) -> tuple[ProductFamily, str] | None:
        for family, table in _TABLE_BY_FAMILY.items():
            with connect_read_only(self.database_paths[family]) as connection:
                row = connection.execute(
                    f"SELECT product_id, ticker FROM {table} WHERE is_quarantined = 0 AND "
                    "(UPPER(product_id) = UPPER(?) OR UPPER(ticker) = UPPER(?) OR "
                    "UPPER(ticker) = UPPER('A' || ?) OR "
                    "UPPER(ticker) LIKE UPPER(?) || '.%') ORDER BY source_row LIMIT 2",
                    (profile_key, profile_key, profile_key, profile_key),
                ).fetchall()
            if len(row) == 1:
                public_id = (
                    row[0][1]
                    if family is ProductFamily.OVERSEAS_ETP
                    else row[0][0]
                )
                return family, str(public_id)
        return None

    @staticmethod
    def _identity_filters(family: ProductFamily) -> tuple[CompoundFilter, ...]:
        return (CompoundFilter("pd_grp_no", "eq", "ETF"),)

    @staticmethod
    def _family_numeric_surface(question: str, family: ProductFamily) -> str:
        """Keep a family-owned numeric clause from leaking into another family.

        Official-document questions often put different filters beside each
        other (for example overseas fee and domestic tracking error).  A field
        can exist in both schemas, so family capability alone cannot establish
        ownership; the explicit ``<family>는`` clause is the authority boundary.
        """

        label = {
            ProductFamily.DOMESTIC_ETP: r"국내\s*(?:ETF|ETN|ETP)",
            ProductFamily.OVERSEAS_ETP: r"해외\s*(?:ETF|ETN|ETP)",
        }.get(family)
        if label is None:
            return question
        start = re.search(rf"{label}\s*(?:은|는)", question, re.IGNORECASE)
        if start is None:
            return question
        tail = question[start.start() :]
        boundary = re.search(
            r",\s*(?:국내|해외)\s*(?:ETF|ETN|ETP)\s*(?:은|는)|"
            r"(?:조건까지|조건을)\s*적용|만\s*남긴\s*뒤|"
            r"별도\s*비교군|상품군별|국내[·ㆍ/]해외\s*ETF를\s*나눠|"
            r"국내외\s*ETF를\s*나눠",
            tail[start.end() - start.start() :],
            re.IGNORECASE,
        )
        if boundary is None:
            return tail
        relative_start = start.end() - start.start()
        return tail[: relative_start + boundary.start()]

    @staticmethod
    def _ranking_metric(question: str, family: ProductFamily) -> CompoundMetric:
        if "1일 수익률" in question:
            return CompoundMetric("field", ("du_er_1d",))
        if family is ProductFamily.DOMESTIC_ETP:
            return CompoundMetric("field", ("pd_net_tamt",))
        return CompoundMetric("field", ("du_last_aum",))

    @staticmethod
    def _projection_fields(question: str, family: ProductFamily) -> tuple[str, ...]:
        fields: list[str] = []
        if family is ProductFamily.DOMESTIC_ETP:
            if "1개월" in question:
                fields.append("du_er_1m")
            if "3개월" in question:
                fields.append("du_er_3m")
            if "위험" in question:
                fields.append("pd_risk_nm")
        return tuple(fields)

    @staticmethod
    def _limit(question: str, *, dated: bool) -> int:
        if dated:
            return 1
        matches = re.findall(r"(?:최대|상위)\s*(\d+)개", question)
        return int(matches[-1]) if matches else 3

    @staticmethod
    def _documents(
        profiles: tuple[OfficialDocumentProfile, ...],
    ) -> tuple[DocumentEvidence, ...]:
        documents: list[DocumentEvidence] = []
        seen: set[str] = set()
        for profile in profiles:
            if profile.document_id in seen:
                continue
            seen.add(profile.document_id)
            evidence_id = hashlib.sha256(
                f"{profile.document_id}:{profile.document_sha256}".encode()
            ).hexdigest()
            documents.append(
                DocumentEvidence(
                    evidence_id=evidence_id,
                    document_id=profile.document_id,
                    chunk_ordinal=0,
                    title=profile.title,
                    text=_profile_excerpt(profile),
                    source_uri=profile.source_uri,
                    source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
                    as_of=profile.as_of,
                    document_sha256=profile.document_sha256,
                    metadata={
                        "publisher": profile.publisher,
                        "profile_key": profile.profile_key,
                        "evidence_scope": "official_top10_snapshot",
                    },
                    relevance_score=1.0,
                )
            )
        return tuple(documents)


__all__ = [
    "canonical_relation_entity",
    "DocumentQueryExecution",
    "OfficialDocumentRelationIndex",
    "TypedDocumentQueryEngine",
]
