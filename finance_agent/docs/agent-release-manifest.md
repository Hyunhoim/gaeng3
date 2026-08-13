# AgentReleaseManifest 배포 계약

상태: Stage 3 애플리케이션·Docker·keyless release CI 계약 및 localhost 합성 rollback
실기동 완료 · 실제 NCP 발급·서명 검증·운영 rollback 대기

## 1. 목적

`AgentReleaseManifest`는 Agent의 코드·계약·Prompt·Model 설정·검색 index 상태·공식
데이터 snapshot을 하나의 `release_id`로 고정한다. 쉬운 말로, DB는 이전 버전인데
Prompt나 검색 규칙만 새 버전인 혼합 배포를 막는 명세서다.

Docker image digest를 manifest 안에 직접 넣으면 manifest가 바뀌고, 그 결과 image
digest도 다시 바뀌는 순환 참조가 생긴다. 그래서 배포 단위는 다음 두 파일로 구성한다.

- `AgentReleaseManifest`: image 안에 포함되는 내용물 명세
- `DeploymentBinding`: image push 뒤 확정된 `repository@sha256`와 manifest file hash를
  연결하는 외부 배포 명세

두 파일과 신뢰된 `DeploymentBinding` SHA-256을 함께 검증해야 하나의 배포 단위가 된다.

### Schema 1.1 전환

Stage 4에서 감사 runtime control이 `AgentReleaseManifest`의 필수 항목이 됐으므로 manifest
schema만 `1.0`에서 `1.1`로 올렸다. 기존 manifest `1.0`은 새 runtime에서 재사용하지 않고
clean source에서 다시 생성해야 한다. Image push 뒤 manifest file hash를 연결하는
`DeploymentBinding` schema는 `1.0`을 유지하되, 새 manifest hash로 새 Binding을 발급한다.

```text
control-plane의 Binding SHA-256
  → DeploymentBinding
    → AgentReleaseManifest file SHA-256
      → 코드·QueryPlan·Registry·Capability·Ontology·Dataset·Prompt·Model·Index
    → Docker repository@sha256 + platform
```

## 2. 고정되는 항목

| 영역 | 고정 내용 |
| --- | --- |
| 코드 | Agent Core와 FastAPI Backend의 배포 파일 tree SHA-256 |
| QueryPlan | JSON Schema 원본, Python 계약, HCX Structured Outputs schema |
| 의미 계약 | Field Registry·Capability matrix의 원본 hash와 canonical contract hash |
| Ontology | Stage 2 `ValidationReceipt`와 같은 canonical Ontology bundle hash |
| 공식 데이터 | Stage 2 dataset release ID, 승인 manifest, 네 상품군 원천·schema·SQLite hash와 snapshot date |
| Prompt | QueryPlan Prompt bundle, grounded answer Prompt bundle, HCLX generation contract |
| 실행기 | Compiler·Verifier·Planning policy·Core·Backend version |
| Model | `disabled` 또는 `hyperclova/HCX-007`, QueryPlan·답변 operation 활성 상태 |
| 검색 index | Schema Dense·Product Dense·Re-ranker·문서 BM25의 명시적 활성 상태 |
| 실행 제어 | HCLX timeout, 전체 답변 timeout, max inflight, worker 수 |
| Docker | image digest, `linux/amd64` 또는 `linux/arm64`, activation generation |
| Rollback | 최초 배포 여부 또는 검증된 직전 Binding의 release·manifest·image |

현재 Dense Schema Linker는 offline 평가 전용이고 Product Dense·Re-ranker는 구현 전이다.
따라서 release schema v1은 이를 빈 값으로 생략하지 않고 각각
`disabled_offline_only`, `disabled_not_implemented`로 고정한다. 승인 전 flag를 켜면
evaluation/production 시작이 실패한다.

HyperCLOVA X는 현재 API에서 immutable model revision을 제공하지 않으므로
`HCX-007` model ID, operation, Prompt·schema·generation parameter 코드까지 고정하되
`provider_revision_not_exposed` 한계를 명시한다. 따라서 외부 provider 자체의 bit 단위
재현성을 보장한다고 표현하지 않는다.

## 3. 시작과 요청 경계

evaluation/production 시작 순서는 고정돼 있다.

