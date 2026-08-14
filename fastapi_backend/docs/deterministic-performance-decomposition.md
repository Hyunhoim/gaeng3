# 결정론적 API 성능 원인 분해 실행 가이드

이 문서는 Dense 경로를 추가하기 전에 현재 결정론적 API의 병목을 재현하고, 변경 전후의
성능과 정확성이 동일한 조건에서 비교되도록 하는 실행 절차를 정의한다. 질문 원문이나 응답
원문을 결과물에 복사하지 않고, 고정 질문 집합의 식별자와 canonical SHA-256으로 동일성을
검증한다.

> **절대 금지:** 공유 중인 `http://127.0.0.1:18001`, `http://127.0.0.1:18002`와 해당
> 컨테이너·Compose project·volume을
> 시작, 중지, 재시작, 재빌드, inspect 또는 부하 테스트 대상으로 사용하지 않는다. 아래 모든
> 명령의 포트는 `18001`·`18002`가 아닌 전용 포트여야 한다. 예시는 `18081`을 사용하지만, 실제 실행
> 시에는 충돌하지 않는 포트를 새로 배정한다.

이 문서는 실행 절차다. 아래의 예시 명령이나 기존 Stage 4 진단 근거를 새로운 측정 완료로
간주하지 않는다. 최종 판정에는 격리 환경에서 새로 생성한 원본 보고서와 Audit commitment가
필요하다.

## 1. 현재 가설과 확인해야 할 지점

Stage 4 기준 요약의 concurrency 2 p95는 약 `1.774 s`다. 당시 Audit JSONL을 진단 목적으로
재분해했을 때 ANSWER p95 약 `1.743 s` 중 VERIFIER p95가 약 `1.698 s`로, 약 97%를
차지했다. 같은 구간에서 기존 계측상 SQL·Oracle·Router·QueryPlan compiler는 각각 수~십
ms 수준이었다.

현재 가장 강한 가설은 Verifier의 실제 비교 계산 자체보다 매 요청마다 verifier projection을
조회하고 전체 행을 Python 객체로 materialize하는 작업이 동시성 2에서 CPU/GIL 경합을
일으킨다는 것이다. 기존 VERIFIER 타이머는 projection 조회, 행 materialization, 순수 검증을
한 덩어리로 포함했으므로, 이 가설은 세부 계측이 들어간 격리 재실행으로 확인해야 한다.

새 계측에서는 다음 구간을 각각 분리한다.

- Router와 QueryPlan compiler
- SQLite authority/oracle/verifier-projection connection 준비
- Oracle SQL statement 실행과 Oracle 전체 실행
- verifier projection fetch, 행 materialization, universe 구성, 순수 Verifier
- evidence·answer renderer와 citation 생성
- backend DTO, official DTO, HTTP response 직렬화
- Audit JSONL append+fsync 비용(동일 파일시스템의 별도 probe)
- 응답 payload bytes, citation 수, 고유 evidence reference 수

Audit append+fsync probe는 downstream 저장 비용이다. API가 bounded async queue를 쓰므로
probe의 latency를 HTTP 응답 latency에 그대로 더해서 해석해서는 안 된다. 응답 경로와 queue
drain/누락 검증을 함께 봐야 한다.

## 2. 격리 원칙

한 번의 측정 대상은 고유한 다음 값들을 가져야 한다.

- Compose project name: 예 `finance-perf-candidate-20260814t120000`
- backend container name: 예 `finance-perf-candidate-20260814t120000-backend`
- loopback port: 예 `18081` (`18001`·`18002` 금지)
- project-scoped data volume과 owner-only Audit/result 디렉터리
- 고정된 backend CPU `2.0`, memory `1 GiB`, PID `256`, nofile soft/hard `4096` 제한과
  `restart: "no"` 정책(data-init도 전용 제한과 `restart: "no"` 적용)
- `OFFICIAL_ANSWER_MAX_INFLIGHT=4` 이상: c1·c2·c4 정상 경로 비교에서 admission control이
  병목 측정을 가리지 않게 한다.
