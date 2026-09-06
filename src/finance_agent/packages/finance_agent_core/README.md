# finance-agent-core

금융상품 Agent의 데이터 감사·정규화, field registry와 Typed QueryPlan 계약, 결정론적
검색·비교·집계, 독립 검증, 근거 생성, HyperCLOVA X provider를 애플리케이션 shell과
독립적으로 담는 Python 패키지.

설치는 저장소 루트의 `requirements.txt`가 이 패키지를 editable로 포함한다.

## 구조

```text
src/finance_agent_core/
├── config/            # field_registry.yaml · capability_matrix.json · 설정 로딩
├── contracts/         # QueryPlan · Backend DTO · HCX Structured Outputs schema
├── domain/            # 상품군 · 필드 · 단위 도메인 모델
├── normalization/     # 원천 XLSX 값 정규화 규칙
├── storage/           # 공식 XLSX 감사 · SQLite 생성 · 승인 manifest
├── agent/             # Intent Router · 의미 관문 · linker · provider · Backend adapter
├── execution/         # SQLite Oracle · Result/Aggregate/Comparison Verifier · renderer
├── retrieval/         # 관계 색인 · 외부 공식 출처 획득 · 스키마 검색
├── answering/         # 근거 기반 최종 답변 · Answer Verifier · 결정론적 fallback
├── audit/             # 구조화 감사 이벤트 · 비밀값 마스킹
├── evaluation/        # 런타임이 쓰는 스키마 검색 artifact 로더 (평가 데이터는 미포함)
├── release.py         # AgentReleaseManifest — 코드·Prompt·데이터·이미지 결합 검증
├── observability.py   # 실행 단계 기록
└── ontology.py        # field registry → 공식 Turtle 5개 결정론적 생성
tests/                 # 단위 · 계약 · 회귀 테스트
```

## 핵심 계약

- [`field_registry.yaml`](src/finance_agent_core/config/field_registry.yaml) — 네 상품군 canonical field,
  원천 매핑, 단위, 연산자, 표면형(alias). 질의 해석·SQL·Verifier·Ontology가 모두 이 파일을 읽는다
- [`queryplan.py`](src/finance_agent_core/contracts/queryplan.py) — 서버의 엄격한 구조·의미 검증
- [`queryplan.hcx.schema.json`](src/finance_agent_core/contracts/queryplan.hcx.schema.json) — HyperCLOVA X Structured Outputs용 schema
- [`capability_matrix.json`](src/finance_agent_core/config/capability_matrix.json) — 상품군·intent별 실행·통제 범위
- [`backend.py`](src/finance_agent_core/contracts/backend.py) — Backend request·response·citation·fallback DTO
- [`ontology.py`](src/finance_agent_core/ontology.py) — 공식 Turtle 5개 생성

## 설계 원칙

- **LLM은 field·filter·정렬·개수를 바꿀 수 없다.** HCX-007은 의도 분석과 최종 문장만 담당하고,
  QueryPlan은 서버가 registry에 대해 검증한다
- **SQL과 Python이 같은 규칙을 두 번 계산한다.** Oracle 결과를 Verifier가 재계산해 대조하고,
  어긋나면 답하지 않는다(fail-closed)
- **근거 없는 값은 답에 들어가지 않는다.** Answer Verifier가 최종 문장의 상품명·수치를 evidence와 대조한다
- **승인된 데이터만 실행한다.** 승인 manifest의 DB SHA-256이 다르면 시작을 거부한다

## 검증

```bash
cd src/finance_agent/packages/finance_agent_core
python -m pytest -q
python -m ruff check .
```

테스트는 원천 데이터·네트워크·LLM 자격증명 없이 실행된다.
