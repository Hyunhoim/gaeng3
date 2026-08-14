# Schema Dense Stage 4 구현·Stage 5 실험 상태 보고서

- 기준일: 2026-08-13
- 기준 base commit: `ea380ed9774a7bedeb2ede9e867d214cfbf9b318`
- 검증 대상: 위 clean commit 뒤의 미커밋 Shadow 운영 계약 변경
- 현재 모드: production OFF, 평가·운영 release에서도 Shadow 주입 차단
- 결론: 모델 파일 고정, 관측 계약, 외부 blind harness, Shadow 격리와 Docker
  컴포넌트 측정까지 구현했지만 **사용자-visible 활성화는 승인하지 않음**

## 1. Main 반영 상태

`origin/main`의 PR #8 merge commit을 `hyunhoim`에 fast-forward로 반영했다. branch를
`main`으로 전환하거나 main을 수정하지 않았고, 현재 작업은 `hyunhoim`에서만 진행한다.

Main에서 받은 공개 Schema Dense 결과는 다음과 같다.

| 방식 | 정확한 field 묶음 | Recall@5 | 해석 |
| --- | ---: | ---: | --- |
| Lexical | 167/181 | 511/527 | 현재 결정론적 기준선 |
| BGE-M3 + lexical_first | 175/181 | 521/527 | 공개 개발 1순위 후보 |
| KURE-v1 + lexical_first | 175/181 | 520/527 | 독립 blind 동시 비교 후보 |

두 모델은 공개 질문 exact가 동률이며 통계적으로 한쪽 우위를 확정하지 못했다. 이 수치는
모델 선택에 사용한 공개 개발 질문 결과이므로 독립 blind 성능이나 공모전 예상 점수가 아니다.

## 2. 모델 artifact 고정

Artifact는 모델을 다시 받을 때 내용이 바뀌지 않았는지 확인할 수 있는 파일 묶음이다.
revision뿐 아니라 weights(학습된 숫자), tokenizer(문장을 token으로 나누는 규칙), config를
모두 파일 크기와 SHA-256으로 고정했다. 모델 가중치 자체는 Git에 넣지 않는다.

| 후보 | 고정 revision | weights SHA-256 | tokenizer.json SHA-256 |
| --- | --- | --- | --- |
| BGE-M3 | `5617a9f61b028005a4858fdac845db406aefb181` | `b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38` | `21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08` |
| KURE-v1 | `d14c8a9423946e268a0c9952fecf3a7aabd73bd9` | `c18156e80caf8ff45eb84a24a853130c3bca03087ccb41b051f86e7556bae02c` | `fb3c3b93c46fd5a8634e262e1b7de7da11a18b527aa2282b312952b692781dfd` |

두 snapshot은 network-none, read-only Docker에서 전체 파일 검증을 통과했다. 안전한 loader는
검증된 exact local path에서만 CPU 모델을 로드하고, 로드 전후로 snapshot을 다시 검사한다.
운영 시 cache 자체를 read-only mount해야 파일 교체 경쟁까지 닫힌다.

## 3. Stage 4 — 관측·감사 계약

관측·감사는 “어떤 안전 경계를 거쳐 어떤 결과가 났는지”를 운영자가 재구성하는 기능이다.
이번 구현은 다음 계약을 고정했다.

- `AuditEvent v1.1`: Request, Route, Safety, Planning, Lexical, Dense, Compiler,
  Authority, SQL/Oracle, Verifier, Renderer, Answer, Schema Shadow 단계 코드
- 원문 질문·답변·API key·DB 경로를 저장하지 않고 SHA-256과 허용된 code/count만 기록
- release, deployment binding, 승인 dataset, model snapshot, index manifest를 hash로 연결
- product/evidence ID도 원문 대신 bounded hash 목록으로 기록
- p50·p95·p99, queue depth, inflight와 안전·timeout·fallback counter를 제한된 cardinality로 집계
- 감사 sink 예외가 Agent 결과·예외를 바꾸지 않는 failure isolation

이후 보완에서 HTTP REQUEST → Safety/Lexical/Planning/Route → Compiler/Authority →
SQL/Oracle → Verifier/Renderer/Answer → 실제 ASGI 응답 완료까지 전체 emit 배선을 연결했다.
한 invocation의 hash와 연속 sequence, ROUTE·ANSWER 단일 기록, 422·overload·timeout·전송
중단, JSONL drop·flush·readiness까지 검증했다. 상세 실측은
[Stage 4 최종 검증 보고서](stage4-audit-blind-image-api-report-2026-08-13.md)를 따른다.

느린 저장소는 bounded async queue로 사용자 요청 경로에서 분리한다. 이벤트가 유실되거나
저장이 실패해도 개별 답변을 바꾸지는 않지만, 프로세스 readiness는 재시작 전까지
`degraded`로 유지한다. 프로세스 간 집계가 없으므로 evaluation/production worker는 1개로
고정한다.

## 4. 외부 blind 최초 평가 계약

Blind 평가는 구현 담당자가 정답을 보기 전에 처음 한 번 실행해야 일반화 성능을 확인할 수
있다. v2는 다음과 같이 fail-closed(조건이 빠지면 실행하지 않음)로 구성했다.

