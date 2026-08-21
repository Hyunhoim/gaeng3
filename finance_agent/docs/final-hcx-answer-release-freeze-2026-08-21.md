# 최종 HCLX answer-only 평가 Release 동결

기준일: 2026-08-21

상태: 로컬 코드·계약 검증 완료, clean commit·서명된 NCP Release 발급 전

## 1. 최종 결정

“구현된 기능을 모두 ON”은 **평가 품질과 안전성이 확인된 production 기능을 모두
활성화한다**는 뜻으로 고정한다. 실험 코드가 존재한다는 이유만으로 Dense나 모델 계획을
켜지 않는다.

최종 evaluation은 다음 한 문장으로 정의한다.

> SafetyEnvelope와 서버 규칙이 질문을 해석하고 SQLite/Python·Verifier가 상품과 수치를
> 확정한 뒤, HyperCLOVA X가 검증된 근거만 answer-only로 표현하는 금융상품 Agent

## 2. 최종 ON/OFF profile

| 구성요소 | 최종 상태 | 이유 |
| --- | --- | --- |
| SafetyEnvelope·SemanticCoverageGate | ON | 탈옥·비금융·미해석 조건을 실행 전에 차단 |
| Ontology·Lexical Router·Server QueryPlan compiler | ON | 명확한 질문을 결정론적으로 구조화 |
| PlanAuthorityGate·read-only SQLite Oracle | ON | 검증된 계획만 검색·계산 실행 |
| Result·Aggregate·Comparison·Answer Verifier | ON | 상품 ID·수치·정렬·근거를 독립 재검산 |
| 제공 데이터 관계 검색 | ON | signed relation artifact와 공식 DB가 일치할 때만 실행 |
| HCLX HCX-007 grounded answer | ON | 검증된 결과를 제한된 Structured Output으로 표현 |
| JSONL Audit·fsync·timeout·single-flight | ON | 요청 인과관계와 재시도·과부하를 통제 |
| HCLX QueryPlan | OFF | external blind 보정 없이 strict plan 비교를 켜면 지연·CLARIFY가 증가할 수 있음 |
| HCLX grounded planning | OFF | final production 배선·독립 품질 근거가 없음 |
| KURE Schema Dense | OFF | test-only shadow smoke만 완료, 사용자 결과 변경 권한 없음 |
| Product Dense·Re-ranker | OFF | 구현·승인 기준선이 없음 |
| 외부 문서 BM25/RAG | OFF | 승인된 실제 corpus가 없음 |
| 공모펀드 실행 | locked | 주최 측 데이터 정정 공지 전 공개 실행 금지 |
| 로컬 Qwen | 평가 runtime 실행 불가 | 개발 이력에만 남고 evaluation·production 설정에서 차단 |

최종 환경값은 다음과 같다.

```text
APP_ENV=evaluation
FINANCE_BACKEND_ANSWER_PROVIDER=hyperclova
FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=false
FINANCE_AGENT_LLM_MODE=evaluation
LLM_PROVIDER=hyperclova
HCX_MODEL=HCX-007
HCX_TIMEOUT_SECONDS=45
CLOVASTUDIO_API_KEY_FILE=/run/secrets/clovastudio_api_key
FINANCE_BACKEND_FUND_EXECUTION_POLICY=locked
FINANCE_DENSE_SCHEMA_LINKER_ENABLED=false
FINANCE_PRODUCT_DENSE_ENABLED=false
WEB_CONCURRENCY=1
OFFICIAL_ANSWER_TIMEOUT_SECONDS=270
OFFICIAL_ANSWER_MAX_INFLIGHT=2
FINANCE_AUDIT_FSYNC_EACH_EVENT=true
BACKEND_BIND_ADDRESS=0.0.0.0
BACKEND_PORT=80
```

공개 80번 포트는 NCP ACG에서 주최 측이 공지할 평가 발신 IP만 허용한다.

## 3. 이번 최우선 구현

1. Immutable Release workflow의 provider 선택 입력을 제거했다. 보호된 main에서는
   `hyperclova + QueryPlan false + HCX-007` 외 Manifest를 발급할 수 없다.
2. HCLX 성공 metric을 HTTP 200 수신 시점이 아니라 operation별 JSON schema·Pydantic
   검증 통과 뒤에만 기록한다.
