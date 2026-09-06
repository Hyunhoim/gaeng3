# 외부 공식 문서 corpus 검토 요약

이 문서는 승인 결정을 돕는 요약이며, 이 파일의 존재 자체는 문서 사용 승인을 뜻하지 않는다.
Runtime 활성화에는 `data_rights`와 `finance_domain` 두 명의 서로 다른 검토자가 원문과
권리 범위를 확인한 뒤 별도의 승인 JSON을 작성해야 한다.

## 고정 대상

- Pending manifest: `finance_agent/artifacts/pending-corpus/20260831-phase4-v11/merged-corpus-v11/pending-manifest.json`
- Pending manifest SHA-256: `ac71028b7d4f90c82a1b2750fa11e1126f200698071e78b8c4072c6e2debb24b`
- Review worksheet: `finance_agent/artifacts/pending-corpus/20260831-phase4-v11/merged-corpus-v11/external-corpus-review-worksheet.json`
- Review worksheet SHA-256: `806449764e3862bc0f509a6003dee67e5e45ece44279b29ab7876093e4ce52f1`
- 문서 수: 106개
- 정제 본문 합계: 25,082,031 bytes
- 증거 기준일: 2026-08-24
- Pending 수집 당시 Source Registry SHA-256: `3b8234b3691ce407c35f5bfe11653cd6124b774fd5c2b75d37e7339aba6ea110`
- 현재 재검증된 Source Registry SHA-256: `a7d4747c058c81da515571c04e567d02e5422e94059aaea8b76a3f50b5fabfc2`

현재 Registry로 URL·발행기관·capability를 다시 대조했으며, 일치하지 않는 문서는 worksheet
생성 전에 차단된다. 발행일이 비어 있는 27개 문서는 Registry에서
`publication_date_required=false`인 정적 상품 페이지 또는 factsheet 출처에만 존재한다.

## 사용 권한 사전 조사 결과

공식 발행기관의 저작권·이용 정책을 확인했지만, 현재 수집본 106개 중 원문 snapshot 저장,
검색 색인, 공모전 답변 근거 사용, 평가용 corpus artifact 포함의 네 권리를 모두 명시적으로
허용하는 자료는 확인되지 않았다. 따라서 아래 결과만으로 문서를 승인하지 않는다.

| 판정 | 문서 수 | 의미 |
| --- | ---: | --- |
| 명시적 제한 또는 별도 허가 필요 | 37 | 무단 복제·DB화·배포 등을 제한하는 정책이 있어 서면 허가 없이 원문 corpus에 포함하지 않음 |
| 조건부이지만 충족 여부 미확인 | 4 | 금융위원회 자체 저작물의 조건부 자유이용 범위에 해당하는지 문서별 확인과 사전 협의가 필요 |
| 명시적 재이용 근거 미확인 | 65 | 공개 열람 가능성만 확인됐으며 저장·색인·배포 허용으로 해석하지 않음 |

