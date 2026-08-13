# Deterministic API baseline v1

이 도구는 **별도 Docker container**의 실제 HTTP 경로를 측정한다. 공유 compose project에는
사용하지 않는다. HCLX, Dense Schema Linker, Product Dense가 모두 꺼진 deterministic
기준선이 대상이다.

## 무엇을 분리해 측정하는가

- `cold_start`: runner 또는 Docker `StartedAt`부터 최초 HTTP와 `ready`까지 걸린 시간
- `health_ready`: `/health`의 HTTP 상태, latency, body bytes, canonical SHA-256
- `post_load_health`: 모든 부하 단계가 끝난 직후의 단일 `/health` readiness 재검사.
  HTTP/service/audit 상태, latency, body bytes, canonical SHA-256, 위반 코드만 남기며
  cold-start용 attempt·origin·time-to-ready 필드는 저장하지 않는다.
- `semantic_contract`: 기존 backend·공식 GET smoke의 의미·DTO 계약
- `warm_c1`, `warm_c2`: concurrency 1·2의 p50/p95/p99/max, RPS, body bytes
- `admission_c4`, `admission_c8`: concurrency 4·8에서 같은 값과
  `request_overloaded`/`request_timeout` 안전 제어 응답
- `memory`: cgroup-v2 `memory.current/peak/max/events`와 단계 전후 차이
- `container`: Docker 상태, exit code, OOMKilled, health, container/image 동일성

응답 원문과 질문 원문은 보고서에 저장하지 않는다. 질문과 canonical JSON 응답은
SHA-256(내용 지문)만 저장한다. 같은 outcome(예: `status:success`)에서 응답 지문이 달라지면
deterministic 기준선은 실패한다.

## 실행 예시

격리 container를 띄운 직후 별도 shell에서 실행한다. 아래의 container 이름과 port는
실제 격리 대상 값으로 바꾼다.

```bash
PYTHONPATH=fastapi_backend python3.12 -m scripts.deterministic_api_benchmark \
  --base-url http://127.0.0.1:18081 \
  --container-name finance-agent-baseline-candidate \
  --cgroup-path /sys/fs/cgroup/EXACT_CONTAINER_CGROUP \
  --warm-requests 20 \
  --stress-requests 24 \
  --required-control-code request_overloaded \
  --require-runtime-metrics \
  --output /tmp/deterministic-api-baseline-v1.json
```

`--output`은 기존 파일을 덮어쓰지 않고 권한 `0600`인 새 파일만 만든다.
`--container-name`은 read-only `docker inspect`만 수행한다. cgroup 경로는 반드시 격리
container의 정확한 경로를 넘겨야 하며, 생략하면 memory 값은 `configured=false`로 남는다.
최종 기준선에서는 `--require-runtime-metrics`를 사용해 cgroup·container 관측 누락도 실패로
처리한다.

`request_timeout`은 정상적으로 빠른 deterministic 경로에서 일부러 만들지 않는다.
전용 지연 fixture를 사용한 별도 격리 실행에서 실제 HTTP 200 +
`control_code=request_timeout`을 관측할 때만
`--required-control-code request_timeout`을 추가한다. 관측하지 않은 timeout을 성공으로
꾸며 기록해서는 안 된다.

## 성공 기준

- 네 상품군 health가 정확히 ready이며 선언한 fund policy와 일치한다.
- 모든 부하가 끝난 뒤에도 `post_load_health`가 ready여야 한다. 특히 audit sink의
  drop·flush·downstream 오류로 `audit_status=degraded`가 되면 전체 기준선은 실패한다.
- 공개 semantic/contract smoke가 모두 통과한다.
- warm 응답에 overload/timeout이 없고, 의미·공식 다섯 문자열 계약이 모두 일치한다.
- admission stress의 overload/timeout은 HTTP 200, `status=error`, citation 없음 계약이다.
- 각 outcome의 canonical response hash가 하나뿐이다.
- cgroup `oom`, `oom_kill` 증가가 없고 container가 교체·재시작·종료되지 않는다.
- 사용자가 `--required-control-code`로 지정한 제어 코드가 실제로 관측된다.

이 보고서는 한 호스트의 공개 모의 측정이다. external blind 정확도나 NCP 운영 성능을
대신하지 않는다.