3. 인증·429·5xx·timeout·transport·응답 거절을 원인별 Audit와 metric으로 분리했다.
   질문·prompt·credential·응답 원문은 Audit에 저장하지 않는다.
4. 최종 answer-only 공식 `GET /answer`에서 모든 HCLX 장애가 검증된 deterministic
   fallback과 HTTP 200으로 복구되는 E2E를 추가했다.
5. 실제 공식 GET이 만드는 Audit 순서를 rollback 검증과 동일하게 고정했다.
   결정론적 경로 25개, HCLX answer-only 27개, QueryPlan+answer 28개 사건이다.
6. Rollback 검증이 HCLX profile·credential 파일 보안·Audit 경로·답변 mode를 검사하도록
   보완했다. 실제 운영 rollback은 과거 Binding 재사용이 아니라 새 generation의 signed
   Binding을 발급해야 한다.
7. 최종 release 환경 예시를 공개 HTTP `0.0.0.0:80`과 HCLX answer-only로 고정하고
   이를 정적 회귀 테스트로 보호했다.
8. 서명된 image·Manifest·Binding 세 개를 모두 Cosign 검증한 뒤, Manifest의 provider·
   QueryPlan·fund·relation·timeout·동시성·Audit profile과 실제 환경값을 Docker 실행 전에
   exact-match한다. 누락·불일치·빈/4KiB 초과 key file은 기존 컨테이너 교체 전에 거부한다.
9. Dockerfile을 multi-stage build로 바꿔 builder worktree와 test 입력 layer가 최종 runtime
   image에 남지 않게 했다. 전이 의존성까지 버전을 고정하고 `pip check`를 수행한다.
10. exact digest image를 network none·read-only·non-root 조건에서 실행해 local-model
    dependency·executable·일반 weight 형식·DB·XLSX·inline credential 부재와 평가 설정
    Guard를 검사한다. image 내부 Manifest 바이트도 서명 대상 Manifest SHA-256·release
    ID·source commit과 exact-match한다.
11. 실제 `build_agent_release_manifest()` 출력과 canonical serialization(항상 같은 바이트로
    만드는 직렬화), 전체 `DeploymentBinding`을 host `release_trust`에 연결해 Cosign 3단계
    검증 뒤 signed profile exact-match까지 통과하는 통합 회귀를 고정한다.

KURE 코드는 모델 snapshot·test-only smoke·low-score 관측까지만 보존한다. 이 결과는
Dense가 정확도를 개선했다는 증거가 아니므로 final runtime에는 연결하지 않는다.

## 4. 로컬 검증 결과

| 검사 | 결과 | 해석 |
| --- | --- | --- |
| Agent Core 전체 pytest | 1,480 passed, 2 skipped | skip은 로컬 sealed key와 승인 DB 경로가 있을 때만 실행하는 opt-in 재검증 |
| FastAPI Backend 전체 pytest | 432 passed | DeploymentBinding 영속 수명·충돌 차단 회귀 포함 |
| 최종 answer-only 공식 GET 집중 E2E | 17 passed | 정상·인증·429·5xx·timeout·transport·schema 오류·fallback |
| Ruff check·format | 통과, 전체 335 files | 각 package 경계의 정적 오류·포맷 불일치 없음 |
| 실제 read-only 컨테이너 Manifest 경계 | 정상 SHA 통과, 변조 SHA exit 2 | image 내부 Manifest와 서명 대상의 바이트 불일치를 fail-closed |
| Ontology 동기화 | 통과, Turtle 5개 | field registry와 exact-match |
| submission boundary development profile | 통과, blocker 0 | 평가 runtime 경계 통과; 과거 연구 파일 자체를 제거했다는 뜻은 아님 |

이 숫자는 금융 질문 generalization(처음 보는 질문에 대한 일반화 정확도)이나 공모전 예상
점수가 아니라 고정된 코드·시스템 계약 회귀다. External Blind는 이번 최종 동결에서
수행하지 않았다.

## 5. 아직 완료라고 말할 수 없는 외부 P0

다음은 코드 구현이 아니라 clean source·GitHub·NCP·실제 credential이 필요한 배포 게이트다.

2026-08-21 저장소를 public으로 전환했다. 기본 branch는 `main`이며, GitHub API로
PR 승인 1회·stale review 무효화·대화 해결 필수·관리자 포함 직접 push 차단·force push와
branch 삭제 금지 protection을 적용했다. `evaluation`·`production` Environment도 만들고
보호된 branch에서만 배포할 수 있도록 고정했다. 따라서 workflow의
`github.ref_protected=true` gate를 만족할 저장소 경계는 준비됐다.