- `WEB_CONCURRENCY=1`, 동일 timeout, 동일 fund policy, Dense/HCLX 비활성화

baseline과 candidate는 동시에 띄우지 않는 편이 좋다. 같은 호스트에서 순차 실행하고, 실행
순서를 한 차례 뒤집어(host cache나 다른 부하의 영향을 확인) 반복한다. 두 대상에 CPU quota,
memory limit, PID limit, Docker/host 버전, 원본 데이터가 완전히 같아야 한다.

실행 전 기본 변수 예시는 다음과 같다. 결과 디렉터리는 새로 만들고 owner-only 권한을 준다.

```bash
export PERF_RUN_ID=candidate-20260814t120000
export PERF_PROJECT=finance-perf-$PERF_RUN_ID
export PERF_CONTAINER=$PERF_PROJECT-backend
export PERF_PORT=18081
export PERF_ROOT=/tmp/$PERF_PROJECT
export PERF_RAW_DATA=/absolute/path/to/raw-finance-data
export SOURCE_COMMIT=<clean-40-hex-Git-commit>
export PERF_IMAGE=localhost:5000/$PERF_PROJECT@sha256:<registry-digest>

test "$PERF_PORT" != 18001
test "$PERF_PORT" != 18002
mkdir -m 700 "$PERF_ROOT"
mkdir -m 700 "$PERF_ROOT/audit" "$PERF_ROOT/results"
```

변수는 예시이므로 다른 작업과 겹치지 않는 새 값을 사용한다. 기존 결과 파일은 덮어쓰지
않으며, 실행 도구가 `O_EXCL`로 새 파일만 만드는 경우 실패 시 새 경로를 지정한다.

## 3. 전용 Docker 시작과 종료

전용 override인 `fastapi_backend/docker-compose.performance.yml`과 launcher인
`fastapi_backend/scripts/isolated_performance_run.py`의 실제 인자는 checkout의 `--help`를
먼저 확인한다.

```bash
PYTHONPATH=fastapi_backend python3.12 \
  fastapi_backend/scripts/isolated_performance_run.py --help
```

launcher가 제공하는 `config`/`up`/`down` 인터페이스를 사용해 고유 project, container,
port, Audit directory, raw data directory, image reference와 자원 제한을 전달한다. `config`로
렌더링 결과를 먼저 확인한다.

```bash
PYTHONPATH=fastapi_backend python3.12 \
  fastapi_backend/scripts/isolated_performance_run.py config \
  --project "$PERF_PROJECT" \
  --container-name "$PERF_CONTAINER" \
  --port "$PERF_PORT" \
  --audit-dir "$PERF_ROOT/audit" \
  --raw-data-dir "$PERF_RAW_DATA" \
  --image-reference "$PERF_IMAGE" \
  --source-commit "$SOURCE_COMMIT" \
  --cpu-limit 2.0 \
  --memory-limit 1g \
  --pids-limit 256
```

렌더링된 config에서 `18001`·`18002`가 전혀 노출되지 않고, host bind가 정확히
`127.0.0.1:$PERF_PORT`이며, `docker-compose.performance.yml`의 CPU, memory, PID 제한과
nofile·restart 정책, `OFFICIAL_ANSWER_MAX_INFLIGHT=4`가 정확히 존재해야 한다. launcher는
backend 자원을 CPU `2.0`, memory `1 GiB`, PID `256`으로 고정하고 다른 값은 거부한다. 확인 후
시작한다. candidate image를 이
checkout에서 직접 빌드하지 않는다. clean checkout에서 먼저 image를 빌드·Registry push하고,
확정된 `repository@sha256`와 일치하는 40자리 source commit을 입력한다. launcher는 로컬에
pull된 image의 RepoDigest·OCI revision label·linux/amd64 platform을 시작 전에 재검사한다.

