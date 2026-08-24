# Multi-stage so the runtime image carries no build toolchain. The frontend is a separate image in
# frontend.Dockerfile; this image is deliberately API + scheduler only.
# Base images and the uv tool image use exact version tags plus immutable manifest digests.
# Dependabot proposes reviewed refreshes; release CI attaches an SBOM and provenance attestation to
# each resulting image.

# --- Stage 1: resolve Python dependencies into a venv ------------------------------------------
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aggregato/ ./aggregato/
COPY README.md ./
RUN uv sync --frozen --no-dev

# --- Stage 2: runtime — venv and code, no toolchain -------------------------------------------
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

RUN useradd --create-home --uid 10001 aggregato
COPY --from=deps /opt/venv /opt/venv
COPY --from=deps /app/aggregato /app/aggregato
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGGREGATO_DATA=/data

RUN mkdir -p /data && chown aggregato:aggregato /data
USER aggregato
VOLUME ["/data"]
EXPOSE 8000

# No shell form: the entrypoint supervises both processes and must receive signals directly.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
