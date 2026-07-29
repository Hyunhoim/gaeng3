# finance-agent-core

금융상품 Agent의 데이터 감사, 정규화 계약, QueryPlan, 결정론적 검색과 검증을 애플리케이션 shell과 독립적으로 개발하는 Python 패키지다.

현재 구현 범위는 원천 XLSX의 재현 가능한 감사, 네 상품군 정규화·SQLite,
네 상품군 field registry·QueryPlan 계약, 해외·국내 ETP·국내채권의 결정론적
검색·독립 검증·field-level evidence, Mock 및 개발 전용 로컬 LLM provider다.
공모펀드는 product·attribute·quarantine 저장, Oracle, Verifier, field
evidence, 동결 50문항 계약과 개발 전용 로컬 parser까지 구현했다. 공식 Agent
실행은 HCX schema·서버 계약 승인 전까지 비활성화 상태다.
최종 답변은 최소권한 GroundedAnswerDraft, Answer Verifier, 결정론적
evidence compiler와 safe fallback으로 구성할 수 있다.

## 환경

`finance_agent/` 디렉터리에서 실행한다.

```bash
/home/haeyeongcho/miniforge3/bin/conda env update -n gaeng3-dev -f environment.yml
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev \
  python -m pip install -r requirements/dev.txt
```

## 실제 데이터 감사

```bash
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev \
  finance-data-audit \
  --data-dir "../../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit
```

감사기는 다음을 보장한다.

- 입력 파일을 파일명 prefix로 찾고 특정 사용자 절대경로나 `(1).xlsx`에 의존하지 않는다.
- 원천 XLSX를 수정하지 않는다.
- 입력 크기와 SHA-256을 manifest에 기록한다.
- 국내채권·국내 ETP·해외 ETP·공모펀드의 핵심 구조와 품질 수치를 계산한다.
- 펀드 coverage를 raw row가 아니라 유효한 `itm_no` product grain에서 계산한다.
- `expectations.json`과 다른 값이 나오면 종료 코드 2로 실패한다.
- 출력은 `artifacts/` 아래에만 생성하며 Git 대상에서 제외된다.

## 계약

- [`field_registry.yaml`](src/finance_agent_core/config/field_registry.yaml): 네 상품군 canonical field, 상품군별 원천 매핑, 품질, 단위, 연산자와 실행 승인 상태
- [`queryplan.py`](src/finance_agent_core/contracts/queryplan.py): 서버의 엄격한 구조·의미 검증
- [`queryplan.hcx.schema.json`](src/finance_agent_core/contracts/queryplan.hcx.schema.json): HyperCLOVA X Structured Outputs용 보수적 schema
- [계약 설명](../../docs/contracts.md): 설계 근거, 첫 vertical slice 예시, 확장 규칙
- [공모펀드 계약](../../docs/public-fund-contract.md): product grain, capability, 품질 규칙, 실행 승인 조건

## 상품군 vertical slice

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset overseas_etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --provider mock \
  --output artifacts/e2e/mock-response.json
```

국내 ETP는 같은 명령에 `--dataset domestic_etp`와 국내 DB를 지정한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset domestic_etp \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --database artifacts/normalized/domestic_etp.sqlite3 \
  --provider mock \
  --output artifacts/e2e/domestic-mock-response.json
```

국내채권도 같은 공통 실행 계층을 사용한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset bond \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.agent \
  --database artifacts/normalized/bond.sqlite3 \
  --provider mock \
  --output artifacts/e2e/bond-mock-response.json
```

공모펀드는 정규화 DB 생성, 내부 Oracle 회귀와 로컬 development parser 평가를
지원하지만 Agent 실행은 아직 지원하지 않는다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.storage \
  --dataset fund \
  --data-dir "../../../2. Data/1. Raw/1.금융상품"
```

로컬 Qwen 연결은 [별도 테스트 런타임 문서](../../docs/local-llm.md)를 따른다.

## 핵심 50문항 평가

동결된 상품군별 질문·기대 QueryPlan·oracle과 평가 하네스를 모델 없이 검증한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset overseas_etp \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/expected-all.json
```

다른 suite는 `--dataset domestic_etp`, `bond`, `fund`로 선택한다.
결과와 한계는 [국내 ETP 평가 기준선](../../docs/evaluation-domestic-etp.md)과
[국내채권 평가 기준선](../../docs/evaluation-domestic-bond.md),
[공모펀드 평가 기준선](../../docs/evaluation-public-fund.md)에 기록한다.

`--provider local_test`는 로컬 Qwen 서버와 세 가지 명시적 opt-in이 모두
필요하다. 최초 holdout과 사후 회귀를 구분한 결과와 재현 절차는
[평가 기준선](../../docs/evaluation.md)에 기록한다. 공모펀드는
`--dataset fund --split development`만 기본 허용하며 결과와 holdout 잠금은
[공모펀드 평가 기준선](../../docs/evaluation-public-fund.md)에 기록한다.

## 근거 기반 답변 평가

답변 계층을 동결 expected QueryPlan과 분리해 평가한다.

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation.answer_cli \
  --dataset domestic_etp \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect
```

실제 로컬 Qwen은 세 가지 opt-in과 `--provider local_test`가 필요하다.
Agent 전체 E2E에서는 `--provider local_test --answer-provider local_test`를
함께 사용한다. 계약, 실패 과정, 지표 해석은
[근거 기반 최종 답변 평가](../../docs/evaluation-grounded-answers.md)에 기록한다.

## 검증

```bash
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev pytest
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev ruff format --check .
/home/haeyeongcho/miniforge3/bin/conda run -n gaeng3-dev ruff check .
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip check
```
