# M4 起：完整运行时依赖（FastAPI + LangGraph + LlamaIndex + Qdrant client + ...）
# 用 `uv sync --frozen` 从 pyproject + uv.lock 还原依赖，保证镜像与本地 dev 一致。
# 项目源码在 dev compose 里以 volume 形式 mount 进来（`../backend:/app`），
# 所以这里不安装本项目自身（`--no-install-project`），仅准备依赖；prod 部署可去掉该 flag。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# uv 比 pip 快 10x+；在镜像里仅作 dep installer 用，不污染运行时
RUN pip install --no-cache-dir "uv>=0.4"

WORKDIR /app

# 单独 COPY 依赖描述，让 deps 安装层可被缓存（源码改动不会重装依赖）
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project --no-dev

# 源码会被 dev compose 覆盖；保留 COPY 以便 prod 直接 build 也能跑
COPY . .

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--reload"]