- Phase 1에는 `id + question`만 허용하고 family·intent·disposition·rationale를 차단
- 비공개 정답키에만 family·intent·QueryPlan·Oracle·답변 gold를 보관
- BGE-M3, KURE-v1, Lexical을 결과 확인 전에 동시에 고정
- 공개 모델 선택에 사용한 `schema-embedding-cpu-public-v1` 입력 manifest, 네
  `core_50` suite 200문항과 정책 migration provenance를 SHA-256으로 고정
- 외부 질문은 정답 공개 전 label-free near-duplicate gate를 통과해야 하며, 공개 질문의
  exact copy 또는 유사도 0.84 이상 paraphrase가 있으면 Provider 로드 전 실행 거부
- 공식 runner가 Provider나 Index를 caller에게서 받지 않음
- 두 모델을 exact snapshot double gate로 모두 로드한 뒤 동일 canonical Schema Index를 내부 생성
- 고정 `IntentRouter`를 사용하고 OOD는 숨겨진 family 없이 네 승인 상품군을 전부 probe
- exact clean image digest의 독립 실행 승인과 외부 append-only prediction receipt가 있어야 채점
- 외부 receipt와 정답키가 없으면 score report를 생성하지 않음
- protocol 원문, reference corpus, near-duplicate report를 commitment·독립 실행 승인·
  prediction lock에 함께 binding
- 정답키 공개 뒤 gold CLARIFY/UNSUPPORTED에서 실제 operational Dense provider 호출 수와
  질문 단위 무실행률을 계산하며, 호출 0회·무실행률 100%만 안전 gate 통과

현재 외부 질문·비공개 정답키·commitment·독립 평가자 승인·외부 receipt가 없다. 따라서
**최초 blind 실행과 독립 점수는 아직 0회이며 대기 상태**다. 단위 테스트의 fake model과
fixture 결과는 protocol 동작 확인일 뿐 실제 성능 점수가 아니다.

100문항 합성 리허설은 `internal_synthetic_not_blind` 상태로 해시·receipt·권한·공식
loader 거부 절차를 통과했다. 실제 모델 추론과 외부 독립성은 없으며 report에
`never_model_selection_evidence=true`가 고정돼 있으므로 모델 선택 근거가 아니다.

## 5. Router·OOD 검증 범위

현재 공개 회귀 근거는 다음과 같다.

- pre-HCX Router diagnostic: 네 상품군·일곱 intent 28/28
- 금융 도메인 QA Router 회귀: 40/40
  - CLARIFY 19, UNSUPPORTED 20, 정상 실행 1
  - control의 잘못된 실행과 오류 0
- 내부 red-team: 공개 개발 40/40
- Stage 5 테스트는 OOD·CLARIFY·UNSUPPORTED·결정론적 Fast Path에서 operational
  embedding 호출 0회를 검사

위 결과는 모두 공개 개발·회귀 자료다. 외부 blind의 Router disposition·family·intent,
OOD false accept와 전체 SQL 결과 정확도는 아직 확인하지 않았다. 특히 현재 v2 외부 harness는
Router와 Schema field/OOD 평가이며 Compiler·SQL·Verifier·최종 답변까지의 독립 full E2E는
별도 평가가 필요하다.

## 6. Stage 5 — OFF 상태의 비배포 실험용 Shadow

Shadow는 실제 질문을 관찰해 Dense 후보를 계산하지만 그 결과를 사용자 답변에 적용하지 않는
모드다. 현재 구현은 `OFF`와 실험용 `SHADOW`만 지원하고 활성 실행 모드는 없다. Stage 5는
배포 준비 완료 상태가 아니라, 향후 별도 승인을 받기 위한 격리 실험 코드다.

- `SafetyEnvelope`와 Router가 실행을 허용한 단일 상품군의 미해결 span에만 enqueue
- 한 worker, bounded queue, embedding max inflight 1
- request thread는 embedding을 기다리지 않으며 queue가 차면 local counter만 증가
- Lexical과 Dense가 충돌하거나 점수·margin이 낮으면 `CONFLICT/ABSTAIN`
- field registry의 상품군·intent capability를 통과한 canonical field ID만 후보로 기록
- Shadow가 받은 `PlanningTrace`는 실행에 쓰는 trace와 별도로 복제하고 결과를 폐기
- trusted bounded async observer만 Agent에 주입 가능하며 Provider·queue·audit 오류가
  QueryPlan·SQL·응답을 바꾸지 않음
- `submit()` 시 요청의 `RequestAuditRecorder`를 함께 보존해 비동기 event도 같은
  invocation hash와 연속 sequence를 사용. 기대 audit sink가 누락·불일치하면 Embedding 전 차단
- durable Audit 지표와 분리된 bounded Shadow snapshot으로 accepted/completed, queue/inflight
  peak, drop, operational/correlation/audit emit 실패, worker·stall·shutdown 상태를 집계
- `FOUND/CONFLICT`, 낮은 score·margin, capable field 없음은 정상 관찰 결과로 두고,
  artifact·embedding·internal·inflight 실패만 operational failure로 분류
