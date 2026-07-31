# 로컬 LLM 테스트 런타임

상태: 개발 전용 · 기존 상품군 회귀와 공모펀드 SEARCH·COMPARE·상품명 resolution·검증 답변 E2E 완료
기준일: 2026-07-30

## 경계

이 런타임은 HyperCLOVA X API를 사용할 수 없는 개발 기간에 QueryPlan parser의
연결과 E2E를 시험하기 위한 임시 장치다.

- 평가·제출·운영 provider가 아니다.
- HyperCLOVA X endpoint나 credential을 읽거나 호출하지 않는다.
- 로컬 모델은 자연어 질문을 `QueryPlan`으로 변환하는 일만 한다.
- 필터링·정렬·후보 수 계산은 parameterized SQLite oracle이 수행한다.
- 독립 Python verifier가 SQL 결과를 다시 계산하고 불일치하면 응답을 거절한다.
- 상품명·수치·기준일·근거는 검증된 데이터에서 결정론적으로 컴파일한다.
  선택적 LLM 설명은 Answer Verifier를 통과한 경우에만 붙인다.
- 기본 테스트와 CI는 모델 없이 Mock provider로 실행한다.

## 고정된 런타임

| 항목 | 값 |
| --- | --- |
| Conda 환경 | `gaeng3-llm-local` |
| Python | 3.12 |
| vLLM | 0.25.1 |
| PyTorch | 2.11.0+cu130 |
| CUDA runtime | 13.0 |
| 호스트 NVCC | 12.8.93 |
| GPU | NVIDIA GeForce RTX 5090 × 2 |
| 모델 | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` |
| 모델 revision | `5a5a776300a41aaa681dd7ff0106608ef2bc90db` |
| 제공 이름 | `qwen3-local-test` |
| endpoint | `http://127.0.0.1:18000/v1` |
| sampler | vLLM native |

모델은 Apache-2.0 공개 가중치이며 30.5B total / 3B activated parameter의 FP8
MoE다. 가중치는 저장소에 넣지 않고 `/home/haeyeongcho/.cache/huggingface`에 둔다.

## 설치

`finance_agent/` 디렉터리에서 실행한다.

```bash
/home/haeyeongcho/miniforge3/bin/conda env create \
  -f environment.local-llm.yml

/home/haeyeongcho/miniforge3/envs/gaeng3-llm-local/bin/python \
  -m pip install -r requirements/local-llm.txt
```

애플리케이션 환경인 `gaeng3-dev`에 vLLM을 설치하지 않는다.

## 모델 다운로드와 검증

먼저 실제 다운로드 대상을 확인한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-llm-local/bin/hf download \
  Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 \
  --revision 5a5a776300a41aaa681dd7ff0106608ef2bc90db \
  --cache-dir /home/haeyeongcho/.cache/huggingface \
  --dry-run
```

그다음 다운로드하고 원격 revision의 파일 목록과 checksum을 대조한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-llm-local/bin/hf download \
  Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 \
  --revision 5a5a776300a41aaa681dd7ff0106608ef2bc90db \
  --cache-dir /home/haeyeongcho/.cache/huggingface

/home/haeyeongcho/miniforge3/envs/gaeng3-llm-local/bin/hf cache verify \
  Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 \
  --revision 5a5a776300a41aaa681dd7ff0106608ef2bc90db \
  --cache-dir /home/haeyeongcho/.cache/huggingface \
  --fail-on-missing-files
```

## 서버와 E2E

터미널 1에서 loopback 서버를 실행한다.

```bash
scripts/local-llm/serve-qwen.sh
```

기본값은 tensor parallel 2, expert parallel, context 32,768, GPU memory
utilization 0.85다. 서버가 준비된 뒤 터미널 2에서 세 가지 opt-in을 모두 명시한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/local-llm/e2e.py
```

E2E는 다음을 모두 검사한다.

1. `/v1/models` health check와 제공 모델명
2. JSON Schema constrained decoding으로 생성된 `QueryPlan`
3. Mock 기준의 필수 조건·정렬·limit와의 의미 동등성
4. 필수 projection 7개의 포함과 추가 projection의 registry 검증
5. SQLite oracle과 독립 verifier의 일치
6. 후보 440개, 상위 상품 5개의 field-level evidence
7. `artifacts/e2e/local-qwen-response.json` 생성

서버는 `Ctrl-C`로 종료한다. loopback 이외의 host를 지정하면 실행 스크립트가
거절한다.

네 상품군 전체 E2E red-team은 같은 서버를 켠 상태에서 별도로 실행한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.red_team_cli \
  --provider local_test \
  --require-perfect \
  --require-no-fallback
```

