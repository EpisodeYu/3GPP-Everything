#!/usr/bin/env bash
# scripts/dev/start-spec-browser.sh
#
# 一键启动 backend + bootstrap-admin + login + 缓存 token，方便用 Swagger UI 浏览 spec。
# 用途：M7.0 手写金标准题时一边写一边查 spec（详见 docs/04-handoff/2026-05-19-m7-plan.md §4.1）。
#
# 用法：
#   bash scripts/dev/start-spec-browser.sh                 # 默认 username=s1yu password=changeme123
#   TGPP_USER=alice TGPP_PASS=mypwd bash ...               # 覆盖凭证
#   TGPP_BACKEND_PORT=18002 bash ...                       # 换端口（默认 8002，与 .env::API_PORT 一致）
#   bash scripts/dev/start-spec-browser.sh --stop          # 停掉脚本起的 backend
#   bash scripts/dev/start-spec-browser.sh --status        # 查状态
#
# 设计要点：
# - 监听 127.0.0.1（不暴露公网）→ Windows 浏览器走 SSH 隧道（脚本最后会打印命令）
# - backend 用 nohup 后台跑，PID 写 /tmp/tgpp-backend.pid，日志 /tmp/tgpp-backend.log
# - bootstrap-admin 409（已 bootstrap）视为成功；login 取 access_token 写 ~/.cache/tgpp-token
# - 全程不依赖 jq（自实现 grep/sed JSON parse，对 access_token / id 等单字段足够稳）
# - 共享服务（PG/Qdrant/LiteLLM/Redis）只 readiness probe，不替你启
#
# 自动决策（CLAUDE.md §4.3）：
# - 端口 / 路径 / 凭证默认值用 .env 与现网约定，覆盖走 env var 不改脚本
# - 不动 backend 代码，不动 .env

set -euo pipefail

# ---------- 路径 ----------
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
PID_FILE="/tmp/tgpp-backend.pid"
LOG_FILE="/tmp/tgpp-backend.log"
TOKEN_FILE="${HOME}/.cache/tgpp-token"

# ---------- 配置（env 覆盖）----------
TGPP_USER="${TGPP_USER:-admin}"
TGPP_PASS="${TGPP_PASS:-changeme123}"
TGPP_BACKEND_HOST="${TGPP_BACKEND_HOST:-127.0.0.1}"
TGPP_BACKEND_PORT="${TGPP_BACKEND_PORT:-8002}"
BACKEND_BASE="http://${TGPP_BACKEND_HOST}:${TGPP_BACKEND_PORT}"

# 颜色（terminal 不支持时降级）
if [[ -t 1 ]]; then
    C_INFO=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_END=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi

log_info() { echo "${C_INFO}[INFO]${C_END} $*"; }
log_ok()   { echo "${C_OK}[ OK ]${C_END} $*"; }
log_warn() { echo "${C_WARN}[WARN]${C_END} $*" >&2; }
log_err()  { echo "${C_ERR}[ERR ]${C_END} $*" >&2; }

# ---------- JSON 单字段抽取（不依赖 jq）----------
# 用法：echo "$json" | json_field access_token
json_field() {
    local field="$1"
    grep -oE "\"${field}\"\s*:\s*\"[^\"]*\"" \
        | head -1 \
        | sed -E "s/.*\"${field}\"\s*:\s*\"([^\"]*)\"/\1/"
}

# ---------- subcommand: --stop / --status ----------
case "${1:-}" in
    --stop)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            kill "$(cat "$PID_FILE")"
            rm -f "$PID_FILE"
            log_ok "backend 已停止"
        else
            log_warn "没找到正在运行的 backend (PID file 不存在或进程已退出)"
            rm -f "$PID_FILE"
        fi
        exit 0
        ;;
    --status)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            log_ok "backend 在跑 PID=$(cat "$PID_FILE") @ ${BACKEND_BASE}"
            log_info "log:   tail -f ${LOG_FILE}"
            log_info "token: cat ${TOKEN_FILE} 2>/dev/null | head -c 40; echo …"
        else
            log_warn "backend 没在跑"
        fi
        exit 0
        ;;
esac

# ---------- Step 0 · 共享服务 sanity check ----------
log_info "Step 0/5 · 共享服务 readiness probe"

