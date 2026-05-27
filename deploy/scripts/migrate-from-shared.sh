#!/usr/bin/env bash
# tgpp ↔ dangdang 解耦一键迁移脚本（2026-05-27）。
#
# 锚：docs/04-handoff/2026-05-27-decouple-from-dangdang.md
#
# 做什么（按顺序，可中断、可重跑）：
#   0. 预检 fail-fast：.env 已切到 tgpp-postgres/tgpp-redis；dangdang-postgres 还在跑；alembic head
#      与预期一致；磁盘够；compose config 通；让运行人在唯一交互点输入 yes 才继续
#   1. make prod-down   —— 停 tgpp 业务容器（不停 dangdang）
#   2. pg_dump 旧库（dangdang-postgres 上的 tgpp_everything） + 复制一份到 backups/<ts>/ 作 fallback
#   3. 起 tgpp-postgres + tgpp-redis，等 healthy
#   4. psql 导入 dump
#   5. 对账：表数 / alembic head / row counts vs 迁前基线（如果给了基线 JSON）
#   6. make prod-up + healthcheck.sh
#   7. 打印下一步：人工浏览器/SSE 验证 → 跑 cleanup-shared.sh
#
# 注：不调 make prod-backup，因为 backup.sh 解耦后指向 tgpp-postgres（此时还没起来）。
# Step 2 的 dump 文件本身就承担"fallback 全量备份"角色 —— 同时复制到 backups/<ts>/ 长期保留。
#
# **不做**清理（DROP DATABASE / FLUSHDB）—— 留给单独的 cleanup-shared.sh，让人在浏览器
# 验证完后手动跑一次，避免本脚本越权动 dangdang 的库。
#
# 用法：
#   ./deploy/scripts/migrate-from-shared.sh                 # 标准跑
#   FORCE=1 ./deploy/scripts/migrate-from-shared.sh         # 跳过 yes 交互（CI 不推荐用）
#   BASELINE=baseline.json ./deploy/scripts/migrate-from-shared.sh   # 给迁前基线做对账
#
# baseline.json 格式（可选；不给就只做表数/alembic head 对账）：
#   {"chunks_meta": 394859, "glossary": 34154, "users": 1, ...}

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.prod.yml"
ENV_FILE="$PROJECT_ROOT/.env"

cd "$PROJECT_ROOT"

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[migrate]${RESET} $*"; }
warn() { echo -e "${YELLOW}[migrate] WARN${RESET} $*" >&2; }
err()  { echo -e "${RED}[migrate] ERROR${RESET} $*" >&2; }
ok()   { echo -e "${GREEN}[migrate] OK${RESET} $*"; }

EXPECTED_ALEMBIC_HEAD="${EXPECTED_ALEMBIC_HEAD:-a1b2c3d4e5f6}"
EXPECTED_PUBLIC_TABLES_MIN="${EXPECTED_PUBLIC_TABLES_MIN:-15}"  # 业务 15 + alembic + langgraph 4
DUMP_FILE="${DUMP_FILE:-/tmp/tgpp_everything.$(date +%Y%m%d-%H%M%S).sql}"
BASELINE="${BASELINE:-}"

# ===== 0. 预检 =====
log "=== 0/7 预检 ==="

# 0.1 .env 已切
echo "$DATABASE_URL" | grep -q "@tgpp-postgres:" || { err "DATABASE_URL 还未切到 tgpp-postgres（当前: $DATABASE_URL）"; exit 1; }
echo "$REDIS_URL"    | grep -q "@tgpp-redis:"    || { err "REDIS_URL 还未切到 tgpp-redis（当前: $REDIS_URL）"; exit 1; }
[[ -n "${POSTGRES_PASSWORD:-}" ]] || { err "POSTGRES_PASSWORD 未在 .env 设置"; exit 1; }
[[ -n "${REDIS_PASSWORD:-}"    ]] || { err "REDIS_PASSWORD 未在 .env 设置"; exit 1; }
ok ".env 已切到 tgpp-postgres / tgpp-redis"

