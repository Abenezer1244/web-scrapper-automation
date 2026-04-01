# BridgeLeads — Progress

## Current State (2026-04-01)

### Production Stats
- **74,430+ records** scraped across 10 WA counties
- **97 Pierce County records** with 98% parcel IDs and 90% addresses
- **King County probate scraper live** — 40 records/week, 6 cause types
- **34 active county connectors** in database
- **90 users**, Agency plan active
- **Daily scrape enabled** at 2 AM PT (9 AM UTC)

### Infrastructure
- Railway: API + 4 workers + Beat scheduler (all redeployed 2026-03-26)
- Supabase: PostgreSQL with RLS (8 tables)
- Upstash: Redis (rate limiting, job logs, token blacklist)
- Cloudflare R2: Export storage (CSV/Excel/JSON)
- Resend: Email delivery
- Stripe: Billing (4 plans: Starter/Pro/Business/Agency)
- Vercel: Frontend (app.bridgeleads.io)

### What Works
- EagleWeb template: 16 counties producing records at $0
- Pierce County ARMS scraper: 88 PROBATE records with parcel IDs + addresses
- Batch GIS enrichment: Free ArcGIS API, 50 parcels per call
- Full job pipeline: PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
- Email delivery with pre-signed R2 download links
- Stripe billing with webhook handlers
- SSE live job logs
- Daily scheduled scraping (Beat scheduler)

### Recent Changes (2026-04-01)
1. **King County probate scraper**: Custom scraper for King County Superior Court Clerk (Journal Technologies eCourt). Searches by filing date, extracts case number, party name, and cause of action (Estate, Trust, Guardianship, etc.). No CAPTCHA — uses `dja-prd-ecexap1.kingcounty.gov` instead of the recorder's office (which has reCAPTCHA). Registered in DB with `scraper_mode=manual`, included in daily schedule.
2. **`doc_type` field added to `ScrapedRecord`**: Cause of action (Estate, Trust, Guardianship, etc.) stored separately in `county_records.doc_type` for frontend filtering.

### Previous Changes (2026-03-30)
1. **Landing page — CTA button fix**: "Start Free Trial" button was rendering as a tall green blob due to `display: inline` on the `<Link>` element. Added `inline-flex items-center` so the absolute-positioned shine overlay computes correct dimensions. Fixed both hero and bottom CTA buttons.
2. **Landing page — Remotion video background**: Removed black background from product demo video. Made all 5 Remotion scenes (CountySelect, Scraping, Enrichment, Delivery, CallToAction) transparent. Removed dark card wrapper from player container.
3. **Landing page — Solution video**: Added a second standalone Remotion video player in the Solution section. Animated "BridgeLeads does it all for you" with 3 staggered feature cards: county portal scraping, automatic enrichment, fresh daily leads. Each card slides in with numbered icons, green accent lines, and detail text.
4. **CSS fix**: Invalid `.dark @keyframes pulse-amber` syntax fixed — `@keyframes` can't be nested inside a class selector. Renamed to `@keyframes pulse-amber-dark` with proper `.dark .pulse-amber` class override.
5. **Vercel deployment**: Discovered GitHub webhook was missing — Vercel wasn't auto-deploying on push. Used `npx vercel --prod` CLI to deploy directly.

### Previous Changes (2026-03-26)
1. Pierce County scraper rewritten (615→405 lines)
   - Infragistics date inputs fixed (keyboard typing)
   - PROBATE checkbox fixed (Playwright click for ASP.NET)
   - Pagination fixed (arrow buttons + page dropdown)
   - Parcel extraction via Legal Description tab (96% hit rate)
   - Batch GIS enrichment (90% address rate)
2. Removed debug exception handler from main.py
3. Removed dead _run_enrichment() from tasks.py
4. Crash-safe browser cleanup in base_scraper.py
5. Frontend: Custom date picker added to scraper wizard
6. All Railway services redeployed

### What Needs Work
- AcclaimWeb template: 3 counties (Chelan, Douglas, Pend Oreille) — needs live testing
- LandmarkWeb template: 2 counties (Clark, King?) — needs live testing
- AVA Fidlar template: 1 county (Yakima) — needs live testing
- 10 custom portal counties need individual scrapers
- EagleWeb parcel ID extraction (detail page clicks) — varies by county
- GIS enrichment coverage for non-Pierce counties
