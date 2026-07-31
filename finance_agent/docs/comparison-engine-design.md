# 네 상품군 공통 COMPARE 엔진 설계

마지막 갱신: 2026-07-30

상태: v1.0 네 상품군 same-family 실행 구현·독립 blind 평가 대기

이 문서는 해외 ETF·ETN, 국내 ETF·ETN, 국내채권, 공모펀드의 상품 간 비교를
하나의 안전 계약으로 일반화하기 위한 구현 설계다. 현재 동작하는 공모펀드
COMPARE를 기준으로 나머지 세 상품군을 확장하되, 필드가 숫자라는 이유만으로
직접 비교하거나 “더 좋다”는 결론을 만들지 않는다.

## 1. 구현 순서와 범위

v1은 다음 범위만 실행한다.

- 같은 상품군에 속한 서로 다른 상품 정확히 두 개
- 서버가 허용한 비교 필드만 사용
- 상품 요청 순서를 그대로 보존
- 숫자는 `두 번째 상품 - 첫 번째 상품` 차이만 계산
- 문자열·enum·boolean·날짜는 값만 나란히 표시
- 결측·통화·기준일·품질이 맞지 않으면 값을 보존하되 차이는 계산하지 않음
- 우열·추천·미래 성과 판단은 생성하지 않음

세 개 이상 상품, 상품군 간 직접 비교와 “가장 좋은 상품” 선정은 v1 범위가
아니다. 여러 상품 순위는 SEARCH ranking, 분포·평균·합계는 AGGREGATE로
분리한다.

## 2. 목표 실행 경로

```text
사용자 비교 질문
→ Intent Router
→ 상품군별 exact identity resolver
→ 서버 Compare QueryPlan compiler
→ comparison capability·통화·기준일 정책 검사
→ parameterized SQLite Oracle
→ 독립 Result Verifier
→ 요청 순서의 product field evidence
→ 결정론적 ComparisonEvidence 계산
→ ComparisonResultVerifier 재계산
→ evidence-only LLM 설명
→ Answer Verifier
→ 실패 시 결정론적 비교 답변
```

LLM은 상품 식별자, 비교값, 수치 차이, 통화, 기준일과 근거를 만들거나 수정하지
않는다.

## 3. 상품 식별 계약

| 상품군 | canonical identity | 자연어 후보 | 안전 규칙 |
| --- | --- | --- | --- |
| 해외 ETP | `product_id` | ticker, ISIN, 정확한 상품명 | ticker는 거래소와 함께 유일성 확인, 중복이면 역질문 |
| 국내 ETP | `product_id` | 종목코드, 정확한 상품명 | 약어명 중복 시 선택하지 않고 후보 제시 |
| 국내채권 | `product_id` | 종목코드, 정확한 상품명 | 약어명·발행기관만으로 식별하지 않음 |
| 공모펀드 | `itm_no` 기반 `product_id` | 정식명, 약어명, 상품번호 | 현재 exact resolver와 공모 범위 잠금 유지 |

- 두 표현이 같은 상품으로 연결되면 실행하지 않음
- 0개 또는 2개 초과 후보가 남으면 Oracle 호출 전 역질문
- 제외·대신·포함처럼 대상 역할을 바꾸는 표현은 명시적 문법 없이는 실행하지 않음
- 모델이 제안한 ID는 서버 resolver가 원문 질문에서 다시 확인

## 4. 상품군별 비교 필드

표의 “수치 차이”는 단위·통화·기준일 검사를 통과한 경우에만 허용한다.
“값 비교”는 값을 나란히 보여 주지만 서열이나 차이를 계산하지 않는다는 뜻이다.

### 4.1 해외 ETF·ETN

| 처리 | 필드 |
| --- | --- |
| 수치 차이 | `total_expense_ratio_pct`, `aum` |
| 값 비교 | `product_type`, `exchange_code`, `sellable`, `trading_suspended`, `asset_type`, `investment_region`, `trading_currency` |
| 식별·표시 전용 | `product_id`, `product_name`, `ticker`, `isin` |
| 미지원 | 수익률 비교 — 해외 ETP 제공 수익률 값은 현재 실행 품질을 충족하지 않음 |

