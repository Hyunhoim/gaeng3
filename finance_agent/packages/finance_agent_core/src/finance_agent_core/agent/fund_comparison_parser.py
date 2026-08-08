from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.agent.fund_resolver import (
    FundMentionResolution,
    FundProductResolver,
    normalize_fund_mention,
    strip_fund_mention_quotes,
)
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan

type FundComparisonField = Literal[
    "product_name",
    "short_name",
    "risk_level",
    "aum",
    "trading_currency",
    "one_week_return_pct",
    "one_month_return_pct",
    "three_month_return_pct",
    "six_month_return_pct",
    "fund_geography_scope",
    "fund_management_attribute",
    "investment_region",
    "investor_type",
    "currency_hedged",
    "sellable",
    "company_sellable",
]

SUPPORTED_FUND_COMPARISON_FIELDS: tuple[FundComparisonField, ...] = (
    "product_name",
    "short_name",
    "risk_level",
    "aum",
    "trading_currency",
    "one_week_return_pct",
    "one_month_return_pct",
    "three_month_return_pct",
    "six_month_return_pct",
    "fund_geography_scope",
    "fund_management_attribute",
    "investment_region",
    "investor_type",
    "currency_hedged",
    "sellable",
    "company_sellable",
)

_FIELD_PATTERNS: tuple[tuple[FundComparisonField, tuple[str, ...]], ...] = (
    (
        "one_week_return_pct",
        (
            r"1\s*주\s*(?:수익률|수익|성과)",
            r"일주일\s*(?:수익률|수익|성과)",
            r"주간\s*수익률",
            r"1W\s*수익률",
        ),
    ),
    (
        "one_month_return_pct",
        (
            r"1\s*개월\s*(?:수익률|수익|성과)",
            r"한\s*달\s*(?:수익률|수익|성과)",
            r"월간\s*수익률",
            r"1M\s*수익률",
        ),
    ),
    (
        "three_month_return_pct",
        (r"3\s*개월\s*(?:수익률|수익|성과)", r"석\s*달\s*(?:수익률|수익|성과)", r"3M\s*수익률"),
    ),
    (
        "six_month_return_pct",
        (r"6\s*개월\s*(?:수익률|수익|성과)", r"반년\s*(?:수익률|수익|성과)", r"6M\s*수익률"),
    ),
    ("company_sellable", (r"당사\s*판매\s*여부", r"미래에셋(?:증권)?\s*판매\s*여부")),
    ("fund_geography_scope", (r"국내외\s*구분", r"해외\s*펀드\s*여부", r"펀드\s*지역\s*구분")),
    ("fund_management_attribute", (r"펀드\s*유형", r"운용\s*속성", r"상품\s*유형")),
    ("investment_region", (r"투자\s*지역", r"투자\s*국가")),
    ("investor_type", (r"투자자\s*유형", r"개인\s*법인\s*구분")),
    ("currency_hedged", (r"환\s*헤지\s*여부", r"환헤지")),
    ("trading_currency", (r"거래\s*통화", r"표시\s*통화", r"통화")),
    ("product_name", (r"정식\s*상품명", r"정확한\s*상품명", r"상품명", r"펀드명", r"정식명")),
    ("short_name", (r"짧은\s*이름", r"단축\s*상품명", r"약어명")),
    ("risk_level", (r"위험\s*등급", r"위험도")),
    ("aum", (r"(?<![A-Z])AUM(?![A-Z])", r"순자산", r"운용\s*자산")),
    ("sellable", (r"판매\s*여부", r"판매\s*상태")),
)

_UNSUPPORTED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"총\s*보수", "총보수"),
    (r"판매\s*수수료", "판매수수료"),
    (r"(?:18\s*개월|1\s*년|2\s*년|3\s*년|5\s*년)\s*(?:수익률|수익|성과)", "장기 수익률"),
    (r"수익률\s*(?:전망|예측)", "수익률 전망"),
    (r"환\s*노출\s*여부", "환 노출 여부"),
    (r"(?:추천|매수할|사야\s*할|가장\s*좋은)", "단정적 투자 추천"),
    (r"클래스(?:를|는)?\s*합", "클래스 합산"),
    (r"대표\s*펀드", "대표 펀드"),
)

