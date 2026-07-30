# 근거 기반 최종 답변 평가

상태: v1.3 국내 ETP·국내채권·공모펀드 SEARCH·COMPARE·자연어 비교 E2E 기준선 · 로컬 Qwen 실험 완료
기준일: 2026-07-30

이 평가는 검색 결과가 맞은 뒤 최종 답변에서 상품·순위·숫자·기준일·근거가
바뀌지 않는지 측정한다. HyperCLOVA X 성능이나 공식 점수가 아니며, 자유 생성
문장 능력만을 측정한 점수도 아니다.

## 1. 평가 경계

기본 답변 회귀는 생성 계층만 분리하기 위해 동결된 기대 QueryPlan을 사용한다.
7.2절의 공모펀드 자연어 비교 회귀는 parser부터 검증 답변까지 추가로 통합한다.

```text
동결 expected QueryPlan
→ SQLite oracle
→ 독립 Result Verifier
→ field-level evidence
→ 로컬 Qwen GroundedAnswerDraft
→ Answer Verifier
→ 숫자·식별자·인용을 서버가 컴파일
→ 실패 시 결정론적 safe renderer
```

이 절의 실행 47문항·안전 차단 3문항은 **국내 ETP 기준선**이다. 실행형 문항은
LLM 답변 계약을 평가하고, 모호성·미지원 문항은 LLM을 호출하지 않고 안전하게
차단한다. 별도로 국내 ETP 대표 질문 한 건은 로컬 Qwen이 QueryPlan과 답변
초안을 연속 생성하는 실제 E2E로 확인했다. 상품군별 구성은 국내채권 46개
LLM 생성·결정론적 빈 결과 1개·안전 차단 3개, 공모펀드 44개 LLM 생성·안전
차단 6개로 서로 다르다. 공모펀드 true COMPARE는 별도 20문항에서 18개
LLM 생성과 2개 누락 대상 결정론 처리로 평가한다.

## 2. 최소권한 답변 계약

LLM에는 상품명, 티커, 상품 ID, 실제 수치, 원천값, 날짜를 전달하지 않는다.
각 결과는 `result_1` 같은 불투명 참조로만 보이며 다음 정보만 제공한다.

- 선택 가능한 canonical field와 한국어 label
- field 단위와 품질 상태
- 필수 ranking 또는 lookup evidence field
- 안정적인 warning code

JSON Schema constrained decoding은 안전한 lead, 결과 참조 순서, warning code를
고정한다. LLM은 evidence field와 제한된 정성 설명을 제안한다. 서버는 다음을
다시 검사한다.

- 결과 참조와 순서가 검증 결과와 동일한가
- 선택한 field가 해당 상품 evidence에 존재하고 사용 가능한가
- ranking·lookup에 필요한 evidence를 빠뜨리지 않았는가
- 허용되지 않은 숫자·상품 식별자·상품명을 문장에 넣었는가
- 추천·매수·매도·예측·전망·유불리 같은 투자 해석을 추가했는가
- 필수 경고를 누락하거나 새 경고를 만들었는가

검증 실패나 provider 오류가 발생하면 LLM 문장을 전부 버리고, 검증된
QueryPlan·결과·evidence로 만든 결정론적 답변을 반환한다.

서버가 컴파일한 최종 답변도 다시 검사한다. 검증된 상품 순서·수치가 담긴
결정론적 본문이 정확히 보존됐는지, 선택한 field의 원천 ID·행·컬럼·기준일과
파일 스냅샷 날짜가 바뀌지 않았는지 확인하고 실패하면 같은 폴백을 적용한다.

## 3. 개발 과정에서 확인한 실패

| 버전 | development strict | 안전한 답변 | 폴백 | 관찰 |
| --- | ---: | ---: | ---: | --- |
| v1 | 8/40 | 40/40 | 32 | 기간명·표시 개수까지 금지한 과도한 무숫자 정책 |
| v2 | 33/40 | 40/40 | 7 | 숫자·상품명 생성과 결과 참조 재정렬을 verifier가 차단 |
| v3 | 40/40 | 40/40 | 0 | 실제 값·날짜 제거, opaque ref, 동적 schema와 정성 문구 검증 |

v2까지 LLM 입력에서 실제 값과 날짜가 완전히 제거되지 않은 구현 누락이 있었다.
실패 출력에서 모델이 값을 다시 문장에 넣는 현상을 확인한 뒤 v3에서
least-privilege payload로 수정했다. 이 과정은 LLM이 그럴듯한 문장을 만들었다는
이유만으로 답변을 신뢰하면 안 된다는 실증 사례다.

