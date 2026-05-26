# M8 部署 Bootstrap — 域名 / Ingress / Let's Encrypt 落地

> **日期**：2026-05-26（午后 rev2：从单项目 nginx 改为独立 ingress 层）
> **状态**：Agent 完成全部代码改动；待人执行 Cloudflare DNS（2 条 A 记录）+ 服务器防火墙 + ingress 项目首次签证书 + 启业务容器。
> **域名**：`3gpp-everything.org`（3GPP-Everything）+ `dangdangdiary.org`（DangDangDiary 共用同一台服务器）
> **锚**：`docs/03-development/07-cicd-and-deployment.md` §3-§8；`pre-m8-validation-guide.md` §7；`~/infra/ingress/README.md`

---

## 一、最终架构

```
公网 80/443
   ↓
[ingress-nginx]                       ← ~/infra/ingress/  (独立项目)
   ↓ 按 server_name 分流
   ├─ Host=3gpp-everything.org   → tgpp-web:80     → tgpp-api:8002
   │                                (~/3GPP-Everything/deploy/docker-compose.prod.yml)
   ├─ Host=dangdangdiary.org      → dangdang-nginx:80
   │                                (~/DangDangDiary/docker-compose.yml)
   └─ /.well-known/acme-challenge → ingress-certbot
```

**核心原则**：业务项目不再直接对外，全部由 ingress 层接管 80/443。

### 三个项目的职责切分

| 项目 | 路径 | 职责 |
|---|---|---|
| **ingress**（新建） | `~/infra/ingress/` | 占 80/443、TLS 卸载、Let's Encrypt、按 server_name 分流 |
| **3GPP-Everything** | `~/3GPP-Everything/` | 业务容器 api + web（web 内部 nginx 反代 `/api/v1/`）；不再起 nginx/certbot |
| **DangDangDiary** | `~/DangDangDiary/` | 业务容器（含 dangdang-nginx 项目内部反代）；nginx 不再 publish 80 |

---

## 二、Agent 本次产出

### 2.1 新建：`~/infra/ingress/`（独立项目）

```text
~/infra/ingress/
├── README.md                      ← 项目说明
├── docker-compose.yml             ← nginx + certbot 两个 service
├── .env.example                   ← TGPP_DOMAIN / DANGDANG_DOMAIN / LETSENCRYPT_EMAIL / PUBLIC_IP / CERTBOT_STAGING
├── nginx/
│   ├── 00-http.conf.template      ← :80 ACME + 301
│   ├── 10-tgpp.conf.template      ← :443 server_name 3gpp-everything.org + SSE 路径
│   └── 20-dangdang.conf.template  ← :443 server_name dangdangdiary.org
├── scripts/
│   ├── init-letsencrypt.sh        ← 一次性签所有域名证书
│   └── healthcheck.sh             ← 探活
└── certbot/{conf,www}             ← 证书 + ACME webroot
```

### 2.2 改动：`~/3GPP-Everything/`

| 文件 | 改动 |
|---|---|
| `deploy/docker-compose.prod.yml` | **删除** nginx 和 certbot 两个 service；改顶部注释反映新架构；保留 api + web + ingest |
| `deploy/scripts/deploy.sh` | DOMAIN 改从 `~/infra/ingress/.env` 读 |
| `deploy/scripts/healthcheck.sh` | 重写：业务层 + 入口层分开探活；证书路径指向 ingress 项目 |
| `deploy/scripts/backup.sh` | 移除证书备份段（证书归 ingress 项目） |
| `deploy/scripts/restore.sh` | 同上 |
| `deploy/nginx/` | **整目录删除** |
| `deploy/certbot/` | **整目录删除** |
| `deploy/scripts/init-letsencrypt.sh` | **删除**（职责搬到 ingress 项目） |
| `Makefile` | 移除 `prod-init-cert` 目标；prod-up 注释更新 |
| `.env.example` | 移除 DOMAIN / LETSENCRYPT_EMAIL / PUBLIC_IP / CERTBOT_STAGING；加引用 ingress 的说明 |

### 2.3 改动：`~/DangDangDiary/`

| 文件 | 改动 |
|---|---|
| `docker-compose.yml` | nginx 服务 `ports: ["80:80"]` → `expose: ["80"]`；加注释说明端口由 ingress 接管 |

> dangdang 项目的 `nginx/nginx.conf` **一行未改**。

### 2.4 自查（按 CLAUDE.md §6.3）

