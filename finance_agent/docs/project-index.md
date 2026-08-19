# AI 기술문서 상세 인덱스

마지막 갱신: 2026-08-20

이 문서는 금융상품 Agent의 전체 기술문서와 상태를 추적하는 상세 장부다. 간단한
길잡이가 필요하면 먼저 [AI 기술문서 안내](README.md)를 확인한다. 연구 요청·외부
모델 답변·감사 산출물은 근거 자료로 보존하되, 실제 구현 판단은
`project-baseline.md`와 `data-audit.md`를 우선한다.

## 1. 빠른 시작

모든 문서를 순서대로 읽을 필요는 없다. 처음에는 다음 문서만 확인한다.

1. [현재 프로젝트 기준](project-baseline.md) — 목표, 공식 제약, 역할과 현재 상태
2. [금융상품 Agent capability matrix](capability-matrix.md) — 지금 가능한 질문과 불가능한 질문
3. [HyperCLOVA X 연결 전 준비 기준](pre-hcx-readiness.md) — 완료된 일과 남은 게이트
4. [재현 가능한 평가 baseline](../evaluation/README.md) — 성능 수치와 실험 해석
5. [P0-4 공식 Acceptance 인수인계](p0-4-official-acceptance-handover-2026-08-19.md) — 공식 GET 계약, Docker 검증, 다음 작업
6. [P0-8 재시도 계약 인수인계](p0-8-retry-contract-handover-2026-08-19.md) — 200·503·504, 270초, 동일 요청 중복 실행 방지
7. [P0-5 외부 문서 반입 계약](p0-5-external-corpus-intake-2026-08-19.md) — 독립 승인, 출처·권한·해시 봉인, 변조 차단, BM25 색인 전 게이트
8. [P0-6 제공 관계 검색 인수인계](p0-6-provided-relation-retrieval-handover-2026-08-19.md) — 공식 DB 관계 58,005개, 출처·기준일, 상품 ID 재검증, Agent 비활성 경계
9. [P0-7 관계·문서 계획과 주장 검증 인수인계](p0-7-knowledge-claim-verifier-handover-2026-08-20.md) — Typed Plan, exact 권한 gate, Claim Verifier, 결정론적 fallback, 공개 API 비활성 경계

배포·복구 담당자는 [NCP immutable release CI](immutable-ncp-release-ci.md)와
[Rollback drill runbook](../../fastapi_backend/ROLLBACK_DRILL.md)을 추가로 확인한다. 현재
승인 DB로 Stage 2 실행 결과를 다시 계산한 증거는
[Stage 2 승인 DB 재검증 baseline](../evaluation/baselines/stage2-approved-db-revalidation-2026-08-12.json)이다.

2026-08-06 설명회 자료는 [반영 기록](../../docs/proposal/briefing-2026-08-06.md)에
확정·잠정·미확정으로 나눠 정리했다. 평가 API와 Ontology 요구는 확인됐지만 정확한
HCX 모델·endpoint·인증과 크레딧 적용 범위는 남아 있어 기존의 보수적인 모델 정책을 유지한다.

## 2. 역할별 읽기 경로

| 독자·목적 | 먼저 읽기 | 이어서 읽기 |
| --- | --- | --- |
| 새 AI 개발자 | [현재 프로젝트 기준](project-baseline.md) | [개발 환경](development.md) → [계약](contracts.md) → [Agent Core README](../packages/finance_agent_core/README.md) |
| Backend 담당 | [Backend DTO](backend-contract.md) | [P0-4 공식 Acceptance](p0-4-official-acceptance-handover-2026-08-19.md) → [P0-8 재시도 계약](p0-8-retry-contract-handover-2026-08-19.md) → [P0-6 관계 검색](p0-6-provided-relation-retrieval-handover-2026-08-19.md) → [P0-7 주장 검증](p0-7-knowledge-claim-verifier-handover-2026-08-20.md) → [HyperCLOVA X provider](hyperclova-provider.md) |
| 금융 도메인 담당 | [capability matrix](capability-matrix.md) | [데이터 감사](data-audit.md) → [P0-5 외부 문서 반입](p0-5-external-corpus-intake-2026-08-19.md) → [P0-6 관계 값·alias 검수](p0-6-provided-relation-retrieval-handover-2026-08-19.md) → [사람 평가 rubric](human-evaluation.md) → [금융 도메인 QA 평가](evaluation-domain-qa.md) |
| 기술 제안서 작성자 | [기술 제안서 허브](../../docs/proposal/README.md) | [현재 프로젝트 기준](project-baseline.md) → [평가 baseline](../evaluation/README.md) |
| 제출 전 검수자 | [제출 체크리스트](../../docs/proposal/submission-checklist.md) | [모델 경계](submission-model-boundary.md) → [연결 전 준비 기준](pre-hcx-readiness.md) |
| 배포·복구 담당 | [NCP immutable release CI](immutable-ncp-release-ci.md) | [AgentReleaseManifest](agent-release-manifest.md) → [Rollback drill runbook](../../fastapi_backend/ROLLBACK_DRILL.md) |

