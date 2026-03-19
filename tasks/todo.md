# BridgeLeads — Master Build Plan

**Product:** Multi-tenant SaaS automating motivated seller lead generation for real estate investors
**Stack:** FastAPI + Celery + Supabase + Playwright + Next.js + Stripe + Cloudflare R2
**North star:** Records that lead to closed deals per customer per month
**Status:** Approved — ready to build

---

## Decisions (locked)

| Decision | Choice |
|----------|--------|
| Entry point | FastAPI app (CLI replaced) |
| Scraper engine | Playwright only (Selenium dropped) |
| Database | Supabase (PostgreSQL + RLS) |
| Billing | Stripe |
| County naming | `{county}_{state}_{record_type}.py` |
| Enrichment | County-agnostic `enrichment/parcel.py` module |

---

## Phase 1 — Foundation
> Repo structure, dependencies, database, config. Nothing works without this.

### 1.1 Dependencies & Docker
- [x] Update `requirements.txt` — add: fastapi, uvicorn, celery, redis, sqlalchemy, alembic, playwright, pydantic-settings, python-jose, bcrypt, boto3, resend, stripe, pandas, openpyxl
- [x] Remove selenium, webdriver-manager from `requirements.txt`
- [x] Create `Dockerfile` — Python 3.12 + Playwright + Chromium install
- [x] Create `docker-compose.yml` — redis + api + worker + beat + migrate services
- [x] Update `.env.example` — all vars: DATABASE_URL, REDIS_URL, S3_*, JWT_SECRET, STRIPE_SECRET_KEY, RESEND_API_KEY, PLAYWRIGHT_HEADLESS

### 1.2 Config
- [x] Rewrite `src/config/settings.py` — Pydantic BaseSettings, plan limits dict, all env vars
- [x] Add validator: raise if SECRET_KEY is default or < 32 chars
- [x] Add plan limits: `{starter: 50, pro: 500, business: 5000, agency: -1}`

### 1.3 Database Layer
- [x] Create `src/db/models.py` — 6 SQLAlchemy models:
  - `User` (id, email, password_hash, api_key_hash, plan, records_used, records_limit, stripe_customer_id)
  - `ScraperConfig` (id, user_id, name, county, state, record_type, fields, enrichment, schedule, deliver, active)
  - `Job` (id, user_id, scraper_config_id, status, trigger, page_current, page_total, record_count, export_key, error_message, retry_count, started_at, finished_at)
  - `Result` (id, job_id, user_id, date_recorded, party_name, heirs, legal_description, parcel_id, property_address, mailing_address, enrichment_data, raw_html_hash)
  - `CountyConnector` (id, county, state, record_types, scraper_class, render_mode, base_url, health_status, last_checked)
  - `JobLog` (id, job_id, level, message, created_at)
- [x] Create `src/db/session.py` — async engine for FastAPI + sync engine for Celery
- [x] Set up `alembic/` — `alembic init alembic`, configure `env.py`
- [x] Create `alembic/versions/001_initial.py`:
  - Full schema SQL
  - RLS policies on scraper_configs, jobs, results
  - Seed first county connector row (pierce, wa, probate)

---

## Phase 2 — API Layer
> FastAPI app, auth, security middleware, all routes.

### 2.1 FastAPI App
- [x] Replace `main.py` with FastAPI app:
  - CORS with explicit allowlist
  - Mount all routers
  - `docs_url=None` when `DEBUG=False`
  - `/health` endpoint

### 2.2 Schemas
- [x] Create `src/api/schemas.py` — all Pydantic models:
  - `UserRegister`, `UserLogin`, `UserResponse`
  - `ScraperConfigCreate`, `ScraperConfigResponse`
  - `JobCreate`, `JobResponse`, `ResultsPage`
  - `LogLine`, `ProgressEvent`

### 2.3 Auth
- [x] Create `src/api/auth.py`:
  - `get_current_user` dependency — accepts JWT bearer OR API key
  - `require_plan(*plans)` dependency factory
  - `create_secure_token()` — includes `jti`, `iss: bridgeleads`, `aud: bridgeleads-api`
  - `decode_secure_token()` — validates iss + aud
  - API key stored as `sha256(raw_key)`, raw shown once

### 2.4 Security Middleware
- [x] Create `src/api/middleware/rate_limit.py` — Redis sliding window:
  - Auth endpoints: 10 req/min
  - Job creation: 5 req/min
  - General: 60 req/min
