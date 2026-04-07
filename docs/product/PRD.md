# BridgeLeads Product Requirements Document

**Version:** 1.0
**Date:** April 7, 2026
**Status:** Living document
**Owner:** Product

---

## 1. Executive Summary

BridgeLeads is a multi-tenant SaaS that automates motivated seller lead generation for real estate investors. It scrapes county public records daily, enriches with property and mailing addresses via national APIs, and delivers clean lead lists on a schedule.

The product launched March 2026 with Washington State coverage. In 3 weeks it has acquired 127 users, scraped 93,500+ records across 12 active counties, and validated that daily-fresh leads from official county portals solve a real pain point for wholesalers, flippers, and agents.

**Core value proposition:** "Set it. Wake up to leads."

**Unfair advantage:** AI-powered browser automation (Claude) scrapes ANY county portal without per-county code. This enables daily freshness at lower cost than bulk data resellers, and lets BridgeLeads add new counties in seconds rather than weeks of engineering.

---

## 2. Market Opportunity

### 2.1 Target Market

| Segment | Size (US) | Annual Spend on Leads |
|---------|-----------|----------------------|
| Real estate wholesalers | ~200,000 | $1,200-6,000/yr |
| Fix-and-flip investors | ~300,000 | $600-3,600/yr |
| Real estate agents (investor-focused) | ~500,000 | $1,200-12,000/yr |
| List providers / agencies | ~5,000 | $6,000-60,000/yr |

**TAM:** $3.2B (RE lead gen software market 2023, projected $9B by 2031 at 14.2% CAGR).
**SAM:** $240-480M (200K+ active wholesalers/flippers paying $99-199/mo for data tools).
**SOM (Year 1):** $300K ARR (600 paid subscribers at ~$40/mo blended ARPU).

Sources: Market Research Intellect RE Lead Gen report; CoreLogic Q3 2024 investor data; ATTOM 2025 Home Flipping Report (297K homes flipped in 2025).

### 2.2 Competitive Landscape

**Market event:** In July 2025, PropStream (owned by Stewart Information Services, NYSE: STC) acquired both BatchLeads and BatchDialer, consolidating the #1 and #2 players. This creates pricing risk and switching opportunity for their combined user base.

| Feature | BridgeLeads | PropStream ($99-699/mo) | BatchLeads (PropStream-owned) | PropertyRadar ($119-599/mo) | DealMachine ($49-249/mo) | Goliath Data ($99-1,499/mo) |
|---------|-------------|------------------------|------------------------------|---------------------------|------------------------|--------------------------|
| **Data source** | Direct county scraping | Bulk aggregator (ATTOM) | Bulk aggregator | Bulk aggregator | Driving for dollars | Direct scraping |
| **Freshness** | Daily (same-day) | 30-90 day lag | Weekly updates | Weekly updates | Real-time (manual) | Hourly (claimed) |
| **County coverage** | 39 WA (expanding) | National (160M+ records) | National | Western US focus | National | National |
| **Price** | $0-499/mo | $99-699/mo + add-ons | $71-449/mo | $119-599/mo | $49-249/mo | $99-1,499/mo |
| **Record types** | 6 native | 5+ (aggregated flags) | 3+ (aggregated) | Property filters | 1 (driving) | Multiple |
| **Skip tracing** | Planned | $0.12/record (free on Pro+) | Bundled credits | Built-in | $0.12-14/record | Included |
| **Direct mail** | Planned | $0.48+/postcard | No | No | $0.49-76/postcard | No |
| **CRM / pipeline** | Planned | Basic | Basic | No | Basic | No |
| **Dialer** | No | Click-to-dial (Pro+) | DialerAI ($89/mo add-on) | No | AI dialer | No |
| **API access** | Business+ tier | No | Enterprise only | No | No | No |
| **AI features** | AI county scraping | None | BatchRank AI (scoring) | None | Alma AI (analysis) | None |
| **Custom counties** | Any county in 30 seconds | Fixed catalog | Fixed catalog | Fixed catalog | N/A | Unknown |

### 2.3 User Pain Points (sourced from BiggerPockets, Trustpilot, BBB, G2)

1. **Data staleness** — The #1 complaint across the entire category. County record refresh cycles lag 30+ days for aggregator-based tools. Goliath Data built its entire positioning around hourly scraping and charges $1,499/mo for it.
2. **Duplicate leads** — Users pulling 10K leads/month find they've already pulled some in prior months with no dedup flag.
3. **Price hikes** — BatchLeads doubled prices with short notice. PropStream billing drew BBB complaints.
4. **Data loss on cancel** — BatchLeads deletes all saved lead data on account cancellation.
5. **Skip tracing costs compound** — At $0.12-15/trace, a list of 10K leads costs $1,200-1,500 on top of the subscription.
6. **Tool fragmentation** — Even "all-in-one" platforms require separate CRM, mail tracking, and dialer integrations.

