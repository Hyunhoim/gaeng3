# Codex 실행 프롬프트 — gaeng3 저장소 부트스트랩과 첫 Agent Vertical Slice

버전: 2
기준일: 2026-07-28
상태: 완료된 실행 명세

> 이 프롬프트는 최초 Agent Core 부트스트랩 당시의 실행 기록이다. 현재 AI
> 작업공간은 저장소 루트 충돌을 피하도록 `finance_agent/`로 이동했다.
> 최신 경로와 실행 방법은 `finance_agent/README.md`와
> `finance_agent/docs/project-index.md`를 따른다.

## 1. 역할과 목표

당신은 제10회 미래에셋증권 AI Festival의 **금융 Agent - Product Finder: 채권, ETF, 데이터 기반** 과제를 구현하는 선임 AI·Backend 엔지니어다.

현재 목표는 전체 제품을 한 번에 완성하는 것이 아니다. 기존 팀원의 작업을 보존하면서 다음 결과를 만드는 것이다.

1. Conda + pip 기반의 재현 가능한 모노레포 개발 환경
2. 공식 과제 제약과 실제 데이터 품질을 반영한 계약
3. Mock으로 CI가 통과하는 Agent 수직 경로
4. 해외 ETP 한정 결정론적 검색 vertical slice
5. 선택적으로 격리된 로컬 LLM을 연결할 수 있는 provider interface
6. 이후 HyperCLOVA X로 교체 가능한 fail-closed 평가 경로

구현 전에 반드시 다음 문서를 읽는다.

- `docs/project-index.md`
- `docs/project-baseline.md`
- `docs/data-audit.md`
- `docs/research/2026-07-28-gpt-pro/README.md`

GPT Pro 원문이나 감사 번들의 초안을 정본으로 간주하지 않는다. 채택된 결정은 위의 현재 기준 문서를 따른다.

## 2. 절대 제약

- 평가·제출 경로에서 사용하는 LLM은 **HyperCLOVA X만 허용**한다.
- 다른 생성형 LLM 또는 VLM이 평가 경로에 들어가면 안 된다.
- 로컬 Qwen provider는 개발자가 명시적으로 켠 로컬 테스트에서만 허용한다.
- CI와 기본 개발 모드는 외부 secret·네트워크·GPU가 필요 없는 Mock/fixture를 사용한다.
- 수치 필터, 날짜 계산, 정렬, 집계, 순위, hard constraint 검증을 LLM에 맡기지 않는다.
- 답변의 숫자와 사실은 evidence에 존재해야 한다.
- 데이터가 없거나 의미가 불명확한 조건은 추정하지 않는다.
- 근거 없는 투자 추천, 수익 보장, 미래 수익률 단정을 생성하지 않는다.
- 공식자료·원천 ZIP·XLSX를 수정하거나 Git에 추가하지 않는다.
- API key, token, 비밀번호, 실제 endpoint credential을 코드·문서·로그에 넣지 않는다.
- 사용자 지시 없이 `git init`, clone, commit, push, branch 생성, remote 변경을 하지 않는다.

## 3. 작업 전 점검

읽기 전용으로 다음을 확인하고 먼저 짧게 보고한다.

1. 현재 사용자, 작업 경로, 디렉터리 트리
2. `gaeng3`가 Git 저장소인지 여부와 현재 tracked·untracked·modified 파일
3. remote가 있다면 `https://github.com/Hyunhoim/gaeng3`와의 관계
4. 기존 Next.js·FastAPI template 또는 동료 작업의 존재 여부
5. Conda, Python, pip, Node.js, npm, Git, Docker, Compose 버전
6. NVIDIA GPU·driver·CUDA는 로컬 LLM 검증을 요청받았을 때만 확인
7. 원천 데이터가 저장소 밖에 있고 읽기 전용으로 접근 가능한지
8. 80·443번이 아닌 사용자별 포트를 사용할 수 있는지

기존 파일이 있으면 구조와 설정을 먼저 이해한다. 템플릿을 다시 생성해 덮어쓰지 않는다. dirty worktree가 있으면 사용자 변경으로 간주하고 보존한다.

## 4. 로컬 배치와 경계

예상 구조:

```text
26-07 미래에셋증권AI공모전/
├── 0. Official Materials/       # 공식 원본, 저장소 밖
├── 1. Project Notes/            # 프로젝트 기록, 저장소 밖
├── 2. Data/                     # ZIP/XLSX, 저장소 밖
└── 3. Workspace/
    └── gaeng3/                  # 애플리케이션 저장소
```

원천 데이터는 절대경로로 고정하지 않는다.