- [x] Create `src/api/middleware/auth_hardening.py`:
  - `TokenBlacklist` — Redis-backed, `POST /auth/logout` blacklists `jti`
  - `BruteForceProtection` — per-IP + per-email lockout: 5→1min, 10→5min, 20→30min, 50→24hr
  - `constant_time_compare()` using `hmac.compare_digest()`
- [x] Create `src/api/middleware/security.py`:
  - `validate_scraping_target()` — HTTPS-only allowlist, block RFC1918 + link-local + metadata IPs
  - `sanitize_for_csv()` — prefix `=+-@\t\r` cells with single quote
  - Security headers middleware (HSTS, X-Frame-Options, X-Content-Type-Options)
  - Strip control chars from all log messages (log injection prevention)
  - Audit logger for auth events

### 2.5 Routes
- [x] Create `src/api/routes/auth.py`:
  - `POST /auth/register` — generic error on duplicate
  - `POST /auth/login` — generic error, always runs `verify_password()` (constant time)
  - `GET /auth/me`
  - `POST /auth/logout` — blacklist JWT jti
  - `POST /auth/logout-all` — revoke all tokens via timestamp
  - `POST /auth/api-key` — Business+ only, returns raw key once
- [x] Create `src/api/routes/scrapers.py`:
  - `GET /scrapers` — list user's configs
  - `POST /scrapers` — create, validate county exists in registry
  - `GET /scrapers/{id}`
  - `DELETE /scrapers/{id}` — soft delete (active=false)
- [x] Create `src/api/routes/jobs.py`:
  - `GET /jobs` — list user's jobs
  - `POST /jobs` — create, check record limit (HTTP 402 if exceeded)
  - `GET /jobs/{id}`
  - `DELETE /jobs/{id}` — cancel (only if PENDING/QUEUED/SCRAPING)
  - `GET /jobs/{id}/results` — paginated, searchable (escape LIKE wildcards, 100-char limit)
  - `GET /jobs/{id}/logs` — SSE stream: replay existing logs + subscribe Redis Pub/Sub

---

## Phase 3 — Scraper Engine
> Playwright-only base, first county connector, reusable enrichment pipeline.

### 3.1 Base Scraper (migrate from Selenium)
- [x] Rewrite `src/scrapers/base_scraper.py` — Playwright only:
  - `probe(url)` — requests.get first, returns `static` or `playwright`
  - `navigate(url)` — validates via `validate_scraping_target()` before any navigation
  - `get_soup_async()` — BeautifulSoup from Playwright page content
  - `make_hash(row_dict)` — md5 fingerprint for deduplication
  - `clean(text)` — strip control chars, normalize whitespace
  - Context manager: `async with BridgeScraper() as s:`
- [x] Delete `src/scrapers/example_scraper.py` (old Selenium example)

### 3.2 First County Connector
- [x] Create `src/scrapers/pierce_wa_probate.py`:
  - **Phase 1 — ARMS Web:**
    - Navigate `armsweb.co.pierce.wa.us/SearchEntry.aspx`
    - Fill form: document type = Probate, date range (from/to)
    - Wait for results table, paginate through all pages
    - Extract per row using heuristics (not hardcoded column indices):
      - Date: regex match `\d{1,2}/\d{1,2}/\d{4}`
      - Party name: longest text field
      - Parcel ID: 10-digit pattern
      - Legal description: keywords (LOT, BLOCK, SEC, TWP)
      - Heirs: all associated name fields
    - Hash each row for deduplication via `make_hash()`
  - **Phase 2 — ATIP Enrichment:**
    - Call `enrichment/parcel.py` for each record with parcel_id
    - 0.3s polite delay between requests
  - Returns `list[ScrapedRecord]`

### 3.3 Reusable Enrichment Pipeline
- [x] Create `src/scrapers/enrichment/parcel.py`:
  - `enrich_parcel(parcel_id, county, state) -> dict`
  - Phase 1: ATIP REST API (`atip.piercecountywa.gov/app/v2/parcelSearch/search`)
  - Fallback: Playwright UI scrape if API returns non-200
  - Returns: `{property_address, mailing_address}`
  - Exponential backoff on HTTP 429
  - New counties plug in their own lookup — same interface

