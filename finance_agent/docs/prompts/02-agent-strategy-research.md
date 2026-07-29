# ChatGPT Pro 요청 프롬프트 — 금융상품 Agent 우승 전략 및 구현 계획

당신은 금융 AI, 정보검색(IR), 데이터베이스, 자연어처리(NLP), Agent 시스템, 신뢰성 평가에 모두 경험이 있는 수석 연구책임자이자 실전 시스템 아키텍트다.

내 목표는 제10회 미래에셋증권 AI Festival의 **금융 Agent - Product Finder: 채권, ETF, 데이터 기반** 과제에서 단순 데모가 아니라 비공개 평가 질의에 강하고, 근거가 명확하며, 제한된 기간 안에 완성 가능한 시스템을 만들어 우승 가능성을 최대화하는 것이다.

일반론이나 기술 목록을 나열하지 말고, 이 프로젝트 Sources에 업로드된 공식 PDF와 8개 Excel 파일을 직접 검사하고 최신 연구·오픈소스를 조사한 뒤 **하나의 주력 구현안**을 선택해 달라.

답변은 한국어로 작성하라.

---

## 1. 반드시 먼저 읽고 검증할 Sources

프로젝트 Sources에 다음 자료가 업로드되어 있다.

- 공식 과제 소개 PDF: `(배표용)과제소개자료_금융상품Agent.pdf`
- `PRBD01N001_국내채권마스터_20260711_datarows.xlsx`
- `PRBD01N001_국내채권마스터_schema.xlsx`
- `PREF01N001_국내ETF마스터_20260711_datarows.xlsx`
- `PREF01N001_국내ETF마스터_schema.xlsx`
- `PREF02N001_해외ETF마스터_20260711_datarows.xlsx`
- `PREF02N001_해외ETF마스터_schema.xlsx`
- `PRFD01N001_공모펀드마스터_20260711_datarows.xlsx`
- `PRFD01N001_공모펀드마스터_schema.xlsx`

파일명 끝에 `(1)`이 붙어 있을 수 있으나 같은 자료다.

먼저 PDF에서 과제 요구사항, 제출물, API 예시, 금지사항, 허용사항, 일정, 평가 관련 확정 사실을 추출하라. 그다음 Python 기반 데이터 분석으로 각 Excel의 다음 항목을 독립적으로 검증하라.

- sheet와 실제 dimension
- 행·열 수
- header와 schema 일치 여부
- 후보 primary/composite key와 중복
- 결측·공백·`NULL`·0 sentinel
- 수치·날짜 parsing 가능성
- 상품군별 주요 category
- 판매·거래 가능 상태
- 필드별 최신 기준일
- 검색 조건으로 안전하게 쓸 수 있는 필드
- 값은 존재하지만 의미상 무효한 필드
- 손상되거나 column shift가 의심되는 레코드
- 논리 상품 grain과 원천 row grain의 차이

Source를 실제로 읽지 못한 경우 읽었다고 가장하지 말고, 어떤 파일과 항목을 확인하지 못했는지 먼저 명시하라.

---

## 2. 확정된 공식 제약

공식 자료에는 다음이 안내되어 있다.

- 제출 시스템에서 사용하는 LLM은 **HyperCLOVA X만 허용**된다.
- HyperCLOVA X 외 다른 LLM을 사용하면 평가 대상에서 제외된다.
- 그 밖의 구현 범위와 방식에는 제한이 없다.
- 데이터 적재, 전처리, 구조화, 색인, 검색 방식은 자유다.
- RDB, GraphDB, Text-to-SQL, Vector DB, RAG, Re-ranker, Agent Framework 등을 선택할 수 있다.
- 외부 금융상품 데이터도 사용할 수 있지만 평가 기준 데이터는 주최 측 제공 데이터다.
- 외부 데이터와 제공 데이터가 충돌하면 제공 데이터를 우선한다.
- 답변은 데이터 근거와 기준일을 표시해야 한다.
- 데이터로 확인할 수 없는 질문은 확인 불가를 명시하거나 필요한 조건을 역질문해야 한다.
- 근거 없는 수익률 전망과 단정적 투자 추천은 금지된다.
- 참고 질의 세트는 2026-08-06 오프라인 설명회에서 추가 공개될 예정이다.

