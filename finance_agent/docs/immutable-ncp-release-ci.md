# NCP immutable release CI와 cosign 신뢰 경계

상태: HCLX answer-only 최종 profile·저장소 계약·localhost 합성 Registry push 검증 완료 · 실제 NCP Registry 실행 대기

## 1. 현재 완료된 범위

`.github/workflows/immutable-ncp-release.yml`은 보호된 `main`에서 운영자가 수동으로
실행하는 release 전용 GitHub Actions workflow다. 일반 Pull Request, 개발 브랜치,
보호되지 않은 `main`에서는 실행을 거부한다.

workflow가 성공하면 다음 순서가 하나의 감사 가능한 실행 기록으로 남는다.

1. NCP Container Registry 주소·repository·release ID·activation generation을 정규식으로
   검사한다. shell 명령에 workflow input을 직접 삽입하지 않는다.
2. 보호된 GitHub Environment의 관계 검색 artifact를 strict base64로 복원한 뒤,
   외부 SHA-256·정확한 schema·canonical JSON·read-only 파일 권한을 검사한다.
3. SHA-256으로 고정된 Python base에서 code-only image를 빌드하고 NCP Registry에 push한다.
4. Registry가 반환한 정확한 digest를 `repository@sha256`로 고정한다.
5. clean Git checkout에서 복원한 artifact와 외부 SHA-256을 함께 넣어
   **관계 검색이 활성화된** `AgentReleaseManifest`를 생성한다. 생성 직후 artifact의
   `approval_manifest_sha256`와 Manifest의 승인 데이터 contract SHA-256이 같은지 다시
   비교해, 과거 데이터 세트의 artifact가 섞이면 Registry 변경 전에 중단한다.
6. code-only image digest와 Manifest를 이용해 release image를 빌드·push한다.
7. 원격 OCI index가 실행 가능한 image를 정확히 `linux/amd64` 하나만 포함하는지 확인한다.
   SBOM·provenance용 `unknown/unknown` attestation descriptor는 실행 image로 세지 않는다.
8. exact digest를 pull한 뒤 source commit, release ID, Python base OCI label을 검증한다.
9. exact image를 network none·read-only·UID/GID 10001로 실행해 금지 model dependency·
   executable·일반 weight 형식·DB·XLSX·inline credential 부재와 평가 Settings Guard를
   확인한다. report에 exact digest·release ID·source commit을 넣고 증거 SHA 목록에 포함한다.
10. 최초 배포 또는 신뢰된 직전 Binding을 이용해 `DeploymentBinding`을 생성한다.
11. GitHub OIDC로 base image, release image, Manifest, Binding을 cosign keyless 서명하고,
   동일 workflow identity와 issuer로 즉시 검증한다.
12. credential을 제외한 Manifest·Binding·digest·OCI inspect·image runtime boundary·
    Sigstore bundle·검증 결과를
    GitHub Actions artifact로 90일간 보존한다.

모든 외부 Action은 버전 tag가 아니라 정확한 commit SHA로 고정돼 있다. Docker build는
SBOM(포함 패키지 목록)과 provenance(어떤 빌드 입력으로 만들었는지에 대한 증명)를 함께
Registry에 기록한다. CI의 cosign(서명·검증 도구)은 `v3.1.3`으로 고정한다. 배포 호스트는
[`install_cosign_verifier.sh`](../../fastapi_backend/scripts/install_cosign_verifier.sh)로
같은 버전을 `/usr/local/bin/cosign`에 설치하며, 실행 파일 SHA-256
`4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71`까지 확인한다.
2026-08-12에는 공식 release asset을 임시 경로에 직접 내려받아 이 SHA-256과
`GitVersion: v3.1.3`, `Platform: linux/amd64`를 실제 확인한 뒤 파일을 삭제했다. 이는
검증기 공급망 확인이며 아직 NCP image에 대한 서명 생성·검증 기록은 아니다.

## 2. 선택한 외부 trust anchor

이번 계약은 장기 private key나 NCP KMS key를 CI에 저장하지 않는다. 대신 다음 신뢰 사슬을
사용한다.

