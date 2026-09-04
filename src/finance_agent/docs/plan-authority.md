# QueryPlan 실행 권한 경계

기준일: 2026-08-12
상태: Stage 2 구현 완료 · Stage 3 release binding 연결 완료

## 1. 결론

일반 `QueryPlan`은 더 이상 SQLite/Python 실행 권한이 없다. 서버가 현재 요청,
라우팅, capability, Ontology/registry, 승인 데이터, 시간·행 예산을 다시 검증해
발급한 `ValidatedPlan`만 Oracle과 Verifier 재조회 경로에 들어갈 수 있다.

```text
ProposedQueryPlan
  → PlanAuthorityGate
  → ValidatedPlan + ValidationReceipt
  → read-only Oracle / Verifier projection
```

- `ProposedQueryPlan`: HCLX나 서버 compiler가 만든 실행 전 계획 제안
- `PlanAuthorityGate`: 계획을 정규화하고 현재 서버 계약과 맞는지 판정하는 문지기
- `ValidatedPlan`: 검증된 계획의 고정 JSON, 감사 영수증, 프로세스 내부 seal을 묶은 실행 권한
- `Oracle`: 검증된 계획만 read-only SQL/Python으로 실행하는 계산기

## 2. Gate가 확인하는 항목

- `QueryPlan` 전체 schema 재검증: field, operator, value type, unit, ranking direction,
  projection, intent payload, limit
- 남은 `ambiguities` 또는 `unsupported_conditions`가 없는지 확인
- 최종 `RouteDecision`과 request ID, intent, 상품군 순서가 같은지 확인
- `PlanningTrace`를 다시 만들어 Stage 1 `PlanningDecision`과 최종 route가 같은지 확인
- capability matrix에서 해당 상품군·intent·Oracle mode가 실행 가능한지 확인
- production에서는 공식 승인 manifest와 SQLite snapshot/hash가 일치하는지 확인
- 활성 `RequestDeadline`과 candidate/result/verifier row budget을 영수증에 고정
- 교차 상품군 검색은 모든 자식 계획을 먼저 승인한 뒤에만 worker를 시작

## 3. ValidationReceipt

영수증에는 다음 실행 조건을 기록한다.

- plan·route·planning decision·질문의 SHA-256
- capability matrix, field registry, Ontology bundle의 version과 SHA-256
- 상품군, 공식 release ID, 승인 manifest·SQLite·원천 파일 hash, snapshot 기준일
- Agent release ID, AgentReleaseManifest file hash, DeploymentBinding file hash와
  release context hash
- 실제 계획 생성 경로와 버전
  - `server_queryplan_compiler`
  - `grounded_plan_gate`와 provider/model 이름
  - `legacy_provider`
  - `internal_evaluation`
- verifier/core version, deadline, 결과·후보·검증 universe 행 수
- 교차 상품군 실행 순서와 전체 개수

영수증은 감사 자료일 뿐 실행 권한 자체가 아니다. `ValidatedPlan`의 HMAC seal은
프로세스마다 임의 생성되고 직렬화에서 제외되므로, 저장된 JSON이나 변조한 Pydantic
객체로 실행 권한을 복원할 수 없다.

## 4. 실행 경로별 정책

| 경로 | 정책 |
| --- | --- |
| FastAPI/production | `RoutedFinanceAgent`, 공식 승인 DB, 활성 deadline, 서버 검증 계획만 허용 |
| Grounded planning | 모델 제안의 원문 근거와 계약을 검증하고 실제 compiler provenance를 별도 기록 |
| Legacy `FinanceAgent` | `allow_unapproved_database=True`를 명시한 offline 평가 전용; production 사용 금지 |
| 평가 runner·suite generator | 별도 `authorize_internal_evaluation_plan()` issuer만 사용 |

`SQLiteOracle`, `SQLiteAggregateOracle`, `load_projected_verifier_records()`는 raw
`QueryPlan`을 DB 연결 전에 거절한다. 승인 후 DB 파일이 바뀌거나 deadline이
사라지거나 seal·receipt·row budget이 달라져도 실행을 중단한다.