# 0.2 dangdang-postgres 容器还在跑（source of truth）
docker ps --format '{{.Names}}' | grep -q '^dangdang-postgres$' || { err "dangdang-postgres 未运行，无法 dump 源数据"; exit 1; }
ok "dangdang-postgres 在跑"

# 0.3 源库 alembic head
SRC_HEAD="$(docker exec dangdang-postgres psql -U tgpp_app -d tgpp_everything -At -c 'SELECT version_num FROM alembic_version LIMIT 1;' 2>/dev/null || true)"
[[ "$SRC_HEAD" == "$EXPECTED_ALEMBIC_HEAD" ]] || {
    err "源 alembic_version=$SRC_HEAD，预期 $EXPECTED_ALEMBIC_HEAD"; exit 1; }
ok "源 alembic head = $SRC_HEAD"

# 0.4 compose config 通
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config -q || { err "prod compose config 失败"; exit 1; }
ok "prod compose config OK"

# 0.5 磁盘
DUMP_DIR="$(dirname "$DUMP_FILE")"
FREE_KB="$(df -P "$DUMP_DIR" | awk 'NR==2 {print $4}')"
(( FREE_KB > 1048576 )) || { err "$DUMP_DIR 剩余空间 < 1GB，不安全"; exit 1; }
ok "磁盘剩余 $((FREE_KB / 1024)) MB（>1GB）"

# 0.6 检测是否已经迁过（tgpp-postgres 容器名已被占用）
if docker ps -a --format '{{.Names}}' | grep -q '^tgpp-postgres$'; then
    warn "tgpp-postgres 容器已存在 → 已经迁过？为安全起见停止"
    warn "若要重跑：docker compose -f $COMPOSE_FILE down 后再来"
    exit 1
fi

# 0.7 人工确认
echo
echo "================================================"
echo "  迁移摘要"
echo "  - 源: dangdang-postgres / tgpp_everything"
echo "  - 目标: tgpp-postgres (新容器，新 volume)"
echo "  - dump 文件: $DUMP_FILE"
echo "  - 预计停服时间: 5-10 分钟"
echo "  - 本脚本只迁 PG；Redis 不迁（数据全瞬时）"
echo "  - 本脚本不会 DROP 源库，清理留给 cleanup-shared.sh"
echo "================================================"
echo
if [[ "${FORCE:-0}" != "1" ]]; then
    read -r -p "输入 yes 继续，其他终止: " ans
    [[ "$ans" == "yes" ]] || { err "用户终止"; exit 1; }
fi

# ===== 1. 停业务 =====
log "=== 1/7 停 tgpp 业务容器 (make prod-down) ==="
# 注：这里要用旧 .env / 旧 compose 还能跑成功是因为 down 不需要连 DB。
# 如果之前是手工 docker run 起来的，down 不到没关系；下面 step 3 会显式杀。
make prod-down || true
# 兜底：手动 stop 老的 tgpp-api / tgpp-web（如果它们不归 prod compose 管）
for c in tgpp-api tgpp-web tgpp-ingest; do
    docker stop "$c" >/dev/null 2>&1 || true
    docker rm "$c"   >/dev/null 2>&1 || true
done
ok "tgpp 业务容器已停"

# ===== 2. pg_dump（兼作 fallback 备份）=====
log "=== 2/7 pg_dump → $DUMP_FILE ==="
# 源库角色密码（旧 .env 里的）从 .env.bak.* 读，因为新 .env 已切到新密码。
SRC_DB_PWD=""
for bak in "$PROJECT_ROOT"/.env.bak.2026-05-27-* "$PROJECT_ROOT"/.env.bak; do
    [[ -f "$bak" ]] || continue
    cand="$(grep -E '^DATABASE_URL=' "$bak" | tail -1 | sed -E 's|.*://tgpp_app:([^@]+)@.*|\1|')"
    if [[ -n "$cand" && "$cand" != "$DATABASE_URL" ]]; then
        SRC_DB_PWD="$cand"
        break
    fi
done
[[ -n "$SRC_DB_PWD" ]] || { err "找不到旧 tgpp_app 密码（应在 .env.bak.2026-05-27-decouple 里）"; exit 1; }