- ✅ ReadLints：0 errors
- ✅ 所有脚本 `bash -n`：5/5 OK
- ✅ `docker compose config -q`：3 个 compose（tgpp prod / ingress / dangdang）全部 OK
- ✅ dev compose `deploy/docker-compose.yml` 未动（md5 不变）
- ✅ 业务代码、Alembic、Agent 状态图：未动
- ⚠️ 未实测：DNS 未指过来、防火墙未放行、ingress 没启过、整链路未跑通 → 由你接力（见 §四）

---

## 三、你在 Cloudflare 要做的事

### 3.1 加 2 条 DNS A 记录

| 域名 | Type | Name | IPv4 | Proxy |
|---|---|---|---|---|
| `3gpp-everything.org` | `A` | `@` | `<服务器公网 IP>` | **DNS only**（灰云） |
| `dangdangdiary.org` | `A` | `@` | `<服务器公网 IP>` | **DNS only**（灰云） |

> 必须灰云。Cloudflare 免费版橙云代理对 SSE 长连接有 100s 空闲断风险，且 3gpp 项目有 SSE。

### 3.2 验证

```bash
dig +short 3gpp-everything.org @1.1.1.1     # 应返回 .env 里的 PUBLIC_IP
dig +short dangdangdiary.org @1.1.1.1        # 应返回 .env 里的 PUBLIC_IP
```

DNS 1-5 分钟生效。

---

## 四、你在服务器要做的事

### 4.1 防火墙放行（如果你用 ufw）

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status numbered
```

### 4.2 配置 ingress 项目

```bash
cd ~/infra/ingress
cp .env.example .env
vi .env
```

填写：

```env
TGPP_DOMAIN=3gpp-everything.org
DANGDANG_DOMAIN=dangdangdiary.org
LETSENCRYPT_EMAIL=<你的常用邮箱>
PUBLIC_IP=<服务器公网 IPv4>
CERTBOT_STAGING=1                # 第一次先 staging
```

### 4.3 3GPP 项目 .env 补一行

```bash
vi ~/3GPP-Everything/.env
# 在 ALLOWED_ORIGINS 末尾追加：
#   ,https://3gpp-everything.org
```

### 4.4 启动业务容器（必须先于 ingress，因为 ingress 要 attach 它们的 network）

```bash
# 1. 3GPP-Everything 业务容器
cd ~/3GPP-Everything
make down                              # 停 dev compose 让位
make web-build API_BASE_URL=/api/v1    # 准备前端静态
make prod-up                           # api + web 起来
docker network ls | grep tgpp_tgpp-net # 确认 network 存在

# 2. DangDangDiary 业务容器（先 down 再 up 让 dangdang-nginx 应用新的 expose 设置）
cd ~/DangDangDiary
docker compose down
docker compose up -d
docker network ls | grep dangdangdiary_default   # 确认 network 存在
```

### 4.5 启动 ingress + 签证书

```bash
cd ~/infra/ingress

# 第一次 staging（验证 ACME 链路）
./scripts/init-letsencrypt.sh

# 期望末尾看到：
#   ✓ 签发完成: 3gpp-everything.org
#   ✓ 签发完成: dangdangdiary.org
#   ✓ https://3gpp-everything.org/ 通
#   ✓ https://dangdangdiary.org/ 通

# Staging 通过后改 .env CERTBOT_STAGING=0
vi .env

# 重签真证书
./scripts/init-letsencrypt.sh

# 起 certbot 自动续期 daemon（init-letsencrypt 已起 nginx，但 certbot 需要单独 up）
docker compose up -d certbot

# 探活
./scripts/healthcheck.sh
```

### 4.6 浏览器验证（最关键）

打开浏览器：

| URL | 期望 |
|---|---|
| `https://3gpp-everything.org/` | 小绿锁（Let's Encrypt）+ Flutter 登录页 |
| `https://3gpp-everything.org/api/v1/health` | `{"status":"ok"}` |
| `https://dangdangdiary.org/` | 小绿锁 + dangdang 入口（无论 dangdang 前端长啥样，HTTP 200 就 OK） |

然后用你的 admin 账号登录 3gpp，发问 `What is HARQ?`，看：
- Chrome DevTools Network 找 `/api/v1/sessions/.../messages` → `Type: eventstream`，逐条 event 到达
- token 持续流式累积到气泡，不卡顿、不一次性喷

---

## 五、常见故障

