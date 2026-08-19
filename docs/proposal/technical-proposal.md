# 금융상품 Agent 기술 제안서

상태: 팀 검토 전 초안 v0.3 · P0-6 제공 관계 검색 기반 반영

기준일: 2026-08-19

이 문서는 최종 PDF 또는 발표자료를 만들기 위한 내용 정본이다. 구현되지 않은
기능과 외부 확인이 필요한 항목은 완료된 기능처럼 표현하지 않는다.

## 1. 제안 요약

서로 다른 스키마와 품질 특성을 가진 국내채권·국내 ETF·해외 ETF·공모펀드
데이터를 하나의 검증 가능한 금융상품 Agent로 연결한다.

제안하는 **Evidence-Compiled Hybrid SQL Agent**는 LLM이 자연어를 제한된
QueryPlan으로 변환하고 결과를 설명하게 하되, 상품 필터링·정렬·비교·집계와
수치 검증은 Python·SQLite의 결정론적 도구가 담당한다. 최종 답변은 상품별
field-level evidence와 기준일을 갖추며, 검증에 실패하면 근거 없는 생성을
중단하고 결정론적 답변 또는 역질문으로 전환한다.

## 2. 문제 정의

금융상품 검색은 단순 키워드 일치로 해결하기 어렵다.

- 네 상품군의 logical grain과 필드 의미가 서로 다름
- 같은 `수익률`, `규모`, `위험` 표현도 상품군별 산식·단위·기준일이 다름
- 결측, sentinel, 오래된 동적 값과 손상 행을 정상값처럼 처리할 위험이 있음
- 자연어 질문에는 상품군·조건·비교 기준이 생략되거나 서로 충돌할 수 있음
- 생성 모델이 데이터에 없는 수치·전망·추천을 만들면 금융 답변의 신뢰성이 훼손됨

따라서 목표는 “자연스러운 문장 생성”만이 아니라 다음을 동시에 만족하는 것이다.

1. 질문을 실행 가능한 조건과 실행할 수 없는 요구로 분리
2. 대규모 정형 데이터를 재현 가능하게 검색·연산
3. 모든 상품·수치·순위·기준일을 원천 근거로 추적
4. 정보 부족·결과 없음·금지 요청을 안전하게 처리

## 3. 제안 방법

### 3.1 데이터 구조화

- 원천 파일과 schema 파일의 hash·행 수·필드 품질을 감사
- 상품군별 logical grain, canonical field와 source mapping을 registry로 관리
- 손상 행과 실행 불가능한 값을 격리하고 원천 row·column·기준일 보존
- 국내채권·국내 ETP·해외 ETP·공모펀드를 상품군별 SQLite로 정규화

### 3.2 질의 이해와 실행 계약

- fail-closed Intent Router가 SEARCH·COMPARE·AGGREGATE·EXPLAIN·CLARIFY를 구분
- 서버 compiler가 상품군 capability, 필드, 연산자, 정렬과 limit의 기준계획 생성
- 복수 상품군 SEARCH는 상품군별 단일 QueryPlan으로 나눠 독립 실행
- LLM을 사용하는 SEARCH의 출력은 축소된 schema의 typed QueryPlan으로 제한
- SEARCH 모델 계획과 서버 기준계획이 다르면 Oracle을 실행하지 않고 역질문
- COMPARE·AGGREGATE는 현재 서버의 결정론적 compiler를 유지

### 3.3 결정론적 검색·연산

- parameterized SQLite Oracle이 필터·정렬·순위와 후보 수 계산
- 복수 상품군 SEARCH는 Oracle·Verifier를 병렬 실행하고 부분 결과를 보존
- 비교는 같은 상품군의 정확한 두 상품과 승인 필드에 한정
- 집계는 Decimal 기반 COUNT·MIN·MAX·AVG와 제한된 SUM을 지원
- 통화·단위·기준일이 호환되지 않으면 차이 또는 합산을 차단
- 제공 데이터의 발행사·운용사·기초지수·자산유형·투자지역은 별도 SQLite FTS5
  관계 색인으로 구성하고, 후보 상품 ID를 공식 상품 DB에서 다시 확인
