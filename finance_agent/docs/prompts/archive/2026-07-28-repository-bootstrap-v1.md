# 금융상품 AI Agent 저장소 부트스트랩 작업 명세

## 1. 문서 목적

이 문서는 제10회 미래에셋증권 AI Festival의 **금융 Agent - Product Finder: 채권, ETF, 데이터 기반** 과제를 위한 코드 저장소의 초기 개발 틀과 최소 동작 가능한 vertical slice를 만드는 작업 명세다.

현재 단계의 목표는 전체 추천 알고리즘이나 운영용 HyperCLOVA X 연동을 완성하는 것이 아니다. 다음을 만족하는 재현 가능한 개발 기반을 만드는 것이 목표다.

- 여러 팀원이 각자의 Ubuntu 계정에서 충돌 없이 개발할 수 있는 모노레포
- Conda 기반 로컬 개발 환경과 pip 기반 Python 의존성 관리
- 공식 과제 제약과 실제 제공 데이터 구조를 반영한 도메인·데이터 계약
- 결정론적 검색과 검증을 수행하는 최소 vertical slice
- Mock을 이용해 외부 secret 없이 검증 가능한 Backend·Frontend·CI
- 공식 평가용 `GET /answer` API와 내부 개발용 API의 분리

이 문서는 2026-07-28 기준 공식 과제자료와 제공 데이터 검사 결과를 반영한다.

## 2. 정본 우선순위

요구사항이 서로 충돌할 경우 다음 순서로 판단한다.

1. 주최 측 공식 과제 소개자료와 이후 공식 공지
2. 실제 제공된 datarows 및 schema 파일
3. 2026-08-06 오프라인 설명회의 추가 안내와 참고 질의 세트
4. 프로젝트 결정 로그
5. 과제 공개 전 작성된 아이디어·전략 문서

과제 공개 전 문서의 예시나 가정이 실제 데이터와 충돌하면 실제 데이터와 공식 안내를 우선한다.

## 3. 공식 과제의 절대 제약

- 평가에 사용하는 LLM은 **HyperCLOVA X만 허용**한다.
- 다른 LLM을 평가 경로에서 사용하면 안 된다.
- Mock provider는 로컬 개발과 자동 테스트에서만 허용한다.
- 금융상품 관련 외부 데이터를 사용할 수 있지만, 제공 데이터와 충돌하면 주최 측 제공 데이터를 우선한다.
- 답변은 데이터에 근거해야 하고 참조 데이터와 기준일을 표시해야 한다.
- 데이터로 확인할 수 없는 질문은 확인 불가를 명시하거나 필요한 조건을 역질문한다.
- 데이터에 근거하지 않은 수익률 전망, 수익 보장, 단정적 투자 추천을 생성하지 않는다.
- 제출물에는 재현 가능한 소스코드, 개발 환경 정의, README, 기술제안서, 평가용 API 정보가 포함되어야 한다.
- 공식 평가 API 예시인 `GET /answer`와 요청·응답 필드를 호환해야 한다.

## 4. 작업 전 점검

파일을 수정하기 전에 다음을 읽기 전용으로 확인하고 간단히 보고한다.

1. 현재 사용자와 작업 디렉터리
2. 현재 디렉터리가 Git 저장소인지 여부
3. 기존 tracked·untracked·modified 파일
4. Git remote와 `https://github.com/Hyunhoim/gaeng3`의 관계
5. 사용 가능한 Conda, Python, pip, Node.js, npm, Git 버전
6. Docker와 Docker Compose 사용 가능 여부
7. 기존 파일을 보존하면서 작업할 수 있는지
8. 공식자료와 원천 데이터가 코드 저장소 외부에 있는지

Git 저장소가 아니거나 remote 접근이 불가능해도 이를 숨기지 않는다. 사용자의 별도 지시 없이 `git init`, commit, push, branch 생성, remote 변경을 하지 않는다.

## 5. 작업 안전 규칙

