#!/usr/bin/env bash
# 一键拉取并恢复「现成索引」到本地 standalone 部署 —— 免去从零跑 ingestion（省 Voyage 费用 + 数小时）。
#
# 锚：docs/04-handoff/2026-06-05-oss-deploy-friendliness-plan.md（阶段二 T2.2）
#     deploy/index/README.md
#
# 前置：先 `make standalone-up` 起栈（tgpp-qdrant / tgpp-postgres / tgpp-api 在跑）。
#
# 数据源（INDEX_SRC）二选一：
#   - HF datasets repo id（默认）：从 huggingface.co 下载
#   - 本地 bundle 目录：传一个含 MANIFEST.txt 的目录（离线 / 自己 export 出来的）
#
# 用法：
#   ./scripts/bootstrap-index.sh                              # 默认 HF repo
#   INDEX_SRC=EpisodeYu/3gpp-everything-index ./scripts/bootstrap-index.sh
#   INDEX_SRC=./dist/index-20260605-120000   ./scripts/bootstrap-index.sh   # 本地 bundle
#   HF_TOKEN=hf_xxx ./scripts/bootstrap-index.sh             # 私有 repo / 提限流

set -euo pipefail

INDEX_SRC="${INDEX_SRC:-EpisodeYu/3gpp-everything-index}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_ROOT/.env}"
[[ -f "$ENV_FILE" ]] || { echo "缺 .env: $ENV_FILE（先 cp .env.example .env）"; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

QDRANT_HTTP="${QDRANT_HTTP:-http://127.0.0.1:6333}"
PG_CONTAINER="${PG_CONTAINER:-tgpp-postgres}"
API_CONTAINER="${API_CONTAINER:-tgpp-api}"
DATA_DIR="${INGEST_DATA_DIR:-/data/tgpp}"
WORK_DIR="${WORK_DIR:-$PROJECT_ROOT/dist/bootstrap}"

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[bootstrap]${RESET} $*"; }
ok()   { echo -e "${GREEN}[bootstrap] OK${RESET} $*"; }
warn() { echo -e "${YELLOW}[bootstrap] WARN${RESET} $*" >&2; }
err()  { echo -e "${RED}[bootstrap] ERROR${RESET} $*" >&2; }

# 取 bundle 内某文件到 $WORK_DIR：本地目录 → cp；HF → curl resolve URL
IS_LOCAL=0; [[ -d "$INDEX_SRC" ]] && IS_LOCAL=1
fetch() {  # $1=文件名
  local f="$1" dst="$WORK_DIR/$1"
  [[ -f "$dst" ]] && { echo "$dst"; return; }
  if [[ "$IS_LOCAL" == "1" ]]; then
    cp "$INDEX_SRC/$f" "$dst"
  else
    local url="https://huggingface.co/datasets/$INDEX_SRC/resolve/main/$f"
    local auth=(); [[ -n "${HF_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer $HF_TOKEN")
    curl -fSL "${auth[@]}" -o "$dst" "$url"
  fi
  echo "$dst"
}
manifest_get() { grep -E "^$1=" "$WORK_DIR/MANIFEST.txt" | head -1 | cut -d= -f2-; }

mkdir -p "$WORK_DIR"

# ---- 0. preflight ----
SRC_KIND=$([[ "$IS_LOCAL" == "1" ]] && echo "本地目录" || echo "HF datasets")
log "数据源: $INDEX_SRC（$SRC_KIND）"
for c in "$PG_CONTAINER" "$API_CONTAINER"; do
  docker ps --format '{{.Names}}' | grep -qx "$c" || { err "$c 未在运行，请先 make standalone-up"; exit 1; }
done
curl -fsS "$QDRANT_HTTP/" >/dev/null 2>&1 || { err "Qdrant 不可达: $QDRANT_HTTP（先 make standalone-up）"; exit 1; }

# ---- 1. MANIFEST + 兼容性校验 ----
log "拉取 MANIFEST..."; fetch MANIFEST.txt >/dev/null
COLLECTION="$(manifest_get qdrant_collection)"
M_PROVIDER="$(manifest_get embedding_provider)"; M_DIM="$(manifest_get embedding_dimensions)"
M_POINTS="$(manifest_get points_count)"; M_CM="$(manifest_get chunks_meta_rows)"
M_QVER="$(manifest_get qdrant_version)"
log "bundle: provider=$M_PROVIDER dim=$M_DIM collection=$COLLECTION points=$M_POINTS qdrant=$M_QVER"

# 必须与本地 .env 一致——索引是按特定 embedding 模型/维度建的，换 provider/dim 整库作废
if [[ "${EMBEDDING_PROVIDER:-voyage}" != "$M_PROVIDER" || "${EMBEDDING_DIMENSIONS:-1024}" != "$M_DIM" ]]; then
  err "不兼容：bundle 是 ${M_PROVIDER}@d${M_DIM}，但 .env 是 ${EMBEDDING_PROVIDER:-voyage}@d${EMBEDDING_DIMENSIONS:-1024}"
  err "把 .env 的 EMBEDDING_PROVIDER/EMBEDDING_DIMENSIONS 改成与 bundle 一致，或换匹配的 bundle。"
  exit 1
fi
# qdrant 版本：本地需 ≥ 产出版本（旧版读不了新版 snapshot 格式）
L_QVER="$(curl -fsS "$QDRANT_HTTP/" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version","0"))')"
[[ "$(printf '%s\n%s\n' "$M_QVER" "$L_QVER" | sort -V | head -1)" == "$M_QVER" ]] \
  || warn "本地 Qdrant $L_QVER < bundle $M_QVER，snapshot recover 可能失败；建议升级 Qdrant。"

