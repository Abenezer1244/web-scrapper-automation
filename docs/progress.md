# BridgeLeads — Progress Report

**Date:** 2026-03-22 (updated)
**Status:** Production — fixing county scrapers, worker stable at 2×2 concurrency

---

## Records Extracted (Confirmed Done)

| County | Records | Method | Cost |
|--------|---------|--------|------|
| Spokane, WA | 5,653 | EagleWeb Template | $0 |
| Kitsap, WA | 5,076 | EagleWeb Template | $0 |
| Benton, WA | 4,528 | EagleWeb Template | $0 |
| Grant, WA | 3,541 | EagleWeb Template (disclaimer fix) | $0 |
| Island, WA | 3,194 | EagleWeb Template (disclaimer fix) | $0 |
| Jefferson, WA | 1,413 | EagleWeb Template | $0 |
| Whitman, WA | 1,087 | EagleWeb Template | $0 |
| Pierce, WA | 325 | Manual Scraper | $0 |
| Pierce AI, WA | 25 | AI Scraper (Claude) | $0.10 |
| Mason, WA | 14 | AI Scraper (Claude) | $0.10 |
| **TOTAL** | **24,856** | **10 counties** | **~$0.20** |

---

## Today's Fixes (2026-03-22)

### Fix 1: EagleWeb Disclaimer — Playwright Native Click
**Problem:** JS `el.click()` on `<input type="submit">` doesn't reliably submit forms in headless Chromium. Grant and Island counties stayed on `login.jsp` after the disclaimer click — the form submitted via GET but redirected back to itself.

**Fix:** Replaced `page.evaluate()` JS clicks with Playwright's native `.click()` + `expect_navigation()`. Playwright handles form submission, actionability checks, and navigation tracking correctly.

**Counties fixed:** Grant, Island, Grays Harbor, Clallam, Okanogan (all EagleWeb)

### Fix 2: AcclaimWeb Single Date Field
**Problem:** Douglas County's AcclaimWeb has a single `#RecordDate` input, not the `#FromDatePicker`/`#ToDatePicker` pair the template expected. Template fell through all tiers and logged "Could not find date inputs."

**Fix:** Added `#RecordDate` detection in `_fill_dates()`. When single-date mode is detected, chunking switches from 7-day ranges to daily searches.

**Counties fixed:** Douglas (AcclaimWeb)

### Fix 3: Stuck Job Cleanup
Marked 9 jobs stuck in "scraping" state for >2 hours as failed.

---

## Current State

### Railway Worker
- **Deploy:** SUCCESS (2×2 = 4 concurrent workers, all active)
- **Status:** All 4 workers confirmed active across both replicas
- **Fixes applied:** pgbouncer routing (MaxClients fix), --max-tasks-per-child=3 (OOM fix)
- **Previous issue:** 4 replicas caused OOM crashes → scaled to 2 replicas × 2 concurrency

### County Status (39 WA counties)

**Done (10):** Spokane (5,653), Kitsap (5,076), Benton (4,528), Grant (3,541), Island (3,194), Jefferson (1,413), Whitman (1,087), Pierce (325), Pierce AI (25), Mason (14)

**Actively Scraping (1):**
- Clallam — EagleWeb, disclaimer fix confirmed working, extracting records

**Pending/Queued (12 — waiting for worker capacity):**
- EagleWeb: Grays Harbor, Okanogan, Lewis, Pacific, Thurston (disclaimer fix applied)
- AcclaimWeb: Chelan, Douglas, Pend Oreille (single-date fix applied)
- Duplicates: Grant, Island, Whitman (already done, will skip or produce more)

**Failed — Need Fixes (22):**

| Group | Counties | Issue | Next Step |
|-------|----------|-------|-----------|
| EagleWeb (stale fails) | Lewis, Pacific, Thurston, Whitman | Failed before today's fix | Rerun after current batch |
| AcclaimWeb | Pend Oreille | May have similar single-date issue | Rerun after fix deploys |
| Tyler Self-Service | Lincoln, Stevens | Different UI from EagleWeb | Needs template or AI debug |
| Custom portals | Columbia, San Juan, Whatcom, Cowlitz | AI scraper failed | Debug AI scraper |
| LandmarkWeb | King, Snohomish | reCAPTCHA / account required | Needs CAPTCHA solving |
| Fidlar Tapestry | Yakima | Different platform | Build template or fix AI |
| Small counties | Ferry, Garfield, Klickitat, Skamania, Skagit, Walla Walla | AI scraper failed | Debug individually |
| No portal | Adams, Asotin, Franklin, Kittitas, Wahkiakum | No online recorder | Mark inactive |

### Infrastructure Built (50+ commits)

1. **EagleWeb Template Scraper** — zero-cost for Tyler EagleWeb sites (16+ WA counties)
2. **AcclaimWeb Template Scraper** — zero-cost for Tyler AcclaimWeb sites (3 WA counties)
3. **Free GIS Enrichment Pipeline** — $0/month (replaces $375/mo Regrid)
4. **AI Scraper** — Claude-powered for non-standard county websites
5. **Production Deployment** — Railway with Xvfb, Celery workers, beat scheduler

---

## Next Steps

### Immediate (today)
1. ✅ Fix EagleWeb disclaimer (deployed)
2. ✅ Fix AcclaimWeb single-date handling (deployed)
3. ⏳ Monitor Grant, Island, Grays Harbor, Douglas, Clallam, Okanogan results
4. Rerun Lewis, Pacific, Thurston, Whitman (EagleWeb — should work now)
5. Rerun Pend Oreille (AcclaimWeb)

### Priority 1: Get More EagleWeb Counties Working
- Expected: 2,000-5,000 records per county
- 10+ EagleWeb counties should "just work" with disclaimer fix

### Priority 2: Fix Remaining Platforms
- Tyler Self-Service (Lincoln, Stevens)
- Custom portals via AI scraper debug
- LandmarkWeb (King, Snohomish) — CAPTCHA handling

### Priority 3: Expand Beyond WA
- TX, FL, CA top investor counties
- Many states use EagleWeb — template works immediately

---

## Architecture

```
User → Frontend (Vercel) → API (Railway) → Job Queue (Redis/Celery)
                                              ↓
                                         Worker (Railway, 2×2)
                                              ↓
                                    ┌─────────┴──────────┐
                                    │                    │
                              EagleWeb Template    AcclaimWeb Template
                              (16 WA counties)     (3 WA counties)
                                    │                    │
                                    ├────────────────────┤
                                    │                    │
                              AI Scraper (Claude)   Manual Scrapers
                              (any county)          (Pierce)
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
| EagleWeb + AcclaimWeb scraping | $0 |
| GIS enrichment (all WA) | $0 |
| AI scraper (non-template counties) | ~$5-10 |
| Railway hosting (API + Worker + Beat) | ~$20 |
| Supabase (PostgreSQL) | Free tier |
| Vercel (Frontend) | Free tier |
| **Total** | **~$25-30/mo** |
