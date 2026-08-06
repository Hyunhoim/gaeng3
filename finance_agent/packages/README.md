# Agent Python 패키지

`packages/`는 애플리케이션과 분리해 설치·테스트할 수 있는 AI 코드를 보관하는 곳

현재 패키지는 하나

| 패키지 | 역할 | 시작 문서 |
| --- | --- | --- |
| `finance_agent_core` | 데이터 정규화, QueryPlan, 검색·비교·집계, 검증, 근거와 답변 계약 | [패키지 README](finance_agent_core/README.md) |

Frontend나 FastAPI가 Agent 내부 SQL과 검증 로직을 다시 구현하지 않도록 공개 DTO와
실행 경계를 이 패키지에서 관리

환경 구성과 전체 작업공간 설명은 [finance_agent README](../README.md)를 기준으로 사용
