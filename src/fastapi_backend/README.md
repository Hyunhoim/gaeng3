# fastapi_backend

`finance_agent_core`를 HTTP API로 제공하는 FastAPI Backend. 전체 실행은 [루트 README](../../README.md),
요청·응답 JSON 스키마는 [API 명세서](../../docs/평가용_API_서버_정보.md)를 따른다.
이 문서는 Backend의 책임, HTTP 계약, 평가 서버 배포, 테스트만 다룬다.

## 1. 역할

- `GET /health`, 평가용 `GET /answer`, 내부용 `POST /answer` route
- 요청 검증, 오류 응답 변환, 요청당 deadline과 동시 실행 상한
- Agent Core 의존성 조립과 시작 시 릴리스 봉인(manifest·binding·데이터 해시) 검증
- Docker 이미지, 컨테이너 시작 명령, 평가 서버 배포 스크립트

금융상품 SQL·검색·검증 로직은 `finance_agent_core`에 있다. Backend는 Agent 결과의 상품명·수치·근거를 수정하지 않는다.

## 2. 디렉터리 구조

```text
src/fastapi_backend/
├── app/
│   ├── main.py                  # FastAPI 앱 · 공통 오류 처리
│   ├── config.py                # 환경 설정 검증
│   ├── dependencies.py          # Agent 실행 객체 조립 · 릴리스 봉인 검증
│   ├── request_execution.py     # deadline · 동시 실행 상한 · 재시도 공유
│   └── routes/                  # health.py · answer.py
├── scripts/
│   ├── manual-release.sh        # 평가 서버 배포 (빌드 → 데이터 → 봉인 → 실행 → 확인)
│   └── smoke.py                 # 실행 중인 서버 HTTP 스모크
├── tests/                       # HTTP 계약 · 릴리스 · 런타임 테스트
├── Dockerfile                   # 타깃 final · adaptive-runtime (KURE 포함)
├── .env.example                 # 개발 Compose 환경변수
└── .env.release.example         # 평가 릴리스 환경변수 예시 (비밀값 없음)
```

## 3. HTTP API

### 3.1 `GET /answer` (평가용)

```bash
curl --get --fail-with-body \
  --data-urlencode 'question_id=Q-001' \
  --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘' \
  http://127.0.0.1:18001/answer
```

| 항목 | 값 |
| --- | --- |
| 파라미터 | `question_id`(최대 128자), `question`(최대 2,000자). 정의되지 않은 파라미터는 무시 |
| 응답 | `question_id` · `question` · `retrieved_context` · `think_trace` · `answer` 다섯 문자열 필드, `application/json; charset=utf-8` |
| 답변 불가 | 결과 없음·역질문·미지원·검증 실패도 같은 스키마와 HTTP 200 |
| 일시 장애 | provider·dataset 장애는 503, 전체 시간 초과는 504. 본문은 같은 다섯 문자열 |
| 시간 제한 | 내부 270초 (`OFFICIAL_ANSWER_TIMEOUT_SECONDS`). 주최 측 300초보다 짧게 둔다 |
| 호출 방식 | 순차 단일 호출. 같은 `question_id`·질문 재시도는 실행 중인 작업을 공유하고, 같은 ID에 다른 질문은 실행하지 않는다 |

`retrieved_context`는 검증된 상품 필드 값·비교·집계·문서 chunk·citation을 JSON 문자열로 담고,
`think_trace`는 질문 분류·필터·검증 결과처럼 다시 확인할 수 있는 실행 기록만 담는다.
원천 raw value·내부 경로·예외 문자열은 응답에 넣지 않는다.

### 3.2 `GET /health`

```bash
curl --fail http://127.0.0.1:18001/health
```

네 상품군 SQLite와 manifest, 관계 색인, 감사 sink를 확인한다. 모두 준비되면 HTTP 200
`status=ok`, 하나라도 누락·불일치면 HTTP 503 `status=degraded`. 주요 필드는
`ready_product_families`, `relation_retrieval_status`(`disabled|ready|degraded`),
`fund_execution_policy`, `audit_status`. `evaluation`·`production`에서는 AgentReleaseManifest와
승인 데이터 manifest의 source hash·행 수·기준일·DB hash가 모두 일치해야 준비 상태가 된다.

### 3.3 `POST /answer` (내부용)