```bash
PYTHONPATH=fastapi_backend python3.12 \
  fastapi_backend/scripts/isolated_performance_run.py up \
  --project "$PERF_PROJECT" \
  --container-name "$PERF_CONTAINER" \
  --port "$PERF_PORT" \
  --audit-dir "$PERF_ROOT/audit" \
  --raw-data-dir "$PERF_RAW_DATA" \
  --image-reference "$PERF_IMAGE" \
  --source-commit "$SOURCE_COMMIT" \
  --cpu-limit 2.0 \
  --memory-limit 1g \
  --pids-limit 256
```

launcher는 포트 `18001`·`18002`를 명시적으로 거부하고, override Compose의 rendered
config에도 두 공유 포트가 노출되지 않는지 검사한다. 이 방어를 수동 확인으로 대체하거나
우회하지 않는다.
측정 중에는 이미지 재빌드, container restart, 원본 데이터 교체, 자원 제한 변경을 하지
않는다.

모든 부하가 끝나면 먼저 측정 대상의 `/health`를 한 번 확인하고 container를 정상 종료한다.
Audit 검증은 반드시 종료 후 수행한다. 그래야 async audit queue가 flush된 뒤 START/terminal과
event sequence 완전성을 판정할 수 있다.

```bash
PYTHONPATH=fastapi_backend python3.12 \
  fastapi_backend/scripts/isolated_performance_run.py down \
  --project "$PERF_PROJECT" \
  --container-name "$PERF_CONTAINER" \
  --port "$PERF_PORT" \
  --audit-dir "$PERF_ROOT/audit" \
  --raw-data-dir "$PERF_RAW_DATA" \
  --image-reference "$PERF_IMAGE" \
  --source-commit "$SOURCE_COMMIT" \
  --cpu-limit 2.0 \
  --memory-limit 1g \
  --pids-limit 256
```

`down`에서도 같은 exact project/container 식별자와 필수 인자를 사용한다.
project/container/port를 생략한 broad `docker compose down`은 사용하지 않는다. 종료 전에
Audit 파일을 분석해 성공으로 확정하지 않는다.

## 4. 짧은 cold/warm benchmark

`deterministic_api_benchmark.py`는 실제 HTTP 경로에서 cold readiness, semantic contract,
strict warm c1·c2·c4, admission c8, payload bytes, canonical response hash와 Docker identity를
수집한다. `--base-url`은 필수이며 공유 포트 `18001`·`18002`가 아닌 명시적인 loopback 포트만
허용한다.

```bash
PYTHONPATH=fastapi_backend python3.12 \
  fastapi_backend/scripts/deterministic_api_benchmark.py \
  --base-url "http://127.0.0.1:$PERF_PORT" \
  --container-name "$PERF_CONTAINER" \
  --warm-requests 100 \
  --stress-requests 80 \
  --request-timeout-seconds 60 \
  --ready-timeout-seconds 120 \
  --output "$PERF_ROOT/results/benchmark.json"
```

필요하면 컨테이너의 정확한 cgroup-v2 경로를 `--cgroup-path`로 전달하고
`--require-runtime-metrics`를 사용한다. 경로를 추정하지 말고 해당 격리 컨테이너의 정확한
cgroup만 지정한다.

이 v1 benchmark에서 `warm_c1`, `warm_c2`, `warm_c4`는 admission control을 허용하지 않는
strict 정상 경로다. 세 phase 모두 `control_code_counts == {}`,
`transport_error_counts == {}`, `violation_counts == {}`, `failed == 0`이어야 report가
통과한다. `admission_c8`만 overload/timeout의 안전한 제어 응답을 허용하는 참고 단계이며,
c1·c2·c4 정상 경로 성능 판정을 대신하지 않는다.

cold와 warm을 섞어 하나의 percentile로 만들지 않는다.

- cold: container `StartedAt` 또는 명시한 cold-start epoch부터 첫 HTTP와 ready까지
- warm: readiness와 명시적인 warm-up이 끝난 뒤의 c1·c2·c4
- 각 warm phase: p50·p95·p99와 max, RPS, body bytes, 오류/제어 코드

