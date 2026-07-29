# Field Registry와 QueryPlan 계약

상태: P1 해외·국내 ETP·국내채권 계약 정본
기준일: 2026-07-29

이 문서는 자연어 질문과 결정론적 검색기 사이의 계약을 설명한다. 현재 동결
범위는 해외 ETP, 국내 ETP와 국내채권이며, 공모펀드는 동일한 감사·검증 절차를
거치기 전까지 fail-closed로 거절한다.

## 1. 구현 파일

| 파일 | 책임 |
| --- | --- |
| [`field_registry.yaml`](../packages/finance_agent_core/src/finance_agent_core/config/field_registry.yaml) | 원천 매핑, 타입, 단위, enum, 연산자, coverage, sentinel, 비교 범위 |
| [`registry.py`](../packages/finance_agent_core/src/finance_agent_core/config/registry.py) | registry 자체의 구조·참조·capability 검증 |
| [`queryplan.py`](../packages/finance_agent_core/src/finance_agent_core/contracts/queryplan.py) | 서버의 엄격한 QueryPlan 구조·의미 검증 |
| [`queryplan.hcx.schema.json`](../packages/finance_agent_core/src/finance_agent_core/contracts/queryplan.hcx.schema.json) | HyperCLOVA X용 보수적 Structured Outputs schema template |
| [`hcx_schema.py`](../packages/finance_agent_core/src/finance_agent_core/contracts/hcx_schema.py) | registry에서 상품군·field enum을 materialize하고 HCX keyword subset 검증 |
| [`linker.py`](../packages/finance_agent_core/src/finance_agent_core/agent/linker.py) | 질문에 명시된 범주·수치·정렬을 결정론적으로 canonicalize |
| [`policy.py`](../packages/finance_agent_core/src/finance_agent_core/execution/policy.py) | 모호성·미지원 조건·비검색 intent를 SQL 전에 fail-closed 차단 |
| [`answering/models.py`](../packages/finance_agent_core/src/finance_agent_core/answering/models.py) | GroundedAnswerDraft·context·verification·composition 계약 |
| [`answering/verifier.py`](../packages/finance_agent_core/src/finance_agent_core/answering/verifier.py) | 결과 순서·evidence·숫자·식별자·투자 해석·경고 후검증 |

## 2. 두 스키마를 분리하는 이유

HyperCLOVA X 공식 Structured Outputs 문서 기준으로 현재 이 기능은 HCX-007에서만 사용할 수 있다. 문서에 열거된 검증 키워드는 다음 범위다.

- 공통 타입: `string`, `number`, `boolean`, `integer`, `object`, `array`
- 문자열: `format`
- 숫자: `minimum`, `maximum`
- 배열: `minItems`, `maxItems`, `items`
- 객체: `properties`, `required`
- 열거·조합: `enum`, `anyOf`

`pattern`은 지원하지 않는다고 명시되어 있고, Structured Outputs는 thinking 또는 function calling과 동시에 사용할 수 없다. 자세한 현재 사양은 [NAVER Cloud HyperCLOVA X Structured Outputs](https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3-so)를 따른다.

그래서 HCX 전송 스키마에는 `additionalProperties`, `const`, `minLength`, `maxLength`, `uniqueItems`, `pattern`, `$ref`, `oneOf`, nullable union을 넣지 않는다. 대신 서버 Pydantic 모델이 다음 엄격성을 복구한다.

- 알려지지 않은 property 거절
- 길이·개수·limit 검증
- intent별 payload 검증
- field별 타입·단위·enum·허용 연산자 검증
- filter·sort·projection·aggregation capability 검증
- 아직 동결되지 않은 상품군과 필드 거절

## 3. 해외 ETP registry의 근거

registry는 감사 산출물 `artifacts/data-audit/audit_overseas_etp.json`과 원천 schema를 근거로 한다.