# Qdrant
if curl -sf -m 3 -o /dev/null "http://localhost:6333/collections"; then
    log_ok "  Qdrant @ :6333 ready"
else
    log_err "  Qdrant @ :6333 不可达，先 docker compose up qdrant 或检查共享服务"
    exit 1
fi

# LiteLLM
LITELLM_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    LITELLM_KEY=$(grep -E "^LITELLM_API_KEY=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)
fi
if curl -sf -m 3 -o /dev/null -H "Authorization: Bearer ${LITELLM_KEY}" "http://localhost:4000/v1/models"; then
    log_ok "  LiteLLM @ :4000 ready"
else
    log_warn "  LiteLLM @ :4000 不可达 - reader 路由不需要 LiteLLM，仅警告，继续"
fi

# Postgres（用 nc 探测端口；没装 nc 跳过）
if command -v nc >/dev/null 2>&1; then
    if nc -z -w 2 localhost 5432 2>/dev/null; then
        log_ok "  Postgres @ :5432 ready"
    else
        log_err "  Postgres @ :5432 不可达，先起 PG"
        exit 1
    fi
fi

# Redis
if command -v nc >/dev/null 2>&1; then
    if nc -z -w 2 localhost 6379 2>/dev/null; then
        log_ok "  Redis @ :6379 ready"
    else
        log_warn "  Redis @ :6379 不可达 - reader 路由可工作但 ratelimit / cache 失效"
    fi
fi

# ---------- Step 1 · 起 backend ----------
log_info "Step 1/5 · 起 backend (${BACKEND_BASE})"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log_ok "  backend 已经在跑 PID=$(cat "$PID_FILE")，复用"
else
    if curl -sf -m 2 -o /dev/null "${BACKEND_BASE}/health"; then
        log_ok "  端口 ${TGPP_BACKEND_PORT} 已有 backend 响应（非本脚本起的），复用"
    else
        cd "$REPO_ROOT/backend"
        # 静默 sync 依赖（已 sync 过的话 < 1s）
        log_info "  uv sync …"
        uv sync --frozen >/dev/null 2>&1 || uv sync >/dev/null 2>&1 || {
            log_err "  uv sync 失败，手动跑：cd backend && uv sync"
            exit 1
        }
        log_info "  nohup uvicorn ... &"
        : > "$LOG_FILE"
        nohup uv run uvicorn app.main:app \
            --host "$TGPP_BACKEND_HOST" --port "$TGPP_BACKEND_PORT" \
            --no-access-log \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        log_info "  PID=$(cat "$PID_FILE") log=${LOG_FILE}"
    fi
fi

# 等 /health 200（最多 30s）
log_info "  等待 /health 返回 200 …"
for i in {1..30}; do
    if curl -sf -m 2 -o /dev/null "${BACKEND_BASE}/health"; then
        log_ok "  backend ready"
        break
    fi
    if [[ $i -eq 30 ]]; then
        log_err "  backend 30s 内没起来，看 ${LOG_FILE}"
        tail -30 "$LOG_FILE" >&2 || true
        exit 1
    fi
    sleep 1
done

# /ready 检查（不阻塞，仅提示）
# /ready 返回结构：{"ok":true,"checks":[{"name":"postgres","ok":true},...]}
# 顶层 ok=true 即四依赖全绿；任一 fail 时 HTTP 状态码会是 503，curl -f 已 fail
READY=$(curl -sf -m 5 "${BACKEND_BASE}/ready" || echo "{}")
if echo "$READY" | grep -qE '^\{"ok":true'; then
    log_ok "  /ready 全绿（PG/Qdrant/Redis/LiteLLM）"
else
    log_warn "  /ready 不是全绿（不阻塞）："
    echo "$READY" | head -c 400; echo
fi

# ---------- Step 2 · bootstrap-admin（首次）----------
log_info "Step 2/5 · bootstrap-admin (username=${TGPP_USER})"

INVITE_CODE=""
if [[ -f "$ENV_FILE" ]]; then
    INVITE_CODE=$(grep -E "^BOOTSTRAP_ADMIN_INVITE_CODE=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)
fi
if [[ -z "$INVITE_CODE" ]]; then
    log_err "  .env::BOOTSTRAP_ADMIN_INVITE_CODE 为空，无法 bootstrap"
    exit 1
fi

BOOTSTRAP_RESP=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${BACKEND_BASE}/api/v1/auth/bootstrap-admin" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${TGPP_USER}\",\"password\":\"${TGPP_PASS}\",\"invite_code\":\"${INVITE_CODE}\"}")

case "$BOOTSTRAP_RESP" in
    201) log_ok "  bootstrap 成功（首次）" ;;
    409) log_ok "  bootstrap 已存在（之前跑过），跳过" ;;
    *)   log_warn "  bootstrap 返回 ${BOOTSTRAP_RESP}（不阻塞 - 之前可能用别的用户名 bootstrap 过；将尝试 login）" ;;