```text
보호된 main + GitHub Environment 승인
  → GitHub Actions OIDC 단기 신원
    → Sigstore Fulcio 단기 인증서
      → cosign image/blob 서명
        → Rekor transparency log
```

검증 시 허용하는 인증서 identity는 다음 한 개뿐이다.

```text
https://github.com/Hyunhoim/gaeng3/.github/workflows/immutable-ncp-release.yml@refs/heads/main
```

OIDC issuer도 `https://token.actions.githubusercontent.com`으로 고정한다. 따라서 다른
repository, 다른 workflow 파일, tag, PR ref에서 만든 서명은 같은 Registry에 존재해도
검증에 실패한다.

NCP KMS를 사용하지 않은 것은 누락이 아니라 keyless 방식을 선택한 결과다. 향후 회사 정책이
Sigstore public service를 허용하지 않거나 조직 소유 장기 키를 요구할 때만 NCP KMS/HSM
서명 profile을 별도 설계한다. 두 신뢰 방식을 동시에 묵시적으로 허용하지 않는다.

## 3. 최초 실제 실행 전 필수 준비

실제 push는 다음 외부 조건이 모두 준비되기 전에는 수행할 수 없다.

- workflow 파일이 `main`에 merge돼 있고 GitHub branch protection이 활성화돼 있어야 한다.
- GitHub `evaluation`과 `production` Environment를 만들고 필요한 승인자를 설정한다.
- NCP Container Registry를 만든 뒤 public network에서 접근 가능한 Registry hostname과
  image repository를 확정한다.
- 각 Environment의 variable(비밀이 아닌 보호 설정)에 `NCP_REGISTRY_HOST`와
  `NCP_IMAGE_REPOSITORY`를 등록한다. Registry와 repository는 수동 실행 입력으로 받지
  않으므로 workflow 실행자가 다른 push 대상을 주입할 수 없다.
- 승인된 원천 데이터로 별도의 통제된 data-preparation을 실행해
  `relation-retrieval-artifact.json`, `relation-retrieval-artifact.sha256`,
  `provided-relations.sqlite3`, 세 상품 DB를 하나의 세트로 생성한다.
- 각 Environment의 secret `APPROVED_RELATION_RETRIEVAL_ARTIFACT_B64`에 canonical
  `relation-retrieval-artifact.json`의 **줄바꿈 없는 strict base64**를 등록한다.
- 각 Environment의 variable `APPROVED_RELATION_RETRIEVAL_ARTIFACT_SHA256`에 위 JSON
  파일의 64자 소문자 SHA-256을 등록한다. 이 값은 동일 세트의
  `relation-retrieval-artifact.sha256`과 정확히 같아야 한다.
- workflow에 승격한 정확한 JSON·SHA와 NCP 배포 호스트의 release-specific
  data volume 안 JSON·index·DB가 동일한 세트임을 보장한다. workflow는
  원천 금융 데이터나 Docker volume을 생성하지 않으며, Backend startup이
  Manifest·artifact·index·DB hash를 다시 교차 검증한다.
- 각 Environment에 `NCP_REGISTRY_USERNAME`, `NCP_REGISTRY_PASSWORD` secret을 설정한다.
  전용 계정에는 대상 Registry의 push·pull에 필요한 최소 권한만 부여한다.
- NCP Registry가 OCI attestation과 cosign signature artifact push/pull을 지원하는지 작은
  비운영 repository에서 먼저 확인한다.
- GitHub-hosted runner가 NCP Registry와 Sigstore Fulcio·Rekor에 outbound HTTPS로
  접근할 수 있어야 한다.
- 실제 NCP Server가 `linux/amd64`인지 확인한다. 현재 공식 workflow는 이 platform만
  허용한다.

두 번째 activation부터는 보호된 GitHub Environment에 다음 두 값을 함께 저장해야 한다.

- `PREVIOUS_DEPLOYMENT_BINDING_B64`: 실제 활성화와 health 검증을 마친 직전 Binding의
  canonical file을 base64로 인코딩한 값
- `PREVIOUS_DEPLOYMENT_BINDING_SHA256`: 해당 원본 file의 SHA-256

