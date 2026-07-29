# 해외 ETP 핵심 평가 기준선

상태: v1.0 동결 · 로컬 Qwen 실험 완료
기준일: 2026-07-29

이 문서는 HyperCLOVA X 연결 전에 만든 해외 ETP 자연어 QueryPlan parser와
결정론적 검색 경로의 첫 회귀 기준선이다. 최종 제출 모델의 성능을 뜻하지 않으며,
현재 데이터 snapshot과 50개 사람이 작성한 질문에서만 유효하다.

국내 ETF·ETN의 별도 50문항 기준선은
[국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md)에 기록한다.

## 1. 평가 대상과 동결 규칙

- suite: `overseas-etp-core-50`
- 질문: 50개
  - development 40개
  - 최초 실행 전까지 열어 보지 않은 holdout 10개
- 처리 기대:
  - 결정론적 검색 실행 42개
  - 모호성·미지원 조건으로 실행 차단 8개
- 범주: 복합 조건, 상품·자산 유형, 지역, 거래소, 상태, 총보수, AUM,
  날짜, 식별자, 상품명, 품질 gate, 모호성, 미지원 조건

질문, 기대 QueryPlan, 후보 수와 상위 상품 ID는 로컬 모델을 실행하기 전에 함께
동결했다. 실행 가능한 42개 문항의 oracle은 parameterized SQLite 검색과 독립
Python verifier가 동일한 결과를 내는지 확인한 뒤 기록했다.

동결 파일:

- [50문항 suite](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/overseas_etp_core_50.json)
- suite SHA-256:
  `166a233166af22cfddee1cf12d96066d133f64a4e5ec3d2f68e6634f7eafa53d`
- SQLite SHA-256:
  `eee9009ca741713a9a61e498cd5ed8366836d754c7d0c2dbd74ed7e456a2ebbe`
- 적재 manifest SHA-256:
  `5dd159ade2353154ed8d3d32068dd512cb2b3becba39972aa0dabff0edd50771`

평가 CLI는 실행할 때 SQLite와 manifest의 hash가 위 값과 다르면 즉시 실패한다.

## 2. 채점 기준

한 문항은 관련 검사를 모두 통과해야만 strict pass다.

| 지표 | 의미 |
| --- | --- |
| valid plan | 모델 출력이 엄격한 서버 `QueryPlan` 검증을 통과 |
| plan exact | 조건, 정렬, limit, disposition이 동결 기대값과 의미상 일치 |
| constraint exact | 명시된 필터 field·operator·value가 정확히 일치 |
| oracle exact | 실행 가능한 질문에서 후보 수와 상위 상품 ID가 모두 일치 |
| safety block | 모호하거나 미지원인 질문이 SQL 실행 전에 차단 |
| strict accuracy | 해당 문항에 적용되는 모든 검사가 통과한 비율 |

`oracle exact`와 `plan exact`는 의도적으로 분리한다. 예를 들어 모든 검색 가능 행이
같은 상태값을 가지면 잘못 추가된 상태 조건도 우연히 같은 상품을 반환할 수 있다.
검색 결과만 맞았다는 이유로 잘못된 QueryPlan을 통과시키지 않는다.

## 3. 로컬 모델 실험 결과

고정 모델은 `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` revision
`5a5a776300a41aaa681dd7ff0106608ef2bc90db`이며, temperature 0,
seed 42, JSON Schema constrained decoding, worker 4로 실행했다.

| 단계 | split | strict | valid plan | constraint | oracle | safety block |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 초기 단일 예시 중심 prompt | development 40 | 5/40 (12.5%) | 97.5% | 20.0% | 85.29% | 0.0% |
| 일반 parser prompt | development 40 | 28/40 (70.0%) | 100% | 70.0% | 85.29% | 100% |
| prompt + 결정론적 lexical linker | development 40 | 40/40 (100%) | 100% | 100% | 100% | 100% |
| **최초 holdout 실행** | **holdout 10** | **9/10 (90.0%)** | **100%** | **100%** | **100%** | **100%** |
| holdout 오류 수정 후 전체 회귀 1 | all 50 | 50/50 (100%) | 100% | 100% | 100% | 100% |
| 같은 코드의 전체 회귀 2 | all 50 | 50/50 (100%) | 100% | 100% | 100% | 100% |

