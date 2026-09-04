from __future__ import annotations

import re

# These patterns describe how the caller wants an already verified result to be
# displayed.  They do not name another product filter, ranking metric, or data
# source.  Keeping the grammar in one module prevents Router, semantic coverage,
# and Compiler from drifting into three different interpretations.
_PRESENTATION_ONLY_SENTENCE = re.compile(
    r"(?:^|(?<=[.!?])\s+)"
    r"(?:"
    r"검색\s*결과가\s*맞는지\s*확인할\s*수\s*있게\s*정리|"
    r"조회\s*결과를\s*숫자까지\s*비교해\s*보고\s*싶|"
    r"상담받기\s*전에\s*후보를\s*직접\s*대조해\s*보려\s*하|"
    r"후보\s*범위를\s*먼저\s*파악한\s*뒤\s*상위\s*상품을\s*보고\s*싶|"
    r"상품\s*번호까지\s*확인\s*가능한\s*형태로\s*조회|"
    r"이\s*조건을\s*만족하는\s*상품이\s*얼마나\s*되는지\s*함께\s*알고\s*싶|"
    r"몇\s*건이\s*후보였는지\s*먼저|"
    r"상품명만\s*(?:나열|말|쓰)하지\s*말고|"
    r"(?:상품명|짧은\s*이름)만\s*쓰지\s*말고\s*(?:원천\s*)?상품\s*식별자|"
    r"결과마다\s*(?:원천\s*)?상품\s*번호|"
    r"조건을\s*만족한\s*건수와\s*상위\s*상품의\s*(?:식별자|ID)|"
    r"검색된\s*(?:총\s*)?후보\s*수를|"
    r"각\s*항목에\s*상품\s*식별자|"
    r"반환\s*순서를\s*검산할\s*수\s*있도록|"
    r"상품군\s*사이의\s*단위를\s*억지로\s*맞추지\s*말고|"
    r"서로\s*다른\s*지표를\s*(?:직접\s*)?(?:합산하지|합치지)\s*말고|"
    r"단위가\s*다른\s*값은\s*직접\s*(?:합산하지|합치지)\s*말고|"
    r"서로\s*다른\s*(?:수치의\s*)?단위를\s*답변에서\s*분명히\s*구분|"
    r"서로\s*다른\s*기간이나\s*금리를\s*하나의\s*숫자로\s*환산하지\s*말|"
    r"순위는\s*상품군마다\s*따로\s*유지하고|"
    r"정확\s*일치가\s*없으면\s*비슷한\s*상품으로\s*바꾸지\s*말고|"
    r"결과\s*뒤에\s*후보가\s*총\s*몇\s*개인지|"
    r"각\s*조건은\s*상품군별로\s*따로\s*적용|"
    r"각\s*상품군(?:의|에서)?\s*(?:후보\s*수와\s*반환\s*수|결과)를|"
    r"상품군별\s*후보\s*수와\s*요청한\s*목록을|"
    r"조건을\s*통과한\s*상품이\s*몇\s*개인지도\s*상품군마다|"
    r"각\s*상품군에서\s*요청한\s*상위\s*개수를\s*정확히\s*맞춰|"
    r"요청한\s*개수보다\s*임의로\s*더\s*많은\s*상품을\s*붙이지\s*말|"
    r"상품군\s*이름을\s*생략하지\s*말고|"
    r"결과에는\s*어느\s*상품군에서\s*나온\s*상품인지|"
    r"결과는\s*표보다\s*상품군별\s*짧은\s*목록으로|"
    r"각\s*목록의\s*첫\s*상품이\s*왜\s*먼저인지|"
    r"조건과\s*정렬은\s*각각\s*유지한\s*채\s*한\s*답변에\s*묶어|"
    r"어떤\s*정렬\s*기준을\s*적용했는지\s*상품군마다|"
    r"필터에\s*사용한\s*조건을\s*목록\s*위에|"
    r"동률이\s*있으면\s*상품번호\s*오름차순으로|"
    r"상위\s*목록과\s*필터\s*통과\s*건수를\s*함께|"
    r"결측값이나\s*0을\s*순위값으로\s*사용하지|"
    r"순위값의\s*방향이\s*높은\s*순인지\s*낮은\s*순인지|"
    r"공백\s*[·,/&]\s*대소문자\s*외에는\s*임의\s*별칭을\s*만들어\s*대체하지\s*말|"
    r"국내채권\s*,\s*국내\s*ETF\s*,\s*해외\s*ETF\s*,\s*공모펀드\s*순서"
    r")"
    r"[^.!?]*(?:[.!?]|$)",
    re.IGNORECASE,
)


# A result-projection list is sometimes appended to the actual query in the
# same sentence (for example, "... TOP 5를 만들고 ID·실제 값·후보 수를
# 밝혀 주세요").  It is presentation-only only when at least one identity
# projection and one audit/value projection occur together before a display
# verb.  A genuine exact lookup such as "상품 ID KR..." therefore cannot match.
_INLINE_RESULT_PROJECTION = re.compile(
    r"(?:상품\s*종류\s*[·,/&]\s*)?"
    r"(?:원천\s*)?(?:상품\s*)?(?:ID|식별자|상품\s*번호)"
    r"(?=[^.!?]{0,120}?(?:후보\s*수|원천\s*값|원천값|실제\s*(?:값|수익률)|"
    r"기준\s*값|근거\s*값|필터\s*[·,/&]\s*정렬\s*값))"
    r"[^.!?]{0,160}?(?:표시|밝혀|적어|제시|알려|보여|정리)(?:해\s*)?"
    r"(?:주세요|줘|주십시오|달라)?",
    re.IGNORECASE,
)


def mask_response_presentation(question: str) -> str:
    """Blank audited response-format spans while preserving character offsets."""

    masked = list(question)
    for pattern in (_PRESENTATION_ONLY_SENTENCE, _INLINE_RESULT_PROJECTION):
        snapshot = "".join(masked)
        for match in pattern.finditer(snapshot):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def semantic_query_surface(question: str) -> str:
    """Return the query semantics without audited answer-presentation prose."""

    return " ".join(mask_response_presentation(question).split())


__all__ = ["mask_response_presentation", "semantic_query_surface"]
