# 공모펀드 핵심 평가 기준선

상태: v1.2 QueryPlan·Oracle 계약 동결 · 최초 holdout 9/10 보존

기준일: 2026-07-29

이 문서는 공모펀드 자연어 질문을 검색 계획으로 바꾸는 기준과 실제 데이터
검색 결과를 고정한 회귀 계약이다. expected provider의 50/50은 사람이 작성한
기대 QueryPlan을 실행한 평가 하네스 검증이다. 별도로 개발 전용 로컬 Qwen과
결정론적 linker를 합친 hybrid parser는 development 40문항을 최초 실행에서
40/40 통과했다. parser 규칙을 commit `32e12fa`로 동결한 뒤 처음 실행한
holdout 10문항은 9/10이었다. 실패 한 건은 수정하거나 숨기지 않고 그대로
보존한다.

## 1. 동결 평가 세트

- suite ID: `fund-core-50`
- 질문 50개
  - development 40개
  - holdout 10개
- 처리 기대
  - 결정론적 검색 44개
  - 모호성·미지원 조건 차단 6개
- 평가 범주
  - 공모 범위와 판매 상태
  - 국내·해외·혼합 및 운용 속성 분류
  - 투자지역·투자자 유형·위험등급·환헤지
  - 1주·1개월·3개월·6개월 수익률
  - 통화가 고정된 AUM
  - 상품번호·정식명·짧은 이름 조회
  - 자연스러운 표현 변형
  - 모호한 추천과 미지원 필드 안전 차단

동결 파일과 데이터 hash:

- [50문항 suite](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/fund_core_50.json)
- suite SHA-256:
  `77d9be9ca86d9654fb61a52290ca08eadff6b618f861b985367b05d195c582b2`
- SQLite SHA-256:
  `99fac786e5be0ec5a7a53e11e1bd3bbccd5b37ab15243ecbf8b864a85b375ca4`
- manifest SHA-256:
  `be83a616d033db2328d231499d1f0492323d02bace4f153ad3da4860a0d10bcd`

평가 CLI는 DB와 manifest hash가 다르면 실행 전에 실패

## 2. 공모펀드 전용 안전 계약

모든 50문항의 기대 QueryPlan에 다음 조건을 정확히 한 번 포함

```text
public_offering = true
strength = locked
```

사용자가 공모라는 단어를 생략해도 시스템이 이 조건을 추가해야 함. 사모 15개와
공·사모 구분 결측 8개는 정상 보존하지만 검색 결과에서는 제외

AUM은 원천 통화가 다르면 직접 비교할 수 없으므로 모든 AUM 검색·정렬 문항에
`trading_currency = KRW` 또는 `USD`를 함께 잠금. AUM 0은 UNKNOWN으로 처리해
필터·정렬에서 제외

다음 질문은 조건을 무시하거나 추측하지 않고 차단

- 안전하고 괜찮은 상품 추천
- 총보수·판매수수료 순위
- 운용사 이름 검색
- 1년 이상 장기 수익률 순위
- 오늘 기준 최신 수익률
- 클래스 합산 후 대표 펀드 순위

## 3. Expected QueryPlan·Oracle 결과

동결 SQLite에서 expected provider로 전체 회귀:

| 지표 | 결과 |
| --- | ---: |
| strict accuracy | 50/50 |
| valid plan | 100% |
| plan exact | 100% |
| constraint exact | 100% |
| Oracle exact | 44/44 |
| safety block | 6/6 |
| development | 40/40 |
| holdout | 10/10 |

`expected` provider는 suite에 기록된 기대 QueryPlan을 그대로 제공. 이 결과가
보장하는 것은 다음 세 가지

- 50개 기대 QueryPlan이 field registry 계약에 맞음
- SQLite Oracle과 독립 Python Result Verifier가 후보 수와 순위를 동일하게 계산
- 안전 문항이 SQL 실행 전에 차단

이 결과만으로 자연어 parser나 LLM 성능을 주장할 수 없음

