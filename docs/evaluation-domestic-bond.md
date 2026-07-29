# 국내채권 핵심 평가 기준선

상태: v1.0 동결 · 로컬 Qwen 실험 완료
기준일: 2026-07-29

국내채권 42,394행을 정규화한 뒤 자연어 QueryPlan, 결정론적 검색, 독립 검증,
field-level evidence와 근거 기반 최종 답변을 시험한 세 번째 상품군 기준선이다.
HyperCLOVA X 성능이나 공식 공모전 점수를 뜻하지 않는다.

## 1. 데이터와 품질 계약

- logical grain: `PD_NO` 한 채권
- 총 42,394행, 검색 가능 42,394행, 격리 0행
- `BUY_YIELD`, `AFTER_TAX_YIELD`, `BUYABLE_QUANTITY`: 881행(2.0781%)
- 수량이 양수인 행: 325개
- 수량이 양수이고 2026-07-11 스냅샷에 만기 전인 실제 매수 가능 행: 254개
- 발행일: 42,055행(99.2004%), 만기일·재계산 잔존일수:
  42,075행(99.2475%)
- 신용등급: 24,750행(58.3809%); 정확값·목록 일치만 허용
- 동적 매수 값의 기준일: 현재 매수 가능 254행 모두 2026-02-24

원천 `REMAINING_DAYS`는 동적 기준일로 계산되어 파일 스냅샷과 정확히 137일
차이가 난다. 실행 계약은 이 값을 사용하지 않고 `MAT_DT - 2026-07-11`로
잔존일수를 다시 계산한다. 결측과 0 날짜 sentinel은 `UNKNOWN`으로 보존한다.

동적 수량·수익률·듀레이션은 137일 stale 경고를 반드시 노출한다. 수익률은
원천 조회값이지 미래 또는 실현 수익 예측이 아니다. 공식 코드북이 없는
위험코드는 정확값만 조회하며 숫자 순서를 위험 서열로 해석하지 않는다.

정규화 artifact:

- SQLite SHA-256:
  `40265aa326d63244727294ac29c1cd38c898f05b6a80dfd51fd8ac38e08764bc`
- manifest SHA-256:
  `d724d99e9ffd24148144b3534795d0d545e0ebff9d01509966f6ebbb218f8312`

## 2. 동결 평가 세트

- suite: `bond-core-50`
- development 40개, local-inference holdout 10개
- 실행 47개, 모호성·미지원 차단 3개
- 범주: 매수 가능 여부, 대·소분류와 채종, 장내·장외, 통화, 날짜,
  수치 조건·정렬, 발행사·상품·식별자, 신용등급, 안전 차단

동결 파일:

- [50문항 suite](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/bond_core_50.json)
- suite SHA-256:
  `b2d19e3af43fb8d6d4be03895088380b263bb8aca2c53c29b32de622a29c4178`

모델 없는 expected provider는 QueryPlan·oracle 50/50과 최종 답변 50/50을
통과했다. 이는 parameterized SQLite 결과와 독립 Python verifier의 후보 수·
상위 ID, evidence와 안전 차단 계약이 동결 정답과 일치한다는 하네스 회귀다.

## 3. 로컬 Qwen QueryPlan 결과

모델:

- `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- revision `5a5a776300a41aaa681dd7ff0106608ef2bc90db`
- temperature 0, seed 42, JSON Schema constrained decoding, worker 4

| 지표 | 결과 |
| --- | ---: |
| 전체 strict | 50/50 |
| development | 40/40 |
| local-inference holdout | 10/10 |
| valid plan | 100% |
| plan·constraint exact | 100% |
| oracle exact | 100% |
| safety block | 100% |
| 생성 latency | p50 3.76초, p95 4.43초, 최대 8.90초 |

report SHA-256:
`d5efc17f4332dd5fa69007a732f9fdd7a7ce72b9fbe17b68f8290791f44de314`

이 점수는 Qwen 단독이 아니라
`로컬 LLM → lexical canonicalizer → registry·Pydantic → oracle → verifier`
전체의 계약 준수율이다. 같은 개발자가 suite와 규칙을 작성했으므로 완전한
blind 일반화 성능으로 주장하지 않는다.

## 4. 근거 기반 최종 답변 결과

답변 평가는 동결 expected QueryPlan을 사용해 검색 해석 오차를 분리한다.
실제 상품명·식별자·값·날짜는 LLM에 주지 않고, opaque result reference와
사용 가능한 field label·단위·품질·warning code만 전달한다.

| 지표 | 결과 |
| --- | ---: |
| 전체 strict | 50/50 |
| LLM grounded | 46 |
| 결정론적 빈 결과 | 1 |
| 안전 차단 | 3 |
| 결정론적 폴백 | 0 |
| 상품 순서·evidence reference | 100% |
| field evidence citation·수치 충실도 | 100% |
| 경고·source date coverage | 100% |
| 검출된 미지원 claim | 0 |
| 생성 latency | p50 2.27초, p95 2.43초, 최대 2.46초 |

첫 실행은 22/50, 폴백 28건이었다. 모델이 만든 `매수수익률`과
`매수가능수량` 설명을 안전 검증기가 `매수` 권유로 오탐한 것이 원인이었다.
실제 “지금 매수하세요”는 계속 차단하면서 두 원천 필드명만 허용하도록
검증 경계를 좁히고 회귀 테스트를 추가한 뒤 50/50을 재현했다.

최종 report SHA-256:
`56fde2ecd0a2db5eadff8b568a257a2f388d8ff9b7903b9e8b12263ad85f9b69`

## 5. 실제 통합 E2E

질문:

> 잔존일수 365일 이하인 매수 가능한 회사채를 매수수익률 높은 순으로
> 3개 보여줘.

로컬 Qwen이 QueryPlan과 GroundedAnswerDraft를 연속 생성했다.

- 잠긴 조건: 잔존일수 `lte 365`, 대분류 `회사채`, 현재 매수 가능 `true`
- 후보: 23개
- 상위 ID: `KR6214346EB9`, `KR6214341F37`, `KR6214343FC1`
- answer mode: `llm_grounded`
- Answer Verifier 모든 check 통과
- 각 수익률에 원천 ID, Excel 행, `BUY_YIELD`, 기준일 2026-02-24 인용
- answer generation latency: 1.12초
- artifact SHA-256:
  `e9c4b671dbdf80cd5b81e4280742484c79c13b34688825f0cc9d70016916c47c`

HyperCLOVA X endpoint·credential은 사용하지 않았다. 테스트 종료 후 GPU는
74MiB·18MiB, utilization 0%로 복귀했고 loopback 18000 포트가 해제됐다.

## 6. 재현

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset bond \
  --data-dir "../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset bond \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset bond \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 모델 재현은 [로컬 LLM 테스트 런타임](local-llm.md)의 세 가지 opt-in과
`--provider local_test`를 사용한다.

## 7. 한계와 다음 단계

- 동적 매수 정보가 스냅샷보다 137일 오래되어 실제 주문 가능성을 보장하지 않는다.
- 신용등급 coverage가 낮고 등급 순서 비교를 아직 지원하지 않는다.
- 이자 지급 구조, 콜·풋 조건, 세제, 듀레이션 산식은 제공 필드만으로 완전한
  상품 설명을 만들기에 부족하다.
- 오타·구어체·복합 대화·prompt injection을 포함한 독립 blind suite와 사람
  기준의 답변 명확성 평가는 별도로 필요하다.
- HyperCLOVA X 연결 시 공식 Structured Outputs subset용 schema adapter와
  동일한 oracle·verifier·evidence 회귀를 재사용해야 한다.