| 항목 | 계약 |
| --- | --- |
| 논리 grain | `(pd_exg_mkt_cd, pd_itm_no)` 한 종목 |
| 행 수 | 5,646 |
| 상품 유형 | ETF 5,587, ETN 59 |
| sparse 행 | 10행; 핵심 상태 필드 결측을 그대로 보존 |
| ISIN | 9행 결측, 중복 행 50개; PK 사용 금지 |
| 총보수 | 100% 존재하지만 0인 363행은 의미 확인 전 `UNKNOWN` |
| AUM | 5,459행 존재, 187행 결측, 8행 0; 동일 거래통화 범위에서 비교 |
| 거래 통화 | 5,646행 모두 USD |
| 1일 수익률 | 존재하는 5,388행이 모두 0; `INVALID`로 실행·표시 금지 |
| 정적 속성 기준일 | 전 행 `2026-06-14` |
| 동적 지표 기준일 | 결측은 없으나 88개 날짜가 섞임; 행별 기준일을 evidence에 포함 |

`sellable`과 `trading_suspended`의 코드 매핑은 필드명과 관측값에 근거한 잠정 계약이다. 각각 10행이 결측이고 공식 코드북이 없으므로 `PARTIAL`로 두며, 8월 6일 설명회 답변에 따라 갱신한다.

## 4. 국내 ETP registry의 근거

registry는 `artifacts/data-audit/audit_domestic_etp.json`, 원천 schema와 직접
profiling 결과를 근거로 한다.

| 항목 | 계약 |
| --- | --- |
| 논리 grain | `pd_itm_no` 한 종목 |
| 행 수 | 1,734 |
| 격리 | Excel 1155행의 key가 `KR`만 남은 열 이동 손상; 복구 없이 격리 |
| 상품 유형 | 검색 가능 행에서 ETF 1,201, ETN 532 |
| 자산·지역 | 격리 행 제외 100% 존재; 한국어 원천 enum 보존 |
| 총보수 | 217행만 제공, 0인 150행은 `UNKNOWN`, 양수 67행만 수치 실행 |
| AUM | 1,453행 제공; 결측 280행과 0인 411행은 `UNKNOWN` |
| 기초지수 | 58행, 3.3468%; 일치 검색은 허용하지만 결측을 지수 부재로 해석 금지 |
| 수익률 | 1D·1M·3M·6M·1Y·YTD별 결측은 행 수준 `UNKNOWN` |
| 거래 통화 | 1,732행 KRW, `CURR_CD_000` 1행은 품질 `UNKNOWN` |

`pd_tr_yn`은 원천 schema에서 상품거래정지여부로 설명되어 있고 정상 행의
0/1 분포도 상태 조합과 일관된다. 그래도 공식 코드북 전까지 `0=false`,
`1=true`를 `PARTIAL` 잠정 매핑으로 유지한다.

canonical field는 이름·타입·단위를 공유하되 `dataset_overrides`로 상품군별
원천 column, coverage, 품질, 실행 capability를 해석한다. 예를 들어
`one_day_return_pct`는 해외 ETP에서는 전부 0이라 `INVALID`, 국내 ETP에서는
실측 변동값이 있어 `PARTIAL`이며 행 수준 품질을 통과한 값만 실행한다.

## 5. 국내채권 registry의 근거

| 항목 | 계약 |
| --- | --- |
| 논리 grain | `PD_NO` 한 채권 |
| 행 수 | 42,394; 전 행 검색 가능, 격리 0 |
| 실제 매수 가능 | 수량 존재·양수·스냅샷 기준 미만기 조건을 모두 만족한 254행 |
| 동적 값 | 수량·매수수익률·세후수익률은 881행, 모두 2026-02-24 기준 |
| 잔존일수 | 원천값을 쓰지 않고 유효한 `MAT_DT`와 2026-07-11의 차이로 재계산 |
| 날짜 | 발행일 42,055행, 만기일 42,075행; 결측과 0 sentinel은 `UNKNOWN` |
| 신용등급 | 24,750행, 58.3809%; 정확값·목록 일치만 지원 |
| 위험코드 | 공식 코드북 전까지 숫자의 위험 순서를 해석하지 않고 정확값만 지원 |

동적 값은 파일 스냅샷보다 137일 오래되었으므로 행 수준 `PARTIAL`과 경고를
유지한다. `AA- 이상` 같은 신용등급 순서 조건, 미래 수익 예측, 안전성 판단은
실행하지 않는다.

## 6. QueryPlan 1.0

필수 최상위 필드는 다음과 같다.

- `schema_version`, `question_id`, `intent`, `product_families`
- `constraints`: field, operator, typed value, unit, strength
- `ranking`, `projection`, `limit`
- `intent_payload`: compare·aggregate·explain 전용 자료
- `ambiguities`, `unsupported_conditions`

