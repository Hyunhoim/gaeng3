# 결정론적 API 성능 원인 분해·Audit 검증 결과

기준일: 2026-08-14
상태: 구현 및 격리 리허설 완료, 장시간 운영 승인 시험은 대기

## 1. 결론

Dense를 추가하기 전 기본 결정론적 경로의 주 병목은 Router, QueryPlan compiler, Oracle,
evidence 또는 DTO 직렬화가 아니다. 대표 국내채권 SEARCH에서 매 요청마다 Verifier용 전체
projection을 SQLite에서 읽고 Python 객체로 materialize하는 구간이 지연의 대부분을
차지한다.

- candidate의 Answer p95 중 Verifier 비율은 c1 `96.38%`, c2 `98.23%`, c4 `99.19%`다.
- c2에서 Router p95는 `3.62 ms`, QueryPlan compiler는 `1.01 ms`, Oracle 전체는
  `16.23 ms`지만 Verifier 전체는 `1,994.22 ms`다.
- c4에서 verifier projection fetch만 p95 `6,375.91 ms`까지 증가한다.
- c1·c2·c4의 오류, timeout, overload, 계약 위반은 모두 0건이다.
- clean Stage 4와 candidate의 응답 fingerprint 19개는 모두 정확히 일치한다.
- 따라서 이번 변경은 성능 개선으로 판정하지 않는다. 병목을 안전하게 분해하고 이후 변경을
  비교할 기준선을 만든 작업이다.

다음 최적화 후보는 DB fingerprint에 묶인 immutable verifier projection cache 또는 동등한
사전 계산 projection이다. 이 변경은 Verifier의 독립 재검산 의미를 바꿀 수 있으므로 별도
설계·정확성 gate 없이 바로 구현하지 않는다.

## 2. 구현한 범위

### API 구간 계측

- Router와 QueryPlan compiler
- SQLite authority, Oracle, verifier projection connection
- Oracle SQL statement와 Oracle 전체
- verifier projection fetch, Python row materialization, universe 구성, 순수 Verifier
- answer/evidence renderer와 citation 생성
- Backend DTO, 공식 DTO, 최종 HTTP 직렬화
- 응답 byte, citation 수, 고유 evidence reference 수

### 격리 benchmark와 soak

- c1·c2·c4는 control 응답도 실패로 보는 strict normal phase
- c8만 overload/timeout의 안전한 admission control을 허용하는 참고 phase
- 동일 응답의 canonical SHA-256, payload, citation, evidence 안정성 검사
- post-load `/health`, container identity, restart, OOM, dead, runtime error 검사
- memory plateau와 file descriptor, thread, PID 변화 검사
- 측정 종료 뒤 완료된 in-flight 요청은 percentile에서 제외하고 별도 집계
- 공유 포트 `18001`, `18002`와 비-loopback 대상 명시적 거부

### Docker 실행 계약

- immutable `repository@sha256` image만 허용
- OCI revision label과 full source commit 일치
- `linux/amd64` platform 일치
- Backend `2 CPU / 1 GiB / 256 PID / nofile 4096 / restart no`
- data-init `1 CPU / 512 MiB / 128 PID / restart no`
- 고유 project, container, port, volume, owner-only Audit 경로 강제
- rendered Compose에서 위 조건이 하나라도 다르면 기동 전 실패

### Audit 검증 CLI

- strict UTF-8 JSONL, 중복 key, 비정상 숫자, record/file/count 상한 검사
- 요청별 START와 terminal event, 연속 event sequence 검사
- Router → QueryPlan → Authority → SQL → Oracle → Verifier → Answer 연결 검사
- CLARIFY/UNSUPPORTED에서 DB 실행 금지 검사
- `BLOCKED + authority_denied`만 DB 접근 전 안전 거절 증거로 허용
- 질문·prompt·credential·응답 원문과 비밀·경로 형태의 노출 검사
- release manifest, DeploymentBinding, image/source/backend hash 연결
- 승인 dataset, source snapshot, DB manifest/fingerprint 연결
- timeout, overload, fallback, failure, abort 집계
- 원본 Audit SHA-256, report SHA-256, commitment SHA-256 고정

## 3. 격리 조건

