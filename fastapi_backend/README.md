# fastapi_backend

`finance_agent_core`를 HTTP API로 제공하는 FastAPI Backend 작업공간

프로젝트 전체 실행은 [루트 README](../README.md)에서 관리. 이 문서는 Backend의
책임, API 계약, 개별 개발·테스트와 장애 확인 방법만 설명

## 1. 이 디렉터리의 역할

### Backend가 담당하는 것

- `GET /health`, 내부용 `POST /answer`, 평가용 `GET /answer` FastAPI route
- 요청 JSON 검증과 오류 응답 변환
- Agent Core 의존성 생성과 실행 수명주기
- Docker 이미지와 Backend 시작 명령
- 네 SQLite의 준비 상태와 상품군 manifest 확인
- HTTP 계약 테스트와 실행 중인 컨테이너 스모크 테스트
- 향후 Next.js 서버와 연결할 안정적인 응답 DTO 제공

### Backend가 담당하지 않는 것

- 금융상품 SQL·검색·정렬·계산 로직 재구현
- Agent 결과의 상품명·숫자·근거 임의 수정
- Frontend 화면과 사용자 경험
- PostgreSQL, 회원·인증과 운영 인프라

## 2. 디렉터리 구조

```text
fastapi_backend/
├── app/
│   ├── main.py                  # FastAPI 앱과 공통 오류 처리
│   ├── config.py                # 환경 설정 검증
│   ├── dependencies.py          # Agent 실행 객체 생성
│   └── routes/                  # health·answer route
├── tests/                       # Backend 단위·계약 테스트
├── scripts/smoke.py             # 실행 중인 HTTP API 스모크
├── Dockerfile                   # Backend 이미지
├── Dockerfile.release           # manifest를 넣는 digest 기반 release layer
├── start.sh                     # 컨테이너 시작 명령
├── .env.example                 # Compose 환경변수 예시
├── .env.release.example         # 비밀값이 없는 release identity 예시
├── docker-compose.release.yml   # mutable build를 제거하는 공식 override
├── docker-compose.local-llm.yml # 개발 전용 로컬 Qwen override
├── compose.sh                   # 과거 명령 호환용, 루트 스크립트로 위임
└── pyproject.toml               # Backend Python package·의존성
```

전체 서비스 정의는 저장소 루트 `docker-compose.yml`, 공식 실행 진입점은 루트
`compose.sh`에서 관리

## 3. Agent Core 연결 계약

Backend는 다음 Agent 공개 계약만 사용

- 요청: `BackendAgentRequest`
- 응답: `BackendAgentResponse`
- 실행: `RoutedFinanceAgent`
- HTTP 변환: `execute_answer_request()`

`execute_answer_request()`가 반환한 HTTP 상태와 DTO 필드를 삭제하거나 재해석하지 않음.
계약을 바꾸려면 Agent와 Backend 담당자가 합의하고 다음 항목을 함께 수정

- [Backend DTO 문서](../finance_agent/docs/backend-contract.md)
- Agent Core 계약 테스트
- `fastapi_backend/tests/`
- Frontend의 응답 타입과 렌더링 분기

## 4. HTTP API

### `GET /health`

네 SQLite가 단순히 설정됐는지만 확인하지 않고 각 manifest의 상품군까지 검증한다.
`APP_ENV=evaluation` 또는 `production` release에서는 `AgentReleaseManifest`와 패키지에
동결된 공식 데이터 승인 manifest의 source hash·행 수·기준일·DB hash가 모두
일치해야 준비 상태가 된다.

- 모두 준비됨: HTTP 200, `status=ok`
- 하나라도 누락·불일치: HTTP 503, `status=degraded`
- 파일 경로와 내부 오류 정보는 공개 응답에 노출하지 않음
- `fund_execution_policy`는 공모펀드가 기본 잠금인지, 명시적 버전 승인으로 열렸는지 표시
- `audit_status`와 `shadow_status`는 각각 `disabled|ok|degraded`만 표시
- 활성 Shadow의 queue drop, artifact·embedding 오류, audit correlation 실패, worker
  death·stall은 답변을 바꾸지 않지만 `shadow_status=degraded`와 HTTP 503으로 운영자에게 알림

