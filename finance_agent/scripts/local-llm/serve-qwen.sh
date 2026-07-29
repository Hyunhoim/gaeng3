#!/usr/bin/env bash
set -euo pipefail

readonly VLLM_BIN="/home/haeyeongcho/miniforge3/envs/gaeng3-llm-local/bin/vllm"
readonly MODEL_ID="${LOCAL_LLM_MODEL_ID:-Qwen/Qwen3-30B-A3B-Instruct-2507-FP8}"
readonly MODEL_REVISION="${LOCAL_LLM_MODEL_REVISION:-5a5a776300a41aaa681dd7ff0106608ef2bc90db}"
readonly MODEL_CACHE_DIR="${LOCAL_LLM_CACHE_DIR:-/home/haeyeongcho/.cache/huggingface}"
readonly SERVED_MODEL_NAME="${LOCAL_TEST_LLM_MODEL:-qwen3-local-test}"
readonly HOST="${LOCAL_LLM_HOST:-127.0.0.1}"
readonly PORT="${LOCAL_LLM_PORT:-18000}"
readonly MAX_MODEL_LEN="${LOCAL_LLM_MAX_MODEL_LEN:-32768}"
readonly GPU_MEMORY_UTILIZATION="${LOCAL_LLM_GPU_MEMORY_UTILIZATION:-0.85}"

if [[ "${HOST}" != "127.0.0.1" && "${HOST}" != "localhost" && "${HOST}" != "::1" ]]; then
  echo "Refusing to expose the test-only local LLM outside loopback." >&2
  exit 2
fi

if [[ ! -x "${VLLM_BIN}" ]]; then
  echo "vLLM is not installed in gaeng3-llm-local." >&2
  exit 2
fi

export CUDA_VISIBLE_DEVICES="${LOCAL_LLM_CUDA_VISIBLE_DEVICES:-0,1}"
# flashinfer-python 0.6.13 sees the host NVCC 12.8 and misdetects Blackwell
# SM 12.0 during sampler JIT. Native vLLM sampling is deterministic for this
# temperature-0 parser workload and avoids that optional kernel path.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

exec "${VLLM_BIN}" serve "${MODEL_ID}" \
  --revision "${MODEL_REVISION}" \
  --download-dir "${MODEL_CACHE_DIR}" \
  --served-model-name "${SERVED_MODEL_NAME}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --tensor-parallel-size 2 \
  --enable-expert-parallel \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --max-num-seqs 4 \
  --generation-config vllm
