#!/usr/bin/env bash
# M8 — 一键发布脚本（host 上跑，不在容器里）。
#
# 锚：docs/03-development/07-cicd-and-deployment.md §6.1
#
# 做什么：
#   1. 校验 .env 必填项
#   2. git pull（可选，DEPLOY_GIT_PULL=1 时执行）
#   3. 前端 web-build（产 frontend/build/web/）
#   4. docker compose build api web
#   5. alembic upgrade head（容器网络内跑，用新 build 的 api 镜像；幂等）
#   6. docker compose up -d
#   7. 等 /health + /ready
#   8. 烟测 https 端点
#
# 用法：
#   ./deploy/scripts/deploy.sh              # 标准发布
#   DEPLOY_GIT_PULL=1 ./deploy/scripts/deploy.sh   # 同时拉最新代码
#   DEPLOY_SKIP_WEB=1 ./deploy/scripts/deploy.sh   # 跳过前端 build（仅改了 backend 时省 1-2min）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.prod.yml"
ENV_FILE="$PROJECT_ROOT/.env"

cd "$PROJECT_ROOT"

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; RESET=$'\033[0m'
log()  { echo -e "${BLUE}[deploy]${RESET} $*"; }
warn() { echo -e "${YELLOW}[deploy] WARN${RESET} $*" >&2; }
err()  { echo -e "${RED}[deploy] ERROR${RESET} $*" >&2; }
ok()   { echo -e "${GREEN}[deploy] OK${RESET} $*"; }

# api 镜像基于 python:3.11-slim，**没装 curl**；且 prod 端口只在 tgpp-net 内、不 publish 到宿主。
# 所以探活必须「在容器内」用 python urllib（与 compose healthcheck 同法），不能用 host curl。
api_probe() {  # $1 = health | ready
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T api \
        python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8002/$1',timeout=5).status==200 else 1)" \
        2>/dev/null
}

# ----- 校验 .env -----
[[ -f "$ENV_FILE" ]] || { err ".env 不存在"; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# DOMAIN 现在归 ~/infra/ingress/.env 管，本项目不再读取。
# 部署完后用域名烟测时从 ingress .env 读，避免重复定义。
INGRESS_ENV="${INGRESS_ENV:-$HOME/infra/ingress/.env}"
if [[ -f "$INGRESS_ENV" ]]; then
    DOMAIN="$(grep -E '^TGPP_DOMAIN=' "$INGRESS_ENV" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi
DOMAIN="${DOMAIN:-3gpp-everything.org}"

# ----- 可选 git pull -----
if [[ "${DEPLOY_GIT_PULL:-0}" == "1" ]]; then
    log "git pull..."
    git pull --ff-only
fi

# ----- 前端 build -----
if [[ "${DEPLOY_SKIP_WEB:-0}" == "1" ]]; then
    warn "DEPLOY_SKIP_WEB=1，跳过前端 build"
else
    log "前端 web-build (--release)..."
    make web-build API_BASE_URL=/api/v1
    ok "frontend/build/web/ 已就绪"
fi

# ----- Docker build -----
log "docker compose build..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build api web

# ----- Alembic migration（在 docker 网络内跑） -----
# 2026-05-27 解耦后 PG 只 expose 到 tgpp-net、不 publish 宿主端口，宿主机解析不了
# `tgpp-postgres` → 旧的 host 侧 `uv run alembic` 必然 gaierror。改用刚 build 的 api 镜像，
# compose run 会按 depends_on 先拉起 postgres 等其 healthy，再在 tgpp-net 内执行。
# `upgrade head` 幂等：DB 已在 head 时是 no-op，无新 migration 时零副作用。
log "alembic upgrade head（容器内）..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm api alembic upgrade head
ok "alembic 已在 head"

# ----- Docker up -----
log "docker compose up -d..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d

# ----- 等服务 -----
# BM25 启动加载 ~36s（pre-m8-validation-guide §0），healthcheck start_period=90s；探活留足 90 次。
log "等 api /health (最长 ~90s)..."
for i in {1..90}; do
    if api_probe health; then
        ok "api /health 通"
        break
    fi
    [[ $i -eq 90 ]] && { err "api 起不来；docker logs tgpp-api"; exit 1; }
    sleep 1
done

log "等 api /ready (最长 60s)..."
for i in {1..60}; do
    if api_probe ready; then
        ok "api /ready 通（PG / Qdrant / Redis / LiteLLM 全连通）"
        break
    fi
    [[ $i -eq 60 ]] && { warn "/ready 未通；可能某个依赖（PG/Qdrant/Redis/LiteLLM）不健康，看 docker logs tgpp-api"; }
    sleep 1
done

# ----- HTTPS 烟测 -----
log "HTTPS 烟测..."
if curl -fsS --max-time 10 "https://$DOMAIN/nginx-health" >/dev/null 2>&1; then
    ok "https://$DOMAIN/nginx-health 200"
else
    warn "外网 HTTPS 烟测失败；可能 DNS 未生效或防火墙没放行 443"
    warn "本机绕 DNS 测："
    warn "  curl -I --resolve $DOMAIN:443:127.0.0.1 https://$DOMAIN/nginx-health -k"
fi

cat <<EOF

${GREEN}========================================${RESET}
${GREEN}  部署完成${RESET}
${GREEN}========================================${RESET}

入口：https://$DOMAIN/
日志：make prod-logs
回滚：./deploy/scripts/restore.sh
EOF
