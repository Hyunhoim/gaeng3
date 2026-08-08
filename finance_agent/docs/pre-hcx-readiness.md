# HyperCLOVA X 연결 전 준비 기준

마지막 갱신: 2026-08-08

이 문서는 HyperCLOVA X API를 연결하기 전에 Agent Core에서 끝내야 할 구현,
평가, 계약과 외부 확인 게이트를 추적하는 정본이다. 완료 표시는 코드·테스트·
재현 가능한 산출물이 모두 일치할 때만 갱신한다.

## 0. 시작 기준선 — 2026-07-30 당시 기록

아래 내용은 준비 작업을 시작한 2026-07-30 당시 상태를 보존한 기록이다. 현재
상태는 이 문서의 완료 대상과 내부 완료 QA, [프로젝트 기준](project-baseline.md)을
함께 확인한다.

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
| 1 | 공통 진단·외부 holdout 프로토콜 | schema, validator, 공개 세트 유사도 검사, SHA 봉인, 최초 1회 상태 잠금 | 내부 완료·외부 작성 대기 |
| 2 | 네 상품군 capability matrix | QueryPlan·Oracle과 자동 정합성 검사 | 완료 |
| 3 | fail-closed Router·공통 답변 경로 | 실행·역질문·거절이 계약대로 분리되고 E2E 검증 | 완료 |
| 3A | 네 상품군 AGGREGATE | Decimal 계산·통화 gate·결측·기준일·독립 verifier·Backend evidence | 완료 |
| 3B | 네 상품군 COMPARE | exact identity·field capability·통화·기준일·stale·독립 verifier·Backend evidence | 완료 |
| 4 | BM25/SQLite FTS 문서 RAG | 적재·필터·top-k·근거·기준일·not-found 테스트 | 최소 기능 완료·실제 corpus 승인 대기 |
| 5 | 사람 rubric·Backend DTO | JSON 예시·schema·contract test 포함 | 계약 완료·사람 평가 대기 |
| 6 | baseline 동결·전체 QA | 회귀·wheel·문서·hash 검증과 외부 게이트 명시 | 내부 완료 |
| 7 | HyperCLOVA X provider 경계 | 세 operation·주입형 transport·오류·관측·전체 경로 E2E | 내부 8/8 완료·실제 HTTP 대기 |
| 8 | `/answer` service adapter·FastAPI route | HTTP status·안전한 ERROR DTO·fallback·비노출·입력 검증 계약 | adapter 12/12·Backend 34/34·Docker 기본/Qwen/장애 각 14/14 |
| 9 | `internal-red-team-v1` | 네 상품군 40문항·10개 공격 유형·전체 `/answer` E2E | expected·수정 후 로컬 Qwen 40/40 |
| 10 | 교차 상품군 grounded answer | family evidence 격리·교차 문구 검증·전체 fallback·무호출 | expected·로컬 Qwen 각각 4/4 |
| 11 | 금융 도메인 QA 실험 | 담당자 작성 40문항 hash 검증·단계별 E2E·Q002 SEARCH gold | v1.2 사후 회귀 40/40·잘못된 실행 0건 |
| 12 | 제출용 모델 경계 | 로컬 provider 제거 검사·투명한 개발·제출 분리 | 개발 자동 검사 통과·제출 자동 차단·공식 범위 서면 확인 대기 |
| 13 | 공식 평가 API adapter | `GET /answer`·다섯 문자열·전 결과 HTTP 200·60초 내부 예산 | route·DTO·상태·오류·55초 외곽 예산 완료 |
| 14 | 도메인별 Ontology | Turtle 5개·field registry 정합성·문법 검사 | 5개 생성·RDFLib 문법·registry exact-match 완료 |
| 15 | 공식 형식 공개 모의평가 | 난이도 10/10/10·답변 불가 5개·공식 5필드·latency | expected·로컬 Qwen 30/30·생성 16/17·안전 fallback 1건 |
| 16 | Qwen 변형·전체 Agent 스트레스 평가 | 세 표현 축·의미 선별·gold 감사·실패 단계·fallback 전후 비교 | 승인 변형 결정론적·Qwen 각각 77/77·fallback 0/61 |
| 17 | 원문 비공개 semantic round-trip | 실행 의미만 제공·계획 지문·근거 첨부 모델 계획 gate | 생성 75·선별 64, 최초 15/64→결정론적 출력·계획 64/64; 강화 Qwen 재실행 대기 |
| 18 | registry 기반 자동 커버리지 | 대표 계획 직접 실행·canonical 최초 관측·Qwen 자연화·shard 병합 | 305개 중 299개 직접 실행, canonical strict 최초 37/299; Qwen 897질문 실험 대기 |

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
- Ubuntu SSH Docker에서 health와 채권·국내 ETP·해외 ETP 실행, 공모펀드 잠금,
  역질문·미지원·HTTP 422의 7개 실제 요청 7/7
