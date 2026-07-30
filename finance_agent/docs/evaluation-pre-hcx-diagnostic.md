# HyperCLOVA X 연결 전 진단·외부 blind 프로토콜

마지막 갱신: 2026-07-30

## 1. 목적

네 상품군과 일곱 질의 유형을 같은 기준으로 측정하고, 공개 회귀 성능과 독립
일반화 성능을 분리한다.

- 상품군: 해외 ETF·ETN, 국내 ETF·ETN, 국내채권, 공모펀드
- 질의 유형: SEARCH, DETAIL, COMPARE, AGGREGATE, EXPLAIN, CLARIFY,
  UNSUPPORTED

## 2. 내부 diagnostic

현재 정본인 `pre_hcx_route_diagnostic_28_v2.json`은 상품군·질의 유형 조합
28개를 한 번씩 포함한다. AI 담당자가 현재 빈 구간을 찾기 위해 작성한 공개 세트이므로
`internal_diagnostic_not_blind`로 고정하고 최종 blind 또는 일반화 성능으로
부르지 않는다.

- v1: AGGREGATE 미지원 시점의 기대 disposition을 봉인한 역사 기준선
- v2: 네 상품군 AGGREGATE 실행 capability를 반영한 현재 회귀 기준선
- 기존 봉인 파일은 수정하지 않고 suite·commitment·baseline을 새 버전으로 추가

내부 diagnostic이 확인하는 항목:

- 질문과 request ID 보존
- 의도 분류
- 상품군 분류
- 실행·역질문·미지원 disposition
- 실행 가능한 경우 서버 QueryPlan intent

v2 재현 결과:

- pre-router search 강제 replay: 4/28
- 현재 Router: 28/28
- suite SHA-256:
  `ef35437ac3b9a02c2438ef664b49c339a631c249f53784b43fb2c1050d86e271`

라우팅 진단은 AGGREGATE 질문이 실행 경로로 연결되는지만 확인한다. 실제 후보
수·함수값·통화·결측·기준일은 네 상품군 집계 단위·통합 테스트에서 검증한다.

## 3. 독립 external blind

최종 holdout은 금융 도메인 담당자가 독립 작성한다. 질문과 정답키는 최초 실행
전까지 AI 담당자와 구현 코드에 공개하지 않는다.

작성 기준:

- 총 100문항
- 상품군별 25문항
- SEARCH 24, DETAIL 12, COMPARE 16, AGGREGATE 12, EXPLAIN 12,
  CLARIFY 12, UNSUPPORTED 12문항
- 실제 사용자가 쓸 표현, 경계값, 동의어, 생략, 모호성, 미지원 조건을 포함
- 공개 50문항과 내부 28문항을 단순 변형하지 않음
- 실행 문항은 QueryPlan·후보 수·상품 순서·필수 답변 검사를 비공개 정답키에 기록
- 통제 문항은 실행 기대값을 넣지 않고 clarify 또는 unsupported 사유를 기록

## 4. 역할 분리와 봉인 순서

1. 금융 도메인 담당자가 질문 JSON 작성
2. 별도 검수자가 비공개 정답키와 네 DB SHA-256 확인
3. validator로 문항 수·분포·QueryPlan·Oracle 기대값 검증
4. 현재 구현 commit, 질문 파일, 정답 파일을 SHA-256 commitment로 봉인
5. commitment를 팀 공유 위치에 먼저 보존
6. 최초 실행을 한 번만 수행하고 원본 report를 수정 없이 보존
7. 실패를 공개한 뒤 수정하고, 최초 결과와 사후 회귀를 분리 보고

질문·정답 원본은 민감한 holdout이므로 Git에 커밋하지 않는다. 저장소에는 schema,
validator, authoring guide, commitment와 집계 baseline만 둔다.

## 5. 해석 제한

- 내부 28문항 결과는 라우팅 배선 진단
- 기존 상품군별 50문항 결과는 공개 회귀 안정성
- external blind 최초 실행만 독립 일반화 성능
- 사람 rubric 점수는 자동 정확도와 별도
- 로컬 Qwen 결과는 HyperCLOVA X 공식 성능이 아님

외부 질문·정답 작성과 사람 평가는 저장소 코드만으로 완료할 수 없는 외부 게이트다.
