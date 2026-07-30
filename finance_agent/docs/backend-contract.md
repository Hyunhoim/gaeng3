# Backend 전달용 Agent DTO v1

마지막 갱신: 2026-07-30

## 1. 목적

Frontend·Backend 구현과 AI Core를 특정 웹 프레임워크에 묶지 않고 연결하기 위한
Pydantic request·response 계약이다. FastAPI route나 Next.js 타입은 이 계약을
소비하는 adapter이며 정본이 아니다.

정본 코드:

- `finance_agent_core.contracts.backend.BackendAgentRequest`
- `finance_agent_core.contracts.backend.BackendAgentResponse`
- `backend_contract_schemas()` JSON Schema exporter
- `routed_result_to_backend()` 공통 Agent 결과 adapter

## 2. 요청

`BackendAgentRequest` 필드:

- `schema_version`: 현재 `1.0`
- `request_id`: Backend가 만든 추적 ID
- `question`: 원문 자연어 질문
- `locale`: 현재 `ko-KR`

빈 질문·ID와 알 수 없는 추가 필드는 거절한다.

## 3. 응답 상태

| status | 의미 | 주요 UI 동작 |
| --- | --- | --- |
| `success` | 상품·집계 또는 문서 evidence가 있는 정상 답변 | 답변·상품·집계·근거 표시 |
| `clarification` | 상품군·식별자·수치 기준이 부족 | required field 입력 요청 |
| `unsupported` | 예측·추천 또는 미구현 연산 | 안전 사유와 지원 질문 예시 표시 |
| `not_found` | 잠긴 조건에서 결과 0건 | 조건 자동 완화 없이 수정 유도 |
| `error` | 시스템 오류 | error code와 retryable에 따라 처리 |

응답은 원래 intent, 상품군, 서버 QueryPlan, 후보 수, 상품·집계·문서 evidence,
구조화 citation, 기준일, warning, 답변 mode와 fallback 여부를 분리해 제공한다.

## 4. 근거와 fallback

상품 citation은 `product_id:canonical_field` evidence를 원천 dataset·row·column·
기준일에 연결한다. 문서 citation은 document·chunk ID, source URI와 기준일에
연결한다.

집계 citation의 `kind`는 `aggregate_field`다. `aggregates` 배열의
`AggregateEvidence`와 다음을 연결한다.

- 함수·field·값·단위와 그룹 값
- 후보 행 수·유효값 수·제외값 수
- 원천 dataset·source ID·column과 스냅샷일
- 집계 입력의 필드 기준일 시작·종료

상품 목록이 없어도 aggregate evidence가 있으면 `success`다. 집계 후보가 0이면
aggregate evidence와 citation 없이 `candidate_count=0`, `not_found`로 반환한다.

`answer_mode`:

- `control`: 역질문·미지원
- `deterministic`: 서버 renderer 답변
- `llm_grounded`: Answer Verifier를 통과한 근거 설명
- `deterministic_fallback`: 생성 또는 검증 실패 뒤 서버 답변으로 대체

`fallback_used=true`는 마지막 mode에서만 허용한다. 따라서 Backend가 답변 문자열을
분석하지 않고도 폴백과 상태를 표시할 수 있다.

## 5. JSON 예시

package에 다음 예시를 포함하고 contract test에서 매번 검증한다.

- `backend_request_v1.json`
- `backend_clarification_response_v1.json`
- `backend_document_response_v1.json`
- `backend_aggregate_response_v1.json`

실제 FastAPI adapter가 추가되면 JSON Schema에서 OpenAPI·TypeScript 타입을
생성하거나 동일 필드를 수동 매핑할 수 있다. adapter는 DTO 필드를 삭제·재해석하지
않고 HTTP status, timeout과 인증만 추가한다.
