# 공모펀드 원천 데이터 계약

기준 원천: `PRFD01N001_공모펀드마스터_20260711_datarows.xlsx`

기준 스키마: `PRFD01N001_공모펀드마스터_schema.xlsx`

파일 스냅샷일: 2026-07-11

계약 버전: field registry 1.3

## 0. 결론

- 원천 grain은 `itm_no × prfd_attr_cd` 한 행
- 검색·비교 grain은 `itm_no`로 식별되는 펀드 클래스 한 상품
- 정상 원천 95,618행은 논리 상품 11,138개로 정규화
- 상품마다 속성행 4~16개가 존재하며 `prfd_attr_cd` 외 44개 필드는 상품 안에서 모두 동일
- Excel 84,563번째 행 1건만 구조 손상으로 격리
- 공식 공모펀드 기본 검색은 `prvo_pbff_desc = 공모`를 강제
- 사모 15개와 공·사모 구분 결측 8개는 구조적으로 정상이나 기본 검색 범위에서 제외
- `fund_products`, `fund_attributes`, `fund_quarantine` 정규화 SQLite와 manifest 구현 완료
- parameterized Oracle, 독립 Result Verifier, field-level evidence와 안전 렌더러 연결 완료
- 40 development·10 holdout 핵심 평가 세트의 expected Oracle 회귀 50/50
- 로컬 Qwen hybrid parser는 development 40/40, 최초 holdout 9/10
- 공개된 holdout 실패는 family handoff 회귀 수정 후 무모델 replay 50/50
- 실제 HyperCLOVA X HTTP transport와 공식 `/answer` adapter 검증 전까지
  공식 Agent 실행은 비활성화

## 1. 근거와 재현 방법

원천을 표준 스트리밍 감사기로 전수 검사하고 다음 조건을 별도 회귀
expectation으로 고정

- 95,619행 × 45열
- 원천 스키마와 헤더 정확히 일치
- 원천 기본키 `(itm_no, prfd_attr_cd)` 중복 0건
- `itm_no` 형식 오류 1건, Excel source row 84,563
- 정상 `itm_no` 11,138개
- 같은 `itm_no` 안에서 `prfd_attr_cd` 외 필드 충돌 0건
- 상품별 속성행 수와 고유 속성코드 수 불일치 0건

재현 명령:

```bash
conda run -n gaeng3-dev finance-data-audit \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit \
  --dataset fund
```

재현 가능한 분석 흐름과 출력 항목은
[공모펀드 계약 감사 노트북](../notebooks/public-fund-contract-audit.ipynb)에도
보존한다. 현재 필수 Conda 환경에는 Jupyter가 없으므로 CI에서는 위 CLI를
정본 실행 경로로 사용

## 2. Grain 계약

| 계층 | grain | 키 | 행 수 | 사용 목적 |
|---|---|---|---:|---|
| 원천 | 상품 × 속성코드 한 행 | `(itm_no, prfd_attr_cd)` | 95,619 | 원문 보존과 추적 |
| 격리 | 구조 손상 원천행 | `source_row` | 1 | 조사와 재처리 |
| 상품 | 펀드 클래스 한 상품 | `itm_no` | 11,138 | 검색·비교·설명 |
| 속성 | 상품에 연결된 속성코드 | `(itm_no, prfd_attr_cd)` | 95,618 | 코드북 확보 후 의미 확장 |

구현된 정규화 모델:

```text
fund_product
  PK: itm_no
  columns: prfd_attr_cd를 제외한 상품 공통 필드 44개

fund_attribute
  PK: (itm_no, prfd_attr_cd)
  FK: itm_no -> fund_product.itm_no

fund_quarantine
  PK: source_row
  columns: raw payload, reason, source file hash
```

`itm_no`는 원천 상품명에 클래스 정보가 포함될 수 있는 상품 클래스 수준
식별자다. `rptt_ksd_itm_no`를 상위 펀드 그룹 키로 사용할 가능성은 있으나 공식
의미가 확인되지 않아 현재 계약에는 포함하지 않음

상품별 원천행 분포:

| 속성행 수 | 상품 수 |
|---:|---:|
| 4 | 299 |
| 5 | 492 |
| 6 | 1,014 |
| 7 | 1,821 |
| 8 | 1,991 |
| 9 | 1,815 |
| 10 | 1,559 |
| 11 | 1,048 |
| 12 | 655 |
| 13 | 343 |
| 14 | 86 |
| 15 | 13 |
| 16 | 2 |

### 2.1 정규화 SQLite 재현

```bash
conda run -n gaeng3-dev finance-build-etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --dataset fund \
  --output artifacts/normalized/fund.sqlite3
```

