# 국내 ETP 핵심 평가 기준선

상태: v1.0 동결 · 로컬 Qwen 실험 완료
기준일: 2026-07-29

국내 ETF·ETN 1,734행을 정규화한 뒤 자연어 QueryPlan, 결정론적 검색,
독립 검증, field evidence까지 시험한 두 번째 상품군 기준선이다. 최종 제출
HyperCLOVA X 성능이나 공식 점수를 뜻하지 않는다.

## 1. 데이터와 계약

- logical grain: `pd_itm_no` 한 종목
- 총 1,734행, 검색 가능 1,733행
- Excel 1155행: 열 이동으로 key가 `KR`만 남은 손상 행; 복구하지 않고 격리
- 상품 유형: ETF 1,201행, ETN 532행(격리 행 제외)
- 총보수: 217행 제공, 0인 150행은 `UNKNOWN`, 양수 67행만 수치 검색 가능
- AUM: 1,453행 제공, 결측 280행과 0인 411행은 `UNKNOWN`
- 1일·1개월·3개월·6개월·1년·YTD 수익률: 기간별 결측을 행 수준
  `UNKNOWN`으로 처리
- 판매·거래정지 코드는 schema 설명과 0/1 분포에 근거한 잠정 매핑이며
  공식 코드북 확인 전 경고

SQLite와 manifest는 같은 원천에서 재구축했을 때 byte hash가 동일했다.

- SQLite SHA-256:
  `a91bfc713b88e9ba1e7da27508857cbe5ea200c4d7821d0b4fee7d9922684484`
- manifest SHA-256:
  `2e52b161a260fa19eab50e8281f04f8617ee1ee69d618b19bd624af4635fca97`

## 2. 동결 평가 세트

- suite: `domestic-etp-core-50`
- development 40개, local-inference holdout 10개
- 실행 47개, 모호성·미지원 차단 3개
- 범주: 유형, 자산·지역, 상태·연금, 총보수·AUM·가격·거래대금,
  기간 수익률, 전략·배수, 위험등급, 운용사·약어·기초지수·식별자, 안전 차단

동결 파일:

- [50문항 suite](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/domestic_etp_core_50.json)
- suite SHA-256:
  `c407cca721e1087ef1cab16368352c21cd7df75ea2b5a6f051e3ba978a6ce098`

expected provider는 50/50을 통과했다. 이는 동결 QueryPlan, parameterized
SQLite oracle, 독립 Python verifier, 후보 수와 상위 ID가 서로 일치한다는
하네스 자체의 회귀 검사다.

## 3. 로컬 Qwen 결과

모델과 revision은 해외 ETP 실험과 같다.

- `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- revision `5a5a776300a41aaa681dd7ff0106608ef2bc90db`
- temperature 0, seed 42, JSON Schema constrained decoding, worker 4

| split | strict | valid plan | plan exact | constraint | oracle | safety block |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| development | 40/40 | 100% | 100% | 100% | 100% | 해당 없음 |
| local-inference holdout 첫 실행 | 10/10 | 100% | 100% | 100% | 100% | 100% |

development generation latency는 p50 3.04초, p95 3.58초, 최대 4.54초였다.
holdout은 p50 3.26초, p95·최대 4.77초였다.

원본 report SHA-256:

- development:
  `1ab842bc9fc71ac5696eac186605ca209e109e92b2f0471c99a3729c4ffb7bae`
- local-inference holdout 첫 실행:
  `8d3f60ced2c4b7622c9eea7be91336015e139b9e0dcb5b20c0de47a8b8ec8924`

이 50/50은 로컬 LLM 단독 점수가 아니다.

```text
로컬 LLM Structured Output
→ 상품군 인지 lexical canonicalizer
→ registry·Pydantic 계약
→ SQLite oracle
→ 독립 Python verifier
```

또한 이 holdout은 **로컬 모델 호출에는 사용하지 않은 split**이지만, 같은
개발자가 전체 질문과 정답 규칙을 작성했고 결정론적 linker의 기계적 정합성
검사에는 전체 suite가 사용됐다. 따라서 완전한 blind/unbiased 일반화 성능으로
주장하지 않는다. 다음 일반화 측정은 다른 사람이 작성하거나 생성 후 봉인한
표현 변형 세트에서 해야 한다.

## 4. 대표 E2E

질문:

> 미국 주식형 국내 ETF 중 판매 가능하고 거래정지가 아니며 연금 거래 가능한
> 상품을 1개월 수익률 순으로 5개 보여줘.

결과:

- 검증 후보 211개
- 상위 5개: `A0181B0`, `A442580`, `A0176P0`, `A469060`, `A491830`
- 각 결과에 원천 key, Excel 행, source column, raw value, canonical value,
  단위, 기준일, 품질 상태 포함
- Mock response SHA-256:
  `b2791f08dc59419e5e8059c84356be7145f3246368c54a3f590403645521559b`

## 5. 재현 명령

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset domestic_etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset domestic_etp \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 모델 평가는 [로컬 LLM 테스트 런타임](local-llm.md)의 세 가지 opt-in을
추가하고 `--provider local_test`를 사용한다.

## 6. 한계와 다음 단계

- search intent와 한 상품군 DB만 실행한다. 비교·집계·설명·교차 상품군은 아직
  실행하지 않는다.
- 수익률은 제공 snapshot의 과거 성과이며 예측·추천 신호가 아니다.
- 총보수와 기초지수의 낮은 coverage를 결과 없음으로 오해하면 안 된다.
- 오탈자, 구어체, 장문 대화, prompt injection을 충분히 포함하지 않았다.
- 새 blind 표현 변형 세트를 추가한 뒤 국내채권, 공모펀드 순으로 같은
  audit→registry→oracle→verifier 계약을 확장한다.

최종 답변 생성·후검증 결과는 별도
[근거 기반 최종 답변 평가](evaluation-grounded-answers.md)에 기록한다.
