# 예선 제출 체크리스트

상태: 활성

기준일: 2026-08-19

공식 과제 소개자료 7페이지의 예선 제출 항목과 변경 금지 조건을 기준으로 한다.

## 0. 일정

- [x] 2026-08-06 설명회 질문 목록 확정
- [x] 참석 팀원의 설명회 기록 전달 및 현장 사진·공식 PDF 교차 검토
- [x] 확정사항·구두 메모·후속 확인 항목을 설명회 반영 기록에 분리
- [x] 크레딧·정확한 모델 ID·endpoint·인증 규격 확보 전 HyperCLOVA X 실제 연결 보류
- [ ] 2026-09-06 이전 최종 source commit·제안서·API 문서 동결
- [ ] 평가 기간 동안 공지된 기간 내 API 활성 상태 유지
- [ ] 마감 이후 금지되는 commit·push·서버 변경 범위 재확인

공식 자료의 API 운영 예시는 2026-09-07~09-20이며 변경 가능성이 명시되어 있다.

## 1. 소스코드와 재현 환경

- [ ] 주최 측 GitHub Organization의 Private Repository에 최종 소스 반영
- [ ] root README에 전체 환경 구성과 한 번에 실행하는 명령 제공
- [ ] Dockerfile 또는 공식 평가 환경에서 재현 가능한 동등한 환경 정의
- [ ] Python·Node·DB·HyperCLOVA X 의존 버전 고정
- [ ] `.env.example`에 변수명만 제공하고 credential은 제외
- [ ] 원천 데이터·DB 생성과 애플리케이션 시작 순서 문서화
- [ ] clean checkout에서 build·test·start 재현
- [x] `ontology/common.ttl`, `bond_kr.ttl`, `etf_kr.ttl`, `etf_gl.ttl`, `fund_pub.ttl` 생성
- [x] Ontology Turtle 문법과 field registry 정합성 자동 검사
- [ ] 비밀정보, 로컬 경로, 모델 weight, `artifacts/`가 Git에 없는지 확인
- [ ] 공식 제출 범위 확정 후 로컬 LLM provider·설정·스크립트·의존성 제거
- [ ] Git 이력을 재작성하지 않고 개발 이력과 제출 경로를 투명하게 분리
- [x] 외부 문서의 금융·데이터 권한 독립 review·HTTPS 출처·본문 hash·변조 차단 계약
- [ ] 실제 외부 corpus 출처·사용 권한·snapshot·manifest SHA-256 승인
- [ ] 승인 corpus BM25 검색 품질·충돌·최신성 평가 후 Release에 연결
- [x] 승인 상품 DB 제공 관계 58,005개 색인·상품 ID 재검증·출처·해시 계약
- [ ] 관계 값·한영 표기·동의어의 금융 도메인 검수
- [x] P0-7 관계·문서 Typed Plan·Claim Verifier·결정론적 fallback 내부 계약
- [ ] P0-7 공개 Router·Backend adapter와 P0-10 Agent Release 연결

현재 상태: Conda·pip와 Agent Core wheel, 공식 XLSX 준비와 FastAPI를 잇는 통합
Docker 실행은 재현 완료. `nextjs-frontend/`는 아직 저장소에 없으며 화면 합류 후
같은 루트 Compose 실행 경로에 추가해야 한다.

## 2. 기술 제안서

- [ ] 제안 요약
- [ ] 문제 정의
- [ ] 제안 방법
- [ ] 시스템 구성도
- [ ] 주요 기능 흐름도
- [ ] 사용자 시나리오
- [ ] 기대효과·확장성
- [ ] 기술완성도·성능 수치에 범위·장비·평가 성격 표시
- [ ] 내부 공개 회귀와 external blind 결과 구분
- [ ] 현업 활용성·금융 리스크 관리와 미지원 범위 포함
- [ ] 모든 수치가 [근거 맵](evidence-map.md)과 일치
- [ ] 최종 PDF의 글꼴·표·그림·링크·페이지 번호 시각 검수

## 3. 평가용 API 서버