# ---- 2. 下载产物 + sha256 校验 ----
SNAP_FILE="$(manifest_get file_snapshot)"
log "下载索引产物（首次约 ~4G，请耐心）..."
F_SNAP="$(fetch "$SNAP_FILE")"; F_BM25="$(fetch "$(manifest_get file_bm25)")"; F_PG="$(fetch "$(manifest_get file_pg)")"
verify_sha() {  # $1=file $2=expected
  local got; got="$(sha256sum "$1" | awk '{print $1}')"
  [[ "$got" == "$2" ]] || { err "sha256 不匹配: $1"; exit 1; }
}
log "校验 sha256..."
verify_sha "$F_SNAP" "$(manifest_get sha256_snapshot)"
verify_sha "$F_BM25" "$(manifest_get sha256_bm25)"
verify_sha "$F_PG"   "$(manifest_get sha256_pg)"
ok "校验和通过"

# ---- 3. schema（alembic 幂等）----
log "alembic upgrade head（建 schema，幂等）..."
docker exec "$API_CONTAINER" alembic upgrade head

# ---- 4. PG 索引表 ----
log "灌 chunks_meta + glossary（先 TRUNCATE 保证幂等）..."
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
  psql -U tgpp_app -d tgpp_everything -c "TRUNCATE chunks_meta, glossary RESTART IDENTITY CASCADE;"
gunzip -c "$F_PG" | docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
  psql -U tgpp_app -d tgpp_everything -v ON_ERROR_STOP=1
# 修正自增序列（data-only 灌入显式 id 后，序列需对齐到 max(id)）
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" psql -U tgpp_app -d tgpp_everything -c \
  "SELECT setval(pg_get_serial_sequence('chunks_meta','id'), COALESCE((SELECT max(id) FROM chunks_meta),1));" >/dev/null
ok "PG 索引表已恢复"

# ---- 5. Qdrant snapshot（multipart 上传即恢复，自动建/替换 collection）----
log "恢复 Qdrant collection $COLLECTION（上传 snapshot，可能数分钟）..."
curl -fSL -X POST "$QDRANT_HTTP/collections/$COLLECTION/snapshots/upload?priority=snapshot" \
  -H 'Content-Type:multipart/form-data' -F "snapshot=@$F_SNAP" >/dev/null
ok "Qdrant collection 已恢复"

# ---- 6. BM25 ----
log "解压 BM25 → $DATA_DIR/bm25 ..."
rm -rf "$DATA_DIR/bm25"; mkdir -p "$DATA_DIR"
tar -xzf "$F_BM25" -C "$DATA_DIR"
ok "BM25 已解压"

# ---- 7. 校验 + 重启 api（重载 BM25 内存索引）----
P_NOW="$(curl -fsS "$QDRANT_HTTP/collections/$COLLECTION" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["points_count"])')"
CM_NOW="$(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" psql -U tgpp_app -d tgpp_everything -tAc 'SELECT count(*) FROM chunks_meta')"
log "校验：Qdrant points=$P_NOW (期望 $M_POINTS) ; chunks_meta=$CM_NOW (期望 $M_CM)"
[[ "$P_NOW" == "$M_POINTS" && "$CM_NOW" == "$M_CM" ]] || warn "计数与 MANIFEST 不完全一致，请核对上面日志。"

log "重启 $API_CONTAINER（重载 BM25 内存索引）..."
docker restart "$API_CONTAINER" >/dev/null
ok "全部完成。等 api 起来后：curl 127.0.0.1:8002/ready ；然后就能问 3GPP 了。"
echo "提示：下载的 bundle 缓存在 $WORK_DIR，确认无误后可删以省空间。"
