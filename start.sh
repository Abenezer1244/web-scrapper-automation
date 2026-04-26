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

if [ "$RAILWAY_SERVICE_NAME" = "worker" ]; then
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
  echo "Starting Celery beat scheduler..."
  exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
else
  echo "Starting API server..."
  # Run Alembic migrations from the API service only. Worker + beat share
  # the same DB — running upgrade-head from multiple replicas at once
  # races; Alembic's own locking will serialize within ONE service but
  # cross-service races (api + worker booting simultaneously) can deadlock
  # on long migrations. The API service is the natural single-replica
  # serializer. If the upgrade fails the deploy fails fast (set -e on the
  # combined command) instead of starting uvicorn against a stale schema.
  echo "Running alembic upgrade head..."
  alembic upgrade head || { echo "alembic upgrade head failed; refusing to start API"; exit 1; }
  echo "Migrations applied."
  exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
