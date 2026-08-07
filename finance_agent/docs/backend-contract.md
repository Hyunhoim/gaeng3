# Backend 전달용 Agent DTO v1

마지막 갱신: 2026-08-07

## 1. 목적

Frontend·Backend 구현과 AI Core를 특정 웹 프레임워크에 묶지 않고 연결하기 위한
Pydantic request·response 계약이다. FastAPI route나 Next.js 타입은 이 계약을
소비하는 adapter이며 정본이 아니다.

정본 코드:

- `finance_agent_core.contracts.backend.BackendAgentRequest`
- `finance_agent_core.contracts.backend.BackendAgentResponse`
- `backend_contract_schemas()` JSON Schema exporter
- `routed_result_to_backend()` 공통 Agent 결과 adapter
- `execute_answer_request()` 프레임워크 독립 `/answer` service adapter
- `AnswerAdapterResult` HTTP status와 응답 DTO 결합 계약
- `OfficialAnswerResponse` 주최 측 평가용 다섯 문자열 계약
- `official_response_from_backend()` 내부 DTO의 평가용 변환

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
| `success` | 상품·비교·집계 또는 문서 evidence가 있는 정상 답변 | 답변·상품·비교·집계·근거 표시 |
| `clarification` | 상품군·식별자·수치 기준이 부족 | required field 입력 요청 |
| `unsupported` | 예측·추천 또는 미구현 연산 | 안전 사유와 지원 질문 예시 표시 |
| `not_found` | 잠긴 조건에서 결과 0건 | 조건 자동 완화 없이 수정 유도 |
| `error` | 시스템 오류 | error code와 retryable에 따라 처리 |

응답은 원래 intent, 상품군, 서버 QueryPlan, 후보 수, 상품·비교·집계·문서 evidence,
구조화 citation, 기준일, warning, 답변 mode와 fallback 여부를 분리해 제공한다.

복수 상품군 SEARCH에서는 최상위 `query_plan`·`source_manifest` 대신
`family_searches`와 `source_manifests`를 사용한다. 각 family item은
상품군·status·단일-family QueryPlan·후보 수·반환 상품 ID·warning·manifest를
보존한다. 한 상품군이 0건이어도 다른 family 결과를 유지하고, 전부 0건일 때만
최상위 status가 `not_found`다. 최상위 products와 citation은 family 순서대로
펼쳐 화면에서 공통 렌더링할 수 있다.

grounded answer provider가 있으면 각 family evidence를 서로 격리해 생성한 뒤
서버가 같은 순서의 섹션으로 조합한다. 최상위 `answer_mode`·`provider_model`·
`fallback_used`는 이 조합 결과를 표현한다. 한 family의 생성·검증이 실패하거나
다른 상품군 언급·교차 비교·합산 문구가 검출되면 부분 모델 문장을 남기지 않고
전체 응답을 `deterministic_fallback`으로 바꾼다.

## 4. 근거와 fallback

상품 citation은 `product_id:canonical_field` evidence를 원천 dataset·row·column·
기준일에 연결한다. 문서 citation은 document·chunk ID, source URI와 기준일에
연결한다.

비교 citation의 `kind`는 `comparison_field`다. `comparisons` 배열은 요청 순서의
두 상품 값·품질·통화·기준일·원천 위치, field status와
`second_minus_first` 차이를 분리해 제공한다. 차이가 차단돼도 두 원천값과
차단 사유는 보존한다.

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

## 5. `/answer` 오류 경계

`execute_answer_request()`는 FastAPI에 의존하지 않는 최외곽 service adapter다.
유효한 `BackendAgentRequest`와 `RoutedFinanceAgent`를 받아 다음 두 값을 함께
반환한다.

- `http_status_code`: Backend route가 그대로 적용할 권장 HTTP status
- `response`: 항상 schema 검증을 마친 `BackendAgentResponse`

정상·역질문·미지원·검색 결과 없음·검증된 fallback은 모두 HTTP 200이다.
이는 Agent가 의미 있는 계약 응답을 정상적으로 만들었다는 뜻이다. 반면
QueryPlan provider 장애처럼 근거 실행을 시작하지 못한 시스템 오류는 다음과
같이 변환한다.

| 내부 오류 | HTTP | 공개 error code | retryable |
| --- | ---: | --- | --- |
| HyperCLOVA X 설정·인증 실패 | 503 | `provider_unavailable` | `false` |
| rate limit·서비스·연결 실패 | 503 | `provider_unavailable` | `true` |
| provider timeout | 504 | `provider_unavailable` | `true` |
| 비정상 JSON·응답 계약 실패 | 502 | `provider_unavailable` | `true` |
| SQLite·dataset I/O 실패 | 503 | `dataset_unavailable` | `true` |
| 분류하지 못한 내부 예외 | 500 | `internal_error` | `false` |

상위 서비스가 요청 질문·credential·provider 오류 본문·파일 경로를 포함한
예외를 던지더라도 공개 DTO에는 고정된 안전 문구만 사용한다. `error` 응답은
QueryPlan, 후보 수, 상품·비교·집계·문서 evidence, citation, 기준일, warning,
provider model, source manifest와 fallback을 함께 반환할 수 없다.

