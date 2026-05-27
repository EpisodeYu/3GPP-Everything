#!/usr/bin/env bash
# tgpp ↔ dangdang 解耦后清理脚本（2026-05-27）。
#
# 锚：docs/04-handoff/2026-05-27-decouple-from-dangdang.md
#
# !!! 危险操作 !!!
# 在 dangdang-postgres 上 DROP 旧的 tgpp_everything 库 + DROP USER tgpp_app；
# 在 dangdang-redis 上 FLUSHDB db=5。
# 跑之前必须：
#   1. ./deploy/scripts/migrate-from-shared.sh 已成功完成
#   2. 浏览器/SSE 端到端验证 OK，业务在新 tgpp-postgres 上跑稳
#
# 跑完之后**就没法零摩擦回滚**了，回滚需要从 backup dump 反向 restore 一份到 dangdang。
#
# 用法：./deploy/scripts/cleanup-shared.sh
#
# 防误触：本脚本要求用户输入 "DROP" 大写才会真执行。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[cleanup]${RESET} $*"; }
warn() { echo -e "${YELLOW}[cleanup] WARN${RESET} $*" >&2; }
err()  { echo -e "${RED}[cleanup] ERROR${RESET} $*" >&2; }
ok()   { echo -e "${GREEN}[cleanup] OK${RESET} $*"; }

# ===== 1. 前置检查：确保已切到 tgpp-postgres/tgpp-redis =====
log "前置检查..."

# tgpp-postgres / tgpp-redis 在跑
docker ps --format '{{.Names}}' | grep -q '^tgpp-postgres$' || { err "tgpp-postgres 未运行；migrate 应该已经把它起来"; exit 1; }
docker ps --format '{{.Names}}' | grep -q '^tgpp-redis$'    || { err "tgpp-redis 未运行"; exit 1; }

# api 用的 host 必须是 tgpp-postgres
echo "$DATABASE_URL" | grep -q "@tgpp-postgres:" || { err "DATABASE_URL 不指向 tgpp-postgres，禁止清理"; exit 1; }
echo "$REDIS_URL"    | grep -q "@tgpp-redis:"    || { err "REDIS_URL 不指向 tgpp-redis，禁止清理"; exit 1; }

# api 在跑 + healthy
state="$(docker inspect -f '{{.State.Health.Status}}' tgpp-api 2>/dev/null || echo absent)"
[[ "$state" == "healthy" ]] || { err "tgpp-api 当前状态=$state（应 healthy），先确认业务跑通再清"; exit 1; }
ok "tgpp-postgres / tgpp-redis / tgpp-api 都在跑且 healthy"

# dangdang-postgres 在跑（要在它身上 DROP）
docker ps --format '{{.Names}}' | grep -q '^dangdang-postgres$' || { err "dangdang-postgres 未运行，无法 DROP"; exit 1; }
docker ps --format '{{.Names}}' | grep -q '^dangdang-redis$'    || { err "dangdang-redis 未运行，无法 FLUSHDB"; exit 1; }
ok "dangdang-postgres / dangdang-redis 在跑（可清理）"

# ===== 2. 二次确认 =====
echo
warn "本操作将："
warn "  1. 在 dangdang-postgres 上：DROP DATABASE tgpp_everything;"
warn "  2. 在 dangdang-postgres 上：DROP USER tgpp_app;"
warn "  3. 在 dangdang-redis 上：FLUSHDB db=5（清空 0 条 key，按 baseline 是空的）"
warn "  4. 删除 /tmp/tgpp_everything.*.sql 临时 dump（如有）"
echo
warn "执行后无法零摩擦回滚（回滚需从 fallback backup 反向恢复）。"
echo
read -r -p "确定继续？输入 DROP 大写确认：" ans
[[ "$ans" == "DROP" ]] || { err "用户中止"; exit 1; }

# ===== 3. DROP DATABASE & USER on dangdang-postgres =====
log "在 dangdang-postgres 上 DROP DATABASE / USER..."

# 用 dangdang 的超级用户身份；密码从 dangdang 项目 .env 读
DANGDANG_ENV="${DANGDANG_ENV:-$HOME/DangDangDiary/.env}"
if [[ -f "$DANGDANG_ENV" ]]; then
    DANG_PG_USER="$(grep -E '^DB_USER=' "$DANGDANG_ENV" | tail -1 | cut -d= -f2- | tr -d '"')"
    DANG_PG_PWD="$(grep -E '^DB_PASSWORD=' "$DANGDANG_ENV" | tail -1 | cut -d= -f2- | tr -d '"')"
