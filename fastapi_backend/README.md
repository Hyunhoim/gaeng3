# 금융상품 Agent 백엔드

기존 `finance_agent_core`를 HTTP API로 연결하는 FastAPI 실행 영역이다. 현재 구성은
예선 평가용 백엔드와 연구실 Ubuntu 서버 검증에 필요한 최소 범위만 포함한다.
프론트엔드, PostgreSQL, 인증 서버는 포함하지 않는다.

## 구조

```text
사용자 또는 평가 서버
  -> FastAPI (fastapi_backend/app)
  -> finance_agent_core
  -> /data/*.sqlite3 (읽기 전용)
```

Docker 이미지는 Python 3.12를 사용하고, 실행 시에는 권한이 제한된 비-root 사용자로
동작한다. Uvicorn은 개발용 자동 재시작 없이 production 방식으로 실행된다.

## 협업 경계

| 영역 | 담당 | 원칙 |
| --- | --- | --- |
| FastAPI·Docker·배포 | 임현호 | `fastapi_backend/`, `docker-compose.yml` 관리 |
| Agent·검색·검증 | 조해영 | `finance_agent/`의 Core와 평가 기준 관리 |
| 금융 질의 평가 | 박재모 | 독립 질의·정답 기준·사람 평가 결과 관리 |

Backend는 Agent 내부의 SQL·검색·검증 로직을 다시 구현하지 않는다. 다음 공개 계약만
사용한다.

- 요청: `BackendAgentRequest`
- 응답: `BackendAgentResponse`
- 실행: `RoutedFinanceAgent`
- HTTP 변환: `execute_answer_request()`

Backend는 `execute_answer_request()`가 반환한 HTTP 상태와 DTO 필드를 삭제하거나
재해석하지 않는다. 위 계약을 바꿔야 하면 Backend와 Agent 담당자가 먼저 합의하고,
계약 테스트와 [Backend DTO 문서](../finance_agent/docs/backend-contract.md)를 함께
수정한다.

## 현재 구현 상태

| 항목 | 상태 |
| --- | --- |
| `GET /health` | 구현 완료; 네 SQLite manifest와 상품군 일치 검증 |
| `POST /answer` | 구현 완료; 기존 Agent adapter 연결 |
| HTTP 계약 테스트 | 구현 완료; SSH 환경 실행 검증 대기 |
| Dockerfile·Compose | 구현 완료; SSH 서버 build·smoke test 대기 |
| HyperCLOVA X 실제 HTTP | 미연결; endpoint·인증 계약 확정 후 연결 |
| 사용자 인증·Frontend | 현재 예선 API 범위에서 제외 |

검증 전 항목을 완료로 기록하지 않는다. SSH 서버 검증이 끝나면 사용한 commit,
Docker 명령, health·answer 결과와 발견한 제약을 이 문서에 추가한다.

## API 계약 빠른 확인

요청 예시:

```json
{
  "schema_version": "1.0",
  "request_id": "manual-001",
  "question": "매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.",
  "locale": "ko-KR"
}
```

Docker 실행 후 호출 예시:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"schema_version":"1.0","request_id":"manual-001","question":"매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.","locale":"ko-KR"}' \
  http://127.0.0.1:18001/answer
