# Stage 4 감사·blind 리허설·image·API 기준선 보고서

- 기준일: 2026-08-13
- 기준 commit: `836828e346aa77e10f76225c3c038aeddb441be1`
- 결론: development mode의 결정론적 Fast Path(HCLX·Dense 없이 SQL/Python으로 처리하는
  경로)에서 HTTP→Core→최종 ASGI body send 경계 감사와 Docker 기준선을 검증 완료
- 배포 판단: **활성화 보류** — dirty candidate이며 외부 blind·NCP release가 아님

> 후속 기록: Stage 4·5 동결 단위는 `ea380ed9774a7bedeb2ede9e867d214cfbf9b318`로
> `origin/hyunhoim`에 push했고, 같은 clean commit의 local BuildKit OCI index digest
> `sha256:a147b58fd7a6c58fbec3d2a222163f027cef5cb6309eec5f3bde0e8f0313aa3d`로 smoke를
> 재검증했다. 이는 NCP Registry RepoDigest나 서명된 release가 아니다. 그 뒤의 Shadow
> correlation·metrics·readiness·shutdown 변경은 별도 미커밋 후속 단위다.

## 1. 이번에 완료한 범위

Stage 4 관측·감사는 요청이 어떤 안전·계획·실행·검증 경계를 거쳤는지 원문 없이
재구성하는 기능이다. 이번 development-mode HTTP 기준선에서 실제로 실행·검증한 범위는
다음과 같다.

- HTTP `/answer` 수신과 마지막 ASGI response body frame의 downstream send 수락·중단
  (클라이언트 또는 Load Balancer 수신 확인은 아님)
- SafetyEnvelope, Lexical, PlanningDecision, 최종 RouteDecision
- Compiler, PlanAuthority, parameterized SQL, Oracle
- Result/Aggregate/Comparison Verifier, Renderer, Answer Verifier
- 단일·교차 상품군 plan hash 연결

HCLX emit 분기와 release·deployment binding·승인 dataset hash 필드는 코드·계약 테스트로
확인했지만 이번 실기동에서는 HCLX가 OFF이고 app_env가 development라 실행·관측하지
않았다. Schema Dense/Product Dense도 OFF였고 Schema Shadow event는 아직 HTTP/Core
invocation 연속열에 연결되지 않았다. 따라서 이 결과는 모든 provider·mode의 전체 E2E
감사 검증이 아니다.

한 서버 invocation마다 서버 UUID의 SHA-256과 증가하는 `event_sequence`를 사용한다.
HTTP START → Core → HTTP terminal이 같은 invocation으로 이어지고 ROUTE와 ANSWER는
각각 최대 한 번만 기록된다.
단, timeout·client cancellation 후 동기 worker가 정리되는 동안 Core event가 HTTP
terminal 뒤에 기록될 수 있다. 따라서 HTTP terminal은 전송 경계의 종료이지
항상 invocation 전체의 마지막 event라는 뜻은 아니다.

## 2. 운영 안전 보완

- 후보 image의 Uvicorn access log를 꺼 애플리케이션 서버 자체가 공식 GET URL/query
  string을 access log로 남기지 않게 했다. NCP Load Balancer·reverse proxy·WAF·APM 등
  upstream 계층의 query string 수집·redaction은 배포 전 별도 검증이 필요하다.
- POST 422, overload, timeout, send 실패·취소도 terminal REQUEST event로 남긴다.
- JSONL sink 자체 쓰기는 owner-only file descriptor에서 O_APPEND로 수행하고 event·신규
  directory entry를 `fsync`한다. 이는 ordering·crash durability를 보완하지만 같은
  UID/root의 rewrite를 막거나 hash chain·서명·WORM을 제공하지 않으므로 tamper-evident
  (위변조 증명 가능) 저장소는 아니다.
