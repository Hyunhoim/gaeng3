# HyperCLOVA X provider 계약

마지막 갱신: 2026-08-25

이 문서는 HyperCLOVA X provider, 공식 HTTP transport와 FastAPI 연결 경계를
설명한다. 2026-08-11 NAVER Cloud 공식 문서 기준 endpoint·Bearer 인증·Structured
Outputs 요청과 응답 형식을 반영했다. 2026-08-14에는 QueryPlan을 끈 answer-only
경로에서 실제 credential로 HCX-007 호출 3회를 수행했다. 고정 2,048 token 요청의
timeout 1회와 출력 예산 보완 뒤 DETAIL·SEARCH 성공 2회를 확인했으며, 이 결과는
연구실 검증이지 clean NCP release 또는 HCLX planning 품질 증거가 아니다.

2026-08-24 운영 공지에 따라 현재 공식 runtime은 모든 평가 요청에서 HCLX의 제한된
Intent 분석과 최종 답변 생성을 모두 필수로 사용한다. 위 answer-only 실호출 기록은 과거
배선의 증거이며, 새 두-operation 배선의 실제 과금 호출·NCP 배포 검증은 별도 수행 대상이다.

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
Intent analysis / 선택적 Semantic Resolver / final answer provider
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
```

provider는 API URL, 인증 header, 응답 wire 구조를 직접 알지 못한다.
`HyperClovaXTransport.complete()`가 의미 단위의 structured request를 실제
서비스 형식으로 변환하도록 분리했다. 실제 transport는 공식 host만 사용하고 redirect를
따르지 않으며, 상위 Agent와 verifier 계약은 그대로 유지한다.

공식 runtime의 `IntentAnalysisDraft`는 intent·상품군·질문 원문의 evidence span·
clarification 여부만 포함한다. field·filter·정렬·limit·SQL을 표현할 수 없으므로 HCLX가
실행 계획을 직접 만들 수 없다. 서버의 Ontology·Lexical·Schema Dense 경로가 만든 해석과
HCLX Intent가 충돌하면 Oracle을 실행하지 않고 `CLARIFY` 또는 `UNSUPPORTED`로 종료한다.

전체 `HyperClovaXQueryPlanProvider` 구현은 과거 실험·호환성 테스트를 위해 Core에 격리되어
있지만 FastAPI production 조립에서 제거됐다. `FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=true`는
설정·Release trust·launcher에서 모두 거부한다. 공모펀드 비교 초안과 기존 grounded-answer
provider도 공식 조립 권한이 없으며, 공식 답변은 모든 상태를 다루는 전용 final provider만 쓴다.

## 3. 공식 runtime에서 HyperCLOVA X가 맡는 역할

| operation | provider | 입력과 출력 |
| --- | --- | --- |
| `intent_analysis` | `HyperClovaXIntentAnalysisProvider` | 자연어 질문 → 실행 권한이 없는 제한된 Intent DTO |
| `semantic_resolver` | `HyperClovaXSemanticResolverProvider` | 서버가 허용한 Dense field 후보 중 하나 선택 또는 `CLARIFY` |
| `final_answer` | `HyperClovaXFinalAnswerProvider` | 검증된 본문 hash를 보존하며 허용된 최종 lead 생성 |

세 경로 모두 모델 출력 뒤에 HCX JSON schema를 서버에서 다시 검사하고 Pydantic
계약 검증도 수행한다. 서버 재검증은 object shape·type·enum·anyOf·범위·배열 길이를
확인하며 undeclared field를 거절한다. 따라서 Structured Outputs가 잘못 동작해도
허용되지 않은 intent·field·`lead`는 통과할 수 없다. 최종 응답은 HCLX가 돌려준 본문
SHA-256과 서버가 고정한 검증 본문을 대조한다. Intent 또는 최종 생성 호출 실패·schema
위반·hash 불일치는 503/504로 종료하며 공식 응답에서 deterministic fallback을 허용하지 않는다.

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

최종 evaluation/production Release는 다음 조합만 허용한다.

1. `FINANCE_BACKEND_ANSWER_PROVIDER=hyperclova`
2. `FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED=true`
3. `FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=false`
4. `FINANCE_AGENT_LLM_MODE`가 `APP_ENV`와 같고 `LLM_PROVIDER=hyperclova`
5. `HCX_MODEL=HCX-007`, credential은 승인된 key file만 사용

이 조합은 Settings, production assembly guard, AgentReleaseManifest, Release trust,
Compose launcher, immutable workflow에서 독립적으로 대조한다. 실제 endpoint는 보안을 위해
설정값으로 열지 않고 공식 HTTPS host로 고정한다.

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
`success`는 transport뿐 아니라 operation별 JSON schema·Pydantic Structured Output
검사까지 통과했음을 뜻한다. 잘못된 JSON·필드·enum은 `response_error`로 집계하며,
이후 상품 evidence와 최종 문장 검증 결과는 별도의 Verifier 사건으로 기록한다.

필수 Intent provider와 final provider의 timeout·인증 실패·429·5xx·schema 오류는 상위
request 경계로 그대로 전파한다. 즉, HCLX 호출 실패를 성공이나 deterministic fallback으로
숨기지 않는다. 공식 GET은 timeout을 504, 나머지 provider 오류를 503으로 반환해 주최 측의
재시도 계약과 일치시킨다. 단,
Intent provider가 정상 응답했지만 서버의 안전한 route와 다르면 오류 fallback이 아니라
의도 충돌로 보고 Oracle 실행 전에 `CLARIFY`로 종료한다.

## 7. API 없이 검증한 provider·HTTP 계약

테스트의 `FakeHyperClovaXTransport`는 네트워크를 전혀 사용하지 않고 정상 응답과
실패를 순서대로 재생한다. 현재 자동 테스트는 다음을 확인한다.

- 공식 mode와 provider 조합 외 실행 차단
- `HCX-` 모델 ID와 timeout 범위 검증
- `intent_analysis`, `semantic_resolver`, `final_answer`의 structured request와 schema subset
- Intent DTO가 field·filter·정렬·limit·SQL을 표현할 수 없는지 확인
- Semantic Resolver가 서버 제공 후보 밖 field를 선택하지 못하는지 확인
- final provider가 검증 본문 SHA-256과 허용 lead만 반환하는지 확인
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

## 8. 과거 QueryPlan 실험 회귀

과거 전체 QueryPlan 실험의 동작을 재현하기 위해 `hcx-contract-e2e-8`을 역사 회귀로
보존한다. 이 suite는 현재 production 조립이나 최종 Release 허용 근거가 아니다.

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
`execute_answer_request()`가 처리한다. Intent timeout은 SQL 전에, final timeout은 검증된
결과를 외부에 내보내기 전에 차단되어 HTTP 504가 된다. 인증·429·5xx·transport·schema·
hash 오류는 안전한 error DTO와 HTTP 503으로 변환된다. 필수 HCLX 경계가 실패한 요청은
결정론적 본문을 성공 응답으로 내보내지 않는다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

기존 adapter baseline은 역사 기록으로 유지한다. 현재 공식 GET E2E는 Intent·final 정상,
인증·rate limit·서비스·timeout·transport·schema 오류, no-fallback과 Audit 순서를
독립 검증한다. 질문·credential·provider 오류 본문·파일 경로는 공개 오류에 포함하지 않는다.

## 10. 최종 evaluation 동결과 남은 외부 검증

최종 evaluation은 **HCLX Intent 분석과 final generation을 항상 ON**으로 고정한다.
HCLX 전체 QueryPlan과 grounded planning, Product Dense는 OFF다. Schema Dense는 승인된
candidate-only adaptive 정책 안에서만 동작하고 HCLX Semantic Resolver는 별도 활성화 전까지
OFF다. 공모펀드는 `public_fund_v1_approved` Release에서만 실행한다.

남은 필수 작업은 다음 네 가지다.

1. 현재 변경을 clean commit으로 만들고 보호된 main에서 Manifest·OCI digest·Cosign
   서명을 발급한다.
2. 그 exact image를 NCP에 기동해 공인 IP의 `/health`와 공식 `GET /answer`를 검증한다.
3. 실제 HCLX Intent+final 정상·timeout·429·5xx가 새 Audit와 no-fallback 계약을
   지키는지 최소 호출로 확인한다.
4. 서명된 새 generation Binding을 사용해 N-1 → N → N-1 rollback과 순차 요청
   p50·p95·p99·payload·memory를 측정한다.

현재 정확한 상태는 “공식 HTTP transport·FastAPI Intent+final 배선과 fake/no-network
장애 계약 검증 완료; 새 배선의 실제 과금 호출과 clean signed NCP release 검증 대기”다.

공식 근거:

- [CLOVA Studio API 개요](https://api.ncloud-docs.com/docs/ai-naver-clovastudio-summary)
- [Structured Outputs](https://api.ncloud-docs.com/docs/clovastudio-chatcompletionsv3-so)
- [API 키](https://guide.ncloud-docs.com/docs/clovastudio-apikey)