## 3. 목적별 문서 분류

### 현재 상태와 운영 기준

- [현재 프로젝트 기준](project-baseline.md)
- [개발 환경과 구현 상태](development.md)
- [HyperCLOVA X 연결 전 준비 기준](pre-hcx-readiness.md)
- [제출용 모델 경계](submission-model-boundary.md)

### 데이터와 실행 계약

- [데이터 감사 기준](data-audit.md)
- [공모펀드 원천 데이터 계약](public-fund-contract.md)
- [Field Registry와 QueryPlan 계약](contracts.md)
- [QueryPlan 실행 권한 경계](plan-authority.md)
- [AgentReleaseManifest 배포 계약](agent-release-manifest.md)
- [NCP immutable release CI](immutable-ncp-release-ci.md)
- [Rollback drill runbook](../../fastapi_backend/ROLLBACK_DRILL.md)
- [Ontology 제출 계약](ontology.md)
- [capability matrix](capability-matrix.md)
- [Backend DTO](backend-contract.md)
- [HyperCLOVA X provider 계약](hyperclova-provider.md)

### Agent 기능과 아키텍처

- [네 상품군 공통 AGGREGATE](aggregate-engine.md)
- [네 상품군 공통 COMPARE](comparison-engine-design.md)
- [교차 상품군 SEARCH](cross-family-search.md)
- [BM25/SQLite FTS 문서 RAG](document-rag.md)
- [P0-6 제공 데이터 관계 검색](p0-6-provided-relation-retrieval-handover-2026-08-19.md)
- [P0-7 관계·문서 계획과 주장 검증](p0-7-knowledge-claim-verifier-handover-2026-08-20.md)
- [Dense Schema Linker 오프라인 컴포넌트 평가](evaluation-dense-schema-linker-shadow.md)
- [Schema Dense CPU 임베딩 모델 비교](evaluation-schema-embedding-cpu.md) · [팀 공유용 HTML](evaluation-schema-embedding-cpu.html)
- [Schema Dense Stage 4 구현·Stage 5 OFF 실험 상태](schema-dense-stage4-stage5-readiness-2026-08-13.md)
- [Stage 4 감사·blind 리허설·image·API 기준선](stage4-audit-blind-image-api-report-2026-08-13.md)
- [근거 기반 최종 답변](evaluation-grounded-answers.md)

### 평가와 품질

- [해외 ETP](evaluation.md) · [국내 ETP](evaluation-domestic-etp.md) · [국내채권](evaluation-domestic-bond.md) · [공모펀드](evaluation-public-fund.md)
- [공모펀드 blind 설계](evaluation-public-fund-blind-v1.1.md)
- [COMPARE 공개 회귀](evaluation-product-comparison.md)
- [SEARCH·AGGREGATE 성능](evaluation-search-aggregate-performance.md)
- [결정론적 API 성능 원인 분해·Audit 검증 결과](../../fastapi_backend/docs/deterministic-performance-audit-report-2026-08-14.md)
- [internal red-team](evaluation-internal-red-team.md)
- [공식 형식 30문항 공개 모의평가](evaluation-official-mock.md)
- [Qwen 변형 질문·전체 Agent 스트레스 평가](evaluation-qwen-metamorphic.md)
- [네 상품군 자동 커버리지·Qwen 자연화 평가](evaluation-coverage-guided.md)
- [금융 도메인 QA](evaluation-domain-qa.md)
- [사람 평가 rubric](human-evaluation.md)
- [P0-4 공식 Acceptance 인수인계](p0-4-official-acceptance-handover-2026-08-19.md)
- [P0-8 평가기 재시도 계약 인수인계](p0-8-retry-contract-handover-2026-08-19.md)
- [P0-5 외부 금융 문서 반입 계약](p0-5-external-corpus-intake-2026-08-19.md)
- [P0-6 제공 데이터 관계 검색 인수인계](p0-6-provided-relation-retrieval-handover-2026-08-19.md)
- [P0-7 관계·문서 계획과 주장 검증 인수인계](p0-7-knowledge-claim-verifier-handover-2026-08-20.md)
- [전체 평가 baseline](../evaluation/README.md)
- [Stage 2 승인 DB 재검증 baseline](../evaluation/baselines/stage2-approved-db-revalidation-2026-08-12.json)

### 과거 기록과 연구 근거

