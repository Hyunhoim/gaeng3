# 금융상품 Agent 현재 프로젝트 기준

상태: 현재 정본
기준일: 2026-07-30
대상 저장소: `https://github.com/Hyunhoim/gaeng3`

## 1. 한 문장 목표

사용자의 자연어 조건을 검증 가능한 QueryPlan으로 바꾸고, 제공 데이터에 대해 결정론적으로 검색·연산·검증한 뒤, 상품별 근거와 기준일을 포함해 답하는 금융상품 Product Finder를 만든다.

## 2. 현재 상태

- 공식 과제 소개자료와 국내채권·국내 ETP·해외 ETP·공모펀드 원천 데이터를 확보했다.
- 원천 145,393행의 1차 감사와 GPT Pro 전략 연구를 마쳤다.
- GPT Pro 산출물은 유용한 연구 근거지만, 그대로 실행 가능한 구현 명세는 아니다.
- 애플리케이션 저장소 로컬 경로는 `3. Workspace/gaeng3`로 정했다.
- 로컬 `haeyeongcho` branch가 `origin/haeyeongcho`를 추적하며 공모펀드 구현·문서
  commit `be2797a`까지 원격에 push했다.
- 동료가 가져올 `vintasoftware/nextjs-fastapi-template`은 루트의 `fastapi_backend`, `nextjs-frontend`, `docs` 구조와 UV를 사용한다. Agent·데이터 작업공간은 `finance_agent/`에 격리하고, 애플리케이션이 재사용할 Python 코드는 `finance_agent/packages/finance_agent_core`에서 개발한다.
- `gaeng3-dev` Conda 환경과 pip requirements를 만들고, 표준 라이브러리 기반 데이터 감사기를 구현했다.
- 4종 145,393행 감사, expectation 65/65, 8개 입력 hash 대조, 두 번의 결정적 재실행을 통과했다.
- 해외 ETP 17개 canonical field registry, 엄격한 서버 QueryPlan, HCX keyword subset schema를 구현하고 계약 테스트를 통과했다.
- 해외 ETP 5,646행을 정규화해 SQLite에 적재하고 sparse 10행을 격리했다.
- parameterized SQLite oracle, 독립 Python verifier, field-level evidence,
  결정론적 safe renderer와 Mock Agent E2E를 구현했다.
- 첫 vertical slice는 검증된 후보 440개와 상위 5개를 재현한다.
- 격리된 Qwen/vLLM provider를 RTX 5090 2장에서 실제 실행해 structured
  QueryPlan부터 evidence 응답까지 연속 E2E와 byte-level 재현성을 확인했다.
- 해외 ETP 핵심 50문항과 기대 QueryPlan·oracle을 동결하고 평가 하네스를
  구현했다. 로컬 Qwen hybrid parser의 최초 미사용 holdout은 9/10이었고,
  오류 수정 후 전체 50문항 회귀는 연속 2회 통과했다.
- 국내 ETP 1,734행을 정규화하고 손상 행 1개를 격리했다. 상품군별 source
  override registry, SQLite oracle, verifier, evidence, Mock E2E를 같은 계약으로
  일반화했다.
- 국내 ETP 50문항은 expected provider 50/50, 로컬 Qwen development 40/40,
  local-inference holdout 첫 실행 10/10을 기록했다. 완전한 blind 평가가
  아니라는 한계를 별도 문서에 명시했다.
- 최소권한 grounded answer 계약과 Answer Verifier를 구현했다. 국내 ETP
  50문항에서 47개 LLM 생성·3개 안전 차단, 수치·순위·evidence·기준일 100%,
  폴백 0건을 기록했다. 이는 자유 생성 점수가 아니라 hybrid 계약 준수율이다.
- 국내채권 42,394행을 정규화하고 254개를 스냅샷 기준 실제 매수 가능으로
  판정했다. stale 동적 값, 0 날짜 sentinel, 재계산 잔존일수, 신용등급·위험코드
  안전 계약을 registry·Oracle·Verifier·evidence에 연결했다.
- 국내채권 QueryPlan과 근거 답변 50문항이 각각 50/50을 통과했다. 실제
  질문→계획→검색→검증→답변 E2E도 통과했고 로컬 Qwen 폴백은 0건이었다.
- 공모펀드 raw 95,619행을 논리 상품 11,138개·속성 95,618개·격리 1개로
  정규화하는 세 테이블 SQLite와 manifest를 구현했다. 독립 2회 빌드의 DB
  SHA-256이 일치했다.