_COMPARISON_TRIGGER = re.compile(
    r"비교|대조|차이|나란히|더\s*(?:높|낮|좋|나쁘)\w*",
    flags=re.IGNORECASE,
)
_SUPPORTED_REQUEST_PREAMBLE_PATTERNS = (
    re.compile(r"\A\s*다음\s+요청을\s+처리해\s+주세요\s*:\s*", flags=re.IGNORECASE),
    re.compile(
        r"\A\s*조건을\s+빠짐없이\s+적용해서\s+답해\s*줘\s*\.\s*"
        r"요청\s*:\s*",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\A\s*답변\s+문장은\s+한\s+문단이면\s+됩니다\s*\.\s*"
        r"원래\s+요청\s*:\s*",
        flags=re.IGNORECASE,
    ),
)
_SUPPORTED_RESPONSE_SUFFIX_PATTERNS = (
    re.compile(
        r"(?:\s*[.!?…]\s*)?답변은\s+표\s+형식으로\s+제공해\s+주세요\s*[.!?…]?\s*\Z",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:\s*[.!?…]\s*)?결과는\s+(?:리스트|목록)\s+형식으로\s+제시해\s+주세요"
        r"\s*[.!?…]?\s*\Z",
        flags=re.IGNORECASE,
    ),
)
_REQUEST_SENTENCE_END_PATTERN = re.compile(
    r"(?:해\s*줘|해\s*주세요|알려\s*줘|알려\s*주세요|보여\s*줘|보여\s*주세요|"
    r"말해\s*줘|말해\s*주세요|설명해\s*줘|설명해\s*주세요)\s*[.!?…]?\s*$",
    flags=re.IGNORECASE,
)
_ID_PATTERN = re.compile(r"(?<![A-Z0-9])KR[A-Z0-9]{10}(?![A-Z0-9])", flags=re.IGNORECASE)
_QUOTED_PATTERNS = (
    re.compile(r'"([^"\n]+)"'),
    re.compile(r"'([^'\n]+)'"),
    re.compile(r"“([^”\n]+)”"),
    re.compile(r"‘([^’\n]+)’"),
    re.compile(r"「([^」\n]+)」"),
    re.compile(r"『([^』\n]+)』"),
)
_IDENTITY_CONTINUATION_PUNCTUATION = frozenset("()[]{}._-+/&#")
_TRAILING_PARTICLES = ("와", "과", "의", "을", "를", "은", "는", "이", "가", "도", "만")
_BOUNDARY_PUNCTUATION = frozenset(",.:;!?…，、")
_QUOTE_DELIMITERS = frozenset("\"'`“”‘’「」『』«»《》")
_UNSAFE_TARGET_ROLE_PATTERN = re.compile(
    r"제외(?:하고|한|해)?|빼고|말고|대신|포함",
    flags=re.IGNORECASE,
)
_UNRESOLVED_TARGET_REFERENCE_PATTERN = re.compile(
    r"(?:해당|그|이)\s*(?:상품|펀드)\s*(?=(?:와|과|및|하고|랑|이랑|,))",
    flags=re.IGNORECASE,
)
_PUNCTUATION_PLACEHOLDER_PATTERN = re.compile(
    r"(?<!\w)[?!_.%+\-#&/=:…]+[\]})\"'`“”‘’「」『』«»《》]*\s*"
    r"(?:와|과|및|하고|랑|이랑|그리고|도|을|를|은|는|이|가|의|에|에서|만)"
    r"(?=\s|$|[,.:;!?…，、])",
)
_TARGET_NOUN_TOKENS = frozenset(
    {
        "상품",
        "상품을",
        "상품의",
        "펀드",
        "펀드를",
        "펀드의",
        "공모펀드",
        "공모펀드를",
        "공모펀드의",
    }
)
_TARGET_NOUN_COUNT_MODIFIERS = frozenset({"두", "2개", "2개의"})
_TARGET_CONNECTOR_TOKENS = frozenset(
    {"와", "과", "이랑", "랑", "하고", "및", "대", "그리고", "vs", "versus"}
)
_ALLOWED_TARGET_PREFIX_FORMS = frozenset(
    {
        "",
        "다음",
        "아래",
        "위",
        "혹시",
        "안녕하세요",
        "안녕하세요,",
        "안녕하세요.",
        "비교대상:",
        "비교대상은",
        "비교대상은:",
        "공모펀드",
        "공모펀드:",
        "두상품",
        "두상품을",
        "두펀드",
        "두펀드를",
        "두공모펀드",
        "두공모펀드를",
        "2개상품",
        "2개의상품",
        "2개펀드",
        "2개의펀드",
        "2개공모펀드",
        "2개의공모펀드",
        "다음두상품",
        "다음두상품을",
        "다음두펀드",
        "다음두펀드를",
        "다음두공모펀드",
        "다음두공모펀드를",
        "다음2개상품",
        "다음2개의상품",
        "다음2개펀드",
        "다음2개의펀드",
        "다음2개공모펀드",
        "다음2개의공모펀드",
        "아래두상품",
        "아래두상품을",
        "아래두펀드",
        "아래두펀드를",
        "아래두공모펀드",
        "아래두공모펀드를",
        "위두상품",
        "위두상품을",
        "위두펀드",
        "위두펀드를",
        "위두공모펀드",
        "위두공모펀드를",
    }
)
_ALLOWED_TARGET_TAIL_FORMS = frozenset(
    {
        "",
        "의",
        "을",
        "를",
        "은",
        "는",
        "이",
        "가",
        "도",
        "만",
        "에",
        "에서",
        "각각",
        "각각의",
        "둘의",
        "사이의",
        "간의",
        "두상품의",
        "두펀드의",
        "두공모펀드의",
        "이두상품의",
        "이두펀드의",
        "이두공모펀드의",
        ",두상품의",
        ",두펀드의",
        ",두공모펀드의",
        ",이두상품의",
        ",이두펀드의",
        ",이두공모펀드의",
        ":",
    }
)
_ALLOWED_QUESTION_TOKENS = frozenset(
    {
        "다음",
        "아래",
        "위",
        "두",
        "2개",
        "2개의",
        "상품",
        "상품인",
        "펀드",
        "펀드인",
        "공모펀드",
        "공모펀드인",
        "비교",
        "비교할",
        "대상",
        "대상은",
        "중",
        "중에서",
        "에서",
        "각각",
        "둘",
        "둘의",
        "사이",
        "사이의",
        "간",
        "간의",
        "대",
        "서로",
        "모두",
        "함께",
        "최근",
        "혹시",
        "안녕하세요",
        "이",
        "그",
        "해당",
        "와",
        "과",
        "이랑",
        "랑",
        "하고",
        "및",
        "의",
        "을",
        "를",
        "은",
        "는",
        "가",
        "도",
        "만",
        "에",
        "대한",
        "대해",
        "으로",
        "로",
        "그리고",
        "또",
        "포함",
        "포함해",
        "포함하고",
        "포함해서",
        "포함하여",
        "해줘",
        "해주세요",
        "알려줘",
        "알려주세요",
        "보여줘",
        "보여주세요",
        "말해줘",
        "말해주세요",
        "설명해줘",
        "설명해주세요",
        "vs",
        "versus",
        "펀드를",
        "펀드의",
        "상품을",
        "상품의",
        "공모펀드를",
        "공모펀드의",
    }
)
_ALLOWED_QUESTION_PUNCTUATION = frozenset(",.:;!?…，、()[]{}._-+/&#%=:") | _QUOTE_DELIMITERS
_TERMINAL_ONLY_QUESTION_PUNCTUATION = frozenset(".!?…")
_UNSUPPORTED_SUFFIX_PUNCTUATION = frozenset("_%+-#&=;")
_SYMMETRIC_QUOTES = frozenset({'"', "'", "`"})
_ASYMMETRIC_QUOTE_PAIRS = {
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "«": "»",
    "《": "》",
}
_ASYMMETRIC_CLOSING_QUOTES = frozenset(_ASYMMETRIC_QUOTE_PAIRS.values())


class FundComparisonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_mentions: list[str] = Field(max_length=4)
    comparison_fields: list[FundComparisonField] = Field(max_length=16)

    @field_validator("target_mentions")
    @classmethod
    def remove_balanced_quote_delimiters(cls, mentions: list[str]) -> list[str]:
        return [strip_fund_mention_quotes(mention) for mention in mentions]

    @model_validator(mode="after")
    def validate_unique_fields(self) -> FundComparisonDraft:
        if len(self.comparison_fields) != len(set(self.comparison_fields)):
            raise ValueError("comparison_fields must not contain duplicates")
        return self


class FundComparisonDraftProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str | None: ...

    def generate_comparison_draft(
        self,
        question: str,
        question_id: str,
    ) -> FundComparisonDraft: ...


@dataclass(frozen=True)
class CompiledFundComparisonPlan:
    plan: QueryPlan
    resolutions: tuple[FundMentionResolution, ...]
    comparison_fields: tuple[FundComparisonField, ...]
    mentions_grounded: tuple[bool, ...]
    question_identity_resolutions: tuple[FundMentionResolution, ...]
    targets_complete: bool
    target_roles_unambiguous: bool

    @property
    def resolved_product_ids(self) -> tuple[str, ...]:
        return tuple(
            product_id
            for resolution in self.resolutions
            if (product_id := resolution.product_id) is not None
        )


def _mask_mentions(question: str, mentions: list[str]) -> str:
    masked = unicodedata.normalize("NFKC", question)
    for mention in sorted(mentions, key=len, reverse=True):
        normalized = unicodedata.normalize("NFKC", mention)
        characters = [re.escape(character) for character in normalized if not character.isspace()]
        if characters:
            masked = re.sub(
                r"\s*".join(characters),
                " ",
                masked,
                flags=re.IGNORECASE,
            )
    return masked


def extract_fund_comparison_fields(
    question: str,
    mentions: list[str] | None = None,
) -> list[FundComparisonField]:
    masked = _mask_mentions(question, mentions or [])
    matches_by_field: dict[FundComparisonField, list[tuple[int, int]]] = {}
    for field_name, patterns in _FIELD_PATTERNS:
        matches_by_field[field_name] = [
            match.span()
            for pattern in patterns
            for match in re.finditer(pattern, masked, flags=re.IGNORECASE)
        ]
    specific_spans = {
        "sellable": matches_by_field["company_sellable"],
        "product_name": matches_by_field["short_name"],
    }
    matched: list[tuple[int, int, FundComparisonField]] = []
    for field_index, (field_name, _) in enumerate(_FIELD_PATTERNS):
        spans = matches_by_field[field_name]
        if field_name in specific_spans:
            spans = [
                (match_start, match_end)
                for match_start, match_end in spans
                if not any(
                    match_start >= specific_start and match_end <= specific_end
                    for specific_start, specific_end in specific_spans[field_name]
                )
            ]
        starts = [start for start, _ in spans]
        if starts:
            matched.append((min(starts), field_index, field_name))
    return [field_name for _, _, field_name in sorted(matched)]


