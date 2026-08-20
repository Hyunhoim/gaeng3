# finance-agent-core

금융상품 Agent의 데이터 감사, 정규화 계약, QueryPlan, 결정론적 검색과 검증을 애플리케이션 shell과 독립적으로 개발하는 Python 패키지다.

현재 구현 범위는 원천 XLSX의 재현 가능한 감사, 네 상품군 정규화·SQLite,
네 상품군 field registry·QueryPlan 계약, 해외·국내 ETP·국내채권의 결정론적
검색·독립 검증·field-level evidence, Mock 및 개발 전용 로컬 LLM provider다.
공모펀드는 product·attribute·quarantine 저장, Oracle, Verifier, field
evidence, 동결 50문항 계약, 개발 전용 로컬 parser와 grounded answer 평가까지
구현했다. 정확한 `itm_no` 두 개를 대상으로 하는 true COMPARE, 필드별 차이·
통화·결측 처리와 20문항 회귀도 구현했다. 정식명·짧은 이름·`itm_no`의 정확
일치 resolver와 자연어 COMPARE parser는 별도 24문항에서 검증했다. 같은
24문항의 parser부터 grounded answer 후검증까지 잇는 통합 E2E도 구현했다.
네 상품군·일곱 intent의 fail-closed Router와 서버 QueryPlan compiler를
구현했다. 상품 검색·비교는 Oracle→Result Verifier→field evidence→Answer
Verifier 경로를 사용한다. BM25/SQLite FTS 문서 검색과 외부 문서의
독립 승인·사용 권한·해시·변조 차단 반입 계약, 프레임워크 독립
Backend DTO·`/answer` service adapter와 사람 평가 rubric은 별도 계약으로
제공한다. 네 상품군 40문항의 공개 `internal-red-team-v1`은 Router부터
로컬 Qwen·Oracle·Verifier·Backend DTO까지 한 경로로 회귀 검증한다.
금융 도메인 담당자 작성 40문항은 별도 개발 QA로 hash를 고정하고 route·
safety·evidence·answer 단계별 현재 상태를 측정한다.
승인 국내채권·국내/해외 ETP DB의 발행사·운용사·기초지수·자산·지역 관계를
SQLite FTS5로 색인하고, 후보 상품 ID를 공식 DB에서 다시 확인하는 P0-6 기반도
제공한다. P0-7은 관계·문서 전용 Typed Plan, 서버 계획 exact-match gate,
구조화 Claim Verifier와 결정론적 fallback을 내부 CLI까지 연결했다. 기존 자연어
Router·공개 `GET /answer`·운영 Agent Release에는 아직 활성화하지 않는다.
필드 registry와 실제 DB에서 대표 검색·정렬·비교·집계 계획을 자동 만들고,
직접 Oracle 정답과 자연어 Agent의 계획·근거를 비교하는 커버리지 평가도 제공한다.
네 상품군 공통 AGGREGATE는 COUNT·MIN·MAX·AVG·허용 SUM, 최대 두 범주
group, 금액 통화 gate, 결측·기준일 보존, 별도 Python verifier와
`AggregateEvidence`까지 구현했다. 집계 답변은 현재 LLM 없이 결정론적으로
컴파일한다.
네 상품군 공통 COMPARE는 같은 상품군의 정확한 두 상품만 받아 field
`comparable` capability, 통화·기준일·stale·결측 정책을 적용한다.
`ComparisonEvidence`와 별도 `ComparisonResultVerifier`, Backend
`comparison_field` citation까지 연결했다.
복수 상품군 SEARCH v1은 상품군별 단일 QueryPlan·SQLite Oracle·Result
Verifier를 병렬 실행하고 부분 결과와 manifest를 Backend family DTO에
보존한다. 상품군 간 직접 비교·합산·우열 판단과 서로 다른 family 조건은
계속 차단한다.
최종 답변은 evidence만 입력받는 최소권한 GroundedAnswerDraft, draft·compiled
Answer Verifier, 결정론적 evidence compiler와 safe fallback으로 구성한다.
공식 Agent 실행은 HCX schema·서버 계약 승인 전까지 비활성화 상태다.

## 환경

`finance_agent/` 디렉터리에서 실행한다.

```bash
/home/haeyeongcho/miniforge3/bin/conda env update -n gaeng3-dev -f environment.yml
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev \
  python -m pip install -r requirements/dev.txt
```