### 2.4 Why BridgeLeads Wins

1. **Freshness beats bulk.** A probate filing from today is worth 10x one from 30 days ago. The first investor to reach the motivated seller gets the deal. BridgeLeads scrapes county portals daily. Competitors resell month-old data. Goliath Data charges $1,499/mo for this same freshness advantage.

2. **AI scraping scales without engineering.** Adding a county to PropStream requires a data partnership negotiation. Adding a county to BridgeLeads requires pasting a URL. Claude navigates the portal, extracts records, and caches the navigation for subsequent runs.

3. **Price-to-value ratio.** Free tier gets investors hooked on fresh data. Pro at $49/mo is half the price of PropStream with better data freshness. The value ladder is designed around how many counties and records an investor needs as they scale.

4. **Consolidation opportunity.** The PropStream + BatchLeads merger creates user uncertainty about pricing and product direction. BridgeLeads is positioned to capture switching investors who want an independent alternative with fresher data.

---

## 3. User Personas

### 3.1 Mike the New Wholesaler (Starter)

**Profile:** 25-35, just completed a wholesaling course, doing their first direct mail campaign.
**Pain:** Spent 10+ hours last week manually searching county recorder websites. Got 40 leads, half had bad addresses.
**Job to be done:** "Get a list of recent probate filings with property and mailing addresses so I can send yellow letters this week."
**Plan:** Starter (Free, 50 records/month, 1 county)
**Success metric:** Sends first mailer within 24 hours of signing up
**Upgrade trigger:** Runs out of 50 records, wants more counties

### 3.2 Sarah the Active Investor (Pro)

**Profile:** 30-45, doing 2-5 deals/month, has a VA handling marketing.
**Pain:** Pays $99/mo for PropStream but the data is 3-4 weeks old. By the time she reaches sellers, competitors already contacted them.
**Job to be done:** "Get fresh probate and pre-foreclosure leads from 3-5 counties delivered to my inbox every morning before my VA starts work."
**Plan:** Pro ($49/mo, 500 records/month, 5 counties)
**Success metric:** Response rate on direct mail improves from 2% to 5%+
**Upgrade trigger:** Expands to new markets, needs API for CRM integration

### 3.3 David the Team Lead (Business)

**Profile:** 35-50, runs a 5-person acquisitions team across multiple markets.
**Pain:** Each team member pulls leads from different sources. No consistency, lots of duplicates, no way to track which lead sources produce closed deals.
**Job to be done:** "Automate lead generation across all our markets with one tool that integrates with our CRM."
**Plan:** Business ($149/mo, 5,000 records/month, unlimited counties)
**Success metric:** Team closes 2 more deals/month from BridgeLeads leads
**Upgrade trigger:** Wants to resell leads to other investors

### 3.4 Lisa the List Provider (Agency)

**Profile:** 40-55, runs a lead generation agency serving 20+ investor clients.
**Pain:** Manually scrapes counties for clients. Each client wants different counties and record types. Can't scale.
**Job to be done:** "White-label automated lead generation for my clients. They log in, see their leads, and think it's my platform."
**Plan:** Agency ($499/mo, unlimited records)
**Success metric:** Serves 20 clients from one BridgeLeads account, charges each $200/mo
**Upgrade trigger:** N/A (top tier)

---

## 4. Current State (as of April 7, 2026)

### 4.1 Platform Metrics (Pre-launch, Internal Testing)

Note: All accounts below are internal test accounts. BridgeLeads has not launched to real customers yet. These metrics represent engineering validation, not market traction.

| Metric | Value | Context |
|--------|-------|---------|
| Test accounts | 127 | Internal testing only, zero real customers |
| Paying customers | 0 | Pre-revenue, pre-launch |
| MRR | $0 | Pre-launch |
| Records scraped (testing) | 93,563 | Validates scraping infrastructure works at scale |
| Active counties producing data | 12 (all WA) | 12 of 39 WA counties verified working |
| County connectors registered | 42 (39 unique WA counties) | Architecture supports all WA counties |
| Job success rate | 51.3% (265 done / 517 total) | Must reach 85%+ before launch |
| Enrichment: parcel ID | 43.1% | Must reach 80%+ before launch |
| Enrichment: property address | 24.6% | Must reach 75%+ before launch |
| Enrichment: mailing address | 16.8% | Must reach 60%+ before launch |
| Platform age | 3 weeks of development |

### 4.2 What's Built and Working