```dotenv
PRODUCT_DATA_DIR=../../2. Data/1. Raw/1.금융상품
```

해당 경로가 없으면 데이터를 꾸며내지 말고 명확한 설정 오류를 낸다. 테스트는 원천 상품을 복제하지 않은 최소 합성 fixture를 사용한다.

다음 항목은 Git에서 제외한다.

- `*.xlsx`, `*.zip`, 원천·가공 금융상품 데이터
- `.env`, `.env.local`, secret·credential
- 로컬 DB, cache, log, build output
- 모델 weight, tokenizer cache, Hugging Face cache, vLLM log
- `*.safetensors`, `*.gguf`, `pytorch_model*.bin`
- Conda·venv 디렉터리

`docs/research/2026-07-28-gpt-pro/audit-bundle/`은 로컬 연구 기록이다. 공식 PDF 추출문과 파생 감사 자료의 배포 가능성을 확인하기 전에는 일괄 stage하지 않는다.

## 5. 개발 환경: Conda + pip

애플리케이션 환경과 GPU 추론 환경을 분리한다.

```text
environment.yml
environment.local-llm.yml
requirements/
├── base.txt
├── dev.txt
└── local-llm.txt
```

### 애플리케이션

- Conda 환경명: `gaeng3-dev`
- Conda가 Python과 pip의 주요 버전을 관리한다.
- pip가 FastAPI·데이터 처리·테스트 의존성을 관리한다.
- Node.js는 동료 템플릿의 NVM 설정을 존중하고 npm과 `package-lock.json`이 Frontend 의존성을 관리한다.
- 기존 template이 안정적으로 사용하는 버전이 있으면 무리하게 올리지 않고 선택 이유를 기록한다.
- Docker application image에는 `requirements/base.txt`만 설치하고 Conda와 vLLM을 강제하지 않는다.

### 로컬 LLM

- Conda 환경명: `gaeng3-llm-local`
- pip 의존성은 `requirements/local-llm.txt`로 분리한다.
- 목표 모델: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- 목표 runtime: vLLM, RTX 5090 2장, tensor parallel 2, 최대 32K context
- CUDA·PyTorch·vLLM 호환 조합을 실제 smoke test로 확인한 뒤 version을 고정한다.
- 모델 download와 GPU smoke test는 사용자 승인 또는 명시적 실행 요청 없이 수행하지 않는다.
- local-LLM 의존성을 CI와 application Docker image에 설치하지 않는다.

직접·전이 의존성을 재현 가능한 방식으로 고정하되, 검토하지 않은 `pip freeze` 전체를 그대로 넣지 않는다. 같은 패키지를 Conda와 pip에서 중복 관리하지 않는다.

## 6. LLM provider와 격리

공통 interface 아래 세 provider를 둔다.

```text
packages/finance_agent_core/src/finance_agent_core/llm/
├── base.py
├── factory.py
└── providers/
    ├── mock.py
    ├── local_test.py
    └── hyperclova.py

fastapi_backend/app/finance_agent/
└── adapter.py
```

기본 설정:

```dotenv
FINANCE_AGENT_LLM_MODE=fixture
ENABLE_NON_HCX_TEST_LLM=0
LLM_PROVIDER=mock
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1
LOCAL_TEST_LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
```

필수 guard:

- `fixture`: `mock`만 허용
- `local_test`: `ENABLE_NON_HCX_TEST_LLM=1`과 `LLM_PROVIDER=local_test`가 모두 필요
- local endpoint는 기본적으로 loopback만 허용
- `evaluation` 또는 production: `LLM_PROVIDER=hyperclova`가 아니면 시작 단계에서 실패
- 테스트 코드에서 production guard를 우회할 수 없게 한다.
- `/health`는 mode와 provider 종류만 반환하고 key·token·전체 endpoint를 노출하지 않는다.
- 로컬 모델 출력은 gold answer로 자동 저장하지 않는다.

provider 입력은 가능한 한 동일한 QueryPlan 요청 계약을 사용하되, HyperCLOVA X 전용 schema adapter와 로컬 OpenAI-compatible adapter를 분리한다.

## 7. 권장 저장소 구조

기존 template의 책임 경계를 보존하면서 다음 구성요소를 배치한다.

```text
fastapi_backend/
nextjs-frontend/
packages/
  finance_agent_core/
    src/finance_agent_core/
      audit/
      agents/
      domain/
      llm/
      repositories/
      tools/
      verification/
      evidence/
    tests/
      fixtures/
      contract/
      integration/
requirements/
scripts/
docs/
  prompts/
  research/
  architecture.md
  data-contract.md
  development.md
  evaluation.md
.github/workflows/
AGENTS.md
README.md
environment.yml
environment.local-llm.yml
compose.yaml
.env.example
.gitignore
Makefile
```