## 실제 데이터 감사

```bash
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev \
  finance-data-audit \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit
```

감사기는 다음을 보장한다.

- 입력 파일을 파일명 prefix로 찾고 특정 사용자 절대경로나 `(1).xlsx`에 의존하지 않는다.
- 원천 XLSX를 수정하지 않는다.
- 입력 크기와 SHA-256을 manifest에 기록한다.
- 국내채권·국내 ETP·해외 ETP·공모펀드의 핵심 구조와 품질 수치를 계산한다.
- 펀드 coverage를 raw row가 아니라 유효한 `itm_no` product grain에서 계산한다.
- `expectations.json`과 다른 값이 나오면 종료 코드 2로 실패한다.
- 출력은 `artifacts/` 아래에만 생성하며 Git 대상에서 제외된다.

## 계약

- [`field_registry.yaml`](src/finance_agent_core/config/field_registry.yaml): 네 상품군 canonical field, 상품군별 원천 매핑, 품질, 단위, 연산자와 실행 승인 상태
- [`queryplan.py`](src/finance_agent_core/contracts/queryplan.py): 서버의 엄격한 구조·의미 검증
- [`queryplan.hcx.schema.json`](src/finance_agent_core/contracts/queryplan.hcx.schema.json): HyperCLOVA X Structured Outputs용 보수적 schema
- [`capability_matrix.json`](src/finance_agent_core/config/capability_matrix.json): 상품군·intent별 실행·통제 범위
- [`ontology.py`](src/finance_agent_core/ontology.py): field registry 기반 공식 Turtle 5개 결정론적 생성
- [`backend.py`](src/finance_agent_core/contracts/backend.py): Backend request·response·citation·fallback DTO
- [`backend_adapter.py`](src/finance_agent_core/agent/backend_adapter.py): HTTP status·안전한 ERROR DTO·fallback service 경계
- [계약 설명](../../docs/contracts.md): 설계 근거, 첫 vertical slice 예시, 확장 규칙
- [공모펀드 계약](../../docs/public-fund-contract.md): product grain, capability, 품질 규칙, 실행 승인 조건
- [문서 RAG 계약](../../docs/document-rag.md): 승인 문서 BM25/SQLite FTS 검색
- [P0-5 외부 문서 반입 계약](../../docs/p0-5-external-corpus-intake-2026-08-19.md): 독립 review·HTTPS 출처·권한 4종·byte/정규화 hash·canonical manifest·BM25 build
- [P0-6 제공 관계 검색](../../docs/p0-6-provided-relation-retrieval-handover-2026-08-19.md): 발행사·운용사·지수·자산·지역 관계·출처·기준일·공식 상품 ID 재검증
- [P0-7 관계·문서 계획과 주장 검증](../../docs/p0-7-knowledge-claim-verifier-handover-2026-08-20.md): Typed Plan·exact 권한·Claim Verifier·전체 fallback·내부 릴리스 해시
- [공통 AGGREGATE 계약](../../docs/aggregate-engine.md): 함수·그룹·통화·결측·근거
- [공통 COMPARE 계약](../../docs/comparison-engine-design.md): exact identity·필드·통화·기준일·stale
- [교차 상품군 SEARCH·답변 계약](../../docs/cross-family-search.md): 상품군별 계획·병렬 실행·evidence 격리 생성·전체 fallback
- [금융 도메인 QA 실험](../../docs/evaluation-domain-qa.md): 담당자 작성 40문항의 hash 검증·행동 기능·단계별 E2E 채점
- [Ontology 제출 계약](../../docs/ontology.md): 파일 역할·생성·문법·registry 정합성 검사
- [사람 평가 rubric](../../docs/human-evaluation.md): 독립 reviewer·critical gate
- [internal-red-team-v1](../../docs/evaluation-internal-red-team.md): 네 상품군 전체 E2E·안전 회귀
- [공식 형식 30문항 공개 모의평가](../../docs/evaluation-official-mock.md): 난이도 10/10/10·답변 불가 5개 전체 경로
- [자동 커버리지·Qwen 자연화 평가](../../docs/evaluation-coverage-guided.md): 대표 계획 305개·단계별 병목·shard 실험 계약
- [Schema Dense CPU 모델 비교](../../docs/evaluation-schema-embedding-cpu.md): 7개 고정 임베딩·Lexical 우선 결합·독립 blind 진입 기준

