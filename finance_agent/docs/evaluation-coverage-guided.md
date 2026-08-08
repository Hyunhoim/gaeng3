# 네 상품군 자동 커버리지·Qwen 자연화 평가

상태: v1.1 canonical·Qwen 최초 관측 동결 · 내부 synthetic 개발 진단 · 독립 blind 아님

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
6. Qwen이 원문을 보지 않고 자연스러운 질문 세 가지를 생성해 네 Agent 구성을 비교

첫 결과는 다음과 같음

| 구분 | 결과 | 뜻 |
| --- | ---: | --- |
| 대표 기능 좌표 | 305개 | 모든 조합이 아니라 registry 기능을 대표하도록 선택한 시험점 |
| 정답 계획 직접 실행 | 299개 | Oracle·Verifier·field evidence까지 정상 실행 |
| 데이터 현실로 제외 | 6개 | 서로 다른 날짜·통화 값이 부족해 BETWEEN·IN 질문을 만들 수 없음 |
| 현재 Agent가 실행까지 도달 | 254/299 | 자연어 질문을 어떤 실행 경로로든 처리 |
| 정답 QueryPlan과 같은 계획 | 44/299 | 조건·연산자·정렬·개수·비교·집계 의미까지 일치 |
| 전체 근거까지 같은 strict 통과 | 37/299 | 계획과 최종 field evidence가 직접 실행 정답과 모두 일치 |

Qwen 자연화 최초 전체 결과는 다음과 같음

| 구분 | 결과 | 뜻 |
| --- | ---: | --- |
| 자연화 질문 요청·생성 | 897/897 | 299개 정답 계획에서 세 문체를 생성했고 모델 생성 오류는 0건 |
| 기계 의미 선별 통과 | 391/897 | 필드·값·연산자·정렬·개수 보존을 엄격히 검사한 실행 분모 |
| 모델 없는 기준선 strict | 65/391 | 자연화 질문에서 계획과 field evidence가 모두 일치 |
| Qwen 계획만 strict | 65/391 | 2건 구제와 2건 퇴행으로 순개선 0 |
| Qwen 답변만 strict | 65/391 | 검색 의미를 바꾸지 않고 Qwen 답변 오류·fallback 0 |
| Qwen 계획+답변 strict | 65/391 | 계획만 구성과 같은 2건 구제·2건 퇴행, 순개선 0 |

Qwen 계획 경로의 정확도 차이 95% 구간은 약 `-1.0%p~+1.0%p`, Holm 보정
`p=1.0`으로 현재 prompt·gate가 기준선을 개선했다는 증거는 없음
답변 전용 경로는 evidence 기반 문장 생성 182회에서 오류·fallback 없이 정확도를
유지했지만, 넓은 capability 질문의 계획 실패를 고치지는 못함

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

## 5. Qwen으로 수행한 역할 분리 실험

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
- 숫자·날짜 조건은 연산자 단어가 질문 어딘가에 있는지만 보지 않고 해당 값의
  가장 가까운 연산자와 결합됐는지 검사해, 여러 조건의 이상·이하가 뒤바뀐 질문 차단
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
- 기계 의미 보존을 통과한 질문에 대한 Agent strict 정확도
- 전체 생성 요청 중 최종 strict 통과율인 end-to-end yield
- QueryPlan 의미 일치율
- field evidence 의미 일치율
- Qwen 오류·gate 거절·fallback 건수
- p50·p95·최대 지연과 역할별 호출 수

897개 변형 질문은 299개 정답 계획에서 세 문체씩 파생되므로 완전히 독립된 897개
표본으로 간주하지 않는다. 구성별 정확도와 paired 차이의 95% 구간은 같은 정답 계획의
세 문체를 한 묶음으로 재표집하는 10,000회 cluster bootstrap으로 계산한다. exact
McNemar도 개별 문장이 아니라 source plan 단위로 계산하고 여러 후보 비교에는 Holm
보정을 적용한다. 문항 수와 문항별 구제·퇴행은 원인 분석을 위해 별도로 그대로 표시한다.

## 6. Qwen 자연화 최초 전체 결과

### 질문 생성과 선별

- source commit `6fe84d08270c2af1f6db84476c41fad746cd29a9`와 네 DB SHA-256을 잠근 뒤 실행
- 299개 정답 계획에서 897개 질문을 모두 생성했고 생성 오류 0건
- 기계 의미 선별은 391개 통과·506개 거절, 통과율 43.6%
- 문체별 통과는 정중한 실무형 191개, 구어체 91개, 검색창형 109개
- 주요 거절은 registry 필드 표현 273건, intent 표현 191건, 조건 연산자 표현
  127건, 값 보존 94건, 값과 연산자 결합 59건
- 거절 질문도 삭제하지 않고 전체 897개 분모와 검수 큐에 보존

