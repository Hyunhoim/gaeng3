#!/usr/bin/env bash
# 수동 배포: 깨끗한 Git 체크아웃 → 도커 이미지 → 승인 데이터 볼륨 → 릴리스 매니페스트·바인딩·외부지식 아티팩트 →
# 키 파일 → evaluation 프로필 컨테이너 실행 → 헬스체크. GitHub 자동 빌드·sigstore 서명 없이
# 같은 봉인 검사(매니페스트 해시)를 통과하는 컨테이너를 서버 한 대에서 만든다.
#
# 사용법 (저장소 루트에서):
#   RAW_DATA_DIR=/path/to/2.금융상품_20260824 SECRETS_DIR=~/.secrets/finance-agent PORT=80 BIND_ADDRESS=0.0.0.0 \
#     src/fastapi_backend/scripts/manual-release.sh all
#   단계별: build | data | release | secrets | run | smoke | stop | status
#
# 외부 근거 모드 (EXTERNAL_MODE):
#   approved_corpus_cache_only (기본, 2026-09-06)  승인 corpus 캐시만 사용. NAVER 키 불필요(있어도 마운트하지 않음).
#       필요한 입력: APPROVED_CORPUS_MANIFEST, APPROVED_CORPUS_ROOT, APPROVED_DOCUMENT_DENSE,
#                    DOCUMENT_CACHE_ACTIVATION, APPROVED_TEMPORAL_RELATIONS  (+ 아래 adaptive 입력, 자동 ON)
#   verified_live_official_with_relation_graph     실시간 공식 검색 + 구성종목 Graph. NAVER 키 2개 필요.
#
# adaptive semantic (KURE Schema Dense, ADAPTIVE=true|false, cache 모드는 강제 true):
#   KURE_CACHE_DIR(models--nlpai-lab--KURE-v1 를 포함한 HF 캐시 루트), KURE_SNAPSHOT_MANIFEST, SCHEMA_DENSE_INDEX,
#   SCHEMA_DENSE_CALIBRATION_REPORT_SHA256 및 임계값. 기본값은 저장소의 v8(registry 1.8) 산출물과 .env.release.example.
#
# 필요한 것: docker, git, 인터넷(도커 베이스 이미지·pip), 원본 XLSX 8개(RAW_DATA_DIR),
#   키 파일(SECRETS_DIR/clovastudio-api-key, live 모드에서는 naver-api-hub-client-id/-secret 추가).
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
RELEASE_DIR="${RELEASE_DIR:-$HOME/finance-agent-release}"
RAW_DATA_DIR="${RAW_DATA_DIR:-}"
SECRETS_DIR="${SECRETS_DIR:-$HOME/.secrets/finance-agent}"
BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"
PORT="${PORT:-80}"
CONTAINER_NAME="${CONTAINER_NAME:-finance-agent-release}"
RELEASE_ENV="${RELEASE_ENV:-evaluation}"
EXTERNAL_MODE="${EXTERNAL_MODE:-approved_corpus_cache_only}"
RELATION_GRAPH="${RELATION_GRAPH:-$REPO_DIR/src/finance_agent/artifacts/runtime/holdings-graph/holdings-graph-2026-08-21.json}"
TOOLS_IMAGE="${TOOLS_IMAGE:-docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2}"

# 승인 corpus 캐시 입력 (cache 모드)
APPROVED_CORPUS_MANIFEST="${APPROVED_CORPUS_MANIFEST:-}"
APPROVED_CORPUS_ROOT="${APPROVED_CORPUS_ROOT:-}"
APPROVED_DOCUMENT_DENSE="${APPROVED_DOCUMENT_DENSE:-}"
DOCUMENT_CACHE_ACTIVATION="${DOCUMENT_CACHE_ACTIVATION:-}"
APPROVED_TEMPORAL_RELATIONS="${APPROVED_TEMPORAL_RELATIONS:-}"