## 상품군 vertical slice

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset overseas_etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --provider mock \
  --output artifacts/e2e/mock-response.json
```

국내 ETP는 같은 명령에 `--dataset domestic_etp`와 국내 DB를 지정한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset domestic_etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --database artifacts/normalized/domestic_etp.sqlite3 \
  --provider mock \
  --output artifacts/e2e/domestic-mock-response.json
```

국내채권도 같은 공통 실행 계층을 사용한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset bond \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --database artifacts/normalized/bond.sqlite3 \
  --provider mock \
  --output artifacts/e2e/bond-mock-response.json
```

공모펀드는 정규화 DB 생성, 내부 Oracle 회귀, 로컬 development parser,
expected QueryPlan 기반 SEARCH·COMPARE 답변 격리 평가를 지원한다.
`finance_agent_core.agent` 공식 실행 경로는 아직 지원하지 않는다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset fund \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"
```

로컬 Qwen 연결은 [별도 테스트 런타임 문서](../../docs/local-llm.md)를 따른다.

## 핵심 50문항 평가

동결된 상품군별 질문·기대 QueryPlan·oracle과 평가 하네스를 모델 없이 검증한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset overseas_etp \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/expected-all.json
```

다른 suite는 `--dataset domestic_etp`, `bond`, `fund`로 선택한다.
결과와 한계는 [국내 ETP 평가 기준선](../../docs/evaluation-domestic-etp.md)과
[국내채권 평가 기준선](../../docs/evaluation-domestic-bond.md),
[공모펀드 평가 기준선](../../docs/evaluation-public-fund.md)에 기록한다.

다음 일반화 평가는 금융 도메인 담당자가 독립 작성한 external blind 100문항으로
수행한다. 분포·정답 계약, hash commitment, 최초 1회 실행과 상태 파일은
[연결 전 진단·external blind 프로토콜](../../docs/evaluation-pre-hcx-diagnostic.md)과
`finance-pre-hcx` 도구를 따른다.

교차 상품군의 결정론적 SEARCH 공개 회귀는 모델과 네트워크 없이 실행한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.cross_family_search_cli \
  --require-perfect
```

같은 4문항에서 family별 grounded answer까지 평가하려면 answer provider를
명시한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.cross_family_answer_cli \
  --provider local_test \
  --require-perfect \
  --require-zero-fallback