이 제약을 다음 세 범주로 구분해 판단하라.

### A. 명백히 사용 가능한 것

예:

- 논문에 공개된 알고리즘과 시스템 설계
- SQL, BM25, inverted index, rule engine, constraint solver
- PostgreSQL, DuckDB, OpenSearch, Neo4j, Qdrant 등의 데이터·검색 소프트웨어
- LangGraph, Haystack, LlamaIndex 등 Agent/RAG 프레임워크 자체
- 직접 구현한 deterministic parser, validator, rule-based/symbolic re-ranker, query planner

### B. 허용 여부를 주최 측에 확인해야 하는 것

예:

- SBERT·E5·BGE 등 encoder-only pretrained embedding model
- BERT·RoBERTa 계열 cross-encoder re-ranker
- BERT·KoBERT 계열 NER·분류용 transformer
- Marian·NLLB 등 전용 encoder-decoder 번역 모델
- Tesseract·PaddleOCR 등 생성형 LLM·VLM을 포함하지 않는 전용 OCR 엔진 또는 OCR 모델
- 외부 API 기반 비생성형 모델

이들은 범용 생성형 LLM과는 구분되지만, 주최 측이 넓은 의미의 언어모델로 해석할 수 있으므로 허용된다고 단정하지 말고 **설명회 질문 목록**에 포함하라. 특히 번역 모델은 텍스트를 생성하므로 embedding·re-ranker·NER보다 더 보수적으로 판단하라.

### C. 평가 경로에서 제외할 것

- HyperCLOVA X 이외의 생성형 LLM
- HyperCLOVA X 이외의 멀티모달 LLM/VLM
- open-source LLM을 fallback, parser, judge, generator로 사용하는 방식
- 평가 답변 생성에 다른 LLM을 간접적으로 사용하는 방식
- 다른 생성형 LLM/VLM이 만든 synthetic QA, label, summary, query expansion, distillation 결과 또는 cache를 제출 시스템의 평가 동작에 반영하는 방식

### 설명회 확인 전 현재 팀 운영 정책

- **사용 가능 후보:** SQL, BM25, 규칙·사전·전통 ML, deterministic verifier. 전용 OCR은 비교적 안전한 후보로 검토하되 실제 모델과 사용 위치를 제출 전 확인한다.
- **기본 비활성화·보류:** BERT 계열 embedding, cross-encoder re-ranker, NER·분류 모델. 인터페이스와 실험 계획만 준비하고 주최 측의 서면 확인 전에는 제출 시스템과 평가 경로에 연결하지 않는다.
- **더 보수적으로 보류:** 전용 번역 모델. 공식 데이터 처리에 꼭 필요한지 먼저 입증하고, 주최 측의 명시적 허용 전에는 사용하지 않는다.
- **사용 금지:** HyperCLOVA X 이외의 생성형 LLM/VLM과 그 출력물이 제출 시스템의 평가 동작에 영향을 주는 방식
- 외부 논문·오픈소스를 조사하거나 개발 의사결정에 참고하는 행위와, 해당 모델을 제출 시스템에 탑재하는 행위를 구분한다.
- 보류 기술은 feature flag 또는 provider interface 뒤에 격리하고, 기본 설정은 `disabled`로 둔다.

오픈소스 모델이나 연구 방법을 추천할 때마다 A/B/C와 현재 운영 상태인 `사용 가능 후보/보류/사용 금지`를 함께 표시하라.

---

## 3. 현재 팀과 분업

팀은 세 역할로 나뉜다.

### Application·Platform Owner

동료 개발자가 다음을 담당한다.

- `vintasoftware/nextjs-fastapi-template` 분석 및 프로젝트 적용
- Next.js
- FastAPI HTTP/API shell
- PostgreSQL
- Docker Compose
- OpenAPI 기반 Frontend client
- 네이버 클라우드 배포
- health check와 운영 기반

### AI·Data·Agent Owner

내가 집중할 영역이다.