43.6%는 Qwen이 56.4%의 질문을 생성하지 못했다는 뜻이 아님
모든 질문은 생성됐지만 원래 정답 계획의 의미가 기계적으로 입증되지 않은 506개를
Agent 실행 전에 제외한 값임. 자연스러운 축약 표현을 보수적으로 거절한 사례도 있으므로
선별기의 정밀도·재현율은 별도 사람 검수가 필요

### 네 구성 결과

| 구성 | strict | 계획 일치 | 근거 일치 | fallback | p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 모델 없음 | 65/391 (16.6%) | 18.9% | 38.6% | 0 | 3,914.6ms |
| Qwen 계획만 | 65/391 (16.6%) | 19.2% | 38.1% | 0 | 6,269.2ms |
| Qwen 답변만 | 65/391 (16.6%) | 18.9% | 38.6% | 0 | 4,134.5ms |
| Qwen 계획+답변 | 65/391 (16.6%) | 19.2% | 38.1% | 0 | 6,304.8ms |

- 모델 없는 기준선의 첫 실패는 routing 64건·planning 253건·evidence 9건
- Qwen 계획 경로는 routing 실패를 51건으로 줄였지만 planning 실패가 265건,
  evidence 실패가 10건으로 늘어 최종 strict 순개선 없음
- Qwen 계획 370회 중 provider 오류 1건, 답변 전용 182회는 오류 0건
- Qwen 계획은 공모펀드 2건을 구제했지만 국내채권·국내 ETP 각 1건을 퇴행
- paired source-plan cluster 기준 정확도 변화 `0.0%p`, Holm 보정 후 유의하지 않음
- 계획만 p95는 기준선보다 약 2.35초, 전체 경로는 약 2.39초 증가
- 전체 경로 최대 22,978.9ms로 내부 60초 예산 안이지만 정확도 개선 없는 비용임

### 채택 판단

현재 Qwen 답변 전용 경로는 검증된 evidence를 자연어로 설명하는 역할에서 안전하게
동작했음. 반면 Qwen 계획 경로는 넓은 필드·연산자 질문에서 기준선보다 낫다는 증거가
없으므로 현재 상태 그대로 공식 후보에 채택하지 않음

다음 수정은 개별 391문장을 외우는 규칙이 아니라 다음 공통 원인에 한정

1. COMPARE의 두 상품 ID와 비교 필드 projection
2. AVG·MIN·MAX·SUM과 대상 필드를 보존하는 AGGREGATE 계획
3. BETWEEN·IN·NOT_IN과 값-연산자 결합
4. registry 필드 alias·코드 값 정규화·필수 projection 병합
5. Qwen 계획이 서버의 확정 조건을 누락하거나 바꾸면 거절하는 gate

수정 후 같은 391개 결과는 사후 회귀로만 기록하고, 최초 65/391과 2건 구제·2건
퇴행 결과를 덮어쓰지 않음

## 7. 긴 실험을 안전하게 실행하는 방법

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

장시간 실행 전에는 Qwen 서버를 시작한 뒤 쓰기 없는 사전 점검을 먼저 수행

~~~bash
python -m finance_agent_core.evaluation.coverage_campaign_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --output-dir artifacts/evaluation/coverage-qwen-campaign-first \
  --shard-size 25 \
  --workers 4 \
  --profile expected \
  --profile local_test_grounded_plan_only \
  --profile local_test_answer_only \
  --profile local_test_grounded \
  --preflight-only
~~~

사전 점검은 코드가 clean commit인지, 네 SQLite 지문이 같은지, 출력 디렉터리가
새 캠페인 또는 같은 protocol의 재개인지, 여유 디스크와 Qwen health가 충분한지
확인한다. 예상 질문 수·최대 Qwen 호출 수도 함께 출력한다. 모델 서버를 아직 시작하지
않은 준비 단계에서는 `--skip-provider-health`로 나머지 조건만 확인할 수 있다.

먼저 서로 다른 출력 디렉터리에서 10개 source pilot을 실행

이 pilot은 suite 앞부분의 국내채권 SEARCH 사례만 사용하므로 provider 연결·JSON 형식·
재시작·보고서 생성을 확인하는 건강검진이다. 네 상품군이나 전체 의도 성능의 대표
표본으로 해석하지 않는다. 상품군·의도별 판단은 299개 source 전체 캠페인에서 수행한다.

~~~bash
python -m finance_agent_core.evaluation.coverage_campaign_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --output-dir artifacts/evaluation/coverage-qwen-pilot-first \
  --source-limit 10 \
  --shard-size 10 \
  --workers 4 \
  --profile expected \
  --profile local_test_grounded_plan_only \
  --profile local_test_answer_only \
  --profile local_test_grounded
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
  --profile local_test_grounded_plan_only \
  --profile local_test_answer_only \
  --profile local_test_grounded
