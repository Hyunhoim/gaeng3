# 팀 협업 가이드

이 문서는 `gaeng3` 개발자가 같은 기준으로 코드와 개발 문서를 관리하기 위한 안내서임

## 1. 담당 영역

| 영역 | 담당자 | 주요 작업 |
| --- | --- | --- |
| Frontend & Backend | 임현호 | 사용자 화면, FastAPI 서버, 배포와 애플리케이션 통합 |
| AI Agent | 조해영 | 질의 이해, 상품 검색·비교·연산, 근거 검증과 답변 생성 |

- 담당 영역을 기준으로 작업하되 다른 코드 영역과 연결되는 변경은 관련 담당자와 먼저 공유
- `finance_agent/`는 AI Agent 코드, 평가, 개발 문서를 관리하는 독립 작업공간
- 저장소 루트의 애플리케이션과 AI Agent는 명시적인 API 계약을 통해 연결

## 2. 처음 확인할 문서

- 프로젝트 전체 소개: [README](README.md)
- AI Agent 소개와 실행 방법: [finance_agent/README](finance_agent/README.md)
- AI 문서 목록: [프로젝트 문서 인덱스](finance_agent/docs/project-index.md)
- AI 개발 환경과 검증 방법: [개발 환경과 현재 구현 상태](finance_agent/docs/development.md)

## 3. 기본 작업 순서

1. 작업을 시작하기 전에 담당 범위와 변경 목적 확인
2. 자신의 작업 브랜치에서 변경
3. 한 커밋에는 하나의 목적만 포함
4. 관련 검사와 테스트 실행
5. `git diff`로 의도하지 않은 파일이 포함되지 않았는지 확인
6. 커밋 후 원격 브랜치에 Push
7. 팀 검토가 필요한 변경은 Pull Request로 공유

`main`은 팀이 검토한 통합 상태를 보관하는 브랜치로 사용하며 직접 작업하지 않음

## 4. 커밋 메시지 규칙

커밋 메시지는 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
형식을 사용

```text
<type>(<scope>): <description>

[optional body]

[optional footer]
```

- `type`과 `scope`는 소문자 영어 사용
- `description`은 변경 결과가 드러나는 짧은 한글 문장 사용
- `scope`가 없어도 의미가 분명한 문서·설정 변경은 생략 가능
- 서로 다른 목적의 변경은 가능한 한 별도 커밋으로 분리

### Type

| Type | 사용 시점 |
| --- | --- |
| `feat` | 새로운 기능 추가 |
| `fix` | 오류 수정 |
| `docs` | 문서만 변경 |
| `test` | 테스트 추가·수정 |
| `refactor` | 동작을 유지하면서 코드나 구조 개선 |
| `chore` | 개발 환경, 의존성, 설정과 같은 유지보수 작업 |
| `build` | 빌드 시스템이나 패키징 변경 |
| `ci` | 자동 검사와 배포 작업 변경 |

### Scope

| Scope | 대상 |
| --- | --- |
| `web` | Frontend |
| `api` | Backend API |
| `agent` | AI Agent 오케스트레이션과 답변 생성 |
| `data` | 금융 데이터 감사·정규화·저장·검색 |
| `eval` | 평가 문항, 기준선과 평가 도구 |
| `repo` | 저장소 전체 구조와 공통 설정 |

### Body

- 제목 다음에 빈 줄 하나를 두고 작성
- 변경 내용이 제목만으로 충분히 설명되는 작은 커밋은 생략 가능
- 다음 변경에는 Body 작성 필수
  - 구현 이유나 선택한 방식의 장단점이 있는 변경
  - API, 데이터 스키마, QueryPlan 또는 Evidence 계약 변경
  - 마이그레이션, 호환성, 보안, 배포 또는 평가 결과에 영향을 주는 변경
- 제목을 반복하지 않고 변경 이유, 핵심 구현과 영향 범위를 설명
- 한글로 작성하고 가능한 한 한 줄을 72자 이내로 작성
- 여러 항목이 있으면 짧은 문단이나 목록으로 구분
- 테스트 로그 전체, 생성 파일 내용과 비밀정보는 포함하지 않음

