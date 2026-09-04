# 금융상품 Agent 기술 제안서

상태: 팀 검토 전 초안 v0.6 · 2026-08-24 공식 데이터 기준 재작성

기준일: 2026-09-01

이 문서는 최종 PDF 또는 발표자료를 만들기 위한 내용 정본이다. 구현되지 않은
기능과 외부 확인이 필요한 항목은 완료된 기능처럼 표현하지 않는다.

### 권장 목차 대응

주최 측 권장 목차와 이 문서의 절 번호는 다음과 같이 대응한다.

| 권장 목차 | 이 문서 |
| --- | --- |
| 01 전체 아키텍처 및 특화기능 | §1 제안 요약 · **§1.1 특화기능과 차별화 포인트** |
| 02 제안 방법 (세 질문) | §3 — 수집·정제 §3.1.1~3.1.2, 온톨로지 §3.1.4, 검색 방법론 §3.1.5 |
| 03 시스템 구성도 (적재→색인→검색→생성) | §4 |
| 04 주요 기능 흐름도 (수집→온톨로지→그래프→검색) | §5 |
| 05 기대효과·확장성 | §7 |
| 06 부록 (온톨로지 스키마·평가 재현 절차) | **§11** |

## 1. 제안 요약

서로 다른 스키마와 품질 특성을 가진 국내채권·국내 ETF·해외 ETF·공모펀드
데이터를 하나의 검증 가능한 금융상품 Agent로 연결한다.

제안하는 **Evidence-Compiled Hybrid SQL Agent**는 LLM이 자연어를 제한된
QueryPlan으로 변환하고 결과를 설명하게 하되, 상품 필터링·정렬·비교·집계와
수치 검증은 Python·SQLite의 결정론적 도구가 담당한다. 최종 답변은 상품별
field-level evidence와 기준일을 갖추며, 검증에 실패하면 근거 없는 생성을
중단하고 결정론적 답변 또는 역질문으로 전환한다.

발행사·운용사·기초지수 등을 묻는 제공 데이터 관계 질문은 공개 질문
경로에 연결했다. 단, 현재 증거는 manifest에 해시로 고정된 릴리스 계약과 clean Docker에서
수행한 `local implementation verified`이며, NCP 실제 배포나 공식 정확도를 입증한
결과는 아니다.

### 1.1 특화기능과 차별화 포인트

주최 측 설명회는 온톨로지를 문서가 아니라 AI의 Semantic Layer로 제시하고,
스키마 설명만 프롬프트에 넣은 경우보다 업무 의미를 런타임에 결합한 경우
정확도가 크게 올랐다는 자체 실험을 공개했다.

이 제안의 차별점은 그 결합을 **설명이 아니라 실행 권한으로** 구현했다는 데 있다.

이 설계는 우리만의 주장이 아니다. dbt Labs가 2026년에 공개한 대조 실험에서
시맨틱 레이어를 붙이자 Claude Sonnet 4.6은 90.0%에서 98.2%로, GPT-5.3-Codex는
84.1%에서 100%로 올랐다. 모델을 바꾸지 않고 의미 계층만 더한 결과다.

#### 1) 온톨로지가 런타임 통제 계층이다

`field_registry.yaml`(schema_version 1.5, field 92개)이 정본이고 Turtle 5개는
여기서 생성된다. 이 파일은 문서가 아니라 실행 시점에 다음을 확정한다.

- 어떤 field가 질의·정렬·집계 가능한가 (`queryable`·`sortable`·`aggregatable`)
- field마다 허용되는 연산자는 무엇인가 (`allowed_operators`)
- 값의 품질 등급과 커버리지는 얼마인가 (`quality`·`coverage_pct`)
- 어떤 표현이 그 field를 가리키는가 (`label`·`aliases`)

registry에 없는 field·연산자·정렬은 실행 계획 단계에서 만들어질 수 없다.
온톨로지를 바꾸면 답변 가능 범위가 바뀐다.

반대 방향도 성립해야 한다. registry가 선언한 표현은 반드시 답이 되어야 한다.
2026-09-01에 선언된 표면형 140건을 전수 검사해 이를 확인했고, 실패 36건을
25.7%에서 2.1%로 낮췄다. 정렬 패턴은 이제 registry에서 생성되므로 별칭을
추가하는 것만으로 동작한다. 회귀는 registry를 읽어 테스트를 만드는 방식으로
고정했다.

#### 2) LLM은 계획을 제안할 뿐 실행을 바꾸지 못한다

HyperCLOVA X는 의도 분석과 최종 문장 작성을 맡는다. 그러나 서버가 만든
Typed QueryPlan의 field·filter·정렬 방향·limit은 **변경할 수 없다.**
LLM 출력은 서버 계획과 exact-match gate로 대조되고, 어긋나면 LLM 쪽이 버려진다.

수치·순위·집계는 SQLite Oracle이 계산하고 독립 Verifier가 재계산해 대조한다.
생성 모델이 숫자를 만들어내는 경로가 구조적으로 없다.

#### 3) 데이터 신뢰의 기준점이 해시로 고정되어 있다