~~~

네 profile을 같은 최초 캠페인에 넣는 이유

- `expected`: 모델 없이 동작하는 현재 기준선
- `local_test_grounded_plan_only`: 질문을 실행 계획으로 바꾸는 역할만 Qwen 사용
- `local_test_answer_only`: 검색 계획은 같게 두고 설명문 생성만 Qwen 사용
- `local_test_grounded`: 계획과 설명문 모두 Qwen을 사용하는 로컬 전체 경로

전체 캠페인은 수천 회의 로컬 생성 호출이 생길 수 있으므로 pilot의 오류·fallback·
지연을 먼저 확인하고 시작. 중단돼도 완료된 shard는 덮어쓰지 않고 재사용

이 도구는 다음 안전장치를 적용

- 질문과 profile 실행을 작은 shard로 저장해 중단 후 같은 명령으로 재개
- 첫 Qwen 호출 전에 clean Git commit·suite·선택 범위·모델을 `protocol.json`으로 잠금
- Git에 무시된 `artifacts/` 결과는 허용하지만, 수정 파일과 무시되지 않은 새 소스 파일이
  하나라도 있으면 시작 거부
- tracked source가 바뀌면 이전 조각과 섞지 않고 새 출력 디렉터리를 요구
- 이미 생긴 최초 관측 파일은 덮어쓰지 않고 hash·source ID·모델을 검증해 재사용
- 질문 shard와 실행 shard를 일대일로 검사한 뒤 캠페인 결과 병합
- 같은 질문의 결정론적 결과와 Qwen 결과를 paired 방식으로 비교
- 질문·실행·비교 파일의 SHA-256을 `manifest.json`에 기록

캠페인이 끝나면 생성 성공률·구성별 정확도·구제·퇴행·지연·실패 단계를 사람이
읽을 수 있는 Markdown 보고서로 변환

~~~bash
python -m finance_agent_core.evaluation.coverage_report_cli \
  --questions artifacts/evaluation/coverage-qwen-campaign-first/questions/campaign.json \
  --ablation artifacts/evaluation/coverage-qwen-campaign-first/ablation.json \
  --output artifacts/evaluation/coverage-qwen-campaign-first/report.md \
  --review-examples 10
~~~

보고서는 Qwen 생성 의미 보존 통과율과 Agent strict 정확도의 분모를 분리하고,
좋아진 문항뿐 아니라 기존 정답을 망가뜨린 퇴행과 p95 지연도 같은 표에 표시한다.
또한 의미 보존 탈락·생성 오류·구제·퇴행 질문을 고정 순서로 표본 추출해 사람이 바로
검수할 수 있게 한다.

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
- Agent profile 비교 분모에서는 기계 거절 질문을 제외하고 생성 통과율은 별도 표시
- 같은 질문의 통과 여부 차이는 seed 고정 10,000회 paired bootstrap 구간 표시
- 구제·퇴행 비대칭은 exact McNemar 검정으로 확인
- 여러 Qwen profile을 한 번에 비교하면 Holm 방식으로 우연한 유의성 보정
- 상품군·기능·필드·연산자·정렬·집계 함수·표현 축별 구제와 퇴행도 함께 집계
- 통계적 개선이 있어도 새 strict 퇴행 사례는 ID 단위로 별도 검토

## 8. exact 점수와 실제 실행 의미를 분리한 사후 감사

최초 자연화 결과의 엄격한 `65/391`은 그대로 보존한다. 다만 실패 지도를 검토하던
중 집계 계획의 다음 두 차이가 현재 실행 결과를 바꾸지 않는다는 사실을 확인했다.

- `AGGREGATE projection`: 실행기는 이 목록을 사용하지 않고 조건·그룹·집계 대상에서
  검증용 필드를 다시 구성
- 그룹이 없는 `AGGREGATE limit`: 결과가 항상 하나이므로 1과 100 모두 같은 결과

점수를 사후에 바꾸는 방식으로 사용하지 않도록 별도 감사 도구로 분리했다. 그룹별
집계의 limit, 집계 함수·대상·그룹·조건·상품군, 검색·비교 계획은 한 항목도 완화하지
않는다.

~~~bash
python -m finance_agent_core.evaluation.coverage_execution_audit_cli \
  --suite-input artifacts/evaluation/coverage-guided-plan-v1-canonical-screened-v2.json \
  --report-input artifacts/evaluation/coverage-qwen-campaign-first/runs/expected/campaign.json \
  --output artifacts/evaluation/coverage-qwen-campaign-first/audits/expected-aggregate-execution-inert-v1.json
~~~