질문 구성, 최초 실패와 수정 후 결과는
[internal-red-team-v1 전체 E2E 평가](evaluation-internal-red-team.md)에
보존한다.

## 2026-07-28 실제 검증 결과

- 모델 cache 14개 파일의 누락·checksum 검증 통과
- 디스크 사용: Conda 환경 약 9.6GB, 모델 cache 약 30GB, vLLM compile
  cache 약 835MB
- 32,768 context, tensor parallel 2, expert parallel로 기동 성공
- 모델 weight는 GPU당 약 14.75GiB
- 정상 대기 상태 GPU 메모리는 각각 28,075MiB, 28,019MiB
- 32K 요청 기준 KV cache 약 245K~248K tokens, 최대 동시성 약 7.5배
- structured output 생성 처리량 약 47~50 tokens/s
- 필수 locked 조건 6개와 AUM 내림차순 정렬 검증 통과
- 결정론적 후보 440개와 상위 5개 일치:
  `NAS:BND.O`, `AMX:AGG`, `NYS:SGOV.K`, `NAS:VCIT.O`, `AMX:BIL`
- 고정 projection 정책 적용 후 연속 2회 응답 SHA-256 일치:
  `fe467288d1897350c5603b56ff1b07f03a26e6066d480ac8a74139b1c5add8d3`
- 종료 후 두 GPU는 각각 74MiB, 18MiB로 복귀했고 18000 포트가 해제됨

첫 기동의 engine 초기화는 약 124초가 걸렸다. compilation cache가 생긴 뒤에는
engine 초기화가 약 10초로 줄었다.

## 핵심 50문항 batch 평가

E2E 한 문항을 통과한 뒤, 질문·기대 QueryPlan·oracle을 미리 동결한 해외 ETP
50문항 평가를 같은 모델로 실행했다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect
```

초기 단일 예시 중심 prompt는 development 5/40에 그쳤다. 일반 parser prompt로
28/40, 명시적인 금융 표현을 canonicalize하는 결정론적 lexical linker를 합쳐
40/40까지 개선했다. 이 상태에서 처음 실행한 미사용 holdout은 9/10이었다.
그 오류를 수정한 뒤의 전체 50문항 회귀는 연속 2회 50/50이었지만, 이미 holdout
오류를 본 뒤이므로 100%를 새로운 질문에 대한 unbiased 성능으로 해석하지 않는다.

실험 설계, 지표, hash, 실패 사례와 한계는
[해외 ETP 핵심 평가 기준선](evaluation.md)에 기록한다. 최종 경로는 Qwen 단독이
아니라 `로컬 LLM → 결정론적 linker → 엄격한 계약 → oracle → verifier`인 hybrid
parser다.

## 국내 ETP batch 평가

같은 모델·서버로 국내 ETP suite를 실행할 때 `--dataset domestic_etp`를
추가한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset domestic_etp \
  --provider local_test \
  --split development \
  --workers 4 \
  --require-perfect
```

2026-07-29 결과는 development 40/40, local-inference holdout 첫 실행
10/10이었다. 실행 후 서버를 종료했고 GPU는 74MiB·18MiB, 18000 포트는
해제됐다. 결과 해석과 hash는
[국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md)에 기록한다.

## 근거 기반 최종 답변 batch 평가

답변 평가는 동결 expected QueryPlan으로 검색 해석 오차를 분리한 뒤, 검증된
결과에서 GroundedAnswerDraft만 로컬 Qwen으로 생성한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset domestic_etp \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect
```

최종 결과는 50/50, LLM grounded 47건, 안전 차단 3건, 폴백 0건이었다.
자유 생성 점수가 아니라 opaque result reference, 최소권한 입력, 동적 JSON
Schema, 서버 Answer Verifier와 결정론적 compiler를 합친 시스템 점수다.
초기 실패와 최종 지표·hash는
[근거 기반 최종 답변 평가](evaluation-grounded-answers.md)에 기록한다.

실행 중 GPU 메모리는 28,255MiB·28,199MiB, 종료 후 71MiB·15MiB였고
18000 포트가 해제됐다.

## 국내채권 batch 평가

국내채권은 `--dataset bond`로 QueryPlan과 답변 평가를 각각 실행한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset bond \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect

FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset bond \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect
```