- Backend 단위·계약 테스트 30/30
- 실제 HyperCLOVA X·request 인증·주최 측 네트워크 transport 품질 점수가 아님

`internal-red-team-v1` 전체 E2E 결과:

- 네 상품군 각 10문항, 10개 공격 유형을 Router부터 Backend DTO까지 실행
- 과거 원본 정답표 기준 expected provider 40/40, 최초 로컬 Qwen strict 36/40·안전 차단 40/40
- 최초 네 실패는 모두 `3건`을 lexical linker가 limit 5로 해석한 `$.limit` 불일치
- Router와 linker의 단위 문법을 맞춘 뒤 로컬 Qwen strict·safety·evidence 40/40
- QueryPlan 12회·grounded answer 12회, provider 오류·verifier fallback 0건
- 이후 Qwen 변형 질문 감사에서 `낮은 순`·`짧은 순` 정답 2건이 반대 방향으로
  산출된 것을 발견함. 올바른 정렬을 적용한 현재 Agent를 변경하지 않은 원본 정답표로
  다시 채점하면 strict 38/40, safety 40/40이며 hash-pinned 교정표를 적용한 평가는 40/40
- 공개 내부 red-team이므로 독립 blind나 HyperCLOVA X 품질 점수가 아님

공식 형식 30문항 공개 모의평가 결과:

- 설명회 예상 분포대로 난이도 하·중·상 각 10문항, 답변 불가 5문항 구성
- 검색·비교·집계·안전·근거와 공식 5필드 계약 expected·로컬 Qwen 30/30
- 답변 생성 대상 17문항 중 16문항 grounded, 가치 판단 문구 1건은 안전 fallback
- 로컬 순차 실행 p50 1,553.318ms, p95 3,876.727ms, 최대 4,398.949ms
- self-authored 공개 모의평가이며 독립 blind·HyperCLOVA X·공모전 점수가 아님

Qwen 변형 질문·전체 Agent 스트레스 평가 결과:

- 공개 원문 30개에서 paraphrase·조건 순서 변경·무해한 부가 문장 90개 생성
- 숫자·상품 식별자·연산자·핵심 개념 보존 validator 통과 77개, 폐기 13개
- 최초 허용 변형 88개 중 60개 통과에서 시작해 Agent 표현 경계를 회귀로 보강
- `낮은 순`·`짧은 순` 두 gold 정렬 오류를 DB 값으로 확인하고 원본 보존 overlay 적용
- 교정 후 원문 30/30, 결정론적 Agent 변형 77/77, 전체 Qwen Agent 77/77
- Qwen 계획 43회·답변 61회에서 provider 오류 0, 의미·safety·evidence 100%
- 안전 설명문 계약 전 검증 fallback 3/61을 같은 전체 재실행에서 0/61로 감소
- 공개 원문 파생·사후 개선 회귀이므로 독립 blind나 공식 성능으로 해석하지 않음

원문 비공개 semantic round-trip 결과:

- Qwen에 기존 질문 문장을 주지 않고 서버가 확정한 실행 의미만 제공
- 정중체·구어체·전문 메모 75개 생성, 기계 의미 보존 64개 통과·11개 폐기
- 결정론적 Agent 최초 15/64를 보존하고 라우팅·식별·비교·집계 표현 공백을 분류
- 출력만 같고 QueryPlan이 다른 5건을 추가로 발견하도록 평가 지문 강화
- 공통 규칙 보강 후 강화된 출력·QueryPlan 의미, safety, evidence 모두 64/64
- Qwen grounded-plan 최초 28/64·gate 구제 9건을 관측했으나 결과 분석 후 강화한
  prompt·gate는 같은 frozen batch에서 재실행해야 함
