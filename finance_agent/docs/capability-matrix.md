# 금융상품 Agent capability matrix

마지막 갱신: 2026-07-30

이 문서는 네 상품군과 일곱 질의 유형의 현재 실행 범위를 설명한다. 기계 판독
정본은 `capability_matrix.json`이며 Python loader가 모든 28개 조합과
QueryPlan·SQLite Oracle 지원 범위를 자동 검사한다.

## 현재 범위

| 질의 유형 | 해외 ETF·ETN | 국내 ETF·ETN | 국내채권 | 공모펀드 |
| --- | --- | --- | --- | --- |
| SEARCH | 실행 | 실행 | 실행 | 내부 평가 실행 |
| DETAIL | 정확한 식별자를 SEARCH로 낮춰 실행 | 정확한 식별자를 SEARCH로 낮춰 실행 | 정확한 식별자를 SEARCH로 낮춰 실행 | 정확한 `itm_no`를 SEARCH로 낮춰 내부 실행 |
| COMPARE | 정확한 두 상품 공통 계약으로 실행 | 정확한 두 상품 공통 계약으로 실행 | 정확한 두 상품 공통 계약으로 실행 | 정확한 두 공모펀드 공통 계약으로 내부 실행 |
| AGGREGATE | 결정론적 실행 | 결정론적 실행 | 결정론적 실행 | 내부 평가 실행 |
| EXPLAIN | 정확한 상품 field evidence 설명 | 정확한 상품 field evidence 설명 | 정확한 상품 field evidence 설명 | 정확한 상품 field evidence 설명 |
| CLARIFY | 역질문 | 역질문 | 역질문 | 역질문 |
| UNSUPPORTED | 안전 거절 | 안전 거절 | 안전 거절 | 안전 거절 |

공모펀드의 “내부 실행” 표시는 데이터·Oracle·verifier가 구현됐지만 공식 Agent
execution flag는 HyperCLOVA X schema와 주최 측 계약 확인 전까지 꺼져 있다는 뜻이다.

## 실행 의미

- SEARCH: 조건·정렬·limit를 서버 QueryPlan으로 컴파일하고 parameterized
  SQLite Oracle과 독립 Python verifier를 통과한 결과만 반환
- DETAIL: 정확한 상품번호·종목코드를 locked equality 조건으로 바꾼 SEARCH
- COMPARE: 같은 상품군의 정확한 상품 두 개와 허용 field만 서버가 비교
- AGGREGATE: 개수·최솟값·최댓값·평균·허용 합계와 최대 두 범주
  `group_by`를 Decimal로 계산하고 독립 Python verifier로 재검산
- EXPLAIN: 정확한 상품을 먼저 조회한 뒤 field-level evidence에 있는 내용만 설명
- CLARIFY: 상품군·식별자·임계값이 부족하면 도구를 실행하지 않고 필요한 조건을 질문
- UNSUPPORTED: 예측·보장·단정적 추천 또는 구현되지 않은 연산을 실행하지 않음

AGGREGATE는 네 상품군 공통 Oracle, `AggregateEvidence`, Backend DTO와 독립
verifier를 사용한다. COMPARE도 exact resolver, `comparable` field capability,
`ComparisonEvidence`와 독립 verifier를 갖춰 same-family 실행으로 전환했다.
금액은 같은 통화일 때만 차이를 계산하고, 동적·snapshot 수치의 기준일이 다르면
원천값만 표시한다. 결측은 0으로 대체하지 않는다.

필드·식별자·통화·기준일 계약은
[네 상품군 공통 COMPARE 엔진 설계](comparison-engine-design.md)를 따른다.

## 자동 정합성 검사

loader는 다음 오류를 시작 단계에서 차단한다.

- 상품군·질의 유형 조합 누락 또는 중복
- QueryPlan enum에 없는 실행 intent
- SQLite Oracle이 지원하지 않는 intent의 실행 선언
- 비교 Oracle이 지원하지 않는 상품군·mode의 COMPARE 실행 선언
- AGGREGATE가 아닌 Oracle mode로 집계 실행을 선언하는 경우
- non-executable 항목에 QueryPlan 또는 Oracle mode가 남은 경우

matrix를 변경할 때는 관련 Oracle·policy·contract test와 28문항 진단 baseline을
함께 갱신해야 한다.

함수·필드·통화·결측·기준일 계약은
[네 상품군 공통 AGGREGATE 엔진](aggregate-engine.md)을 따른다. AGGREGATE
미지원 상태를 보존한 라우팅 진단 v1과 AGGREGATE까지 연 v2는 수정하지 않는다.
현재 AGGREGATE·COMPARE capability는 v3 진단과 baseline으로 별도 동결한다.
