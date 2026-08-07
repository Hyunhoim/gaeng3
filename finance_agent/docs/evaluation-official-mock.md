# 공식 형식 30문항 공개 모의평가

상태: v1.1 기능 E2E 30/30 · 실제 Docker GET 최초 관측 24/30 · blind 아님

기준일: 2026-08-07

## 0. 한눈에 보기

설명회에서 안내된 예상 평가 형태를 따라 내부 모의평가 30문항을 구성했다

- 난이도 하·중·상 각 10문항
- 정상적으로 답해야 하는 질문 25문항
- 데이터나 정책상 답하지 않아야 하는 질문 5문항
- 국내채권·국내 ETP·해외 ETP·공모펀드 모두 포함
- 질문 분류부터 로컬 Qwen, DB 검색·계산, 두 검증기, Backend DTO와 공식
  5개 문자열 응답 변환까지 한 번에 실행

로컬 Qwen 실행 결과는 검색·비교·계산·안전 처리·근거·공식 응답 형식 30/30이다
답변 문장 생성 대상 17문항 중 16문항은 검증을 통과했고, 1문항은 위험한 표현을
Answer Verifier가 막아 검증된 정해진 답변으로 교체했다

같은 30문항을 실제 Docker FastAPI `GET /answer`로 호출한 최초 관측은 24/30이다
공식 다섯 문자열과 60초 예산은 30/30이지만, 공모펀드 정상 질문 6건이 현재
Backend의 의도적인 공식 실행 잠금 때문에 안전한 역질문으로 끝났다. 이는 모델의
무작위 실패가 아니라 실제 배포 설정과 내부 평가 설정 사이에서 발견한 기능 차이다

이 결과는 공모전 예상 점수가 아니다. AI 담당자가 기존 구현과 데이터를 본 뒤
만든 공개 모의평가이므로 처음 보는 비공개 질문에 대한 성능을 나타내지 않는다

## 1. 왜 만들었나

기존 평가는 상품군별 기능이나 공격 유형을 자세히 확인하지만, 설명회에서 안내된
`하 10 + 중 10 + 상 10`, `답변 불가 5` 형태를 한 번에 재현하지 않았다

이번 세트의 목적은 다음 세 가지다

1. 공식 예상 분포에서 현재 전체 배선이 끊기지 않는지 확인
2. 답변할 수 없는 질문을 억지로 실행하지 않는지 확인
3. HyperCLOVA X를 연결하기 전에 로컬 Qwen으로 호출 수와 응답시간을 측정

## 2. 문항 구성

| 난이도 | 답변 가능 | 답변 불가 | 합계 | 주요 범위 |
| --- | ---: | ---: | ---: | --- |
| 하 | 9 | 1 | 10 | 상품 상세, 판매·매수 가능 개수, 단순 정렬 검색 |
| 중 | 9 | 1 | 10 | 같은 상품군 비교, 여러 조건 검색, 모호한 추천 요청 |
| 상 | 7 | 3 | 10 | 평균·최댓값·그룹 분포, 국내외 ETP 동시 검색, 빈 결과·예측·prompt injection |
| 합계 | 25 | 5 | 30 | 네 상품군 전체 |

정상 답변 25문항의 대표 상품군 기준 분포는 해외 ETP 7, 국내 ETP 6,
국내채권 6, 공모펀드 6이다. 해외 ETP로 분류한 상 난이도 1문항은 국내·해외 ETP를
각각 검색하므로 실제 실행에서는 두 상품군을 함께 확인한다

## 3. 정답을 만드는 방법

로컬 Qwen이 정답을 작성하거나 채점하지 않는다

- 기존 공개 회귀에서 검증한 질문·후보 수·상위 상품 ID·비교 필드·집계 함수를 재사용
- 네 정규화 DB와 manifest의 SHA-256을 세트 안에 고정
- 검색·필터·정렬·비교·집계는 SQLite Oracle이 실행
- Result Verifier가 DB 결과를 별도로 재검사
- Qwen은 질문을 구조화하고 검증된 evidence만 보고 설명문을 생성
- Answer Verifier가 모델 문장에 상품명·숫자·예측·추천·가치 판단이 섞이지 않았는지 검사
- 마지막에 공식 `question_id`, `question`, `retrieved_context`, `think_trace`,
  `answer` 다섯 문자열로 변환

