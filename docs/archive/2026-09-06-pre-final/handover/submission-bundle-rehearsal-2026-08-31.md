# 제출 소스 묶음 예행연습

작성일: 2026-08-31
기준 commit: `2b80c66`
방식: 저장소를 건드리지 않고 `git archive HEAD`로 뜬 복사본에서만 실험

> 마감 직전에 처음 제출 묶음을 만들면 정리 과정에서 무엇이 깨지는지 알 수 없다.
> 미리 한 번 만들어 보고 실패 지점을 기록한다.

## 0. 결론

**로컬 LLM 코드를 파일 단위로 지우는 방식은 쓸 수 없다.** 실행 경로 import가 끊기고,
그것을 고치면 평가 CLI와 테스트로 연쇄가 이어진다. 실험에서 제거 대상이 22개 파일을
넘어선 뒤에도 collection 오류가 8건에서 30건으로 늘었다.

**개발 산출물만 제외하는 방식은 안전하다.** 코드를 건드리지 않으므로
`Agent Core 1,916 passed`와 `Backend 466 passed`가 그대로 유지된다.

## 1. 두 시나리오 비교

| | A. 개발 산출물만 제외 | B. 로컬 LLM 코드까지 제거 |
| --- | --- | --- |
| 제외 파일 | 116개 | 144개 이상 |
| `--profile submission` 차단 | 1,433 → 742 | 1,433 → 471 |
| Agent Core 테스트 | **1,916 passed** | **collection 실패** |
| Backend 테스트 | **466 passed** | 미측정 |
| 실행 경로 import | 정상 | 3곳 수정 후 정상 |
| 작업 성격 | 파일 제외만 | 소스 수정 + 연쇄 추적 |

### A안에서 제외한 것

| 경로 | 파일 | 성격 |
| --- | ---: | --- |
| `src/finance_agent/docs/` | 60 | 개발 문서 |
| `src/finance_agent/notebooks/` | 2 | 감사 노트북 |
| `src/finance_agent/scripts/local-llm/` | 2 | 로컬 모델 실행 스크립트 |

`src/finance_agent/evaluation/baselines/`는 처음에 제외했다가 되돌렸다.
`test_schema_embedding_tracked_baselines.py`가 이 디렉터리를 직접 읽어
`FileNotFoundError`로 실패한다. 제출 묶음에서도 유지해야 한다.

## 2. B안이 실패하는 경로

로컬 provider 파일 네 개를 지우면 곧바로 실행 경로가 끊긴다.

```
ModuleNotFoundError: No module named
  'finance_agent_core.agent.providers.local_test'
```

세 곳을 고쳐야 Agent가 다시 뜬다.

- `agent/providers/__init__.py` — import와 `__all__` 항목
- `answering/providers.py` — import와 `LocalGroundedAnswerProvider` 클래스 전체
- `answering/__init__.py` — 재수출

여기까지 고치면 실행 경로 import는 복구되고 Backend도 뜬다. 그러나 테스트 13개와
평가 CLI 22개가 지워진 심볼을 계속 참조한다. 그것들을 제거하면
`evaluation/__init__.py`의 재수출이 끊겨 collection 오류가 오히려 늘어난다.

`local_test`는 평가 하위 시스템(blind·metamorphic·coverage·red-team·comparison CLI)에
깊게 엮여 있다. 제출 묶음에서 이 코드를 걷어내려면 평가 하위 시스템을 함께 정리하는
설계 판단이 필요하며, 마감 6일 전에 착수할 범위가 아니다.

## 3. 남은 차단 742건의 성격

A안 적용 후 남은 것은 모두 **문자열 표식**이며 실행 파일이 아니다.

| 건수 | 위치 |
| ---: | --- |
| 442 | 평가 도구·문항 |
| 119 | 테스트 |
| 98 | 기타 |
| 46 | 제출 문서 |
| 37 | 실행 소스 |

실행 소스 37건은 `agent/providers/local_test.py`(15), `config/submission_boundary.py`(12),
`agent/cli.py`(4), `answering/providers.py`(3) 등이다.
`submission_boundary.py`의 12건은 **탐지 규칙 자체가 표식 문자열을 담고 있어서** 잡히는
것이므로 제거 대상이 아니다.

## 4. 판단이 필요한 지점

주최 측은 **평가·제출 경로에서 실행되는 LLM**을 HyperCLOVA X로 제한했다. 개발 단계
사용을 금지하는 문구는 공지·기술질문답변 어디에도 없고, 제출 소스에 개발 이력이
남아도 되는지에 대한 공식 답변도 없다.

현재 런타임은 이미 삼중으로 차단되어 있다.

- `config.py`가 `local_test`/Qwen provider를 시작 단계에서 거부
- 로컬 provider는 환경변수 3중 opt-in과 loopback 제한을 동시에 요구
- 이미지 경계 검사가 `ENABLE_NON_HCX_TEST_LLM`을 포함해 확인

따라서 선택지는 두 가지다.

**A안으로 제출하고 경계를 문서로 설명한다.** 실행 경로에서 HyperCLOVA X 외 provider가
선택될 수 없음을 자동 검사로 보이고, 개발 이력을 숨기지 않는다. 테스트가 온전히
남으므로 재현 가능성이 유지된다.

**B안을 완주한다.** 평가 하위 시스템 정리를 포함한 별도 작업으로 다루고,
제출 이후 또는 충분한 시간을 확보한 뒤 진행한다.

## 5. 재현 절차

```bash
git archive HEAD | tar -x -C <임시경로>
cd <임시경로> && git init -q && git add -A && git commit -q -m base

rm -rf src/finance_agent/docs src/finance_agent/notebooks src/finance_agent/scripts/local-llm

PYTHONPATH=src/finance_agent/packages/finance_agent_core/src \
  python src/finance_agent/scripts/check-submission-boundary.py --profile submission

cd src/finance_agent/packages/finance_agent_core && PYTHONPATH=src python -m pytest -q
cd - && PYTHONPATH=src/finance_agent/packages/finance_agent_core/src python -m pytest src/fastapi_backend/tests -q
```

Backend는 `umask 022`가 필요하다. 이 서버의 `umask 002`에서는 릴리스 보안 검사가
테스트 fixture를 group-writable로 판정해 39건이 실패한다.