- 이 저장소 내부 파일만 수정한다.
- 저장소 외부의 공식자료, 프로젝트 노트, 원천 ZIP/XLSX는 읽기 전용으로 취급한다.
- sudo를 사용하지 않는다.
- 시스템 패키지, 방화벽, SSH, Git 전역 설정, Docker 데몬 설정을 변경하지 않는다.
- 실제 API 키, 비밀번호, 토큰을 만들거나 코드·문서에 넣지 않는다.
- 기존 파일은 내용을 확인하기 전 삭제하거나 덮어쓰지 않는다.
- Git commit, push, branch 생성은 하지 않는다.
- 서버의 80번과 443번 포트를 사용하지 않는다.
- 개발 서비스는 기본적으로 `127.0.0.1`에만 바인딩한다.
- Docker Compose에 고정된 `container_name`을 사용하지 않는다.
- 사용자별 Compose 프로젝트 이름과 호스트 포트를 환경 변수로 변경할 수 있게 한다.
- 현재 단계의 완료 조건을 충족한 뒤 불필요한 확장 구현을 계속하지 않는다.

## 6. 저장소와 외부 자료의 경계

예상 로컬 배치는 다음과 같다.

```text
26-07 미래에셋증권AI공모전/
├── 0. Official Materials/       # 공식 원본, Git 제외
├── 1. Project Notes/            # 프로젝트 기록, Git 제외
├── 2. Data/                     # 원천 ZIP/XLSX, Git 제외
└── 3. Workspace/
    └── gaeng3/                  # 이 Git 저장소
```

원천 데이터 경로는 코드에 절대경로로 고정하지 않고 환경 변수로 받는다.

```dotenv
PRODUCT_DATA_DIR=../../2. Data/1. Raw/1.금융상품
```

위 값은 로컬 배치 예시일 뿐이며 팀원별로 변경할 수 있어야 한다. 데이터 경로는 존재 여부를 검사하고, 누락되면 실제 데이터를 꾸며내지 말고 명확한 오류를 반환한다.

다음은 Git에 포함하지 않는다.

- `*.xlsx`
- `*.zip`
- `data/raw/`
- `data/processed/`
- `.env`
- `.env.local`
- `.conda/`
- `.venv/`
- secret 또는 credential 파일
- 로컬 DB·캐시·로그·빌드 산출물

테스트에 필요한 데이터는 실제 상품을 복사하지 않고, schema에 맞춘 최소 합성 fixture로 작성한다.

## 7. 개발 환경: Conda + pip

가상환경은 Conda로 관리하고 Python 패키지는 활성화된 Conda 환경 안에서 pip로 설치한다.

책임을 다음처럼 분리한다.

- Conda: Python·Node.js·pip 등 개발 실행 환경과 주요 버전
- pip: Python 애플리케이션 의존성 설치
- npm: Frontend 의존성과 `package-lock.json`
- Docker: 제출·평가 환경의 재현성

저장소 루트에 `environment.yml`을 작성한다.

```yaml
name: gaeng3-dev
channels:
  - conda-forge
dependencies:
  - python=3.12
  - nodejs=24
  - pip
```

위 버전이 실제 Backend·Frontend 라이브러리와 호환되지 않으면 안정적인 호환 버전으로 조정하고 이유를 문서화한다. 임의로 시스템 Python을 변경하지 않는다.

Python 의존성은 다음처럼 구분한다.

```text
requirements/
├── base.txt
└── dev.txt
```

- `base.txt`: FastAPI 서버와 데이터 처리에 필요한 운영 의존성
- `dev.txt`: pytest, Ruff 등 개발·검증 의존성. `-r base.txt`를 포함한다.
- 모든 직접 및 전이 의존성을 재현 가능한 버전으로 고정한다.
- 같은 Python 패키지를 Conda와 pip 양쪽에서 중복 관리하지 않는다.
- `pip freeze` 결과를 검토 없이 그대로 사용하지 않는다.

