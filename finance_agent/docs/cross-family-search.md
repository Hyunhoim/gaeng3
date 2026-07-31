# 교차 상품군 병렬 SEARCH v1

상태: v1.0 구현·공개 실데이터 회귀 4/4

기준일: 2026-07-31

## 1. 목적

한 질문에서 둘 이상의 상품군을 요청하더라도 상품군의 서로 다른 수치 의미를
억지로 합치지 않고 각각 독립 검색한다.

지원 예시:

> 국내 ETF와 해외 ETF 중 총보수 0.01% 이하인 상품을 각각 3개 보여줘

지원하지 않는 예시:

> 국내 ETF와 해외 ETF의 수익률을 비교해줘

첫 질문은 같은 조건을 두 상품군의 별도 QueryPlan에 적용한다. 두 번째 질문은
기간·산식·기준일을 승인한 교차 비교 계약이 없으므로 역질문한다.

## 2. 실행 흐름

```text
사용자 질문
→ fail-closed Intent Router
→ 복수 상품군 + SEARCH 확인
→ 상품군별 단일-family QueryPlan 컴파일
→ 데이터셋 실행 승인·지원 조건 사전 검사
→ 상품군별 SQLite Oracle 병렬 실행
→ 상품군별 Result Verifier
→ 상품군별 field-level evidence
→ 결과 순서를 유지한 결정론적 답변
```

각 QueryPlan은 계속 한 상품군만 포함한다. 최상위 route와 Backend 응답만 여러
`family_searches`를 묶는다. 따라서 서로 다른 SQLite schema, manifest와 검증
결과가 섞이지 않는다.

## 3. 안전 계약

- 복수 상품군은 `SEARCH`만 실행
- `COMPARE`, `AGGREGATE`, 우열·추천·환율 환산은 기존 fail-closed 정책 유지
- 상품군을 먼저 나열한 뒤 모든 상품군에 적용할 공통 조건을 한 번만 작성
- 상품군 사이에 서로 다른 조건이 들어가면 실행하지 않고 역질문
- 각 상품군의 QueryPlan·후보 수·상품·warning·manifest를 별도 보존
- 한 상품군이 0건이어도 다른 상품군의 검증 결과를 유지
- 모든 상품군이 0건일 때만 Backend status를 `not_found`로 변환
- 요청한 DB가 하나라도 없거나 실행 비활성 상품군이면 부분 실행 전에 전체 차단
- 상품군 간 수치 직접 비교·합산·우열 판단을 수행하지 않았음을 답변에 명시
- v1은 QueryPlan provider와 answer provider를 호출하지 않는 결정론적 경로

공모펀드는 정규화·내부 회귀가 완료됐지만 공식 registry의
`execution_enabled=false`가 유지된다. 따라서 공모펀드가 포함된 교차 검색은
내부 평가 flag가 없는 공식 경로에서 실행하지 않는다.

## 4. DTO

최상위 `RoutedAgentResult`와 `BackendAgentResponse`는 기존 단일 상품군 필드를
호환 유지하면서 다음 묶음을 추가한다.

```text
family_searches[]
├─ product_family
├─ status: success | not_found
├─ query_plan
├─ candidate_count
├─ returned products 또는 returned_product_ids
├─ warnings
└─ source_manifest
```

복수 상품군 응답에서는 최상위 `query_plan`과 `source_manifest`를 비운다.
`candidate_count`는 상품군별 후보 수의 합이고, 최상위 `products`는
`family_searches` 순서대로 펼친 화면 표시용 목록이다. Backend는
`source_manifests`도 같은 순서로 제공한다.

## 5. 회귀 평가

공개 suite:

- `cross_family_search_v1.json`
- 국내 ETP·해외 ETP SQLite와 manifest SHA-256 고정
- 모델·네트워크 호출 없음

| 범주 | 기대 동작 | 결과 |
| --- | --- | ---: |
| 양쪽 성공 | 두 family 결과와 evidence 보존 | 1/1 |
| 부분 성공 | 국내 0건, 해외 결과 유지 | 1/1 |
| 전체 빈 결과 | 두 family 0건, Backend `not_found` | 1/1 |
| 교차 비교 | Oracle 없이 clarification | 1/1 |

총 4/4, strict accuracy 1.0이다. 공개 고정 문항의 회귀 결과이며 독립 blind
일반화 성능은 아니다.

재현:

```bash
python -m finance_agent_core.evaluation.cross_family_search_cli \
  --require-perfect
```

합성 fixture 계약 테스트는 동시 실행, 모델 무호출, DB 누락 선차단,
비대칭 조건 역질문까지 별도로 검증한다.

## 6. 다음 확장 조건

교차 상품군 직접 비교는 다음 항목이 금융 도메인 검수를 통과한 뒤 별도
allowlist로 연다.

- 같은 이름의 지표가 실제로 같은 산식과 기간을 쓰는지
- 단위와 통화가 같은지 또는 승인된 환산 기준이 있는지
- 기준일 차이를 허용할 수 있는지
- 결측·stale 상태에서 차이 계산을 차단하는 규칙
- 숫자 차이를 사용자에게 어떤 의미로 설명할지

그 전까지 교차 질문의 최선 경로는 상품군별 독립 검색과 근거 표시다.