2026-07-29 결과는 QueryPlan 50/50, 답변 50/50이었다. 답변 47개 실행 중
46개는 grounded 생성, 1개는 정상적인 빈 결과였고 3개는 SQL 전에 차단됐다.
폴백은 0건이다. 실제 질문→계획→검색→검증→답변 통합 E2E도 통과했다.
결과와 안전 검증기 오탐 수정 이력은
[국내채권 핵심 평가 기준선](evaluation-domestic-bond.md)에 기록한다.

실행 후 서버를 종료했고 GPU는 74MiB·18MiB, utilization 0%로 복귀했으며
18000 포트가 해제됐다.

## 공모펀드 QueryPlan development 평가

공모펀드는 공식 Agent에서 계속 비활성화한 채, 평가 CLI 안에서만 전용 내부
schema를 사용한다. 기본 명령은 development split만 허용하고, holdout은
parser 규칙을 commit한 뒤 명시적 unlock으로 최초 1회 실행했다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset fund \
  --provider local_test \
  --split development \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-local-qwen-development.json
```

2026-07-29 최초 실행 결과는 40/40이다. valid plan, plan·constraint exact,
Oracle exact와 safety block이 모두 100%였고 생성 지연은 p50 2,905.228ms,
p95 4,288.772ms, max 4,437.341ms였다. 전체 report SHA-256은
`c0d8a60b0a6465b9ef6035f1a3787b4835d765385ae18bfc0ad97d15d1cd99f6`이다.

이 결과는 모델 단독 성능이 아니라 로컬 Qwen, 결정론적 linker, 엄격한 계약,
Oracle과 Verifier를 합친 hybrid parser의 개발 세트 점수다.

commit `32e12fa` 이후 처음 실행한 holdout은 9/10이었다. 실패한 `fund-050`은
클래스 합산·대표 펀드 집계를 unsupported로 인식하지 못했다. 실행은 AUM 통화
모호성으로 차단됐지만 올바른 이유가 아니므로 strict failure로 보존했다. 전체
report SHA-256은
`4bc96ecd7278bbbefe299a0ccea9bff94d14784621b91b2e7a71b414f945846f`다.
상세 계약과 실패 분석은
[공모펀드 핵심 평가 기준선](evaluation-public-fund.md)에 기록한다.
최초 결과를 commit한 뒤 family handoff를 회귀 수정했으며 로컬 Qwen holdout은
다시 호출하지 않았다. 공개된 50문항의 무모델 linker replay만 50/50 통과했다.
평가 후 서버를 종료했고 GPU는 71MiB·15MiB, utilization 0%로 복귀했으며
18000 포트가 해제됐다.

## 공모펀드 grounded answer 평가

공개된 `fund-core-50`의 expected QueryPlan으로 검색 해석을 고정한 뒤,
field-level evidence를 입력으로 받는 최종 답변 계층을 격리 평가한다.
로컬 Qwen은 evidence에 없는 상품명·수치·순위·기준일·근거를 만들 수 없으며
draft verifier와 compiled verifier를 모두 통과해야 한다. 하나라도 실패하면
검증된 검색 결과만 사용하는 결정론적 답변으로 fallback한다.

expected provider 기준선:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset fund \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-answer-expected-all-v1.json
```