### 3.4 Registry
- [x] Create `src/scrapers/registry.py`:
  - `get_scraper_class(county, state, record_type)` — looks up `county_connectors` DB table
  - `list_supported()` — returns all active connectors
  - Case-insensitive lookup
  - Raises `UnsupportedCountyError` for unknown connectors

---

## Phase 4 — Worker Layer
> Celery job state machine, scheduler, export, email delivery.

### 4.1 Celery Setup
- [x] Configure Celery in `src/workers/__init__.py`:
  - `task_acks_late=True`
  - `worker_prefetch_multiplier=1`
  - `task_soft_time_limit=1700` (28min), `task_time_limit=1800` (30min)
  - Redis as broker + backend

### 4.2 Main Job Task
- [x] Create `src/workers/tasks.py` — `run_scrape_job(job_id)`:
  - PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE/FAILED
  - Each transition: update `jobs.status` in DB + publish to Redis Pub/Sub `job_logs:{job_id}`
  - Max 3 retries, 30s cooldown
  - On DONE: upload export to S3/R2, update `jobs.export_key`
  - On FAILED: write human-readable `error_message` (never raw stacktrace)
  - Increment `users.records_used` on completion

### 4.3 Scheduler (4 Beat Tasks)
- [x] Create `src/workers/scheduler.py`:
  - `dispatch_scheduled_jobs` (every 1 min) — check active configs, enqueue if schedule matches, idempotent
  - `watchdog_stuck_jobs` (every 5 min) — jobs stuck >30min in SCRAPING/ENRICHING → FAILED + re-queue
  - `canary_check` (every 1 hr) — 1-page test scrape per active connector, set health_status
  - `reset_monthly_usage` (1st of month, midnight UTC) — reset all `users.records_used` to 0

### 4.4 Exporter
- [x] Extend `src/utils/data_exporter.py`:
  - Apply `sanitize_for_csv()` to all fields before DataFrame construction
  - Upload to Cloudflare R2 via boto3 (`ContentDisposition: attachment`)
  - Return signed URL: 48hr expiry (email delivery), 1hr (in-app download)
  - Formats: CSV (UTF-8), Excel (openpyxl, amber header style), JSON (orient=records)

### 4.5 Email Delivery
- [x] Create `src/workers/delivery.py`:
  - `deliver_job_results(job_id)` — called after successful export
  - Send via Resend API with signed download URL
  - Email template: clean, minimal, download CTA prominent
  - CC all addresses in `scraper_config.deliver.emails`

---

## Phase 5 — Stripe Billing ✅
> Plan enforcement, webhooks, usage tracking.

### 5.1 Stripe Integration
- [x] Create `src/api/routes/billing.py`:
  - `GET /billing/plans` — full plan catalog for frontend upgrade UI
  - `GET /billing/usage` — current records_used, records_limit, percent_used
  - `GET /billing/subscription` — live Stripe subscription details
  - `POST /billing/checkout` — create Stripe checkout session, auto-create Stripe customer
  - `POST /billing/portal` — Stripe customer portal URL
  - `POST /billing/webhook` — handle Stripe events:
    - `checkout.session.completed` → update `users.plan` + `users.records_limit`
    - `customer.subscription.updated` → handle upgrades/downgrades mid-cycle
    - `customer.subscription.deleted` → downgrade to starter
    - `invoice.payment_failed` → notify user via email
- [x] Add `_send_payment_failed_email(email, attempt_count)` to `src/workers/delivery.py`
- [x] Wire Business+ feature gating in `src/api/routes/scrapers.py`:
  - `webhook_url` in deliver config → HTTP 402 for Starter/Pro
  - `skip_tracing` in enrichment list → HTTP 402 for Starter/Pro
- [x] `GET /scrapers/connectors` — public county browser endpoint (no auth required)
- [x] HTTP 402 on job creation when `records_used >= records_limit` (Phase 4, jobs.py)

---

## Phase 6 — DevOps & Infrastructure ✅
> CI/CD, containers, monitoring, production deploy.

### 6.1 CI/CD
- [x] Create `.github/workflows/ci-cd.yml`:
  - PR to main: test only
  - Push to staging: test → build → deploy staging
  - Push to main: test → build → migrate DB → deploy production
  - Services: PostgreSQL + Redis for test runs
  - Linting: ruff
  - Coverage upload to Codecov
- [x] Create `.github/dependabot.yml` — weekly Python + npm dependency updates

