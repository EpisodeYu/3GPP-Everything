#!/usr/bin/env bash
# M8 — 一键探活脚本，部署后或定时跑都行。
#
# 锚：docs/03-development/07-cicd-and-deployment.md §6
#
# 检查项：
#   1. nginx :80 /nginx-health
#   2. api 容器内 /health
#   3. api 容器内 /ready（4 依赖）
#   4. 外网 https://$DOMAIN/nginx-health
#   5. 证书有效期（< 7 天告警）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$DEPLOY_DIR")"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.prod.yml"
ENV_FILE="$PROJECT_ROOT/.env"

# 域名归 ingress 项目管，本脚本从 ingress 的 .env 读取做域名烟测。
# 如果 ingress 不在标准路径，传 INGRESS_ENV=/path/to/.env 覆盖。
INGRESS_ENV="${INGRESS_ENV:-$HOME/infra/ingress/.env}"
DOMAIN=""
if [[ -f "$INGRESS_ENV" ]]; then
    DOMAIN="$(grep -E '^TGPP_DOMAIN=' "$INGRESS_ENV" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; RESET=$'\033[0m'
fail=0
check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo -e "${GREEN}[OK]${RESET}     $name"
    else
        echo -e "${RED}[FAIL]${RESET}   $name"
        fail=$((fail+1))
    fi
}

echo "=== 业务容器状态 (tgpp) ==="
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps

echo
echo "=== 数据面健康（2026-05-27 解耦后专属 PG/Redis）==="
check "tgpp-postgres pg_isready" \
    docker exec tgpp-postgres pg_isready -U tgpp_app -d tgpp_everything
check "tgpp-redis    PONG" \
    bash -c 'docker exec -e RP="$REDIS_PASSWORD" tgpp-redis sh -c "redis-cli -a \"\$RP\" ping 2>/dev/null" | grep -q PONG'

echo
echo "=== 应用层健康 ==="
# api 容器是 python:3.11-slim，没 curl/wget，用 python urllib 替代。
check "api    /health  (container)" \
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T api \
    python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8002/health',timeout=3).status==200 else 1)"
check "api    /ready   (container)" \
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T api \
    python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8002/ready',timeout=10).status==200 else 1)"

if [[ -n "$DOMAIN" ]]; then
    echo
    echo "=== 入口层端到端 (ingress + 业务) ==="
    check "https://$DOMAIN/ (外网，经 ingress)" curl -fsS --max-time 10 "https://$DOMAIN/" -o /dev/null
    check "https://$DOMAIN/health (容器内 /health 透 ingress)" \
        curl -fsS --max-time 10 "https://$DOMAIN/health" -o /dev/null

    echo
    echo "=== 证书有效期（ingress 项目管理） ==="
    CERT_FILE="$HOME/infra/ingress/certbot/conf/live/$DOMAIN/fullchain.pem"
    # 用 -L 验 symlink 存在；过期时间从外网拉（避免读 root-owned archive）。
    if [[ -L "$CERT_FILE" ]]; then
        # 从外网拿证书过期时间（绕开 host 端 archive 权限）
        expiry=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null \
            | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
        if [[ -n "$expiry" ]]; then
            expiry_epoch=$(date -d "$expiry" +%s 2>/dev/null || echo 0)
            days_left=$(( ($expiry_epoch - $(date +%s)) / 86400 ))
            if [[ $days_left -gt 30 ]]; then
                echo -e "${GREEN}[OK]${RESET}     证书还剩 $days_left 天"
            elif [[ $days_left -gt 7 ]]; then
                echo -e "${YELLOW}[WARN]${RESET}   证书快过期：$days_left 天"
            else
                echo -e "${RED}[FAIL]${RESET}   证书剩 $days_left 天，紧急"
                fail=$((fail+1))
            fi
        else
            echo -e "${YELLOW}[WARN]${RESET}   证书 symlink 在但取过期日期失败"
        fi
    else
        echo -e "${YELLOW}[INFO]${RESET}   未找到 $CERT_FILE；ingress 项目可能未初始化"
    fi
else
    echo
    echo -e "${YELLOW}[INFO]${RESET} 未从 $INGRESS_ENV 读到 TGPP_DOMAIN，跳过入口层探活"
fi

echo
if [[ $fail -eq 0 ]]; then
    echo -e "${GREEN}全部通过${RESET}"
    exit 0
else
    echo -e "${RED}$fail 项失败${RESET}"
    exit 1
fi
