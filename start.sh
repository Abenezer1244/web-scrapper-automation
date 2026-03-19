#!/bin/sh
# Route to the correct process based on Railway service name
case "${RAILWAY_SERVICE_NAME}" in
  worker)
    echo "Starting Celery worker..."
    exec celery -A src.workers worker --loglevel=info --concurrency=2
    ;;
  beat)
    echo "Starting Celery beat scheduler..."
    exec celery -A src.workers beat --loglevel=info --scheduler celery.beat.PersistentScheduler
    ;;
  *)
    echo "Starting API server..."
    exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
esac