- 공모펀드 공모 범위 잠금, Oracle, Result Verifier, field evidence를 연결했다.
  대표 질의는 후보 1,811개와 상위 5개를 SQL·Python에서 동일하게 재현했다.
  클래스 상위 그룹 의미는 추측하지 않고 경고한다.
- 공모펀드 40 development·10 holdout 평가 계약을 동결했다. 실행 44개와 안전
  차단 6개의 expected QueryPlan·Oracle 회귀는 50/50이며, 공식 Agent 실행은
  HCX schema·서버 계약 승인 전까지 비활성화했다.
- 공모펀드 전용 내부 schema와 lexical/schema linker를 구현했다. 개발 전용
  로컬 Qwen hybrid parser는 development 40문항을 최초 실행에서 40/40
  통과했다. commit `32e12fa` 이후 최초 실행한 holdout은 9/10이며 클래스 합산
  질문 한 건의 의미 해석 실패를 관측값 그대로 보존했다.
- 공개된 실패는 family handoff와 unsupported ranking 제거로 회귀 수정했다.
  로컬 Qwen holdout은 다시 호출하지 않았고 무모델 50문항 replay만 50/50이다.
- 공모펀드 field-level evidence부터 로컬 Qwen 설명, 최종 Answer Verifier,
  결정론적 폴백까지 연결했다. `fund-core-50`의 expected·local provider가
  각각 50/50을 통과했으며, 44개 grounded 생성·6개 안전 차단·폴백 0건이다.
  상품명·수치·순위·evidence·기준일·warning 검증률은 모두 100%다.
- 공모펀드 true COMPARE 계약을 정확한 `itm_no` 두 개, 공모 범위, 요청 순서,
  지원 필드와 서버 계산 규칙으로 제한해 구현했다. `fund-compare-core-20`의
  expected·로컬 Qwen이 각각 20/20을 통과했다. 완전한 비교 18건은 grounded
  생성, 누락 대상 2건은 LLM 미호출 결정론 처리, verifier 폴백은 0건이다.
- COMPARE 답변은 서버가 `두 번째-첫 번째` 차이, AUM 통화 호환성과 결측을
  결정하고 LLM은 evidence 설명만 담당한다.
- 공모펀드 정식명·짧은 이름·`itm_no`의 정확 일치 resolver와 최소권한 자연어
  COMPARE parser를 구현했다. 공개 `fund-compare-parser-core-24`에서
  expected·로컬 Qwen 모두 24/24이며, 실행 16건의 Oracle과 차단 8건의
  fail-closed 정책이 모두 일치했다.
- 같은 공개 24문항을 자연어 parser→resolver→Oracle·Result Verifier→field
  evidence→Qwen grounded answer→Answer Verifier·fallback으로 연결한 통합
  E2E도 expected·로컬 Qwen 모두 24/24를 통과했다. 로컬 Qwen은 parser 24회와
  실행 문항 answer 16회를 호출했고, 실행 16건·안전 차단 8건, grounded
  answer 16건·fallback 0건이다. parser·resolution·계획·Oracle·차단·답변
  핵심 검증률과 동결 field status·numeric delta·실제 비교 셀 값과 별도의
  근거 provenance 정확도는 모두 100%이며 p95 latency는 parser 751.575ms,
  answer 2,225.406ms, 전체 2,737.07ms다. 독립 QueryPlan 계약과 정확한
  상품명 span·전체 대상 순서, identity 사이의 정확한 연결어와 위치별
  문장부호 문법도 함께 회귀 검증한다.
- 자연어 비교 parser 단독 로컬 지연은 p50 569.018ms, p95 796.637ms, 최대
  889.169ms다. 제외·대신·포함 표현, 질문 전체의 미등록 잔여 표현과 비어
  있거나 미종결·역방향·중첩·줄바꿈이 잘못된 따옴표를 fail-closed로 차단한다.
- 이 결과는 공개 회귀 세트의 통합 배선 검증이다. 독립 blind E2E·사람
  rubric·HyperCLOVA X 재현은 아직 완료하지 않았고 공모펀드 공식 Agent
  실행은 계속 비활성화한다.
- 네 상품군·일곱 intent의 공개 내부 진단은 Router 도입 전 replay 4/28,
  fail-closed Router 28/28이다. self-authored diagnostic이므로 blind 점수가 아니다.