# adaptive semantic 입력
ADAPTIVE="${ADAPTIVE:-true}"
KURE_CACHE_DIR="${KURE_CACHE_DIR:-}"
KURE_SNAPSHOT_MANIFEST="${KURE_SNAPSHOT_MANIFEST:-$REPO_DIR/src/finance_agent/artifacts/evaluation/schema-embedding/snapshot-manifests/kure-v1-d14c8a9423946e268a0c9952fecf3a7aabd73bd9-snapshot-v2.json}"
KURE_SNAPSHOT_ID="d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
SCHEMA_DENSE_INDEX="${SCHEMA_DENSE_INDEX:-$REPO_DIR/src/finance_agent/artifacts/runtime/schema-dense/kure-schema-dense-production-v9-registry18-calibrated-20260906.json}"
SCHEMA_DENSE_CALIBRATION_REPORT_SHA256="${SCHEMA_DENSE_CALIBRATION_REPORT_SHA256:-8c41295bf0746bf395b0898863b406f18f6cbd8a42a4cc379069e88ab85c3a09}"
SCHEMA_DENSE_MIN_SCORE="${SCHEMA_DENSE_MIN_SCORE:-0.340015}"
SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE="${SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE:-0.3}"
SCHEMA_DENSE_MINIMUM_MARGIN="${SCHEMA_DENSE_MINIMUM_MARGIN:-0.034684}"
SCHEMA_DENSE_TOP_K="${SCHEMA_DENSE_TOP_K:-10}"
KURE_CPU_THREADS="${KURE_CPU_THREADS:-2}"
KURE_BATCH_SIZE="${KURE_BATCH_SIZE:-16}"

log() { printf '\n[manual-release] %s\n' "$*"; }
die() { printf '[manual-release] ERROR: %s\n' "$*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

case "$EXTERNAL_MODE" in
    approved_corpus_cache_only) ADAPTIVE=true ;;
    verified_live_official_with_relation_graph) ;;
    *) die "EXTERNAL_MODE 는 approved_corpus_cache_only | verified_live_official_with_relation_graph 중 하나" ;;
esac
if [ "$ADAPTIVE" = true ]; then IMAGE_TARGET=adaptive-runtime; else IMAGE_TARGET=final; fi

require_clean_checkout() {
    [ -d "$REPO_DIR/.git" ] || die "REPO_DIR is not a git checkout: $REPO_DIR"
    if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
        die "git checkout is not clean (commit or stash everything first)"
    fi
    COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
    SHORT="${COMMIT:0:12}"
    IMAGE_TAG="${IMAGE_TAG:-gaeng3-backend:${SHORT}-${IMAGE_TARGET}}"
    DATA_VOLUME="${DATA_VOLUME:-finance-data-${SHORT}}"
}

require_cache_inputs() {
    for v in APPROVED_CORPUS_MANIFEST APPROVED_DOCUMENT_DENSE DOCUMENT_CACHE_ACTIVATION APPROVED_TEMPORAL_RELATIONS; do
        [ -n "${!v}" ] && [ -f "${!v}" ] || die "cache 모드 입력 파일이 없습니다: $v=${!v:-<비어 있음>}"
    done
    [ -n "$APPROVED_CORPUS_ROOT" ] && [ -d "$APPROVED_CORPUS_ROOT" ] || die "APPROVED_CORPUS_ROOT 폴더가 없습니다: ${APPROVED_CORPUS_ROOT:-<비어 있음>}"
}

require_adaptive_inputs() {
    [ -n "$KURE_CACHE_DIR" ] && [ -d "$KURE_CACHE_DIR/models--nlpai-lab--KURE-v1/snapshots/$KURE_SNAPSHOT_ID" ] \
        || die "KURE_CACHE_DIR 아래에 models--nlpai-lab--KURE-v1/snapshots/$KURE_SNAPSHOT_ID 가 없습니다: ${KURE_CACHE_DIR:-<비어 있음>}"
    [ -f "$KURE_SNAPSHOT_MANIFEST" ] || die "KURE 스냅샷 매니페스트가 없습니다: $KURE_SNAPSHOT_MANIFEST"
    [ -f "$SCHEMA_DENSE_INDEX" ] || die "스키마 Dense 색인이 없습니다: $SCHEMA_DENSE_INDEX"
}

