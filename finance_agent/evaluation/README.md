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

이 수치는 HyperCLOVA X나 공식 공모전 평가 결과가 아니다. 동결된 개발 질문에서
로컬 LLM, 결정론적 linker, 계약, Oracle과 Verifier를 합친 시스템의 회귀
기준선이다.

대부분의 baseline은 완전 통과한 회귀 기준이다. `holdout_first_run_observed`
상태는 최초 실행 결과가 완전하지 않아도 수정하거나 숨기지 않고 관측값 그대로
보존한다.

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