- 상품군별 capability matrix와 서버 QueryPlan compiler를 공통
  실행 경로에 연결했다. 상품 검색은 field evidence·Answer Verifier를 사용하고,
  집계는 Decimal 계산·독립 AggregateResultVerifier·AggregateEvidence를 사용한다.
- 네 상품군 same-family COMPARE를 공통 실행 경로에 연결했다. exact resolver,
  registry `comparable` capability, 요청 순서, 통화·기준일·stale·결측 상태,
  `ComparisonEvidence`와 독립 `ComparisonResultVerifier`를 사용한다. 해외·국내
  ETP와 국내채권은 합성 fixture E2E를 통과했고, 기존 공모펀드 공개 회귀는
  그대로 유지했다. 실제 DB 기반 세 상품군 공개 자연어 회귀 30문항도 30/30을
  통과해 기존 공모펀드 24문항과 합친 비교 공개 범위는 54문항이다. 네 상품군
  독립 blind 일반화 평가는 아직 수행하지 않았다. 비교 경로는 전체 레코드를
  보관하지 않고 compact identity 49,774건만 파일 변경 감지형 cache에 둔다.
  3 workers 기준 p50 65.522ms·p95 954.670ms이며 전체 레코드 cache는
  메모리 비용 때문에 제외했다. 이 수치는 개발 장비의 방향성 기준선이다.
- SEARCH·AGGREGATE 기본 verifier는 전체 Pydantic 레코드 대신 QueryPlan의
  조건·정렬·그룹·집계 필드와 품질·기준일만 별도 projection으로 읽는다.
  실제 데이터 8문항 결과 지문은 8/8 일치했고 p50 308.749ms, 최대 추가 RSS
  51,000KiB다. 변경 전 대비 국내채권은 약 92~93%, 공모펀드는 약 94~95%
  메모리 증가량이 감소했다. 단일 장비·단일 실행의 방향성 기준선이다.
- 복수 상품군 SEARCH는 Router가 확인한 각 상품군을 단일-family QueryPlan으로
  분리하고 SQLite Oracle·Result Verifier를 병렬 실행한다. 한 상품군의 0건
  결과가 다른 상품군 결과를 지우지 않으며, 상품군별 plan·후보 수·evidence·
  manifest를 Backend DTO에 별도 보존한다. 국내·해외 ETP 실제 데이터 공개
  회귀는 양쪽 성공·부분 성공·전체 0건·교차 비교 차단 4/4다. 상품군 간 직접
  수치 비교·합산·우열 판단, 서로 다른 상품군별 조건과 모델 호출은 v1에서
  차단한다. 공모펀드 공식 실행 비활성 정책도 유지한다.
- HyperCLOVA X QueryPlan·공모펀드 비교 초안·근거 답변 provider가 공유하는
  semantic structured request와 주입형 transport 계약을 구현했다. 공식
  mode/provider gate, HCX schema subset, token·latency 관측, 인증·rate limit·
  timeout·서비스·응답 오류를 fake transport로 검증했다. 실제 endpoint·인증
  header·HTTP transport와 공식 재현은 아직 외부 게이트다.
- SEARCH는 공통 Router와 서버 기준 QueryPlan을 먼저 통과한 뒤 HCX QueryPlan이
  완전히 일치할 때만 Oracle을 실행하도록 선택 주입했다. API 없는 전체 경로
  8개 시나리오에서 세 실행 상품군 Backend DTO, Answer Verifier fallback,
  timeout, 금지 질의·비활성 공모펀드 무호출, 계획 불일치를 8/8 검증했다.
  AGGREGATE·COMPARE는 서버 결정론적 compiler를 유지한다.
- 프레임워크 독립 `/answer` service adapter를 구현했다. 정상·control·not-found·
  검증된 fallback은 HTTP 200으로 유지하고, QueryPlan provider·dataset·내부
  장애는 evidence 없는 안전한 ERROR DTO와 HTTP 502·503·504·500으로 변환한다.
  질문·credential·provider 본문·파일 경로 비노출을 포함한 12개 계약을 통과했다.
- 네 상품군 각 10문항과 10개 공격 유형으로 구성한 공개
  `internal-red-team-v1`을 Router부터 `/answer` service adapter까지 실행했다.
  최초 로컬 Qwen은 strict 36/40이었지만 네 실패 모두 Oracle 전 안전 차단됐다.
  `3건` limit handoff를 수정한 뒤 strict·safety·evidence 40/40, QueryPlan·
  grounded answer 각 12회, provider 오류·fallback 0건을 기록했다. 독립 blind나
  HyperCLOVA X 품질 점수는 아니다.
