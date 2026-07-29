# gaeng3 프로젝트 문서

마지막 갱신: 2026-07-29

이 디렉터리는 금융상품 Agent 구현에 직접 사용하는 문서의 정본이다. 연구 요청·외부 모델 답변·감사 산출물은 근거 자료로 보존하되, 실제 구현 판단은 `project-baseline.md`와 `data-audit.md`를 우선한다.

## 먼저 읽을 문서

1. [현재 프로젝트 기준](project-baseline.md)
2. [데이터 감사 기준](data-audit.md)
3. [Field Registry와 QueryPlan 계약](contracts.md)
4. [해외 ETP 핵심 평가 기준선](evaluation.md)
5. [국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md)
6. [국내채권 핵심 평가 기준선](evaluation-domestic-bond.md)
7. [근거 기반 최종 답변 평가](evaluation-grounded-answers.md)
8. [개발 환경과 현재 구현 상태](development.md)
9. [로컬 LLM 테스트 런타임](local-llm.md)
10. [저장소 부트스트랩 작업 명세](prompts/01-repository-bootstrap.md)

## 문서 지도

| 문서 | 역할 | 상태 |
| --- | --- | --- |
| [현재 프로젝트 기준](project-baseline.md) | 공식 제약, 모델 정책, 역할 분담, 목표 아키텍처, 우선순위 | 현재 정본 |
| [데이터 감사 기준](data-audit.md) | 상품군별 grain·결측·sentinel·손상 행·검색 허용 범위 | 현재 정본 |
| [Field Registry와 QueryPlan 계약](contracts.md) | 해외·국내 ETP·국내채권 field capability, 서버 QueryPlan, HCX schema subset | P1 정본 |
| [해외 ETP 핵심 평가 기준선](evaluation.md) | 동결 50문항, oracle·채점 규칙, 최초 holdout과 사후 회귀 결과 | v1.0 정본 |
| [국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md) | 국내 ETP 동결 50문항, 품질 계약, local-inference split 결과 | v1.0 정본 |
| [국내채권 핵심 평가 기준선](evaluation-domestic-bond.md) | 국내채권 동결 50문항, stale·날짜 계약, 로컬 Qwen·답변 결과 | v1.0 정본 |
| [근거 기반 최종 답변 평가](evaluation-grounded-answers.md) | Answer Verifier, 최소권한 LLM 입력, 폴백, 국내 ETP·채권 결과 | v1.1 정본 |
| [개발 환경과 현재 구현 상태](development.md) | Git branch, Conda + pip, 검증 명령, 템플릿 통합 경계 | 현재 정본 |
| [로컬 LLM 테스트 런타임](local-llm.md) | 격리된 Qwen/vLLM 환경, 안전 경계, 재현 가능한 E2E | 개발 전용 |
| [저장소 부트스트랩 작업 명세](prompts/01-repository-bootstrap.md) | 애플리케이션 저장소를 처음 구현할 때 Codex에 전달할 실행 명세 | 활성 프롬프트 |
| [Agent 전략 연구 요청](prompts/02-agent-strategy-research.md) | GPT Pro에 전달했던 질문과 당시 제약 | 과거 입력 기록 |
| [GPT Pro 연구 기록](research/2026-07-28-gpt-pro/README.md) | GPT Pro 원문 답변, 감사 번들, 검토 결과, 원본 ZIP 위치 | 연구·감사 기록 |

## 현재 구현

- [finance_agent_core](../packages/finance_agent_core/README.md): 감사, 해외·국내 ETP·국내채권
  정규화·SQLite 적재, QueryPlan, oracle, verifier, evidence, Agent
- [개발 Conda 환경](../environment.yml): `gaeng3-dev`, Python 3.12
- [로컬 LLM Conda 환경](../environment.local-llm.yml): `gaeng3-llm-local`,
  Python 3.12
- [개발 requirements](../requirements/dev.txt): editable core, Pydantic, PyYAML,
  pytest, Ruff
- [로컬 추론 requirements](../requirements/local-llm.txt): 개발 전용 vLLM
- 감사 회귀 기준: 4종 145,393행, 핵심 expectation 49개
- 해외 ETP 적재 기준: 5,646행, 검색 가능 5,636행, sparse 격리 10행
- 첫 vertical slice oracle 기준: 후보 440개, 결정론적 상위 5개
- 국내 ETP 적재 기준: 1,734행, 검색 가능 1,733행, 손상 행 1개 격리
- 국내 ETP 대표 oracle: 후보 211개, 수익률 상위 5개와 field evidence 재현
- 국내채권 적재 기준: 42,394행, 검색 가능 42,394행, 실제 매수 가능 254행
- 국내채권 대표 oracle: 잔존일수 365일 이하 회사채 후보 23개와 상위 3개 재현
- 코드 회귀 기준: 전체 pytest 64개, Ruff, pip dependency check
- 로컬 Qwen 평가 기준: 동결 50문항에서 최초 미사용 holdout 9/10,
  오류 수정 후 전체 회귀 50/50을 연속 2회 재현
- 국내 ETP 로컬 Qwen 기준: development 40/40, local-inference holdout 첫 실행 10/10
- 국내채권 로컬 Qwen 기준: QueryPlan 50/50, grounded answer 50/50,
  실제 통합 E2E Answer Verifier 통과
- 근거 기반 답변 기준: 47개 LLM 생성·3개 안전 차단, 전체 50/50,
  수치·순위·evidence·기준일 100%, 폴백 0
- 다음 구현: 새 blind 표현 변형·사람 품질 평가, 공모펀드, 공식 `/answer` adapter

## 저장소 밖의 근거 자료

- [공식 과제 소개자료](<../../../0. Official Materials/(배표용)과제소개자료_금융상품Agent.pdf>)
- [공식 공지 정리](<../../../0. Official Materials/07-28(화) - 공지사항 정리하기.md>)
- [원천 데이터](<../../../2. Data/1. Raw/1.금융상품/>)
- [원천 데이터 ZIP](<../../../2. Data/0. Source Archive/1.금융상품.zip>)
- [프로젝트 허브](<../../../26-07 미래에셋증권AI공모전.md>)

## 판단 우선순위

내용이 충돌하면 다음 순서로 판단한다.

1. 주최 측 공식 과제자료와 이후 공식 공지·설명회 답변
2. 실제 제공 데이터와 재현 가능한 감사 결과
3. `project-baseline.md`와 `data-audit.md`
4. 활성 구현 명세와 코드 계약
5. GPT Pro 답변, 회의록, 과제 공개 전 아이디어 문서

연구 문서의 설계 제안은 자동으로 요구사항이 되지 않는다. 정본 문서로 승격한 결정만 구현 범위로 간주한다.

## 문서 운영 규칙

- 공식 원본과 원천 데이터는 수정하지 않는다.
- 외부 모델의 원문 답변과 원본 번들은 재현성을 위해 보존한다.
- 연구 결과에서 채택한 결정은 `project-baseline.md`에 다시 기록한다.
- 데이터 수치나 지원 범위가 바뀌면 `data-audit.md`와 관련 계약 테스트를 함께 갱신한다.
- 평가 질문을 튜닝에 사용한 뒤에는 기존 holdout 성능으로 주장하지 않고 새
  미사용 split을 만든다.
- 2026-08-06 설명회 이후 공식 답변과 참고 질의 세트를 반영하고 모델·API·데이터 정책을 다시 동결한다.
