#!/usr/bin/env bash
# BridgeLeads bootstrap script
# Verifies required env vars, runs DB migrations, and seeds initial data.
# Run once after first deploy or when setting up a new environment.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[bootstrap]${NC} $*"; }
warn()    { echo -e "${YELLOW}[bootstrap]${NC} $*"; }
error()   { echo -e "${RED}[bootstrap]${NC} $*" >&2; }

# ─── Required env vars ────────────────────────────────────────────────────────

REQUIRED_VARS=(
    DATABASE_URL
    DATABASE_URL_SYNC
    REDIS_URL
    SECRET_KEY
    R2_ENDPOINT_URL
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET_NAME
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    STRIPE_PRICE_PRO
    STRIPE_PRICE_BUSINESS
    STRIPE_PRICE_AGENCY
    RESEND_API_KEY
    EMAIL_FROM
    FRONTEND_URL
)

info "Checking required environment variables..."
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        MISSING+=("$var")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    error "Missing required environment variables:"
    for var in "${MISSING[@]}"; do
        error "  - $var"
    done
    error "Copy .env.example to .env and fill in all values."
    exit 1
fi
info "All required environment variables are set."

# ─── SECRET_KEY length check ──────────────────────────────────────────────────

if [[ ${#SECRET_KEY} -lt 32 ]]; then
    error "SECRET_KEY must be at least 32 characters. Generate one with:"
    error "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    exit 1
fi
info "SECRET_KEY length OK (${#SECRET_KEY} chars)."

# ─── Wait for Postgres ────────────────────────────────────────────────────────

info "Waiting for PostgreSQL to be ready..."
MAX_ATTEMPTS=30
ATTEMPT=0
until python -c "
import psycopg2, os, sys
try:
    psycopg2.connect(os.environ['DATABASE_URL_SYNC'])
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    ATTEMPT=$((ATTEMPT + 1))
    if [[ $ATTEMPT -ge $MAX_ATTEMPTS ]]; then
        error "PostgreSQL did not become ready after ${MAX_ATTEMPTS} attempts."
        exit 1
    fi
    warn "PostgreSQL not ready, retrying in 2s... (attempt ${ATTEMPT}/${MAX_ATTEMPTS})"
    sleep 2
done
info "PostgreSQL is ready."

# ─── Wait for Redis ───────────────────────────────────────────────────────────

info "Waiting for Redis to be ready..."
ATTEMPT=0
until python -c "
import redis, os, sys
try:
    r = redis.from_url(os.environ['REDIS_URL'])
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    ATTEMPT=$((ATTEMPT + 1))
    if [[ $ATTEMPT -ge $MAX_ATTEMPTS ]]; then
        error "Redis did not become ready after ${MAX_ATTEMPTS} attempts."
        exit 1
    fi
    warn "Redis not ready, retrying in 2s... (attempt ${ATTEMPT}/${MAX_ATTEMPTS})"
    sleep 2
done
info "Redis is ready."

# ─── Run migrations ───────────────────────────────────────────────────────────

info "Running Alembic migrations..."
alembic upgrade head
info "Migrations complete."

# ─── Verify seed data ─────────────────────────────────────────────────────────

info "Verifying seed data..."
python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['DATABASE_URL_SYNC'])
cur = conn.cursor()
cur.execute(\"SELECT COUNT(*) FROM county_connectors WHERE active = true\")
count = cur.fetchone()[0]
conn.close()
if count == 0:
    print('WARNING: No active county connectors found. Run the seed migration.')
else:
    print(f'OK: {count} active county connector(s) found.')
"

info "Bootstrap complete. BridgeLeads is ready to run."
info ""
info "Start services:"
info "  API:    uvicorn main:app --host 0.0.0.0 --port 8000"
info "  Worker: celery -A src.workers worker --loglevel=info"
info "  Beat:   celery -A src.workers beat --loglevel=info"