승인 시 DB를 `O_NOFOLLOW` read-only descriptor로 열고 실제 descriptor bytes의 SHA-256과
inode fingerprint를 고정한다. SQLite가 `/proc/self/fd` 경로를 다시 열 때도 SQLite가
실제로 연 descriptor의 device·inode가 승인 descriptor와 같은지 확인한다. 따라서 경로가
승인 후 A→B로 바뀌거나 잠시 B로 교체됐다 A로 복원돼도 B의 결과를 반환할 수 없고,
승인된 A를 계속 읽거나 결과를 폐기한다. descriptor discovery와 close는 같은 lock으로
직렬화해 병렬 상품군 검색의 FD 번호 재사용(ABA race)도 차단한다.

FastAPI의 `create_app(agent=...)` 테스트 주입 seam은 development/testing에서만
사용할 수 있다. evaluation/production은 외부 주입을 거부하고, 설정과 같은 네 DB를
가진 `RoutedFinanceAgent(require_approved_databases=True)` 조립만 시작한다.

## 5. 외부 계약과 검증 상태

- Backend 공개 request/response DTO는 변경하지 않았다.
- `ValidatedPlan`, receipt, seal은 응답에 노출하지 않는다.
- Agent Core 전체 회귀 `1,061 passed`, FastAPI Backend 전체 회귀 `162 passed`
- `test_plan_authority.py` `44/44`, 병렬 pinned SQLite 집중 반복 `10/10`
- 네 상품군 실제 승인 DB를 read-only로 연결한 SEARCH·AGGREGATE·COMPARE 동결 계약
  `62/62`; HCLX·Qwen 호출 0회
- `finance_agent_core` 패키지·Backend Ruff lint/format 통과
- 현재 승인 네 DB의 기존 8문항 fresh-process Core benchmark: `8/8`, p50
  `328.752ms`, p95/최대 `1,741.539ms`, 최대 추가 RSS `40,756KiB`
- 승인 DB·55초 deadline을 사용한 동일 프로세스 production authority 채권
  SEARCH smoke: 두 번 모두 후보 254개·결과 3개, cold `981.463ms`, warm `514.023ms`

위 숫자는 현재 작업 트리의 배선·회귀 결과이며 독립 blind 정확도나 공모전 예상
점수가 아니다.

## 6. 아직 남은 범위

- Stage 3 코드 경계는 [AgentReleaseManifest 배포 계약](agent-release-manifest.md)에
  연결됐다. localhost 합성 Registry push와 rollback mechanics는 검증했지만 clean commit의
  NCP image·cosign 서명·공식 Binding·NCP rollback은 외부 환경 준비 후 수행해야 한다.
- 위 측정은 cold/fresh-process 8개와 단일 smoke뿐이다. 같은 프로세스 warm 반복,
  production 전체 8경로, 동시 요청 처리량과 Stage 2 전후 통제 A/B는 추가 측정 필요
- 과거 `search_aggregate_performance_8`의 normalized DB byte hash는 현재 승인 DB와
  달라 legacy benchmark가 실행 전 중단됐다. 원천 workbook 계약과 현재 승인
  manifest를 검증하는 `approved_sql_baseline`으로 측정했으며, 과거 동결 hash는
  임의 갱신하지 않았다.
- 현재 SEARCH/AGGREGATE Verifier projection은 기본 cache 미주입 시 전체 상품
  universe를 다시 읽으므로 실서비스 규모의 O(N) 병목 개선 필요
- 실제 HyperCLOVA X credential·응답·latency 검증은 별도 연결 단계에서 수행
- 이 방어는 read-only container mount와 신뢰된 host 관리자 경계를 전제로 한다. host root가
  동일 inode bytes를 변경하고 metadata까지 복원하는 공격까지 증명하려면 fs-verity 또는
  별도 immutable snapshot 같은 OS 수준 장치가 추가로 필요하다.

따라서 현재 상태는 **한 요청의 실행 조건을 식별하고 우회 실행을 막는 Stage 2**와
**그 실행 조건을 하나의 immutable release에 연결하는 Stage 3 코드 경계**까지다. 실제
registry artifact 발급과 무중단 rollback이 완료됐다는 뜻은 아니다.