```

응답의 `status`는 `success`, `clarification`, `unsupported`, `not_found`, `error` 중
하나다. `answer_mode`, `fallback_used`, `citations`, `as_of_dates`를 별도 필드로
확인하며, 답변 문자열만 보고 성공 여부를 판단하지 않는다.

입력 JSON이 DTO 규칙을 위반하면 HTTP 422와 같은 `BackendAgentResponse` 형식의
`status=error`, `error.code=invalid_request`를 반환한다. 검증 오류의 내부 위치나
입력값은 공개 응답에 반사하지 않으며, 유효한 `request_id`만 추적용으로 보존한다.

## 1. 사전 준비

Ubuntu 서버에 Docker Engine과 Docker Compose v2가 있어야 한다.

```bash
docker --version
docker compose version
```

정규화가 끝난 SQLite 파일 네 개를 한 디렉터리에 준비한다.

```text
finance_agent/artifacts/normalized/
├── overseas_etp.sqlite3
├── domestic_etp.sqlite3
├── bond.sqlite3
└── fund.sqlite3
```

파일 위치가 다르면 `.env`의 `FINANCE_ARTIFACTS_DIR`에 해당 디렉터리의 절대 경로를
지정한다. Compose는 이 디렉터리를 컨테이너의 `/data`에 읽기 전용으로 연결하므로,
API가 원본 DB를 수정할 수 없다.

정규화 도구가 보안을 위해 `0700` 디렉터리와 `0600` SQLite를 만들 수 있다.
`compose.sh`는 컨테이너 Backend를 현재 호스트 사용자와 같은 UID/GID로 실행해 이
권한을 그대로 유지하면서 DB를 읽는다. 데이터 권한을 `777` 또는 `666`으로 넓히지
말고 Docker 명령은 `sudo` 없이 아래 wrapper로 실행한다.

## 2. 환경 설정

저장소 루트에서 예시 파일을 복사한다.

```bash
cp fastapi_backend/.env.example fastapi_backend/.env
```

`.env`는 Git에 올리지 않는다. 포트나 데이터 경로를 바꿀 때 이 파일만 수정한다.

## 3. Docker 실행

저장소 루트에서 실행한다.

```bash
./fastapi_backend/compose.sh up --build --detach backend
./fastapi_backend/compose.sh ps
```

기본 포트는 서버 내부의 `127.0.0.1:18001`이다. API 상태와 문서는 다음 주소에서
확인한다.

```bash
curl --fail http://127.0.0.1:18001/health
curl --fail http://127.0.0.1:18001/openapi.json
```

`/health`는 네 DB 경로가 단순히 설정됐는지만 보지 않는다. 각 SQLite의 manifest를
읽고 상품군이 기대값과 일치할 때만 HTTP 200과 `status=ok`를 반환한다. 누락되거나
잘못된 DB가 하나라도 있으면 HTTP 503과 `status=degraded`를 반환하므로, Docker의
healthcheck가 실제 질의 가능 상태를 반영한다. 응답에는 파일 경로를 노출하지 않는다.

Swagger 문서 주소는 `http://127.0.0.1:18001/docs`이다. 서비스가 서버의 공인망에
직접 노출되지 않도록 기본 바인딩을 loopback으로 제한했다.

NCP 평가 서버에서 Public 통신을 열어야 할 때만 `BACKEND_BIND_ADDRESS=0.0.0.0`으로
변경한다. 그 전에는 인증·방화벽·허용 IP·TLS 요구사항을 먼저 확정해야 하며,
연구실 서버에서는 기본값 `127.0.0.1`을 유지한다.

노트북 브라우저에서 확인하려면 별도 로컬 터미널에서 SSH 터널을 연다.

```bash
ssh -L 18001:127.0.0.1:18001 infolab_hyunhoim
```

그 후 노트북에서 `http://127.0.0.1:18001/docs`를 연다.

로그 확인과 종료 명령은 다음과 같다.

```bash
./fastapi_backend/compose.sh logs --follow backend
./fastapi_backend/compose.sh down
```

## 4. Docker 없이 개발할 때

Python 3.12 가상환경을 활성화한 뒤 로컬 Agent core와 백엔드를 차례대로 설치한다.
`finance-agent-core`라는 이름의 별도 PyPI 패키지를 설치하면 안 된다.

```bash
python -m pip install -e ./finance_agent/packages/finance_agent_core
python -m pip install -e './fastapi_backend[dev]'
uvicorn app.main:app --app-dir fastapi_backend --host 127.0.0.1 --port 18001 --reload
```

테스트는 저장소 루트에서 실행한다.

```bash
python -m pytest fastapi_backend/tests
```

## 5. 주요 환경변수

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `BACKEND_BIND_ADDRESS` | `127.0.0.1` | 호스트 바인딩 주소; 연구실 서버는 기본값 유지 |
| `BACKEND_PORT` | `18001` | Ubuntu 서버에서 여는 loopback 포트 |
| `FINANCE_ARTIFACTS_DIR` | `./finance_agent/artifacts/normalized` | 서버의 SQLite 파일 디렉터리 |
| `FINANCE_DB_OVERSEAS_ETP` | `/data/overseas_etp.sqlite3` | 해외 ETP DB의 컨테이너 경로 |
| `FINANCE_DB_DOMESTIC_ETP` | `/data/domestic_etp.sqlite3` | 국내 ETP DB의 컨테이너 경로 |
| `FINANCE_DB_BOND` | `/data/bond.sqlite3` | 채권 DB의 컨테이너 경로 |
| `FINANCE_DB_FUND` | `/data/fund.sqlite3` | 펀드 DB의 컨테이너 경로 |
| `WEB_CONCURRENCY` | `1` | Uvicorn worker 수; 초기에는 1 유지 권장 |

## 템플릿 출처

Dockerfile, `start.sh`, Compose 중심 구조는 Vinta Software의
[`nextjs-fastapi-template`](https://github.com/vintasoftware/nextjs-fastapi-template/tree/62b67456e8f01760970455282282ecaa393fbd38)
커밋 `62b67456e8f01760970455282282ecaa393fbd38`을 참고해 이 프로젝트의
backend-only 구조로 다시 작성했다. 원 템플릿은 MIT License이며 자세한 고지는
루트의 `THIRD_PARTY_NOTICES.md`에 있다.
