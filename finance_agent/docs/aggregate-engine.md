# 네 상품군 공통 AGGREGATE 엔진

마지막 갱신: 2026-07-30

이 문서는 해외 ETF·ETN, 국내 ETF·ETN, 국내채권, 공모펀드의 개수·통계·
그룹 집계를 같은 계약으로 실행하는 결정론적 엔진의 정본이다.

## 1. 현재 상태

- 네 상품군 모두 `AGGREGATE → QueryPlan → SQLite 후보 선택 → Decimal 집계 →
  독립 Python verifier → AggregateEvidence → 결정론적 답변` 경로 구현
- `COUNT`, `MIN`, `MAX`, `AVG`, `SUM` 지원
- 선택 가능한 범주 필드 최대 두 개의 `group_by` 지원
- 조건 필터는 SEARCH와 같은 locked constraint와 품질 규칙 사용
- 공모펀드는 내부 실행까지 구현했지만 공식 Agent execution flag는 계속 비활성
- 집계 답변은 현재 LLM을 호출하지 않는 결정론적 evidence compiler 사용

```text
사용자 질문
→ Intent Router
→ 서버 Aggregate QueryPlan compiler
→ field registry·통화·함수 정책 검사
→ SQLite에서 locked 조건 후보 선택
→ Decimal 기반 집계
→ 독립 Python AggregateResultVerifier 재계산
→ AggregateEvidence
→ 근거·기준일·유효값 수를 포함한 결정론적 답변
```

## 2. 함수 계약

| 함수 | 허용 대상 | 결측 처리 |
| --- | --- | --- |
| `COUNT` | selectable field | 유효값만 계산, 상품 수 질문은 `product_id` 사용 |
| `MIN` | aggregatable numeric field | 유효값만 계산 |
| `MAX` | aggregatable numeric field | 유효값만 계산 |
| `AVG` | aggregatable numeric field | 유효값만 계산, 소수점 이하 12자리 반올림 |
| `SUM` | 금액 또는 수량처럼 더할 수 있는 field | 유효값만 계산 |

퍼센트포인트·일수·연수·레버리지 배수의 `SUM`은 숫자라는 이유만으로 허용하지
않는다. 이 필드에는 `MIN`, `MAX`, `AVG`만 허용한다.

상위·하위 상품 N개는 AGGREGATE가 아니라 기존 SEARCH의 결정론적 ranking과
limit로 처리한다.

## 3. 상품군별 수치 집계 필드

| 상품군 | 허용된 수치 필드 |
| --- | --- |
| 해외 ETP | 총보수율, AUM |
| 국내 ETP | 총보수율, AUM, 레버리지 배수, 종가, 1일·1·3·6개월·1년·YTD 수익률, 일 거래대금 |
| 국내채권 | 발행잔액, 표면이율, 매수수익률, 세후수익률, 매수가능수량, 잔존일수, 듀레이션 |
| 공모펀드 | AUM, 1주·1·3·6개월·1년 수익률 |

실제 허용 여부는 표가 아니라 `field_registry.yaml`의 상품군별
`aggregatable`, `selectable`, 품질 상태가 정본이다. 공식 산식이나 이상치 의미가
공모펀드 1년 수익률은 원천값을 수정·제거·상한 처리하지 않고 집계하며 결측만
제외하고 이상치 포함 경고를 표시한다. 18개월·2년·3년·5년 수익률은 계속 표시
전용이며 집계하지 않는다.

## 4. 그룹 집계

- 문자열·enum·boolean처럼 이미 범주가 정해진 selectable field만 허용
- 한 질문에서 최대 두 개의 그룹 기준 허용
- 숫자와 날짜는 구간 경계가 명시된 bucketing 계약이 없으므로 차단
- 상품 ID·상품명·티커·ISIN 같은 identity field의 그룹화 차단
- 그룹은 행 수 내림차순, 같은 행 수에서는 canonical 값 순으로 결정론적 정렬
- 전체 그룹 수와 반환 그룹 수를 분리해 표시 한도에 따른 잘림 공개
- 그룹 값이 결측이면 임의의 범주로 바꾸지 않고 `확인 불가` 그룹으로 보존

지원 예시:

```text
국내 ETP의 상품유형별 분포를 집계해줘
공모펀드의 위험등급별 분포를 알려줘
국내채권의 통화별 발행잔액 합계를 집계해줘
```

지원하지 않는 예시:

```text
AUM별로 상품 수를 알려줘
```

위 질문은 AUM 구간 경계가 없어 상품마다 다른 숫자를 그대로 그룹으로 만들 수
있으므로 역질문으로 종료한다.

## 5. 통화 안전 규칙

`source_currency_amount`의 `MIN`, `MAX`, `AVG`, `SUM`은 다음 중 하나가
필수다.

1. `trading_currency = KRW`처럼 정확히 하나의 통화를 locked equality로 지정
2. `trading_currency`를 `group_by`에 포함해 통화별로 따로 계산

