# Dense Schema Linker 오프라인 컴포넌트 평가 보고서

측정일: 2026-08-11
상태: 구현 완료·production OFF·AI Core 담당자 검토 필요

후속 업데이트: 2026-08-12 실제 임베딩 7개를 CPU에서 비교해 BGE-M3와 Lexical
우선 결합을 독립 blind 후보로 선정했다. 이 문서의 fake Dense 결과는 최초 계약
기준선으로 보존하며, 실제 모델 결과는
[Schema Dense CPU 임베딩 모델 비교](evaluation-schema-embedding-cpu.md)를 따른다.

## 1. 최종 판단

- 현재 승인 DB를 사용한 SQL 검색·집계 공개 회귀 8문항은 **8/8 정확
  일치**했다. SEARCH 상위 상품 ID는 4/4, AGGREGATE의 field·function·value는
  4/4, 후보 수는 8/8 일치했다.
- BM25(단어 일치 기반 문서 검색)는 합성 positive 검색 4/4와 negative control
  2/2를 통과했다. 승인된 실제 금융 문서 corpus(검색 대상 문서 모음)와 relevance
  gold(어떤 문서가 정답인지 정한 기준)가 없으므로 **실제 BM25 정확도는 측정하지
  못했다**.
- 기존 Lexical Schema Linker(규칙·별칭으로 질문을 DB field ID에 연결하는 기능)는
  공개 컴포넌트 평가 181문항에서 Recall@5 96.9639%였다.
- fake Dense는 Recall@5 51.8027%, Lexical+fake Dense는 96.3947%로 기존보다
  0.5692%p 낮았다. Recall@10만 1.8975%p 높지만 후보를 10개로 넓힌 결과이고,
  Precision@5와 정확 field-set은 크게 낮아졌다.
- fake embedding provider(실제 학습 모델 대신 계약만 시험하는 가짜 벡터 생성기)는
  실제 Dense의 의미 이해·latency·memory를 대표하지 않는다. 따라서 **Dense 개선은
  입증되지 않았고 판정은 `insufficient_evidence`**다.
- 모호·금지 질문의 공개 안전 회귀 84건은 모두 QueryPlan compiler(실행계획
  생성기)·provider·SQLite Oracle(정답 계산기)을 호출하지 않았다. Dense 컴포넌트의
  현재 정책 control 19건도 embedding 호출이 0회였다.
- Dense production 채택과 상품 의미 검색은 보류한다. 기존 Agent 실행 경로는
  변경하지 않았고 feature flag(기능 스위치)는 기본 OFF다.

## 2. 무엇을 측정했는가

- SQL: 현재 Backend와 같은 승인 release
  `miraeasset-ai-festival-2026-20260711-v1`의 네 SQLite를 read-only로 연결했다.
- SQL 정답: 동일 source workbook SHA-256에서 만든 기존 공개 SEARCH·AGGREGATE
  8문항의 상품 ID·후보 수·집계 fingerprint를 사용했다.
- SQL 실행기는 현재 production FastAPI E2E가 아니라 혼합 공개 Oracle 회귀다.
  일반 Routed 경로 5건, 국내 ETP `DomesticMockProvider` 1건, 공식 비활성인 FUND를
  내부 평가 flag로 연 경로 2건이다.
- BM25: 실제 corpus가 없어서 합성 문서 4개, positive 검색 4건, 필터·not-found
  negative control 2건만 사용했다.
- Schema Linker: 네 상품군 core-50 공개 suite 200문항에서 현재 정책상 실행 가능한
  181문항·gold field 527개를 평가했다.
- Dense 수치는 정답 상품군을 미리 고정한 **컴포넌트 평가**다. 실제 Router(질문
  분류기)의 상품군 판단까지 포함한 E2E(처음부터 끝까지) 성능이 아니다.
- Safety 84건은 Dense harness가 만든 수치가 아니라 별도 공개 pytest 회귀다.
  비공개 정답을 쓰는 독립 safety blind 최초 평가는 이번에 실행하지 않았으며,
  별도 봉인·사람 검토 절차가 남아 있다.
- 사용하지 않은 것: Qwen, HCLX, 외부 embedding 모델, GPU, 실제 문서 corpus,
  비공개 blind 정답.

보고서에는 실제 상품 ID와 문항별 field ID를 저장하지 않는다. 일치 여부와 집계
수치만 기록해 평가 정답과 상품 행이 로그로 유출되지 않게 했다.

