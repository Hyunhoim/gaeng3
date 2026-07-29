# 로컬 LLM 테스트 런타임

상태: 개발 전용 · 3개 기존 상품군 회귀와 공모펀드 development 검증 완료
기준일: 2026-07-29

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

## 공모펀드 development 평가

공모펀드는 공식 Agent에서 계속 비활성화한 채, 평가 CLI 안에서만 전용 내부
schema를 사용한다. 로컬 모델로 아직 사용하지 않은 holdout을 보호하기 위해
기본 명령은 development split만 허용한다.

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
Oracle과 Verifier를 합친 hybrid parser의 개발 세트 점수다. holdout 10문항은
아직 모델에 노출하지 않았다. 상세 계약과 다음 실행 순서는
[공모펀드 핵심 평가 기준선](evaluation-public-fund.md)에 기록한다.
평가 후 서버를 종료했고 GPU는 71MiB·15MiB, utilization 0%로 복귀했으며
18000 포트가 해제됐다.

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