- 현재 승인 DB에서 관계 58,005개와 검색 smoke 4/4를 검증했지만, 금융 alias·
  관계 의미 검수와 P0-7 Claim Verifier가 끝날 때까지 Agent 답변에는 연결하지 않음

### 3.4 근거와 답변 검증

- 독립 Result Verifier가 Oracle 결과를 별도 Python 경로로 재계산
- field-level evidence가 값·품질·원천·기준일을 보존
- LLM은 상품군별 질문·계획·검증된 evidence만 입력받아 설명 초안을 생성
- 서버가 상품군별 답변 섹션을 조합하고, 교차 상품군 문구·비교·집계를 다시 검증
- Answer Verifier가 상품명·수치·순위·근거·기준일을 다시 확인
- 하나라도 검증에 실패하면 전체 답변을 deterministic fallback으로 전환하며, 데이터 부족이면 역질문, 금지 요청이면 거절

### 3.5 문서 설명

BM25/SQLite FTS 기반 문서 검색의 적재·필터·출처·기준일 계약을 구현했다.
외부 문서는 금융·데이터 권한 독립 review, HTTPS 출처, 사용 권한 4종,
byte·정규화 본문 SHA-256, canonical manifest, 변조·경로·덮어쓰기 차단을 통과해야
별도 BM25 색인으로 build된다. 이 반입 계약은 합성 문서 24/24를 통과했지만,
실제 투자설명서·약관·용어집은 출처·사용 권한·검색 품질을 확인한 뒤에만
Release에 연결한다.

## 4. 시스템 구성도

[시스템 구성도 정본](diagrams/system-architecture.md)은 다음 두 범위를 분리한다.

- 현재 검증 완료: Agent Core, SQLite Oracle, Verifier, evidence, Backend DTO,
  FastAPI 내부 `POST /answer`, 공식 `GET /answer` 계약, Ontology Turtle 5개,
  Docker 데이터 준비·HTTP smoke
- 교차 상품군 SEARCH의 family별 근거 격리·답변 검증·전체 fallback
- 승인 상품 DB의 제공 관계 58,005개 색인·공식 상품 ID 재검증·출처 추적
- 외부 통합 대기: Next.js, 공식 `GET /answer` 공개 통신 재현,
  HyperCLOVA X transport, 관계 QueryPlan·Claim Verifier, 공개 API 서버

목표 구조를 현재 구현 완료 상태로 오해하지 않도록 실선과 점선으로 구분한다.

## 5. 주요 기능 흐름도

[답변 기능 흐름도](diagrams/answer-flow.md)를 정본으로 사용한다.

| 기능 | 현재 범위 | 안전 경계 |
| --- | --- | --- |
| SEARCH | 네 상품군 조건 검색·상세 조회, 복수 상품군 독립 검색과 family별 grounded answer | 공통 조건만 상품군별 실행·근거를 family별로 격리·직접 비교 금지 |
| COMPARE | 같은 상품군의 정확한 두 상품 | 통화·기준일·결측 불일치 차단 |
| AGGREGATE | 네 상품군 COUNT·MIN·MAX·AVG·제한 SUM | 독립 Python 재검산 |
| EXPLAIN | 정형 evidence 설명, 문서 RAG·승인 반입 최소 계약 | 실제 corpus 출처·권한·검색 평가 대기 |
| CLARIFY | 상품군·식별자·기준 누락 | Oracle·LLM 불필요 호출 차단 |
| UNSUPPORTED | 전망·수익 보장·단정 추천 | 근거 없는 생성 금지 |

상품군 간 직접 수치 비교, 세 상품 이상 비교, 환율 환산과 우열·추천 판단은
현재 실행 범위가 아니다.

## 6. 사용자 시나리오

[사용자 시나리오 정본](user-scenarios.md)은 다음 상황을 포함한다.