- caller-fed BM25/SQLite FTS 문서 검색은 chunk·필터·top-k·출처·기준일·
  provided 우선순위·not-found를 검증했다. 실제 corpus는 승인 전이다.
- 사람 평가 rubric v1과 프레임워크 독립 Backend DTO·JSON 예시를 구현했다.
  실제 사람 평가는 외부 게이트다.
- 전체 코드 회귀는 pytest 320개, Ruff lint·format, pip dependency check와
  wheel 빌드를 통과했다.

## 3. 변경할 수 없는 공식 제약

- 평가 경로에서 사용하는 LLM은 HyperCLOVA X로 제한한다.
- 다른 생성형 LLM 또는 VLM을 평가·제출 경로에 연결하지 않는다.
- 답변은 제공 데이터 또는 허용된 외부 데이터에 근거해야 하며, 사용한 참조 데이터와 기준일을 표시한다.
- 데이터로 확인할 수 없는 조건은 추정하지 않고, 확인 불가 또는 역질문으로 처리한다.
- 수익 보장, 근거 없는 미래 수익률 전망, 단정적인 투자 권유를 생성하지 않는다.
- 공식 `GET /answer` 요청·응답 계약과 주최 측 실행 환경을 최종적으로 준수한다.
- 임베딩 등 비-LLM 영역에는 구현 방식 제한이 없다는 공식 문구가 있지만, 경계 모델의 허용 여부는 설명회에서 재확인한다.

## 4. 모델·도구 사용 정책

| 구성요소 | 개발 단계 | 평가·제출 경로 | 현재 결정 |
| --- | --- | --- | --- |
| HyperCLOVA X | 요청·응답·오류 계약과 fake transport 완료 | 허용·필수 | 실제 HTTP transport 대기 |
| Mock/fixture provider | 기본 테스트와 CI | 실제 답변 생성에 사용하지 않음 | 항상 유지 |
| `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` | 명시적으로 켠 로컬 실험만 | 금지 | 임시 개발 provider |
| 다른 생성형 LLM/VLM | 사용하지 않음 | 금지 | 제외 |
| 전용 OCR | 필요한 문서 입력에 한해 사용 가능 | 비-LLM 도구로 관리 | 비교적 안전 |
| BERT 계열 임베딩·Cross-encoder·NER·분류 | 공식 확인 전 실험 결과를 제품 경로에 채택하지 않음 | 보류 | 보수적 보류 |
| 전용 번역 모델 | 공식 확인 전 사용하지 않음 | 보류 | 더 보수적으로 보류 |
| SQL·lexical 검색·규칙·통계 코드 | 허용 | 허용 | 핵심 실행 계층 |

논문, 오픈소스 알고리즘, 공개 코드는 연구와 구현에 활용할 수 있다. 다만 모델 가중치를 사용하는 구성요소는 이름이 아니라 기능과 평가 경로에서의 역할을 기준으로 허용 여부를 판단한다.

### 로컬 LLM 격리 원칙

- 로컬 LLM은 개발 편의를 위한 비공식 테스트 대역이며 사용자가 그 위험을 인지하고 선택했다.
- 기본 실행과 CI는 외부 모델이 필요 없는 fixture를 사용한다.
- 로컬 provider는 `FINANCE_AGENT_LLM_MODE=local_test`,
  `ENABLE_NON_HCX_TEST_LLM=1`, `LLM_PROVIDER=local_test`를 모두 설정해야만
  활성화한다.
- 로컬 endpoint는 기본적으로 `http://127.0.0.1:18000/v1`에만 노출한다.
- evaluation·production 설정은 provider가 HyperCLOVA X가 아니면 시작 단계에서
  실패한다. service adapter는 이 설정 오류를 안전한 비재시도 ERROR DTO로 변환한다.
  실제 FastAPI route와 네트워크 transport는 아직 없다.
- 로컬 모델의 응답·로그·캐시·가중치는 Git과 제출물에서 제외한다.
- 로컬 모델이 만든 결과는 정답 데이터로 자동 승격하지 않고, 결정론적 oracle 또는 사람 검토를 거친다.
- 애플리케이션 환경과 GPU 추론 환경은 각각 `gaeng3-dev`, `gaeng3-llm-local` Conda 환경으로 분리하고 pip 의존성도 나눈다.

