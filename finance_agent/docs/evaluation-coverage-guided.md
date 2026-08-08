# 네 상품군 자동 커버리지·Qwen 자연화 평가

상태: v1.0 최초 관측 동결 · 내부 synthetic 개발 진단 · 독립 blind 아님

기준일: 2026-08-08

## 0. 한눈에 보기

기존에 만든 30개·40개·50개 질문을 계속 통과시키는 것만으로는 아직 한 번도
시험하지 않은 필드와 질문 표현을 알기 어려움

그래서 사람이 질문을 먼저 쓰는 대신 다음 순서로 평가 범위를 자동 생성

1. 필드 registry에서 실제로 검색·정렬·비교·집계할 수 있다고 선언한 기능 확인
2. 네 정규화 DB에서 유효한 실제 값을 표본으로 선택
3. 정답 작업 지시서인 QueryPlan을 먼저 만들고 Oracle로 직접 실행
4. 직접 실행 결과의 상품·수치·근거를 의미 지문으로 고정
5. 같은 계획을 규칙형 자연어 질문으로 바꿔 현재 Agent가 복원하는지 검사
6. 다음 단계에서 Qwen이 원문을 보지 않고 자연스러운 질문 세 가지를 생성

첫 결과는 다음과 같음

| 구분 | 결과 | 뜻 |
| --- | ---: | --- |
| 대표 기능 좌표 | 305개 | 모든 조합이 아니라 registry 기능을 대표하도록 선택한 시험점 |
| 정답 계획 직접 실행 | 299개 | Oracle·Verifier·field evidence까지 정상 실행 |
| 데이터 현실로 제외 | 6개 | 서로 다른 날짜·통화 값이 부족해 BETWEEN·IN 질문을 만들 수 없음 |
| 현재 Agent가 실행까지 도달 | 254/299 | 자연어 질문을 어떤 실행 경로로든 처리 |
| 정답 QueryPlan과 같은 계획 | 44/299 | 조건·연산자·정렬·개수·비교·집계 의미까지 일치 |
| 전체 근거까지 같은 strict 통과 | 37/299 | 계획과 최종 field evidence가 직접 실행 정답과 모두 일치 |

이 결과는 현재 시스템 전체가 12.4점이라는 뜻이 아님

- 기존 개발 질문은 자주 쓰는 표현과 핵심 시나리오를 중심으로 만들어 높은 회귀율을 확보
- 이번 299개는 드물게 쓰는 필드와 연산자까지 넓혀 의도적으로 빈 구간을 찾는 진단
- 규칙으로 만든 canonical 질문은 실제 사용자 질문 분포나 공모전 문항을 대표하지 않음
- 다만 정답 계획은 299개 모두 실행되므로 현재 가장 큰 병목이 자연어 질문 이해층이라는
  사실은 분명하게 보여줌

## 1. 왜 이 평가가 필요한가

기존 공개 회귀 100점에는 두 가지 위험이 있음

- 같은 질문을 보며 Router와 parser를 수정했기 때문에 처음 보는 표현의 성능과 다름
- 사람이 생각한 질문만 포함되므로 registry에 등록됐지만 한 번도 질문하지 않은 필드가
  남을 수 있음

자동 커버리지 평가는 이 두 공백을 보완

~~~text
필드 registry + 정규화 DB
  → 대표 capability 좌표 305개
  → 정답 QueryPlan 구성
  → Oracle 직접 실행
      ├─ 데이터 값 부족 6개: 제외 사유 보존
      └─ 실행 가능 299개: 계획·근거 지문 고정
           → canonical 자연어 질문
           → 현재 Agent 최초 실행
           → Router / 계획 / 검색 / 근거 단계별 진단
           → Qwen 자연화 질문 3종 생성·기계 선별
           → 결정론적·Qwen 역할별 Agent 비교
~~~

## 2. 무엇을 자동으로 만들었나

### 대표 좌표 선택 규칙