자동화에서는 셸 activation 상태에 과도하게 의존하지 않도록 `conda run -n gaeng3-dev ...` 명령을 제공한다.

Docker 이미지에는 Conda를 강제하지 않는다. 경량 Python 이미지에서 동일한 `requirements/base.txt`를 pip로 설치해 애플리케이션 의존성의 일관성을 유지한다.

## 8. 기본 기술 스택

- Frontend: Next.js, TypeScript
- Backend: Python, FastAPI
- Local environment: Conda
- Python packages: pip + pinned requirements
- Database: PostgreSQL
- Cache 및 향후 요청 큐 기반: Redis
- Local orchestration: Docker Compose
- Backend test: pytest
- Python lint·format: Ruff
- Frontend lint·type check·production build
- 설정: 환경 변수와 `.env.local`
- 내부 API prefix: `/api/v1`
- 공식 평가 호환 API: `/answer`

특정 라이브러리 버전은 현재 호환되는 안정 버전을 선택해 고정하고, 선택 결과를 README에 기록한다.

## 9. 권장 저장소 구조

필요한 경우 세부 구조는 합리적으로 조정할 수 있지만 각 책임은 분명히 유지한다.

```text
frontend/
backend/
  app/
    api/
    agents/
    core/
    domain/
    llm/
    repositories/
    services/
    tools/
    db/
  tests/
requirements/
scripts/
docs/
  prompts/
  architecture.md
  data-contract.md
  development.md
  deployment.md
.github/workflows/
AGENTS.md
README.md
environment.yml
compose.yaml
.env.example
.gitignore
Makefile
```

`data/` 디렉터리를 저장소에 만들 경우 원본 파일을 두지 말고 외부 데이터 경로, 처리 결과 경로, read-only mount 사용법만 설명하는 `README.md`만 둔다.

## 10. 실제 제공 데이터 계약

원천 datarows는 총 145,393행이다.

| 상품군 | 원천 행 수 | 핵심 구조와 주의점 |
| --- | ---: | --- |
| 국내채권 | 42,394 | `PD_NO` 유일. 만기·표면금리는 거의 완전하지만 매수수익률·매수가능수량은 881건뿐이다. |
| 국내 ETP | 1,734 | ETF 1,202건과 ETN 532건이 함께 있다. ETF와 ETN을 명시적으로 구분한다. |
| 해외 ETP | 5,646 | ETF 5,587건과 ETN 59건. 보수·전략·자산군·지역·AUM이 상대적으로 완전하다. |
| 공모펀드 | 95,619 | `itm_no` 기준 논리 상품은 11,139개이며 `상품 × prfd_attr_cd` 형태로 반복된다. |

적재와 검색에서 다음 규칙을 준수한다.

### 국내채권

- 전체 채권과 현재 매수 가능 채권을 구분한다.
- `BUY_YIELD`와 `BUYABLE_QUANTITY`가 있는 881건을 전체 채권의 일반 특성처럼 취급하지 않는다.
- 실제 매수 가능 여부는 `BUYABLE_QUANTITY > 0` 등 명시적 규칙으로 판단하고 근거 필드를 반환한다.
- 신용등급은 약 58%만 존재하므로 결측을 최저등급이나 무등급으로 임의 변환하지 않는다.

### 국내 ETP

- `pd_grp_no`로 ETF와 ETN을 구분한다.
- 자산군·지역·위험등급은 주요 검색축으로 사용할 수 있다.
- 총보수는 217건, 기초지수는 58건만 값이 있다.
- 분배주기는 전부 비어 있고 배당수익률 값은 실질적으로 모두 0이므로 월분배·배당수익률 조건을 지원한다고 주장하지 않는다.
- 추적오차가 모두 0인 상태이므로 유효한 비교 지표로 사용하지 않는다.

### 해외 ETP