- 제공 데이터 profiling과 품질 계약
- loader, validator, normalizer
- 검색·필터·정렬·집계 Tool
- QueryPlan
- Agent orchestration
- HyperCLOVA X provider
- 근거와 기준일
- hallucination 방지
- QA와 정량 평가

### Domain·QA·Presentation Owner

경영학과 팀원이 담당한다.

- 사용자 시나리오
- 금융 질문과 정답 검수
- 위험 표현 검토
- 시연 스토리와 기술제안서

이번 답변에서는 Frontend나 인증 시스템을 설계하는 데 분량을 낭비하지 말고, **AI·Data·Agent 트랙의 승리 전략과 Application 트랙과의 계약**에 집중하라.

개발 환경은 다음으로 통일할 예정이다.

- 로컬 Python 환경: Conda
- Python package: pip + pinned requirements
- 컨테이너: 동일한 pip requirements
- 평가 LLM: HyperCLOVA X
- 테스트 LLM: deterministic Mock

---

## 4. 사전 분석에서 발견한 데이터 사실

아래는 예비 profiling 결과다. 반드시 Sources를 직접 분석해 재검증하고, 틀리면 정정하라.

### 전체

- 원천 datarows 합계 약 145,393행

### 국내채권

- 42,394행
- `PD_NO`는 유일한 것으로 보임
- 만기·표면금리는 거의 완전
- `BUY_YIELD`, `BUYABLE_QUANTITY`는 881행, 약 2.1%만 존재
- `BUYABLE_QUANTITY > 0`은 약 325행
- 신용등급 관련 필드는 약 58%만 존재

### 국내 ETP

- 1,734행
- ETF 1,202행, ETN 532행
- 자산군·지역·위험등급은 비교적 완전
- 총보수는 217행, 약 12.5%
- 기초지수는 58행, 약 3.3%
- 분배주기는 전부 비어 있는 것으로 보임
- 배당수익률과 추적오차는 값이 있어도 모두 0에 가까워 검색 근거로 부적합할 가능성이 큼

### 해외 ETP

- 5,646행
- ETF 5,587행, ETN 59행
- 총보수·운용사·전략·자산군·지역·AUM은 비교적 완전
- 제공된 1일 수익률 값은 전부 0인 것으로 보임

### 공모펀드

- 95,619행
- `itm_no` 기준 논리 상품은 약 11,139개
- 동일 상품이 `prfd_attr_cd`별로 4~16회 반복되는 것으로 보임
- `itm_no + prfd_attr_cd`는 유일한 것으로 보임
- AUM 약 87%, 위험등급 약 81%, 기간별 수익률 약 53~73% 존재
- 보수 필드는 없음
- 원천 Excel 84,563행 부근에 column shift 형태의 손상 레코드가 의심됨

### 기준일

- 파일 추출일은 2026-07-11
- 국내 ETP 주요 일간 데이터는 2026-06-15
- 해외 ETP 주요 데이터는 주로 2026-06-14~16
- 채권 `PD_STD_INFO_UPDATE`는 최대 2026-02-24로 보임

따라서 “파일 기준일”과 “실제 사용 필드 기준일”을 구분해야 할 가능성이 높다.

---

## 5. 외부 연구와 오픈소스 조사 원칙

웹 검색과 가능한 경우 심층 리서치를 사용하라. 2026-07-28 현재의 자료를 기준으로 하되 다음 원칙을 지켜라.

### 논문

- 동료평가된 1차 자료를 우선한다.
- ACL, EMNLP, NAACL, SIGIR, KDD, WWW, NeurIPS, ICML, ICLR, VLDB, SIGMOD, ICDE, AAAI, IJCAI와 주요 저널을 우선 검토한다.
- 2022~2026 연구를 우선하되 중요한 기반 연구는 이전 논문도 포함한다.
- 논문 제목, 저자, venue, 연도, DOI 또는 공식 proceedings/arXiv URL을 제공한다.
- 실제 존재를 확인하지 않은 논문·수치·벤치마크를 만들지 않는다.
- 논문의 아이디어를 이 데이터와 평가 상황에 어떻게 변환할지 설명한다.

### 오픈소스

