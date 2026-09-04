# 교차 상품군 병렬 SEARCH와 grounded answer v2

상태: v2.0 구현·expected/로컬 Qwen 공개 실데이터 회귀 각각 4/4

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
→ 상품군별 evidence-only grounded answer
→ 상품군별 Answer Verifier
→ 서버의 섹션 조합·교차 답변 검증
→ 검증 실패 시 전체 결정론적 fallback
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
- 복수 상품군 QueryPlan은 모델에 맡기지 않고 서버가 단일-family 계획으로 분해
- Answer provider에는 한 번에 한 상품군의 질문·QueryPlan·evidence·manifest만 전달
- 다른 상품군 언급 또는 교차 비교·합산·우열 표현이 생성되면 최종 답변에 사용하지 않음
- 한 상품군의 provider 오류나 Answer Verifier 실패도 전체 결정론적 fallback으로 전환
- 전체 빈 결과와 control route는 Answer provider를 호출하지 않음

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

grounded answer가 활성화되면 최상위 `answer_mode`는 다음처럼 정해진다.

- 하나 이상의 비어 있지 않은 family 답변이 모두 검증됨: `llm_grounded`
- 모든 family가 빈 결과여서 생성 호출이 없음: `deterministic`
- family 생성·검증 또는 교차 답변 검증 중 하나라도 실패: `deterministic_fallback`

fallback에서는 부분적으로 성공한 모델 문장도 최종 답변에서 제거한다. 화면에는
항상 같은 상품군 섹션과 field-level citation을 유지하므로 Frontend가 문자열을
분석해 안전 상태를 추측할 필요가 없다.

## 5. 회귀 평가

공개 suite:

- `cross_family_search_v1.json`
- 국내 ETP·해외 ETP SQLite와 manifest SHA-256 고정
- 기존 4문항을 결정론적 검색과 grounded answer 배선에 함께 사용

| 범주 | 기대 동작 | 결과 |
| --- | --- | ---: |
| 양쪽 성공 | 두 family 결과와 evidence 보존 | 1/1 |
| 부분 성공 | 국내 0건, 해외 결과 유지 | 1/1 |
| 전체 빈 결과 | 두 family 0건, Backend `not_found` | 1/1 |
| 교차 비교 | Oracle 없이 clarification | 1/1 |

결정론적 검색 v1과 grounded answer v2 expected provider는 각각 4/4다.
로컬 Qwen도 4/4, 생성 대상 2문항 모두 `llm_grounded`, fallback 0이다.
양쪽 성공은 family별 2회, 부분 성공은 비어 있지 않은 family 1회만 생성해
실제 모델 호출은 총 3회다. 전체 빈 결과와 교차 비교 control은 0회다.

로컬 Qwen의 세 생성 호출 지연 합계는 5,572.617ms다. RTX 5090 두 장의 단일
개발 실행값이며 운영 SLO가 아니다. 공개 고정 문항의 회귀 결과이므로 독립 blind
일반화 성능도 아니다.

재현:

```bash
python -m finance_agent_core.evaluation.cross_family_search_cli \
  --require-perfect
```

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
python -m finance_agent_core.evaluation.cross_family_answer_cli \
  --provider local_test \
  --require-perfect \
  --require-zero-fallback
```

합성 fixture 계약 테스트는 Oracle 병렬 실행, QueryPlan provider 무호출,
상품군별 질문·evidence 격리, provider 장애 전체 fallback, 교차 비교 문구 제거,
control 무호출, DB 누락 선차단과 비대칭 조건 역질문까지 별도로 검증한다.

## 6. 다음 확장 조건

교차 상품군 직접 비교는 다음 항목이 금융 도메인 검수를 통과한 뒤 별도
allowlist로 연다.

- 같은 이름의 지표가 실제로 같은 산식과 기간을 쓰는지
- 단위와 통화가 같은지 또는 승인된 환산 기준이 있는지
- 기준일 차이를 허용할 수 있는지
- 결측·stale 상태에서 차이 계산을 차단하는 규칙
- 숫자 차이를 사용자에게 어떤 의미로 설명할지

그 전까지 교차 질문의 최선 경로는 상품군별 독립 검색과 근거 표시다.

현재 v2의 생성 기능은 비교 기능을 추가한 것이 아니다. LLM은 이미 검증된 각
상품군 결과를 읽기 쉽게 설명할 뿐이며, 상품군 사이의 수치 의미를 연결하거나
차이를 계산할 권한이 없다.
