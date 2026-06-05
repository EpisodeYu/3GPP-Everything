#!/usr/bin/env bash
# 导出可分发的「索引 bundle」（供 scripts/publish-index-hf.sh 上传到 HuggingFace，
# 终端用户用 scripts/bootstrap-index.sh 一键恢复）。
#
# 锚：docs/04-handoff/2026-06-05-oss-deploy-friendliness-plan.md（阶段二 T2.1）
#     deploy/index/README.md（bundle 格式 + 发布/版权说明）
#
# 在「已建好索引」的机器（maintainer 侧）上跑。产物（约 ~4G）：
#   <OUT>/MANIFEST.txt                          清单：模型/维度/点数/行数/sha256/git rev
#   <OUT>/<collection>.snapshot                 Qdrant collection snapshot
#   <OUT>/bm25.tar.gz                           BM25 jsonl
#   <OUT>/pg_index.sql.gz                        ★只含 chunks_meta + glossary（data-only）★
#
# ★隐私红线：PG 只导出索引表（chunks_meta / glossary），绝不导 users/sessions/messages/
#   checkpoint*/feedbacks 等用户运行时数据。白名单硬编码在 PG_INDEX_TABLES。
#
# 用法：
#   ./scripts/export-index.sh                    # 输出到 ./dist/index-<ts>/
#   OUT_DIR=/mnt/big/bundle ./scripts/export-index.sh
#   QDRANT_CONTAINER=qdrant QDRANT_HTTP=http://127.0.0.1:6333 ./scripts/export-index.sh

set -euo pipefail

# 只导出这两张「索引/内容表」；改这里前务必确认新表不含用户隐私数据。
PG_INDEX_TABLES=(chunks_meta glossary)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
[[ -f "$ENV_FILE" ]] || { echo "缺 .env: $ENV_FILE"; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# Qdrant：host 侧直连（.env 里多为 host.docker.internal，host 上不可解析，故单独给 HTTP 地址）
QDRANT_HTTP="${QDRANT_HTTP:-http://127.0.0.1:6333}"
QDRANT_CONTAINER="${QDRANT_CONTAINER:-qdrant}"
PG_CONTAINER="${PG_CONTAINER:-tgpp-postgres}"
COLLECTION="${QDRANT_COLLECTION_PREFIX:-tgpp_chunks}_${EMBEDDING_PROVIDER:-voyage}_d${EMBEDDING_DIMENSIONS:-1024}"

TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-$PROJECT_ROOT/dist/index-$TS}"
mkdir -p "$OUT_DIR"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log() { echo -e "${BLUE}[export]${RESET} $*"; }
ok()  { echo -e "${GREEN}[export] OK${RESET} $*"; }
err() { echo -e "${RED}[export] ERROR${RESET} $*" >&2; }

log "collection=$COLLECTION  qdrant=$QDRANT_HTTP  out=$OUT_DIR"

# ---- 1. Qdrant snapshot ----
log "创建 Qdrant snapshot..."
SNAP_NAME="$(curl -fsS -X POST "$QDRANT_HTTP/collections/$COLLECTION/snapshots" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["name"])')"
log "snapshot=$SNAP_NAME，docker cp 出容器..."
docker cp "$QDRANT_CONTAINER:/qdrant/snapshots/$COLLECTION/$SNAP_NAME" "$OUT_DIR/$COLLECTION.snapshot"
# 删掉容器内 snapshot，避免 qdrant 卷膨胀
curl -fsS -X DELETE "$QDRANT_HTTP/collections/$COLLECTION/snapshots/$SNAP_NAME" >/dev/null || true
ok "Qdrant: $OUT_DIR/$COLLECTION.snapshot ($(du -h "$OUT_DIR/$COLLECTION.snapshot" | cut -f1))"

# ---- 2. BM25 ----
BM25_DIR="${INGEST_DATA_DIR:-/data/tgpp}/bm25"
[[ -d "$BM25_DIR" ]] || { err "BM25 目录不存在: $BM25_DIR"; exit 1; }
log "tar BM25 jsonl..."
tar -czf "$OUT_DIR/bm25.tar.gz" -C "${INGEST_DATA_DIR:-/data/tgpp}" bm25
ok "BM25: $OUT_DIR/bm25.tar.gz ($(du -h "$OUT_DIR/bm25.tar.gz" | cut -f1))"

# ---- 3. PG 索引表（仅白名单，data-only）----
log "pg_dump 索引表（仅 ${PG_INDEX_TABLES[*]}，data-only）..."
T_ARGS=(); for t in "${PG_INDEX_TABLES[@]}"; do T_ARGS+=(-t "$t"); done
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
  pg_dump -U tgpp_app -d tgpp_everything --data-only --no-owner --no-acl "${T_ARGS[@]}" \
  | gzip > "$OUT_DIR/pg_index.sql.gz"
ok "PG: $OUT_DIR/pg_index.sql.gz ($(du -h "$OUT_DIR/pg_index.sql.gz" | cut -f1))"

# ---- 4. 计数 ----
POINTS="$(curl -fsS "$QDRANT_HTTP/collections/$COLLECTION" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["points_count"])')"
read_count() {  # $1=table
  docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    psql -U tgpp_app -d tgpp_everything -tAc "SELECT count(*) FROM $1"
}
CM_ROWS="$(read_count chunks_meta)"; GL_ROWS="$(read_count glossary)"
QVER="$(curl -fsS "$QDRANT_HTTP/" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version","?"))')"

# ---- 5. MANIFEST + sha256 ----
log "写 MANIFEST + 校验和..."
sha() { sha256sum "$1" | awk '{print $1}'; }
cat > "$OUT_DIR/MANIFEST.txt" <<EOF
created_at=$TS
git_revision=$(cd "$PROJECT_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)
embedding_provider=${EMBEDDING_PROVIDER:-voyage}
embedding_model=${VOYAGE_EMBEDDING_MODEL:-voyage-4-large}
embedding_dimensions=${EMBEDDING_DIMENSIONS:-1024}
qdrant_collection=$COLLECTION
qdrant_version=$QVER
points_count=$POINTS
chunks_meta_rows=$CM_ROWS
glossary_rows=$GL_ROWS
file_snapshot=$COLLECTION.snapshot
file_bm25=bm25.tar.gz
file_pg=pg_index.sql.gz
sha256_snapshot=$(sha "$OUT_DIR/$COLLECTION.snapshot")
sha256_bm25=$(sha "$OUT_DIR/bm25.tar.gz")
sha256_pg=$(sha "$OUT_DIR/pg_index.sql.gz")
EOF

ok "全部完成: $OUT_DIR ($(du -sh "$OUT_DIR" | cut -f1))"
echo "----- MANIFEST -----"; cat "$OUT_DIR/MANIFEST.txt"
echo
echo "下一步：./scripts/publish-index-hf.sh $OUT_DIR   # 上传到 HuggingFace（需你的 HF token + 版权确认）"
