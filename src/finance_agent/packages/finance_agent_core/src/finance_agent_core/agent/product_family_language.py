from __future__ import annotations

import re

from finance_agent_core.contracts.queryplan import ProductFamily

ETP_KIND_PATTERN = r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))"
BOND_KIND_PATTERN = (
    r"(?:국내\s*채권|국내채권|회사채|일반사채|국채|국공채|국고채|특수채|"
    r"특수은행채|금융채|은행채|신용카드채|시설대여채|MBS|지역개발채|"
    r"도시철도공채|원화\s*채권|채권)"
)

# Market and product must be joined by noun-like modifiers, not an arbitrary
# character window.  This admits unseen themes (``국내 양자컴퓨팅 ETF``) but
# rejects relation clauses (``국내 주식을 추종하는 미국 ETF``), where 국내
# describes the underlying asset rather than the listing market.
_MARKET_MODIFIER_TOKEN_PATTERN = (
    r"(?!(?:국내|해외|및|와|과)(?=\s|$))"
    r"(?!(?:[^\s]*(?:을|를|에|에게|에서|로|으로)|"
    r"추종[^\s]*|투자[^\s]*|편입[^\s]*|보유[^\s]*|담[^\s]*|"
    r"연결[^\s]*|연관[^\s]*|관련[^\s]*)(?=\s|$))"
    r"[0-9A-Za-z가-힣&+._/-]+"
)
_MARKET_MODIFIER_SEQUENCE_PATTERN = (
    rf"(?:{_MARKET_MODIFIER_TOKEN_PATTERN}\s+){{0,4}}"
)
_MARKET_LISTING_LINK_PATTERN = (
    r"(?:(?:증시|시장)\s*)?에\s*상장(?:된|한|되어\s*있는)\s*"
)
DOMESTIC_ETP_SCOPE_PATTERN = (
    rf"(?:국내\s*{_MARKET_MODIFIER_SEQUENCE_PATTERN}{ETP_KIND_PATTERN}|"
    rf"국내\s*{_MARKET_LISTING_LINK_PATTERN}{ETP_KIND_PATTERN})"
)
OVERSEAS_ETP_SCOPE_PATTERN = (
    rf"(?:해외\s*{_MARKET_MODIFIER_SEQUENCE_PATTERN}{ETP_KIND_PATTERN}|"
    rf"해외\s*{_MARKET_LISTING_LINK_PATTERN}{ETP_KIND_PATTERN})"
)
COMBINED_ETP_SCOPE_PATTERN = (
    rf"(?:(?:국내\s*(?:[·・ㆍ/,]|및|와|과)\s*해외|"
    rf"해외\s*(?:[·・ㆍ/,]|및|와|과)\s*국내)\s*"
    rf"{_MARKET_MODIFIER_SEQUENCE_PATTERN}{ETP_KIND_PATTERN}|"
    rf"{DOMESTIC_ETP_SCOPE_PATTERN}\s*(?:[·・ㆍ/,]|및|와|과)\s*"
    rf"{OVERSEAS_ETP_SCOPE_PATTERN}|"
    rf"{OVERSEAS_ETP_SCOPE_PATTERN}\s*(?:[·・ㆍ/,]|및|와|과)\s*"
    rf"{DOMESTIC_ETP_SCOPE_PATTERN})"
)

_DOMESTIC_ETP_SCOPE = re.compile(DOMESTIC_ETP_SCOPE_PATTERN, re.IGNORECASE)
_OVERSEAS_ETP_SCOPE = re.compile(OVERSEAS_ETP_SCOPE_PATTERN, re.IGNORECASE)
_COMBINED_ETP_SCOPE = re.compile(COMBINED_ETP_SCOPE_PATTERN, re.IGNORECASE)
_STANDALONE_BOND_SCOPE = re.compile(
    rf"{BOND_KIND_PATTERN}"
    rf"(?!\s*(?:(?:형|혼합형|관련|중심|투자|추종|테마)\s*)?{ETP_KIND_PATTERN})",
    re.IGNORECASE,
)
_STANDALONE_PUBLIC_FUND_SCOPE = re.compile(
    r"공모\s*펀드|증권(?:자)?투자신탁|(?<![0-9A-Za-z가-힣])펀드"
    r"(?=(?:의|을|를|은|는|이|가|에서|중|\s|$))",
    re.IGNORECASE,
)


def explicit_etp_market_families(text: str) -> tuple[ProductFamily, ...]:
    """Return markets explicitly attached to an ETP noun in one local clause."""

    families: set[ProductFamily] = set()
    if _COMBINED_ETP_SCOPE.search(text) is not None:
        families.update((ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP))
    if _DOMESTIC_ETP_SCOPE.search(text) is not None:
        families.add(ProductFamily.DOMESTIC_ETP)
    if _OVERSEAS_ETP_SCOPE.search(text) is not None:
        families.add(ProductFamily.OVERSEAS_ETP)
    return tuple(sorted(families, key=lambda item: item.value))


def has_standalone_bond_product_scope(text: str) -> bool:
    """Return whether ``text`` names a bond product, not a bond-themed ETP."""

    return _STANDALONE_BOND_SCOPE.search(text) is not None


def has_standalone_public_fund_scope(text: str) -> bool:
    """Return whether ``text`` names a fund outside an ETP product noun."""

    without_etp_nouns = re.sub(ETP_KIND_PATTERN, " ", text, flags=re.IGNORECASE)
    return _STANDALONE_PUBLIC_FUND_SCOPE.search(without_etp_nouns) is not None


__all__ = [
    "COMBINED_ETP_SCOPE_PATTERN",
    "BOND_KIND_PATTERN",
    "DOMESTIC_ETP_SCOPE_PATTERN",
    "ETP_KIND_PATTERN",
    "OVERSEAS_ETP_SCOPE_PATTERN",
    "explicit_etp_market_families",
    "has_standalone_bond_product_scope",
    "has_standalone_public_fund_scope",
]
