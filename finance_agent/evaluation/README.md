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

`schema-embedding-cpu-public-v1.json`은 네 상품군 공개 200문항에서 고정된
오픈소스 임베딩 7개를 CPU로 비교한 집계다. BGE-M3와 Lexical 우선 결합이 공개
품질 문턱을 통과했지만 모델·결합 선택에 같은 질문을 사용했으므로 독립 blind나
production 채택 결과가 아니다. 상세 해석은
[Schema Dense CPU 비교](../docs/evaluation-schema-embedding-cpu.md)를 따른다.

개별 상품명, 상품 ID, 원천 행 값, 전체 답변과 모델 로그는 포함하지 않는다.
DB, 원천 XLSX, 전체 report, 모델 가중치는 계속 `artifacts/` 또는 로컬 cache에
두고 Git에서 제외한다.

## baseline 목록

| 파일 | 범위 |
| --- | --- |
| [설명회 공개 예시 채권 개선](baselines/briefing-examples-v1-bond-improved.json) | 원화·판매 가능·AA- 이상 검색과 근거 검증을 연결한 회귀 |
| [설명회 공개 예시 최초 관측](baselines/briefing-examples-v1-initial.json) | 답변 가능 5개·답변 불가 3개의 Router→Oracle→Verifier 현재 도달 범위 |
| [설명회 공개 예시 안전 개선](baselines/briefing-examples-v1-safety-improved.json) | 잘못된 신용등급 차단과 요청 어미 오분류 수정 후 회귀 |
| [해외 ETP QueryPlan](baselines/overseas-etp-queryplan-v1.json) | 로컬 Qwen hybrid parser 사후 회귀 |
| [국내 ETP QueryPlan](baselines/domestic-etp-queryplan-v1.json) | development와 최초 local-inference holdout |
| [국내 ETP 답변](baselines/domestic-etp-answer-v1.json) | 최소권한 grounded answer |
| [국내채권 QueryPlan](baselines/domestic-bond-queryplan-v1.json) | parser·Oracle·안전 차단 |
| [국내채권 답변](baselines/domestic-bond-answer-v1.json) | grounded answer·빈 결과·안전 차단 |
| [공모펀드 QueryPlan·Oracle](baselines/public-fund-queryplan-v1.json) | expected plan·Oracle·안전 차단 계약 |
| [공모펀드 로컬 development](baselines/public-fund-local-development-v1.json) | 로컬 Qwen hybrid parser 개발 40문항·holdout 잠금 |
| [공모펀드 최초 holdout](baselines/public-fund-local-holdout-first-run-v1.json) | commit 이후 최초 10문항 9/10·실패 원인 보존 |
| [공모펀드 답변](baselines/public-fund-answer-v1.json) | 최소권한 grounded answer·최종 컴파일 검증·fallback |
| [공모펀드 비교 답변](baselines/public-fund-compare-v1.json) | 두 상품 true COMPARE·차이 계산·통화·결측·fallback |
| [공모펀드 자연어 비교](baselines/public-fund-compare-parser-v1.json) | 상품명·짧은 이름·상품번호 resolver·COMPARE parser·안전 차단 |
| [공모펀드 자연어 비교 E2E](baselines/public-fund-compare-e2e-v1.json) | 자연어 parser·resolver·Oracle·검증 답변·안전 차단 통합 |
| [세 상품군 자연어 비교](baselines/product-compare-v1.json) | 해외·국내 ETP·국내채권 30문항의 결정론적 비교·Backend 계약·안전 차단 |
| [SEARCH·AGGREGATE 성능](baselines/search-aggregate-performance-v1.json) | 네 상품군 8문항의 결과 지문·새 프로세스 지연·RSS·경량 verifier 전후 비교 |
| [교차 상품군 SEARCH](baselines/cross-family-search-v1.json) | 국내·해외 ETP 양쪽 성공·부분 성공·전체 빈 결과·직접 비교 차단 4문항 |
| [교차 상품군 grounded answer](baselines/cross-family-answer-v1.json) | family별 evidence-only Qwen 생성·교차 문구 검증·전체 fallback·무호출 계약 |
| [HyperCLOVA X API 없는 계약 E2E](baselines/hcx-contract-e2e-v1.json) | 세 실행 상품군 SEARCH·fallback·timeout·정책 차단·계획 guard 8개 |
| [Backend answer adapter 계약](baselines/answer-adapter-contract-v1.json) | HTTP status·안전한 ERROR DTO·fallback·민감정보 비노출 12개 |
| [내부 red-team 전체 E2E](baselines/internal-red-team-v1.json) | 네 상품군 40문항의 Router→Qwen→Oracle→Verifier→Backend DTO와 공격 유형 회귀 |
| [공식 형식 30문항 공개 모의평가](baselines/official-mock-v1-30.json) | 난이도 10/10/10·답변 불가 5개에서 Qwen→Oracle→Verifier→공식 5필드 전체 경로 관측 |
| [Qwen 변형 질문 스트레스 평가](baselines/qwen-eval-lab-v1.json) | 공개 원문 30개에서 세 표현 축 90개 생성·77개 선별·gold 감사·결정론적/Qwen 전체 경로 회귀 |
| [원문 비공개 semantic round-trip](baselines/semantic-roundtrip-v1.json) | Qwen에 원문 문장을 숨기고 실행 의미로 75개 재생성·64개 선별·최초 15/64→계획 의미까지 64/64 사후 회귀 |
| [registry 기반 자동 커버리지 최초 관측과 사후 회귀](baselines/coverage-guided-v1.json) | 대표 기능 305개·정답 계획 299개 직접 실행, Qwen 자연화 최초 65/391 보존, 비교·검색 개선 94→153→170과 최신 실행 의미 보조 strict 242/391 |
| [공식 GET Docker 30문항 최초 관측](baselines/official-mock-http-v1-30.json) | 실제 FastAPI 네트워크의 5필드 30/30·의미 24/30·공모펀드 정책 잠금 6건 |
| [공식 GET Docker 공모펀드 명시적 승인](baselines/official-mock-http-fund-approved-v1-30.json) | 최초 24/30 보존 후 공모펀드만 여는 v1 정책으로 의미·형식·60초 30/30, Qwen 17/17 |
| [공식 GET 결정론적 동시성](baselines/official-mock-http-concurrency-v1.json) | 동시성 1·2·4에서 의미·형식 90/90과 단일 worker 지연 변화 관측 |
| [공식 GET Qwen 동시성 2](baselines/official-mock-http-qwen-approved-c2-v1.json) | 공모펀드 승인·Qwen 답변 17/17·fallback 0·전체 30/30 재검증 |
| [Docker HTTP 확장 스모크](baselines/docker-http-smoke-v2.json) | 기본 잠금·결정론적 Backend 7건과 공식 정상·예외 GET 7건 14/14 |
| [Docker Qwen 정상·장애 스모크](baselines/docker-http-smoke-qwen-v2.json) | 네 상품군 Qwen grounded 14/14와 모델 중단 fallback 14/14 |
| [금융 도메인 QA 최초 관측](baselines/domain-qa-e2e-v1.json) | 금융 도메인 담당자 40문항의 route·safety·evidence·answer 단계별 최초 관측 |
| [금융 도메인 QA SEARCH gold](baselines/domain-qa-e2e-v1.1-gold.json) | Q002 QueryPlan·Oracle 후보·상위 ID·evidence 지문 동결 후 Router 개선 전 관측 |
| [금융 도메인 QA Router 회귀](baselines/domain-qa-e2e-v1.2-router.json) | Router·linker 안전 경계 개선 후 strict·route·safety·evidence·answer 40/40 |
| [연결 전 라우팅 초기 진단 v1](baselines/pre-hcx-route-diagnostic-initial-v1.json) | AGGREGATE 미지원 시점의 Router 도입 전 search 강제 동작 4/28 |
| [연결 전 라우팅 개선 진단 v1](baselines/pre-hcx-route-diagnostic-improved-v1.json) | AGGREGATE 미지원 시점의 네 상품군·일곱 intent 라우팅 28/28 |
| [연결 전 라우팅 초기 진단 v2](baselines/pre-hcx-route-diagnostic-initial-v2.json) | 현재 AGGREGATE 기대값을 적용한 도입 전 replay 4/28 |
| [연결 전 라우팅 개선 진단 v2](baselines/pre-hcx-route-diagnostic-improved-v2.json) | 네 상품군 AGGREGATE 실행을 포함한 현재 회귀 28/28 |
| [연결 전 라우팅 초기 진단 v3](baselines/pre-hcx-route-diagnostic-initial-v3.json) | AGGREGATE·COMPARE 기대값을 적용한 도입 전 replay 4/28 |
| [연결 전 라우팅 개선 진단 v3](baselines/pre-hcx-route-diagnostic-improved-v3.json) | 네 상품군 AGGREGATE·same-family COMPARE 실행을 포함한 현재 회귀 28/28 |
| [Stage 2 승인 DB 재검증](baselines/stage2-approved-db-revalidation-2026-08-12.json) | 네 상품군 62건과 SQLite 교체·복원 race 방어를 실제 read-only DB에서 재검증 |
| [Stage 3 release 계약](baselines/stage3-release-contract-2026-08-12.json) | Manifest·Binding·anti-replay·trust·rollback harness·SQLite 실행 권한 145건 |
| [Stage 3 localhost OCI·rollback](baselines/stage3-local-oci-rollback-2026-08-12.json) | 합성 Registry exact digest와 격리 N-1→N→N-1 실기동; 외부 trust는 stub임을 함께 동결 |
| [Schema Dense CPU 임베딩 비교](baselines/schema-embedding-cpu-public-v1.json) | 네 상품군 공개 200문항에서 7개 모델을 비교하고 BGE-M3·Lexical 우선 결합을 blind 후보로 선정 |

