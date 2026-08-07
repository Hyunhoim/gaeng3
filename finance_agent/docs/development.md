# 개발 환경과 현재 구현 상태

상태: 현재 정본
기준일: 2026-08-07

## 저장소 상태

- 로컬 branch: `haeyeongcho`
- upstream: `origin/haeyeongcho`
- AI Agent와 FastAPI Backend 통합 코드가 같은 branch에 있음
- 로컬 branch는 이번 검증 커밋을 포함해 upstream보다 앞서며, 이 문서는 특정
  commit ID 대신 source-freeze manifest로 검증 대상을 고정

동료가 가져올 `vintasoftware/nextjs-fastapi-template`은 저장소 루트의
`fastapi_backend`, `nextjs-frontend`, `docs`를 사용한다. AI·데이터 작업공간은
루트 충돌을 피하도록 `finance_agent/`에 격리했고, 재사용 코드는
[finance_agent_core](../packages/finance_agent_core/README.md)에서 독립적으로
개발한다. AI 문서 인덱스도 `finance_agent/docs/project-index.md`에 둔다.

## 로컬 도구

| 도구 | 확인된 버전·상태 |
| --- | --- |
| Conda | Miniforge `26.3.2`, `/home/haeyeongcho/miniforge3/bin/conda` |
| 프로젝트 Python | `gaeng3-dev`, Python `3.12.13` |
| pip | `26.1.2` |
| Node.js | NVM Node `24.18.0` |
| npm | `11.16.0` |
| Git | `2.34.1` |
| Docker | Engine `29.7.1`, Compose `5.4.0`, 일반 사용자 실행 확인 |

Conda는 Python 환경을 격리하고 pip는 Python 패키지를 설치한다. Frontend Node는 동료 템플릿과 NVM 설정을 존중하며 Conda에 중복 설치하지 않는다.

## 환경 생성

`finance_agent/` 디렉터리에서 실행한다.

```bash
/home/haeyeongcho/miniforge3/bin/conda env create -f environment.yml
```

이미 환경이 있으면 다음을 사용한다.

```bash
/home/haeyeongcho/miniforge3/bin/conda env update \
  -n gaeng3-dev \
  -f environment.yml
```

Python 개발 의존성:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m pip install -r requirements/dev.txt
```

검토하지 않은 `pip freeze`를 requirements로 사용하지 않는다. editable package를 Git URL로 직렬화하는 환경별 결과와 Conda package의 로컬 build path가 포함될 수 있기 때문이다.

## 검사 명령

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/ruff format --check .
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/ruff check .
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/pytest
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip check
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python scripts/check-docs.py
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python scripts/sync-ontology.py --check
SOURCE_DATE_EPOCH=1785283200 \
  /home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip wheel \
  --no-deps \
  --no-build-isolation \
  --wheel-dir packages/finance_agent_core/dist \
  packages/finance_agent_core
```

2026-08-07 결과:

- Agent Core pytest 370개, Backend pytest 34개 통과
- Ruff format·lint, pip dependency, ontology sync 검사 통과
- 문서 링크·인덱스·평가 baseline·suite hash 55건과 baseline 38개 통과
- 루트 `./rehearse.sh`로 새 Docker 이미지 준비, health, 기본 공모펀드 잠금,
  Backend·공식 GET 확장 smoke 14/14, 전체 회귀를 한 번에 재현
- 제출 경계 자동 검사는 개발 저장소를 `development`로 통과시키고, 로컬 모델
  흔적이 남은 현재 소스를 `submission`에서 의도적으로 차단
- 최신 source-freeze 값과 wheel 검증 결과는
  [연결 전 준비 기준](pre-hcx-readiness.md)과 source-freeze manifest에 기록

## P1 계약 구현

