#!/bin/sh
# FastAPI container entrypoint: apply Alembic migrations, optionally seed demo data, then exec CMD.
# Django is started first (see docker-compose depends_on) because the seed reads Django-owned catalog tables.
set -e

echo "[fastapi] Applying database migrations…"
alembic upgrade head

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
    echo "[fastapi] Seeding demo customers, orders and reviews (idempotent)…"
    python -m app.scripts.seed || echo "[fastapi] Seed skipped/failed — continuing startup."
fi

exec "$@"
