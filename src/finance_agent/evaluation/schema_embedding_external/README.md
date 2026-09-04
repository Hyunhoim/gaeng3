# Schema Dense 독립 blind·OOD 평가 인계서

상태: v2 평가 계약 동결 · 외부 bundle/독립 평가자 승인 대기 · 최초 점수 미생성

이 평가는 공개 개발 질문으로 고른 Schema Dense 후보가 처음 보는 질문에서도
도움이 되는지 확인한다. 아직 외부 질문, 비공개 정답키, 독립 실행 승인과 외부
prediction receipt가 없으므로 실제 최초 평가는 수행하지 않았다.

정본은
[external blind v2 protocol](../protocols/schema-embedding-external-blind-v2.protocol.json)이다.
v1 protocol은 과거 설계 기록이며 신규 평가에 사용하지 않는다.

## 1. 질문을 보기 전에 고정한 후보

- Lexical: 현재 서버 단어·별칭 규칙 baseline
- BGE-M3: `BAAI/bge-m3` revision
  `5617a9f61b028005a4858fdac845db406aefb181`
- KURE-v1: `nlpai-lab/KURE-v1` revision
  `d14c8a9423946e268a0c9952fecf3a7aabd73bd9`
- 결합 방식: 기존 Lexical 순서를 보존하는 `lexical_first`
- 검색 대상: 상품 행이 아니라 field registry의 Schema 설명문
- 운영 권한: OFF/Shadow 전용, QueryPlan·SQL·사용자 답변 변경 권한 없음

각 모델은 revision뿐 아니라 weights·tokenizer·config의 파일 크기와 SHA-256까지
v2 snapshot manifest에 고정한다. 최초 실행에서는 caller가 Provider나 Index를
주입할 수 없다. 공식 runner가 두 snapshot을 다시 검증해 직접 로드한 뒤, 동일한
canonical field 문서로 `DenseSchemaIndex`를 내부 생성한다.

공개 모델 선택에 사용한 질문 inventory도 protocol에 고정한다. 정본은
`schema-embedding-cpu-public-v1` 입력 manifest와 그 manifest가 가리키는 네
`core_50` suite(총 200문항), 정책 migration 파일이다. runner는 protocol 원문 SHA-256,
입력 manifest SHA-256, 네 suite SHA-256과 corpus SHA-256을 모두 다시 확인한다.

## 2. 외부 평가자가 준비·보관할 파일

1. 질문 파일: `id + question`만 포함하는 `ExternalBlindQuestionOnlySet`
2. 비공개 정답키: family·intent·disposition·QueryPlan·Oracle gold와 명시적
   `gold_schema_field_ids`를 담는 `ExternalBlindPrivateAnswerKey`. EXECUTE는 승인
   registry에서 해당 family에 속한 field ID가 1개 이상이어야 하고 Control은 빈 배열
3. 질문과 정답키 SHA-256, clean 구현 commit을 묶은 commitment
4. exact Docker image digest와 두 모델 manifest hash를 승인한
   `ExternalBlindExecutionAuthorization`
5. 답 공개 전에 prediction SHA-256을 외부 append-only 저장소에 기록한
   `ExternalBlindPredictionReceipt`

질문 작성이 끝나면 commitment보다 먼저 `reference` 명령을 실행한다. 이 명령은 정답
label을 읽지 않고 공개 모델 선택 200문항과 유사도를 비교한다. exact copy 또는 기준값
0.84 이상의 가벼운 paraphrase가 하나라도 있으면 report를 만들지 않는다. 통과 report의
SHA-256과 protocol·reference corpus SHA-256을 commitment와 실행 승인에 함께 넣는다.

질문 단계에는 product family, intent, disposition, 작성자 메모 같은 정답 단서를
넣지 않는다. 정답키는 prediction hash가 외부에 기록되기 전까지 Agent 실행 환경에
전달하지 않는다.

