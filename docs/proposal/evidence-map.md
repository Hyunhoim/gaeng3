# 기술 제안서 근거 맵

상태: 활성

기준일: 2026-08-19

제안서에 사용하는 주장과 실제 코드·문서·baseline을 연결한다. `검증 완료`는
해당 범위의 저장소 회귀를 통과했다는 뜻이며 공식 평가 성능을 뜻하지 않는다.

## 공식 기술 제안서 항목

| 항목 | 사용할 핵심 주장 | 근거 | 상태 |
| --- | --- | --- | --- |
| 제안 요약 | LLM과 결정론적 검색·검증을 분리한 Evidence-Compiled Hybrid SQL Agent | [프로젝트 기준](../../finance_agent/docs/project-baseline.md) | 검증 완료 |
| 문제 정의 | 네 상품군의 grain·필드·결측·기준일이 서로 다름 | [데이터 감사](../../finance_agent/docs/data-audit.md) | 검증 완료 |
| 제안 방법 | typed QueryPlan·Oracle·Verifier·field evidence·Answer Verifier | [계약](../../finance_agent/docs/contracts.md) | 검증 완료 |
| 연구 근거 | 행동 기능 검사·구조화 출력 제약·단계별 진단·paired 통계 비교를 실제 구현과 연결 | [연구 근거](research-basis.md) | 적용 범위 기록 완료 |
| 시스템 구성도 | Agent Core·FastAPI·공식 GET 계약·Docker 로컬 통합 완료, Next.js·HCX·공개 서버 대기 | [연결 전 readiness](../../finance_agent/docs/pre-hcx-readiness.md) | 부분 완료 |
| 주요 기능 흐름 | SEARCH·교차 상품군 독립 검색·family별 grounded answer·교차 검증·전체 fallback·COMPARE·AGGREGATE·제어 응답 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md) | 검증 완료 |
| 사용자 시나리오 | 검색·비교·집계·역질문·거절 | [사용자 시나리오](user-scenarios.md) | 금융·화면 검수 대기 |
| 기대효과·확장성 | 근거 추적·안전 fallback·상품군 adapter·평가 재사용 | [Backend 계약](../../finance_agent/docs/backend-contract.md) | 내부 계약 완료 |
| 공식 제출 계약 | GET `/answer`, 다섯 문자열, 답변·제어 200, 일시 장애 503, timeout 504, 동일 요청 중복 실행 방지, Ontology 5개 | [P0-8 인수인계](../../finance_agent/docs/p0-8-retry-contract-handover-2026-08-19.md)·[Ontology 계약](../../finance_agent/docs/ontology.md) | API·Ontology 내부 구현 완료 |

## 정성평가 축

| 평가 축 | 현재 근거 | 남은 증거 |
| --- | --- | --- |
| 문제정의 | 데이터 grain·품질·금융 안전 문제를 실제 원천 수치로 정의 | 금융 도메인 담당자의 사용자 pain point 검수 |
| 기술완성도·성능 | 전체 테스트, 검색·집계 성능 기준선, family별 grounded answer, FastAPI·공식 GET·Ontology·Docker·provider·오류·재시도 계약 | 실제 HCX·NCP 공개 서버·장시간 부하 테스트 |
| 창의성·확장성 | LLM 계획과 서버 계획의 exact-match gate, 독립 verifier, field evidence | 기존 방식 대비 비교표와 실제 확장 사례 |
| 답변 정확성·완결성 | 상품명·수치·순위·근거·기준일 Answer Verifier와 red-team | external blind와 실제 사람 평가 |
| 현업 활용성·리스크 관리 | 결측·stale·통화·추천 금지·fallback·오류 비노출·중복 비용 방지·외부 문서 출처·권한·해시 게이트 | 실제 사용자 시나리오·화면 데모·승인 corpus·NCP 운영 로그 |

## 정량 근거

