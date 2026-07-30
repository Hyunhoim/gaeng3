# HyperCLOVA X provider 계약

마지막 갱신: 2026-07-30

이 문서는 실제 API credential 없이 먼저 동결한 HyperCLOVA X provider 경계와
fake transport 테스트 범위를 설명한다. 현재 완료된 것은 요청·응답·오류 계약이며,
NAVER Cloud endpoint에 실제 HTTP 요청을 보내는 transport는 아직 구현하지 않았다.

## 1. 왜 API 연결보다 계약을 먼저 만드는가

Agent의 검색·계산·근거 검증은 이미 결정론적 코드가 담당한다. HyperCLOVA X는
그 위에서 자연어를 구조화하거나 검증된 evidence를 설명하는 제한된 역할만 맡는다.
API가 없는 동안 이 경계를 테스트하면 다음 문제를 미리 차단할 수 있다.

- 로컬 모델 전용 설정이 평가 경로로 섞이는 문제
- prompt마다 서로 다른 응답 형식을 기대하는 문제
- 인증 실패·rate limit·timeout을 정상 응답처럼 처리하는 문제
- 모델이 만든 상품명·수치·순위가 evidence 검증을 우회하는 문제
- 질문과 API 오류 본문이 로그나 예외에 그대로 노출되는 문제

## 2. 현재 구조

```text
QueryPlan / 공모펀드 비교 초안 / 근거 답변 provider
                         │
                         ▼
                HyperClovaXClient
        설정 gate · schema 검사 · 오류 정규화
                         │
                         ▼
              HyperClovaXTransport 계약
                 │                 │
                 ▼                 ▼
       테스트용 fake transport   실제 HTTP transport
              완료                 API 확인 후 구현
```

provider는 API URL, 인증 header, SDK 응답 구조를 직접 알지 못한다.
`HyperClovaXTransport.complete()`가 의미 단위의 structured request를 실제
서비스 형식으로 변환하도록 분리했다. 따라서 공식 API 계약이 확인되면 transport만
추가하고 상위 Agent와 verifier는 그대로 유지할 수 있다.

## 3. HyperCLOVA X가 맡는 세 가지 역할

| operation | provider | 입력과 출력 |
| --- | --- | --- |
| `query_plan` | `HyperClovaXQueryPlanProvider` | 자연어 질문 → 제한된 `QueryPlan` |
| `fund_comparison_draft` | `HyperClovaXFundComparisonDraftProvider` | 공모펀드 비교 질문 → 상품명 표현과 비교 필드 초안 |
| `grounded_answer` | `HyperClovaXGroundedAnswerProvider` | 검증된 field-level evidence → 설명용 답변 초안 |

세 경로 모두 모델 출력 뒤에 Pydantic 계약 검증을 수행한다. QueryPlan은
결정론적 canonicalization과 Oracle을 거치고, 근거 답변은 기존 Answer Verifier를
통과해야 한다. 검증에 실패하면 기존 결정론적 fallback 정책을 유지한다.

## 4. 공식 경로 설정 gate

현재 provider 설정은 다음 값만 받는다.

```text
FINANCE_AGENT_LLM_MODE=evaluation 또는 production
LLM_PROVIDER=hyperclova
HCX_MODEL=HCX-로 시작하는 공식 확인 모델 ID
HCX_TIMEOUT_SECONDS=0보다 크고 300 이하인 값
```

mode와 provider가 정확히 일치하지 않으면 시작 전에 fail-closed로 차단한다.
정확한 모델 ID는 주최 측 또는 공식 API 계약을 확인한 뒤 입력해야 한다.

다음 값은 아직 설정 계약에 넣지 않았다.

- API base URL
- API key·gateway key 등 credential
- 인증 header 이름
- 서비스별 request ID header
- retry와 rate-limit header 해석

확인되지 않은 값을 추측해 코드에 굳히지 않기 위한 결정이다. 따라서 현재
CLI에도 HyperCLOVA X 실제 호출 옵션을 노출하지 않았다.