자연어 비교 E2E baseline은 실행 16문항의 실제 `ComparisonCell.value`와 field
evidence provenance를 서로 별도의 fingerprint로 동결한다. parser 안전 계약은
ordered identity·정확한 연결어·위치별 문장부호 문법과 질문 전체 잔여 표현,
제외·대신·포함 역할, 빈·미종결·역방향·중첩·줄바꿈 따옴표 차단을 포함한다.

`product-compare-core-30`은 공모펀드와 겹치지 않는 세 상품군의 실행 18문항과
안전 차단 12문항을 동결한다. 기존 공모펀드 24문항과 합쳐 네 상품군 자연어
COMPARE 공개 회귀 54문항을 구성한다.

`search-aggregate-performance-8`은 네 상품군에서 SEARCH와 AGGREGATE를 하나씩
새 프로세스로 실행한다. 후보 수와 결과 지문이 모두 일치해야 통과하며 지연과
추가 RSS는 같은 장비의 방향성 기준선으로만 사용한다.

`cross-family-search-v1-4`는 국내·해외 ETP를 상품군별 단일 QueryPlan과
독립 Oracle·Verifier로 실행한다. 한쪽 0건 보존, 전체 `not_found`, family별
manifest와 직접 비교 차단을 실제 데이터 hash와 함께 4/4 고정한다. 결정론적
검색 baseline은 모델과 네트워크를 호출하지 않는다.