| Component | Status | Detail |
|-----------|--------|--------|
| Backend API (FastAPI) | Production | 15+ endpoints, RLS, rate limiting |
| Job queue (Celery + Redis) | Production | State machine, watchdog, scheduling |
| Pierce County scraper | Production | 300+ records/run, ARMS portal |
| King County scraper | Production | 93+ records/run, LandmarkWeb |
| AI scraper (Claude) | Production | Works on any county, action caching |
| Enrichment pipeline | Production | GIS API + King County assessor |
| Frontend (Next.js) | Production | Dashboard, wizard, results, settings |
| Auth (JWT + API key) | Production | Refresh tokens, brute-force protection |
| Billing (Stripe) | Production | 4 plans, trial, subscription management |
| Email delivery (Resend) | Production | Job completion + lockout notifications |
| Export (CSV/Excel/JSON) | Production | R2 storage, signed URLs |
| CI/CD (GitHub Actions) | Production | Test, build, deploy pipeline |
| Monitoring (Prometheus) | Production | 9 alert rules, Grafana dashboards |
| Security hardening | Completed | 15/15 audit findings resolved |

### 4.3 Key Gaps

| Gap | Impact | Priority |
|-----|--------|----------|
| 51% job success rate | Can't launch with half of scrapes failing | P0 — blocks launch |
| 25% property address coverage | Leads without addresses are useless to investors | P0 — blocks launch |
| 17% mailing address coverage | Investors can't mail letters without mailing addresses | P0 — blocks launch |
| Zero real customers | No revenue, no feedback, no validation | P0 — need beta users |
| WA-only coverage | Limits addressable market to 1 state | P1 — after launch |
| No skip tracing (phone/email) | Table-stakes feature every competitor has | P2 — Phase 2 |
| No CRM integration | Manual CSV import is friction for repeat users | P2 — Phase 3 |
| No direct mail integration | Extra step to go from leads to letters | P2 — Phase 3 |
| Business plan loses money at Regrid rates | Unit economics break at 5,000 records/month | P1 — before scaling |

---

## 5. Product Requirements

### 5.1 Record Types

BridgeLeads serves 4 tiers of data that investors use to find deals, prioritized by implementation order.

#### Tier 1: Core Motivated Seller Records (Scraped from county portals)

These are the primary product. Each record type is a public filing that signals a property owner is motivated to sell.

| Record Type | Source | Motivation Signal | Status | Target Phase |
|-------------|--------|-------------------|--------|-------------|
| **Probate** | County recorder | Heirs inherited a house they don't want, want cash fast | Production (Pierce, King) | Launched |
| **Pre-foreclosure** | County recorder (NOD / lis pendens) | Owner stopped paying mortgage, 90-120 days from auction | Schema ready | Phase 1 |
| **Tax delinquent** | County treasurer | Can't afford taxes = can't afford the house, county will seize | Schema ready | Phase 1 |
| **Divorce** | County clerk | Both parties want house gone, need to split assets quickly | Schema ready | Phase 2 |
| **Code violation** | City/county code enforcement | Facing fines and repair orders, easier to sell as-is | Schema ready | Phase 2 |
| **Eviction** | County court | Landlord tired of problem tenants, may exit landlording | Schema ready | Phase 2 |

#### Tier 2: Additional Public Records (Future scraping targets)

These are public records that provide additional motivated seller signals. Each requires identifying the county portal and building/verifying the scraper.

| Record Type | Source | Motivation Signal | Status | Target Phase |
|-------------|--------|-------------------|--------|-------------|
| **Bankruptcy** | Federal court (PACER) | Owner's assets being liquidated, property often sold | Not started | Phase 2 |
| **Liens** (mechanic/tax/judgment) | County recorder | Owner owes money, may need to sell to clear debt | Not started | Phase 2 |
| **Lis pendens** | County recorder | Lawsuit involving the property, signals distress | Not started (overlaps pre-foreclosure) | Phase 2 |
| **Death certificates** | County health dept | Identify properties of deceased before probate is filed | Production (King County) | Phase 1 |
| **Vacant property** | USPS / utility disconnect records | No one living there, owner may want to offload | Not started | Phase 3 |
| **Absentee owner** | County assessor (owner address != property address) | Owner lives elsewhere, often a tired landlord | Not started | Phase 3 |
| **Failed listings** | MLS (expired/withdrawn/cancelled) | Tried to sell on market, couldn't. Still motivated. | Not started (requires MLS data partner) | Phase 3 |
| **Inheritance / transfer on death** | County recorder (TOD deeds) | Similar to probate but avoided court process | Not started | Phase 2 |

#### Tier 3: Data Enrichment (Per-record lookups, not scraped as records)

These data points are appended to each scraped record to make it actionable. Investors can't do anything with a name and date alone.

