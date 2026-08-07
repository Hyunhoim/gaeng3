# 금융상품 Ontology 제출 계약

마지막 갱신: 2026-08-07

## 0. 쉽게 설명하면

Ontology는 금융상품 14만여 건을 다시 저장한 데이터베이스가 아니다. 각 상품군에
어떤 항목이 있고, 그 항목이 숫자인지 날짜인지, 어떤 단위인지, 검색·정렬·비교에
사용해도 되는지를 기계가 읽을 수 있게 적은 데이터 설명서다.

정본은 기존 `field_registry.yaml`이며, 제출용 Turtle 파일은 이 정본에서
자동 생성한다. 같은 필드의 뜻을 YAML과 TTL에 사람이 따로 적어 서로 달라지는
문제를 막기 위한 구조다.

## 1. 제출 파일

저장소 루트의 `ontology/`에는 다음 다섯 파일만 둔다.

| 파일 | 설명 | registry 상품군 | 필드 수 |
| --- | --- | --- | ---: |
| `common.ttl` | 모든 상품군이 공유하는 클래스와 속성 정의 | 공통 | 공통 vocabulary |
| `bond_kr.ttl` | 국내채권 데이터와 필드 설명 | `bond` | 25 |
| `etf_kr.ttl` | 국내 ETF·ETN 데이터와 필드 설명 | `domestic_etp` | 32 |
| `etf_gl.ttl` | 해외 ETF·ETN 데이터와 필드 설명 | `overseas_etp` | 17 |
| `fund_pub.ttl` | 공모펀드 데이터와 필드 설명 | `fund` | 27 |

각 상품군 파일에는 원천 ID, 데이터 grain, 기준일, 기본키, 격리 행 수,
실행 활성 상태와 필드별 다음 정보가 포함된다.

- canonical 이름과 한글 이름·별칭
- 값 유형·단위·품질·coverage
- 검색·선택·정렬·집계·비교 가능 여부
- 허용 연산자와 enum 값
- 원천 열·정규화 방식·기준일 방식
- sentinel과 코드 변환 규칙

## 2. 생성과 검사

`finance_agent/`에서 개발 의존성을 설치한 뒤 실행한다.

```bash
python scripts/sync-ontology.py
python scripts/sync-ontology.py --check
```

첫 명령은 `field_registry.yaml`에서 다섯 TTL을 다시 만든다. 두 번째 명령은
파일을 수정하지 않고 다음을 확인한다.

1. 파일명이 공식 다섯 개와 정확히 같은지
2. 각 파일이 RDFLib Turtle parser를 통과하는지
3. TTL 내용이 현재 field registry에서 생성한 값과 한 글자도 다르지 않은지
4. 네 상품군의 모든 canonical field가 빠짐없이 포함됐는지

전체 Agent pytest에도 같은 검사가 포함된다.

## 3. 변경 규칙

- 필드 의미를 바꿀 때 TTL을 직접 수정하지 않고 `field_registry.yaml`을 먼저 수정
- registry 단위 테스트를 통과한 뒤 `sync-ontology.py`로 다섯 파일 재생성
- `--check`, 전체 pytest와 문서 검사를 통과한 변경만 커밋
- 현장 구두 메모와 서면 화면이 충돌하므로, 공식 정정 전까지 Turtle 다섯 파일을 유지

## 4. 현재 범위와 한계

- 현재 Ontology는 실제 구현과 일치하는 데이터 schema·capability 설명서
- GraphDB, 지식그래프 검색이나 상품 간 추천 관계를 사용했다는 뜻은 아님
- 실제 상품 인스턴스와 원천 데이터 행은 TTL에 복제하지 않음
- 금융 도메인 담당자의 용어·한글 label 검수와 주최 측의 최종 형식 확인은 남아 있음
- 관계 기반 검색이 현재 SQL·BM25 기준선보다 좋아지는지는 별도 실험 후 판단

