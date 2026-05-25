# M5.6 — Flutter web 静态托管镜像。
#
# 锚：docs/03-development/05-frontend.md §0 M5.6 / §14；docs/03-development/07-cicd-and-deployment.md
#
# 设计：单阶段 nginx，假设 build/web/ 已由 host / CI `flutter build web --release` 产出。
# 不在镜像里塞 Flutter SDK 的原因：
# - cirruslabs/flutter 镜像 ~10GB（含 Android SDK），在 50GB / 受限磁盘的 dev box
#   上 docker pull 就 OOM。
# - CI 早已要装 Flutter 来跑 analyze/test/build，重复装两次没意义。
# - 镜像最终只 ~20-30MB（nginx + flutter web 静态产物），传输 / 部署都快。
#
# 用法：
#   # 1) Host 上先 build 产出 frontend/build/web/
#   make web-build                                              # 默认 API_BASE_URL=/api/v1
#   make web-build API_BASE_URL=https://api.example.com/api/v1  # 覆盖
#
#   # 2) 然后构建镜像
#   docker build -t tgpp-web frontend/
#
#   # 3) 运行
#   docker run --rm -p 8082:80 tgpp-web
#
# 生产：deploy/docker-compose.prod.yml 把本镜像 + nginx 反代组合（M8 落）。
FROM nginx:1.27-alpine

# build/web 是 `flutter build web --release` 的产物；通过 .dockerignore 把
# build/ 排除掉的 default 行为在这里被显式 unignore：见 frontend/.dockerignore
# 的 `!build/web` 例外（让 docker build 仍能拿到产物）。
COPY build/web /usr/share/nginx/html
COPY nginx/default.conf /etc/nginx/conf.d/default.conf

# nginx:alpine 自带 daemon off 入口 + EXPOSE 80；重写一次让镜像 inspect 清晰。
EXPOSE 80
