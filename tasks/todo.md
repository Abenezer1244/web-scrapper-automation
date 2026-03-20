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

## Phase 13 — Free Enrichment System (County GIS + AI Scraper)

> Replace $375/mo Regrid with free county GIS APIs + AI scraper fallback.
> Cost: $0 infrastructure + ~$0.01/lookup Claude API (fallback only).

### The Problem
Regrid Standard costs $375/mo — too expensive pre-revenue. Need free alternatives.

### The Solution
Three-tier enrichment pipeline:
1. **Primary: County GIS REST APIs** — Free ArcGIS endpoints (no API key, ~60-70% of counties)
2. **Fallback: AI Scraper** — Claude navigates county assessor websites (~$0.01/lookup)
3. **Last resort: "(enrichment unavailable)"**

### Todo

#### 13.1 — County GIS API Client ✅
- [x] Create `src/scrapers/enrichment/county_gis.py`:
  - `enrich_parcel_gis(parcel_id, county, state) -> dict`
  - Built-in Pierce County ArcGIS endpoint (no DB lookup needed)
  - HTTP GET to ArcGIS REST API (no auth needed)
  - Parse JSON response → property_address, mailing_address, owner_name
  - Timeout: 10s
  - Returns `{"property_address": None, "mailing_address": None}` on failure
  - **Tested: returns real addresses for real parcels**

#### 13.2 — AI Assessor Scraper ✅
- [x] Create `src/scrapers/enrichment/ai_assessor.py`:
  - `enrich_parcel_ai(parcel_id, county, state) -> dict`
  - Uses Claude API + Playwright to navigate assessor websites
  - Two-step: Claude analyzes screenshot → navigates form → extracts results
  - Returns same dict format as other enrichment sources

#### 13.3 — Update Enrichment Pipeline ✅
- [x] Update `src/scrapers/enrichment/parcel.py`:
  - New priority order:
    1. County GIS REST API (free, fast) ← NEW
    2. Regrid national API (paid, if enabled)
    3. AI assessor scraper (Claude API) ← NEW
    4. Pierce County ATIP fallback (legacy)
    5. "(enrichment unavailable)"
  - Circuit breaker per source (existing pattern)

#### 13.4 — County GIS Endpoint Discovery ✅
- [x] Add `gis_endpoint` column to `county_connectors` table
- [x] Add `assessor_url` column to `county_connectors` table
- [x] Alembic migration 003 — applied to Supabase
- [x] Pierce County populated with GIS endpoint + assessor URL

#### 13.5 — Settings & Config ✅
- [x] Add to `settings.py`: `GIS_ENRICHMENT_ENABLED` (default: True)
- [x] Add to `settings.py`: `AI_ENRICHMENT_ENABLED` (default: True)
- [x] Add to `.env.example`
- [x] Update DB model with new columns
- [x] Update API schemas (ConnectorCreate, ConnectorResponse)

#### 13.6 — Test with Pierce County ✅
- [x] Found Pierce County GIS REST API: `services2.arcgis.com/1UvBaQ5y1ubjUPmd/.../Tax_Parcels/FeatureServer/0/query`
- [x] Tested GIS enrichment with real parcel IDs — returns property + mailing addresses
- [x] APN 0219011007 → 3624 96TH ST SW, mailing: 5634 S ADAMS ST, TACOMA, WA 98409-2617
- [x] APN 0320261028 → 3410 64TH ST E, mailing: 3410 64TH ST E, TACOMA, WA 98443-1304
- [x] Unknown parcels correctly return empty (not found)
- [ ] Deploy to Railway and run end-to-end test

### Build Order (strict)
```
1. settings.py + .env.example updates         (13.5)
2. county_gis.py                              (13.1)
3. ai_assessor.py                             (13.2)
4. Update parcel.py pipeline                  (13.3)
5. DB migration + column additions            (13.4)
6. Test with Pierce County                    (13.6)
```

---

*(Previous phases preserved below for reference)*

## Previous Phases (Completed)

- Phase 1-9: Foundation through Production Launch — all completed
- Phase 10: Go-to-Market — pending
- Phase 11: Scale — pending
- Phase 12/12A: AI-Powered Extraction — completed
