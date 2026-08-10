# 금융상품 데이터 감사 기준

상태: 현재 정본
기준일: 2026-07-29
원천 스냅샷: 파일명 기준 2026-07-11

이 문서는 제공된 4종 datarows의 구조와 품질을 구현 계약으로 정리한다. 상세 계산 산출물은 [GPT Pro 감사 번들](research/2026-07-28-gpt-pro/README.md)에 보존되어 있다.

## 0. 재현 가능한 구현

현재 감사 정본은 [finance_agent_core 감사기](../packages/finance_agent_core/README.md)로 재현할 수 있다.
감사 결과에서 실행 가능 필드로 승격한 범위는 [Field Registry와 QueryPlan 계약](contracts.md)에 동결한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/finance-data-audit \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit
```

2026-07-29 검증 결과:

- 4종 합계 145,393행과 상품군별 열 수 재현
- 핵심 구조·손상 행·sentinel·product-grain coverage expectation 65/65 통과
- GPT Pro source manifest의 8개 XLSX SHA-256과 모두 일치
- 동일 입력을 연속 두 번 실행한 5개 JSON 산출물의 SHA-256이 모두 일치
- 원천 경로·파일명 suffix·`lxml`에 의존하지 않음

2026-07-29에는 국내채권 정규화 DB에서 날짜 sentinel과 동적 기준일을 다시
검산해 발행일·만기일 coverage와 잔존일수 재계산 계약을 갱신했다.

생성 결과는 `artifacts/data-audit/`에 기록하고 Git에서 제외한다.

### 승인된 대회 데이터 release 경계

평가·운영 환경은 현재 디렉터리에 놓인 파일을 새 정본으로 신뢰하지 않는다.
패키지의
[`approved_dataset_manifest.json`](../packages/finance_agent_core/src/finance_agent_core/config/approved_dataset_manifest.json)에
다음 값을 release 단위로 고정한다.

- 네 datarows와 네 schema workbook의 파일 크기와 SHA-256
- source ID, 원천 행 수, 검색 가능·격리·논리 상품 수와 2026-07-11 기준일
- Field Registry schema version
- 승인된 normalizer가 생성한 네 SQLite의 파일 크기와 SHA-256

Docker `data-init`은 원천 workbook을 열기 전에 data·schema hash를 모두 검사하고,
정규화 후 SQLite manifest·무결성·파일 hash를 다시 검사한다. 하나라도 다르면 기존
파일을 승인된 것으로 간주하지 않으며, 재구축 결과도 승인 hash와 다르면 배포를
중단한다. `APP_ENV=evaluation|production` Backend도 Agent를 만들기 전에 네 DB를
같은 release와 대조한다. `/health`는 경로나 hash를 공개하지 않고 승인 실패
상품군만 `unavailable`로 표시한다.

원천 수정, registry/normalizer 변경 또는 SQLite 생성 방식 변경은 자동 승인하지
않는다. 네 workbook 재감사와 독립 재구축을 마친 뒤 새 versioned release로 manifest를
명시적으로 갱신해야 한다.

해외 ETP 정규화 적재도 같은 원천에서 재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output artifacts/normalized/overseas_etp.sqlite3
```

2026-07-28 결과는 전체 5,646행, 검색 가능 5,636행, sparse 격리 10행이다.
서로 다른 두 출력 경로에서 새로 빌드한 SQLite와 manifest가 byte 단위로
일치했다. SQLite SHA-256은
`eee9009ca741713a9a61e498cd5ed8366836d754c7d0c2dbd74ed7e456a2ebbe`다.

## 1. 감사 대상과 논리 grain

| 상품군 | 원천 행 | 논리 grain | 핵심 주의점 |
| --- | ---: | --- | --- |
| 국내채권 | 42,394 | `PD_NO`당 1행 | 조회 가능 전체와 실제 매수 가능 상품을 구분 |
| 국내 ETP | 1,734 | 종목당 1행 | ETF 1,202건·ETN 532건을 명시적으로 구분 |
| 해외 ETP | 5,646 | 종목당 1행 | ETF 5,587건·ETN 59건, sparse 10행 |
| 공모펀드 | 95,619 | `itm_no`당 상품 1개 + 속성 N개 | 정규화 후 논리 상품 11,138개 |
| 합계 | 145,393 | 상품군별 상이 | 원천 행 수를 상품 수로 사용하지 않음 |

