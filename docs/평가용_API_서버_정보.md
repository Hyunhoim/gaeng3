# 평가용 API 명세서

기준일: 2026-09-06
서비스: 금융상품 Agent 평가용 API
데이터 기준: 주최 측 2026-08-24 배포 데이터

공식 제출 항목 중 `평가용 API 서버 정보`에 해당하는 문서다. Endpoint URL과
요청·응답 JSON 스키마를 명시한다. 아래 내용은 실행 중인 서비스가 게시하는
OpenAPI 문서와 실제 응답에서 확인한 값이다.

## 1. Endpoint

| 항목 | 값 |
| --- | --- |
| 평가용 Endpoint | `http://101.79.25.57/answer` |
| Method | `GET` |
| 인증 | 없음. 인증 헤더를 사용하지 않는다 |
| Content-Type | `application/json; charset=utf-8` |
| 상태 확인 | `GET /health` |
| OpenAPI 문서 | `GET /openapi.json`, 화면은 `GET /docs` |

`POST /answer`는 내부용 API이며 평가 대상이 아니다.
평가는 `GET /answer`만 사용한다.

## 2. 요청

### 2.1 파라미터

| 이름 | 위치 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- | --- |
| `question_id` | query | string | 필수 | 주최 측이 부여한 문항 ID. 최대 128자 |
| `question` | query | string | 필수 | 평가 질의 원문. URL 인코딩. 최대 2,000자 |

정의되지 않은 query parameter가 함께 전달되어도 무시하고 정상 응답한다.
HTTP 500을 반환하지 않는다.

### 2.2 요청 예시

```bash
curl --get "http://101.79.25.57/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘"
```

```python
import requests

response = requests.get(
    "http://101.79.25.57/answer",
    params={"question_id": "Q-001", "question": "평가 질의"},
    timeout=300,
)
```

## 3. 응답

### 3.1 스키마

모든 응답은 아래 다섯 필드를 가지며 **다섯 필드 모두 문자열**이다.
정의되지 않은 필드는 추가하지 않는다.