| 주장 | 값 | 정본 | 해석 제한 |
| --- | ---: | --- | --- |
| 원천 감사 | 4종 145,393행, 65/65 | [데이터 감사](../../finance_agent/docs/data-audit.md) | 제공 스냅샷 기준 |
| 전체 코드 회귀 | Agent Core 1,305 passed·2 조건부 skip, Backend 최근 기준 320 passed·2 기존 warning | [P0-6 인수인계](../../finance_agent/docs/p0-6-provided-relation-retrieval-handover-2026-08-19.md)·[P0-8 인수인계](../../finance_agent/docs/p0-8-retry-contract-handover-2026-08-19.md) | 단위·계약 회귀이며 독립 성능 평가가 아님 |
| 평가기 retry·중복 실행 제어 | 정상 동일 요청 200·200/Agent 1회, dataset 장애 503·503/Agent 2회, 강제 timeout 504 | [P0-8 baseline](../../finance_agent/evaluation/baselines/retry-contract-p0-8-v1.json)·[인수인계](../../finance_agent/docs/p0-8-retry-contract-handover-2026-08-19.md) | 로컬 Docker fault injection, 실제 evaluator·HCLX·NCP 아님 |
| 외부 문서 반입 게이트 | 승인·권한·출처·해시·변조 차단·BM25 build 합성 계약 24/24 | [P0-5 baseline](../../finance_agent/evaluation/baselines/external-corpus-intake-contract-v1.json)·[인수인계](../../finance_agent/docs/p0-5-external-corpus-intake-2026-08-19.md) | 실제 외부 문서 0건, 승인·검색 정확도·Release 활성화 아님 |
| 제공 데이터 관계 검색 기반 | 승인 국내채권·국내/해외 ETP 관계 58,005개, 계약 14/14, 실제 검색 smoke 4/4, warm p50 4.846ms·p95 5.960ms | [P0-6 baseline](../../finance_agent/evaluation/baselines/p0-6-provided-relation-retrieval-v1.json)·[인수인계](../../finance_agent/docs/p0-6-provided-relation-retrieval-handover-2026-08-19.md) | Agent 비활성, 공모펀드·외부 관계 없음, lexical 검색·단일 로컬 장비이며 관계 의미 정확도나 공식 성능이 아님 |
| 교차 상품군 SEARCH | 국내·해외 ETP 4/4 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md) | 공개 회귀, not blind |
| 교차 상품군 grounded answer | expected·로컬 Qwen 각각 4/4; 생성 대상 2문항 모두 grounded; 실제 모델 호출 3회; fallback 0 | [교차 SEARCH](../../finance_agent/docs/cross-family-search.md)·[baseline](../../finance_agent/evaluation/baselines/cross-family-answer-v1.json) | 공개 4문항, not blind; 로컬 Qwen은 개발 전용 |
| 내부 red-team | 수정 후 40/40 | [red-team 평가](../../finance_agent/docs/evaluation-internal-red-team.md) | self-authored, not blind |
| 공식 형식 공개 모의평가 | 전체 계약 30/30, 답변 불가 5/5, Qwen 생성 16/17, fallback 1 | [모의평가](../../finance_agent/docs/evaluation-official-mock.md)·[baseline](../../finance_agent/evaluation/baselines/official-mock-v1-30.json) | 설명회 분포 모사, self-authored, not blind |
| Qwen 표현 강건성·역할 ablation | 의미 보존 변형 네 실행 profile 모두 77/77, 의미·safety·evidence 100%, 답변 fallback 3/61→0/61; plan+answer p95 4,096.584ms | [변형 평가](../../finance_agent/docs/evaluation-qwen-metamorphic.md)·[baseline](../../finance_agent/evaluation/baselines/qwen-eval-lab-v1.json) | 공개 원문 파생·사후 수정, 생성 90개 중 13개 실행 전 폐기, 단일 순차 장비, not blind |
| 원문 비공개 의미 재구성·모델 계획 gate | 생성 75·선별 64, 최초 결정론적 15/64→출력·QueryPlan 의미 64/64; grounded-plan 최초 28/64, gate 구제 9건 | [변형 평가 9절](../../finance_agent/docs/evaluation-qwen-metamorphic.md#9-원문-표현을-숨긴-semantic-round-trip)·[baseline](../../finance_agent/evaluation/baselines/semantic-roundtrip-v1.json) | 공개 정답 의미 파생·사후 수정, 강화된 Qwen prompt·gate 재실행 대기, not blind |
| registry 기반 자동 커버리지·Qwen 역할 비교 | 대표 좌표 305개 중 정답 계획 299개 직접 실행; canonical 최초 37/299; Qwen 질문 897개 생성·391개 선별; 네 구성 최초 strict 65/391; 같은 공개 질문에서 비교 94/391·검색 153/391→170/391, 최신 실행 의미 보조 strict 242/391 | [자동 커버리지 평가](../../finance_agent/docs/evaluation-coverage-guided.md)·[baseline](../../finance_agent/evaluation/baselines/coverage-guided-v1.json) | 최초 관측과 사후 회귀를 분리해 보존하며, 실제 사용자 분포·독립 blind·공식 점수 아님 |
| 공식 GET Docker 30문항 | 형식·60초 30/30, 의미 24/30, 공모펀드 정책 잠금 6건 | [모의평가](../../finance_agent/docs/evaluation-official-mock.md)·[HTTP baseline](../../finance_agent/evaluation/baselines/official-mock-http-v1-30.json) | 개발 서버 순차 1회, local Qwen 답변 전용, not blind |
| 공식 GET 공모펀드 명시적 승인 | 의미·형식·60초 30/30, Qwen 17/17, fallback 0 | [모의평가](../../finance_agent/docs/evaluation-official-mock.md)·[승인 baseline](../../finance_agent/evaluation/baselines/official-mock-http-fund-approved-v1-30.json) | 최초 24/30 보존 후 팀 배포 정책 재평가, 주최 승인 아님, not blind |
| 공식 GET 제한 동시성 | 결정론적 동시성 1·2·4 각 30/30, Qwen 동시성 2에서 30/30·fallback 0 | [모의평가](../../finance_agent/docs/evaluation-official-mock.md)·[결정론적 baseline](../../finance_agent/evaluation/baselines/official-mock-http-concurrency-v1.json)·[Qwen baseline](../../finance_agent/evaluation/baselines/official-mock-http-qwen-approved-c2-v1.json) | 단일 worker·프로필별 1회, 부하·SLO 아님 |
| 금융 도메인 QA Router 회귀 | 40/40, 잘못된 실행·오류 0건 | [도메인 QA 평가](../../finance_agent/docs/evaluation-domain-qa.md)·[baseline](../../finance_agent/evaluation/baselines/domain-qa-e2e-v1.2-router.json) | 개선에 사용한 개발 세트, not blind |
| 네 상품군 자연어 비교 공개 회귀 | 54문항 | [비교 평가](../../finance_agent/docs/evaluation-product-comparison.md) | 공개 회귀 |
| SEARCH·AGGREGATE 결과 지문 | 8/8 | [성능 기준선](../../finance_agent/docs/evaluation-search-aggregate-performance.md) | 단일 개발 장비 |
| HCX API 없는 전체 계약 | 8/8 | [HCX provider](../../finance_agent/docs/hyperclova-provider.md) | 실제 API 성능 아님 |
| Backend service adapter | 12/12 | [Backend 계약](../../finance_agent/docs/backend-contract.md) | 실제 HTTP route 아님 |
| Docker HTTP 확장 smoke | 기본 14/14, Qwen 14/14, Qwen 장애 fallback 14/14 | [Backend README](../../fastapi_backend/README.md)·[기본 baseline](../../finance_agent/evaluation/baselines/docker-http-smoke-v2.json)·[Qwen baseline](../../finance_agent/evaluation/baselines/docker-http-smoke-qwen-v2.json) | 공개 개발 스모크, 보안 침투·운영 SLO 아님 |
| 제출 모델 경계 | development 통과·submission 차단 자동 검사 | [제출 경계](../../finance_agent/docs/submission-model-boundary.md) | 현재는 개발 저장소라 제출 차단이 정상, 공식 범위 확인 후 cleanup 필요 |

## 완료되지 않은 주장

다음 표현은 근거가 생기기 전까지 기술 제안서에서 완료형으로 사용하지 않는다.

- HyperCLOVA X 연결·성능·비용 검증 완료
- 공식 `GET /answer` 공개 평가 서버 재현 완료
- Ontology 용어의 금융 도메인 검수와 주최 측 최종 형식 승인 완료
- 독립 blind 일반화 성능 100%
- 실제 투자설명서·약관 기반 문서 RAG 완료
- 제공 관계 검색의 Agent 답변 연결·금융 alias 검수·관계 의미 정확도 입증 완료
- 상품군 간 직접 수치 비교 지원
- 세 상품 이상 비교·환율 환산·개인화 투자 추천 지원
- 사람 평가에서 현업 유용성 입증

## 갱신 규칙

- 수치 변경 시 원본 baseline과 이 표를 같은 변경 단위로 갱신
- 최초 관측과 사후 수정 결과를 함께 기록
- 성능값에는 장비·실행 횟수·평가 범위를 함께 명시
- 계획을 완료로 바꾸기 전에 재현 명령과 담당자 검수를 연결
