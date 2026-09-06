# 수동 배포 절차 (NCP 서버, 2026-09-04)

GitHub 자동 빌드·sigstore 서명 없이 서버 한 대에서 배포한다. 봉인 검사(릴리스 매니페스트 해시)는
그대로 통과하므로 컨테이너의 동작은 자동 빌드 결과와 같다. 스크립트:
`src/fastapi_backend/scripts/manual-release.sh`. 2026-09-04 5090 서버에서 처음부터 끝까지 검증했다.

## 서버에 있어야 하는 것

| 항목 | 설명 |
| --- | --- |
| docker, git, curl | 인터넷 연결(도커 베이스 이미지·pip 다운로드) |
| 저장소 체크아웃 | `git clone` 후 배포할 커밋으로 `git checkout`. **작업 트리가 깨끗해야** 한다(수정 파일·미추적 파일 없음) |
| 원본 XLSX 8개 | `2. Data/1. Raw/2.금융상품_20260824/` 폴더를 서버로 복사 (예: `/opt/finance-agent-data/raw`) |
| 키 파일 3개 | `~/.secrets/finance-agent/{clovastudio-api-key, naver-api-hub-client-id, naver-api-hub-client-secret}` — 값만 들어 있는 파일, 소유자 전용(0600). 환경변수로 값을 넣지 않는다 |
| 포트 80 | 주최측 평가는 인증 없는 HTTP `GET /answer`. NCP ACG Inbound TCP 80을 주최측 IP(공지 전에는 점검 IP)에 허용 |

## 실행

```bash
cd /opt/finance-agent            # 저장소 루트
git status --porcelain           # 아무것도 출력되지 않아야 함
RAW_DATA_DIR=/opt/finance-agent-data/raw \
SECRETS_DIR=$HOME/.secrets/finance-agent \
BIND_ADDRESS=0.0.0.0 PORT=80 CONTAINER_NAME=finance-agent-release \
  src/fastapi_backend/scripts/manual-release.sh all
```

단계별로도 된다: `build` → `data` → `release` → `secrets` → `run` → `smoke`. 중간에 실패하면 그 단계만 다시 실행.
`status`로 상태, `stop`으로 중지. 결과물은 `~/finance-agent-release/`에 남는다(매니페스트·바인딩·외부지식 아티팩트·키 사본·감사 로그).

소요 시간(5090 기준): 이미지 빌드 5~10분, 데이터 볼륨 2분, 매니페스트 도구 컨테이너 3분, 기동 30초.

## 확인

```bash
curl -fsS http://127.0.0.1:80/health
python3 "1. Project Notes/scripts/run_blind_set.py" --base-url http://127.0.0.1:80 \
  --questions official8.questions.json --output official8.responses.json
```

기대값(09-04): E1·E2·E3·E4·E5 success, U1·U3 not_found, U2 되묻기. 문항당 5~40초.
외부에서는 `curl -G http://<공인IP>/answer --data-urlencode question_id=T1 --data-urlencode question=...`.

## 알아 둘 것

- **adaptive semantic(KURE 스키마 링킹)은 꺼진 상태로 배포된다.** 켜려면 KURE 모델 캐시·스키마 인덱스 v6·보정 리포트가 서버에 있어야 하고 `FINANCE_ADAPTIVE_*`·`FINANCE_KURE_*`·`FINANCE_SCHEMA_DENSE_*` 환경과 매니페스트 옵션(`--adaptive-semantic-enabled …`)을 같이 맞춰야 한다. 이 스크립트 범위 밖.
- 배포 후 코드가 바뀌면 `all`을 다시 실행한다(새 커밋 = 새 이미지·새 매니페스트). **마감(09-06 23:59) 후에는 어떤 변경도 하지 않는다.**
- 삼성 KODEX 사이트(`www.samsungfund.com`)는 짧은 시간에 많이 부르면 Cloudflare가 1시간 이상 막는다(429). 배포 서버에서 KODEX 문항을 반복 테스트할 때 주의. 5090은 09-04 오후 그 상태여서 E5의 마지막 단계(투자설명서 PDF)만 실패했다.
- 응답이 없거나 느리면: `docker logs --tail 50 finance-agent-release`, `~/finance-agent-release/audit/events.jsonl`(uid 10001 소유).
- 롤백: 이전 커밋으로 체크아웃 후 `all` 재실행(이미지·볼륨은 커밋별로 이름이 달라 공존한다).