## 3. 현재 기준선

### 3.1 SQL 검색·집계

| 지표 | 결과 |
| --- | ---: |
| 전체 strict | 8/8, 100% |
| SEARCH 상위 상품 ID exact | 4/4, 100% |
| AGGREGATE field·function·value exact | 4/4, 100% |
| 후보 수 exact | 8/8, 100% |
| 성능 측정 성공 표본 | 8건 |
| Agent Core 내부 execution latency p50 | 373.869ms |
| Agent Core 내부 execution latency p95·max | 1,819.208ms |
| process peak RSS p50 | 64,380KiB |
| process peak RSS p95·max | 102,960KiB |
| 실행 중 peak RSS 증가 p50 | 3,408KiB |
| 실행 중 peak RSS 증가 p95·max | 41,988KiB |

각 문항은 승인 검사를 마친 새 Python process에서 한 번씩 단독 실행했다. 실패한
child process는 latency·RSS percentile에서 제외하고 측정 건수를 별도로 기록한다.
각 child에는 60초 timeout이 있으며, timeout은 정확도 실패로 처리한다. 결과 case
ID·상품군·intent가 기대 suite와 일대일로 같지 않으면 보고서 생성 자체를 거부한다.
latency timer는 Agent Core의 질문 실행 구간만 재며 process startup·승인 검사·JSON
입출력과 HTTP 왕복은 포함하지 않는다.

표본이 8건뿐이고 nearest-rank 방식에서는 p95가 최댓값이므로 운영 SLO(서비스
속도 보장 기준)로 해석하면 안 된다. 같은 source workbook이라도 정규화 DB SHA가
다를 수 있어, 실행 전에 현재 승인 manifest와 네 DB를 다시 검증한다. 이번 세
benchmark는 서로 겹치지 않게 순차 실행했지만 공유 연구실 서버의 다른 사용자 부하는
통제하지 못했다. 성능 채택 기준은 같은 조건의 반복 측정과 cold·warm 분리를 거쳐야
한다.

따라서 8/8은 현재 데이터에서 SQL/Python Oracle(결정론적 검색·계산기)의 상품 ID와
수치가 기존 정답과 일치한다는 뜻이다. 현재 `/answer` FastAPI E2E 8/8, 국내 ETP의
실제 provider 품질, 공모펀드 production 활성화를 의미하지 않는다.

### 3.2 BM25 문서 검색

| 지표 | 결과 |
| --- | ---: |
| 합성 positive top-1 | 4/4, 100% |
| 합성 filter·not-found control | 2/2, 100% |
| 전체 계약 | 6/6, 100% |
| warm 검색 수 | 180회 |
| warm latency p50 | 0.412818ms |
| warm latency p95 | 0.509955ms |
| warm latency max | 2.932023ms |
| index build | 3.343262ms |
| process peak RSS | 58,864KiB |
| 측정 중 peak RSS 증가 | 7,104KiB |
| 실제 corpus 정확도 | 측정 불가능 |

percentile은 linear interpolation 방식이다. 이 BM25는 caller-fed document RAG
(호출자가 승인 문서를 넣는 근거 검색)용이며 상품 SQL 검색이 아니다. 위 결과는
SQLite FTS5 적재·검색·source filter·not-found 계약과 실행 비용만 검증한다.

### 3.3 Schema Linker field ID 정확도

공개 200문항 중 legacy 기준은 실행 180·차단 20이었다. `bond-049`의 “AA- 이상”은
현재 코드가 ordered credit-rating을 고정된 `credit_rating IN (...)` 조건으로
처리할 수 있어 실행 181·차단 19로 migration했다. 이 변경은 별도 versioned JSON에
원본 bond suite SHA와 질문 SHA를 고정했다. 다만 상태는
`developer_authored_pending_finance_domain_review`이며 금융 도메인 담당자의 독립
승인이 끝난 gold라고 주장하지 않는다.

