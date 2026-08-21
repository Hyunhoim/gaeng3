# Stage 3 deterministic·HCLX rollback drill

이 drill은 현재 운영 중인 `hyunholim-finance-agent` Compose project를 사용하지 않는다.
사용자가 명시한 `finance-agent-rollback-drill-...` 전용 project와 포트에서만 다음 순서를
검증한다.

```text
generation N-1 기동·health 확인
  → 대표 /answer 확인
  → release·dataset·QueryPlan에 연결된 감사 체인 확인
  → generation N 기동·health·대표 /answer 확인
  → N의 감사 체인 확인
  → N-1 image·DB volume 보존 확인
  → 동일 Binding으로 N-1 재기동·health·대표 /answer 확인
  → 복귀한 N-1의 감사 체인 확인
  → N image·DB volume 보존 확인
```

각 기동에서는 실행 container의 image reference, `/data` volume,
`DeploymentBinding` mount가 해당 release와 정확히 일치하는지도 검사한다. 대표
공식 `GET /answer?question_id=...&question=...`는 다섯 필드가 모두 문자열인지 확인하고,
국내채권 SEARCH 한 건이 `status=success`, `intent=search`인지 검사한다. deterministic
release는 `answer_mode=deterministic`, HCLX release는 `answer_mode=llm_grounded`,
`fallback_used=false`여야 한다. HCLX 오류가 결정론적 fallback으로 가려져도 drill 성공으로
인정하지 않는다.
각 probe 뒤에는 owner-only append-only JSONL에서 같은 invocation의
Request→Safety→Lexical→Planning→Route→Compiler→Authority→SQL→Oracle→Verifier→
Renderer→Answer→Serialization(citation·Backend DTO·Official DTO·HTTP 응답)→Request 순서의
deterministic **25개 event**를 확인한다. HCLX grounded answer profile은 같은 실행·검증
경로 뒤에 HCLX generation과 Answer Verifier를 포함한 **27개 event**, HCLX QueryPlan과
grounded answer를 모두 켠 profile은 Compiler 뒤 QueryPlan HCLX까지 포함한 **28개 event**를
확인한다. 모든 profile은 연속 sequence, release·manifest·Binding hash,
승인 dataset·QueryPlan hash를 다시 확인한다. N-1과 N은 하나의 감사 디렉터리를 보존해야
하며 파일 교체·부분 JSON·중복 key·2 MiB 초과 신규 구간은 모두 차단한다.
이는 질문 전체의 품질 평가가 아니라 rollback 뒤 실제 Agent 경로가 동작하는지 확인하는
최소 smoke probe(대표 동작 점검)다.

실제 기동 전에는 cosign(서명 검증 도구)으로 각 release image와
`AgentReleaseManifest`·`DeploymentBinding`의 Sigstore bundle을 검증한다. 허용된
`main` release workflow identity로 서명되지 않았거나 Binding SHA-256·Manifest SHA-256·
image reference 연결이 다르면 container를 만들기 전에 중단한다.

검증 후에는 drill container와 network, 임시 env·Binding snapshot을 항상 정리한다.
`--volumes`와 `--rmi`는 사용하지 않으므로 두 release의 image와 DB volume은 rollback
근거로 의도적으로 남는다. 성공 기준의 “잔여물 없음”은 격리 container·network·임시
snapshot이 없다는 뜻이지 image·DB volume 삭제를 뜻하지 않는다.

## 준비물

- 연속된 generation의 read-only `DeploymentBinding` 두 개
- 각 Binding과 일치하는 `.env.release` 두 개
- 각 release의 `AgentReleaseManifest`, Manifest Sigstore bundle, Binding Sigstore bundle
- `/usr/local/bin/cosign`에 설치된 고정 `v3.1.3` 검증기
- 로컬 Docker에 존재하는 두 개의 digest-pinned release image
- 이미 적재·승인된 서로 다른 release-specific DB volume 두 개
- 각 release manifest와 같은 관계 artifact·index·세 상품 DB가 들어 있는 volume 및
  `.env.release`의 `FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256`
- UID `10001` 소유의 owner-only 감사 디렉터리 하나와 `WEB_CONCURRENCY=1`, event별 fsync
- HCLX release라면 UID `10001` 소유, group/other 권한 없음, hardlink·symlink가 아닌
  1~4096 byte API Key host 파일. container 경로는 `/run/secrets/clovastudio_api_key`로 고정
- 감사 JSONL을 읽을 수 있는 root 권한. UID `10001` 계정에 Docker socket과 모든 release
  artifact 읽기 권한을 별도로 부여한 환경이라면 그 계정으로도 실행할 수 있다.
