# 저장소 문서 안내

저장소 문서는 팀 제안서와 AI Agent 구현 문서로 나뉜다.

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
