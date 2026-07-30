# gaeng3

미래에셋증권 AI 페스티벌 금융상품 Agent 프로젝트다. 자연어 질문을 검증 가능한
`QueryPlan`으로 바꾸고, 제공 데이터에서 결정론적으로 검색·검증한 뒤 상품별
원천과 기준일을 포함해 답하는 Evidence-Compiled Hybrid Agent를 만든다.

## 현재 범위

| 상품군·계층 | 상태 |
| --- | --- |
| 해외 ETF·ETN | 감사, 정규화, SQLite, Oracle, Verifier, 50문항·공통 COMPARE 완료 |
| 국내 ETF·ETN | 감사, 정규화, SQLite, Oracle, Verifier, 50문항·공통 COMPARE 완료 |
| 국내채권 | 감사, stale·날짜 계약, SQLite, Oracle, Verifier, 50문항·공통 COMPARE 완료 |
| 공모펀드 | SEARCH parser 40/40·최초 holdout 9/10, 답변 50/50·COMPARE 20/20, 자연어 COMPARE 통합 E2E 24/24, Agent 실행 비활성 |
| 근거 기반 답변 | 공모펀드 SEARCH 44개·정확 ID COMPARE 18개·자연어 COMPARE 16개 grounded, 폴백 0 |
| 공통 Router | 네 상품군·7 intent 공개 진단: 도입 전 4/28, fail-closed Router 28/28 |
| 공통 AGGREGATE | 네 상품군 COUNT·MIN·MAX·AVG·허용 SUM, 최대 2개 group, 통화·결측·기준일·독립 verifier |
| 공통 COMPARE | 같은 상품군의 정확한 두 상품, 필드 allowlist·통화·기준일·stale·독립 verifier·Backend evidence, 공개 자연어 54문항 |
| SEARCH·AGGREGATE 성능 | 네 상품군 8문항 결과 지문 8/8, projected verifier, 새 프로세스 p50 308.749ms·최대 추가 RSS 51,000KiB |
| 문서 RAG | caller-fed BM25/SQLite FTS 적재·필터·근거·기준일·not-found 최소 기능 |
| 팀 통합 계약 | 프레임워크 독립 Backend DTO·JSON Schema/예시, 사람 평가 rubric v1 |
| HyperCLOVA X | 세 provider 계약·fake transport·SEARCH 전체 경로 E2E 8/8 완료, 실제 HTTP 연결 대기 |

로컬 Qwen은 개발 전용 테스트 대역이다. 평가·제출 경로의 LLM은 공식 규칙에
따라 HyperCLOVA X로 제한하며, 로컬 provider는 세 가지 명시적 opt-in 없이는
활성화되지 않는다.

## 아키텍처

```text
질문
→ fail-closed Intent Router
→ minimal draft·capability matrix
→ 서버 QueryPlan compiler
→ 정확 일치 상품 identity resolver
→ typed QueryPlan
→ registry·Pydantic 검증
├─ SEARCH·DETAIL·COMPARE·EXPLAIN
│  → parameterized SQLite Oracle
│  → 독립 Python Result Verifier
│  → field-level product evidence
│  → COMPARE는 ComparisonEvidence·ComparisonResultVerifier
│  → Answer Verifier
│  → evidence compiler 또는 deterministic safe fallback
└─ AGGREGATE
   → SQLite 후보 선택·Decimal 집계
   → 독립 Python AggregateResultVerifier
   → AggregateEvidence
   → deterministic aggregate renderer
```

LLM은 수치 계산, 필터, 정렬, 상품 순위와 원천 인용을 직접 만들지 않는다.

## 작업공간 경계

이 디렉터리는 금융상품 Agent의 검색·검증·답변 생성 코드, 평가, 개발 문서와
로컬 실행 환경을 함께 관리한다. 저장소 루트의 FastAPI·Next.js application
shell과는 독립적으로 개발하며, 애플리케이션은 `packages/finance_agent_core`를
명시적인 API 계약으로 연결한다.

아래 명령은 저장소 루트가 아니라 `finance_agent/` 디렉터리에서 실행한다.

```bash
cd finance_agent
```

## 개발 환경

Conda는 Python 런타임을 격리하고 pip는 프로젝트 Python 패키지를 설치한다.

```bash
conda env create -f environment.yml
conda run -n gaeng3-dev \
  python -m pip install -r requirements/dev.txt
```

이미 환경이 있으면 첫 명령 대신 다음을 사용한다.

```bash
conda env update -n gaeng3-dev -f environment.yml
```

원천 데이터 없이도 전체 단위·계약 테스트와 문서 검사를 실행할 수 있다.

```bash
conda run -n gaeng3-dev python -m pytest -q
conda run -n gaeng3-dev python -m ruff check .
conda run -n gaeng3-dev python -m ruff format --check .
conda run -n gaeng3-dev python scripts/check-docs.py
```

## 실데이터 Mock 실행

공식 원천 XLSX 디렉터리를 지정해 상품군별 SQLite를 만든다. 원천 파일과 생성된
DB·응답은 `artifacts/`에만 두며 Git에 포함하지 않는다.

