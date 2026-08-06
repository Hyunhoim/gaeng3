# HyperCLOVA X provider 계약

마지막 갱신: 2026-08-06

이 문서는 실제 API credential 없이 먼저 동결한 HyperCLOVA X provider 경계와
fake transport 테스트 범위를 설명한다. 현재 완료된 것은 요청·응답·오류 계약이며,
NAVER Cloud endpoint에 실제 HTTP 요청을 보내는 transport는 아직 구현하지 않았다.

2026-08-06 오프라인 설명회 질문은 확정했으며 현재 참석 팀원의 기록 전달을
기다리고 있다. 공식 답변과 출처를 확인하기 전까지 endpoint·credential·header·
실제 HTTP 연결을 시도하지 않는다. 현재 범위는 네트워크 없이 재생하는 계약·오류·
fallback 검증까지다.

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

공통 `RoutedFinanceAgent`의 SEARCH 경로에는 QueryPlan provider를 선택적으로
주입할 수 있다. 서버가 같은 질문으로 먼저 만든 기준 QueryPlan과 모델
QueryPlan이 완전히 일치할 때만 Oracle을 실행한다. 모델이 조건·정렬·상품군·
limit·projection을 추가하거나 누락하면 역질문으로 종료한다. AGGREGATE와
COMPARE는 현재 서버 결정론적 compiler를 그대로 사용한다.

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

## 7. API 없이 검증한 provider 계약

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

## 8. API 없는 전체 경로 E2E

provider 단위 계약과 별도로 `hcx-contract-e2e-8`을 동결했다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-hcx-contract-e2e.py \
  --require-perfect
```

8개 시나리오의 현재 결과는 8/8이다.

- 해외 ETP·국내 ETP·국내채권 SEARCH가 HCX QueryPlan부터 Oracle·Result
  Verifier·field-level evidence·HCX 답변·Answer Verifier·Backend DTO까지 통과
- 모델 답변의 상품 순서가 다르면 결정론적 답변으로 fallback
- QueryPlan timeout은 Oracle과 답변 provider 실행 전에 예외로 종료
- 금지 질의는 Router가 모델 호출 없이 거절
- 비활성 공모펀드는 모델과 데이터베이스 호출 없이 차단
- 모델 QueryPlan과 서버 기준계획이 다르면 Oracle 전에 역질문
- 모든 fake transport 요청에서 실제 네트워크와 credential 사용 0건

재현 집계는
[`hcx-contract-e2e-v1.json`](../evaluation/baselines/hcx-contract-e2e-v1.json)에
보존한다. 이 결과는 합성 SQLite와 fake 응답을 사용한 시스템 계약 회귀이며,
실제 HyperCLOVA X 생성 성능이나 API 호환성 점수가 아니다.

## 9. Backend `/answer` 오류 adapter

provider 예외를 그대로 웹 계층에 올리지 않도록 프레임워크 독립
`execute_answer_request()`를 추가했다. QueryPlan 생성 단계의 provider 오류는
Oracle 실행 전이므로 안전한 `error` DTO와 HTTP 502·503·504로 변환한다.
grounded answer 단계의 provider 오류는 이미 검증된 evidence가 있으므로
결정론적 fallback으로 복구해 HTTP 200을 유지한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

`answer-adapter-contract-12`는 provider 설정·인증·rate limit·서비스·timeout·
transport·응답 오류, dataset 장애, 알 수 없는 예외와 answer fallback을
12/12 통과한다. 질문·credential·provider 오류 본문·파일 경로는 공개 오류에
포함하지 않는다. 실제 FastAPI route, request 인증과 실제 네트워크 transport는
이 계약의 바깥이며 아직 구현하지 않았다.

## 10. 8월 6일 공식 안내 후 남은 작업

1. 허용 모델명과 Structured Outputs 지원 범위·제출 범위 재확인
2. 공식 endpoint·인증 header·요청·응답 body를 구현하는 HTTP transport 추가
3. credential을 환경변수 또는 secret store에서 읽고 로그 마스킹 검증
4. 공식 timeout·QPS·retry-after 정책을 반영한 제한적 retry 구현
5. 세 operation의 실제 API smoke test와 `hcx-contract-e2e-8` 재현
6. token·latency·오류·fallback 비율 측정
7. 로컬 Qwen 결과와 분리된 HyperCLOVA X 평가 baseline 기록
8. FastAPI 공식 `/answer` route에서 provider 선택·인증·request validation 연결
9. framework-neutral adapter와 실제 route를 합친 HTTP E2E 검증
10. 공식 답변에 따라 제출 후보에서 로컬 LLM provider·설정·
    스크립트·의존성을 제거하고 clean checkout에서 재검증

이 작업이 완료되기 전에는 “HyperCLOVA X API 연결 완료”라고 표현하지 않는다.
