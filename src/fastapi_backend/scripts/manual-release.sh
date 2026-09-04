#!/usr/bin/env bash
# 수동 배포: 깨끗한 Git 체크아웃 → 도커 이미지 → 승인 데이터 볼륨 → 릴리스 매니페스트·바인딩 →
# 키 파일 → evaluation 프로필 컨테이너 실행 → 헬스체크. GitHub 자동 빌드·sigstore 서명 없이
# 같은 봉인 검사(매니페스트 해시)를 통과하는 컨테이너를 서버 한 대에서 만든다.
#
# 사용법 (저장소 루트에서):
#   RAW_DATA_DIR=/path/to/2.금융상품_20260824 SECRETS_DIR=~/.secrets/finance-agent PORT=80 BIND_ADDRESS=0.0.0.0 \
#     src/fastapi_backend/scripts/manual-release.sh all
#   단계별: build | data | release | secrets | run | smoke | stop | status
#
# 필요한 것: docker, git, 인터넷(도커 베이스 이미지·pip), 원본 XLSX 8개(RAW_DATA_DIR),
#   키 파일 3개(SECRETS_DIR/clovastudio-api-key, naver-api-hub-client-id, naver-api-hub-client-secret).
# 주의: adaptive semantic(KURE)은 이 스크립트가 다루지 않는다(꺼짐). 켜려면 FINANCE_ADAPTIVE_* 환경을 별도 추가.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
RELEASE_DIR="${RELEASE_DIR:-$HOME/finance-agent-release}"
RAW_DATA_DIR="${RAW_DATA_DIR:-}"
SECRETS_DIR="${SECRETS_DIR:-$HOME/.secrets/finance-agent}"
BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"
PORT="${PORT:-80}"
CONTAINER_NAME="${CONTAINER_NAME:-finance-agent-release}"
RELEASE_ENV="${RELEASE_ENV:-evaluation}"
RELATION_GRAPH="${RELATION_GRAPH:-$REPO_DIR/src/finance_agent/artifacts/runtime/holdings-graph/holdings-graph-2026-08-21.json}"
TOOLS_IMAGE="${TOOLS_IMAGE:-docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2}"

log() { printf '\n[manual-release] %s\n' "$*"; }
die() { printf '[manual-release] ERROR: %s\n' "$*" >&2; exit 1; }

require_clean_checkout() {
    [ -d "$REPO_DIR/.git" ] || die "REPO_DIR is not a git checkout: $REPO_DIR"
    if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
        die "git checkout is not clean (commit or stash everything first)"
    fi
    COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
    SHORT="${COMMIT:0:12}"
    IMAGE_TAG="${IMAGE_TAG:-gaeng3-backend:${SHORT}}"
    DATA_VOLUME="${DATA_VOLUME:-finance-data-${SHORT}}"
}

step_build() {
    require_clean_checkout
    log "1/6 이미지 빌드 $IMAGE_TAG (commit $COMMIT)"
    docker build --file "$REPO_DIR/src/fastapi_backend/Dockerfile" --tag "$IMAGE_TAG" "$REPO_DIR"
}

step_data() {
    require_clean_checkout
    [ -n "$RAW_DATA_DIR" ] || die "RAW_DATA_DIR(원본 XLSX 8개 폴더)를 지정하세요"
    [ "$(find "$RAW_DATA_DIR" -maxdepth 1 -name '*.xlsx' | wc -l)" -ge 8 ] || die "RAW_DATA_DIR에 XLSX 8개가 없습니다"
    log "2/6 승인 데이터 볼륨 $DATA_VOLUME 생성"
    docker volume create "$DATA_VOLUME" >/dev/null
    docker run --rm --user 0:0 --network none \
        -v "$RAW_DATA_DIR:/raw:ro" \
        -v "$REPO_DIR/src/finance_agent/packages/finance_agent_core/src:/opt/finance-agent-core-src:ro" \
        -v "$DATA_VOLUME:/data" "$IMAGE_TAG" \
        sh -c 'PYTHONPATH=/opt/finance-agent-core-src exec python -m finance_agent_core.storage.prepare --data-dir /raw --output-dir /data --owner-uid 10001 --owner-gid 10001' \
        | tail -n 3
    docker run --rm -v "$DATA_VOLUME:/data:ro" "$IMAGE_TAG" sh -c 'cat /data/relation-retrieval-artifact.sha256' | tr -d '\n' > "$RELEASE_DIR/relation-retrieval-artifact.sha256"
    docker run --rm -v "$DATA_VOLUME:/data:ro" "$IMAGE_TAG" sh -c 'cat /data/relation-retrieval-artifact.json' > "$RELEASE_DIR/relation-retrieval-artifact.json"
    chmod 444 "$RELEASE_DIR/relation-retrieval-artifact.json"
    log "relation artifact sha256 = $(cat "$RELEASE_DIR/relation-retrieval-artifact.sha256")"
}