| 기능 | 선택 방식 |
| --- | --- |
| 조건 검색 | queryable 필드마다 대표 연산자 한 건 + 상품군에서 빠진 연산자 클래스 보충 |
| 정렬 검색 | sortable 필드마다 한 방향 + 상품군별 오름·내림 방향 보충 |
| 상품 비교 | comparable 필드마다 통화·기준일 범위가 맞는 실제 상품 두 개 선택 |
| 필드 집계 | aggregatable 필드마다 유효 함수 + COUNT·MIN·MAX·AVG·허용 SUM 보충 |
| 그룹 집계 | 식별자가 아닌 enum·boolean 필드별 상품 수 계산 |

305개는 각 필드와 각 연산자를 모두 곱한 완전 탐색이 아님
대표 필드와 연산자 클래스를 넓고 재현 가능하게 확인하는 최소 시험망임

### 직접 실행 분포

| 구분 | 실행 가능 |
| --- | ---: |
| 국내채권 | 81 |
| 국내 ETF·ETN | 105 |
| 해외 ETF·ETN | 50 |
| 공모펀드 | 63 |
| 합계 | 299 |

| 기능 종류 | 실행 가능 |
| --- | ---: |
| 조건 검색 | 112 |
| 정렬 검색 | 47 |
| 상품 비교 | 72 |
| 필드 집계 | 34 |
| 그룹 집계 | 34 |

### 제외 6개의 의미

- 국내채권·국내 ETP·해외 ETP·공모펀드의 정적 기준일은 유효한 서로 다른 값이 하나뿐
- 국내 ETP 동적 기준일도 유효한 서로 다른 값이 하나뿐
- 해외 ETP 거래 통화도 현재 유효 범위에 서로 다른 값이 하나뿐
- 따라서 두 경계가 필요한 BETWEEN 또는 두 값이 필요한 IN을 억지로 만들지 않음
- 결측을 가짜 값으로 채우지 않고 case_construction_error와 이유를 그대로 보존

## 3. 최초 자연어 실행에서 무엇을 발견했나

### 실패 단계

| 처음 막힌 단계 | 건수 | 쉽게 말하면 |
| --- | ---: | --- |
| 질문 분류 | 45 | 검색·비교·집계 또는 상품군을 잘못 판단 |
| 작업 계획 | 210 | 실행은 시도했지만 필드·조건·정렬·개수 등이 정답 계획과 다름 |
| 근거 | 7 | 계획 이후 상품·수치·field evidence가 직접 실행 정답과 다름 |
| 통과 | 37 | 계획과 근거가 모두 같음 |

실패 262개 중 255개가 검색 전에 수행하는 질문 분류와 계획 단계에서 시작
DB 조회·계산 엔진을 다시 만드는 것보다 질문을 정확한 계획으로 연결하는 작업의
기대효과가 훨씬 큼

### 기능별 strict 결과

| 기능 | 통과 | 해석 |
| --- | ---: | --- |
| 조건 검색 | 4/112 | 넓은 필드·연산자 표현을 공통 parser가 아직 충분히 읽지 못함 |
| 정렬 검색 | 9/47 | 일부 핵심 정렬은 되지만 드문 필드·날짜 정렬 공백 존재 |
| 상품 비교 | 0/72 | 기존 비교 parser가 상품 식별 문장 중심이라 자동 생성 형식과 차이 |
| 필드 집계 | 0/34 | 기존 집계가 자주 쓰는 문법 중심이고 필드 전체를 일반화하지 못함 |
| 그룹 집계 | 24/34 | 가장 넓게 일반화된 현재 강점 |

### 상품군별 strict 결과

| 상품군 | 통과 |
| --- | ---: |
| 국내채권 | 15/81 |
| 국내 ETF·ETN | 7/105 |
| 해외 ETF·ETN | 5/50 |
| 공모펀드 | 10/63 |