- 총보수·운용사·운용전략·자산군·지역·AUM을 첫 baseline의 핵심 검색축으로 사용한다.
- 제공된 1일 수익률 값이 모두 0이므로 수익률 비교에 사용하지 않는다.
- 영어 운용전략 텍스트는 의미 보조 정보로 사용할 수 있지만 수치 필터를 대체하지 않는다.

### 공모펀드

- 상품 테이블은 `itm_no` 기준 한 행으로 정규화한다.
- `prfd_attr_cd`는 별도 속성 테이블 또는 배열로 집계한다.
- 원천 행을 그대로 상품 검색 인덱스에 넣어 동일 상품을 반복 노출하지 않는다.
- 수익률은 기간별로 약 53~73%, 위험등급은 약 81%, AUM은 약 87%만 존재한다.
- 펀드 보수 정보는 제공되지 않으므로 보수 조건을 지원하지 않는다.
- 원천 Excel 84,563행의 컬럼 이동 형태 손상 레코드는 격리하고 품질 리포트에 남긴다.

### 기준일

파일 추출 스냅샷은 2026-07-11이지만 개별 필드의 갱신일은 다르다.

- 국내 ETP 주요 일간 데이터: 2026-06-15
- 해외 ETP 주요 데이터: 2026-06-14~16
- 국내채권 `PD_STD_INFO_UPDATE`: 최대 2026-02-24

답변에는 파일 추출일만 일괄 표시하지 말고 사용한 필드의 가용한 기준일을 함께 표시한다.

## 11. 데이터 모델과 검색 책임

모든 필드를 하나의 거대한 공통 테이블에 억지로 합치지 않는다.

다음과 같은 공통 식별 계층과 상품군별 상세 모델을 설계한다.

- `products`: 상품군, 원천 테이블, 원천 키, 표시명, 통화, 판매·거래 상태
- `bonds`
- `domestic_etp`
- `global_etp`
- `funds`
- `fund_attributes`
- provenance 또는 evidence 구조: 원천 테이블, 원천 키, 사용 필드, 값, 기준일

숫자·날짜·등급·상태 조건은 결정론적 코드와 SQL로 처리한다.

벡터 검색이나 LLM이 다음을 대신하게 하지 않는다.

- 수치 비교
- 범위 필터
- 정렬·순위
- 집계
- 날짜와 만기 계산
- 상품군 판별
- 결과가 조건을 만족하는지에 대한 최종 검증

LLM 또는 의미 검색은 사용자 표현을 정해진 필드와 enum에 매핑하거나 영문 전략 설명을 보조적으로 해석하는 용도로만 제한한다.

## 12. QueryPlan과 Agent 구조

Agent 흐름은 다음과 같다.

```text
사용자 질문
→ HyperCLOVA X 또는 Mock이 Typed QueryPlan 생성
→ schema와 지원 가능 조건 검증
→ 결정론적 검색·필터·정렬·집계
→ 결과 재검증
→ 0건이면 최소 조건 완화 계산
→ 근거·기준일·미확인 조건을 포함한 답변
```

책임을 분리한다.

- 질의 해석: 상품군, 필수 조건, 선호 조건, 정렬, 개수, 모호성
- 지원 가능성 검사: 데이터에 해당 필드가 있는지 확인
- 상품 검색 도구: 결정론적 repository query
- 조건 완화 도구: 한 번에 하나의 조건만 변경하고 후보 수를 재계산
- 결과 검증: 반환 상품이 모든 필수 조건을 실제로 만족하는지 재검사
- 답변 생성: 결과, 근거, 기준일, 가정, 미확인 항목 표현
- LLM provider: `mock`, `hyperclova` 두 구현만 제공

평가 환경에서 `LLM_PROVIDER`가 `hyperclova`가 아니면 명확한 설정 오류로 실패하게 한다. 테스트에서는 외부 API 없이 Mock을 사용한다.

## 13. 최소 vertical slice

첫 vertical slice는 데이터 완성도가 높은 해외 ETP로 구현한다.

