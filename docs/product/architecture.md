# BridgeLeads — System Architecture (v2.0)

*Updated: March 2026 — national enrichment, multi-county scale. 2026-10-01: AI mode removed; scraping is recorder-platform templates + hand-coded scrapers.*

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
- **Redis (Upstash)** — Celery queue + Pub/Sub for SSE + CAPTCHA token cache
- **Cloudflare R2** — export files (CSV, Excel, JSON), served via signed URLs

### Worker Layer
- **Celery worker pool** — consumes jobs from Redis, dispatches to correct scraper
- **Manual scraper** — hand-coded per-county (Pierce County probate)
- **Template scrapers** — one per recorder platform, picked by the connector's base_url
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

## Template Scraper Architecture

```
county_connectors DB row (scraper_mode='template', base_url, record_types)
  → registry._detect_template(base_url): match the URL to a recorder platform
       EagleWeb · AcclaimWeb · Tyler SelfService · LandmarkWeb · AVA Fidlar ·
       Laserfiche WebLink · Skagit recording · iDocMarket
  → that template's BridgeScraper subclass (Playwright, standardized selectors)
  → no match = UnsupportedCountyError (and POST /scrapers/connectors refuses the URL)

county_connectors DB row (scraper_mode='manual', scraper_class)
  → the allowlisted hand-coded scraper class
```

**Adding a county on a supported platform = one DB row.** Zero Python code.
(The mode was stored as 'ai' until migration 108, 2026-10-01; no LLM was involved.)

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

  → 3. County-specific fallback (ATIP for Pierce, etc.)

  → 4. "(enrichment unavailable)"
```

Fallback chain (cheapest first):
1. County GIS REST API (free, fast, no auth)
2. Regrid API (paid, if enabled)
3. County-specific API (ATIP for Pierce, etc.)
4. "(enrichment unavailable)"

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
| `scraper_mode` | "template" (platform template from base_url) or "manual" (hand-coded) |
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
- **CAPTCHA detection**: scrapers fail fast when a site has reCAPTCHA