`approved_dataset_manifest.json`이 네 상품군 DB의 SHA-256을 담는다. 실행 시점의
DB가 이 지문과 다르면 질문 API가 503으로 멈춘다. 조용히 다른 데이터로 답하는
경우가 없다는 뜻이다.

#### 4) 답할 수 없음을 상태로 구분한다

승인되지 않은 관계를 요구하는 질문(예: 특정 종목을 편입한 ETF)은 실행 전에
차단된다. 근거를 찾지 못하면 `CANNOT_VERIFY`, 실행 범위 밖이면 `UNSUPPORTED`,
조건이 모자라면 역질문으로 끝난다. 이 셋은 실패가 아니라 설계된 종료 상태다.

주최 측 평가에 답변 불가 문항이 포함된다는 점에서, 근거 없는 답을 만들지 않는
것 자체가 득점 요소다.

#### 5) 모든 수치에 field 단위 근거와 기준일이 붙는다

답변의 각 수치는 어느 상품의 어느 field에서 왔는지, 정적·동적 기준일이
언제인지를 함께 제시한다. 외부 근거를 쓴 경우 출처·게시일·수집일·본문 해시를
보존한다.

## 2. 문제 정의

금융상품 검색은 단순 키워드 일치로 해결하기 어렵다.

- 네 상품군의 logical grain과 필드 의미가 서로 다름
- 같은 `수익률`, `규모`, `위험` 표현도 상품군별 산식·단위·기준일이 다름
- 결측, sentinel, 오래된 동적 값과 손상 행을 정상값처럼 처리할 위험이 있음
- 자연어 질문에는 상품군·조건·비교 기준이 생략되거나 서로 충돌할 수 있음
- 생성 모델이 데이터에 없는 수치·전망·추천을 만들면 금융 답변의 신뢰성이 훼손됨

따라서 목표는 “자연스러운 문장 생성”만이 아니라 다음을 동시에 만족하는 것이다.

1. 질문을 실행 가능한 조건과 실행할 수 없는 요구로 분리
2. 대규모 정형 데이터를 재현 가능하게 검색·연산
3. 모든 상품·수치·순위·기준일을 원천 근거로 추적
4. 정보 부족·결과 없음·금지 요청을 안전하게 처리

## 3. 제안 방법

### 3.1 데이터 구조화

- 원천 파일과 schema 파일의 hash·행 수·필드 품질을 감사
- 상품군별 logical grain, canonical field와 source mapping을 registry로 관리
- 손상 행과 실행 불가능한 값을 격리하고 원천 row·column·기준일 보존
- 국내채권·국내 ETP·해외 ETP·공모펀드를 상품군별 SQLite로 정규화

### 3.1.1 데이터 수집 방법

| 구분 | 출처 | 수집 방식 | 범위 |
| --- | --- | --- | --- |
| 공식 상품 데이터 | 주최 측 배포 XLSX 8개 | Discord 공지 배포본을 그대로 보관 | 2026-08-24 배포본. 국내 2026-08-22, 해외 2026-08-23 반영 |
| 외부 근거 | 승인 출처 14곳 | 공식 도메인 connector로 수집 | `official-external-sources-20260824-v5`, 수집 상한 2026-08-24 |

승인 출처는 발행 주체가 확인되는 공식 도메인으로 제한한다. 한국거래소 KIND,
금융위원회, 그리고 삼성·미래에셋·KB·신한자산운용과 KraneShares 등 운용사·발행사의
공식 페이지다. 임의 크롤링이나 출처 불명 게시물은 반입하지 않는다.

원천 XLSX는 저장소에 포함하지 않는다. 대신 파일별 SHA-256과 크기를 승인 manifest에
고정하고, 실행 시점에 실제 파일과 대조해 불일치하면 기동을 중단한다.

### 3.1.2 수집·적재 Flow

```text
공식 XLSX 8개 (읽기 전용)
  → 파일 SHA-256·크기 대조            불일치 시 중단
  → 상품군별 판독·정규화               field registry가 canonical field와 source column을 결정
  → 행 단위 품질 판정                  VALID·PARTIAL·UNKNOWN·INVALID·STALE·UNSUPPORTED
  → 실행 불가 행 격리(quarantine)      원천 row·column·기준일은 보존
  → 상품군별 SQLite 4개 생성
  → DB SHA-256을 승인 manifest와 대조   불일치 시 중단
  → 승인 DB에서 관계 색인(FTS5) 생성
  → 관계 artifact 해시를 승인 manifest에 결속
  → Backend 기동
```

각 단계는 fail-closed다. 어느 하나라도 해시가 어긋나면 다음 단계로 넘어가지 않고,
기동 후 파일이 바뀌어도 `/health`와 질문 API가 함께 HTTP 503으로 전환된다.

적재 결과는 상품군별 manifest sidecar에 원천 파일명·SHA-256·기준일·전체 행 수·
검색 대상 행 수·격리 행 수로 남긴다.

