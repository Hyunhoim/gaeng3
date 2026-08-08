# Agent 개발 스크립트

`scripts/`는 반복 실행이 필요한 데이터셋 생성, 계약 검사, 평가와 개발용 로컬
LLM 실행 도구를 보관하는 곳

일반 사용자는 전체 시스템을 [루트 README](../../README.md)의 Docker Compose
명령으로 실행하고, 이 스크립트는 AI 개발과 회귀 검증이 필요할 때 사용

## 스크립트 분류

| 파일 | 역할 |
| --- | --- |
| `check-docs.py` | 문서 링크, 제안서 구조, 평가 baseline, source-freeze 검사 |
| `check-submission-boundary.py` | 개발용 로컬 LLM의 운영 파일 혼입과 제출 후보 잔존 검사 |
| `sync-ontology.py` | field registry에서 공식 Turtle 5개 생성·문법·정합성 검사 |
| `generate-bond-suite.py` | 국내채권 평가 질문 세트 생성 |
| `generate-domestic-etp-suite.py` | 국내 ETF·ETN 평가 질문 세트 생성 |
| `generate-fund-suite.py` | 공모펀드 평가 질문 세트 생성 |
| `generate-official-mock-suite.py` | 설명회 예상 분포의 30문항 공개 모의평가를 기존 검증 정답에서 재생성·확인 |
| `blind-fund-eval.py` | 봉인된 공모펀드 blind 평가 실행 보조 |
| `run-hcx-contract-e2e.py` | 실제 API 없이 HyperCLOVA X provider 계약 검사 |
| `run-answer-adapter-contract.py` | Agent 응답과 Backend adapter 계약 검사 |
| `local-llm/` | 개발 전용 Qwen 서버와 E2E 실행 도구 |

registry 전체 Qwen 자연화·Agent 역할 비교는 패키지 명령
`finance-run-coverage-campaign`을 사용하며, 중단 후 재개와 최초 산출물
덮어쓰기 방지 절차는 [자동 커버리지 평가](../docs/evaluation-coverage-guided.md)를 따름

## 사용 원칙

- 저장소의 `finance_agent/` 디렉터리에서 실행
- 생성 결과는 Git에 커밋하지 않고 `artifacts/` 아래에 저장
- 제출용 `ontology/*.ttl`은 예외적으로 Git에 보관하며 registry 변경 후 재생성
- 동결 질문이나 baseline을 다시 만들기 전에 관련 평가 프로토콜 확인
- 로컬 Qwen은 공식 평가·제출용이 아닌 내부 개발 도구로만 사용

자세한 평가 명령은 [평가 README](../evaluation/README.md), 로컬 모델 실행법은
[로컬 LLM 문서](../docs/local-llm.md)를 기준으로 사용