## 4. 국내 ETP 최종 로컬 Qwen 결과

모델:

- `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- revision `5a5a776300a41aaa681dd7ff0106608ef2bc90db`
- temperature 0, seed 42, worker 4

| 지표 | 결과 |
| --- | ---: |
| 전체 strict | 50/50 |
| LLM grounded 실행 | 47/47 |
| 모호성·미지원 안전 차단 | 3/3 |
| 결정론적 폴백 | 0 |
| 상품·순위 일치 | 100% |
| evidence reference 정확도 | 100% |
| field evidence citation 포함 | 100% |
| 수치 충실도 | 100% |
| warning coverage | 100% |
| source date coverage | 100% |
| 검출된 미지원 claim | 0 |
| 생성 latency | p50 2.36초, p95 4.51초, 최대 4.85초 |

답변 생성에 처음 사용한 10문항 split도 10/10이었다. 다만 동일 질문은 이전
QueryPlan 평가에 사용됐고 같은 개발자가 suite를 작성했으므로 프로젝트 전체의
완전한 blind/unbiased holdout이라고 주장하지 않는다.

최종 report SHA-256:

- expected 50문항:
  `105dbefd12b5e61f8cd8a38191775073f6425d69df5613b3e42e34d9c7d210f6`
- development v3:
  `45927d7f31bad9a091a7e609bdf545d98ec72c8a72bea2dca0c5088e7516e02d`
- 답변 생성 holdout 첫 실행:
  `a5dd01a5271e9df0369f897072324dfaa342ec34bcfef515423697f79ccb372e`
- 최종 전체 회귀:
  `5e4374adb5427ae2e195c592f5bc2f6b57ed60a836d32ff60bd5643ebcbae2ec`

## 5. 대표 전체 E2E

대표 국내 ETP 질문은 로컬 Qwen QueryPlan과 답변 초안을 모두 사용했다.

- 후보 211개, 상위 5개와 기존 oracle 일치
- answer mode: `llm_grounded`
- Answer Verifier 모든 check 통과
- 각 해설에 원천 ID, Excel 행, source column, 기준일을 서버가 컴파일
- E2E artifact SHA-256:
  `7e865f54e2a405e7ed98d77339f6b70d8ebb984b5acfccfb2b580e3db0cceb15`

실행 중 GPU 메모리는 28,255MiB·28,199MiB였다. 종료 후 71MiB·15MiB로
복귀했고 loopback 18000 포트도 해제됐다. HyperCLOVA X endpoint나 credential은
사용하지 않았다.

## 6. 국내채권 답변 기준선

국내채권 50문항은 실행 47개, 안전 차단 3개로 구성된다. 최종 결과는
LLM grounded 46개, 결정론적 빈 결과 1개, 폴백 0개이며 전체 50/50이다.
상품 순서, evidence reference와 citation, 숫자, 필수 경고, source date는 모두
100%였고 검출된 미지원 claim은 0건이었다. 생성 latency는 p50 2.27초,
p95 2.43초, 최대 2.46초였다.

첫 실행은 22/50이었다. `매수수익률`과 `매수가능수량`의 필드명까지 투자
권유로 간주한 verifier 오탐 때문에 28건이 안전한 결정론적 답변으로 폴백됐다.
실제 “지금 매수하세요” 차단은 유지하면서 원천 field phrase만 허용하고 회귀
테스트를 추가한 뒤 50/50을 재현했다.

대표 통합 질문은 잔존일수 365일 이하·회사채·현재 매수 가능의 세 조건을
정확히 잠갔다. 후보 23개와 상위 3개가 oracle과 일치했고, 각 매수수익률에
원천 행·컬럼·2026-02-24 기준일이 컴파일됐다.

상세 데이터 계약, report·artifact hash와 재현 명령은
[국내채권 핵심 평가 기준선](evaluation-domestic-bond.md)에 기록한다.

## 7. 공모펀드 답변 기준선

공개된 `fund-core-50`의 expected QueryPlan으로 답변 계층만 분리해 평가했다.
parser와 자연어 질문은 로컬 Qwen에 다시 전달하지 않았으며 새 blind v1.1
파일도 로드하지 않았다.

```text
expected QueryPlan
→ 공모 범위·AUM 통화 실행 정책
→ SQLite Oracle·Result Verifier
→ 공모펀드 field-level evidence
→ 로컬 Qwen GroundedAnswerDraft
→ draft·최종 컴파일 Answer Verifier
→ 안전한 답변 또는 결정론적 fallback
```

field evidence에는 `itm_no` 상품 키, `PRFD01N001`, 원본 Excel 행·컬럼,
정규화값, 단위, 품질과 2026-07-11 스냅샷 기준일을 보존한다. 공모 범위와
클래스 grain 경고는 항상 포함하고 단기 수익률·AUM·운용 속성에는 해당 품질
경고를 추가한다. AUM을 필터·정렬·집계하는 계획은 `KRW` 또는 `USD`가
locked 조건으로 정확히 하나 없으면 Oracle 실행 전에 차단한다.

| 지표 | expected | 로컬 Qwen |
| --- | ---: | ---: |
| 전체 strict | 50/50 | 50/50 |
| 실행형 답변 | 44/44 | 44/44 |
| 모호성·미지원 안전 차단 | 6/6 | 6/6 |
| 결정론적 폴백 | 0 | 0 |
| fallback rate | 0% | 0% |
| 상품·순위 일치 | 100% | 100% |
| evidence reference·citation | 100% | 100% |
| 수치·기준일·warning coverage | 100% | 100% |
| 검출된 미지원 claim | 0건 | 0건 |

로컬 생성 latency는 p50 2,602.016ms, p95 4,806.568ms, 최대
5,069.169ms였다. 전체 report SHA-256:

- expected:
  `e516e07e135bca0ae54f9d10f2ee917d6518cafe76c1ffd87717f56e9dd66f38`
- 로컬 Qwen:
  `30b02b11b6780c422f709f88f45a92fc0a16e6c85a553ae415ee5ebd4eb46b6c`

실행 중 GPU 메모리는 28,253MiB·28,197MiB였다. 종료 후
71MiB·15MiB로 복귀했고 loopback 18000 포트도 해제됐다.

자동 grounding 계약은 모두 통과했지만 44개 초안의 lead는 1종, 상품별 설명
216개는 18종이었다. 현재 생성 문체는 안전성을 우선해 보수적이고 반복적이며,
사람 기준 자연스러움·중복·비교 용이성은 아직 평가하지 않았다.

이 50문항은 이미 공개된 회귀 세트이며 SEARCH 결과의 순위·근거 설명을
평가한다. 새로운 blind 성능으로 해석하지 않으며, COMPARE는 다음의 별도
공개 세트로 평가한다. 공식 Agent의 공모펀드 실행도 계속 비활성이다.

### 7.1 공모펀드 true COMPARE 답변

`fund-compare-core-20`은 정확한 `itm_no` 두 개와 비교 필드를 가진 expected
QueryPlan으로 실제 `Intent.COMPARE` 경로를 실행한다. 서버가 요청 순서,
필드별 원천값, `두 번째-첫 번째` 차이, AUM 통화 호환성, 결측과 누락을
결정한다. Qwen은 상품명·수치·날짜·원천행을 보지 않고 허용된 evidence field의
설명만 제안한다.

| 지표 | expected | 로컬 Qwen |
| --- | ---: | ---: |
| 전체 strict | 20/20 | 20/20 |
| grounded answer | 18/18 | 18/18 |
| 누락 대상 결정론 답변 | 2/2 | 2/2 |
| verifier fallback | 0 | 0 |
| field status·numeric delta | 100% | 100% |
| field citation·기준일 | 100% | 100% |

로컬 생성 latency는 p50 1,522.937ms, p95·최대 4,447.683ms다. 통화가 다른
AUM은 각 값만 보여주고 차이를 계산하지 않으며, 한쪽 값이 없으면 `확인 불가`,
상품이 없으면 LLM 호출 자체를 생략한다.

이 결과도 공개된 구현 회귀 세트의 계약 준수율이다. 이 20문항 답변 세트
자체는 자연어 질문에서 비교 상품명을 해석하지 않는다. 다음 24문항 통합
회귀가 두 계층을 한 번에 잇지만, 새로운 blind 성능과 사람이 판단하는
자연스러움은 포함하지 않는다.

### 7.2 공모펀드 자연어 COMPARE 통합 답변

`fund-compare-parser-core-24`의 자연어 질문을 parser·exact resolver에서
끝내지 않고, COMPARE QueryPlan·Oracle·field-level evidence와 검증 답변까지
연결했다. `fund-compare-e2e-core-24` overlay가 실행 문항의 field status,
numeric delta, 실제 비교 셀 값과 evidence provenance를 별도로 동결한다.

```text
자연어 비교 질문
→ 대상 표현·필드 parsing과 exact resolution
→ COMPARE QueryPlan·Oracle·Result Verifier
→ 서버 비교 계산·field-level evidence
→ GroundedAnswerDraft
→ draft·compiled Answer Verifier
→ 근거·기준일 포함 답변 또는 결정론적 fallback
```

| 지표 | expected | 로컬 Qwen |
| --- | ---: | ---: |
| 전체 strict | 24/24 | 24/24 |
| 실행·안전 차단 | 16/16 · 8/8 | 16/16 · 8/8 |
| parser 호출·답변 생성 시도 | 24 · 16 | 24 · 16 |
| grounded answer | 16/16 | 16/16 |
| verifier fallback | 0 | 0 |
| parser·resolution·plan·Oracle·차단 | 100% | 100% |
| field status·numeric delta | 100% | 100% |
| 실제 비교 셀 값·evidence provenance fingerprint | 100% | 100% |
| Answer Verifier | 100% | 100% |
| evidence citation·기준일 | 100% | 100% |

로컬 Qwen의 parser 지연은 p50 567.105ms, p95 751.575ms, 최대
805.811ms였다. 답변 지연은 p50 1,582.531ms, p95·최대 2,225.406ms였고,
전체 E2E 지연은 p50 2,142.249ms, p95 2,737.07ms, 최대 2,853.783ms였다.

- E2E overlay suite SHA-256:
  `5f1511c8dea53b13d1207ee1c80adcf6db4c431581ca8b15d5d683951bc7f88c`
- source question suite SHA-256:
  `9e2bd72c001f6384a08111ae195de0bf1a962fe6aafa46b52df012917c1b4c9c`
- expected report SHA-256:
  `01840035f13f14923d335bb01ad77355d0ef3b493c4849ab7c89bfaa6bee435d`
- 로컬 Qwen report SHA-256:
  `b67ccbad9ab1cc93d682f4b27d0ae38a901623081477309a7f53f07d91709976`

모든 24문항이 parser를 거치지만 실행 가능한 16문항만 답변 생성을 시도한다.
안전 차단 8문항은 Oracle·답변 생성 전에 종료한다. 이 결과는 공개 개발
회귀이며 독립 plan 계약, 동결된 field status·delta·실제 비교 셀 값과 별도의
근거 provenance, 정확한 상품명 span과 질문 전체 대상 순서까지 검사한다.
두 identity 사이의 정확한 연결어와 위치별 문장부호 문법도 검증하고 제외·대신·
포함 표현, 질문 전체의 미등록 잔여 표현, 비어 있거나 미종결·역방향·중첩·
줄바꿈이 잘못된 따옴표를 차단한다. 독립 blind·사람 품질·HyperCLOVA X 결과는
아니며 공식 fund Agent 실행도 계속 비활성 상태다.

## 8. 재현

서버 시작:

```bash
scripts/local-llm/serve-qwen.sh
```

국내 ETP 답변 평가:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset domestic_etp \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect
```