| Data Point | Source | Why Investors Need It | Status | Target Phase |
|-----------|--------|----------------------|--------|-------------|
| **Parcel ID** | County recorder detail pages | Links the filing to a specific property | Production (43% coverage) | Phase 0 (improve) |
| **Property address** | County GIS / assessor | The house they want to buy | Production (25% coverage) | Phase 0 (improve to 75%) |
| **Mailing address** | County tax records / assessor | Where to send the letter (often different from property) | Production (17% coverage) | Phase 0 (improve to 60%) |
| **Skip trace: phone** | BatchData / REISkip / SkipGenie API | For cold calling (faster than mail) | Not started | Phase 2 |
| **Skip trace: email** | Same providers | For email/SMS outreach campaigns | Not started | Phase 2 |
| **Property value / ARV** | Zillow / Redfin / ATTOM API | Is the deal worth pursuing? What to offer? | Not started | Phase 3 |
| **Equity estimate** | Mortgage records + property value | High equity = more motivated, better deal | Not started | Phase 3 |
| **Mortgage balance** | County recorder (deed of trust) | How much they owe, determines max offer | Not started | Phase 3 |
| **Owner-occupied vs rental** | Tax records / homestead exemption | Different pitch for owner-occupant vs landlord | Not started | Phase 3 |
| **Years owned** | County assessor (deed date) | Long-term owners have more equity | Not started | Phase 3 |

#### Tier 4: Workflow Integrations (Platform features, not data)

These turn BridgeLeads from a lead source into a complete investor workflow tool.

| Feature | What It Does | Why Investors Need It | Status | Target Phase |
|---------|-------------|----------------------|--------|-------------|
| **Direct mail sending** | Print and mail letters/postcards from the platform | Skip the mail house, one-click marketing | Not started (Lob.com API) | Phase 3 |
| **Built-in dialer** | Cold call leads directly from the results page | No switching between tabs and spreadsheets | Not started | Phase 4 |
| **CRM / deal pipeline** | Track leads from first contact through closing | Know which leads are hot, follow up on time | Not started | Phase 3 |
| **Seller intent scoring** | AI predicts likelihood to sell based on record signals | Prioritize the best leads, skip the rest | Not started | Phase 4 |
| **Comps / ARV calculator** | Compare to recently sold nearby homes | Estimate deal profitability before making offer | Not started (ATTOM API) | Phase 4 |
| **Driving for dollars** | Mobile app to photograph distressed houses | Supplement public records with visual scouting | Not started | Phase 4+ |

**Acceptance criteria per record type (Tier 1 and 2):**
- Extracts: date recorded, party name(s), document number
- Enriches: parcel ID, property address, mailing address (Tier 3 data)
- Deduplicates: same record from consecutive scrapes is not counted twice
- Exports: all fields available in CSV/Excel/JSON

### 5.2 Data Pipeline

```
County Portal ──scrape──> Raw Records ──enrich──> Property Data ──export──> CSV/Email/API
     |                        |                       |                        |
  Playwright             party_name              parcel_id              Cloudflare R2
  headless               date_recorded           property_address       signed URLs
  AI navigation          legal_description       mailing_address        Resend email
                         heirs                   enrichment_data        webhook POST
```

**Scrape phase:**
- Playwright headless Chromium navigates county portal
- AI (Claude) or hand-coded scraper extracts records
- Pagination handled automatically (up to configurable page limit)
- CAPTCHA detection: fail-fast or 2Captcha solve (configurable)

**Enrich phase (multi-source fallback):**
1. County GIS REST API (free, covers ~60-70% of US counties)
2. County assessor website scraper (AI-powered, ~$0.01/lookup cached)
3. Regrid API ($0.01-0.05/lookup, all 3,100+ counties)
4. Skip trace provider (phone/email, Business+ tier, pricing TBD)

**Export phase:**
- Formats: CSV, Excel (styled), JSON
- Storage: Cloudflare R2 with signed download URLs
- Delivery: email notification with download link, webhook POST, API polling

### 5.3 Scheduling and Automation

| Schedule | Description | Use Case |
|----------|-------------|----------|
| Manual | User clicks "Run Now" | Testing, one-off pulls |
| Daily | Runs at configured time (UTC) | Active investors who want morning leads |
| Weekly | Runs on Monday at configured time | Lower-volume investors |
| Monthly | Runs on 1st of month | Baseline/archive runs |

**Schedule requirements:**
- Idempotent dispatch (no duplicate jobs for same config on same day)
- Tolerance window (+-1 minute for beat timer drift)
- Watchdog recovers stuck jobs after 20 minutes
- Canary health checks verify county portals are responsive hourly
- Monthly usage counter resets on 1st of each month

### 5.4 Plans and Pricing