해외 ETP와 국내 ETP는 동결 데이터 계약상 각각 USD와 KRW이므로 서버 compiler가
해당 통화를 locked constraint로 추가한다. 국내채권과 공모펀드는 통화가 섞일 수
있으므로 사용자가 통화를 지정하거나 통화별 집계를 요청하지 않으면 실행하지
않는다.

통화별 그룹에서 통화 자체가 UNKNOWN인 행은 금액을 합치지 않고 해당 metric을
`확인 불가`로 둔다. 환율 변환과 서로 다른 통화의 직접 합산은 지원하지 않는다.

## 6. 결측·품질·기준일

- `UNKNOWN`, `INVALID`, `UNSUPPORTED`, 실제 `None`을 0으로 대체하지 않음
- 후보 행 수, 유효값 수, 계산 제외 수를 metric마다 반환
- 유효값이 하나도 없으면 `SUM=0`으로 만들지 않고 집계값을 `null`로 반환
- `STALE` 값은 원천값으로 계산하되 결과 품질과 경고에 stale 상태 유지
- 필드별 `static`, `dynamic`, `snapshot` 기준일 규칙 적용
- 서로 다른 기준일이 섞이면 `as_of_start`, `as_of_end` 범위와 경고 표시
- 평균은 유효값만 사용하고 `ROUND_HALF_EVEN`으로 소수점 이하 12자리 반올림

## 7. 실행과 독립 검증

`SQLiteAggregateOracle`은 parameterized SQL로 격리 행과 locked 조건을
적용한 후보 행을 고른다. 실제 수치 축약은 SQLite 부동소수점 오차와 정수
overflow를 피하기 위해 Python `Decimal`로 수행한다.

`AggregateResultVerifier`는 QueryPlan의 조건·그룹·집계 필드와 품질·기준일만
별도 SQL projection으로 읽어 다음을 Python에서 재계산한다. 기본 경로에서는
전체 정규화 Pydantic 레코드와 원천값 사전을 메모리에 올리지 않는다.

- locked constraint를 만족하는 후보 수
- 전체 그룹 수와 반환 그룹 순서
- 각 그룹의 행 수
- 함수·필드별 값
- 유효값·제외값 수
- 품질 상태와 기준일 범위

후보 수, 그룹, metric 중 하나라도 실행 결과와 다르면 답변을 만들지 않고
`ResultVerificationError`로 종료한다.

네 상품군 SEARCH·AGGREGATE의 projected record 동등성과 변조 탐지, 실제 데이터
성능 결과는 [SEARCH·AGGREGATE 성능 기준선](evaluation-search-aggregate-performance.md)에
고정한다.

## 8. AggregateEvidence와 Backend DTO

각 그룹·metric 조합은 하나의 `AggregateEvidence`가 된다.

- 함수, canonical field, 표시 label, 값, 단위
- 그룹 값과 그룹 field의 원천 column
- 후보 행 수, 유효값 수, 제외값 수
- 원천 dataset·source ID·metric source column
- 파일 스냅샷일, 필드 기준일 시작·종료
- 품질 상태와 품질 사유

Backend 응답의 `aggregates` 배열과 `aggregate_field` citation으로 전달한다.
집계 성공 응답은 상품 목록이 비어 있어도 aggregate evidence가 있으면
`success`이며, 후보가 0개면 evidence 없이 `not_found`가 된다.

## 9. 현재 제한

- 한 번에 하나의 상품군만 집계
- 숫자·날짜 구간 분포와 median·percentile·표준편차는 아직 미지원
- 서로 다른 상품군의 필드를 하나의 값으로 합산하지 않음
- 환율 변환 미지원
- 공모펀드 합계는 클래스 grain이므로 같은 대표 펀드의 여러 클래스가 포함될 수
  있음을 경고
- 자연어 aggregate parser는 명시적인 함수와 필드만 연결하며, “평균 수익률”처럼
  기간이 빠진 표현은 추측하지 않고 역질문
- AggregateEvidence를 입력으로 받는 LLM 전용 답변 schema와 Answer Verifier는
  아직 추가하지 않았으므로 현재 집계 답변은 결정론적으로만 생성
- 독립 external blind와 사람 평가는 아직 수행하지 않음

## 10. 회귀 기준

- 네 상품군 count·평균·최댓값·합계·그룹 분포 E2E
- UNKNOWN 제외와 유효값·제외값 수 검증
- 국내채권·공모펀드 금액 집계의 통화 gate
- SQL 실행 결과 metric 변조 탐지
- Backend aggregate evidence와 citation 변환
- 라우팅 진단 v2: 도입 전 replay 4/28, 현재 Router 28/28

라우팅 v1 진단은 AGGREGATE 미지원 시점의 봉인 이력으로 유지한다. 현재 회귀
AGGREGATE 최초 활성화 이력은 `pre_hcx_route_diagnostic_28_v2.json`과 v2
baseline에 보존한다. 현재 capability 정본은 공통 COMPARE까지 포함한 v3다.