로컬 Qwen 평가:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset fund \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-answer-local-qwen-all-v1.json
```

두 실행 모두 strict 50/50이며, 실행 가능한 44문항은 grounded answer로
생성되고 안전 문항 6개는 검색 전에 차단됐다. deterministic fallback은 0건,
LLM 생성 시도 기준 fallback rate는 0%다. 상품명·수치·순위·기준일·근거와
warning 검증도 모두 100%다.

- expected report SHA-256:
  `e516e07e135bca0ae54f9d10f2ee917d6518cafe76c1ffd87717f56e9dd66f38`
- local Qwen report SHA-256:
  `30b02b11b6780c422f709f88f45a92fc0a16e6c85a553ae415ee5ebd4eb46b6c`
- local Qwen 생성 지연: p50 `2,602.016ms`, p95 `4,806.568ms`,
  max `5,069.169ms`
- 실행 중 GPU 메모리: `28,253MiB`·`28,197MiB`

자동 grounding 품질과 별개로 문장 다양성도 기록했다. 44개 초안의 lead는
1종, 상품별 설명 216개는 18종으로 문체가 보수적이고 반복적이다. 사람 rubric은
아직 실행하지 않았으므로 자연스러움이나 사용자 선호가 100%라는 의미는 아니다.

AUM 조건·정렬·집계에는 `trading_currency = KRW` 또는 `USD`가 정확히 하나의
locked 조건으로 있어야 한다. 그렇지 않으면 실행 정책과 Oracle compiler가
모두 fail-closed한다.

이 명령의 `answer_cli`는 동결 suite의 expected QueryPlan만 사용한다. 자연어
parser, 최초 holdout, 새 blind 질문을 재실행하지 않으므로 QueryPlan 성능이나
새 질문 일반화 성능으로 해석하지 않는다. 상세 계약과 결과는
[공모펀드 핵심 평가 기준선](evaluation-public-fund.md)과
[근거 기반 최종 답변 평가](evaluation-grounded-answers.md)에 기록한다.

## 공모펀드 true COMPARE 평가

정확한 `itm_no` 두 개를 비교하는 `fund-compare-core-20`은 SEARCH 답변과
분리된 COMPARE 실행·생성·검증 경로를 시험한다. 수치 차이, 통화 호환성과
결측 판정은 서버가 수행하고 Qwen은 최소권한 설명만 생성한다.

expected provider:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-answer-expected-all-v1.json
```

로컬 Qwen:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_cli \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-answer-local-all-v1.json
```

두 실행 모두 strict 20/20이다. 완전한 비교 18건은 grounded answer,
존재하지 않는 대상이 포함된 2건은 Qwen을 호출하지 않는 결정론적 답변이며
verifier fallback은 0건이다. field status·numeric delta·citation·기준일은
모두 100%다.

- local Qwen 생성 지연: p50 `1,522.937ms`, p95·max `4,447.683ms`
- expected report SHA-256:
  `d4b76c1743963b45673c63436f210d9975bcf74c7ce74a1de95f5eb7bbb86d85`
- local Qwen report SHA-256:
  `fc64cffa4f920af752ea0c6948cf40691980072a5c073ed7f2aab0f1c25dfd8f`

이 평가는 expected COMPARE QueryPlan을 사용하므로 자연어 parser나 상품명
entity resolution 성능이 아니다. 공개 회귀 세트이며 공식 HyperCLOVA X
평가·제출 경로도 아니다.

## 공모펀드 자연어 COMPARE parser·resolver 평가

`fund-compare-parser-core-24`는 자연어 질문에 적힌 정식명·짧은 이름·상품번호를
exact resolver로 공모펀드 ID에 연결한다. Qwen은 대상 문자열과 비교 필드만
구조화하며 상품을 고르거나 SQL·수치·차이를 만들지 않는다.

expected provider:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_parser_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 Qwen:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_parser_cli \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect
```

두 실행 모두 strict 24/24다. 정상 비교 16건의 Oracle과 요청 순서, 안전 차단
8건의 정책이 모두 일치했다. Qwen의 대상 표현·비교 필드 exact, 질문 grounding,
resolution과 최종 plan도 모두 100%다.

- local Qwen 생성 지연: p50 `569.018ms`, p95 `796.637ms`,
  max `889.169ms`
- suite SHA-256:
  `9e2bd72c001f6384a08111ae195de0bf1a962fe6aafa46b52df012917c1b4c9c`
- expected report SHA-256:
  `579283ec3ccd67574a70c4bce387819c0247922458bd3e9688fa2fd8ccdfe7dd`
- local Qwen report SHA-256:
  `c886abd61861abc10bca7ae727c8c7f32caf4390e9e2b4ceccc4dbb8fc4fdfea`

