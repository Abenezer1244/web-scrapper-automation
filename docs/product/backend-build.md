# BridgeLeads — Backend Build Summary

---

## What Was Built

Full production backend for BridgeLeads — 24 files, fully wired, ready to run with `docker-compose up`.

---

## File Inventory

```
proppulse/
├── .env.example              ← All env vars documented
├── Dockerfile                ← Python 3.12 + Playwright + Chromium
├── docker-compose.yml        ← postgres + redis + api + worker + beat + migrate
├── README.md                 ← Setup + API reference + county add guide
├── requirements.txt
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial.py    ← Full schema + RLS policies + Pierce County seed
├── src/
│   ├── config.py             ← Pydantic settings, plan limits, all env vars
│   ├── main.py               ← FastAPI app, CORS, route mounting
│   ├── db/
│   │   ├── models.py         ← 6 SQLAlchemy models
│   │   └── session.py        ← Async engine (FastAPI) + sync engine (Celery)
│   ├── api/
│   │   ├── auth.py           ← JWT + API key + bcrypt + plan enforcement
│   │   ├── schemas.py        ← All Pydantic request/response models
│   │   └── routes/
│   │       ├── auth.py       ← Register, login, /me, API key generation
│   │       ├── jobs.py       ← CRUD + SSE live log stream
│   │       └── scrapers.py   ← Scraper config CRUD
│   ├── scrapers/
│   │   ├── base.py           ← BaseScraper: Playwright + requests + retries
│   │   ├── pierce_county_probate.py  ← Mike's scraper (ARMS + ATIP)
│   │   └── registry.py       ← County connector registry
│   └── workers/
│       ├── tasks.py          ← Main Celery job: probe→scrape→enrich→export→deliver
│       ├── scheduler.py      ← Beat: dispatch, watchdog, canary, monthly reset
│       ├── exporter.py       ← CSV/Excel/JSON → S3/R2
│       └── delivery.py       ← Email via Resend
└── tests/
    └── test_backend.py       ← 14 tests: registry, scraper utils, exporter
```

---

## Key Implementation Decisions

### Auth (`src/api/auth.py`)

Two auth paths on the same `get_current_user` dependency:

- **JWT bearer token** — for the web app (NextAuth.js sessions)
- **API key** — for Business+ users calling the REST API directly

API keys are stored as `sha256(raw_key)`. The raw key is shown exactly once at creation. This matches the GitHub/Stripe pattern.

Plan enforcement via `require_plan(*plans)` dependency factory — use as `Depends(require_plan("business", "agency"))` on any route.

### Jobs (`src/api/routes/jobs.py`)

The SSE log stream endpoint (`GET /jobs/{id}/logs`) does two things:

1. Replays all existing `job_logs` rows immediately (client reconnect support)
2. Subscribes to Redis Pub/Sub channel `job_logs:{job_id}` for live events

This means a user who opens the live view mid-run sees everything from the beginning, not just new events.

Job cancellation checks status before allowing — can't cancel a DONE or FAILED job.

Record limit enforcement happens at job creation, not mid-run. Soft limit: if usage is at limit, job creation is blocked with HTTP 402. The job that runs to completion always completes.

### Pierce County Scraper (`src/scrapers/pierce_county_probate.py`)

Two-phase scrape:

**Phase 1 — ARMS Web (probate filings)**
- Playwright navigates to `armsweb.co.pierce.wa.us/SearchEntry.aspx`
- Fills form: document type = Probate, date range
- Paginates through results table
- Extracts: date, party name, parcel ID, legal description per row
- Hashes each row for deduplication

**Phase 2 — ATIP enrichment (parcel → address)**
- For each record with a parcel_id, calls ATIP REST API
- Falls back to Playwright UI scrape if API returns non-200
- Populates property_address and mailing_address
- 0.3s polite delay between requests

Parser uses heuristics not hardcoded column indices — ARMS column order can vary. Patterns: date regex, longest-text party name, 10-digit parcel ID pattern, legal description keywords.

### Celery Worker (`src/workers/tasks.py`)

Key Celery config:
- `task_acks_late=True` — task only acknowledged after completion, safe retry on worker crash
- `worker_prefetch_multiplier=1` — one task per worker (browsers are 500MB+)
- `task_soft_time_limit` — raises SoftTimeLimitExceeded before hard kill
- Max 3 retries with 30s cooldown

Job flow: PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE

Each state transition is written to both `jobs.status` (DB) and `job_logs` (DB + Redis Pub/Sub).

