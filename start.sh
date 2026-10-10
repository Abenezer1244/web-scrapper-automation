#!/bin/sh
echo "start.sh: RAILWAY_SERVICE_NAME=${RAILWAY_SERVICE_NAME:-unset}"

# ─── Worker concurrency (configurable via env) ──────────────────────────────
# WORKER_CONCURRENCY: how many parallel scrape tasks per worker instance.
# Each task runs Playwright/Chromium (~400-500MB), so scale with available RAM.
# Railway 8GB plan: set to 4. Railway 2GB plan: set to 2. Default: 2.
CONCURRENCY="${WORKER_CONCURRENCY:-2}"

# ─── Worker queue routing ────────────────────────────────────────────────────
# WORKER_QUEUES: which queues this worker consumes (comma-separated).
# Priority queue listed first = processed first when multiple jobs are waiting.
# "scrape-priority,scrape,enrichment" = default (paid users first)
# "scrape" = scrape jobs only
# "enrichment" = enrichment jobs only
QUEUES="${WORKER_QUEUES:-scrape-priority,scrape,enrichment}"

# ─── Schema gate (all services) ──────────────────────────────────────────────
# Every service must agree not to run against a stale schema. The API has always
# done this; worker and beat used to skip it and start on whatever schema was
# there. That is a real failure mode, not a theoretical one: adding a column to
# `users` makes every ORM `select(User)` name it, so a worker that boots on new
# code before the migration lands fails ordinary tenant queries, scrape execution
# included. Rolling deploys start these services independently, so "the API will
# have migrated by then" is a race, not a guarantee.
#
# ONE migrator: the API runs scripts/migrate.py (advisory locked, idempotent, so
# its replicas serialize) and fails CLOSED: serving against a stale schema is
# worse than not serving. Worker and beat never migrate and hold no DDL
# credential; scripts/wait_for_schema.py blocks their boot on the runtime role
# until the schema this code needs is in place, and fails CLOSED too (exit, so
# Railway restarts the boot) if it never arrives. They used to run migrate.py
# themselves and start anyway on failure. See docs/deployment/migrations.md.
run_migrations() {
  echo "Running migrations (advisory-locked) for API..."
  if python scripts/migrate.py; then
    echo "Migrations applied."
    return 0
  fi
  echo "migration run failed; refusing to start API"
  exit 1
}

wait_for_schema() {
  echo "Waiting for the schema this code needs (${1})..."
  if ! python scripts/wait_for_schema.py; then
    echo "schema never reached this code's head; refusing to start ${1}"
    exit 1
  fi
}

if [ "$RAILWAY_SERVICE_NAME" = "worker" ]; then
  wait_for_schema worker
  echo "Starting Celery worker (concurrency=$CONCURRENCY, queues=$QUEUES)..."

  # Expand /dev/shm for multiple Chromium instances (default 64MB is too small)
  if mount | grep -q '/dev/shm'; then
    mount -o remount,size=512M /dev/shm 2>/dev/null && echo "/dev/shm expanded to 512MB" || echo "/dev/shm remount skipped (no permission)"
  fi

  # Try to start Xvfb for headed Playwright (fixes EagleWeb JS redirects).
  if command -v Xvfb > /dev/null 2>&1; then
    export DISPLAY=:99
    Xvfb :99 -screen 0 1280x800x24 -ac -nolisten tcp > /dev/null 2>&1 &
    sleep 1
    if [ -n "$(pgrep Xvfb)" ]; then
      echo "Xvfb started (DISPLAY=$DISPLAY)"
    else
      echo "Xvfb failed to start, running in headless mode"
      unset DISPLAY
    fi
  else
    echo "Xvfb not available, running in headless mode"
  fi
  exec celery -A src.workers worker \
    --loglevel=info \
    --concurrency="$CONCURRENCY" \
    --queues="$QUEUES" \
    --hostname="worker-${RAILWAY_REPLICA_ID:-0}@%h" \
    --max-tasks-per-child=3
elif [ "$RAILWAY_SERVICE_NAME" = "beat" ]; then
  wait_for_schema beat
  echo "Starting Celery beat scheduler..."
  exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
else
  echo "Starting API server..."
  # The API runs MULTIPLE replicas and rolling deploys overlap old + new
  # instances, so two fresh replicas can race the same revision: one wins, the
  # loser's `UPDATE alembic_version WHERE version=<prev>` matches 0 rows and
  # Alembic aborts that boot. scripts/migrate.py serializes the runners behind a
  # PostgreSQL advisory lock (held on a direct, non-pgbouncer connection) so the
  # losers wait, then run a no-op upgrade and start cleanly.
  run_migrations
  exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
