# 연구 근거와 프로젝트 적용 원칙

상태: 활성 · 기술 제안서 보조 근거

기준일: 2026-08-08

이 문서는 유명 논문 이름을 나열하기 위한 문서가 아님
현재 Agent의 설계와 평가에서 실제로 차용한 원칙, 구현한 범위, 아직 구현하지
않은 범위를 구분하기 위한 문서임

## 1. 직접 차용한 핵심 원칙

| 연구 | 연구에서 가져온 원칙 | 현재 프로젝트 적용 | 동일하지 않은 부분 |
| --- | --- | --- | --- |
| [CheckList, ACL 2020](https://aclanthology.org/2020.acl-main.442/) | 전체 정확도 하나 외에 언어 능력과 변화 유형별 행동 검사 | 상품군·의도·필드·연산자 capability matrix, 부정·순서·표현 변형 회귀, 자동 대표 좌표 305개 | 원 논문의 소프트웨어를 그대로 사용하지 않고 금융 QueryPlan 계약에 맞춰 별도 구현 |
| [Spider, EMNLP 2018](https://aclanthology.org/D18-1425/)·[Spider 2.0, ICLR 2025](https://openreview.net/forum?id=XmProj9cPs) | 본 질문을 외운 정확도보다 새로운 schema·질문에서 올바른 실행 의미를 복원하는 능력과 실행 결과가 중요 | 자연어 답만 채점하지 않고 QueryPlan 의미 지문, Oracle 결과, 상품 ID, field evidence를 단계별 비교 | 범용 Text-to-SQL benchmark가 아니라 공식 네 상품군과 허용된 연산만 다루는 폐쇄형 실행 계약 |
| [Evaluating NL2SQL via SQL2NL, Findings of EMNLP 2025](https://aclanthology.org/2025.findings-emnlp.1031/) | schema와 의도를 보존한 다양한 표현의 질문으로 언어 변화에 대한 실행 견고성을 별도 평가 | 실행 가능한 정답 QueryPlan에서 Qwen이 세 가지 문체의 질문을 생성하고, 의미 보존 검사 후 같은 Oracle 결과가 나오는지 비교 | SQL2NL 방법이나 구현을 그대로 사용하지 않으며, SQL 대신 네 금융상품군의 제한된 QueryPlan·registry·evidence 계약을 기준으로 평가 |
| [PICARD, EMNLP 2021](https://aclanthology.org/2021.emnlp-main.779/) | 형식 언어 생성은 출력 후 오류 처리만 하지 말고 허용되지 않는 생성을 제약 | Structured Outputs JSON Schema, Pydantic 계약, registry capability, 서버 고정 조건과 grounded-plan gate를 연속 적용 | 토큰별 incremental parser인 PICARD 자체를 구현한 것은 아님 |
| [RAGChecker, NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/hash/27245589131d17368cccdfa990cbf16e-Abstract-Datasets_and_Benchmarks_Track.html) | 모듈형 시스템은 최종 답만이 아니라 검색과 생성 단계를 분리해 진단 | routing·planning·execution·evidence·answer 단계, plan/evidence strict, fallback과 provider 호출·지연을 별도 기록 | 현재 주력은 정형 SQL 검색이며 실제 문서 corpus RAG 평가는 승인 corpus 확보 후 수행 |
| [RAGTruth, ACL 2024](https://aclanthology.org/2024.acl-long.585/) | 근거를 제공해도 생성 답변에 미지원·모순 주장이 남을 수 있으므로 별도 검증 필요 | LLM에는 field evidence만 제공하고 Answer Verifier가 상품명·수치·순위·기준일·근거를 검사, 실패 시 결정론적 fallback | 별도 hallucination 분류 모델을 학습하지 않고 서버 규칙으로 검증 가능한 금융 사실만 검사 |
| [Dror et al., ACL 2018](https://aclanthology.org/P18-1128/) | 성능 차이가 우연이 아닌지 평가 설정과 지표에 맞는 통계 검정 필요 | 동일 질문 paired 비교, canonical Wilson 95% 구간, 자연어 변형은 source plan 단위 cluster bootstrap·exact McNemar, 다중 후보 Holm 보정 | 내부 synthetic 표본의 통계적 유의성이 실제 공모전 분포의 유효성을 보장하지 않음 |

## 2. 이 원칙으로 만든 평가 흐름

~~~text
field registry + 실제 정규화 DB
  → 실행 가능한 정답 QueryPlan을 먼저 구성
  → Oracle·독립 Verifier·field evidence 정답 지문 고정
  → Qwen이 원문 없이 정중체·구어체·검색창형 질문 생성
  → 의미 보존 기계 검사
  → 같은 질문을 결정론·Qwen 역할별 Agent에 입력
  → 계획·상품·수치·근거·답변·지연을 단계별 비교
  → 구제·퇴행과 신뢰구간·통계 검정 기록
  → 영향이 큰 공통 원인만 수정하고 동결 세트 전체 회귀
~~~

이 흐름의 목적은 Qwen 점수를 높이는 것이 아님
HyperCLOVA X를 연결하기 전에 데이터 실행부와 검증부를 고정하고, 모델을 바꿨을 때
동일한 질문·정답·지표로 역할별 차이를 측정할 수 있게 하는 것이 목적임

## 3. 채택 여부를 숫자로 결정할 기술

GraphDB·Vector DB·embedding·re-ranker를 사용하면 제안서가 자동으로 좋아지는 것은
아님
다음 조건을 모두 만족할 때만 공식 후보에 추가

1. 공식 규칙상 사용할 수 있는 모델·데이터인지 서면 확인
2. 현재 SQL·BM25 기준선과 동일한 미사용 질문에서 비교
3. strict 정확도 또는 사람 평가가 개선되고 새로운 치명적 퇴행이 없음
4. 문항당 60초 예산과 운영 메모리·비용을 만족
5. 근거 출처와 기준일을 기존 DTO로 끝까지 보존

효과가 없거나 근거를 흐리게 하는 구성요소는 넣지 않음
이는 기술 수를 줄이는 선택이 아니라 평가 정확도와 설명 가능성을 높이는 선택임

## 4. 제안서에 사용할 수 있는 표현

사용 가능

- “행동 기능별 테스트와 단계별 진단 원리를 금융상품 QueryPlan에 맞게 구현”
- “구조화 계획·실행 결과·field evidence를 분리 채점”
- “동일 질문 paired 비교로 모델 역할의 구제와 퇴행을 함께 측정”
- “근거 제공 이후에도 Answer Verifier로 미지원 수치와 순위를 차단”

사용 금지

- “PICARD·RAGChecker 알고리즘을 그대로 구현”
- “논문 방법을 사용했으므로 성능이 보장됨”
- “내부 synthetic 통계 유의성이 공모전 성능을 입증함”
- “Qwen 결과가 HyperCLOVA X 결과와 동일함”

## 5. 남은 연구 검증

- 금융 도메인 담당자가 독립 작성한 외부 blind 질문과 비공개 정답
- 최소 2명의 사람 평가자가 수행한 정확성·근거성·도움됨 평가
- 승인된 실제 문서 corpus에서 BM25 기준선과 추가 검색기의 ablation
- 같은 동결 질문에서 Qwen과 HyperCLOVA X의 계획·답변 역할별 비교
- 공식 서버 환경의 지연·오류·비용 측정

연구 근거는 설계 선택을 설명하는 자료이며, 최종 주장은 반드시 이 저장소의
실제 baseline과 외부 검수 결과에 연결