| 구성 | 기존 exact strict | 실행 의미 보조 strict | 표기 차이로만 승격 |
| --- | ---: | ---: | ---: |
| 규칙 기반 계획 | 65/391 | 134/391 | 69 |
| Qwen 계획 | 65/391 | 132/391 | 67 |
| 규칙 기반 계획 + Qwen 답변 | 65/391 | 134/391 | 69 |
| Qwen 계획 + Qwen 답변 | 65/391 | 132/391 | 67 |

규칙 기반 경로의 기능별 보조 strict는 조건 검색 `6/104`, 순위 검색 `11/103`,
비교 `0/29`, 일반 집계 `69/79`, 그룹 집계 `48/76`이다. 따라서 일반 집계 전체를
가장 큰 실패로 보던 최초 exact 지도는 실행 표기 차이의 영향을 많이 받았다. 다음
구현 우선순위는 비교, 조건·순위 검색, 남은 실제 집계 오류 순으로 조정한다.

이 보조 수치도 현재 실행기와 고정 데이터에 대한 사후 진단일 뿐 공식 점수나 외부
blind 성능이 아니다. 실행기에서 projection 또는 limit의 의미가 바뀌면 감사 정책과
기존 산출물을 새 버전으로 다시 검토해야 한다.

## 9. 비교 질문 공통 문법 개선 후 사후 회귀

최초 관측을 지우지 않고 같은 391문항을 다시 실행해 비교 질문 개선의 영향만
확인했다. 정확한 두 상품 ID를 `A와 B`, `A 또는 B`, `A 및 B`처럼 표현한 문장을
안전하게 인식하고, 비교 결과에는 요청한 숫자뿐 아니라 상품 기본 정보와 기준일까지
같이 보존하도록 네 상품군의 계획을 통일했다.

| 지표 | 최초 관측 | 비교 개선 후 | 변화 |
| --- | ---: | ---: | ---: |
| exact strict | 65/391 | 94/391 | +29 |
| 실행 의미 보조 strict | 134/391 | 163/391 | +29 |
| 비교 질문 | 0/29 | 29/29 | +29 |
| 조건 검색 | 6/104 | 6/104 | 변화 없음 |
| 순위 검색 | 11/103 | 11/103 | 변화 없음 |
| 일반 집계 | 69/79 | 69/79 | 변화 없음 |
| 그룹 집계 | 48/76 | 48/76 | 변화 없음 |

비교 29건을 모두 고쳤고 다른 기능의 통과 건수는 바뀌지 않아 이번 변경이 비교
경로에만 영향을 준 것을 확인했다. 전체 Agent Core 테스트도 `496 passed`를 기록했다.

단, 이 수치는 최초 결과를 본 뒤 같은 공개 질문으로 고친 **사후 회귀 결과**다.
처음 보는 질문에 대한 성능이나 공모전 예상 점수로 사용하지 않는다. 다음 개선
대상은 조건 검색과 순위 검색이며, 독립 blind에서 함께 좋아지는지 별도로 확인한다.

- 소스 커밋: `9aac2ff08c9808f5f3513939e2df71e7a51446c6`
- 규칙 기반 재실행 SHA-256:
  `3be8f598b6be343ec296d3502ef630fc9f416d00c23b2120f27376590f24add5`
- 실행 의미 감사 SHA-256:
  `299694143a42543e8fbd6777fc00ba3db557fa62e3157f0bf9a7fcd2d509bc22`

## 10. 1등 전략에서 이 실험의 위치

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

## 11. 아직 반드시 필요한 외부 검증

- 금융 도메인 담당자가 기존 문항과 코드를 보지 않고 만든 외부 blind 100문항
- 질문과 비공개 정답을 hash 봉인한 뒤 최초 1회 실행
- 실제 사용자 관점의 정확성·도움됨·명료성 사람 평가
- 승인된 비정형 금융 문서 corpus의 문서 RAG 평가
- 크레딧 수령 후 같은 동결 세트에서 HyperCLOVA X 역할별 A/B
- 공식 제출 전 로컬 Qwen provider·설정·스크립트·의존성 제거 검사

## 12. 정본과 해석 경계

- 프로토콜:
  [coverage-guided-v1.protocol.json](../evaluation/protocols/coverage-guided-v1.protocol.json)
- 최초 관측 baseline:
  [coverage-guided-v1.json](../evaluation/baselines/coverage-guided-v1.json)
- 전체 suite·report·진단 결과는 상품 ID와 원천 근거를 포함하므로
  artifacts/evaluation/에만 로컬 보존
- 로컬 Qwen 결과는 HyperCLOVA X나 공식 공모전 성능으로 표현하지 않음
- canonical 37/299와 자연화 65/391을 개선한 뒤에도 최초 관측 수치는 삭제하지 않고
  별도 사후 회귀로 추가