- 실제 존재하지만 질문에 없는 상품 ID, 부정된 ID·조건·정렬, malformed JSON은
  실행 권한을 얻지 못하도록 단위 회귀로 고정
- 공개 정답 파생·사후 개선 회귀이므로 독립 blind나 공식 점수로 해석하지 않음

registry 기반 자동 커버리지 최초 관측:

- queryable·sortable·comparable·aggregatable 필드와 실제 DB 값을 조합해 대표
  capability 좌표 305개를 자동 구성
- 서로 다른 날짜·통화 값이 부족한 6개는 제외 이유를 보존하고, 나머지 정답
  QueryPlan 299개는 Oracle·Verifier·field evidence 직접 실행에 성공
- 같은 계획의 규칙형 질문을 현재 Agent에 넣은 최초 strict는 37/299,
  실행 도달 254/299, 계획 의미 44/299, 근거 의미 77/299
- 실패의 첫 단계는 질문 분류 45건, 작업 계획 210건, 근거 7건으로 자연어
  이해층이 현재 가장 큰 병목임을 확인
- 비교 0/72, 일반 필드 집계 0/34, 조건 검색 4/112를 숨기지 않고 최초
  baseline으로 동결
- Qwen은 canonical 원문 없이 의미 명세만 보고 정중체·구어체·검색창형
  최대 897문항을 생성하며, 기계 의미 선별과 hash 기반 shard·실행 병합 후 평가
- 자동 생성·공개 데이터 진단이므로 독립 blind·실사용 분포·공모전 점수가 아님

부정 표현 안전장치 보강 후 교차 회귀 결과:

- 상품 비교 30/30, 네 상품군 검색·집계 8/8, 금융 도메인 개발 QA 40/40
- `공모가 아닌 공모펀드`, `거래 가능하지 않은 ETF`, `AUM이 크지 않은`,
  `특정 상품을 제외한`처럼 조건을 반대로 해석하기 쉬운 질문은 임의 실행하지 않고
  미지원 또는 조건 확인으로 종료
- 전체 단위·계약 테스트 461/461, lint·format 검사 통과
- 위 결과도 이미 확인한 공개 개발 세트의 사후 회귀이며 독립 blind가 아님

같은 30문항의 실제 Docker FastAPI `GET /answer` 최초 관측:

- 공식 다섯 문자열과 질문당 60초 예산 30/30
- 기대 검색·비교·집계 의미 일치 24/30, 답변 불가 안전 처리 5/5
- 실패 6건은 모두 의도적인 공모펀드 공식 실행 잠금이며 HTTP·Qwen 오류 0건
- Qwen 도달 13건 중 grounded 12건, 채권 가치 판단 문구 1건은 안전 fallback
- p50 486.924ms, p95 2,491.057ms, 최대 2,885.126ms
- 실제 배포 설정의 최초 관측이며 내부 평가 전용 30/30과 구분해 보존

최초 결과 보존 후 공모펀드 명시적 v1 승인 경로 재평가:

- 기본 `locked`는 유지하고 `public_fund_v1_approved`를 지정한 배포에서만 공모펀드 실행
- 같은 동결 30문항 의미·공식 형식·60초 30/30, 답변 불가 5/5
- Qwen 문장 검증 17/17, fallback 0건
- 실험 후 Backend 기본 `locked` 복구와 Qwen 프로세스·GPU 메모리 종료 확인
- 이 정책은 팀 내부 배포 승인이며 주최 측의 공식 이용 승인을 뜻하지 않음

교차 상품군 grounded answer 결과:

- 국내·해외 ETP를 별도 QueryPlan·Oracle·Verifier·evidence 경계로 유지
- Answer provider에는 한 번에 한 상품군 evidence만 전달하고 서버가 최종 조합
- expected·로컬 Qwen 공개 4문항 각각 4/4, 생성 대상 2문항 grounded
- 실제 로컬 모델 호출 3회, fallback 0, 전체 빈 결과·control 모델 무호출
- 다른 상품군 언급·교차 비교·합산 또는 family 하나의 실패 시 전체 결정론 fallback
- 공개 기존 문항의 배선 회귀이며 독립 blind나 HyperCLOVA X 품질 점수가 아님

