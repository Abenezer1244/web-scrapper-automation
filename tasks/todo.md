# BridgeLeads — Evolution Plan

**Goal:** Evolve `web-scrapper-automation` into the full BridgeLeads backend SaaS.
**Status:** Approved — ready to build. All decisions locked, MCPs connected.

---

## What We're Keeping

| File | Keep As-Is | Notes |
|------|-----------|-------|
| `src/utils/logger.py` | Yes | Already solid colored logging |
| `src/utils/data_exporter.py` | Yes | CSV/JSON/Excel export works |
| `src/scrapers/base_scraper.py` | Migrate | Drop Selenium, migrate fully to Playwright |
| `src/config/settings.py` | Extend | Add all new env vars |
| `tests/` | Extend | Keep existing, add new |
| `.claude/` | Yes | Rules + agents stay |
| `docs/` | Yes | Product docs stay |

---

## What We're Adding

Everything needed to go from CLI scraper → production multi-tenant SaaS.

---

## Phase 1 — Foundation (do this first)

### 1.1 Repo restructure
- [ ] Keep `src/scrapers/` — add first county scraper + registry
- [ ] Add `src/api/` directory (FastAPI)
- [ ] Add `src/db/` directory (SQLAlchemy models + session)
- [ ] Add `src/workers/` directory (Celery tasks + scheduler)
- [ ] Update `requirements.txt` with all new dependencies
- [ ] Add `docker-compose.yml` (postgres + redis + api + worker + beat)
- [ ] Add `Dockerfile`
- [ ] Add `.env.example` with all vars documented

### 1.2 Database layer
- [ ] `src/db/models.py` — 6 SQLAlchemy models (users, scraper_configs, jobs, results, county_connectors, job_logs)
- [ ] `src/db/session.py` — async engine (FastAPI) + sync engine (Celery)
- [ ] `alembic/` — migrations setup
- [ ] `alembic/versions/001_initial.py` — full schema + RLS policies + first county connector seed row

### 1.3 Config
- [ ] Extend `src/config/settings.py` with Pydantic BaseSettings
- [ ] Add all env vars: DATABASE_URL, REDIS_URL, S3, JWT, STRIPE, RESEND
- [ ] Pydantic validator: raise if SECRET_KEY is default or < 32 chars

---

## Phase 2 — API Layer

### 2.1 FastAPI app
- [ ] `src/main.py` — FastAPI app, CORS, route mounting, docs hidden in prod

### 2.2 Auth
- [ ] `src/api/auth.py` — JWT + API key dependency, plan enforcement
- [ ] `src/api/middleware/auth_hardening.py` — token blacklist, brute force protection, constant-time compare
- [ ] `src/api/middleware/rate_limit.py` — Redis sliding window rate limiter
- [ ] `src/api/middleware/security.py` — SSRF firewall, CSV injection sanitizer, security headers, audit logger

### 2.3 Schemas
- [ ] `src/api/schemas.py` — all Pydantic request/response models

### 2.4 Routes
- [ ] `src/api/routes/auth.py` — register, login, /me, logout, logout-all, API key generation
- [ ] `src/api/routes/scrapers.py` — scraper config CRUD
- [ ] `src/api/routes/jobs.py` — job CRUD + SSE live log stream endpoint

---

## Phase 3 — Scraper Layer

### 3.1 Base scraper upgrade
- [ ] Migrate `src/scrapers/base_scraper.py` — drop Selenium entirely, rewrite with Playwright
- [ ] Add probe logic: requests first → fallback to Playwright if JS-rendered
- [ ] Add SSRF URL validation before any navigation

### 3.2 First county scraper (Phase 1 implementation)
- [ ] `src/scrapers/pierce_wa_probate.py` — first connector, validates the pattern
  - Playwright navigates ARMS Web (`armsweb.co.pierce.wa.us`)
  - Fills form: document type = Probate, date range
  - Paginates through results
  - Extracts: date, party name, parcel ID, legal description, heirs
  - Hashes each row for deduplication
- [ ] All future county scrapers follow identical interface — same `scrape()` signature, same `ScrapedRecord` output

### 3.3 Parcel enrichment pipeline (reusable across all counties)
- [ ] `src/scrapers/enrichment/parcel.py` — county-agnostic enrichment module
  - Accepts parcel ID + county/state → returns property_address + mailing_address
  - Phase 1 implementation: ATIP API (`atip.piercecountywa.gov`)
  - Falls back to Playwright UI if API returns non-200
  - 0.3s polite delay between requests
  - New counties plug in their own parcel lookup — same interface

### 3.4 Registry
- [ ] `src/scrapers/registry.py` — county connector registry (reads from `county_connectors` DB table)
  - Each county = one DB row + one Python file
  - Adding a county requires no changes to core infrastructure

---

## Phase 4 — Worker Layer

### 4.1 Celery setup
- [ ] `src/workers/tasks.py` — main job task: PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
  - `task_acks_late=True`
  - `worker_prefetch_multiplier=1`
  - Max 3 retries, 30s cooldown
  - Each state transition written to DB + Redis Pub/Sub