PostgreSQL·Redis의 호스트 노출은 최소화한다. 고정 `container_name`을 사용하지 않고 `COMPOSE_PROJECT_NAME`, Frontend·Backend 포트를 사용자별로 바꿀 수 있게 한다. 서비스는 기본적으로 `127.0.0.1`에만 바인딩한다.

## 8. 실제 데이터 계약

원천 datarows 합계는 145,393행이다.

| 상품군 | 원천 행 | 논리 구조 |
| --- | ---: | --- |
| 국내채권 | 42,394 | `PD_NO` 유일 |
| 국내 ETP | 1,734 | ETF 1,202·ETN 532 |
| 해외 ETP | 5,646 | ETF 5,587·ETN 59 |
| 공모펀드 | 95,619 | `itm_no` 기준 논리 상품 11,138개와 다중 속성 |

반드시 반영할 규칙:

- 국내채권: 값이 채워진 881행, 양수 매수 가능 수량 325행, 스냅샷 기준 만기 전 후보 254행을 구분한다.
- 국내 ETP: Excel 1,155행 손상 레코드를 격리한다. 총보수는 217행에만 존재한다.
- 해외 ETP: sparse 10행을 검사한다. 총보수 0인 363개는 의미 확인 전 `UNKNOWN`이다. 1일 수익률 상수 0을 비교에 사용하지 않는다.
- 공모펀드: `itm_no` 한 행의 상품 테이블과 별도 속성 테이블로 정규화한다. Excel 84,563행 손상 레코드를 격리한다. raw-row coverage를 사용하지 않는다.
- `0`, 빈 문자열, `NULL`, parse error, sentinel을 구분한다.
- 필드마다 `VALID`, `PARTIAL`, `UNKNOWN`, `INVALID`, `STALE`, `UNSUPPORTED` 품질 상태를 유지한다.
- 원천 key·테이블·값·정규화값·단위·기준일을 provenance로 보존한다.

감사 스크립트를 구현할 때 `/mnt/data`, 특정 사용자 절대경로, `(1).xlsx` 파일명에 의존하지 않는다. 입력·출력 경로, parser 버전, 입력 SHA-256을 manifest에 기록한다.

## 9. Agent 아키텍처

구현 흐름:

```text
질문
→ lexical/schema linker
→ provider가 Typed QueryPlan 생성
→ server-side strict validation
→ parameterized SQL 또는 deterministic tool
→ independent result verifier
→ field-level evidence builder
→ provider 또는 template renderer
→ citation·number·finance-language validator
→ AgentResponse
```

다음 계약을 분리한다.

1. `queryplan.hcx.schema.json`: HCX Structured Outputs 호환 subset
2. 서버 Pydantic 모델 또는 strict schema: 전체 semantic validation
3. `field_registry.yaml`: alias, 타입, 단위, enum, 연산자, coverage, sentinel, freshness, 비교 가능 범위
4. evidence DTO: 원천 key와 사용 필드 단위 근거

HCX 전송 schema에는 지원 여부가 확인되지 않은 `additionalProperties`, `const`, `minLength`, `maxLength`, `uniqueItems`, `pattern`, `oneOf`, nullable union을 넣지 않는다. 필요한 엄격성은 서버 검증에서 복구한다.

QueryPlan 최소 의미:

- `intent`: search, compare, aggregate, explain 등
- `product_families`
- `constraints`
- constraint strength: `locked`, `ask_before_relaxing`, `preference`
- `ranking`, `projection`, `limit`
- `ambiguities`, `unsupported_conditions`
- intent별 payload

LLM이 만든 SQL 문자열은 실행하지 않는다. 검증된 QueryPlan만 allowlist compiler가 parameterized query로 바꾼다.

## 10. 첫 vertical slice

대표 질문:

> 미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.

완료 동작:

1. ETF만 선택하고 ETN 제외
2. 미국, 채권형, 거래 가능 상태를 field registry로 해석
3. 총보수 `<= 0.20%` 결정론적 필터
4. AUM 내림차순과 안정적인 tie-break
5. 상위 5개 반환
6. 동일 조건으로 결과 재검증
7. 상품명, 티커·식별자, 총보수, AUM, 원천, 사용 필드, 기준일 반환
8. 총보수 0 후보는 의미 확인 전 기본 결과에서 제외하거나 명시적 경고
9. 0건이면 조건을 자동 완화하지 않고 한 조건씩 바꾼 후보 수와 확인 요청 제공

