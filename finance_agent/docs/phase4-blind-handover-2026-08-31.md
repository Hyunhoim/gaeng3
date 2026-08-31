# Phase 4 Blind 재개 인수인계 — 2026-08-31

## 이번 동결의 목적

이 변경은 2026-08-24 공식 데이터 기준의 금융상품 Agent를 다음 경계까지
준비한다.

1. HCLX는 모든 평가 요청의 `Intent` 분석과 검증된 근거 기반 최종 답변만
   담당한다.
2. 서버는 Typed QueryPlan, 제공 DB SQL/Python 계산, Verifier를 계속
   결정론적으로 실행한다.
3. Schema Dense와 HCLX Semantic Resolver는 서버가 제한한 후보·근거 표현
   밖으로 확장하지 못한다.
4. 외부 문서는 공식 Source Registry와 제공 DB 상품의 교집합을 검증하는
   보조 근거이며, 아직 수집 대기 corpus는 Runtime/Release에 연결되지 않는다.

## 이번 commit의 검증 결과

| 범위 | 결과 |
| --- | --- |
| Agent Core 전체 pytest | 1,862 passed, 1 opt-in skip |
| FastAPI Backend 전체 pytest | 463 passed, 1 Docker Compose opt-in skip |
| Ruff check | 통과 |
| Ruff format --check | 통과 |
| `git diff --check` | 통과 |

skip은 각각 승인 DB 경로와 Docker Compose CLI가 현재 격리 테스트 환경에
없어서 발생한 선택 실행 항목이다. 실패한 회귀는 없다.

## 핵심 구현 범위

- 2026-08-24 공식 DB manifest·field registry·0/결측 제외 규칙을 반영했다.
- `BUYABLE_QUANTITY`는 채권 구매 가능 여부를 결정하지 않도록 유지했다.
- Typed Alias Registry를 Router·Compiler·SemanticCoverageGate에서 공통으로
  소비하도록 확장했다.
- 명시적 상품명·한국어 조사 결합을 정규화하고, 미해결 식별자와 순위·조건
  누락은 실행 대신 `CLARIFY`/`UNSUPPORTED`로 종료하도록 보강했다.
- HCLX Intent DTO의 첫 응답이 구조 검증에 실패할 경우 정확히 한 번만 닫힌
  schema의 repair를 시도하고, 재실패하면 SQL 전에 fail-closed한다.
- 공식 외부문서 경로는 Source Selection, Discovery, Connector, Evidence
  Verifier, 제공 DB 교집합 검증까지 연결했다.
- corpus 확장 도구는 운영 Source Registry와 별도의 intake-only Registry를
  사용한다. 수집물은 `pending`이며 Release에 사용할 수 없다.
- field registry 변경에 맞춰 Ontology Turtle 산출물을 재생성했다.

## 보존된 Pending Corpus

운영 Runtime에는 연결하지 않은 공식문서 intake snapshot이 다음 경로에
Git 제외 상태로 보존되어 있다.

`finance_agent/artifacts/pending-corpus/20260831-expanded-v10/`

- 고유 공식 문서 109건: PDF 64건, HTML 45건
- 다운로드·본문 추출 실패 0건, 기준일 위반 0건, 빈 파일 0건
- manifest SHA-256:
  `01def2764d2cc24e0df8394cd789148753a54ad9efd5771dfc97b753c93b094a`
- catalog SHA-256:
  `41f525fee75e741ec5bb012b586fd7ac7ecef6bc7d0e163f69c0ed1338a756b7`

KURE Document Dense index 생성은 의도적으로 완료하지 않았다. pending corpus를
Runtime에 연결하려면 별도 calibration, artifact SHA-256, Release manifest,
회귀 검증이 모두 필요하다.

## Phase 4 Blind 입력 상태

| 항목 | 값 |
| --- | --- |
| Question-only 경로 | `/home/hyunholim/uploads/document-dense-blind/independent-holdout-phase4-questions-only.json` |
| suite ID | `finance-agent-independent-holdout-phase4-v1-1000` |
| 문항 수 | 1,000 |
| question-only SHA-256 | `55f42578afa9e665920e5eee294bfd98a82661fc10404e7a4abecc8eb4c431eb` |
| Gold 경로 | `/home/hyunholim/uploads/document-dense-blind/independent-holdout-phase4-gold-private.json` |

Gold는 아직 열람·hash·parse하지 않았다. 이 상태가 최초 Blind의 전제다.

## 다음 Sol 작업의 엄격한 순서

1. `run_sealed_lab_blind.py`에 위 Phase 4 suite ID, `P4` ID prefix, 1,000건
   계약을 등록하고 관련 pytest를 추가한다. 이 단계에서 Gold는 열지 않는다.
2. clean commit에서 `git rev-parse HEAD`, question-only SHA, source diff SHA,
   네 DB SHA, relation/schema-dense/document-dense artifact SHA를 기록한다.
3. `--preflight-only`로 Secret file, DB, artifact, runner 계약만 검증한다.
   HCLX 호출과 question-only 열람은 하지 않는다.
4. 동일 clean commit·image·승인 입력으로 최초 1회 prediction을 생성한다.
   결과 JSON과 receipt SHA-256을 먼저 고정한다.
5. 그 뒤에만 Gold의 SHA를 확인하고 comparator로 상태·상품 ID·Top-N·수치·근거
   정확도, Precision, Recall, F1을 계산한다.
6. Gold 공개 뒤 수정은 반드시 `post-gold-development-regression`으로 분리한다.
   최초 Blind prediction을 덮어쓰거나 재생성하지 않는다.
7. Phase 1~4의 receipt와 report를 합쳐 통합 HTML 보고서를 작성한다.

## 금지 사항

- Phase 4 Gold를 prediction 생성 전에 읽거나 hash/parse하지 않는다.
- Gold, HCLX/Naver Secret, 질문 원문·응답 원문, DB, model weight, pending corpus를
  Git에 stage하지 않는다.
- 이번 개발 cycle에서는 NCP 배포·Release 생성·production runtime 활성화를 하지
  않는다. 사용자의 "최종 버전" 명시 승인 전에는 연구실 서버에서만 검증한다.