- queue drop, operational/correlation/audit emit 실패, worker death·stall은 readiness를
  재시작 전까지 `degraded`로 고정. observer 없음/OFF는 `disabled`, lazy 미시작은 `ok`
- 종료는 요청 worker → Shadow → audit 순서이며 하나의 deadline을 공유. shutdown과 submit
  경쟁에서도 종료 뒤 처리되지 않은 trace가 남지 않도록 수명주기 잠금
- 실제 SQLite 경로에서 Shadow 유무의 응답을 byte 단위로 비교

현재 AgentReleaseManifest는 Schema Dense를 `disabled_offline_only`로 고정한다. Core와
FastAPI approval guard 모두 evaluation/production에 Shadow observer가 들어오면 시작을
거부한다. 즉, 이번 코드는 향후 제한된 실험을 위한 껍데기이며 현재 배포에는 자동 연결되지 않는다.

## 7. 실제 Docker CPU 측정

두 모델을 동일한 image digest, linux/amd64, CPU 8개, memory 6GiB, network-none,
read-only root/repository/cache, `/tmp` tmpfs 조건에서 측정했다. 각 동시성 단계는 24회이고
embedding max inflight는 1이다.

| 모델 | c1 p50 / p95 | c2 p50 / p95 | c4 p50 / p95 | process peak RSS | 오류 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BGE-M3 | 60.59 / 92.36ms | 82.35 / 117.10ms | 189.89 / 345.59ms | 1.94GiB | 0 |
| KURE-v1 | 79.29 / 97.83ms | 103.76 / 177.36ms | 198.46 / 323.51ms | 1.93GiB | 0 |

사전에 정한 컴포넌트 gate는 동시성 1·2의 p95 250ms 이하, peak 4GiB 이하, 오류 0이다.
컨테이너 cgroup memory peak·limit가 실제로 관측되지 않으면 gate는 자동 실패한다. 두 모델
모두 이 최소 전제는 통과했다. 그러나 진단용 동시성 4에서는 둘 다 p95 250ms를
넘었다. 또한 이 측정은 `embed_query()`만 포함하고 표본도 작으며, uncommitted candidate
worktree에서 만들었다. FastAPI, Router, SQL, Verifier, Shadow가 사용자 요청과 경쟁할 때의
HTTP p95·timeout·메모리를 의미하지 않는다.

원본 요약은
[Docker runtime baseline](../evaluation/baselines/schema-embedding-docker-runtime-2026-08-13.json)에
동결했다.

## 8. 최종 회귀 검증

| 검증 | 결과 |
| --- | ---: |
| Agent Core 전체, 승인 네 SQLite read-only 포함 | 1,225 passed |
| FastAPI Backend 전체 | 258 passed, 기존 fork warning 2건 |
| Agent Core Ruff lint | 통과 |
| Agent Core Ruff format | 통과 |
| 문서·baseline·내부 링크 검사 | 66 Markdown, 47 baselines 통과 |
| JSON·whitespace 검사 | 통과 |

테스트는 Python 3.12.13, network-none, read-only root filesystem의 임시 Docker
container에서 수행했다. 테스트용 `/tmp` tmpfs에만 `exec`를 허용했고 repository와 승인
DB는 계속 read-only로 유지했다. 실행 중인 공유 Backend container는 변경하거나 재시작하지
않았다.

release QA와 같은 범위인 Agent Core `src`·`tests`와 `fastapi_backend`
전체 293개 파일은 Ruff lint·format을 모두 통과했다. blind sealing·
remediation 등 release runtime 밖의 역사 파일을 임의로 재포맷하지는 않았다.

## 9. 활성화 판단과 남은 gate

Stage 4 코드 배선과 로컬 결정론적 후보 기준선은 완료했다. 현재 전체 판단은 여전히
**활성화 차단 유지**다. 다음 조건을 순서대로 모두 충족한 뒤에만 별도 승인을
검토한다.

1. 현재 Shadow 운영 계약 변경을 승인된 clean commit과 exact OCI image digest로 다시 고정
2. 독립 작성자가 label 없는 질문·비공개 정답키·commitment 제공
3. 독립 평가자가 exact image를 실행하고 prediction hash를 외부 append-only 저장소에 기록
4. BGE·KURE 최초 1회 동시 blind 결과와 OOD false accept 0 확인
5. Router → Compiler → read-only SQL/Python → Verifier → Answer의 독립 full E2E 확인
6. clean evaluation release에서 승인 dataset hash의 실제 AuditEvent 연결 검증
7. Shadow OFF 대비 실제 Docker HTTP 동시성·p95·timeout·메모리 간섭 재측정
8. NCP evaluation 환경에서 같은 release·dataset·model/index binding으로 재검증
9. 모델 선택과 threshold를 별도 AgentReleaseManifest에 반영하고 Core·Backend 계약 공동 승인

이전까지 Dense는 후보 제안과 관찰만 담당하며, 사용자 의도 확정·QueryPlan 승인·SQL 실행·
수치 생성 권한을 갖지 않는다.