- [Agent Core v0.1 마일스톤](milestones/2026-07-29-agent-core-v0.1.md)
- [저장소 부트스트랩 작업 명세](prompts/01-repository-bootstrap.md)
- [Agent 전략 연구 요청](prompts/02-agent-strategy-research.md)
- [GPT Pro 연구 기록](research/2026-07-28-gpt-pro/README.md)

과거 기록과 연구 문서는 설계 배경을 설명하지만 현재 구현 요구사항은 아니다.

## 4. 전체 문서 지도

| 문서 | 역할 | 상태 |
| --- | --- | --- |
| [현재 프로젝트 기준](project-baseline.md) | 공식 제약, 모델 정책, 역할 분담, 목표 아키텍처, 우선순위 | 현재 정본 |
| [데이터 감사 기준](data-audit.md) | 상품군별 grain·결측·sentinel·손상 행·검색 허용 범위 | 현재 정본 |
| [공모펀드 원천 데이터 계약](public-fund-contract.md) | 공모펀드 grain·field capability·품질 규칙·실행 승인 조건 | P1 정본 |
| [공모펀드 계약 감사 노트북](../notebooks/public-fund-contract-audit.ipynb) | product-grain 전수 감사 재현 흐름과 품질 회귀 | 재현 보조 |
| [Field Registry와 QueryPlan 계약](contracts.md) | 네 상품군 field capability, 서버 QueryPlan, HCX schema subset | P1 정본 |
| [QueryPlan 실행 권한 경계](plan-authority.md) | ProposedQueryPlan→ValidatedPlan 발급, receipt·seal·dataset/deadline/row budget, Oracle 우회 차단 | Stage 2 구현 |
| [AgentReleaseManifest 배포 계약](agent-release-manifest.md) | 코드·Prompt·Model·index·공식 데이터와 detached Docker image binding, startup·host anti-replay fail-closed | Stage 3 코드 경계 구현·NCP release 대기 |
| [NCP immutable release CI](immutable-ncp-release-ci.md) | 보호된 main에서 NCP push·exact digest·OCI platform/label·cosign OIDC 서명 검증 | 코드 계약 구현·외부 Registry 실행 대기 |
| [Rollback drill runbook](../../fastapi_backend/ROLLBACK_DRILL.md) | N-1 → N → N-1 image·Binding·DB volume·health·대표 `/answer` 복귀 검증 | localhost 합성 실기동 완료·공식 NCP 두 release 대기 |
| [Ontology 제출 계약](ontology.md) | 공식 Turtle 5개·registry 기반 생성·문법·정합성 검사 | v1.0 구현 |
| [P0-4 공식 Acceptance 인수인계](p0-4-official-acceptance-handover-2026-08-19.md) | 설명회 예시·공식 GET·다섯 문자열·UTF-8·안전 무실행의 Docker 검증과 재현 절차 | P0-4 완료 |
| [P0-8 평가기 재시도 계약 인수인계](p0-8-retry-contract-handover-2026-08-19.md) | 답변·제어 200, 일시 장애 503, timeout 504, 270초 outer deadline과 동일 요청 single-flight·replay | P0-8 완료 |
| [P0-5 외부 문서 반입 계약](p0-5-external-corpus-intake-2026-08-19.md) | 금융·권한 독립 review, HTTPS 출처, 사용 권한 4종, byte·정규화 hash, canonical manifest, 변조·경로 차단 | 반입 코드 완료·실제 corpus 승인 대기 |
| [P0-6 제공 관계 검색 인수인계](p0-6-provided-relation-retrieval-handover-2026-08-19.md) | 승인 DB의 발행사·운용사·기초지수·자산·지역 관계 색인, 출처·기준일, 공식 상품 ID 재조회 | 실제 58,005관계 검증·Agent 연결 대기 |
| [P0-7 관계·문서 계획과 주장 검증 인수인계](p0-7-knowledge-claim-verifier-handover-2026-08-20.md) | 관계·문서 Typed Plan, exact 권한 gate, evidence 주장 대조, 모델 실패·환각 fallback | 내부 계약 22/22·실제 관계 smoke 4/4·공개 Router/Release 연결 대기 |
| [해외 ETP 핵심 평가 기준선](evaluation.md) | 동결 50문항, oracle·채점 규칙, 최초 holdout과 사후 회귀 결과 | v1.0 정본 |
| [국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md) | 국내 ETP 동결 50문항, 품질 계약, local-inference split 결과 | v1.0 정본 |
| [국내채권 핵심 평가 기준선](evaluation-domestic-bond.md) | 국내채권 동결 50문항, stale·날짜 계약, 로컬 Qwen·답변 결과 | v1.0 정본 |
| [공모펀드 핵심 평가 기준선](evaluation-public-fund.md) | Oracle·최초 SEARCH parser holdout 9/10·답변 50/50·COMPARE 20/20·자연어 COMPARE 통합 E2E 24/24 | v1.6 정본 |
| [공모펀드 blind v1.1 평가 설계](evaluation-public-fund-blind-v1.1.md) | 독립 100문항 분포·역할 분리·hash 봉인·최초 실행 프로토콜 | 작성 준비 |
| [근거 기반 최종 답변 평가](evaluation-grounded-answers.md) | Answer Verifier, 최소권한 LLM 입력, 폴백, SEARCH·COMPARE·자연어 통합 E2E 결과 | v1.3 정본 |
| [개발 환경과 현재 구현 상태](development.md) | Git branch, Conda + pip, 검증 명령, 템플릿 통합 경계 | 현재 정본 |
| [로컬 LLM 테스트 런타임](local-llm.md) | 격리된 Qwen/vLLM 환경, 안전 경계, 재현 가능한 E2E | 개발 전용 |
| [Agent Core v0.1 마일스톤](milestones/2026-07-29-agent-core-v0.1.md) | 시작 상태, 채택 결정, 구현·실험·검증·다음 단계 인수인계 | 완료 |
| [재현 가능한 평가 baseline](../evaluation/README.md) | Git에서 제외된 전체 report 대신 집계 지표·hash·재현 조건 보존 | v1.0 |
| [Stage 2 승인 DB 재검증 baseline](../evaluation/baselines/stage2-approved-db-revalidation-2026-08-12.json) | 현재 승인 네 DB로 SEARCH·AGGREGATE·COMPARE 62건 지문을 다시 계산한 공개 회귀 | 62/62·독립 blind 아님 |
| [Stage 3 release 계약 baseline](../evaluation/baselines/stage3-release-contract-2026-08-12.json) | Manifest·Binding·activation anti-replay·trust·rollback harness·SQLite authority 계약 | 145/145·무네트워크 |
| [Stage 3 localhost OCI·rollback baseline](../evaluation/baselines/stage3-local-oci-rollback-2026-08-12.json) | exact Registry digest와 합성 N-1→N→N-1 실기동 | 7/7·trust stub·NCP 아님 |
| [HyperCLOVA X 연결 전 준비 기준](pre-hcx-readiness.md) | API 연결 전 구현·진단·계약·외부 게이트와 단계별 증거 추적 | 진행 중 |
| [연결 전 진단·외부 blind 프로토콜](evaluation-pre-hcx-diagnostic.md) | 네 상품군·일곱 intent 내부 진단과 금융 도메인 담당자 external blind 봉인 | v1.0 |
| [금융상품 Agent capability matrix](capability-matrix.md) | 네 상품군·일곱 intent 실행·역질문·미지원 범위와 자동 정합성 검사 | v1.0 |
| [네 상품군 공통 AGGREGATE 엔진](aggregate-engine.md) | 함수·그룹·통화·결측·기준일·독립 verifier·Backend evidence 계약 | v1.0 |
| [네 상품군 공통 COMPARE 엔진 설계](comparison-engine-design.md) | 상품군별 비교 필드·식별·통화·기준일·결측·공통 evidence 설계 | v1.0 구현 |
| [네 상품군 자연어 COMPARE 공개 회귀](evaluation-product-comparison.md) | 세 상품군 신규 30문항과 기존 공모펀드 24문항의 비교 배선·안전 경계 | v1.0 정본 |
| [SEARCH·AGGREGATE 성능 기준선](evaluation-search-aggregate-performance.md) | 네 상품군 8문항의 결과 지문·새 프로세스 지연·RSS와 projected verifier 전후 비교 | v1.0 정본 |
| [결정론적 API 성능 원인 분해·Audit 검증 결과](../../fastapi_backend/docs/deterministic-performance-audit-report-2026-08-14.md) | c1·c2·c4 strict HTTP, Router~직렬화 구간, short soak, Audit CLI와 local evaluation release linkage | Verifier projection 병목 확정·장시간 soak/NCP 대기 |
| [교차 상품군 병렬 SEARCH와 grounded answer v2](cross-family-search.md) | 상품군별 QueryPlan·병렬 Oracle·evidence 격리 생성·교차 문구 검증·전체 fallback 계약 | v2.0 정본 |
| [HyperCLOVA X provider 계약](hyperclova-provider.md) | 공식 HTTP transport, optional QueryPlan·grounded answer 배선, planning 권한·오류·관측 계약 | 무호출 검증 완료·실제 credential 호출 대기·HCLX grounded plan 미배선 |
| [internal-red-team-v1 전체 E2E 평가](evaluation-internal-red-team.md) | 네 상품군 40문항의 공격 유형·전체 `/answer` 경로·최초 실패·수정 후 회귀 | v1.0 정본 |
| [공식 형식 30문항 공개 모의평가](evaluation-official-mock.md) | 최초 Docker GET 의미 24/30 보존·명시적 공모펀드 v1 승인 경로 30/30 | v1.2 HTTP 재평가 |
| [Qwen 변형 질문·전체 Agent 스트레스 평가](evaluation-qwen-metamorphic.md) | 세 표현 축·원문 비공개 의미 재구성·gold 감사·전체 Qwen E2E·grounded-plan gate | v1.1 공개 회귀 |
| [네 상품군 자동 커버리지·Qwen 자연화 평가](evaluation-coverage-guided.md) | 대표 계획 305개·직접 실행 299개, Qwen 897질문·391개 선별·최초 exact 보존·비교와 검색 2단계 사후 회귀 | v1.4 검색 개선 회귀 |
| [금융 도메인 QA 실험 파이프라인](evaluation-domain-qa.md) | 금융 도메인 담당자 40문항의 hash 검증·단계별 채점·Q002 QueryPlan·Oracle·evidence gold | v1.2 Router 회귀 40/40 |
| [제출용 모델 경계와 로컬 LLM 정리 메모](submission-model-boundary.md) | 8월 6일 공식 확인, 제출 후보의 로컬 provider·설정·의존성 제거, 투명한 개발·제출 경계 | release gate |
| [BM25/SQLite FTS 문서 RAG](document-rag.md) | 승인 문서 적재·BM25 검색·필터·근거·기준일·not-found 계약 | 최소 기능 완료 |
| [Dense Schema Linker 오프라인 컴포넌트 평가](evaluation-dense-schema-linker-shadow.md) | 현재 승인 SQL·합성 BM25 기준선, fake embedding·index manifest·production OFF와 채택 gate | 컴포넌트 구현·채택 보류 |
| [Schema Dense CPU 임베딩 모델 비교](evaluation-schema-embedding-cpu.md) · [팀 공유용 HTML](evaluation-schema-embedding-cpu.html) | 7개×두 결합 방식, 상품군·6개 실패·paired bootstrap 분석, BGE·KURE blind 후보 | 공개 후보 압축 완료·HTML 공유본 생성·독립 blind/OOD 대기·production OFF |
| [Schema Dense Stage 4 구현·Stage 5 OFF 실험 상태](schema-dense-stage4-stage5-readiness-2026-08-13.md) | 모델 artifact 고정, external blind v2, audit correlation·전용 metrics·readiness·단일 deadline Shadow | Stage 5 운영 계약 구현·코드는 OFF·production 주입 차단·독립 blind/full E2E 대기 |
| [Stage 4 감사·blind 리허설·image·API 기준선](stage4-audit-blind-image-api-report-2026-08-13.md) | 결정론적 Fast Path의 HTTP→Core→최종 ASGI body send 경계 감사, 합성 blind 절차, clean local image, API p50·p95·memory | clean local freeze 완료·NCP Registry/signing·HCLX/release linkage·external blind 대기 |
| [Schema Dense Docker CPU runtime baseline](../evaluation/baselines/schema-embedding-docker-runtime-2026-08-13.json) | exact loader와 고정 image에서 BGE·KURE embed_query 동시성·p95·메모리 측정 | c1/c2 컴포넌트 전제 통과·c4 250ms 초과·활성화 근거 아님 |
| [Schema Dense 외부 평가 인계서](../evaluation/schema_embedding_external/README.md) | label 없는 외부 100문항, BGE·KURE 동시 고정, OOD calibration/test와 외부 receipt | v2 protocol 완료·외부 bundle/독립 실행 승인 대기 |
| [Backend 전달용 Agent DTO](backend-contract.md) | 프레임워크 독립 request·response·citation·fallback·HTTP 오류 adapter와 JSON 예시 | v1.0·adapter 12/12 |
| [금융상품 Agent 사람 평가 rubric](human-evaluation.md) | 6개 평가 축·critical gate·독립 reviewer·집계 계약 | rubric 완료·실평가 대기 |
| [저장소 부트스트랩 작업 명세](prompts/01-repository-bootstrap.md) | 최초 Agent Core를 구현할 때 Codex에 전달한 실행 명세 | 완료 기록 |
| [Agent 전략 연구 요청](prompts/02-agent-strategy-research.md) | GPT Pro에 전달했던 질문과 당시 제약 | 과거 입력 기록 |
| [GPT Pro 연구 기록](research/2026-07-28-gpt-pro/README.md) | GPT Pro 원문 답변, 감사 번들, 검토 결과, 원본 ZIP 위치 | 연구·감사 기록 |
| [팀 기술 제안서 작성 허브](../../docs/proposal/README.md) | 공식 7개 항목, 평가 근거, 사용자 시나리오, 제출 체크리스트를 한곳에서 관리 | 초안 v0.1 |