| 상품군 | 전체 행 | 검색 대상 | 격리 |
| --- | ---: | ---: | ---: |
| 국내채권 | 21,882 | 21,882 | 0 |
| 국내 ETF·ETN | 1,780 | 1,777 | 3 |
| 해외 ETF·ETN | 6,037 | 6,023 | 14 |
| 공모펀드 | 23,676 | 14,606 | 116 |

공모펀드는 원천이 클래스 단위라 논리 상품 23,560건·속성 23,560건으로 재구성하고,
계좌 단위 등 실행 대상이 아닌 8,954행을 범위에서 제외한다.

### 3.1.3 데이터 활용 로직

**결측과 0의 처리.** 주최 측은 `0`과 빈 값이 의도된 값이며 임의 보정을 금지했고,
2026-08-26 답변에서 해당 컬럼을 묻는 질의에서는 값이 0인 행을 결과에 포함하지 않는
것이 맞다고 확인했다. 이를 저장과 조회로 나눠 구현한다.

- 저장: 원천값을 그대로 보존하고 보간하지 않는다
- 조회: 필터·정렬 대상 컬럼에 `IS NOT NULL AND <> 0`을 적용해 기준이 없는 행을 제외한다
- 답변: 값이 없으면 `확인 불가`로 명시하고 추정하지 않는다

**필드 권한.** 질문에서 읽어낸 조건은 field registry에 등록된 canonical field로만
변환한다. 등록되지 않은 조건은 실행하지 않고 역질문하거나 미지원으로 답한다.
LLM은 이 결정을 바꿀 수 없다.

**기준일.** 정적 필드와 동적 지표의 기준일이 다르므로 답변에 사용한 기준일을 함께
제시하고, 기준일이 호환되지 않는 두 값의 차이는 계산하지 않는다.

**외부 근거.** 승인 출처에서 수집한 문서는 URL·게시일·수집일·본문 해시를 함께
보존하고, 2026-08-24 상한을 넘는 자료는 사용하지 않는다. 외부에서 찾은 상품이
공식 제공 DB에 없으면 `NOT_FOUND`, 근거를 끝까지 검증하지 못하면 `CANNOT_VERIFY`로
답한다. 공식 데이터와 충돌하면 공식 데이터를 우선한다.

**결측 보완 범위.** 현재 구현은 제공 데이터의 결측을 외부 자료로 채우지 않는다.
결측은 검색에서 제외하거나 값이 제공되지 않았다고 답변하는 방식만 사용한다.
이는 공식 데이터와의 충돌 위험을 없애기 위한 선택이며, 결측률을 낮추는 보완은
적용 범위와 출처를 분리해 기록할 수 있을 때 도입한다.

### 3.1.4 온톨로지 엔지니어링

**온톨로지를 문서가 아니라 실행 통제 계층으로 쓴다.** 제출하는 Turtle 5종은
설명을 위해 따로 작성한 파일이 아니라, 런타임이 실제로 참조하는 field registry를
직렬화한 결과다. 둘의 동일성은 `sync-ontology.py`가 검사하며 어긋나면 실패한다.

```
field_registry.yaml  (schema_version 1.5)
  ├─ 런타임: 실행 권한·필터 field·연산자·정렬·단위·품질 판정
  └─ 직렬화: ontology/{common,bond_kr,etf_kr,etf_gl,fund_pub}.ttl
                (ga:registryVersion "1.5" 로 버전 일치를 선언)
```

같은 정의가 두 곳에 존재하면 반드시 어긋난다. 정본을 하나로 두었기 때문에
제출한 온톨로지는 곧 실행되는 규칙이다.

**무엇을 선언하는가.** 공통 `common.ttl`이 `FinancialProduct`·`ProductFamily`·
`Dataset`·`CanonicalField` 상위 개념과 관계 property를 정의하고, 상품군별 파일이
이를 `owl:imports`로 상속해 각 상품군의 canonical field를 선언한다. field마다
다음을 함께 담는다.

| 선언 | 역할 |
| --- | --- |
| `ga:canonicalName` | 실행에 사용하는 유일한 이름 |
| `rdfs:label`, `skos:altLabel` | 한국어·영어 동의어. 질의어를 field로 연결 |
| `ga:valueType`, `ga:unit` | 값 타입과 단위. 단위가 다르면 비교·합산 차단 |
| `ga:quality`, `ga:coveragePct` | 결측·부분 품질. 검증기가 사용 |
| `ga:queryable`, `ga:selectable` | 필터·정렬에 쓸 수 있는지 |

**왜 이 방식인가.** 원천 컬럼명은 `cu_charge_rt`처럼 의미가 드러나지 않고,
같은 개념이 상품군마다 다른 컬럼에 있다. 사용자는 `총보수`·`보수율`·`연간보수`를
섞어 쓴다. 이 셋을 잇는 층이 없으면 모델이 컬럼을 직접 고르게 되고, 문법은 맞지만
값이 틀린 질의가 만들어진다. 온톨로지가 그 선택을 대신 확정한다.

**데이터 무결성 규칙도 함께 담는다.** 신용등급은 enum과 최고→최저 순서를 선언해
`AA- 이상` 같은 표현을 허용 목록으로 확장하고, 임계값이 없는 `등급이 높은`은
추정하지 않는다. 값이 `0`이거나 비어 있으면 정렬·필터 기준이 없다고 보고 결과에서
제외한다.