감사 기준으로 0.20% 이하 원천 후보는 480개이며 그중 0 보수 후보 40개, 비영(非零) 확인 후보 440개다. 이 숫자를 fixture 정답으로 하드코딩하지 말고 raw-data audit 회귀 테스트에서 검증한다.

## 11. API 계약

최소 endpoint:

- `GET /health`: app, repository, mode, provider 준비 상태
- `GET /api/v1/products`: allowlist 구조화 필터와 pagination
- `POST /api/v1/chat`: 내부 Agent 계약
- `GET /answer`: 공식 평가 호환 adapter

응답에는 최소한 answer, status, parsed conditions, products, evidence, field dates, assumptions, unsupported conditions, error code를 포함한다. `think_trace`가 필요하면 숨은 사고과정이 아니라 다음 실행 사실만 기록한다.

- 해석된 상품군·조건
- 적용한 filter·sort·aggregate
- 호출한 도구와 원천
- verifier 결과
- 미지원·모호 조건

## 12. 테스트와 평가

기본 CI는 실제 원천 데이터·GPU·LLM secret 없이 통과해야 한다.

- Ruff와 pytest
- schema·Pydantic 계약 테스트
- 합성 fixture 적재·검색 테스트
- QueryPlan compiler SQL injection·allowlist 테스트
- hard constraint verifier 테스트
- evidence 숫자·인용 일치 테스트
- provider mode fail-closed 테스트
- `/answer` end-to-end Mock 테스트
- Frontend lint, type check, production build

별도 marker:

- `raw_data`: 로컬 원천 데이터가 있을 때만 실행
- `local_llm`: 명시적 opt-in과 GPU server가 있을 때만 실행
- `hyperclova`: credential과 공식 API 접근이 있을 때만 실행

평가 세트는 핵심 50개 질문으로 시작한다. parser, retrieval, hard violation, evidence, unsupported 처리, latency를 따로 측정하고 이후 250~400개 검토 세트로 확장한다.

## 13. 문서 산출물

- `README.md`: 설치, Conda + pip, 실행, 포트, 데이터 경로, 테스트, 현재 범위
- `docs/architecture.md`: provider·QueryPlan·SQL·verifier·evidence 흐름
- `docs/data-contract.md`: 상품군 grain, 품질 상태, 격리, 기준일, 지원 범위
- `docs/development.md`: 애플리케이션 환경과 로컬 LLM 환경의 분리
- `docs/evaluation.md`: 평가 세트, oracle, 지표, marker
- `AGENTS.md`: 안전 규칙, 명령, 정본 링크, 평가 LLM 제약

문서는 구현된 것과 계획만 있는 것을 명확히 구분한다.

## 14. 실행 순서

1. preflight와 기존 작업 보존 보고
2. 저장소·환경·설정·Git 제외 규칙
3. 재현 가능한 data audit와 계약 테스트
4. field registry와 QueryPlan 이중 schema
5. 해외 ETP 정규화와 oracle search
6. verifier와 evidence
7. Mock 기반 내부 API와 `/answer`
8. 최소 Frontend 연결
9. 선택적 local-test provider adapter
10. 테스트·문서·완료 보고

로컬 모델 download·server 실행과 실제 HyperCLOVA X 연동은 별도의 명시적 요청이 있을 때 수행한다.

## 15. 완료 조건

- 기존 팀원 파일과 변경이 보존되어 있다.
- Conda + pip 애플리케이션 환경을 재현할 수 있다.
- local-LLM 환경이 애플리케이션·CI에서 분리되어 있다.
- secret, raw data, model weight가 Git 대상에 없다.
- QueryPlan HCX schema와 서버 strict model이 분리되어 있다.
- 해외 ETP vertical slice가 결정론적 검색·검증·evidence를 수행한다.
- 0건과 미지원 조건을 조용히 완화하거나 추정하지 않는다.
- 기본 CI가 Mock/fixture만으로 통과한다.
- 평가 mode가 HyperCLOVA X 이외 provider를 차단한다.
- 실행하지 못한 검증은 원인과 재실행 명령을 남긴다.

## 16. 완료 보고

다음을 간결히 보고하고 현재 단계에서 멈춘다.

- 생성·수정한 주요 파일
- 보존한 기존 작업과 Git 상태
- 구현된 기능과 명시적 미구현 범위
- 반영한 데이터 품질 규칙
- 실행한 검증과 결과
- 실행하지 못한 검증과 이유
- 다음 사람이 실행할 명령
- 8월 6일 공식 확인이 필요한 결정