## 5. 현재 구현

- [finance_agent_core](../packages/finance_agent_core/README.md): 네 상품군 감사·정규화·SQLite
  적재, 해외·국내 ETP·국내채권 QueryPlan·oracle·verifier·evidence·Agent
- [개발 Conda 환경](../environment.yml): `gaeng3-dev`, Python 3.12
- [로컬 LLM Conda 환경](../environment.local-llm.yml): `gaeng3-llm-local`,
  Python 3.12
- [개발 requirements](../requirements/dev.txt): editable core, Pydantic, PyYAML,
  pytest, Ruff
- [로컬 추론 requirements](../requirements/local-llm.txt): 개발 전용 vLLM
- 감사 회귀 기준: 4종 145,393행, 핵심 expectation 65개
- 해외 ETP 적재 기준: 5,646행, 검색 가능 5,636행, sparse 격리 10행
- 첫 vertical slice oracle 기준: 후보 440개, 결정론적 상위 5개
- 국내 ETP 적재 기준: 1,734행, 검색 가능 1,733행, 손상 행 1개 격리
- 국내 ETP 대표 oracle: 후보 211개, 수익률 상위 5개와 field evidence 재현
- 국내채권 적재 기준: 42,394행, 검색 가능 42,394행, 실제 매수 가능 254행
- 국내채권 대표 oracle: 잔존일수 365일 이하 회사채 후보 23개와 상위 3개 재현
- 공모펀드 적재 기준: 95,619 raw행, 논리 상품 11,138개, 속성 95,618개,
  손상 source row 84,563 한 건, 공모 검색 범위 11,115개
