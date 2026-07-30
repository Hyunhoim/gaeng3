# SEARCH·AGGREGATE 성능 기준선

마지막 갱신: 2026-07-30

이 문서는 네 상품군의 결정론적 SEARCH·AGGREGATE 경로가 같은 결과를 유지하면서
과도한 정규화 레코드 적재를 피하는지 확인하는 개발 성능 기준선이다

## 1. 평가 범위

- 상품군마다 대표 SEARCH 1문항과 AGGREGATE 1문항, 총 8문항 사용
- 각 문항을 새 Python 프로세스에서 한 번 실행
- wall-clock 지연과 Linux `ru_maxrss` 증가량 기록
- 후보 수, SEARCH 상위 상품 ID, AGGREGATE 그룹·값·유효값·결측 수를 고정 지문과 비교
- DB·manifest·원천 파일 SHA-256이 suite와 다르면 실행 전 실패
- 공개 회귀이므로 독립 blind·일반화 성능·운영 SLO로 주장하지 않음

Suite 정본:
[`search_aggregate_performance_8.json`](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/search_aggregate_performance_8.json)

집계 baseline:
[`search-aggregate-performance-v1.json`](../evaluation/baselines/search-aggregate-performance-v1.json)

## 2. 경량 verifier 구조

기존 경로는 SQL 결과를 검증하기 위해 상품군의 모든 정규화 Pydantic 레코드와
원천값 사전을 메모리에 올렸다. 현재 기본 경로는 QueryPlan에 실제로 필요한 열만
별도 SQL projection으로 읽는다

```text
QueryPlan
├─ SEARCH Oracle: 상위 N개 전체 evidence 행
├─ AGGREGATE Oracle: 집계에 필요한 최소 열
└─ Verifier projection: 조건·정렬·그룹·집계 필드와 품질·기준일만
   → 독립 Python 재계산
   → 결과 지문 일치 시에만 evidence·답변 생성
```

- SEARCH 검증 projection: 상품 ID, locked 조건, 정렬 필드
- AGGREGATE 검증 projection: 상품 ID, locked 조건, 그룹 필드, 집계 필드
- 공통 메타데이터: 격리 여부, 파일 스냅샷일, 정적·동적 기준일, 행 수준 품질
- 원천값 사전과 답변 표시용 나머지 필드는 verifier universe에서 제외
- Oracle과 verifier는 같은 canonical field 정의를 쓰지만 별도 SQL 조회와
  별도 Python 재계산을 유지
- 네 상품군마다 전체 정규화 레코드와 projected record의 canonical 값·품질
  동등성 테스트 적용
- 결과 순서 또는 집계값을 변조하면 projected verifier도 답변 전에 실패

명시적으로 `RecordSnapshotCache`를 주입한 테스트·호출자는 기존 전체 레코드
검증 경로를 사용할 수 있다. 기본 Agent 경로에서는 이 cache를 활성화하지 않는다

## 3. 동결 결과

최종 report의 8문항은 모두 후보 수와 결과 지문이 일치

| 구분 | 문항 수 | 통과 | 평균 지연 | 최대 추가 RSS |
| --- | ---: | ---: | ---: | ---: |
| SEARCH | 4 | 4 | 345.635ms | 51,000KiB |
| AGGREGATE | 4 | 4 | 580.937ms | 45,620KiB |
| 전체 | 8 | 8 | p50 308.749ms | 최대 51,000KiB |

상품군별 최종 결과:

| 상품군 | SEARCH 지연·메모리 | AGGREGATE 지연·메모리 |
| --- | --- | --- |
| 해외 ETP | 227.792ms · 12,588KiB | 190.967ms · 14,520KiB |
| 국내 ETP | 86.010ms · 5,868KiB | 308.749ms · 7,036KiB |
| 국내채권 | 535.950ms · 51,000KiB | 493.161ms · 45,620KiB |
| 공모펀드 | 532.788ms · 16,440KiB | 1,330.871ms · 22,240KiB |

같은 장비에서 변경 전 전체 정규화 레코드 verifier와 비교한 주요 메모리 변화:

| 경로 | 변경 전 | projected verifier | 감소 |
| --- | ---: | ---: | ---: |
| 국내채권 SEARCH | 651,072KiB | 51,000KiB | 약 92.2% |
| 국내채권 AGGREGATE | 651,072KiB | 45,620KiB | 약 93.0% |
| 공모펀드 SEARCH | 274,368KiB | 16,440KiB | 약 94.0% |
| 공모펀드 AGGREGATE | 464,064KiB | 22,240KiB | 약 95.2% |

모든 변경 전·후 수치와 8/8 결과 지문은 baseline과 로컬 전체 report에 보존

## 4. 재현

`finance_agent/`에서 실행:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.search_aggregate_benchmark_cli \
  --require-perfect \
  --output artifacts/evaluation/search-aggregate-performance-v1.json
```

설치된 console script는 `finance-benchmark-search-aggregate`

## 5. 해석 한계

- OS page cache와 장비 부하에 따라 지연·RSS가 달라질 수 있음
- 각 문항 1회 측정이므로 통계적 부하 시험이 아니라 구현 전후 방향 확인용
- 프로세스 시작 시간, API·네트워크, LLM 생성 시간은 측정하지 않음
- 국내 ETP SEARCH는 현재 공통 자연어 compiler가 처리하지 않는 복합 표현이어서
  동결 `DomesticMockProvider` QueryPlan을 사용
- 성능 수치가 바뀌어도 결과 지문 8/8과 전체 회귀가 먼저 통과해야 기준선 갱신 가능