위 설명은 같은 프로세스 안에서 실행한 기능 E2E 기준이다. 현재 Docker Backend의
`local_test` 설정은 Qwen을 **답변 문장 생성에만** 연결하고, QueryPlan은 서버 규칙으로
확정한다. 실제 HTTP 결과에서는 이 범위를 별도로 표시해 전체가 Qwen을 통과한 것처럼
해석하지 않는다

세트 파일은
`packages/finance_agent_core/src/finance_agent_core/evaluation/suites/official_mock_v1_30.json`,
SHA-256은
`c9250cd45656beadad563087941112b31318254c50fc72bf9f15dcaa1d88a36e`다

`scripts/generate-official-mock-suite.py --check`는 기존 정답 출처에서 같은 세트가
다시 만들어지는지 검사한다. 문항을 새로 생성하는 LLM은 사용하지 않는다

## 4. 실행 결과

| 실행 | 엄격 통과 | 답변 불가 안전 처리 | 근거 일치 | 공식 5필드 | 생성 통과 | fallback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 모델 없는 정답 배선 | 30/30 | 5/5 | 30/30 | 30/30 | 17/17 | 0 |
| 로컬 Qwen 최초 관측 | 30/30 | 5/5 | 30/30 | 30/30 | 16/17 | 1 |
| 호출 채점 수정 후 재실행 | 30/30 | 5/5 | 30/30 | 30/30 | 16/17 | 1 |
| 실제 Docker GET 최초 관측 | 24/30 | 5/5 | 24/30 | 30/30 | 12/13 | 1 |

호출 채점 수정 후 로컬 실행의 응답시간은 다음과 같다

| 지표 | 시간 |
| --- | ---: |
| 최소 | 0.099ms |
| 중앙값 p50 | 1,553.318ms |
| p95 | 3,876.727ms |
| 최대 | 4,398.949ms |

- QueryPlan provider 호출 13회, 오류 0
- grounded answer provider 호출 18회, 오류 0
- 답변 생성 대상은 17문항이지만 국내·해외 ETP 동시 검색 1문항은 상품군별로
  설명을 생성해 answer provider를 2회 호출
- 답하지 않아야 하는 5문항은 안전하게 차단하거나 빈 결과를 반환
- 단일 개발 서버에서 순차 1회 실행한 값이므로 운영 SLO로 해석하지 않음
- 최대 약 4.40초는 설명회 권장 60초보다 짧지만, HyperCLOVA X 네트워크 지연을
  측정한 값은 아님

### 4.1 실제 Docker GET 최초 관측

개발용 Qwen을 loopback에 띄우고 Backend 컨테이너를 local-test override로 다시 만든
뒤, 동결 30문항을 실제 `GET /answer` 네트워크 요청으로 순차 호출했다

| 항목 | 결과 |
| --- | ---: |
| 전체 의미 일치 | 24/30 |
| 공식 다섯 문자열 형식 | 30/30 |
| 답변 불가 안전 처리 | 5/5 |
| 60초 이내 응답 | 30/30 |
| 답변 생성 대상 중 실제 Qwen 도달 | 13/17 |
| Qwen 문장 검증 통과 | 12/13 |
| 안전 fallback | 1 |

실패 6건은 모두 공모펀드의 상세 1·개수 1·비교 1·검색 2·집계 1문항이다. 현재
Backend는 공모펀드 데이터가 준비돼 있어도 공식 실행을 허용하지 않으므로 모두
`clarification`으로 종료했다. 내부 기능 E2E에서는 명시적인 평가 전용 권한으로
공모펀드를 실행하기 때문에 30/30이지만, 실제 Docker 기본 정책과는 다르다

응답시간은 최소 1.261ms, p50 486.924ms, p95 2,491.057ms, 최대 2,885.126ms다.
공모펀드 6건은 검색을 실행하지 않아 매우 빨리 끝났으므로 이 지연을 네 상품군 전체
실행 성능으로 해석하지 않는다

최초 report는 `official-mock-http-v1-30-local-qwen-first.json`, SHA-256은
`6a809057e0d4d83a0bc809648e4eb9349e519baff08db2dddbeb5f0f9353551d`다.
전체 파일은 Git에서 제외하고 [집계 baseline](../evaluation/baselines/official-mock-http-v1-30.json)에
결과와 해석 한계를 보존한다

## 5. fallback 1건

대상 질문은 `원화 매수 가능한 국내채권을 매수수익률 높은 순으로 5개 보여줘`다