새 workflow artifact가 생성됐다는 이유만으로 이 값을 자동 승격하면 안 된다. 배포 성공,
health, 대표 `/answer`, rollback 가능성을 확인한 운영자가 Environment 승인을 통해 현재
신뢰 대상을 바꿔야 한다.

generation 1은 `PREVIOUS_DEPLOYMENT_BINDING_SHA256`가 비어 있을 때만 허용된다. 신뢰된
release가 한 번이라도 생긴 뒤 이 값을 유지하면 bootstrap(최초 기동) 재실행을 차단한다.
단, 관리자가 해당 secret을 지워 버리면 GitHub Environment 자체만으로 과거 발급 여부를
복원할 수 없다. 따라서 Environment 보호·감사 로그와 별개로 최초 Binding부터 모든 release
증거를 보존하는 durable archive(90일보다 오래 유지되는 외부 불변 보관소)가 실제 배포 전
필수 blocker다. 이 보관소와 복구 절차가 정해지기 전에는 generation 1 재생 방지를 운영
수준으로 완료했다고 주장하지 않는다.

배포 host는 GitHub Environment 값만 믿지 않고 독립적인 활성 상태를
`/var/lib/finance-agent-release/active-binding.json`에 보관하며
`/run/lock/finance-agent-release/activation.lock`의 `flock` 안에서 전환을 검증한다.
정확히 같은 Binding은 재시작할 수 있지만, 다른 Binding은 현재 generation보다 정확히 1
커야 하고 rollback의 일곱 identity가 현재 활성 상태를 모두 가리켜야 한다. 이 경계는 이미
서명된 과거 Binding의 replay(재실행)를 막는다. 다만 host 상태가 함께 유실되는 새 서버·
재해복구 상황은 위 durable archive와 복구 절차가 여전히 필요하다.

## 4. 수동 실행 입력과 성공 기준

GitHub Actions의 `Immutable NCP release`에서 다음 값을 입력한다.

| 입력 | 예시 | 검사 |
| --- | --- | --- |
| `release_id` | `finance-agent-eval-20260812-001` | 소문자·숫자·`.`·`_`·`-`, 8~100자 |
| `environment` | `evaluation` | `evaluation` 또는 `production` |
| `activation_generation` | `1` | 최초 1, 이후 직전 값+1 |

`answer_provider`와 `hcx_queryplan_enabled`는 운영자 입력이 아니다. 최종 workflow가
각각 `hyperclova`, `false`로 고정하며 `release_ci.py`는 다른 조합을 Registry 변경 전에
거부한다. 최종 Manifest는 HCX-007 answer-only, Dense OFF, 공모펀드 locked를 기록한다.

push 대상은 입력이 아니라 선택한 GitHub Environment의 보호 variable로 읽는다.

| GitHub Environment variable | 예시 | 검사 |
| --- | --- | --- |
| `NCP_REGISTRY_HOST` | `<team>.kr.ncr.ntruss.com` | NCP Registry hostname 형식 |
| `NCP_IMAGE_REPOSITORY` | `finance-agent/backend` | 정규화된 소문자 repository path |
| `APPROVED_RELATION_RETRIEVAL_ARTIFACT_SHA256` | `<64 lowercase hex>` | 복원한 canonical artifact와 exact hash 대조 |

`APPROVED_RELATION_RETRIEVAL_ARTIFACT_B64`는 크기·base64·JSON·hash가 모두 일치해야
`$RUNNER_TEMP/relation-retrieval-artifact.json`으로 0444 권한으로 복원된다.
workflow shell은 이 값을 출력하지 않으며, 실패 메시지에도 artifact 내용을
포함하지 않는다. 배포 호스트의 `.env.release` 속
`FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256`에도 반드시 같은 SHA-256을 입력한다.
발급된 `release-metadata.json`의 `relation_retrieval_artifact_sha256`과 검증 대상
Manifest 속 `knowledge_retrieval.relation.artifact_file_sha256`에서도 같은 값을
독립적으로 대조할 수 있다. `release-metadata.json`은 관계 artifact SHA-256을 필수
필드로 추가한 schema version `1.1`을 사용한다.

성공 기준은 단순히 Docker build가 끝난 것이 아니다. 다음이 모두 성공해야 한다.