같은 공개 4문항의 grounded answer baseline은 각 family의 evidence만 로컬
Qwen에 전달하고 서버가 섹션을 조합하는 v2 배선을 검증한다. 생성 대상 2문항은
모두 `llm_grounded`, fallback 0이며 실제 호출은 양쪽 성공 2회와 부분 성공
1회로 총 3회다. 전체 빈 결과와 교차 비교 control은 모델을 호출하지 않는다.
다른 상품군 언급, 교차 비교·합산 문구 또는 family 하나의 provider·검증 실패가
발생하면 부분 모델 문장을 남기지 않고 전체 결정론적 답변으로 교체한다.

`hcx-contract-e2e-8`은 네트워크 없이 HCX semantic transport를 재생한다.
세 실행 상품군의 QueryPlan→Oracle→Verifier→Evidence→답변→Backend DTO와
fallback·timeout·무호출 정책 차단·서버 계획 일치 guard를 8개 시나리오로
검증한다. 실제 HyperCLOVA X 생성 품질이나 API 호환성 평가는 아니다.

`answer-adapter-contract-12`는 프레임워크 독립 `/answer` service adapter의
정상 응답, provider 설정·인증·rate limit·서비스·timeout·transport·응답 오류,
dataset 장애, 알 수 없는 내부 오류와 grounded answer fallback을 검사한다.
질문·credential·provider 오류 본문·파일 경로는 공개 ERROR DTO에 포함하지
않는다. 실제 FastAPI route나 네트워크 품질 평가는 아니다.

