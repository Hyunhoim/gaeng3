from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from finance_agent_core.agent.fund_comparison_parser import (
    SUPPORTED_FUND_COMPARISON_FIELDS,
    FundComparisonDraft,
)
from finance_agent_core.agent.grounded_planning import (
    GroundedPlanProposal,
    build_grounded_plan_system_prompt,
    canonicalize_grounded_plan_proposal_payload,
    grounded_plan_proposal_schema,
)
from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_linked_query_plan,
    canonicalize_query_plan_payload,
)
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts import QueryPlan, load_hcx_queryplan_schema
from finance_agent_core.contracts.hcx_schema import (
    load_internal_evaluation_queryplan_schema,
)
from finance_agent_core.contracts.queryplan import ProductFamily


class LocalProviderError(RuntimeError):
    """Raised when the isolated local provider cannot return a valid plan."""


@dataclass(frozen=True)
class LocalTestSettings:
    base_url: str
    model: str
    timeout_seconds: float = 180.0

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> LocalTestSettings:
        values = os.environ if environment is None else environment
        required = {
            "FINANCE_AGENT_LLM_MODE": "local_test",
            "ENABLE_NON_HCX_TEST_LLM": "1",
            "LLM_PROVIDER": "local_test",
        }
        mismatches = [
            f"{name}={values.get(name)!r}"
            for name, expected in required.items()
            if values.get(name) != expected
        ]
        if mismatches:
            raise LocalProviderError(
                "local provider requires explicit double opt-in: " + ", ".join(mismatches)
            )
        base_url = values.get("LOCAL_TEST_LLM_BASE_URL", "http://127.0.0.1:18000/v1").rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise LocalProviderError(
                "local provider endpoint must be unauthenticated HTTP on loopback"
            )
        model = values.get("LOCAL_TEST_LLM_MODEL", "").strip()
        if not model:
            raise LocalProviderError("LOCAL_TEST_LLM_MODEL is required")
        try:
            timeout = float(values.get("LOCAL_TEST_LLM_TIMEOUT_SECONDS", "180"))
        except ValueError as error:
            raise LocalProviderError("LOCAL_TEST_LLM_TIMEOUT_SECONDS must be numeric") from error
        if timeout <= 0 or timeout > 600:
            raise LocalProviderError("local provider timeout must be in (0, 600]")
        return cls(base_url=base_url, model=model, timeout_seconds=timeout)


def _field_catalog(product_family: str | None = None) -> dict[str, Any]:
    registry = load_field_registry()
    catalog: dict[str, Any] = {}
    for name, definition in registry.fields.items():
        datasets = (
            [product_family]
            if product_family is not None and product_family in definition.datasets
            else definition.datasets
            if product_family is None
            else []
        )
        resolved = [definition.resolve(dataset) for dataset in datasets]
        if not any(item.queryable for item in resolved):
            continue
        effective = resolved[0] if len(resolved) == 1 else definition
        catalog[name] = {
            "label": effective.label,
            "aliases": effective.aliases,
            "type": effective.value_type.value,
            "unit": effective.unit,
            "operators": effective.allowed_operators,
            "enum": effective.enum_values,
            "quality": effective.quality.value,
            "notes": definition.notes,
            "datasets": {
                dataset: {
                    "quality": resolved_definition.quality.value,
                    "queryable": resolved_definition.queryable,
                    "sortable": resolved_definition.sortable,
                }
                for dataset, resolved_definition in zip(datasets, resolved, strict=True)
            },
        }
    return catalog


