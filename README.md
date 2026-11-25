# 금융상품 Agent (팀 갱갱갱)

제10회 2026 미래에셋증권 AI Festival — 금융상품 Agent 과제 제출물

자연어 질문을 받아 주최 측 제공 데이터(국내채권, 국내·해외 ETF·ETN, 공모펀드)에서 상품을
조회·비교·집계하고 근거와 기준일을 함께 답한다. 상품명·수치·순위·출처는 SQLite와 Python 검증기가
확정하고, HyperCLOVA X(HCX-007)는 질문 의도 해석과 검증된 결과의 문장화만 담당한다. 구성종목·
자회사·테마·위험요인 같은 외부 근거는 검토·승인해 봉인한 공식 문서 258건(12개 발행기관)에서만
찾는다. 데이터로 확인할 수 없는 질문에는 추측하지 않고 확인 불가를 안내하거나 되묻는다.

## 제출물

| 항목 | 위치 |
| --- | --- |
| 소스코드 | `src/`, `requirements.txt`, 이 README |
| Ontology | [`ontology/`](ontology/) — `common.ttl` `bond_kr.ttl` `etf_kr.ttl` `etf_gl.ttl` `fund_pub.ttl` |
| 기술 제안서 | [`docs/기술_제안서.pdf`](docs/기술_제안서.pdf) · [부록: 수집·적재 Flow, 활용 로직, 재현 절차](docs/기술_제안서_부록.md) |
| 평가용 API 서버 정보 | [`docs/평가용_API_서버_정보.md`](docs/평가용_API_서버_정보.md) |

## 환경 구성과 실행

요구 사항: Ubuntu 22.04/24.04 x86_64, Docker Engine 24 이상, Docker Compose v2, 주최 측 XLSX 디렉터리(`2.금융상품_20260824`, 파일 8개). 아래 절차는 Ubuntu에서 확인했다.

### 1. 기본 구성

```bash
cp src/fastapi_backend/.env.example src/fastapi_backend/.env   # FINANCE_RAW_DATA_DIR 를 XLSX 디렉터리 경로로 수정
./compose.sh up --detach --wait
curl --fail http://127.0.0.1:18001/health
```

이미지 빌드 → XLSX 정규화·검증 → 상품군별 SQLite·관계 색인 생성 → 서버 시작까지 한 번에 수행한다.
검색·조건·비교·집계 질문에 결정론적 경로로 답하며, HyperCLOVA X 호출과 외부 문서 검색은 하지 않는다.
종료는 `./compose.sh down`.

### 2. 평가 구성

준비할 것 세 가지.

1. **CLOVA Studio API 키** — 키 값만 담은 파일을 만든다. 환경변수로 넘기지 않는다.

   ```bash
   mkdir -p ~/.secrets/finance-agent
   printf '%s' '<CLOVA Studio API 키>' > ~/.secrets/finance-agent/clovastudio-api-key
   chmod 600 ~/.secrets/finance-agent/clovastudio-api-key
   ```

2. **KURE-v1 임베딩 모델** (Hugging Face 공개 모델, 약 2.2 GB)

   ```bash
   pip install -U huggingface_hub
   huggingface-cli download nlpai-lab/KURE-v1 \
     --revision d14c8a9423946e268a0c9952fecf3a7aabd73bd9 --cache-dir ~/kure-cache
   ```

3. **승인 문서 묶음** — 저장소에 포함되어 있다(`src/finance_agent/artifacts/approved-corpus/`, 97 MB).
   12개 발행기관 공식 문서 258건의 정규화 텍스트, 문서 매니페스트(문서별 출처 URL·발행일·SHA-256),
   KURE Document Dense 색인, 활성화 영수증, 관계 파일이다. 원문 HTML·PDF는 저장하지 않는다.

실행은 한 명령이다. 이미지 빌드 → 데이터 준비 → 릴리스 봉인(코드·DB·색인·문서 묶음의 SHA-256 결속) →
컨테이너 기동 → `/health`·`/answer` 확인 순서로 진행하며, 봉인 값과 실제 파일이 다르면 시작하지 않는다.
문서 묶음 경로는 저장소 안의 기본값을 쓴다.

```bash
RAW_DATA_DIR=/path/to/2.금융상품_20260824 \
SECRETS_DIR=$HOME/.secrets/finance-agent \
KURE_CACHE_DIR=$HOME/kure-cache \
  src/fastapi_backend/scripts/manual-release.sh all
```

```bash
curl --get "http://127.0.0.1:8080/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=캠브리콘이 편입된 중국 반도체 ETF를 알려줘"
```

중지는 `src/fastapi_backend/scripts/manual-release.sh stop`. 포트는 `PORT=`로 바꾼다.
Docker 없이 실행하려면 Python 3.12에서 `pip install -r requirements.txt` 뒤 [Agent README](src/finance_agent/README.md)를 따른다.
환경변수 전체 목록은 [Backend README](src/fastapi_backend/README.md)에 있다.

## 저장소 구조

```text
.
├── README.md
├── requirements.txt
├── ontology/              # 도메인별 Ontology (.ttl)
├── docs/                  # 기술 제안서 · 부록 · API 명세서
├── src/finance_agent/     # Agent Core
├── src/fastapi_backend/   # API 서버 · 배포
├── docker-compose.yml · compose.sh · compose-release.sh
└── THIRD_PARTY_NOTICES.md
```

## Ontology

`ontology/`의 Turtle 5개(`common.ttl`, `bond_kr.ttl`, `etf_kr.ttl`, `etf_gl.ttl`, `fund_pub.ttl`)는
Agent가 질문 해석에 쓰는 field registry에서 생성한다. `common.ttl`이 공통 상위 클래스와 공통 속성을,
상품군별 파일이 하위 클래스와 전용 속성을 정의한다.