- base·release image push digest가 `sha256:<64 hex>` 형식이고 exact reference pull 성공
- 원격 OCI index의 실행 manifest가 `linux/amd64` 하나뿐임
- release image label의 source commit·release ID·base image가 workflow 입력과 일치
- Binding 생성기가 Manifest hash·image digest·rollback generation을 수용
- Manifest의 `knowledge_retrieval.relation.status`가 `activated`이고, 보호된
  relation artifact와 exact file SHA-256이 함께 고정됨
- relation artifact의 승인 데이터 contract SHA-256과 새 Manifest가 계산한 승인 데이터
  contract SHA-256이 정확히 같음
- 네 cosign 서명과 exact workflow identity 검증 성공
- non-secret release evidence artifact 업로드 성공

workflow 성공은 artifact 발급까지의 기준이다. 실제 배포 호스트에서
`./compose-release.sh up`을 실행하면 container 생성 전에 다음 세 신뢰 검증을 다시
통과해야 한다.

- `cosign verify`: exact `repository@sha256` image가 허용된 workflow identity로 서명됐는지
- NCP Registry는 OCI 1.1 Referrers API를 제공하지 않으므로 image 서명과 검증은 Cosign
  v3.1.3의 `--new-bundle-format=false` legacy tag 경로로 고정한다. Manifest와 Binding은
  기존 Sigstore bundle 파일 경로를 유지한다.
- `cosign verify-blob`: `AgentReleaseManifest`와 그 Sigstore bundle이 일치하는지
- `cosign verify-blob`: `DeploymentBinding`과 그 Sigstore bundle이 일치하는지

그 뒤에도 Binding SHA-256, Binding이 가리키는 Manifest SHA-256과 image reference를 서로
대조한다. 하나라도 다르면 activation(실제 기동) 전에 fail-closed(안전하게 중단)한다.

신뢰 검증과 대조가 끝나도 launcher는 바로 Docker를 실행하지 않는다. root-controlled
activation broker가 전체 절차를 lock으로 직렬화하고 이전 활성 상태와 generation·rollback
chain을 검증한 뒤 `--no-build --force-recreate --wait`로 기동한다. health가 성공한 경우에만
새 active state를 fsync 후 원자적으로 교체한다. 상태 저장이 실패하면 새 기동을 내리고
기록되지 않은 release를 활성 상태로 남기지 않는다. 운영 rollback 역시 오래된 Binding을
그대로 재생하지 않고, 이전 image·manifest를 가리키는 다음 generation의 새 서명 Binding을
사용한다.

### 4.1 P0-10 관계 artifact 신뢰원 점검 — 2026-08-20

관계 검색은 개발과 공개 release에서 서로 다른 신뢰원을 사용하며, 최종 Compose에는
정확히 하나만 남아야 한다.

| 프로필 | 관계 artifact SHA-256 신뢰원 | 최종 Compose 계약 |
| --- | --- | --- |
| 개발 | data-init이 같은 volume에 만든 read-only `relation-retrieval-artifact.sha256` | `FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256_FILE`만 사용 |
| evaluation·production | 보호된 GitHub Environment의 외부 SHA-256 | release overlay가 sidecar를 `!reset null`로 제거하고 `FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256`만 사용 |

CI는 secret의 artifact를 `base64.b64decode(..., validate=True)`로 복원한다. 크기 제한,
UTF-8·duplicate key·정확한 schema, canonical JSON bytes와 외부 64자 소문자 SHA-256을
검사한 뒤에만 `$RUNNER_TEMP/relation-retrieval-artifact.json`을 새 파일로 만든다.
파일은 symlink·hardlink가 아니어야 하고 mode `0444`, 동일 inode를 다시 확인한 뒤 parent
directory까지 fsync한다. 값과 artifact 원문은 workflow log에 출력하지 않는다.

clean checkout에서 manifest 1.2를 만든 직후 다음 세 값을 다시 교차 검사한다.

1. 복원 artifact의 `approval_manifest_sha256`
2. 현재 source tree에서 생성한 `components.approved_datasets.manifest.contract_sha256`
3. `components.knowledge_retrieval.relation`의 artifact 전체와 `artifact_file_sha256`