### 6.2 Infrastructure as Code
- [x] Create `infra/nginx/api.bridgeleads.io.conf`:
  - Rate limiting zones (auth, jobs, general)
  - SSE: `proxy_buffering off`, `chunked_transfer_encoding on`, 300s timeout
  - TLS 1.2/1.3, HSTS
- [x] Create `infra/terraform/main.tf`:
  - Cloudflare DNS (api., app. subdomains)
  - Cloudflare WAF rules (block bots, challenge auth floods, block SQLi)
  - Cloudflare R2 bucket (WNAM region)

### 6.3 Monitoring
- [x] Create `monitoring/prometheus.yml` — scrape FastAPI, Celery, Redis, Postgres exporters
- [x] Create `monitoring/alerts.yml` — 9 rules:
  - APIDown (critical), CeleryWorkersDown (critical)
  - QueueDepthHigh, HighJobFailureRate, DBPoolExhausted, RedisMemoryHigh
  - DiskSpaceLow, APIHighLatency, CountyConnectorDown
- [x] Create `docker-compose.prod.yml` — full stack + Prometheus + Grafana + Loki + Promtail + Flower

### 6.4 Deployment Config
- [x] Create `railway.toml` — API + worker + beat services
- [x] Create `pyproject.toml` — ruff config + pytest settings + coverage config
- [x] Create `scripts/bootstrap.sh` — verify env vars + run migrations + seed data

---

## Phase 7 — Tests ✅
> No mocks. Real DB, real files, real behavior.

- [x] Create `tests/conftest.py` — shared fixtures: db, client, redis, user factories, scraper/job factories
- [x] Rewrite `tests/test_settings.py` — Pydantic BaseSettings, PLAN_LIMITS, SECRET_KEY validator, CORS helper
- [x] Rewrite `tests/test_data_exporter.py` — CSV injection (parametrized), column ordering, all 3 formats, dispatch
- [x] Create `tests/test_auth.py`:
  - Register, login, /me
  - Generic error on bad credentials (no enumeration)
  - Brute force lockout after 5 failures
  - JWT blacklist after logout
  - API key generation (Business+ only), API key auth
- [x] Create `tests/test_jobs.py`:
  - List, get, create jobs
  - HTTP 402 when record limit reached
  - Unlimited plan (-1) never blocked
  - Cancel only valid statuses (done/failed → 400)
  - SSE replays existing logs and terminates on done
- [x] Create `tests/test_scrapers.py`:
  - Registry lookup, case insensitivity, unsupported raises
  - `make_hash()` deduplication, key-order invariance
  - `validate_scraping_target()` blocks RFC1918, loopback, metadata IPs, HTTP
  - `sanitize_for_csv()` blocks all formula injection prefixes
- [x] Create `tests/test_workers.py`:
  - Exporter: dataframe column order, CSV injection sanitized, CSV/Excel/JSON real files
  - Watchdog: stuck job re-queued, max retries → failed, recent job untouched
  - Monthly reset: records_used → 0 for all users
  - Delivery: soft-fails gracefully with fake API key

---

## Phase 8 — Frontend (Next.js)
> Dark command-center UI. Time to first CSV < 5 minutes.

### 8.1 Project Setup
- [x] Project already scaffolded at `bridgeleads-web/` with all deps installed
- [x] `app/globals.css` — full CSS custom properties + Google Fonts + scrollbar + keyframes
- [x] `tailwind.config.ts` — extended design tokens
- [x] `lib/types.ts` — all TypeScript types
- [x] `lib/api.ts` — full FastAPI client with auth
- [x] `lib/auth.ts` — NextAuth CredentialsProvider config
- [x] `next.config.ts` — serverActions + image domains
- [x] `.env.local` — local env vars

### 8.2 Auth Pages
- [x] `app/(auth)/login/page.tsx` — email + password, redirect to dashboard
- [x] `app/(auth)/register/page.tsx` — email + password + confirm, Zod validation
- [x] `lib/auth.ts` — NextAuth JWT strategy, FastAPI credentials provider

### 8.3 Dashboard Shell
- [x] `app/layout.tsx` — root layout with fonts + providers
- [x] `components/providers.tsx` — client-side SessionProvider + QueryClientProvider + TooltipProvider
- [x] `app/(dashboard)/layout.tsx` — sidebar with 5 nav items, running indicator, plan badge

### 8.4 Today Screen (home)
- [x] `app/(dashboard)/page.tsx` — greeting, 3 stat cards, job run list
- [x] `components/stat-card.tsx` — animated stat card
- [x] `components/run-card.tsx` — job run card with status dot + actions