```json
{
  "question_id": "string",
  "question": "string",
  "retrieved_context": "string",
  "think_trace": "string",
  "answer": "string"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `question_id` | string | 필수 | 요청값을 그대로 반환 |
| `question` | string | 필수 | 요청값을 그대로 반환 |
| `retrieved_context` | string | 필수 | 답변 근거로 참조한 데이터. JSON 직렬화 문자열 |
| `think_trace` | string | 필수 | 실행 과정 기록. JSON 직렬화 문자열 |
| `answer` | string | 필수 | 최종 생성 답변 |

`additionalProperties`는 `false`로 고정되어 있어 내부 DTO의 추가 필드가
평가 응답에 노출되지 않는다.

### 3.2 `retrieved_context` 내부 구조

문자열을 JSON으로 파싱하면 다음 키를 가진다.

| 키 | 설명 |
| --- | --- |
| `schema_version` | 근거 문서 스키마 버전 |
| `evidence` | 상품·비교·집계·문서 근거. 필드값과 원천 컬럼·기준일 포함 |
| `citations` | 출처 목록. 원천 테이블 ID와 기준일, 외부 근거의 URL·게시일 |
| `truncation` | 길이 제한으로 잘린 항목이 있는지 표시 |

답변할 수 없는 경우에도 같은 구조를 유지하며 `citations`가 비고 `reason`이 담긴다.

### 3.3 `think_trace` 내부 구조

숨은 사고과정이 아니라 **실제로 수행한 실행 사실**을 구조화해 기록한다.

| 키 | 설명 |
| --- | --- |
| `trace_type` | 항상 `structured_execution_summary_not_hidden_reasoning` |
| `intent` | 판정한 질의 의도 |
| `product_families` | 대상 상품군 |
| `filters` | 확정된 조건. field·연산자·값 또는 관계 capability |
| `ranking` | 정렬 field·방향·null 처리 |
| `candidate_count` | 조건을 만족한 후보 수 |
| `execution_steps` | 실행한 단계 목록 |
| `as_of_dates` | 사용한 기준일 |
| `returned_evidence` | 반환한 상품·비교·집계·문서·인용 수 |
| `answer_mode` | `llm_grounded` 또는 결정론적 응답 여부 |
| `fallback_used` | 검증 실패로 안전 응답을 사용했는지 |
| `status` | 실행 결과 상태 |
| `control_code` | 실행하지 않은 경우의 사유 코드 |

### 3.4 응답 예시

```json
{
  "question_id": "Q-001",
  "question": "국내 ETF를 순자산 큰 순으로 2개 보여줘",
  "retrieved_context": "{\"schema_version\":\"...\",\"evidence\":{...},\"citations\":[...],\"truncation\":{...}}",
  "think_trace": "{\"intent\":\"search\",\"product_families\":[\"domestic_etp\"],\"ranking\":[{\"field\":\"aum\",\"direction\":\"desc\",\"nulls\":\"last\"}],\"candidate_count\":1170,\"execution_steps\":[\"intent_router\",\"query_plan_validation\",\"deterministic_oracle\",\"result_verifier\",\"field_evidence_builder\",\"answer_verifier\",\"response_contract_validation\"],\"as_of_dates\":[\"2026-08-21\",\"2026-08-24\"],\"answer_mode\":\"llm_grounded\",\"fallback_used\":false,\"status\":\"success\"}",
  "answer": "검증된 후보 1170개 중 상위 2개입니다. 1. ... (근거: PREF01N001, 기준일 2026-08-21)"
}
```

## 4. HTTP 상태 코드

| 코드 | 사용 시점 | 응답 본문 |
| --- | --- | --- |
| 200 | 정상 답변, 결과 없음, 역질문, 미지원, 잘못된 필수 입력 | 다섯 문자열 |
| 503 | 일시적 장애. 재시도로 해소될 수 있음 | 다섯 문자열 |
| 504 | 외곽 timeout 도달 | 다섯 문자열 |

**답할 수 없는 질의도 HTTP 200과 동일한 스키마를 유지한다.** 데이터로 확인할 수
없으면 확인 불가를 명시하거나 필요한 조건을 역질문하며, 근거 없는 수익률 전망이나
단정적 투자 추천은 생성하지 않는다.

`question_id` 또는 `question`이 없거나 비어 있어도 HTTP 200과 다섯 필드를
반환하며 `think_trace`의 `control_code`가 `invalid_request`가 된다.

FastAPI가 자동 생성하는 OpenAPI 문서에는 422가 선언되어 있으나, 두 파라미터를
선택형으로 받고 라우트에서 직접 검증하므로 실제로 422가 반환되지 않는다.

## 5. 호출 조건

| 항목 | 값 |
| --- | --- |
| 호출 방식 | 문항 단위 순차 호출. 동시 호출 없음 |
| 요청 제한 | 문항당 최대 300초 |
| 외곽 timeout | 270초. 초과 시 HTTP 504와 안전 응답 |
| 재시도 | timeout 또는 5xx에 대해 최대 2회 |
| 중복 실행 방지 | 같은 `question_id`의 재시도는 동일 결과를 반환하며 Agent를 중복 실행하지 않는다 |

응답 시간은 평가 항목이며 문항당 60초 이내를 목표로 한다.

## 6. 참고

| 항목 | 값 |
| --- | --- |
| `GET /health` | 상품군 준비 상태, 관계 검색 상태, 공모펀드 실행 정책을 반환 |
| 데이터 기준일 | 국내 상품 2026-08-22, 해외 상품 2026-08-23 반영. 스냅샷 2026-08-24 |
| 외부 자료 수집 상한 | 2026-08-24 |
| 생성 모델 | 질의 의도 분석과 최종 답변 생성에 HyperCLOVA X(HCX-007) 사용 |