| 지표 | Lexical | fake Dense | Lexical+fake Dense |
| --- | ---: | ---: | ---: |
| field-set exact | 92.2652% | 2.7624% | 64.0884% |
| 반환된 top≤5 중 Precision | 99.4163% | 30.1657% | 56.1326% |
| 고정 분모 Precision@5 | 56.4641% | 30.1657% | 56.1326% |
| Recall@3 | 88.2353% | 37.3814% | 83.3017% |
| Recall@5 | 96.9639% | 51.8027% | 96.3947% |
| Recall@10 | 97.9127% | 71.5370% | 99.8102% |
| full-recall case@5 | 92.2652% | 12.7072% | 92.2652% |
| MRR | 99.4475% | 72.2838% | 98.2505% |
| nDCG@5 | 98.1472% | 50.5771% | 96.3036% |

Lexical은 평균 2.84개만 반환해 “반환된 후보 중 Precision”이 높다. 세 방식을 같은
181×5 분모로 비교한 고정 Precision@5에서는 Hybrid가 0.3315%p 낮다. Recall@5는
0.5692%p, 정확 field-set은 28.1768%p 낮으므로 fake 결과를 개선으로 판정할 수 없다.

### 3.4 Dense 컴포넌트 비용과 안전

| 지표 | 결과 |
| --- | ---: |
| family-field 문서·vector | 100개 |
| dimension | 256 |
| 이론상 raw float64 vector payload | 204,800 bytes |
| index build | 82.198493ms |
| index build peak RSS 증가 | 1,728KiB |
| process peak RSS | 54,808KiB |
| fake Dense query p50·p95 | 0.597688ms · 0.703366ms |
| Schema Link 단계 전체 p50·p95 | 0.970250ms · 1.340884ms |
| 현재 정책 control provider 무호출 | 19/19, 100% |
| registry 밖 field ID | 0건 |
| 상품군 밖 field ID | 0건 |
| production feature probe 호출 | 0건 |

percentile은 linear interpolation 방식이다. 204,800 bytes는 Python 객체 overhead를
제외한 이론상 float64 배열 크기이며 실제 메모리 사용량이 아니다. 위 latency와 RSS도
fake provider 수치이므로 실제 embedding 모델 용량·속도로 해석하면 안 된다.

Router만 보면 control 6건을 실행 후보로 잘못 보았지만 Semantic Coverage Gate
(필요한 의미 조건이 안전하게 표현되는지 확인하는 문턱)와 Lexical 검사 뒤 Dense 직전
false positive는 0건이었다. 반대로 실행 gold 11건은 현재 production gate에서
멈췄고, 실행으로 분류된 1건은 상품군이 달랐다. 이 181건의 검색 점수는 정답 상품군을
강제로 사용한 컴포넌트 진단이므로 이 routing 오류를 감추는 E2E 점수로 사용하면 안
된다.

## 4. 구현한 계약

### Dense index manifest와 불변성

- `scope=offline_evaluation_only`, `production_enabled=false`
- `abstention_policy=not_calibrated`
- field registry schema·SHA-256 고정
- corpus template·전체 canonical field text·SHA-256 고정
- `(product_family, field_id)` 전체 key 수·SHA-256 고정
- provider kind·model ID·license 고정, 실제 `frozen_model` revision은 40/64자리
  commit digest만 허용
- dimension·pooling·L2 normalization·cosine metric 고정
- vector 수·artifact SHA-256 고정
- 일부 field를 빼고 SHA를 다시 계산한 self-consistent 위조 artifact도 canonical 전체
  corpus와 다르면 load 단계에서 fail-closed
- record와 vector는 tuple로 보존하고 생성자에서 다시 검증·복사해 검증 후 변경 차단

fake index의 field registry schema는 `1.3`, key·vector는 100개다. migration suite
SHA-256은
`82613954ce1734f34f51f1254d7d8e65d34c966b5048984b9a535d1c45ad5405`다.

### Feature flag와 abstention

- `FINANCE_DENSE_SCHEMA_LINKER_ENABLED` 기본값은 `false`다.
- v1 manifest가 production 사용을 금지하므로 flag를 `true`로 강제해도 embedding
  query 전에 오류로 차단한다.
- `RoutedFinanceAgent`, FastAPI `build_agent()`, `/answer`에는 연결하지 않았다.
- OOD(학습·승인 범위 밖 질문) score·margin·abstain 임계값은 아직 보정되지 않았다.
  현재 검색 함수는 비어 있지 않은 질문이면 항상 top-k를 반환하므로, 실제 모델과
  독립 holdout으로 기권 기준을 정하기 전에는 production 활성화할 수 없다.
- `torch`, `faiss`, `sentence-transformers` 등 새 runtime 의존성을 추가하지 않았다.