`BackendAgentRequest` JSON(`schema_version`, `request_id`, `question`, `locale`)을 받아
구조화된 `BackendAgentResponse`(`status`, `products`, `comparisons`, `aggregates`,
`citations`, `as_of_dates`, `warnings`)를 돌려준다. 잘못된 입력은 HTTP 422. 평가 대상이 아니다.

## 4. 답변 처리 순서

```text
HCX-007 Intent 분석 (의도 + Semantic Frame) → 서버 Intent 교차검증
→ 서버 Typed QueryPlan → SQLite Oracle / Python Verifier → 근거 · citation
→ (필요 시) 승인 문서 묶음 검색: KURE-v1 Document Dense + BM25
→ HCX-007 최종 답변 생성 → Answer Verifier → 공식 응답
```

- HCX endpoint는 `https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007`,
  `Authorization: Bearer`와 Structured Outputs를 사용한다
- Intent DTO는 `intent`, 상품군, 원문 evidence span, clarification 여부만 담는다.
  field·filter·정렬·limit·SQL 권한이 없고, 서버 계획과 충돌하면 SQL을 실행하지 않는다
- 최종 생성은 검증된 본문 SHA-256과 허용 lead를 검사한다. HCX가 상품명·수치·순위·출처를
  추가하거나 바꿀 수 없다. 두 HCX 호출 중 하나라도 실패하면 503/504를 반환한다
- `evaluation`·`production`에서 `hyperclova` 외 provider, inline `CLOVASTUDIO_API_KEY`,
  전체 HCX QueryPlan 생성은 시작 단계에서 거부된다. API 키는 read-only 파일로만 읽는다
- 외부 근거는 승인·봉인된 공식 문서 258건(`approved_corpus_cache_only`)만 조회한다.
  실시간 웹·NAVER 검색은 평가 환경에서 사용하지 않는다

## 5. 관계 검색 계약

"한국주택금융공사가 발행한 국내채권 3개 보여줘" 같은 발행사·운용사·기초지수·자산유형·
투자지역 질문은 상품 조건 검색과 다른 `KnowledgeQueryPlan` 경로를 쓴다.

- 관계 Router가 엔터티와 상품군을 확정한 뒤 관계 index(FTS)로 후보를 찾고, 정규화한
  엔터티 이름 전체가 정확히 같은 행만 반환한다. 일부 단어만 같은 이름은 0건
- 관계 후보와 공식 상품 DB의 교집합을 결정론적으로 검증하고 최종 문장만 HCX grounded
  answer 경계를 사용한다. 내부 경로·hash·예외 문자열은 응답에 노출하지 않는다
- 릴리스 manifest에 해시로 고정된 관계 계약과 실제 관계 index가 exact match여야 한다.
  시작 뒤 관계 index·상품 DB 파일이 바뀌면 `/health`와 관계 요청이 HTTP 503

## 6. 실행

### 6.1 개발 (Docker Compose)

저장소 루트에서 `./compose.sh up --detach --wait`를 실행한다([루트 README](../../README.md)).
기본 Compose는 `APP_ENV=development`로 HCX 자격증명 없이 결정론적 경로만 실행한다.
Docker 없이 실행할 때는 네 SQLite와 관계 색인을 먼저 만들고([Agent README](../finance_agent/README.md)),
`.env.example`의 `FINANCE_DB_*`·`FINANCE_RELATION_*` 경로를 환경변수로 설정한 뒤
`uvicorn app.main:app --app-dir src/fastapi_backend --port 18001`로 시작한다.

### 6.2 평가 서버 배포

[`scripts/manual-release.sh`](scripts/manual-release.sh)가 NCP 서버 한 대에서 다음을 순서대로 수행한다.

1. `build` — `Dockerfile` 타깃 `adaptive-runtime`으로 이미지 빌드 (clean git checkout 필요)
2. `data` — 원천 XLSX를 정규화·검증해 네 SQLite·관계 색인·관계 artifact를 데이터 volume에 생성
3. `release` — AgentReleaseManifest·DeploymentBinding·외부 지식 artifact를 생성하고 코드·DB·색인·문서 묶음·활성화 영수증을 SHA-256으로 결속
4. `secrets` — `SECRETS_DIR/clovastudio-api-key`를 컨테이너 uid 10001 전용 사본으로 준비
5. `run` — read-only 컨테이너 기동 후 `/health` 응답 대기
6. `smoke` — `GET /answer` 호출 확인

