# 2026-07-28 GPT Pro 전략 연구와 데이터 감사

이 디렉터리는 GPT Pro에 요청한 전략 연구의 원문 답변과 함께 생성된 감사 산출물을 보존한다. 연구 결과는 설계 근거이며 구현 정본은 아니다. 검토를 거쳐 채택한 결정은 [현재 프로젝트 기준](../../project-baseline.md)과 [데이터 감사 기준](../../data-audit.md)에 반영했다.

## 출처와 무결성

- 요청 프롬프트: [Agent 전략 연구 요청](../../prompts/02-agent-strategy-research.md)
- 원문 답변: [response.md](response.md)
- 추출된 산출물: [audit-bundle](audit-bundle/)
- 원본 ZIP: [Source Archive](<../../../../../2. Data/0. Source Archive/2026-07-28-gpt-pro-finance-agent-audit-bundle.zip>)
- 검사한 공식 PDF: [공식 과제 소개자료](<../../../../../0. Official Materials/(배표용)과제소개자료_금융상품Agent.pdf>)
- 검사한 원천 데이터: [Raw 데이터](<../../../../../2. Data/1. Raw/1.금융상품/>)

2026-07-28 이동 전후 확인값:

| 대상 | SHA-256 |
| --- | --- |
| GPT Pro 원문 답변 | `6753688873f96dc9a853f03f230e6854869c83f26739175cac94462ba63a2d41` |
| 원본 감사 번들 ZIP | `089622c2351181b07c40704877279df430cdc02e5fab2810c0cf8e7980012996` |

ZIP 안의 13개 파일과 추출된 `audit-bundle/`의 상대 경로·SHA-256은 모두 일치했다. 원문 답변은 링크 보존을 위해 수정하지 않았다.

## 산출물 지도

| 산출물 | 용도 |
| --- | --- |
| [데이터 감사 요약](audit-bundle/finance_data_audit_summary.md) | GPT Pro가 계산한 주요 구조·품질 요약 |
| [통합 데이터 감사 JSON](audit-bundle/finance_data_audit.json) | 입력 manifest와 상품군별 감사 결과 |
| [국내채권 감사](audit-bundle/audit_bond.json) | 국내채권 상세 profile |
| [국내 ETP 감사](audit-bundle/audit_domestic_etp.json) | 국내 ETF·ETN 상세 profile |
| [해외 ETP 감사](audit-bundle/audit_overseas_etp.json) | 해외 ETF·ETN 상세 profile |
| [공모펀드 감사](audit-bundle/audit_fund.json) | 공모펀드 raw-row profile |
| [QueryPlan schema 초안](audit-bundle/queryplan.schema.json) | Typed QueryPlan 연구 초안 |
| [QueryPlan 예시](audit-bundle/queryplan.example.json) | 첫 vertical slice 예시 |
| [Agent/API 계약 예시](audit-bundle/agent_contract_examples.json) | 요청·응답 형태 연구 초안 |
| [일반 감사 스크립트](audit-bundle/run_finance_audit.py) | 전체 감사 시도 |
| [Excel 감사 도구](audit-bundle/xlsx_audit.py) | XLSX 구조 검사 유틸리티 |
| [펀드 빠른 감사기](audit-bundle/audit_fund_fast.py) | `lxml` 기반 펀드 전용 검사 |
| [공식 PDF 텍스트](audit-bundle/finance_agent_pdf.txt) | PDF 텍스트 추출본 |

## 원문 안의 깨진 링크 매핑

`response.md`의 `sandbox:/mnt/data/...` 링크는 GPT Pro 실행 환경에만 존재했던 경로라 현재 열리지 않는다. 다음 로컬 파일을 사용한다.

| 원문 링크 표시명 | 현재 위치 |
| --- | --- |
| 공식 과제 소개 PDF | [공식 PDF](<../../../../../0. Official Materials/(배표용)과제소개자료_금융상품Agent.pdf>) |
| 데이터 감사 요약 | [finance_data_audit_summary.md](audit-bundle/finance_data_audit_summary.md) |
| 전체 데이터 감사 JSON | [finance_data_audit.json](audit-bundle/finance_data_audit.json) |
| Typed QueryPlan JSON Schema | [queryplan.schema.json](audit-bundle/queryplan.schema.json) |
| 첫 vertical slice QueryPlan 예시 | [queryplan.example.json](audit-bundle/queryplan.example.json) |
| Agent/API 계약 예시 | [agent_contract_examples.json](audit-bundle/agent_contract_examples.json) |
| 감사 스크립트와 결과 전체 재현 번들 | [원본 ZIP](<../../../../../2. Data/0. Source Archive/2026-07-28-gpt-pro-finance-agent-audit-bundle.zip>) |

## 검토 결론

### 채택

- Evidence-Compiled Hybrid SQL Agent
- Typed QueryPlan → server validation → deterministic SQL → verifier → evidence → renderer
- 해외 ETP를 이용한 첫 vertical slice
- field registry와 상품군별 정규화
- 0건 원인 분석과 사용자 확인 기반 조건 완화
- parser·retrieval·evidence·latency를 분리한 평가

### 수정 후 채택

- 펀드 통계는 raw-row coverage가 아니라 `itm_no` product grain으로 다시 계산한다.
- HCX 전송 schema와 서버의 엄격한 검증 schema를 분리한다.
- constraint strength를 `locked`, `ask_before_relaxing`, `preference`로 세분화한다.
- QueryPlan의 intent별 payload와 compiler 규칙을 명시한다.
- 초기 평가는 핵심 50문항으로 시작해 250~400개 검토 세트로 확장한다.

### 그대로 사용하지 않음

- `/mnt/data`와 `(1).xlsx`에 고정된 감사 스크립트
- `lxml` 의존성이 문서화되지 않은 펀드 전용 감사기
- HCX 미지원 keyword가 포함된 단일 QueryPlan schema
- 빈 evidence와 placeholder가 남은 계약 예시
- 보수 0을 실제 0으로 단정한 필터 결과
- 54 engineer-hour 또는 72시간 일정을 검증 없이 확정하는 계획

## 알려진 재현성 문제

- `run_finance_audit.py`와 `audit_fund_fast.py`는 GPT Pro 환경의 절대경로와 파일명에 의존한다.
- `audit_fund_fast.py`는 별도 `lxml` 설치가 필요하다.
- 표준 parser와 빠른 펀드 parser 사이에는 빈값·손상 행 표현 중심의 차이가 있다.
- 보강된 통합 JSON 전체가 제공된 스크립트만으로 생성되는 것은 아니다.
- 스크립트를 현재 위치에서 바로 실행하면 통합 감사 파일을 더 단순한 구조로 덮어쓸 수 있으므로, 원본 보존 상태에서는 실행하지 않는다.

구현 시 이 번들을 복사해 고치는 대신, 입력 경로·출력 경로·parser 버전·입력 hash를 명시적으로 받는 감사 파이프라인과 회귀 테스트를 새로 만든다.