종료 시에는 새 요청을 멈춘 뒤 `요청 worker → Shadow worker → audit sink` 순서로 정리한다.
세 단계는 하나의 timeout 예산을 공유해 Shadow의 마지막 audit event가 저장되기 전에 sink가
닫히거나 단계별 timeout이 중복 소비되지 않게 한다. 현재 evaluation/production에서는
Schema Dense와 Shadow가 OFF이고 observer 주입도 시작 단계에서 차단된다.

```bash
curl --fail http://127.0.0.1:18001/health
```

### `POST /answer`

요청 예시

```json
{
  "schema_version": "1.0",
  "request_id": "manual-001",
  "question": "매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.",
  "locale": "ko-KR"
}
```

호출 예시

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"schema_version":"1.0","request_id":"manual-001","question":"매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.","locale":"ko-KR"}' \
  http://127.0.0.1:18001/answer
```

응답 `status` 종류

| 값 | 의미 |
| --- | --- |
| `success` | 검색·검증을 통과한 정상 응답 |
| `clarification` | 조건이 부족하거나 모호해 추가 확인 필요 |
| `unsupported` | 예측·단정 추천 등 지원하지 않는 요청 |
| `not_found` | 유효한 조건이지만 일치 상품 없음 |
| `error` | 요청 형식 또는 내부 처리 오류 |

화면과 호출자는 답변 문자열만 보지 않고 `answer_mode`, `fallback_used`, `products`,
`comparisons`, `aggregates`, `citations`, `as_of_dates`, `warnings`, `error`도 함께 확인

입력 DTO가 잘못되면 HTTP 422와 `status=error`, `error.code=invalid_request` 반환.
검증 오류의 내부 위치와 입력값은 공개 응답에 그대로 반사하지 않음

Swagger UI는 `http://127.0.0.1:18001/docs`, OpenAPI JSON은
`http://127.0.0.1:18001/openapi.json`에서 확인

### 평가용 `GET /answer`

주최 측 평가 규격에 맞춰 `question_id`와 `question`을 query parameter로 받는다.

```bash
curl --get --fail-with-body \
  --data-urlencode 'question_id=Q-001' \
  --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘' \
  http://127.0.0.1:18001/answer
```

응답은 `question_id`, `question`, `retrieved_context`, `think_trace`, `answer`의
다섯 필드만 갖고 모두 문자열이다. 정상 검색, 결과 없음, 역질문, 미지원과 재시도해도
회복되지 않는 오류는 같은 다섯 필드와 HTTP 200을 반환한다. 일시적인 provider·dataset·
과부하 장애는 HTTP 503, 전체 시간 초과는 HTTP 504를 쓰되 본문은 같은 다섯 문자열을
유지한다. 정의되지 않은 추가 query parameter는 무시하며
`Content-Type: application/json; charset=utf-8`을 명시한다.

`retrieved_context`와 `think_trace`는 JSON을 문자열로 직렬화한 값이다.
`retrieved_context`에는 검증된 상품 field 값·비교·집계·문서 chunk·citation을
고정 상한 안에서 담고 원천 raw value와 내부 경로는 넣지 않는다.
`think_trace`는 모델의 숨은 사고과정이 아니라 질문 분류·필터·검증·fallback 결과처럼
다시 확인할 수 있는 실행 기록만 담는다.

전체 처리의 바깥쪽 제한은 주최 측 300초보다 여유를 둔 기본 270초다. 시간이 다 되면
근거가 없는 공식 다섯 문자열 안전 응답을 HTTP 504로 반환한다. 실행 중인 작업을 강제로
종료하는 방식은 아니므로 실제 HyperCLOVA X 호출 제한은 이 값보다 짧게 설정하고 동시
요청 수를 별도로 제한해야 한다.

평가기 재시도가 같은 `question_id`와 질문을 다시 보내면 실행 중인 작업을 공유하거나
300초 안의 안전한 완료 결과를 재사용한다. 같은 ID에 다른 질문이 오면 실행하지 않는다.
재시도 가능한 503·504 결과는 저장하지 않아 다음 순차 시도가 다시 실행될 수 있다.

## 5. 전체 시스템에서 실행

일반적인 실행·종료·로그 확인은 저장소 루트에서 수행

```bash
test -f fastapi_backend/.env || \
  cp fastapi_backend/.env.example fastapi_backend/.env
./compose.sh up --detach --wait
```