step_release() {
    require_clean_checkout
    [ -f "$RELEASE_DIR/relation-retrieval-artifact.sha256" ] || die "먼저 data 단계를 실행하세요"
    [ -f "$RELATION_GRAPH" ] || die "구성종목 Graph 파일이 없습니다: $RELATION_GRAPH"
    chmod 444 "$RELATION_GRAPH" 2>/dev/null || true
    IMAGE_ID="$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')"
    rm -rf "$RELEASE_DIR/work" "$RELEASE_DIR/release"
    mkdir -p "$RELEASE_DIR/work" "$RELEASE_DIR/release"
    log "3/6 매니페스트·바인딩·외부지식 아티팩트 생성 (도구 컨테이너, python 3.12)"
    # 저장소를 같은 절대경로로 마운트한다. release_cli는 git 체크아웃이 깨끗한지, HEAD가 --source-commit과 같은지 직접 확인한다.
    docker run --rm --user 0:0 \
        -v "$REPO_DIR:$REPO_DIR:ro" -v "$RELEASE_DIR:$RELEASE_DIR" \
        -e REPO_DIR="$REPO_DIR" -e RELEASE_DIR="$RELEASE_DIR" -e COMMIT="$COMMIT" -e IMAGE_ID="$IMAGE_ID" \
        -e RELEASE_ENV="$RELEASE_ENV" -e RELATION_GRAPH="$RELATION_GRAPH" -e RELEASE_ID="manual-$(date +%Y%m%d-%H%M)-${SHORT}" \
        -w "$REPO_DIR" "$TOOLS_IMAGE" bash -eu -c '
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq >/dev/null && apt-get install -y -qq git >/dev/null
            git config --global --add safe.directory "*"
            # 저장소는 읽기 전용 마운트라 egg-info를 쓸 수 없다. 코어 패키지는 복사본에서 설치한다.
            cp -r src/finance_agent/packages/finance_agent_core /tmp/finance_agent_core-install
            python -m venv /tmp/venv && /tmp/venv/bin/pip install -q --disable-pip-version-check \
                --constraint src/finance_agent/requirements/constraints.txt \
                --requirement src/fastapi_backend/requirements.txt /tmp/finance_agent_core-install
            export PYTHONPATH="$REPO_DIR/src/finance_agent/packages/finance_agent_core/src"
            PY=/tmp/venv/bin/python
            $PY src/fastapi_backend/scripts/build_runtime_core.py --source src/finance_agent/packages/finance_agent_core \
                --output "$RELEASE_DIR/work/runtime-core" --manifest src/fastapi_backend/runtime-core-manifest.json \
                --pyproject src/fastapi_backend/runtime-core-pyproject.toml
            GRAPH_SHA="$(sha256sum "$RELATION_GRAPH" | cut -d" " -f1)"
            $PY -m finance_agent_core.retrieval.external_knowledge_release_cli \
                --mode verified_live_official_with_relation_graph \
                --artifact "$RELEASE_DIR/release/external-knowledge-artifact.json" \
                --temporal-relations "$RELEASE_DIR/release/external-temporal-relations.json" \
                --approved-relation-graph "$RELATION_GRAPH" --approved-relation-graph-sha256 "$GRAPH_SHA"
            EXT_SHA="$(sha256sum "$RELEASE_DIR/release/external-knowledge-artifact.json" | cut -d" " -f1)"
            $PY -m finance_agent_core.release_cli manifest --release-id "$RELEASE_ID" --environment "$RELEASE_ENV" \
                --source-commit "$COMMIT" --git-root "$REPO_DIR" --backend-root "$REPO_DIR/src/fastapi_backend/app" \
                --runtime-core-root "$RELEASE_DIR/work/runtime-core/src/finance_agent_core" --platform linux/amd64 \
                --answer-provider hyperclova --hcx-intent-analysis-enabled --hcx-model HCX-007 \
                --fund-execution-policy public_fund_v1_approved \
                --relation-retrieval-artifact "$RELEASE_DIR/relation-retrieval-artifact.json" \
                --relation-retrieval-artifact-sha256 "$(cat "$RELEASE_DIR/relation-retrieval-artifact.sha256")" \
                --external-knowledge-artifact "$RELEASE_DIR/release/external-knowledge-artifact.json" \
                --external-knowledge-artifact-sha256 "$EXT_SHA" \
                --hcx-timeout-seconds 45 --official-answer-timeout-seconds 270 --official-answer-max-inflight 2 \
                --worker-count 1 --audit-queue-capacity 2048 --audit-shutdown-timeout-seconds 5 --audit-fsync-each-event \
                --output "$RELEASE_DIR/release/agent-release-manifest.json"
            $PY -m finance_agent_core.release_cli binding --manifest "$RELEASE_DIR/release/agent-release-manifest.json" \
                --image-reference "local/gaeng3-backend@${IMAGE_ID}" --platform linux/amd64 \
                --activation-generation 1 --rollback-mode initial_bootstrap \
                --output "$RELEASE_DIR/release/deployment-binding.json"
            chmod 444 "$RELEASE_DIR/release/"* && chmod 755 "$RELEASE_DIR/release" && chown -R "$(stat -c %u:%g "$RELEASE_DIR")" "$RELEASE_DIR/release" "$RELEASE_DIR/work"
        '
    printf '%s\n' "$COMMIT" > "$RELEASE_DIR/release-commit.txt"
    printf '%s\n' "$IMAGE_ID" > "$RELEASE_DIR/release-image-id.txt"
    log "release/ 준비 완료: $(ls "$RELEASE_DIR/release" | tr '\n' ' ')"
}