### Scheduler (`src/workers/scheduler.py`)

Four beat tasks:

| Task | Schedule | Purpose |
|------|----------|---------|
| `dispatch_scheduled_jobs` | Every minute | Check all active configs, enqueue if time matches |
| `watchdog_stuck_jobs` | Every 5 min | Re-queue or fail stuck jobs |
| `canary_check` | Every hour | Health-check county portals |
| `reset_monthly_usage` | 1st of month, midnight | Reset `records_used` to 0 |

Dispatch is idempotent — checks for existing job today before creating a new one.

### Exporter (`src/workers/exporter.py`)

Builds a pandas DataFrame from results, applies column labels (display-friendly headers), then writes to:
- **CSV:** `io.StringIO` → bytes, UTF-8
- **Excel:** `openpyxl` writer with custom header styles (amber on dark background)
- **JSON:** `orient="records"` for frontend consumption

Uploads to S3/R2 with `ContentDisposition: attachment` so browser downloads automatically.

Pre-signed URLs expire in 48 hours (email delivery) or 1 hour (in-app download).

---

## API Reference

### Auth
```
POST /auth/register    { email, password } → { access_token, user_id, plan }
POST /auth/login       { email, password } → { access_token, user_id, plan }
GET  /auth/me          → UserResponse
POST /auth/api-key     → { raw_key }  (Business+ only, shown once)
```

### Scrapers
```
GET    /scrapers         → ScraperConfig[]
POST   /scrapers         body: ScraperConfigCreate → ScraperConfigResponse
GET    /scrapers/{id}    → ScraperConfigResponse
DELETE /scrapers/{id}    → 204 (soft delete — sets active=false)
```

#### ScraperConfigCreate body
```json
{
  "name": "Pierce County Probate",
  "county": "pierce",
  "state": "wa",
  "record_type": "probate",
  "fields": ["date_recorded", "party_name", "heirs", "legal_description", "parcel_id", "property_address", "mailing_address"],
  "enrichment": ["parcel"],
  "schedule": { "frequency": "daily", "time": "06:00", "range_mode": "rolling_90" },
  "deliver": { "emails": ["mike@example.com"], "format": "csv" }
}
```

### Jobs
```
GET    /jobs                      → Job[]
POST   /jobs                      { scraper_config_id, date_from?, date_to? } → Job
GET    /jobs/{id}                 → Job
DELETE /jobs/{id}                 → 204
GET    /jobs/{id}/results         → ResultsPage (paginated, searchable)
GET    /jobs/{id}/logs            → SSE stream of LogLine events
```

#### SSE Event Types
```
event: log      { level, message, timestamp }
event: progress { page_current, page_total, record_count }
event: done     { status, record_count? }
```

---

## First Run — Mike's Pierce County Setup

```bash
# Start stack
docker-compose up --build

# Register Mike
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "mike@example.com", "password": "secure123"}'

# Save the access_token from response
TOKEN=<access_token>

# Create Pierce County Probate scraper
curl -X POST http://localhost:8000/scrapers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Pierce County Probate",
    "county": "pierce",
    "state": "wa",
    "record_type": "probate",
    "fields": ["date_recorded","party_name","heirs","legal_description","parcel_id","property_address","mailing_address"],
    "enrichment": ["parcel"],
    "schedule": {"frequency": "daily", "time": "06:00", "range_mode": "rolling_90"},
    "deliver": {"emails": ["mike@example.com"], "format": "csv"}
  }'

# Save config ID, then run it
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"scraper_config_id": "<CONFIG_ID>"}'

# Watch it live
curl -N http://localhost:8000/jobs/<JOB_ID>/logs \
  -H "Authorization: Bearer $TOKEN"
```

---

## Adding a New County

1. Create `src/scrapers/{county}_{state}_probate.py` extending `BaseScraper`
2. Implement `scrape(date_from, date_to) -> list[ScrapedRecord]`
3. Add to registry in `src/scrapers/registry.py`
4. Run: `INSERT INTO county_connectors (...) VALUES (...)`
5. Done — the scheduler, watchdog, and canary all pick it up automatically

---

## Test Suite

14 tests covering:
- Registry: lookup, case insensitivity, unknown raises, list supported
- ScrapedRecord: to_dict, defaults
- BaseScraper utilities: clean, make_hash, context manager
- Exporter: build_dataframe, to_csv, to_json, column order

Run: `pytest tests/`