금융 도메인 QA 최초 관측과 사후 회귀:

- 금융 도메인 담당자 작성·AI 담당자 검토 40문항을 원본 수정 없이 hash로 고정
- SEARCH 1·CLARIFY 9·UNSUPPORTED 17·문서 RAG 9·외부 정책 2·외부 데이터 2
- 현재 결정론적 `/answer` 경로 strict 1/40, route 1/40, safety·evidence 32/40
- control이어야 할 7문항 검색 실행과 1문항 오류를 수정 전 baseline으로 보존
- Q002 SEARCH QueryPlan·Oracle 후보·상위 ID·evidence 지문 1/1 완성
- Router·linker 개선 후 v1.2 strict·route·safety·evidence·answer
  40/40, control 잘못된 실행·오류 0건
- 문서·외부 dependency 13문항은 별도 pending 유지
- v1.2 40/40은 개선에 사용한 MFT 세트의 회귀이며 독립 blind·
  LLM 생성 품질·공식 평가 점수가 아님

## 3. 외부 완료 게이트

다음 항목은 저장소 코드만으로 완료할 수 없으며 최종 baseline과 분리해 관리한다.

- 금융 도메인 담당자가 독립 작성한 네 상품군 blind 질문과 비공개 정답키
- 봉인 이후 단 한 번 수행하는 최초 blind 실행
- 금융 도메인 담당자와 팀원이 수행한 사람 평가 점수
- 주최 측이 허용한 외부 비정형 문서 corpus와 사용 범위 확인
- HyperCLOVA X 정확한 모델명·Structured Outputs 범위·endpoint·인증·제출 범위의
  공식 서면 확인과 크레딧 수령·적용 서비스 확인
- 위 공식 답변 확인 후의 실제 HTTP transport 연결과 공식 재현
- 공식 답변에 따른 제출 후보의 로컬 LLM provider·설정·
  스크립트·의존성 제거와 정적·기계적 검수
- 주최 측 실행 환경에서 Docker·포트·인증·네트워크 정책 최종 재현

설명회 현장 자료에서 공식 `GET /answer`와 Ontology 요구는 확인했다. 위 게이트가
남아 있는 동안 저장소는 “HyperCLOVA X 연결 전 내부 준비 완료”까지만
주장할 수 있고, 최종 평가 준비 완료나 일반화 성능 완료를 주장하지 않는다.

## 4. 내부 완료 QA

- Agent Core pytest `461 passed`
- Backend pytest `34 passed`
- Ruff lint와 format 통과
- 문서 검사 `57 Markdown files`, `41 evaluation baselines` 통과
- `pip check` 통과
- build isolation 없이 wheel 생성과 신규 JSON package data 포함 여부 통과
- `git diff --check` 통과
- source·test·문서·baseline·protocol tree SHA-256 manifest 검증
- 공식 XLSX 4종에서 Docker volume의 SQLite 4개를 자동 생성하고 두 번째 실행에서
  모두 재사용, 기본 Backend·공식 GET 확장 스모크 14/14 통과
- 실제 Docker 공식 GET 30문항의 형식·60초 30/30, 의미 24/30과 공모펀드 잠금
  6건의 최초 관측 보존
- 명시적 공모펀드 v1 승인 경로의 동일 30문항 30/30과 실험 후 기본 잠금 복구 확인
- 로컬 Qwen·공모펀드 승인 Docker 스모크 14/14, 모델 중단 fallback 14/14,
  공식 모의 30문항 동시성 2에서 30/30·fallback 0 확인
- 결정론적 공식 30문항은 동시성 1·2·4에서 모두 30/30. 단일 worker에서
  동시성이 커질수록 지연이 증가해 처리량·SLO 근거로는 사용하지 않음
- 외부 blind 100문항은 공개 질문 유사도 검사와 원자적 최초 실행 상태·report hash
  결합까지 구현. 실제 질문·정답 작성과 최초 실행은 외부 게이트로 유지
- 제출 경계 개발 프로필 통과, 제출 프로필은 현재 로컬 개발 흔적을 의도적으로 차단

source freeze는
`evaluation/protocols/pre-hcx-readiness-v1.manifest.json`에 보존한다. 외부
게이트가 남아 있으므로 status는 `internal_ready_external_gates_pending`이다.
