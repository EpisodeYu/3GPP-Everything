# M0 placeholder: 真正的 Flutter web 构建在 05-frontend.md 阶段写入。
# 当前镜像仅起一个 nginx serve 静态占位页，保证 docker compose up 时 web 服务能起来。
FROM nginx:1.27-alpine

RUN echo '<!doctype html><meta charset="utf-8"><title>tgpp web</title><h1>3GPP-Everything web placeholder</h1><p>M0 infra check OK; frontend lands in M5.</p>' \
    > /usr/share/nginx/html/index.html

EXPOSE 80