- [ ] Public 통신 가능한 endpoint
- [x] `GET /answer` route와 다섯 문자열 response model
- [x] `question_id`, `question` 필수 query parameter 규격 확인
- [x] 다섯 필수 응답 필드가 모두 문자열인 규격 확인
- [x] 성공·미검색·역질문·미지원·재시도 불필요 오류가 동일한 다섯 필드와 HTTP 200 반환
- [x] 정의되지 않은 query parameter에도 HTTP 500을 반환하지 않음
- [x] 질문당 300초 제한보다 짧은 270초 외곽 timeout과 HTTP 504 안전 응답 처리
- [x] timeout·5xx 최대 2회 재시도에 맞춘 일시 장애 503/504와 동일 요청 중복 실행 방지
- [ ] 미확정 QPS·동시 요청·최대 입력 길이 확인 후 처리
- [ ] 인증이 필요하면 주최 측 호출 방식과 호환
- [ ] health check·구조화 로그·credential masking
- [x] provider·dataset 장애의 안전한 공식 응답 계약
- [ ] 평가 기간 가용성·재시작·모니터링 계획

### 공식 예시와 내부 DTO의 경계

| 공식 예시 | 내부 Agent Core | adapter 원칙 |
| --- | --- | --- |
| `question_id` | `request_id` | 값 보존, 이름만 매핑 |
| `question` | request의 원문 | 응답에도 원문 그대로 포함 |
| `retrieved_context` | products·comparisons·aggregates·documents·citations | JSON 문자열로 안전하게 직렬화 |
| `think_trace` | intent·QueryPlan hash·도구·검증·fallback 상태 | 숨은 사고과정 없이 실행 사실만 제공 |
| `answer` | 검증된 최종 answer | 변경 없이 전달 |

내부 DTO의 추가 필드를 공식 응답에 그대로 노출하지 않는다. 현장 자료에서 두 context
필드가 문자열로 확인됐으므로 별도 official adapter의 다섯 문자열 필드를 동결한다.

## 4. 2026-08-06 설명회 반영 상태

현장 질문과 공식 답변 원문은
[8월 6일 설명회 반영 기록](briefing-2026-08-06.md)에 확정·잠정·미확정 사항을
구분해 기록한다.

- [ ] 허용 HyperCLOVA X 모델명·버전
- [ ] Structured Outputs 또는 JSON schema 지원 범위
- [ ] endpoint·인증 header·요청·응답 body
- [x] `retrieved_context`, `think_trace`의 필수 문자열 타입
- [ ] `retrieved_context`, `think_trace`의 세부 채점 방식
- [ ] 숨은 사고과정 대신 구조화 실행 기록을 제공해도 되는지
- [x] 공식 제출 adapter는 예시의 다섯 필드만 반환하기로 결정
- [x] 후속 운영 정보의 300초·timeout/5xx 최대 2회 재시도를 270초 외곽 계약으로 반영
- [ ] QPS·retry-after·동시 요청·입력 길이
- [x] 답할 수 없는 질문도 같은 응답 스키마와 HTTP 200 사용
- [ ] 잘못된 필수 입력의 공식 처리 규칙
- [ ] Docker·DB·외부 데이터·네트워크 제약
- [ ] 평가 질의 분포와 정확성·지연·정성평가 배점
- [x] 임베딩은 비-생성 검색 구성요소로 사용 가능하다는 팀 확인 반영
- [ ] re-ranker·NER·번역 모델의 LLM 해당 여부
- [ ] 임베딩 허용 근거로 제출할 공식 문구 원본 보존
- [ ] 로컬 LLM을 개발 단계에 사용한 코드·문서·Git 이력의 제출 허용 범위
- [ ] 보수 0·수익률 0·판매 가능 상태·공모펀드 grain의 공식 의미
- [x] 평가 배점 20·40·40, 예상 30문항·미응답 5문항, 초기 60초 권장·후속 300초 제한 기록 분리
- [x] 도메인별 Ontology `.ttl` 5개를 서면 기준으로 준비하기로 결정
- [ ] 구두 메모와 충돌하는 Ontology 형식을 서면으로 재확인
- [ ] HyperCLOVA X가 20만 원 크레딧 적용 서비스인지 확인

## 5. 최종 동결

- [ ] source commit SHA 기록
- [ ] 데이터·suite·baseline·제안서 PDF SHA-256 기록
- [ ] 공식 provider 외 모델이 평가 mode에서 fail-closed인지 확인
- [ ] 제출 후보에 `local_test`, `Qwen`, `vLLM`, `ENABLE_NON_HCX_TEST_LLM` 미포함 확인
- [ ] external blind 최초 실행 결과와 사후 수정 결과 분리
- [ ] 최소 2명 사람 평가 완료
- [ ] API smoke·contract·load·장애 복구 테스트 완료
- [ ] 제출 후 변경 금지 절차를 팀 전원이 확인