- 다른 사용자나 기존 project가 사용하지 않는 localhost 포트

현재 release를 N, 직전 release를 N-1이라고 할 때 N의 `rollback` 필드는 N-1의 release,
manifest hash, Binding file hash, image digest, generation, environment, platform을 모두
정확히 고정해야 한다.

## 1. Dry-run

Dry-run은 Docker나 HCLX를 호출하지 않는다. 두 env·Binding의 hash와 rollback chain,
서로 다른 image·volume 이름, 격리 project·port, provider profile과 HCLX secret의
metadata만 검증한다. secret 값이나 그 hash는 읽거나 결과에 기록하지 않는다. cosign 서명과
실제 image·volume·API 동작은 `--execute`에서만 검증한다.

```bash
sudo -- python3 fastapi_backend/scripts/rollback_drill.py \
  --previous-env /absolute/release-n-1/.env.release \
  --current-env /absolute/release-n/.env.release \
  --project-name finance-agent-rollback-drill-team01 \
  --port 19081
```

성공 기준은 종료 코드 `0`과 JSON의 `status=validated`다. 이 결과만으로 image·volume의
실재나 실제 rollback 성공을 주장하면 안 된다.

## 2. 격리된 실제 Docker drill

Dry-run이 통과하고 명시한 포트가 비어 있을 때만 `--execute`를 추가한다. `--execute`는
감사 디렉터리·JSONL의 UID `10001`/owner-only 경계를 우회하지 않도록 root 또는 실제
effective UID `10001`만 허용한다. 일반 사용자 UID로 실행하면 release 파일이나 Docker를
읽기 전에 fail-closed로 중단한다.

```bash
sudo -- python3 fastapi_backend/scripts/rollback_drill.py \
  --previous-env /absolute/release-n-1/.env.release \
  --current-env /absolute/release-n/.env.release \
  --project-name finance-agent-rollback-drill-team01 \
  --port 19081 \
  --execute
```

성공 기준은 종료 코드 `0`, JSON의 `status=verified`,
`artifacts_preserved=true`, `containers_stopped_after_verification=true`,
`audit_chain_verified=true`와 세 개의 `audit_observations`다. Docker image나
volume이 없거나, cosign image/blob 검증이 실패하거나, 기존 drill project의
container/network가 발견되거나, 어느 generation 하나라도 healthy·대표 `/answer`·감사
연결 계약을 통과하지 못하면 fail-closed로 종료한다.

HCLX release는 세 번의 activation마다 실제 공식 GET probe를 수행하므로 grounded answer만
켠 profile은 최소 3회, QueryPlan도 켠 profile은 최대 6회의 과금 가능한 HCLX 호출이
발생한다. 따라서 `--execute`만으로는 HCLX release를 시작하지 않으며, 승인한 경우에만 다음
플래그를 함께 사용한다.

```bash
sudo -- python3 fastapi_backend/scripts/rollback_drill.py \
  --previous-env /absolute/release-n-1/.env.release \
  --current-env /absolute/release-n/.env.release \
  --project-name finance-agent-rollback-drill-team01 \
  --port 19081 \
  --execute \
  --allow-billable-hclx
```

HCLX release라도 startup은 네트워크 호출을 하지 않는다. 실제 GET에서 HCX-007 응답이 로컬
Answer Verifier를 통과해 `llm_grounded`가 되고, profile별 HCLX Audit event가 정확히 남아야
성공한다. timeout·인증 실패·429·5xx·schema 불일치·fallback은 모두 drill 실패다.

`--leave-running`은 immutable snapshot(실행 중 교체되지 않게 복사한 배포 파일)과 자동
정리 계약에 맞지 않아 명시적으로 거부한다. drill에서 검증한 N-1을 계속 실행하는 방식으로
실제 traffic을 전환하지 않는다. 운영 rollback은 별도 승인된 배포 절차로 다시 기동한다.

이 격리 drill runner는 production host의 active-state broker를 대신하지 않는다. 기계적인
N-1→N→N-1 복귀 가능성을 별도 project에서 확인하기 위해 같은 N-1 Binding을 재사용하지만,
실제 운영 host는 이미 활성 이력보다 오래된 서명 Binding을 replay로 차단한다. 운영 rollback은
복귀할 N-1 image·manifest·DB volume을 가리키면서도 현재 활성 값보다 정확히 1 큰 generation을
가진 **새 DeploymentBinding을 발급·서명**한 뒤 activation broker로 실행해야 한다.

## 안전 경계