field 점수는 QueryPlan에서 field를 다시 추론하지 않고 `gold_schema_field_ids`만
사용한다. 또한 commitment 생성 ≤ 실행 승인 ≤ prediction 생성 ≤ 외부 receipt 기록 ≤
score 생성의 UTC 순서가 하나라도 뒤집히면 실행 또는 채점을 거부한다.

## 3. 최초 실행 순서

```text
질문 → 고정 public corpus near-duplicate gate
→ protocol·report·commitment·독립 실행 승인 binding 검증
→ BGE-M3·KURE-v1 snapshot 전체 byte gate
→ 두 모델을 exact local path에서 double gate로 로드
→ 동일 canonical Schema 문서로 두 Index 내부 생성
→ 고정 IntentRouter와 Lexical/BGE/KURE 동시 실행
→ prediction 파일 atomic create-only 저장
→ 독립 평가자가 prediction SHA-256을 외부 append-only 저장소에 기록
→ 그 뒤에만 정답키 공개·채점
```

공식 실행 진입점은 다음 CLI다.

```bash
finance-evaluate-schema-embedding-external-v2 reference --help
finance-evaluate-schema-embedding-external-v2 run --help
finance-evaluate-schema-embedding-external-v2 score --help
```

실제 질문 bundle과 정답키가 없는 현재 상태에서는 실행하지 않는다.

공식 bundle이 오기 전에는 `rehearse --output-dir <시스템 임시 경로 아래 빈 디렉터리>`로 해시·receipt·
채점 절차만 연습할 수 있다. 모든 파일은 `internal_synthetic_not_blind` envelope로
감싸 공식 loader가 읽지 못하며, 보고서에는 `never_model_selection_evidence=true`가
고정된다. 이 결과는 후보 선정이나 blind 성능 근거로 사용할 수 없다.

## 4. Router와 OOD 안전 평가

- Router는 질문만 보고 EXECUTE·CLARIFY·UNSUPPORTED와 상품군·intent를 판단한다.
- Family 정확도는 상품군을 실행에 사용하는 EXECUTE 문항에서만 계산한다. 올바르게
  상품군 없이 차단한 CLARIFY·UNSUPPORTED를 family 오답으로 세지 않는다.
- Phase 1에서는 Router가 예측한 CLARIFY·UNSUPPORTED가 operational Dense를 호출하지
  않았는지 기록한다. 정답키 공개 뒤에는 gold CLARIFY·UNSUPPORTED 전체를 다시 대조해
  실제 provider 호출 수와 질문 단위 무실행률을 계산하며, 0회·100%만 통과한다.
- OOD probe는 숨겨진 family label을 읽지 않고 네 승인 상품군을 모두 독립 검색한다.
- OOD probe 결과에는 실행 권한이 없으며 threshold 평가에만 사용한다.
- 100문항은 질문 ID SHA-256 순서로 calibration 50개와 test 50개로 고정 분할한다.
- calibration에서 false accept 0인 기준만 test로 넘기며, test false accept도 0이어야 한다.
- 안전한 threshold가 없으면 Dense 기권 기준을 채택하지 않고 기존 Router·서버 규칙을
  유지한다.

## 5. 결과 해석 규칙

- BGE-M3·KURE-v1·Lexical의 exact, Recall@5·@10을 함께 보고한다.
- Router disposition·family·intent 정확도와 control 무실행률을 별도로 보고한다.
- paired bootstrap(같은 질문별 모델 차이를 반복 표본화하는 통계)의 95% 구간을
  확인한다.
- Docker p50·p95·p99·메모리와 전체 Agent E2E 결과를 컴포넌트 점수와 분리한다.
- 최초 결과를 본 뒤 후보, prompt, threshold나 정답키를 고쳐 같은 평가를 다시
  “최초 blind”라고 부르지 않는다.

외부 receipt와 비공개 정답키가 없으면 채점기가 report 생성을 거부한다. 이 조건을
모두 통과하기 전에는 모델을 최종 선정하거나 사용자-visible 경로를 활성화하지 않는다.