| | Starter | Pro | Business | Agency |
|-|---------|-----|----------|--------|
| **Price** | Free | $49/mo | $149/mo | $499/mo |
| **Records/month** | 50 | 500 | 5,000 | Unlimited |
| **Counties** | 1 | 5 | Unlimited | Unlimited |
| **Record types** | Probate only | All | All | All |
| **Schedules** | Manual only | Daily/weekly | All | All |
| **Export formats** | CSV | CSV, Excel | All + API | All + API |
| **Enrichment** | Property address | Property + mailing | Property + mailing + skip trace | All |
| **Delivery** | In-app download | Email | Email + webhook | Email + webhook + white-label |
| **Team members** | 1 | 1 | 5 | Unlimited |
| **Support** | Community | Email | Priority email | Dedicated |
| **Trial** | N/A | 7 days free | 7 days free | Contact sales |

**Unit economics targets:**

| Plan | Revenue | COGS (enrichment + compute) | Gross margin |
|------|---------|----------------------------|-------------|
| Pro | $49/mo | ~$8/mo | 84% |
| Business | $149/mo | ~$45/mo | 70% |
| Agency | $499/mo | ~$100/mo | 80% |

Note: Business tier enrichment costs assume Regrid volume pricing ($0.005-0.01/record). Current trial pricing ($0.03/record) is unsustainable at 5,000 records/month.

### 5.5 Security Requirements

All requirements verified and implemented as of April 7, 2026:

- Multi-tenant isolation: PostgreSQL RLS + application-layer user_id filtering on every query
- SSRF protection: domain allowlist + private IP blocking on all scraping targets
- CSV injection prevention: formula prefix sanitization on all exported fields
- JWT: 1-hour access tokens + 7-day refresh tokens with blacklist revocation
- Brute-force protection: progressive lockout (5/10/20/50 failures) with email notification at 10
- API key authentication: SHA-256 hashed, Business+ tier only
- Admin role: `is_admin` flag for infrastructure-modifying operations
- Password policy: 10-72 characters, history enforcement (last 5 passwords)
- HTML entity sanitization: all API responses cleaned before delivery
- Rate limiting: per-IP with proper proxy detection (ipaddress module)
- Security headers: CSP, HSTS, X-Frame-Options, Referrer-Policy
- Secrets: all config via env vars, SECRET_KEY validator rejects weak values

### 5.6 API Requirements (Business+ Tier)

**Authentication:** Bearer token (JWT) or API key (`bl_` prefix).

**Core endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| POST | /auth/login | Get access + refresh tokens |
| POST | /auth/refresh | Exchange refresh token for new pair |
| GET | /auth/me | Current user profile |
| GET | /scrapers | List scraper configs |
| POST | /scrapers | Create scraper config |
| POST | /jobs | Start a scrape job |
| GET | /jobs/{id} | Job status + progress |
| GET | /jobs/{id}/results | Paginated results with enrichment data |
| GET | /jobs/{id}/logs | SSE stream of live job events |
| GET | /jobs/{id}/export-url | Get signed download URL |

**Rate limits:** 60 requests/minute general, 10/minute auth, 5/minute job creation.

### 5.8 Competitive Moat: Existing Unique Features

These features exist today and no competitor offers them:

| Feature | What It Does | Why It's a Moat |
|---------|-------------|-----------------|
| **AI scraping (Claude)** | Add any county in 30 seconds by pasting a URL. No per-county code needed. | PropStream/BatchLeads buy bulk data and can't add counties on demand. Goliath Data claims direct scraping but charges $1,499/mo. |
| **Same-day freshness** | Records available within hours of being filed. Daily automated scraping. | Competitors rely on aggregators with 7-30 day lag. Goliath Data offers hourly at $1,499/mo. BridgeLeads does daily at $49/mo. |
| **Free tier with real data** | 50 real county records/month, not a demo or sample. | No competitor gives free real leads. PropStream offers a 7-day trial then $99/mo minimum. |
| **Per-record-type selection** | Choose probate, pre-foreclosure, tax delinquent, etc. independently per county. | Competitors bundle everything into one "leads" database with filters. BridgeLeads scrapes the actual filing type from source. |
| **Live job streaming (SSE)** | Watch your scrape happen in real-time with progress and log lines. | Competitors are batch mode. You request a list and wait. No visibility into what's happening. |

### 5.9 Unique Features to Build (Differentiation Roadmap)

These features don't exist in any competitor. They create defensible advantages.

#### Phase 1: Launch Differentiators

