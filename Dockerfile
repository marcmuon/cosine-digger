FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Cosine Digger" \
      org.opencontainers.image.description="Local-first music discovery with a taste map and daily/weekly scheduler" \
      org.opencontainers.image.source="https://github.com/marcmuon/cosine-digger" \
      org.opencontainers.image.licenses="AGPL-3.0"

RUN groupadd --gid 10001 digger \
    && useradd --uid 10001 --gid 10001 --create-home digger \
    && install -d -o 10001 -g 10001 -m 0700 /data
COPY --from=builder /app/.venv /app/.venv
WORKDIR /app
ENV COSINE_DIGGER_HOME=/data \
    COSINE_DIGGER_HOST=0.0.0.0 \
    COSINE_DIGGER_PORT=8765 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER 10001:10001
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json, os, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('COSINE_DIGGER_PORT', '8765') + '/api/health', timeout=3))['ok'] is True"
CMD ["cosine-digger", "serve"]
