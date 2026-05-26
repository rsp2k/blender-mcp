# syntax=docker/dockerfile:1.7
# Multi-stage build for the blender-mcp FastMCP HTTP server.
#   build -> uv-managed venv with --extra oauth, frozen lock, no editable installs
#   prod  -> python-slim runtime, non-root, tini-wrapped, exposes :8000 /mcp + /health
#   dev   -> python-slim runtime, source mounted by compose, uvicorn --reload

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13-python3.13-bookworm-slim
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

# ---------------------------------------------------------------------------
# build stage — resolves deps into /app/.venv via uv sync
# ---------------------------------------------------------------------------
FROM ${UV_IMAGE} AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies-only layer: bind-mount lock + project metadata so this layer
# only invalidates when uv.lock or pyproject.toml changes, not on source edits.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --extra oauth

# Now copy source and install the project itself (non-editable for prod).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY addon/ ./addon/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-dev --extra oauth

# ---------------------------------------------------------------------------
# prod stage — minimal runtime, no uv, no build toolchain
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS prod

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

# tini reaps zombies and forwards SIGTERM to the python process so docker stop
# is graceful rather than a 10s SIGKILL fallback.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 app \
 && useradd --uid 1000 --gid app --home-dir /home/app --create-home --shell /bin/bash app

WORKDIR /app

COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/
COPY --chown=app:app addon/ ./addon/
COPY --chown=app:app pyproject.toml ./

USER app

EXPOSE 8000

# Slim image has no curl or wget; use stdlib urllib so we don't bloat the image
# just for healthchecks.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["blender-mcp"]

# ---------------------------------------------------------------------------
# dev stage — same runtime as prod, but source comes from a volume mount
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 app \
 && useradd --uid 1000 --gid app --home-dir /home/app --create-home --shell /bin/bash app

WORKDIR /app

# Venv only — compose mounts /app/src and /app/addon over the empty workdir.
COPY --from=build --chown=app:app /app/.venv /app/.venv

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "blender_mcp.oauth_server:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]
