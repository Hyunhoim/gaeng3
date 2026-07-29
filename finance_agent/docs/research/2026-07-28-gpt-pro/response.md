# 검증 상태와 산출물

공식 PDF 1개와 Excel 8개를 **모두 직접 열어 검사했습니다. 읽지 못한 파일은 없습니다.** Excel은 표시된 dimension만 신뢰하지 않고 실제 셀 범위·헤더·스키마·키·결측·sentinel·날짜·손상 행을 Python으로 다시 계산했습니다.

* [공식 과제 소개 PDF](sandbox:/mnt/data/%28배표용%29과제소개자료_금융상품Agent.pdf)
* [데이터 감사 요약](sandbox:/mnt/data/finance_data_audit_summary.md)
* [전체 데이터 감사 JSON·SHA-256 manifest](sandbox:/mnt/data/finance_data_audit.json)
* [Typed QueryPlan JSON Schema](sandbox:/mnt/data/queryplan.schema.json)
* [첫 vertical slice QueryPlan 예시](sandbox:/mnt/data/queryplan.example.json)
* [Agent/API 계약 예시](sandbox:/mnt/data/agent_contract_examples.json)
* [감사 스크립트와 결과 전체 재현 번들](sandbox:/mnt/data/finance_agent_audit_bundle.zip)

아래에서는 **[공식]**, **[계산]**, **[연구]**, **[제안]**을 구분한다.

---

# 1) Executive verdict

## 한 문장 주력 전략

**Baseline B를 선택하되 Baseline A를 실행 코어로 삼는 `Evidence-Compiled Hybrid SQL Agent`—사전·lexical schema linking → HyperCLOVA X Typed QueryPlan → 결정론적 SQL compiler → 독립 verifier → field-level evidence·기준일·0건 설명—를 구현한다.**

## 이 전략이 가장 강한 이유

이 과제의 실제 난점은 “그럴듯한 답변 생성”이 아니라 다음 네 가지다.

1. 4개 마스터의 서로 다른 필드를 정확히 연결하는 것
2. 숫자·날짜·판매 상태·위험등급을 **정확한 조건으로 실행**하는 것
3. 결측·무효값·stale field를 정상값처럼 사용하지 않는 것
4. 비공개 질문에서 질문 표현이 바뀌어도 동일한 QueryPlan을 만드는 것

실제 데이터를 보면 국내 ETP 총보수는 12.5%, 채권 매수수익률은 2.1%, 공모펀드 위험등급은 80.7%만 존재하고, 해외 ETP 1일 수익률은 비결측값 전체가 0이다. 따라서 “모든 필드에 대한 자연어 RAG”보다 **field availability와 semantic validity를 아는 컴파일러형 Agent**가 훨씬 유리하다.

이 구조는 정량평가에서 필터·정렬·집계 정확도를 확보하면서, 정성평가에서는 다음을 명확하게 시연할 수 있다.

* 질문을 어떤 조건으로 해석했는지
* 어떤 데이터와 기준일을 사용했는지
* 왜 결과가 0건인지
* 어떤 조건 하나를 바꾸면 결과가 생기는지
* 어떤 질문은 왜 답할 수 없는지

## 버려야 할 유혹적인 접근 3개

1. **LLM이 raw SQL을 직접 생성하는 Text-to-SQL 중심 구조**
   실행 가능하더라도 잘못된 컬럼·결측 조건·status semantics를 사용하기 쉽고, 독립 검증이 어렵다.

2. **Vector DB 중심의 row-level RAG**
   “총보수 0.2% 이하”, “2028년 이전 만기”, “판매중이면서 거래정지 아님” 같은 정밀 조건에 약하다. 추가 embedding 모델의 규칙 위험도 생긴다.

3. **GraphDB·멀티 Agent 중심 구조**
   현재 데이터는 대체로 평면 마스터이며 유의미한 관계는 펀드의 반복 속성 정도다. Graph traversal이나 여러 Agent 간 협업으로 얻는 측정 가능한 이득보다 latency와 장애 경로가 커진다.

---

# 2) 공식 규칙 해석표

## 2.1 공식적으로 확정된 사실

| 구분             | 확인 결과                                                                   | 근거              |
| -------------- | ----------------------------------------------------------------------- | --------------- |
| 허용 LLM         | **HyperCLOVA X만 허용**                                                    | 공식 PDF p.4      |
| 타 LLM 사용       | HyperCLOVA X 외 LLM 사용 시 평가 대상 제외                                        | 공식 PDF p.4      |
| 구현 방식          | 데이터 적재·전처리·구조화·색인·검색 방식 자유                                              | 공식 PDF p.5      |
| 예시 기술          | RDB, GraphDB, Text-to-SQL, Vector DB, RAG, Re-ranker, Agent Framework   | 공식 PDF p.5      |
| 외부 데이터         | 금융상품 관련 외부 데이터 사용 가능                                                    | 공식 PDF p.5      |
| 평가 기준 데이터      | 주최 측 제공 데이터가 기준이며 충돌 시 제공 데이터 우선                                        | 공식 PDF p.5      |
| 답변 근거          | 참조 데이터를 표시해야 함                                                          | 공식 PDF p.5      |
| 확인 불가 질문       | 확인 불가 명시 또는 필요한 조건 역질문                                                  | 공식 PDF p.5      |
| 금지 답변          | 데이터에 근거 없는 수익률 전망·단정적 투자 추천                                             | 공식 PDF p.5      |
| 정량평가           | 주최 측이 참가팀 API에 GET 요청, 비공개 평가문제 사용                                      | 공식 PDF p.6      |
| 문제 난도          | 상·중·하 혼합                                                                | 공식 PDF p.6      |
| 정성평가           | 문제정의, 기술완성도·성능, 창의성·확장성, 답변 정확성·완결성, 현업 활용성·리스크 관리                      | 공식 PDF p.6      |
| 결선             | 본선 6팀, PT 및 라이브 시연 중심                                                   | 공식 PDF p.6      |
| 공식 endpoint 예시 | GET `/answer`, `question_id`, `question`                                | 공식 PDF p.7      |
| 응답 예시          | `question_id`, `question`, `retrieved_context`, `think_trace`, `answer` | 공식 PDF p.7      |
| 제출물            | 소스·재현환경·README, 기술제안서, API URL·명세                                       | 공식 PDF p.7      |
| 예선 마감          | 2026-09-06                                                              | 공식 PDF p.3, p.7 |
| 마감 후 변경        | commit/push·서버 배포 등 변경 적발 시 실격                                          | 공식 PDF p.7      |
| 참고 질의          | 2026-08-06 오프라인 설명회에서 공지 예정                                             | 공식 PDF p.5      |

**아직 공개되지 않은 것:** 정량평가의 정확한 metric, top-k 크기, numeric tolerance, endpoint timeout/QPS, HyperCLOVA X 세부 모델, `think_trace` 평가 방식, extra field 허용 여부다.

## 2.2 A/B/C 판단

| 기술·행위                                                                         | 분류 | 설명회 전 상태                | 판단 근거                                         |
| ----------------------------------------------------------------------------- | -- | ----------------------- | --------------------------------------------- |
| SQL, parameterized query, materialized view                                   | A  | 사용 가능 후보                | 데이터·검색 구현 방식 자유                               |
| BM25, inverted index, PostgreSQL FTS, `pg_trgm`                               | A  | 사용 가능 후보                | 생성형 LLM이 아닌 검색 알고리즘                           |
| deterministic parser·validator·rule engine                                    | A  | 사용 가능 후보                | 자체 코드·symbolic processing                     |
| constraint solver·최소 조건 완화                                                    | A  | 사용 가능 후보                | 수학적·결정론적 알고리즘                                 |
| Pydantic/JSON Schema/SQL AST 검사                                               | A  | 사용 가능 후보                | 검증 소프트웨어                                      |
| PostgreSQL, DuckDB, OpenSearch, Qdrant, Neo4j                                 | A  | 사용 가능 후보이나 데이터 적합성으로 선택 | 공식 자료에서 관련 구현 방식 허용                           |
| LangGraph, Haystack, LlamaIndex 등의 framework                                  | A  | 사용 가능 후보                | framework 자체는 모델이 아님                          |
| BGE-M3·multilingual-E5 등 embedding model                                      | B  | **기본 비활성화·보류**          | 범용 생성형 LLM과 다르지만 “LLM” 범위가 불명확                |
| CLOVA Studio `clir-emb-dolphin`, `clir-sts-dolphin`, `bge-m3`                 | B  | **보류**                  | Naver 제공 모델이어도 공식 대회 문서가 HCX 외 모델 범위를 설명하지 않음 |
| BGE cross-encoder re-ranker                                                   | B  | **기본 비활성화·보류**          | encoder-only이나 언어모델 해석 가능                     |
| KLUE-RoBERTa·KoBERT NER/분류                                                    | B  | **기본 비활성화·보류**          | 비생성형 transformer지만 명시적 허용 없음                  |
| Tesseract·classic PaddleOCR                                                   | B  | **보류**                  | OCR 전용 모델이나 model 사용 범위 확인 필요                 |
| NLLB·Marian 번역 모델                                                             | B  | **더 보수적으로 보류**          | 텍스트를 생성하고 현재 데이터 처리에 필수적이지 않음                 |
| HyperCLOVA X 외 생성형 LLM                                                        | C  | 사용 금지                   | 공식 PDF의 명시적 제외 규칙                             |
| HyperCLOVA X 외 VLM                                                            | C  | 사용 금지                   | 생성형 멀티모달 모델                                   |
| 비-HCX LLM을 parser·judge·fallback으로 사용                                         | C  | 사용 금지                   | 최종 평가 동작에 비-HCX LLM이 관여                       |
| 비-HCX LLM/VLM의 synthetic QA·label·summary·query expansion을 runtime asset으로 사용 | C  | 사용 금지                   | 간접적으로 평가 동작에 영향을 미침                           |
| PICARD의 T5 생성기, 외부 Text-to-SQL LLM                                            | C  | 사용 금지                   | 비-HCX 생성형 모델                                  |
| PaddleOCR-VL                                                                  | C  | 사용 금지                   | VLM 기반 문서 이해 모델                               |

CLOVA Studio에는 `clir-emb-dolphin`, `clir-sts-dolphin`, `bge-m3` embedding API가 실제로 제공되지만, **서비스 존재가 대회 허용을 의미하지는 않는다.** 따라서 이 역시 설명회 전에는 B로 둔다. ([Ncloud Docs Guide][1])

---

# 3) 재현 가능한 데이터 감사 결과

## 3.1 파일·sheet·dimension·header 검증

| 상품군    | schema sheets                    | datarows sheet | 실제 dimension |    실제 행×열 | schema-header |
| ------ | -------------------------------- | -------------- | -----------: | --------: | ------------- |
| 국내채권   | `Sheet1_Schema`, `Sheet2_Sample` | `datarows`     | `A1:AN42395` | 42,394×40 | 정확히 일치        |
| 국내 ETP | `Sheet1_Schema`, `Sheet2_Sample` | `datarows`     |  `A1:BU1735` |  1,734×73 | 정확히 일치        |
| 해외 ETP | `Sheet1_Schema`, `Sheet2_Sample` | `datarows`     |  `A1:AW5647` |  5,646×49 | 정확히 일치        |
| 공모펀드   | `Sheet1_Schema`, `Sheet2_Sample` | `datarows`     | `A1:AS95620` | 95,619×45 | 정확히 일치        |

* 원천 datarows 합계: **145,393행**
* 네 파일 모두 exact full-row duplicate: **0행**
* 단, 공모펀드는 논리적으로 동일한 상품이 `prfd_attr_cd`별로 반복된다.
* 원본값은 보존하고, 정규화값·quality flag·quarantine 사유를 별도 생성해야 한다.