- 공모펀드 재현 기준: 독립 2회 SQLite·manifest SHA-256 byte 일치,
  `integrity_check=ok`, foreign-key 위반 0건
- 공모펀드 대표 oracle: 해외·주식형·판매중·당사 판매 후보 1,811개,
  3개월 수익률 상위 5개와 13개 field evidence 재현
- 공모펀드 평가 기준: development 40·holdout 10, 실행 44·안전 차단 6,
  expected QueryPlan·Oracle 전체 50/50
- 공모펀드 로컬 Qwen hybrid parser: development 최초 실행 40/40,
  commit 이후 최초 holdout 9/10, 합계 49/50
- 공개된 공모펀드 실패 회귀 수정 후 무모델 linker replay 50/50,
  로컬 holdout 미재실행
- 공모펀드 답변 기준: expected·local provider 각각 50/50,
  44개 grounded 생성·6개 안전 차단, 폴백 0
- 공모펀드 답변 검증 기준: 상품명·수치·순위·evidence·기준일·warning 100%
- 공모펀드 COMPARE 기준: expected·local provider 각각 20/20,
  18개 grounded 생성·2개 누락 대상 결정론 처리·폴백 0
- 공모펀드 COMPARE 검증 기준: 요청 순서·field status·numeric delta·
  evidence·기준일 100%, AUM 통화 불일치와 결측은 차이 미계산