### 8.5 Scraper Wizard (4 steps)
- [x] `app/(dashboard)/scrapers/page.tsx` — list of scraper configs
- [x] `app/(dashboard)/scrapers/new/page.tsx` — 4-step wizard with RHF + Zod

### 8.6 Live Run View
- [x] `app/(dashboard)/live/[id]/page.tsx` — live job view with progress + logs
- [x] `hooks/use-log-stream.ts` — SSE hook with reconnect
- [x] `components/log-stream.tsx` — color-coded log viewer

### 8.7 Results & Export
- [x] `app/(dashboard)/results/[id]/page.tsx` — results table + download

### 8.8 Settings
- [x] `app/(dashboard)/settings/page.tsx` — Account / Billing / API Keys tabs

### 8.9 Deliver
- [x] `app/(dashboard)/deliver/page.tsx` — delivery settings overview

---

## Phase 9 — Production Launch ✅ (infrastructure built — awaiting manual deploy)
> Deploy, verify, activate Mike.
> Full step-by-step guide: `docs/deployment/launch-checklist.md`

### 9.1 Pre-Launch Checklist
- [x] `scripts/bootstrap.sh` — verifies all env vars + waits for DB + runs migrations
- [x] `infra/terraform/main.tf` — Cloudflare DNS + WAF + R2 (run `terraform apply`)
- [x] `.github/workflows/ci-cd.yml` — staging + production deploy pipelines
- [ ] **MANUAL:** Set all env vars in Railway (see `docs/deployment/launch-checklist.md` Step 6)
- [ ] **MANUAL:** Set GitHub secrets (Step 7)
- [ ] **MANUAL:** Run `terraform apply` for DNS (Step 8)
- [ ] **MANUAL:** Push to `staging` → verify CI passes
- [ ] **MANUAL:** Push to `main` → production live
- [ ] **MANUAL:** Verify `/health` returns 200

### 9.2 First Customer (Mike)
- [x] `scripts/onboard_customer.py` — one-command account + scraper + first run
- [ ] **MANUAL:** `python scripts/onboard_customer.py --email mike@... --run-now`
- [ ] **MANUAL:** Send Mike Stripe checkout link for Pro plan
- [ ] **MANUAL:** Verify CSV arrives in Mike's email

### 9.3 Monitoring
- [x] `infra/grafana/dashboards/bridgeleads.json` — pre-built dashboard (API latency, job success rate, queue depth, connector health)
- [x] `infra/grafana/provisioning/` — auto-provisioned on container start
- [x] `infra/alertmanager/alertmanager.yml` — Slack routing (critical + warning channels)
- [ ] **MANUAL:** Set `SLACK_WEBHOOK_URL` + create `#bridgeleads-alerts` channel
- [ ] **MANUAL:** Enable Supabase PITR backups

---

## Phase 10 — Go-to-Market (Month 2–3)
> Community infiltration, not paid ads.

- [ ] Record Mike's success story video — "I wake up to leads now"
- [ ] Post case study in WA state RE investor Facebook groups
- [ ] Offer free trials to group admins
- [ ] Reach out to top RE wholesaling YouTube channels (free Business access for honest review)
- [ ] Set up referral system: refer a paying customer → 1 free month
- [ ] Content SEO: "How to find probate leads in [County], [State]" — one article per county supported

---

## Phase 11 — Scale (Month 3–9)
> More counties, more record types, more states.

### 11.1 WA State Expansion
- [ ] Add King County, WA (probate)
- [ ] Add Snohomish County, WA (probate)
- [ ] Add Clark County, WA (probate)
- [ ] Add Spokane County, WA (probate)
- [ ] Add remaining WA counties to reach 10 total
- [ ] Add pre-foreclosure record type (all WA counties)
- [ ] Add tax delinquent record type
- [ ] Add divorce filings record type

### 11.2 Business Tier Features
- [ ] REST API with API key auth (already built, unlock for Business+)
- [ ] Webhook delivery on job completion
- [ ] Zapier integration
- [ ] Skip tracing enrichment (BatchData or similar — phone + email append)

### 11.3 Multi-State Expansion
- [ ] Texas (top 5 investor counties)
- [ ] Florida (top 5 investor counties)
- [ ] California (top 5 investor counties)
- [ ] Ohio, Pennsylvania, Georgia, North Carolina, Arizona, Michigan, Colorado
- [ ] Migrate to `browserless.io` shared pool ($50/mo, 20 concurrent browsers)