docker exec -e PGPASSWORD="$SRC_DB_PWD" dangdang-postgres \
    pg_dump -U tgpp_app -d tgpp_everything --no-owner --no-acl --clean --if-exists \
    > "$DUMP_FILE"
DUMP_SIZE="$(du -h "$DUMP_FILE" | cut -f1)"
DUMP_SHA="$(sha256sum "$DUMP_FILE" | cut -d' ' -f1)"
[[ -s "$DUMP_FILE" ]] || { err "dump 文件为空"; exit 1; }
ok "dump 完成: $DUMP_SIZE, sha256=$DUMP_SHA"

# 复制一份到 backups/<ts>/ 作长期 fallback
FALLBACK_DIR="$PROJECT_ROOT/backups/$(date +%Y%m%d-%H%M%S)-pre-decouple"
mkdir -p "$FALLBACK_DIR"
cp "$DUMP_FILE" "$FALLBACK_DIR/tgpp_everything.sql"
cp "$ENV_FILE"  "$FALLBACK_DIR/.env" 2>/dev/null || true
for bak in "$PROJECT_ROOT"/.env.bak.2026-05-27-*; do
    [[ -f "$bak" ]] && cp "$bak" "$FALLBACK_DIR/$(basename "$bak")"
done
cat > "$FALLBACK_DIR/MANIFEST.txt" <<EOF
backup timestamp: $(date +%Y-%m-%d_%H:%M:%S_%z)
purpose:          pre-decouple fallback (2026-05-27 decoupling from dangdang)
dump source:      dangdang-postgres / tgpp_everything (old shared instance)
dump sha256:      $DUMP_SHA
dump size:        $DUMP_SIZE
restore guide:    see docs/04-handoff/2026-05-27-decouple-from-dangdang.md §七
EOF
ok "fallback 备份: $FALLBACK_DIR/"

# ===== 3. 起 tgpp-postgres + tgpp-redis =====
log "=== 3/7 启 tgpp-postgres + tgpp-redis ==="
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d postgres redis

log "等 postgres healthcheck → healthy（最多 60s）..."
for i in {1..60}; do
    state="$(docker inspect -f '{{.State.Health.Status}}' tgpp-postgres 2>/dev/null || echo unknown)"
    [[ "$state" == "healthy" ]] && { ok "tgpp-postgres healthy"; break; }
    sleep 1
    [[ $i -eq 60 ]] && { err "tgpp-postgres 60s 未 healthy（state=$state）"; docker logs --tail 50 tgpp-postgres; exit 1; }
done

log "等 redis 接受连接（最多 30s）..."
for i in {1..30}; do
    if docker exec -e RP="$REDIS_PASSWORD" tgpp-redis sh -c 'redis-cli -a "$RP" ping 2>/dev/null' | grep -q PONG; then
        ok "tgpp-redis PONG"
        break
    fi
    sleep 1
    [[ $i -eq 30 ]] && { err "tgpp-redis 30s 未响应 PING"; docker logs --tail 50 tgpp-redis; exit 1; }
done

# ===== 4. 导入 dump =====
log "=== 4/7 psql 导入 dump ==="
# pg_dump 用了 --clean --if-exists：导入时先 drop 现有表（init.sql 留下来的 extension 不受影响）。
# stderr 偶尔会有 NOTICE （比如 "table X does not exist, skipping"），保留以便排查。
LOAD_LOG="/tmp/tgpp_restore.$(date +%Y%m%d-%H%M%S).log"
if ! docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" tgpp-postgres \
        psql -U tgpp_app -d tgpp_everything --set ON_ERROR_STOP=on \
        < "$DUMP_FILE" > "$LOAD_LOG" 2>&1; then
    err "psql 导入失败，看 $LOAD_LOG 末尾："
    tail -30 "$LOAD_LOG" >&2
    exit 1
fi
ok "导入完成，psql 输出: $LOAD_LOG"

# ===== 5. 对账 =====
log "=== 5/7 对账 ==="

