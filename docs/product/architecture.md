# BridgeLeads — System Architecture (v2.0)

*Updated: March 2026 — reflects AI scraper, national enrichment, multi-county scale*

---

## Architectural Constraints

1. **3,100+ county websites, each unique** — must scrape ANY site without per-county code
2. **Scraping is I/O-bound and unreliable** — isolated from API, graceful degradation
3. **Multi-tenancy requires row-level isolation** — PostgreSQL RLS + query-level filtering
4. **Enrichment must be national** — can't build per-county integrations for 3,100 counties
5. **Playwright workers are memory-hungry** (~500MB per browser) — need isolated containers
6. **Live log streaming must be decoupled** — workers crash without taking down the API

---

## System Topology

### Client Layer
- **Next.js 16 frontend** — Vercel, CDN edge, talks to FastAPI via HTTPS
- **Developer API** — same FastAPI, authenticated via API keys (Business+ plans)
- **Email delivery** — Resend, triggered by job completion
- **User webhooks** — HTTPS POST on job completion (Business+ plans)

### API Layer
- **FastAPI gateway** — single entry point
  - JWT auth (NextAuth) + API key auth
  - Rate limiting per user/plan tier (Redis sliding window)
  - Job CRUD, scraper config CRUD, billing, admin
  - SSE endpoint for live log streaming
  - Multi-tenant RLS enforcement
  - `POST /scrapers/connectors` — add any county with just a URL (Agency plan)

### Storage Layer
- **PostgreSQL (Supabase)** — users, jobs, results, scraper configs, county connectors, job logs
- **Redis (Upstash)** — Celery queue + Pub/Sub for SSE + CAPTCHA token cache + AI action cache
- **Cloudflare R2** — export files (CSV, Excel, JSON), served via signed URLs

### Worker Layer
- **Celery worker pool** — consumes jobs from Redis, dispatches to correct scraper
- **Manual scraper** — hand-coded per-county (Pierce County probate)
- **AI scraper** — Claude API analyzes screenshots, navigates ANY county website
- **Enrichment task** — separate Celery task, runs AFTER scraping completes

### Enrichment Layer (NEW)
- **Primary: Regrid national API** — parcel → address for ALL US counties ($0.01-0.05/lookup)
- **Fallback: County-specific** — ATIP with 2Captcha for Pierce County
- **Circuit breaker** — marks source as down, skips remaining parcels

---

## Data Flow

```
User creates job via dashboard
  → API validates + creates Job record (status=pending)
  → Celery task dispatched to Redis queue
  → Worker picks up job
    → Registry resolves county → AIScraper or ManualScraper
    → Scraper: navigate site → fill form → extract records → paginate
    → Detail pages: click instruments → extract real parcel IDs
    → In-line enrichment: call Regrid API for each parcel
    → Save results to PostgreSQL
    → Export to CSV/Excel/JSON
    → Upload to R2
    → Send email delivery
  → Job status → done
  → Separate enrichment task for remaining parcels
```

---

## AI Scraper Architecture

```
county_connectors DB row (base_url, record_types)
  → AIScraper.__init__(base_url, county, state)
  → Step 1: Navigate to county portal
  → Step 2: Claude analyzes screenshot + DOM snapshot
  → Step 3: Claude returns JSON actions (click, fill, evaluate, wait)
  → Step 4: Playwright executes actions
  → Step 5: Repeat steps 2-4 (multi-step navigation)
  → Step 6: Claude extracts records from results page
  → Step 7: Claude handles pagination
  → Action cache: Redis (7-day TTL), replays on subsequent runs
```

**Adding a new county = one DB row.** Zero Python code.

---

## Enrichment Architecture (Cost-Optimized)

```
Parcel ID from any county
  → 1. County GIS REST API (FREE — ArcGIS, no auth, no CAPTCHA)
       GET .../FeatureServer/0/query?where=TaxParcelNumber='APN'&f=json
       ~60-70% of US counties have free ArcGIS endpoints
       $0.00 per lookup

  → 2. Regrid API (paid, if enabled — $375/mo)
       GET /api/v2/parcels/apn?parcelnumb=APN&token=TOKEN
       Works for ALL 3,100+ US counties
       $0.01-0.05 per lookup

  → 3. AI Assessor Scraper (Claude API — ~$0.01/lookup)
       Claude navigates county assessor website via Playwright
       Cached after first lookup (subsequent replays free)

  → 4. County-specific fallback (ATIP for Pierce, etc.)

  → 5. "(enrichment unavailable)"
```

Fallback chain (cheapest first):
1. County GIS REST API (free, fast, no auth)
2. Regrid API (paid, if enabled)
3. AI assessor scraper (Claude API, cached)
4. County-specific API (ATIP for Pierce, etc.)
5. "(enrichment unavailable)"

---

## Deployment Architecture

```
Railway (3 services, same Docker image, different start commands):
  ├── api:    uvicorn main:app (FastAPI)
  ├── worker: celery -A src.workers worker (job processing)
  └── beat:   celery -A src.workers beat (scheduler)

Vercel:
  └── bridgeleads-web (Next.js 16 frontend)

Supabase:
  └── PostgreSQL + RLS (BridgeLeads project, us-west-2)

Upstash:
  └── Redis (TLS, us-west-2)

Cloudflare:
  └── R2 bucket (exports), DNS (api.bridgeleads.io, app.bridgeleads.io)
```

---

## County Connector Registry

| Field | Purpose |
|-------|---------|
| `county` | Lowercase slug (e.g. "pierce") |
| `state` | 2-letter code (e.g. "WA") |
| `record_types` | JSON array: ["probate", "pre_foreclosure", ...] |
| `scraper_mode` | "ai" (Claude) or "manual" (hand-coded) |
| `base_url` | County portal URL |
| `scraper_class` | Python class path (manual mode only) |
| `health_status` | healthy / degraded / down / unknown |
| `active` | Boolean |

**Scale path**: WA (39 counties) → top 10 states (~500) → national (~3,100)

---

## Security Architecture

- **SSRF protection**: URL allowlist, block RFC1918/loopback/metadata IPs
- **CSV injection**: sanitize all exported fields
- **JWT**: HS256, 7-day expiry, jti blacklist on logout
- **API keys**: SHA256 hashed, shown once, Business+ only
- **RLS**: PostgreSQL row-level security on all user-scoped tables
- **Rate limiting**: Redis sliding window (auth: 10/min, jobs: 5/min, general: 60/min)
- **Brute force**: progressive lockout (5→1min, 10→5min, 20→30min, 50→24hr)
- **CAPTCHA detection**: AI scraper fails fast when site has reCAPTCHA