부하 단계 사이에 container가 재시작되거나 image/container ID가 바뀌면 전체 실행을 폐기한다.

## 5. 장시간 soak test

soak는 c1·c2·c4 각각 별도 실행하는 것을 기본으로 한다. 한 실행에서 concurrency를 바꾸지
않아야 post-warmup plateau와 누수 판단이 명확하다. 최소 60초 warm-up 뒤 15분을 측정하는
예시는 다음과 같다.

```bash
for CONCURRENCY in 1 2 4; do
  PYTHONPATH=fastapi_backend python3.12 \
    fastapi_backend/scripts/deterministic_api_soak.py \
    --base-url "http://127.0.0.1:$PERF_PORT" \
    --container-name "$PERF_CONTAINER" \
    --concurrency "$CONCURRENCY" \
    --warmup-seconds 60 \
    --duration-seconds 900 \
    --sample-interval-seconds 5 \
    --request-timeout-seconds 60 \
    --output "$PERF_ROOT/results/soak-c$CONCURRENCY.json"
done
```

고정 질문이 기본 bond 질문과 다르면 `--question-id`, `--question`, `--expected-status`,
`--expected-intent`, `--expected-family`를 함께 명시한다. 보고서는 질문 원문 대신 SHA-256만
보존한다.

soak의 `runtime_passed=true`만으로 최종 성공으로 확정하지 않는다. 이 값은 HTTP 계약,
timeout/error, container identity, memory plateau, FD/thread/PID 변화뿐 아니라 post-load
`/health`, Docker의 dead/runtime error/unhealthy 상태를 검사한다. 첫 번째 정상 측정 응답을
baseline으로 고정해 canonical response fingerprint, payload bytes, citation/evidence 개수가
끝까지 같은지도 확인한다. 측정 종료시각 뒤 완료된 요청은 percentile과 성공 건수에서 제외하고
`late_completions_excluded`로 별도 집계한다.
최종 `complete_success_gate_passed`는 종료 후 Audit queue/sequence 검증까지 통과해야 충족되는
상위 게이트다.

plateau 판단은 단순히 시작과 끝 두 점만 비교하지 않는다. warm-up 이후 첫/마지막 사분위의
memory median 차이와 후반부 slope를 함께 보고, 최소 네 개의 post-warmup runtime sample이
있어야 한다. 다음 현상이 하나라도 있으면 실패다.

- memory가 허용 범위를 넘어 계속 증가하거나 후반 slope가 양의 누수 형태를 보임
- file descriptor, thread 또는 PID 수가 허용 범위 밖으로 증가함
- container restart, 교체, OOM kill 또는 runtime probe 실패
- timeout, transport error, contract/semantic violation 또는 admission control 발생
- payload/evidence count가 기대 범위를 벗어나거나 동일 질문의 응답 fingerprint가 바뀜

## 6. Audit 구간 분석

Audit 파일은 종료로 queue flush가 끝난 뒤 분석한다. benchmark가 한 고정 질문으로
`warm_c1=100`, `warm_c2=100`, `warm_c4=80`, `admission_c8=80` 순서로 실행되었다면,
가장 빈번한 enriched request hash를 자동 선택하는 legacy phase 문법을 사용할 수 있다.
strict 성공 판정에는 앞의 세 warm phase만 포함한다.

```bash
PYTHONPATH="finance_agent/packages/finance_agent_core/src:fastapi_backend" \
python3.12 fastapi_backend/scripts/deterministic_performance_analysis.py \
  --audit-jsonl "$PERF_ROOT/audit/events.jsonl" \
  --phase warm_c1=100 \
  --phase warm_c2=100 \
  --phase warm_c4=80 \
  --stage4-baseline-summary \
    finance_agent/evaluation/baselines/deterministic-api-stage4-final-2026-08-13.json \
  --require-complete-instrumentation \
  --output "$PERF_ROOT/results/performance-analysis-strict.json"
```