1. `Settings`의 release 설정 묶음을 검사한다.
2. 신뢰된 Binding file SHA-256과 strict/canonical JSON을 검사한다.
3. Binding의 manifest hash·image digest·platform·source commit을 교차 검사한다.
4. 코드·Prompt·Model·index 상태와 공식 데이터 계약을 deep hash로 재검사한다.
5. 네 SQLite의 승인 hash를 확인한다.
6. 그 뒤에만 HCLX transport와 `RoutedFinanceAgent`를 조립한다. 시작 중 HCLX 호출은 없다.

요청 시작과 종료에는 manifest·Binding이 그대로인지 다시 확인한다. 실행 중 발급되는
`ValidatedPlan`에도 Agent release ID, manifest file hash, Binding file hash와 release
context hash를 기록하며 Oracle이 이를 다시 검사한다. 코드 전체 deep hash는 시작과
`/health`에서 수행한다. 공식 Docker root filesystem이 digest-pinned·read-only이므로
요청마다 약 200개 파일을 다시 읽는 비용은 넣지 않는다.

불일치 결과는 다음처럼 fail-closed 처리한다.

- 시작 전 불일치: Backend 시작 중단
- readiness 불일치: `/health` HTTP 503 `degraded`
- 요청 중 불일치: 결과 폐기 후 안전한 503 오류
- 공개 DTO: release ID, hash, 내부 경로와 예외 상세를 노출하지 않음

`development`와 `test`는 빠른 로컬 개발을 위해 release를 강제하지 않는다. 공식 평가나
운영을 흉내 내려면 단순히 `APP_ENV=evaluation`만 설정하지 말고 아래 release 실행
경로를 사용해야 한다.

## 4. 릴리스 발급 순서

아래는 CI 또는 배포 담당자가 수행할 절차다. 현재 dirty working tree에서는 첫 명령이
거부되는 것이 정상이며, 이 문서의 명령을 현재 서버에서 자동 실행하지 않는다.

### 4.1 clean source에서 manifest 생성

Core가 검증한 Git checkout에서 import되도록 source path를 명시한다.

```bash
export SOURCE_COMMIT=<clean-Git-HEAD-40-hex>
export RELEASE_ID=<unique-release-id>

PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python -m finance_agent_core.release_cli manifest \
  --release-id "$RELEASE_ID" \
  --environment evaluation \
  --source-commit "$SOURCE_COMMIT" \
  --git-root /home/hyunholim/projects/finance-agent \
  --backend-root /home/hyunholim/projects/finance-agent/fastapi_backend/app \
  --platform linux/amd64 \
  --answer-provider deterministic \
  --output /home/hyunholim/projects/finance-agent/fastapi_backend/release/agent-release-manifest.json
```

생성기는 다음을 강제한다.

- Git top-level, HEAD와 `source_commit` 일치
- tracked·untracked 변경이 없는 상태를 hash 전후 두 번 확인
- Core와 Backend가 그 Git checkout의 정확한 경로인지 확인
- 기존 파일 덮어쓰기 금지, symlink 경로 금지, 결과를 read-only로 생성

HCLX release라면 승인된 설정에 맞춰 `--answer-provider hyperclova`,
`--hcx-model HCX-007`, 필요할 때만 `--hcx-queryplan-enabled`를 사용한다. 이 생성 명령은
API key를 읽거나 HCLX를 호출하지 않는다.

### 4.2 digest-pinned base와 release image 생성

코드-only base image도 먼저 registry에 push해 digest를 확정한다. Release Dockerfile에는
mutable local tag 기본값이 없으며, base를 `repository@sha256`로 전달해야 한다.

```bash
docker build --file fastapi_backend/Dockerfile \
  --tag <registry>/<repo>:base-"$SOURCE_COMMIT" .
docker push <registry>/<repo>:base-"$SOURCE_COMMIT"

docker build --file fastapi_backend/Dockerfile.release \
  --build-arg BACKEND_BASE_IMAGE=<registry>/<repo>@sha256:<base-digest> \
  --build-arg FINANCE_SOURCE_COMMIT="$SOURCE_COMMIT" \
  --build-arg FINANCE_RELEASE_ID="$RELEASE_ID" \
  --tag <registry>/<repo>:"$RELEASE_ID" .
docker push <registry>/<repo>:"$RELEASE_ID"
```

`Dockerfile.release.dockerignore`는 기본적으로 전체 build context를 제외하고 release
manifest 하나만 허용한다. `.env`, credential, 공식 XLSX, SQLite, 평가 결과는 release
image build context에 포함되지 않는다.

