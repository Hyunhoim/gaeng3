# 금융상품 Agent 최종 인수인계

작성일: 2026-09-03
대상: 조해영
범위: Agent 흐름, Phase 1~8 평가 이력, 재실행 방법, NCP·Docker Compose 운영 상태

## 1. 한 줄 결론

현재 코드는 `SafetyEnvelope → HCX-007 Intent → 결정론적 QueryPlan·SQL → Verifier → HCX-007 답변` 경계를 갖춘 상태다. Phase 8 최초 Blind에서는 **상품 Precision 100%와 false execution 0건**을 유지했지만, 공식 외부문서 경로가 거의 열리지 않아 Outcome Exact 58.00%, 상품 micro F1 84.94%에 머물렀다. 이 수치는 현재 일반화 한계를 보여주는 최초 실행 결과이며 후검증 점수와 섞어 해석하면 안 된다.

## 2. 현재 Agent 흐름

```text
사용자 질문
→ SafetyEnvelope(공격·주문·범위 밖 요청 차단)
→ HCX-007 Intent(질문 의도 구조 제안)
→ Ontology·Lexical·Typed Alias Registry(명확한 조건 확정)
→ 미해결 의미만 Schema Dense + 제한된 HCX Semantic Resolver
→ 서버가 Typed QueryPlan 검증·확정
→ 제공 DB에서 상품 후보 고정
→ SQLite/Python 필터·정렬·계산
→ 필요한 경우 공식 출처 한정 외부 검색 + Document Dense
→ 외부 근거 상품과 제공 DB 상품의 교집합
→ Verifier가 상품 ID·수치·기간·출처 재검산
→ HCX-007이 검증된 근거만으로 최종 답변 생성
→ Answer Verifier·공식 GET 응답 계약 검사
```

- HCLX는 Intent 분석과 최종 답변 생성에 참여한다. SQL 결과·상품·순위·수치를 바꿀 권한은 없다.
- `SUCCESS`: 제공 DB 상품이며 필요한 외부 근거까지 검증됨.
- `NOT_FOUND`: 외부 근거를 정상 확인했지만 제공 DB와 교집합이 없음.
- `CANNOT_VERIFY`: 공식 검색 장애 또는 근거 부족으로 검증을 끝내지 못함.
- `CLARIFY`: 조건을 하나로 확정할 수 없어 짧은 역질문으로 종료.
- `UNSUPPORTED`: 정책 위반·비금융·주문·근거 없는 예측 등 실행 금지 요청.

## 3. Phase 1~8 평가 이력

Phase마다 질문 수·난도가 달라 점수를 직접 순위처럼 비교하면 안 된다. `최초 Blind`는 Gold를 열기 전 일반화 진단이고, `후검증`은 Gold 공개 뒤 수정한 회귀 결과다.

| 평가 | 문항 | 상태 정확도 | Outcome Exact | 상품 micro F1 | Strict Exact | 비고 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Phase 1 최초 Blind | 300 | 75.67% | 50.67% | 56.78% | 40.67% | 초기 단일 상품군 기준선 |
| Phase 2 최종 후검증 | 300 | 100.00% | 99.00% | 98.94% | 83.00% | post-Gold 회귀 |
| Phase 3 최초 Blind | 600 | 62.33% | 59.17% | 72.84% | 48.67% | 복합 표현 확대 |
| Phase 3 후검증 v20 | 600 | 93.33% | 87.17% | 90.23% | 73.50% | post-Gold 회귀 |
| Phase 4 최초 Blind | 1,000 | 17.20% | 14.80% | 11.44% | 11.10% | 구조적 병목 탐지 |
| Phase 4 후검증 | 1,000 | 100.00% | 100.00% | 100.00% | 100.00% | post-Gold 회귀, 일반화 수치 아님 |
| Phase 5 최초 Blind | 105 | 62.86% | 34.29% | 37.14% | 17.14% | 보수적 일반화 진단 |
| Phase 6 최초 Blind | 150 | 92.00% | 85.33% | 90.78% | 82.67% | 혼합 세트, 독립성 한계 있음 |
| Phase 7 | — | — | — | — | — | 유형별 구조·외부문서 표적 검증. 단일 봉인 aggregate는 보존되지 않아 수치를 만들지 않음 |
| **Phase 8 최초 Blind** | **200** | **60.00%** | **58.00%** | **84.94%** | **49.50%** | Gold 공개 전 prediction 봉인 |