| 항목 | baseline | candidate |
| --- | --- | --- |
| source | clean Stage 4 `ea380ed9774a7bedeb2ede9e867d214cfbf9b318` | clean 임시 snapshot `f0e4f4221839e70b3d172c7049a6c39d88a71afe` |
| image digest | `sha256:a147b58fd7a6c58fbec3d2a222163f027cef5cb6309eec5f3bde0e8f0313aa3d` | `sha256:fb6ee635f5c751f3616e55c73b0ad351363a0e99527d126228349b48f8dad1ac` |
| host port | `127.0.0.1:18151` | `127.0.0.1:18152` |
| Backend 자원 | 2 CPU, 1 GiB, 256 PID | 동일 |
| worker / max inflight | 1 / 4 | 동일 |
| HCLX / Dense | OFF / OFF | OFF / OFF |
| 데이터 | 같은 공식 원본에서 독립 volume 생성 | 같은 공식 원본에서 독립 volume 생성 |

공유 `127.0.0.1:18001`과 `127.0.0.1:18002`의 container, network, volume에는 부하를
보내거나 상태를 변경하지 않았다.

## 4. strict benchmark 결과

단위는 ms다. 각 c1·c2·c4 phase는 20건이며 오류·timeout·overload가 모두 0일 때만
통과한다.

| 대상 | 동시성 | p50 | p95 | p99 | 오류·timeout·overload |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 1 | 510.452 | 563.149 | 565.505 | 0 |
| candidate | 1 | 527.441 | 580.292 | 690.984 | 0 |
| baseline | 2 | 1,382.884 | 1,773.521 | 1,878.103 | 0 |
| candidate | 2 | 1,466.930 | 2,042.372 | 2,052.422 | 0 |
| baseline | 4 | 6,073.657 | 6,958.981 | 6,966.358 | 0 |
| candidate | 4 | 5,856.029 | 6,859.090 | 7,002.202 | 0 |

c8 참고 phase에서는 baseline과 candidate 모두 24건 중 12건이 정상 실행되고 12건이
`request_overloaded`로 DB 실행 전에 차단됐다. 이는 오류가 아니라 max inflight 4의
의도된 admission control이다. c8 결과는 c1·c2·c4 성능 성공 판정에 사용하지 않는다.

candidate p95는 baseline 대비 c1 `+3.04%`, c2 `+15.16%`, c4 `-1.44%`다. 알고리즘을
최적화하지 않았고 계측과 공유 호스트 잡음이 포함되므로 이를 개선 또는 회귀의 확정값으로
해석하지 않는다. 핵심 성과는 같은 실행에서 병목 구간을 분리한 것이다.

모든 strict phase의 공식 API payload는 `13,872 bytes`로 동일했다. 짧은 soak의 동일
질문에서는 `13,863 bytes`, citation 36개, 고유 evidence reference 36개가 계속 유지됐다.

### cold 구분

benchmark의 cold 필드는 container 기동과 runner 시작 사이의 준비 대기까지 포함해 이번
실행의 실제 cold latency로 사용할 수 없다. 별도 즉시 probe 1회에서는 baseline `2,131 ms`,
candidate `2,610 ms`가 측정됐다. 표본이 각각 1개뿐이므로 참고값이며, cold p95 기준선으로
고정하지 않는다.

## 5. candidate 구간별 p95

| 구간 | c1 | c2 | c4 |
| --- | ---: | ---: | ---: |
| Router | 3.289 | 3.621 | 3.007 |
| QueryPlan compiler | 0.817 | 1.014 | 1.076 |
| Oracle SQL statements | 5.162 | 11.507 | 25.311 |
| Oracle 전체 | 8.317 | 16.227 | 35.794 |
| verifier projection fetch | 180.463 | 1,430.973 | 6,375.908 |
| Python row materialization | 324.803 | 706.224 | 571.454 |
| verifier universe 전체 | 509.921 | 1,933.674 | 6,739.784 |
| 순수 Verifier | 42.998 | 62.052 | 59.229 |
| Verifier 전체 | 552.493 | 1,994.216 | 6,783.917 |
| answer/evidence renderer | 0.939 | 1.293 | 1.266 |
| citation 생성 | 0.138 | 0.215 | 0.189 |
| Backend DTO | 0.218 | 0.314 | 0.274 |
| 공식 DTO | 0.732 | 0.716 | 0.736 |
| HTTP 직렬화 | 0.381 | 0.403 | 0.387 |
| Answer 전체 | 573.221 | 2,030.125 | 6,839.473 |

