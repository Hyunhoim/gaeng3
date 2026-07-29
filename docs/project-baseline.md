# 금융상품 Agent 현재 프로젝트 기준

상태: 현재 정본
기준일: 2026-07-29
대상 저장소: `https://github.com/Hyunhoim/gaeng3`

## 1. 한 문장 목표

사용자의 자연어 조건을 검증 가능한 QueryPlan으로 바꾸고, 제공 데이터에 대해 결정론적으로 검색·연산·검증한 뒤, 상품별 근거와 기준일을 포함해 답하는 금융상품 Product Finder를 만든다.

## 2. 현재 상태

- 공식 과제 소개자료와 국내채권·국내 ETP·해외 ETP·공모펀드 원천 데이터를 확보했다.
- 원천 145,393행의 1차 감사와 GPT Pro 전략 연구를 마쳤다.
- GPT Pro 산출물은 유용한 연구 근거지만, 그대로 실행 가능한 구현 명세는 아니다.
- 애플리케이션 저장소 로컬 경로는 `3. Workspace/gaeng3`로 정했다.
- 로컬 `haeyeongcho` branch가 `origin/haeyeongcho`를 추적한다. 2026-07-28 확인 시 원격 `main`·`hyunhoim`·`haeyeongcho`는 모두 초기 README 커밋 `f366414`를 가리킨다.
- 검증된 Agent Core 기반은 2026-07-29 로컬 commit `a65c2b2`로 보존했으며
  원격에는 아직 push하지 않았다.
- 동료가 가져올 `vintasoftware/nextjs-fastapi-template`은 `fastapi_backend`, `nextjs-frontend`, `docs` 구조와 UV를 사용한다. 동료의 템플릿 적응 작업이 원격에 반영되기 전까지 Agent·데이터 코드는 충돌 가능성이 낮은 `packages/finance_agent_core`에서 개발한다.
- `gaeng3-dev` Conda 환경과 pip requirements를 만들고, 표준 라이브러리 기반 데이터 감사기를 구현했다.
- 4종 145,393행 감사, expectation 49/49, 8개 입력 hash 대조, 두 번의 결정적 재실행을 통과했다.
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
- 전체 코드 회귀는 pytest 64개, Ruff lint·format, pip dependency check와
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
| HyperCLOVA X | API 확보 후 통합 | 허용·필수 | 최종 provider |
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
- 평가·production adapter를 연결할 때는 provider가 HyperCLOVA X가 아니면 시작
  단계에서 실패하도록 구현한다. 현재는 해당 adapter 자체가 아직 없다.
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
→ 독립 result verifier
→ field-level evidence DTO 생성
→ 검증된 evidence로 결정론적 safe renderer 실행
→ 인용·수치·금융 문구 후검증
→ 선택적으로 HCX 설명 계층을 붙이고 실패 시 안전한 template 응답
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
- evidence DTO: 원천 테이블·키·필드·값·단위·기준일·품질 상태

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
- [ ] 공모펀드 product-grain 계약을 동결한다.

### P2 — Agent 수직 통합

- [x] Mock provider로 QueryPlan부터 검증 응답까지 연결한다.
- [x] QueryPlan compiler, verifier, evidence builder, safe renderer를 연결한다.
- [x] 격리된 로컬 Qwen provider의 실제 GPU E2E를 통과한다.
- [x] 국내 ETP 대표 질문의 Mock E2E와 로컬 Qwen batch 회귀를 통과한다.
- [x] 국내채권 대표 질문의 Mock·로컬 Qwen 통합 E2E를 통과한다.
- [x] grounded answer 생성·후검증·결정론적 폴백과 답변 평가를 연결한다.
- [ ] HyperCLOVA X provider와 공식 `/answer` adapter·오류·timeout 계약을 연결한다.

### P3 — 평가 확장

- [x] 먼저 50개 핵심 질문으로 parser·검색·검증을 회귀 테스트한다.
- [x] 국내 ETP에도 40 development/10 local-inference split을 추가한다.
- [x] 국내 ETP 50문항에서 최종 답변의 수치·순위·evidence·기준일을 평가한다.
- [x] 국내채권 50문항에서 QueryPlan·oracle·안전 차단과 근거 답변을 평가한다.
- [ ] 사람 rubric으로 명확성·중복·비교 용이성과 deterministic 대비 선호를 측정한다.
- [ ] 다른 작성자가 만든 blind 표현 변형·경계값 중심 v1.1 세트를 최소
  100개로 새로 만들고 최초 holdout 성능을 측정한다.
- 이후 250~400개의 사람 검토·oracle 생성 평가 세트로 확장한다.
- intent, 상품군, 연산자, hard-constraint violation, evidence 정확성, unsupported 처리, latency를 분리 측정한다.
- 다른 상품군은 데이터 신뢰도와 예상 평가 비중에 따라 순차 확장한다.

## 9. 2026-08-06 설명회에서 확인할 항목

- 허용되는 HyperCLOVA X 정확한 모델명·버전과 Structured Outputs 지원 범위
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
