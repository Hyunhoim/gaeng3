# finance-agent-core

금융상품 Agent의 데이터 감사·정규화, field registry와 Typed QueryPlan 계약, 결정론적
검색·비교·집계, 독립 검증, 근거 생성, 최종 답변 생성, HyperCLOVA X provider를 애플리케이션
shell과 독립적으로 담는 Python 패키지. 저장소 루트의 `requirements.txt`가 이 패키지를
editable로 설치한다.

## 1. 구조

```text
src/finance_agent_core/
├── config/            # field_registry.yaml · capability_matrix.json · 승인 데이터 manifest
├── contracts/         # QueryPlan · KnowledgeQueryPlan · Backend DTO · HCX Structured Outputs schema
├── domain/            # 상품군 · 필드 · 단위 도메인 모델
├── normalization/     # 원천 XLSX 값 정규화 규칙
├── storage/           # 공식 XLSX 감사 · SQLite 생성 · 승인 manifest 검증
├── agent/             # Intent 분석 · Router · 의미 관문 · linker · provider · Backend adapter
├── execution/         # SQLite Oracle · Result/Aggregate/Comparison Verifier · renderer
├── retrieval/         # 관계 색인 · Schema Dense · Document Dense · BM25 · 승인 corpus
├── answering/         # 근거 기반 최종 답변 · Answer Verifier
├── audit/             # 구조화 감사 이벤트 · 비밀값 마스킹
├── evaluation/        # 런타임 artifact 로더 · 평가 도구 (평가 데이터는 미포함)
├── release.py         # AgentReleaseManifest · DeploymentBinding — 코드·Prompt·데이터·이미지 결합 검증
├── release_cli.py     # manifest · binding 생성 CLI
├── deadline.py        # 요청 deadline 전파
├── observability.py   # 실행 단계 기록
└── ontology.py        # field registry → Turtle 5개 결정론적 생성
tests/                 # 단위 · 계약 · 회귀 테스트
```

## 2. 핵심 계약

| 파일 | 역할 |
| --- | --- |
| [`config/field_registry.yaml`](src/finance_agent_core/config/field_registry.yaml) | 네 상품군 canonical field, 원천 매핑, 단위, 연산자, 표면형(alias). 질의 해석·SQL·Verifier·Ontology가 모두 이 파일을 읽는다 |
| [`config/capability_matrix.json`](src/finance_agent_core/config/capability_matrix.json) | 상품군·intent별 실행·통제 범위 |
| [`config/approved_dataset_manifest.json`](src/finance_agent_core/config/approved_dataset_manifest.json) | 승인 데이터의 source hash·행 수·기준일·DB hash |
| [`contracts/queryplan.py`](src/finance_agent_core/contracts/queryplan.py) | Typed QueryPlan과 서버 측 구조·의미 검증 |
| [`contracts/queryplan.hcx.schema.json`](src/finance_agent_core/contracts/queryplan.hcx.schema.json) | HyperCLOVA X Structured Outputs schema |
| [`contracts/knowledge.py`](src/finance_agent_core/contracts/knowledge.py) | 관계 질문용 `KnowledgeQueryPlan` |
| [`contracts/backend.py`](src/finance_agent_core/contracts/backend.py) | Backend request·response·citation DTO |
| [`release.py`](src/finance_agent_core/release.py) | `AgentReleaseManifest` · `DeploymentBinding` |
| [`ontology.py`](src/finance_agent_core/ontology.py) | 공식 Turtle 5개 생성 |

## 3. 처리 단계와 모듈

