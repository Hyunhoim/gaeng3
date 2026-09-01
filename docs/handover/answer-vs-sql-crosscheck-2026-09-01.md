# 답변과 독립 SQL 대조

작성일: 2026-09-01

## 1. 왜 했는가

지금까지 검증한 것은 **계획이 맞는가**였다. 질문이 올바른 field와 방향으로
컴파일되는지 확인했을 뿐, 그 계획이 **실제로 맞는 상품을 돌려주는지**는 보지
않았다. 정확도 40점이 채점하는 것은 후자다.

승인 DB에 직접 SQL을 던져 Agent의 TOP-K와 대조했다. 정렬 컬럼과 순서는 planner를
거치지 않고 직접 적었다. 필터 해석(예: `국내 ETF` → `product_type='ETF'`)은 질문
해석의 일부이므로 plan에서 읽어 반영했다.

DB는 실행 중인 컨테이너의 `/data`에서 복사했다. 저장소의
`finance_agent/artifacts/normalized/`는 스키마가 낡아 현재 코드로 열리지 않는다.

## 2. 결과

18개 질의 기준이다.

| 상품군 | 착수 전 | 수정 후 |
| --- | --- | --- |
| 국내 ETF | 6/6 | 6/6 |
| 공모펀드 | 4/4 | 4/4 |
| 해외 ETF | 1/2 | 1/2 (동점 순서) |
| 국내채권 | **1/6** | **2/6 + 동점 4** |

**ETP와 공모펀드는 처음부터 문제가 없었다.** 결함은 전부 채권에 있었다.

수정 후 전체는 정확일치 13 · 동점 순서 차이 5 · **불일치 0**이다. 남은 5건은
값이 같은 구간에서 순서만 다르거나, 대조 SQL이 한 종목의 여러 행 중 최댓값을
쓰는 데서 오는 차이다.

## 3. 채권 결함 1: 만기 기준일이 어긋났다 (수정함)

### 확인한 사실

```
remaining_days   = maturity_date − source_snapshot_date   (21,877건 전부 일치)
만기 종료 판정   = maturity_date > dynamic_as_of          (3일 이른 기준)
```

`remaining_days`는 스냅샷 날짜(2026-08-24) 기준으로 저장되는데, 종료된 채권을
걸러내는 조건은 동적 기준일(2026-08-21)을 썼다. 그 사이에 만기가 온 채권
**14건**이 필터를 통과했고, `remaining_days`가 음수인 채로 남았다.

`잔존일수 짧은 순으로 5개`의 1~5위가 전부 이 행들이었다. 이미 만기된 채권을
"잔존만기가 가장 짧은 상품"으로 답하고 있었다.

### 조치

종료 판정을 `remaining_days`와 같은 기준으로 맞췄다.

```sql
maturity_date IS NULL OR source_snapshot_date IS NULL
  OR maturity_date >= source_snapshot_date
```

`source_snapshot_date >= dynamic_as_of`가 전 행에서 성립하므로 이 조건은 기존
조건을 포함한다. 즉 새로 통과하는 행은 없고 14건만 제외된다. 만기 당일
(`remaining_days = 0`)은 아직 종료가 아니므로 남긴다.

수정 후 `잔존일수 짧은 순` 상위 5건은 모두 `remaining_days = 0`이다.

기존 테스트가 SQL 문자열 `maturity_date > dynamic_as_of`를 그대로 검사하고
있어 함께 갱신했다. 왜 기준을 바꿨는지 주석으로 남겼다.

## 4. 채권 결함 2: 대표 행 선택 (수정함)

### 확인한 사실

채권은 한 종목이 장내·장외·회차별로 여러 행을 갖는다. Oracle은 종목당 한 행으로
접는데, 그 기준이 **`MIN(source_row)`** 다.

```sql
source_row IN (SELECT MIN(source_row) FROM bond_products
               WHERE is_quarantined = 0 GROUP BY ticker)
```

이 선택은 **무엇을 묻는지와 무관하게** 이뤄진다. 그래서 대표 행에 그 값이
없으면 다른 행에 값이 있어도 종목 전체가 빠진다.

```
KR6000036DA8  장내 행: 매수수익률 NULL   ← 대표로 선택됨 → 제외
              장외 행: 매수수익률 5.29%  ← 실제 1위인데 보이지 않음
```

`매수수익률 높은 순` 1위로 4.85%짜리가 나온다. 실제 1위 5.29%를 포함해 위쪽
네 종목이 통째로 빠진다.

### 영향 범위

전체 20,497개 종목 중 대표 행에 값이 없어 빠지는 종목 수다.

| field | 빠지는 종목 |
| --- | ---: |
| 매수수익률 | 228 |
| 세후수익률 | 228 |
| 듀레이션 | 2 |
| 표면금리·민평수익률 | 0 |

비율로는 1.1%지만, 문제는 **빠지는 것이 상위권**이라는 점이다. TOP-K 답변이
직접 틀린다.

### 제안하는 수정

대표 행을 고를 때 정렬 대상 field에 쓸 수 있는 값이 있는 행을 먼저 고른다.

```sql
source_row IN (
  SELECT source_row FROM (
    SELECT source_row, ROW_NUMBER() OVER (
      PARTITION BY ticker
      ORDER BY CASE WHEN <col> IS NOT NULL AND <col> <> 0 THEN 0 ELSE 1 END,
               source_row
    ) rn FROM bond_products WHERE is_quarantined = 0
  ) WHERE rn = 1
)
```

### 조치

`logical_product_scope()`가 `usable_value_sql`을 받아, 정렬 대상 값이 있는 행을
먼저 고르고 `source_row`로 동점을 끊는다. 무엇을 쓸 수 있는 값으로 볼지는
Oracle이 그 field에 붙이는 WHERE 조건과 같게 맞췄다(NULL 아님, 품질 VALID·PARTIAL,
`remaining_days`가 아니면 0 아님).

검색 SQL과 검증 SQL 두 곳에 같은 범위를 적용했다. 한쪽만 바꾸면 Verifier가
다른 행 집합에서 재계산해 불일치로 판정한다.

정렬 기준이 없는 질문에는 고를 근거가 없으므로 기존 `MIN(source_row)`가 그대로
쓰인다.

수정 후 `매수수익률 높은 순` 1위가 실제 1위(5.29%)로 바뀌었다.

### 남은 도메인 확인 사항

한 종목의 장내·장외 행이 **둘 다 값을 가진** 경우, 지금은 `source_row`가 작은
쪽을 쓴다. 두 시장의 호가는 실제로 다르므로 어느 쪽을 종목 대표값으로 제시할지는
도메인 판단이 남아 있다.

- 담당: 박재모
- 물어볼 것: 장내와 장외 값이 모두 있을 때 어느 쪽을 그 종목의 대표값으로
  제시해야 하는가

## 5. 남은 동점 순서 차이 4건

값이 같은 구간에서 순서만 다르다. 우리는 `ticker ASC`로 끊고, 대조 SQL은
값 순서만 본다. 동점 절단 규칙은 박재모에게 확인 대기 중인 항목이다.

## 6. 재현

대조 스크립트는 `finance_agent/scripts/check-ranking-against-sql.py`가 원본이고,
이번에는 백엔드 없이 라이브러리 수준에서 더 많은 케이스로 돌렸다. 컨테이너
`/data`의 DB가 필요하다.