- 짧은 write·InterruptedError를 재시도하고, queue drop·저장 실패·flush 실패를 구분한다.
- 감사 event가 하나라도 유실되면 `/health`가 재시작 전까지 `degraded`로 유지된다.
- 종료 시 앞선 drop이 있으면 `flush_succeeded=false`로 남긴다.
- 실제 timeout이 `RoutedExecutionError`에 감싸져도 Core ANSWER를 `timed_out`으로 분류한다.
- evaluation/production은 `WEB_CONCURRENCY=1`만 허용한다. 프로세스 간 감사 집계가
  없는 상태에서 여러 worker가 서로의 장애를 가리는 문제를 차단하기 위함이다.
- release audit host directory는 backend 고정 UID `10001` 소유·owner-only여야 한다.

감사 오류는 개별 Agent 답변을 바꾸거나 `/answer`를 자체 차단하지 않는다. `/health` HTTP
503은 외부 orchestrator·Load Balancer용 readiness 신호다. 실제 트래픽 제외는 해당 장비가
이 health check와 drain 정책을 사용해야 하며, 이번 로컬 기준선에서는 자동 제외를
검증하지 않았다.

## 3. external blind 모의 리허설

외부 파일을 받기 전 절차만 검증하는 합성 100문항 리허설을 실행했다.

- 상태: `internal_synthetic_not_blind`
- 네 상품군 각 25문항, 일곱 intent 합계 100문항
- 절차 mechanics score: `1.0`
- 외부 독립성: `false`
- 실제 embedding model 추론: `false`
- control 문항의 operational Dense 호출 0회
- 질문·정답·commitment·authorization·prediction·receipt hash 결합 검증 통과
- 공식 question loader가 합성 envelope를 거부하는 계약 통과
- report SHA-256:
  `0abc55779a519b017699b7a3c987ed6515336acf7f59ad4506d6a1512cfdcb6c`

이 결과는 절차와 파일 무결성만 확인한다. 실제 외부 독립성도 없고 실제 embedding
추론도 하지 않았으므로 BGE-M3·KURE-v1 성능이나 모델 선정 근거로 사용할 수 없다.

## 4. Docker 후보 image

### 4.1 성능 기준선 측정 image

5절의 374건 성능 기준선은 다음 시점의 dirty image로 측정했다.

- local image tag: `finance-agent-stage4-audit-candidate-postfix:20260813`
- local image ID:
  `sha256:e7caffbb70a827465eb46315db261f01c02f8f26bef1673513322bea582a5fb9`
- runtime source snapshot:
  `cd9fca900f849f87885a3d0e6a3dbb5ff25090270529f5ccc910ba92782c21fc`
- image size: 59,992,907 bytes

이 image는 이후의 crash-tail·HTTP 분류·rollback audit 보완 전 기준선이다.
따라서 수치는 역사적 측정값으로 보존하되, 현재 코드의 OCI 증거로 확대
해석하지 않는다.

### 4.2 최종 동결 검토 smoke image

최종 코드 보완과 1,219/246 회귀 통과 후, 현재 작업트리로 새 지역 후보를
빌드해 `linux/amd64`, non-root, read-only root filesystem, read-only DB mount를
다시 확인했다.

- local image tag: `finance-agent-stage4-freeze-final-candidate:20260813`
- local BuildKit OCI index digest/image ID:
  `sha256:8ae3d37a267fdfff8ef979ffeee330a783d0711a37520018ef0b45cf5ac8c73e`
- runtime source snapshot:
  `d5b0dc37c23426f83682273047a378bf20860e10a4b2e93a1949a3d8a891069d`
  (AgentReleaseManifest와 같은 runtime-tree 방식으로 Core 223개·Backend 12개를 각각
  hash한 뒤 두 component hash를 canonical JSON으로 결합)
- source base commit: `836828e346aa77e10f76225c3c038aeddb441be1`
- source label: `dirty-stage4-freeze-final-candidate`
- base image:
  `python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`
