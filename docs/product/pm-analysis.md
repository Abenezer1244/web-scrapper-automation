# BridgeLeads — PM Analysis (v2.0)

**Updated:** 2026-03-20
**Scope:** National scale SaaS — all US counties

---

## Product Summary

BridgeLeads automates motivated seller lead generation for real estate investors. Scrapes county public records (probate, pre-foreclosure, tax delinquent), enriches with property/mailing addresses via Regrid national API, and delivers clean CSV lead lists on a schedule.

**Key differentiator**: AI-powered scraping works on ANY county website. Add a URL → Claude handles the rest. No per-county code needed.

---

## What's Built (Production)

| Component | Status | Details |
|-----------|--------|---------|
| Pierce County scraper | Production | 300 records, all pages, heirs, legal descriptions |
| AI scraper (Claude) | Production | Any county, zero code, action caching |
| Regrid enrichment | Built | National parcel → address API |
| SaaS frontend | Production | Vercel, app.bridgeleads.io |
| API backend | Production | Railway, api.bridgeleads.io |
| Worker + Beat | Production | Railway, 3 services |
| Stripe billing | Built | 4 plans |
| Admin county UI | Built | Agency plan adds counties |
| 2Captcha solving | Built | Pierce County ATIP fallback |

---

## Unit Economics

| Plan | Price | Records/mo | Enrichment ($0.03/rec) | AI Cost | Net Margin |
|------|-------|-----------|----------------------|---------|------------|
| Starter | Free | 50 | $1.50 | $0.15 | N/A |
| Pro | $49/mo | 500 | $15 | $1.50 | **66%** |
| Business | $149/mo | 5,000 | $150 | $15 | **-11%** |
| Agency | $499/mo | 50,000 | Volume ($500) | Volume | **TBD** |

**Action needed**: Negotiate Regrid volume pricing for Business+ tiers, or offer enrichment as add-on.

---

## Scale Path

| Phase | Timeline | Counties | Customers | Revenue |
|-------|----------|----------|-----------|---------|
| WA State | Month 1-2 | 39 | 50 | $2.5K MRR |
| Top 10 States | Month 3-6 | ~500 | 500 | $25K MRR |
| National | Month 6-12 | ~3,100 | 5,000 | $250K MRR |
| Enterprise | Month 12+ | All + intl | 10,000+ | $2M+ ARR |

---

## Competitive Position

**Only product that automates county-by-county scraping with AI.** Competitors require manual search or limited county coverage.

---

## Top Risks

1. **County blocks scraping** → polite delays, rate limiting, user-agent rotation
2. **Regrid pricing** → multi-source (ATTOM, CoreLogic alternatives)
3. **Business plan margin** → volume pricing or enrichment add-on
4. **AI scraper accuracy** → action cache, retry logic, canary checks

---

## Next Steps

1. Regrid API trial → test enrichment
2. WA county directory (39 URLs)
3. Test AI scraper on 5+ WA counties
4. Pre-foreclosure record type
5. TX/FL expansion
