# docs

팀 전체가 함께 사용하는 문서와 기술 제안서 자료를 관리하는 디렉터리

프로젝트 소개와 실행법은 [루트 README](../README.md), Agent 구현 문서는
[AI 기술문서 안내](../finance_agent/docs/README.md)에서 관리

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
| Agent 1~9단계 구현 방향 | [단계별 구현 로드맵](agent-implementation-roadmap-2026-08-12.html) | 전체 팀 |
| 남은 P0·P1 순서와 현재 완료 상태 | [남은 구현 로드맵](p0-p1-remaining-roadmap-2026-08-14.html) | AI·Backend 담당 |
| P0-4 공식 API 계약 결과 인계 | [공식 Acceptance 인수인계](../finance_agent/docs/p0-4-official-acceptance-handover-2026-08-19.md) | AI·Backend 담당 |
| P0-8 timeout·5xx 재시도 결과 인계 | [재시도 계약 인수인계](../finance_agent/docs/p0-8-retry-contract-handover-2026-08-19.md) | AI·Backend 담당 |
| P0-5 외부 금융 문서 반입·승인 절차 | [외부 문서 반입 계약](../finance_agent/docs/p0-5-external-corpus-intake-2026-08-19.md) | 금융 도메인·AI 담당 |
| P0-6 제공 데이터 관계 검색 결과 | [관계 검색 인수인계](../finance_agent/docs/p0-6-provided-relation-retrieval-handover-2026-08-19.md) | 금융 도메인·AI·Backend 담당 |
| P0-7 관계·문서 계획과 주장 검증 결과 | [계획·주장 검증 인수인계](../finance_agent/docs/p0-7-knowledge-claim-verifier-handover-2026-08-20.md) | AI·Backend 담당 |
| P0-10 공개 관계 검색·릴리스 통합 결과 | [공개 관계 검색·릴리스 통합 인수인계](../finance_agent/docs/p0-10-public-relation-release-integration-handover-2026-08-20.md) | AI·Backend·배포 담당 |
| 기술 제안서·제출 준비 | [기술 제안서 작성 허브](proposal/README.md) | 전체 팀 |
| AI 구현·데이터·평가 확인 | [AI 기술문서 안내](../finance_agent/docs/README.md) | AI·Backend 담당 |
| Agent release·Docker 배포 고정 | [AgentReleaseManifest 배포 계약](../finance_agent/docs/agent-release-manifest.md) | AI·Backend·배포 담당 |
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

1. [AI 기술문서 안내](../finance_agent/docs/README.md)
2. [AI 상세 문서 인덱스](../finance_agent/docs/project-index.md)
3. [Backend DTO](../finance_agent/docs/backend-contract.md)
4. [Backend README](../fastapi_backend/README.md)
5. [P0-4 공식 Acceptance 인수인계](../finance_agent/docs/p0-4-official-acceptance-handover-2026-08-19.md)
6. [P0-8 평가기 재시도 계약 인수인계](../finance_agent/docs/p0-8-retry-contract-handover-2026-08-19.md)
7. [P0-5 외부 금융 문서 반입 계약](../finance_agent/docs/p0-5-external-corpus-intake-2026-08-19.md)
8. [P0-6 제공 데이터 관계 검색 인수인계](../finance_agent/docs/p0-6-provided-relation-retrieval-handover-2026-08-19.md)
9. [P0-7 관계·문서 계획과 주장 검증 인수인계](../finance_agent/docs/p0-7-knowledge-claim-verifier-handover-2026-08-20.md)
10. [P0-10 공개 관계 검색·릴리스 통합 인수인계](../finance_agent/docs/p0-10-public-relation-release-integration-handover-2026-08-20.md)
