# 공모펀드 blind v1.1 평가 설계

상태: 프로토콜·계약·봉인 도구 구현 완료 · 독립 문항 작성 전

기준일: 2026-07-29

## 0. 결론

기존 `fund-core-50`은 개발자가 문항과 규칙을 모두 확인했으므로 앞으로 사후
회귀에만 사용한다. 새로운 일반화 성능은 금융 도메인 담당자가 독립 작성한
`fund-blind-v1.1-100`으로 측정한다.

blind의 핵심은 질문을 어렵게 만드는 것이 아니라 다음 순서를 지키는 것이다.

1. 평가 도구와 parser 코드를 먼저 commit
2. AI 담당자와 분리된 작성자가 문항·정답키 작성
3. 기존 문항 중복·분포·공모 범위·Oracle을 자동 검증
4. parser commit, 질문 파일, 비공개 정답키를 SHA-256으로 봉인
5. 같은 parser commit에서 로컬 Qwen 최초 실행을 한 번만 허용
6. 성공과 실패를 수정하지 않은 최초 report로 보존
7. 결과를 본 뒤의 수정은 사후 회귀로만 보고

현재 실제 100문항은 만들지 않는다. AI 담당자가 문항과 정답을 먼저 작성하면
blind라고 부를 수 없기 때문이다.

## 1. 권장 역할 분리

| 역할 | 권장 담당 | 접근 범위 |
| --- | --- | --- |
| 독립 문항 작성자 | Financial Domain | 질문과 의도·정답 근거 작성 |
| 평가 steward | Frontend & Backend 또는 합의된 비-AI 담당자 | 정답키 검증, Oracle 확인, hash 봉인, 최초 실행 관리 |
| 평가 대상 코드 담당 | AI Agent | parser commit 동결, 모델 서버 준비, 최초 report 이후 오류 분석 |

- 문항 작성자는 기존 `fund-core-50` 문장을 복사하거나 단어만 바꾸지 않음
- AI 담당자는 최초 report가 생성되기 전에 새 질문·비공개 정답키를 열지 않음
- 평가 steward는 최초 모델 실행 전까지 비공개 정답키를 AI 담당자에게 설명하지 않음
- 팀 인원이 부족해 한 사람이 두 역할을 맡으면 해당 한계를 결과에 명시

## 2. 100문항 표본 계약

### 금융 범주

| 범주 | 문항 수 | 주요 검증 |
| --- | ---: | --- |
| `scope_status` | 10 | 공모 범위, 판매 상태, 당사 판매 |
| `classification` | 14 | 국내·해외·혼합, 운용 속성, 투자지역 |
| `risk_hedge` | 10 | 위험등급, 투자자 유형, 환헤지 |
| `return` | 14 | 1주·1개월·3개월·6개월 수익률과 경계값 |
| `aum_currency` | 10 | 원화·달러 AUM, 통화 누락 차단 |
| `lookup` | 8 | 상품번호, 정식명, 짧은 이름 |
| `compound` | 16 | 서로 다른 필드 3개 이상의 복합 조건 |
| `safety` | 18 | 추천, 장기 수익률, 보수, 클래스 집계 등 차단 |
| 합계 | 100 |  |

### 자연어 표현 유형

| 표현 유형 | 문항 수 | 작성 규칙 |
| --- | ---: | --- |
| `explicit` | 20 | 정확한 `공모펀드` 표현 포함 |
| `paraphrase` | 25 | 기존 suite와 다른 자연스러운 표현 |
| `implicit_public_scope` | 20 | `공모펀드`를 생략하고 `펀드`·`상품` 등으로 표현 |
| `colloquial_ellipsis` | 15 | 구어체·생략·어순 변화 |
| `noisy_surface` | 10 | 띄어쓰기·영문 단위·기호 등 표면 변형 |
| `adversarial` | 10 | 지원 조건과 미지원 조건을 함께 섞어 잘못된 실행 유도 |
| 합계 | 100 |  |

### 기대 처리

| 처리 | 문항 수 | 의미 |
| --- | ---: | --- |
| `execute` | 72 | QueryPlan 실행 후 Oracle·Verifier까지 일치 |
| `ambiguity` | 12 | 조건이 부족해 역질문 또는 명시적 차단 |
| `unsupported` | 16 | 데이터·grain·field capability 밖의 요구 차단 |
| 합계 | 100 |  |

분포는 코드에서 정확히 검증하며 임의로 쉬운 문항을 더 넣어 비율을 바꿀 수 없음

### 범주별 처리 교차 분포

