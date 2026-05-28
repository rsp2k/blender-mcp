#!/bin/sh
# Container entrypoint: run pending Alembic migrations BEFORE handing
# off to the app process. Tini (in CMD) still wraps the final process
# for proper SIGTERM forwarding.
#
# - alembic.ini is shipped at /app/alembic.ini (set by Dockerfile)
# - DATABASE_URL must be set (compose enforces this via :? on the password)
# - alembic upgrade head is idempotent — safe to re-run on every restart

set -e

if [ -n "$DATABASE_URL" ]; then
  echo "[entrypoint] running 'alembic upgrade head'..."
  alembic -c /app/alembic.ini upgrade head
  echo "[entrypoint] migrations complete"
else
  echo "[entrypoint] DATABASE_URL not set — skipping migrations (probably stdio mode)"
fi

# exec replaces the shell — tini in the parent Dockerfile CMD then
# becomes the actual PID 1.
exec "$@"