def _unsupported_spans(question: str, mentions: list[str]) -> list[str]:
    masked = _mask_mentions(question, mentions)
    spans: list[str] = []
    for pattern, label in _UNSUPPORTED_PATTERNS:
        if re.search(pattern, masked, flags=re.IGNORECASE):
            spans.append(label)
    return spans


def _leading_boundary_is_valid(question: str, start: int) -> bool:
    if start == 0:
        return True
    preceding = question[start - 1]
    return not (preceding.isalnum() or preceding in _IDENTITY_CONTINUATION_PUNCTUATION)


def _trailing_boundary_is_valid(question: str, end: int) -> bool:
    if end == len(question):
        return True
    trailing = question[end:]
    first = trailing[0]
    if first.isspace() or first in _BOUNDARY_PUNCTUATION | _QUOTE_DELIMITERS:
        return True
    if first.isalnum() or first in _IDENTITY_CONTINUATION_PUNCTUATION:
        for particle in _TRAILING_PARTICLES:
            if not trailing.startswith(particle):
                continue
            remainder = trailing[len(particle) :]
            return (
                not remainder
                or remainder[0].isspace()
                or remainder[0] in _BOUNDARY_PUNCTUATION | _QUOTE_DELIMITERS
            )
    return False


def _mention_grounded(question: str, mention: str) -> bool:
    normalized_mention = normalize_fund_mention(mention)
    if not normalized_mention:
        return False
    normalized_question = unicodedata.normalize("NFKC", question)
    for pattern in _QUOTED_PATTERNS:
        if any(
            normalize_fund_mention(match.group(1)) == normalized_mention
            for match in pattern.finditer(normalized_question)
        ):
            return True

    display_mention = strip_fund_mention_quotes(unicodedata.normalize("NFKC", mention))
    characters = [re.escape(character) for character in display_mention if not character.isspace()]
    if not characters:
        return False
    mention_pattern = re.compile(
        r"\s*".join(characters),
        flags=re.IGNORECASE,
    )
    quoted_spans = [
        match.span()
        for pattern in _QUOTED_PATTERNS
        for match in pattern.finditer(normalized_question)
    ]
    return any(
        not any(
            match.start() >= quoted_start and match.end() <= quoted_end
            for quoted_start, quoted_end in quoted_spans
        )
        and _leading_boundary_is_valid(normalized_question, match.start())
        and _trailing_boundary_is_valid(normalized_question, match.end())
        for match in mention_pattern.finditer(normalized_question)
    )


def _collapsed_question_with_positions(question: str) -> tuple[str, str, list[int]]:
    normalized_question = unicodedata.normalize("NFKC", question)
    collapsed: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(normalized_question):
        if character.isspace():
            continue
        for folded in character.casefold():
            collapsed.append(folded)
            positions.append(index)
    return normalized_question, "".join(collapsed), positions