### SQL·BM25 측정 계약

- SQL child timeout 60초, 실패 성능 표본 제외, case ID·상품군·intent 완전성 검증
- SQL은 nearest-rank, Dense·BM25는 linear-interpolation percentile 사용을 결과에
  명시
- BM25 positive top-1과 negative filter·not-found control을 별도 집계
- 실제 상품 ID 대신 exact 여부와 fingerprint SHA-256만 보고서에 보존

## 5. 재현 명령

```bash
finance-evaluate-approved-sql \
  --database-dir /data \
  --require-perfect

finance-benchmark-bm25-contract \
  --repetitions 30 \
  --require-perfect

finance-evaluate-dense-schema-linker \
  --require-contract
```

결과 JSON은 Git 제외 대상인 `artifacts/evaluation/`에 저장한다.

공개 safety 회귀의 근거는
`finance_agent/packages/finance_agent_core/tests/test_agent_safety.py`이며 파일
SHA-256은
`32966f3501174e08187d9b139d01f8c6e02126c73ef5f0518013d6584b40fa42`다.
84개 parametrized route 검사와 compiler·provider·Oracle을 강제로 실패시키는 무호출
검사를 함께 실행해야 한다.

## 6. 상품 의미 검색으로 넘어가기 위한 조건

현재는 상품 vector 검색을 구현하지 않는다. 다음 조건을 모두 통과한 뒤 검토한다.

1. 현재 공개 200문항과 표현이 겹치지 않는 unseen paraphrase suite를 금융 도메인
   담당자가 만들고 정답 field ID를 hash 봉인한다.
2. 실제 embedding 후보 하나만 model ID·revision·license까지 고정한다. 공유 연구실
   서버의 model download·CPU/GPU 점유는 별도 승인 뒤 실행한다.
3. Lexical·Dense·Hybrid를 같은 문항으로 paired 비교한다.
4. OOD 질문의 top-1 score와 top-1/top-2 margin을 독립 holdout에서 보정하고,
   불확실하면 반드시 abstain하도록 한다.
5. 다음 gate를 모두 만족해야 production shadow 후보로 올린다.
   - 독립 평가 Recall@5 또는 exact field-set 순개선 `+2%p 이상`
   - 모호·금지 질문 embedding 무호출 `100%`
   - registry·상품군 밖 field ID `0건`
   - hard filter·정렬·수치 판단은 계속 SQL·QueryPlan이 담당
   - 실제 embedding 추가 p95 `250ms 이하`
   - manifest mismatch·OOD 저신뢰 차단 `100%`
6. 상품 vector화 대상 text와 provenance(어디서 온 문서인지)를 먼저 승인한다. 현재
   해외 ETP 정규화 경로에는 원천의 영문 운용전략 서술이 포함되지 않아 지금 상품
   index를 만들면 의미 정보가 불완전하다.
7. 상품 후보는 SQL hard filter 뒤의 승인 product universe 안에서만 Dense로
   재정렬하고, 최종 상품 ID·수치·근거는 SQLite에서 재조회해 Verifier(재검산기)로
   검증한다.
8. Re-ranker(상위 후보를 더 정밀하게 재정렬하는 모델)는 실제 Dense top-20에 정답이
   있지만 순서만 틀린 독립 실패가 충분히 쌓일 때만 별도 실험한다.

## 7. 검증 상태

- Dense/BM25/SQL 표적 pytest: **24 passed**
- 공개 safety 84건 route·무실행 pytest: **85 passed**
  (84개 parametrized case + 1개 전체 무호출 경계 검사)
- AI Core 전체 pytest: **944 passed**
- Ruff lint: **통과**
- Ruff format: **223 files 통과**
- 실제 HCLX·Qwen·외부 embedding 호출: **0회**
- 이번 Dense 작업의 live Backend container·현재 Compose project 재기동/변경:
  **없음**
- worktree의 별도 HCLX/FastAPI 선행 변경: **본 보고서 범위 밖·보존**
- 이번 작업이 만든 임시 Docker image 2개(약 712MB): **정확한 태그만 삭제,
  필요 시 재빌드 가능**
- commit·push·PR: **수행하지 않음**

이번 변경은 `finance_agent/`의 AI Core 계약과 평가 도구에 해당하므로 조해영 담당자의
검토가 필요하다. 검토 전 production feature는 계속 OFF로 유지한다.
