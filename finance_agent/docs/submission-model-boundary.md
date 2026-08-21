# 제출용 모델 경계와 로컬 LLM 정리 메모

기준일: 2026-08-21

## 0. 결정

- 현재 로컬 Qwen 연결 코드와 실험 기록은 HyperCLOVA X가 없던
  개발 단계의 내부 검증용으로만 관리
- 예선 평가·제출물에서 실행되는 LLM은 HyperCLOVA X로 제한
- 공식 Direct v3 API 규격, HTTP transport와 FastAPI 조립을 구현하고 실제 credential로
  answer-only DETAIL·SEARCH 호환성을 확인
- 최종 evaluation artifact는 HCLX answer-only로 고정하고 HCLX QueryPlan·Dense를 OFF,
  공모펀드를 locked로 유지
- 개발 tree의 로컬 LLM 실험 파일과 평가 runtime의 실행 권한을 구분하며,
  evaluation·production에서는 HCLX 외 provider를 시작 단계에서 거부

핵심은 로컬 모델 사용 이력을 숨기는 것이 아니라, **내부 개발 이력과
공식 제출 경로를 투명하게 분리**하는 것

## 1. 왜 나중에 정리해야 하는가

현재 저장소에는 로컬 Qwen으로 질문 분석·근거 답변·fallback을
반복 시험한 개발 코드가 있음. 이 코드는 현재 세 개의 명시적인
설정을 동시에 켜야만 작동하지만, 제출 심사에서는 다음 항목이
혼란을 줄 수 있음

- `local_test` provider 코드
- `Qwen`, `vLLM`, `ENABLE_NON_HCX_TEST_LLM` 같은 설정값
- `scripts/local-llm/` 실행 스크립트
- 로컬 모델 실험 명령이 포함된 개발 문서
- 로컬 모델명과 결과가 담긴 평가 baseline

따라서 개발 저장소를 그대로 제출하지 않고, 공식 규칙에 맞는
제출 후보를 따로 검사해야 함

## 2. 지금 지우지 않는 이유

- 실제 answer-only credential·응답 호환성은 확인했지만 signed NCP release의 latency,
  quota·429·5xx와 공인 IP 경로는 아직 검증하지 않음
- 현재 로컬 provider는 Router·Oracle·Verifier·fallback을 저비용으로
  반복 검사하는 개발 도구임
- 먼저 삭제하면 HyperCLOVA X adapter와 비교할 기준을 잃을 수 있음
- 공식 규칙이 “최종 실행 경로”만 제한하는지, “제출 소스·Git 이력”까지
  제한하는지 먼저 확인해야 함

정리 전에 과거 이력을 재작성하거나 Git history를 숨기는 작업은 하지 않음

## 3. 설명회 이후에도 남은 확인 질문

1. 다른 LLM의 “사용”이 최종 평가 API 실행만 뜻하는지
2. 개발 단계의 로컬 LLM 실험 코드·문서·Git 이력도 제출 제한인지
3. 제출 저장소에 모델 가중치가 없어도 로컬 provider adapter가 있으면
   제외 사유인지
4. 평가 모드에서 HyperCLOVA X만 선택되는 fail-closed 검사로 충분한지
5. 마지막 제출은 전체 개발 Git 저장소인지, 별도 release 소스 묶음인지

설명회 자료만으로 위 항목은 해결되지 않았음. 공식 답변은 날짜·질문·답변 원문·
팀 해석을 구분해 보존

## 4. 공식 답변 확인 후 제출 후보 정리 순서

1. 공식 답변으로 제출 범위 확정
2. 발급받은 credential로 HyperCLOVA X answer-only smoke 완료
3. HCLX final profile에서 QueryPlan·Oracle·Verifier·fallback 회귀 통과
4. 제출 후보에서 로컬 provider·스크립트·설정·의존성 제거
5. 로컬 모델명·실행 명령·실험 baseline이 제출 문서에 섞이지 않았는지 검사
6. 평가·production 모드에서 `LLM_PROVIDER=hyperclova` 외 시작 실패 확인
7. 모델 weight·cache·로컬 응답·API key·개인 경로 미포함 확인
8. clean checkout에서 build·test·start 재현

## 5. 제출 전 필수 검사표

| 검사 대상 | 통과 조건 |
| --- | --- |
| LLM provider | 공식 평가 assembly에서 HyperCLOVA X 외 provider 활성화 불가 |
| 설정 | evaluation·production에서 `local_test`, Qwen, vLLM 선택 불가 |
| 의존성 | runtime image에 로컬 LLM server·model dependency·weight 미포함 |
| 파일 | 모델 weight·cache·SQLite·`artifacts/`·credential 미포함 |
| 런타임 | 평가·production mode에서 HyperCLOVA X 외 provider 선택 불가 |
| 회귀 | dormant 개발 source의 실행 권한 없이 전체 필수 테스트 통과 |
| 문서 | 완료·미완료·개발 실험·공식 성능을 구분 |
| 검수 | AI·Backend·금융 도메인 담당자가 각각 1회 확인 |

### 자동 검사

현재 개발 저장소가 로컬 LLM 파일을 격리하고 운영용 Compose·Dockerfile·필수
의존성에 섞지 않았는지 확인

```bash
PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python finance_agent/scripts/check-submission-boundary.py \
  --profile development
```

공식 범위 확인 후 별도 제출 후보에서는 더 엄격한 검사 실행

```bash
PYTHONPATH=finance_agent/packages/finance_agent_core/src \
python finance_agent/scripts/check-submission-boundary.py \
  --profile submission \
  --output finance_agent/artifacts/release/submission-boundary.json
```

- `development`: 로컬 실험 파일은 허용하지만 루트 Compose·Backend Dockerfile·
  운영 의존성에 로컬 LLM 표식이 들어가면 실패
- `submission`: Git 추적 파일의 로컬 provider·모델·실행 문구와 `.env`, 모델
  weight, SQLite까지 모두 실패 처리
- 현재 개발 저장소는 `development` 통과, 전체 Git 이력까지 검사하는 `submission`
  profile은 과거 연구 파일 때문에 차단되는 것이 정상
- 검사는 현재 Git 추적 파일만 확인하며 Git 이력 재작성이나 공식 허용 범위 판단을
  대신하지 않음

## 6. 현재 상태

- 로컬 provider는 아직 개발 저장소에 존재
- 평가·production 모드의 HyperCLOVA X 제한 게이트는 구현 완료
- HyperCLOVA X fake transport·오류·fallback 계약은 구현 완료
- 공식 Direct v3 HTTP transport와 FastAPI answer-only 배선은 구현 완료
- 실제 credential 인증·HCX-007 DETAIL·SEARCH answer-only 호출 검증 완료
- final workflow는 `hyperclova + QueryPlan false` 외 Manifest 발급을 거부
- 로컬 Qwen은 평가 runtime에서 실행할 수 없으며 개발 이력에만 남음
- 로컬 LLM 파일 자체를 별도 제출 소스에서 제거할지는 runtime 제한과 분리해 최종 제출
  저장소 범위가 확정될 때 결정
- 자동 경계 검사는 구현 완료. 현재 개발 프로필은 통과하고 제출 프로필은 남아 있는
  개발 흔적을 의도적으로 차단

이 문서는 제거 완료 증명이 아니라, **제거를 놓치지 않기 위한
release gate**임