상품군별 숫자만 보고 우선순위를 정하면 안 됨
국내 ETP에 시험 가능한 registry 필드가 가장 많아 분모도 가장 크기 때문

### 높은 영향의 공백

- 정적 기준일 관련 20/20 실패
- AUM 관련 15/16 실패
- 총보수율 관련 12/12 실패
- 거래 통화 관련 10/14 실패
- BETWEEN 연산자 29/29 실패
- 비교 72/72와 일반 필드 집계 34/34 실패

자동 진단기는 각 실패의 예상·실제 QueryPlan 차이도 집계
대표적으로 결과 개수 변경 32건, 계획 자체가 생성되지 않은 사례 23건,
상품 ID 두 개 조건 누락 16건, 거래 통화 조건 누락 14건을 확인

## 4. 이 결과로 무엇을 고칠 것인가

우선순위는 다음 원칙으로 결정

1. 한 수정으로 많은 질문을 살릴 수 있는 공통 문법 우선
2. 모델이 없어도 명확한 수치·날짜·연산자는 결정론적 parser 우선
3. 규칙이 모호하거나 표현 종류가 많은 경우만 Qwen 계획 후보 활용
4. Qwen 계획은 원문 근거·registry capability·서버 확정 조건 gate를 통과해야 실행
5. 최초 결과를 보존한 뒤 별도 사후 회귀로만 개선 효과 기록

현재 예상 순서

1. COMPARE의 두 상품 ID와 비교 필드를 registry 기반 공통 경로로 일반화
2. AGGREGATE의 함수·대상 필드·그룹 필드를 registry alias로 일반화
3. 날짜와 BETWEEN·IN·NOT_IN 연산자 문법 보강
4. AUM·보수율·수익률·통화 등 공통 필드 alias 연결 보강
5. Qwen grounded-plan이 규칙 parser의 빈 구간을 얼마나 안전하게 구제하는지 측정

단순히 299개 canonical 문장을 외우도록 정규식을 추가하는 방식은 금지
같은 의미의 Qwen 자연화 질문과 이후 외부 blind에서 함께 좋아지는 수정만 채택

## 5. Qwen으로 다음에 할 실험

Qwen은 기존 질문 문장을 보지 않음
서버가 만든 다음 의미 명세만 입력

- 상품군과 요청 종류
- 조건 필드의 한글 label·alias
- 연산자와 실제 값
- 정렬 필드·방향·결과 개수
- 비교 필드 또는 집계 함수·그룹 필드

각 정답 계획에서 세 질문을 생성

- semantic_formal: 정중한 실무 문장
- semantic_colloquial: 일반 대화형 문장
- semantic_telegraphic: 짧은 검색창형 문장

299개 계획을 모두 실행하면 생성 요청은 897개

### 생성 질문의 안전 선별

다음을 모두 통과한 질문만 Agent에 입력

- 상품군과 검색·비교·집계 의도 보존
- 모든 필드와 실제 값 보존
- 이하·초과·범위·제외·포함 등 연산자 보존
- 정렬 방향과 결과 개수 보존
- 예측·추천·수익 보장 같은 새 의도 미추가
- 원래 canonical 문장의 단순 복사 아님
- 같은 계획의 세 질문이 서로 다른 표현

생성 실패와 검사 거절은 삭제하지 않고 전체 897개 분모에 남김

### 비교할 Agent 구성

| 구성 | 확인 목적 |
| --- | --- |
| 결정론적 Agent | 현재 공통 Router·parser가 자연화 질문을 얼마나 처리하는지 |
| Qwen 계획만 | Qwen이 규칙 parser의 빈 구간을 안전하게 구제하는지 |
| Qwen 답변만 | 검색은 같을 때 설명 품질·fallback·지연이 어떻게 달라지는지 |
| Qwen 계획+답변 | 현재 로컬 개발 전체 경로의 상한과 비용 |

주요 지표