### Phase 8 핵심 결과

- HTTP 200: 200/200, timeout·429·5xx 0건.
- 상품 micro Precision 100.00%, Recall 73.83%, F1 84.94%.
- false execution 0건, false block 79건(39.50%).
- QueryPlan filter 50.54%, ranking 53.55%, family 57.53%, limit 57.53%.
- 외부근거 필요 59건의 Outcome 0건. Document Dense 조회 2회, cache hit 0회, Live Search 2회.
- HCLX Intent 203회 중 schema 오류 5회는 재시도로 복구. 최종 답변 198회 성공.
- 지연: p50 6.34초, p95 13.04초, p99 17.76초, 최대 34.33초.

주요 유형별 Outcome은 지정상품 상세 20/20, 단순 카테고리 정렬 14/15, 교차 bounded 검색 14/15였고, 공식 구성종목·보유관계·위험요인·테마·펀드 구조 유형은 0건이었다. 핵심 병목은 검색 품질보다 **외부 검색 계획으로 전환되지 않는 Router·QueryPlan 배선**과 과도한 `CLARIFY`다.

Phase 8 원본 질문·Gold·응답·API Key는 Git에 포함하지 않는다. 봉인 정보는 연구실 서버의 `/tmp/finance-agent-phase8-blind`에만 있다.

## 4. Blind 테스트 실행 방법

1. question-only 파일의 SHA-256을 기록하고 이후 수정하지 않는다.
2. 코드 commit 또는 pre-Gold diff, 네 DB SHA, Schema/Document Dense index SHA, KURE snapshot SHA를 기록한다.
3. Gold를 읽지 않은 상태에서 `run_sealed_lab_blind.py`의 `--preflight-only`로 모든 입력을 검사한다.
4. 공식 GET과 같은 순차 방식으로 질문을 실행한다. HCLX Intent·필요 시 Resolver·최종 답변, NAVER API HUB 공식 검색을 사용한다.
5. prediction과 journal을 새 파일로 쓰고 SHA-256 기록 후 mode `0400`으로 고정한다.
6. 그 뒤에만 Gold SHA를 확인하고 `compare_sealed_blind.py`로 최초 1회 채점한다.
7. Gold 공개 뒤 수정한 실행은 반드시 `--post-gold-development-regression`으로 표시한다.

핵심 명령 구조:

```bash
python3 fastapi_backend/scripts/run_sealed_lab_blind.py \
  --questions <QUESTION_ONLY> --questions-sha256 <SHA256> \
  --output <PREDICTION_JSON> --journal <JOURNAL_JSONL> \
  --source-commit "$(git rev-parse HEAD)" --image-id <IMAGE_ID> \
  --runner-sha256 <RUNNER_SHA256> \
  --hclx-key-file <OWNER_ONLY_KEY_FILE> \
  --naver-client-id-file <OWNER_ONLY_ID_FILE> \
  --naver-client-secret-file <OWNER_ONLY_SECRET_FILE> \
  <DB·relation·Schema Dense·Document Dense·KURE의 고정 경로와 SHA 인수>

chmod 400 <PREDICTION_JSON> <JOURNAL_JSONL>

python3 fastapi_backend/scripts/compare_sealed_blind.py \
  --predictions <PREDICTION_JSON> --predictions-sha256 <PREDICTION_SHA256> \
  --gold <GOLD_JSON> --gold-sha256 <GOLD_SHA256> \
  --output <SCORE_JSON>
```

