# BM25/SQLite FTS 문서 RAG 최소 계약

마지막 갱신: 2026-07-30

## 1. 범위

정형 상품 검색으로 답할 수 없는 금융 용어·설명 문서를 검색하기 위한 최소
retrieval 계층이다. 생성형 모델이나 외부 수집기를 포함하지 않으며, 사용 권한을
확인해 호출자가 직접 전달한 문서만 적재한다.

현재 포함:

- 결정론적 문단·길이 기반 chunking
- SQLite FTS5 `unicode61`과 BM25 검색
- 한국어 조사의 최소 lexical expansion
- source kind, 문서 ID, 기준일, metadata equality 필터
- top-k와 명시적 `not_found`
- chunk text·문서 hash·출처 URI·기준일·metadata를 포함한 evidence
- 동일 ID 재적재의 idempotency와 다른 내용 덮어쓰기 차단
- 같은 relevance 구간에서 주최 측 제공 데이터 우선

현재 제외:

- 웹 crawler, 자동 다운로드, 대량 외부 수집
- OCR·PDF parser
- embedding, cross-encoder, NER, 번역 모델
- 문서 evidence를 자유 생성 답변으로 바꾸는 production provider
- 문서 간 사실 충돌을 자동 판정하는 금융 의미 모델

## 2. 적재 계약

문서 입력에는 다음 값이 필요하다.

- 안정적인 `document_id`
- 제목과 정규화 전 원문 text
- 팀이 검토할 수 있는 `source_uri`
- `provided` 또는 `external_approved` source kind
- 문서 기준일 `as_of`
- 짧은 문자열 metadata

문서 전체의 정규화 text SHA-256을 저장한다. 같은 ID와 완전히 같은 내용·출처·
metadata를 다시 넣으면 기존 chunk 수를 반환하고, 하나라도 다르면
`DocumentConflictError`로 차단한다. 묵시적 update나 overwrite는 없다.

## 3. 검색·근거 계약

검색 결과는 `DocumentSearchResponse`이며 일치 chunk가 없으면
`status=not_found`, 빈 evidence를 반환한다. 각 evidence는 다음을 포함한다.

- document·chunk 식별자와 chunk 순서
- 제목과 검색에 사용된 원문 chunk
- 출처 URI와 source kind
- 문서 기준일과 문서 SHA-256
- metadata와 BM25 relevance score

FTS query와 모든 filter 값은 parameter binding으로 전달한다. metadata key도
제한된 형식만 허용한다. 결과 정렬은 BM25 relevance를 먼저 사용하고 같은
relevance 구간에서는 `provided`를 `external_approved`보다 우선한다.

## 4. 데이터 우선순위

외부 승인 문서는 보조 설명에 사용할 수 있지만 제공 데이터와 충돌할 때 평가
정본은 주최 측 제공 데이터다. 현재 retrieval 계층은 출처 우선순위를 보존해
반환할 뿐 사실 충돌을 자동 해결하지 않는다. 답변 계층은 충돌을 발견하면 제공
데이터를 채택하고 충돌 사실을 경고하거나 확인 불가로 처리해야 한다.

## 5. 승인 게이트

실제 corpus 적재 전 팀이 다음을 확인한다.

- 금융상품 관련 데이터인지
- 수집·저장·제출·평가 사용이 허용되는지
- 문서 기준일과 원출처를 표시할 수 있는지
- 공식 제공 데이터와 충돌할 때 우선순위를 지킬 수 있는지
- 개인정보·유료 콘텐츠·재배포 제한이 없는지

금융 도메인 담당자가 후보 corpus와 활용 목적을 검토하고, AI 담당자가 적재
manifest·hash·검색 품질 테스트를 추가한 뒤 활성화한다. 승인된 실제 corpus가
없으므로 현재 테스트는 synthetic 문서만 사용한다.