## 3.2 공통 결측·sentinel 처리 계약

다음 값들을 전역적으로 한 번에 `NULL`로 바꾸면 안 된다.

| 유형              | 처리                                                       |
| --------------- | -------------------------------------------------------- |
| 실제 빈 셀          | `MISSING`                                                |
| 공백 문자열·padding  | trim 후 비어 있으면 `BLANK`                                    |
| 문자열 `"NULL"`    | `LITERAL_NULL`                                           |
| 숫자 0            | **field-specific**. 정상 0인지 sentinel인지 필드 규칙으로 판단         |
| `00000000`      | 날짜 무효                                                    |
| `99991231`      | 종료일 없음/미정 sentinel로 별도 표현                                |
| `10001231`      | 국내 ETP 상장일 1건의 비정상 날짜로 격리                                |
| `CURR_CD='000'` | 코드북 없이는 무효                                               |
| placeholder 문구  | “Index is not provided…”, “not available…” 등을 의미 결측으로 변환 |
| 수익률 극단값         | 원본 보존, ranking 기본 제외, quality flag 부여                    |

날짜는 `YYYYMMDD`, Excel 숫자, timestamp 문자열을 ISO `YYYY-MM-DD`로 변환하되 strict parsing을 적용한다.

---

## 3.3 국내채권

| 항목          | 감사 결과                                                          |
| ----------- | -------------------------------------------------------------- |
| 원천 grain    | 1행 = 1개 채권 종목                                                  |
| 논리 grain    | 원천과 동일                                                         |
| key         | `PD_NO`, 42,394개 모두 유일·결측 없음                                   |
| 주요 category | 회사채 31,447; 특수채 8,755; 국공채 2,137; 개인투자용국채 49                   |
| 시장          | 장내 24,749; 장외 17,645                                           |
| 통화          | KRW 42,372; USD 19; EUR 1; JPY 1; 무효 `000` 1                   |
| 판매·거래 상태    | 완전한 현재 판매상태 컬럼 없음                                              |
| 기준일         | `PD_STD_INFO_UPDATE` 최대 2026-02-24; `CRD_GRD_DT` 최대 2026-06-09 |
| 손상/shift    | 명백한 column shift는 없음. 일부 날짜·기본정보가 0인 sparse 종목 존재              |

### 필드 유효성

* `PD_NO`, `PD_NM`, `PD_EXG_MKT`, 분류, 통화는 거의 완전하다.
* `ISU_DT`, `MAT_DT`는 약 99.99% 값이 있으나 각각 0 sentinel이 존재한다.
* `SRFC_IRT`는 거의 완전하지만 0이 2,758행이다. 할인채·무이표채 가능성이 있으므로 **일괄 무효 처리하면 안 된다.**
* `PD_EVCO_CRD_GRD`: 58.89%
* `CRD_GRD`: 58.38%
* `PD_STD_INFO_UPDATE`: 75.06%
* `BUY_YIELD`, `BUYABLE_QUANTITY`: 각각 881행, **2.078%**
* `BUYABLE_QUANTITY>0`: 325행
* 이 중 파일 기준일 2026-07-11에 만기가 지나지 않은 행: **254행**
* `AVG_ANNUAL_TAX_YIELD`: 값이 있는 881행이 모두 0이므로 근거 필드에서 제외
* `REMAINING_DAYS`는 28,886행이 `MAT_DT-PD_STD_INFO_UPDATE`와 일치하지만 2,863행은 불일치

### 검색 안전도

| 안전                                                              | 부분 지원                                     | 사용 금지·강한 주의                                    |
| --------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------- |
| `PD_NO`, 명칭, 장내/장외, 대·소분류, 채권종류, 통화, sentinel 제거 후 발행·만기일, 발행잔액 | 신용등급, 위험등급, 표면금리의 0 해석, 평가가격, 적용수익률, 듀레이션 | `AVG_ANNUAL_TAX_YIELD`, 무효 날짜, `CURR_CD='000'` |
| 만기·분류·표면금리 조건                                                   | `BUY_YIELD`, `BUYABLE_QUANTITY`           | “현재 매수 가능” 단정                                  |
| 명시된 기준일의 평가정보                                                   | credit/date provenance가 있는 결과             | stale 매수 가능 정보                                 |

**핵심 정책:** 채권에서는 “현재 매수 가능한 종목” 대신 “제공 데이터에서 매수가능수량이 기록된 종목”으로만 답하고, coverage 2.1%와 기준일 한계를 표시한다.

---

## 3.4 국내 ETP

공식 파일명은 국내 ETF 마스터지만 `pd_grp_no`에는 ETF와 ETN이 함께 존재하므로 서비스 명칭은 **국내 ETP**가 정확하다.

| 항목          | 감사 결과                                               |
| ----------- | --------------------------------------------------- |
| 원천·논리 grain | 1행 = 1개 국내 ETF 또는 ETN                               |
| key         | `pd_itm_no` 유일; `pd_itm_no_ma`도 유일                  |
| 구성          | ETF 1,202; ETN 532                                  |
| 판매상태        | `pd_sale_yn=1` 1,520; `0` 214                       |
| 거래정지        | `pd_tr_yn=0` 1,661; `1` 72; 손상/기타 1                 |
| 자산군         | 주식 1,069; 채권 261; 상품 207; 혼합자산 78; 단기금융 42; 통화 41 등 |
| 지역          | 국내 1,036; 미국 453; 글로벌 76; 중국 63 등                   |
| 위험등급        | 전 행에 가까운 coverage, 1~6등급                            |
| 기준일         | 주요 `du_upt_dt`, `wu_upt_dt`는 2026-06-15             |
| 손상          | Excel **1155행** 1건 격리                               |

손상 행은 `pd_itm_no='KR'`, `pd_itm_no_ma='A0193MO'`, `pd_nm='.'`이고 명칭이 다른 컬럼에 들어가 있어 열 이동 또는 원천 손상이 강하게 의심된다.

### 필드 유효성

* `cu_charge_rt`: 217행, **12.51%**
* 의미 있는 `cu_base_index`: 58행, **3.34%**
* `pd_dvid_cycl`: 의미 있는 값 0
* `du_chas_errt`: 존재 행 전부 0
* `du_diff_rt`: 존재 행 전부 0
* `pd_dvid_yield`, `pd_divd_amt_pshr`: 존재 행 전부 0
* `du_er_1d`: 약 90.1%
* `du_er_1m`: 약 88.8%
* `du_er_3m`: 약 86.8%
* `du_er_6m`: 약 85.1%
* `du_er_ytd`: 약 85.2%
* `du_er_1y`: 약 79.4%
* 수익률에 `-100`, `2738.95` 등의 lifecycle/outlier가 있으므로 ranking 전 quality rule 필요
* `pd_net_tamt`: 89.45%로 국내 ETP의 순자산 근거에 적합
* `du_last_aum`: 83.79%이나 0값 411행

### 검색 안전도

| 안전                                | 부분 지원                  | 사용 금지·무효             |
| --------------------------------- | ---------------------- | -------------------- |
| `pd_grp_no`, ID·명칭, 자산군, 지역, 위험등급 | 총보수 217행               | 분배주기                 |
| 판매·거래정지, 연금거래 가능 여부               | 기초지수 58행               | 추적오차, 괴리율, 배당수익률     |
| `pd_net_tamt`, 기준일 포함 기간 수익률      | 전략·레버리지, `du_last_aum` | 실시간 가격·거래량, 섹터, 손상 행 |

---

## 3.5 해외 ETP

| 항목          | 감사 결과                                                                  |
| ----------- | ---------------------------------------------------------------------- |
| 원천·논리 grain | 1행 = 1개 해외 ETF 또는 ETN                                                  |
| key         | `pd_itm_no` 유일; `pd_itm_no_ma` 유일                                      |
| 보조 ID       | `pd_isin_cd`는 결측·중복 때문에 key 불가                                         |
| 구성          | ETF 5,587; ETN 59                                                      |
| 판매·거래       | 5,636행이 `pd_sale_yn=1`, `pd_tr_yn=0`; 10행은 상태정보 불완전                    |
| 통화          | 거래통화는 정상 행 대부분 USD                                                     |
| 기준일         | `cu_upt_dt`, `wu_upt_dt`, NAV 기준일 주로 2026-06-14; 가격·업데이트 최대 2026-06-16 |
| 손상·sparse   | 상태정보 불완전 10행, 이 중 8행은 상장일 `00000000`                                   |

### 필드 유효성

* `cu_charge_rt`: 100% 존재, 0~2.5. 단, 0인 363행은 실제 무보수인지 sentinel인지 추가 확인
* 운용사·전략·자산군·지역: 약 99.86%
* `du_last_aum`: 96.69%
* 가격·거래량: 약 99.82%
* `du_er_1d`: 5,388개 값이 존재하지만 **전부 0**
* `du_diff_rt`: 3행뿐이며 극단값
* NAV: 약 12.08%
* `cu_lev_fector`: 전부 결측
* `cu_base_index`: raw 값은 거의 있으나 placeholder 제거 후 의미 있는 값은 약 **2,933행, 51.95%**
* `cu_etn_yn`은 ETN 59행에만 `Y`이고 ETF는 null이므로 ETF/ETN 구분에는 `pd_grp_no`를 사용

### 검색 안전도

| 안전                                   | 부분 지원                       | 사용 금지·무효                |
| ------------------------------------ | --------------------------- | ----------------------- |
| ID·ticker·ISIN 조건부, 명칭, ETF/ETN, 운용사 | 기초지수 placeholder 정제 후 약 52% | 1일 수익률                  |
| 총보수, 자산군·지역, 거래소, AUM                | 전략·복제방식·인버스, NAV            | 괴리율, 레버리지 배수, core flag |
| 가격·거래량·판매·거래정지 및 각 기준일               | 0% 총보수의 의미                  | sparse 10행              |

---

## 3.6 공모펀드

| 항목               | 감사 결과                                    |
| ---------------- | ---------------------------------------- |
| 원천 grain         | 1행 = 상품 × `prfd_attr_cd` 속성              |
| 논리 상품 grain      | 1개 `itm_no` = 1개 펀드                      |
| 원천 key           | `itm_no + prfd_attr_cd`, 95,619행 모두 유일   |
| raw `itm_no` 고유값 | 11,139                                   |
| 정상 논리 상품         | **11,138개**                              |
| 비정상 ID           | 손상 행의 `itm_no='"'` 1개                    |
| 상품별 반복 수         | 4~16행, 중앙값 8, 평균 8.5848                  |
| 정상 상품 내 변동 필드    | 사실상 `prfd_attr_cd`만 변동                   |
| 기준일              | field별 날짜 없음. 파일 snapshot 2026-07-11만 사용 |
| 손상               | Excel **84563행** 열 이동 1건                 |

손상 행에서는 `itm_no='"'`, `itm_nm='공모'`, `prfd_attr_cd='해외'`, `thco_sale_yn='KRZ50226929C'`처럼 값이 오른쪽으로 이동해 있다.

### 반드시 적용할 정규화

```text
fund_product
  PK: itm_no
  상품 공통 필드 44개

fund_attribute
  PK: (itm_no, prfd_attr_cd)
  FK: itm_no -> fund_product

fund_quarantine
  source_row, raw_payload, reason
```

적재 시 동일한 `itm_no`의 비속성 필드가 모두 동일한지 assert한다. 향후 데이터에서 충돌이 발견되면 임의로 첫 행을 선택하지 말고 상품 전체를 quarantine한다.

### 필드 유효성