원천 파일은 [Raw 데이터 디렉터리](<../../../../2. Data/1. Raw/1.금융상품/>)에서 읽고 수정하지 않는다.

## 2. 상품군별 판정

### 국내채권

- `PD_NO`는 유일하다.
- `BUY_YIELD`와 `BUYABLE_QUANTITY`가 채워진 행은 881개지만, 양수 매수 가능 수량을 가진 원천 행은 325개다.
- 여기에 스냅샷 기준 만기 조건을 적용하면 후보는 254개다.
- 발행일은 결측·0 날짜 sentinel을 제외한 42,055행(99.2004%), 만기일은
  42,075행(99.2475%)이 유효하다.
- 원천 `REMAINING_DAYS`는 동적 기준일 2026-02-24를 사용해 스냅샷과
  137일 어긋난다. 잔존일수는 유효한 만기일과 2026-07-11의 차이로 재계산한다.
- 전체 채권 검색, 기준일 현재 만기 전 채권, 실제 매수 가능 채권을 서로 다른 상태로 모델링한다.
- 평균 연 세전 수익률 계열이 전부 0인 필드는 유효한 비교 지표로 사용하지 않는다.
- 신용등급 결측을 최저등급·무등급으로 임의 치환하지 않는다.
- 매수수량·매수수익률·세후수익률·듀레이션의 현재 매수 가능 254행은 모두
  2026-02-24 기준이므로 `PARTIAL`로 보존하고 137일 stale 경고와 필드 기준일을
  노출한다.

### 국내 ETP

- ETF와 ETN은 `pd_grp_no` 등 명시적 코드로 분리한다.
- 원천 Excel 1,155행의 컬럼 이동 형태 손상 레코드는 격리한다.
- 총보수 값은 217행에만 있어 전체 상품의 보수 비교로 일반화하지 않는다.
- 분배주기는 비어 있고 배당수익률·추적오차 등 일부 상수 0 필드는 유효한 검색축에서 제외한다.
- 자산군·지역·위험등급은 값과 코드 의미를 검증한 뒤 검색축으로 사용할 수 있다.

### 해외 ETP

- 운용사·전략·자산군·지역·AUM·총보수의 coverage가 높아 첫 vertical slice에 사용한다.
- 10개 sparse 행은 핵심 식별·검색 필드 누락 여부를 검사해 격리 또는 제한 노출한다.
- 총보수 필드는 채워져 있지만 0인 값이 363개다. 0이 실제 무보수인지 sentinel인지 공식 확인 전에는 `UNKNOWN` 품질로 관리한다.
- 첫 vertical slice의 0.20% 이하 후보는 원천 기준 480개이며, 이 중 보수 0인 40개를 제외한 비영(非零) 확인 후보는 440개다.
- 1일 수익률은 값이 있는 5,388행이 모두 0이므로 비교·정렬에 사용하지 않는다.
- ISIN은 유일키가 아니므로 원천 종목 key를 별도로 유지한다.

### 공모펀드

- 95,619개 원천 행은 `상품 × prfd_attr_cd` 구조이며, 상품 수로 세지 않는다.
- `itm_no` 기준 논리 상품은 11,138개다.
- 상품 기본 테이블과 다중 속성 테이블을 분리하고, 동일 상품의 반복 노출을 막는다.
- 상품별 속성행은 4~16개이며 `prfd_attr_cd` 외 44개 필드는 같은
  `itm_no` 안에서 모두 동일하다.
- 원천 Excel **84,563번째 행 1건**의 컬럼 이동 형태 손상 레코드를 격리한다.
- raw-row coverage는 반복 속성 수에 의해 왜곡되므로 모든 검색 가능성 판단은 product grain에서 계산한다.
- 정상 상품 중 공모는 11,115개, 사모는 15개, 공·사모 구분 결측은 8개다.
  공모펀드 기본 검색에는 공모만 포함하고 나머지는 구조 격리가 아니라 범위
  제외로 관리한다.
- `fd_nast_suma=0`인 298개와 `or_attr_desc=06`인 686개는 공식 의미 확인 전
  `UNKNOWN`으로 처리한다.
- 1주·1·3·6개월 수익률만 조건·정렬·집계에 사용한다. 18개월·1·2·3·5년은
  비정상 범위가 있어 공식 산식 확인 전 표시 전용이다.
- 제공 데이터에 펀드 보수가 없으므로 보수 조건을 지원하지 않는다.