### 3.1.5 검색 방법론 선택과 근거

**한 가지 검색으로는 답할 수 없는 질문이 있다.** 평가 질의는 성격이 섞여 있다.

| 질문 성격 | 필요한 것 | 우리 구성요소 |
| --- | --- | --- |
| 숫자 조건·정렬·집계 | 정확한 필터와 순서 | SQLite 결정론적 Oracle |
| 정확한 상품·발행사 이름 | 정확 일치 | SQLite FTS5 정확 색인 |
| 질의어와 field 연결 | 의미 유사 | Schema Dense (후보 제안 전용) |
| 기업 관계·구성종목·테마 | 관계 순회 | 관계 색인과 공식 외부 근거 |
| 문서 근거 | 출처 있는 서술 | 승인 출처 14곳의 공식 connector |

그래서 질문을 성격 단위로 나누고 각 조각을 알맞은 저장소로 보낸 뒤 근거를 합친다.

```
질문 → 의도 분석 → 온톨로지 접지 → 계획·라우팅
        ├─ SQLite : 숫자 조건·정렬·집계
        ├─ 관계 색인·외부 근거 : 발행사·기업 관계·테마
        └─ Schema Dense : field 후보 제안
      → 통합·검증 → 근거 → 최종 답변
```

**설계 원칙 두 가지.**

첫째, **제안과 확정을 분리한다.** Dense와 LLM은 후보 field를 제안할 뿐이고
hard filter·상품군·정렬 방향·limit·SQL 결과를 바꿀 권한이 없다. 서버가 registry로
다시 확정한다. 검색을 넓히면 회수율은 오르지만 근거 없는 실행도 함께 늘기 때문이다.

둘째, **확정하지 못하면 실행하지 않는다.** 질의어를 하나의 field로 좁히지 못하거나
근거를 끝까지 검증하지 못하면 결과를 만들지 않고 확인 불가를 밝히거나 필요한 조건을
역질문한다. 조건을 조용히 버리고 그럴듯한 목록을 반환하는 것이 가장 나쁜 실패다.

**한계.** 기업 단위 관계는 제공 데이터에 없어 공식 출처에서 수집한 근거에 의존한다.
출처·게시일·수집일·본문 해시를 보존하고 2026-08-24 상한을 적용하며, 외부에서 찾은
상품이 제공 DB에 없으면 `NOT_FOUND`, 근거를 끝까지 검증하지 못하면 `CANNOT_VERIFY`로
답한다. 현재 이 경로의 coverage는 확대 대상이다.

### 3.2 질의 이해와 실행 계약

- fail-closed Intent Router가 SEARCH·COMPARE·AGGREGATE·EXPLAIN·CLARIFY를 구분
- 서버 compiler가 상품군 capability, 필드, 연산자, 정렬과 limit의 기준계획 생성
- 복수 상품군 SEARCH는 상품군별 단일 QueryPlan으로 나눠 독립 실행
- LLM을 사용하는 SEARCH의 출력은 축소된 schema의 typed QueryPlan으로 제한
- SEARCH 모델 계획과 서버 기준계획이 다르면 Oracle을 실행하지 않고 역질문
- COMPARE·AGGREGATE는 현재 서버의 결정론적 compiler를 유지

### 3.3 결정론적 검색·연산

- parameterized SQLite Oracle이 필터·정렬·순위와 후보 수 계산
- 복수 상품군 SEARCH는 Oracle·Verifier를 병렬 실행하고 부분 결과를 보존
- 비교는 같은 상품군의 정확한 두 상품과 승인 필드에 한정
- 집계는 Decimal 기반 COUNT·MIN·MAX·AVG와 제한된 SUM을 지원
- 통화·단위·기준일이 호환되지 않으면 차이 또는 합산을 차단
- 제공 데이터의 발행사·운용사·기초지수·자산유형·투자지역은 별도 SQLite FTS5
  관계 색인으로 구성하고, 후보 상품 ID를 공식 상품 DB에서 다시 확인
- 현재 승인 DB에서 관계 37,793개와 검색 smoke 4/4를 검증하고, 관계·문서 전용
  Typed Plan과 Claim Verifier를 내부 경로에 연결
- P0-10에서 안전 검사 → 결정론적 관계 Router → manifest에 해시로 고정된 public release 결속 →
  exact FTS 후보 → canonical 전체 일치 → 공식 DB identity 재검증 순서로 공개
  Router·Backend adapter·`GET /answer`에 연결
- 일부 토큰만 맞는 표현은 유사 결과를 추측하지 않고 `not_found`로 종료
- 금융 alias·관계 의미 검수와 독립 blind 성능은 아직 확정하지 않음

### 3.4 근거와 답변 검증

