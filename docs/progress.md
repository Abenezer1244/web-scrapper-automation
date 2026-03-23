# BridgeLeads — Progress Report

**Date:** 2026-03-23 (comprehensive update)
**Status:** Production SaaS — backend scraping live, frontend deployed, cached records system built

---

## Session Summary (2026-03-22 to 2026-03-23)

### What Was Built This Session

#### Backend — Scraper Fixes
- Fixed EagleWeb disclaimer (Playwright native click instead of JS el.click())
- Fixed AcclaimWeb single-date field handling (#RecordDate)
- Fixed Thurston EagleWeb variant (recordingDateIDStart/End date input IDs)
- Added "eagleweb." domain pattern to template detection
- Removed 5,000 record cap from template scrapers
- Removed 200 enrichment cap — all records now enriched
- Scaled to 4 replicas × 2 concurrency = 8 workers
- Fixed MaxClients DB error (routed sync engine through pgbouncer port 6543)
- Added --max-tasks-per-child=3 to prevent OOM
- Updated Anthropic API key (credits topped up)
- Triggered 32-county backfill with full enrichment

#### Backend — Cached Scraping System (NEW)
- **Design spec:** `docs/superpowers/specs/2026-03-23-cached-scraping-design.md`
- **Implementation plan:** `docs/superpowers/plans/2026-03-23-cached-scraping.md`
- **New DB tables:** `county_records` (shared cache) + `user_record_views` (per-user "new" badges)
- **New API endpoint:** `GET /scrapers/{config_id}/records` — serves cached records instantly with is_new flags
- **Daily scrape worker:** `src/workers/daily_scrape.py` — runs at 2 AM UTC, backfills 90 days for new counties
- **Beat tasks:** `scrape_county_daily` + `purge_old_records` (365-day retention)
- **Settings:** `ENABLE_DAILY_SCRAPE=true`, `RECORD_RETENTION_DAYS=365`
- **Atomic "new" badge:** SELECT FOR UPDATE + UPSERT pattern prevents race conditions
- **RLS policies:** county_records (shared read), user_record_views (user-only)
- **Verified E2E:** Benton County — 2,574 records served instantly, new badges working

#### Frontend — Landing Page (Phase 1)
- **Repo:** `bridgeleads-web` on GitHub + Vercel
- **URL:** https://bridgeleads-web.vercel.app/
- **Design spec:** `docs/superpowers/specs/2026-03-23-frontend-ui-ux-design.md`
- **Design system:** Generated via ui-ux-pro-max skill + taste-skill
- Premium dark landing page with:
  - Spline 3D animated background
  - Progressive gradient blur overlay (6-layer backdrop-filter)
  - Shimmer mask on headlines
  - Word-by-word text reveal animation
  - Section enter/exit blur transitions
  - Horizontal snap-scroll carousel
  - Glass card features with gradient icon containers
  - Timeline process section
  - 3-tier pricing with monthly/annual toggle
  - Urgency-driven CTA sections
  - Premium 4-column footer
- Route setup: `/` = public landing, `/dashboard` = protected app
- Vercel deployment protection disabled for public access

#### Frontend — Dashboard + Records (Phase 2)
- **Implementation plan:** `docs/superpowers/plans/2026-03-23-dashboard-records.md`
- **New types:** `CachedRecord`, `CachedResultsPage` in `lib/types.ts`
- **New API function:** `getCachedRecords()` in `lib/api.ts`
- **New components:** `NewBadge`, `ScraperCard`
- **Dashboard redesign:** Scraper config cards with new count badges + recent activity
- **Cached records page:** `/scrapers/[id]/records` with NEW badges, search, pagination

---

## Records Extracted (Confirmed Done)

| County | Records | Method | Cost |
|--------|---------|--------|------|
| Spokane, WA | 5,653 | EagleWeb Template | $0 |
| Kitsap, WA | 5,076 | EagleWeb Template | $0 |
| Benton, WA | 4,528 | EagleWeb Template | $0 |
| Grant, WA | 3,541 | EagleWeb Template | $0 |
| Island, WA | 3,194 | EagleWeb Template | $0 |
| Jefferson, WA | 1,413 | EagleWeb Template | $0 |
| Whitman, WA | 1,087 | EagleWeb Template | $0 |
| Pierce, WA | 325 | Manual Scraper | $0 |
| Pierce AI, WA | 25 | AI Scraper | $0.10 |
| Mason, WA | 14 | AI Scraper | $0.10 |
| **TOTAL** | **24,856** | **10 counties** | **~$0.20** |

**32-county backfill in progress** — 8 workers processing all active WA counties with full enrichment.

---

## Infrastructure Status

### Railway (Backend)
- **API:** `api.bridgeleads.io` — FastAPI, deployed
- **Worker:** 4 replicas × 2 concurrency = 8 workers
- **Beat:** Celery beat scheduler with 6 periodic tasks
- **Deploy:** Auto-deploy from GitHub push

### Vercel (Frontend)
- **URL:** https://bridgeleads-web.vercel.app/
- **Stack:** Next.js 16, React 19, Tailwind CSS 4, Framer Motion, shadcn/ui
- **Deploy:** Manual via `vercel deploy --prod`
- **Auth:** NextAuth v5 (JWT + Credentials)

### Database (Supabase)
- **Tables:** users, scraper_configs, jobs, results, job_logs, county_connectors, county_records, user_record_views
- **RLS:** Enabled on all tables
- **Connection:** pgbouncer (port 6543) for workers, direct (port 5432) for async API

### Key Environment Variables
- `ENABLE_DAILY_SCRAPE=true` — daily county scrape at 2 AM UTC
- `RECORD_RETENTION_DAYS=365` — auto-purge old records
- `WORKER_CONCURRENCY=2` — per replica
- `ANTHROPIC_API_KEY` — updated with funded key

---

## Architecture

```
Visitor → Landing Page (Vercel, public)
                ↓ Sign Up
User → Dashboard (Vercel, protected)
                ↓ GET /scrapers/{id}/records
        API (Railway) → county_records table (cached)
                            ↓ instant response + "NEW" badges
                            ↓ user_record_views tracks last_viewed_at

Nightly (2 AM UTC):
        Beat Scheduler → scrape_county_daily task
                            ↓ dispatches 1 task per county
        8 Workers → EagleWeb/AcclaimWeb/AI scrapers
                            ↓ records → county_records (ON CONFLICT DO NOTHING)
                            ↓ enrichment → GIS APIs (property + mailing address)
```

---

## Remaining Work

### Phase 3: Frontend (Not Started)
- Scraper creation wizard redesign
- Settings page redesign
- Auth pages (login/register) redesign
- Live job monitoring page redesign

### Backend
- Fix remaining failing counties (Tyler Self-Service, LandmarkWeb, custom portals)
- AcclaimWeb Kendo Grid extraction (Chelan shows data but extraction gets 0)
- Okanogan Tyler Self-Service disclaimer flow
- State expansion (TX, FL, CA)

### Product
- Stripe billing integration testing
- Email delivery via Resend
- R2 export upload fix (currently Unauthorized)
- Custom domain setup (bridgeleads.io)

---

## Key Files & Specs

| Document | Path |
|----------|------|
| UI/UX Design Spec | `docs/superpowers/specs/2026-03-23-frontend-ui-ux-design.md` |
| Cached Scraping Spec | `docs/superpowers/specs/2026-03-23-cached-scraping-design.md` |
| Cached Scraping Plan | `docs/superpowers/plans/2026-03-23-cached-scraping.md` |
| Landing Page Plan | `docs/superpowers/plans/2026-03-23-landing-page.md` |
| Dashboard Plan | `docs/superpowers/plans/2026-03-23-dashboard-records.md` |
| Design System | `design-system/bridgeleads/MASTER.md` |

## Cost Structure
| Component | Monthly Cost |
|-----------|-------------|
| EagleWeb + AcclaimWeb scraping | $0 |
| GIS enrichment (all WA) | $0 |
| AI scraper (non-template counties) | ~$5-10 |
| Railway hosting (API + 4 worker replicas + Beat) | ~$40 |
| Supabase (PostgreSQL) | Free tier |
| Vercel (Frontend) | Free tier |
| **Total** | **~$45-50/mo** |
