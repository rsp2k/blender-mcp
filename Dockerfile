# syntax=docker/dockerfile:1.7
# Multi-stage build for the blender-mcp FastMCP HTTP server.
#   build -> uv-managed venv with --extra oauth, frozen lock, EDITABLE install
#            (.venv/.../site-packages contains a .pth pointing at /app/src)
#   prod  -> python-slim runtime, non-root, tini-wrapped, exposes :8000 /mcp + /health
#   dev   -> python-slim runtime, source mounted by compose, uvicorn --reload
#
# Editable-install rationale (phase G6 follow-up):
#   The previous --no-editable build baked src/ into the venv as installed
#   wheel contents. That meant /app/src/ in the runtime image was dead
#   weight — Python imported from /app/.venv/lib/.../site-packages/blender_mcp/,
#   not from /app/src/blender_mcp/. Two consequences burned us:
#     1) "Lying grep": editing /app/src/blender_mcp/foo.py in a debug
#        session showed new code via grep but the running server still
#        imported the venv snapshot.
#     2) Slow `make prod`: a one-line src/ edit invalidated the build
#        stage's second `uv sync`, which rebuilt + reinstalled the wheel
#        (~30-60s). Editable install skips wheel construction entirely;
#        only the COPY src/ layer rebuilds (~2-5s).
#   With editable install, /app/src/ becomes the single source of truth
#   (the venv's .pth file points at it). Dev hot-reload starts working
#   correctly too — uvicorn --reload-dir /app/src watches the bind-mount,
#   process restarts, Python re-imports from the live bind-mount path.

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

# Now copy source and install the project EDITABLY. The .venv ends up with
# a .pth file in site-packages pointing at /app/src/blender_mcp/, so this
# build-stage path must match the runtime image's src/ path (we COPY src/
# to /app/src/ in the prod stage too).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY addon/ ./addon/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra oauth

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

# src/ is load-bearing now (editable install — .venv's .pth points here).
# addon/ is load-bearing for installation_manager._find_addon_source(),
# which looks for `addon/` at Path.cwd() = /app to auto-install into a
# target Blender instance.
# pyproject.toml is NOT needed at runtime — version metadata comes from
# /app/.venv/lib/python3.13/site-packages/blender_mcp-*.dist-info/.
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/
COPY --chown=app:app addon/ ./addon/

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
