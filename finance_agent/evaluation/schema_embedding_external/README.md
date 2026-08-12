# Schema Dense 독립 blind·OOD 평가 인계서

상태: 평가 방법 동결 완료 · 외부 질문/정답 대기 · 점수 미생성

이 디렉토리는 공개 개발 질문으로 고른 임베딩 모델이 처음 보는 질문에서도 도움이
되는지 확인하기 위한 인계서다. 현재 점수를 일부러 만들지 않았다. AI 구현 담당자가
질문을 직접 만들거나 외부 정답을 먼저 보면 독립 blind가 아니기 때문이다

## 1. 이미 고정한 것

- 1순위 후보: `BAAI/bge-m3` 고정 revision
- 비교 후보: `nlpai-lab/KURE-v1` 고정 revision
- 결합 방식: 기존 단어 검색 순서를 보존하는 `lexical_first`
- 검색 대상: 상품 행이 아니라 field registry의 schema 설명문
- production 기능: OFF
- 통과 기준과 OOD 분할 규칙: 질문을 받기 전에 protocol로 동결

정확한 설정은
[schema-embedding-external-blind-v1.protocol.json](../protocols/schema-embedding-external-blind-v1.protocol.json)에
기록되어 있다

## 2. 금융 도메인 담당자가 준비할 파일

기존 external blind 100문항 절차를 그대로 사용한다

1. 질문 파일: `ExternalBlindQuestionSet` 형식
2. 비공개 정답 파일: `ExternalBlindAnswerKey` 형식
3. 현재 구현 commit과 두 파일의 SHA-256을 묶은 commitment

질문은 네 상품군 각 25개이며 SEARCH·DETAIL·COMPARE·AGGREGATE·EXPLAIN·CLARIFY·
UNSUPPORTED를 포함한다. 공개 질문을 단순히 숫자나 상품명만 바꿔 재작성하면 안 된다

Schema Dense 정답은 실행 가능한 QueryPlan의 다음 필드에서 자동 추출한다

- 검색 조건 필드
- 정렬 필드
- 비교 필드
- 그룹 필드
- 집계 대상 필드

화면 표시를 위해 자동으로 붙는 `projection` 필드는 정답에서 제외한다

## 3. 왜 BGE-M3만 평가하지 않는가

공개 질문에서 BGE-M3와 KURE-v1의 strict exact는 모두 175/181이었다. BGE-M3의
Recall@5가 정답 필드 한 개 높았지만 paired bootstrap exact 차이의 95% 구간은
`-1.66%p ~ +1.66%p`로 0을 포함했다

따라서 BGE-M3가 확실히 우월하다고 단정하지 않고, 외부 질문에서 두 모델을 같은 조건으로
한 번씩 비교한다. blind에서도 사실상 동률이면 원격 코드가 필요 없고 운영이 단순한 쪽,
지연시간과 메모리가 안정적인 쪽을 선택한다

## 4. OOD 기권 평가는 어떻게 분리하는가

Schema Dense의 높은 cosine 점수가 정답 확률을 뜻하지 않는다. 공개 질문에서는 exact
성공 175개의 top-1 중앙값이 `0.600643`, 실패 6개의 중앙값이 오히려 `0.615316`으로
겹쳤다. 현재 값만 보고 `0.6 이상이면 안전` 같은 규칙을 만들 수 없다

외부 100문항은 질문 ID의 SHA-256 순서로 미리 정한 두 부분으로 나눈다

- 앞 50문항: threshold를 정하는 calibration
- 뒤 50문항: 정한 threshold를 한 번만 검사하는 test

calibration에서는 실행 질문과 CLARIFY·UNSUPPORTED 질문의 top-1 점수와 1·2위 점수
차이를 비교한다. OOD 질문을 한 건도 실행하지 않으면서 정상 질문을 가장 많이 살리는
threshold가 있을 때만 test로 넘긴다. 그런 기준이 없으면 점수 기반 기권을 채택하지 않고
기존 Router와 서버 규칙만 유지한다

## 5. 최초 실행 전 체크리스트

- [ ] 질문과 정답을 AI 구현 담당자가 사전에 열어보지 않음
- [ ] 외부 bundle validator 통과
- [ ] 공개 suite와 정규화 유사도 0.84 이상 질문 0개
- [ ] BGE-M3·KURE-v1 revision과 `lexical_first` 설정 확인
- [ ] 구현 commit, 질문 hash, 정답 hash commitment 생성
- [ ] clean checkout에서 최초 실행 상태 파일을 한 번만 생성
- [ ] 원본 report와 SHA-256 보존 위치 합의
- [ ] 결과를 본 뒤 모델·지시문·threshold를 바꾸지 않기로 합의

기존 봉인 도구와 전체 external blind 작성 기준은
[연결 전 진단·외부 blind 문서](../../docs/evaluation-pre-hcx-diagnostic.md)를 참고한다

## 6. 결과를 받은 뒤 보고할 표

1. BGE-M3·KURE-v1·Lexical의 exact와 Recall@5·@10
2. 상품군별·질문 intent별 결과와 표본 수
3. 모델별 strict 실패 질문 ID와 원인
4. paired bootstrap 차이와 95% 구간
5. OOD calibration에서 고른 threshold 또는 채택 불가 사유
6. OOD test의 false accept와 정상 질문 false reject
7. CPU p50·p95·max, 메모리, 안전 계약 위반 수

이 일곱 항목이 채워지기 전에는 공개 개발 결과만으로 임베딩 모델을 최종 확정하거나
Backend의 사용자-visible 경로를 활성화하지 않는다