grounded answer provider가 실패한 경우에는 이미 Oracle과 Result Verifier를
통과한 evidence가 존재한다. 이 오류는 답변 composer가 결정론적 답변으로
복구하므로 HTTP 200, `answer_mode=deterministic_fallback`,
`fallback_used=true`로 반환한다. provider rate limit이어도 클라이언트가
잘못한 요청은 아니므로 HTTP 429로 재해석하지 않는다.

입력 JSON의 형식 오류는 FastAPI request validation 경계에서 HTTP 422,
`status=error`, `error.code=invalid_request`, `retryable=false`인 같은
`BackendAgentResponse`로 변환한다. 유효한 `request_id`는 보존하고 누락·공백·길이
초과 ID는 `invalid-request`로 대체한다. validation 상세 위치·원문 입력값은 공개
응답에 반사하지 않는다. 인증은 이후 middleware 책임이다. 프레임워크 adapter는
`AnswerAdapterResult`의 status와 response를 그대로 직렬화하고 내부 예외를 다시
노출하지 않는다.

## 6. JSON 예시

package에 다음 예시를 포함하고 contract test에서 매번 검증한다.

- `backend_request_v1.json`
- `backend_clarification_response_v1.json`
- `backend_document_response_v1.json`
- `backend_aggregate_response_v1.json`
- `backend_error_response_v1.json`

COMPARE 응답은 같은 schema의 `comparisons`와 `comparison_field` citation으로
전달한다. `product-compare-core-30` 공개 회귀가 실행·차단 30문항에서
Backend response 검증과 비교 citation 수를 함께 확인한다.

교차 상품군 SEARCH는 `cross-family-search-v1-4` 공개 회귀에서 양쪽 성공,
부분 성공, 전체 빈 결과와 직접 비교 차단을 4/4 검증한다. 같은 suite의
grounded answer v2는 expected·로컬 Qwen 각각 4/4, 생성 대상 2문항
`llm_grounded`, fallback 0, 전체 빈 결과·control 모델 무호출을 확인한다.

오류 경계는 다음 명령으로 네트워크 없이 재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

동결된 12개 시나리오는 정상 응답, HyperCLOVA X 설정·인증·rate limit·서비스·
timeout·transport·응답 오류, dataset 장애, 알 수 없는 내부 오류, grounded
answer fallback과 민감정보 비노출을 12/12 검증한다. 이는 실제 HTTP route나
HyperCLOVA X API 호환성 평가가 아니다.

현재 FastAPI `POST /answer` route는 같은 DTO를 response model로 사용한다. 이후
JSON Schema에서 OpenAPI·TypeScript 타입을 생성하거나 동일 필드를 수동 매핑한다.
route는 DTO 필드를 삭제·재해석하지 않고 HTTP 인증, request parsing과 transport
lifecycle만 추가한다.

## 7. 주최 측 평가용 `GET /answer`

Frontend에 제공하는 상세 `POST /answer`와 별개로, 공식 평가 route는 다음
다섯 문자열만 반환한다.

| 필드 | 내용 |
| --- | --- |
| `question_id` | 요청의 평가 질문 ID |
| `question` | 요청 원문 |
| `retrieved_context` | citation·근거·기준일을 JSON 문자열로 직렬화한 값 |
| `think_trace` | intent·필터·검증·fallback의 구조화 실행 기록 JSON 문자열 |
| `answer` | 검증된 최종 답변 또는 안전한 한계 안내 |

`think_trace`는 모델의 숨은 사고과정을 노출하지 않고, 서버가 관측하고 다시
검사할 수 있는 실행 사실만 담는다. 결정론적 답변에서 실행하지 않은
Answer Verifier를 실행했다고 표시하지 않는다.

성공·결과 없음·역질문·미지원·내부 오류와 입력 오류를 모두 같은 스키마와
HTTP 200으로 반환한다. 이는 주최 측 계약을 위한 예외이며, 내부 `POST /answer`의
HTTP 422·502·503·504 오류 의미를 바꾸지 않는다. 정의되지 않은 query parameter는
무시하며, 내부 예외·credential·파일 경로는 다섯 문자열에 노출하지 않는다.

Agent Core 계약 2건과 Backend의 모든 응답 상태·예외·추가 parameter 테스트로
자동 검증한다. Docker HTTP smoke도 실제 공식 GET 응답의 필드·문자열·
직렬화를 검사하도록 확장했다.

평가 route에는 기본 55초의 바깥쪽 시간 예산을 둔다. 예산이 끝나면
`control_code=request_timeout`, 빈 citation과 안전 문구를 담은 같은 다섯 문자열을
HTTP 200으로 반환한다. 이 제한은 실행 중인 Python thread를 강제 종료하지 않으므로,
실제 provider timeout은 55초보다 짧게 두고 동시 요청 상한도 별도로 확정해야 한다.