1. 해외 ETF 조건 검색과 근거 확인
2. 국내채권 상세 조회와 오래된 동적 값 경고
3. 국내 ETF 또는 공모펀드의 조건 집계
4. 같은 상품군 두 상품 비교
5. 국내·해외 ETP 교차 검색과 family별 근거 답변
6. 모호한 조건 역질문과 단정적 추천 거절

최종 화면 시나리오는 Next.js 통합 후 실제 캡처와 API 응답으로 교체한다.

## 7. 기대효과·확장성

### 기대효과

- 상품 선택 근거를 dataset·row·column·기준일까지 추적 가능
- 모델 오류가 곧바로 잘못된 SQL·답변으로 이어지지 않는 다중 검증 경계
- 결측·stale·통화 불일치를 숨기지 않아 금융 답변의 리스크 감소
- 복수 상품군에서도 다른 상품군의 근거가 섞이지 않고, 검증 실패 시 전체를 안전하게 fallback
- 새로운 모델을 연결해도 Oracle·Verifier·평가 기준을 재사용 가능
- 로컬 개발 모델로 반복 검증하고 공식 경로는 HyperCLOVA X로 제한해 비용 통제

### 확장성

- 상품군 adapter·registry를 추가해 데이터 schema 변화에 대응
- 문서 corpus 승인 후 동일 citation 계약으로 비정형 설명 확장
- compact identity cache와 필요한 필드만 읽는 projected verifier로 메모리 절감
- Backend DTO와 공식 제출 schema를 분리해 UI·평가 API 변화에 대응
- `GET /answer` 공식 다섯 문자열 필드는 별도 adapter로 고정해 내부 DTO 확장과 분리
- 평가 suite·baseline·hash를 통해 기능 추가 후 회귀 여부를 자동 확인

확장성은 “어떤 질문도 처리”한다는 뜻이 아니다. 새 필드·상품군·문서는 의미,
단위, 기준일과 검증 방법이 승인된 뒤 capability matrix에 추가한다.

## 8. 기술완성도·성능·정확성 근거

정량 주장과 해석 제한은 [제안서 근거 맵](evidence-map.md)을 따름
행동 기능 검사·구조화 출력 제약·단계별 평가·통계 비교의 출처와 실제 차용 범위는
[연구 근거](research-basis.md)에 분리해 기록

현재 대표 근거:

- 4종 원천 145,393행 감사, 핵심 expectation 65/65
- Agent Core 1,305 passed·2 조건부 skip, Backend 최근 기준 320 passed
- 공식 XLSX에서 SQLite 4개를 자동 생성·검증한 뒤 Backend를 시작하는 Docker 경로 완료
- 승인 상품 DB 관계 58,005개·관계 계약 14/14·실제 검색 smoke 4/4와 공식 상품 ID 재검증 완료
- 국내·해외 ETP 교차 SEARCH 공개 실제 데이터 회귀 4/4
- 교차 상품군 grounded answer 공개 회귀 expected·로컬 Qwen 각각 4/4, 생성 대상 2문항 모두 grounded, 모델 호출 3회, fallback 0
- 내부 red-team 40문항 수정 후 strict·safety·evidence 40/40
- 공개 원문 30개에서 Qwen으로 세 표현 축 90개를 만들고 의미 보존 77개를
  선별한 스트레스 회귀에서 결정론적·Qwen 전체 Agent 각각 77/77,
  의미·safety·evidence 100%, 안전 설명문 개선 후 fallback 0/61
- 변형 질문으로 기존 `낮은 순`·`짧은 순` gold 오류 2건을 발견하고 원본을
  바꾸지 않는 hash-pinned 교정 overlay로 평가 정답 자체도 감사
- 같은 77개를 모델 없음·Qwen 계획만·Qwen 답변만·둘 다 사용으로 나눈 ablation에서
  모두 77/77을 확인하고, 전체 Qwen p95 4,096.584ms를 역할별 비용 기준선으로 확보
- Qwen에 원문 문장을 숨기고 실행 의미만 주어 75개를 재생성한 더 어려운
  semantic round-trip에서 64개를 선별하고, 최초 15/64에서 출력과 QueryPlan
  의미까지 64/64로 개선
