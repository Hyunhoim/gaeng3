# Field Registry와 QueryPlan 계약

상태: P1 네 상품군 field 계약 정본
기준일: 2026-07-29

이 문서는 자연어 질문과 결정론적 검색기 사이의 계약을 설명한다. 해외 ETP,
국내 ETP, 국내채권은 실행 가능하다. 공모펀드는 grain·field capability와
정규화 SQLite·oracle·result verifier·field-level evidence, grounded answer와
동결 50문항 계약까지 구현했다. 공모펀드 답변 평가는 동결 expected QueryPlan을
입력으로 사용하는 격리 하네스에서만 허용하며, HCX schema 노출과 서버 계약
테스트를 마칠 때까지 공식 Agent 실행은 fail-closed로 거절한다.

## 1. 구현 파일

| 파일 | 책임 |
| --- | --- |
| [`field_registry.yaml`](../packages/finance_agent_core/src/finance_agent_core/config/field_registry.yaml) | 원천 매핑, 타입, 단위, enum, 연산자, coverage, sentinel, 비교 capability·범위 |
| [`registry.py`](../packages/finance_agent_core/src/finance_agent_core/config/registry.py) | registry 자체의 구조·참조·capability 검증 |
| [`queryplan.py`](../packages/finance_agent_core/src/finance_agent_core/contracts/queryplan.py) | 서버의 엄격한 QueryPlan 구조·의미 검증 |
| [`queryplan.hcx.schema.json`](../packages/finance_agent_core/src/finance_agent_core/contracts/queryplan.hcx.schema.json) | HyperCLOVA X용 보수적 Structured Outputs schema template |
| [`hcx_schema.py`](../packages/finance_agent_core/src/finance_agent_core/contracts/hcx_schema.py) | registry에서 상품군·field enum을 materialize하고 HCX keyword subset 검증 |
| [`hyperclova.py`](../packages/finance_agent_core/src/finance_agent_core/agent/providers/hyperclova.py) | 공식 mode gate, 세 operation의 semantic structured request, transport·오류·token 관측 계약 |
| [`linker.py`](../packages/finance_agent_core/src/finance_agent_core/agent/linker.py) | 질문에 명시된 범주·수치·정렬을 결정론적으로 canonicalize |
| [`policy.py`](../packages/finance_agent_core/src/finance_agent_core/execution/policy.py) | 모호성·미지원 조건·비검색 intent를 SQL 전에 fail-closed 차단 |
| [`answering/models.py`](../packages/finance_agent_core/src/finance_agent_core/answering/models.py) | GroundedAnswerDraft·context·verification·composition 계약 |
| [`answering/providers.py`](../packages/finance_agent_core/src/finance_agent_core/answering/providers.py) | expected·로컬 provider와 evidence-only HyperCLOVA X 답변 provider |
| [`answering/verifier.py`](../packages/finance_agent_core/src/finance_agent_core/answering/verifier.py) | draft와 최종 compiled answer의 결과 순서·evidence·숫자·식별자·기준일·경고 후검증 |
| [`answering/composer.py`](../packages/finance_agent_core/src/finance_agent_core/answering/composer.py) | evidence-only 생성, 검증된 결정론적 core 결합, 실패 시 safe fallback |
| [`evaluation/answer_cli.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/answer_cli.py) | expected QueryPlan 기반 상품군별 답변 격리 회귀 평가 |
| [`fund_resolver.py`](../packages/finance_agent_core/src/finance_agent_core/agent/fund_resolver.py) | 공모 범위의 `itm_no`·정식명·짧은 이름 exact resolution |
| [`fund_comparison_parser.py`](../packages/finance_agent_core/src/finance_agent_core/agent/fund_comparison_parser.py) | 최소권한 자연어 비교 초안과 서버 검증 COMPARE QueryPlan |
| [`product_comparison.py`](../packages/finance_agent_core/src/finance_agent_core/agent/product_comparison.py) | 해외·국내 ETP·국내채권 exact resolver와 비교 field compiler |
| [`identity_cache.py`](../packages/finance_agent_core/src/finance_agent_core/storage/identity_cache.py) | DB 파일 변경을 감지하는 비교용 compact identity snapshot·bounded LRU |
| [`record_cache.py`](../packages/finance_agent_core/src/finance_agent_core/storage/record_cache.py) | 명시적 opt-in 시 사용하는 SEARCH·AGGREGATE Python verifier용 전체 레코드 snapshot·bounded LRU |
| [`sql_schema.py`](../packages/finance_agent_core/src/finance_agent_core/execution/sql_schema.py) | 네 상품군 canonical field와 SQLite 열·scale의 공통 실행 매핑 |
| [`verification_types.py`](../packages/finance_agent_core/src/finance_agent_core/execution/verification_types.py) | 전체·경량 레코드가 함께 만족하는 최소 verifier Protocol |
| [`verifier_projection.py`](../packages/finance_agent_core/src/finance_agent_core/execution/verifier_projection.py) | QueryPlan 조건·정렬·그룹·집계에 필요한 열만 읽는 기본 verifier universe |
| [`comparison.py`](../packages/finance_agent_core/src/finance_agent_core/execution/comparison.py) | 공통 ProductComparison·ComparisonEvidence·ComparisonResultVerifier |
| [`comparison_e2e_runner.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/comparison_e2e_runner.py) | 자연어 비교부터 검증 답변·안전 차단까지 공개 통합 회귀 |
| [`comparison_e2e_cli.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/comparison_e2e_cli.py) | expected·개발 전용 로컬 provider 통합 E2E 실행 |
| [`product_comparison_runner.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/product_comparison_runner.py) | 세 상품군 자연어 비교의 실제 DB·결정론적 답변·Backend 계약 공개 회귀 |
| [`product_comparison_cli.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/product_comparison_cli.py) | DB·manifest hash 검증 후 30문항 공통 비교 실행 |
| [`search_aggregate_benchmark.py`](../packages/finance_agent_core/src/finance_agent_core/evaluation/search_aggregate_benchmark.py) | 새 프로세스 8문항의 결과 지문·지연·RSS 회귀 |

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
- COMPARE field의 `comparable`·`comparison_mode`·`comparison_scope` 검증
- 아직 실행 승인되지 않은 상품군과 필드 거절

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

## 6. 공모펀드 registry의 근거

상세 정본은 [공모펀드 원천 데이터 계약](public-fund-contract.md)이다.

| 항목 | 계약 |
| --- | --- |
| 원천 grain | `(itm_no, prfd_attr_cd)` 한 행, 중복 0 |
| 논리 grain | `itm_no` 한 펀드 클래스, 정상 11,138개 |
| 반복 구조 | 상품별 속성 4~16행, 속성코드 외 필드 충돌 0 |
| 격리 | Excel source row 84,563 한 건 |
| 기본 과제 범위 | 공모 11,115개만 기본 검색, 사모 15개·구분 결측 8개 제외 |
| AUM | 9,290개 존재, 0인 298개는 `UNKNOWN`, 동일 통화 안에서 비교 |
| 위험등급 | 코드 1~6이 있는 8,565개, 코드 기준 canonical label 변환 |
| 단기 수익률 | 1주·1·3·6개월만 조건·정렬·집계 허용 |
| 장기 수익률 | 극단·비정상 값 때문에 18개월·1·2·3·5년은 표시 전용 |
| 기준일 | 필드별 날짜가 없어 파일 스냅샷 2026-07-11만 경고와 함께 표시 |
| 미지원 | 보수, 운용사명, 대표펀드 그룹, 장기 수익률 순위 |

registry에는 공모펀드 원천 매핑과 capability를 포함하고
`fund_products`·`fund_attributes`·`fund_quarantine` SQLite에 적재하되 dataset의
`execution_enabled`를 `false`로 유지한다. 따라서 계약과 저장 결과를 코드로
검사하고 내부 Oracle 회귀를 실행할 수 있지만 HCX schema의 상품군 enum과 공식
Agent 실행에는 아직 노출되지 않는다. QueryPlan 구조 검증과 배포 실행 허용은
서로 다른 안전 경계로 관리한다.

내부 답변 하네스는 공모펀드 검색 결과를 field-level evidence DTO로 변환하고,
로컬 Qwen에는 질문 해석이나 원천 행 대신 사용 가능한 evidence만 전달한다.
생성된 `GroundedAnswerDraft`와 결정론적 core를 결합한 최종 답변을 각각
Answer Verifier가 검사하며, 상품명·식별자·수치·순서·기준일·근거 인용이나
필수 경고가 계약과 다르면 추측 없는 결정론적 답변으로 대체한다. AUM
조건·정렬·집계는 하나의 거래 통화가 `locked` 조건으로 지정된 경우에만 내부
실행한다.

공개 `fund-core-50` 답변 회귀는 expected provider와 로컬 Qwen 모두 50/50을
통과했다. 실행 가능 44문항은 grounded answer, 정책상 차단할 6문항은 blocked로
처리됐고, 로컬 Qwen의 verifier fallback은 0건이었다. 이 수치는 동결 expected
QueryPlan을 직접 재사용한 답변 계층 평가이므로 parser를 다시 실행하거나 blind
질문의 일반화 성능을 측정한 결과는 아니다.

## 7. QueryPlan 1.0

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

### 7.1 공모펀드 내부 COMPARE 계약

공모펀드 true COMPARE는 공식 Agent 실행과 분리된 내부 평가 경로에서 다음
조건을 모두 만족할 때만 허용한다.

- `intent = compare`, `product_families = ["fund"]`
- `public_offering = true` locked 조건
- 서로 다른 정확한 `product_id` 두 개를 하나의 locked `IN` 조건으로 지정
- ranking 없음, limit 2
- 비교 필드를 projection과 `intent_payload.comparison_fields`에 모두 포함
- AUM 비교는 각 레코드의 실제 `trading_currency`도 projection
- 모호성·미지원 조건 없음

Oracle은 parameterized SQL로 두 레코드를 조회하고 Result Verifier가 조건과
반환 집합을 독립 재검사한다. 이후 비교 builder가 사용자의 요청 순서로
레코드와 evidence를 다시 정렬한다. 수치 차이는 `두 번째-첫 번째`로 계산하고,
비수치 필드는 순서를 부여하지 않고 원천값만 대조한다. AUM 통화가 다르거나
값이 없으면 차이를 생성하지 않는다. 상품이 없으면 누락 ID를 표시하고 LLM
호출을 건너뛴다.

이 계약은 `fund-compare-core-20` 내부 회귀에서만 승인한다. 공모펀드
`execution_enabled: false`, 공식 HCX schema와 일반 Agent 실행 차단은
그대로 유지한다.

### 7.2 공모펀드 비교 대상 resolution 계약

자연어 COMPARE 초안은 상품을 직접 선택하지 않고 질문에 적힌 대상 표현과 비교
필드 이름만 복사한다. 서버 resolver가 다음 우선순위의 정확 일치로 ID를 결정한다.

1. `itm_no`
2. 정식 상품명 `product_name`
3. 짧은 이름 `short_name`

Unicode NFKC, 대소문자·공백 차이와 균형 잡힌 바깥쪽 따옴표만 정규화한다.
상품명 내부 괄호·하이픈·대괄호·클래스 표기는 상품 의미를 구분할 수 있어
제거하지 않는다. 공모 범위에서 하나만 일치할 때만 `product_id` locked 조건을
만든다.

- 같은 별칭이 여러 공모펀드에 연결되면 후보 ID·정식명을 제시하고 역질문
- 사모 또는 공모 여부 미확인 상품만 일치하면 범위 밖으로 차단
- 일치하지 않거나 질문에 없는 대상이면 차단
- 질문의 전체 대상 surface·순서와 draft가 다르면 차단
- 두 대상 사이에는 허용된 연결어만 정확히 한 번 두고, 접두·꼬리 구문과
  문장부호는 위치별 허용 문법을 벗어나면 차단
- 제외·대신·포함 역할, 세 번째 대상, 미등록 상품번호가 있으면 차단
- 알려진 identity와 지원 비교 언어를 마스킹한 뒤 질문 전체에 미등록 비인용
  표현이나 허용되지 않은 문장부호가 남으면 차단
- 비어 있거나 닫히지 않았거나 역방향·중첩·줄바꿈이 잘못된 따옴표가 있으면 차단
- 두 표현이 같은 상품으로 연결되거나 대상이 정확히 두 개가 아니면 차단
- 비교 의도·지원 비교 필드가 없거나 미지원 필드가 있으면 차단

LLM이 반환한 비교 필드 목록은 평가 지표로 기록하지만 실행 필드는 서버가
질문에서 다시 추출한다. 따라서 모델이 질문에 없는 필드나 상품을 만들어도
Oracle 조건으로 승격되지 않는다. plan 회귀는 동일 compiler가 만든 기대값이
아닌 동결 case의 schema·의도·범위·identity·projection·limit·blocker
계약으로 검사한다. 이 계약은
`fund-compare-parser-core-24`의 내부 회귀에만 승인하며 공식 fund 실행 상태를
바꾸지 않는다.

### 7.3 공모펀드 공개 COMPARE 통합 E2E 계약

공개 통합 회귀는 같은 `fund-compare-parser-core-24` 질문과
`fund-compare-e2e-core-24` 동결 overlay를 사용해 다음 경계를 한 번에
검사한다.

```text
자연어 질문
→ 최소권한 비교 draft
→ exact resolver
→ 서버 검증 COMPARE QueryPlan
→ Oracle·Result Verifier
→ 요청 순서의 field-level evidence
→ grounded answer draft
→ Answer Verifier·결정론적 fallback
```

실행 가능한 문항은 Oracle 결과가 정확한 두 상품인지 확인한 뒤에만 answer
provider를 호출한다. 모호성·범위 밖 상품·미지원 조건이 있는 문항은 Oracle과
answer provider를 모두 호출하지 않고 결정론적 차단 답변을 반환한다. Answer
Verifier가 상품 순서, 근거 필드, 경고, 숫자·상품 식별자 누출, 투자 조언,
compiled evidence와 기준일을 모두 검사하며 하나라도 실패하면 서버의
결정론적 비교 답변으로 대체한다.

E2E의 QueryPlan 검증은 같은 compiler로 만든 예상값과 비교하지 않는다.
schema·의도·상품군·공모 범위·locked 상품 순서·projection·limit·차단 사유를
독립 계약으로 검사한다. overlay는 실행 16문항의 field status·numeric
delta와 두 상품의 실제 `ComparisonCell.value`·field evidence provenance
fingerprint를 별도로 동결해 조회·비교·근거 회귀를 검출한다. 대상 grounding은 정확한 인용 span
또는 공백 허용 식별자 경계와 질문의 전체 대상 순서를 요구한다. 두 identity
사이의 연결어는 정확히 검사하고 문장부호는 접두·연결·꼬리 위치에 맞는 문법만
허용한다. 더 긴 상품명의 prefix·suffix, 누락된 세 번째 대상, 제외·대신·포함
역할, 미등록 상품번호, identity와 지원 언어를 제외한 질문 전체의 미등록 잔여
표현을 실행하지 않는다. 비어 있거나 닫히지 않았거나 역방향·중첩·줄바꿈이
잘못된 따옴표도 차단한다. parser 예외는 모든 parser 세부 지표에서 실패로 계산한다.

expected·로컬 Qwen 공개 회귀는 각각 24/24다. 로컬 Qwen 호출은 parser 24회와
실행 문항의 answer 16회이며, 실행 16건·안전 차단 8건, grounded answer
16건·fallback 0건이다. parser·resolution·계획·Oracle·차단·답변의 핵심
검증률은 모두 100%다. 이 결과는 공개 문항의 계약 회귀이며 독립 blind E2E나
사람 생성 품질 점수가 아니다. `execution_enabled: false`, 공식 HCX schema
미노출과 일반 Agent 실행 차단은 그대로 유지한다.

### 7.4 공통 fail-closed Router와 서버 compiler

`IntentRouter`는 모델 호출 전에 SEARCH, DETAIL, COMPARE, AGGREGATE,
EXPLAIN, CLARIFY, UNSUPPORTED를 분류하고 상품군과 disposition을 결정한다.
의도 인식과 실행 가능 여부는 분리한다. 예를 들어 국내채권 비교 질문의 의도는
COMPARE지만 capability matrix에 검증된 비교 executor가 없으므로 Oracle을
호출하지 않고 unsupported로 종료한다.

공통 실행 경로:

```text
자연어 질문
→ fail-closed IntentRouter
→ 비실행 MinimalQueryDraft
→ capability matrix
→ 서버 소유 QueryPlan compiler
├─ SEARCH·DETAIL·COMPARE·EXPLAIN
│  → 상품군 SQLite Oracle
│  → 독립 Result Verifier
│  → field-level evidence
│  → 결정론적 renderer 또는 grounded answer
│  → Answer Verifier
│  → 검증 실패 시 결정론적 fallback
└─ AGGREGATE
   → SQLite locked 후보 선택
   → Decimal 기반 함수·그룹 집계
   → 독립 AggregateResultVerifier 재계산
   → AggregateEvidence
   → 결정론적 aggregate renderer
```

DETAIL과 EXPLAIN은 정확한 상품번호·종목코드가 서버 linker에서 locked equality
조건으로 다시 확인될 때만 SEARCH QueryPlan으로 낮춘다. Router가 문자열을
식별자처럼 보았더라도 compiler가 정확한 field constraint로 연결하지 못하면
역질문으로 종료한다. 공모펀드 COMPARE는 정확한 두 `itm_no`와 지원 field를
기존 resolver·비교 compiler에서 다시 검증한다.

공모펀드는 capability가 구현돼 있어도 공식 registry의
`execution_enabled: false`를 유지한다. 공통 서비스에서 내부 평가 flag를
명시한 경우에만 기존 internal evaluation policy로 실행할 수 있다.

## 8. 검증

`finance_agent/` 디렉터리에서 실행한다.

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
- draft 및 compiled answer가 상품명·수치·순서·기준일·근거를 바꾸거나 누락하는 변경
- Answer Verifier 실패 시 결정론적 fallback을 거치지 않는 변경
- 차단된 자연어 COMPARE가 Oracle 또는 grounded answer provider를 호출하는 변경
- COMPARE parser와 답변 계층을 각각 통과하지만 통합 경계에서 순서·근거가
  달라지는 변경
- AGGREGATE에 ranking, 중복 metric, projection 밖 group·metric field를 넣는 변경
- 숫자·날짜·identity group 또는 허용되지 않은 `SUM`을 실행하는 변경
- 통화 범위를 잠그지 않은 금액 집계와 결측을 0으로 대체하는 변경
- AggregateResultVerifier가 후보 수·그룹·값·유효/제외 개수 변조를 놓치는 변경
- 공통 COMPARE가 요청 상품·필드 순서, field status·delta 또는 Backend
  comparison citation을 바꾸는 변경

## 9. 연결 상태와 다음 순서

완료:

1. 원천 key와 원천값을 보존하는 네 상품군 정규화 레코드
2. exact decimal을 integer scale로 저장하는 네 상품군별 SQLite 적재와 manifest
3. registry 기반 parameterized predicate·sort를 만드는 oracle
4. SQL과 독립된 Python result verifier
5. field-level evidence DTO와 결정론적 safe renderer
6. 동일 QueryPlan을 사용하는 Mock 및 개발 전용 로컬 provider
7. 상품군별 동결 50문항의 parser·oracle·안전 차단 평가 하네스
8. 최소권한 grounded answer 계약, draft·compiled Answer Verifier, 결정론적 폴백과 답변 평가 하네스
9. 공모펀드 공모 범위 잠금, parameterized oracle, 독립 result verifier, field-level evidence
10. `answer_cli --dataset fund` expected·로컬 Qwen 공개 50문항 50/50 회귀
11. 공모펀드 true COMPARE 선택·요청 순서·차이·통화·결측·근거·fallback과
    공개 20문항 expected·로컬 Qwen 20/20 회귀
12. 공모펀드 정식명·짧은 이름·상품번호 exact resolver, 자연어 COMPARE parser와
    공개 24문항 expected·로컬 Qwen 24/24 회귀
13. 공개 24문항의 자연어 COMPARE parser→resolver→Oracle·Verifier→field
    evidence→grounded answer→Answer Verifier·fallback 통합 E2E 24/24 회귀
14. 네 상품군·일곱 intent Router, capability matrix, 서버 compiler와 공통
    Oracle 실행 경로
15. 네 상품군 COUNT·MIN·MAX·AVG·허용 SUM과 최대 두 범주 group의
    Decimal 집계, 통화 gate, 독립 AggregateResultVerifier, AggregateEvidence
16. BM25/SQLite FTS 문서 RAG 최소 기능, 사람 평가 rubric, Backend DTO·JSON 예시

다음:

1. 금융 도메인 담당자가 external blind 100문항과 비공개 정답키를 작성한다.
2. 독립 blind 질문에서 parser부터 답변까지 전체 경로를 별도로 평가한다.
3. 승인된 실제 문서 corpus를 적재하고 출처·활용 범위를 검수한다.
4. 최소 두 명의 reviewer가 사람 평가 rubric을 실제로 수행한다.
5. HCX schema에 fund를 노출하고 공식 HyperCLOVA X provider에서 같은 fixture를 재사용한다.
6. 공식 `/answer` adapter와 오류·timeout 계약을 연결한다.

다른 상품군을 추가할 때는 HCX enum만 늘리지 않는다. 데이터 감사, logical grain, sentinel, 단위, 기준일, field capability, 계약 테스트를 함께 추가해야 한다.