실제 원천을 독립 임시 디렉터리에서 두 번 빌드한 결과:

- 원천 95,619행 = 속성 95,618행 + 격리 1행
- 논리 상품 11,138개 = 공모 검색 범위 11,115개 + 범위 제외 23개
- `fund.sqlite3` SHA-256 두 번 일치:
  `99fac786e5be0ec5a7a53e11e1bd3bbccd5b37ab15243ecbf8b864a85b375ca4`
- sidecar manifest SHA-256 두 번 일치:
  `be83a616d033db2328d231499d1f0492323d02bace4f153ad3da4860a0d10bcd`
- SQLite `integrity_check=ok`, foreign-key 위반 0건

manifest schema 1.1은 raw 행 수, 논리 상품 수, 속성 수, 격리 수와 기본 공모
검색 범위 제외 수를 각각 기록

## 3. 검색 범위 계약

구조 검증과 과제 검색 범위를 분리

| 구분 | 상품 수 | 처리 |
|---|---:|---|
| 공모 | 11,115 | 공모펀드 검색 후보 |
| 사모 | 15 | 정상 보존, 공모펀드 기본 검색 제외 |
| 공·사모 결측 | 8 | 정상 보존, 기본 검색 제외 및 `UNKNOWN` |
| 판매중 | 8,445 | `sellable = true` |
| 당사 판매 Y | 10,444 | `company_sellable = true` |
| 공모 + 판매중 + 당사 판매 Y | 8,434 | 세 조건을 모두 요구할 때의 후보 |

기본 공모펀드 도구는 사용자가 말하지 않아도 `public_offering = true`를 잠금
조건으로 추가해야 함. `판매중`과 `당사 판매`는 서로 다른 조건이므로 사용자
질문에 따라 각각 적용

## 4. Field capability 계약

상태 정의:

- `VALID`: 현재 의미와 값 규칙으로 안전하게 사용 가능
- `PARTIAL`: 결측·센티널·기준일 한계를 표시하면 사용 가능
- `UNKNOWN`: 원문 표시만 허용하고 검색·정렬·집계에는 사용 금지
- `UNSUPPORTED`: 원천에 필요한 필드가 없음

### 4.1 검색·정렬에 사용할 필드

| Canonical field | 원천 필드 | coverage | 품질 | 허용 기능 | 핵심 규칙 |
|---|---|---:|---|---|---|
| `product_id` | `itm_no` | 100% | VALID | 검색·표시 | 논리 상품 PK |
| `product_name` | `itm_nm` | 100% | VALID | 검색·정렬·표시 | 식별자는 아님 |
| `short_name` | `itm_abrv_nm` | 100% | VALID | 검색·정렬·표시 | 식별자는 아님 |
| `public_offering` | `prvo_pbff_desc` | 99.9282% | PARTIAL | 검색·표시 | 공모=true, 사모=false, 결측=UNKNOWN |
| `sellable` | `sale_yn` | 100% | VALID | 검색·표시 | 판매중=true, 판매완료=false |
| `company_sellable` | `thco_sale_yn` | 93.7691% | PARTIAL | 검색·표시 | Y=true, N=false, 결측=UNKNOWN, 관측 N은 0개 |
| `trading_currency` | `curr_cd` | 100% | VALID | 검색·표시 | KRW 11,067개, USD 71개 |
| `investment_region` | `fd_ivst_rgn_desc` | 99.9282% | PARTIAL | 검색·표시 | 결측 8개 |
| `fund_geography_scope` | `ovrs_fd_desc` | 99.9282% | PARTIAL | 검색·표시 | 국내·해외·국내외혼합 |
| `fund_management_attribute` | `or_attr_desc` | 93.7691% 의미상 | PARTIAL | 검색·표시 | 코드 `06` 686개는 UNKNOWN |
| `investor_type` | `pers_corp_desc` | 99.9282% | PARTIAL | 검색·표시 | 해당없음·개인·법인 |
| `currency_hedged` | `exchdg_yn` | 62.1207% | PARTIAL | 검색·표시 | Y/N 외 결측 4,219개 |
| `risk_level` | `zrin_fd_ivst_risk_gcd` | 76.8989% | PARTIAL | 검색·표시 | 코드 1~6으로 정규화 |
| `aum` | `fd_nast_suma` | 83.4082% | PARTIAL | 검색·정렬·집계·표시 | 0인 298개는 UNKNOWN, 동일 통화만 비교 |
| `one_week_return_pct` | `fd_wk1_ern_r` | 68.2349% | PARTIAL | 검색·정렬·집계·표시 | -60.92~26.59 |
| `one_month_return_pct` | `fd_mm1_ern_r` | 67.9655% | PARTIAL | 검색·정렬·집계·표시 | -63.11~15.65 |
| `three_month_return_pct` | `fd_mm3_ern_r` | 67.3101% | PARTIAL | 검색·정렬·집계·표시 | -88.46~144.21 |
| `six_month_return_pct` | `fd_mm6_ern_r` | 65.9005% | PARTIAL | 검색·정렬·집계·표시 | -84.16~369.68 |