| 단계 | 모듈 | 내용 |
| --- | --- | --- |
| 안전 검사 | `agent/safety.py` | 예측·단정 추천 등 미지원 요청 차단 |
| Intent 분석 | `agent/intent_analysis.py`, `agent/providers/hyperclova*.py` | HCX-007 Structured Outputs로 의도·상품군·Semantic Frame 추출, 서버 교차검증 |
| 필드 의미 연결 | `agent/adaptive_semantic.py`, `retrieval/schema_dense.py` | KURE-v1 Schema Dense 색인으로 표면형을 registry field에 연결 |
| QueryPlan | `agent/compiler.py`, `contracts/queryplan.py` | registry 기준 field·조건·정렬·개수 검증 |
| 관계 검색 | `agent/knowledge_router.py`, `retrieval/relations.py` | 관계 FTS 후보, 엔터티 전체 exact match, 상품 DB 교집합 |
| 실행 | `execution/oracle.py` | SQLite 검색·정렬·비교·집계 |
| 검증 | `execution/verifier.py`, `execution/aggregation.py`, `execution/comparison.py` | Python 재계산으로 Oracle 결과 대조 |
| 근거 | `execution/evidence.py`, `execution/renderer.py` | 필드별 값·출처·기준일 보존 |
| 외부 근거 | `retrieval/document_dense.py`, `retrieval/corpus.py`, `execution/external_knowledge_*.py` | 승인 문서 묶음 258건을 KURE-v1 Document Dense + BM25로 검색해 DB 후보를 줄인다 |
| 최종 답변 | `answering/final_generation.py`, `answering/verifier.py` | HCX-007이 검증된 근거로 문장을 만들고 Answer Verifier가 상품명·수치를 재검사 |
| Backend adapter | `agent/backend_adapter.py`, `agent/routed_service.py` | `execute_answer_request()`, `RoutedFinanceAgent` |

## 4. 설계 원칙

- **LLM은 field·filter·정렬·개수를 바꿀 수 없다.** HCX-007은 Intent 분석과 최종 문장만 담당하고,
  QueryPlan은 서버가 registry에 대해 검증한다. Intent가 서버 계획과 충돌하면 SQL을 실행하지 않는다
- **SQL과 Python이 같은 규칙을 두 번 계산한다.** Oracle 결과를 Verifier가 재계산해 대조하고,
  어긋나면 답하지 않는다
- **근거 없는 값은 답에 들어가지 않는다.** Answer Verifier가 최종 문장의 상품명·수치를 evidence와 대조한다
- **승인된 데이터·색인만 실행한다.** 승인 manifest의 DB SHA-256, Schema Dense·Document Dense 색인
  해시, 관계 artifact 해시가 릴리스 manifest와 다르면 시작을 거부한다
- **provider 경계.** `evaluation`·`production`에서는 `hyperclova` provider만 조립되며 다른 provider는
  설정 단계에서 거부된다

## 5. 런타임 artifact

| artifact | 내용 |
| --- | --- |
| 네 상품군 SQLite · 관계 색인 · 관계 artifact | `storage/prepare.py`가 공식 XLSX에서 생성. 승인 manifest와 해시 대조 |
| KURE-v1 Schema Dense 색인 v9 (registry 1.8) | `src/finance_agent/artifacts/runtime/schema-dense/`. 필드 의미 연결 |
| 승인 문서 묶음 · Document Dense 색인 · 활성화 영수증 | 258건 공식 문서. Git에 포함하지 않고 배포 시 read-only 마운트 |
| `AgentReleaseManifest` · `DeploymentBinding` | 위 artifact와 코드·이미지의 SHA-256을 결속. `release_cli.py manifest|binding`으로 생성 |

## 6. 명령

```bash
python -m finance_agent_core.storage.prepare --data-dir /path/to/2.금융상품_20260824 --output-dir /path/to/out
```

```bash
python -m finance_agent_core.release_cli manifest --help
```

```bash
python src/finance_agent/scripts/sync-ontology.py --check
```

## 7. 테스트

원천 데이터·네트워크·LLM 자격증명 없이 실행된다.

```bash
cd src/finance_agent/packages/finance_agent_core && python -m pytest -q
```

```bash
python -m ruff check .
```