### 4.2 Scheduler
- [ ] `src/workers/scheduler.py` — 4 Celery Beat tasks:
  - `dispatch_scheduled_jobs` — every minute, idempotent
  - `watchdog_stuck_jobs` — every 5 min, re-queue or fail stuck jobs
  - `canary_check` — every hour, health-check county portals
  - `reset_monthly_usage` — 1st of month midnight

### 4.3 Exporter
- [ ] `src/workers/exporter.py` — extend existing `data_exporter.py`
  - Build DataFrame from results
  - Apply CSV injection sanitization
  - Upload to S3/R2, return signed URL (48hr expiry)

### 4.4 Delivery
- [ ] `src/workers/delivery.py` — email via Resend with signed download URL

---

## Phase 5 — DevOps

- [ ] `.github/workflows/ci-cd.yml` — test → build → staging → production
- [ ] `.github/dependabot.yml`
- [ ] `infra/nginx/api.proppulse.io.conf` — reverse proxy + SSE config
- [ ] `infra/terraform/main.tf` — Cloudflare DNS + WAF + R2
- [ ] `monitoring/prometheus.yml` + `monitoring/alerts.yml`
- [ ] `docker-compose.prod.yml` — full stack with Prometheus + Grafana + Loki
- [ ] `railway.toml`
- [ ] `pyproject.toml` — ruff + pytest + coverage
- [ ] `scripts/bootstrap.sh`

---

## Phase 6 — Tests

- [ ] Extend `tests/test_settings.py` — new env vars
- [ ] Extend `tests/test_data_exporter.py` — CSV injection sanitization
- [ ] `tests/test_auth.py` — register, login, JWT, API key, brute force
- [ ] `tests/test_jobs.py` — job CRUD, status transitions, record limits
- [ ] `tests/test_scrapers.py` — registry, hash dedup, SSRF validation
- [ ] `tests/test_workers.py` — exporter, delivery, watchdog logic

---

## Build Order (strict sequence)

```
1. requirements.txt + Dockerfile + docker-compose.yml
2. src/config/settings.py (extend)
3. src/db/models.py + session.py
4. alembic/ setup + 001_initial migration
5. src/api/schemas.py
6. src/api/auth.py + middleware/
7. src/api/routes/ (auth → scrapers → jobs)
8. src/main.py
9. src/scrapers/base.py (extend)
10. src/scrapers/pierce_county_probate.py
11. src/workers/tasks.py
12. src/workers/scheduler.py
13. src/workers/exporter.py (extend data_exporter)
14. src/workers/delivery.py
15. tests/
16. DevOps files
```

---

## Final Directory Structure (after evolution)

```
web-scrapper-automation/
├── .env.example
├── .github/
│   ├── workflows/ci-cd.yml
│   └── dependabot.yml
├── .claude/
│   ├── agents/
│   └── rules/
├── Dockerfile
├── docker-compose.yml
├── docker-compose.prod.yml
├── railway.toml
├── pyproject.toml
├── requirements.txt
├── alembic/
│   ├── env.py
│   └── versions/001_initial.py
├── infra/
│   ├── nginx/
│   └── terraform/
├── monitoring/
│   ├── prometheus.yml
│   └── alerts.yml
├── scripts/
│   └── bootstrap.sh
├── src/
│   ├── config/
│   │   └── settings.py       ← Extended
│   ├── db/
│   │   ├── models.py         ← New
│   │   └── session.py        ← New
│   ├── api/
│   │   ├── auth.py           ← New
│   │   ├── schemas.py        ← New
│   │   ├── middleware/       ← New
│   │   └── routes/           ← New
│   ├── scrapers/
│   │   ├── base_scraper.py        ← Migrated (Playwright only, Selenium dropped)
│   │   ├── pierce_wa_probate.py   ← Phase 1 connector (first of many)
│   │   ├── enrichment/
│   │   │   └── parcel.py          ← Reusable parcel lookup (county-agnostic)
│   │   └── registry.py            ← County connector registry (DB-driven)
│   ├── utils/
│   │   ├── data_exporter.py  ← Extended (S3 + sanitization)
│   │   └── logger.py         ← Keep as-is
│   ├── workers/
│   │   ├── tasks.py          ← New
│   │   ├── scheduler.py      ← New
│   │   └── delivery.py       ← New
│   └── main.py               ← Replace with FastAPI app
├── docs/
│   └── product/              ← All 8 docs
├── tasks/
│   └── todo.md               ← This file
└── tests/
    ├── test_settings.py      ← Extended
    ├── test_data_exporter.py ← Extended
    ├── test_auth.py          ← New
    ├── test_jobs.py          ← New
    ├── test_scrapers.py      ← New
    └── test_workers.py       ← New
```

---

## Decisions (locked)

1. **main.py** — replace entirely with FastAPI app. No separate CLI.
2. **base_scraper.py** — migrate fully to Playwright. Drop Selenium.
3. **Database** — Supabase for both local dev and production. No self-hosted Postgres.
4. **Billing** — Stripe. Wire up in Phase 1 so Mike pays $99/mo from day one.

---

## Review

*(To be filled after build is complete)*