상세 grain·field capability·품질 규칙은
[공모펀드 원천 데이터 계약](public-fund-contract.md)을 따른다.

공모펀드 product-grain 주요 coverage:

| 지표 | Coverage |
| --- | ---: |
| AUM | 83.4082% |
| 위험등급 | 76.8989% |
| 1주 수익률 | 68.2349% |
| 1개월 수익률 | 67.9655% |
| 3개월 수익률 | 67.3101% |
| 6개월 수익률 | 65.9005% |
| 1년 수익률 | 63.0005% |
| 18개월 수익률 | 61.7615% |
| 2년 수익률 | 57.1018% |
| 3년 수익률 | 54.7854% |
| 5년 수익률 | 50.0988% |

## 3. 공통 품질 상태

각 필드는 최소한 다음 상태 중 하나를 갖는다.

- `VALID`: 의미·단위·기준일이 확인되어 필터·정렬·표시에 사용 가능
- `PARTIAL`: 유효하지만 coverage가 제한되어 결측을 명시해야 함
- `UNKNOWN`: 값은 있으나 0·sentinel·코드 의미가 확인되지 않음
- `INVALID`: 상수 오류, 파싱 오류, 손상 행 등으로 사용 불가
- `STALE`: 값은 유효할 수 있으나 기준일이 오래되어 경고 필요
- `UNSUPPORTED`: 데이터에 없거나 상품군 간 비교 정의가 없어 질의 지원 불가

`0`, 빈 문자열, `NULL`, 파싱 실패를 같은 값으로 합치지 않는다. 원천값, 정규화값, 품질 상태, 변환 규칙을 함께 보존한다.

## 4. 검색·답변 정책

- 필터·정렬·집계 전에 field registry의 상품군, 타입, 단위, 허용 연산자, 품질 상태를 검사한다.
- `UNKNOWN`, `INVALID`, `UNSUPPORTED` 필드는 hard constraint를 만족한 것으로 간주하지 않는다.
- coverage가 낮은 필드는 “값이 있는 상품 중”이라는 모집단 제한을 응답에 표시한다.
- 상품군 간 수익률·위험·보수 비교는 동일 정의·기간·단위가 확인된 필드만 허용한다.
- 손상 행은 자동 보정하지 않고 quarantine 테이블과 감사 로그에 남긴다.
- 답변의 기준일은 파일명 하나가 아니라 실제 사용한 각 필드의 갱신일을 우선한다.
- 원천 key, 원천 테이블, 사용 필드, 원천값, 정규화값, 단위, 기준일을 evidence에 기록한다.

## 5. 감사 번들의 사용 범위와 한계

보존된 번들은 다음 근거를 제공한다.

- 상품군별 profile JSON
- 통합 감사 JSON과 요약 Markdown
- QueryPlan 초안과 예시
- Agent/API 계약 예시
- Excel 감사 스크립트
- 공식 PDF 텍스트 추출본

다만 현재 스크립트는 그대로 운영 코드에 넣지 않는다.

- `/mnt/data`와 `(1).xlsx` 파일명 하드코딩이 있다.
- 빠른 펀드 감사기는 `lxml`에 의존하며 표준 parser와 일부 빈값·손상 값 표현이 다르다.
- 통합 `finance_data_audit.json`의 보강 정보 전체를 실행 스크립트가 재생성하지 못하며, 재실행 시 단순 구조로 덮어쓸 위험이 있다.
- QueryPlan schema는 HyperCLOVA X Structured Outputs가 지원하지 않을 수 있는 keyword를 포함한다.
- Agent/API 계약 예시는 실제 evidence가 채워진 합격 fixture가 아니다.

운영 전에는 경로를 인자로 받고, 입력 SHA-256과 parser 버전을 기록하며, 격리 행과 product-grain 통계를 검증하는 하나의 재현 가능한 감사 파이프라인으로 다시 작성한다.

## 6. 확인이 필요한 공식 질문

- 해외 ETP 총보수 0의 정확한 의미
- 수익률·배당·추적오차의 상수 0이 결측 sentinel인지 실제값인지
- 판매 가능·거래 가능·매수 가능 상태 코드의 정의
- 공모펀드 `prfd_attr_cd` 코드북, `or_attr_desc=06`, AUM 0과 장기 수익률 산식
- 상품군 간 동일 이름 지표의 단위·산식·비교 가능성
- 평가 시 기준일 표시 방식과 외부 데이터 갱신 허용 범위