- image size: 60,024,968 bytes
- runtime user: `app` (UID/GID 10001)

일반 smoke에서 `/health` HTTP 200·네 상품군 ready와 대표 `/answer` HTTP
200·deterministic SEARCH·`fallback_used=false`를 확인했다. 별도 owner-only
감사 volume을 사용한 audit-ON smoke에서는 단일 요청의 13개 event가 연속
sequence로 기록되고, 파일 mode `0600`, raw request·question 미저장을 확인했다.
격리 container와 volume은 검증 후 제거했고 공유 container는 재시작·변경하지
않았다.

두 image 모두 build 시점의 작업트리가 dirty이므로 clean Git release gate를
통과할 수 없다. 위 SHA-256은 지역 Docker 저장소의 digest이며 NCP Registry가
반환한 RepoDigest가 아니다. 따라서 AgentReleaseManifest·Registry push·cosign과
연결된 clean release로 취급하지 않는다. 사용자 승인 전에 임의 commit을
만들지 않았다.

## 5. 결정론적 API 기준선

격리 container에서 2 CPU, 1GiB, worker 1, max inflight 2, read-only DB/root,
JSONL event별 `fsync`, HCLX·Qwen·Dense OFF로 실제 HTTP를 측정했다.

| 단계 | 요청 | p50 | p95 | p99 | max | RPS | 해석 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| container start→ready | 1 | 14,342.287ms | - | - | - | - | 첫 HTTP 200 ready까지 |
| initial `/health` | 1 | 47.942ms | - | - | 47.942ms | - | HTTP 200 |
| warm c1 | 100 | 521.065ms | 695.828ms | 777.253ms | 816.853ms | 1.847 | 정상 100/100 |
| warm c2 | 100 | 1,434.811ms | 1,773.783ms | 1,924.028ms | 1,955.361ms | 1.248 | 정상 100/100 |
| admission c4 | 80 | 524.738ms | 1,894.731ms | 1,980.782ms | 2,029.325ms | 2.375 | 정상 40, overload 40 |
| admission c8 | 80 | 15.779ms | 1,613.948ms | 1,699.312ms | 1,754.102ms | 5.032 | 정상 20, overload 60 |
| post-load `/health` | 1 | 5.731ms | - | - | 5.731ms | - | HTTP 200, audit `ok` |

- benchmark 평가 대상 `/answer` 요청 374/374 계약·의미 통과
- 공개 smoke 14/14 통과
- transport 오류·계약 위반·의미 위반 0
- overload 100건 모두 HTTP 200의 안전 제어 DTO, citation 없음
- 같은 outcome의 canonical response SHA-256 단일값 유지
- 부하 후 `/health`: HTTP 200, 5.731ms, service `ok`, audit `ok`
- cgroup peak memory 151,232,512 bytes, 부하 후 current 148,353,024 bytes, OOM·OOM kill 0
- container ID·image ID·start time 유지, identity stable·safe, restart 0

메모리는 초기 약 49.1MB에서 부하 후 148.35MB로 증가했다. 따라서 이 실행은 374개
요청 구간의 OOM 부재만 증명하며, 장시간 soak test(오래 실행해 누수 여부를 보는 시험),
memory plateau(메모리 사용량 안정화), leak 부재는 아직 증명하지 않는다.

감사 JSONL은 mode `0600`, 5,762,330 bytes이며 3,672 event·374 invocation을 전수
파싱했다. 최대 event는 6,255 bytes였고 schema validation, invocation별 sequence
연속성, REQUEST START/terminal 쌍, ROUTE·ANSWER 최대 한 건 계약을 모두 만족했다.
실행 가능한 plan을 연결한 event는 1,848건이었다.
정확히 금지된 sensitive key와 `/home` fragment는 각각 0건이었다. 단, 이는 생성된
JSONL의 schema·redaction scan 범위이며 임의의 모든 개인정보·secret이나 upstream 로그
부재를 증명하지 않는다. 또한 development Fast Path라 release·승인 dataset
linkage event는 0건이며, evaluation/production linkage 검증 근거가 아니다.

