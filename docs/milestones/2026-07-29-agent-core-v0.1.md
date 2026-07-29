# Agent Core v0.1 마일스톤

상태: 완료
기준일: 2026-07-29
기반 커밋: `a65c2b2` (`feat(agent): bootstrap verified finance agent core`)

이 문서는 공식 HyperCLOVA X 연결 전까지 구현한 데이터·Agent·로컬 LLM
개발 기준선을 한곳에 정리한 인수인계 기록이다.

## 1. 시작 상태

- GitHub 저장소: `Hyunhoim/gaeng3`
- 작업 branch: `haeyeongcho`
- 시작 원격 commit: `f366414`
- 동료는 `vintasoftware/nextjs-fastapi-template` 분석·적응을 담당
- AI·데이터 코드는 충돌을 줄이기 위해 `packages/finance_agent_core`에서 독립 개발
- Python 환경은 Conda `gaeng3-dev`와 pip requirements로 관리
- HyperCLOVA X API를 아직 사용할 수 없어 별도 `gaeng3-llm-local` 환경의
  Qwen을 개발 전용 대역으로 사용

## 2. 채택한 시스템

주력 구조는 Evidence-Compiled Hybrid SQL Agent다.

```text
자연어 질문
→ LLM structured QueryPlan
→ lexical canonicalizer
→ registry·Pydantic 검증
→ parameterized SQLite Oracle
→ 독립 Python Result Verifier
→ field-level evidence
→ 최소권한 GroundedAnswerDraft
→ Answer Verifier
→ evidence compiler 또는 deterministic fallback
```

LLM에는 계산, SQL, 상품 순위, 원천값과 실제 숫자 작성을 맡기지 않는다.
검증 실패 시 모델 문장을 전부 버리고 결정론적 답변으로 닫힌다.

## 3. 데이터 감사와 상품군

| 상품군 | 원천 행 | 정규화·검색 상태 | 핵심 계약 |
| --- | ---: | --- | --- |
| 해외 ETP | 5,646 | 검색 5,636, sparse 격리 10 | 보수 0·1일 수익률 sentinel |
| 국내 ETP | 1,734 | 검색 1,733, 손상 행 격리 1 | 낮은 보수·AUM coverage, 상태 코드 잠정 |
| 국내채권 | 42,394 | 전 행 검색, 실제 매수 가능 254 | stale 동적 값, 날짜 sentinel, 잔존일수 재계산 |
| 공모펀드 | 95,619 raw | product-grain 감사 11,138개 | 실행 파이프라인 예정 |

4종 합계 145,393행 감사, expectation 49/49, 제공 XLSX 8개 hash 대조와
감사 JSON의 결정적 재실행을 통과했다.

국내채권은 다음 데이터 현실을 계약으로 고정했다.

- 매수수익률·세후수익률·매수가능수량 881행
- 양수 매수 가능 수량 325행
- 수량 양수이면서 2026-07-11에 만기 전인 채권 254행
- 현재 매수 가능 254행의 동적 기준일은 모두 2026-02-24
- 원천 `REMAINING_DAYS` 대신 `MAT_DT - 2026-07-11`로 잔존일수 재계산
- 신용등급은 정확값·목록 일치만 허용하고 순서 비교는 차단
- 공식 코드북 없는 위험코드는 정확값만 사용

## 4. 평가

각 상품군은 동결된 50문항, 기대 QueryPlan과 Oracle 결과를 갖는다.

| 평가 | 결과 |
| --- | --- |
| 해외 ETP QueryPlan | 최초 holdout 9/10, 수정 후 전체 회귀 50/50 |
| 국내 ETP QueryPlan | development 40/40, 최초 local-inference holdout 10/10 |
| 국내 ETP grounded answer | 47 LLM 생성·3 안전 차단, 전체 50/50, 폴백 0 |
| 국내채권 QueryPlan | development 40/40·holdout 10/10, 전체 50/50 |
| 국내채권 grounded answer | 46 LLM 생성·1 빈 결과·3 안전 차단, 전체 50/50, 폴백 0 |

국내채권 답변의 첫 실행은 22/50이었다. `매수수익률`과 `매수가능수량`의
필드명까지 투자 권유로 판정한 Answer Verifier 오탐이 원인이었다. 실제
“지금 매수하세요” 차단은 유지하면서 두 원천 field phrase만 허용하고 회귀
테스트를 추가한 뒤 50/50을 통과했다.

이 수치는 자유 생성 LLM 정확도나 공식 점수가 아니다. 로컬 Qwen, 결정론적
linker, 계약, Oracle, Verifier와 evidence compiler를 합친 시스템 회귀 결과다.
요약 지문은 [평가 baseline](../../evaluation/README.md)에 보존한다.

## 5. 실제 통합 E2E

국내채권 대표 질문:

> 잔존일수 365일 이하인 매수 가능한 회사채를 매수수익률 높은 순으로
> 3개 보여줘.

질문부터 QueryPlan과 답변 초안까지 로컬 Qwen을 연속 사용했다.

- 잔존일수, 대분류, 매수 가능 조건을 모두 `locked`로 보존
- 결정론적 후보 23개
- 상위 3개와 독립 Verifier 일치
- answer mode `llm_grounded`
- Answer Verifier 모든 check 통과
- 각 매수수익률에 원천 ID·Excel 행·`BUY_YIELD`·2026-02-24 기준일 인용

HyperCLOVA X endpoint와 credential은 호출하지 않았다. 실험 후 vLLM을
종료하고 두 GPU와 loopback 18000 포트가 해제된 것을 확인했다.

## 6. 검증 상태

2026-07-29 기준:

- pytest 64개 통과
- Ruff lint·format 통과
- pip dependency check 통과
- `SOURCE_DATE_EPOCH=1785283200`으로 wheel을 서로 다른 임시 디렉터리에
  두 번 빌드해 byte hash 일치
- 재현 wheel SHA-256:
  `a199a8b532537256be43c6e1da2a742b27e6ce063ade23b727efabaab8a33431`
- 추적 후보에서 secret·원천 XLSX·ZIP·SQLite·모델 가중치 혼입 없음

DB, 전체 평가 report, wheel과 모델 파일은 의도적으로 Git에서 제외한다.
재현에 필요한 aggregate metrics와 hash만 추적한다.

## 7. 현재 한계

- HyperCLOVA X provider와 공식 `/answer` adapter가 아직 없다.
- 로컬 vLLM의 `const`·`prefixItems` schema는 HCX 공식 subset과 다르다.
- 평가 질문은 같은 개발자가 작성했으므로 완전한 blind 일반화 평가가 아니다.
- 공모펀드 실행 파이프라인은 아직 없다.
- 판매·거래 상태, 채권 위험코드와 경계 모델 허용 범위는 공식 확인이 필요하다.
- 사람 기준의 명확성·중복·비교 용이성 평가는 아직 하지 않았다.

## 8. 다음 순서

1. 다른 작성자가 만든 blind 표현 변형·오타·장문·prompt injection 세트
2. 사람 rubric과 deterministic-only 대비 답변 선호 평가
3. 공모펀드 product-grain 수직 파이프라인
4. 동료 application shell과 `AgentRequest`·`AgentResponse`·오류 계약 통합
5. HyperCLOVA X provider와 공식 `/answer` adapter
6. 2026-08-06 설명회 답변 반영 후 모델·데이터 정책 재동결