esac

# ---------- Step 3 · login 拿 access_token ----------
log_info "Step 3/5 · login 拿 access_token"

LOGIN_RESP=$(curl -s -X POST "${BACKEND_BASE}/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${TGPP_USER}\",\"password\":\"${TGPP_PASS}\"}")

TOKEN=$(echo "$LOGIN_RESP" | json_field access_token || true)
if [[ -z "$TOKEN" ]]; then
    log_err "  login 失败，response:"
    echo "$LOGIN_RESP" | head -c 400; echo
    log_err "  常见原因：1) 密码错（试 TGPP_PASS=... 重跑）  2) 账号被停用"
    exit 1
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
echo -n "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
log_ok "  access_token 写入 ${TOKEN_FILE}（chmod 600）"
log_info "  token 前 40 字符: $(echo -n "$TOKEN" | head -c 40)…"

# ---------- Step 4 · 烟测 reader 路由 ----------
log_info "Step 4/5 · 烟测 reader 路由"

DOCS_COUNT=$(curl -sf -m 5 "${BACKEND_BASE}/api/v1/docs?release=Rel-18" \
    -H "Authorization: Bearer ${TOKEN}" \
    | grep -oE '"spec_id"' | wc -l)
log_ok "  GET /api/v1/docs?release=Rel-18 → ${DOCS_COUNT} 个 spec"

# ---------- Step 5 · 输出使用提示 ----------
log_info "Step 5/5 · 一键完成 ✅"

cat <<EOF

${C_OK}===========================================================${C_END}
  ${C_OK}Swagger UI 就绪，下面三步在 Windows 浏览器打开${C_END}
${C_OK}===========================================================${C_END}

${C_INFO}1.${C_END} 在 ${C_INFO}Windows 端${C_END} 开 PowerShell 或 CMD，跑下面命令做 SSH 端口转发
   （把服务器 8002 端口映射到 Windows 本地 8002；每次新开 ssh session 跑一次）：

   ${C_OK}ssh -N -L 8002:127.0.0.1:${TGPP_BACKEND_PORT} s1yu@130.94.66.142${C_END}

   说明：
     -N           不开远程 shell（专做转发）
     -L A:B:C     Windows 本地 A 端口 → 服务器 B:C
     130.94.66.142 是这台服务器的公网 IP（从 eth1 拿到，可能要换）

${C_INFO}2.${C_END} 浏览器打开：

   ${C_OK}http://127.0.0.1:8002/docs${C_END}

${C_INFO}3.${C_END} Swagger UI 右上角点 ${C_OK}Authorize${C_END}，粘贴下面这串（带 Bearer 前缀）：

   ${C_OK}Bearer ${TOKEN}${C_END}

   token 同时已经存到 ${TOKEN_FILE}，过期时重跑本脚本刷新即可（access token 15 分钟 expire）。

${C_INFO}重点路由${C_END}（写题主战场，详见 docs/04-handoff/2026-05-19-m7-plan.md §4.1）：
   GET /api/v1/docs?release=Rel-18                       列已索引 spec
   GET /api/v1/docs/{spec_id}                            spec 章节树（如 23.501）
   GET /api/v1/docs/{spec_id}/sections/{path}            完整章节 markdown（如 23.501 / 5.6.1）
   GET /api/v1/docs/{spec_id}/search?q={kw}              spec 内 BM25 搜索

${C_INFO}backend 控制：${C_END}
   bash scripts/dev/start-spec-browser.sh --status       看状态
   bash scripts/dev/start-spec-browser.sh --stop         停掉
   tail -f ${LOG_FILE}                                    看日志

EOF