- 독립 Result Verifier가 Oracle 결과를 별도 Python 경로로 재계산
- field-level evidence가 값·품질·원천·기준일을 보존
- LLM은 상품군별 질문·계획·검증된 evidence만 입력받아 설명 초안을 생성
- 서버가 상품군별 답변 섹션을 조합하고, 교차 상품군 문구·비교·집계를 다시 검증
- Answer Verifier가 상품명·수치·순위·근거·기준일을 다시 확인
- 관계·문서 답변에서는 모델 주장을 evidence의 개수·순서·값·참조와 정확히 대조하고,
  자유 수치·요약 필드를 허용하지 않음
- 현재 공개 관계 경로는 HyperCLOVA X claim provider를 사용하지 않고 검증된
  field evidence만으로 결정론적 답변을 생성
- Router 판단, release·artifact 검증, SQL 검색, 주장 검증, 최종 결과를 인과
  순서가 역전될 수 없는 Audit event로 남김
- 하나라도 검증에 실패하면 전체 답변을 deterministic fallback으로 전환하며, 데이터 부족이면 역질문, 금지 요청이면 거절

### 3.5 문서 설명

BM25/SQLite FTS 기반 문서 검색의 적재·필터·출처·기준일 계약을 구현했다.
외부 문서는 금융·데이터 권한 독립 review, HTTPS 출처, 사용 권한 4종,
byte·정규화 본문 SHA-256, canonical manifest, 변조·경로·덮어쓰기 차단을 통과해야
별도 BM25 색인으로 build된다. 이 반입 계약은 합성 문서 24/24를 통과했지만,
실제 투자설명서·약관·용어집은 출처·사용 권한·검색 품질을 확인한 뒤에만
Release에 연결한다.

## 4. 시스템 구성도

[시스템 구성도 정본](diagrams/system-architecture.md)은 다음 두 범위를 분리한다.

- 현재 검증 완료: Agent Core, SQLite Oracle, Verifier, evidence, Backend DTO,
  FastAPI 내부 `POST /answer`, 공식 `GET /answer` 계약, Ontology Turtle 5개,
  Docker 데이터 준비·HTTP smoke
- 교차 상품군 SEARCH의 family별 근거 격리·답변 검증·전체 fallback
- 승인 상품 DB의 제공 관계 37,793개 색인·공식 상품 ID 재검증·출처 추적
- 관계 Typed Plan·정확 일치·manifest에 해시로 고정된 public release 계약·공식 DB identity 재검증·
  field evidence·결정론적 답변·인과적 Audit의 P0-10 로컬 공개 경로
- 외부 통합 대기: Next.js, NCP의 실제 서명 배포·공인 IP,
  서명된 두 release rollback, 관계 HyperCLOVA X claim provider, 공개 API 운영

목표 구조를 현재 구현 완료 상태로 오해하지 않도록 실선과 점선으로 구분한다.

## 5. 주요 기능 흐름도

[답변 기능 흐름도](diagrams/answer-flow.md)를 정본으로 사용한다.

| 기능 | 현재 범위 | 안전 경계 |
| --- | --- | --- |
| SEARCH | 네 상품군 조건 검색·상세 조회, 복수 상품군 독립 검색과 family별 grounded answer | 공통 조건만 상품군별 실행·근거를 family별로 격리·직접 비교 금지 |
| COMPARE | 같은 상품군의 정확한 두 상품 | 통화·기준일·결측 불일치 차단 |
| AGGREGATE | 네 상품군 COUNT·MIN·MAX·AVG·제한 SUM | 독립 Python 재검산 |
| RELATION SEARCH | 발행사·운용사·기초지수·자산유형·투자지역의 제공 관계 조회 | exact FTS 후보를 canonical 전체 일치와 공식 DB identity로 재검증·부분 표현은 `not_found` |
| EXPLAIN | 정형 evidence 설명, 문서 RAG·승인 반입 최소 계약 | 실제 corpus 출처·권한·검색 평가 대기 |
| CLARIFY | 상품군·식별자·기준 누락 | Oracle·LLM 불필요 호출 차단 |
| UNSUPPORTED | 전망·수익 보장·단정 추천 | 근거 없는 생성 금지 |

상품군 간 직접 수치 비교, 세 상품 이상 비교, 환율 환산과 우열·추천 판단은
현재 실행 범위가 아니다.

## 6. 사용자 시나리오

[사용자 시나리오 정본](user-scenarios.md)은 다음 상황을 포함한다.

1. 해외 ETF 조건 검색과 근거 확인
2. 국내채권 상세 조회와 오래된 동적 값 경고
3. 국내 ETF 또는 공모펀드의 조건 집계
4. 같은 상품군 두 상품 비교
5. 국내·해외 ETP 교차 검색과 family별 근거 답변
6. 발행사·운용사를 기준으로 한 제공 데이터 관계 검색과 필드별 citation
7. 모호한 조건 역질문과 단정적 추천 거절

최종 화면 시나리오는 Next.js 통합 후 실제 캡처와 API 응답으로 교체한다.

## 7. 기대효과·확장성

### 7.1 현업 적용 지점

이 Agent가 대체하는 것은 "검색"이 아니라 **답변에 근거를 붙이는 사람의 시간**이다.