수익률은 원천 퍼센트포인트를 그대로 보존. 공모펀드에는 필드별 갱신일이 없어
파일 스냅샷일 2026-07-11만 함께 표시

### 4.2 표시 전용 필드

| Canonical field | 원천 필드 | coverage | 제한 이유 |
|---|---|---:|---|
| `base_index` | `bmrk_nm` | 100% | placeholder와 의미 품질 미검증 |
| `one_year_return_pct` | `fd_yr1_ern_r` | 63.0005% | 500% 초과 15개 |
| `eighteen_month_return_pct` | `fd_mm18_ern_r` | 61.7615% | -100% 미만 1개, 500% 초과 20개 |
| `two_year_return_pct` | `fd_yr2_ern_r` | 57.1018% | 최솟값 -3675.44, 500% 초과 14개 |
| `three_year_return_pct` | `fd_yr3_ern_r` | 54.7854% | 최솟값 -4381.56, 500% 초과 21개 |
| `five_year_return_pct` | `fd_yr5_ern_r` | 50.0988% | 최솟값 -4254.75, 500% 초과 15개 |
| `dynamic_as_of` | 파일 스냅샷 | 100% | 개별 지표 기준일이 아님 |

표시 전용 수익률은 원문과 경고를 보여주는 용도로만 보존. 공식 산식이나
이상치 정책을 확인하기 전에는 조건 검색·순위·평균에 사용 금지

### 4.3 원문 보존만 하는 필드

| 원천 필드 | 제한 이유 |
|---|---|
| `prfd_attr_cd` | 다중 속성 구조는 확인했으나 공식 코드북 없음 |
| `fd_estb_ctry_cd` | `000`, `410`의 업무 의미와 결측 규칙 불명확 |
| `rptt_ksd_itm_no` | 클래스 상위 그룹 키인지 공식 의미 미확인 |
| `or_co_xtn_itt_cd`, `trusc_xtn_itt_cd` | 기관 코드만 있고 표시명이 없음 |
| `hdge_fd_yn` | 전 값 0이며 환헤지 필드가 아니라 헤지펀드 여부 |
| `ofsfd_yn` | 전 값 0으로 분류력이 없음 |
| `itm_eabrv_nm` | 18개 상품만 존재해 검색명으로 부적합 |

### 4.4 지원하지 않는 질의

| 사용자 조건 | 판단 |
|---|---|
| 총보수·판매보수·수수료가 낮은 공모펀드 | 원천에 비용 필드가 없어 UNSUPPORTED |
| 운용사 이름으로 검색 | 기관 코드만 있어 이름 검색 UNSUPPORTED |
| 정확한 국가 코드 검색 | 공식 코드북 확인 전 UNSUPPORTED |
| 대표 펀드 단위로 클래스 자동 합산 | 상위 그룹 키 의미 확인 전 UNSUPPORTED |
| 장기 수익률 순위·평균 | 이상치 정책 확인 전 UNSUPPORTED |
| 오늘 기준 최신 수익률 | 필드별 기준일이 없어 UNSUPPORTED |

지원하지 않는 조건은 무시하지 말고 `unsupported_conditions` 또는 사용자
역질문으로 명시

## 5. 품질 규칙