- [Field Registry와 QueryPlan 계약](contracts.md)
- 네 상품군 60개 canonical field와 상품군별 field capability를 Pydantic으로 검증
- 서버 QueryPlan은 추가 property, 타입·단위·enum·연산자, intent payload를 fail-closed로 검증
- HCX 전송 schema는 공식 Structured Outputs keyword subset만 사용
- registry의 queryable·sortable·selectable·aggregatable·comparable field와
  HCX enum 정합성을 테스트
- 공모펀드 QueryPlan과 전체 답변 경로는 구현했지만 Backend 기본 정책은
  `fund_execution_policy=locked`
- 팀이 검증한 개발 리허설에서만 `public_fund_v1_approved`를 명시해 실행하며,
  이는 주최 측의 공식 승인이나 제출 설정을 뜻하지 않음

## 상품 정규화·검색

- 해외 ETP: 5,646행, sparse 10행 격리, 검색 가능 5,636행
- 국내 ETP: 1,734행, 손상 Excel 1155행 격리, 검색 가능 1,733행
- 국내채권: 42,394행, 격리 없이 전 행 검색 가능, 실제 매수 가능 254행
- 공모펀드: raw 95,619행을 논리 상품 11,138개·속성 95,618개·격리 1개로
  정규화, 공모 기본 범위 11,115개
- 네 상품군은 별도 SQLite·manifest와 같은 QueryPlan, parameterized oracle,
  독립 verifier, field evidence 계약을 공유한다.
- 공모펀드 대표 Oracle은 해외·주식형·판매중·당사 판매 조건에서 후보
  1,811개와 3개월 수익률 상위 5개를 SQL과 독립 Python 검증으로 재현했다.
- 공모펀드 expected QueryPlan·Oracle 50문항은 실행 44개·안전 차단 6개로
  전체 50/50을 통과했다.
- 공모펀드 field-level evidence, 로컬 Qwen 설명, 최종 Answer Verifier와
  결정론적 폴백을 연결했다. `fund-core-50`의 expected·local provider가
  각각 50/50이며, 44개 grounded 생성·6개 안전 차단·폴백 0건이다.
- 상품명·수치·순위·evidence·기준일·warning 검증률은 모두 100%다.
- 공모펀드 전용 내부 schema와 lexical/schema linker를 구현했고 로컬 Qwen
  hybrid parser의 development 최초 실행은 40/40이다. commit `32e12fa`
  이후 최초 holdout은 9/10이며 실패 1건을 그대로 보존했다.
- 공모펀드 공식 Agent 실행은 기본 `locked`로 유지한다. 팀이 승인한 v1 개발
  리허설에서만 `public_fund_v1_approved`를 명시해 열고, 실험 후 기본 잠금으로
  복구한다.
- 국내 DB와 manifest를 임시 경로에 재구축했을 때 원본 artifact와 SHA-256이
  byte 단위로 일치했다.
- 공모펀드 SQLite를 두 임시 경로에서 재구축했을 때 DB와 manifest가 각각
  SHA-256 `99fac786e5be0ec5a7a53e11e1bd3bbccd5b37ab15243ecbf8b864a85b375ca4`,
  `be83a616d033db2328d231499d1f0492323d02bace4f153ad3da4860a0d10bcd`로
  byte 단위 일치했다.