| 업무 | 지금 방식 | 이 Agent를 붙였을 때 |
| --- | --- | --- |
| 상품 상담 | 담당자가 여러 화면을 조회해 조건에 맞는 상품을 추린 뒤 수치를 옮겨 적는다 | 조건 검색·정렬·집계를 실행하고 상품별로 어느 field·어느 기준일에서 온 값인지 함께 제시한다 |
| 상품 기획·리서치 | 상품군마다 산식과 단위가 달라 비교 전 정규화를 반복한다 | field 92개의 단위·품질·커버리지가 registry에 고정되어 있어 같은 기준으로 비교한다 |
| 컴플라이언스 | 응대 문구에 추천·전망이 섞였는지 사후 점검한다 | 전망·수익 보장·단정적 추천은 실행 전에 `UNSUPPORTED`로 차단된다 |
| 신규 상품 온보딩 | 검색 화면과 질의 로직을 개별 수정한다 | registry에 field와 표현을 등록하면 질의·정렬·집계 경로가 그대로 동작한다 |

마지막 항목은 주장이 아니라 2026-09-01에 확인한 동작이다. registry가 선언한
표면형 140건으로 질의를 만들어 전수 검사했고, 별칭을 추가하는 것만으로 정렬과
임계값 조건이 동작하도록 실행 경로를 registry에서 파생시켰다.

### 7.2 금융 답변에서 무엇이 줄어드는가

금융 답변의 위험은 "틀린 답"보다 **틀렸는지 알 수 없는 답**이다. 이 구조는 그
경우를 상태로 분리한다.

- 근거를 찾지 못하면 `CANNOT_VERIFY`, 실행 범위 밖이면 `UNSUPPORTED`, 조건이
  모자라면 역질문으로 끝난다. 셋 다 설계된 종료 상태다
- 승인 DB의 SHA-256이 실행 시점과 다르면 질문 API가 503으로 멈춘다. 조용히
  다른 데이터로 답하는 경우가 없다
- 수치·순위·집계는 SQLite가 계산하고 독립 Verifier가 **Python으로 다시 계산해**
  대조한다. 두 구현이 어긋나면 답변이 나가지 않는다

세 번째는 실제로 작동한다. 2026-09-01에 집계 SQL만 고치고 Python 쪽을 빠뜨리자
`aggregate candidate_count mismatch: execution=5, Python=6`으로 즉시 막혔다.
한쪽만 고치면 틀린 답이 나가는 대신 답이 막힌다.

### 7.3 상품군 확장 방안

새 상품군을 붙일 때 **바꾸는 것과 그대로 두는 것**이 분리되어 있다.

바꾸는 것은 다섯 가지다.

1. 원천 스키마를 읽는 normalization adapter. 결측·sentinel·0의 의미를 여기서
   정하고 field별 품질 등급으로 남긴다
2. `field_registry.yaml`에 field 등록. 질의·정렬·집계 가능 여부, 허용 연산자,
   단위, 커버리지, 그 field를 가리키는 표현을 함께 적는다
3. `sync-ontology.py`로 Turtle 생성. registry와 어긋나면 검사가 실패한다
4. SQL 컬럼 매핑과 **Verifier의 Python 미러**. 이 둘은 반드시 함께 바꾼다
5. 승인 manifest에 DB SHA-256 등록

그대로 두는 것은 Router, QueryPlan 계약, Compiler, Oracle 골격, Verifier 뼈대,
Renderer, 그리고 평가 API의 다섯 필드 계약이다. 질의 이해와 안전 경계를 다시
만들 필요가 없다.

현재 네 상품군이 이 절차로 붙어 있다. field는 국내 ETF·ETN 42, 공모펀드 39,
국내채권 34, 해외 ETF·ETN 29개다.

### 7.4 데이터가 넓어질 때

외부 근거는 출처를 승인 목록으로 잠근 상태에서만 쓴다. 승인 출처 24곳이
capability별로 등록되어 있고, 출처·게시일·수집일·본문 해시를 보존하며
2026-08-24 상한을 지킨다.

넓히는 방법은 출처를 추가하는 것이지 모델을 바꾸는 것이 아니다. 다만 커버리지를
실측한 결과 국내 ETF·ETN은 77.3%, 국내채권 32.0%, 해외 ETF·ETN 0.6%다. 해외는
운용사 382곳이 6,025개를 운용해 상위 20곳을 더해도 55.9%에 그친다. 그래서 근거가
부실한 답을 만드는 대신 `CANNOT_VERIFY`로 끝내는 편을 택했다.

이 판단 자체가 확장 방침이다. **덮는 범위를 넓히는 것과 답할 수 있다고 말하는
것은 다르다.**

### 7.5 확장성의 한계를 명시한다

확장성은 "어떤 질문도 처리한다"는 뜻이 아니다. 새 필드·상품군·문서는 의미,
단위, 기준일과 검증 방법이 승인된 뒤 capability matrix에 추가한다. 승인되지
않은 관계를 묻는 질문은 데이터가 생기기 전까지 차단된 상태로 남는다.

## 8. 기술완성도·성능·정확성 근거

정량 주장과 해석 제한은 [제안서 근거 맵](evidence-map.md)을 따름
행동 기능 검사·구조화 출력 제약·단계별 평가·통계 비교의 출처와 실제 차용 범위는
[연구 근거](research-basis.md)에 분리해 기록