필수 인수 전체 목록은 `python3 fastapi_backend/scripts/run_sealed_lab_blind.py --help`로 확인한다. Secret·question-only·Gold·prediction·journal·score 원본은 Git에 추가하지 않는다.

## 5. NCP·Docker Compose 배포

### 마지막 확인된 배포 단위

- 서버: `finance-agent-eval-01`, 공인 IP `101.79.25.57`.
- 마지막 확인 Release: `evaluation-20260829-external-audit-g9-r6` (Generation 9).
- Image: `gaeng3-finance.kr.ncr.ntruss.com/finance-agent/backend@sha256:771260d17e93ea80582aed74e87e3bd7018ce2329b1cc89d871910e66279607f`.
- GitHub immutable workflow run: `33250014098`.
- 평가 endpoint: `GET http://101.79.25.57/answer?question_id=...&question=...`.

이 Release는 현재 작업 트리보다 오래됐다. **현재 commit을 NCP에 배포한 상태가 아니다.** 또한 2026-09-03 외부에서 `/health`와 SSH 22를 각각 5초 상한으로 확인했으나 모두 connection timeout이었다. 따라서 지금은 endpoint 접근 가능 상태를 확인할 수 없다. 서버 중지, ACG, 공인 IP 연결, 컨테이너 상태를 순서대로 확인해야 한다.

### Release Compose 절차

```bash
cd /opt/finance-agent
umask 022

# .env.release에는 값 자체가 아니라 owner-only Secret 파일 경로와
# immutable image@sha256, release/binding/artifact 경로·SHA만 둔다.
RELEASE_ENV_FILE=fastapi_backend/.env.release ./compose-release.sh config --quiet
RELEASE_ENV_FILE=fastapi_backend/.env.release ./compose-release.sh up --detach --wait

curl --fail http://127.0.0.1/health
curl -G http://127.0.0.1/answer \
  --data-urlencode 'question_id=SMOKE-001' \
  --data-urlencode 'question=최근 1년 수익률이 높은 국내 ETF 3개 알려줘'
```

외부 접근 성공 조건:

- NCP 서버 `운영중`, 공인 IP `101.79.25.57` 연결.
- `finance-agent-api-acg` Inbound TCP 80에 주최측 발신 IP(공지 전에는 제한된 점검 IP) 허용.
- release Compose의 `BACKEND_BIND_ADDRESS=0.0.0.0`, `BACKEND_PORT=80`.
- Backend container healthy, 네 상품군·relation·audit가 `/health`에서 ready/ok.
- `curl -G http://101.79.25.57/answer ...`가 HTTP 200과 다섯 문자열 필드를 반환.

## 6. 동결 전 검증 상태

- Backend 전체: 493 passed, 1 skipped.
- Agent Core 마지막 전체: 3,111 passed, 5 failed, 12 skipped. 실패 5건은 release/DB 최종 경계 2건, 매수수익률 Alias 2건, Ontology drift 1건이었다.
- 위 5건을 수정한 뒤 영향 테스트만 다시 실행: **36 passed**.
- 사용자의 최소 검증 지시에 따라 Agent Core 전체 재실행은 하지 않았다.

## 7. 바로 이어서 할 일

1. PR에서 현재 대규모 변경과 main 최신 변경의 충돌·계약 영향을 조해영이 검토한다.
2. 최종 제출 직전 별도 승인 후에만 clean commit 기반 immutable Release를 새로 만든다.
3. NCP 서버·ACG·Compose를 복구하고 공인 IP `/health`, 공식 GET `/answer`, 실제 HCLX를 검증한다.
4. Phase 8 병목 개선은 별도 commit에서 수행한다. 특히 외부 질문 → External Search Plan 배선과 과잉 `CLARIFY`를 먼저 고치고, 기존 봉인 결과를 일반화 점수처럼 재사용하지 않는다.