def _question_identity_scan(
    question: str,
    resolver: FundProductResolver,
) -> tuple[
    tuple[FundMentionResolution, ...],
    str,
    tuple[tuple[int, int], ...],
]:
    normalized_question, collapsed_question, positions = _collapsed_question_with_positions(
        question
    )
    quoted_spans: list[tuple[int, int]] = []
    candidates: list[tuple[int, int, FundMentionResolution]] = []
    for pattern in _QUOTED_PATTERNS:
        for match in pattern.finditer(normalized_question):
            quoted_spans.append(match.span())
            candidates.append(
                (
                    match.start(1),
                    match.end(1),
                    resolver.resolve(match.group(1)),
                )
            )

    for match in _ID_PATTERN.finditer(normalized_question):
        if any(
            match.start() >= quoted_start and match.end() <= quoted_end
            for quoted_start, quoted_end in quoted_spans
        ):
            continue
        candidates.append(
            (
                match.start(),
                match.end(),
                resolver.resolve(match.group(0)),
            )
        )

    for alias_key in resolver.identity_alias_keys:
        search_start = 0
        while (collapsed_start := collapsed_question.find(alias_key, search_start)) >= 0:
            collapsed_end = collapsed_start + len(alias_key)
            original_start = positions[collapsed_start]
            original_end = positions[collapsed_end - 1] + 1
            search_start = collapsed_start + 1
            if any(
                original_start >= quoted_start and original_end <= quoted_end
                for quoted_start, quoted_end in quoted_spans
            ):
                continue
            if not _leading_boundary_is_valid(normalized_question, original_start):
                continue
            if not _trailing_boundary_is_valid(normalized_question, original_end):
                continue
            candidates.append(
                (
                    original_start,
                    original_end,
                    resolver.resolve(alias_key),
                )
            )

    selected: list[tuple[int, int, FundMentionResolution]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = candidate
        if any(
            start < selected_end and end > selected_start
            for selected_start, selected_end, _ in selected
        ):
            continue
        selected.append(candidate)
    selected.sort()
    masked = list(normalized_question)
    for start, end, _ in selected:
        masked[start:end] = " " * (end - start)
    return (
        tuple(resolution for _, _, resolution in selected),
        "".join(masked),
        tuple((start, end) for start, end, _ in selected),
    )


def _mask_supported_question_language(masked_question: str) -> str:
    masked = list(masked_question)
    patterns = [pattern for _, field_patterns in _FIELD_PATTERNS for pattern in field_patterns]
    patterns.extend(pattern for pattern, _ in _UNSUPPORTED_PATTERNS)
    patterns.append(_COMPARISON_TRIGGER.pattern)
    patterns.append(_REQUEST_SENTENCE_END_PATTERN.pattern)
    for pattern in patterns:
        for match in re.finditer(pattern, masked_question, flags=re.IGNORECASE):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _mask_supported_request_framing(question: str) -> str:
    """Mask only audited presentation framing while preserving character offsets."""
    normalized = unicodedata.normalize("NFKC", question)
    for pattern in _SUPPORTED_REQUEST_PREAMBLE_PATTERNS:
        if match := pattern.match(normalized):
            normalized = " " * match.end() + normalized[match.end() :]
            break
    for pattern in _SUPPORTED_RESPONSE_SUFFIX_PATTERNS:
        if match := pattern.search(normalized):
            normalized = normalized[: match.start()] + " " * (match.end() - match.start())
            break
    return normalized


def _field_first_prefix_supported(raw_prefix: str) -> bool:
    """Accept a narrow `fields 기준으로 공모펀드 ...` target introduction."""
    if not re.search(
        r"(?:을|를)?\s*기준(?:으로)?\s*공모\s*펀드\s*[:：]?\s*\Z",
        raw_prefix,
        flags=re.IGNORECASE,
    ):
        return False
    field_spans = _supported_field_spans(raw_prefix)
    if not field_spans:
        return False
    masked = list(raw_prefix)
    for start, end in field_spans:
        masked[start:end] = " " * (end - start)
    residual = "".join(masked)
    residual = re.sub(
        r"(?:을|를)?\s*기준(?:으로)?\s*공모\s*펀드\s*[:：]?\s*\Z",
        " ",
        residual,
        flags=re.IGNORECASE,
    )
    residual = re.sub(r"(?:\s|와|과|및|,|，|、)+", "", residual)
    return not residual


def _mask_supported_field_first_prefix(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
) -> str:
    if not identity_spans:
        return question
    first_start = identity_spans[0][0]
    if not _field_first_prefix_supported(question[:first_start]):
        return question
    return " " * first_start + question[first_start:]


def _question_has_unrecognized_text(masked_question: str) -> bool:
    if _UNRESOLVED_TARGET_REFERENCE_PATTERN.search(
        masked_question
    ) or _PUNCTUATION_PLACEHOLDER_PATTERN.search(masked_question):
        return True
    residual = _mask_supported_question_language(masked_question)
    tokens = [token.casefold() for token in re.findall(r"[^\W_]+", residual, flags=re.UNICODE)]
    if any(token not in _ALLOWED_QUESTION_TOKENS for token in tokens):
        return True
    for index, token in enumerate(tokens):
        if token not in _TARGET_NOUN_TOKENS:
            continue
        if token == "공모펀드" and index == 0:
            continue
        previous = tokens[index - 1] if index else None
        if previous not in _TARGET_NOUN_COUNT_MODIFIERS:
            return True
    return any(
        not character.isspace()
        and not character.isalnum()
        and character not in _ALLOWED_QUESTION_PUNCTUATION
        for character in residual
    )


def _supported_field_spans(text: str) -> tuple[tuple[int, int], ...]:
    return tuple(
        sorted(
            {
                match.span()
                for _, patterns in _FIELD_PATTERNS
                for pattern in patterns
                for match in re.finditer(pattern, text, flags=re.IGNORECASE)
            }
        )
    )


def _suffix_punctuation_is_supported(suffix: str) -> bool:
    stripped_suffix = suffix.rstrip()
    last_suffix_index = len(stripped_suffix) - 1
    field_spans = _supported_field_spans(suffix)
    for index, character in enumerate(suffix):
        if character in _TERMINAL_ONLY_QUESTION_PUNCTUATION:
            if index == last_suffix_index:
                continue
            before = suffix[:index].rstrip()
            after_offset = index + 1
            if not _REQUEST_SENTENCE_END_PATTERN.search(before) or not any(
                field_start >= after_offset for field_start, _ in field_spans
            ):
                return False
        elif character == ":":
            before = suffix[:index]
            if any(
                not prefix_character.isspace() and prefix_character not in _QUOTE_DELIMITERS
                for prefix_character in before
            ):
                return False
            after_offset = index + 1
            if not any(
                not suffix[after_offset:field_start].strip()
                for field_start, _ in field_spans
                if field_start >= after_offset
            ):
                return False
        elif character == "/":
            directly_after_field = any(
                field_end <= index and not suffix[field_end:index].strip()
                for _, field_end in field_spans
            )
            directly_before_field = any(
                field_start > index and not suffix[index + 1 : field_start].strip()
                for field_start, _ in field_spans
            )
            if not directly_after_field or not directly_before_field:
                return False
        elif character in _UNSUPPORTED_SUFFIX_PUNCTUATION:
            return False
    return True


def _question_target_structure_unambiguous(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
) -> bool:
    if not identity_spans:
        return False
    normalized = unicodedata.normalize("NFKC", question)
    first_start = identity_spans[0][0]
    raw_prefix = normalized[:first_start]
    prefix_tokens = [
        token.casefold() for token in re.findall(r"[^\W_]+", raw_prefix, flags=re.UNICODE)
    ]
    if any(token in _TARGET_CONNECTOR_TOKENS for token in prefix_tokens):
        return False
    if any(token.startswith("포함") for token in prefix_tokens):
        return False
    prefix_form = "".join(
        character.casefold()
        for character in raw_prefix
        if not character.isspace() and character not in _QUOTE_DELIMITERS
    )
    if prefix_form not in _ALLOWED_TARGET_PREFIX_FORMS:
        return False

    connector_pattern = "|".join(
        re.escape(token) for token in sorted(_TARGET_CONNECTOR_TOKENS, key=len, reverse=True)
    )
    for (_, previous_end), (next_start, _) in zip(
        identity_spans,
        identity_spans[1:],
        strict=False,
    ):
        between = "".join(
            character
            for character in normalized[previous_end:next_start]
            if not character.isspace() and character not in _QUOTE_DELIMITERS
        )
        if (
            re.fullmatch(
                rf"[,，、]?(?:{connector_pattern})[,，、]?",
                between,
                flags=re.IGNORECASE,
            )
            is None
        ):
            return False

    second_end = identity_spans[-1][1]
    suffix = normalized[second_end:]
    if not _suffix_punctuation_is_supported(suffix):
        return False

    tail_boundaries = [
        match.start()
        for _, patterns in _FIELD_PATTERNS
        for pattern in patterns
        for match in re.finditer(
            pattern,
            normalized[second_end:],
            flags=re.IGNORECASE,
        )
    ]
    tail_boundaries.extend(
        match.start()
        for pattern, _ in _UNSUPPORTED_PATTERNS
        for match in re.finditer(
            pattern,
            normalized[second_end:],
            flags=re.IGNORECASE,
        )
    )
    trigger = _COMPARISON_TRIGGER.search(normalized, second_end)
    if trigger is not None:
        tail_boundaries.append(trigger.start() - second_end)
    target_tail = normalized[
        second_end : second_end + min(tail_boundaries, default=len(normalized) - second_end)
    ]
    target_tail = re.sub(r"\b최근\s*", "", target_tail)
    tail_form = "".join(
        character.casefold()
        for character in target_tail
        if not character.isspace() and character not in _QUOTE_DELIMITERS
    )
    return tail_form in _ALLOWED_TARGET_TAIL_FORMS


def _identity_placeholder_surface(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
) -> str:
    pieces: list[str] = []
    previous_end = 0
    for start, end in identity_spans:
        pieces.extend((question[previous_end:start], "<ID>"))
        previous_end = end
    pieces.append(question[previous_end:])
    return unicodedata.normalize("NFKC", "".join(pieces))


def _audited_natural_id_comparison_surface(
    question: str,
    identity_spans: tuple[tuple[int, int], ...],
) -> bool:
    """Accept narrow, fully anchored natural-language forms around exactly two IDs."""

    if len(identity_spans) != 2:
        return False
    surface = _identity_placeholder_surface(question, identity_spans)
    field = (
        "(?:" + "|".join(pattern for _, patterns in _FIELD_PATTERNS for pattern in patterns) + ")"
    )
    patterns = (
        re.compile(
            rf"^\s*공모\s*펀드\s*중에서\s*상품\s*(?:ID|아이디|번호)"
            rf"(?:가|는|은|이)?\s*<ID>\s*(?:및|와|과|랑|이랑)\s*<ID>"
            rf"\s*인\s*두\s*상품의\s*(?:상품\s*)?{field}"
            rf"\s*(?:과|와|및|,)\s*(?:상품\s*)?{field}"
            r"\s*(?:을|를)?\s*비교해\s*주세요\s*[.!?]?\s*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*<ID>\s*(?:랑|이랑|와|과)\s*<ID>\s*이\s*두\s*"
            rf"공모\s*펀드\s*중에서\s*어떤\s*게\s*최근\s*{field}"
            rf"(?:이|가)?\s*더\s*(?:높|낮|좋|나쁘)\w*\s*[,，]?\s*"
            rf"{field}(?:은|는|이|가)?\s*어떤지\s*좀\s*알려\s*줘"
            r"\s*[.!?]?\s*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*<ID>\s*(?:vs|versus)\s*<ID>\s*[,，]\s*"
            rf"공모\s*펀드\s*[,，]\s*(?:상품\s*)?{field}\s*[,，]\s*"
            rf"(?:상품\s*)?{field}\s*비교\s*[.!?]?\s*$",
            flags=re.IGNORECASE,
        ),
    )
    return any(pattern.fullmatch(surface) is not None for pattern in patterns)


def _is_word_apostrophe(text: str, index: int, character: str) -> bool:
    if character not in {"'", "’"}:
        return False
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    return (previous.isalnum() and following.isalnum()) or (
        character == "'" and following.isdigit() and not previous.isalnum()
    )


def _target_quotes_well_formed(question: str) -> bool:
    normalized = unicodedata.normalize("NFKC", question)
    expected_closings: list[tuple[str, bool]] = []
    for index, character in enumerate(normalized):
        if expected_closings and character == expected_closings[-1][0]:
            _, has_content = expected_closings.pop()
            if not has_content:
                return False
            continue
        if _is_word_apostrophe(normalized, index, character):
            continue
        if character in _SYMMETRIC_QUOTES:
            if expected_closings:
                return False
            expected_closings.append((character, False))
        elif character in _ASYMMETRIC_QUOTE_PAIRS:
            if expected_closings:
                return False
            expected_closings.append((_ASYMMETRIC_QUOTE_PAIRS[character], False))
        elif character in _ASYMMETRIC_CLOSING_QUOTES:
            return False
        elif expected_closings:
            if character == "\n":
                return False
            if not character.isspace():
                closing, _ = expected_closings[-1]
                expected_closings[-1] = (closing, True)
    return not expected_closings


def _ambiguity(
    span: str,
    reason: str,
    options: list[str],
) -> dict[str, object]:
    return {
        "span": (span or "비교 대상")[:200],
        "reason": reason[:500],
        "options": options[:10] or ["정확한 상품번호(itm_no)를 입력한다"],
    }


def compile_fund_comparison_query_plan(
    *,
    question: str,
    question_id: str,
    draft: FundComparisonDraft,
    resolver: FundProductResolver,
) -> CompiledFundComparisonPlan:
    mentions = draft.target_mentions
    resolutions = tuple(resolver.resolve(mention) for mention in mentions)
    lexical_grounded = tuple(_mention_grounded(question, mention) for mention in mentions)
    validation_question = _mask_supported_request_framing(question)
    (
        question_identities,
        identity_masked_question,
        question_identity_spans,
    ) = _question_identity_scan(
        validation_question,
        resolver,
    )
    validation_question = _mask_supported_field_first_prefix(
        validation_question,
        question_identity_spans,
    )
    (
        question_identities,
        identity_masked_question,
        question_identity_spans,
    ) = _question_identity_scan(
        validation_question,
        resolver,
    )
    target_sequence_complete = tuple(
        resolution.normalized_mention for resolution in question_identities
    ) == tuple(resolution.normalized_mention for resolution in resolutions)
    grounded = tuple(
        lexical_match
        or any(
            resolution.normalized_mention == question_resolution.normalized_mention
            for question_resolution in question_identities
        )
        for lexical_match, resolution in zip(
            lexical_grounded,
            resolutions,
            strict=True,
        )
    )
    strict_surface = _question_target_structure_unambiguous(
        validation_question,
        question_identity_spans,
    ) and not _question_has_unrecognized_text(identity_masked_question)
    audited_surface = _audited_natural_id_comparison_surface(
        validation_question,
        question_identity_spans,
    )
    targets_complete = (
        target_sequence_complete
        and (strict_surface or audited_surface)
        and _target_quotes_well_formed(validation_question)
    )
    target_roles_unambiguous = _UNSAFE_TARGET_ROLE_PATTERN.search(identity_masked_question) is None
    comparison_fields = tuple(extract_fund_comparison_fields(question, mentions))
    ambiguities: list[dict[str, object]] = []

    if not _COMPARISON_TRIGGER.search(_mask_mentions(question, mentions)):
        ambiguities.append(
            _ambiguity(
                "비교 의도",
                "비교·대조·차이처럼 두 상품을 비교하라는 표현이 확인되지 않음",
                ["두 상품을 어떤 항목으로 비교할지 명시한다"],
            )
        )
    if len(mentions) != 2:
        ambiguities.append(
            _ambiguity(
                "비교 대상 수",
                f"비교 대상이 정확히 2개여야 하나 {len(mentions)}개가 추출됨",
                ["비교할 공모펀드 두 개를 따옴표 또는 상품번호로 명시한다"],
            )
        )
    if not targets_complete:
        ambiguities.append(
            _ambiguity(
                "비교 대상 완전성",
                "질문에 명시된 상품 전체와 parser가 선택한 비교 대상이 정확히 일치하지 않음",
                ["비교할 공모펀드 두 개만 정확한 상품명 또는 상품번호로 명시한다"],
            )
        )
    if not target_roles_unambiguous:
        ambiguities.append(
            _ambiguity(
                "비교 대상 역할",
                "제외·대신·포함 표현이 있어 어떤 두 상품을 비교할지 결정론적으로 확정할 수 없음",
                ["대상 역할을 바꾸는 표현 없이 실제 비교할 공모펀드 두 개만 다시 명시한다"],
            )
        )
    for mention, resolution, is_grounded in zip(
        mentions,
        resolutions,
        grounded,
        strict=True,
    ):
        if not is_grounded:
            ambiguities.append(
                _ambiguity(
                    mention,
                    "parser가 질문에 없는 비교 대상을 생성했으므로 사용할 수 없음",
                    ["질문에 적힌 정확한 상품명 또는 상품번호를 사용한다"],
                )
            )
            continue
        if resolution.status == "ambiguous":
            ambiguities.append(
                _ambiguity(
                    mention,
                    "같은 정규화 이름에 여러 공모펀드 클래스가 연결됨",
                    [candidate.option_label for candidate in resolution.candidates],
                )
            )
        elif resolution.status == "out_of_scope":
            ambiguities.append(
                _ambiguity(
                    mention,
                    "제공 데이터에는 있으나 공모펀드 검색 범위에 포함되지 않음",
                    ["공모 여부가 확인되는 다른 상품을 선택한다"],
                )
            )
        elif resolution.status == "not_found":
            ambiguities.append(
                _ambiguity(
                    mention,
                    "제공 데이터의 정확한 상품번호·정식명·짧은 이름과 일치하지 않음",
                    ["정확한 상품번호(itm_no)를 입력한다"],
                )
            )

    resolved_ids = [
        product_id
        for resolution in resolutions
        if (product_id := resolution.product_id) is not None
    ]
    if len(resolved_ids) == 2 and len(set(resolved_ids)) != 2:
        ambiguities.append(
            _ambiguity(
                "비교 대상 중복",
                "두 표현이 같은 공모펀드 클래스에 연결됨",
                ["서로 다른 두 상품을 선택한다"],
            )
        )
    unsupported_spans = _unsupported_spans(question, mentions)
    if not comparison_fields and not unsupported_spans:
        ambiguities.append(
            _ambiguity(
                "비교 항목",
                "지원되는 비교 항목이 질문에 명시되지 않음",
                ["위험등급, 단기 수익률, AUM 등 비교 항목을 명시한다"],
            )
        )

    registry = load_field_registry()
    constraints: list[dict[str, object]] = [
        {
            "field": "public_offering",
            "operator": "eq",
            "value": True,
            "unit": "boolean",
            "strength": "locked",
        }
    ]
    if (
        len(mentions) == 2
        and len(resolved_ids) == 2
        and len(set(resolved_ids)) == 2
        and all(grounded)
        and targets_complete
        and target_roles_unambiguous
        and all(resolution.status == "resolved" for resolution in resolutions)
    ):
        constraints.append(
            {
                "field": "product_id",
                "operator": "in",
                "value": resolved_ids,
                "unit": "code",
                "strength": "locked",
            }
        )
    projection = [
        "product_id",
        "product_name",
        "short_name",
        *comparison_fields,
        *(["trading_currency"] if "aum" in comparison_fields else []),
        "dynamic_as_of",
    ]
    projection = list(dict.fromkeys(projection))
    for field_name in comparison_fields:
        if not registry.require_field(field_name, ["fund"]).selectable:
            raise ValueError(f"fund comparison field is not selectable: {field_name}")
    plan = QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "compare",
            "product_families": ["fund"],
            "constraints": constraints,
            "ranking": [],
            "projection": projection,
            "limit": 2,
            "intent_payload": {
                "comparison_fields": list(comparison_fields),
                "group_by": [],
                "aggregations": [],
                "explain_product_ids": [],
            },
            "ambiguities": ambiguities,
            "unsupported_conditions": [
                {
                    "span": span,
                    "reason": "현재 공모펀드 비교 계약에서 지원하지 않는 항목",
                }
                for span in unsupported_spans
            ],
        }
    )
    return CompiledFundComparisonPlan(
        plan=plan,
        resolutions=resolutions,
        comparison_fields=comparison_fields,
        mentions_grounded=grounded,
        question_identity_resolutions=question_identities,
        targets_complete=targets_complete,
        target_roles_unambiguous=target_roles_unambiguous,
    )