검증된 로컬 추론 환경은 RTX 5090 2장, vLLM tensor parallel 2, 32K context다.
CUDA·PyTorch·vLLM 조합의 smoke test와 실제 structured-output E2E를 통과했으며,
세부 버전·호환성 우회·재현 명령은 `local-llm.md`에 고정했다.

## 5. 역할 분담

### 애플리케이션·플랫폼 담당

- Next.js UI와 FastAPI API shell
- PostgreSQL, Docker Compose, 배포·포트·환경 변수
- 공식 API adapter와 화면 계약
- 공통 CI와 저장소 기반

### AI·데이터·Agent 담당

- 원천 데이터 profiling, 정규화, 품질 규칙
- field registry와 QueryPlan 계약
- lexical/schema linking과 결정론적 검색 도구
- HyperCLOVA X 및 격리된 로컬 테스트 provider
- evidence DTO, 결과 verifier, 답변 후검증
- 평가 질문·oracle·회귀 테스트

### 금융 도메인·발표 담당

- 실제 사용자 시나리오와 금융 표현 검토
- 필터·비교 기준, 위험 문구, 미지원 조건 검수
- 데모 스토리와 기술제안서·발표자료

팀 경계는 HTTP만으로 늦게 연결하지 않는다. 초기에 `AgentRequest`, `AgentResponse`, QueryPlan, evidence, error code 계약과 fixture를 함께 고정한다.

## 6. 목표 아키텍처

주력 구조는 **Evidence-Compiled Hybrid SQL Agent**다.

```text
사용자 질문
→ lexical/schema linker
→ LLM이 Typed QueryPlan 생성
→ 서버의 엄격한 schema·지원 범위 검증
→ parameterized SQL 또는 결정론적 도구 실행
├─ 상품 조회·비교
│  → 독립 result verifier
│  → field-level evidence DTO
│  → 결정론적 safe renderer 또는 HCX 설명 계층
│  → 인용·수치·금융 문구 Answer Verifier
│  → 실패 시 안전한 template 응답
└─ 집계
   → Decimal reducer
   → 독립 AggregateResultVerifier
   → AggregateEvidence DTO
   → 결정론적 aggregate renderer
```

LLM은 주로 언어를 계약으로 변환하고 결과를 설명한다. 다음 작업은 LLM에 맡기지 않는다.

- 수치·날짜·등급·상태 필터
- 정렬, 집계, 순위, 통화·단위 변환
- 상품군 판별의 최종 결정
- 반환 상품의 필수 조건 충족 여부
- evidence에 없는 수치나 사실 생성

### 반드시 분리할 계약

- `queryplan.hcx.schema.json`: HyperCLOVA X Structured Outputs가 지원하는 keyword만 사용
- 서버 Pydantic 모델 또는 엄격한 JSON Schema: `additionalProperties`, 복합 조건, 길이·범위 등 전체 검증
- `field_registry.yaml`: alias, 타입, 단위, enum, 연산자, coverage, sentinel, freshness, 비교 가능 범위
- product evidence DTO: 원천 테이블·키·필드·값·단위·기준일·품질 상태
- aggregate evidence DTO: 함수·그룹·값·유효/제외 개수·통화·기준일·품질 상태

QueryPlan에는 최소한 intent, 상품군, 필수 조건, 완화 전 확인이 필요한 조건, 선호 조건, 정렬, projection, limit, 모호성, 미지원 조건이 있어야 한다. 조건 강도는 `locked`, `ask_before_relaxing`, `preference`로 구분한다.

## 7. 첫 vertical slice

대표 질문:

> 미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.

필수 동작:

1. 해외 ETP에서 ETF만 선택하고 ETN을 제외한다.
2. 미국·채권형·거래 가능 상태를 명시적 필드와 매핑 규칙으로 판단한다.
3. 총보수 `<= 0.20%`를 결정론적으로 적용한다.
4. AUM 내림차순으로 정렬해 상위 5개를 반환한다.
5. 모든 결과를 같은 조건으로 다시 검증한다.
6. 상품명, 티커·식별자, 보수, AUM, 사용 필드, 원천, 필드 기준일을 반환한다.
7. 보수 0인 40개 후보는 의미가 확인될 때까지 `UNKNOWN` 품질로 취급하고 기본 결과에서 제외하거나 별도 경고한다.
8. 결과가 없을 때 조건을 몰래 완화하지 않고, 한 조건씩 바꾼 후보 수와 사용자 확인 요청을 제시한다.

