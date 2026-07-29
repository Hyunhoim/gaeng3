# 개발 환경과 현재 구현 상태

상태: 현재 정본
기준일: 2026-07-29

## 저장소 상태

- 로컬 branch: `haeyeongcho`
- upstream: `origin/haeyeongcho`
- 원격 commit: `f366414e16e4e0eb00d08a68920d32af4a24b740`
- 원격 `main`, `hyunhoim`, `haeyeongcho`는 모두 같은 초기 README commit을 가리킨다.
- Agent Core 기반은 로컬 commit `a65c2b2`로 보존했다.
- 온보딩·평가 baseline·문서 검사 강화는 기반 commit 다음의 별도 commit으로
  관리하며, 원격 push는 아직 하지 않는다.

동료가 가져올 `vintasoftware/nextjs-fastapi-template`은 `fastapi_backend`, `nextjs-frontend`, `docs`를 사용한다. 충돌을 줄이기 위해 AI·데이터 코드는 [finance_agent_core](../packages/finance_agent_core/README.md)에서 독립적으로 시작했다. 템플릿의 `docs/README.md` symlink와 충돌하지 않도록 프로젝트 문서 인덱스는 `docs/project-index.md`를 사용한다.

## 로컬 도구

| 도구 | 확인된 버전·상태 |
| --- | --- |
| Conda | Miniforge `26.3.2`, `/home/haeyeongcho/miniforge3/bin/conda` |
| 프로젝트 Python | `gaeng3-dev`, Python `3.12.13` |
| pip | `26.1.2` |
| Node.js | NVM Node `24.18.0` |
| npm | `11.16.0` |
| Git | `2.34.1` |
| Docker | 현재 호스트 PATH에서 없음 |

Conda는 Python 환경을 격리하고 pip는 Python 패키지를 설치한다. Frontend Node는 동료 템플릿과 NVM 설정을 존중하며 Conda에 중복 설치하지 않는다.

## 환경 생성

저장소 루트에서 실행한다.

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
SOURCE_DATE_EPOCH=1785283200 \
  /home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python -m pip wheel \
  --no-deps \
  --no-build-isolation \
  --wheel-dir packages/finance_agent_core/dist \
  packages/finance_agent_core
```

2026-07-29 결과:

- Ruff: 통과
- pytest: 64개 통과
- pip dependency check: 통과
- 문서 링크·인덱스·평가 baseline·suite hash 검사: 통과
- 고정 `SOURCE_DATE_EPOCH` wheel을 서로 다른 임시 디렉터리에서 두 번 빌드해
  byte hash 일치, SHA-256
  `a199a8b532537256be43c6e1da2a742b27e6ce063ade23b727efabaab8a33431`

## P1 계약 구현

- [Field Registry와 QueryPlan 계약](contracts.md)
- 해외·국내 ETP·국내채권 49개 canonical field와 상품군별 field capability를 Pydantic으로 검증
- 서버 QueryPlan은 추가 property, 타입·단위·enum·연산자, intent payload를 fail-closed로 검증
- HCX 전송 schema는 공식 Structured Outputs keyword subset만 사용
- registry의 queryable·sortable·selectable·aggregatable field와 HCX enum 정합성을 테스트
- 아직 동결되지 않은 공모펀드와 교차 상품군 QueryPlan은 명시적으로 거절

## 상품 정규화·검색

- 해외 ETP: 5,646행, sparse 10행 격리, 검색 가능 5,636행
- 국내 ETP: 1,734행, 손상 Excel 1155행 격리, 검색 가능 1,733행
- 국내채권: 42,394행, 격리 없이 전 행 검색 가능, 실제 매수 가능 254행
- 세 상품군은 별도 SQLite·manifest를 사용하고 같은 QueryPlan,
  parameterized oracle, 독립 verifier, field evidence 계약을 공유한다.
- 국내 DB와 manifest를 임시 경로에 재구축했을 때 원본 artifact와 SHA-256이
  byte 단위로 일치했다.

## 원천 데이터 감사

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/finance-data-audit \
  --data-dir "../../2. Data/1. Raw/1.금융상품" \
  --output-dir artifacts/data-audit
```

결과:

- 국내채권 42,394행 × 40열
- 국내 ETP 1,734행 × 73열
- 해외 ETP 5,646행 × 49열
- 공모펀드 95,619행 × 45열
- 회귀 expectation 49/49 통과
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

## 근거 기반 최종 답변

[근거 기반 최종 답변 평가](evaluation-grounded-answers.md)는 기대 QueryPlan으로
답변 계층을 격리해 측정한다. 로컬 Qwen에는 실제 값·날짜·상품 식별자를 주지
않고 opaque result reference와 field label만 제공한다. Answer Verifier가
순위·evidence·숫자·식별자·투자 해석·경고를 검사하며 실패 시 결정론적
renderer로 폴백한다.

최종 국내 ETP 50문항은 47개 grounded 생성과 3개 안전 차단을 통과했다.
국내채권은 46개 grounded 생성, 1개 결정론적 빈 결과, 3개 안전 차단으로
50/50이며 폴백은 0건이었다. 자유 생성 LLM 점수가 아니라 제한된 hybrid
system의 계약 준수율이며, 사람 기준의 표현 품질과 새 blind 질문 평가는 아직
남아 있다.