- 공모펀드 자연어 COMPARE 기준: 정식명·짧은 이름·상품번호 exact resolver,
  ordered identity·정확한 연결어·위치별 문장부호 문법, expected·로컬 Qwen
  각각 24/24, 실행 16·안전 차단 8
- 공모펀드 자연어 COMPARE 통합 E2E: 공개 24문항 expected·로컬 Qwen
  각각 24/24, parser 호출 24·answer 호출 16, grounded 16·폴백 0,
  parser·resolution·독립 계획 계약·Oracle·field status·numeric delta·
  실제 비교 셀 값·별도 근거 provenance·차단·답변 핵심 검증률 100%
- 공모펀드 자연어 COMPARE parser 단독 로컬 지연: p50 569.018ms,
  p95 796.637ms, 최대 889.169ms
- 공모펀드 자연어 COMPARE 통합 E2E 로컬 p95: parser 751.575ms, answer
  2,225.406ms, 전체 2,737.07ms
- 공모펀드 자연어 COMPARE 안전 문법: 제외·대신·포함과 질문 전체 미등록
  잔여 표현, 빈·미종결·역방향·중첩·줄바꿈 따옴표 차단
- 세 상품군 자연어 COMPARE 공개 회귀: 해외 ETP·국내 ETP·국내채권
  `product-compare-core-30` 30/30, 실행 18·안전 차단 12, QueryPlan·상품 순서·
  field status·numeric delta·Backend citation 100%, compact identity cache 적용 후
  3 workers p50 65.522ms·p95 954.670ms
- 네 상품군 비교 공개 문항: 위 30문항과 기존 공모펀드 24문항을 합쳐 54문항
- 네 상품군 SEARCH·AGGREGATE 실제 데이터 성능 회귀: 새 프로세스 8문항 8/8,
  p50 308.749ms, 최대 추가 RSS 51,000KiB, 결과 지문 100% 일치