## 원천 데이터 감사

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/finance-data-audit \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit
```

결과:

- 국내채권 42,394행 × 40열
- 국내 ETP 1,734행 × 73열
- 해외 ETP 5,646행 × 49열
- 공모펀드 95,619행 × 45열
- 회귀 expectation 65/65 통과
- GPT Pro manifest의 8개 XLSX 입력 SHA-256과 모두 일치
- 동일 명령을 두 번 실행했을 때 5개 JSON 출력 SHA-256이 모두 동일

`artifacts/`는 생성 결과이며 Git에서 제외된다. 원천 XLSX는 읽기 전용이다.

## 로컬 LLM

HyperCLOVA X를 사용할 수 없는 개발 기간에만 별도 `gaeng3-llm-local` Conda
환경에서 Qwen/vLLM을 사용한다. 애플리케이션 환경·CI·평가 provider와
분리되며 세 가지 명시적 opt-in 없이는 호출할 수 없다.

설치, 고정 모델 revision, loopback 서버, E2E 절차는
[로컬 LLM 테스트 런타임](local-llm.md)에 기록한다.

2026-07-28 실제 RTX 5090 2장 환경에서 vLLM 0.25.1과 고정된 Qwen FP8
revision으로 연속 E2E와 byte-level 재현성 검증을 통과했다. HyperCLOVA X
endpoint·credential은 사용하지 않았다.

## 핵심 평가 회귀

[해외 ETP 핵심 평가 기준선](evaluation.md)은 40개 development와 10개 holdout,
총 50문항을 포함한다. 기대 QueryPlan과 실행 가능한 42문항의 후보 수·상위 상품
ID를 데이터 hash와 함께 동결했다.

모델 없는 expected provider로 평가 하네스 자체를 검증할 수 있다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

로컬 Qwen hybrid parser의 최초 미사용 holdout은 9/10이었다. 해당 실패를
수정한 뒤의 전체 회귀는 연속 2회 50/50을 기록했다. 두 결과는 일반화 성능과
사후 회귀 성능으로 구분해 관리한다.

[국내 ETP 핵심 평가 기준선](evaluation-domestic-etp.md)도 development 40개와
local-inference holdout 10개를 동결했다. expected provider 50/50, 로컬 Qwen
development 40/40, local-inference holdout 첫 실행 10/10이다. 다만 같은
개발자가 질문·규칙을 작성하고 전체 suite로 결정론적 linker 정합성을 확인했기
때문에 완전한 blind 일반화 점수로 주장하지 않는다.

국내 평가 재현 시 `--dataset domestic_etp`를 추가한다.

[국내채권 핵심 평가 기준선](evaluation-domestic-bond.md)은 같은 40/10 구조로
50문항을 동결했다. expected provider와 로컬 Qwen hybrid parser가 모두 50/50을
통과했고, 실제 통합 E2E에서도 세 조건·상위 3개·field evidence와 Answer
Verifier가 일치했다.

[공모펀드 핵심 평가 기준선](evaluation-public-fund.md)은 같은 40/10 구조로
50문항을 동결했다. expected provider에서 실행 44개와 안전 차단 6개가 모두
  통과했다. 로컬 Qwen hybrid parser는 development 40문항을 최초 실행에서
40/40 통과했다. parser·규칙을 commit한 뒤 최초 실행한 holdout은 9/10이다.
실패한 클래스 합산 질문은 실행 자체는 차단됐지만 기대한 unsupported가 아니라
AUM 통화 모호성으로 차단되어 strict failure다. 이 결과는 HyperCLOVA X
parser 성능이 아니다.

최초 결과를 별도 commit으로 보존한 뒤, 질문에 상품군이 없을 때 모델의 단일
상품군을 보조 힌트로 넘기는 family handoff와 공모펀드 unsupported 정렬 제거를
추가했다. 로컬 Qwen holdout은 다시 실행하지 않았고, 공개된 실패의 회귀 테스트와
무모델 50문항 replay만 50/50 통과했다.

[공모펀드 blind v1.1 평가 설계](evaluation-public-fund-blind-v1.1.md)는
기존 문항과 분리된 100문항의 범주·표현·처리 분포, 역할 분리, 질문·정답키
SHA-256 commitment와 최초 실행 상태 계약을 고정한다. 검증·봉인·실행 코드는
구현했고 실제 문항은 금융 도메인 담당자의 독립 작성 전이다.

## 근거 기반 최종 답변

[근거 기반 최종 답변 평가](evaluation-grounded-answers.md)는 기대 QueryPlan으로
답변 계층을 격리해 측정한다. 로컬 Qwen에는 실제 값·날짜·상품 식별자를 주지
않고 opaque result reference와 field label만 제공한다. Answer Verifier가
순위·evidence·숫자·식별자·투자 해석·경고를 검사하며 실패 시 결정론적
renderer로 폴백한다.

최종 국내 ETP 50문항은 47개 grounded 생성과 3개 안전 차단을 통과했다.
국내채권은 46개 grounded 생성, 1개 결정론적 빈 결과, 3개 안전 차단으로
50/50이며 폴백은 0건이었다.

공모펀드는 expected·local provider 모두 50/50을 통과했다. 실행 가능한
44문항은 grounded answer를 생성했고 6문항은 안전하게 차단했으며 폴백은
0건이었다. 상품명·수치·순위·evidence·기준일·warning 검증률은 모두 100%다.

별도의 `comparison_cli`는 정확한 `itm_no` 두 개를 지정하는 true COMPARE
20문항을 실행한다. expected·로컬 Qwen 모두 20/20이며 완전한 비교 18개는
grounded answer, 누락 상품이 있는 2개는 LLM 미호출 결정론 답변으로 처리됐다.
field status·numeric delta·근거·기준일은 100%, verifier fallback은 0건이다.
서버가 요청 순서, 차이, 통화 호환성과 결측을 결정하고 LLM은 설명만 담당한다.

별도의 `comparison_parser_cli`는 자연어의 정식명·짧은 이름·`itm_no`를 정확한
공모펀드 비교 대상으로 연결한다. 공개 24문항에서 expected·로컬 Qwen 모두
24/24이며 정상 비교 16개와 안전 차단 8개를 Oracle 전후로 검증했다. 중복
단축명·사모·미등록·동일 상품 중복은 추측하지 않는다.

`comparison_e2e_cli`는 같은 공개 24문항에서 두 격리 계층을 실제로 연결한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.comparison_e2e_cli \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

expected·로컬 Qwen 모두 24/24다. 로컬 Qwen은 parser를 24회 호출하고, 정책을
통과한 16문항에서만 answer를 호출했다. 결과는 실행 16개·안전 차단 8개,
grounded answer 16개, verifier fallback 0개다. parser target·field,
grounding, resolution, QueryPlan, Oracle, 안전 차단, Answer Verifier,
field status·numeric delta·실제 비교 셀 값과 별도의 근거 provenance, field
evidence 인용과 기준일 검증률은 모두 100%다. 자연어 비교 parser 단독 실행의
로컬 지연은 p50 569.018ms, p95 796.637ms, 최대 889.169ms다. 통합 E2E의
로컬 p95 latency는 parser 751.575ms, answer 2,225.406ms, 전체
2,737.07ms다.

E2E overlay는 QueryPlan 핵심 계약과 실행 16건의 field status·delta·실제
비교 셀 값과 별도의 근거 provenance를 동결한다. 전체 대상 sequence와 두
identity 사이의 정확한 연결어, 접두·연결·꼬리 위치별 문장부호 문법을 검사해
상품명 prefix·suffix와 누락된 세 번째 대상을 막는다. 제외·대신·포함 역할,
identity와 지원 언어를 제외한 질문 전체의 미등록 잔여 표현, 비어 있거나
미종결·역방향·중첩·줄바꿈이 잘못된 따옴표도 차단한다. 이 통합 결과는 공개
회귀 질문에 대한 배선 검증이다. 다른 작성자가 만든 독립 blind 질문의
parser→답변 E2E와 사람 rubric은 아직 실행하지 않았다.
HyperCLOVA X는 세 operation의 provider·주입형 transport·오류 계약과 API 없는
fake 테스트까지 완료했다. 설명회에서 확인한 공식 `GET /answer` 다섯 문자열과
질문당 60초 계약은 FastAPI route와 Docker에서 검증했지만, 실제 endpoint·인증
header·credential은 크레딧 수령 전이라 아직 연결하지 않았다. 공모펀드는 기본
`fund_execution_policy=locked`이며 명시적 개발 승인 경로만 별도로 검증했다.

이 수치는 자유 생성 LLM 점수가 아니라 제한된 hybrid system의 계약 준수율이다.
네 상품군·일곱 intent Router, capability matrix, BM25/SQLite FTS 문서 검색,
사람 rubric validator와 Backend DTO까지 구현했다. 다음 평가는 금융 도메인
담당자의 external blind 100문항 parser→답변 E2E와 실제 사람 평가이며, 이후
공식 API 계약에 맞는 HyperCLOVA X HTTP transport와 FastAPI route를 연결한다.

같은 상품군의 정확한 두 상품 COMPARE도 네 상품군 공통 경로로 일반화했다.
해외·국내 ETP·국내채권 exact resolver, registry 비교 capability,
`ComparisonEvidence`·독립 verifier·Backend citation을 합성 fixture E2E로
검증했다. 세 상품군 `product-compare-core-30`은 실제 정규화 DB에서 실행
18문항·안전 차단 12문항을 모두 통과했다. 기존 공모펀드 24문항과 합친 네
상품군 자연어 비교 공개 회귀는 54문항이다. 독립 blind는 아직 남아 있다.
비교 resolver는 원본 DB의 상품번호·이름·티커·ISIN 등 최소 identity 열만
bounded cache에 보관한다. DB inode·크기·수정시각이 바뀌면 자동 무효화하며
COMPARE Result Verifier는 전체 레코드 적재 없이 locked 두 상품 ID·후보 수·
정렬·범위 조건을 재검사한다. 공개 30문항의 3 workers 기준 지연은 p50
65.522ms·p95 954.670ms다.

SEARCH·AGGREGATE 기본 경로도 전체 정규화 레코드를 verifier universe로
적재하지 않는다. QueryPlan의 조건·정렬·그룹·집계 필드와 품질·기준일만
별도 projection으로 읽는다. 네 상품군 대표 8문항을 각각 새 프로세스에서
실행한 결과 지문은 8/8 일치했고 p50 308.749ms, 최대 추가 RSS 51,000KiB다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.search_aggregate_benchmark_cli \
  --require-perfect
```

