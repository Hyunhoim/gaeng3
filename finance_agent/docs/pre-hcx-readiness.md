# HyperCLOVA X 연결 전 준비 기준

마지막 갱신: 2026-07-30

이 문서는 HyperCLOVA X API를 연결하기 전에 Agent Core에서 끝내야 할 구현,
평가, 계약과 외부 확인 게이트를 추적하는 정본이다. 완료 표시는 코드·테스트·
재현 가능한 산출물이 모두 일치할 때만 갱신한다.

## 0. 시작 기준선

점검 대상은 `haeyeongcho` 브랜치의 현재 작업 트리다. 공모펀드 자연어 비교
작업이 아직 커밋되지 않은 상태이므로 기존 변경을 보존하고 그 위에서 작업한다.
이번 준비 작업에서는 commit, push, PR을 수행하지 않는다.

2026-07-30 최초 점검 결과:

- pytest `204 passed`
- Ruff lint 통과
- 문서 검사 `24 Markdown files`, `12 evaluation baselines` 통과
- `pip check` 통과

최초 확인한 핵심 간극과 현재 해결 상태:

- QueryPlan 계약에는 `search`, `compare`, `aggregate`, `explain`이 있으나
  SQLite Oracle은 `search`, `compare`만 실행 가능 → 네 상품군 공통
  AGGREGATE Oracle·독립 verifier·evidence까지 해결
- 공통 Intent Router가 없고 상품군별 linker와 공모펀드 비교 parser가 분리됨
- `detail`, `clarify`, `unsupported`는 공통 라우팅 계약으로 표현되지 않음
- 네 상품군과 일곱 질의 유형을 함께 측정하는 진단 세트가 없음
- 상품군·질의 유형별 실행 가능 범위를 기계적으로 검사하는 capability matrix가 없음
- 비정형 문서를 위한 BM25/SQLite FTS 검색 계층이 없음
- 사람 평가 rubric과 Backend 전달용 프레임워크 독립 DTO가 확정되지 않음
- 독립 blind holdout과 사람 평가는 금융 도메인 담당자의 외부 작성·검수가 필요

## 1. 완료 대상

| 단계 | 산출물 | 완료 조건 | 상태 |
| --- | --- | --- | --- |
| 0 | 현재 코드·문서·평가 감사 | 시작 검사와 간극 기록 | 완료 |
| 1 | 공통 진단·외부 holdout 프로토콜 | schema, validator, SHA 봉인, 진단 전·후 report | 내부 완료·외부 작성 대기 |
| 2 | 네 상품군 capability matrix | QueryPlan·Oracle과 자동 정합성 검사 | 완료 |
| 3 | fail-closed Router·공통 답변 경로 | 실행·역질문·거절이 계약대로 분리되고 E2E 검증 | 완료 |
| 3A | 네 상품군 AGGREGATE | Decimal 계산·통화 gate·결측·기준일·독립 verifier·Backend evidence | 완료 |
| 3B | 네 상품군 COMPARE | exact identity·field capability·통화·기준일·stale·독립 verifier·Backend evidence | 완료 |
| 4 | BM25/SQLite FTS 문서 RAG | 적재·필터·top-k·근거·기준일·not-found 테스트 | 최소 기능 완료·실제 corpus 승인 대기 |
| 5 | 사람 rubric·Backend DTO | JSON 예시·schema·contract test 포함 | 계약 완료·사람 평가 대기 |
| 6 | baseline 동결·전체 QA | 회귀·wheel·문서·hash 검증과 외부 게이트 명시 | 내부 완료 |
| 7 | HyperCLOVA X provider 경계 | 세 operation·주입형 transport·오류·관측·전체 경로 E2E | 내부 8/8 완료·실제 HTTP 대기 |
| 8 | `/answer` service adapter | HTTP status·안전한 ERROR DTO·fallback·비노출 계약 | 프레임워크 독립 12/12 완료·FastAPI route 대기 |
| 9 | `internal-red-team-v1` | 네 상품군 40문항·10개 공격 유형·전체 `/answer` E2E | expected·수정 후 로컬 Qwen 40/40 |

## 2. 평가 해석 원칙

- AI 담당자가 만든 문항은 `internal diagnostic`으로만 부르고 최종 blind라고
  주장하지 않는다
- 최종 blind 질문과 비공개 정답키는 금융 도메인 담당자가 독립 작성하고,
  최초 공개 전에 질문·정답·코드 버전을 SHA-256으로 봉인한다
- 공개 회귀 세트의 100%는 배선과 회귀 안정성을 뜻하며 일반화 성능을 뜻하지 않는다
- 로컬 Qwen 결과는 개발 경로의 관측값이며 공식 HyperCLOVA X 성능이 아니다
- 지원하지 않는 기능을 억지로 실행하지 않고 `clarify` 또는 `unsupported`로
  명시적으로 종료하는 것도 올바른 결과로 채점한다

내부 라우팅 진단 결과:

- Router 도입 전 search 강제 replay: 4/28, strict accuracy `0.142857`
- 현재 fail-closed Router: 28/28, strict accuracy `1.0`
- 현재 AGGREGATE·COMPARE capability를 포함한 diagnostic v3 suite SHA-256:
  `8bab9b0f4fd3e40782c591e2e3aea2f9d76b94a2d31d846d8b957604af0313b0`
- v1·v2는 당시 capability의 봉인 이력으로 그대로 보존
- 위 결과는 공개 배선 진단이며 독립 blind나 최종 답변 점수가 아님

