# BridgeLeads National Scale Plan

## The Challenge
- 50 states + DC = ~3,100 counties
- Every county has a different recorder website
- Every county has a different assessor/property lookup system
- Different record types: probate, pre-foreclosure, tax delinquent, divorce, eviction
- Different data formats, form UIs, CAPTCHAs, session management

## What We Proved with Pierce County
- AI scraper (Claude) can navigate ANY website with zero code
- CAPTCHA solving works ($0.003/solve)
- Parcel-to-address enrichment works when we have the right API
- The full pipeline (scrape → enrich → export → deliver) works end-to-end

## What Doesn't Scale
1. **Hand-coded scrapers** — Pierce took days. Can't do 3,100 counties manually
2. **County-specific enrichment** — ATIP is Pierce-only. Each county has its own assessor
3. **County-specific parcel formats** — 10-digit in Pierce, different everywhere else
4. **Detail page clicking** — ASP.NET session state doesn't work across browser sessions

## Architecture for National Scale

### Tier 1: County Recorder Scraping (records)
**Use AI scraper (already built)** — add a county URL, Claude figures out the rest.

For each county, we need:
- `base_url` — the county's public records search page
- `record_types` — which record types are available
- `has_captcha` — whether the site requires CAPTCHA

The AI scraper handles:
- Disclaimer acceptance
- Form filling (date range, document type)
- Results extraction
- Pagination

**Action: Build a county directory database** with URLs for all 39 WA counties, then top investor states.

### Tier 2: Property Data Enrichment (addresses)
**Stop building per-county enrichment. Use a national property data API instead.**

The Pierce County approach (ATIP + CAPTCHA) doesn't scale — every county has a different assessor website. Instead, use a **national parcel-to-address API**:

| Service | Coverage | Cost | API |
|---------|----------|------|-----|
| **Regrid** | All US counties | $0.01-0.05/lookup | REST API, parcel → address |
| **ATTOM** | All US counties | ~$0.03/lookup | REST API, APN → property details |
| **Lightbox** | All US counties | Enterprise pricing | REST API |
| **CoreLogic** | All US counties | Enterprise pricing | Batch + API |
| **BatchData** | All US counties | $0.01-0.02/lookup | REST API, skip tracing too |

**Recommendation: Regrid or ATTOM** — they have REST APIs, cover all US counties, and cost $0.01-0.05 per parcel lookup. For 300 records × $0.03 = $9/job. This replaces ALL county-specific enrichment code with ONE API call.

**Action: Integrate one national property API (Regrid recommended) to replace ATIP.**

### Tier 3: County Directory (adding counties at scale)

**Phase 1: WA State (39 counties)**
- Research all 39 WA county recorder URLs
- Add to county_connectors DB table with `scraper_mode="ai"`
- Test each with the AI scraper
- Mark counties with CAPTCHA as `health_status="degraded"`

**Phase 2: Top 10 Investor States**
- Texas, Florida, California, Ohio, Pennsylvania, Georgia, North Carolina, Arizona, Michigan, Colorado
- ~500 counties total
- Same process: find URLs, add to DB, test with AI scraper

**Phase 3: National (remaining ~2,500 counties)**
- Automated discovery: scrape state association of counties websites for recorder URLs
- Batch test with AI scraper (canary check)
- Community-contributed: let Agency users add their own counties

### Tier 4: Record Type Expansion

Currently: probate only.

Expand to:
1. **Pre-foreclosure** — Notice of Default, Lis Pendens filings
2. **Tax delinquent** — delinquent tax lists (often published as CSV/PDF by county)
3. **Divorce** — decree of dissolution filings
4. **Code violation** — building code violations (often city-level, not county)
5. **Eviction** — eviction filings (often court system, not recorder)

Each record type may be on a DIFFERENT website per county. The county_connectors table already supports multiple record_types per connector.

## Implementation Priority

```
1. National property API (Regrid) — replaces ALL per-county enrichment
2. WA county directory (39 URLs) — proves multi-county works
3. Top 10 states directory (~500 URLs) — proves national scale
4. Self-service county addition (Agency plan) — already built
5. Pre-foreclosure record type — highest demand after probate
6. Automated county discovery — scrape state websites for recorder URLs
```

## Cost Model at Scale

Per job (one county, one scrape):
- AI scraper: $0.15-0.25 (cached: $0.01-0.03)
- Property API: 300 records × $0.03 = $9.00
- CAPTCHA (if needed): $0.04
- **Total: ~$9-10 per job**

Revenue per customer:
- Pro plan: $49/mo, 500 records/mo → ~2 jobs/mo → cost $20 → **59% margin**
- Business plan: $149/mo, 5000 records/mo → ~17 jobs/mo → cost $170 → **-14% margin (needs optimization)**
- Agency plan: $499/mo, unlimited → need volume pricing from property API

**Key insight: The property API is the biggest cost. Need to negotiate volume pricing or find cheaper alternatives for Business+ tiers.**

## Next Steps

1. [ ] Sign up for Regrid API (or ATTOM) — get API key
2. [ ] Build generic property enrichment module (`src/scrapers/enrichment/national.py`)
3. [ ] Replace Pierce-specific ATIP code with national API
4. [ ] Research and add all 39 WA county recorder URLs
5. [ ] Test AI scraper on 5 different WA counties
6. [ ] Build county directory admin page (already started)
7. [ ] Launch WA state beta with 10+ counties
