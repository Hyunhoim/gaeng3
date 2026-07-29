# 공모펀드 핵심 평가 기준선

상태: v1.0 QueryPlan·Oracle 계약 동결

기준일: 2026-07-29

이 문서는 공모펀드 자연어 질문을 검색 계획으로 바꾸는 기준과 실제 데이터
검색 결과를 고정한 회귀 계약이다. 현재 50/50 결과는 사람이 작성한 기대
QueryPlan을 실행한 평가 하네스 검증이며, 로컬 LLM이나 HyperCLOVA X의 언어
이해 성능을 뜻하지 않음

## 1. 동결 평가 세트

- suite ID: `fund-core-50`
- 질문 50개
  - development 40개
  - holdout 10개
- 처리 기대
  - 결정론적 검색 44개
  - 모호성·미지원 조건 차단 6개
- 평가 범주
  - 공모 범위와 판매 상태
  - 국내·해외·혼합 및 운용 속성 분류
  - 투자지역·투자자 유형·위험등급·환헤지
  - 1주·1개월·3개월·6개월 수익률
  - 통화가 고정된 AUM
  - 상품번호·정식명·짧은 이름 조회
  - 자연스러운 표현 변형
  - 모호한 추천과 미지원 필드 안전 차단

동결 파일과 데이터 hash:

- [50문항 suite](../packages/finance_agent_core/src/finance_agent_core/evaluation/suites/fund_core_50.json)
- suite SHA-256:
  `77d9be9ca86d9654fb61a52290ca08eadff6b618f861b985367b05d195c582b2`
- SQLite SHA-256:
  `99fac786e5be0ec5a7a53e11e1bd3bbccd5b37ab15243ecbf8b864a85b375ca4`
- manifest SHA-256:
  `be83a616d033db2328d231499d1f0492323d02bace4f153ad3da4860a0d10bcd`

평가 CLI는 DB와 manifest hash가 다르면 실행 전에 실패

## 2. 공모펀드 전용 안전 계약

모든 50문항의 기대 QueryPlan에 다음 조건을 정확히 한 번 포함

```text
public_offering = true
strength = locked
```

사용자가 공모라는 단어를 생략해도 시스템이 이 조건을 추가해야 함. 사모 15개와
공·사모 구분 결측 8개는 정상 보존하지만 검색 결과에서는 제외

AUM은 원천 통화가 다르면 직접 비교할 수 없으므로 모든 AUM 검색·정렬 문항에
`trading_currency = KRW` 또는 `USD`를 함께 잠금. AUM 0은 UNKNOWN으로 처리해
필터·정렬에서 제외

다음 질문은 조건을 무시하거나 추측하지 않고 차단

- 안전하고 괜찮은 상품 추천
- 총보수·판매수수료 순위
- 운용사 이름 검색
- 1년 이상 장기 수익률 순위
- 오늘 기준 최신 수익률
- 클래스 합산 후 대표 펀드 순위

## 3. 평가 결과

동결 SQLite에서 expected provider로 전체 회귀:

| 지표 | 결과 |
| --- | ---: |
| strict accuracy | 50/50 |
| valid plan | 100% |
| plan exact | 100% |
| constraint exact | 100% |
| Oracle exact | 44/44 |
| safety block | 6/6 |
| development | 40/40 |
| holdout | 10/10 |

`expected` provider는 suite에 기록된 기대 QueryPlan을 그대로 제공. 이 결과가
보장하는 것은 다음 세 가지

- 50개 기대 QueryPlan이 field registry 계약에 맞음
- SQLite Oracle과 독립 Python Result Verifier가 후보 수와 순위를 동일하게 계산
- 안전 문항이 SQL 실행 전에 차단

이 결과만으로 자연어 parser나 LLM 성능을 주장할 수 없음

## 4. 실행 비활성 상태에서의 평가

공모펀드 dataset은 계속 `execution_enabled: false`로 유지. 일반 Agent와
`local_test` provider는 공모펀드 실행을 허용하지 않음

평가 runner는 다음 조건을 모두 만족할 때만 내부 승인 회귀를 허용

- dataset이 정확히 `fund`
- provider가 동결된 `expected`
- 공모 범위·모호성·미지원 조건 검사를 모두 통과

따라서 평가 세트를 만들었다는 이유로 공식 Agent 실행 범위가 열리지 않음

## 5. 재현 명령

`finance_agent/` 디렉터리에서 실행:

```bash
/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  scripts/generate-fund-suite.py

/home/haeyeongcho/miniforge3/envs/gaeng3-dev/bin/python \
  -m finance_agent_core.evaluation \
  --dataset fund \
  --provider expected \
  --split all \
  --workers 4 \
  --require-perfect \
  --output artifacts/evaluation/fund-expected-all.json
```

첫 명령은 실제 DB에서 44개 문항의 후보 수와 상위 상품 ID를 다시 계산해 suite를
생성. DB·manifest가 바뀌지 않았다면 suite hash도 동일해야 함

## 6. 해석 한계와 다음 단계

- 같은 개발자가 질문과 기대 조건을 작성했으므로 holdout 10개도 완전한
  unbiased 일반화 세트가 아님
- 현재 평가는 `SEARCH` intent와 QueryPlan·검색 결과만 검증
- 공모펀드 답변 문장 품질과 Answer Verifier는 아직 평가하지 않음
- 로컬 Qwen parser는 아직 공모펀드 field linker를 지원하지 않음
- HyperCLOVA X 성능과 공식 평가 점수를 대변하지 않음

다음 단계:

1. 공모펀드 lexical/schema linker를 구현하고 development 40문항으로만 조정
2. 규칙 동결 뒤 holdout 10문항을 최초 1회 실행
3. 공모펀드 grounded answer와 사람 품질 평가 추가
4. HCX schema에 fund를 노출하고 서버 계약 테스트 통과
5. 그 뒤에만 `execution_enabled: true` 전환 검토