## 8. 구현 순서

### P0 — 재현 가능한 기반

- [x] 동료의 템플릿 분석 결과와 로컬 작업물을 보존해 `gaeng3` 저장소 상태를 확정한다.
- [x] Conda + pip 환경과 secret·원천 데이터 제외 규칙을 만든다.
- [x] 감사 스크립트의 `/mnt/data`와 `(1).xlsx` 하드코딩을 제거한다.
- [x] 표준 Excel parser로 동일한 핵심 통계가 재현되는 manifest와 품질 테스트를 만든다.
- [ ] 동료 템플릿 통합 후 `.env.example`과 application shell 설정을 확정한다.

### P1 — 계약과 적재

- [x] 해외 ETP logical grain과 격리·품질 규칙을 동결한다.
- [x] 해외 ETP `field_registry.yaml`, 서버 QueryPlan, HCX용 축소 schema를 작성한다.
- [x] 원천 key·원천값·기준일을 보존하는 정규화 적재를 구현한다.
- [x] 해외 ETP vertical slice의 결정론적 oracle SQL을 구현한다.
- [x] 국내 ETP logical grain·field capability·손상 행 격리·정규화 적재를 동결한다.
- [x] oracle·verifier·evidence를 해외·국내 ETP·국내채권 상품군 라우팅으로 일반화한다.
- [x] 국내채권 grain·stale·날짜·검색 capability와 정규화 적재를 동결한다.
- [x] 공모펀드 product-grain·field capability·품질 규칙을 동결한다.
- [x] 동결된 공모펀드 계약으로 product·attribute·quarantine 정규화 적재를 구현한다.
- [x] 공모펀드 정규화 DB에 oracle·verifier·field evidence를 연결한다.

### P2 — Agent 수직 통합

- [x] Mock provider로 QueryPlan부터 검증 응답까지 연결한다.
- [x] QueryPlan compiler, verifier, evidence builder, safe renderer를 연결한다.
- [x] 격리된 로컬 Qwen provider의 실제 GPU E2E를 통과한다.
- [x] 국내 ETP 대표 질문의 Mock E2E와 로컬 Qwen batch 회귀를 통과한다.
- [x] 국내채권 대표 질문의 Mock·로컬 Qwen 통합 E2E를 통과한다.
- [x] grounded answer 생성·후검증·결정론적 폴백과 답변 평가를 연결한다.
- [x] 네 상품군 공통 AGGREGATE의 함수·그룹·통화·결측·기준일 계약,
  결정론적 실행·독립 verifier·Backend evidence를 연결한다.
- [x] 네 상품군 공통 COMPARE의 exact identity·필드·통화·기준일 계약과
  공통 ComparisonEvidence·독립 verifier를 연결한다.
- [x] HyperCLOVA X provider의 세 operation, 주입형 transport, 오류·timeout
  계약과 API 없는 fake 테스트를 구현한다.
- [x] 세 실행 상품군 SEARCH를 공통 Router·서버 계획 guard·Oracle·Evidence·
  Answer Verifier·Backend DTO까지 API 없는 E2E로 검증한다.
- [x] 복수 상품군 SEARCH를 상품군별 단일 계획·병렬 Oracle·독립 verifier로
  실행하고 부분 결과·manifest·Backend family DTO와 직접 비교 차단을 검증한다.
- [x] 프레임워크 독립 `/answer` service adapter에 HTTP status·ERROR DTO·
  fallback·민감정보 비노출 계약을 연결한다.
- [ ] 공식 endpoint·인증 계약에 맞는 HTTP transport와 FastAPI `/answer`
  route를 연결하고 실제 API로 재현한다.

### P3 — 평가 확장