```bash
FINANCE_DATA_DIR="/path/to/1.금융상품"

conda run -n gaeng3-dev \
  python -m finance_agent_core.storage \
  --dataset bond \
  --data-dir "$FINANCE_DATA_DIR"

conda run -n gaeng3-dev \
  python -m finance_agent_core.agent \
  --database artifacts/normalized/bond.sqlite3 \
  --provider mock \
  --output artifacts/e2e/bond-mock-response.json
```

`--dataset`은 `overseas_etp`, `domestic_etp`, `bond`, `fund` 중에서 선택한다.
`fund`는 정규화 SQLite 생성과 동결 50문항의 내부 Oracle·Verifier 회귀를
지원한다. 실제 HyperCLOVA X HTTP transport와 공식 `/answer` adapter를
검증할 때까지 공식 Agent 실행은 fail-closed로 비활성화되어 있다.

## 문서

처음 참여한다면 [프로젝트 문서 인덱스](docs/project-index.md)부터 읽는다.

- [현재 프로젝트 기준](docs/project-baseline.md)
- [데이터 감사 기준](docs/data-audit.md)
- [공모펀드 원천 데이터 계약](docs/public-fund-contract.md)
- [공모펀드 핵심 평가 기준선](docs/evaluation-public-fund.md)
- [Field Registry와 QueryPlan 계약](docs/contracts.md)
- [HyperCLOVA X 연결 전 준비 기준](docs/pre-hcx-readiness.md)
- [HyperCLOVA X provider 계약](docs/hyperclova-provider.md)
- [Capability matrix](docs/capability-matrix.md)
- [네 상품군 공통 AGGREGATE 엔진](docs/aggregate-engine.md)
- [네 상품군 자연어 COMPARE 공개 회귀](docs/evaluation-product-comparison.md)
- [문서 RAG](docs/document-rag.md)
- [Backend DTO](docs/backend-contract.md)
- [사람 평가 rubric](docs/human-evaluation.md)
- [개발 환경과 구현 상태](docs/development.md)
- [재현 가능한 평가 baseline](evaluation/README.md)
- [Agent Core v0.1 마일스톤](docs/milestones/2026-07-29-agent-core-v0.1.md)

패키지별 실행 방법은 [finance-agent-core README](packages/finance_agent_core/README.md),
로컬 Qwen 격리 절차는 [로컬 LLM 테스트 런타임](docs/local-llm.md)에 기록한다.

## 팀 통합 경계

`packages/finance_agent_core`는 Next.js·FastAPI application shell과 독립적이다.
동료의 템플릿 작업과 합칠 때는 v1 Backend DTO와 JSON Schema 예시를 사용하고,
FastAPI adapter에는 HTTP status·인증·timeout만 추가한다.

공모펀드 grounded answer 평가는 SEARCH 50문항과 별도의 true COMPARE
20문항을 모두 통과했다. 비교는 정확한 `itm_no` 두 개를 요청 순서대로 조회하고,
서버가 필드값·차이·통화·결측을 계산한 뒤 field-level evidence와 기준일을
컴파일한다. 로컬 Qwen은 검증된 근거의 설명만 담당하며 18개 생성 답변이 모두
검증을 통과했고, 누락 상품 2개는 LLM 호출 없이 결정론적으로 처리됐다.

정식명·짧은 이름·`itm_no`를 공모 범위의 정확한 상품 ID로 연결하는 자연어
COMPARE parser도 공개 24문항에서 expected·로컬 Qwen 모두 24/24를 통과했다.
ordered identity와 두 대상 사이의 정확한 연결어, 접두·연결·꼬리 위치별
문장부호 문법을 서버가 검사한다. 중복 단축명, 사모상품, 미등록 상품, 같은
상품 중복뿐 아니라 제외·대신·포함 표현, 질문 전체의 미등록 잔여 표현과
빈·미종결·역방향·중첩·줄바꿈 따옴표도 Oracle 실행 전에 차단한다.

같은 공개 24문항을 사용해 자연어 parser부터 resolver, Oracle·Result Verifier,
field-level evidence, Qwen grounded answer, Answer Verifier·fallback까지 한
번에 잇는 통합 E2E도 완료했다. expected·로컬 Qwen 모두 24/24이며 정상 비교
16개는 grounded answer, 정책 차단 8개는 Answer LLM을 호출하지 않는 안전
응답이다. 로컬 실행은 parser 24회와 answer 16회, 폴백 0회였고 parser·resolution·
계획·Oracle·차단·답변 검증의 핵심 지표는 모두 100%였다.

이 결과는 공개 회귀 문항의 전체 배선 검증이며 독립 blind 일반화 평가나 실제
사람 rubric 결과는 아니다. 내부 구현으로는 네 상품군 Router, BM25 문서 검색,
공통 AGGREGATE, rubric·Backend DTO까지 준비했다. 남은 우선순위는 금융 도메인 담당자의
external blind 100문항·비공개 정답키 작성, 승인된 실제 문서 corpus와 사람
평가, HyperCLOVA X 실제 HTTP transport, 공식 `/answer` adapter다. 최초 SEARCH parser
holdout 실패 1건은 회귀 수정했지만 9/10 기록은 그대로 유지한다. 공모펀드
공식 Agent 실행도 계속 비활성화한다.