같은 시점에 repository-level Actions variable·secret은 등록된 이름이 없었다. NCP
Registry public `/v2/`는 DNS·TLS 연결 후 무인증 HTTP 403을 반환해 endpoint 자체는
도달 가능했지만, 기존 NCP 공인 IP의 `/health`는 5초 안에 연결되지 않았다. 이는 서버를
꺼 둔 비용 절감 정책 또는 80번 포트 미개방 상태와 일치하며, 배포 성공 증거가 아니다.

모든 현재 Git ref의 도달 가능한 이력을 파일명·credential 할당 표식·private-key marker로
읽기 전용 검사한 결과, 민감 확장자·실제 `.env` 이력은 없고 추적된 환경 파일은
`.env.example`과 `.env.release.example`뿐이었다. 현재 계정의 pending repository
invitation도 0건이다.

추가로 checksum을 검증한 Gitleaks v8.18.4가 합성 GitHub PAT positive control을 실제로
탐지하는지 먼저 확인한 뒤 `--all` full history와 현재 tracked+untracked candidate tree를
100% redaction으로 검사했다. history 7건과 candidate 8건은 각각 64자 SHA-256
fingerprint, 의도된 safety test canary, 두 생성 HTML의 동일한 `<template>` 렌더링
문자열로 분류됐고 실제 credential은 없었다. 최신 v8.30.1은 공식 저장소에도
[탐지 회귀 이슈](https://github.com/gitleaks/gitleaks/issues/2170)가 있으며, v8.29.1도 같은
positive control을 잡지 못해 이 감사의 통과 근거로 사용하지 않았다. clean commit 직전에는
새 변경을 포함한 동일 positive-control-first 검사를 한 번 더 수행한다.

1. 현재 변경을 clean commit으로 만들고 PR 검토를 거쳐 main에 반영
2. `evaluation` Environment에 NCP Registry·승인 relation artifact variable과 secret 등록
3. 보호된 GitHub Environment에서 immutable workflow 실행
4. NCP Registry의 exact image digest·OCI `linux/amd64`·Cosign 서명 검증
5. workflow가 만든 바로 그 image와 승인 DB·relation artifact volume을 NCP에 기동
6. 공인 IP에서 `/health`와 공식 `GET /answer` 정상·CLARIFY·UNSUPPORTED·not-found 확인
7. live HCLX 정상 응답 1회와, exact release image의 fault injection으로
   timeout·429·5xx Audit·fallback·민감정보 비노출 검증
8. 새 signed Binding으로 N-1 → N → N-1 rollback 실기동
9. 주최 측 순차 호출 조건에서 p50·p95·p99·payload bytes·memory 측정

현재 working tree가 clean commit이 아니므로 AgentReleaseManifest 생성기가 실패하는 것이
정상이다. 이번 변경을 commit·push한 뒤 PR 검토·승인을 받아 main에 반영해야 한다.

## 6. 잔존 운영 위험

- `urllib`의 socket timeout은 전체 wall-clock이 아니라 개별 blocking I/O 상한이다.
  공식 서버가 짧은 간격으로 매우 느리게 응답하는 특수 상황에서는 45초를 넘길 수 있다.
  바깥 270초 deadline은 응답을 종료하지만 실행 중 thread를 강제 종료하지 않는다.
  NCP 최종 smoke에서 worker drain과 재시도 시나리오를 확인하고, 필요하면 total-deadline을
  보장하는 cancellable HTTP client로 교체한다.
- 개발 source에는 로컬 Qwen 실험 코드와 역사 baseline이 남아 있다. evaluation·production
  runtime image에도 dormant provider source는 설치되지만 모델 dependency·weight·실행기는
  없고 공식 FastAPI assembly에서 활성화할 수 없다. 주최 측 제출 저장소가 개발 이력 자체의 제거를 요구하면
  별도 curated source package와 strict submission 검사가 필요하다.
- rollback은 relation artifact가 활성화된 최초 서명 Release부터 지원한다. 그 이전의
  `disabled_not_activated` schema Release까지 generic N-1로 지원한다고 주장하지 않는다.

이 두 항목을 숨기지 않되, 현재 검증된 평가 실행 경계를 깨는 급한 기능 확장은 하지 않는다.
