# 재현 가능한 평가 baseline

이 디렉터리는 Git에서 제외되는 전체 `artifacts/evaluation/*.json` 대신,
평가의 재현 조건과 집계 지표만 보존한다.

baseline에는 다음을 포함한다.

- 동결 suite ID·버전·SHA-256
- 정규화 DB·manifest·원천 파일 SHA-256
- 로컬 테스트 모델과 고정 revision
- split, strict accuracy, 안전·근거 지표와 latency
- 전체 로컬 report의 파일명과 SHA-256
- 재현 명령과 해석 한계

개별 상품명, 상품 ID, 원천 행 값, 전체 답변과 모델 로그는 포함하지 않는다.
DB, 원천 XLSX, 전체 report, 모델 가중치는 계속 `artifacts/` 또는 로컬 cache에
두고 Git에서 제외한다.

## baseline 목록

| 파일 | 범위 |
| --- | --- |
| [해외 ETP QueryPlan](baselines/overseas-etp-queryplan-v1.json) | 로컬 Qwen hybrid parser 사후 회귀 |
| [국내 ETP QueryPlan](baselines/domestic-etp-queryplan-v1.json) | development와 최초 local-inference holdout |
| [국내 ETP 답변](baselines/domestic-etp-answer-v1.json) | 최소권한 grounded answer |
| [국내채권 QueryPlan](baselines/domestic-bond-queryplan-v1.json) | parser·Oracle·안전 차단 |
| [국내채권 답변](baselines/domestic-bond-answer-v1.json) | grounded answer·빈 결과·안전 차단 |
| [공모펀드 QueryPlan·Oracle](baselines/public-fund-queryplan-v1.json) | expected plan·Oracle·안전 차단 계약 |
| [공모펀드 로컬 development](baselines/public-fund-local-development-v1.json) | 로컬 Qwen hybrid parser 개발 40문항·holdout 잠금 |
| [공모펀드 최초 holdout](baselines/public-fund-local-holdout-first-run-v1.json) | commit 이후 최초 10문항 9/10·실패 원인 보존 |
| [공모펀드 답변](baselines/public-fund-answer-v1.json) | 최소권한 grounded answer·최종 컴파일 검증·fallback |
| [공모펀드 비교 답변](baselines/public-fund-compare-v1.json) | 두 상품 true COMPARE·차이 계산·통화·결측·fallback |
| [공모펀드 자연어 비교](baselines/public-fund-compare-parser-v1.json) | 상품명·짧은 이름·상품번호 resolver·COMPARE parser·안전 차단 |
| [공모펀드 자연어 비교 E2E](baselines/public-fund-compare-e2e-v1.json) | 자연어 parser·resolver·Oracle·검증 답변·안전 차단 통합 |
| [세 상품군 자연어 비교](baselines/product-compare-v1.json) | 해외·국내 ETP·국내채권 30문항의 결정론적 비교·Backend 계약·안전 차단 |
| [SEARCH·AGGREGATE 성능](baselines/search-aggregate-performance-v1.json) | 네 상품군 8문항의 결과 지문·새 프로세스 지연·RSS·경량 verifier 전후 비교 |
| [연결 전 라우팅 초기 진단 v1](baselines/pre-hcx-route-diagnostic-initial-v1.json) | AGGREGATE 미지원 시점의 Router 도입 전 search 강제 동작 4/28 |
| [연결 전 라우팅 개선 진단 v1](baselines/pre-hcx-route-diagnostic-improved-v1.json) | AGGREGATE 미지원 시점의 네 상품군·일곱 intent 라우팅 28/28 |
| [연결 전 라우팅 초기 진단 v2](baselines/pre-hcx-route-diagnostic-initial-v2.json) | 현재 AGGREGATE 기대값을 적용한 도입 전 replay 4/28 |
| [연결 전 라우팅 개선 진단 v2](baselines/pre-hcx-route-diagnostic-improved-v2.json) | 네 상품군 AGGREGATE 실행을 포함한 현재 회귀 28/28 |
| [연결 전 라우팅 초기 진단 v3](baselines/pre-hcx-route-diagnostic-initial-v3.json) | AGGREGATE·COMPARE 기대값을 적용한 도입 전 replay 4/28 |
| [연결 전 라우팅 개선 진단 v3](baselines/pre-hcx-route-diagnostic-improved-v3.json) | 네 상품군 AGGREGATE·same-family COMPARE 실행을 포함한 현재 회귀 28/28 |

자연어 비교 E2E baseline은 실행 16문항의 실제 `ComparisonCell.value`와 field
evidence provenance를 서로 별도의 fingerprint로 동결한다. parser 안전 계약은
ordered identity·정확한 연결어·위치별 문장부호 문법과 질문 전체 잔여 표현,
제외·대신·포함 역할, 빈·미종결·역방향·중첩·줄바꿈 따옴표 차단을 포함한다.

`product-compare-core-30`은 공모펀드와 겹치지 않는 세 상품군의 실행 18문항과
안전 차단 12문항을 동결한다. 기존 공모펀드 24문항과 합쳐 네 상품군 자연어
COMPARE 공개 회귀 54문항을 구성한다.

`search-aggregate-performance-8`은 네 상품군에서 SEARCH와 AGGREGATE를 하나씩
새 프로세스로 실행한다. 후보 수와 결과 지문이 모두 일치해야 통과하며 지연과
추가 RSS는 같은 장비의 방향성 기준선으로만 사용한다.

이 수치는 HyperCLOVA X나 공식 공모전 평가 결과가 아니다. 동결된 개발 질문에서
로컬 LLM, 결정론적 linker, 계약, Oracle과 Verifier를 합친 시스템의 회귀
기준선이다.

대부분의 baseline은 완전 통과한 회귀 기준이다. `holdout_first_run_observed`
상태는 최초 실행 결과가 완전하지 않아도 수정하거나 숨기지 않고 관측값 그대로
보존한다.

라우팅 v1은 AGGREGATE 미지원, v2는 COMPARE가 공모펀드에만 열렸던 당시의
봉인 이력이다. 현재 capability 정본은 원본을 수정하지 않고 별도
suite·commitment로 봉인한 v3를 사용한다.

## 검사

```bash
conda run -n gaeng3-dev python scripts/check-docs.py
```

검사기는 baseline 구조, SHA-256 형식, suite 실제 hash, 문서 인덱스와 로컬
Markdown 링크를 함께 확인한다.

## 새 blind 평가

공모펀드 다음 일반화 평가는 기존 50문항을 늘려 쓰지 않고 독립 작성한
100문항으로 수행한다. 질문·비공개 정답키·parser commit을 hash로 봉인하고
최초 실행 상태 파일로 중복 실행을 막는다.

- [blind v1.1 설계](../docs/evaluation-public-fund-blind-v1.1.md)
- 검증·봉인·실행 도구: `scripts/blind-fund-eval.py`

실제 문항과 비공개 정답키는 첫 실행 전 Git에 포함하지 않는다.

## HyperCLOVA X 연결 전 source freeze

내부 준비 코드·테스트·문서·baseline·protocol의 정렬된 source tree hash는
[`pre-hcx-readiness-v1.manifest.json`](protocols/pre-hcx-readiness-v1.manifest.json)에
보존한다. 이 manifest는 실제 external blind, 승인 corpus, 사람 평가와
HyperCLOVA X 재현이 남아 있음을 status와 external gate로 함께 기록한다.
`scripts/check-docs.py`가 현재 파일 수와 tree SHA-256을 매번 재계산한다.