## 4. 로컬 Qwen development 최초 결과

공모펀드 전용 내부 schema와 lexical/schema linker를 구현한 뒤 로컬 Qwen
hybrid parser로 development 40문항만 실행했다.

| 지표 | 최초 결과 |
| --- | ---: |
| strict accuracy | 40/40 |
| valid plan | 100% |
| plan exact | 100% |
| constraint exact | 100% |
| Oracle exact | 100% |
| safety block | 100% |
| 생성 지연 p50 | 2,905.228ms |
| 생성 지연 p95 | 4,288.772ms |
| 생성 지연 max | 4,437.341ms |

실행 조건:

- provider: `local_test`
- model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- served name: `qwen3-local-test`
- model revision:
  `5a5a776300a41aaa681dd7ff0106608ef2bc90db`
- workers: 4
- report:
  `fund-local-qwen-development-first.json`
- report SHA-256:
  `c0d8a60b0a6465b9ef6035f1a3787b4835d765385ae18bfc0ad97d15d1cd99f6`

이 점수는 Qwen 단독 점수가 아니다. 모델이 구조화된 초안을 만들고, 서버의
결정론적 linker가 문장에서 확인 가능한 공모 범위·분류·수치·정렬·안전 신호를
canonicalize한 뒤 엄격한 QueryPlan 계약, SQLite Oracle과 독립 verifier가
검사한 전체 hybrid parser 점수다.

개발 세트는 구현 과정에서 linker 정합성 테스트에도 사용했으므로 새로운 질문에
대한 일반화 성능으로 주장하지 않는다. 다만 실제 로컬 모델 요청부터 구조화
출력, canonicalization, 검색, 검증까지 연결된 개발 경로가 동작함을 보장한다.

## 5. 로컬 Qwen holdout 최초 결과

parser·규칙과 development 결과를 commit `32e12fa`로 먼저 고정하고, 기존
holdout report가 없는 것을 확인한 뒤 10문항을 최초 1회 실행했다.

| 지표 | 최초 결과 |
| --- | ---: |
| strict accuracy | 9/10 |
| valid plan | 100% |
| plan exact | 90% |
| constraint exact | 90% |
| Oracle exact | 100% |
| safety block | 100% |
| 생성 지연 p50 | 3,900.262ms |
| 생성 지연 p95 | 8,786.938ms |
| 생성 지연 max | 8,786.938ms |

결과 보존:

- report:
  `fund-local-qwen-holdout-first-run.json`
- report SHA-256:
  `4bc96ecd7278bbbefe299a0ccea9bff94d14784621b91b2e7a71b414f945846f`
- development와 최초 holdout 합계: 49/50
- 실패 case: `fund-050`

실패 질문:

> 클래스는 합쳐서 대표 펀드별 AUM 합계가 큰 순으로 5개 보여줘

기대 동작은 공모 범위를 잠그고, 클래스 합산과 대표 펀드 집계를 현재 데이터
grain에서 지원하지 않는 조건으로 인식해 차단하는 것이다. 실제 plan은
`product_families=["fund"]`를 선택했지만 다음 차이가 있었다.

- `public_offering=true` locked 조건 누락
- 클래스 합산·대표 펀드 집계의 unsupported 신호 누락
- 지원할 수 없는 AUM 순위 생성

서버는 AUM 비교 통화가 지정되지 않았다는 별도 모호성을 통해 SQL 실행을
차단했다. 따라서 `safety_block_rate=100%`였지만 올바른 이유로 차단한 것은
아니며, strict failure가 맞다.

확인된 원인은 lexical family 감지가 질문에 `공모펀드`라는 정확한 표현이 있을
때만 `fund`를 반환한다는 점이다. `대표 펀드`라고만 말한 holdout 표현에서는
모델이 선택한 fund 상품군을 linker가 이어받지 못해 공모 범위와 펀드 전용
unsupported 규칙을 적용하지 못했다.