```

`--provider local_test`는 로컬 Qwen 서버와 세 가지 명시적 opt-in이 모두
필요하다. 최초 holdout과 사후 회귀를 구분한 결과와 재현 절차는
[평가 기준선](../../docs/evaluation.md)에 기록한다. 공모펀드는
`--dataset fund --split development`만 기본 허용한다. commit 이후 명시적으로
unlock한 최초 holdout 9/10 결과와 실패 분석은
[공모펀드 평가 기준선](../../docs/evaluation-public-fund.md)에 기록한다.

## 근거 기반 답변 평가

답변 계층을 동결 expected QueryPlan과 분리해 평가한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset fund \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

실제 로컬 Qwen은 세 가지 opt-in과 `--provider local_test`가 필요하다.
Agent 전체 E2E에서는 `--provider local_test --answer-provider local_test`를
함께 사용한다. 계약, 실패 과정, 지표 해석은
[근거 기반 최종 답변 평가](../../docs/evaluation-grounded-answers.md)에 기록한다.

공개 `fund-core-50` SEARCH 결과는 expected provider와 로컬 Qwen 모두 50/50이다.
44개 실행 가능 문항은 grounded answer, 6개 정책 차단 문항은 blocked이며 로컬
Qwen의 verifier fallback은 0건이다. 이 명령은 동결 expected QueryPlan을
직접 재사용해 답변 계층만 격리 평가하므로 parser나 blind 질문을 다시 실행하지
않는다.

공모펀드 true COMPARE는 별도 공개 회귀 세트로 평가한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 Qwen은 동일 명령에 세 가지 opt-in과 `--provider local_test`를 적용한다.
`fund-compare-core-20`은 expected·로컬 Qwen 모두 20/20이며, 완전한 비교
18개는 grounded answer, 존재하지 않는 비교 대상이 포함된 2개는 LLM을 호출하지
않고 결정론적으로 답했다. verifier fallback은 0건이다. 현재 계약은 한 상품군,
두 개의 정확한 상품 ID, 서버 계산 가능한 필드만 허용한다.

자연어 비교 대상 resolution은 별도 공개 회귀로 평가한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_parser_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 Qwen은 같은 명령에 세 가지 opt-in과 `--provider local_test`를 적용한다.
`fund-compare-parser-core-24`의 expected·로컬 Qwen 결과는 모두 24/24다.
resolver는 Unicode NFKC·대소문자·공백 차이와 균형 잡힌 바깥쪽 따옴표만
정규화하고, 상품명 내부 문장부호와 클래스 표기는 보존한다. 중복명·미등록명·
사모 범위·중복 대상·미지원 비교는 추측하지 않고 차단한다. 질문의 ordered
identity를 draft와 대조하고 두 identity 사이의 연결어와 접두·연결·꼬리
위치별 문장부호 문법을 정확히 검사한다. 누락된 세 번째 대상, 제외·대신·포함
역할, 미등록 상품번호와 identity·지원 비교 언어를 제외한 질문 전체의 미등록
잔여 표현을 실행하지 않는다. 비어 있거나 미종결·역방향·중첩·줄바꿈이 잘못된
따옴표도 차단한다. 로컬 parser 지연은 p50 569.018ms, p95 796.637ms, 최대
889.169ms다.

공개 24문항의 전체 경로는 다음 명령으로 재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

설치된 console script에서는 `finance-evaluate-fund-compare-e2e`를 사용할 수
있다. 로컬 Qwen은 같은 명령에 세 가지 opt-in과 `--provider local_test`를
적용한다. 이 E2E는 자연어 draft 24건을 모두 생성하고, 실행 가능한 16건에만
grounded answer를 추가 생성한다. expected·로컬 결과는 모두 24/24이며
실행 16건·안전 차단 8건, grounded answer 16건, verifier fallback 0건이다.
parser target·field, grounding, resolution, QueryPlan, Oracle, 안전 차단,
field status·numeric delta·실제 비교 셀 값과 별도의 근거 provenance,
Answer Verifier·인용·기준일 지표는 모두 100%다. 로컬 Qwen p95 latency는
parser 751.575ms, answer 2,225.406ms, 전체 2,737.07ms였다.

E2E overlay는 QueryPlan의 핵심 필드를 동일 compiler가 아닌 독립 계약으로
검사하고, 실행 16건의 field status·numeric delta와 실제 비교 셀 값·근거
provenance fingerprint를 별도로 동결한다. 상품명은 정확한 인용 span·식별자 경계뿐 아니라
질문의 전체 대상 순서까지 일치해야 한다. 이는 공개 회귀 세트의 통합 배선
검증이며 AI 담당자와 분리된 작성자의 독립 blind E2E, 사람의 생성 품질 rubric,
공식 HyperCLOVA X 재현은 다음 단계다.

해외 ETP·국내 ETP·국내채권의 공통 자연어 COMPARE 경로는 별도의 결정론적
30문항 회귀로 평가한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.product_comparison_cli \
  --workers 3 \
  --require-perfect
```

설치된 console script는 `finance-evaluate-product-compare`다. 실행 18문항과
안전 차단 12문항이 모두 통과하며 QueryPlan, 상품·필드 순서, field status,
numeric delta, 답변 필수 문구와 Backend comparison citation을 함께 검사한다.
기존 공모펀드 24문항과 합친 공개 자연어 비교 범위는 54문항이다.
COMPARE resolver는 전체 정규화 레코드 대신 최소 identity 열만 bounded
process-local cache에 보관한다. DB 파일 버전이 달라지면 자동 무효화하며,
30문항 report에는 identity/full-record cache hit·miss·load와 latency가 함께
기록된다. 현재 3 workers 기준 p50은 65.522ms, p95는 954.670ms이고 비교
경로의 full-record cache load는 0회다.

SEARCH·AGGREGATE verifier도 기본 실행에서는 전체 정규화 Pydantic 레코드를
적재하지 않는다. QueryPlan의 조건·정렬·그룹·집계 필드와 품질·기준일만
projection으로 읽어 독립 재검산한다. 네 상품군 실제 데이터 8문항의 결과
지문·지연·RSS를 함께 재현하려면 다음을 실행한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.search_aggregate_benchmark_cli \
  --require-perfect
