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
| 제출용 Ontology가 무엇인지 | [Ontology 제출 계약](ontology.md) |
| 평가 수치를 어떻게 해석하는지 | [평가 README](../evaluation/README.md) |
| HyperCLOVA X 연결 전에 무엇이 남았는지 | [연결 전 준비 기준](pre-hcx-readiness.md) |

## 목적별 위치

| 구분 | 내용 | 대표 문서 |
| --- | --- | --- |
| 현재 기준 | 공식 제약, 구현 상태, 우선순위 | [프로젝트 기준](project-baseline.md) |
| 데이터·계약 | 원천 데이터 품질, 필드, QueryPlan, API DTO | [데이터 감사](data-audit.md), [계약](contracts.md) |
| Ontology | 공식 Turtle 5개, 생성·문법·registry 정합성 | [Ontology 제출 계약](ontology.md) |
| 기능 설계 | 검색, 비교, 집계, 문서 RAG, 답변 검증 | [상세 문서 인덱스](project-index.md) |
| 평가 해석 | 공개 회귀, blind, red-team, 사람 평가 | [평가 README](../evaluation/README.md) |
| 개발 전용 | 로컬 Qwen 실행과 안전 경계 | [로컬 LLM](local-llm.md) |
| 과거 기록 | 초기 프롬프트와 외부 연구 답변 | `prompts/`, `research/` |

## 2026-08-06 설명회 반영 상태

- 팀원 기록, 현장 사진, 네이버클라우드 공식 PDF 교차 검토 완료
- 평가용 `GET /answer`의 query parameter와 다섯 문자열 응답 필드 확인·구현
- 성공·결과 없음·역질문·미지원·오류의 HTTP 200 계약 테스트 완료
- 예상 30문항·미응답 5문항·60초 권장과 도메인별 `.ttl` 제출 화면 확인
- 도메인별 Turtle 5개 생성과 RDFLib 문법·field registry 정합성 검사 완료
- 정확한 HCX 모델 ID·endpoint·인증 규격과 크레딧 적용 범위는 후속 확인 필요
- 크레딧을 받기 전까지 실제 HCX 연결은 보류하고 로컬 Qwen 내부 시험 계속
- 상세 기록: [8월 6일 설명회 반영 기록](../../docs/proposal/briefing-2026-08-06.md)

## 문서 사용 원칙

- 구현 판단은 `project-baseline.md`와 실제 코드·평가 baseline을 우선
- 외부 연구 답변과 과거 프롬프트는 설계 배경이며 현재 요구사항으로 자동 적용하지 않음
- 새 설계 문서를 만들면 [상세 문서 인덱스](project-index.md)에 상태와 역할을 함께 기록
- 파일을 옮길 때는 먼저 전체 내부 링크와 source-freeze 영향을 확인