현재 대표 근거:

- 4종 원천 53,375행 감사, 핵심 expectation 55/55 (2026-08-24 공식 데이터)
- Agent Core 2,232 passed·2 skipped, Backend 466 passed (2026-09-01 `e58896f`)
- 공식 XLSX에서 SQLite 4개를 자동 생성·검증한 뒤 Backend를 시작하는 Docker 경로 완료
- 승인 상품 DB 관계 37,793개·관계 계약 14/14·실제 검색 smoke 4/4와 공식 상품 ID 재검증 완료
- P0-7 관계·문서 계획·주장 검증 계약 22/22, 승인 관계 전체 경로 smoke 4/4
- P0-10 집중 회귀 522/522, clean Docker Backend smoke
  8/8·공식 형식 GET 호환 smoke 8/8, 관계 질문 3 products/3 citations, 부분 표현 `not_found`,
  변조 후 health·API 503 ([machine-readable baseline](../../src/finance_agent/evaluation/baselines/p0-10-public-relation-release-integration-v1.json)·[P0-10 인수인계](../../src/finance_agent/docs/p0-10-public-relation-release-integration-handover-2026-08-20.md))
- 국내·해외 ETP 교차 SEARCH 공개 실제 데이터 회귀 4/4
- 국내·해외 ETP 교차 상품군 grounded answer 공개 회귀 4/4, 생성 대상 2문항 모두
  grounded, fallback 0
- 내부 red-team 40문항 수정 후 strict·safety·evidence 40/40
- 변형 질문으로 기존 `낮은 순`·`짧은 순` gold 오류 2건을 발견하고 원본을
  바꾸지 않는 hash-pinned 교정 overlay로 평가 정답 자체도 감사
- 실재하지만 원문에 없는 상품 ID, 부정된 ID·필드·정렬, 서버 조건 누락,
  malformed provider 응답이 실행 권한을 얻지 못하도록 공격 회귀로 고정
- registry와 실제 DB에서 대표 기능 좌표 305개를 자동 구성하고 정답 계획
  299개를 직접 실행해 검색·계산·근거 엔진의 넓은 실행 범위를 확인
- 같은 299개 계획의 canonical 자연어 최초 strict 37/299를 보존하고,
  질문 분류 45건·작업 계획 210건·근거 7건으로 실패 시작점을 분해해
  공통 문법 개선의 우선순위를 확보
- 공식 예상 분포 공개 모의평가에서 계약 30/30, 답변 불가 5/5 안전 처리
- 같은 30문항의 실제 Docker GET에서 공식 형식·60초 30/30
- 금융 도메인 개발 QA 40문항 Router 사후 회귀 40/40,
  잘못된 control 실행·오류 0건
- Backend service adapter 오류·fallback·비노출 계약 12/12
- 새 Docker 이미지의 Backend 7건·공식 GET 정상/예외 7건 14/14
- 평가·production mode에서 HyperCLOVA X 외 provider가 선택될 수 없음을
  시작 단계 거부와 이미지 경계 검사로 이중 확인
- 설명회 현장 자료에서 평가 API 다섯 문자열 필드·미응답 HTTP 200·60초 권장과
  도메인별 Ontology 제출 요구 확인

### HyperCLOVA X 실행 근거

평가·제출 경로의 생성은 HyperCLOVA X만 사용한다. 아래는 HCX-007 실호출 결과다.

- 2026-08-29 All-ON 300문항 실행에서 HCX Intent 304회, grounded answer 298회,
  총 480,016 token을 사용하고 HTTP non-200 2건을 기록
- 같은 실행의 상태 정확도 93.33%, 상품 micro F1 73.93%, 상태+상품 순서 결과
  일치 69.00%, p50 3.763초·p95 7.038초
- 실제 복수 해석 40문항 전부 역질문, 답변 불가·정책 30문항 중 29문항 상태 일치
- 2026-08-31 공개 endpoint에서 공식 `GET /answer`가 다섯 문자열과
  `answer_mode=llm_grounded`로 2.9~4.4초에 응답

**해석 제한.** 위 300문항 수치는 Gold를 공개한 뒤 일반화 가능한 병목을 수정하고
같은 문항을 다시 실행한 post-Gold development regression이며 독립 blind가 아니다.
같은 Gold의 최초 blind 실행은 상태 정확도 75.67%·상품 micro F1 56.78%였다.
2026-08-31에 수정한 병목은 아직 재측정하지 않았다.

내부 공개 평가의 100%는 배선·회귀 안정성이지 독립 blind 일반화 성능이 아니다.
P0-10 수치도 로컬 구현·계약 검증이며 NCP 실제 배포, 서명된 두 release
rollback, 관계 HyperCLOVA X 답변 품질을 의미하지 않는다.

## 9. 현업 활용성과 리스크 관리

