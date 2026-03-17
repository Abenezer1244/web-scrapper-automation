# BridgeLeads — System Architecture

---

## Architectural Constraints

1. **Scraping is I/O-bound, slow, and unreliable** — must be async and isolated from the API
2. **County portals can change at any time** — system must degrade gracefully without losing data
3. **Multi-tenancy requires row-level isolation from day one** — retrofitting this is painful and dangerous
4. **Live log stream and scraping workers must be fully decoupled** — a crashing worker cannot take down the API
5. **Playwright/Selenium workers are memory-hungry** (~500MB per browser instance) — they need isolated containers

---

## System Topology

### Client Layer
- **Next.js frontend** — deployed on Vercel, CDN edge, talks to FastAPI via HTTPS
- **Developer API** — same FastAPI, authenticated via API keys (hashed, shown once)
- **Email delivery** — Resend or SendGrid, triggered by job completion webhook internally
- **User webhooks** — HTTPS POST to user-configured URLs on job completion

### API Layer
- **FastAPI gateway** — single entry point for all clients
  - JWT auth (NextAuth.js tokens) + API key auth
  - Rate limiting per user/plan tier
  - Job CRUD endpoints
  - SSE endpoint for live log streaming
  - Multi-tenant RLS enforcement (belt + suspenders with PostgreSQL RLS)
  - PgBouncer connection pooling in front of PostgreSQL

### Storage Layer
- **PostgreSQL (Supabase)** — users, jobs, results, scraper configs, county connectors, job logs
- **Redis (Upstash)** — Celery task queue + Pub/Sub for SSE log stream + response caching
- **S3 / Cloudflare R2** — completed export files (CSV, Excel, JSON), served via signed URLs

### Worker Layer
- **Celery worker pool** — consumes jobs from Redis queue, dispatches to correct scraper
- **Static scraper** — requests + BeautifulSoup, fastest path, used for static HTML portals
- **Playwright scraper** — headless Chromium, handles JS-rendered SPAs and form-based portals (ARMS Web + ATIP)
- **Enrichment pipeline** — parcel lookup, skip tracing, AVM — runs after scraping, updates results in-place

---

## Job State Machine

Every job transitions through these states. All transitions logged to `job_logs`.

```
PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
                                    ↘                ↘ FAILED
                                                     ↘ CANCELLED
```

| State | Description | Who Sets It |
|-------|-------------|-------------|
| PENDING | Job created, not yet queued | FastAPI on POST /jobs |
| QUEUED | Pushed to Redis, waiting for worker | FastAPI after DB write |
| PROBING | Worker probing site render mode | Celery worker |
| SCRAPING | Pages being scraped, records streaming in | Celery worker |
| ENRICHING | Parcel lookups and enrichment running | Celery worker |
| DONE | All records written, export uploaded to S3 | Celery worker |
| FAILED | Unrecoverable error after max retries | Celery worker / watchdog |
| CANCELLED | User cancelled via API or dashboard | FastAPI on DELETE |

**Watchdog:** A separate Celery beat task runs every 5 minutes. Any job stuck in SCRAPING or ENRICHING for >30 minutes is moved to FAILED and re-queued (up to `max_retries`).

---

## Database Schema

### users
```sql
CREATE TABLE users (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email            TEXT UNIQUE NOT NULL,
  password_hash    TEXT,
  api_key_hash     TEXT,
  plan             TEXT DEFAULT 'starter',
  records_used     INT DEFAULT 0,
  records_limit    INT DEFAULT 50,
  stripe_customer_id TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
);
```

### scraper_configs
```sql
CREATE TABLE scraper_configs (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID REFERENCES users(id) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  county           TEXT NOT NULL,
  state            TEXT NOT NULL,
  record_type      TEXT NOT NULL,
  fields           JSONB NOT NULL,
  enrichment       JSONB DEFAULT '[]',
  schedule         JSONB NOT NULL,
  deliver          JSONB DEFAULT '{}',
  active           BOOLEAN DEFAULT true,
  created_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON scraper_configs(user_id);
```