`internal-red-team-v1`은 네 상품군에 같은 10개 공격 유형을 적용한 공개
40문항이다. expected 하네스 40/40 이후 로컬 Qwen 최초 관측은 `3건` limit
handoff 불일치로 36/40이었고, 원인을 수정한 사후 회귀는 40/40이다. 최초
관측 report를 덮어쓰지 않으며 독립 blind 성능으로 주장하지 않는다.

이 수치는 HyperCLOVA X나 공식 공모전 평가 결과가 아니다. 동결된 개발 질문에서
로컬 LLM, 결정론적 linker, 계약, Oracle과 Verifier를 합친 시스템의 회귀
기준선이다.

대부분의 baseline은 완전 통과한 회귀 기준이다. `holdout_first_run_observed`,
`domain_qa_initial_observed`, `domain_qa_gold_observed` 상태는 최초 실행
결과가 완전하지 않아도
수정하거나 숨기지 않고 관측값 그대로 보존한다.

`official_http_first_observed`도 같은 원칙을 따른다. 실제 Docker GET 30문항은
공식 다섯 문자열·60초 예산 30/30, 의미 일치 24/30이며, 실패 6건은 모두
Backend의 의도적인 공모펀드 공식 실행 잠금이다. 내부 평가 전용 30/30과 실제
배포 정책을 섞지 않고 최초 차이를 그대로 보존한다.

`fund_approved_http_regression`은 그 최초 결과를 수정하지 않고 동일한 30문항에
명시적 `public_fund_v1_approved` 배포 정책만 적용한 재평가다. 의미·형식·시간
30/30, 답변 불가 5/5, Qwen 문장 검증 17/17, fallback 0건이다. 이 정책명은
팀의 배포 승인이며 주최 측의 공식 사용 승인을 뜻하지 않는다.

결정론적 공식 GET 동시성 회귀는 같은 30문항을 1·2·4개씩 제한적으로 전송해
모두 30/30을 확인했다. 단일 Uvicorn worker에서 동시성이 커질수록 p50·p95가
증가했으므로 처리량 개선이나 SLO로 주장하지 않고 계약 안정성 경계로만 사용한다.

로컬 Qwen 최신 재검증은 공모펀드 승인 정책에서 동시성 2로 30/30, 생성 대상
17/17, fallback 0건이다. 별도 확장 스모크는 정상 모델에서 14/14, 모델을 끈
뒤 네 상품군 결정론적 fallback에서도 14/14를 확인했다. 모두 개발용 공개
회귀이며 HyperCLOVA X·독립 blind·공식 평가 결과가 아니다.

`domain-qa-dev-v1-40`은 금융 도메인 담당자가 작성하고 AI 담당자가 검토한
개발 QA다. 40문항을 route·plan·retrieval·evidence·answer·safety·contract로
분해해 최초 strict 1/40, safety 32/40을 기록했다. 모델 성능이나 독립 blind
점수가 아니라 기존 상품군별 회귀가 포착하지 못한 자연어 경계의 개선 출발점이다.
설계와 해석은 [금융 도메인 QA 실험](../docs/evaluation-domain-qa.md)을 따른다.
v1 최초 관측은 SEARCH gold pending 상태를 그대로 보존하고,
`domain-qa-dev-v1.1-40`은 Q002의 잔존일수 0~365일 QueryPlan·Oracle·
evidence 지문을 추가한다. 정답은 완성됐지만 Router 수정 전이므로
strict 1/40·safety 32/40은 유지되며 E2 사후 회귀의 비교점이다.

`domain-qa-dev-v1.2-40`은 만기 표현 정규화와 예측·정책·
외부 시세·문서 dependency의 fail-closed Router 경계를 개선한 사후
회귀다. strict·route·safety·evidence·answer를 40/40, control의
잘못된 실행·오류를 0건으로 기록했다. 개선에 사용한 개발 세트이므로
독립 blind·LLM 생성 품질·공식 평가 점수로 해석하지 않는다.