def extract_explicit_fund_comparison_draft(question: str) -> FundComparisonDraft:
    matches: list[tuple[int, str]] = []
    for pattern in _QUOTED_PATTERNS:
        matches.extend(
            (match.start(), match.group(1).strip()) for match in pattern.finditer(question)
        )
    matches.extend((match.start(), match.group(0)) for match in _ID_PATTERN.finditer(question))
    mentions: list[str] = []
    seen: set[str] = set()
    for _, mention in sorted(matches):
        key = normalize_fund_mention(mention)
        if key and key not in seen:
            mentions.append(mention)
            seen.add(key)
    return FundComparisonDraft(
        target_mentions=mentions,
        comparison_fields=extract_fund_comparison_fields(question, mentions),
    )


class RuleFundComparisonDraftProvider:
    @property
    def provider_name(self) -> Literal["mock"]:
        return "mock"

    @property
    def model_name(self) -> None:
        return None

    def generate_comparison_draft(
        self,
        question: str,
        question_id: str,
    ) -> FundComparisonDraft:
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        return extract_explicit_fund_comparison_draft(question)


class ResolvedFundComparisonPlanProvider:
    def __init__(
        self,
        draft_provider: FundComparisonDraftProvider,
        resolver: FundProductResolver,
    ) -> None:
        self.draft_provider = draft_provider
        self.resolver = resolver

    @property
    def provider_name(self) -> str:
        return self.draft_provider.provider_name

    @property
    def model_name(self) -> str | None:
        return self.draft_provider.model_name

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        draft = self.draft_provider.generate_comparison_draft(question, question_id)
        return compile_fund_comparison_query_plan(
            question=question,
            question_id=question_id,
            draft=draft,
            resolver=self.resolver,
        ).plan