이 문항은 이제 공개된 실패 사례이므로 이후 수정 후 통과하더라도 새로운
holdout 성능으로 보고하지 않는다. 기존 suite의 사후 회귀와 별도의 새 blind
질문 평가를 구분한다.

### 5.1 사후 회귀 수정

최초 9/10 report를 commit `8871055`로 먼저 보존한 뒤 다음 최소 수정만 적용했다.

- 질문에서 상품군을 명시하면 기존 lexical 판정을 계속 우선
- 명시가 없으면 모델의 구조화 출력에 있는 단일 상품군을 보조 힌트로 전달
- 공모펀드 미지원 조건이 발견되면 실행 가능한 정렬도 fail-closed로 제거

`fund-050`의 실제 질문과 모델이 선택했던 `product_families=["fund"]`를 고정한
회귀 테스트를 추가했다. 수정 후 기대한 `public_offering=true` 조건,
unsupported 신호, 빈 ranking과 block disposition이 모두 일치한다.

로컬 Qwen holdout은 다시 호출하지 않았다. 무모델 deterministic linker로 동결
50문항을 재생한 결과는 50/50이고, 실제 DB의 expected QueryPlan·Oracle도
50/50이다. 이는 공개된 실패의 사후 회귀 통과일 뿐 최초 holdout 9/10을
대체하지 않는다.

## 6. 실행 비활성 상태에서의 평가

공모펀드 dataset은 계속 `execution_enabled: false`로 유지. 일반 Agent와
공식 HCX schema는 공모펀드 실행을 허용하지 않음

평가 runner는 다음 조건을 모두 만족할 때만 내부 승인 회귀를 허용

- dataset이 정확히 `fund`
- provider가 동결된 `expected` 또는 개발 전용 `local_test`
- 공모 범위·모호성·미지원 조건 검사를 모두 통과

`local_test`는 공모펀드 전용 내부 schema를 사용하며 일반 Agent 경로에는
노출되지 않는다. 로컬 fund holdout 또는 전체 split은 별도 unlock flag 없이는
CLI가 실행 전에 실패한다. 따라서 개발 평가를 추가했다는 이유로 공식 Agent
실행 범위가 열리지 않음

## 7. 재현 명령

`finance_agent/` 디렉터리에서 실행:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/generate-fund-suite.py

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset fund \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-expected-all.json
```

첫 명령은 실제 DB에서 44개 문항의 후보 수와 상위 상품 ID를 다시 계산해 suite를
생성. DB·manifest가 바뀌지 않았다면 suite hash도 동일해야 함

로컬 development 재현:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset fund \
  --provider local_test \
  --split development \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-local-qwen-development.json
```

최초 holdout에 사용한 명령:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset fund \
  --provider local_test \
  --split holdout \
  --unlock-fund-holdout \
  --workers 4 \
  --output artifacts/evaluation/fund-local-qwen-holdout-first-run.json
```

이 명령을 다시 실행한 결과는 최초 holdout이 아니며 사후 회귀로만 취급한다.

## 8. 해석 한계와 다음 단계

- 같은 개발자가 질문과 기대 조건을 작성했으므로 holdout 10개도 완전한
  unbiased 일반화 세트가 아님
- 현재 평가는 `SEARCH` intent와 QueryPlan·검색 결과만 검증
- 공모펀드 답변 문장 품질과 Answer Verifier는 아직 평가하지 않음
- development 40개는 구현·정합성 검사에 사용됐으므로 튜닝 세트임
- holdout 10개는 commit 이후 최초 1회 실행해 9/10이며 이제 미사용 세트가 아님
- HyperCLOVA X 성능과 공식 평가 점수를 대변하지 않음

다음 단계:

1. 기존 50문항은 사후 회귀로만 사용하고 새 blind 표현 변형 세트를 별도 작성
2. 공모펀드 grounded answer와 사람 품질 평가 추가
3. HCX schema에 fund를 노출하고 서버 계약 테스트 통과
4. 그 뒤에만 `execution_enabled: true` 전환 검토