- 교차 상품군 SEARCH 실제 데이터 회귀: 국내·해외 ETP 성공·부분 성공·전체
  빈 결과·교차 비교 차단 4/4, 상품군별 plan·manifest 보존
- 교차 상품군 grounded answer: expected·로컬 Qwen 각각 4/4, 생성 대상
  2문항 `llm_grounded`, 실제 호출 3회, fallback 0, 빈 결과·control 무호출
- HyperCLOVA X 경계: QueryPlan·공모펀드 비교 초안·근거 답변의 semantic
  structured request, 공식 mode/provider gate, HCX schema 검사, token·latency
  call record와 fake transport 오류 계약 완료
- HyperCLOVA X API 없는 전체 경로: 해외·국내 ETP·국내채권 SEARCH와
  fallback·timeout·무호출 정책·서버 계획 guard 8/8; Direct v3 HTTP transport와
  FastAPI QueryPlan·grounded answer 무호출 배선 완료, 실제 credential 호출은 대기
- `/answer` service adapter: 정상·fallback·provider·dataset·내부 오류의
  HTTP status와 안전한 ERROR DTO, 민감정보 비노출 계약 12/12
- 공모펀드 평가 경계: 동결 expected QueryPlan의 SEARCH·COMPARE 답변 격리
  회귀와 공개 24문항 자연어 통합 E2E까지 완료, 독립 blind E2E·사람 rubric·
  HyperCLOVA X 실제 HTTP 재현 미완료, 공식 Agent 실행 비활성
- 연결 전 Router 진단: 도입 전 search 강제 replay 4/28, 현재 28/28
- 네 상품군 공통 집계: COUNT·MIN·MAX·AVG·허용 SUM, 최대 두 그룹,
  금액 통화 gate, 결측·기준일 공개, SQL 후보와 독립 Python 재검산
- 문서 검색 기준: BM25/SQLite FTS synthetic 적재·필터·근거·not-found와 P0-5 승인·권한·해시·변조 차단 계약 24/24 통과. 실제 corpus는 대기
- 관계·문서 Agent 내부 계약: 서버 확정 계획과 모델 제안 exact-match, relation/document
  evidence의 개수·순서·값·참조 검증, 오류·허위 주장 전체 fallback을 22/22로 확인.
  승인 관계 전체 경로 smoke 4/4이며 공개 Router·`GET /answer`·P0-10 Release는 대기
- 팀 계약 기준: Backend DTO JSON 예시·schema·오류 adapter, 사람 rubric validator 통과
- 내부 red-team 기준: 네 상품군 40문항 expected·수정 후 로컬 Qwen 40/40,
  safety·evidence 40/40, 최초 36/40과 수정 이력 별도 보존
- 금융 도메인 QA: v1 개발 40문항 최초 strict 1/40,
  safety·evidence 각각 32/40을 보존하고 v1.2 Router·linker 회귀에서
  모든 계약 40/40·잘못된 실행 0건, dependency pending 13건은 유지
- 코드 회귀 기준: pre-HCX 동결 507개·Backend 34개를 역사 기록으로 보존하고,
  P0-7 코드 기준 Agent Core 1,327개 통과·2개 조건부 skip. Backend의 최근 P0-8
  기준은 320개 통과·기존 fork warning 2건이며 이번 P0-7에서는 Backend를 변경하지 않음
- 로컬 Qwen 평가 기준: 동결 50문항에서 최초 미사용 holdout 9/10,
  오류 수정 후 전체 회귀 50/50을 연속 2회 재현
- 국내 ETP 로컬 Qwen 기준: development 40/40, local-inference holdout 첫 실행 10/10
- 국내채권 로컬 Qwen 기준: QueryPlan 50/50, grounded answer 50/50,
  실제 통합 E2E Answer Verifier 통과
- 국내 ETP 답변 기준: 47개 LLM 생성·3개 안전 차단, 전체 50/50,
  수치·순위·evidence·기준일 100%, 폴백 0
- 국내채권 답변 기준: 46개 LLM 생성·1개 결정론적 빈 결과·3개 안전 차단,
  전체 50/50, 폴백 0
- 다음 외부 게이트: 금융 도메인 담당자의 external blind 100문항·비공개 정답키,
  실제 corpus 출처·사용 권한·검색 평가, 실제 사람 평가
- Ubuntu SSH Docker 통합: 실제 이미지 build, 네 DB health, 채권·국내 ETP·
  해외 ETP 실행, 공모펀드 잠금, 역질문·미지원·HTTP 422 Backend 7건과
  공식 GET 정상·예외 7건의 확장 스모크 14/14 완료