### 4.2 국내 ETF·ETN

| 처리 | 필드 |
| --- | --- |
| 수치 차이 | `total_expense_ratio_pct`, `aum`, `leverage_factor`, `close_price`, `one_day_return_pct`, `one_month_return_pct`, `three_month_return_pct`, `six_month_return_pct`, `one_year_return_pct`, `ytd_return_pct`, `daily_trading_value` |
| 값 비교 | `product_type`, `exchange_code`, `sellable`, `trading_suspended`, `asset_type`, `investment_region`, `manager`, `base_index`, `strategy`, `risk_level`, `pension_eligible`, `core_etf`, `trading_currency` |
| 식별·표시 전용 | `product_id`, `product_name`, `short_name`, `ticker`, `isin` |

`base_index`는 coverage가 낮으므로 한 상품이라도 결측이면 `확인 불가`를
표시하며 같은 지수를 추종한다고 추정하지 않는다.

### 4.3 국내채권

| 처리 | 필드 |
| --- | --- |
| 수치 차이 | `issue_amount`, `coupon_rate_pct`, `buy_yield_pct`, `after_tax_yield_pct`, `buyable_quantity`, `remaining_days`, `duration_years` |
| 값 비교 | `bond_market`, `issuer`, `bond_major_class`, `bond_subclass`, `bond_type`, `issue_date`, `maturity_date`, `credit_rating`, `bond_risk_code`, `currently_buyable`, `trading_currency` |
| 식별·표시 전용 | `product_id`, `product_name`, `short_name`, `ticker` |

- `credit_rating`과 `bond_risk_code`는 공식 순서 계약이 없으므로 값만 표시
- `buy_yield_pct`, `after_tax_yield_pct`, `buyable_quantity`, `duration_years`는
  오래된 동적 값임을 기준일과 함께 경고
- 세후수익률은 투자자별 세제 조건을 추정하지 않고 원천값 차이만 계산

### 4.4 공모펀드

| 처리 | 필드 |
| --- | --- |
| 수치 차이 | `aum`, `one_week_return_pct`, `one_month_return_pct`, `three_month_return_pct`, `six_month_return_pct` |
| 값 비교 | `risk_level`, `trading_currency`, `fund_management_attribute`, `fund_geography_scope`, `investor_type`, `currency_hedged`, `sellable`, `company_sellable` |
| 식별·표시 전용 | `product_id`, `product_name`, `short_name` |
| 미지원 | 1년 이상 장기 수익률, 총보수, 판매수수료 — 현재 데이터 품질 또는 필드 부재 |

공모펀드는 클래스 grain이므로 서로 다른 클래스의 비교 결과를 대표 펀드 전체의
차이로 확대 해석하지 않도록 경고한다.

## 5. 공통 안전 정책

### 5.1 통화

- `source_currency_amount`는 두 상품의 `trading_currency`가 모두 존재하고 같을
  때만 차이 계산
- 통화가 다르면 원천값과 통화를 각각 표시하고 `currency_mismatch`
- 환율 환산은 별도 환율 데이터·기준시각·변환 계약 전까지 미지원

### 5.2 기준일

- 각 비교 셀에 필드 기준일을 별도로 보존
- 동적·snapshot 수치의 기준일이 다르면 `as_of_mismatch`
- 기준일이 다른 AUM·가격·수익률·수익률성 지표는 값을 표시하되 차이 계산 차단
- 같은 과거 기준일의 STALE 채권 지표는 차이를 계산할 수 있지만
  `stale_input` 경고와 “현재값 아님”을 필수 표시

### 5.3 결측과 품질

- `UNKNOWN`, `INVALID`, `UNSUPPORTED`, 실제 `None`을 0으로 바꾸지 않음
- 한쪽이라도 사용할 수 없으면 `unavailable`
- `PARTIAL`은 값·근거·품질 사유를 함께 표시
- 비교 evidence와 원천 product evidence의 값·단위·기준일이 다르면 검증 실패

### 5.4 수치 의미

