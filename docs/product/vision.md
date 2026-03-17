# BridgeLeads — Product Vision

> "Set it, wake up to leads."

Real estate investors — wholesalers, flippers, and agents — live and die by motivated seller leads. The earlier they find a distressed property owner, the less competition, the better the deal. **BridgeLeads** automates that entire discovery process: pulling directly from county public records daily, enriching with property and mailing addresses, and delivering clean, actionable lead lists on a schedule.

Competitors (PropStream, BatchLeads, ATTOM) sell stale data dumps. BridgeLeads runs the prospecting for the investor automatically.

---

## The Problem

Today, investors have three bad options:

- **Manual public record searching** — slow, tedious, zero scale
- **PropStream / BatchLeads / ATTOM** — 30–90 day stale data, expensive, no customization
- **Hiring a VA** — $5–15/hr, still slow, still error-prone

The gap: fresh, automated, county-specific motivated seller data pulled directly from the source.

---

## Customer Zero: Mike Girma

**Source websites:**
- **ARMS Web** (`armsweb.co.pierce.wa.us`) — search: Documents > Recorded Documents > Document Type: Probate, Date Range: 01/01/2026–03/17/2026
- **ATIP Parcel Search** (`atip.piercecountywa.gov/app/v2/parcelSearch/search`) — parcel ID → property address + mailing address

**Required fields per record:**
- Date recorded
- Name of estate / party name
- Names of heirs and associated parties
- Legal description of property
- Parcel ID number
- Property address (from parcel lookup)
- Mailing address (from parcel lookup, separate column)

**Delivery:** grouped CSV, automated, on a schedule. Charge $99/mo from day one.

---

## Market Sizing

| Market | Size | Revenue Potential |
|--------|------|-------------------|
| TAM — US real estate investors | ~2M investors | $1.2B |
| SAM — active wholesalers | ~400K | $240M |
| SOM — Year 1–3 target | ~5,000 customers | $3–6M ARR |

**Unit economics:**
- ARPU: $99–$299/mo ($1,200–$3,600 ARR)
- Target churn: <5%/mo
- LTV:CAC target: 5:1 minimum, CAC <$200 via community GTM

**Key insight:** one closed deal = $5K–$50K profit for the investor. Any subscription tier pays for itself in a single transaction.

---

## Record Types — Motivated Seller Signals

| Record Type | Why Investors Want It |
|-------------|----------------------|
| **Probate filings** | Heirs inheriting property, often want quick cash sale |
| **Pre-foreclosure / NOD** | Owner behind on payments, motivated to sell |
| **Tax delinquent** | Owner can't pay taxes, distressed |
| **Divorce filings** | Couples liquidating shared property |
| **Code violations** | Distressed property, motivated landlord |
| **Eviction filings** | Landlords exiting rental business |
| **Out-of-state owners** | Absentee landlords, less attached |

**Phase 1 focus: probate only.** Do it perfectly before adding more.

---

## Pricing Tiers

| Tier | Price | Core Limits | Key Features |
|------|-------|-------------|--------------|
| **Starter** | Free | 1 county, 50 records/mo | Probate only, manual runs, CSV |
| **Pro** | $99/mo | 3 counties, 500 records/mo | All record types, daily schedule, parcel enrichment, email delivery |
| **Business** | $299/mo | Statewide, 5,000 records/mo | Skip tracing, API access, webhooks, Zapier, CRM push |
| **Agency** | $999+/mo | National, unlimited | White label, multi-client dashboard, SLA, custom counties |

**Upgrade triggers:**
- Starter → Pro: scheduling + more counties
- Pro → Business: API + CRM integration
- Business → Agency: white label + multi-client

---

## Competitive Positioning

| Competitor | Weakness | Our Edge |
|------------|----------|----------|
| PropStream | 30–90 day stale data | Daily fresh pulls from county source |
| BatchLeads | Limited customization | User-defined counties, record types, fields |
| ATTOM Data | Expensive API, stale | Affordable, automated, enriched |
| Manual VA | Slow, costly, inconsistent | 100x faster, runs overnight |

**Moat layers:**
1. County connector library — each county scraper built is a barrier competitors must replicate
2. Workflow lock-in — once an investor's morning routine includes their lead digest, switching is behaviorally costly
3. AI extraction — Claude-powered extraction means no brittle selector maintenance

---

## Go-To-Market Strategy

**Don't advertise. Infiltrate.**

Real estate investors live in tight communities — Facebook groups, BiggerPockets, REI meetups, YouTube channels, podcasts.

- **Month 1:** Build for Mike → get his success story on video
- **Month 2–3:** Post case study in every WA state RE investor Facebook group. Free trials to group admins. Reach out to top RE wholesaling YouTube channels.
- **Month 4–6:** Referral loop — refer a converting customer → one free month
- **Month 6+:** Content SEO — "How to find probate leads in [County], [State]" — one article per county
- **Agency play (Month 6+):** RE coaches with email lists → white-label + 30% revenue share

---

## 12-Month Roadmap

### Phase 1 — Month 1–2: Build for Mike
- [ ] Pierce County probate scraper (ARMS Web + ATIP enrichment)
- [ ] Daily CSV email delivery
- [ ] Minimal dashboard (run, download, status)
- [ ] Stripe billing — first paying customer

### Phase 2 — Month 3–5: Go to Market
- [ ] Expand to 10 WA counties, all record types
- [ ] Auth + self-serve signup
- [ ] Parcel enrichment pipeline
- [ ] First 50 customers via community GTM

### Phase 3 — Month 6–9: Scale
- [ ] Multi-state rollout (TX, FL, CA, OH, PA, GA, NC, AZ, MI, CO)
- [ ] Skip tracing (phone + email append)
- [ ] API + webhooks (Business tier unlock)
- [ ] Target: 500 customers, $500K+ ARR run rate

### Phase 4 — Month 10–12: Enterprise
- [ ] White label / agency tier
- [ ] AI-powered extraction (any URL, no selectors needed)
- [ ] CRM integrations (Podio, HubSpot, InvestorFuse, Zapier)
- [ ] Target: $2M+ ARR, Series A ready

---

## Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Scraping engine | Python (Selenium + Playwright) | Handles static, JS-rendered, and form-based sites |
| Backend API | FastAPI | Async, same language as engine, fast |
| Job queue | Celery + Redis | Parallel scraping without blocking |
| Database | PostgreSQL + Redis | Jobs, users, run history + queue broker |
| File delivery | S3 / Cloudflare R2 | Export downloads |
| Frontend | Next.js + React | Dashboard, config, live logs |
| Real-time | Server-Sent Events | Live log streaming during scrape runs |
| Hosting | Vercel (frontend) + Railway/Fly.io (backend) | Ship fast, managed infrastructure |

**Scraper engine logic:**
1. Fast probe with `requests.get()` — if data is in response, use BS4 (fastest path)
2. If JS-rendered or SPA — switch to Playwright
3. If form-based — Playwright fills + submits + waits for network idle

---

## North Star Metric

> **Records that lead to closed deals per customer per month.**

If customers close deals from our leads, they never leave. Track this obsessively from day one.

---

## Three Decisions to Make Now

1. **Name** — BridgeLeads (leading candidate). Avoid "scraper" (technical, scares buyers).
2. **Charge Mike from day one** — yes, $99/mo minimum. Paying customers give real feedback.
3. **One record type done perfectly** — probate only until it's airtight.

---

## Target States for Phase 3 Expansion

Texas, Florida, California, Ohio, Pennsylvania, Georgia, North Carolina, Arizona, Michigan, Colorado