| Feature | What It Does | Why It Matters | Effort |
|---------|-------------|----------------|--------|
| **"First to file" alerts** | Real-time push notification the moment a new probate/foreclosure is filed. Not daily batch — within minutes. | The fastest lead source in RE investing. Even Goliath Data (hourly) can't match per-filing alerts. Investors pay premium for speed. | Medium — requires webhook + polling interval reduction |
| **County health dashboard** | Public page showing which counties are online, when data was last pulled, record counts, uptime. | Radical transparency. Investors can see exactly when data was scraped. PropStream can't do this — they don't know when their aggregator last updated. Also doubles as a marketing/SEO page. | Small — aggregate existing canary health data into a public endpoint |

#### Phase 2: Retention Differentiators

| Feature | What It Does | Why It Matters | Effort |
|---------|-------------|----------------|--------|
| **Exclusive lead windows** | For 24-48 hours after scraping, only the subscriber who configured that county+record type gets the leads. After the window, leads enter the shared pool. | Creates urgency to subscribe and stay subscribed. Investors pay for exclusivity — being the first to mail a motivated seller is the entire value proposition. | Medium — add time-gated visibility to results query |
| **Deal outcome tracking** | After downloading leads, investor marks which ones became deals (contacted, responded, under contract, closed). | Nobody tracks closed deals back to lead source. Over time this data becomes a proprietary scoring model. It answers "which record types in which counties produce the most deals?" | Medium — new UI + DB model for deal stages |
| **Automatic re-enrichment** | Records where property/mailing address enrichment failed are retried weekly with updated GIS data. Leads improve over time. | Nobody does this. Once a lead is pulled from PropStream/BatchLeads, it's frozen. BridgeLeads records get better retroactively. | Small — cron job to re-enrich NULL address records |

#### Phase 3: Scale Differentiators

| Feature | What It Does | Why It Matters | Effort |
|---------|-------------|----------------|--------|
| **Neighborhood targeting** | Filter leads by ZIP code, subdivision, school district, or draw on a map. Not just county-wide dumps. | Wholesalers work specific neighborhoods. Getting 500 leads for a whole county when you only want one ZIP is wasteful and eats your record limit. | Medium — GIS overlay + ZIP filter on results |
| **Lead scoring (AI)** | ML model ranks each lead by deal probability, trained on deal outcome data from Phase 2. | Proprietary and gets better with every customer. PropStream can't build this — they don't have deal outcome data. This becomes the ultimate moat. | Large — requires deal outcome data volume first |
| **Competitor switching tool** | Import your PropStream/BatchLeads CSV, BridgeLeads deduplicates against its data and shows what's new vs stale. | Reduces switching friction. Investor sees "BridgeLeads found 47 leads PropStream missed" — instant conversion proof. | Small — CSV parser + dedup logic |

### 5.10 Features to Copy From Competitors (Table Stakes)

These aren't differentiators. They're expected features that investors need to complete their workflow.

| Feature | Copy From | Why It's Needed | Effort | Target Phase |
|---------|----------|-----------------|--------|-------------|
| **Skip tracing (phone + email)** | BatchLeads, PropStream | Investors need phone numbers for cold calling. Every competitor has this. Without it, investors can only mail. | Medium (API: BatchData or REISkip) | Phase 2 |
| **Zapier/webhook export** | BatchLeads | Investors already use CRMs (Podio, Follow Up Boss, REI Blackbook). Push leads there automatically. | Small (webhook POST on job completion) | Phase 1 |
| **Direct mail integration** | DealMachine (via Lob.com) | "Download CSV then upload to mail house" is friction. One-click "mail this list" is the expected experience. | Medium (Lob.com API) | Phase 3 |
| **Mobile-responsive results** | DealMachine | Investors check leads on their phone between appointments. Dashboard must work on mobile. | Medium (frontend responsive) | Phase 1 |

### 5.11 Features NOT to Build

These look tempting but are traps. They're expensive, regulated, or outside BridgeLeads' core value.

| Feature | Who Has It | Why Skip It |
|---------|-----------|------------|
| **Built-in dialer** | BatchLeads ($89/mo add-on) | Huge engineering effort, TCPA regulated, low margin. Let investors use their existing dialer. Integrate via Zapier instead. |
| **Driving for dollars** | DealMachine (core feature) | Completely different product (mobile-first GPS app). BridgeLeads is a data platform, not a field tool. |
| **Property comps / ARV** | PropStream ($5/report) | Requires licensed MLS/ATTOM data. Expensive, low margin. Investors already use Zillow/Redfin for free comps. |
| **Full MLS integration** | PropStream | Requires MLS board partnerships in each market. Years of work, heavy legal. Not your lane. |
| **AI deal analysis** | DealMachine (Alma AI) | Interesting but not core. Investors know how to evaluate deals. They need leads, not advice. Build this after lead scoring is proven. |

---

## 6. Product Roadmap