- Qwen grounded-plan을 단순 JSON 정답이 아닌 원문 근거 첨부 제안으로 바꾸고,
  registry·유일 식별자·서버 고정 조건 gate를 통과한 항목만 실행하는 계약 구현
- 실재하지만 원문에 없는 상품 ID, 부정된 ID·필드·정렬, 서버 조건 누락,
  malformed provider 응답이 실행 권한을 얻지 못하도록 공격 회귀로 고정
- registry와 실제 DB에서 대표 기능 좌표 305개를 자동 구성하고 정답 계획
  299개를 직접 실행해 검색·계산·근거 엔진의 넓은 실행 범위를 확인
- 같은 299개 계획의 canonical 자연어 최초 strict 37/299를 보존하고,
  질문 분류 45건·작업 계획 210건·근거 7건으로 실패 시작점을 분해해
  다음 Qwen 자연화 897문항과 공통 문법 개선의 우선순위를 확보
- Qwen 자연화 중 의미 선별을 통과한 공개 391문항의 최초 exact 65를 보존하고,
  공통 비교·검색 문법을 단계적으로 개선해 94→153→170, 최신 실행 의미 보조
  strict 242/391을 기록. 두 검색 단계는 각각 구제 59·17건, 퇴행 0건
- 공식 예상 분포 공개 모의평가 30/30, 답변 불가 5/5 안전 처리,
  로컬 Qwen 생성 16/17·검증 fallback 1건
- 같은 30문항의 실제 Docker GET은 공식 형식·60초 30/30, 의미 24/30이며
  공모펀드 공식 실행 잠금 6건을 배포 차이로 확인해 최초 결과로 보존
- 공모펀드만 여는 명시적 v1 배포 정책을 적용한 재평가는 동일 30문항 의미·형식·
  60초 30/30, Qwen 문장 검증 17/17, fallback 0건
- 금융 도메인 개발 QA 40문항 Router 사후 회귀 40/40,
  잘못된 control 실행·오류 0건
- HyperCLOVA X API 없는 provider·Agent 계약 8/8
- Backend service adapter 오류·fallback·비노출 계약 12/12
- 새 Docker 이미지의 Backend 7건·공식 GET 정상/예외 7건 14/14
- 로컬 Qwen·공모펀드 승인 14/14, Qwen 중단 fallback 14/14,
  공식 모의 30문항 동시성 2에서 30/30·fallback 0
- 제출 경계 자동 검사로 개발용 로컬 LLM의 운영 파일 혼입과 제출 후보 잔존을
  서로 다른 프로필로 차단
- 설명회 현장 자료에서 평가 API 다섯 문자열 필드·미응답 HTTP 200·60초 권장과
  도메인별 Ontology 제출 요구 확인

내부 공개 평가의 100%는 배선·회귀 안정성이지 독립 blind 일반화 성능이 아니다.

## 9. 현업 활용성과 리스크 관리

- 답변마다 출처와 기준일 표시
- 공식 데이터와 외부 데이터 충돌 시 공식 데이터 우선
- 확인 불가능한 조건은 추정하지 않고 확인 불가 또는 역질문
- 수익률 전망·수익 보장·단정적 투자 추천 금지
- provider·dataset 장애는 evidence 없는 안전한 오류 DTO로 변환
- 실제 사람 평가는 금융 도메인 담당자와 제품 담당자가 독립 rubric으로 수행

## 10. 완료 전 게이트

- 금융 도메인 담당자의 external blind와 비공개 정답키
- 최소 2명의 독립 reviewer가 수행한 사람 평가
- 허용된 실제 비정형 문서 corpus와 사용 범위
- HyperCLOVA X 모델·endpoint·인증·Structured Outputs 확인과 실제 재현
- FastAPI 공식 `GET /answer` Docker·공개 서버 통합
- Ontology 용어의 금융 도메인 검수와 주최 측 최종 형식 확인
- `think_trace`의 구조화 실행 기록에 대한 세부 평가 방식 확인
- 크레딧 승인과 HyperCLOVA X 적용 서비스 확인
