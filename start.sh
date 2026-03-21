#!/bin/sh
echo "start.sh: RAILWAY_SERVICE_NAME=${RAILWAY_SERVICE_NAME:-unset}"

if [ "$RAILWAY_SERVICE_NAME" = "worker" ]; then
  echo "Starting Celery worker..."
  # Try to start Xvfb for headed Playwright (fixes EagleWeb JS redirects).
  # If Xvfb fails, worker still runs in headless mode.
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
  # Concurrency=1 when using Xvfb (headed mode uses ~500MB per browser)
  exec celery -A src.workers worker --loglevel=info --concurrency=1
elif [ "$RAILWAY_SERVICE_NAME" = "beat" ]; then
  echo "Starting Celery beat scheduler..."
  exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
else
  echo "Starting API server..."
  exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
