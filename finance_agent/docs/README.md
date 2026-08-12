# AI 기술문서 안내

`finance_agent/docs/`는 금융상품 Agent의 현재 설계와 구현 판단을 기록하는 곳

처음 보는 팀원은 이 문서에서 필요한 목적을 고르고, AI 담당자는
[상세 문서 인덱스](project-index.md)에서 전체 문서 상태를 확인

## 가장 먼저 볼 문서

| 알고 싶은 내용 | 문서 |
| --- | --- |
| 지금 무엇이 구현됐는지 | [현재 프로젝트 기준](project-baseline.md) |
| 어떤 질문이 가능한지 | [Capability matrix](capability-matrix.md) |
| 데이터에서 무엇을 믿을 수 있는지 | [데이터 감사 기준](data-audit.md) |
| Backend와 어떤 JSON을 주고받는지 | [Backend DTO](backend-contract.md) |
| 어떤 계획만 DB를 실행할 수 있는지 | [QueryPlan 실행 권한 경계](plan-authority.md) |
| 코드·Prompt·Model·index·Docker image를 어떻게 한 release로 묶는지 | [AgentReleaseManifest 배포 계약](agent-release-manifest.md) |
| NCP Registry push·OCI 검증·cosign 서명을 어떻게 수행하는지 | [NCP immutable release CI](immutable-ncp-release-ci.md) |
| N-1 → N → N-1 rollback을 어떻게 검증하는지 | [Rollback drill runbook](../../fastapi_backend/ROLLBACK_DRILL.md) |
| 제출용 Ontology가 무엇인지 | [Ontology 제출 계약](ontology.md) |
| 평가 수치를 어떻게 해석하는지 | [평가 README](../evaluation/README.md) |
| 현재 승인 DB로 Stage 2 지문을 다시 계산한 결과 | [Stage 2 승인 DB 재검증 baseline](../evaluation/baselines/stage2-approved-db-revalidation-2026-08-12.json) |
| Stage 3 release·anti-replay 자동 계약 결과 | [Stage 3 release 계약 baseline](../evaluation/baselines/stage3-release-contract-2026-08-12.json) |
| localhost OCI push·합성 rollback 실기동 결과 | [Stage 3 OCI·rollback baseline](../evaluation/baselines/stage3-local-oci-rollback-2026-08-12.json) |
| 설명회 형식 30문항 결과가 궁금한지 | [공식 형식 공개 모의평가](evaluation-official-mock.md) |
| 질문 표현을 바꾼 Qwen 스트레스 평가가 궁금한지 | [Qwen 변형 질문 평가](evaluation-qwen-metamorphic.md) |
| 원문을 숨긴 의미 재구성·모델 계획 gate가 궁금한지 | [Qwen 변형 질문 평가 9절](evaluation-qwen-metamorphic.md#9-원문-표현을-숨긴-semantic-round-trip) |
| 전체 필드의 미시험 구간과 다음 Qwen 실험이 궁금한지 | [자동 커버리지 평가](evaluation-coverage-guided.md) |
| HyperCLOVA X 연결 전에 무엇이 남았는지 | [연결 전 준비 기준](pre-hcx-readiness.md) |
| Dense 도입 판단과 현재 SQL·BM25 수치가 궁금한지 | [Dense Schema Linker 오프라인 컴포넌트 평가](evaluation-dense-schema-linker-shadow.md) |
| 실제 Schema Dense 임베딩 7개 CPU 비교가 궁금한지 | [Schema Dense CPU 임베딩 모델 비교](evaluation-schema-embedding-cpu.md) |

## 목적별 위치

| 구분 | 내용 | 대표 문서 |
| --- | --- | --- |
| 현재 기준 | 공식 제약, 구현 상태, 우선순위 | [프로젝트 기준](project-baseline.md) |
| 데이터·계약 | 원천 데이터 품질, 필드, QueryPlan, 실행 권한, 배포 release, rollback, API DTO | [데이터 감사](data-audit.md), [계약](contracts.md), [실행 권한](plan-authority.md), [배포 계약](agent-release-manifest.md), [rollback runbook](../../fastapi_backend/ROLLBACK_DRILL.md) |
| Ontology | 공식 Turtle 5개, 생성·문법·registry 정합성 | [Ontology 제출 계약](ontology.md) |
| 기능 설계 | 검색, 비교, 집계, 문서 RAG, 답변 검증 | [상세 문서 인덱스](project-index.md) |
| 평가 해석 | 공개 회귀, Stage 2 승인 DB, Stage 3 release·rollback, blind, red-team, 사람 평가 | [평가 README](../evaluation/README.md), [Stage 2 baseline](../evaluation/baselines/stage2-approved-db-revalidation-2026-08-12.json), [Stage 3 release baseline](../evaluation/baselines/stage3-release-contract-2026-08-12.json), [Stage 3 rollback baseline](../evaluation/baselines/stage3-local-oci-rollback-2026-08-12.json) |
| 개발 전용 | 로컬 Qwen 실행과 안전 경계 | [로컬 LLM](local-llm.md) |
| 과거 기록 | 초기 프롬프트와 외부 연구 답변 | `prompts/`, `research/` |

## 2026-08-06 설명회 반영 상태

- 팀원 기록, 현장 사진, 네이버클라우드 공식 PDF 교차 검토 완료
- 평가용 `GET /answer`의 query parameter와 다섯 문자열 응답 필드 확인·구현
- 성공·결과 없음·역질문·미지원·오류의 HTTP 200 계약 테스트 완료
- 예상 30문항·미응답 5문항·60초 권장과 도메인별 `.ttl` 제출 화면 확인
- 같은 10/10/10·미응답 5개 분포의 공개 모의평가를 구성해 로컬 Qwen 전체 경로 30/30 확인
- Qwen이 만든 의미 보존 변형 77개를 전체 Agent로 재생해 77/77·fallback 0 확인
- 원문 문장을 숨긴 의미 재구성 64개에서 최초 15/64를 보존하고 출력·계획 의미 64/64로 개선
- registry 기반 대표 기능 305개 중 299개 직접 실행, canonical 자연어 최초 strict
  37/299로 질문 이해층의 넓은 공백을 별도 동결
- 같은 299개 의미 명세에서 Qwen 질문 897개 생성·391개 선별 후 네 역할 구성을
  비교해 모두 strict 65/391, 계획 경로 2건 구제·2건 퇴행·순개선 0을 최초 동결
- 기존 strict를 보존한 실행 의미 사후 감사에서 규칙 기반 134/391, Qwen 계획
  132/391을 확인해 다음 우선순위를 비교·검색·남은 실제 집계 오류 순으로 조정
- 정확한 두 상품 ID 비교 문법과 표준 근거 범위를 보강한 같은 공개 질문 사후
  회귀에서 exact 94/391·보조 strict 163/391·비교 29/29 확인
- Qwen 계획 재실행도 exact 94/391·비교 29/29였으나 2건 구제·2건 퇴행과
  보조 strict 161/391로 계획 경로 순개선 없음
- 일반 ETF·ETN 상품군 해석, 검색 조건·정렬 필드의 근거 자동 포함, 거래 중지
  부정을 보강한 사후 회귀에서 exact 153/391·보조 strict 222/391, 구제 59·퇴행 0
- 명확한 총보수율과 티커 정렬 Router를 추가한 두 번째 검색 회귀에서 exact
  170/391·보조 strict 242/391, 구제 17·퇴행 0. 모두 같은 공개 질문을 사용한
  개발 회귀이며 독립 blind나 공모전 예상 점수가 아님
- 도메인별 Turtle 5개 생성과 RDFLib 문법·field registry 정합성 검사 완료
- 정확한 HCX 모델 ID·endpoint·인증 규격과 크레딧 적용 범위는 후속 확인 필요
- 크레딧을 받기 전까지 실제 HCX 연결은 보류하고 로컬 Qwen 내부 시험 계속
- 상세 기록: [8월 6일 설명회 반영 기록](../../docs/proposal/briefing-2026-08-06.md)

## 문서 사용 원칙

- 구현 판단은 `project-baseline.md`와 실제 코드·평가 baseline을 우선
- 외부 연구 답변과 과거 프롬프트는 설계 배경이며 현재 요구사항으로 자동 적용하지 않음
- 새 설계 문서를 만들면 [상세 문서 인덱스](project-index.md)에 상태와 역할을 함께 기록
- 파일을 옮길 때는 먼저 전체 내부 링크와 source-freeze 영향을 확인