### 4.3 image digest를 Binding으로 연결

최초 activation은 generation 1과 `initial_bootstrap`만 허용한다.

```bash
PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python -m finance_agent_core.release_cli binding \
  --manifest /absolute/path/agent-release-manifest.json \
  --image-reference <registry>/<repo>@sha256:<release-image-digest> \
  --platform linux/amd64 \
  --activation-generation 1 \
  --rollback-mode initial_bootstrap \
  --output /absolute/path/deployment-binding.json
```

두 번째 이후 activation은 임의 문자열을 rollback 대상으로 쓰지 않는다. 직전의
read-only `DeploymentBinding`과 그 신뢰된 file SHA-256을 입력해야 하며 generation은
정확히 1 증가해야 한다.

```bash
python -m finance_agent_core.release_cli binding \
  --manifest /absolute/path/new-agent-release-manifest.json \
  --image-reference <registry>/<repo>@sha256:<new-release-image-digest> \
  --platform linux/amd64 \
  --activation-generation <previous-generation-plus-one> \
  --rollback-mode pinned_previous_release \
  --rollback-binding /absolute/path/previous-deployment-binding.json \
  --rollback-binding-sha256 <previous-binding-file-sha256> \
  --output /absolute/path/new-deployment-binding.json
```

## 5. Release Compose 실행

`.env.release.example`을 Git에서 제외되는 `.env.release`로 복사한 뒤 실제 값만 넣는다.
데이터 volume 이름은 다음처럼 manifest hash까지 포함한다.

```text
finance-data-<release_id>-<manifest-file-sha256-first-12>
```

공식 진입점은 다음 하나다.

```bash
RELEASE_ENV_FILE=fastapi_backend/.env.release \
./compose-release.sh up --detach --wait
```

Launcher와 release Compose는 다음을 강제한다.

- project 이름 `hyunholim-finance-agent`
- 두 service의 Compose `build` 설정 제거
- 모든 위치의 `build`, `--build`, 추가 compose file·project override 차단
- `run`·`exec`·`create`처럼 환경·entrypoint를 바꿀 수 있는 subcommand 차단
- image `repository@sha256`와 platform 고정
- Binding SHA-256, source commit, release별 data volume 이름 확인
- release data volume은 별도 사전 준비 artifact로 취급하고 `data-init`에서도 read-only 연결
- Shell 환경변수가 검증된 release env file을 덮어쓰지 못하게 identity 변수 제거
- Backend root filesystem read-only, Binding·DB read-only mount
- host의 활성 Binding 상태를 `/var/lib/finance-agent-release/active-binding.json`, activation
  lock을 `/run/lock/finance-agent-release/activation.lock`에 고정하고 두 경로를 root만
  쓸 수 있게 관리
- `flock`으로 trust 검증부터 Compose 기동과 상태 저장까지 직렬화하고, 현재와 완전히
  같은 Binding만 idempotent restart(동일 배포 재시작)로 허용
- 새 Binding은 `active generation + 1`이어야 하며 rollback 대상의 release·manifest·
  Binding·image·generation·environment·platform이 현재 활성 상태와 모두 일치해야 함
- `--no-build --force-recreate --wait`로 health 성공을 확인한 뒤에만 fsync·원자적 교체로
  활성 상태를 기록; 상태 기록 실패 시 새 기동을 내리고 이전 상태를 유지

따라서 과거에 올바르게 서명된 Binding이라도 현재 활성 generation보다 오래됐으면 다시
실행할 수 없다. 운영 rollback도 예전 Binding을 재생하는 방식이 아니라, 복귀할 이전
image·manifest를 대상으로 하되 현재 generation보다 1 큰 **새 서명 Binding**을 발급해
진행한다. 새 호스트나 재해복구에서 위 상태 파일이 사라지는 문제는 외부 불변 archive와
복구 절차가 필요한 운영 범위로 남아 있다.

HCLX release도 별도 Compose file을 추가하지 않는다. 같은 release override가 manifest와
일치하는 provider/model profile을 받고, `CLOVASTUDIO_API_KEY_HOST_FILE`의 host 파일을
Docker secret으로만 연결한다. inline `CLOVASTUDIO_API_KEY`는 launcher와 애플리케이션
설정 경계에서 거부한다. Deterministic release는 HCLX model·credential 설정이 있으면
launcher 단계에서 거부된다.