step_secrets() {
    log "4/6 키 파일 사본 (컨테이너 uid 10001 전용, 값은 출력하지 않음)"
    for name in clovastudio-api-key naver-api-hub-client-id naver-api-hub-client-secret; do
        [ -s "$SECRETS_DIR/$name" ] || die "키 파일이 없습니다: $SECRETS_DIR/$name"
    done
    rm -rf "$RELEASE_DIR/secrets"; mkdir -p "$RELEASE_DIR/secrets" "$RELEASE_DIR/audit"
    cp "$SECRETS_DIR"/clovastudio-api-key "$SECRETS_DIR"/naver-api-hub-client-id "$SECRETS_DIR"/naver-api-hub-client-secret "$RELEASE_DIR/secrets/"
    docker run --rm -v "$RELEASE_DIR/secrets:/s" -v "$RELEASE_DIR/audit:/a" alpine sh -c 'chown -R 10001:10001 /s /a && chmod 700 /s /a && chmod 600 /s/*'
    chmod 711 "$RELEASE_DIR"
}

step_run() {
    [ -f "$RELEASE_DIR/release/deployment-binding.json" ] || die "먼저 release 단계를 실행하세요"
    [ -d "$RELEASE_DIR/secrets" ] || die "먼저 secrets 단계를 실행하세요"
    COMMIT="$(cat "$RELEASE_DIR/release-commit.txt")"; SHORT="${COMMIT:0:12}"
    IMAGE_TAG="${IMAGE_TAG:-gaeng3-backend:${SHORT}}"; DATA_VOLUME="${DATA_VOLUME:-finance-data-${SHORT}}"
    IMAGE_ID="$(cat "$RELEASE_DIR/release-image-id.txt")"
    [ "$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')" = "$IMAGE_ID" ] || die "이미지가 release 단계 때와 다릅니다"
    log "5/6 컨테이너 $CONTAINER_NAME 실행 ($BIND_ADDRESS:$PORT)"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" --init --user 10001:10001 --read-only --restart unless-stopped \
        --tmpfs /tmp:rw,noexec,nosuid,size=256m --security-opt no-new-privileges:true \
        -p "$BIND_ADDRESS:$PORT:8000" \
        -v "$DATA_VOLUME:/data:ro" \
        -v "$RELEASE_DIR/release:/run/finance-release:ro" \
        -v "$RELEASE_DIR/secrets/clovastudio-api-key:/run/secrets/clovastudio_api_key:ro" \
        -v "$RELEASE_DIR/secrets/naver-api-hub-client-id:/run/secrets/naver_api_hub_client_id:ro" \
        -v "$RELEASE_DIR/secrets/naver-api-hub-client-secret:/run/secrets/naver_api_hub_client_secret:ro" \
        -v "$RELEASE_DIR/audit:/audit" \
        -e APP_ENV="$RELEASE_ENV" -e LOG_LEVEL=info -e WEB_CONCURRENCY=1 \
        -e OFFICIAL_ANSWER_TIMEOUT_SECONDS=270 -e OFFICIAL_ANSWER_MAX_INFLIGHT=2 \
        -e FINANCE_BACKEND_FUND_EXECUTION_POLICY=public_fund_v1_approved \
        -e FINANCE_DB_OVERSEAS_ETP=/data/overseas_etp.sqlite3 -e FINANCE_DB_DOMESTIC_ETP=/data/domestic_etp.sqlite3 \
        -e FINANCE_DB_BOND=/data/bond.sqlite3 -e FINANCE_DB_FUND=/data/fund.sqlite3 \
        -e FINANCE_RELATION_RETRIEVAL_ARTIFACT_FILE=/data/relation-retrieval-artifact.json \
        -e FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256="$(cat "$RELEASE_DIR/relation-retrieval-artifact.sha256")" \
        -e FINANCE_RELATION_INDEX_FILE=/data/provided-relations.sqlite3 \
        -e FINANCE_AUDIT_MODE=jsonl -e FINANCE_AUDIT_FILE=/audit/events.jsonl \
        -e FINANCE_AUDIT_QUEUE_CAPACITY=2048 -e FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS=5 -e FINANCE_AUDIT_FSYNC_EACH_EVENT=true \
        -e FINANCE_BACKEND_ANSWER_PROVIDER=hyperclova -e FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=false \
        -e FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED=true -e FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED=false \
        -e FINANCE_ADAPTIVE_SEMANTIC_ENABLED=false \
        -e FINANCE_AGENT_LLM_MODE="$RELEASE_ENV" -e LLM_PROVIDER=hyperclova -e HCX_MODEL=HCX-007 -e HCX_TIMEOUT_SECONDS=45 \
        -e CLOVASTUDIO_API_KEY_FILE=/run/secrets/clovastudio_api_key \
        -e FINANCE_RELEASE_MANIFEST_FILE=/run/finance-release/agent-release-manifest.json \
        -e FINANCE_DEPLOYMENT_BINDING_FILE=/run/finance-release/deployment-binding.json \
        -e FINANCE_DEPLOYMENT_BINDING_SHA256="$(sha256sum "$RELEASE_DIR/release/deployment-binding.json" | cut -d' ' -f1)" \
        -e FINANCE_SOURCE_COMMIT="$COMMIT" \
        -e FINANCE_RUNTIME_IMAGE_REFERENCE="local/gaeng3-backend@${IMAGE_ID}" -e FINANCE_RUNTIME_PLATFORM=linux/amd64 \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ENABLED=true \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE=/run/finance-release/external-knowledge-artifact.json \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256="$(sha256sum "$RELEASE_DIR/release/external-knowledge-artifact.json" | cut -d' ' -f1)" \
        -e FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE=/run/finance-release/external-temporal-relations.json \
        -e FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256="$(sha256sum "$RELEASE_DIR/release/external-temporal-relations.json" | cut -d' ' -f1)" \
        -e NAVER_API_HUB_CLIENT_ID_FILE=/run/secrets/naver_api_hub_client_id \
        -e NAVER_API_HUB_CLIENT_SECRET_FILE=/run/secrets/naver_api_hub_client_secret \
        "$IMAGE_TAG" >/dev/null
    for _ in $(seq 1 30); do
        sleep 3
        if curl -fsS --max-time 5 "http://$BIND_ADDRESS:$PORT/health" >/dev/null 2>&1; then
            log "healthy: $(curl -fsS --max-time 5 "http://$BIND_ADDRESS:$PORT/health")"; return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
            docker logs --tail 20 "$CONTAINER_NAME" 2>&1 | tail -n 20; die "컨테이너가 종료됐습니다"
        fi
    done
    docker logs --tail 20 "$CONTAINER_NAME" 2>&1 | tail -n 20; die "90초 안에 /health 가 응답하지 않습니다"
}

step_smoke() {
    log "6/6 스모크: GET /answer"
    curl -fsS --max-time 300 -G "http://$BIND_ADDRESS:$PORT/answer" \
        --data-urlencode 'question_id=SMOKE-001' \
        --data-urlencode 'question=캠브리콘이 편입된 중국 반도체 ETF를 알려줘' | cut -c1-600; echo
}

step_stop() { docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 && log "stopped $CONTAINER_NAME" || log "not running"; }
step_status() { docker ps --filter "name=$CONTAINER_NAME" --format '{{.Names}} {{.Status}} {{.Ports}}'; curl -fsS --max-time 5 "http://$BIND_ADDRESS:$PORT/health" || true; echo; }

mkdir -p "$RELEASE_DIR"
case "${1:-all}" in
    all) step_build; step_data; step_release; step_secrets; step_run; step_smoke ;;
    build) step_build ;;
    data) step_data ;;
    release) step_release ;;
    secrets) step_secrets ;;
    run) step_run ;;
    smoke) step_smoke ;;
    stop) step_stop ;;
    status) step_status ;;
    *) die "unknown command: $1 (all|build|data|release|secrets|run|smoke|stop|status)" ;;
esac