이 수치는 같은 개발 장비의 단일 실행 방향성 기준선이며 운영 SLO가 아니다.
상세 계약은
[SEARCH·AGGREGATE 성능 기준선](evaluation-search-aggregate-performance.md)에
기록한다.

HyperCLOVA X 경계는 QueryPlan, 공모펀드 비교 초안, 근거 답변이 공유하는
semantic structured request와 오류·token·latency 관측을 제공한다. fake
transport로 정상 응답, 401·403·429·500, timeout, 연결 실패와 잘못된 응답을
검증한다. 실제 API 호출 완료로 오해하지 않도록
[HyperCLOVA X provider 계약](hyperclova-provider.md)에 완료 범위와 외부
게이트를 분리해 기록한다.

API 없는 전체 경로는 다음 명령으로 재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-hcx-contract-e2e.py \
  --require-perfect
```

세 실행 상품군 SEARCH의 QueryPlan→Oracle→Evidence→답변→Backend DTO와
fallback·timeout·Router 무호출·비활성 공모펀드·서버 계획 guard를 8개
시나리오로 검사한다. 현재 8/8이며 실제 네트워크 호출은 없다.

프레임워크 독립 `/answer` service adapter의 HTTP status·ERROR DTO·fallback·
민감정보 비노출 계약은 다음 명령으로 재현한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/run-answer-adapter-contract.py \
  --require-perfect
```

정상·provider 설정·인증·rate limit·서비스·timeout·transport·응답 오류,
dataset 장애, 알 수 없는 내부 오류와 grounded answer fallback을 포함한
12개 시나리오가 12/12다. 실제 FastAPI route나 네트워크 호출은 포함하지 않는다.
