#!/bin/sh
echo "start.sh: RAILWAY_SERVICE_NAME=${RAILWAY_SERVICE_NAME:-unset}"

if [ "$RAILWAY_SERVICE_NAME" = "worker" ]; then
  echo "Starting Celery worker..."
  exec celery -A src.workers worker --loglevel=info --concurrency=2
elif [ "$RAILWAY_SERVICE_NAME" = "beat" ]; then
  echo "Starting Celery beat scheduler..."
  exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
else
  echo "Starting API server..."
  exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