대표 질문:

> 미국 채권형 해외 ETF 중 총보수 0.1% 이하이고 AUM이 큰 상품 3개를 찾아줘.

구현 범위:

1. 질문을 구조화된 QueryPlan fixture 또는 Mock provider 결과로 변환
2. ETF만 선택하고 ETN 제외
3. 자산군, 지역, 총보수 조건 적용
4. AUM 내림차순 정렬
5. 상위 N개 반환
6. 반환 결과의 조건 만족 여부 재검증
7. 상품명, 티커·식별자, 보수, AUM, 원천 테이블, 기준일을 근거로 반환
8. 결과가 0건이면 조건을 하나씩 완화해 후보 수와 변경 폭을 계산
9. 데이터에 없는 조건이 들어오면 역질문 또는 답변 보류

현재 단계에서는 네 상품군 전체의 모든 질의를 구현하지 않는다. 다만 이후 loader와 repository를 추가할 수 있는 인터페이스와 데이터 계약은 만든다.

## 14. API

### 상태 확인

`GET /health`

- 애플리케이션 상태
- 데이터 repository 준비 상태
- LLM provider 종류
- secret 값은 반환하지 않음

### 내부 상품 검색

`GET /api/v1/products`

- 허용된 구조화 필터를 받는다.
- pagination과 결과 개수 제한을 둔다.
- 원천 row 전체를 무제한 반환하지 않는다.

### 내부 채팅

`POST /api/v1/chat`

- 사용자 메시지를 받는다.
- Mock 또는 HyperCLOVA provider와 검색 도구를 사용한다.
- 답변, 처리 상태, 구조화 조건, 참고 상품, evidence, 미지원 조건을 반환한다.

### 공식 평가 호환 API

`GET /answer`

요청:

```text
question_id={id}
question={평가 질의}
```

최소 응답:

```json
{
  "question_id": "Q-001",
  "question": "평가 질의 원문",
  "retrieved_context": "검색에 사용한 데이터와 결과 요약",
  "think_trace": "질의 해석, 실행 필터, 도구 호출, 검증 결과의 간결한 감사 기록",
  "answer": "최종 답변"
}
```

`think_trace`에는 모델의 숨은 사고과정이나 장문의 자유로운 추론을 노출하지 않는다. 다음과 같은 구조화된 실행 사실만 기록한다.

- 해석된 상품군과 조건
- 적용한 필터·정렬·집계
- 사용한 도구와 원천 테이블
- 조건 검증 결과
- 적용하지 못한 조건과 이유

8월 6일 설명회에서 공식 스키마가 변경되면 adapter만 수정할 수 있게 내부 response 모델과 공식 response 모델을 분리한다.

## 15. Frontend

한 페이지에서 다음을 확인할 수 있게 한다.

- Backend health
- 사용자 질문 입력창
- 전송 버튼
- Mock Agent 답변
- 해석된 조건
- 참고 상품과 근거
- 기준일
- 확인하지 못한 조건
- 0건일 때 조건 완화 대안
- 요청 실패 시 오류 메시지

디자인은 최소한으로 하고 end-to-end 연결과 근거 표시를 우선한다.

## 16. 다중 사용자 Ubuntu 서버 대응

1. `COMPOSE_PROJECT_NAME`을 `.env.local`에서 사용자별로 설정할 수 있게 한다.
2. Frontend와 Backend 호스트 포트를 환경 변수로 변경할 수 있게 한다.
3. Docker volume과 network는 Compose 프로젝트별로 분리한다.
4. 고정된 `container_name`을 사용하지 않는다.
5. PostgreSQL과 Redis는 기본적으로 호스트 포트에 노출하지 않는다.
6. Frontend와 Backend는 기본적으로 `127.0.0.1`에 바인딩한다.
7. `.env.local`은 Git에서 제외한다.
8. `.env.example`에는 예시 값만 제공한다.
9. 원천 데이터 mount는 read-only로 구성하고 실제 호스트 절대경로를 강제하지 않는다.
10. Conda 환경은 사용자별 홈에 생성되므로 동일한 환경명을 사용할 수 있다.