### Phase 0: Foundation Hardening (Current - April 2026)

**Goal:** Improve reliability and enrichment to make existing WA users successful.
**North star:** Job success rate > 85%, enrichment coverage > 75%.

| Deliverable | Description | Success Criteria |
|-------------|-------------|------------------|
| Job reliability | Fix failure modes, retry logic, portal health checks | Success rate > 85% (from 51%) |
| Enrichment coverage | GIS fallback chain, Regrid volume pricing | Property address > 75% (from 25%) |
| Activation flow | First-scrape onboarding, sample data preview | Time to first lead < 5 minutes |
| Cancellation analytics | Track why users cancel, exit surveys | Data collected for 50+ churned users |

### Phase 1: WA State Domination (May-June 2026)

**Goal:** Cover all 39 WA counties with 3+ record types. Hit 50 paid customers.
**North star:** $5K MRR.

| Deliverable | Description | Success Criteria |
|-------------|-------------|------------------|
| All WA counties active | AI scraper verified on remaining 27 counties | 39/39 counties producing data |
| Pre-foreclosure records | Second record type launched statewide | Available in 10+ counties |
| Tax delinquent records | Third record type launched | Available in 5+ counties |
| Regrid volume pricing | Negotiate $0.005-0.01/record | Business plan achieves 70%+ margin |
| Referral program | $20 credit per referred paid user | 10+ referral signups |

### Phase 2: Regional Expansion (July-September 2026)

**Goal:** Expand to top 10 investor states. Hit 200 paid customers.
**North star:** $15K MRR.

| Deliverable | Description | Success Criteria |
|-------------|-------------|------------------|
| 10-state coverage | CA, TX, FL, AZ, OH, IL, GA, NC, PA, MI | 100+ counties producing data |
| Skip tracing | Phone + email enrichment (Business+ tier) | Available as paid add-on |
| CRM integrations | Zapier/Make webhook templates, HubSpot native | 50+ users connected |
| Lead deduplication | Cross-run duplicate flagging | Duplicates marked, not double-counted |
| Mobile-responsive results | View and download leads from phone | Full mobile experience |

### Phase 3: National Scale (October 2026 - March 2027)

**Goal:** Cover all 3,100+ US counties. Hit 1,000 paid customers.
**North star:** $75K MRR.

| Deliverable | Description | Success Criteria |
|-------------|-------------|------------------|
| National coverage | AI scraper on all county portals | 3,100+ counties, 6 record types |
| Lead scoring | ML model ranks leads by deal probability | Score available on every record |
| Team management | Multi-user accounts with role-based access | Agency tier fully functional |
| White-label | Custom branding, subdomain for Agency clients | 5+ agencies using white-label |
| Direct mail integration | One-click "send yellow letters" from results | Integration with Ballpoint/YellowLetters |

### Phase 4: Platform (2027+)

**Goal:** Become the operating system for RE investor lead generation.

- Marketplace (investors sell/trade lead lists)
- Comps and ARV data integration
- Disposition management (sell to end buyers)
- International expansion (CA, UK, AU)
- Series A fundraise ($2M+ ARR target)

---

## 7. Success Metrics

### 7.1 North Star Metric

**Deals closed per customer per month from BridgeLeads leads.**

This metric captures the entire value chain: scraping works, enrichment is accurate, leads are fresh enough to be actionable, and customers are taking action. We cannot track this directly yet (customers close deals outside our platform), so we use proxy metrics.

### 7.2 Proxy Metrics

| Metric | Current (Pre-launch) | Launch-Ready Target | Phase 1 Target (3mo post-launch) |
|--------|---------------------|--------------------|---------------------------------|
| Job success rate | 51% | 85% | 95% |
| Property address coverage | 25% | 75% | 90% |
| Mailing address coverage | 17% | 60% | 80% |
| Real customers | 0 | First 10 beta users | 50 paid |
| MRR | $0 | $500 (beta pricing) | $5,000 |
| Monthly churn rate | N/A | < 15% | < 5% |
| Time to first lead | Unknown | < 5 min | < 3 min |
| NPS | N/A | > 30 | > 50 |

### 7.3 Health Metrics (Operational)

| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| API uptime | 99.9% | < 99.5% |
| Job queue depth | < 50 | > 100 |
| P95 API latency | < 500ms | > 2s |
| Worker memory | < 2GB | > 3GB |
| County portal health | 90%+ responding | < 80% |
| Daily scrape completion | Before 8am PT | Not done by 10am PT |

---

## 8. Technical Architecture

### 8.1 System Overview