### 5.1 `record_cache` 정리 상태

`ServerQueryPlanCompiler`의 `record_cache` 인자를 제거할지는 Agent Core 담당자와의 계약
합의가 필요한 미확정 사항이다. 현재 배선에서 실제 사용 여부와 downstream 호출 호환성을
확인하기 전에는 dead code(쓰이지 않는 코드)로 단정하거나 삭제하지 않는다. 합의와 영향
검토는 GitHub [issue #7](https://github.com/Hyunhoim/gaeng3/issues/7)에서 추적하며, 결정 전까지
현 구현을 유지한다. 따라서 이 항목은 Stage 3 완료 주장이나 release blocker로 자동 해석하지
않고 Core 계약 정리 과제로 분리한다.

## 6. 검증된 범위와 남은 운영 과제

현재 코드·테스트로 검증한 범위:

- strict JSON, duplicate key, noncanonical file, symlink·hardlink·writable file 차단
- code·Prompt·Registry·Ontology·Dataset·Model·index·runtime control mismatch 차단
- image reference와 platform mismatch 차단
- startup 순서, 요청 전후 변경 감지, Receipt→Oracle release binding
- HCLX release 조립 시 startup network call 없음
- 최종 Compose service에 `build`가 남지 않는 병합 결과 확인
- global option을 앞세운 `--build`, profile 오인식, 부분 service 실행과
  `--force-recreate=false` 우회 차단
- rollback data volume·image를 지우는 `down --volumes`, `-v` 결합형, `--rmi` 차단
- Agent Core 전체 회귀 `1,061 passed`(실제 DB opt-in 1건 제외), FastAPI Backend 전체 회귀
  `162 passed`
- 네 상품군 승인 DB opt-in 회귀는 별도 실행해 SEARCH·AGGREGATE·COMPARE `62/62` 통과
- `finance_agent_core` 패키지·Backend Ruff lint/format 통과

보호된 `main`에서 수동 실행하는
[NCP immutable release CI](immutable-ncp-release-ci.md)가 아래 발급·검증·서명 계약을
구현한다. 다만 외부 Registry와 credential이 아직 없어 실제 실행 증거는 대기 상태다.

- code-only base·release image build/push와 exact registry digest 고정
- 원격 OCI index의 단일 `linux/amd64` 실행 platform, source commit·release ID·base label 검사
- GitHub OIDC 기반 cosign keyless image/blob 서명과 exact workflow identity 재검증
- SBOM·provenance 및 non-secret release evidence artifact 보존

외부 registry/NCP가 준비된 뒤 실제로 완료할 범위:

- clean commit으로 workflow를 dispatch해 실제 manifest·base·release digest 발급
- Registry의 OCI attestation·cosign signature 저장 호환성 확인
- NCP에 발급·서명된 이전 image·Binding·data volume을 이용한 공식 rollback drill
- blue/green 또는 load balancer를 이용한 무중단·원자적 traffic 전환
- transitive dependency hash lock과 조직 정책상 필요할 때의 NCP KMS/HSM profile
- 실제 배포가 검증된 Binding을 GitHub Environment의 직전 release trust anchor로 승격

두 개의 연속된 공식 release artifact가 준비되면
[`fastapi_backend/ROLLBACK_DRILL.md`](../../fastapi_backend/ROLLBACK_DRILL.md)의 격리된
N-1 → N → N-1 drill로 image·Binding·release별 DB volume 보존과 실제 재기동을 검증한다.
현재 localhost 합성 image·Binding·DB volume으로 같은 production harness의 실제 Docker
N-1 → N → N-1 재기동, health, 대표 `/answer`까지 통과했다. 다만 합성 artifact에는
GitHub OIDC·cosign 서명이 없어 trust verifier만 격리된 test stub으로 대체했으므로, 이를
외부 신뢰 anchor 또는 NCP 운영 rollback 완료로 표현하지 않는다.

따라서 현재 구현은 **배포 artifact를 고정하고 불일치·과거 Binding 재생을 차단하는
Stage 3 코드 경계**다. localhost 합성 Registry push는 OCI 동작 시험일 뿐 NCP image와
공식 Binding이 아직 발급되지 않았으므로 “공식 release 발급·rollback 실증 완료” 상태로
표현하지 않는다.