haeyeongcho 계정 예시:

```dotenv
COMPOSE_PROJECT_NAME=gaeng3_haeyeongcho
FRONTEND_PORT=13001
BACKEND_PORT=18001
PRODUCT_DATA_DIR=../../2. Data/1. Raw/1.금융상품
LLM_PROVIDER=mock
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
```

다른 팀원은 Compose 이름, 포트, 데이터 경로를 자신의 환경에 맞게 변경한다.

## 17. 개발 명령

Makefile 또는 문서화된 명령으로 다음을 제공한다.

- Conda 환경 생성·갱신
- `.env.example`을 `.env.local`로 안전하게 복사
- Backend 의존성 설치
- Frontend 의존성 설치
- Docker 서비스 시작·종료
- Backend test
- Backend lint·format check
- 데이터 계약·품질 테스트
- Frontend lint
- Frontend type check
- Frontend production build
- 전체 검증
- 로그 확인
- 서비스 상태 확인

기존 `.env.local`이 있으면 초기화 명령이 덮어쓰지 않게 한다.

예상 인터페이스:

```bash
make env-create
make env-update
make init
make dev
make down
make test
make lint
make typecheck
make build
make verify
```

## 18. 데이터 품질 테스트

실데이터가 있을 때 다음을 검사하는 재실행 가능한 스크립트 또는 테스트를 제공한다.

- 예상 파일 8개 존재 여부
- datarows의 header와 schema 컬럼 일치
- 상품군별 원천 행 수
- 주요 식별자의 결측·중복
- 국내 ETP의 ETF·ETN 구분
- 해외 ETP의 ETF·ETN 구분
- 펀드 `itm_no + prfd_attr_cd` 복합키 유일성
- 펀드 논리 상품 정규화 후 중복 여부
- 알려진 손상 레코드 격리
- 주요 필드의 결측률과 sentinel 값
- 숫자·날짜 형식 변환 실패
- 기준일과 갱신일 범위
- 검색 결과가 필터 조건을 실제로 만족하는지

원천 데이터가 없을 때 CI가 실패하지 않도록 schema와 합성 fixture 기반 계약 테스트를 별도로 둔다. 실데이터 검증은 명시적인 명령으로 실행한다.

## 19. 테스트와 평가 준비

최소 다음 범주의 golden question을 작성한다.

- 정상 검색
- 숫자 범위
- 정렬·상위 N개
- 결과 0건
- 조건 하나 완화
- 모호한 상품군
- 지원하지 않는 조건
- 결측 필드
- 오래된 기준일
- ETF와 ETN 구분
- 펀드 중복 방지
- 데이터로 확인할 수 없는 전망·추천 요청

초기에는 10개 이상의 질문으로 vertical slice를 검증하고, 8월 6일 참고 질의 세트가 공개되면 30개 이상으로 확장한다.

## 20. CI

GitHub Actions에서 외부 LLM secret과 실제 원천 데이터 없이 다음을 검사한다.

- Conda 환경 또는 명시된 Python 버전 준비
- pip 의존성 설치
- Ruff
- pytest
- schema·합성 fixture 계약 테스트
- Frontend npm clean install
- Frontend lint
- Frontend type check
- Frontend production build

CI에서 HyperCLOVA X를 호출하지 않는다.

## 21. 문서

### README.md

- 프로젝트 개요와 공식 제약
- 저장소 구조
- Conda 환경 생성·활성화
- pip 의존성 설치
- 최초 실행
- 사용자별 포트와 Compose 설정
- 외부 데이터 경로 설정
- VS Code Port Forwarding
- 테스트·lint·build
- Git 브랜치·PR 기본 방식
- 원천 데이터와 secret을 Git에 올리지 않는 규칙
- 현재 구현 기능과 미구현 기능
- 문제 해결