---

## Phase 12 — Enterprise (Month 10–12)
> Agency tier, AI extraction, CRM integrations.

- [ ] White-label / agency tier (multi-client dashboard, custom domain)
- [ ] **AI-powered extraction — Claude API for any county URL, no selector maintenance** ← IN PROGRESS
- [ ] CRM integrations: Podio, HubSpot, InvestorFuse, Follow Up Boss
- [ ] Zapier + Make.com native integrations
- [ ] AVM (automated valuation model) enrichment
- [ ] Lead scoring (property value × estate complexity × days since filing)
- [ ] Series A prep: $2M+ ARR, metrics deck, investor pipeline

---

## Phase 12A — AI-Powered Extraction (Claude API)

### The Problem
Every county has a unique website with different:
- URLs, disclaimers, login flows
- Form controls (dropdowns vs checkboxes, date pickers, custom JS widgets)
- Results table HTML structure (different column names, layouts, pagination)

The current approach requires a hand-coded `{county}_{state}_{type}.py` file per county,
which took hours of debugging just for Pierce County. This doesn't scale to 3,000+ US counties.

### The Solution
Replace per-county selector code with a **Claude-powered AI agent** that:
1. Navigates to any county public records URL
2. Takes a page screenshot + accessibility snapshot
3. Asks Claude to identify the form fields and how to fill them
4. Executes Claude's instructions via Playwright
5. Extracts structured records from the results using Claude vision/HTML analysis

### Architecture

```
Current flow:
  county_connectors DB → get_scraper_class() → PierceWAProbateScraper (hand-coded)

New flow:
  county_connectors DB → get_scraper_class() → AIScraper (universal)
    ├── Step 1: Navigate to base_url
    ├── Step 2: Screenshot + snapshot → Claude "navigate" prompt
    ├── Step 3: Execute Claude's actions (click, fill, submit)
    ├── Step 4: Screenshot results page → Claude "extract" prompt
    ├── Step 5: Claude returns structured JSON → ScrapedRecord[]
    └── Step 6: Paginate (Claude identifies next page control)
```

The `county_connectors` DB table already has `base_url` and `record_types` —
that's all Claude needs to figure out the rest.

### Todo

#### 12A.1 — Dependencies & Config ✅
- [x] Add `anthropic` to `requirements.txt`
- [x] Add `ANTHROPIC_API_KEY` to `settings.py` and `.env.example`
- [x] Add `AI_MODEL` setting (default: `claude-sonnet-4-6`)
- [x] Add `AI_MAX_TOKENS` setting (default: 4096)
- [x] Add `AI_SCRAPER_ENABLED` feature flag (default: false)

#### 12A.2 — Claude Client Module ✅
- [x] Create `src/scrapers/ai/client.py`:
  - `async def ask_claude(system_prompt, user_message, images=[]) -> dict`
  - Sends screenshot bytes + text to Claude API
  - Handles rate limits (429 → exponential backoff)
  - Logs token usage for cost tracking
  - Returns dict with text, input_tokens, output_tokens, cost_usd

#### 12A.3 — AI Navigation Agent ✅
- [x] Create `src/scrapers/ai/navigator.py`:
  - `async def ai_navigate_form(page, base_url, record_type, date_from, date_to) -> None`
  - **Step 1:** Navigate to `base_url`, handle disclaimers/terms
  - **Step 2:** Take screenshot + get page accessibility snapshot
  - **Step 3:** Send to Claude with prompt:
    ```
    You are a browser automation agent. The screenshot shows a county public
    records search portal. I need to search for {record_type} records from
    {date_from} to {date_to}.

    Analyze the page and return a JSON array of actions to fill and submit
    the search form. Each action is one of:
    - {"action": "click", "selector": "...", "description": "..."}
    - {"action": "fill", "selector": "...", "value": "...", "description": "..."}
    - {"action": "check", "selector": "...", "description": "..."}
    - {"action": "select", "selector": "...", "value": "...", "description": "..."}
    - {"action": "wait", "ms": 1000}

    Rules:
    - Use CSS selectors or accessible roles
    - If there's a disclaimer/terms page, include accept actions first
    - Always end with a submit/search action
    - Include waits after page-changing actions
    ```
  - **Step 4:** Parse Claude's JSON response
  - **Step 5:** Execute each action via Playwright
  - **Step 6:** Verify results loaded (screenshot → Claude confirmation)

