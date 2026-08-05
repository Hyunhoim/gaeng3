# 네 상품군 자연어 COMPARE 공개 회귀

마지막 갱신: 2026-07-30

## 1. 목적

사용자의 자연어 비교 질문이 상품 식별, QueryPlan, Oracle, 검증된 비교값,
결정론적 답변과 Backend DTO까지 같은 계약으로 이어지는지 반복 검사한다.

이 평가는 AI 담당자가 작성하고 결과를 확인한 공개 회귀다. 구현 배선과 안전
경계의 퇴행을 찾는 용도이며 독립 blind 일반화 성능이 아니다.

## 2. 평가 구성

신규 `product-compare-core-30`은 기존 공모펀드 비교 평가와 중복되지 않게
세 상품군을 담당한다.

| 상품군 | 실행 | 안전 차단 | 합계 |
| --- | ---: | ---: | ---: |
| 해외 ETF·ETN | 6 | 4 | 10 |
| 국내 ETF·ETN | 6 | 4 | 10 |
| 국내채권 | 6 | 4 | 10 |
| 합계 | 18 | 12 | 30 |

기존 `fund-compare-e2e-core-24`의 공모펀드 24문항과 합치면 네 상품군 자연어
COMPARE 공개 회귀는 총 54문항이다.

실행 문항은 다음 상태를 포함한다.

- 숫자 차이와 요청 순서
- 문자열·등급·boolean의 값 대조
- 결측으로 인한 `unavailable`
- 채권 동적 값의 `stale_input`
- 정확한 상품번호·티커·ISIN·따옴표 상품명

안전 차단 문항은 다음 표현을 포함한다.

- 동일 상품 두 번 지정
- 제공 데이터에 없는 식별자
- 해외 ETP 수익률·채권 만기수익률처럼 미지원인 필드
- 기간이 없는 국내 ETP 수익률
- 제외·빼고·포함처럼 비교 대상 역할을 바꾸는 표현

## 3. 동결 계약

- suite:
  `packages/finance_agent_core/src/finance_agent_core/evaluation/suites/product_compare_core_30.json`
- suite SHA-256:
  `7eec27471f2dbd218b0ed056f03b02ef69aed6b283055d72d98ff7d423e411ee`
- commitment:
  `evaluation/protocols/product-compare-core-30.commitment.json`
- baseline:
  `evaluation/baselines/product-compare-v1.json`

suite는 해외 ETP·국내 ETP·국내채권 SQLite와 manifest SHA-256을 각각
고정한다. 데이터가 달라지면 질문을 실행하기 전에 실패한다.

## 4. 검증 항목

각 문항에서 다음을 동시에 확인한다.

1. 실행·역질문 상태
2. COMPARE QueryPlan과 단일 상품군
3. 두 상품의 요청 순서
4. 비교 필드 순서
5. field status와 `두 번째-첫 번째` 차이
6. 빈 결과가 아닌 실행 문항의 후보 수
7. 차단 문항의 상품·근거·실행 결과 미노출
8. 결정론적 답변 필수 문구
9. Backend `comparisons`와 `comparison_field` citation 계약

## 5. 공개 회귀와 성능 기준선

| 지표 | 결과 |
| --- | ---: |
| 전체 | 30/30 |
| strict accuracy | 1.0 |
| QueryPlan 정확도 | 1.0 |
| 상품 순서 정확도 | 1.0 |
| 필드·상태·차이 정확도 | 1.0 |
| Backend 계약 통과율 | 1.0 |
| 차단 결과 억제율 | 1.0 |
| 답변 계약 통과율 | 1.0 |

전체 report SHA-256은
`901197bb41a239ad8391374d5c602b3c0c3eca7a0f7ec75d451d3fa398db8b3a`다.

같은 개발 장비에서 3 workers로 실행한 방향성 비교는 다음과 같다.

| 단계 | p50 | p95 | 최대 |
| --- | ---: | ---: | ---: |
| 캐시 전 | 1,464.120ms | 25,091.920ms | 28,334.202ms |
| 전체 레코드 캐시 실험 | 103.334ms | 3,835.903ms | 4,511.354ms |
| 최종 compact identity cache | 65.522ms | 954.670ms | 1,001.297ms |

전체 레코드 캐시는 지연을 줄였지만 국내채권 42,394건의 Pydantic 객체가
상주하면서 통제 실험의 최대 RSS가 약 `650,304KiB` 증가해 최종 설계에서
제외했다. 최종 비교 경로는 상품번호·이름·티커·ISIN 등 정확 식별에 필요한
최소 열만 캐시하며, 원본 DB 파일의 inode·크기·수정시각이 달라지면 자동
무효화한다.

최종 30문항 실행에서 identity cache는 miss/load 3회, hit 24회,
무효화·축출 0회였고 49,774개의 compact identity를 보관했다. 전체 레코드
캐시는 한 번도 적재하지 않았다. 별도 국내채권 고정 질문 1회 cold와 5회
warm 측정은 cold `527.237ms`, warm `64.668~104.046ms`, 최대 RSS 증가
`56,548KiB`였으며 여섯 응답의 비교 결과 fingerprint가 모두 같았다.

이 지연과 메모리 수치는 한 장비의 단일 실행값이므로 Backend SLO로 사용하지
않는다. 성능 회귀의 방향과 전체 레코드 재적재 여부를 확인하는 개발 기준선이다.

## 6. 재현

`finance_agent/`에서 실행한다.

```bash
conda run -n gaeng3-dev \
  python -m finance_agent_core.evaluation.product_comparison_cli \
  --workers 3 \
  --require-perfect
```

설치된 console script에서는 다음을 사용할 수 있다.

```bash
conda run -n gaeng3-dev \
  finance-evaluate-product-compare \
  --workers 3 \
  --require-perfect
```

전체 report는
`artifacts/evaluation/product-compare-deterministic-all.json`에 생성되며 Git에
포함하지 않는다.

## 7. 해석 제한과 다음 단계

- 정확 일치 식별만 평가하며 오탈자·부분 이름을 추측하지 않음
- 같은 상품군의 두 상품만 비교
- 상품군 간 비교, 세 상품 이상, 환율 환산, 우열·추천은 계속 미지원
- 이 문서의 캐시 기준선은 COMPARE 경로만 다루며 SEARCH·AGGREGATE 성능은
  별도 기준선이 필요
- LLM을 호출하지 않아 생성 품질이나 HyperCLOVA X 성능을 측정하지 않음
- 최종 일반화 성능은 금융 도메인 담당자가 별도로 작성·봉인한 external blind의
  최초 1회 실행으로 측정
