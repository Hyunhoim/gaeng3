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

## 2. 환경 설정

저장소 루트에서 예시 파일을 복사한다.

```bash
cp fastapi_backend/.env.example fastapi_backend/.env
```

`.env`는 Git에 올리지 않는다. 포트나 데이터 경로를 바꿀 때 이 파일만 수정한다.

## 3. Docker 실행

저장소 루트에서 실행한다.

```bash
docker compose --env-file fastapi_backend/.env up --build --detach backend
docker compose --env-file fastapi_backend/.env ps
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
docker compose --env-file fastapi_backend/.env logs --follow backend
docker compose --env-file fastapi_backend/.env down
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