`admission_c8`의 overload/timeout 분포가 필요하면 별도 참고 report를 만든다. 이 명령은
c8의 제어 응답을 관측하기 위한 것이므로 strict 성공 게이트로 사용하지 않는다.

```bash
PYTHONPATH="finance_agent/packages/finance_agent_core/src:fastapi_backend" \
python3.12 fastapi_backend/scripts/deterministic_performance_analysis.py \
  --audit-jsonl "$PERF_ROOT/audit/events.jsonl" \
  --phase warm_c1=100 \
  --phase warm_c2=100 \
  --phase warm_c4=80 \
  --phase admission_c8=80 \
  --output "$PERF_ROOT/results/performance-analysis-c8-reference.json"
```

Audit에 smoke, soak 또는 여러 질문이 함께 섞였거나 phase 사이 다른 호출이 존재하면 offset을
추정하지 않는다. 아래 schema의 phase map을 별도 JSON으로 만들고 정확한 request hash,
skip, count를 기록한다.

같은 `request_id_sha256`를 쓰는 phase range는 서로 겹치면 안 된다. overlap이 있으면 analyzer가
phase label 오염을 막기 위해 분석 전에 실패한다.

```json
{
  "schema_version": "1.0",
  "phases": [
    {
      "name": "warm_c1",
      "request_id_sha256": "<64-lowercase-hex>",
      "skip_invocations": 0,
      "invocation_count": 100
    },
    {
      "name": "warm_c2",
      "request_id_sha256": "<same-64-lowercase-hex>",
      "skip_invocations": 100,
      "invocation_count": 100
    }
  ]
}
```

```bash
PYTHONPATH="finance_agent/packages/finance_agent_core/src:fastapi_backend" \
python3.12 fastapi_backend/scripts/deterministic_performance_analysis.py \
  --audit-jsonl "$PERF_ROOT/audit/events.jsonl" \
  --phase-map "$PERF_ROOT/results/phase-map.json" \
  --require-complete-instrumentation \
  --output "$PERF_ROOT/results/performance-analysis-with-map.json"
```

`instrumentation_coverage.complete=true`인지 먼저 확인한다. 그 다음 c1·c2·c4 각각에서
non-overlapping segment의 p50·p95·p99를 비교한다. verifier가 우세하면 다시
projection connection/fetch, row materialization, universe total, pure verifier를 나눠
본다. 합성 가능한 nested timer와 상위 timer를 중복 합산해서 전체 응답 시간으로 해석하지
않는다.

예를 들어 `verifier_total`과 `verifier_row_materialization`은 포함 관계다. 병목 순위는
non-overlapping 상위 구간을 사용하고, 하위 구간은 해당 상위 구간의 원인을 설명하는 데만
사용한다. 새 c2에서 materialization이 verifier 대부분을 차지하지 않는다면 현재 97% 가설은
기각하고, 실제 dominant segment를 새 기준으로 삼는다.

## 7. Audit fsync 비용 분리

격리 API Audit와 같은 호스트·filesystem에 owner-only probe 디렉터리를 준비한다. probe는
두 개의 새 JSONL을 생성하므로 같은 run ID로 재실행하지 않는다.

```bash
mkdir -m 700 "$PERF_ROOT/fsync-probe"

PYTHONPATH="finance_agent/packages/finance_agent_core/src:fastapi_backend" \
python3.12 fastapi_backend/scripts/audit_fsync_probe.py \
  --directory "$PERF_ROOT/fsync-probe" \
  --warmup-events 20 \
  --measured-events 500 \
  --run-id "$PERF_RUN_ID" \
  --output "$PERF_ROOT/results/audit-fsync-probe.json"
```

`append_and_fsync_latency_ms`와 `write_only_latency_ms`의 p50·p95·p99 차이를 본다. 이 값은
동일 filesystem의 동기 내구성 비용 추정치다. HTTP latency에 대한 영향은 Audit queue depth,
drop/downstream error, shutdown flush 결과와 함께 해석한다.

