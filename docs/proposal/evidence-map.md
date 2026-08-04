# 기술 제안서 근거 맵

상태: 활성

기준일: 2026-07-31

제안서에 사용하는 주장과 실제 코드·문서·baseline을 연결한다. `검증 완료`는
해당 범위의 저장소 회귀를 통과했다는 뜻이며 공식 평가 성능을 뜻하지 않는다.

## 공식 기술 제안서 항목

| 항목 | 사용할 핵심 주장 | 근거 | 상태 |
| --- | --- | --- | --- |
| 제안 요약 | LLM과 결정론적 검색·검증을 분리한 Evidence-Compiled Hybrid SQL Agent | [프로젝트 기준](../../finance_agent/docs/project-baseline.md) | 검증 완료 |
| 문제 정의 | 네 상품군의 grain·필드·결측·기준일이 서로 다름 | [데이터 감사](../../finance_agent/docs/data-audit.md) | 검증 완료 |
| 제안 방법 | typed QueryPlan·Oracle·Verifier·field evidence·Answer Verifier | [계약](../../finance_agent/docs/contracts.md) | 검증 완료 |
| 시스템 구성도 | Agent Core 검증 완료, FastAPI·HCX 실제 연결 대기 | [연결 전 readiness](../../finance_agent/docs/pre-hcx-readiness.md) | 부분 완료 |
| 주요 기능 흐름 | SEARCH·교차 상품군 독립 검색·family별 grounded answer·교차 검증·전체 fallback·COMPARE·AGGREGATE·제어 응답 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md) | 검증 완료 |
| 사용자 시나리오 | 검색·비교·집계·역질문·거절 | [사용자 시나리오](user-scenarios.md) | 금융·화면 검수 대기 |
| 기대효과·확장성 | 근거 추적·안전 fallback·상품군 adapter·평가 재사용 | [Backend 계약](../../finance_agent/docs/backend-contract.md) | 내부 계약 완료 |

## 정성평가 축

| 평가 축 | 현재 근거 | 남은 증거 |
| --- | --- | --- |
| 문제정의 | 데이터 grain·품질·금융 안전 문제를 실제 원천 수치로 정의 | 금융 도메인 담당자의 사용자 pain point 검수 |
| 기술완성도·성능 | 전체 테스트, 검색·집계 성능 기준선, family별 grounded answer, provider·오류 계약 | 실제 FastAPI·HCX·Docker·부하 테스트 |
| 창의성·확장성 | LLM 계획과 서버 계획의 exact-match gate, 독립 verifier, field evidence | 기존 방식 대비 비교표와 실제 확장 사례 |
| 답변 정확성·완결성 | 상품명·수치·순위·근거·기준일 Answer Verifier와 red-team | external blind와 실제 사람 평가 |
| 현업 활용성·리스크 관리 | 결측·stale·통화·추천 금지·fallback·오류 비노출 | 실제 사용자 시나리오·화면 데모·운영 로그 |

## 정량 근거

| 주장 | 값 | 정본 | 해석 제한 |
| --- | ---: | --- | --- |
| 원천 감사 | 4종 145,393행, 65/65 | [데이터 감사](../../finance_agent/docs/data-audit.md) | 제공 스냅샷 기준 |
| 전체 코드 회귀 | pytest 331개 | [readiness](../../finance_agent/docs/pre-hcx-readiness.md) | Agent Core 범위 |
| 교차 상품군 SEARCH | 국내·해외 ETP 4/4 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md) | 공개 회귀, not blind |
| 교차 상품군 grounded answer | expected·로컬 Qwen 각각 4/4; 생성 대상 2문항 모두 grounded; 실제 모델 호출 3회; fallback 0 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md)·[baseline](../../finance_agent/evaluation/baselines/cross-family-answer-v1.json) | 공개 4문항, not blind; 로컬 Qwen은 개발 전용 |
| 내부 red-team | 수정 후 40/40 | [red-team 평가](../../finance_agent/docs/evaluation-internal-red-team.md) | self-authored, not blind |
| 네 상품군 자연어 비교 공개 회귀 | 54문항 | [비교 평가](../../finance_agent/docs/evaluation-product-comparison.md) | 공개 회귀 |
| SEARCH·AGGREGATE 결과 지문 | 8/8 | [성능 기준선](../../finance_agent/docs/evaluation-search-aggregate-performance.md) | 단일 개발 장비 |
| HCX API 없는 전체 계약 | 8/8 | [HCX provider](../../finance_agent/docs/hyperclova-provider.md) | 실제 API 성능 아님 |
| Backend service adapter | 12/12 | [Backend 계약](../../finance_agent/docs/backend-contract.md) | 실제 HTTP route 아님 |

## 완료되지 않은 주장

다음 표현은 근거가 생기기 전까지 기술 제안서에서 완료형으로 사용하지 않는다.

- HyperCLOVA X 연결·성능·비용 검증 완료
- 실제 FastAPI 평가 서버와 Docker 재현 완료
- 독립 blind 일반화 성능 100%
- 실제 투자설명서·약관 기반 문서 RAG 완료
- 상품군 간 직접 수치 비교 지원
- 세 상품 이상 비교·환율 환산·개인화 투자 추천 지원
- 사람 평가에서 현업 유용성 입증

## 갱신 규칙

- 수치 변경 시 원본 baseline과 이 표를 같은 변경 단위로 갱신
- 최초 관측과 사후 수정 결과를 함께 기록
- 성능값에는 장비·실행 횟수·평가 범위를 함께 명시
- 계획을 완료로 바꾸기 전에 재현 명령과 담당자 검수를 연결