#### 12A.4 — AI Record Extractor ✅
- [x] Create `src/scrapers/ai/extractor.py`:
  - `async def ai_extract_records(page) -> list[ScrapedRecord]`
  - Takes screenshot + full HTML of results page
  - Sends to Claude with prompt:
    ```
    Extract all public records from this page as a JSON array.
    Each record should have these fields (null if not found):
    - date_recorded: string (MM/DD/YYYY)
    - party_name: string
    - heirs: string (comma-separated if multiple)
    - legal_description: string
    - parcel_id: string (numeric)
    - property_address: string
    - mailing_address: string

    Return ONLY valid JSON. No markdown, no explanation.
    ```
  - Parses Claude's JSON → list[ScrapedRecord]
  - Validates each record (date format, non-empty party_name or parcel_id)

#### 12A.5 — AI Pagination Handler ✅
- [x] Create `src/scrapers/ai/paginator.py`:
  - `async def ai_has_next_page(page) -> bool`
  - `async def ai_go_next_page(page) -> None`
  - Screenshot → Claude: "Is there a next page button? If yes, return the selector."
  - Executes click if found, returns False if no next page

#### 12A.6 — AIScraper Class (Unified) ✅
- [x] Create `src/scrapers/ai_scraper.py`:
  - Extends `BridgeScraper`
  - Implements `scrape(date_from, date_to) -> list[ScrapedRecord]`
  - Orchestrates: navigator → extractor → paginator loop
  - Falls back to hand-coded scraper if AI extraction fails
  - Tracks Claude API cost per job (store in `job_logs`)

#### 12A.7 — Registry Integration ✅
- [x] Update `county_connectors` table:
  - Add `scraper_mode` column: `"manual"` (hand-coded) | `"ai"` (Claude-powered)
  - Default: `"ai"` for new counties, `"manual"` for existing Pierce County
- [ ] Update `registry.py`:
  - If `scraper_mode == "ai"`, return `AIScraper` instead of the hand-coded class
  - Pass `base_url` and `record_types` to `AIScraper.__init__()`
- [ ] Alembic migration for the new column

#### 12A.8 — Admin: Add Any County (no code) ✅
- [x] Create API endpoint `POST /scrapers/connectors`:
  - Accepts: `county`, `state`, `record_types`, `base_url`
  - Creates `county_connectors` row with `scraper_mode="ai"`
  - No Python code needed — Claude handles the rest
- [ ] Update `GET /scrapers/connectors` to include `scraper_mode`

#### 12A.9 — Cost Controls ✅
- [x] Track Claude API usage per job:
  - Input tokens, output tokens, cost estimate
  - Store in `job_logs` with level="ai_usage"
- [ ] Add plan-based AI limits:
  - Starter: 5 AI scrape jobs/month
  - Pro: 50 AI scrape jobs/month
  - Business: 500
  - Agency: unlimited
- [ ] Add `AI_COST_ALERT_THRESHOLD` setting (default: $10/day)

#### 12A.10 — Prompt Cache / Learning ✅
- [x] Create `src/scrapers/ai/cache.py`:
  - After a successful AI scrape, cache the navigation actions for that `base_url`
  - On subsequent runs, try cached actions first → only call Claude if they fail
  - Store in Redis with TTL of 7 days
  - This turns a ~$0.05/run Claude call into a ~$0.00/run cached replay
  - If cached actions fail, re-run with Claude and update the cache

### Build Order (strict)
```
1. requirements.txt + settings.py + .env.example  (12A.1)
2. src/scrapers/ai/client.py                       (12A.2)
3. src/scrapers/ai/navigator.py                    (12A.3)
4. src/scrapers/ai/extractor.py                    (12A.4)
5. src/scrapers/ai/paginator.py                    (12A.5)
6. src/scrapers/ai_scraper.py                      (12A.6)
7. registry.py update + migration                  (12A.7)
8. Admin endpoint                                  (12A.8)
9. Cost controls                                   (12A.9)
10. Prompt cache                                   (12A.10)
```

### Cost Estimate
- Claude Sonnet per page: ~2K input tokens (screenshot) + ~500 output tokens
- Navigation: 1 call (~$0.01)
- Extraction per page: 1 call (~$0.01)
- Pagination check: 1 call per page (~$0.005)
- **Total per job (10 pages): ~$0.15–$0.25**
- With prompt cache: **~$0.01–$0.03** (cached navigation replays free)

