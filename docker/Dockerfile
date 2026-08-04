# Multi-stage so the operator never installs Node and the runtime image carries no toolchain
# (research.md  — the mitigation for the SPA deviation recorded in plan.md Complexity Tracking).

# --- Stage 1: build the SPA -------------------------------------------------------------------
FROM node:22.23.2-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm install --global npm@12.0.2 --silent && npm --loglevel=error ci
COPY frontend/ ./
RUN npm --loglevel=error run build

# --- Stage 2: resolve Python dependencies into a venv ----------------------------------------
FROM python:3.13-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aggregato/ ./aggregato/
COPY README.md ./
RUN uv sync --frozen --no-dev

# --- Stage 3: runtime — venv, code, built assets, no toolchain -------------------------------
FROM python:3.13-slim AS runtime

RUN useradd --create-home --uid 10001 aggregato
COPY --from=deps /opt/venv /opt/venv
COPY --from=deps /app/aggregato /app/aggregato
COPY --from=frontend /build/dist /app/static
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGGREGATO_DATA=/data \
    AGGREGATO_STATIC_DIR=/app/static

RUN mkdir -p /data && chown aggregato:aggregato /data
USER aggregato
VOLUME ["/data"]
EXPOSE 8000

# No shell form: the entrypoint supervises both processes and must receive signals directly.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