| ID | 규칙 | 실패 시 처리 |
|---|---|---|
| FUND-GRAIN-001 | 원천 키는 `(itm_no, prfd_attr_cd)`이며 중복 0건 | 적재 중단 |
| FUND-GRAIN-002 | 논리 상품 키는 `itm_no`이며 정상 상품 11,138개 | 적재 중단 |
| FUND-GRAIN-003 | 한 상품의 `prfd_attr_cd` 외 44개 필드는 동일 | 해당 상품 격리 |
| FUND-GRAIN-004 | 상품별 속성행 수와 고유 속성코드 수가 동일 | 해당 상품 격리 |
| FUND-QUAR-001 | Excel 84,563번째 행 1건은 구조 손상 | 원문 보존 후 검색 제외 |
| FUND-SCOPE-001 | 공모펀드 기본 검색은 `public_offering=true` | 잠금 조건 누락 시 verifier 실패 |
| FUND-NULL-001 | 결측·빈 문자열·문자열 NULL은 UNKNOWN | 임의 보간 금지 |
| FUND-SENT-001 | `fd_nast_suma=0`은 UNKNOWN | 필터·정렬·집계 제외 |
| FUND-SENT-002 | `or_attr_desc=06`은 UNKNOWN | 펀드 유형 필터 제외 |
| FUND-BOOL-001 | `thco_sale_yn` 결측은 false가 아님 | UNKNOWN 유지 |
| FUND-RISK-001 | 위험등급은 코드 1~6으로 정규화 | 이름 띄어쓰기 변형 무시 |
| FUND-RET-001 | 1주·1·3·6개월만 실행 허용 | 결측은 UNKNOWN |
| FUND-RET-002 | 18개월·1·2·3·5년은 표시 전용 | 조건·순위·집계 거절 |
| FUND-ASOF-001 | 동적 수치 기준일은 파일 스냅샷 수준 | 답변에 한계 경고 |
| FUND-CUR-001 | AUM 직접 비교는 같은 통화 안에서만 수행 | 통화 혼합 순위 거절 |

## 6. 손상 행 판정

격리 대상은 84,563개 행이 아니라 Excel source row **84,563 한 건**

관측값:

- `itm_no = "\""`
- `itm_nm = "공모"`
- `prfd_attr_cd = "해외"`
- 의미 있는 셀 15개
- 이후 필드가 왼쪽으로 이동한 형태

키 형식과 열 의미가 동시에 깨졌으므로 부분 복원하지 않고 원문 전체를 격리

## 7. 아직 필요한 금융 도메인 확인

다음 항목은 코드 추측 대신 공식 코드북이나 금융 도메인 검토가 필요

1. `prfd_attr_cd` 각 코드의 공식 명칭과 한 상품에 여러 코드가 붙는 이유
2. `or_attr_desc = 06`의 실제 의미
3. `fd_nast_suma = 0`이 실제 0인지 미제공 sentinel인지
4. 장기 수익률의 공식 산식과 -100% 미만 값의 발생 원인
5. `rptt_ksd_itm_no`가 클래스 상위 펀드 그룹 식별자인지
6. 기관 코드를 운용사·수탁사 이름으로 변환할 공식 매핑 제공 여부
7. `bmrk_nm`의 placeholder 값과 유효 벤치마크 구분 규칙

확인 결과가 나오기 전까지 현재 fail-closed 규칙을 유지

## 8. 대표 Oracle 회귀

대표 질문:

> 당사에서 판매 중인 해외 주식형 공모펀드 중 3개월 수익률이 높은 상품 5개

잠금 조건:

- `public_offering = true`: 사용자가 생략해도 시스템이 반드시 요구
- `sellable = true`
- `company_sellable = true`
- `fund_geography_scope = 해외`
- `fund_management_attribute = 주식형`
- `three_month_return_pct` 내림차순, UNKNOWN은 마지막

실제 `artifacts/normalized/fund.sqlite3` 실행 결과:

- 독립 SQL Oracle과 Python Result Verifier 후보 수 일치: 1,811개
- 상위 상품 ID와 3개월 수익률:
  `KR5114450606` 79.82%, `KR5114450609` 79.80%,
  `KR5114450608` 79.78%, `KR5114450607` 79.74%,
  `KR5114450603` 79.66%
- 각 상품에 constraint·ranking·projection을 합친 13개 field evidence 생성
- 공모 범위, 클래스 grain, 과거 수익률, 파일 수준 기준일, 코드 06 제외 경고 생성

상위 5개는 같은 대표 펀드의 서로 다른 클래스다. 현재 공식적으로 확인된
`itm_no` 클래스 grain을 그대로 적용한 결과이며, `rptt_ksd_itm_no` 의미를
확인하기 전에는 임의로 합치거나 대표 클래스만 선택하지 않음

## 9. 구현 승인 조건

공모펀드 실행을 `execution_enabled: true`로 전환하려면 다음을 모두 충족해야 함

- [x] `fund_products`, `fund_attributes`, `fund_quarantine` 재현 가능 빌드
- [x] 기본 공모 범위 잠금 조건 구현
- [x] field registry 기반 parameterized oracle 구현
- [x] AUM 0, 코드 06, 장기 수익률 제한 oracle 회귀 테스트
- [x] result verifier와 field-level evidence DTO 연결
- [x] 핵심 평가 질문과 blind 표현 변형의 expected Oracle 회귀 세트 통과
- [ ] HCX schema에 fund를 노출한 뒤 서버 QueryPlan 계약 테스트 통과