### docs/architecture.md

다음 흐름과 책임 경계를 설명한다.

```text
Frontend
→ FastAPI Backend
→ QueryPlan / Agent Service
→ Deterministic Tools / ProductRepository
→ PostgreSQL 또는 외부 원천 데이터
→ Evidence Validator
→ Mock 또는 HyperCLOVA X Provider
```

현재 구현과 향후 구현을 명확히 구분한다.

### docs/data-contract.md

- 상품군별 원천 키와 논리 grain
- 공통·전용 필드
- 결측 처리
- sentinel 처리
- 기준일
- 펀드 정규화
- 손상 레코드 격리
- 지원 가능·불가능 질의 조건

## 22. AGENTS.md

간결하게 다음을 포함한다.

- 프로젝트 목적
- 주요 디렉터리
- Conda·pip 개발 명령
- 테스트·검증 명령
- secret과 원천 데이터 규칙
- 고정 `container_name` 금지
- 사용자별 Compose·포트 분리
- HyperCLOVA X 외 LLM을 평가 경로에 사용하지 말 것
- 금융상품 스키마와 가용 필드를 추정하지 말 것
- 수치 필터와 검증을 LLM에 맡기지 말 것
- 실제 금융 추천 로직을 임의로 확장하지 말 것
- `docs/architecture.md`, `docs/data-contract.md`, `docs/development.md` 링크

## 23. 현재 단계의 명시적 제외 범위

- 실제 HyperCLOVA X API 호출
- 전체 네 상품군의 완전한 검색 구현
- 개인화 투자 적합성 판단
- 미래 수익률 예측
- 자동 매매
- 인증·권한 시스템
- 운영 클라우드 배포
- Vector DB와 Graph DB의 선제 도입
- 데이터에 없는 보수·분배·편입종목의 추정

## 24. 완료 조건

1. 기존 파일이 보존되어 있다.
2. 저장소 구조와 문서가 생성되어 있다.
3. `environment.yml`로 Conda 환경을 만들 수 있다.
4. pip requirements가 운영·개발로 분리되고 버전이 고정되어 있다.
5. `.env.example`만 추적 대상이고 `.env.local`은 제외되어 있다.
6. 실제 secret과 원천 금융상품 파일이 Git 대상에 없다.
7. Docker Compose에 고정 `container_name`이 없다.
8. 사용자별 Compose 프로젝트와 포트를 변경할 수 있다.
9. Backend health와 내부 API가 동작한다.
10. 공식 `GET /answer` 호환 API가 Mock으로 동작한다.
11. 해외 ETP vertical slice가 결정론적 검색과 결과 검증을 수행한다.
12. 0건 질문에서 조건 완화 결과를 재계산한다.
13. schema·합성 fixture 데이터 계약 테스트가 성공한다.
14. Backend pytest와 Ruff가 성공한다.
15. Frontend lint, type check, production build가 성공한다.
16. 가능한 경우 Docker Compose build와 `/health` smoke test가 성공한다.
17. 실행할 수 없는 검증은 실패를 숨기지 않고 원인과 재실행 명령을 보고한다.

Docker가 현재 호스트에 없다면 Compose 파일은 작성하되 실행 검증을 성공으로 가장하지 않는다.

## 25. 작업 완료 보고 형식

- 생성하거나 수정한 주요 파일
- 구현된 기능
- 실제 데이터에 반영한 구조와 품질 규칙
- 실행한 검증 명령과 결과
- 실행하지 못한 검증과 이유
- 아직 구현하지 않은 기능
- 발견한 환경·데이터 문제
- 사용자가 다음으로 실행할 명령
- 검토가 필요한 설계 결정

현재 단계의 완료 조건을 충족하면 작업을 멈추고, 전체 데이터 적재나 실제 HyperCLOVA X 연동을 임의로 계속하지 않는다.
