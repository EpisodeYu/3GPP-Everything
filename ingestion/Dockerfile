# M0 占位：完整依赖在 M1 ingestion POC 时再加。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir typer structlog

COPY . .

CMD ["python", "-c", "print('tgpp-ingestion image ready (M0 placeholder)')"]