- 공식 GitHub 또는 공식 문서를 우선한다.
- repository URL, license, 최근 유지보수 상태, 주요 dependency, CPU/GPU 요구사항을 확인한다.
- Apache-2.0, MIT, BSD 계열을 우선한다.
- GPL·AGPL·비상업·연구전용 license는 제출·배포 위험을 표시한다.
- 스타 수만으로 선택하지 말고 데이터 적합성, 지연시간, 재현성, 통합 비용으로 판단한다.
- 오픈소스 “모델”은 공식 LLM 제한과 충돌하는지 A/B/C로 분류하고, 설명회 전 현재 운영 상태도 함께 표시한다.

### 조사할 연구 주제

- schema linking과 자연어 조건 구조화
- Text-to-SQL과 constrained decoding
- table QA와 hybrid structured retrieval
- sparse lexical retrieval와 semantic retrieval 결합
- learned/symbolic re-ranking
- query routing과 tool selection
- constraint satisfaction 및 최소 조건 완화
- zero-result explanation
- uncertainty, abstention, selective prediction
- evidence-grounded generation과 citation correctness
- Agent tool-use evaluation
- RAG 평가와 hallucination detection
- heterogeneous table integration
- cross-domain 또는 cross-product comparison
- deterministic verification과 self-checking을 LLM 없이 구현하는 방법

---

## 6. 반드시 비교할 후보 아키텍처

최소한 다음을 비교하되, 최종적으로는 하나를 주력안으로 선택하라.

### Baseline A — Schema-first deterministic SQL Agent

- HyperCLOVA X가 Typed QueryPlan 생성
- validator가 field·operator·unit·enum 검증
- SQL이 필터·정렬·집계
- evidence validator가 결과 재검증
- 데이터 부족 시 abstain 또는 clarification

### Baseline B — Hybrid retrieval + deterministic execution

- lexical/schema retrieval로 관련 table·field 후보 탐색
- 허용될 경우 semantic retrieval 또는 re-ranker 보조
- 숫자·날짜·등급·상태는 SQL로만 실행
- 답변은 evidence에 기반

### Baseline C — General Agent framework / Text-to-SQL 중심

- Agent가 table·tool을 선택
- SQL 또는 tool call 생성
- execution feedback와 repair
- guardrail과 timeout 적용

다음 대안도 데이터 적합성이 있을 때만 검토하라.

- GraphDB
- Vector DB 중심 RAG
- PostgreSQL + pgvector
- DuckDB/Parquet serving
- OpenSearch
- precomputed semantic layer
- domain-specific re-ranker

기술적으로 화려하다는 이유만으로 GraphDB·Vector DB·멀티 Agent를 선택하지 말라. 현재 데이터와 예상 평가질의에서 **측정 가능한 이득**이 있는지를 증명하라.

---

## 7. 핵심 설계 질문

다음 질문에 명확히 답하라.

1. 자연어 질문을 어떤 Typed QueryPlan으로 표현할 것인가?
2. 상품군별 서로 다른 schema를 어떻게 공통 projection과 전용 필드로 나눌 것인가?
3. 펀드의 반복 row를 어떻게 정규화할 것인가?
4. ETF와 ETN을 어떻게 구분하고 사용자 표현과 매핑할 것인가?
5. field availability를 Agent가 어떻게 인지할 것인가?
6. 0건 결과에서 어떤 조건이 병목인지 어떻게 계산할 것인가?
7. “조건 하나를 가장 적게 바꾼 대안”을 어떤 알고리즘으로 찾을 것인가?
8. 매수 가능, 판매 가능, 거래 정지, 결측을 어떤 규칙으로 처리할 것인가?
9. 숫자 단위, 퍼센트, 날짜, 통화, 위험등급을 어떻게 정규화할 것인가?
10. cross-product 비교에서 공통 지표가 없을 때 어떻게 abstain할 것인가?
11. field별 기준일과 provenance를 어떻게 응답에 보존할 것인가?
12. HyperCLOVA X 출력이 schema를 벗어나거나 tool call이 실패할 때 어떻게 복구할 것인가?
13. LLM이 만든 QueryPlan과 SQL 결과를 독립 코드로 어떻게 검증할 것인가?
14. 공식 `think_trace` 필드에는 숨은 사고과정 대신 어떤 감사 가능한 execution trace를 넣을 것인가?
15. 비공개 질의에서 distribution shift에 강한 query taxonomy와 테스트 전략은 무엇인가?

