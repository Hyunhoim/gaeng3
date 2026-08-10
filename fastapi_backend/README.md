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
├── start.sh                     # 컨테이너 시작 명령
├── .env.example                 # Compose 환경변수 예시
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

네 SQLite가 단순히 설정됐는지만 확인하지 않고 각 manifest의 상품군까지 검증

- 모두 준비됨: HTTP 200, `status=ok`
- 하나라도 누락·불일치: HTTP 503, `status=degraded`
- 파일 경로와 내부 오류 정보는 공개 응답에 노출하지 않음
- `fund_execution_policy`는 공모펀드가 기본 잠금인지, 명시적 버전 승인으로 열렸는지 표시

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
다섯 필드만 갖고 모두 문자열이다. 정상 검색, 결과 없음, 역질문, 미지원,
내부 오류도 같은 다섯 필드와 HTTP 200을 반환한다. 정의되지 않은 추가
query parameter는 무시한다.

`retrieved_context`와 `think_trace`는 JSON을 문자열로 직렬화한 값이다.
`think_trace`는 모델의 숨은 사고과정이 아니라 질문 분류·필터·검증·fallback 결과처럼
다시 확인할 수 있는 실행 기록만 담는다.

전체 처리의 바깥쪽 제한은 기본 55초다. 시간이 다 되면 근거가 없는 안전한 시간 초과
답변을 HTTP 200으로 먼저 반환한다. 실행 중인 작업을 강제로 종료하는 방식은 아니므로,
실제 HyperCLOVA X 연결 때는 모델 호출 제한을 이 값보다 짧게 설정하고 동시 요청 수를
별도로 제한해야 한다.

## 5. 전체 시스템에서 실행

일반적인 실행·종료·로그 확인은 저장소 루트에서 수행

```bash
test -f fastapi_backend/.env || \
  cp fastapi_backend/.env.example fastapi_backend/.env
./compose.sh up --detach --wait
```

이미지 빌드, 공식 XLSX 정규화·검증, Backend 시작은 루트 스크립트가 순서대로 처리.
자세한 사용법은 [루트 전체 시스템 실행](../README.md#5-전체-시스템-실행) 참고

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

스모크는 `/health`, 내부 `POST /answer` 7건과 공식 `GET /answer` 7건을 함께
검사. 공식 GET에는 정상 질문뿐 아니라 결측·공백·길이 초과·유니코드·마크업 형태
입력을 포함하며, 어떤 경우에도 HTTP 200과 다섯 문자열 계약이 유지되는지 확인

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

## 8. 주요 환경변수

환경변수 예시는 `.env.example`, 개인 설정은 Git에서 제외되는 `.env`에서 관리

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `BACKEND_BIND_ADDRESS` | `127.0.0.1` | 호스트 바인딩 주소 |
| `BACKEND_PORT` | `18001` | 호스트에서 접근할 Backend 포트 |
| `FINANCE_RAW_DATA_DIR` | `../../2. Data/1. Raw/1.금융상품` | 읽기 전용 공식 XLSX 경로 |
| `WEB_CONCURRENCY` | `1` | Uvicorn worker 수 |
| `OFFICIAL_ANSWER_TIMEOUT_SECONDS` | `55` | 평가용 GET의 바깥쪽 응답 제한, 0초 초과 60초 미만만 허용 |
| `FINANCE_BACKEND_ANSWER_PROVIDER` | `deterministic` | 답변 provider, 기본은 모델 미사용 |
| `FINANCE_BACKEND_FUND_EXECUTION_POLICY` | `locked` | 공모펀드 실행 정책, 팀이 승인한 버전에서만 `public_fund_v1_approved` |

Compose에서는 네 DB를 전용 volume의 `/data/*.sqlite3`로 자동 연결하므로 DB 경로를
개인 `.env`에서 직접 지정하지 않음

공인망에 열어야 할 때만 `BACKEND_BIND_ADDRESS=0.0.0.0` 검토. 그 전에 인증·방화벽·
허용 IP·TLS 요구사항을 먼저 확정하며 연구실 서버에서는 `127.0.0.1` 유지

## 9. 개발 전용 로컬 Qwen 연결

로컬 Qwen은 HyperCLOVA X 연결 전 내부 개발에만 사용. 평가·제출·운영 경로가 아니며
`docker-compose.local-llm.yml`도 제출 후보에서 제거할 대상

Backend에서는 검증된 검색 결과와 field-level evidence를 설명하는 단계에만 연결.
검색·정렬·계산과 Result/Answer Verifier는 결정론적 경로를 유지

실행 순서와 장애 fallback 검증은
[로컬 LLM 테스트 런타임](../finance_agent/docs/local-llm.md)을 기준으로 사용

## 10. `nextjs-frontend` 연결 약속

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

## 11. 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| Docker API `permission denied` | `id`에 `docker` 그룹이 있는지 확인 후 재로그인 |
| `port is already allocated` | `.env`의 `BACKEND_PORT`를 사용하지 않는 포트로 변경 |
| `data-init` 실패 | `./compose.sh logs data-init`과 원천 XLSX 8개 확인 |
| `/health`가 503 | 응답의 누락 상품군과 data-init 로그 확인 |
| `/answer`가 422 | 요청 JSON과 `schema_version`, `request_id`, `question`, `locale` 확인 |
| 로컬 Qwen 장애 | 기본 결정론적 fallback 여부 확인 후 로컬 LLM 문서 참고 |