각 행의 percentile은 서로 다른 요청에서 나올 수 있으므로 하위 구간 p95를 단순 합산하지
않는다. 포함 관계인 `verifier universe 전체`와 그 하위 fetch/materialization도 중복해서
전체 지연으로 더하지 않는다.

## 6. 짧은 soak 리허설

각 동시성에서 10초 warm-up 후 30초를 측정했다. 운영 승인용 15분 soak가 아니라 hardened
gate의 실제 작동을 확인하는 리허설이다.

| 동시성 | 포함 요청 | 제외된 late completion | 실패 | p95 ms | memory 증가 | tail slope B/s | FD·thread·PID 누수 | runtime 판정 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | 51 | 1 | 0 | 760.610 | 700,416 B | -16,341 | 없음 | 통과 |
| 2 | 34 | 2 | 0 | 1,834.405 | 1,073,152 B | -65,355 | 없음 | 통과 |
| 4 | 12 | 4 | 0 | 8,256.490 | 782,336 B | 35,496 | 없음 | 통과 |

세 실행 모두 fingerprint, payload, citation, evidence가 고정됐고 post-load health, 실행 중
Audit 상태, container identity, restart/OOM/dead/runtime error, memory plateau 검사를
통과했다. late completion은 측정 종료 전에 시작했지만 종료 뒤 완료된 요청이며
percentile에서 정확히 제외됐다.

단, 각 soak JSON의 `queue_integrity.validated`와 `complete_success_gate_passed`는 `false`다.
runner가 살아 있는 컨테이너 내부의 Audit queue drain을 스스로 증명할 수 없기 때문이다.
표의 통과는 runtime 하위 gate 판정이며 최종 soak 성공 판정이 아니다. 세 실행을 마친 뒤
container를 정상 종료하고 결합 Audit JSONL에 CLI를 실행해 lifecycle·sequence issue 0건을
확인했지만, 이번 30초 리허설은 per-soak queue drain 약정까지 따로 고정하지 않았으므로 최종
release soak gate는 대기 상태로 둔다.

## 7. Audit와 정확성 검증

### strict performance 실행

| 항목 | baseline | candidate |
| --- | ---: | ---: |
| Audit event | 1,052 | 5,461 |
| invocation | 98 | 238 |
| lifecycle 완결 | 98 | 238 |
| 성공 실행 경로 완결 | 76 | 216 |
| timeout / failure / fallback | 0 / 0 / 0 | 0 / 0 / 0 |
| 예상 overload | 12 | 12 |
| issue | 0 | 0 |
| Audit SHA-256 | `f6b820877642229bdb1806427cc10532d2858a9d2c6da256ce36e1091a478f46` | `bc4a57ee62ffeb3e5472debd35d77e13967603f3a102f9504288e0d22240bd18` |
| report SHA-256 | `8a4ba381b1289529e4ec435cb1f2082602083828c4c2218acdf7cd44497be163` | `c61488c588284199c805f36935eceb74624d08f3b85167c3e8820b1b31e193b6` |
| commitment SHA-256 | `fb527c491b5284fa02447620847d2a4960ef1c2c742d38cf1e91dd7974cdb420` | `86bb64015724a21e250bb0349db851aebad4e252dd94971fb0fd3302eca77a8b` |

baseline과 candidate benchmark의 semantic case와 phase outcome fingerprint 19개는 모두
일치했다. candidate Audit의 질문 ID hash, 질문 hash, warm c1·c2·c4 순서, offset, invocation
수도 candidate benchmark report와 일치했다.

자동으로 가장 많은 request hash를 선택한 최초 분석은 같은 파일의 soak 요청을 골라
`audit_request_id_mismatch`로 실패했다. benchmark report에 고정된 request ID SHA-256을
명시한 재실행만 통과했다. 이는 다른 실행의 Audit를 섞어도 성공하는 fail-open을 막는
검증 사례다.

### HCLX 없는 local evaluation release

clean candidate snapshot과 digest-pinned image로 별도 local evaluation release를 구성했다.
HCLX와 Dense는 모두 OFF다.