```

설치된 console script는 `finance-benchmark-search-aggregate`다. 이 평가는 공개된
고정 문항의 회귀·개발 장비 성능 기준선이며 독립 blind나 운영 SLO가 아니다.

## 금융 도메인 QA 실험

질문 CSV와 검토 CSV를 원본 수정 없이 검증하고 현재 Router부터 Backend DTO까지
실행한다.

```bash
python -m finance_agent_core.evaluation.domain_qa_cli validate \
  --questions-csv "<questions.csv>" \
  --review-csv "<review.csv>"

python -m finance_agent_core.evaluation.domain_qa_cli run \
  --questions-csv "<questions.csv>" \
  --review-csv "<review.csv>" \
  --database-dir artifacts/normalized \
  --report-id domain-qa-dev-v1-2-router-e2 \
  --output artifacts/evaluation/domain-qa-dev-v1-2-router-e2.json \
  --require-safe \
  --require-perfect
```

설치된 console script는 `finance-evaluate-domain-qa`다. 현재 40문항은 개발
MFT 세트이며 v1.1에서 SEARCH 1문항의 QueryPlan·Oracle·evidence
gold를 완성했다. v1.2 Router·linker 사후 회귀는 모든 계약
40/40, control 잘못된 실행·오류 0건이다. 개선에 사용한 세트이므로
독립 blind나 모델 생성 품질 점수가 아니다.
최초 관측을 보존하려면 사후 실행마다 새로운 `--report-id`와 출력 파일명을
사용한다.

## HyperCLOVA X provider 경계

실제 API 없이 QueryPlan, 공모펀드 비교 초안, 근거 답변의 공통 요청·응답·오류
계약을 테스트할 수 있다. 공식 경로 설정은 다음과 같이 fail-closed로 제한한다.

```text
FINANCE_AGENT_LLM_MODE=evaluation 또는 production
LLM_PROVIDER=hyperclova
HCX_MODEL=HCX-로 시작하는 공식 확인 모델 ID
HCX_TIMEOUT_SECONDS=60
```

현재 구현은 주입형 transport와 fake transport 테스트까지다. 2026-08-06
오프라인 설명회의 공식 안내 전에는 실제 연결을 시도하지 않는다. endpoint·credential·
인증 header를 추측하지 않았고 실제 API 호출용 transport나 CLI 선택지는 아직
없다. 자세한 범위와 남은 작업은
[HyperCLOVA X provider 계약](../../docs/hyperclova-provider.md)을 따른다.

SEARCH에서는 `RoutedFinanceAgent(query_plan_provider=...)`로 provider를
선택 주입할 수 있다. 서버가 독립적으로 만든 QueryPlan과 모델 QueryPlan이
완전히 일치해야만 Oracle을 실행한다. API 없는 전체 Agent 경로는 다음 명령으로
재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-hcx-contract-e2e.py \
  --require-perfect
```

해외·국내 ETP와 국내채권 정상 경로, Answer Verifier fallback, timeout,
Router 제어, 비활성 공모펀드, 계획 불일치 총 8개 시나리오를 검사한다.

## Backend `/answer` service adapter

FastAPI route가 연결되기 전에도 framework-neutral adapter를 호출해 권장 HTTP
status와 schema 검증된 응답 DTO를 함께 받을 수 있다.

```python
from finance_agent_core.agent import execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest

request = BackendAgentRequest(
    request_id="request-001",
    question="미국 주식형 해외 ETF 중 총보수가 낮은 3개를 보여줘",
)
result = execute_answer_request(agent, request)

result.http_status_code
result.response.model_dump(mode="json")
```

정상·control·not-found·검증된 fallback은 HTTP 200이다. QueryPlan provider,
dataset과 내부 장애는 원문 예외를 노출하지 않는 `error` DTO와 HTTP
500·502·503·504로 변환한다. grounded answer provider 장애는 이미 검증된
evidence가 있으므로 결정론적 fallback과 HTTP 200으로 복구한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

동결된 12개 시나리오 결과는 12/12다. 실제 FastAPI route와 HTTP 인증은
application shell 통합 시 이 반환값 위에 추가한다.

## 검증

```bash
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev pytest
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev ruff format --check .
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev ruff check .
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip check
```