조건 강도:

- `locked`: 몰래 완화할 수 없는 필수 조건
- `ask_before_relaxing`: 결과가 없을 때 사용자 확인 후에만 완화
- `preference`: 결과 순위에 반영할 수 있는 선호

첫 vertical slice의 유효한 계획 예:

```json
{
  "schema_version": "1.0",
  "question_id": "overseas-etp-001",
  "intent": "search",
  "product_families": ["overseas_etp"],
  "constraints": [
    {"field": "product_type", "operator": "eq", "value": "ETF", "unit": "code", "strength": "locked"},
    {"field": "investment_region", "operator": "eq", "value": "United States of America", "unit": "code", "strength": "locked"},
    {"field": "asset_type", "operator": "eq", "value": "Bond", "unit": "code", "strength": "locked"},
    {"field": "sellable", "operator": "eq", "value": true, "unit": "boolean", "strength": "locked"},
    {"field": "trading_suspended", "operator": "eq", "value": false, "unit": "boolean", "strength": "locked"},
    {"field": "total_expense_ratio_pct", "operator": "lte", "value": 0.2, "unit": "pct_point", "strength": "locked"}
  ],
  "ranking": [
    {"field": "aum", "direction": "desc", "nulls": "last"}
  ],
  "projection": [
    "product_id",
    "product_name",
    "ticker",
    "total_expense_ratio_pct",
    "aum",
    "dynamic_as_of"
  ],
  "limit": 5,
  "intent_payload": {
    "comparison_fields": [],
    "group_by": [],
    "aggregations": [],
    "explain_product_ids": []
  },
  "ambiguities": [],
  "unsupported_conditions": []
}
```

이 계획이 유효하다는 것은 데이터가 실제로 조건을 만족한다는 뜻이 아니다. 다음 계층에서 정규화된 행에 적용하고, 보수 0의 `UNKNOWN`, 결측, sparse 행을 제외·경고한 뒤 독립 verifier가 반환 결과를 재검사해야 한다.

QueryPlan은 현재 한 번에 한 상품군만 실행한다. schema에는 세 상품군이
있지만 교차 상품군 검색·비교는 별도 통화·날짜·field 정합성 계약 전까지
oracle이 거절한다.

## 7. 검증

저장소 루트에서 실행한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pytest
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/ruff check .
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip check
```

계약 테스트는 다음 회귀를 막는다.

- registry의 감사 수치·품질 상태 변경
- `INVALID` 필드를 filter·sort·projection에 사용
- enum 밖 값, 잘못된 단위, field에 허용되지 않은 연산자
- 검색 intent에 비교·집계 payload를 섞는 행위
- 서버 전용 JSON Schema keyword를 HCX schema에 넣는 행위
- registry field 목록과 HCX enum이 어긋나는 변경

## 8. 연결 상태와 다음 순서

완료:

1. 원천 key와 원천값을 보존하는 해외·국내 ETP·국내채권 정규화 레코드
2. exact decimal을 integer scale로 저장하는 상품군별 SQLite 적재와 manifest
3. registry 기반 parameterized predicate·sort를 만드는 oracle
4. SQL과 독립된 Python result verifier
5. field-level evidence DTO와 결정론적 safe renderer
6. 동일 QueryPlan을 사용하는 Mock 및 개발 전용 로컬 provider
7. 상품군별 동결 50문항의 parser·oracle·안전 차단 평가 하네스
8. 최소권한 grounded answer 계약, Answer Verifier, 결정론적 폴백과 답변 평가 하네스

다음:

1. 다른 작성자가 만든 blind 표현 변형 세트를 최소 100문항으로 만든다.
2. 사람이 명확성·중복·비교 용이성을 평가하는 답변 rubric을 추가한다.
3. 공모펀드를 감사→product-grain 정규화→registry→oracle→verifier 순서로 확장한다.
4. 공식 HyperCLOVA X provider에서 동일 계약 fixture를 재사용한다.
5. 공식 `/answer` adapter와 오류·timeout 계약을 연결한다.

다른 상품군을 추가할 때는 HCX enum만 늘리지 않는다. 데이터 감사, logical grain, sentinel, 단위, 기준일, field capability, 계약 테스트를 함께 추가해야 한다.
