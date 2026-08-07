# 제출용 모델 경계와 로컬 LLM 정리 메모

기준일: 2026-08-07

## 0. 결정

- 현재 로컬 Qwen 연결 코드와 실험 기록은 HyperCLOVA X가 없던
  개발 단계의 내부 검증용으로만 관리
- 예선 평가·제출물에서 실행되는 LLM은 HyperCLOVA X로 제한
- 설명회 자료를 검토했지만 정확한 HCX API 규격·크레딧 적용·로컬 LLM 제출 범위는
  확인되지 않았으므로 실제 HTTP transport나 credential 연결을 시도하지 않음
- 공식 서면 답변으로 제출 범위가 확정되면 로컬 LLM 코드·설정·의존성·
  실행 명령을 제출 후보에서 제거하고 기계적으로 검사

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

- 설명회 자료에는 평가 adapter 규격만 있고 HyperCLOVA X의 실제 요청·응답 방식은 없음
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
2. HyperCLOVA X 실제 transport와 최소 smoke test 완료
3. 로컬 provider 없이 같은 QueryPlan·Oracle·Verifier 회귀 통과
4. 제출 후보에서 로컬 provider·스크립트·설정·의존성 제거
5. 로컬 모델명·실행 명령·실험 baseline이 제출 문서에 섞이지 않았는지 검사
6. 평가·production 모드에서 `LLM_PROVIDER=hyperclova` 외 시작 실패 확인
7. 모델 weight·cache·로컬 응답·API key·개인 경로 미포함 확인
8. clean checkout에서 build·test·start 재현

## 5. 제출 전 필수 검사표

| 검사 대상 | 통과 조건 |
| --- | --- |
| LLM provider | 실행 코드에 HyperCLOVA X만 존재 |
| 설정 | `local_test`, `Qwen`, `vLLM`, `ENABLE_NON_HCX_TEST_LLM` 미포함 |
| 의존성 | 로컬 LLM server·model package 미포함 |
| 파일 | 모델 weight·cache·SQLite·`artifacts/`·credential 미포함 |
| 런타임 | 평가·production mode에서 HyperCLOVA X 외 provider 선택 불가 |
| 회귀 | 로컬 provider 없이 전체 필수 테스트 통과 |
| 문서 | 완료·미완료·개발 실험·공식 성능을 구분 |
| 검수 | AI·Backend·금융 도메인 담당자가 각각 1회 확인 |

## 6. 현재 상태

- 로컬 provider는 아직 개발 저장소에 존재
- 평가·production 모드의 HyperCLOVA X 제한 게이트는 구현 완료
- HyperCLOVA X fake transport·오류·fallback 계약은 구현 완료
- 실제 HyperCLOVA X 연결은 크레딧·정확한 모델 ID·endpoint·인증 규격 확보 후로 보류
- 그 전까지 로컬 Qwen은 내부 회귀·E2E·fallback 시험에 계속 사용
- 로컬 LLM 제거 작업은 공식 제출 범위 확정 후 수행

이 문서는 제거 완료 증명이 아니라, **제거를 놓치지 않기 위한
release gate**임