### jobs
```sql
CREATE TABLE jobs (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            UUID REFERENCES users(id) ON DELETE CASCADE,
  scraper_config_id  UUID REFERENCES scraper_configs(id),
  status             TEXT DEFAULT 'pending',
  trigger            TEXT DEFAULT 'manual',
  page_current       INT DEFAULT 0,
  page_total         INT,
  record_count       INT DEFAULT 0,
  export_key         TEXT,
  error_message      TEXT,
  retry_count        INT DEFAULT 0,
  started_at         TIMESTAMPTZ,
  finished_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON jobs(user_id, created_at DESC);
CREATE INDEX ON jobs(status) WHERE status IN ('pending','queued','scraping','enriching');
```

### results
```sql
CREATE TABLE results (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id            UUID REFERENCES jobs(id) ON DELETE CASCADE,
  user_id           UUID REFERENCES users(id),
  date_recorded     DATE,
  party_name        TEXT,
  heirs             TEXT[],
  legal_description TEXT,
  parcel_id         TEXT,
  property_address  TEXT,
  mailing_address   TEXT,
  enrichment_data   JSONB DEFAULT '{}',
  raw_html_hash     TEXT,
  created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON results(job_id);
CREATE INDEX ON results(user_id, date_recorded DESC);
CREATE INDEX ON results(parcel_id);
```

### county_connectors
```sql
CREATE TABLE county_connectors (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  county         TEXT NOT NULL,
  state          TEXT NOT NULL,
  record_types   TEXT[] NOT NULL,
  scraper_class  TEXT NOT NULL,
  render_mode    TEXT NOT NULL,
  base_url       TEXT NOT NULL,
  health_status  TEXT DEFAULT 'ok',
  last_checked   TIMESTAMPTZ,
  UNIQUE(county, state)
);
```

### job_logs
```sql
CREATE TABLE job_logs (
  id         BIGSERIAL PRIMARY KEY,
  job_id     UUID REFERENCES jobs(id) ON DELETE CASCADE,
  level      TEXT NOT NULL,
  message    TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON job_logs(job_id, id ASC);
```

### Row Level Security
```sql
ALTER TABLE scraper_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE results ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_isolation ON scraper_configs
  USING (user_id = current_setting('app.current_user_id')::uuid);

CREATE POLICY user_isolation ON jobs
  USING (user_id = current_setting('app.current_user_id')::uuid);

CREATE POLICY user_isolation ON results
  USING (user_id = current_setting('app.current_user_id')::uuid);
```

---

## Key Architectural Decisions

### Decision 1 — Row Level Security from Day One
Every table with a `user_id` gets a PostgreSQL RLS policy. Even buggy queries that forget `WHERE user_id = ?` are safe. Multi-tenant data leaks are company-ending. Not optional.

### Decision 2 — Store Results in DB, Not Export-Only
Store every record in DB for deduplication (`raw_html_hash`), cross-run search, incremental exports, and future lead scoring/CRM sync. Cost: ~500 bytes/record × 5,000 records/month/user = trivial.

### Decision 3 — `county_connectors` as a Registry Table
Each county scraper is a DB row, not hardcoded Python. Adding a county = DB insert + Python file, not a code deployment. `health_status` + `last_checked` power the canary monitoring system.

### Decision 4 — Log Stream via Redis Pub/Sub
Celery worker publishes log lines to a Redis channel keyed by `job_id`. FastAPI SSE endpoint subscribes and streams to browser. Decoupled: worker crash doesn't affect log stream.

### Decision 5 — `user_id` Denormalized onto `results`
Technically redundant but RLS policy needs it for single-table scan. Without it, every query hits two tables for auth. At 10M rows, this matters significantly.

---

## Live Log Stream — SSE Implementation