| 범주 | 실행 | 모호성 | 미지원 | 합계 |
| --- | ---: | ---: | ---: | ---: |
| `scope_status` | 8 | 1 | 1 | 10 |
| `classification` | 12 | 1 | 1 | 14 |
| `risk_hedge` | 8 | 1 | 1 | 10 |
| `return` | 12 | 1 | 1 | 14 |
| `aum_currency` | 8 | 2 | 0 | 10 |
| `lookup` | 8 | 0 | 0 | 8 |
| `compound` | 14 | 1 | 1 | 16 |
| `safety` | 2 | 5 | 11 | 18 |
| 합계 | 72 | 12 | 16 | 100 |

`safety`의 실행 2문항은 위험해 보이는 단어가 있어도 공식 데이터로 안전하게
처리할 수 있는 경계 사례. 모든 안전 문항을 무조건 차단하는 parser도 통과하지
못하도록 포함

### 표현별 처리 교차 분포

| 표현 유형 | 실행 | 모호성 | 미지원 | 합계 |
| --- | ---: | ---: | ---: | ---: |
| `explicit` | 16 | 2 | 2 | 20 |
| `paraphrase` | 19 | 3 | 3 | 25 |
| `implicit_public_scope` | 15 | 2 | 3 | 20 |
| `colloquial_ellipsis` | 11 | 2 | 2 | 15 |
| `noisy_surface` | 7 | 1 | 2 | 10 |
| `adversarial` | 4 | 2 | 4 | 10 |
| 합계 | 72 | 12 | 16 | 100 |

교차 분포도 자동 검사. 특정 표현 유형을 모두 차단하거나 쉬운 실행 문항으로만
채우는 구성을 허용하지 않음

## 3. 문항 작성 규칙

- 모든 문항은 한 가지 정답 QueryPlan으로 해석 가능한지 먼저 확인
- 실행 문항은 공식 데이터에서 확인 가능한 필드만 사용
- 공모 범위 `public_offering=true`, `locked`를 정답키에 정확히 한 번 포함
- AUM 실행 문항은 `trading_currency=KRW` 또는 `USD`를 정확히 지정
- 차단 문항은 실행 가능한 ranking을 남기지 않음
- 모호성과 미지원을 구분하고 정답 근거를 최소 10자 이상 기록
- 상품 ID나 실제 상위 결과를 질문 문장에 노출하지 않음
- 기존 core-50과 정규화 문자열 유사도 0.84 이상이면 봉인 전에 자동 거절
- 자동 중복 검사는 문자열 기준이므로 평가 steward가 의미상 복제 여부도 수동 확인
- 실제 사용자 발화처럼 작성하되 오탈자를 과도하게 만들어 난이도를 인위적으로
  높이지 않음

## 4. 두 파일과 commitment

질문 파일:

```json
{
  "schema_version": "1.0",
  "suite_id": "fund-blind-v1.1-100",
  "dataset": "fund",
  "author_role": "financial_domain",
  "cases": [
    {
      "id": "fund-blind-v1.1-001",
      "question": "독립 작성 질문",
      "category": "compound",
      "language_profile": "implicit_public_scope"
    }
  ]
}
```

비공개 정답키:

```json
{
  "schema_version": "1.0",
  "suite_id": "fund-blind-v1.1-100",
  "dataset": "fund",
  "database_sha256": "64자리 hash",
  "manifest_sha256": "64자리 hash",
  "cases": [
    {
      "id": "fund-blind-v1.1-001",
      "constraints": [],
      "ranking": [],
      "limit": 5,
      "disposition": "block",
      "blocker": "unsupported",
      "oracle": null,
      "rationale": "현재 펀드 클래스 grain에서 지원하지 않는 집계"
    }
  ]
}
```

실제 파일은 001부터 100까지 정확히 포함해야 함. 예시는 구조 설명용이라 검증
가능한 완성본이 아님

commitment에는 다음을 기록

- 동결된 40자리 parser commit
- 질문 파일 SHA-256
- 정답키 SHA-256
- 작성 역할, 생성 시각, 문항 수

질문이나 정답키의 공백 한 글자라도 바뀌면 verify가 실패

## 5. 봉인 전 절차

실제 파일은 Git에 넣지 않고 `artifacts/blind-evaluation/` 또는 평가 steward의
비공개 작업공간에서 관리

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/blind-fund-eval.py validate \
  --questions artifacts/blind-evaluation/fund-v1.1.questions.json \
  --answers artifacts/blind-evaluation/fund-v1.1.answers.private.json
