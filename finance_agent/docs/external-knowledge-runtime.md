# External Knowledge Runtime

이 문서는 제공 DB 상품을 기준으로 공식 외부 근거를 보강하는 실행 계약을 설명한다.
외부 문서에 등장한 상품을 새 상품처럼 답변하지 않는 것이 가장 중요한 불변조건이다.

## 고정 처리 흐름

1. `SafetyEnvelope`가 공격·예측·보장·시스템 노출 요청을 먼저 차단한다.
2. HCLX가 제한된 `IntentAnalysisDraft`를 만들며, URL·상품·수치·SQL은 제안하지 않는다.
3. 서버의 Ontology·Lexical·Schema Dense/HCLX Resolver가 명확한 조건과 미해결 의미를
   처리하고, 서버가 `Typed QueryPlan`을 확정한다.
4. 승인된 2026-08-24 SQLite DB에서 비격리 상품 ID를 먼저 고정한다.
5. 외부 근거가 필요한 relation·동향만 `Source Registry` 범위 안에서 Cache-first로 찾는다.
6. Cache Miss에서는 NAVER API HUB가 공식 도메인별 후보 URL만 찾는다. 검색 snippet은
   근거로 사용하지 않는다.
7. Connector가 승인 hostname·path·media type·발행일·크기·timeout을 검사하고 HTML/PDF
   원문을 가져온다. 명시적 URL/HTML 발행일이 없거나 날짜가 충돌하면 폐기한다.
8. exact evidence span과 시점을 검증한 relation만 제공 DB 상품 ID와 교집합한다.
9. SQLite/Python이 교집합 상품의 수치와 순위를 계산하고 Verifier가 상품 ID·기간·출처·
   DB field를 다시 검사한다.
10. HCLX는 검증된 본문 hash를 보존하는 범위에서만 최종 답변을 생성한다.

## 상태 계약

- `SUCCESS`: 외부 relation 근거와 제공 DB 상품 ID의 교집합이 검증됨.
- `NOT_FOUND`: 완전한 근거 체인을 정상 확인했지만 제공 DB 교집합이 비어 있음.
- `CANNOT_VERIFY`: 검색 장애, 검색 비완전성, 날짜·근거 부족 등으로 존재 여부를 끝까지
  검증하지 못함.
- `CLARIFY`: 상품군·기간·관계 의미를 하나로 확정할 수 없음.
- `UNSUPPORTED`: 금융상품 범위 밖이거나 안전정책 위반 요청임.

일반 웹 검색 결과가 0건이라는 이유만으로 `NOT_FOUND`를 반환하지 않는다. 검색엔진은
공식 사이트 전체가 비어 있음을 증명할 수 없으므로 이 경우 `CANNOT_VERIFY`다.

## Trust boundary

- Source Registry는 공식 publisher의 HTTPS hostname·path prefix·문서 유형·capability를
  고정한다.
- Redirect는 허용하지 않으며 최종 URL을 다시 검사한다.
- HCLX와 NAVER API HUB credential은 evaluation/production에서 owner-only 파일로만 읽는다.
- 검색·fetch·byte·deadline은 서버 budget으로 제한한다.
- corpus, BM25/Dense index, Temporal Relation Store, Verifier contract, 제공 DB hash는 하나의
  `AgentReleaseManifest`에 결합해야 실행할 수 있다.
- Release와 runtime artifact가 다르면 시작 단계에서 실패한다.

## Document Dense

KURE-v1 `Document Dense`는 BM25가 놓친 공식 문서 후보를 의미 유사도로 보완한다. 현재
artifact는 `offline_evaluation_only`이고 실제 답변 권한이 없다. 공개 16문항 calibration은
기계적 기준선일 뿐 독립 Blind 성능 근거가 아니다.

`finance-check-external-preblind`는 Blind 파일을 읽지 않고 다음을 검증한다.

- 다섯 external capability에 공식 live source scope가 있는가
- coverage와 production execution이 여전히 OFF인가
- KURE revision과 artifact hash가 고정됐는가
- 공개 calibration이 정확한 artifact에 묶였는가
- 공개 Dense recall@5가 후보 생성 기준 98% 이상인가

## 독립 Blind 전 의도적으로 남긴 잠금

- `approved_knowledge_coverage_manifest.json`의 다섯 capability는 모두 `not_configured`이며
  runtime execution은 `false`다.
- 독립 Blind 100문항과 private Gold는 최초 실행 전까지 열거나 학습 데이터로 사용하지
  않는다.
- Blind에서 `SUCCESS/NOT_FOUND/CANNOT_VERIFY/CLARIFY/UNSUPPORTED`, 제공 DB 상품 ID,
  source/date/evidence 정확성을 확인한 뒤 통과한 capability만 승인한다.
- 승인 후 corpus·BM25·KURE index·relation bundle SHA-256을 새 Release에 묶어 활성화한다.

## 현재의 보수적 제한

- JavaScript 전용 동적 페이지는 `OfficialDynamicPageRenderer` 주입 seam은 있으나 기본
  Backend에는 브라우저 renderer가 조립되지 않았다. 정적 HTML에 근거가 없으면
  `CANNOT_VERIFY`다.
- 자회사 → 구성종목 같은 multi-hop은 봉인된 relation chain이 있을 때만 실행한다.
  단순 문서 동시 등장만으로 자회사 관계를 추론하지 않는다.
- 외부 근거는 제공 DB 후보를 줄일 수만 있으며, 제공 DB에 없는 상품을 추가할 수 없다.