step_build() {
    require_clean_checkout
    log "1/6 이미지 빌드 $IMAGE_TAG (commit $COMMIT, target $IMAGE_TARGET)"
    docker build --file "$REPO_DIR/src/fastapi_backend/Dockerfile" --target "$IMAGE_TARGET" --tag "$IMAGE_TAG" "$REPO_DIR"
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
    rm -f "$RELEASE_DIR/relation-retrieval-artifact.sha256" "$RELEASE_DIR/relation-retrieval-artifact.json"
    docker run --rm -v "$DATA_VOLUME:/data:ro" "$IMAGE_TAG" sh -c 'cat /data/relation-retrieval-artifact.sha256' | tr -d '\n' > "$RELEASE_DIR/relation-retrieval-artifact.sha256"
    docker run --rm -v "$DATA_VOLUME:/data:ro" "$IMAGE_TAG" sh -c 'cat /data/relation-retrieval-artifact.json' > "$RELEASE_DIR/relation-retrieval-artifact.json"
    chmod 444 "$RELEASE_DIR/relation-retrieval-artifact.json"
    log "relation artifact sha256 = $(cat "$RELEASE_DIR/relation-retrieval-artifact.sha256")"
}

step_release() {
    require_clean_checkout
    [ -f "$RELEASE_DIR/relation-retrieval-artifact.sha256" ] || die "먼저 data 단계를 실행하세요"
    if [ "$EXTERNAL_MODE" = approved_corpus_cache_only ]; then require_cache_inputs; else
        [ -f "$RELATION_GRAPH" ] || die "구성종목 Graph 파일이 없습니다: $RELATION_GRAPH"; chmod 444 "$RELATION_GRAPH" 2>/dev/null || true; fi
    if [ "$ADAPTIVE" = true ]; then require_adaptive_inputs; fi
    IMAGE_ID="$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')"
    # 이전 실행이 root 소유(0444)로 남긴 산출물도 지울 수 있게 컨테이너 안에서 정리한다
    docker run --rm -v "$RELEASE_DIR:$RELEASE_DIR" alpine sh -c "rm -rf '$RELEASE_DIR/work' '$RELEASE_DIR/release'"
    mkdir -p "$RELEASE_DIR/work" "$RELEASE_DIR/release"
    log "3/6 매니페스트·바인딩·외부지식 아티팩트 생성 (도구 컨테이너, python 3.12, 모드 $EXTERNAL_MODE, adaptive $ADAPTIVE)"
    # 입력 파일은 모두 같은 절대경로로 읽기 전용 마운트한다.
    EXTRA_MOUNTS=()
    for p in "$APPROVED_CORPUS_MANIFEST" "$APPROVED_CORPUS_ROOT" "$APPROVED_DOCUMENT_DENSE" "$DOCUMENT_CACHE_ACTIVATION" "$APPROVED_TEMPORAL_RELATIONS"; do
        [ -n "$p" ] && EXTRA_MOUNTS+=(-v "$p:$p:ro")
    done
    docker run --rm --user 0:0 \
        -v "$REPO_DIR:$REPO_DIR:ro" -v "$RELEASE_DIR:$RELEASE_DIR" "${EXTRA_MOUNTS[@]}" \
        -e REPO_DIR="$REPO_DIR" -e RELEASE_DIR="$RELEASE_DIR" -e COMMIT="$COMMIT" -e IMAGE_ID="$IMAGE_ID" \
        -e RELEASE_ENV="$RELEASE_ENV" -e RELATION_GRAPH="$RELATION_GRAPH" -e RELEASE_ID="manual-$(date +%Y%m%d-%H%M)-${SHORT}" \
        -e EXTERNAL_MODE="$EXTERNAL_MODE" -e ADAPTIVE="$ADAPTIVE" \
        -e APPROVED_CORPUS_MANIFEST="$APPROVED_CORPUS_MANIFEST" -e APPROVED_CORPUS_ROOT="$APPROVED_CORPUS_ROOT" \
        -e APPROVED_DOCUMENT_DENSE="$APPROVED_DOCUMENT_DENSE" -e DOCUMENT_CACHE_ACTIVATION="$DOCUMENT_CACHE_ACTIVATION" \
        -e APPROVED_TEMPORAL_RELATIONS="$APPROVED_TEMPORAL_RELATIONS" \
        -e SCHEMA_DENSE_INDEX="$SCHEMA_DENSE_INDEX" -e KURE_SNAPSHOT_MANIFEST="$KURE_SNAPSHOT_MANIFEST" -e SCHEMA_DENSE_CALIBRATION_REPORT_SHA256="$SCHEMA_DENSE_CALIBRATION_REPORT_SHA256" \
        -e SCHEMA_DENSE_MIN_SCORE="$SCHEMA_DENSE_MIN_SCORE" -e SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE="$SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE" \
        -e SCHEMA_DENSE_MINIMUM_MARGIN="$SCHEMA_DENSE_MINIMUM_MARGIN" -e SCHEMA_DENSE_TOP_K="$SCHEMA_DENSE_TOP_K" \
        -w "$REPO_DIR" "$TOOLS_IMAGE" bash -eu -c '
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq >/dev/null && apt-get install -y -qq git >/dev/null
            git config --global --add safe.directory "*"
            cp -r src/finance_agent/packages/finance_agent_core /tmp/finance_agent_core-install
            python -m venv /tmp/venv && /tmp/venv/bin/pip install -q --disable-pip-version-check \
                --constraint src/finance_agent/requirements/constraints.txt \
                --requirement src/fastapi_backend/requirements.txt /tmp/finance_agent_core-install
            export PYTHONPATH="$REPO_DIR/src/finance_agent/packages/finance_agent_core/src"
            PY=/tmp/venv/bin/python
            sha() { sha256sum "$1" | cut -d" " -f1; }
            $PY src/fastapi_backend/scripts/build_runtime_core.py --source src/finance_agent/packages/finance_agent_core \
                --output "$RELEASE_DIR/work/runtime-core" --manifest src/fastapi_backend/runtime-core-manifest.json \
                --pyproject src/fastapi_backend/runtime-core-pyproject.toml
            if [ "$EXTERNAL_MODE" = approved_corpus_cache_only ]; then
                $PY -m finance_agent_core.retrieval.external_knowledge_release_cli \
                    --mode approved_corpus_cache_only \
                    --artifact "$RELEASE_DIR/release/external-knowledge-artifact.json" \
                    --temporal-relations "$RELEASE_DIR/release/external-temporal-relations.json" \
                    --approved-corpus-manifest "$APPROVED_CORPUS_MANIFEST" --approved-corpus-root "$APPROVED_CORPUS_ROOT" \
                    --approved-corpus-manifest-sha256 "$(sha "$APPROVED_CORPUS_MANIFEST")" \
                    --approved-document-dense "$APPROVED_DOCUMENT_DENSE" --approved-document-dense-sha256 "$(sha "$APPROVED_DOCUMENT_DENSE")" \
                    --document-dense-output "$RELEASE_DIR/release/document-dense-approved.json" \
                    --document-cache-activation "$DOCUMENT_CACHE_ACTIVATION" --document-cache-activation-sha256 "$(sha "$DOCUMENT_CACHE_ACTIVATION")" \
                    --document-cache-activation-output "$RELEASE_DIR/release/document-cache-activation.json" \
                    --approved-temporal-relations "$APPROVED_TEMPORAL_RELATIONS" --approved-temporal-relations-sha256 "$(sha "$APPROVED_TEMPORAL_RELATIONS")"
                # 승인 corpus를 매니페스트의 relative_path 그대로 release/approved-corpus 에 복사 (크기·SHA 검증)
                $PY - <<PYEOF
import hashlib, json, os, shutil
from pathlib import Path
manifest = Path(os.environ["APPROVED_CORPUS_MANIFEST"]); root = Path(os.environ["APPROVED_CORPUS_ROOT"])
out_root = Path(os.environ["RELEASE_DIR"]) / "release" / "approved-corpus"; out_root.mkdir(mode=0o755)
shutil.copyfile(manifest, Path(os.environ["RELEASE_DIR"]) / "release" / "approved-corpus-manifest.json")
docs = json.loads(manifest.read_bytes())["documents"]; n = 0
for d in docs:
    rel = d["relative_path"]; src = root / rel; data = src.read_bytes()
    if len(data) != d["content_size_bytes"]: raise SystemExit(f"size mismatch: {rel}")
    expected = d.get("content_sha256") or d.get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected: raise SystemExit(f"sha mismatch: {rel}")
    dst = out_root / rel; dst.parent.mkdir(parents=True, exist_ok=True, mode=0o755); dst.write_bytes(data); dst.chmod(0o444); n += 1
print(f"approved corpus copied: {n} documents")
PYEOF
            else
                GRAPH_SHA="$(sha "$RELATION_GRAPH")"
                $PY -m finance_agent_core.retrieval.external_knowledge_release_cli \
                    --mode verified_live_official_with_relation_graph \
                    --artifact "$RELEASE_DIR/release/external-knowledge-artifact.json" \
                    --temporal-relations "$RELEASE_DIR/release/external-temporal-relations.json" \
                    --approved-relation-graph "$RELATION_GRAPH" --approved-relation-graph-sha256 "$GRAPH_SHA"
            fi
            EXT_SHA="$(sha "$RELEASE_DIR/release/external-knowledge-artifact.json")"
            ADAPTIVE_ARGS=()
            if [ "$ADAPTIVE" = true ]; then
                # 봉인 검사는 쓰기 비트가 전혀 없는(0444) 일반 파일만 받는다 → 릴리스 폴더에 읽기 전용 사본
                install -m 0444 "$SCHEMA_DENSE_INDEX" "$RELEASE_DIR/release/schema-dense-index.json"
                install -m 0444 "$KURE_SNAPSHOT_MANIFEST" "$RELEASE_DIR/release/kure-snapshot-manifest.json"
                ADAPTIVE_ARGS=(--hcx-semantic-resolver-enabled --adaptive-semantic-enabled \
                    --schema-dense-index "$RELEASE_DIR/release/schema-dense-index.json" --schema-dense-index-sha256 "$(sha "$SCHEMA_DENSE_INDEX")" \
                    --schema-dense-calibration-report-sha256 "$SCHEMA_DENSE_CALIBRATION_REPORT_SHA256" \
                    --schema-dense-min-score "$SCHEMA_DENSE_MIN_SCORE" --schema-dense-hclx-candidate-min-score "$SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE" \
                    --schema-dense-minimum-margin "$SCHEMA_DENSE_MINIMUM_MARGIN" --schema-dense-top-k "$SCHEMA_DENSE_TOP_K")
            fi
            $PY -m finance_agent_core.release_cli manifest --release-id "$RELEASE_ID" --environment "$RELEASE_ENV" \
                --source-commit "$COMMIT" --git-root "$REPO_DIR" --backend-root "$REPO_DIR/src/fastapi_backend/app" \
                --runtime-core-root "$RELEASE_DIR/work/runtime-core/src/finance_agent_core" --platform linux/amd64 \
                --answer-provider hyperclova --hcx-intent-analysis-enabled --hcx-model HCX-007 "${ADAPTIVE_ARGS[@]}" \
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
            find "$RELEASE_DIR/release" -type f -exec chmod 444 {} + ; find "$RELEASE_DIR/release" -type d -exec chmod 755 {} +
            chown -R "$(stat -c %u:%g "$RELEASE_DIR")" "$RELEASE_DIR/release" "$RELEASE_DIR/work"
        '
    printf '%s\n' "$COMMIT" > "$RELEASE_DIR/release-commit.txt"
    printf '%s\n' "$IMAGE_ID" > "$RELEASE_DIR/release-image-id.txt"
    printf '%s\n' "$EXTERNAL_MODE" > "$RELEASE_DIR/release-external-mode.txt"
    printf '%s\n' "$ADAPTIVE" > "$RELEASE_DIR/release-adaptive.txt"
    log "release/ 준비 완료: $(ls "$RELEASE_DIR/release" | tr '\n' ' ')"
}