```bash
RAW_DATA_DIR=/path/to/2.금융상품_20260824 SECRETS_DIR=~/.secrets/finance-agent \
BIND_ADDRESS=0.0.0.0 PORT=80 EXTERNAL_MODE=approved_corpus_cache_only \
KURE_CACHE_DIR=/path/to/kure-cache \
APPROVED_CORPUS_MANIFEST=... APPROVED_CORPUS_ROOT=... APPROVED_DOCUMENT_DENSE=... \
DOCUMENT_CACHE_ACTIVATION=... APPROVED_TEMPORAL_RELATIONS=... \
  src/fastapi_backend/scripts/manual-release.sh all
```

`all` 대신 `build | data | release | secrets | run | smoke | stop | status`를 단계별로 실행할 수 있다.
산출물은 `RELEASE_DIR`(기본 `~/finance-agent-release`)에 놓인다. 봉인 값과 실제 파일이 하나라도
다르면 컨테이너는 시작하지 않는다. 승인 문서 묶음·KURE-v1 모델 캐시·API 키는 Git에 포함하지 않고
read-only로 마운트한다.

## 7. 주요 환경변수

평가 릴리스 값은 [`.env.release.example`](.env.release.example), 개발 Compose 값은
[`.env.example`](.env.example)에 있다.

| 변수 | 평가 릴리스 값 | 의미 |
| --- | --- | --- |
| `APP_ENV` | `evaluation` | 릴리스 봉인·provider 제한 강제 |
| `BACKEND_BIND_ADDRESS` / `BACKEND_PORT` | `0.0.0.0` / `80` | 평가자 접근 주소 (NCP ACG로 발신 IP 제한) |
| `OFFICIAL_ANSWER_TIMEOUT_SECONDS` | `270` | `GET /answer` 내부 시간 제한 |
| `OFFICIAL_ANSWER_MAX_INFLIGHT` / `WEB_CONCURRENCY` | `2` / `1` | 프로세스당 동시 Agent 작업 상한 / Uvicorn worker 수 |
| `FINANCE_BACKEND_ANSWER_PROVIDER` | `hyperclova` | 최종 답변 생성 provider |
| `FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED` / `FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED` | `true` / `false` | HCX Intent 분석 켬 / 전체 QueryPlan 생성은 `true`면 시작 거부 |
| `FINANCE_AGENT_LLM_MODE` / `LLM_PROVIDER` / `HCX_MODEL` | `evaluation` / `hyperclova` / `HCX-007` | LLM 경계 |
| `HCX_TIMEOUT_SECONDS` | `45` | HCX 단일 HTTP 요청 상한 |
| `CLOVASTUDIO_API_KEY_FILE` | `/run/secrets/clovastudio_api_key` | read-only 키 파일. inline 키는 거부 |
| `FINANCE_BACKEND_FUND_EXECUTION_POLICY` | `public_fund_v1_approved` | 공모펀드 실행 허용. `locked`면 공모펀드 질문은 unsupported |
| `FINANCE_ADAPTIVE_SEMANTIC_ENABLED` · `FINANCE_SCHEMA_DENSE_INDEX_FILE` · `..._SHA256` | `true` · KURE-v1 Schema Dense v9 | 필드 의미 연결 색인과 해시 |
| `FINANCE_KURE_CACHE_HOST_DIR` · `FINANCE_KURE_SNAPSHOT_MANIFEST_HOST_FILE` | 경로 | KURE-v1 모델 캐시 (read-only) |
| `FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE` · `..._SHA256`, `FINANCE_EXTERNAL_DOCUMENT_*` | 경로 · 해시 | 외부 지식 artifact, 승인 corpus manifest·원문·Document Dense 색인·활성화 영수증 |
| `FINANCE_RELEASE_MANIFEST_FILE` · `FINANCE_DEPLOYMENT_BINDING_FILE` · `..._SHA256` · `FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256` | 경로 · 해시 | 릴리스 봉인과 관계 artifact 신뢰원 |
| `FINANCE_AUDIT_HOST_DIR` | 경로 | UID/GID 10001, mode 0700 감사 로그 디렉터리 |

## 8. 테스트

```bash
umask 022; PYTHONPATH=src python -m pytest -q src/fastapi_backend/tests
```

실행 중인 서버에 대한 HTTP 스모크(`/health`, `POST /answer`, `GET /answer` 계약). 공모펀드
실행을 허용한 릴리스에는 `--expected-fund-execution-policy public_fund_v1_approved`를 추가한다.

```bash
python src/fastapi_backend/scripts/smoke.py --base-url http://127.0.0.1:18001
```