# 6.1 alembic head
NEW_HEAD="$(docker exec tgpp-postgres psql -U tgpp_app -d tgpp_everything -At -c 'SELECT version_num FROM alembic_version LIMIT 1;')"
[[ "$NEW_HEAD" == "$EXPECTED_ALEMBIC_HEAD" ]] || { err "新库 alembic head=$NEW_HEAD，预期 $EXPECTED_ALEMBIC_HEAD"; exit 1; }
ok "新库 alembic head = $NEW_HEAD ✓"

# 6.2 表数
NEW_TABLES="$(docker exec tgpp-postgres psql -U tgpp_app -d tgpp_everything -At -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
(( NEW_TABLES >= EXPECTED_PUBLIC_TABLES_MIN )) || {
    err "新库 public schema 表数=$NEW_TABLES，至少应 $EXPECTED_PUBLIC_TABLES_MIN"; exit 1; }
ok "新库 public 表数 = $NEW_TABLES（≥ $EXPECTED_PUBLIC_TABLES_MIN）✓"

# 6.3 row count 对账
log "row counts:"
TABLES=(chunks_meta documents document_versions users sessions messages \
        message_citations feedbacks refresh_tokens api_usage audit_logs \
        favorites notes glossary tasks)
declare -A NEW_COUNTS=()
for t in "${TABLES[@]}"; do
    c="$(docker exec tgpp-postgres psql -U tgpp_app -d tgpp_everything -At -c "SELECT count(*) FROM $t;" 2>/dev/null || echo "?")"
    NEW_COUNTS[$t]="$c"
    if [[ -n "$BASELINE" && -f "$BASELINE" ]]; then
        expected="$(python3 -c "import json,sys;d=json.load(open('$BASELINE'));print(d.get('$t','?'))")"
        if [[ "$c" == "$expected" ]]; then
            printf "  %-22s %10s ✓\n" "$t" "$c"
        else
            printf "  %-22s %10s ${RED}!=${RESET} %s\n" "$t" "$c" "$expected"
            err "row count 不一致: $t (new=$c, baseline=$expected)"
            exit 1
        fi
    else
        printf "  %-22s %10s\n" "$t" "$c"
    fi
done
ok "对账通过"

# ===== 6. 起业务 =====
log "=== 6/7 make prod-up + healthcheck ==="
make prod-up

log "等 api healthcheck → healthy（最多 180s，含 BM25 加载）..."
for i in {1..180}; do
    state="$(docker inspect -f '{{.State.Health.Status}}' tgpp-api 2>/dev/null || echo unknown)"
    [[ "$state" == "healthy" ]] && { ok "tgpp-api healthy"; break; }
    sleep 1
    [[ $i -eq 180 ]] && { err "tgpp-api 180s 未 healthy（state=$state），看 docker logs tgpp-api"; exit 1; }
done

if ! "$SCRIPT_DIR/healthcheck.sh"; then
    err "healthcheck.sh 报告失败 —— 看输出"
    exit 1
fi
ok "healthcheck 全绿"

# ===== 7. 完成 =====
echo
echo "${GREEN}========================================"
echo "  迁移完成"
echo "========================================${RESET}"
cat <<EOF

接下来人工执行：
  1. 浏览器登录 https://3gpp-everything.org/，发问 'What is HARQ?'
     - DevTools Network 看 /api/v1/sessions/.../messages 为 eventstream，token 流式累积
     - 登录态、历史会话列表都在（PG 数据迁过来了）
  2. 验证通过后跑 cleanup:
       ./deploy/scripts/cleanup-shared.sh
     （会 DROP 旧库 tgpp_everything + DROP USER tgpp_app + FLUSHDB Redis db=5）
  3. 不放心可保留 dump 文件 $DUMP_FILE 一段时间再删

回滚（cleanup-shared.sh 之前）：
  - make prod-down
  - cp .env.bak.2026-05-27-decouple .env
  - git checkout HEAD -- deploy/docker-compose.prod.yml  # 或人工还原
  - make prod-up
  （dangdang-postgres 上的 tgpp_everything 还在）

EOF

exit 0