공모펀드는 같은 명령에 `--dataset fund`를 사용한다. 이 명령은 expected
QueryPlan을 사용해 답변 계층만 격리하며 parser holdout을 다시 실행하지 않는다.

공모펀드 COMPARE expected 기준선:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 Qwen은 같은 명령에 로컬 provider 환경변수와 `--provider local_test`를
적용한다.

공모펀드 자연어 COMPARE 통합 expected 기준선:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-e2e-expected-all.json
```

로컬 Qwen 통합 회귀:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-compare-e2e-local_test-all.json
```

대표 전체 E2E는 Agent CLI에 `--provider local_test
--answer-provider local_test`를 함께 지정한다.

## 9. 해석과 다음 단계

최종 100%는 **강하게 제한된 evidence-compiled hybrid system의 계약 준수율**이다.
Qwen이 자유 형식 금융 답변을 100% 정확하게 생성했다는 뜻이 아니다. 실제 수치와
상품 식별자는 LLM이 보지 못하고 서버가 작성하므로 수치 정확도는 시스템
아키텍처의 결과다.

다음 평가는 별도로 진행한다.

1. 다른 작성자가 만든 blind 표현 변형·오타·장문·prompt injection 세트
2. 사람 rubric 기반 명확성·비교 용이성·중복·과도한 경고 평가
3. deterministic-only와 grounded narrative의 사용자 선호 비교
4. HyperCLOVA X answer provider와 비용·latency·fallback 비교
5. 봉인한 독립 blind 질문에서 자연어 COMPARE 통합 E2E 최초 평가

로컬 vLLM이 사용한 `const`, `prefixItems` 중심 동적 schema는 HyperCLOVA X
공식 Structured Outputs subset과 동일하지 않다. HyperCLOVA X 연결 시 전송
schema adapter를 별도로 만들고, 이 서버 Answer Verifier와 폴백은 그대로
유지해야 한다.