- [x] 먼저 50개 핵심 질문으로 parser·검색·검증을 회귀 테스트한다.
- [x] 국내 ETP에도 40 development/10 local-inference split을 추가한다.
- [x] 국내 ETP 50문항에서 최종 답변의 수치·순위·evidence·기준일을 평가한다.
- [x] 국내채권 50문항에서 QueryPlan·oracle·안전 차단과 근거 답변을 평가한다.
- [x] 공모펀드 50문항의 expected QueryPlan·oracle·안전 차단 계약을 동결한다.
- [x] 공모펀드 parser·lexical linker를 development 40문항에서 평가한다.
- [x] parser 규칙을 commit한 뒤 공모펀드 holdout을 최초 1회 평가한다.
- [x] 공개된 holdout 실패를 family handoff 회귀 테스트로 수정한다.
- [x] 독립 100문항 blind 세트의 분포·봉인·최초 실행 프로토콜을 구현한다.
- [x] 공모펀드 grounded answer를 `fund-core-50`에서 평가한다.
- [x] 공모펀드 true COMPARE의 선택·계산·근거·검증·폴백을 20문항에서 평가한다.
- [x] 자연어 상품명·짧은 이름·상품번호를 정확한 COMPARE 대상으로 연결하는
  parser·entity resolution을 공개 24문항에서 평가한다.
- [x] 공개 24문항에서 자연어 COMPARE parser부터 Answer Verifier·fallback까지
  통합 E2E를 평가한다.
- [x] 해외·국내 ETP·국내채권 자연어 COMPARE 30문항의 실제 DB·Backend
  통합 회귀를 동결하고 기존 공모펀드 24문항과 함께 관리한다.
- [x] 네 상품군·일곱 intent 내부 diagnostic, fail-closed Router와 capability
  matrix를 구현하고 도입 전·후 결과를 분리 보존한다.
- [x] 서버 MinimalQueryDraft→QueryPlan compiler와 공통 답변 경로를 구현한다.
- [x] BM25/SQLite FTS 문서 검색 최소 기능과 synthetic contract test를 구현한다.
- [x] 사람 평가 rubric·집계 validator와 Backend DTO·JSON 예시를 확정한다.
- [ ] 금융 도메인 담당자가 새 blind 100문항과 비공개 정답키를 독립 작성한다.
- [ ] 독립 blind 질문으로 자연어 COMPARE 전체 E2E 일반화 성능을 평가한다.
- [ ] 완성된 rubric으로 명확성·근거·안전·비교 용이성과 deterministic 대비
  선호를 실제 팀원이 측정한다.
- [ ] 다른 작성자가 만든 blind 표현 변형·경계값 중심 v1.1 세트를 최소
  100개로 새로 만들고 최초 holdout 성능을 측정한다.
- 이후 250~400개의 사람 검토·oracle 생성 평가 세트로 확장한다.
- intent, 상품군, 연산자, hard-constraint violation, evidence 정확성, unsupported 처리, latency를 분리 측정한다.
- 다른 상품군은 데이터 신뢰도와 예상 평가 비중에 따라 순차 확장한다.

## 9. 2026-08-06 설명회에서 확인할 항목

- 허용되는 HyperCLOVA X 정확한 모델명·버전과 Structured Outputs 지원 범위
- 공식 endpoint·인증 header·요청·응답 body와 request ID 규칙
- API 인증, timeout, QPS, 재시도, 입력 길이, 응답 필수 필드
- 다른 모델로 만든 개발용 synthetic 질문·응답 또는 평가 데이터의 제출 가능 여부
- 임베딩, re-ranker, NER, 번역 모델이 공식 정의상 LLM에 포함되는지
- 보수 0, 수익률 0, 판매·거래 가능 상태의 정확한 의미와 코드북
- 펀드 속성 코드와 상품 grain, 손상 행 처리 기준
- 평가 질의 분포, 정확도·응답시간·설명 품질의 배점
- 네트워크·GPU·Docker·DB·외부 데이터의 평가 환경 제약

공식 답변을 받으면 이 문서와 `data-audit.md`, 활성 구현 명세를 함께 갱신한다.

## 10. 현재 완료 판단

다음 단계의 성공 기준은 “모델이 그럴듯하게 답함”이 아니다.

- 동일 입력이 동일한 QueryPlan과 결정론적 검색 결과를 낸다.
- 필수 조건 위반 상품이 0건이다.
- 답변의 숫자와 사실이 evidence에 모두 존재한다.
- 데이터가 없거나 의미가 불명확한 조건을 명시적으로 보류한다.
- 로컬 LLM을 끈 상태에서도 CI와 핵심 Agent 테스트가 통과한다.
- 평가 모드는 HyperCLOVA X 외 provider를 fail-closed로 차단한다.
- 이미 튜닝에 사용한 50문항의 100% 회귀와 새 미사용 질문에 대한 일반화
  성능을 구분해 보고한다.
