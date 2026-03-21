#!/bin/sh
echo "start.sh: RAILWAY_SERVICE_NAME=${RAILWAY_SERVICE_NAME:-unset}"

if [ "$RAILWAY_SERVICE_NAME" = "worker" ]; then
  echo "Starting Celery worker with Xvfb virtual display..."
  # Xvfb provides a virtual X display so Playwright can run in headed mode.
  # This fixes EagleWeb sites where headless mode breaks JS redirects.
  export DISPLAY=:99
  Xvfb :99 -screen 0 1280x800x24 -ac &
  sleep 1
  exec celery -A src.workers worker --loglevel=info --concurrency=2
elif [ "$RAILWAY_SERVICE_NAME" = "beat" ]; then
  echo "Starting Celery beat scheduler..."
  exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
else
  echo "Starting API server..."
  exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