`briefing-examples-v1-8`은 2026-08-06 설명회 화면에 공개된 난이도별 답변 가능
예시 5개와 답변 불가 예시 3개를 그대로 보존한 개발 회귀다. 최초 관측은 엄격
1/8, 답변 가능 실행 0/5, 답변 불가 안전 처리 2/3이다. 특히 존재하지 않는
`AAAA` 신용등급이 조건 없이 전체 채권 검색으로 실행된 문제를 숨기지 않고 기록한다.
안전 개선 회귀에서는 공손한 `알려줘`를 설명 intent로 고정하던 규칙을 제거하고,
등록되지 않은 신용등급을 SQL 전에 차단해 엄격 2/8, 답변 불가 안전 처리 3/3,
잘못된 실행 0건을 확인한다.
채권 개선 회귀에서는 `판매 가능`을 고객의 스냅샷 기준 매수 가능으로 연결하고,
`AA- 이상`을 registry의 최고→최저 등급 순서에 따라 AAA·AA+·AA0·AA- 목록으로
확정한다. 전체 데이터에서 27개 후보를 Oracle과 Verifier가 함께 확인해 엄격 3/8,
답변 가능 실행 1/5를 기록한다.
이 질문들은 실제 평가 문항이 아니며, 공개 후 수정 결과도 blind 성능으로 해석하지
않는다.

`official-mock-v1-30`은 설명회의 예상 난이도 하·중·상 각 10개와 답변 불가
5개 분포만 모사한 공개 모의평가다. 로컬 Qwen에서 검색·비교·집계·안전·근거·
공식 응답 계약 30/30, 답변 생성 16/17, 안전 fallback 1건을 기록했다. fallback은
`수익성 평가`라는 가치 판단 가능 문구를 Answer Verifier가 차단한 결과다.
AI 담당자가 기존 질문과 정답을 재사용했으므로 독립 blind나 공식 평가 점수가 아니다.

`qwen-eval-lab-v1`은 위 공개 30문항을 paraphrase·clause reordering·
distractor resistance 세 축으로 바꾼 90개 후보를 만든다. 숫자·식별자·연산자·
핵심 개념 보존 검사를 통과한 77개만 실행하고 폐기 13개는 생성 품질 실패로 별도
기록한다. gold 정렬 오류 2건을 원본 변경 없이 hash-pinned overlay로 교정한 뒤
결정론적 Agent와 전체 Qwen Agent가 각각 77/77, 의미·safety·evidence 100%를
기록했다. 안전 설명문 계약 전 fallback 3/61은 사후 전체 회귀에서 0/61로 줄었다.
질문 파생·사후 수정 세트이므로 독립 blind나 공식 점수가 아니다.

`semantic-roundtrip-v1`은 생성기에 원문 문장을 주지 않고 서버가 확정한
조건·정렬·비교·집계 의미로만 새 질문을 만든다. 생성 75개 중 기계 의미
보존을 통과한 64개에서 결정론적 Agent는 최초 15/64였고, 공통 문법 개선
후 출력과 QueryPlan 의미를 모두 비교하는 기준에서 64/64를 기록했다. 최초
Qwen grounded-plan은 28/64였고, 강화된 prompt·gate 재실행은 대기 중이다. 이 또한
공개 정답 파생·사후 수정 회귀이며 blind나 공식 점수가 아니다.

라우팅 v1은 AGGREGATE 미지원, v2는 COMPARE가 공모펀드에만 열렸던 당시의
봉인 이력이다. 현재 capability 정본은 원본을 수정하지 않고 별도
suite·commitment로 봉인한 v3를 사용한다.

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

네 상품군 공통 외부 blind 프로토콜은 공개 suite와의 근접 중복 검사, 질문·정답·
구현 커밋 봉인, 원자적 단일 사용 `started` 상태와 완료 report SHA-256 결합을
지원한다. 첫 실행이 중단되면 상태를 지우고 재시도하지 않고 최초 실패로 보존한다.

## HyperCLOVA X 연결 전 source freeze

내부 준비 코드·테스트·문서·baseline·protocol의 정렬된 source tree hash는
[`pre-hcx-readiness-v1.manifest.json`](protocols/pre-hcx-readiness-v1.manifest.json)에
보존한다. 이 manifest는 실제 external blind, 승인 corpus, 사람 평가와
HyperCLOVA X 재현이 남아 있음을 status와 external gate로 함께 기록한다.
`scripts/check-docs.py`가 현재 파일 수와 tree SHA-256을 매번 재계산한다.