주요 공식 정책은 [금융위원회 저작권정책](https://www.fsc.go.kr/ut020104),
[한국거래소 법적고지](https://info.krx.co.kr/contents/KRX/06/06070200/KRX06070200.jsp),
[NH-Amundi 이용약관](https://www.nh-amundi.com/policy/terms-of-use),
[미래에셋자산운용 이용약관](https://develop.investments.miraeasset.com/tigeretf/ko/footer/termsOfUse/index.do)에서 확인했다.
삼성자산운용 공식 사이트도 사전 허가 없는 자료 사용·DB화를 제한한다. Global X,
KraneShares, 에코프로, Moonshot AI, TIMEFOLIO 자료에서는 네 권리를 모두 부여하는 명시적
라이선스를 찾지 못했다. 이는 법률 판단이 아니라 Runtime 승인에 필요한 긍정적 근거가
현재 없다는 보수적 데이터 거버넌스 판정이다.

Runtime 활성화 경로는 다음 두 가지뿐이다.

1. 발행기관의 서면 허가 또는 문서별 공공누리·CC 등 명시적 라이선스를 확보해 두 독립
   검토자가 승인한다.
2. 원문 corpus 대신 사용 권한이 명확한 공개 데이터와 독자적으로 구조화한 사실 레코드로
   교체하고, 짧은 인용·출처 URL의 사용 범위를 별도로 검토한다.

두 번째 경로 중 기업 공시는 Open DART 공식 API로 전환할 수 있다. Open DART는 API 원문
XML에서 원하는 자료를 추출해 사용할 수 있다고 안내하며, 공공데이터포털의
[공시서류 원본파일 API](https://www.data.go.kr/data/15060602/openapi.do)는 무료·이용허락범위
제한 없음으로 등록돼 있다. 따라서 API XML에서 추출한 최소 사실 레코드·근거 구간·원문
URL·hash만 저장하고, DART viewer HTML·외부 첨부·이미지는 포함하지 않는 별도 corpus를
만든다. API key는 수집 단계 Secret으로만 사용하고 Runtime·Docker image·Git에 넣지 않는다.
이 경로도 실제 수집 결과와 문서별 금융 의미를 두 검토자가 확인한 뒤 승인한다.

## 출처별 범위

| Source Registry ID | 발행기관 | 문서 | 발행일 없음 | 사용 범위 |
| --- | --- | ---: | ---: | --- |
| `ecopro-corporate` | 에코프로 | 1 | 1 | 기업·자회사 관계 |
| `fsc-policy` | 금융위원회 | 2 | 0 | 펀드 구조·전략·동향 |
| `fsc-policy-other` | 금융위원회 | 2 | 0 | 펀드 구조·전략·동향 |
| `globalx-etf-factsheets` | Global X | 12 | 12 | 구성종목·위험요인·테마 이력 |
| `hanaro-etf-official` | NH-Amundi자산운용 | 2 | 0 | 펀드 구조·구성종목·위험·테마 |
| `kind-corporate-disclosures` | 한국거래소 KIND | 23 | 0 | 기업·자회사 관계 |
| `kraneshares-main-product` | KraneShares | 42 | 2 | 구성종목·위험요인·테마 이력 |
| `kraneshares-product` | KraneShares | 5 | 4 | 구성종목·위험요인·테마 이력 |
| `miraeasset-investments` | 미래에셋자산운용 | 3 | 0 | 구성종목·위험요인·테마 이력 |
| `moonshot-ai-official-forum` | Moonshot AI | 1 | 0 | 테마·기업 관련성 확인 후 제공 DB 교집합 검증 |
| `samsungfund-kodex-newsroom` | 삼성자산운용 | 1 | 0 | 구성종목·위험요인·테마 이력 |
| `samsungfund-kodex-product` | 삼성자산운용 | 8 | 8 | 구성종목·위험요인·테마 이력 |
| `timefolio-manager-official` | 타임폴리오자산운용 | 4 | 0 | 펀드 구조·구성종목·위험·테마 |

## 검토자가 확인할 사항

### 데이터 사용 권한 담당자 (`data_rights`)

각 문서에 대해 다음 네 항목을 실제로 확인한다.

1. 원문 snapshot 저장 가능 여부
2. Agent 검색 색인 사용 가능 여부
3. 공모전 평가 답변 근거 사용 가능 여부
4. 평가 서버의 별도 읽기 전용 corpus artifact에 포함 가능 여부

네 항목 중 하나라도 확인할 수 없으면 해당 문서를 승인 목록에서 제외한다. 공식 사이트라는
사실만으로 저장·재배포 권한이 자동으로 생긴다고 간주하지 않는다.

### 금융 도메인 담당자 (`finance_domain`)

각 문서에 대해 다음 내용을 실제로 확인한다.

1. 문서가 선언된 capability의 금융 근거로 적합한지
2. 제목·발행기관·발행일·기준일이 원문과 일치하는지
3. 2026-08-24 이후 정보가 섞이지 않았는지
4. 제공 상품 DB와 충돌할 때 외부 문서가 상품 수치의 기준으로 사용되지 않는지
5. 근거 문장이 상품 관계·동향·위험요인을 과장하지 않는지

## 승인 이후의 자동 경계

실제 승인 입력이 들어오면 다음 순서로만 Runtime artifact를 만든다.

1. Pending metadata와 승인 입력의 문서별 SHA-256 대조
2. 승인 intake 생성
3. Approved Corpus Manifest 봉인
4. BM25 색인과 KURE Document Dense 색인 생성
5. 관계 저장소 생성
6. activation receipt에 corpus·index·Registry·기준일 결합
7. capability별 coverage manifest에 동일 SHA-256 기록
8. Cache-only Release preflight 통과 후에만 평가 Runtime 활성화

NAVER API HUB Live Search는 평가 답변 경로에서 사용하지 않는다. 승인 corpus에 근거가 없으면
일부 조건을 무시해 상품을 반환하지 않고 `CANNOT_VERIFY`로 종료한다. 외부 근거까지 정상적으로
확인했지만 제공 DB와의 교집합이 비어 있을 때만 `NOT_FOUND`를 반환한다.