fi
DANG_PG_USER="${DANG_PG_USER:-dangdang}"

if [[ -z "${DANG_PG_PWD:-}" ]]; then
    warn "未能从 $DANGDANG_ENV 读到 DB_PASSWORD"
    read -r -s -p "请输入 dangdang-postgres 超级用户 ($DANG_PG_USER) 密码: " DANG_PG_PWD
    echo
fi

# 切到 postgres 库再 DROP（不能在要 drop 的库上 drop 自己）。
# 注：docker exec 必须加 -i，否则 stdin (heredoc) 不会传到容器内的 psql，
# psql 收到 EOF 后正常退出 0 → 表面成功但 SQL 一句都没跑。
docker exec -i -e PGPASSWORD="$DANG_PG_PWD" dangdang-postgres \
    psql -U "$DANG_PG_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='tgpp_everything' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS tgpp_everything;
DROP USER IF EXISTS tgpp_app;
SQL

ok "dangdang-postgres 上 tgpp_everything + tgpp_app 已清理"

# 验证
LEFT="$(docker exec -e PGPASSWORD="$DANG_PG_PWD" dangdang-postgres \
    psql -U "$DANG_PG_USER" -d postgres -At -c \
    "SELECT 1 FROM pg_database WHERE datname='tgpp_everything';" 2>/dev/null || true)"
[[ -z "$LEFT" ]] || { err "DROP 后 tgpp_everything 仍存在，奇怪"; exit 1; }
ok "验证: tgpp_everything 已不在 dangdang-postgres"

# ===== 4. FLUSHDB on dangdang-redis db=5 =====
log "在 dangdang-redis 上 FLUSHDB db=5..."

DANG_REDIS_PWD="${DANG_REDIS_PWD:-}"
if [[ -f "$DANGDANG_ENV" && -z "$DANG_REDIS_PWD" ]]; then
    DANG_REDIS_PWD="$(grep -E '^REDIS_PASSWORD=' "$DANGDANG_ENV" | tail -1 | cut -d= -f2- | tr -d '"')"
fi

if [[ -z "$DANG_REDIS_PWD" ]]; then
    read -r -s -p "请输入 dangdang-redis 密码: " DANG_REDIS_PWD
    echo
fi

# baseline 是 0 keys；FLUSHDB 幂等
BEFORE="$(docker exec dangdang-redis redis-cli -a "$DANG_REDIS_PWD" -n 5 DBSIZE 2>/dev/null | tail -1)"
docker exec dangdang-redis redis-cli -a "$DANG_REDIS_PWD" -n 5 FLUSHDB >/dev/null 2>&1
AFTER="$(docker exec dangdang-redis redis-cli -a "$DANG_REDIS_PWD" -n 5 DBSIZE 2>/dev/null | tail -1)"
ok "dangdang-redis db=5：FLUSHDB 前 $BEFORE keys → 后 $AFTER keys"

# ===== 5. 清临时 dump =====
DUMPS=(/tmp/tgpp_everything.*.sql)
if compgen -G "/tmp/tgpp_everything.*.sql" >/dev/null; then
    log "删除临时 dump:"
    for f in "${DUMPS[@]}"; do
        rm -f "$f" && echo "  removed $f"
    done
fi

echo
echo "${GREEN}========================================"
echo "  解耦清理完成"
echo "========================================${RESET}"
cat <<EOF

dangdang 这边干净了：
  - dangdang-postgres 上不再有 tgpp_everything 库与 tgpp_app 角色
  - dangdang-redis db=5 已清空

tgpp 这边全在本项目自有容器上：
  - tgpp-postgres (volume: tgpp_tgpp-pgdata)
  - tgpp-redis    (无 volume)

后续维护：
  - 备份: make prod-backup（已经走 tgpp-postgres）
  - 探活: make prod-health（已加 tgpp-postgres / tgpp-redis 探活）
  - 回滚不再可零摩擦；如需，从 backups/<ts>/tgpp_everything.sql 反向恢复

如 .env.bak.2026-05-27-decouple 不再需要：rm ~/3GPP-Everything/.env.bak.2026-05-27-decouple

EOF
exit 0