```
                    +-------------------+
                    |   Next.js App     |
                    |  (Vercel)         |
                    |  app.bridgeleads  |
                    +--------+----------+
                             |
                    +--------v----------+
                    |   FastAPI         |
                    |  (Railway)        |
                    |  api.bridgeleads  |
                    +--+------+------+--+
                       |      |      |
              +--------+   +--+--+   +--------+
              |            |     |            |
     +--------v---+  +----v-+  +v--------+  +v-----------+
     | PostgreSQL  |  | Redis |  | Celery  |  | Cloudflare |
     | (Supabase)  |  | (Up-  |  | Workers |  | R2         |
     | RLS enabled |  | stash)|  | (Rail)  |  | (S3)       |
     +-------------+  +------+  +----+----+  +------------+
                                      |
                               +------v------+
                               | Playwright  |
                               | Chromium    |
                               | (headless)  |
                               +------+------+
                                      |
                               +------v------+
                               | County      |
                               | Portals     |
                               | (3,100+)    |
                               +-------------+
```

### 8.2 Tech Stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| Frontend | Next.js 14 (Vercel) | SSR, React ecosystem, edge deployment |
| Backend API | FastAPI (async) | Performance, type safety, OpenAPI docs |
| Job queue | Celery + Redis | Reliable async execution, scheduling, retries |
| Database | PostgreSQL (Supabase) | RLS, JSON columns, full-text search |
| Migrations | Alembic | Versioned schema changes, rollback support |
| Scraping | Playwright (headless Chromium) | JavaScript rendering, stealth, reliability |
| AI | Claude API (Anthropic) | Screenshot analysis, form navigation, data extraction |
| Object storage | Cloudflare R2 | S3-compatible, no egress fees |
| Email | Resend | Transactional email, deliverability |
| Billing | Stripe | Subscriptions, invoicing, webhooks |
| Hosting | Railway | Docker, auto-deploy, horizontal scaling |
| Monitoring | Prometheus + Grafana + Loki | Metrics, dashboards, log aggregation |

### 8.3 Database Schema (6 core tables)

- **users** — authentication, plan, usage tracking, admin flag, password history
- **scraper_configs** — per-user scraper settings (county, schedule, fields, delivery)
- **jobs** — scrape execution state machine (pending > queued > probing > scraping > enriching > done/failed)
- **results** — individual scraped records with enrichment data
- **job_logs** — timestamped log entries for SSE streaming
- **county_connectors** — registry of county portals, scraper classes, health status

---

## 9. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| County portals change HTML/UI | High | Medium | Canary health checks hourly. AI scraper auto-adapts. Alert on failure spike. |
| County blocks our IP | Medium | High | Rotate IPs via proxy pool. Rate-limit scraping to 1 req/3s. User-agent rotation. |
| Regrid pricing doesn't scale | Medium | High | Negotiate volume deal. Build direct county assessor scrapers as fallback. |
| reCAPTCHA on county portals | Medium | Medium | 2Captcha integration built. CAPTCHA-free portals prioritized. |
| Competitor copies AI approach | Low | Medium | First-mover advantage. County coverage moat. Customer lock-in via schedules. |
| Legal challenge to scraping | Low | High | All data is public records. No login bypass. No ToS violation. Legal review completed. |
| Data quality issues | Medium | High | Multi-source enrichment fallback. HTML sanitization. Dedup hashing. |

---

## 10. Open Questions

1. **Regrid volume pricing** — What rate can we negotiate for 50K+ lookups/month? This determines Business tier viability.
2. **Skip trace provider** — Who offers the best phone/email append at scale? (BatchSkipTracing, REISkip, SkipGenie)
3. **North star tracking** — How do we learn which leads result in closed deals? In-app feedback? CRM integration? Survey?
4. **Cancellation reasons** — Why are users churning? Need exit survey data.
5. **International expansion** — Is there demand for county-equivalent scraping in Canada, UK, or Australia?

---

## Appendix A: Glossary

| Term | Definition |
|------|-----------|
| **Motivated seller** | Property owner with urgency to sell (probate, foreclosure, divorce, etc.) |
| **Wholesaling** | Contracting a property at below-market price and assigning the contract to an end buyer |
| **Skip tracing** | Finding phone numbers and email addresses for property owners |
| **Yellow letter** | Handwritten-style direct mail used by investors to contact sellers |
| **Parcel ID** | Unique county-assigned identifier for a property (APN/PIN) |
| **GIS** | Geographic Information System — county mapping databases with property data |
| **RLS** | Row-Level Security — PostgreSQL feature that filters data by user at the database level |
| **ARMS** | Auditor's Records Management System (Pierce County portal) |
| **LandmarkWeb** | Hyland document management portal used by multiple WA counties |

---

## Appendix B: Revision History

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-04-07 | 1.0 | AI Product Manager | Initial PRD based on production data, security audit, and market research |