- audit JSONL SHA-256:
  `e692da9f69caea36dbc32db0215de22d4e8c53381016e58b38da001053d5eac9`
- benchmark report SHA-256:
  `0f6321f55ac91314e4e93829ad47309c2e0159fb9d8b0ed09970fcd108f70f85`

전체 요약은
[결정론적 Stage 4 API baseline](../evaluation/baselines/deterministic-api-stage4-final-2026-08-13.json)에
고정했다. 전체 원본 report와 JSONL은 민감 원문을 포함하지 않더라도 Git에 넣지 않는다.

## 6. 회귀 결과

| 검증 | 결과 |
| --- | ---: |
| Agent Core 전체 + 승인 네 SQLite read-only | 1,219 passed |
| FastAPI Backend 전체 | 246 passed, 기존 fork warning 2건 |
| Ruff lint·format | 통과 |
| `git diff --check`, `bash -n` | 통과 |

기존 Stage 3의 2026-08-12 baseline JSON 두 개는 바이트를 수정하지 않았다.
Stage 4로 교체된 component hash의 예외는 frozen evidence(동결 증거) 안에
쓰지 않고, `check-docs.py`에 baseline 파일명·component 경로를 정확히 고정한
allowlist로 분리했다. 예상하지 않은 하나의 hash 차이나 동결 JSON 내부의
우회 field는 문서 검사를 실패시킨다.

공유 `hyunholim-finance-agent` container와 127.0.0.1:18001은 재시작·변경하지 않았다.
측정 container는 별도 port와 이름으로 실행한 뒤 종료·삭제했다.

위 1,219/246은 crash-tail·HTTP 4xx/timeout 분류·audit-aware rollback 보완까지 반영한
최종 동결 재검증 수치다. 앞서 생성한 dirty candidate API baseline JSON의 1,203/209는
그 측정 시점의 역사 수치로 유지하며, 현재 clean commit의 OCI 기준선으로 재해석하지 않는다.

## 7. 최종 판단과 남은 gate

Stage 4 **결정론적 Fast Path의 코드 배선과 로컬 후보 기준선은 GO**다. HCLX·Dense·
evaluation release/dataset linkage를 포함한 전체-mode 감사, 배포, Dense 활성화는 다음이
남아 있어 **NO-GO**다.

1. 변경을 승인된 clean commit으로 만든 뒤 AgentReleaseManifest와 새 OCI digest 재생성
2. NCP Registry push digest·platform·label·cosign trust 검증
3. 외부 작성 100문항·비공개 정답키·외부 append-only receipt로 최초 blind 1회 실행
4. evaluation mode에서 승인 dataset hash가 실제 AuditEvent에 연결되는지 검증
5. 실제 NCP CPU·memory·disk에서 같은 API benchmark 재측정
6. c2 p95 1.774초의 원인을 profile하고 payload·SQLite·fsync 비용을 분리 측정
7. 장시간 soak test로 memory plateau와 leak 부재 검증
8. NCP Load Balancer의 `/health` 503 트래픽 제외와 GET query logging/redaction 실환경 검증
9. 감사 증거가 필요하면 hash chain·서명 checkpoint·외부 WORM 중 승인 trust anchor 적용

Stage 5 Shadow는 현재 OFF인 비배포 실험 기능이다. 후속 변경에서 비동기 Shadow event의
HTTP/Core invocation 연결, 전용 bounded metrics, queue·operational·correlation·audit emit
장애 readiness, 단일 deadline 종료 계약을 구현했다. 그래도 실제 모델 Shadow Docker
동시성·p95·메모리와 external blind를 통과하기 전에는 evaluation/production 활성화를
승인하지 않는다.