## 8. clean Stage 4와 candidate 정확성 비교

기준은 clean Git commit `ea380ed`다. 현재 Stage 4 summary에는 원본 응답 fingerprint가 없으므로
summary 파일만으로 “변경 전 응답과 정확히 같다”를 증명할 수 없다. 다음 두 이미지를 같은 고정
질문과 동일 benchmark 옵션으로 각각 새로 실행해야 한다.

1. 별도의 clean checkout 또는 detached worktree에서 `ea380ed` image를 빌드한다.
2. candidate checkout에서 candidate image를 빌드한다.
3. 두 이미지를 같은 project/container/port 이름으로 동시에 실행하지 않는다.
4. 동일 자원 제한, 동일 데이터, 동일 질문 ID·질문·기대값, 동일 요청 수로 순차 실행한다.
5. 각 실행의 benchmark JSON과 Audit JSONL을 별도 owner-only 경로에 보존한다.

working tree의 기존 변경을 지우거나 `git reset --hard`로 clean baseline을 만들지 않는다. 별도
clean checkout/worktree를 사용한다. 예시 디렉터리 구조는 다음과 같다.

```text
/tmp/finance-perf-compare/
  stage4/benchmark.json
  stage4/events.jsonl
  candidate/benchmark.json
  candidate/events.jsonl
```

candidate Audit을 분석하면서 두 benchmark report를 함께 넘기면 semantic smoke case와 각
phase/outcome의 canonical response SHA-256을 정확히 비교할 수 있다.

```bash
PYTHONPATH="finance_agent/packages/finance_agent_core/src:fastapi_backend" \
python3.12 fastapi_backend/scripts/deterministic_performance_analysis.py \
  --audit-jsonl /tmp/finance-perf-compare/candidate/events.jsonl \
  --phase warm_c1=100 \
  --phase warm_c2=100 \
  --phase warm_c4=80 \
  --baseline-benchmark-report /tmp/finance-perf-compare/stage4/benchmark.json \
  --candidate-benchmark-report /tmp/finance-perf-compare/candidate/benchmark.json \
  --require-complete-instrumentation \
  --require-fingerprint-match \
  --output /tmp/finance-perf-compare/candidate/performance-and-fingerprint.json
```

`response_fingerprint_comparison.exact_match=true`와 `benchmark_audit_binding.passed=true`가
모두 필요하다. analyzer는 candidate Audit의 질문 ID·질문 SHA-256과 strict warm phase의
이름·순서·invocation 수를 benchmark report에 묶는다. changed, missing, candidate-only key 또는
binding mismatch가 하나라도 있으면 성능이 빨라졌더라도 정확성 동일성 게이트는 실패다.
Stage 4 summary의 기존 p95는 참고선으로만 사용하고, baseline/candidate 비교의 주 결과는 이번
동일 조건 재실행에서 가져온다.

## 9. 종료 후 Audit queue·sequence 검증

컨테이너를 정상 종료한 뒤 Audit validation CLI로 원본 JSONL schema, 요청 lifecycle,
event_sequence, 실행 경로, 민감 원문 노출, release/dataset linkage를 검증한다. 출력 report와
commitment는 기존 파일을 덮어쓰지 않는 절대 경로여야 한다.

```bash
PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python3.12 -m finance_agent_core.audit_validation_cli \
  --audit "$PERF_ROOT/audit/events.jsonl" \
  --report "$PERF_ROOT/results/audit-validation-report.json" \
  --commitment "$PERF_ROOT/results/audit-validation-commitment.json"
```

local evaluation release와 deployment binding을 만든 실행에서는 linkage를 필수로 건다.