Qwen이 검색 상품과 `buy_yield_pct` 근거를 올바른 순서로 선택했지만 설명문에
`수익성 평가`라는 표현을 사용했다. 시스템은 이 표현을 단순 데이터 설명이 아니라
가치 판단으로 이어질 수 있는 문구로 분류했다

- 상품·순위·후보 수·수치·출처는 모두 정확
- provider 요청과 JSON 응답도 정상
- Answer Verifier의 `prose_has_no_advice_or_forecast` 검사에서만 실패
- 최종 응답은 모델 문장을 버리고 검증된 결정론적 답변으로 교체
- 잘못된 모델 문장이 사용자에게 전달되지 않음

이 결과를 숨기기 위해 질문이나 검증 규칙을 바꾸지 않는다. HyperCLOVA X 연결 후
같은 질문에서 생성 품질을 다시 확인하고, 필요할 때만 검증 실패 1회 재시도와 비용·
지연의 trade-off를 별도 실험한다

## 6. 최초 관측에서 발견한 채점기 오류

최초 report는 실제 QueryPlan provider 호출 13회를 예상 15회와 비교해 전체
`perfect=false`로 표시했다. 원인은 국내·해외 ETP 동시 검색의 상품군별 계획을
서버가 직접 만드는 데도 Qwen 호출 2회를 예상한 채점기 오류였다

- Agent 동작과 답변에는 영향 없음
- 세트와 정답도 변경하지 않음
- 예상 호출을 13회로 수정하고 회귀 테스트 추가
- 수정 후 실제 13회·예상 13회 일치
- 최초 report hash를 그대로 보존해 수정 전 관측을 덮어쓰지 않음

## 7. 재현

`finance_agent/`에서 모델 없는 기준 배선을 확인한다

```bash
python scripts/generate-official-mock-suite.py --check

python -m finance_agent_core.evaluation.official_mock_cli \
  --provider expected \
  --require-perfect \
  --require-no-fallback
```

개발 전용 Qwen 서버를 loopback으로 켠 뒤 실행한다

```bash
scripts/local-llm/serve-qwen.sh

FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
python -m finance_agent_core.evaluation.official_mock_cli \
  --provider local_test \
  --require-perfect
```

`--require-no-fallback`을 추가하면 모델 문장 생성 17건이 모두 검증을 통과해야 성공한다
현재 최초 관측에서는 1건 fallback이 있으므로 이 옵션을 사용한 실행은 의도대로 실패한다

실제 Docker 공식 GET 30문항은 Qwen Backend를 실행한 상태에서 다음과 같이 확인한다

```bash
python -m finance_agent_core.evaluation.official_mock_http_cli \
  --base-url http://127.0.0.1:18002 \
  --backend-profile local_test \
  --declared-model qwen3-local-test \
  --output artifacts/evaluation/official-mock-http-v1-30-local-qwen.json
```

`--require-perfect`는 현재 의도적인 공모펀드 잠금 6건 때문에 실패한다. 이 옵션을
빼면 최초 관측처럼 report를 보존하고 종료하며, 잠금을 승인 없이 해제해 30/30으로
만들지 않는다

전체 report는 `artifacts/evaluation/`에만 생성하며 Git에는 넣지 않는다. 집계 지표와
report SHA-256은 [baseline](../evaluation/baselines/official-mock-v1-30.json)에 보존한다

## 8. 해석 제한

- 실제 평가 문항이 아니라 설명회의 예상 분포만 모사한 문항
- 기존 공개 회귀 문항과 정답을 재사용한 self-authored 세트
- 독립 blind·금융 도메인 담당자 비공개 정답·사람 평가가 아님
- 로컬 Qwen 결과이며 HyperCLOVA X 생성 품질·비용·네트워크 지연이 아님
- 문서 RAG용 실제 외부 corpus가 없어 구조·투자전략·편입 종목 관계 질문은 제외
- 공모전 점수나 1등 가능성을 직접 증명하지 않음
- 실제 Docker 최초 관측 24/30은 공모펀드 실행 승인 전의 배포 정책 기준이며,
  공모펀드 기능 미구현이나 Qwen 생성 실패 6건을 뜻하지 않음

다음 공식 모델 평가는 정확한 HyperCLOVA X API 계약과 크레딧을 받은 뒤 이 세트를
변경하지 않고 별도 report로 수행한다