- release manifest SHA-256:
  `177aadf10c8d73f7d383564e106ec413c8f40676fe487ca161c27f1a59e46dce`
- DeploymentBinding SHA-256:
  `dee01e29b0a46891332533244127b28e25106b90ad6143b9c6bbc2fe19b61a9c`
- Backend smoke 7/7, 공식 API smoke 7/7
- Audit event 137개, invocation 14개, lifecycle 14개 완결
- 성공 실행 4개와 실행 경로 4개 완결
- release 연결 event 137개
- dataset·DB fingerprint 연결 event 44개
- timeout, failure, fallback, overload, issue 모두 0
- Audit SHA-256:
  `cd08e783c5b92e1af0f7f5bfb9fa9d2d3fec868782ee8ef929091015c77362b5`
- report SHA-256:
  `29d32136f4def5d1c9000b616394f4686646fd558d6c703eb40346ef05d88d83`
- commitment SHA-256:
  `a9980bd13dab888b50574edcb87d019843b8cceb7d78a9c61f3e56088eb562a1`

이 release는 linkage 기능 검증용 local rehearsal이다. 사용자 브랜치의 최종 commit이나 NCP
배포 release가 아니다. 실제 배포 전에는 승인된 clean commit에서 manifest, image digest,
Binding과 Audit 약정을 다시 생성해야 한다.

## 8. Audit fsync 분리 측정

동일 임시 filesystem에서 20회 warm-up 후 500회를 측정했다.

| 모드 | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| append only | 0.031 | 0.034 | 0.036 |
| append + fsync | 2.440 | 8.249 | 11.853 |
| 추정 fsync 증가분 | 2.409 | 8.215 | 11.817 |

서비스는 bounded async Audit queue를 사용하므로 이 값을 HTTP latency에 그대로 더하지 않는다.
정상 종료 뒤 lifecycle·sequence 검증을 함께 통과해야 queue drain 증거로 사용한다.

## 9. 코드 회귀와 품질 gate

- Finance Agent Core 전체: `1,264 passed, 1 skipped`
- 공식 DB 경로가 있어야 하는 Stage 2 gate 별도 실행: `2 passed`
- FastAPI Backend 전체: `307 passed`
- 변경 구간 집중 회귀: `240 passed`; 최종 Audit CLI P1 보강 후 `39 passed`
- 문서·동결 baseline 검사: `66 Markdown files`, `47 evaluation baselines` 통과
- 저장소 고정 Ruff `0.16.0`: Core 259개·Backend 51개 파일의 lint와 format 통과
- `git diff --check`: 통과

Core의 1건 skip은 `FINANCE_STAGE2_DATABASE_DIR`가 없는 일반 테스트 환경에서 의도된
조건부 skip이다. 동일 gate를 승인된 read-only DB 경로로 별도 실행해 2건 모두 통과했다.

## 10. 남은 한계와 다음 안전한 단계

1. 30초 soak는 도구 리허설이다. release 후보에서는 c1·c2·c4 각각 최소 60초 warm-up과
   15분 측정을 다시 수행해야 한다.
2. 이번 병목 분석은 대표 국내채권 SEARCH 한 질문이다. 다른 상품군과 COMPARE·AGGREGATE도
   같은 구간 분해로 확인해야 한다.
3. cold는 표본 1개뿐이다. clean image pull/cache 조건을 분리한 반복 측정이 필요하다.
4. 정적 Audit 파일만으로 통째로 기록되지 않은 invocation을 단독 증명할 수는 없다.
   benchmark 요청 수·phase hash 바인딩, 정상 종료, Audit lifecycle 검증을 함께 사용해야 한다.
5. 실제 NCP Registry, NCP 서버, HCLX 호출은 이번 작업 범위가 아니다.
6. Verifier projection 최적화 전에는 DB fingerprint 교체, 네 상품군, 동시성, 정확성 fingerprint,
   memory 상한을 포함한 별도 설계와 회귀가 필요하다.

현재 데이터로는 Dense 또는 Re-ranker를 추가해도 이 대표 경로의 주 지연을 해결하지 못한다.
먼저 Verifier projection 비용을 줄이고, 기존 응답 fingerprint와 독립 재검산 의미가 정확히
보존되는지 확인한 뒤 Dense shadow의 추가 비용을 비교하는 순서가 안전하다.