```bash
PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python3.12 -m finance_agent_core.audit_validation_cli \
  --audit "$PERF_ROOT/audit/events.jsonl" \
  --report "$PERF_ROOT/results/audit-release-report.json" \
  --commitment "$PERF_ROOT/results/audit-release-commitment.json" \
  --release-manifest /absolute/path/agent-release-manifest.json \
  --deployment-binding /absolute/path/deployment-binding.json \
  --expected-binding-sha256 <64-lowercase-hex> \
  --require-release-linkage \
  --require-dataset-linkage
```

CLI exit code 0과 report status `passed`가 모두 필요하다. 특히 다음을 확인한다.

- 매 invocation에 START와 terminal request event가 정확히 존재함
- event sequence가 1부터 연속이며 누락·중복이 없음
- queue drop, flush timeout, downstream error가 없음
- Router → QueryPlan → Oracle → Verifier → Answer 실행 경로가 완전함
- 질문, prompt, credential, 응답 원문이 Audit에 노출되지 않음
- release/source/backend/image hash와 dataset/DB fingerprint가 기대 binding과 일치함
- report가 원본 Audit SHA-256을 포함하고 commitment가 report SHA-256을 고정함

Audit 파일이 아직 열려 있거나 queue가 drain되지 않은 상태에서의 통과는 인정하지 않는다.

## 10. 최종 성공 게이트

한 실행은 아래 조건을 모두 만족할 때만 성공이다.

| 영역 | 필수 조건 |
|---|---|
| 격리 | 공유 `127.0.0.1:18001`·`:18002` 무접촉, 고유 project/container/port/volume, 동일 자원 제한 |
| cold/warm | cold 별도 기록, warm c1·c2·c4 각각 p50·p95·p99 기록 |
| 안정성 | timeout, overload, transport/contract/semantic 오류 모두 0 |
| 응답 | payload byte와 citation/evidence reference count 기록, 비정상 증가 없음 |
| 자원 | OOM/restart 없음, FD/thread/PID 누수 없음, warm-up 뒤 memory plateau 형성 |
| 계측 | 모든 필수 세부 segment의 invocation coverage 완전 |
| 정확성 | clean `ea380ed`와 candidate의 canonical response fingerprint exact match |
| Audit | 종료 후 lifecycle·sequence·queue·민감정보·실행경로 검증 통과 |
| Release | local evaluation release hash, dataset hash, DB fingerprint linkage 통과 |
| 고정성 | Audit SHA-256, validation report SHA-256, commitment SHA-256 보존 |

성능 개선 판정은 정확성과 안정성 게이트가 먼저 통과한 경우에만 한다. p95만 보지 않고
c1·c2·c4의 p50·p95·p99, RPS와 dominant segment를 함께 비교한다. 한 concurrency에서만
빨라지고 c4 tail이나 memory slope가 악화되면 개선으로 채택하지 않는다.

## 11. 결과 해석 순서

1. 격리/identity/resource-limit 증거가 없으면 실행을 폐기한다.
2. timeout·오류·overload·restart·OOM이 하나라도 있으면 성능 숫자를 성공 결과로 쓰지 않는다.
3. Audit instrumentation coverage와 종료 후 sequence/queue 검증을 확인한다.
4. clean Stage 4와 candidate fingerprint exact match를 확인한다.
5. c1 → c2 → c4 순으로 p50·p95·p99 증가율을 계산한다.
6. non-overlapping 상위 segment에서 dominant p95를 찾는다.
7. dominant가 Verifier이면 projection fetch/materialization/pure verifier 하위 구간을 확인한다.
8. fsync probe와 Audit queue 상태를 함께 보고 저장 비용의 영향 범위를 판단한다.
9. soak의 memory tail slope와 FD/thread/PID 변화를 확인한다.
10. 모든 게이트를 통과한 뒤에만 Dense 추가 전 기준선 또는 개선 후보로 고정한다.

현재의 약 97% Verifier 진단은 우선순위를 정하는 근거이지 결론이 아니다. 강화된 세부 계측과
동일 조건 baseline/candidate replay가 다른 병목을 가리키면 새 측정을 우선한다.
