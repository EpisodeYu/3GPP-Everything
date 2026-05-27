#!/usr/bin/env bash
# M8 — 备份恢复脚本。配 backup.sh 使用。
#
# 锚：docs/03-development/07-cicd-and-deployment.md §6.2
#
# !!! 危险操作 !!!
# 会 drop 然后重建 PG 数据库；Qdrant collection 由用户手动 restore。
# 必须经过 CLAUDE.md §5.4 流程：本脚本只在用户明确手动执行时跑，绝不在 deploy 自动链路里调用。
#
# 用法：
#   ./deploy/scripts/restore.sh <backup_dir>
#   # 示例：./deploy/scripts/restore.sh ./backups/20260526-180000

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "用法: $0 <backup_dir>"
    echo "示例: $0 ./backups/20260526-180000"
    exit 1
fi

BACKUP_DIR="$(realpath "$1")"
[[ -d "$BACKUP_DIR" ]] || { echo "目录不存在: $BACKUP_DIR"; exit 1; }
[[ -f "$BACKUP_DIR/MANIFEST.txt" ]] || { echo "MANIFEST.txt 缺失，疑似不是 backup.sh 产物"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[restore]${RESET} $*"; }
warn() { echo -e "${YELLOW}[restore] WARN${RESET} $*" >&2; }
err()  { echo -e "${RED}[restore] ERROR${RESET} $*" >&2; }
ok()   { echo -e "${GREEN}[restore] OK${RESET} $*"; }

echo "================================================"
cat "$BACKUP_DIR/MANIFEST.txt"
echo "================================================"
echo
warn "本操作将："
warn "  1. drop 现有 PG database 'tgpp_everything' 并从备份恢复"
warn "  2. 解压 BM25 tarball 到 $INGEST_DATA_DIR/bm25"
warn "  3. .env 文件 *不会* 自动覆盖（防止误丢密钥）；如需恢复请手动 cp"
warn "  4. Qdrant collection 不会自动恢复（用户走 admin API 或手动 snapshot restore）"
warn "  5. Let's Encrypt 证书不在本备份范围（归 ~/infra/ingress/ 管）"
echo
read -r -p "确定继续？输入 RESTORE 大写确认：" ans
[[ "$ans" == "RESTORE" ]] || { err "用户中止"; exit 1; }

# ----- 1. PG -----
if [[ -f "$BACKUP_DIR/tgpp_everything.sql" ]]; then
    log "恢复 PG database 到 tgpp-postgres..."
    # 2026-05-27 解耦后走本项目专属 tgpp-postgres 容器。
    docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" tgpp-postgres \
        psql -U tgpp_app -d tgpp_everything < "$BACKUP_DIR/tgpp_everything.sql"
    ok "PG 恢复完成"
else
    warn "缺 $BACKUP_DIR/tgpp_everything.sql；跳过 PG"
fi

# ----- 2. BM25 -----
if [[ -f "$BACKUP_DIR/bm25.tar.gz" ]]; then
    log "恢复 BM25 jsonl..."
    rm -rf "$INGEST_DATA_DIR/bm25"
    tar -xzf "$BACKUP_DIR/bm25.tar.gz" -C "$INGEST_DATA_DIR"
    ok "BM25 恢复到 $INGEST_DATA_DIR/bm25"
else
    warn "缺 $BACKUP_DIR/bm25.tar.gz；跳过"
fi

# ----- 3. Qdrant 提示 -----
if [[ -f "$BACKUP_DIR/qdrant_snapshot_name.txt" ]]; then
    SNAP_NAME="$(cat "$BACKUP_DIR/qdrant_snapshot_name.txt")"
    cat <<EOF

${YELLOW}=== Qdrant collection 手动恢复 ===${RESET}
snapshot name: $SNAP_NAME

恢复步骤（在 Qdrant 容器上）：
  1. 若 snapshot 文件已在 qdrant 容器内：
     curl -X PUT '${QDRANT_URL:-http://127.0.0.1:6333}/collections/tgpp_chunks_voyage_d1024/snapshots/recover' \\
          -H 'Content-Type: application/json' \\
          -d '{"location": "/qdrant/snapshots/tgpp_chunks_voyage_d1024/$SNAP_NAME"}'
  2. 若 snapshot 备份在外部，先 docker cp <file> qdrant:/qdrant/snapshots/tgpp_chunks_voyage_d1024/
EOF
fi

ok "全部恢复步骤完成"