* `sale_yn='판매중'`: 76,318행
* `thco_sale_yn='Y'`: 91,594행
* 판매중이면서 당사판매 Y: 76,250행
* `prvo_pbff_desc='공모'`: 95,451행
* 사모 102행이 섞여 있으므로 이름만 믿지 말고 반드시 공모 필터 적용
* `fd_nast_suma`: 86.89%, 양수 80,603행
* 위험등급: 80.74%, literal `"NULL"` 18,416행
* 기간 수익률 coverage:

  * 1주 72.63%
  * 1개월 72.36%
  * 3개월 71.68%
  * 6개월 70.25%
  * 18개월 66.10%
  * 1년 67.38%
  * 2년 60.79%
  * 3년 58.37%
  * 5년 53.17%
* 18개월·1~5년 수익률에 ±500%, 1000% 이상의 비정상값이 반복되므로 기본 ranking에서 제외
* **보수 컬럼은 존재하지 않음**
* `or_attr_desc='06'` 5,436행은 코드북 없이는 의미 해석 금지
* `fd_estb_ctry_cd='000'` 92,838행도 코드북 없이는 국가 필드로 사용 금지

### 검색 안전도

| 안전                               | 부분 지원                     | 미지원·격리                             |
| -------------------------------- | ------------------------- | ---------------------------------- |
| `itm_no`, 명칭, 통화, 투자지역           | 위험등급 80.7%                | 보수·총보수                             |
| `prvo_pbff_desc='공모'`, 판매중, 당사판매 | AUM 86.9%, benchmark, 환헤지 | field별 기준일                         |
| 정제된 운용속성, 품질검사를 통과한 수익률          | 기간 수익률 53~73%             | 코드북 없는 `06`, 극단 수익률, 사모 102행, 손상 행 |

## 3.7 예비 profiling과 달라진 핵심

1. 공모펀드 논리 상품은 약 11,139개가 아니라 **정상 11,138개**다.
2. 국내 ETP에 손상 행 1건이 추가 확인됐다.
3. 해외 ETP의 기초지수는 raw coverage가 높아도 placeholder 제거 후 의미 coverage는 약 52%다.
4. 국내채권 `BUYABLE_QUANTITY>0` 325행 중 snapshot 기준 미만기 행은 254행뿐이다.
5. “파일 추출일 2026-07-11”과 “실제 field 기준일”은 반드시 분리해야 한다.
6. 값이 존재하는 필드와 검색 근거로 유효한 필드는 다르다.

---

# 4) 지원 질의 taxonomy

## 4.1 완전 지원

| 예시 질문                                                                    | 실행 필드                                                                                                 |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| 1. “미국 채권형 해외 ETF 중 판매 가능하고 거래정지가 아니며 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.” | `pd_grp_no`, `wu_inv_ast_type`, `wu_inv_rgn`, `pd_sale_yn`, `pd_tr_yn`, `cu_charge_rt`, `du_last_aum` |
| 2. “국내 주식형 ETF 중 위험등급 4~6등급이고 연금거래 가능한 상품을 찾아줘.”                         | `pd_grp_no`, `wu_inv_ast_type`, `wu_inv_rgn`, `pd_risk_cd`, `pd_pen_tr_yn`                            |
| 3. “판매중이며 거래정지가 아닌 국내 ETN을 자산군별로 몇 개인지 집계해줘.”                            | `pd_grp_no`, `pd_sale_yn`, `pd_tr_yn`, `wu_inv_ast_type`                                              |
| 4. “공모·판매중·당사판매 상품인 글로벌 채권형 펀드를 순자산 순으로 10개 보여줘.”                        | `prvo_pbff_desc`, `sale_yn`, `thco_sale_yn`, `fd_ivst_rgn_desc`, `or_attr_desc`, `fd_nast_suma`       |
| 5. “원화 회사채 중 2027년 이후 만기이고 표면금리 4% 이상이며 위험등급 4~6인 상품을 찾아줘.”              | `CURR_CD`, `STD_PD_MCLS_NM`, `MAT_DT`, `SRFC_IRT`, `PD_RISK_GCD`                                      |

## 4.2 조건부 지원

| 예시 질문                         | 응답 정책                                     |
| ----------------------------- | ----------------------------------------- |
| 1. “총보수 0.3% 이하인 국내 ETF”      | 총보수가 존재하는 217행만 대상으로 했음을 표시               |
| 2. “S&P 500을 기초지수로 하는 국내 ETF” | 의미 있는 기초지수 58행 범위에서만 검색했다고 표시             |
| 3. “AAA 이상 채권”                | 신용등급이 있는 약 58% 범위에서만 검색, 결측은 불충족이 아니라 미확인 |
| 4. “1년 수익률이 높은 저위험 공모펀드”      | 수익률·위험등급 모두 존재하고 품질검사를 통과한 상품만 사용         |
| 5. “매수 가능한 채권 중 수익률 상위”       | “매수가능수량이 기록된 데이터 행”으로 제한하고 현재 매수 가능 단정 금지 |

## 4.3 clarification 필요

1. “안전한 ETF 추천해줘.”
   → 위험등급, 채권형 여부, 연금 안전자산 여부 중 무엇을 의미하는지 확인한다.

2. “미국 상품을 찾아줘.”
   → 미국 상장, 투자지역 미국, 미국 자산 기초지수 중 무엇인지 확인한다.

3. “수수료가 싼 상품.”
   → 상품군과 최대 보수 기준을 묻는다. 펀드는 보수 데이터가 없음을 안내한다.

4. “수익률 좋은 상품.”
   → 1개월·3개월·1년 등 기간과 상품군을 확인한다.

5. “살 수 있는 채권.”
   → snapshot에 기록된 매수수량을 묻는지, 실시간 판매 가능 여부를 묻는지 확인한다.

## 4.4 데이터 부족으로 abstain

1. “공모펀드 총보수가 가장 낮은 상품”
   → 펀드 데이터에 보수 컬럼이 없다.

2. “분배주기가 매월인 국내 ETF”
   → `pd_dvid_cycl`이 의미 있게 채워져 있지 않다.

3. “어제 가장 많이 오른 해외 ETF”
   → 해외 ETP 1일 수익률 비결측값이 모두 0이어서 근거로 사용할 수 없다.

4. “현재 즉시 매수 가능한 회사채”
   → 현재 판매상태를 보장하는 완전하고 최신인 필드가 없다.

5. “위험등급이 같은 해외 ETF와 공모펀드를 비교”
   → 해외 ETP에는 비교 가능한 동일 위험등급 필드가 없다.

## 4.5 금지된 투자 전망·단정적 추천

1. “다음 달 수익률이 가장 높을 ETF를 골라줘.”
2. “원금 보장되면서 고수익인 상품을 추천해줘.”
3. “지금 무조건 사야 할 상품 하나만 알려줘.”
4. “2027년에 금리가 떨어질 테니 가장 오를 채권을 골라줘.”
5. “전 재산을 넣기에 가장 좋은 상품을 정해줘.”

이 경우 예측·단정은 거부하고, 제공 데이터로 가능한 **조건 기반 조회·과거 지표 비교**로 전환한다.

---

# 5) 최신 논문·오픈소스 evidence table

## 5.1 연구 근거

