# Base images can be overridden (e.g. a registry mirror when Docker Hub rate-limits):
#   docker compose build --build-arg NODE_IMAGE=mirror.gcr.io/library/node:22-bookworm-slim \
#                        --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ---- Frontend build -------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Runtime (API and worker share this image) ---------------------------
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/frontend/dist \
    PATH="/app/backend/.venv/bin:$PATH"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY fixtures /app/fixtures
RUN useradd --create-home --uid 10001 demojo && mkdir -p /data && chown demojo:demojo /data
USER demojo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["demojo", "serve", "--host", "0.0.0.0", "--port", "8000"]