- `hyunholim-finance-agent` project는 입력으로 허용하지 않는다.
- project override용 Compose 환경변수와 release identity shell 변수를 제거한다.
- `127.0.0.1:<명시한 포트>`로만 bind한다.
- 전역 prune, image 삭제, volume 삭제, 다른 project 조작을 수행하지 않는다.
- deterministic 25-event, HCLX answer-only 27-event, HCLX QueryPlan+answer 28-event의
  정적 감사 경로만 허용한다. QueryPlan-only HCLX profile은 아직 drill 대상이 아니므로 거부한다.
- deterministic profile은 HCLX model·provider·credential 설정이 있으면 거부한다.
- HCLX profile은 HCX-007과 고정된 Docker secret 경로만 허용한다. host secret은 값·hash·경로를
  성공 결과나 오류에 남기지 않고, 시작 전과 각 activation 뒤 metadata fingerprint 불변을 확인한다.
- runner는 감사 JSONL의 원문 record, 질문, request ID를 stdout/stderr에 출력하지 않는다.
  실패 메시지는 고정된 검증 경계만 알리며, 성공 결과에는 one-way hash와 event 수만 남긴다.
- 실제 traffic 전환과 NCP rollback은 별도의 배포 승인·load balancer 절차가 필요하다.
- production active-state 파일과 lock, launcher는 root-controlled 경로에 설치해야 하며
  새 host·재해복구 시 상태 복원은 외부 불변 archive 절차를 따른다.

현재는 연속된 두 개의 공식 clean NCP release image·Binding·Sigstore bundle·승인 DB
volume이 없으므로 실제 공식 `--execute`를 수행하지 않았다. 준비물이 생기기 전에도 아래
무외부 계약을 독립 검증한다.

```bash
pytest -q fastapi_backend/tests/test_release_rollback_drill.py
```

## 로컬 합성 실기동 기록 — 2026-08-12

localhost 일회용 Registry에 서로 다른 합성 N-1·N image를 실제 push하고, 각 release 전용
네 상품군 승인 DB volume과 Binding을 연결해 production `rollback_drill.py --execute`를
실행했다. `synthetic-nminus1-local12 → synthetic-current-local12 →
synthetic-nminus1-local12` 세 기동에서 exact image·volume·Binding, health, 대표
`/answer`가 모두 통과했고 자동 `down`도 확인했다.

공용 서버의 umask `0077`에서도 UID `10001`이 Binding을 읽도록 snapshot directory를
`0711`, Binding을 명시적 `0444`로 고정했고, container 진단 로그와 probe JSON을 구분하는
marker 계약도 이 실기동에서 검증했다. 합성 image·volume·Registry·임시 project는 검증 후
정확한 이름으로 삭제했으며 기존 `hyunholim-finance-agent`는 계속 healthy였다.

단, 합성 artifact에는 GitHub OIDC·cosign 서명이 없어서 `release_trust.py` 호출만 격리된
test stub으로 대체했다. 따라서 이 결과는 rollback mechanics(실제 복귀 동작) 검증이며,
NCP Registry·Sigstore 외부 trust anchor·운영 traffic 전환 검증은 아니다.

## P0-10 관계 검색 통합 기록 — 2026-08-20

현재 rollback runner는 두 release 환경 모두에 명시적
`FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256`을 요구한다. release Compose가 개발용 SHA
sidecar를 제거하므로, rollback 대상도 각 release manifest에 고정된 값과 같은 explicit
relation artifact SHA-256·release별 data volume을 가져야 한다. Backend startup과 `/health`가
artifact·index·세 상품 DB 결속을 다시 검사한다.

별도의 fresh Docker 통합에서는 관계 검색 data-init, exact 관계 질문 3건·근거 3건,
부분 관계명 0건, Backend 8/8·공식 GET 8/8 smoke와 index 1바이트 변조 후 health·관계
요청 HTTP 503을 확인했다. 그러나 rollback runner의 대표 probe 자체는 여전히 국내채권
상품 SEARCH 한 건이며, 관계 질문의 N-1→N→N-1 실행·관계 감사 chain을 검증한다고
주장하지 않는다.

특히 **보호된 GitHub Environment에서 발급·cosign 서명된 연속 두 NCP release**로
`rollback_drill.py --execute`를 실행한 적은 없다. 2026-08-12 기록은 서명 verifier를
test stub으로 바꾼 localhost 합성 mechanics 시험이고, 2026-08-20 기록은 단일 fresh
Docker 관계 통합·변조 시험이다. 따라서 실제 NCP signed rollback 완료 상태가 아니다.