따라서 과거 승인 데이터에서 만든 관계 artifact를 현재 dataset manifest에 섞거나,
manifest 생성 뒤 artifact만 바꾸면 Registry의 다음 변경 전에 중단한다. 배포 host에서는
같은 explicit SHA-256과 검증 대상 release manifest를 사용하고, Backend startup이 실제
index·세 상품 DB·관계 집합을 다시 대조한다. startup 이후 path/inode/size/mtime/ctime drift는 `/health`를
`degraded` HTTP 503으로 바꾸며 관계 요청도 503으로 거부한다.

현재 최종 동결 후보의 로컬 증거는 Agent Core `1,481 passed, 1 skipped`, Backend
`430 passed, 1 skipped`, P0-10 집중 회귀 `522/522`다. fresh Docker smoke는
Backend 8/8와 공식 GET 8/8이고, relation index 1바이트 변조 뒤 health·관계 요청 503을
확인했다. 이는 workflow의 계약과 로컬 runtime 동작 증거이지 보호된 GitHub Environment의
실제 dispatch·NCP push·cosign 서명이나 서명된 두 release rollback 증거는 아니다.

## 5. 현재 수행하지 않은 일

이 문서를 작성한 시점에는 NCP Registry hostname, Registry credential, 보호된 GitHub
Environment가 제공되지 않았다. 따라서 workflow를 dispatch하거나 **NCP Registry**에
image를 push하지 않았고, NCP release의 Sigstore 인증서·Rekor entry도 아직 존재하지
않는다.

OCI push·pull과 platform·label 검사 자체는 연구실 서버의 `localhost:25001` 합성 Registry로
검증했다. 이는 dirty working tree로 만든 비공식 시험 image이며 결과는 다음과 같다.

- OCI index digest:
  `sha256:043e19a7b26747ca540e55c9e1d63dfe88c130f66c31cc841f97ce215cebaaaa`
- 실행 가능한 `linux/amd64` manifest:
  `sha256:72b5ec77e95a0a21ad95f6dcafdb8817cad48d3efed905ccc6703de0c467e514`
- SBOM·provenance용 `unknown/unknown` attestation:
  `sha256:fee239e5c4cdef2d75e2a1f7f10252030f584eb39b7a7bbcd46e0e5aef656492`
- revision label: `dirty-worktree-2fb199e3d6a6`
- Python base label:
  `docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`

같은 base digest에서 공식 `Dockerfile.release`로 manifest-bound 합성 release image도
push·exact digest pull했다. OCI index digest는
`sha256:4188daf98cae0d136ea37cd7978d0ee00289bc9d233818f0fd1cfc8a89dd17d5`, 실행
manifest는 `sha256:ef96ffcccd58b7f0bdf8e228779938825a31036562ab233241bb2f69c3fcb28a`,
attestation은 `sha256:845e0277acafb6b90c9fbeca79d1047efce61b4c924e444fdae752db723e32ca`다.
실제 inspect에서 `linux/amd64`, non-root `app`, source revision, release ID, exact backend
base digest, pinned Python base digest 네 release label을 모두 대조했다. 이 역시 합성
manifest를 쓴 OCI 계약 시험이며 공식 `AgentReleaseManifest` 발급 증거는 아니다.

이 localhost digest는 NCP digest가 아니며, clean commit·GitHub OIDC·cosign 서명·공식
Binding·NCP 기동 성공의 근거로 사용하지 않는다.

이 상태를 “NCP release 완료”라고 표현하면 안 된다. 현재 완료된 것은 **실제 외부 조건이
주어졌을 때 불일치하면 중단하는 CI 계약과 그 로컬 단위 테스트**다. NCP 준비 후에는
evaluation Environment에서 최초 generation 1을 발급하고, 별도 rollback runbook에 따라
이전 image·Binding·DB volume으로 실제 복귀 훈련을 완료해야 한다.

또한 GitHub artifact의 90일 보존 기간을 넘겨 Manifest·Binding·Sigstore bundle·digest·
검증 기록을 보존할 durable archive와 hash 검증 복구 절차가 아직 확정되지 않았다. 이는
실제 production release 전에 해소해야 할 외부 운영 blocker다.