| 现象 | 原因 | 处置 |
|---|---|---|
| `init-letsencrypt.sh` 报 "DNS 未解析" | DNS 未生效 | 等 5 分钟；`dig` 验证 |
| ACME challenge 失败 | 80 端口外网不通 | `curl -I http://3gpp-everything.org/nginx-health` 应得 200；不通查防火墙 |
| `rate limited` | Let's Encrypt 限流 | 先 staging；prod 锁 7 天 |
| ingress-nginx 启动报 "network tgpp_tgpp-net not found" | 业务项目没起 | 先 `cd ~/3GPP-Everything && make prod-up` |
| 浏览器 ERR_CERT_AUTHORITY_INVALID | 用了 staging 证书 | `.env` 改 CERTBOT_STAGING=0 重跑 init |
| SSE 卡 100s 后断 | 中间代理 idle | 确认 Cloudflare 是灰云（非橙云）；本项目两层 nginx 都已关 buffering + 600s timeout |
| 网页加载白屏 | web 容器或 ingress 链路问题 | `docker logs tgpp-web`；`docker logs ingress-nginx` |
| dangdang 外部 app 连不上了 | 客户端还在用 `http://IP/`（HTTP） | 改成 `https://dangdangdiary.org/`；ingress 没有 HTTP 上的 dangdang 反代（只有 301） |

---

## 六、进 M8 的硬门禁

- [ ] 两个域名 DNS 都已生效（`dig` 验证）
- [ ] 防火墙 80/443 放行
- [ ] xray 已停 + disabled（上一步骤）
- [ ] dangdang-nginx 改成 expose（已由 Agent 改）+ `docker compose up -d` 应用
- [ ] tgpp 业务容器起来（`make prod-up`）
- [ ] ingress `init-letsencrypt.sh` 跑通（staging + prod 各一次）
- [ ] `cd ~/infra/ingress && docker compose ps` 看到 nginx + certbot 都 Up
- [ ] 浏览器分别访问两个域名，HTTPS 小锁正常
- [ ] 3gpp 端 SSE 流式问答跑通（按 `pre-m8-validation-guide.md §2 场景 3`）
- [ ] 失败回滚演练 1 次（停 ingress → 起 → 验证）
- [ ] live eval × 2 次（按 `pre-m8-validation-guide.md §4`，把 `EVAL_BACKEND_BASE_URL` 换成 `https://3gpp-everything.org`）

---

## 七、Agent 自主决策记录

1. **新建独立 ingress 项目而非寄生在某业务项目下**：路径 `~/infra/ingress/`（你已选过），方便日后扩展（监控、备份等同类基础设施）
2. **ingress 通过 external network 直连业务容器**：而不是 `host.docker.internal:port` 暴露+反代。优点：业务容器不暴露任何宿主端口，安全且端口干净
3. **3GPP web 不再暴露 8082**：prod 走 ingress，dev 仍可 `make dev` 暴露 8082
4. **SSE 处理只在 ingress 这一层**：之前外层 + 内层都关 buffering 的双保险方案被简化，因为：
   - 业务项目的 `frontend/nginx/default.conf` 也已经关了 buffering
   - 实际有效的是浏览器看到的第一层（ingress）；双关其实是冗余
5. **dangdang nginx.conf 一行不改**：只改它的 `docker-compose.yml` 端口暴露方式，最小耦合
6. **HSTS 默认关闭**：稳定运行 ≥ 1 周后由你手动加 `add_header Strict-Transport-Security ...`；过早开启会被旧浏览器缓存绑死
7. **Qdrant snapshot 不自动备份**：保持 3GPP-Everything 项目 backup.sh 的原决策

---

## 八、不在本次范围

- ❌ Cloudflare 橙云代理（SSE 风险）
- ❌ 多域名 SAN 证书（每个域名独立证书更清晰）
- ❌ www 子域名（要可通过加 `-d www.${DOMAIN}` 单独签）
- ❌ HSTS preloading
- ❌ Prometheus / Grafana 监控
- ❌ GHCR + GitHub Actions 自动部署（M8 阶段本地 build 即可）
- ❌ DangDangDiary 客户端 base URL 切换（dangdang 项目自己的事；建议你同时让 app 支持 `https://dangdangdiary.org/` 灰度切换，避免一刀切）

---

## 九、回滚到旧架构（应急）

如果新架构出问题需要回到 dev 模式临时救火：

```bash
# 1. 停 ingress
cd ~/infra/ingress && docker compose down

# 2. 停 prod 业务容器
cd ~/3GPP-Everything && make prod-down

# 3. dangdang 临时改回 publish 80（编辑 docker-compose.yml expose → ports）
cd ~/DangDangDiary && vi docker-compose.yml   # 改 expose 回 ports
docker compose down && docker compose up -d

# 4. 起 3GPP dev compose
cd ~/3GPP-Everything && make dev
```

dangdang 现状回来：`http://<服务器公网 IP>/` 可达。
3gpp dev：`http://localhost:8082/`（仅本机）。