resolver는 Unicode NFKC·대소문자·공백 차이와 균형 잡힌 바깥쪽 따옴표만
정규화한다. 상품명 내부 구두점과 클래스 표기는 유지하며, 중복 단축명·사모
범위·미등록명·중복 대상·미지원 비교는 추측하지 않고 Oracle 전에 차단한다.
질문의 전체 대상 surface·순서와 draft를 대조하고 두 identity 사이의 연결어와
접두·연결·꼬리 위치별 문장부호 문법을 정확히 검사한다. 제외·대신·포함 역할,
세 번째 대상, 미등록 상품번호, identity와 지원 비교 언어를 마스킹한 뒤 질문
전체에 남는 미등록 비인용 표현을 차단한다. 비어 있거나 닫히지 않았거나 역방향·
중첩·줄바꿈이 잘못된 따옴표도 실행하지 않는다. plan은 동일 compiler 자기비교
없이 동결 case 계약으로 검사한다. 이 세트는 공개 개발 회귀이며 독립 blind나
공식 HyperCLOVA X 결과가 아니다.

## 공모펀드 자연어 COMPARE부터 검증 답변까지 E2E

분리 검증한 자연어 parser·resolver와 true COMPARE 답변 계층을 같은 공개
24문항에서 연속 실행한다.

expected provider:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-e2e-expected-all.json
```

로컬 Qwen:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-e2e-local_test-all.json
```

expected와 로컬 Qwen 모두 strict 24/24다. 모든 문항에서 parser를 호출하고,
정상 실행 16문항에서만 답변 생성을 시도해 grounded answer 16건을 만들었다.
안전 차단 8문항은 Oracle·답변 생성 전에 종료했고 fallback은 0건이다.
대상·필드·질문 grounding, resolution·plan·Oracle·차단, Answer Verifier,
field status·numeric delta, 실제 비교 셀 값·evidence provenance
fingerprint, evidence citation과 기준일 지표가 모두 100%다.

로컬 Qwen 지연:

- parser: p50 `567.105ms`, p95 `751.575ms`, max `805.811ms`
- answer: p50 `1,582.531ms`, p95·max `2,225.406ms`
- 전체 E2E: p50 `2,142.249ms`, p95 `2,737.07ms`, max `2,853.783ms`

동결 정보:

- E2E overlay suite SHA-256:
  `5f1511c8dea53b13d1207ee1c80adcf6db4c431581ca8b15d5d683951bc7f88c`
- source question suite SHA-256:
  `9e2bd72c001f6384a08111ae195de0bf1a962fe6aafa46b52df012917c1b4c9c`
- expected report SHA-256:
  `01840035f13f14923d335bb01ad77355d0ef3b493c4849ab7c89bfaa6bee435d`
- local Qwen report SHA-256:
  `b67ccbad9ab1cc93d682f4b27d0ae38a901623081477309a7f53f07d91709976`

실행 가능한 문항은 parser와 answer provider를 각각 한 번 호출하므로 모델 호출
횟수는 parser 24회와 answer 16회다. 이 수치는 같은 개발자가 작성한 공개
회귀에서 정확한 상품명 경계와 전체 대상 순서, 독립 QueryPlan 계약, 동결
field status·delta·실제 비교 셀 값과 별도의 근거 provenance까지 검사한
hybrid system 계약 준수율이다. 독립 blind 일반화, 사람의 설명 품질,
HyperCLOVA X나 공식 평가 결과는 아니며 공모펀드 공식 Agent 실행도 계속
비활성 상태다.

## 확인된 호환성 메모

호스트 `/usr/local/cuda`의 NVCC 12.8은 RTX 5090의 SM 12.0을 FlashInfer
sampler JIT에서 올바르게 판정하지 못한다. 이 선택적 sampler만
`VLLM_USE_FLASHINFER_SAMPLER=0`으로 끄고 검증된 vLLM native sampler를 쓴다.
FlashAttention과 DeepGEMM FP8 MoE 실행은 유지된다.

pip 환경에는 NVCC 13.2와 CUDA 13.3 header가 함께 들어 있어 `CUDA_HOME`을 해당
경로로 강제하면 compiler/header minor mismatch가 발생했다. 따라서 현재
검증된 실행 스크립트의 CUDA 설정을 임의로 바꾸지 않는다. vLLM 0.25.1은
`Ctrl-C` 종료 시 정리 대상 shared-memory 경고를 출력할 수 있으나, 실제 worker,
GPU 메모리, 18000 포트가 모두 해제되는 것을 별도로 확인했다.

## 참고

- [Qwen 모델 카드](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8)
- [vLLM GPU 설치 문서](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)
- [vLLM OpenAI-compatible server 문서](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/)