이미지 빌드, 공식 XLSX 정규화·검증, Backend 시작은 루트 스크립트가 순서대로 처리.
자세한 사용법은 [루트 전체 시스템 실행](../README.md#5-전체-시스템-실행) 참고

기본 `docker-compose.yml`은 `APP_ENV=development`인 로컬 개발 경로다. 기존 개인
`.env`에 `evaluation` 또는 `production`이 남아 있으면 다음 재생성부터 release 설정
없이는 의도적으로 시작이 실패하므로, 로컬 개발은 `APP_ENV=development`를 사용한다.
현재 실행 중인 컨테이너 설정은 파일을 수정했다고 자동으로 바뀌지 않는다.

평가·운영 release는 mutable local build를 허용하는 `compose.sh`가 아니라 다음 전용
진입점을 사용한다.

```bash
RELEASE_ENV_FILE=fastapi_backend/.env.release \
./compose-release.sh up --detach --wait
```

이 경로는 코드·Prompt·Model·index·공식 데이터 manifest가 고정된 image digest만
실행하며 release별 SQLite volume도 read-only로 연결한다. 따라서 해당 volume은
배포 전에 승인된 image/data 준비 job으로 생성·검증돼 있어야 한다. Manifest/Binding
생성 순서, localhost에서 완료한 Registry·합성 rollback 검증과 아직 남은 NCP 공식 검증은
[AgentReleaseManifest 배포 계약](../finance_agent/docs/agent-release-manifest.md)을 따른다.
HCLX release도 임의 `-f` override를 추가하지 않고 `.env.release`의 manifest와 동일한
provider profile 및 read-only host secret file 경로를 사용한다.

실제 evaluation/production host에서는 root 관리자가 다음 두 경로를 미리 만들고 일반
사용자가 쓰지 못하게 해야 한다.

```text
/var/lib/finance-agent-release/active-binding.json
/run/lock/finance-agent-release/activation.lock
```

`compose-release.sh up`은 이 host activation broker를 반드시 거친다. broker는 서명·hash
검증과 generation 전환을 lock 안에서 수행하고, 현재와 같은 Binding의 재시작 또는 현재보다
정확히 1 큰 새 Binding만 허용한다. 과거 Binding replay는 차단되며, rollback도 이전 image를
가리키는 **새 generation·새 서명 Binding**으로 수행한다. health 성공 뒤에만 활성 상태가
원자적으로 기록된다. 실제 NCP에서는 launcher·Python·Docker 실행 경로 자체도 root-controlled
설치본으로 배포해야 하며, 개발자가 쓸 수 있는 checkout에서 직접 root 실행하지 않는다.

`fastapi_backend/compose.sh`는 이전 명령과의 호환을 위해 루트 스크립트로 위임할 뿐임.
새 문서와 자동화에서는 사용하지 않음

## 6. Backend만 개발할 때

Python 3.12 가상환경에서 로컬 Agent Core와 Backend를 editable package로 설치

```bash
python -m pip install -e ./finance_agent/packages/finance_agent_core
python -m pip install -e './fastapi_backend[dev]'
```

저장소 루트에서 Backend 테스트 실행

```bash
python -m pytest fastapi_backend/tests
python -m ruff check fastapi_backend
python -m ruff format --check fastapi_backend
```

Docker 없이 서버를 실행하려면 네 SQLite 경로를 먼저 환경변수로 지정한 뒤 실행

```bash
uvicorn app.main:app \
  --app-dir fastapi_backend \
  --host 127.0.0.1 \
  --port 18001 \
  --reload
```

SQLite 생성 방법은 [Agent README](../finance_agent/README.md#7-공식-데이터로-agent-core만-실행)
참고

## 7. Docker HTTP 스모크

전체 컨테이너가 정상 실행된 뒤 저장소 루트에서 실행

```bash
python fastapi_backend/scripts/smoke.py \
  --base-url http://127.0.0.1:18001
```

다른 포트를 사용하거나 JSON 결과를 저장할 때

```bash
python fastapi_backend/scripts/smoke.py \
  --base-url http://127.0.0.1:18002 \
  --output /tmp/gaeng3-docker-http-smoke-v2.json
```

스모크는 `/health`, 내부 `POST /answer` 7건과 공식 `GET /answer` 8건을 함께
검사. 공식 GET에는 정상 질문뿐 아니라 결측·공백·길이 초과·유니코드·마크업 형태
입력과 공모펀드 잠금 질문을 포함하며, HTTP 200·다섯 문자열·UTF-8 Content-Type
계약이 유지되는지 확인

기본값은 `fund_execution_policy=locked`를 요구. 팀이 공모펀드 실행 정책을 명시한
개발 리허설에서는 다음 인자를 추가

```bash
python fastapi_backend/scripts/smoke.py \
  --base-url http://127.0.0.1:18002 \
  --timeout 180 \
  --success-answer-mode llm_grounded \
  --provider-model qwen3-local-test \
  --expected-fund-execution-policy public_fund_v1_approved
```

2026-08-07 실제 Docker에서 기본 결정론적·잠금 경로 14/14, Qwen·공모펀드 승인
경로 14/14, Qwen 중단 후 결정론적 fallback 경로 14/14를 확인. 공개 개발
스모크이므로 공식 점수·독립 blind·운영 지연 보장으로 해석하지 않음

### 동결 30문항 실제 GET 평가

단일 공식 GET뿐 아니라 설명회 분포를 모사한 동결 30문항을 실제 네트워크로
호출할 때는 `finance_agent/`에서 다음 채점기를 실행한다

```bash
python -m finance_agent_core.evaluation.official_mock_http_cli \
  --base-url http://127.0.0.1:18002 \
  --backend-profile local_test \
  --declared-model qwen3-local-test \
  --concurrency 2 \
  --expected-fund-execution-policy public_fund_v1_approved
```

이 채점기는 공식 다섯 문자열, 질문 보존, intent·상품군·후보 수, 상품 ID,
비교·집계 근거와 60초 예산을 함께 검사한다. 2026-08-07 최초 관측은 형식·시간
30/30, 의미 24/30이다. 여섯 실패는 모두 현재 Backend에서 의도적으로 잠근
공모펀드 정상 질문이며, 모델 오류나 HTTP 오류가 아니다. 자세한 해석은
[공식 형식 30문항 공개 모의평가](../finance_agent/docs/evaluation-official-mock.md)를 따른다

최초 결과를 보존한 뒤 공모펀드만 `public_fund_v1_approved`로 열고
같은 30문항을 재실행한 결과는 형식·시간·의미 30/30, 답변 생성
17/17, fallback 0건이다. 최신 동시성 2 재검증도 30/30, fallback 0건,
p95 약 3.01초·최대 약 3.04초로 통과. 단일 서버의 1회 관측이므로 부하 시험이나
운영 SLO가 아니며, 정책값은 팀의 배포 승인을 표현할 뿐 주최 측의 공식 사용
승인을 뜻하지 않음

저장소 루트에서 다음과 같이 새 프로세스에만 적용한다

```bash
FINANCE_BACKEND_FUND_EXECUTION_POLICY=public_fund_v1_approved \
./compose.sh -f docker-compose.yml \
  -f fastapi_backend/docker-compose.local-llm.yml \
  up --no-build --detach --force-recreate --wait backend

curl --fail http://127.0.0.1:18002/health
```

실험이 끝나면 환경변수와 override를 빼고 Backend를 재생성한 다음,
`fund_execution_policy=locked`를 확인한다

결정론적 API의 Router·SQLite·Oracle·Verifier·직렬화 구간 분해, 격리 Docker
benchmark·soak 실행법과 2026-08-14 검증 결과는 다음 문서를 따른다.

- [성능 원인 분해 실행 가이드](docs/deterministic-performance-decomposition.md)
- [성능 원인 분해·Audit 검증 결과](docs/deterministic-performance-audit-report-2026-08-14.md)

## 8. 주요 환경변수

환경변수 예시는 `.env.example`, 개인 설정은 Git에서 제외되는 `.env`에서 관리

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `BACKEND_BIND_ADDRESS` | `127.0.0.1` | 호스트 바인딩 주소 |
| `BACKEND_PORT` | `18001` | 호스트에서 접근할 Backend 포트 |
| `FINANCE_RAW_DATA_DIR` | `../../2. Data/1. Raw/1.금융상품` | 읽기 전용 공식 XLSX 경로 |
| `APP_ENV` | `development` | 기본 Compose는 로컬 개발. `evaluation`·`production`은 전용 release manifest·Binding을 강제 |
| `WEB_CONCURRENCY` | `1` | Uvicorn worker 수 |
| `OFFICIAL_ANSWER_TIMEOUT_SECONDS` | `270` | 평가용 GET의 바깥쪽 응답 제한, 0초 초과 300초 미만만 허용 |
| `OFFICIAL_ANSWER_MAX_INFLIGHT` | `2` | 프로세스당 동시 Agent 작업 상한(허용 1~8). timeout 뒤에도 실제 worker 종료까지 자리를 유지하며 초과 요청은 실행 전에 거절 |
| `FINANCE_BACKEND_ANSWER_PROVIDER` | `deterministic` | 답변 provider, 기본은 모델 미사용 |
| `FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED` | `false` | HCLX QueryPlan 선택 기능. 독립 품질·지연 평가 전에는 비활성 유지 |
| `FINANCE_AGENT_LLM_MODE` | `disabled` | HCLX 사용 시 `APP_ENV`와 같은 `evaluation` 또는 `production` |
| `LLM_PROVIDER` | `disabled` | HCLX 사용 시에만 `hyperclova` |
| `HCX_MODEL` | 미설정 | Structured Outputs 사용 경로는 현재 `HCX-007`만 허용 |
| `HCX_TIMEOUT_SECONDS` | `45` | HCLX 단일 HTTP 요청 상한. 전체 요청 deadline이 더 짧으면 자동 축소 |
| `CLOVASTUDIO_API_KEY_FILE` | 미설정 | evaluation/production에서만 허용되는 credential 파일의 컨테이너 내부 경로. inline `CLOVASTUDIO_API_KEY`는 거부 |
| `FINANCE_BACKEND_FUND_EXECUTION_POLICY` | `locked` | 공모펀드 실행 정책, 팀이 승인한 버전에서만 `public_fund_v1_approved` |
| `FINANCE_RELEASE_MANIFEST_FILE` | 미설정 | evaluation/production image 안의 read-only AgentReleaseManifest 경로 |
| `FINANCE_DEPLOYMENT_BINDING_FILE` | 미설정 | image digest와 manifest를 잇는 read-only DeploymentBinding 경로 |
| `FINANCE_DEPLOYMENT_BINDING_SHA256` | 미설정 | 배포 control plane이 주입하는 Binding file 신뢰 hash |
| `FINANCE_SOURCE_COMMIT` | 미설정 | clean release source commit |
| `FINANCE_RUNTIME_IMAGE_REFERENCE` | 미설정 | `repository@sha256` 형식의 실행 image |
| `FINANCE_RUNTIME_PLATFORM` | `linux/amd64` | Binding·Compose·runtime이 함께 확인하는 image platform |

Compose에서는 네 DB를 전용 volume의 `/data/*.sqlite3`로 자동 연결하므로 DB 경로를
개인 `.env`에서 직접 지정하지 않음

`OFFICIAL_ANSWER_MAX_INFLIGHT`는 Uvicorn **프로세스마다** 적용되므로 전체 이론 상한은
`WEB_CONCURRENCY × OFFICIAL_ANSWER_MAX_INFLIGHT`다. 현재는 예측 가능한 자원 사용을 위해
`WEB_CONCURRENCY=1`을 유지한다. 2026-08-10 격리 실측에서는 동일한 채권 검색을 8개씩
보냈을 때 동시 1·2개는 전부 성공했지만, 동시 4개부터 처리량이 감소하고 동시 8개는
10초 시험 제한에서 모두 timeout됐다. 따라서 기본값 2를 유지하고 실제 NCP 사양과 평가
호출 패턴을 확인한 뒤에만 상향한다.

공인망에 열어야 할 때만 `BACKEND_BIND_ADDRESS=0.0.0.0` 검토. 그 전에 인증·방화벽·
허용 IP·TLS 요구사항을 먼저 확정하며 연구실 서버에서는 `127.0.0.1` 유지

## 9. HyperCLOVA X 연결 경계

2026-08-11 공식 계약에 맞춰 Direct Chat Completions v3 HTTP transport와 FastAPI
의존성 주입을 구현했다. endpoint는
`https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007`로 고정하고,
`Authorization: Bearer`와 Structured Outputs를 사용한다. 구형 API Gateway URL과
구형 이중 인증 header는 사용하지 않는다.

기본 연결 순서는 다음과 같다.

```text
서버 QueryPlan → SQLite/Python → Verifier → HCLX grounded answer
→ 로컬 schema/Answer Verifier → 검증된 DB 수치·근거를 서버가 최종 조립
```

즉 HCLX는 상품명·수치·순위·출처를 새로 만들 수 없다. 허용 문장, result 순서,
evidence field와 warning이 하나라도 다르면 모델 초안을 폐기하고 결정론적 답변으로
fallback한다. QueryPlan 호출은 질문당 모델 호출 수와 지연을 늘리므로 별도 flag의
기본값을 `false`로 유지한다.

evaluation/production에서는 실제 키를 container environment에 넣지 않는다. 공식
`docker-compose.release.yml`이 저장소 밖의 read-only host 파일을 Docker secret으로
마운트하고, 애플리케이션은 AgentReleaseManifest·DeploymentBinding·승인 DB 검증을
끝낸 뒤에만 파일을 읽는다. inline `CLOVASTUDIO_API_KEY`는 설정 단계에서 거부한다.
caller가 API Key를 넣어 만든 임의 transport나 Agent를 주입하는 seam도 evaluation과
production에서는 거부하므로, 공식 조립은 `CLOVASTUDIO_API_KEY_FILE`을 읽는 한 경로뿐이다.

clean release artifact와 Git에서 제외되는 `.env.release`를 준비한 뒤 다음처럼
**release 설정만** 검증할 수 있다.

```bash
RELEASE_ENV_FILE=fastapi_backend/.env.release \
./compose-release.sh config --quiet
```

`.env.release`에는 credential 값이 아니라 `CLOVASTUDIO_API_KEY_HOST_FILE`의 절대
경로와 컨테이너 경로 `/run/secrets/clovastudio_api_key`만 기록한다. 위 명령은 HCLX를
호출하지 않는다. 애플리케이션 시작도 API healthcheck를 보내지 않지만, HCLX release로
실제 `/answer`를 요청하면 과금 가능한 호출이 발생한다. 최초 실제 호출은 팀 승인 후
한 건만 수행하고 인증·모델 사용 권한·응답 schema·latency를 별도 기록한다.

- [CLOVA Studio API 개요](https://api.ncloud-docs.com/docs/ai-naver-clovastudio-summary)
- [Structured Outputs](https://api.ncloud-docs.com/docs/clovastudio-chatcompletionsv3-so)
- [API 키 가이드](https://guide.ncloud-docs.com/docs/clovastudio-apikey)

## 10. 개발 전용 로컬 Qwen 연결

로컬 Qwen은 내부 개발 회귀에만 사용. 실제 HyperCLOVA X 평가·제출·운영 경로가 아니며
`docker-compose.local-llm.yml`도 제출 후보에서 제거할 대상

Backend에서는 검증된 검색 결과와 field-level evidence를 설명하는 단계에만 연결.
검색·정렬·계산과 Result/Answer Verifier는 결정론적 경로를 유지

실행 순서와 장애 fallback 검증은
[로컬 LLM 테스트 런타임](../finance_agent/docs/local-llm.md)을 기준으로 사용

## 11. `nextjs-frontend` 연결 약속

Frontend가 합류하면 다음 최소 범위부터 연결

- 화면 진입 시 `GET /health`로 상품군 준비 상태 표시
- 질문 입력에서 `POST /answer` 호출
- `status`별 정상·역질문·미지원·결과 없음·오류 화면 구분
- `products`, `comparisons`, `aggregates`는 값이 있을 때만 표시
- `citations`, `as_of_dates`, `warnings`, `fallback_used`를 숨기지 않음
- Frontend가 금융 조건·순위·평균을 다시 계산하지 않고 Backend DTO 사용
- Next.js 서버가 `http://backend:8000`으로 대리 요청해 내부 주소와 CORS 문제 분리
- Compose `frontend`는 Backend health 확인 후 연결

첫 통합 목표는 디자인 완성이 아니라 질문 한 건이 Frontend→Backend→Agent를 거쳐
근거 포함 응답으로 돌아오는지 확인하는 것

## 12. 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| Docker API `permission denied` | `id`에 `docker` 그룹이 있는지 확인 후 재로그인 |
| `port is already allocated` | `.env`의 `BACKEND_PORT`를 사용하지 않는 포트로 변경 |
| `data-init` 실패 | `./compose.sh logs data-init`과 원천 XLSX 8개 확인 |
| `/health`가 503 | 응답의 누락 상품군과 data-init 로그 확인 |
| `/answer`가 422 | 요청 JSON과 `schema_version`, `request_id`, `question`, `locale` 확인 |
| 로컬 Qwen 장애 | 기본 결정론적 fallback 여부 확인 후 로컬 LLM 문서 참고 |