---

## 8. 아이디어 평가 기준

각 후보 기술과 아키텍처를 다음 가중치로 평가하라. 필요하면 가중치를 조정하되 이유를 설명한다.

| 기준 | 기본 가중치 |
| --- | ---: |
| 비공개 질의 정답률과 조건 충족 정확도 | 25 |
| 근거성·환각 방지·abstention | 20 |
| 실제 데이터 필드와의 적합성 | 20 |
| 6주 내 구현·검증 가능성 | 15 |
| latency·안정성·운영 복잡도 | 10 |
| 차별성과 시연 전달력 | 5 |
| 공식 규칙·license 위험 | 5 |

주력안은 “가장 연구적으로 화려한 것”이 아니라 이 점수와 실패 위험을 기준으로 선택하라.

---

## 9. 요구 산출물

답변을 다음 순서로 작성하라.

### 1) Executive verdict

- 한 문장으로 주력 전략
- 왜 이 전략이 이 데이터와 평가 방식에서 가장 강한지
- 버려야 할 유혹적인 접근 3개

### 2) 공식 규칙 해석표

- 확정 허용
- 설명회 확인 필요
- 평가 경로 금지
- 설명회 전 팀 운영 상태
- 각 판단의 공식 근거

### 3) 재현 가능한 데이터 감사 결과

상품군별로 다음 표를 제공한다.

- 원천 grain
- 논리 상품 grain
- key
- 행 수
- 검색에 안전한 필드
- 부분 지원 필드
- 사용 금지 또는 무효 필드
- 결측·중복·손상 위험
- 기준일

예비 profiling과 다른 결과가 있으면 차이를 설명한다.

### 4) 지원 질의 taxonomy

다음 수준으로 나눈다.

- 완전 지원
- 조건부 지원
- clarification 필요
- 데이터 부족으로 abstain
- 금지된 투자 전망·단정적 추천

각 수준에 실제 데이터 필드를 이용한 예시 질문을 최소 5개씩 제시한다.

### 5) 최신 논문·오픈소스 evidence table

각 항목에 다음을 포함한다.

- 방법 또는 프로젝트
- 논문·공식 repository
- venue·연도
- license
- A/B/C 허용성
- 현재 운영 상태: 사용 가능 후보/보류/사용 금지
- 우리 시스템에서의 역할
- 예상 이득
- 구현 비용
- latency·hardware
- 실패 위험
- 채택/실험/보류/제외 결정

논문과 오픈소스 목록을 길게 나열한 뒤 끝내지 말고, 최종적으로 실제 실험할 3~5개만 선택한다.

### 6) 후보 아키텍처 비교와 최종 선택

- Baseline A/B/C 비교표
- 점수와 근거
- 주력안 1개
- 규칙 해석이 달라질 때의 fallback 1개
- Mermaid 또는 ASCII 시스템 구조도

### 7) 세부 Agent 설계

- Typed QueryPlan JSON schema 초안
- tool 목록과 입력·출력
- router
- deterministic executor
- verifier
- abstention/clarification
- minimal relaxation
- evidence/provenance
- HyperCLOVA X prompt와 structured output 전략
- 실패 복구와 timeout

완전한 구현 코드를 길게 쓰지 말고 interface, pseudocode, 핵심 알고리즘을 제시한다.

### 8) 검색·조건 완화 알고리즘

특히 다음을 구체화한다.

- exact filter
- ranking
- 0-result diagnosis
- single-constraint relaxation
- 여러 변경 후보의 비용 함수
- 사용자 필수 조건과 선호 조건 구분
- 결측을 조건 불충족과 구분하는 방식
- 결과를 재실행해 검증하는 방식

### 9) 평가 계획

최소 다음 metric을 정의한다.