가장 정직한 미사용 데이터 성능은 **최초 holdout 9/10, 90%**다. 실패한
`etp-core-035`는 “AUM이 정확히 0인 해외 ETF를 상품명 순서로”라는 질문에서
AUM 필터와 후보 0건은 맞았지만 정렬 field를 `aum`으로 잘못 선택했다. 이 사례를
본 뒤 일반적인 명시 정렬 우선순위와 복수 범주값 canonicalization을 수정했으므로,
이후의 50/50은 **사후 수정된 회귀 통과 결과**이지 새로운 unbiased holdout
성능이 아니다.

최종 두 실행의 generation latency는 각각 p50 2.23초·2.21초,
p95 3.36초·3.39초, 최대 4.79초·5.13초였다. latency가 들어가는 원본 report
hash는 서로 다르지만, 문항 ID·검사 결과·생성 QueryPlan·후보 수·상위 ID·오류만
선택한 semantic fingerprint는 두 실행 모두 다음과 같다.

`c4cd15d08a4758736fa78778be63ce335d3a5a3d98bd24691a8ad6c47f1f5ec9`

주요 원본 report SHA-256:

- 초기 development:
  `e366ee51683b6bc2d00883ae382c2ede253eed21cbc2799d1fe889129544ca02`
- 최종 development:
  `30e5d2c53a95d73187a498e43041a4ab5708ed2d8f76de1b301f46087e8465c2`
- 최초 holdout:
  `84ecf751c3b8780c48950352fae8821a9cc287e3d34dc7fb745e77cc804ec0b0`
- 전체 회귀 1:
  `265b1cdb64c58d23e8e68bff8111d32270af65f1eb1eec7d9b4df6389e55e082`
- 전체 회귀 2:
  `3d8f08936c6036cce5c38b1624a040b907b01122c19e49ca9888140078747644`

report는 `artifacts/evaluation/`에 생성되며 Git에서 제외된다.

## 4. 현재 parser가 실제로 하는 일

최종 100% 회귀는 로컬 LLM 단독 성능이 아니다.

```text
질문
→ 질문에서 안전한 lexical hint 계산
→ 로컬 LLM이 JSON Schema 범위의 QueryPlan 제안
→ 결정론적 linker가 명시된 범주·수치·정렬을 canonicalize
→ Pydantic과 registry가 타입·단위·capability 검증
→ execution policy가 모호성·미지원 조건을 SQL 전에 차단
→ SQLite oracle 실행
→ 독립 Python verifier로 재계산
```

이 hybrid 구조는 LLM의 언어 해석 능력을 쓰되, 데이터에 존재하는 값과 명시적
금융 표현을 코드로 고정해 환각과 run-to-run 변동을 줄인다. baseline 12.5%에서
prompt만으로 70%까지 올랐고, 남은 오류는 결정론적 linker와 fail-closed 실행
정책으로 제거했다.

## 5. 재현 명령

`finance_agent/` 디렉터리에서 모델 없이 suite·oracle·채점기를 먼저 검증한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/expected-all.json
```

로컬 Qwen 서버를 실행한 상태에서 세 가지 개발 전용 opt-in을 모두 지정한다.

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --provider local_test \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/local-qwen-all.json
```

튜닝 중에는 `--split development`만 사용하고, 새 holdout은 규칙을 동결한 뒤
한 번만 실행한다.

## 6. 해석 한계와 다음 기준선

- 해외 ETP의 `search` intent만 포함한다. 비교·집계·설명 intent는 아직 없다.
- 한 시점의 SQLite snapshot과 사람이 작성한 50문항에 맞춘 작은 회귀 세트다.
- prompt injection, 오탈자, 긴 대화 문맥, 다중 의미 표현을 충분히 다루지 않았다.
- local Qwen 결과이며 HyperCLOVA X 성능이나 공식 평가 점수를 대변하지 않는다.
- holdout을 한 번 본 뒤의 100%는 신규 질문 일반화의 증거로 사용하지 않는다.

다음 단계는 현재 50문항을 변경하지 않고 유지하면서, 표현 변형·경계값·공격적
입력을 포함한 **새로운 미사용 v1.1 세트 최소 100문항**을 만드는 것이다. 이후
전체 평가군을 250~400문항으로 확장하고 같은 하네스로 HyperCLOVA X provider를
평가한다.