def build_query_plan_system_prompt(
    question_id: str,
    question: str,
    internal_evaluation_family: Literal["fund"] | None = None,
) -> str:
    catalog = json.dumps(
        _field_catalog(internal_evaluation_family),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    hints = json.dumps(
        build_lexical_hints(question),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if internal_evaluation_family == "fund":
        supported_scope = """
이번 요청은 공식 실행과 분리된 공모펀드 development 평가다.
상품군은 fund 하나만 사용한다. 공식 Agent의 fund 실행은 여전히 비활성 상태다.
"""
        fund_rules = """
- fund는 사용자가 생략해도 public_offering=true를 locked로 정확히 한 번 넣는다.
- fund의 국내·해외·국내외혼합은 fund_geography_scope, 주식형·채권형·재간접·
  MMF 등은 fund_management_attribute를 쓴다.
- fund의 판매 중/완료는 sellable, 당사 판매는 company_sellable,
  환헤지 여부는 currency_hedged, 개인·법인은 investor_type을 쓴다.
- fund는 1주·1개월·3개월·6개월 수익률만 검색·정렬할 수 있다.
- fund의 AUM 조건·정렬에는 KRW 또는 USD trading_currency를 반드시 잠근다.
"""
        fund_safety = """
- fund의 운용사 이름, 비용, 오늘 기준 최신값, 장기 수익률 순위와 대표 펀드
  클래스 합산은 unsupported_conditions로 처리한다.
"""
        fund_projection = """
fund 검색 projection은 product_id, product_name, short_name,
fund_geography_scope, fund_management_attribute, risk_level,
three_month_return_pct, aum, trading_currency, dynamic_as_of를 이 순서로 쓴다.
"""
    else:
        supported_scope = """
현재 지원 상품군은 overseas_etp와 domestic_etp, bond다.
질문의 해외 ETF·ETN·ETP는 overseas_etp, 국내 ETF·ETN·ETP는 domestic_etp,
국내채권·회사채·국공채·국고채·특수채는 bond로 구분한다.
"""
        fund_rules = ""
        fund_safety = (
            "- 공모펀드처럼 아직 실행하지 않는 상품군 요청은 unsupported_conditions에 기록한다."
        )
        fund_projection = ""
    return f"""
당신은 금융상품 검색 질문을 QueryPlan JSON으로만 변환하는 parser다.
계산, 검색, 상품 추천, 답변 문장 생성은 하지 않는다.
question_id는 {question_id!r}를 정확히 사용한다.
{supported_scope}
명시된 조건만 constraints에 넣고 추정·기본 조건을 추가하지 않는다.
특히 판매 가능과 거래 중지 여부는 사용자가 직접 말한 경우에만 넣는다.
모든 명시적 조건은 몰래 완화하지 않고 strength=locked로 둔다.

정규화 규칙:
- ETF/ETN은 각각 product_type eq ETF/ETN이다. "해외 ETP"만 있으면 유형 조건을
  추가하지 않는다. 다른 조건이 미지원이어도 "해외 ETF/ETN" 유형 조건은
  반드시 보존한다.
- 주식형=Equity, 채권형=Bond, 대체자산형=Alternatives,
  혼합자산형=Mixed Assets, 원자재형=Commodity, 머니마켓=Money Market다.
- 미국=United States of America, 일본=Japan, 중국=China, 유럽=Europe,
  글로벌 신흥국=Global Emerging Markets, 미국 제외 글로벌=Global Ex US다.
- NASDAQ=NAS, NYSE=NYS, AMEX=AMX 거래소 코드다.
- 국내 ETP에서는 주식형=주식, 채권형=채권, 원자재형=원자재,
  혼합자산형=혼합자산, 단기자금형=단기자금이다. 미국·중국·일본·유럽·
  글로벌·아시아·인도 등 지역명은 한국어 원천 enum을 그대로 쓴다.
- 국내 ETP의 연금 거래 가능/불가능은 pension_eligible true/false,
  핵심 ETF는 core_etf=true다. 수익률 기간은 one_day_return_pct,
  one_month_return_pct, three_month_return_pct, six_month_return_pct,
  one_year_return_pct, ytd_return_pct 중 정확히 대응하는 field를 쓴다.
- bond의 "매수 가능"과 고객에게 "판매 가능"한 상품은 currently_buyable=true다. 이 필드는
  BUYABLE_QUANTITY가 존재하고 0보다 크며 MAT_DT가 2026-07-11 이후인 경우만
  true인 보수적 파생값이다. 결측 수량을 false나 0으로 추정하지 않는다.
- bond의 회사채·특수채·국공채·개인투자용국채는 bond_major_class,
  국고채는 bond_subclass, 장내·장외는 bond_market을 쓴다.
- bond의 매수수익률·세후수익률·표면이율은 buy_yield_pct,
  after_tax_yield_pct, coupon_rate_pct이고 퍼센트포인트다. 잔존일수는
  remaining_days(day), 듀레이션은 duration_years(year), 매수가능수량은
  buyable_quantity(source_quantity)다.
- bond 신용등급 QueryPlan은 credit_rating exact eq/in만 허용한다. "AA- 이상"처럼
  임계 등급과 방향이 명시된 조건은 서버 linker가 registry의 최고→최저 enum 순서를
  기준으로 in 목록으로 확정한다. 모델이 임의 목록을 만들지 않는다. 임계값 없는
  "등급이 높은" 표현과 bond_risk_code 숫자의 순서는 해석하지 않는다.
{fund_rules}
- "판매 가능"은 sellable=true, "판매 불가"는 sellable=false다.
  "거래 중지 아님/거래 가능"은 trading_suspended=false,
  "거래 중지"는 trading_suspended=true다. "현재 거래 가능"처럼 두 의미가
  함께 명시되면 sellable=true와 trading_suspended=false를 모두 넣는다.
- 이하=lte, 미만=lt, 이상=gte, 초과=gt, 정확히=eq다.
  "A에서 B 사이"는 양끝을 포함하는 between이고, 서로 다른 포함 여부가
  명시되면 두 constraint로 표현한다. "사이"는 gte/lte 두 개로 풀지 말고
  반드시 between 하나를 쓴다.
- 퍼센트는 pct_point다. 0.1%는 0.1이며 0.001로 바꾸지 않는다.
- 금액은 source_currency_amount다. 만=10^4, 억=10^8, 조=10^12로 계산한다.
- 날짜는 YYYY-MM-DD 문자열과 date unit을 사용한다.
- "큰/높은/최신/내림차순/상위" 정렬은 desc,
  "작은/낮은/오름차순" 정렬은 asc다. 모든 ranking의 nulls는 last다.
- 사용자가 정렬을 말하지 않으면 ranking=[]다.
- 정렬 표현은 ranking만 만든다. 값의 범위를 말하지 않았다면 정렬을 위해
  품질 필터용 임의 경계 constraint를 절대 추가하지 않는다. UNKNOWN 제외는
  실행 계층의 품질 규칙이며 QueryPlan에 가짜 범위로 표현하지 않는다.
- 명시한 반환 개수를 limit로 쓴다. 정확한 ID·티커·ISIN·상품명 단건 조회는
  개수가 없으면 limit=1, 그 밖에 개수가 없으면 limit=5다.

안전 규칙:
- 선택한 상품군의 field catalog에 없거나 queryable하지 않은 배당수익률·
  환율 변환 등의 조건은 unsupported_conditions에 기록하고 대체
  constraint를 만들지 않는다. 배당수익률을 총보수율로 바꾸는 것처럼 다른
  field에 끼워 맞추지 않는다.
{fund_safety}
- "적당한", "안전한", "괜찮은"처럼 판단 기준이 없는 표현은 ambiguities에
  기록하고 임의의 수치·정렬로 바꾸지 않는다.
- field나 상품군이 명백히 지원되지 않는 경우는 unsupported만 사용하고 같은
  span을 ambiguity에 중복 기록하지 않는다. 환율 변환과 국내 상품 요청도
  ambiguity가 아니라 unsupported다.
- ambiguity 또는 unsupported가 있어도 명시적으로 지원되는 조건과 정렬은
  모두 보존하되, intent는 search로 둔다. 한 문장에 나온 자산 유형·지역·ETF
  유형을 빠뜨리지 않는다.

overseas_etp 검색 projection은 product_id, product_name, ticker,
total_expense_ratio_pct, aum, trading_currency, dynamic_as_of를 이 순서로 쓴다.
domestic_etp 검색 projection은 product_id, product_name, ticker,
one_month_return_pct, aum, trading_currency, dynamic_as_of를 이 순서로 쓴다.
bond 검색 projection은 product_id, product_name, ticker, issuer, bond_type,
maturity_date, remaining_days, coupon_rate_pct, buy_yield_pct,
buyable_quantity, dynamic_as_of를 이 순서로 쓴다.
{fund_projection}
검색 intent에서는 intent_payload의 네 배열을 모두 빈 배열로 출력한다.
서버의 결정론적 lexical linker가 찾은 아래 힌트는 사용자 문장에서 직접 확인한
항목이다. required_eq_constraints는 빠짐없이 넣고, unsupported_spans는
unsupported_conditions에만, ambiguity_spans는 ambiguities에만 넣는다.
required_rankings가 있으면 같은 순서와 방향으로 ranking에 넣는다.
힌트에 없는 수치·범위·정렬·식별자도 사용자 문장에서 계속 파싱한다.
lexical hints:
{hints}
사용 가능한 field catalog:
{catalog}
JSON 외의 텍스트나 Markdown을 출력하지 않는다.
""".strip()


class LocalTestProvider:
    def __init__(
        self,
        settings: LocalTestSettings,
        *,
        internal_evaluation_family: Literal["fund"] | None = None,
    ) -> None:
        self.settings = settings
        self.internal_evaluation_family = internal_evaluation_family

    @property
    def provider_name(self) -> Literal["local_test"]:
        return "local_test"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def _request_json(self, path: str, payload: dict[str, Any] | None) -> Any:
        url = f"{self.settings.base_url}/{path.lstrip('/')}"
        body = None
        method = "GET"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read(2000).decode("utf-8", errors="replace")
            raise LocalProviderError(f"local provider HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise LocalProviderError(f"local provider request failed: {error}") from error

    def healthcheck(self) -> dict[str, Any]:
        payload = self._request_json("models", None)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise LocalProviderError("local /models response has an unexpected shape")
        model_ids = [item.get("id") for item in payload["data"] if isinstance(item, dict)]
        if self.settings.model not in model_ids:
            raise LocalProviderError(
                f"configured model {self.settings.model!r} is not served: {model_ids}"
            )
        return {"status": "ok", "provider": "local_test", "model": self.settings.model}

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        if not question.strip():
            raise ValueError("question cannot be blank")
        schema = (
            load_internal_evaluation_queryplan_schema(self.internal_evaluation_family)
            if self.internal_evaluation_family is not None
            else load_hcx_queryplan_schema()
        )
        request_payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_query_plan_system_prompt(
                        question_id,
                        question,
                        self.internal_evaluation_family,
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "seed": 42,
            "max_tokens": 4096,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "finance_query_plan",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        response = self._request_json("chat/completions", request_payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LocalProviderError(
                "local chat completion response has an unexpected shape"
            ) from error
        if not isinstance(content, str):
            raise LocalProviderError("local chat completion content is not text")
        try:
            plan_payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise LocalProviderError("local model did not return JSON") from error
        if not isinstance(plan_payload, dict):
            raise LocalProviderError("local model did not return a JSON object")
        plan_payload["question_id"] = question_id
        plan_payload = canonicalize_query_plan_payload(question, plan_payload)
        plan_payload["question_id"] = question_id
        try:
            plan = QueryPlan.model_validate(plan_payload)
        except ValueError as error:
            raise LocalProviderError(
                f"local model returned an invalid QueryPlan: {error}"
            ) from error
        return canonicalize_linked_query_plan(question, plan)

    def generate_grounded_plan(
        self,
        question: str,
        question_id: str,
        product_family_hint: ProductFamily | None = None,
    ) -> GroundedPlanProposal:
        """Return an evidence-span proposal without granting it execution authority."""

        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        if self.internal_evaluation_family == "fund":
            if product_family_hint not in {None, ProductFamily.FUND}:
                raise ValueError("fund provider received a non-fund family hint")
            families = [ProductFamily.FUND]
        elif product_family_hint is not None:
            families = [product_family_hint]
        else:
            families = [
                ProductFamily(name) for name in load_field_registry().executable_dataset_names()
            ]
        catalog_family = families[0].value if len(families) == 1 else None
        payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_grounded_plan_system_prompt(
                        question_id,
                        _field_catalog(catalog_family),
                        families,
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "seed": 43,
            "max_tokens": 4096,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "grounded_finance_plan",
                    "strict": True,
                    "schema": grounded_plan_proposal_schema(families),
                },
            },
        }
        response = self._request_json("chat/completions", payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LocalProviderError(
                "local grounded plan response has an unexpected shape"
            ) from error
        if not isinstance(content, str):
            raise LocalProviderError("local grounded plan response content is not text")
        try:
            raw_proposal = json.loads(content)
            if not isinstance(raw_proposal, dict):
                raise ValueError("grounded proposal must be a JSON object")
            proposal = GroundedPlanProposal.model_validate(
                canonicalize_grounded_plan_proposal_payload(raw_proposal)
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise LocalProviderError(
                f"local model returned an invalid grounded plan: {error}"
            ) from error
        if proposal.question_id != question_id:
            proposal = proposal.model_copy(update={"question_id": question_id})
        return proposal


def build_fund_comparison_draft_system_prompt(question: str) -> str:
    fields = ", ".join(SUPPORTED_FUND_COMPARISON_FIELDS)
    return f"""
당신은 공모펀드 비교 질문에서 두 가지 정보만 추출하는 parser다.
검색하거나 답변하지 말고 JSON 하나만 출력한다.

target_mentions 규칙:
- 질문에 실제로 적힌 비교 대상 표현만 등장 순서대로 복사한다.
- 따옴표 안의 이름은 바깥 따옴표를 제외한 문자열을 그대로 복사한다.
- KR로 시작하는 12자리 상품번호는 그대로 복사한다.
- 띄어쓰기·괄호·클래스 표기·대소문자를 고치거나 새 이름을 만들지 않는다.
- 대상이 하나뿐이면 하나만, 확인할 수 없으면 빈 배열로 둔다.
- 같은 대상을 이름과 상품번호로 두 번 말했어도 두 표현을 모두 보존한다.

comparison_fields 규칙:
- 질문에 명시된 항목만 등장 순서대로 canonical field로 바꾼다.
- 사용할 수 있는 값은 다음 목록뿐이다: {fields}
- 위험등급=risk_level, AUM·순자산·운용자산=aum, 거래통화=trading_currency
- 1주·1개월·3개월·6개월 수익률은 각각 one_week_return_pct,
  one_month_return_pct, three_month_return_pct, six_month_return_pct
- 국내외 구분=fund_geography_scope, 펀드 유형·운용 속성=fund_management_attribute
- 투자 지역=investment_region, 투자자 유형=investor_type
- 환헤지 여부=currency_hedged, 판매 여부=sellable,
  당사·미래에셋 판매 여부=company_sellable
- 정식 상품명=product_name, 짧은 이름·단축 상품명=short_name
- 총보수·판매수수료·장기 수익률·전망·추천처럼 목록에 없는 항목은 넣지 않는다.
- 항목이 없거나 모두 미지원이면 빈 배열로 둔다.

질문:
{question}
""".strip()


def _fund_comparison_draft_schema() -> dict[str, Any]:
    schema = FundComparisonDraft.model_json_schema()
    schema["properties"]["target_mentions"].update(
        {
            "minItems": 0,
            "maxItems": 4,
            "items": {"type": "string"},
        }
    )
    schema["properties"]["comparison_fields"].update(
        {
            "minItems": 0,
            "maxItems": 16,
            "items": {
                "type": "string",
                "enum": list(SUPPORTED_FUND_COMPARISON_FIELDS),
            },
        }
    )
    return schema


class LocalFundComparisonDraftProvider:
    """Development-only Qwen adapter for minimum-privilege comparison parsing."""

    def __init__(self, settings: LocalTestSettings) -> None:
        self.settings = settings
        self._client = LocalTestProvider(settings, internal_evaluation_family="fund")

    @property
    def provider_name(self) -> Literal["local_test"]:
        return "local_test"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def healthcheck(self) -> dict[str, Any]:
        return self._client.healthcheck()

    def generate_comparison_draft(
        self,
        question: str,
        question_id: str,
    ) -> FundComparisonDraft:
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        request_payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_fund_comparison_draft_system_prompt(question),
                },
                {
                    "role": "user",
                    "content": "질문에 실제로 적힌 비교 대상과 비교 항목만 추출해줘.",
                },
            ],
            "temperature": 0,
            "seed": 42,
            "max_tokens": 1024,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "fund_comparison_draft",
                    "strict": True,
                    "schema": _fund_comparison_draft_schema(),
                },
            },
        }
        response = self._client._request_json("chat/completions", request_payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LocalProviderError(
                "local comparison draft response has an unexpected shape"
            ) from error
        if not isinstance(content, str):
            raise LocalProviderError("local comparison draft content is not text")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise LocalProviderError("local model did not return comparison draft JSON") from error
        if not isinstance(payload, dict):
            raise LocalProviderError("local model did not return a JSON object")
        try:
            return FundComparisonDraft.model_validate(payload)
        except ValueError as error:
            raise LocalProviderError(
                f"local model returned an invalid fund comparison draft: {error}"
            ) from error