- intent/product-family accuracy
- slot/constraint extraction exact match 또는 F1
- executable QueryPlan rate
- filter result precision
- top-k relevance
- evidence coverage
- citation/provenance correctness
- unsupported-condition detection
- abstention precision/recall
- zero-result diagnosis accuracy
- relaxation correctness
- answer factuality
- latency p50/p95
- HyperCLOVA X call 수와 비용

정답 생성 방법, golden QA 형식, train/dev/test 분리, ablation 계획도 제시한다.

### 10) Ablation과 실험 우선순위

최소 다음을 비교한다.

- SQL only
- HyperCLOVA X parser + SQL
- hybrid retrieval + SQL
- re-ranker 유무
- verifier 유무
- 조건 완화 유무
- framework 사용과 custom orchestration

각 실험의 성공 기준과 중단 기준을 숫자로 제안한다.

### 11) 6주 실행 로드맵

주 단위로 다음을 포함한다.

- 산출물
- owner
- dependency
- acceptance criteria
- 실험
- 통합 시점
- kill/pivot decision

2026-08-06 참고 질의 공개 전과 후의 계획을 분리한다.

### 12) 첫 72시간 실행 백로그

지금 바로 구현할 작업을 순서대로 10개 이내로 제시한다.

각 작업에 다음을 포함한다.

- 목적
- 수정·생성할 module
- 입력과 출력
- 완료 기준
- 예상 시간
- 선행 조건

첫 vertical slice는 실제 데이터가 풍부한 상품군과 질의를 근거로 선택한다.

### 13) Application 팀과 먼저 고정할 계약

- `AgentRequest`
- `AgentResponse`
- `ProductSummary`
- `Evidence`
- `QueryPlan`
- 오류 상태
- timeout
- Mock fixture
- 공식 `/answer` adapter

JSON 예시를 제공하되 개인화 투자 적합성이나 데이터에 없는 필드를 임의 추가하지 않는다.

### 14) 위험 등록부

최소 다음 위험을 포함한다.

- 참고 질의 공개 후 방향 불일치
- 모델 사용 규칙 오해
- 데이터 결측·중복
- stale field
- HyperCLOVA X structured output 실패
- latency
- framework 과설계
- license
- 외부 데이터 충돌
- 평가 API 변경

각 위험에 확률, 영향, 탐지 신호, 완화책, owner를 지정한다.

### 15) 8월 6일 설명회 질문

규칙·API·평가·모델·embedding·re-ranker·NER·분류·전용 OCR·전용 번역·외부 데이터·latency·배포에 관한 질문을 우선순위 순으로 작성한다. 모델 관련 질문에는 가능하면 실제 후보 모델명, 사용 목적, 온라인/오프라인 사용 여부를 적어 주최 측이 포괄적 답변 대신 명시적으로 허용 또는 금지할 수 있게 한다.

### 16) 최종 의사결정

마지막에는 반드시 다음 형식으로 끝낸다.

```text
주력 아키텍처:
핵심 차별점:
첫 vertical slice:
이번 주 실험 3개:
지금 제외할 기술:
8월 6일 확인 전 보류할 기술:
Application 팀에 오늘 전달할 계약:
72시간 뒤 Go/No-Go 기준:
```

---

## 10. 답변 품질 규칙

- “상황에 따라 다르다”로 끝내지 말고 하나의 주력안을 선택한다.
- 실제 Sources의 표명·컬럼명·수치로 판단한다.
- 확인한 사실, 계산한 결과, 추론, 제안을 명확히 구분한다.
- 외부 자료에는 가까운 위치에 실제 링크와 citation을 단다.
- 존재하지 않는 논문, repository, benchmark를 만들지 않는다.
- 평가 기준이 공개되지 않은 부분은 가정이라고 표시한다.
- 다른 LLM 사용을 우회적으로 권하지 않는다.
- 데이터에 없는 기능을 있는 것처럼 설계하지 않는다.
- 연구 아이디어마다 “어떤 metric이 얼마나 좋아져야 채택할지”를 제안한다.
- 구현 기간과 팀 규모를 무시한 과설계를 피한다.
- 우승을 보장한다고 말하지 말고 우승 확률을 높이는 검증 가능한 전략을 제안한다.
