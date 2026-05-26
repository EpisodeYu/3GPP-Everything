#!/usr/bin/env bash
# M8 — 本项目数据备份脚本（不动其他项目的共享数据库）。
#
# 锚：docs/03-development/07-cicd-and-deployment.md §6.2
#
# 备份项（按 CLAUDE.md §3 surgical：只备份本项目数据）：
#   1. PostgreSQL：tgpp_everything 数据库 pg_dump
#   2. Qdrant：tgpp_chunks_voyage_d1024 collection snapshot
#   3. INGEST_DATA_DIR/bm25：BM25 jsonl
#   4. .env：生产配置
#
# **不再备份**：Let's Encrypt 证书 —— 证书在 ~/infra/ingress/ 项目里，由那个项目独立备份。
#
# **不备份**：
#   - chunks_meta 重新索引可生成；Qdrant snapshot 已含
#   - LangGraph checkpoints（瞬时数据）—— 但本备份会通过 pg_dump 一并捞起
#   - Redis 缓存（瞬时）
#
# 用法：
#   ./deploy/scripts/backup.sh                  # 输出到 ./backups/<timestamp>/
#   BACKUP_DIR=/mnt/external/backups ./deploy/scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/backups}/$TS"
mkdir -p "$BACKUP_DIR"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[backup]${RESET} $*"; }
ok()   { echo -e "${GREEN}[backup] OK${RESET} $*"; }
err()  { echo -e "${RED}[backup] ERROR${RESET} $*" >&2; }

# ----- 1. PG dump -----
log "pg_dump tgpp_everything..."
# DATABASE_URL 格式：postgresql+asyncpg://user:pass@host:port/db
# 转成 pg_dump 可读的：postgresql://user:pass@host:port/db
PG_URL="$(echo "$DATABASE_URL" | sed -E 's|postgresql\+asyncpg|postgresql|')"
# host.docker.internal 在 host 上跑 pg_dump 时不存在，换 127.0.0.1
PG_URL="${PG_URL//host.docker.internal/127.0.0.1}"
docker exec dangdang-postgres pg_dump "$PG_URL" --no-owner --no-acl --clean --if-exists \
    > "$BACKUP_DIR/tgpp_everything.sql"
ok "PG dump: $BACKUP_DIR/tgpp_everything.sql ($(du -h "$BACKUP_DIR/tgpp_everything.sql" | cut -f1))"

# ----- 2. Qdrant snapshot -----
log "Qdrant snapshot tgpp_chunks_voyage_d1024..."
SNAP_RESP="$(curl -fsS -X POST "${QDRANT_URL:-http://127.0.0.1:6333}/collections/tgpp_chunks_voyage_d1024/snapshots")"
SNAP_NAME="$(echo "$SNAP_RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["name"])')"
log "snapshot name: $SNAP_NAME（已落地到 qdrant 容器内 /qdrant/snapshots/...）"
log "提示：Qdrant snapshot 默认存在 qdrant 容器卷里，本脚本不再拷贝出来"
log "如需拷出：docker cp qdrant:/qdrant/snapshots/tgpp_chunks_voyage_d1024/$SNAP_NAME $BACKUP_DIR/"
echo "$SNAP_NAME" > "$BACKUP_DIR/qdrant_snapshot_name.txt"
ok "Qdrant snapshot 记录: $BACKUP_DIR/qdrant_snapshot_name.txt"

# ----- 3. BM25 jsonl -----
log "tar BM25 jsonl..."
BM25_DIR="${INGEST_DATA_DIR}/bm25"
if [[ -d "$BM25_DIR" ]]; then
    tar -czf "$BACKUP_DIR/bm25.tar.gz" -C "$INGEST_DATA_DIR" bm25
    ok "BM25: $BACKUP_DIR/bm25.tar.gz ($(du -h "$BACKUP_DIR/bm25.tar.gz" | cut -f1))"
else
    err "BM25 目录不存在: $BM25_DIR；跳过"
fi

# ----- 4. .env -----
log "复制 .env..."
cp "$ENV_FILE" "$BACKUP_DIR/.env"
chmod 600 "$BACKUP_DIR/.env"
ok ".env: $BACKUP_DIR/.env (600)"

# ----- 5. metadata -----
cat > "$BACKUP_DIR/MANIFEST.txt" <<EOF
backup timestamp: $TS
host:             $(hostname)
git revision:     $(cd "$PROJECT_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)
git branch:       $(cd "$PROJECT_ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
size:             $(du -sh "$BACKUP_DIR" | cut -f1)
EOF

ok "全部完成: $BACKUP_DIR"
echo "MANIFEST:"
cat "$BACKUP_DIR/MANIFEST.txt"