```python
# FastAPI SSE endpoint
@router.get("/jobs/{job_id}/logs")
async def stream_logs(job_id: str, current_user: User = Depends(get_current_user)):
    job = await db.get_job(job_id, user_id=current_user.id)
    if not job:
        raise HTTPException(404)

    async def event_generator():
        # Replay existing logs first (client reconnect support)
        existing = await db.get_job_logs(job_id)
        for log in existing:
            yield f"data: {log.json()}\n\n"

        # Subscribe to Redis channel for new logs
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"job_logs:{job_id}")

        async for message in pubsub.listen():
            if message['type'] == 'message':
                yield f"data: {message['data']}\n\n"
            if job.status in ('done', 'failed', 'cancelled'):
                break

    return EventSourceResponse(event_generator())
```

```python
# Celery worker — publishing logs
def log(job_id: str, level: str, message: str):
    entry = {"timestamp": datetime.utcnow().isoformat(), "level": level, "message": message}
    db.insert_job_log(job_id, level, message)
    redis.publish(f"job_logs:{job_id}", json.dumps(entry))
```

---

## Failure Modes and Mitigations

| Failure | Detection | Recovery |
|---------|-----------|----------|
| County portal HTML changes | Canary job returns 0 records | Alert Slack + freeze job + notify user |
| Celery worker OOM crash | Job stuck in SCRAPING >30min | Watchdog → FAILED + re-queue |
| ATIP rate limited (HTTP 429) | Exception in enrichment | Exponential backoff, partial results saved |
| PostgreSQL pool exhausted | FastAPI 503s | PgBouncer in front of Postgres |
| S3 upload fails | export_key not written | Retry 3x, fall back to email attachment |
| User hits record limit mid-job | Checked at job creation | Soft limit: job completes, overage flagged |
| Playwright CAPTCHA | Page detection in scraper | Pause job, notify user, manual review |
| Redis goes down | Queue unavailable | Jobs queue in Postgres fallback table |

---

## Deployment Topology

| Service | Provider | Notes |
|---------|----------|-------|
| Frontend | Vercel | Global CDN, automatic deploys from main |
| FastAPI | Railway / Fly.io | 2 replicas minimum, auto-scale |
| Celery workers | Railway | Auto-scale 1–10, isolated from API |
| PostgreSQL | Supabase | Managed, RLS built-in, daily backups |
| Redis | Upstash | Serverless, pay-per-request |
| Object storage | Cloudflare R2 | S3-compatible, zero egress fees |
| Email | Resend | Transactional, 3,000 free/mo |

### Worker Sizing
- 1 Playwright worker = ~500MB RAM, 1 vCPU
- Start with 2 workers, auto-scale at queue depth >10
- Each worker handles 1 job at a time (browser is single-threaded)
- Target: <5 min job completion for 10 pages + enrichment

### Environment Variables
```
DATABASE_URL=postgresql://...
REDIS_URL=redis://...
S3_BUCKET=proppulse-exports
S3_REGION=auto
S3_ENDPOINT=https://...r2.cloudflarestorage.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
JWT_SECRET=...
STRIPE_SECRET_KEY=...
RESEND_API_KEY=...
PLAYWRIGHT_HEADLESS=true
CELERY_CONCURRENCY=1
MAX_RETRIES=3
JOB_TIMEOUT_MINUTES=30
```

---

## Canary Monitoring System

A Celery beat task runs every hour:

1. For each `county_connector` with `health_status != 'down'`
2. Run a 1-page test scrape with a known date range
3. If 0 records returned → set `health_status = 'degraded'`, alert Slack
4. If exception → set `health_status = 'down'`, alert Slack + pause all jobs for that connector
5. On recovery → set `health_status = 'ok'`, resume jobs

---

## Build Order (Backend)

- [ ] PostgreSQL schema + RLS policies + migrations (Alembic)
- [ ] FastAPI skeleton + auth (JWT + API key)
- [ ] Job CRUD endpoints
- [ ] Redis + Celery setup
- [ ] BaseScraper + PlaywrightScraper
- [ ] Pierce County ARMS scraper (Mike's use case)
- [ ] ATIP parcel enrichment pipeline
- [ ] SSE log stream endpoint
- [ ] S3 export upload + signed URL generation
