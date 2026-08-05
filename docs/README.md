# docs

팀 전체가 함께 사용하는 문서와 기술 제안서 자료를 관리하는 디렉터리

프로젝트 소개와 실행법은 [루트 README](../README.md), Agent 구현 문서는
[`finance_agent/docs/`](../finance_agent/docs/project-index.md)에서 관리

## 이 디렉터리의 역할

```text
docs/
├── README.md          # 문서 분류와 읽는 순서
└── proposal/          # 기술 제안서 초안·근거 연결·제출 준비
```

`docs/`에는 Frontend·Backend·AI·금융 도메인 담당자가 함께 보거나 제출 자료에 사용할
문서를 둠. 특정 코드 영역의 상세 설계와 실행법은 해당 디렉터리 README에 둠

## 목적별 시작 문서

| 목적 | 시작 문서 | 독자 |
| --- | --- | --- |
| 프로젝트 전체 파악 | [저장소 README](../README.md) | 전체 팀 |
| 기술 제안서·제출 준비 | [기술 제안서 작성 허브](proposal/README.md) | 전체 팀 |
| AI 구현·데이터·평가 확인 | [AI 기술문서 인덱스](../finance_agent/docs/project-index.md) | AI·Backend 담당 |
| Backend 연동 | [Backend DTO](../finance_agent/docs/backend-contract.md) | AI·Backend 담당 |
| 개발·커밋 규칙 | [CONTRIBUTING](../CONTRIBUTING.md) | 코드 기여자 |

## 문서 경계

- `docs/proposal/`: 심사위원에게 전달할 팀 공통 주장과 제출 자료
- `finance_agent/docs/`: AI 구현의 현재 기준, 데이터 계약, 설계와 평가 해석
- `finance_agent/evaluation/`: 재현 가능한 평가 baseline과 봉인 프로토콜
- `finance_agent/docs/research/`, `prompts/archive/`: 구현 정본이 아닌 연구·과거 기록

제안서의 수치와 완료 주장은 먼저 코드·평가 baseline에서 검증한 뒤
`proposal/evidence-map.md`에 연결한다.

## 권장 읽기 순서

### 새 팀원

1. [루트 README](../README.md)
2. [개발 협업 가이드](../CONTRIBUTING.md)
3. 담당 영역의 README

### 기술 제안서 작성자

1. [기술 제안서 작성 허브](proposal/README.md)
2. [근거 연결표](proposal/evidence-map.md)
3. [기술 제안서 초안](proposal/technical-proposal.md)

### AI·Backend 연동 담당자

1. [AI 기술문서 인덱스](../finance_agent/docs/project-index.md)
2. [Backend DTO](../finance_agent/docs/backend-contract.md)
3. [Backend README](../fastapi_backend/README.md)