- 답변마다 출처와 기준일 표시
- 공식 데이터와 외부 데이터 충돌 시 공식 데이터 우선
- 확인 불가능한 조건은 추정하지 않고 확인 불가 또는 역질문
- 수익률 전망·수익 보장·단정적 투자 추천 금지
- provider·dataset 장애는 evidence 없는 안전한 오류 DTO로 변환
- 관계 artifact·DB의 경로·크기·수정 상태가 릴리스 검증 시점과 달라지면
  health·질문 API를 503으로 중단
- 실제 사람 평가는 금융 도메인 담당자와 제품 담당자가 독립 rubric으로 수행

## 10. 완료 전 게이트

- 금융 도메인 담당자의 external blind와 비공개 정답키
- 최소 2명의 독립 reviewer가 수행한 사람 평가
- 허용된 실제 비정형 문서 corpus와 사용 범위
- HyperCLOVA X 모델·endpoint·인증·Structured Outputs 확인과 실제 재현
- NCP에 서명된 release 실제 배포·공인 IP `GET /answer`·`/health` 재현
- 서명된 두 release 간 forward·rollback drill과 Audit receipt 보존
- 관계 답변용 HyperCLOVA X claim provider 결속·주장 검증·장애 fallback
- 금융 alias·관계 의미의 독립 blind 평가
- Ontology 용어의 금융 도메인 검수와 주최 측 최종 형식 확인
- `think_trace`의 구조화 실행 기록에 대한 세부 평가 방식 확인
- 크레딧 승인과 HyperCLOVA X 적용 서비스 확인

## 11. 부록

### 11.1 온톨로지 스키마

정본은 `field_registry.yaml`(schema_version 1.5)이고, Turtle 5개는 여기서
생성된다. `src/finance_agent/scripts/sync-ontology.py --check`가 둘의 일치를 검사하며
어긋나면 실패한다.

| 파일 | 줄 수 | 범위 |
| --- | --- | --- |
| `ontology/common.ttl` | 193 | 공통 어휘. class 4개, datatype property 38개, object property 3개 |
| `ontology/bond_kr.ttl` | 1,121 | 국내채권 |
| `ontology/etf_kr.ttl` | 1,430 | 국내 ETF·ETN |
| `ontology/etf_gl.ttl` | 1,009 | 해외 ETF·ETN |
| `ontology/fund_pub.ttl` | 1,313 | 공모펀드 |

상품군 Turtle은 `owl:imports`로 `common`을 가져오고, `ga:ProductFamily`에
`ga:hasCanonicalField`로 그 상품군이 실제로 답할 수 있는 field를 나열한다.
`ga:registryVersion`이 registry 판번호를 고정한다.

field 92개의 분포는 다음과 같다.

| 상품군 | field 수 |
| --- | --- |
| 국내 ETF·ETN | 42 |
| 공모펀드 | 39 |
| 국내채권 | 34 |
| 해외 ETF·ETN | 29 |

품질 등급은 `VALID` 31개, `PARTIAL` 59개, `UNKNOWN` 2개다. `PARTIAL`은 원천
데이터에 결측이 있는 field로, 커버리지를 `coverage_pct`에 기록하고 답변에서
그 사실을 숨기지 않는다.

### 11.2 평가 재현 절차

#### 실행

```bash
./compose.sh up --detach --wait
```

이 한 명령이 이미지 빌드, 공식 XLSX에서 네 상품군 SQLite 생성, 관계 artifact와
SHA sidecar 생성, 승인 manifest 대조, Backend 기동, 정상 상태 대기를 수행한다.
원천 데이터 경로 기본값은 `2. Data/1. Raw/2.금융상품_20260824`이며
`src/fastapi_backend/.env`의 `FINANCE_RAW_DATA_DIR`로 바꾼다.

#### 질의

```bash
curl --get "http://{endpoint}/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=국내 ETF를 순자산 큰 순으로 5개 보여줘"
```

응답 형식은 [API 명세서](../submission/api-specification.md)를 따른다.

#### 테스트

```bash
umask 022 && python -m pytest src/finance_agent/packages/finance_agent_core -q
umask 022 && PYTHONPATH=. python -m pytest fastapi_backend -q
```

`umask 022`가 필요하다. 서버 기본값 `umask 002`에서는 릴리스 보안 검사가 테스트
fixture를 group-writable로 판정해 실패한다. Backend는 저장소 루트에서 실행해야
한다.

commit `e58896f` 기준 측정값은 Agent Core 2,232 passed·2 skipped, Backend
466 passed다. skip 2건은 봉인된 로컬 키와 선택적 승인 DB 경로 환경변수를
요구하는 opt-in 테스트다.

#### 온톨로지 일치 검사

```bash
python src/finance_agent/scripts/sync-ontology.py --check
```

Turtle 5개가 `field_registry.yaml`과 일치하면 통과한다.

#### 데이터 지문 확인

`approved_dataset_manifest.json`이 네 상품군 DB의 SHA-256을 고정한다. 실행
시점 DB가 이 값과 다르면 질문 API가 503으로 중단된다.

원천 파일 이름이 DB `metadata` 테이블의 `source_file_name`에 기록되므로,
같은 내용이라도 파일명이 다르면 지문이 달라진다. 재현 시 공식 배포본의
파일명을 그대로 사용해야 한다.