- 생성 의미 보존 통과율
- 통과 질문에 대한 Agent strict 정확도
- 전체 생성 요청 중 최종 strict 통과율인 end-to-end yield
- QueryPlan 의미 일치율
- field evidence 의미 일치율
- Qwen 오류·gate 거절·fallback 건수
- p50·p95·최대 지연과 역할별 호출 수

## 6. 긴 실험을 안전하게 실행하는 방법

로컬 Qwen은 개발용 opt-in 세 변수를 모두 켠 상태에서만 사용

~~~bash
export FINANCE_AGENT_LLM_MODE=local_test
export ENABLE_NON_HCX_TEST_LLM=1
export LLM_PROVIDER=local_test
export LOCAL_TEST_LLM_MODEL=qwen3-local-test
export LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1
export LOCAL_TEST_LLM_TIMEOUT_SECONDS=180
export PYTHONPATH=packages/finance_agent_core/src
~~~

### 권장: 쓰기 전용·재개 가능 캠페인

먼저 서로 다른 출력 디렉터리에서 10개 source pilot을 실행

~~~bash
python -m finance_agent_core.evaluation.coverage_campaign_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --output-dir artifacts/evaluation/coverage-qwen-pilot-first \
  --source-limit 10 \
  --shard-size 10 \
  --workers 4 \
  --profile expected \
  --profile local_test_grounded_plan_only
~~~

pilot의 생성 실패·기계 거절·오해 사례를 확인한 뒤 최초 전체 캠페인을 별도
디렉터리에 실행

~~~bash
python -m finance_agent_core.evaluation.coverage_campaign_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --output-dir artifacts/evaluation/coverage-qwen-campaign-first \
  --shard-size 25 \
  --workers 4 \
  --profile expected \
  --profile local_test_grounded_plan_only
~~~

이 도구는 다음 안전장치를 적용

- 질문과 profile 실행을 작은 shard로 저장해 중단 후 같은 명령으로 재개
- 이미 생긴 최초 관측 파일은 덮어쓰지 않고 hash·source ID·모델을 검증해 재사용
- 질문 shard와 실행 shard를 일대일로 검사한 뒤 캠페인 결과 병합
- 같은 질문의 결정론적 결과와 Qwen 결과를 paired 방식으로 비교
- 질문·실행·비교 파일의 SHA-256을 `manifest.json`에 기록

생성, 실행, 비교를 분리해야 하면 같은 출력 디렉터리에 `--phase generate`,
`--phase run`, `--phase compare`를 순서대로 사용

### 하위 단계별 명령

캠페인 자동화 없이 각 파일을 직접 점검할 때만 아래 명령을 사용

먼저 10개 계획으로 pilot 생성

~~~bash
python -m finance_agent_core.evaluation.coverage_question_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --offset 0 \
  --limit 10 \
  --workers 4 \
  --output artifacts/evaluation/coverage-question-shard-0000-0010.json
~~~

pilot의 거절 사유를 검토한 뒤 25개 계획 단위 shard로 전체 생성
서로 겹치지 않는 shard만 hash 검증 후 병합

~~~bash
python -m finance_agent_core.evaluation.coverage_question_merge_cli \
  --input artifacts/evaluation/coverage-question-shard-0000-0025.json \
  --input artifacts/evaluation/coverage-question-shard-0025-0050.json \
  --output artifacts/evaluation/coverage-question-campaign-v1.json
~~~

각 shard를 Agent로 실행

~~~bash
python -m finance_agent_core.evaluation.coverage_question_run_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --batch-input artifacts/evaluation/coverage-question-shard-0000-0025.json \
  --agent-provider local_test_grounded_plan_only \
  --output artifacts/evaluation/coverage-run-plan-shard-0000-0025.json
~~~

질문 shard와 실행 report를 같은 순서로 전달해 캠페인 집계