---

## Final Directory Structure

```
web-scrapper-automation/  (BridgeLeads backend)
├── main.py                        ← FastAPI app entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml             ← dev stack
├── docker-compose.prod.yml        ← prod stack + monitoring
├── railway.toml
├── pyproject.toml                 ← ruff + pytest
├── .env.example
├── .github/
│   ├── workflows/ci-cd.yml
│   └── dependabot.yml
├── alembic/
│   ├── env.py
│   └── versions/001_initial.py
├── infra/
│   ├── nginx/api.bridgeleads.io.conf
│   └── terraform/main.tf
├── monitoring/
│   ├── prometheus.yml
│   └── alerts.yml
├── scripts/
│   └── bootstrap.sh
├── src/
│   ├── config/
│   │   └── settings.py            ← Pydantic BaseSettings
│   ├── db/
│   │   ├── models.py              ← 6 SQLAlchemy models
│   │   └── session.py             ← async + sync engines
│   ├── api/
│   │   ├── auth.py                ← JWT + API key + plan enforcement
│   │   ├── schemas.py             ← all Pydantic schemas
│   │   ├── middleware/
│   │   │   ├── rate_limit.py
│   │   │   ├── auth_hardening.py
│   │   │   └── security.py
│   │   └── routes/
│   │       ├── auth.py
│   │       ├── scrapers.py
│   │       ├── jobs.py
│   │       └── billing.py
│   ├── scrapers/
│   │   ├── base_scraper.py        ← Playwright only
│   │   ├── pierce_wa_probate.py   ← first connector
│   │   ├── enrichment/
│   │   │   └── parcel.py          ← county-agnostic enrichment
│   │   └── registry.py
│   ├── utils/
│   │   ├── data_exporter.py       ← CSV/Excel/JSON + S3
│   │   └── logger.py
│   └── workers/
│       ├── tasks.py               ← Celery job state machine
│       ├── scheduler.py           ← 4 beat tasks
│       └── delivery.py            ← Resend email
├── tests/
│   ├── test_settings.py
│   ├── test_data_exporter.py
│   ├── test_auth.py
│   ├── test_jobs.py
│   ├── test_scrapers.py
│   └── test_workers.py
├── docs/
│   └── product/                   ← 8 reference docs
└── tasks/
    └── todo.md                    ← this file

bridgeleads-web/  (Next.js frontend — separate repo)
├── app/
│   ├── (auth)/login + register
│   └── (dashboard)/
│       ├── layout.tsx             ← sidebar shell
│       ├── page.tsx               ← Today screen
│       ├── scrapers/new           ← 4-step wizard
│       ├── results/[id]           ← export screen
│       ├── live/[id]              ← SSE live view
│       └── settings/
├── components/
│   ├── run-card, stat-card
│   ├── log-stream, live-preview-table
│   └── ui/ (shadcn)
├── hooks/
│   ├── use-log-stream.ts          ← SSE hook
│   └── use-jobs.ts
└── lib/
    ├── api.ts                     ← FastAPI client
    └── types.ts
```

---

## Build Sequence (strict order)

```
1.  requirements.txt + Dockerfile + docker-compose.yml
2.  src/config/settings.py
3.  src/db/models.py + session.py
4.  alembic/ setup + 001_initial migration
5.  src/api/schemas.py
6.  src/api/middleware/ (rate_limit, auth_hardening, security)
7.  src/api/auth.py
8.  src/api/routes/auth.py
9.  src/api/routes/scrapers.py
10. src/api/routes/jobs.py (including SSE endpoint)
11. src/api/routes/billing.py
12. main.py (FastAPI app)
13. src/scrapers/base_scraper.py (Playwright migration)
14. src/scrapers/enrichment/parcel.py
15. src/scrapers/pierce_wa_probate.py
16. src/scrapers/registry.py
17. src/workers/tasks.py
18. src/workers/scheduler.py
19. src/utils/data_exporter.py (extend + S3)
20. src/workers/delivery.py
21. tests/
22. DevOps files (CI/CD, nginx, terraform, monitoring)
23. Next.js frontend
24. Stripe billing
25. Production deploy + Mike onboarding
```

---

## Review

*(To be filled after build is complete)*