- `pct_point` 차이는 퍼센트포인트로 표시
- 금액·수량·일수·연수는 원천 단위를 보존
- 절댓값·비율 차이와 “몇 배” 표현은 명시적 계약 전까지 계산하지 않음
- 낮은 보수, 높은 수익률 또는 높은 수익률성 지표를 자동으로 우수하다고 판정하지 않음

## 6. ComparisonEvidence 초안

공통 DTO는 다음 정보를 포함해야 한다.

- 비교 요청 순서와 두 `product_id`
- canonical field, label, value type, unit
- 각 상품의 값, 통화, 품질, 품질 사유, 필드 기준일
- 각 값의 원천 dataset·source ID·source column
- 상태: `numeric_delta`, `value_only`, `currency_mismatch`, `as_of_mismatch`,
  `stale_input`, `unavailable`, `incomplete`
- 차이값과 `second_minus_first` 기준
- 차이 계산을 생략한 결정론적 사유

Backend DTO는 상품 field citation과 별도로 비교 셀을 가리키는
`comparison_field` citation을 제공한다.

## 7. field registry 변경안

현재 `selectable`은 “답변에 표시 가능”을 뜻하며 “비교 가능”과 같지 않다.
구현 단계에서 다음 capability를 별도로 추가한다.

```yaml
comparable: false
comparison_mode: value_only
comparison_scope: same_dataset
```

- `comparable`: Compare QueryPlan에서 사용할 수 있는지 여부
- `comparison_mode`: `value_only` 또는 `numeric_delta`
- `comparison_scope`: `same_dataset`, `same_trading_currency`,
  `same_as_of`, `same_trading_currency_and_as_of`
- dataset override에서 품질·coverage·기준일 차이에 맞게 별도 설정

QueryPlan 검증은 모든 `comparison_fields`가 대상 상품군에서 `comparable`인지
확인해야 한다.

## 8. 상품군 간 질의

상품군 간 질의를 곧바로 두 상품의 수치 비교로 해석하지 않는다.

- 해외·국내 ETP의 총보수율처럼 canonical 의미와 단위가 같은 필드는 후속 v2에서 검토
- 국내 ETP와 공모펀드의 같은 기간 수익률은 기준일·산식 확인 전 직접 차이 계산 보류
- 채권 수익률과 ETF·펀드 기간 수익률은 의미가 다르므로 직접 비교 금지
- 서로 다른 상품군을 한 질문에서 조회하는 기능은 상품군별 독립 병렬 SEARCH
  v1으로 구현. 직접 수치 비교·합산·우열 판단은 하지 않음
- cross-family field는 금융 도메인 담당자의 의미 검수 후 별도 allowlist로 승인

## 9. 구현 상태

- [x] registry에 `comparable`, `comparison_mode`, dataset override capability 추가
- [x] 해외 ETP·국내 ETP·국내채권 exact resolver와 fail-closed compiler 추가
- [x] 공모펀드 `FundComparison` 호환성을 유지하며 공통 `ProductComparison`으로 일반화
- [x] 통화·기준일·STALE·결측 상태를 포함하는 `ComparisonEvidence` 구현
- [x] 요청 순서·셀 값·근거·차이를 재검산하는 `ComparisonResultVerifier` 구현
- [x] Router·compiler·capability matrix에서 네 상품군 COMPARE 활성화
- [x] Backend DTO에 `comparisons`와 `comparison_field` citation 추가
- [x] 세 상품군 합성 fixture E2E와 기존 공모펀드 공개 회귀를 함께 통과
- [x] 세 상품군 `product-compare-core-30` 공개 회귀 30/30과 기존 공모펀드
  24문항을 합쳐 네 상품군 자연어 비교 54문항 구성
- [x] 국내·해외 ETP 교차 검색의 성공·부분·빈 결과·비교 차단 공개 회귀 4/4
- [ ] 금융 도메인 담당자가 봉인한 독립 blind에서 최초 일반화 평가

현재 `executable`은 같은 상품군의 정확한 두 상품 비교만 뜻한다. 상품군 간
비교, 세 상품 이상 비교, 환율 환산, 우열·추천 판단은 계속 닫혀 있다.