### Footer

- Body 다음에 빈 줄 하나를 두고 작성
- 관련 Issue나 PR은 `Refs: #번호` 또는 `Closes: #번호`로 연결
- 호환되지 않는 변경은 제목의 `!`와 `BREAKING CHANGE:` 설명을 함께 사용

### 예시

작은 변경:

```text
docs: 팀 협업 가이드 추가
```

Body가 필요한 변경:

```text
feat(agent): 공모펀드 질의 파이프라인 추가

상품 단위 공모펀드 레코드를 정규화하고 지원하는 검색 조건을
결정론적 Oracle로 실행함

계좌 단위 미지원 필드는 실행 전에 거부하고 새로운 실행 경로를
회귀 평가 문항으로 검증함

Refs: #12
```

기존 사용자나 다른 영역의 코드가 그대로 사용할 수 없는 변경에는 `!`를 표시

```text
feat(api)!: 검증된 답변의 응답 스키마 변경

기존 응답을 검증된 Evidence DTO로 교체함
클라이언트는 새로운 근거와 오류 필드로 전환해야 함

BREAKING CHANGE: 기존 answer와 sources 필드 제거
```

## 5. 커밋에 포함하지 않는 파일

- `.env`와 API key 등 비밀정보
- 공식 원천 XLSX와 별도로 수집한 대용량 원천 데이터
- SQLite DB, 로그, 캐시와 `artifacts/` 생성 결과
- 로컬 LLM 가중치와 Hugging Face 캐시
- 개인 IDE 설정과 로컬 가상환경

공유가 필요한 외부 금융 데이터는 출처, 수집일, 사용 조건과 공식 데이터와의 충돌
처리 방식을 먼저 문서화

## 6. 커밋 전 확인

공통 확인:

```bash
git status
git diff
git diff --cached
git diff --check
git diff --cached --check
```

AI Agent 변경은 `finance_agent/`에서 다음 검사를 실행

```bash
conda run -n gaeng3-dev python -m pytest -q
conda run -n gaeng3-dev python -m ruff check .
conda run -n gaeng3-dev python -m ruff format --check .
conda run -n gaeng3-dev python scripts/check-docs.py
```

Backend 변경은 저장소 루트에서 다음 검사를 추가로 실행

```bash
python -m pytest fastapi_backend/tests
docker compose --env-file fastapi_backend/.env config --quiet
```

Docker가 없는 환경에서는 Compose 검증을 실행하지 못한 사실을 PR에 명시하고,
Ubuntu 서버에서 build·health·`/answer` smoke test를 수행

## 7. Pull Request 작성

PR 제목도 커밋 메시지와 같은 형식 사용

PR 본문에 다음 내용을 간단히 기록

- 변경 목적과 배경
- 주요 변경 내용
- 실행한 테스트와 결과
- 화면 변경이 있으면 스크린샷
- API·데이터 계약 변경 여부
- 추가 검토가 필요한 부분

다른 코드 영역에 영향을 주는 PR은 해당 담당자의 검토 후 Merge

## 8. 금융상품 Agent 필수 원칙

- 평가와 제출에 사용하는 LLM은 HyperCLOVA X로 제한
- 로컬 LLM은 개발 중 파이프라인 검증에만 사용
- 금융상품의 수치, 조건, 순위와 출처는 코드로 검색·연산·검증
- 데이터로 확인할 수 없는 내용은 추측하지 않고 확인 불가 또는 추가 조건을 안내
- 외부 데이터와 공식 제공 데이터가 충돌하면 공식 제공 데이터를 우선
- 답변에는 사용한 데이터 근거와 기준일을 표시

## 9. 빠른 확인 목록

- [ ] 담당 범위와 변경 목적이 분명한지 확인
- [ ] 비밀정보, 원천 데이터, 생성 결과가 포함되지 않았는지 확인
- [ ] 관련 테스트와 문서 검사를 통과했는지 확인
- [ ] 한 커밋에 하나의 목적만 담았는지 확인
- [ ] 커밋 메시지가 Conventional Commits 형식인지 확인
- [ ] 다른 담당 영역에 미치는 영향을 PR에 적었는지 확인