| 방법·논문                                                                                                                        | venue·연도·식별자                                                                                                                                                 | A/B/C·운영 상태                                                     | 우리 시스템에서의 역할                                                                    | 구현비용·위험                                           | 결정                                          |
| ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------- |
| **PICARD: Parsing Incrementally for Constrained Auto-Regressive Decoding from Language Models**, Scholak, Schucher, Bahdanau | EMNLP 2021, DOI `10.18653/v1/2021.emnlp-main.779`, [proceedings](https://aclanthology.org/2021.emnlp-main.779/), [code](https://github.com/ElementAI/picard) | incremental constraint 원리 A/사용 가능 후보; T5 구현 C/사용 금지             | HCX 출력 후 토큰 단위가 아니라 **schema validation·allowed enum·operator allowlist**로 변환   | 중간. PICARD 자체 통합 대신 server-side compiler가 단순하고 안전 | **원리 채택, 모델·코드 경로 제외** ([ACL Anthology][2]) |
| **Re-appraising the Schema Linking for Text-to-SQL**, Gan, Chen, Purver                                                      | Findings ACL 2023, DOI `10.18653/v1/2023.findings-acl.53`                                                                                                    | typo·synonym 견고성 원리 A; PLM encoder B/보류                         | exact match만 쓰지 않고 alias 사전, `pg_trgm`, RapidFuzz 조합                            | 낮음. 한글 띄어쓰기·오탈자 테스트 필요                            | **채택** ([ACL Anthology][3])                 |
| **LitE-SQL**, Piao, Lee, Park                                                                                                | Findings EACL 2026, DOI `10.18653/v1/2026.findings-eacl.186`                                                                                                 | execution feedback A; vector linker B/보류; 비-HCX SQL generator C | schema 후보 축소와 실행 오류 feedback을 “HCX 재생성”이 아니라 deterministic validation error로 변환 | 중간. raw SQL self-correction은 제외                   | **패턴 일부 채택** ([ACL Anthology][4])           |
| **VET: Verifiable Execution Tracing for Reliable Text-to-SQL Generation**, Wang et al.                                       | Findings ACL 2026, DOI `10.18653/v1/2026.findings-acl.1544`                                                                                                  | 관찰 가능한 실행 trace A; 외부 LLM 구현 C                                  | 계획·실행·row count·검증을 숨은 사고가 아닌 audit trace로 기록                                   | 중간. trace schema 설계 필요                            | **채택** ([ACL Anthology][5])                 |
| **Why Not?**, Chapman, Jagadish                                                                                              | SIGMOD 2009, DOI `10.1145/1559845.1559901`                                                                                                                   | A/사용 가능 후보                                                      | 0건 결과에서 어떤 predicate가 결과를 제거했는지 설명                                              | 낮음~중간. predicate bitset 구현                        | **채택** ([dblp][6])                          |
| **Enabling LLMs to Generate Text with Citations**, Gao et al.                                                                | EMNLP 2023, DOI `10.18653/v1/2023.emnlp-main.398`                                                                                                            | citation evaluation 원리 A                                        | citation correctness·completeness metric 정의                                     | 낮음                                                | **평가 설계 채택** ([ACL Anthology][7])           |
| **RAGTruth**, Niu et al.                                                                                                     | ACL 2024, DOI `10.18653/v1/2024.acl-long.585`                                                                                                                | hallucination taxonomy A; 비-HCX detector C                      | 답변 claim과 evidence의 entailment를 field-value exact check로 대체                     | 중간. 자연어 claim atomization 최소화 필요                  | **metric만 채택** ([ACL Anthology][8])         |
| **TRAQ**, Li et al.                                                                                                          | NAACL 2024, DOI `10.18653/v1/2024.naacl-long.210`                                                                                                            | conformal idea A                                                | availability·parser uncertainty를 이용한 risk-coverage calibration 후보               | 현재 일정에는 비용 큼                                      | **후순위 실험** ([ACL Anthology][9])             |
| **LatentRefusal**, Ren et al.                                                                                                | Findings ACL 2026, DOI `10.18653/v1/2026.findings-acl.1007`                                                                                                  | answerability gate 원리 A; Llama/Qwen hidden-state probe C        | 모델 hidden state 대신 deterministic field availability gate                        | 낮음                                                | **개념만 채택** ([ACL Anthology][10])            |
| **τ-bench**                                                                                                                  | Agent tool-use benchmark                                                                                                                                     | DB 결과·정책 준수·반복 안정성 평가 A                                         | single-run뿐 아니라 동일 질문 반복 `pass^k`와 최종 DB 결과 기반 채점                               | 낮음                                                | **평가 방식 채택** ([τ-bench][11])                |

## 5.2 오픈소스·모델 후보

| 프로젝트                                                                                                                         | license·유지보수                                       | A/B/C·현재 상태             | 역할·예상 이득                                                 | 비용·hardware·위험                        | 결정                                                            |
| ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- | ----------------------- | -------------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------- |
| [PostgreSQL](https://www.postgresql.org/) + [`pg_trgm`](https://www.postgresql.org/docs/current/pgtrgm.html)                 | PostgreSQL License, 현재 공식 문서 유지                    | A·사용 가능 후보              | serving DB, FTS, typo·부분문자열 schema/name retrieval        | CPU, 별도 검색 서비스 불필요                    | **주력 채택**. trigram similarity와 GiST/GIN 활용 ([PostgreSQL][12]) |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)                                                                          | MIT, 3.14.5가 2026-04-07 공개, Python 3.10+, C++ core | A·사용 가능 후보              | 짧은 field alias·상품명 fuzzy match                           | CPU 수 ms, dependency 낮음               | **채택** ([GitHub][13])                                         |
| [Pydantic](https://github.com/pydantic/pydantic)                                                                             | MIT                                                | A·사용 가능 후보              | QueryPlan·tool I/O·contract 검증                           | CPU, 낮은 비용                            | **채택** ([GitHub][14])                                         |
| [SQLGlot](https://github.com/tobymao/sqlglot)                                                                                | MIT, Python parser/transpiler                      | A·사용 가능 후보              | 생성된 SQL AST allowlist·table/column 제한                    | CPU. parser가 실행 정확성을 보장하지는 않음         | **보조 채택**. sole validator로 사용 금지 ([GitHub][15])               |
| [DuckDB](https://github.com/duckdb/duckdb)                                                                                   | MIT, 1.5.3이 2026-05-20 공개                          | A·사용 가능 후보              | offline profiling, Parquet regression, SQL result oracle | embedded CPU                          | **offline 채택, serving 제외** ([GitHub][16])                     |
| [LangGraph](https://github.com/langchain-ai/langgraph)                                                                       | MIT, 1.2.9가 2026-07-10 공개                          | A·사용 가능 후보              | 상태 전이·timeout·trace framework                            | Python, 낮은 compute지만 추상화 비용           | **custom orchestration과 ablation만** ([GitHub][17])            |
| [Haystack](https://github.com/deepset-ai/haystack)                                                                           | Apache-2.0 중심, 2.29.0이 2026-05-12 공개               | A·사용 가능 후보              | pipeline·retriever·routing                               | dependency와 telemetry 설정 검토 필요        | **비교 실험만, 주력 제외** ([GitHub][18])                              |
| [OpenSearch](https://github.com/opensearch-project/OpenSearch)                                                               | Apache-2.0                                         | A·사용 가능 후보              | 대규모 lexical/BM25 검색                                      | JVM service·운영 복잡도                    | 14.5만 행에는 **초기 제외** ([GitHub][19])                            |
| [Qdrant](https://github.com/qdrant/qdrant) / [pgvector](https://github.com/pgvector/pgvector)                                | Qdrant Apache-2.0, pgvector는 제출 전 LICENSE 재확인      | software A; embedding B | semantic retrieval                                       | 별도 index·embedding 규칙 위험              | **현재 제외**, B 허용 후 실험                                          |
| [Neo4j](https://neo4j.com/)                                                                                                  | Community/Enterprise license 검토 필요                 | software A              | 관계 탐색                                                    | 현재 데이터에서 graph benefit 미미             | **제외** ([Neo4j Graph Intelligence Platform][20])              |
| [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3)                                                                          | MIT, 1024차원, 최대 8192 token, 100개 이상 언어             | B·보류                    | unseen synonym schema linking                            | PyTorch/ONNX, GPU 권장·CPU benchmark 필요 | 서면 허용 후 **실험** ([Hugging Face][21])                           |
| [`intfloat/multilingual-e5-base`](https://huggingface.co/intfloat/multilingual-e5-base)                                      | MIT                                                | B·보류                    | schema/alias semantic retrieval                          | 약 1GB급 weight, CPU latency 위험         | BGE-M3보다 우선순위 낮음 ([Hugging Face][22])                         |
| [`BAAI/bge-reranker-v2-m3`](https://huggingface.co/BAAI/bge-reranker-v2-m3)                                                  | Apache-2.0, multilingual XLM-R 계열                  | B·보류                    | top-N field/schema reranking                             | cross-encoder라 GPU 선호, CPU p95 위험     | 서면 허용·2pp 이상 개선 시만 채택 ([Hugging Face][23])                    |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) / [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) classic OCR | Apache-2.0                                         | B·보류                    | 향후 외부 PDF OCR                                            | CPU 가능. 이번 제공 Excel에는 불필요             | 인터페이스만, runtime 미연결 ([GitHub][24])                            |
| [`facebook/nllb-200-distilled-600M`](https://huggingface.co/facebook/nllb-200-distilled-600M)                                | CC-BY-NC-4.0                                       | B·더 보수적 보류              | 영문 전략 번역                                                 | 생성형 translation, GPU·license·규칙 위험    | **사실상 제외**. 영문 원문/HCX 설명 사용 ([Hugging Face][25])              |

## 실제로 실험할 5개만 남기면

1. `pg_trgm + PostgreSQL FTS + RapidFuzz + domain alias` schema linking
2. HCX Typed QueryPlan + strict server-side validation
3. deterministic parameterized SQL compiler + independent verifier
4. why-not zero-result diagnosis + single-constraint relaxation
5. **주최 측 서면 허용 시에만** BGE-M3 또는 BGE reranker 중 하나

---

# 6) 후보 아키텍처 비교와 최종 선택

가중치는 사용자가 제시한 25/20/20/15/10/5/5를 유지한다. 공식 예선이 비공개 API 정량평가와 신뢰성·리스크 관리 정성평가를 혼합하므로 조정할 이유가 없다.

| 기준               |     가중치 | A: schema-first SQL | B: hybrid+deterministic | C: general Agent/Text-to-SQL |
| ---------------- | ------: | ------------------: | ----------------------: | ---------------------------: |
| 비공개 질의 정확도       |      25 |                  22 |                  **24** |                           19 |
| 근거·환각·abstention |      20 |              **20** |                  **20** |                           13 |
| 실제 필드 적합성        |      20 |              **19** |                  **19** |                           13 |
| 6주 구현성           |      15 |              **14** |                      13 |                            8 |
| latency·안정성      |      10 |               **9** |                       8 |                            5 |
| 차별성·시연           |       5 |                   2 |                   **5** |                            5 |
| 규칙·license 위험    |       5 |               **5** |                   **5** |                            4 |
| **총점**           | **100** |              **91** |                  **94** |                       **67** |

## 주력안

**Baseline B: Hybrid retrieval + deterministic execution**

다만 hybrid는 “모든 상품 행을 vector search”한다는 의미가 아니다.

* lexical/schema retrieval은 **질문과 관련된 canonical field·table 후보를 찾는 용도**
* 숫자·날짜·status·등급·집계는 **100% SQL**
* 결과 검증은 **100% 코드**
* 생성 답변은 **검증된 evidence만 HCX에 제공**
* embedding branch는 설명회 전 기본 `disabled`

## 규칙 해석이 달라질 때 fallback

주최 측이 encoder-only 모델도 금지한다고 답하면:

```text
Domain alias dictionary
+ PostgreSQL FTS
+ pg_trgm
+ RapidFuzz
+ HCX Typed QueryPlan
+ deterministic SQL/verifier
```

로 그대로 운영한다. 핵심 구조를 바꿀 필요가 없다.

## 시스템 구조도

```text
GET /answer
   │
   ▼
Request Adapter ── deadline / request validation
   │
   ▼
Rule Router + Lexical Schema Linker
(alias dictionary + FTS + pg_trgm + RapidFuzz)
   │
   ├── [feature flag OFF by default]
   │       BGE embedding / cross-encoder reranker
   │
   ▼
HyperCLOVA X QueryPlan Parser
(Structured Output where available)
   │
   ▼
Pydantic + Field Registry Validator
 ├─ field exists?
 ├─ operation allowed?
 ├─ unit/enum valid?
 ├─ field VALID/PARTIAL/INVALID?
 └─ ambiguity/unsupported?
   │
   ▼
Deterministic Query Compiler
(parameterized SQL only)
   │
   ▼
PostgreSQL Semantic Layer
(product_common + family detail + fund_attribute)
   │
   ▼
Independent Result Verifier
(reapply predicates / recompute sort & aggregate)
   │
   ├─ result > 0 ──────────────────────┐
   │                                   │
   └─ result = 0                       │
        ▼                              │
   Why-not Diagnosis                   │
   + Minimal Relaxation Proposal       │
        └──────────────────────────────┘
                    │
                    ▼
Evidence & Provenance Ledger
                    │
                    ▼
HCX Evidence Renderer
   └─ timeout/failure → deterministic template
                    │
                    ▼
Official /answer Adapter
```

---

# 7) 세부 Agent 설계

## 7.1 데이터 모델

### 공통 projection

```text
product_common
- product_id
- family                 bond | domestic_etp | overseas_etp | public_fund
- product_type           BOND | ETF | ETN | FUND
- name
- short_name_or_ticker
- currency
- asset_type
- region
- risk_level
- sellable               TRUE | FALSE | UNKNOWN
- trading_suspended      TRUE | FALSE | UNKNOWN
- aum_value
- aum_currency
- source_snapshot_date
- quality_flags[]
```

### 상품군 전용

```text
bond_detail
- maturity_date
- issue_date
- coupon_rate_pct
- credit_rating
- duration
- valuation_price
- applied_yield_pct
- recorded_buy_yield_pct
- recorded_buyable_quantity

domestic_etp_detail
- total_expense_ratio_pct
- base_index
- return_1d/1m/3m/6m/1y/ytd_pct
- pension_eligible
- pension_risk
- leverage_factor

overseas_etp_detail
- isin
- exchange
- issuer
- total_expense_ratio_pct
- base_index
- strategy
- replication_method
- inverse_flag
- market_price
- volume

fund_detail
- public_private_type
- company_sale_flag
- fund_type
- benchmark
- hedge_flag
- return_1w/1m/3m/6m/18m/1y/2y/3y/5y_pct

fund_attribute
- product_id
- prfd_attr_cd
```

## 7.2 Field Registry

Agent가 전체 raw schema를 암기하게 하지 않고 다음 registry만 본다.

```yaml
common.aum:
  families: [domestic_etp, overseas_etp, public_fund]
  source:
    domestic_etp: pd_net_tamt
    overseas_etp: du_last_aum
    public_fund: fd_nast_suma
  type: decimal
  unit:
    domestic_etp: KRW
    overseas_etp: pd_trd_ccy
    public_fund: curr_cd
  allowed_ops: [eq, gt, gte, lt, lte, between]
  semantic_state:
    domestic_etp: VALID
    overseas_etp: VALID
    public_fund: PARTIAL
  missing_policy: EXCLUDE_AND_REPORT
  date_source:
    domestic_etp: du_upt_dt
    overseas_etp: du_upt_dt
    public_fund: snapshot_date
```

각 field에 다음을 둔다.

* canonical ID
* 상품군
* 원천 column
* type·unit
* 한글·영문 alias
* enum mapping
* allowed operators
* coverage
* `VALID | PARTIAL | INVALID | STALE | UNSUPPORTED`
* sentinel rule
* field 기준일 원천
* cross-product comparability group
* quality notes

## 7.3 Typed QueryPlan JSON Schema 초안

전체 버전은 [QueryPlan JSON Schema](sandbox:/mnt/data/queryplan.schema.json)에 있다. HCX 전송용 schema에서는 공식 Structured Outputs가 지원하지 않는 `pattern` 등을 제거하고, server-side Pydantic에서 다시 엄격하게 검증한다.

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "version": {
      "type": "string",
      "enum": ["1.0"]
    },
    "question_id": {
      "type": "string"
    },
    "intent": {
      "type": "string",
      "enum": [
        "search",
        "lookup",
        "compare",
        "aggregate",
        "explain",
        "clarify"
      ]
    },
    "product_families": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "bond",
          "domestic_etp",
          "overseas_etp",
          "public_fund"
        ]
      },
      "minItems": 1,
      "uniqueItems": true
    },
    "constraints": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "field": {"type": "string"},
          "op": {
            "type": "string",
            "enum": [
              "eq", "ne", "in", "not_in",
              "gt", "gte", "lt", "lte",
              "between", "contains", "is_known", "is_unknown"
            ]
          },
          "value": {},
          "unit": {
            "type": ["string", "null"]
          },
          "strength": {
            "type": "string",
            "enum": ["hard", "soft"]
          },
          "source_span": {
            "type": "string"
          }
        },
        "required": [
          "field", "op", "value",
          "unit", "strength", "source_span"
        ]
      }
    },
    "ranking": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "field": {"type": "string"},
          "direction": {
            "type": "string",
            "enum": ["asc", "desc"]
          },
          "missing": {
            "type": "string",
            "enum": ["exclude", "last"]
          }
        },
        "required": ["field", "direction", "missing"]
      }
    },
    "projection": {
      "type": "array",
      "items": {"type": "string"},
      "uniqueItems": true
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "comparison_scope": {
      "type": "string",
      "enum": ["within_family", "cross_family", "none"]
    },
    "ambiguities": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": [
    "version", "question_id", "intent",
    "product_families", "constraints",
    "ranking", "projection", "limit",
    "comparison_scope", "ambiguities"
  ]
}
```

첫 vertical slice의 실제 plan은 [QueryPlan 예시](sandbox:/mnt/data/queryplan.example.json)에 포함했다.

## 7.4 Tool 목록

HCX가 자유롭게 tool을 탐색하는 구조가 아니라, QueryPlan에 따라 deterministic orchestrator가 호출한다.

| Tool                   | 입력                        | 출력                                    |
| ---------------------- | ------------------------- | ------------------------------------- |
| `resolve_schema`       | 질문 token·alias 후보         | canonical field 후보와 lexical score     |
| `search_products`      | validated QueryPlan       | 제품 rows, row count, SQL hash          |
| `get_product`          | family, product ID        | 정규화 상품과 evidence                      |
| `compare_products`     | IDs, compatible metrics   | 비교표 또는 incompatibility                |
| `aggregate_products`   | filters, group-by, metric | count/min/max/avg 등                   |
| `diagnose_zero_result` | QueryPlan                 | predicate별 pass/fail/unknown·복원 count |
| `propose_relaxation`   | diagnosis, target k       | 최소 비용 단일 변경안                          |
| `verify_execution`     | QueryPlan, raw result     | predicate·sort·aggregate 검증 결과        |
| `render_evidence`      | verified result           | 답변용 compact evidence bundle           |

## 7.5 Router

순서가 중요하다.

1. exact ID·ticker·ISIN·종목번호를 먼저 탐지
2. ETF/ETN/채권/펀드 명시어로 family 후보 생성
3. alias·FTS·trigram으로 field 후보 top-N 생성
4. 질문과 field 후보만 HCX에 제공
5. HCX가 canonical field ID로 QueryPlan 생성
6. ambiguity가 남으면 SQL을 실행하지 않고 clarification

`ETF`는 ETF만, `ETN`은 ETN만, `ETP`·상장지수상품은 둘 다로 매핑한다. `cu_etn_yn`이 아니라 `pd_grp_no`를 authoritative source로 사용한다.

## 7.6 HyperCLOVA X prompt 전략

```text
역할:
사용자 질문을 금융상품 조회용 Typed QueryPlan으로 변환한다.

규칙:
1. 제공된 canonical field 목록에 없는 field를 만들지 않는다.
2. SQL, table name, raw column name을 출력하지 않는다.
3. 숫자, 단위, 기간, 등급, 판매상태를 분리한다.
4. “반드시/이하/이상”은 hard constraint다.
5. “가급적/우선/선호”만 soft constraint다.
6. 지원되지 않거나 의미가 모호한 조건은 삭제하지 말고 ambiguities에 기록한다.
7. 공통 의미·단위·기준일이 없는 cross-product 비교를 계획하지 않는다.
8. 지정된 JSON Schema 외 텍스트를 출력하지 않는다.
```

현재 CLOVA Studio 공식 문서상 Structured Outputs는 HCX-007에서 제공되며, JSON Schema 일부만 지원하고 `pattern`은 지원하지 않는다. 또한 Structured Outputs는 Thinking·Function Calling과 동시에 사용할 수 없다. 따라서 parser call에서는 `thinking.effort="none"`을 사용해야 한다. 대회 계정에서 HCX-007을 허용하는지는 8월 6일에 확인해야 한다. ([Ncloud Docs][26])

## 7.7 실패 복구

```text
HCX parser call
  ├─ JSON/schema valid → deterministic validation
  ├─ schema invalid → validation error를 포함해 1회 repair
  └─ 2회 실패 →
       exact/lexical quick path 가능: deterministic 실행
       ambiguity 존재: clarification
       그 외: PLAN_VALIDATION_FAILED
```

* HCX가 raw SQL을 만들 기회 자체를 주지 않는다.
* tool timeout은 각 단계별 budget으로 끊는다.
* HCX 답변 renderer가 실패해도 evidence가 이미 있으면 template answer를 반환한다.
* SQL execution error는 HCX에게 SQL을 고치게 하지 않고 compiler bug로 처리한다.
* 같은 plan hash에 대해 결과와 evidence가 항상 재현되어야 한다.

## 7.8 독립 검증

LLM plan과 SQL 결과는 별도 코드가 검증한다.

1. 결과 row에 각 predicate를 다시 적용
2. hard constraint 위반 row 수가 0인지 확인
3. sort key 순서 재계산
4. aggregate를 Python 또는 독립 SQL로 재계산
5. projection field가 실제 evidence에 존재하는지 확인
6. 표시된 기준일이 field provenance와 같은지 확인
7. invalid·quarantine row가 포함되지 않았는지 확인

## 7.9 `think_trace`

공식 필드명은 `think_trace`지만 숨은 사고과정을 넣지 않는다.

```json
{
  "trace_version": "1.0",
  "hidden_reasoning_included": false,
  "route": "overseas_etp.search",
  "plan_hash": "sha256:...",
  "planner_attempts": 1,
  "canonical_constraints": [
    "product_type=ETF",
    "asset_type=채권",
    "region=미국",
    "total_expense_ratio_pct<=0.20",
    "sellable=true",
    "trading_suspended=false"
  ],
  "tools": [
    {
      "name": "search_products",
      "duration_ms": 38,
      "result_count": 7
    },
    {
      "name": "verify_execution",
      "duration_ms": 4,
      "hard_constraint_violations": 0
    }
  ],
  "evidence_ids": ["E-001", "E-002"],
  "decision": "answered"
}
```

이는 **감사 가능한 execution trace**이지 chain-of-thought가 아니다.

---

# 8) 검색·조건 완화 알고리즘

## 8.1 exact filter

```python
def execute(plan):
    validated = validate_plan(plan, field_registry)
    sql, params = compile_parameterized_sql(validated)
    rows = db.execute(sql, params)
    verification = verifier.verify(validated, rows)
    assert verification.hard_constraint_violations == 0
    return rows, verification
```

SQL compiler는 다음만 허용한다.

* 등록된 table/view
* 등록된 canonical field
* field별 allowed operator
* parameter binding
* allowlisted aggregate
* 최대 limit
* 지정된 sort

## 8.2 결측의 3값 논리

각 조건 결과를 `PASS | FAIL | UNKNOWN`으로 둔다.

```text
field 값 존재 + predicate 만족     → PASS
field 값 존재 + predicate 불만족   → FAIL
field 결측·무효·stale              → UNKNOWN
```

기본 검색에서는 `UNKNOWN`을 결과에 포함하지 않지만, 답변에는 다음처럼 표시한다.

> 총보수 값이 확인되는 상품만 대상으로 검색했으며, 총보수 미확인 상품은 조건 불충족으로 간주하지 않고 결과에서 제외했습니다.

결측을 0으로 대체하지 않는다.

## 8.3 ranking

순위는 다음 순서로 결정한다.

1. 모든 hard constraint 만족
2. 모든 ranking field가 known인지 확인
3. 사용자 지정 sort
4. 동률이면 freshness
5. 다시 동률이면 stable product ID

통화가 다른 AUM은 환율 데이터·기준일 없이 직접 비교하지 않는다.

## 8.4 0건 진단

조건이 `c1...cn`일 때 N번 full scan을 하지 않고 한 번의 predicate matrix를 만든다.

```text
row_id | c1 | c2 | c3 | c4 | ...
P001   | P  | P  | F  | P
P002   | P  | U  | P  | P
```

각 조건 `ci`를 제외했을 때의 복원 결과 수를 계산한다.

```math
restored_i =
|{r : 모든 c_j (j≠i)를 PASS하고 c_i만 FAIL 또는 UNKNOWN}|
```

출력:

```json
{
  "full_result_count": 0,
  "bottlenecks": [
    {
      "constraint": "total_expense_ratio_pct <= 0.20",
      "count_without_constraint": 7,
      "failed_count": 1850,
      "unknown_count": 0
    },
    {
      "constraint": "region = 미국",
      "count_without_constraint": 2
    }
  ]
}
```

## 8.5 사용자 필수 조건과 선호 조건

* “반드시”, “이하”, “이상”, “제외”, “ETF만” → `hard`
* “가급적”, “가능하면”, “우선”, “선호” → `soft`
* 명시된 조건은 기본적으로 hard
* hard 조건을 사용자 동의 없이 바꾸지 않는다

자동 완화 금지 항목:

* 상품군·ETF/ETN 구분
* 통화
* 판매 가능·거래정지
* 공모/사모
* 사용자가 “반드시”라고 지정한 조건
* 비교 기준일·단위 호환성

## 8.6 single-constraint relaxation

후보 완화는 한 번에 하나만 만든다.

* 숫자: 실제 데이터의 가장 가까운 breakpoint
* 위험등급: 인접 등급
* 날짜: 가장 가까운 월·연도 경계
* enum: 사전에 정의된 인접 ontology가 있을 때만
* 결측 허용: 기본적으로 자동 제안하지 않고 “미확인 상품 포함” 옵션으로 별도 표시

비용 함수:

```math
C(r) =
∞ × hard_violation
+ Σ_i w_i × normalized_delta_i
+ λ_u × unknown_inclusions
+ λ_f × family_switch
+ λ_n × number_of_changed_constraints
```

선택 규칙:

1. hard violation 0
2. 변경 조건 수 최소
3. 최소 `k`개 결과를 복원
4. normalized delta 최소
5. unknown 포함 수 최소
6. 더 최신인 field 우선

예:

```text
현재 조건으로는 0건입니다.

가장 작은 단일 변경:
- 총보수 상한을 0.20%에서 0.23%로 변경하면 3개 상품이 확인됩니다.

지역·자산군·ETF·판매·거래상태 조건은 그대로 유지됩니다.
이 조건으로 다시 조회할까요?
```

## 8.7 완화 결과 재검증

완화된 plan도 처음부터 다시 실행한다.

```text
relaxed plan
→ Pydantic validation
→ fresh SQL compile
→ fresh execution
→ independent verifier
→ original hard constraints preservation check
```

기존 결과를 임의로 추가하거나 LLM이 상품을 고르지 않는다.

---

# 9) 평가 계획

## 9.1 정량 metric

| Metric                      | 정의                                                    |                     목표 |
| --------------------------- | ----------------------------------------------------- | ---------------------: |
| Intent accuracy             | `search/lookup/compare/aggregate/explain/clarify` 정확도 |                   ≥97% |
| Product-family accuracy     | bond/domestic ETP/overseas ETP/fund exact match       |                   ≥98% |
| Slot/constraint F1          | field·operator·value·unit·hard/soft 단위 micro F1       |                   ≥95% |
| QueryPlan exact match       | normalization 후 gold plan과 일치                         |                   ≥90% |
| Executable QueryPlan rate   | validation·compile·execution 성공 비율                    |                   ≥98% |
| Filter result precision     | 반환 ID 중 gold 조건 만족 비율                                 |                   ≥99% |
| Result-set F1               | 전체 eligible product ID 기준                             |                   ≥97% |
| Top-k relevance             | P@5 또는 P@10                                           |                  ≥0.98 |
| Ranking quality             | gold order 대비 NDCG@5/10                               |                  ≥0.95 |
| Evidence coverage           | 사실 claim 중 evidence가 있는 비율                            |                   ≥99% |
| Provenance correctness      | field·source column·value·date 일치                     |                   ≥99% |
| Unsupported-condition F1    | 미지원 field 탐지                                          |                   ≥95% |
| Abstention precision        | abstain 중 실제 미지원 비율                                   |                   ≥95% |
| Abstention recall           | 미지원 질문 중 abstain한 비율                                  |                   ≥90% |
| Zero-result diagnosis top-1 | 실제 bottleneck을 1순위로 제시                                |                   ≥90% |
| Relaxation correctness      | 최소 단일 변경 정답률                                          |                   ≥90% |
| Hard-constraint violation   | 완화·검색 결과의 필수 조건 위반                                    |              **0/500** |
| Answer factuality           | 답변 atomic claim의 evidence 일치율                         |                   ≥99% |
| Repeat reliability          | 동일 질문 반복에서 동일 plan/result                             |             pass³ ≥98% |
| Latency                     | end-to-end                                            | p50·p95 기록, p95 목표 <8초 |
| HCX calls                   | 요청당 평균 call 수                                         |                   ≤2.1 |
| HCX 비용                      | input/output token × 공식 단가                            |        질문 taxonomy별 측정 |

Agent 평가는 단일 평균 외에도 반복 실행 성공률을 봐야 한다. τ-bench도 tool·policy·최종 상태와 반복 신뢰성을 강조한다. ([τ-bench][11])

## 9.2 Golden QA 형식

```json
{
  "question_id": "G-0001",
  "question": "미국 채권형 해외 ETF 중 총보수 0.2% 이하...",
  "taxonomy": "fully_supported",
  "gold_query_plan": {},
  "gold_result_ids": ["...", "..."],
  "gold_ordered_ids": ["...", "..."],
  "gold_evidence": [
    {
      "product_id": "...",
      "field_id": "overseas_etp.total_expense_ratio_pct",
      "expected_value": 0.18,
      "as_of": "2026-06-14"
    }
  ],
  "answer_policy": "answer",
  "unsupported_fields": [],
  "expected_clarification": null
}
```

## 9.3 정답 생성

* 조건 조합은 deterministic template로 생성
* 정답 product IDs·aggregation은 SQL oracle로 생성
* 자연어 paraphrase는 Domain·QA 담당자가 직접 작성
* test 질문과 정답은 수작업 재검수
* 비-HCX 생성형 LLM의 synthetic QA·label은 사용하지 않음
* HCX를 QA 생성에 사용할 경우에도 평가 artifact에 반영 가능한지 설명회에서 확인하기 전에는 사용하지 않음

## 9.4 분리

목표 800개 기준:

* prompt·rule development: 480
* dev: 160
* untouched test: 160
* 별도 distribution-shift set: 120

단순 random split이 아니라 다음을 분리한다.

* 질문 template
* product name/entity
* alias 표현
* 숫자 단위 표현
* unsupported field 조합
* contradictory constraints

## 9.5 Distribution shift taxonomy

1. 오탈자: “총보슈”, “미국체권”
2. 띄어쓰기: “채권 혼합”, “채권혼합”
3. 영문·한글 혼합: “US bond ETF”
4. 단위: 20bp, 0.2%, 0.002
5. 기간: 1년, 12개월, year-to-date
6. 등급: 4등급 이하의 의미 방향
7. 부정조건: ETN 제외, 거래정지 아닌
8. 모순조건: ETF이면서 ETN
9. 지원되지 않는 metric
10. stale/live information 요구
11. 상품군 교차 비교
12. 이름·ticker의 부분 일치

---

# 10) Ablation과 실험 우선순위

| 실험 | 비교                                               | 성공 기준                                                                  | 중단·제외 기준                                   |
| -- | ------------------------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------ |
| E1 | SQL-only rule parser vs HCX parser+SQL           | unseen paraphrase constraint F1 **+10pp 이상** 또는 plan EM +8pp           | +3pp 미만이면 simple query는 rule quick path 유지 |
| E2 | 전체 schema prompt vs lexical top-N schema linking | plan EM +3pp 또는 unseen synonym F1 +5pp, p95 overhead ≤150ms            | 개선 <2pp 또는 field 누락 증가                     |
| E3 | lexical only vs embedding hybrid                 | **서면 허용 후**, plan EM/NDCG +2pp 이상, p95 overhead ≤250ms                 | 허용 불명확, +2pp 미만, CPU p95 초과                |
| E4 | reranker 없음 vs BGE reranker                      | top-1 field accuracy +2pp 이상                                           | hard field miss 증가 또는 p95 +250ms 초과        |
| E5 | verifier 없음 vs 있음                                | false-supported answer 50% 이상 감소, provenance error <1%, overhead ≤50ms | overhead만 증가하고 오류 탐지 <20%                  |
| E6 | 조건 완화 없음 vs 있음                                   | zero diagnosis top-1 ≥90%, relaxation ≥90%, hard violation 0/500       | hard violation 1건이라도 발생 시 자동완화 비활성화        |
| E7 | custom state machine vs LangGraph                | orchestration defect 또는 test boilerplate 20% 이상 감소, p95 +100ms 이하      | trace 불투명·dependency 증가·성능 이득 없음           |
| E8 | HCX renderer vs deterministic template           | factuality 유지 99%+, human completeness +10pp                           | factuality 저하 또는 평균 HCX call >2.1          |

## 우선순위

1. SQL-only oracle
2. HCX QueryPlan parser
3. verifier
4. zero-result diagnosis
5. lexical schema linker
6. rendering
7. framework ablation
8. 허용 후 embedding/reranker

---

# 11) 6주 실행 로드맵

## Week 1: 07.28–08.02 — 데이터 계약과 첫 end-to-end slice

| 항목         | 내용                                                                                                     |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| 산출물        | source manifest, normalization spec, quarantine report, field registry v1, overseas ETP vertical slice |
| Owner      | AI·Data·Agent                                                                                          |
| Dependency | 제공 Excel                                                                                               |
| Acceptance | 4개 header 정확히 일치; 손상 행 격리; 해외 ETP 50개 gold query에서 filter precision 100%                               |
| 실험         | SQL-only deterministic oracle                                                                          |
| 통합         | Application 팀 Docker/Postgres에 migration 전달                                                            |
| Kill/Pivot | 공통 projection에서 의미 손실이 발생하면 family view를 우선하고 physical merge 중단                                        |

## Week 2: 08.03–08.09 — QueryPlan·HCX·설명회 반영

### 08.06 이전

* QueryPlan schema/Pydantic
* Mock provider
* HCX provider interface
* Structured Output 최소 실험
* 설명회 질문 확정
* B 모델 feature flags 기본 `false`

### 08.06 이후

| 항목         | 내용                                                                              |
| ---------- | ------------------------------------------------------------------------------- |
| 산출물        | 규칙 결정 기록, model allowlist, API contract v1 freeze, 참고 질의 taxonomy               |
| Owner      | AI·Data + Application                                                           |
| Dependency | 오프라인 설명회 답변                                                                     |
| Acceptance | reference 질문을 gold plan으로 변환; rule ambiguity 0건                                 |
| 실험         | SQL-only vs HCX parser; full schema vs lexical schema linking                   |
| 통합         | `/answer` Mock → real agent 교체                                                  |
| Kill/Pivot | reference 질의가 비교·집계보다 단순 lookup 중심이면 relaxation 개발 비중 일부를 entity resolution로 이동 |

## Week 3: 08.10–08.16 — 4개 상품군 완성

| 항목         | 내용                                                                             |
| ---------- | ------------------------------------------------------------------------------ |
| 산출물        | bond/domestic ETP/overseas ETP/fund tools, fund normalization, evidence schema |
| Owner      | AI·Data                                                                        |
| Dependency | field registry v1                                                              |
| Acceptance | 각 family 100문항, executable ≥98%, result precision ≥99%                         |
| 실험         | 공통 projection vs family-specific route                                         |
| 통합         | ProductSummary/Evidence가 frontend API에 노출                                      |
| Kill/Pivot | cross-family semantic mismatch가 많으면 지원 가능한 metric whitelist만 남김                |

## Week 4: 08.17–08.23 — 신뢰성·0건·조건 완화

| 항목         | 내용                                                                                                    |
| ---------- | ----------------------------------------------------------------------------------------------------- |
| 산출물        | verifier, tri-state missing, why-not diagnosis, single relaxation, cross-product compatibility matrix |
| Owner      | AI·Data; Domain·QA 검수                                                                                 |
| Dependency | 전체 search tool                                                                                        |
| Acceptance | hard violation 0/500; zero diagnosis ≥90%; evidence correctness ≥99%                                  |
| 실험         | verifier·relaxation on/off                                                                            |
| 통합         | clarification·abstention UI/status                                                                    |
| Kill/Pivot | relaxation 정확도 90% 미만이면 자동 제안 대신 병목조건 설명만 제공                                                          |

## Week 5: 08.24–08.30 — 비공개 질의 대비와 최적화

| 항목         | 내용                                                                   |
| ---------- | -------------------------------------------------------------------- |
| 산출물        | 800 gold QA, 120 shift QA, latency report, ablation report, 기술제안서 초안 |
| Owner      | AI·Data / Domain·QA / Application                                    |
| Dependency | end-to-end system                                                    |
| Acceptance | test plan EM ≥90%, factuality ≥99%, abstention F1 목표 충족              |
| 실험         | framework, renderer, 허용된 경우 BGE/re-ranker                            |
| 통합         | NCP staging 전체 dry run                                               |
| Kill/Pivot | semantic model 이득 <2pp면 제거; framework overhead가 크면 custom 고정         |

## Week 6: 08.31–09.06 — freeze·배포·재현성

| 항목         | 내용                                                                          |
| ---------- | --------------------------------------------------------------------------- |
| 산출물        | pinned requirements, Docker image digest, README, 기술제안서, API 명세, 운영 runbook |
| Owner      | 전원                                                                          |
| Dependency | 모든 기능 freeze                                                                |
| Acceptance | clean machine 재현, 1,000회 soak test, endpoint p95, 장애 복구 테스트, 결과 checksum    |
| 실험         | 없음. regression only                                                         |
| 통합         | production endpoint                                                         |
| Kill/Pivot | 09.03 이후 신규 framework·model·schema 변경 금지; 오류 기능은 disable하고 안정 경로 선택         |

---

# 12) 첫 72시간 실행 백로그

첫 vertical slice는 **해외 ETF**다. 운용사·총보수·자산군·지역·AUM·판매상태가 거의 완전하고, 조건 필터·정렬·근거·기준일을 한 번에 검증할 수 있기 때문이다. 무효인 1일 수익률은 사용하지 않는다.

| 순서 | 목적                       | module                                                        | 입력 → 출력                                    | 완료 기준                                          |  시간 | 선행  |
| -: | ------------------------ | ------------------------------------------------------------- | ------------------------------------------ | ---------------------------------------------- | --: | --- |
|  1 | 원천 재현성 고정                | `data/audit/`, `source_manifest.json`                         | 8 Excel → audit JSON·SHA-256               | CI에서 동일 count·hash                             |  4h | 없음  |
|  2 | 품질 계약 정의                 | `data/quality/contracts.py`, `quarantine.yaml`                | raw row → valid/partial/invalid/quarantine | 손상 2건·해외 sparse 행 탐지                           |  6h | 1   |
|  3 | 해외 ETP 정규화               | `data/normalize/overseas_etp.py`, migration                   | Excel → Postgres tables                    | 5,646행 적재, 10행 flag, key uniqueness            |  6h | 1,2 |
|  4 | Field Registry 구축        | `domain/field_registry.yaml`, `enums.py`                      | schema·profiling → canonical fields        | vertical slice field 100% 등록                   |  6h | 2   |
|  5 | QueryPlan 구현             | `agent/query_plan.py`, `queryplan.schema.json`                | JSON → validated plan                      | 잘못된 field/operator/unit 전부 reject              |  6h | 4   |
|  6 | lexical schema linker    | `retrieval/schema_linker.py`                                  | 질문 → field candidates                      | typo·동의어 50문항 top-3 recall ≥98%                |  6h | 4   |
|  7 | compiler·search·verifier | `sql/compiler.py`, `tools/product_search.py`, `verification/` | plan → rows·evidence                       | 50 gold query precision 100%, hard violation 0 | 10h | 3,5 |
|  8 | Mock·API vertical slice  | `providers/mock.py`, `adapters/answer.py`, `eval/golden/`     | GET request → 공식 response                  | Docker에서 `/answer` end-to-end 통과               | 10h | 5~7 |

총 약 54 engineer-hour이며, 1~4와 Application API shell 작업은 병렬화할 수 있다.

---

# 13) Application 팀과 먼저 고정할 계약

전체 예시는 [Agent/API 계약 예시 JSON](sandbox:/mnt/data/agent_contract_examples.json)에 있다.

## 13.1 `AgentRequest`

```json
{
  "question_id": "Q-001",
  "question": "미국 채권형 해외 ETF 중 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.",
  "locale": "ko-KR",
  "deadline_ms": 9000
}
```

`locale`과 `deadline_ms`는 내부 필드다. 공식 GET adapter가 생성한다.

## 13.2 내부 `AgentResponse`

```json
{
  "question_id": "Q-001",
  "question": "미국 채권형 해외 ETF 중 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.",
  "status": "ok",
  "answer": "조건에 부합하는 상품은 ...입니다. 총보수·자산군 정보 기준일은 2026-06-14이며, 가격 정보는 상품별 표시 기준일을 사용했습니다.",
  "products": [],
  "evidence": [],
  "query_plan": {},
  "trace": {
    "hidden_reasoning_included": false,
    "plan_hash": "sha256:...",
    "hard_constraint_violations": 0
  },
  "error": null
}
```

Status:

```text
ok
clarification_required
abstained
unsupported
no_results
timeout
internal_error
```

## 13.3 `ProductSummary`

```json
{
  "product_id": "ARKI.K",
  "family": "overseas_etp",
  "product_type": "ETF",
  "name": "ARK DIET Q2 Buffer ETF",
  "ticker_or_short_name": "ARKI",
  "currency": "USD",
  "asset_type": "Alternatives",
  "region": "United States of America",
  "risk_level": null,
  "sellable": true,
  "trading_suspended": false,
  "aum": {
    "value": 1060000.0,
    "currency": "USD"
  },
  "as_of": {
    "product": "2026-06-14",
    "market": "2026-06-16"
  },
  "quality_flags": []
}
```

해외 ETP에 없는 위험등급은 임의 생성하지 않고 `null`이다.

## 13.4 `Evidence`

```json
{
  "evidence_id": "E-001",
  "dataset_id": "PREF02N001",
  "source_file": "PREF02N001_해외ETF마스터_20260711_datarows.xlsx",
  "source_snapshot_date": "2026-07-11",
  "product_id": "ARKI.K",
  "row_key": {
    "pd_itm_no": "ARKI.K"
  },
  "field_id": "overseas_etp.total_expense_ratio_pct",
  "source_column": "cu_charge_rt",
  "raw_value": 0.0,
  "normalized_value": 0.0,
  "unit": "pct_point",
  "field_as_of": "2026-06-14",
  "quality_flags": [
    "ZERO_REQUIRES_INTERPRETATION"
  ],
  "predicate_role": "filter"
}
```

## 13.5 오류

```json
{
  "code": "UNSUPPORTED_FIELD",
  "message": "공모펀드 데이터에는 총보수 필드가 없습니다.",
  "retryable": false,
  "details": {
    "field": "public_fund.total_expense_ratio_pct"
  }
}
```

오류 코드:

```text
INVALID_REQUEST
AMBIGUOUS_QUERY
UNSUPPORTED_FIELD
NO_RESULTS
STALE_OR_PARTIAL_DATA
PLAN_VALIDATION_FAILED
TOOL_TIMEOUT
HCX_TIMEOUT
INTERNAL_ERROR
```

## 13.6 공식 `/answer` adapter

설명회에서 object 허용 여부를 확인하기 전에는 `retrieved_context`와 `think_trace`를 JSON object가 아니라 **JSON-serialized string**으로 반환하는 편이 예시 스키마와 가장 가깝다.

```json
{
  "question_id": "Q-001",
  "question": "미국 채권형 해외 ETF 중 총보수 0.20% 이하인 상품을 AUM 순으로 5개 보여줘.",
  "retrieved_context": "{\"evidence\":[{\"dataset_id\":\"PREF02N001\",\"product_id\":\"...\",\"fields\":{...}}]}",
  "think_trace": "{\"hidden_reasoning_included\":false,\"plan_hash\":\"sha256:...\",\"tools\":[...],\"decision\":\"answered\"}",
  "answer": "조건에 부합하는 해외 ETF는 ...입니다. 상품 속성 기준일은 2026-06-14입니다."
}
```

## 13.7 timeout 계약

* 공식 timeout 확인 전 환경변수화: `EVAL_REQUEST_TIMEOUT_MS`
* 내부 budget 예시:

  * request/router: 300ms
  * HCX parser: 3,500ms
  * SQL: 1,000ms
  * verifier: 100ms
  * renderer: 3,000ms
* renderer timeout이 나도 verified data가 있으면 template fallback
* parser가 실패하면 임의 답변을 생성하지 않음
* valid evaluation request는 abstention이더라도 공식 5-field JSON을 반환
* malformed request의 HTTP code와 retry 정책은 설명회 답변 후 고정

## 13.8 Mock fixture

```json
{
  "fixture_id": "overseas_etp_fee_aum_v1",
  "input": {
    "question_id": "Q-MOCK-001",
    "question": "미국 채권형 해외 ETF 중 총보수 0.2% 이하"
  },
  "mock_query_plan": {},
  "mock_tool_result": {
    "result_count": 3,
    "rows": []
  },
  "expected_status": "ok"
}
```

Application 팀은 실제 HCX 없이도 이 fixture로 frontend·FastAPI·OpenAPI client·timeout을 개발한다.

---

# 14) 위험 등록부

확률과 영향은 1~5 척도다.

| 위험                           | 확률 | 영향 | 탐지 신호                             | 완화책                                                           | Owner            |
| ---------------------------- | -: | -: | --------------------------------- | ------------------------------------------------------------- | ---------------- |
| 8월 6일 참고 질의와 현재 taxonomy 불일치 |  4 |  5 | lookup보다 설명·비정형 질문 비중이 높음         | reference 질문 24시간 내 분류, roadmap 비중 재조정                        | AI·Data + Domain |
| 모델 사용 규칙 오해                  |  3 |  5 | 답변이 구두·모호함                        | 실제 모델명·온라인/오프라인 위치를 적어 서면 yes/no 요청, allowlist 문서화            | 전원               |
| 결측·중복·손상 행 누락                |  5 |  4 | row count·key·coverage regression | source hash, quality contract, quarantine CI                  | AI·Data          |
| stale field를 현재값으로 표현        |  4 |  5 | snapshot와 field date 불일치          | field-level provenance, freshness policy, 답변 템플릿 강제           | AI·Data          |
| HCX structured output 실패     |  3 |  4 | schema invalid·unknown field 증가   | strict schema, repair 1회, template/clarification fallback     | AI·Data          |
| HCX-007 미제공                  |  3 |  4 | 대회 계정 모델 목록에 없음                   | JSON-only prompt + extraction + Pydantic validator fallback   | AI·Data          |
| latency·rate limit           |  3 |  4 | p95 상승, 429, timeout              | HCX 최대 2회, connection pool, compact schema, template renderer | AI·Data + App    |
| framework 과설계                |  3 |  3 | 상태 추적 어려움, dependency 증가          | custom state machine 주력, framework gate 수치화                   | AI·Data          |
| license 위반                   |  2 |  5 | NC·research-only dependency 발견    | SBOM, license CI, NLLB 제외, dependency review                  | App              |
| 외부 데이터 충돌                    |  2 |  4 | 같은 상품의 값이 다름                      | 제공 데이터 authoritative, source 우선순위 명시                          | AI·Data          |
| 평가 API schema 변경             |  3 |  5 | 필드 타입·auth·method 변경 공지           | thin adapter 분리, 내부 contract 고정                               | App              |
| evaluation timeout 미확인       |  4 |  4 | staging은 성공하나 evaluator timeout   | 8월 6일 P0 질문, configurable budget                              | App              |
| 마감 후 서버 장애                   |  2 |  5 | restart·secret rotation 필요        | immutable image, health check, auto-restart, 운영 변경 허용 범위 확인   | App              |
| 데이터 재배포·refresh              |  3 |  4 | hash·row count 변경                 | idempotent loader, manifest diff report                       | AI·Data          |
| cross-product 잘못된 비교         |  3 |  5 | 통화·기간·정의가 다른 metric 비교            | compatibility matrix, incompatible이면 abstain                  | AI·Data          |
| QA 정답 누수·template 과적합        |  3 |  4 | random test만 높고 shift set 하락      | template/entity-disjoint test, adversarial set                | Domain·QA        |
| 답변은 맞지만 evidence가 틀림         |  3 |  5 | product ID와 field source 불일치      | evidence ID 기반 렌더링, provenance exact test                     | AI·Data          |

---

# 15) 8월 6일 설명회 질문

가능하면 현장에서 구두 답변만 듣지 말고 디스코드 Q&A에 같은 내용을 다시 올려 **서면 답변을 남겨야 한다.**

## P0 — 모델 허용 범위

1. **“HyperCLOVA X 외 다른 LLM 모델 사용 금지”에서 LLM은 생성형 모델만 의미합니까, 아니면 encoder-only embedding·cross-encoder·BERT NLU 모델도 포함합니까?**

2. 다음 **offline embedding** 사용을 명시적으로 허용 또는 금지해 주십시오.

   * `BAAI/bge-m3`
   * `intfloat/multilingual-e5-base`
     사용 위치: 상품·schema description을 사전 embedding하고, 평가 시 query embedding만 실행

3. 다음 **CLOVA Studio Embedding API** 사용을 명시적으로 허용 또는 금지해 주십시오.

   * `clir-emb-dolphin`
   * `clir-sts-dolphin`
   * `bge-m3` Embedding v2
     사용 위치: 평가 요청의 schema retrieval

4. 다음 **online cross-encoder reranker** 사용을 명시적으로 허용 또는 금지해 주십시오.

   * `BAAI/bge-reranker-v2-m3`
     사용 위치: lexical retrieval로 얻은 schema field 20개를 top-5로 재정렬

5. 다음 encoder-only NER·분류 모델을 허용 또는 금지해 주십시오.

   * `klue/roberta-base`
   * `skt/kobert-base-v1`
     사용 위치: 상품군·숫자·지역·위험등급 추출 보조

6. 다음 전용 OCR을 허용 또는 금지해 주십시오.

   * Tesseract 5.x
   * PaddleOCR classic PP-OCR 계열
     사용 위치: 외부 금융 PDF의 offline OCR
     그리고 **PaddleOCR-VL 같은 VLM은 금지**로 이해하면 되는지 확인해 주십시오.

7. 다음 전용 번역 모델을 허용 또는 금지해 주십시오.

   * `facebook/nllb-200-distilled-600M`
   * `Helsinki-NLP/opus-mt-ko-en`
     offline 전처리와 online 번역을 구분해 답변해 주십시오.

8. 비-HCX 생성형 LLM의 출력이 **개발 아이디어 검토에만 사용되고**, synthetic QA·label·summary·cache·distillation artifact가 제출 시스템에 전혀 포함되지 않는 경우까지 금지 대상입니까?

9. 제출 시스템에서 허용하거나 필수로 요구하는 HyperCLOVA X 모델명은 무엇입니까?

   * HCX-007
   * HCX-005
   * 기타 대회 전용 endpoint

10. HCX-007의 Structured Outputs를 사용할 수 있습니까? 대회 계정·credit에서 Chat Completions v3가 활성화됩니까?

11. HyperCLOVA X tuning·fine-tuning은 허용됩니까? 허용될 경우 대회 제공 credit에서 가능한 방식과 학습 데이터 제약은 무엇입니까?

12. CLOVA Studio의 공식 re-ranker·RAG Reasoning tool도 HyperCLOVA X 허용 범주에 포함됩니까, 아니면 별도 모델로 간주됩니까?

## P0 — 평가 API

13. GET `/answer`의 **connect timeout, read timeout, 전체 timeout**은 각각 몇 초입니까?

14. 평가 시 동시 요청 수, QPS, 총 질문 수, retry 횟수와 retry 조건은 무엇입니까?

15. `retrieved_context`와 `think_trace`는 반드시 문자열입니까, JSON object/array도 허용됩니까?

16. 공식 예시 5개 필드 외에 `status`, `evidence`, `products` 같은 extra field를 추가하면 무시됩니까, schema error가 발생합니까?

17. `think_trace`에는 숨은 chain-of-thought가 아니라 다음과 같은 실행 audit trace를 넣어도 됩니까?

* QueryPlan
* tool 호출
* SQL row count
* verifier 결과
* evidence IDs
* latency

18. endpoint 인증 방식, IP allowlist, TLS certificate 요구사항, health-check endpoint 요구사항이 있습니까?

19. 서버가 반드시 네이버 클라우드에 있어야 합니까, 아니면 외부 cloud/on-prem endpoint도 허용됩니까?

20. 평가 서버에서 외부 인터넷 outbound가 허용됩니까? API key와 secret 제공 방식은 무엇입니까?

## P0 — 채점

21. 상품 검색 답변은 상품명, `pd_itm_no`, ticker, ISIN 중 어떤 식별자를 기준으로 채점합니까?

22. top-k 질문에서 정확한 k, 순서, 동률 처리, 추가 정답 포함에 대한 tolerance는 무엇입니까?

23. 숫자 답변의 반올림·허용 오차는 얼마입니까?

24. 결과가 0건일 때 “0건” 답변과 조건 완화 제안은 어떻게 채점합니까?

25. 데이터에 없는 질문에서 abstention과 clarification 중 어느 응답을 기대합니까?

26. 비공개 질문은 한국어만 사용합니까? 영문, 한영 혼합, 오탈자, 약어, 대화형 follow-up도 포함됩니까?

27. 정량평가는 최종 `answer`만 보나요, `retrieved_context`·`think_trace`도 함께 자동/수동 평가합니까?

## P1 — 데이터·외부 자료

28. 평가 시 제공 데이터는 2026-07-11 snapshot으로 고정됩니까, 평가 전에 갱신됩니까?

29. 채권 `BUYABLE_QUANTITY`·`BUY_YIELD`처럼 일부 행만 있고 field 기준일이 불명확한 값은 “현재 매수 가능”으로 해석해도 됩니까?

30. 국내 ETP의 추적오차·괴리율·배당수익률과 해외 ETP 1일 수익률이 0으로 채워진 경우, 0을 실제 값으로 평가합니까 아니면 미확인으로 처리해야 합니까?

31. 공모펀드에서 동일 `itm_no`가 `prfd_attr_cd`별로 반복되는 구조의 공식 의미와 속성 코드북을 제공할 예정입니까?

32. `or_attr_desc='06'`, 전략 코드 `C`, 각 위험등급 code와 같은 미해석 코드의 추가 dictionary가 제공됩니까?

33. 외부 데이터와 주최 데이터가 충돌할 때 제공 데이터 우선이라는 원칙을 답변에 명시해야 합니까?

34. 외부 데이터의 허용 시점은 offline 적재와 evaluation-time 실시간 호출이 모두 포함됩니까?

## P1 — 제출·운영

35. 09.06 이후 코드·image 변경 없이 서버 restart, autoscaling, secret rotation, 장애 복구를 하는 것도 “서버 배포 변경”에 포함됩니까?

36. 제출 시 Docker image digest·Git commit hash를 등록합니까?

37. 평가 기간 중 로그·metric 열람은 허용됩니까? 평가 질문 원문을 로그에 저장해도 됩니까?

38. 개인정보·보안상 평가 질문 또는 결과에 대한 logging 제한이 있습니까?

39. NCP credit 한도, HCX token quota, requests-per-minute 제한과 추가 사용 비용은 어떻게 됩니까?

40. API 장애 시 공식 error response schema와 재평가 정책은 무엇입니까?

---

# 16) 최종 의사결정

```text
주력 아키텍처: Evidence-Compiled Hybrid SQL Agent — lexical/schema retrieval + HCX Typed QueryPlan + deterministic SQL + independent verifier
핵심 차별점: field availability·기준일·결측을 1급 객체로 만들고, 0건의 원인과 최소 단일 조건 완화를 검증 가능한 execution trace로 설명
첫 vertical slice: 미국 채권형 해외 ETF, 총보수 0.20% 이하, 판매·거래 가능, AUM 상위 5개
이번 주 실험 3개: SQL-only vs HCX parser; direct-schema vs lexical schema linking; verifier/zero-result diagnosis on/off
지금 제외할 기술: raw Text-to-SQL, Vector DB 중심 RAG, GraphDB, 멀티 Agent, 비-HCX 생성형 LLM/VLM
8월 6일 확인 전 보류할 기술: BGE-M3/E5 embedding, BGE cross-encoder, KLUE/KoBERT NER·분류, Tesseract/PP-OCR, NLLB/Marian
Application 팀에 오늘 전달할 계약: AgentRequest/Response, QueryPlan, ProductSummary, Evidence, 오류·timeout, Mock fixture, 공식 /answer adapter v1
72시간 뒤 Go/No-Go 기준: vertical slice 50문항에서 executable plan ≥98%, filter precision ≥99%, evidence correctness 100%, hard-constraint violation 0, p95(Mock)<1.0s
```

[1]: https://guide.ncloud-docs.com/docs/en/clovastudio-explorer03?utm_source=chatgpt.com "Using APIs"
[2]: https://aclanthology.org/2021.emnlp-main.779/ "https://aclanthology.org/2021.emnlp-main.779/"
[3]: https://aclanthology.org/2023.findings-acl.53/ "https://aclanthology.org/2023.findings-acl.53/"
[4]: https://aclanthology.org/2026.findings-eacl.186/ "LitE-SQL: A Lightweight and Efficient Text-to-SQL Framework with Vector-based Schema Linking and Execution-Guided Self-Correction - ACL Anthology"
[5]: https://aclanthology.org/2026.findings-acl.1544/ "VET: Verifiable Execution Tracing for Reliable Text-to-SQL Generation - ACL Anthology"
[6]: https://dblp.org/rec/conf/sigmod/ChapmanJ09.html "https://dblp.org/rec/conf/sigmod/ChapmanJ09.html"
[7]: https://aclanthology.org/2023.emnlp-main.398/ "Enabling Large Language Models to Generate Text with Citations - ACL Anthology"
[8]: https://aclanthology.org/2024.acl-long.585/ "RAGTruth: A Hallucination Corpus for Developing Trustworthy Retrieval-Augmented Language Models - ACL Anthology"
[9]: https://aclanthology.org/2024.naacl-long.210/ "https://aclanthology.org/2024.naacl-long.210/"
[10]: https://aclanthology.org/2026.findings-acl.1007/ "LatentRefusal: Latent-Signal Refusal for Unanswerable Text-to-SQL Queries - ACL Anthology"
[11]: https://taubench.com/ "τ-bench — Benchmarking AI Agents on Real-World Tasks"
[12]: https://www.postgresql.org/docs/current/pgtrgm.html "PostgreSQL: Documentation: 18: F.35. pg_trgm — support for similarity of text using trigram matching"
[13]: https://github.com/rapidfuzz/RapidFuzz?utm_source=chatgpt.com "GitHub - rapidfuzz/RapidFuzz: Rapid fuzzy string matching in Python using various string metrics · GitHub"
[14]: https://github.com/pydantic/pydantic/blob/main/LICENSE "https://github.com/pydantic/pydantic/blob/main/LICENSE"
[15]: https://github.com/tobymao/sqlglot "https://github.com/tobymao/sqlglot"
[16]: https://github.com/duckdb/duckdb "https://github.com/duckdb/duckdb"
[17]: https://github.com/langchain-ai/langgraph?utm_source=chatgpt.com "GitHub - langchain-ai/langgraph: Build resilient agents. · GitHub"
[18]: https://github.com/deepset-ai/haystack?utm_source=chatgpt.com "GitHub - deepset-ai/haystack: Open-source AI orchestration framework for building context-engineered, production-ready LLM applications. Design modular pipelines and agent workflows with explicit control over retrieval, routing, memory, and generation. Built for scalable agents, RAG, multimodal applications, semantic search, and conversational systems. · GitHub"
[19]: https://github.com/opensearch-project/opensearch "GitHub - opensearch-project/OpenSearch: 🔎 Open source distributed and RESTful search engine. · GitHub"
[20]: https://neo4j.com/open-core-and-neo4j/ "https://neo4j.com/open-core-and-neo4j/"
[21]: https://huggingface.co/BAAI/bge-m3 "https://huggingface.co/BAAI/bge-m3"
[22]: https://huggingface.co/intfloat/multilingual-e5-base "https://huggingface.co/intfloat/multilingual-e5-base"
[23]: https://huggingface.co/BAAI/bge-reranker-v2-m3 "https://huggingface.co/BAAI/bge-reranker-v2-m3"
[24]: https://github.com/tesseract-ocr/tesseract "https://github.com/tesseract-ocr/tesseract"
[25]: https://huggingface.co/facebook/nllb-200-distilled-600M "https://huggingface.co/facebook/nllb-200-distilled-600M"
[26]: https://api.ncloud-docs.com/docs/clovastudio-chatcompletionsv3-so?utm_source=chatgpt.com "Structured Outputs"
