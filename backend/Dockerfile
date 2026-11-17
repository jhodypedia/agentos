# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

FROM base AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1

COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM base AS runtime
RUN groupadd --gid 10001 agentos \
    && useradd --uid 10001 --gid agentos --create-home --shell /usr/sbin/nologin agentos \
    && mkdir -p /data/workspaces /data/skills \
    && chown -R agentos:agentos /data

WORKDIR /app
COPY --from=builder /app /app
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    WORKSPACE_ROOT=/data/workspaces \
    SKILLS_ROOT=/data/skills

USER agentos
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