## 5. Transport 요청·응답 계약

상위 provider가 transport에 전달하는 요청에는 다음 항목이 있다.

- operation, model, timeout
- system prompt와 user prompt
- schema 이름과 HyperCLOVA X 지원 subset으로 검증한 JSON schema
- 최대 출력 token 수

transport가 돌려주는 정규화 응답은 다음 항목으로 제한한다.

- HTTP status code
- 구조화 출력 문자열
- 안전한 request ID
- input·output·total token usage

실제 HTTP transport는 공식 응답을 이 형태로 변환해야 한다. token 합계가
입력과 출력의 합과 다르거나, request ID가 안전한 문자 범위를 벗어나거나,
성공 응답에 content가 없으면 응답 계약 오류로 차단한다.

## 6. 오류와 관측 계약

| 원인 | 정규화 오류 |
| --- | --- |
| 설정 mode·provider·model·timeout 오류 | `HyperClovaXConfigurationError` |
| HTTP 401·403 | `HyperClovaXAuthenticationError` |
| HTTP 429 | `HyperClovaXRateLimitError` |
| 그 밖의 non-2xx | `HyperClovaXServiceError` |
| transport timeout | `HyperClovaXTimeoutError` |
| 연결·운영체제 I/O 오류 | `HyperClovaXTransportError` |
| 응답 형식·JSON·Pydantic 계약 오류 | `HyperClovaXResponseError` |

오류 메시지는 prompt, 질문, credential, 서비스 오류 본문을 포함하지 않는다.
선택적 call record에는 operation, model, outcome, status, latency, request ID,
token usage만 기록하고 prompt와 응답 content는 기록하지 않는다. 이 기록의
`success`는 transport와 content 계약 통과를 뜻하며, 이후 도메인 검증 결과와는
별도로 해석한다.

## 7. API 없이 검증한 항목

테스트의 `FakeHyperClovaXTransport`는 네트워크를 전혀 사용하지 않고 정상 응답과
실패를 순서대로 재생한다. 현재 자동 테스트는 다음을 확인한다.

- 공식 mode와 provider 조합 외 실행 차단
- `HCX-` 모델 ID와 timeout 범위 검증
- 세 operation의 semantic structured request와 HCX schema subset
- 외부에서 받은 question ID가 모델이 만든 ID보다 우선하는지 확인
- 공모펀드 비교가 허용 필드만 모델에 노출하는지 확인
- 근거 답변 provider가 검증된 evidence만 입력으로 사용하는지 확인
- 401·403·429·500, timeout, 연결 실패의 오류 매핑
- 오류 본문·질문·prompt가 예외와 call record에 노출되지 않는지 확인
- 잘못된 transport 응답, JSON, extra field를 fail-closed로 차단

fake 응답이 통과한다는 사실은 HyperCLOVA X의 실제 생성 품질이나 공식 API
호환성을 뜻하지 않는다. 상위 Agent 배선이 예상 응답과 실패를 안전하게 처리한다는
계약 테스트다.

## 8. 실제 API 확보 후 남은 작업

1. 허용 모델명과 Structured Outputs 지원 범위 재확인
2. 공식 endpoint·인증 header·요청·응답 body를 구현하는 HTTP transport 추가
3. credential을 환경변수 또는 secret store에서 읽고 로그 마스킹 검증
4. 공식 timeout·QPS·retry-after 정책을 반영한 제한적 retry 구현
5. 세 operation의 실제 API smoke test와 고정 fixture 재현
6. token·latency·오류·fallback 비율 측정
7. 로컬 Qwen 결과와 분리된 HyperCLOVA X 평가 baseline 기록
8. Backend 공식 `/answer` adapter에서 provider를 선택하고 E2E 검증

이 작업이 완료되기 전에는 “HyperCLOVA X API 연결 완료”라고 표현하지 않는다.
