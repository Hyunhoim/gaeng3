# gaeng3

미래에셋증권 AI 페스티벌 금융상품 Agent 프로젝트다. 자연어 질문을 검증 가능한
`QueryPlan`으로 바꾸고, 제공 데이터에서 결정론적으로 검색·검증한 뒤 상품별
원천과 기준일을 포함해 답하는 Evidence-Compiled Hybrid Agent를 만든다.

## 현재 범위

| 상품군·계층 | 상태 |
| --- | --- |
| 해외 ETF·ETN | 감사, 정규화, SQLite, Oracle, Verifier, 50문항 완료 |
| 국내 ETF·ETN | 감사, 정규화, SQLite, Oracle, Verifier, 50문항 완료 |
| 국내채권 | 감사, stale·날짜 계약, SQLite, Oracle, Verifier, 50문항 완료 |
| 공모펀드 | product-grain 감사 완료, 실행 파이프라인 예정 |
| 근거 기반 답변 | 최소권한 LLM 입력, Answer Verifier, 결정론적 폴백 완료 |
| HyperCLOVA X | 공식 API 확보 후 연결 예정 |

로컬 Qwen은 개발 전용 테스트 대역이다. 평가·제출 경로의 LLM은 공식 규칙에
따라 HyperCLOVA X로 제한하며, 로컬 provider는 세 가지 명시적 opt-in 없이는
활성화되지 않는다.

## 아키텍처

```text
질문
→ lexical/schema linker
→ typed QueryPlan
→ registry·Pydantic 검증
→ parameterized SQLite Oracle
→ 독립 Python Result Verifier
→ field-level evidence
→ Answer Verifier
→ evidence compiler 또는 deterministic safe fallback
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

`--dataset`은 `overseas_etp`, `domestic_etp`, `bond` 중에서 선택한다.

## 문서

처음 참여한다면 [프로젝트 문서 인덱스](docs/project-index.md)부터 읽는다.

- [현재 프로젝트 기준](docs/project-baseline.md)
- [데이터 감사 기준](docs/data-audit.md)
- [Field Registry와 QueryPlan 계약](docs/contracts.md)
- [개발 환경과 구현 상태](docs/development.md)
- [재현 가능한 평가 baseline](evaluation/README.md)
- [Agent Core v0.1 마일스톤](docs/milestones/2026-07-29-agent-core-v0.1.md)

패키지별 실행 방법은 [finance-agent-core README](packages/finance_agent_core/README.md),
로컬 Qwen 격리 절차는 [로컬 LLM 테스트 런타임](docs/local-llm.md)에 기록한다.

## 팀 통합 경계

`packages/finance_agent_core`는 Next.js·FastAPI application shell과 독립적이다.
동료의 템플릿 작업과 합칠 때는 `AgentRequest`, `AgentResponse`, evidence,
오류·timeout 계약을 먼저 고정하고 공식 `/answer` adapter를 연결한다.

현재 우선순위는 새 blind 표현 변형·사람 답변 품질 평가, 공모펀드 수직
파이프라인, HyperCLOVA X와 공식 `/answer` adapter 순이다.