- 동결 30문항의 실제 Docker 공식 GET: 형식·60초 30/30, 의미 24/30,
  공모펀드 공식 실행 잠금 6건을 최초 관측 baseline으로 보존
- 공모펀드만 여는 명시적 v1 배포 정책으로 동일 30문항 의미·형식·60초 30/30,
  Qwen 17/17, fallback 0건 재평가. 실험 후 기본 `locked`로 복구
- 제한 동시성 회귀: 결정론적 1·2·4에서 각 30/30, Qwen·공모펀드 승인
  동시성 2에서 30/30·fallback 0. 부하·운영 SLO가 아닌 계약 안정성 관측
- Qwen 정상 스모크 14/14, Qwen 중단 fallback 14/14, 종료 후 포트·GPU 해제와
  기본 공모펀드 잠금 Backend 복구 확인
- 외부 blind는 공개 세트 유사도 검사·SHA 봉인·최초 1회 상태·report hash 결합 완료,
  실제 100문항 작성과 최초 실행은 금융 도메인 담당자 외부 게이트
- 제출 경계 자동 검사: 개발 프로필 통과, 현재 제출 프로필 차단. 공식 범위 확인 후
  로컬 개발 파일을 제거한 release 후보에서 재검사
- Docker 데이터 준비: 읽기 전용 공식 XLSX에서 네 SQLite를 자동 생성·검증하고
  두 번째 실행에서 네 DB 모두 재사용, 성공 후에만 Backend 시작
- 다음 기술 통합: P0-7 내부 관계·문서 계획과 Claim Verifier까지 완료. P0-9 private
  blind 자산을 확보하면 수정 전에 최초 실행하고, P0-10에서 공개 Router·Backend
  adapter·AgentReleaseManifest·clean Docker image를 함께 고정. 이후 크레딧·정확한
  model ID·endpoint·인증 계약을 확보하면 같은 안전 gate 안에 HyperCLOVA X를 연결

## 6. 저장소 밖의 근거 자료

- [공식 과제 소개자료](<../../../../0. Official Materials/(배표용)과제소개자료_금융상품Agent.pdf>)
- [공식 공지 정리](<../../../../0. Official Materials/07-28(화) - 공지사항 정리하기.md>)
- [8월 6일 설명회 분석](<../../../../0. Official Materials/2026-08-06 오프라인 설명회 분석/2026-08-06 오프라인 설명회 분석.md>)
- [원천 데이터](<../../../../2. Data/1. Raw/1.금융상품/>)
- [원천 데이터 ZIP](<../../../../2. Data/0. Source Archive/1.금융상품.zip>)
- [프로젝트 시작 안내](<../../../../README.md>)
- [과거 프로젝트 허브·결정 로그](<../../../../26-07 미래에셋증권AI공모전.md>)

## 7. 판단 우선순위

내용이 충돌하면 다음 순서로 판단한다.

1. 주최 측 공식 과제자료와 이후 공식 공지·설명회 답변
2. 실제 제공 데이터와 재현 가능한 감사 결과
3. `project-baseline.md`와 `data-audit.md`
4. 활성 구현 명세와 코드 계약
5. GPT Pro 답변, 회의록, 과제 공개 전 아이디어 문서

연구 문서의 설계 제안은 자동으로 요구사항이 되지 않는다. 정본 문서로 승격한 결정만 구현 범위로 간주한다.

## 8. 문서 운영 규칙

- 공식 원본과 원천 데이터는 수정하지 않는다.
- 외부 모델의 원문 답변과 원본 번들은 재현성을 위해 보존한다.
- 연구 결과에서 채택한 결정은 `project-baseline.md`에 다시 기록한다.
- 데이터 수치나 지원 범위가 바뀌면 `data-audit.md`와 관련 계약 테스트를 함께 갱신한다.
- 평가 질문을 튜닝에 사용한 뒤에는 기존 holdout 성능으로 주장하지 않고 새
  미사용 split을 만든다.
- 설명회 자료에서 확인되지 않은 모델 ID·API 인증·보조 모델 범위는 추측하지 않고
  공식 서면 답변을 받은 뒤 모델·API·데이터 정책을 다시 동결한다.

새 문서를 만들 때는 다음 위치를 사용한다.

| 문서 유형 | 위치 |
| --- | --- |
| 현재 기준·계약·설계 | `finance_agent/docs/` |
| 재현 가능한 평가 수치 | `finance_agent/evaluation/` |
| 코드 사용법 | 해당 package의 `README.md` |
| 팀 기술 제안서 | 저장소 루트 `docs/proposal/` |
| 외부 연구 원문 | `finance_agent/docs/research/` |
| 더 이상 활성화되지 않는 프롬프트 | `finance_agent/docs/prompts/archive/` |
