# HyperCLOVA X provider 계약

마지막 갱신: 2026-08-12

이 문서는 HyperCLOVA X provider, 공식 HTTP transport와 FastAPI 연결 경계를
설명한다. 2026-08-11 NAVER Cloud 공식 문서 기준 endpoint·Bearer 인증·Structured
Outputs 요청과 응답 형식을 반영했다. 실제 credential을 사용한 외부 API 호출은 아직
수행하지 않았으며, 현재 완료 범위는 **HTTP 배선과 무네트워크 계약 검증**이다.

평가용 `GET /answer`와 다섯 문자열 응답 계약은 별도 Backend adapter가 담당한다.
HCLX는 그 안에서 검증된 결과를 제한된 문장으로 표현하며, 공식 데이터 검색·수치·
정렬·근거는 계속 SQLite/Python과 Verifier가 결정한다.

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
       테스트용 fake transport   공식 Direct v3 HTTP transport
              완료                 구현·무호출 검증 완료
```

provider는 API URL, 인증 header, 응답 wire 구조를 직접 알지 못한다.
`HyperClovaXTransport.complete()`가 의미 단위의 structured request를 실제
서비스 형식으로 변환하도록 분리했다. 실제 transport는 공식 host만 사용하고 redirect를
따르지 않으며, 상위 Agent와 verifier 계약은 그대로 유지한다.

공통 `RoutedFinanceAgent`의 SEARCH 경로에는 QueryPlan provider를 선택적으로
주입할 수 있다. 서버가 같은 질문으로 먼저 만든 기준 QueryPlan과 모델
QueryPlan이 완전히 일치할 때만 Oracle을 실행한다. 모델이 조건·정렬·상품군·
limit·projection을 추가하거나 누락하면 역질문으로 종료한다. AGGREGATE와
COMPARE는 현재 서버 결정론적 compiler를 그대로 사용한다.

HCLX planning(모델을 이용한 계획 제안)은 provider가 주입됐다는 이유만으로 호출되지
않는다. 서버가 요청마다 만든 `PlanningDecision.hclx_allowed=true`가 QueryPlan provider와
grounded planning provider 모두의 필수 권한이다. 쉬운 말로, 배포 flag와 provider가 있어도
서버 정책이 그 요청의 HCLX 계획 제안을 명시적으로 허용하지 않으면 호출 자체를 하지 않는다.
이 권한은 답변 문장 생성 권한과 별개이며 Oracle 실행 권한을 대신하지 않는다.

여기서 구현 범위를 구분해야 한다. `HyperClovaXQueryPlanProvider`는 FastAPI의 별도 flag로
실제 production 조립할 수 있다. 반면 grounded planning은 Core의 `GroundedPlanProvider`·
원문 evidence gate·fake/local 계약과 `PlanningDecision`/`PlanAuthorityGate` 권한 검증까지만
구현됐고, **HCLX용 grounded-plan operation/provider 및 FastAPI production 배선은 아직 없다.**
현재 production 조립 검사는 grounded provider가 들어오면 오히려 시작을 거부한다. 두 planning
provider를 한 번에 연결했을 때 중복 호출·우선순위가 생기지 않도록 별도 operation flag와
release manifest 계약, 실제 HCLX 품질 평가를 먼저 확정한 뒤 연결한다.

## 3. HyperCLOVA X가 맡는 세 가지 역할

| operation | provider | 입력과 출력 |
| --- | --- | --- |
| `query_plan` | `HyperClovaXQueryPlanProvider` | 자연어 질문 → 제한된 `QueryPlan` |
| `fund_comparison_draft` | `HyperClovaXFundComparisonDraftProvider` | 공모펀드 비교 질문 → 상품명 표현과 비교 필드 초안 |
| `grounded_answer` | `HyperClovaXGroundedAnswerProvider` | 검증된 field-level evidence → 설명용 답변 초안 |

세 경로 모두 모델 출력 뒤에 HCX JSON schema를 서버에서 다시 검사하고 Pydantic
계약 검증도 수행한다. 서버 재검증은 object shape·type·enum·anyOf·범위·배열 길이를
확인하며 undeclared field를 거절한다. 따라서 Structured Outputs가 잘못 동작해도
허용되지 않은 `lead`나 상품별 `explanation`은 통과할 수 없다. QueryPlan은
결정론적 canonicalization과 Oracle을 거치고, 근거 답변은 기존 Answer Verifier를
통과한다. 검증 실패 시 기존 결정론적 fallback 정책을 유지한다.

## 4. 공식 경로 설정 gate

Core provider 설정은 다음 값을 받는다.

```text
FINANCE_AGENT_LLM_MODE=evaluation 또는 production
LLM_PROVIDER=hyperclova
HCX_MODEL=HCX-로 시작하는 공식 모델 ID
HCX_TIMEOUT_SECONDS=0보다 크고 300 이하인 값
```

FastAPI 배선은 이보다 더 엄격하다. `APP_ENV`와 `FINANCE_AGENT_LLM_MODE`가 같은
`evaluation|production`이어야 하고 `LLM_PROVIDER=hyperclova`,
`HCX_MODEL=HCX-007`이어야 한다. credential은 `CLOVASTUDIO_API_KEY_FILE`만 받으며
inline `CLOVASTUDIO_API_KEY`는 evaluation/production 설정 단계에서 거부한다. 공식
release Compose는 저장소 밖의 read-only host 파일을 Docker secret으로 연결하고,
애플리케이션은 AgentReleaseManifest·DeploymentBinding·승인 DB 검증 뒤에만 그 파일을
읽는다. caller가 inline key로 만든 임의 HTTP transport 또는 외부 Agent를 주입하는 경로도
evaluation/production에서는 거부한다.

`FINANCE_BACKEND_ANSWER_PROVIDER=hyperclova`가 HCLX 답변 표현을 켠다.
`FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED`는 별도 선택값이며 기본 `false`다. 현재
QueryPlan은 서버 계획과 완전히 같을 때만 실행되므로 실제 생성 품질·latency·비용을
측정하기 전에는 켜지 않는다. 실제 endpoint는 보안을 위해 설정값으로 열지 않고 공식
HTTPS host로 고정한다.

QueryPlan 또는 Core의 optional grounded planning 호출에는 다음 gate(실행 전 허가 조건)가
모두 필요하다.

1. 배포 설정의 `FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=true`
2. 해당 provider가 명시적으로 주입됨
3. 서버 소유 `PlanningDecision.hclx_allowed=true`
4. 각 planning 경로가 허용하는 intent·상품군 조건 충족

`PlanningDecision`은 사용자 문구가 아니라 서버 정책이 만든다. 현재 정책에서는 이미
실행 가능한 deterministic fast path(규칙으로 명확히 해석된 빠른 경로)에만 HCLX 권한을
기록하며, Dense나 모델이 스스로 권한을 확대할 수 없다. 최종 계획은 다시
`PlanAuthorityGate`와 Oracle 앞의 검증을 통과해야 한다.

현재 이 권한 정책은 명확한 서버 계획과 같은지 평가하는 보수적 단계다. schema-link gap이나
복수 해석을 모델이 임의로 구제하도록 열지 않는다. 향후 grounded planning을 실제 HCLX에
연결하려면 `query_plan`과 `grounded_plan`의 operation별 권한·최대 호출 1회·provider 우선순위,
Prompt/schema hash를 release manifest에 별도로 고정해야 한다.

## 5. 공식 HTTP 요청·응답 계약

고정 endpoint와 인증은 다음과 같다.

```text
POST https://clovastudio.stream.ntruss.com/v3/chat-completions/{modelName}
Authorization: Bearer <CLOVA Studio API Key>
Content-Type: application/json
X-NCP-CLOVASTUDIO-REQUEST-ID: <서버 생성 UUID>
```

구형 `clovastudio.apigw.ntruss.com`과
`X-NCP-CLOVASTUDIO-API-KEY`/API Gateway key 조합은 사용하지 않는다.

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

Direct v3 body에는 system/user message, `maxCompletionTokens`, 고정 생성 옵션,
`thinking: {"effort": "none"}`와
`responseFormat: {"type": "json", "schema": ...}`를 넣는다. Structured Outputs와
활성화된 Thinking 추론을 동시에 사용하지 않도록 effort를 명시적으로 `none`으로 둔다.

HTTP transport는 `result.message.content`, `finishReason`, `result.usage`를 위 정규화
형태로 변환한다. HTTP 2xx여도 service code가 `20000`이 아니거나 role이 assistant가
아니거나 finish reason이 stop이 아니면 실패다. token 합계가 입력과 출력의 합과
다르거나 request ID가 안전하지 않거나 content가 없으면 응답 계약 오류로 차단한다.
응답은 최대 2 MB까지만 읽고 redirect는 따르지 않는다.

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

planning provider의 timeout은 안전한 서버 계획으로 조용히 바꾸지 않고 상위 request
deadline으로 그대로 전파한다. 즉, 시간 예산을 넘긴 호출을 성공처럼 숨기지 않는다.
반대로 timeout이 아닌 transport·schema·adapter 오류는 모델에 실행 권한을 주지 않은 채
독립적으로 만든 deterministic server plan(서버 규칙 계획)으로 fallback한다. 단,
QueryPlan provider가 정상 응답했지만 서버 계획과 내용이 다르면 오류 fallback이 아니라
의도 충돌로 보고 Oracle 실행 전에 CLARIFY로 종료한다.

## 7. API 없이 검증한 provider·HTTP 계약

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
- 공식 URL·header·camelCase body와 HCX-007 Structured Outputs 변환
- Bearer key와 오류 body가 repr·예외·관측 record에 노출되지 않는지 확인
- redirect 차단, response 2 MB 제한, timeout·연결 오류 정규화
- service code·role·finish reason·usage와 로컬 response schema 재검증

fake 응답이 통과한다는 사실은 API key 인증, 모델 사용 권한이나 실제 HCLX 생성
품질을 뜻하지 않는다. 상위 Agent와 공식 wire adapter가 예상 응답과 실패를 안전하게
처리한다는 계약 테스트다.

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
- QueryPlan·grounded planning은 `PlanningDecision.hclx_allowed` 없이는 무호출
- planning timeout은 상위 deadline으로 전파하고, 비-timeout provider 장애만 서버 계획으로 fallback
- 모든 fake transport 요청에서 실제 네트워크와 credential 사용 0건

재현 집계는
[`hcx-contract-e2e-v1.json`](../evaluation/baselines/hcx-contract-e2e-v1.json)에
보존한다. 이 결과는 합성 SQLite와 fake 응답을 사용한 시스템 계약 회귀이며,
실제 HyperCLOVA X 생성 성능이나 API 호환성 점수가 아니다.

## 9. Backend `/answer` 오류 adapter

provider 예외를 그대로 웹 계층에 올리지 않도록 프레임워크 독립
`execute_answer_request()`를 추가했다. planning 단계의 timeout은 상위로 전파되어
안전한 timeout `error` DTO와 HTTP 504로 변환된다. timeout이 아닌 QueryPlan·grounded
planning provider 장애는 독립 서버 계획으로 복구하므로 Oracle에 모델 계획을 넘기거나
HTTP 500을 만들지 않는다. grounded answer 단계의 provider 오류도 이미 검증된 evidence가
있으므로 결정론적 fallback으로 복구해 HTTP 200을 유지한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

`answer-adapter-contract-12`는 provider 설정·인증·rate limit·서비스·timeout·
transport·응답 오류, dataset 장애, 알 수 없는 예외와 answer fallback을
12/12 통과한다. 질문·credential·provider 오류 본문·파일 경로는 공개 오류에
포함하지 않는다. 실제 FastAPI route에는 answer-only HCLX provider를 조립했고,
QueryPlan provider는 별도 flag로 분리했다. 사용자 인증과 실제 네트워크 호출 검증은
여전히 이 계약의 바깥이다.

## 10. HTTP 배선 이후 남은 작업

1. 테스트 API key를 안전하게 보관하고 팀 승인 후 최소 한 건 호출
2. 실제 인증·HCX-007 사용 권한·응답 schema와 latency 확인
3. QueryPlan을 끈 answer-only 경로부터 공개 회귀와 독립 평가
4. token·latency·오류·fallback·예상 비용 측정
5. 의미상 같은 QueryPlan 표현 차이와 strict equality 실패 유형 분석
6. 필요할 때만 전체 request deadline 안의 제한적 retry 설계
7. QueryPlan flag 활성화 여부를 독립 blind 결과로 결정
8. 로컬 Qwen 결과와 분리된 HyperCLOVA X 평가 baseline 기록
9. 실제 route를 포함한 Docker/NCP HTTP E2E 검증
10. 제출 후보에서 로컬 LLM provider·설정·스크립트·의존성을 제거하고
    clean checkout에서 재검증

최초 실제 호출이 성공하기 전에는 “HyperCLOVA X API 연결 검증 완료”라고 표현하지
않는다. 현재 정확한 상태는 “공식 HTTP transport와 FastAPI 배선·무호출 계약 검증
완료”다.

공식 근거:

- [CLOVA Studio API 개요](https://api.ncloud-docs.com/docs/ai-naver-clovastudio-summary)
- [Structured Outputs](https://api.ncloud-docs.com/docs/clovastudio-chatcompletionsv3-so)
- [API 키](https://guide.ncloud-docs.com/docs/clovastudio-apikey)