step_secrets() {
    log "4/6 키 파일 사본 (컨테이너 uid 10001 전용, 값은 출력하지 않음)"
    NAMES=(clovastudio-api-key)
    [ "$EXTERNAL_MODE" = verified_live_official_with_relation_graph ] && NAMES+=(naver-api-hub-client-id naver-api-hub-client-secret)
    for name in "${NAMES[@]}"; do [ -s "$SECRETS_DIR/$name" ] || die "키 파일이 없습니다: $SECRETS_DIR/$name"; done
    docker run --rm -v "$RELEASE_DIR:$RELEASE_DIR" alpine sh -c "rm -rf '$RELEASE_DIR/secrets'"   # 이전 실행의 uid 10001 소유 폴더 정리
    mkdir -p "$RELEASE_DIR/secrets" "$RELEASE_DIR/audit"
    for name in "${NAMES[@]}"; do cp "$SECRETS_DIR/$name" "$RELEASE_DIR/secrets/"; done
    docker run --rm -v "$RELEASE_DIR/secrets:/s" -v "$RELEASE_DIR/audit:/a" alpine sh -c 'chown -R 10001:10001 /s /a && chmod 700 /s /a && chmod 600 /s/*'
    chmod 711 "$RELEASE_DIR"
}

step_run() {
    [ -f "$RELEASE_DIR/release/deployment-binding.json" ] || die "먼저 release 단계를 실행하세요"
    [ -d "$RELEASE_DIR/secrets" ] || die "먼저 secrets 단계를 실행하세요"
    COMMIT="$(cat "$RELEASE_DIR/release-commit.txt")"; SHORT="${COMMIT:0:12}"
    EXTERNAL_MODE="$(cat "$RELEASE_DIR/release-external-mode.txt")"; ADAPTIVE="$(cat "$RELEASE_DIR/release-adaptive.txt")"
    if [ "$ADAPTIVE" = true ]; then IMAGE_TARGET=adaptive-runtime; else IMAGE_TARGET=final; fi
    IMAGE_TAG="${IMAGE_TAG:-gaeng3-backend:${SHORT}-${IMAGE_TARGET}}"; DATA_VOLUME="${DATA_VOLUME:-finance-data-${SHORT}}"
    IMAGE_ID="$(cat "$RELEASE_DIR/release-image-id.txt")"
    [ "$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')" = "$IMAGE_ID" ] || die "이미지가 release 단계 때와 다릅니다"
    log "5/6 컨테이너 $CONTAINER_NAME 실행 ($BIND_ADDRESS:$PORT, 모드 $EXTERNAL_MODE, adaptive $ADAPTIVE)"
    EXT_ARGS=()
    if [ "$EXTERNAL_MODE" = approved_corpus_cache_only ]; then
        EXT_ARGS+=(-v "$RELEASE_DIR/release/document-dense-approved.json:/models/document-dense-approved.json:ro" \
            -v "$RELEASE_DIR/release/approved-corpus:/run/finance-corpus:ro" \
            -e FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE=/models/document-dense-approved.json \
            -e FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE=/run/finance-release/document-cache-activation.json \
            -e FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE=/run/finance-release/approved-corpus-manifest.json \
            -e FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT=/run/finance-corpus)
    else
        EXT_ARGS+=(-v "$RELEASE_DIR/secrets/naver-api-hub-client-id:/run/secrets/naver_api_hub_client_id:ro" \
            -v "$RELEASE_DIR/secrets/naver-api-hub-client-secret:/run/secrets/naver_api_hub_client_secret:ro" \
            -e NAVER_API_HUB_CLIENT_ID_FILE=/run/secrets/naver_api_hub_client_id \
            -e NAVER_API_HUB_CLIENT_SECRET_FILE=/run/secrets/naver_api_hub_client_secret)
    fi
    ADAPTIVE_RUN=()
    if [ "$ADAPTIVE" = true ]; then
        require_adaptive_inputs
        ADAPTIVE_RUN+=(-v "$RELEASE_DIR/release/schema-dense-index.json:/models/schema-dense-index.json:ro" \
            -v "$KURE_CACHE_DIR:/models/kure-cache:ro" -v "$RELEASE_DIR/release/kure-snapshot-manifest.json:/models/kure-snapshot-manifest.json:ro" \
            -e FINANCE_ADAPTIVE_SEMANTIC_ENABLED=true -e FINANCE_DENSE_SCHEMA_LINKER_ENABLED=true -e FINANCE_PRODUCT_DENSE_ENABLED=false \
            -e FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED=true \
            -e FINANCE_SCHEMA_DENSE_INDEX_FILE=/models/schema-dense-index.json -e FINANCE_SCHEMA_DENSE_INDEX_SHA256="$(sha "$RELEASE_DIR/release/schema-dense-index.json")" \
            -e FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256="$SCHEMA_DENSE_CALIBRATION_REPORT_SHA256" \
            -e FINANCE_SCHEMA_DENSE_MIN_SCORE="$SCHEMA_DENSE_MIN_SCORE" -e FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE="$SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE" \
            -e FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN="$SCHEMA_DENSE_MINIMUM_MARGIN" -e FINANCE_SCHEMA_DENSE_TOP_K="$SCHEMA_DENSE_TOP_K" \
            -e FINANCE_KURE_TRUSTED_CACHE_ROOT=/models/kure-cache \
            -e FINANCE_KURE_SNAPSHOT_DIR="/models/kure-cache/models--nlpai-lab--KURE-v1/snapshots/$KURE_SNAPSHOT_ID" \
            -e FINANCE_KURE_SNAPSHOT_MANIFEST_FILE=/models/kure-snapshot-manifest.json \
            -e FINANCE_KURE_CPU_THREADS="$KURE_CPU_THREADS" -e FINANCE_KURE_BATCH_SIZE="$KURE_BATCH_SIZE")
    else
        ADAPTIVE_RUN+=(-e FINANCE_BACKEND_HCX_SEMANTIC_RESOLVER_ENABLED=false -e FINANCE_ADAPTIVE_SEMANTIC_ENABLED=false)
    fi
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" --init --user 10001:10001 --read-only --restart unless-stopped \
        --tmpfs /tmp:rw,noexec,nosuid,size=256m --security-opt no-new-privileges:true \
        -p "$BIND_ADDRESS:$PORT:8000" \
        -v "$DATA_VOLUME:/data:ro" \
        -v "$RELEASE_DIR/release:/run/finance-release:ro" \
        -v "$RELEASE_DIR/secrets/clovastudio-api-key:/run/secrets/clovastudio_api_key:ro" \
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
        -e FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED=true \
        -e FINANCE_AGENT_LLM_MODE="$RELEASE_ENV" -e LLM_PROVIDER=hyperclova -e HCX_MODEL=HCX-007 -e HCX_TIMEOUT_SECONDS=45 \
        -e CLOVASTUDIO_API_KEY_FILE=/run/secrets/clovastudio_api_key \
        -e FINANCE_RELEASE_MANIFEST_FILE=/run/finance-release/agent-release-manifest.json \
        -e FINANCE_DEPLOYMENT_BINDING_FILE=/run/finance-release/deployment-binding.json \
        -e FINANCE_DEPLOYMENT_BINDING_SHA256="$(sha "$RELEASE_DIR/release/deployment-binding.json")" \
        -e FINANCE_SOURCE_COMMIT="$COMMIT" \
        -e FINANCE_RUNTIME_IMAGE_REFERENCE="local/gaeng3-backend@${IMAGE_ID}" -e FINANCE_RUNTIME_PLATFORM=linux/amd64 \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ENABLED=true \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE=/run/finance-release/external-knowledge-artifact.json \
        -e FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256="$(sha "$RELEASE_DIR/release/external-knowledge-artifact.json")" \
        -e FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE=/run/finance-release/external-temporal-relations.json \
        -e FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256="$(sha "$RELEASE_DIR/release/external-temporal-relations.json")" \
        "${EXT_ARGS[@]}" "${ADAPTIVE_RUN[@]}" \
        "$IMAGE_TAG" >/dev/null
    for _ in $(seq 1 60); do
        sleep 3
        if curl -fsS --max-time 5 "http://$BIND_ADDRESS:$PORT/health" >/dev/null 2>&1; then
            log "healthy: $(curl -fsS --max-time 5 "http://$BIND_ADDRESS:$PORT/health")"; return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
            docker logs --tail 30 "$CONTAINER_NAME" 2>&1 | tail -n 30; die "컨테이너가 종료됐습니다"
        fi
    done
    docker logs --tail 30 "$CONTAINER_NAME" 2>&1 | tail -n 30; die "180초 안에 /health 가 응답하지 않습니다"
}

step_smoke() {
    log "6/6 스모크: GET /answer"
    curl -fsS --max-time 300 -G "http://$BIND_ADDRESS:$PORT/answer" \
        --data-urlencode 'question_id=SMOKE-001' \
        --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘' | cut -c1-600; echo
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
