# BridgeLeads — Progress Report

**Date:** 2026-03-22
**Status:** Production — extracting real public records at scale

---

## What We Accomplished

### Records Extracted
| County | Records | Method | Cost | Time |
|--------|---------|--------|------|------|
| Spokane, WA | 5,653 | EagleWeb Template | $0 | 4 min |
| Kitsap, WA | 5,076 | EagleWeb Template | $0 | 4 min |
| Benton, WA | 4,528 | EagleWeb Template | $0 | 2.5 min |
| Jefferson, WA | 1,355 | EagleWeb Template | $0 | 1 min |
| Pierce, WA | 325 | Manual Scraper | $0 | 1 min |
| Pierce AI, WA | 25 | AI Scraper (Claude) | $0.10 | 1 min |
| Mason, WA | 14 | AI Scraper (Claude) | $0.10 | 1 min |
| **TOTAL** | **16,976** | | **~$0.20** | |

### Infrastructure Built (50+ commits)
1. **EagleWeb Template Scraper** — zero-cost scraper for Tyler Technologies EagleWeb sites (16+ WA counties)
   - 7-day date chunking (splits 90 days into small searches that redirect correctly)
   - `pressSequentially` for date inputs (triggers EagleWeb JS events)
   - `expect_navigation` for form submission tracking
   - JS-based record extraction from Description/Summary columns
   - Pagination across 50+ pages per chunk

2. **Free GIS Enrichment Pipeline** — $0/month (replaces $375/mo Regrid)
   - County GIS REST APIs (ArcGIS, free, no auth)
   - WA statewide parcel API (covers all 39 counties)
   - AI assessor fallback (Claude navigates assessor websites)
   - 5-tier priority: GIS → Regrid → AI Assessor → ATIP → unavailable

3. **Production Deployment**
   - Xvfb virtual display on Railway (headed Playwright)
   - Anti-headless detection (navigator.webdriver override)
   - Bulk DB insert (1000 rows/statement)
   - 60-min task timeout, 55-min watchdog
   - 5-county canary health checks per hour

4. **Frontend**
   - 50-state dropdown → county picker (39 WA counties active)
   - 4-step wizard: County → Fields → Schedule → Delivery
   - Live job streaming, results table with search + pagination
   - CSV/Excel/JSON download

5. **AI Scraper** — Claude-powered for any county website
   - Screenshot → Claude analysis → form navigation → record extraction
   - Action caching (7-day TTL) — subsequent runs replay free
   - Works on non-standard sites where template can't be used

---

## Current State

### Working Counties (7 of 39 WA)
Spokane, Kitsap, Benton, Jefferson, Pierce, Pierce AI, Mason

### Processing (8 counties — results coming in)
Grant, Island, Pacific, Thurston, Clark, Lewis, Whitman, Okanogan, Grays Harbor
Each expected: 2,000-5,000 records in ~4 min

### Enrichment
- GIS enrichment deployed for EagleWeb template
- Records with parcel IDs get property + mailing addresses automatically
- Confirmed working: "GINTER ORLAN ROSS EST OF" → PID 5670400260 → 3846 E HOWE ST

### Not Yet Working (11 counties)
| County | Platform | Issue |
|--------|----------|-------|
| Chelan | AcclaimWeb | Different Tyler platform, needs template |
| Pend Oreille | AcclaimWeb | Same as Chelan |
| Columbia | iDocMarket | Third-party service |
| San Juan | Custom | Digital Research Room |
| Whatcom | Custom | Digital Research Room |
| Cowlitz | Custom | County portal |
| Yakima | Fidlar Tapestry | Different platform |
| Lincoln | Tyler Self-Service | Different from EagleWeb |
| Stevens | Tyler Self-Service | Different from EagleWeb |
| Snohomish | LandmarkWeb | Requires account creation |
| King | LandmarkWeb | Has reCAPTCHA |

### Inactive (5 counties — no online portal)
Adams, Asotin, Franklin, Kittitas, Wahkiakum

### DNS Down (2 counties)
Clallam, Douglas

---

## Next Steps

### Priority 1: Enrichment Coverage
- Add detail page extraction for records missing parcel IDs
- Click into each EagleWeb record → extract parcel number → GIS lookup
- This will fill Property Address + Mailing Address for all records

### Priority 2: Non-EagleWeb Counties (11 counties)
- Build AcclaimWeb template (Chelan, Pend Oreille) — similar to EagleWeb
- Build Fidlar Tapestry template (Yakima)
- Debug AI scraper on custom portals (Columbia, San Juan, Whatcom, Cowlitz)
- Handle Tyler Self-Service (Lincoln, Stevens) — different UI from EagleWeb
- Snohomish: automate account creation or use alternative
- King: integrate 2Captcha for reCAPTCHA solving

### Priority 3: Expand to More States
- TX (top 5 investor counties) — research recorder portals
- FL (top 5 investor counties)
- CA (top 5 investor counties)
- Many states also use EagleWeb — template will work immediately

### Priority 4: Product Polish
- Fix R2 upload credentials (CSV export to cloud storage)
- Email delivery of results via Resend
- Stripe billing integration testing
- Clean up duplicate "Pierce" and "Pierce AI" connectors

---

## Architecture

```
User → Frontend (Vercel) → API (Railway) → Job Queue (Redis/Celery)
                                              ↓
                                         Worker (Railway)
                                              ↓
                                    ┌─────────┴──────────┐
                                    │                    │
                              EagleWeb Template    AI Scraper (Claude)
                              (16 WA counties)     (any county)
                                    │                    │
                                    └─────────┬──────────┘
                                              ↓
                                    GIS Enrichment (free)
                                    WA Statewide API
                                              ↓
                                    PostgreSQL (Supabase)
                                              ↓
                                    CSV/Excel/JSON Export
```

## Cost Structure
| Component | Monthly Cost |
|-----------|-------------|
| EagleWeb scraping (16+ counties) | $0 |
| GIS enrichment (all WA) | $0 |
| AI scraper (non-EagleWeb counties) | ~$5-10 |
| Railway hosting (API + Worker + Beat) | ~$20 |
| Supabase (PostgreSQL) | Free tier |
| Vercel (Frontend) | Free tier |
| **Total** | **~$25-30/mo** |