```

평가 steward는 72개 실행 문항의 Oracle·Verifier가 모두 일치하는지도
비공개로 확인. 한 건이라도 실패하면 정답키를 고친 뒤 다시 검사하고, 이
과정에서는 로컬 LLM을 호출하지 않음

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/blind-fund-eval.py oracle-check \
  --questions artifacts/blind-evaluation/fund-v1.1.questions.json \
  --answers artifacts/blind-evaluation/fund-v1.1.answers.private.json \
  --workers 4 \
  --output artifacts/blind-evaluation/fund-v1.1.expected-private.json
```

AI 담당자가 parser commit을 동결한 뒤 전체 SHA를 전달

```bash
git rev-parse HEAD
```

그 commit을 포함해 commitment 생성

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/blind-fund-eval.py commit \
  --questions artifacts/blind-evaluation/fund-v1.1.questions.json \
  --answers artifacts/blind-evaluation/fund-v1.1.answers.private.json \
  --parser-commit <40자리-parser-commit> \
  --output artifacts/blind-evaluation/fund-v1.1.commitment.json
```

## 6. 최초 실행 절차

AI 담당자의 checkout이 commitment의 parser commit과 같고 worktree가 clean인지
먼저 확인

```bash
git status --short
git rev-parse HEAD
```

`run` 명령도 현재 `HEAD`와 clean worktree를 다시 검사하며 다르면 모델 호출 전에
실패

봉인 검증:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/blind-fund-eval.py verify \
  --questions artifacts/blind-evaluation/fund-v1.1.questions.json \
  --answers artifacts/blind-evaluation/fund-v1.1.answers.private.json \
  --commitment artifacts/blind-evaluation/fund-v1.1.commitment.json \
  --parser-commit <40자리-parser-commit>
```

최초 로컬 Qwen 실행:

```bash
FINANCE_AGENT_LLM_MODE=local_test \
ENABLE_NON_HCX_TEST_LLM=1 \
LLM_PROVIDER=local_test \
LOCAL_TEST_LLM_BASE_URL=http://127.0.0.1:18000/v1 \
LOCAL_TEST_LLM_MODEL=qwen3-local-test \
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/blind-fund-eval.py run \
  --questions artifacts/blind-evaluation/fund-v1.1.questions.json \
  --answers artifacts/blind-evaluation/fund-v1.1.answers.private.json \
  --commitment artifacts/blind-evaluation/fund-v1.1.commitment.json \
  --parser-commit <40자리-parser-commit> \
  --provider local_test \
  --workers 4 \
  --confirm-first-run \
  --first-run-state artifacts/blind-evaluation/fund-v1.1.first-run.json \
  --output artifacts/evaluation/fund-blind-v1.1-first-run.json
```

`first-run-state`는 원자적으로 한 번만 생성. 같은 경로가 존재하면 재실행을
거절하며 완료 후 report 이름과 hash를 기록

## 7. 사전 등록 지표

최초 실행 전에 다음 지표와 목표를 고정

| 지표 | 목표 |
| --- | ---: |
| schema valid | 99% 이상 |
| strict plan accuracy | 90% 이상 |
| constraint exact | 95% 이상 |
| Oracle exact | 실행 문항의 98% 이상 |
| safety disposition | 100% |
| blocker 종류 exact | 95% 이상 |
| hard-constraint violation | 0건 |
| 범주별 strict accuracy | 각각 80% 이상 |

latency p50·p95·max는 기록하되 로컬 GPU 수치를 공식 API 합격 기준으로 사용하지
않음. 목표 미달이어도 최초 report를 삭제하거나 재실행하지 않음

## 8. 결과 해석 규칙

- 최초 점수는 `first_run`으로 영구 보존
- 오류를 본 뒤의 수정 결과는 `post_fix_regression`으로만 표기
- Qwen 단독 성능이 아니라 모델·linker·계약·Oracle·Verifier 전체 시스템 점수
- 같은 작성자가 질문과 정답을 만들거나 AI 담당자가 최초 report 전에 문항을 보면
  `developer_blind`가 아니라 `model_unseen`으로 격하해 표기
- HyperCLOVA X와 공식 공모전 성능을 대변하지 않음
- 공식 Agent의 `fund execution_enabled=false`는 별도 승인 전까지 유지

## 9. 현재 남은 작업

1. 팀에서 독립 작성자와 평가 steward 확정
2. 두 사람이 저장소 밖에서 100문항과 비공개 정답키 작성
3. Oracle·분포·중복 검사 통과
4. AI parser commit 동결 및 commitment 생성
5. 최초 로컬 Qwen 1회 실행