자연어 비교 공개 회귀 결과:

- 해외 ETP·국내 ETP·국내채권 신규 30문항 30/30
- 실행 18문항과 안전 차단 12문항의 계획·순서·상태·차이·Backend 계약 100%
- 기존 공모펀드 24문항과 합친 네 상품군 공개 비교 질문 54문항
- suite SHA-256:
  `7eec27471f2dbd218b0ed056f03b02ef69aed6b283055d72d98ff7d423e411ee`
- 공개 회귀이므로 독립 blind 일반화 성능으로 해석하지 않음

SEARCH·AGGREGATE 경량 verifier 결과:

- 네 상품군 대표 SEARCH·AGGREGATE 8문항 결과 지문 8/8 일치
- 각 문항을 새 프로세스에서 실행한 p50 308.749ms, 최대 추가 RSS 51,000KiB
- QueryPlan에 필요한 열만 별도 projection으로 읽어 전체 원천값 사전 적재 제거
- 국내채권 약 92~93%, 공모펀드 약 94~95%의 추가 RSS 감소
- 단일 개발 장비·문항별 1회 측정이며 운영 SLO나 일반화 성능으로 해석하지 않음

HyperCLOVA X provider 경계 결과:

- QueryPlan·공모펀드 비교 초안·근거 답변의 세 operation 계약 완료
- evaluation·production mode와 `LLM_PROVIDER=hyperclova` 조합만 허용
- 응답 schema를 공식 Structured Outputs 지원 subset으로 전송 전 검사
- 401·403·429·500, timeout, 연결 실패, 잘못된 응답을 fake transport로 검증
- prompt와 오류 본문을 제외한 token·latency·상태 call record 계약 완료
- 실제 endpoint·credential·인증 header·HTTP transport는 공식 계약 확인 후 구현

HyperCLOVA X API 없는 전체 경로 결과:

- 해외 ETP·국내 ETP·국내채권 SEARCH가 QueryPlan부터 Backend DTO까지 통과
- HCX QueryPlan은 서버 기준계획과 완전히 일치할 때만 Oracle 실행
- 잘못된 답변 순서는 Answer Verifier가 결정론적 fallback으로 전환
- timeout·금지 질의·비활성 공모펀드·계획 불일치를 호출 단계에 맞춰 차단
- 동결 `hcx-contract-e2e-8` 8개 시나리오 8/8, 네트워크 호출 0건
- 실제 HCX 품질·비용·latency·API 호환성 점수로 해석하지 않음

Backend `/answer` service adapter 결과:

- 정상·control·not-found·검증된 fallback은 의미 있는 Agent 응답이므로 HTTP 200
- QueryPlan provider 설정·인증·rate limit·서비스·timeout·transport·응답
  장애는 안전한 `provider_unavailable`과 HTTP 502·503·504로 변환
- SQLite·dataset I/O와 알 수 없는 내부 예외는 evidence 없는 `error` DTO로 변환
- grounded answer provider 장애는 검증된 evidence를 사용한 결정론적 fallback
- 질문·credential·provider 본문·파일 경로 비노출 포함 동결 12개 시나리오 12/12
- 실제 FastAPI route·request 인증·네트워크 transport 품질 점수가 아님

`internal-red-team-v1` 전체 E2E 결과:

- 네 상품군 각 10문항, 10개 공격 유형을 Router부터 Backend DTO까지 실행
- expected provider 40/40, 최초 로컬 Qwen strict 36/40·안전 차단 40/40
- 최초 네 실패는 모두 `3건`을 lexical linker가 limit 5로 해석한 `$.limit` 불일치
- Router와 linker의 단위 문법을 맞춘 뒤 로컬 Qwen strict·safety·evidence 40/40
- QueryPlan 12회·grounded answer 12회, provider 오류·verifier fallback 0건
- 공개 내부 red-team이므로 독립 blind나 HyperCLOVA X 품질 점수가 아님

## 3. 외부 완료 게이트

다음 항목은 저장소 코드만으로 완료할 수 없으며 최종 baseline과 분리해 관리한다.

- 금융 도메인 담당자가 독립 작성한 네 상품군 blind 질문과 비공개 정답키
- 봉인 이후 단 한 번 수행하는 최초 blind 실행
- 금융 도메인 담당자와 팀원이 수행한 사람 평가 점수
- 주최 측이 허용한 외부 비정형 문서 corpus와 사용 범위 확인
- HyperCLOVA X 모델명·Structured Outputs 범위, endpoint·인증·실제 HTTP
  transport 확인과 공식 재현

이 게이트가 남아 있는 동안 저장소는 “HyperCLOVA X 연결 전 내부 준비 완료”까지만
주장할 수 있고, 최종 평가 준비 완료나 일반화 성능 완료를 주장하지 않는다.

## 4. 내부 완료 QA

- pytest `320 passed`
- Ruff lint와 format 통과
- 문서 검사 `45 Markdown files`, `24 evaluation baselines` 통과
- `pip check` 통과
- build isolation 없이 wheel 생성과 신규 JSON package data 포함 여부 통과
- `git diff --check` 통과
- source·test·문서·baseline·protocol tree SHA-256 manifest 검증

source freeze는
`evaluation/protocols/pre-hcx-readiness-v1.manifest.json`에 보존한다. 외부
게이트가 남아 있으므로 status는 `internal_ready_external_gates_pending`이다.