~~~bash
python -m finance_agent_core.evaluation.coverage_question_run_merge_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --batch-input artifacts/evaluation/coverage-question-shard-0000-0025.json \
  --report-input artifacts/evaluation/coverage-run-plan-shard-0000-0025.json \
  --output artifacts/evaluation/coverage-run-plan-campaign-v1.json
~~~

중복 source case, 서로 다른 모델·평가 profile·suite hash, 잘못 연결한 batch와
report가 있으면 병합을 중단

같은 질문을 실행한 결정론적·Qwen profile report를 문항별 비교

~~~bash
python -m finance_agent_core.evaluation.coverage_ablation_cli \
  --input deterministic=artifacts/evaluation/coverage-canonical-expected.json \
  --input qwen_plan=artifacts/evaluation/coverage-canonical-qwen-plan.json \
  --output artifacts/evaluation/coverage-canonical-ablation.json
~~~

비교 결과는 strict 상승만 보여주지 않고 Qwen이 구제한 문항과 새로 실패시킨
문항, 계획·근거의 구제·퇴행, 실패 단계 이동, 추가 호출·오류·지연을 함께 기록

정확도 하나만 보고 개선으로 판정하지 않음

- profile 정확도에는 Wilson 95% 신뢰구간 표시
- 같은 질문의 통과 여부 차이는 seed 고정 10,000회 paired bootstrap 구간 표시
- 구제·퇴행 비대칭은 exact McNemar 검정으로 확인
- 여러 Qwen profile을 한 번에 비교하면 Holm 방식으로 우연한 유의성 보정
- 상품군·기능·필드·연산자·정렬·집계 함수·표현 축별 구제와 퇴행도 함께 집계
- 통계적 개선이 있어도 새 strict 퇴행 사례는 ID 단위로 별도 검토

## 7. 1등 전략에서 이 실험의 위치

이 실험 자체가 수상 근거는 아님
하지만 다음 세 가지를 동시에 만들 수 있다는 점이 차별점

- 기능을 몇 개 구현했다고 주장하는 대신 registry 전체의 시험 범위를 수치로 공개
- 모델 답이 맞았는지만 보지 않고 질문 계획과 field evidence까지 정답과 비교
- 실패를 기능·상품군·필드·연산자·단계별로 분해해 가장 영향 큰 수정부터 반복

제안서 화면은 성능 기준선을 고정한 뒤 다음 내용을 보여주는 수준으로 제작

- 사용자의 자연어 질문
- Agent가 이해한 조건과 정렬
- 검색·비교·계산 결과
- 필드별 출처와 기준일
- 답변 불가 또는 추가 조건이 필요한 이유

현재는 화면보다 위 평가 루프와 외부 blind 질문 확보가 우선

## 8. 아직 반드시 필요한 외부 검증

- 금융 도메인 담당자가 기존 문항과 코드를 보지 않고 만든 외부 blind 100문항
- 질문과 비공개 정답을 hash 봉인한 뒤 최초 1회 실행
- 실제 사용자 관점의 정확성·도움됨·명료성 사람 평가
- 승인된 비정형 금융 문서 corpus의 문서 RAG 평가
- 크레딧 수령 후 같은 동결 세트에서 HyperCLOVA X 역할별 A/B
- 공식 제출 전 로컬 Qwen provider·설정·스크립트·의존성 제거 검사

## 9. 정본과 해석 경계

- 프로토콜:
  [coverage-guided-v1.protocol.json](../evaluation/protocols/coverage-guided-v1.protocol.json)
- 최초 관측 baseline:
  [coverage-guided-v1.json](../evaluation/baselines/coverage-guided-v1.json)
- 전체 suite·report·진단 결과는 상품 ID와 원천 근거를 포함하므로
  artifacts/evaluation/에만 로컬 보존
- 로컬 Qwen 결과는 HyperCLOVA X나 공식 공모전 성능으로 표현하지 않음
- 37/299를 개선한 뒤에도 최초 baseline은 수정하지 않고 별도 사후 회귀로 추가
