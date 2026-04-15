# BridgeLeads Full Marketing Audit
*Date: 2026-04-12 | Updated: 2026-04-15 | Status: Early access, 144 users, 4 paying*

---

## Executive Summary

Five parallel audits were run against BridgeLeads' current positioning, pricing, launch readiness, conversion funnel, and content strategy. Key findings:

1. **Pro is underpriced at $49/mo** — raise to $79/mo. You're selling fresher data than PropStream ($99) at half the price, signaling "budget tool."
2. **Free tier is too generous** — 50 daily-fresh records lets casual users never convert. Add a 7-day data delay on free tier.
3. **Tagline needs specificity** — "Set it. Wake up to leads." is good but generic. "County records. Today's date. Your inbox." differentiates.
4. **Registration-to-value gap is too wide** — 6 steps before payoff. Collapse county selection into signup, auto-queue first scrape.
5. **Launch via direct outreach first** — DM 50 WA wholesalers on BiggerPockets/Facebook, post builder story, attend Seattle REI meetup.

---

## 1. Pricing Strategy

### Current vs Recommended

| Tier | Current | Recommended |
|------|---------|-------------|
| Free | 50 rec, 1 county, daily | **25 rec, 1 county, 7-day delay** |
| Pro | $49/mo, 500 rec, 5 counties | **$79/mo, 1,000 rec, 5 counties** |
| Business | $149/mo, 5K rec, unlimited | $149/mo (no change) |
| Power (NEW) | -- | **$299/mo, 15K rec, priority support** |
| Agency | $499/mo, unlimited | $499/mo + **100K soft cap** |

### Key Changes
- **Raise Pro to $79/mo** — still 20% cheaper than PropStream, properly values freshness advantage. Wholesalers making $5-20K/deal don't blink at $79/mo.
- **7-day data delay on free tier** — demonstrate quality, gate freshness. Daily-fresh is your moat; don't give it away.
- **Add Power tier at $299** — bridges the $149→$499 gap (3.3x jump is too large).
- **Per-record overage ($0.05)** — converts limit-hitters into revenue instead of churn.
- **Annual pricing: 20% off (2 months free)** — standard SaaS, improves cash flow.
- **14-day Pro trial** — let users experience daily-fresh at volume, then drop to Free.

### Pricing Psychology
- Anchor with Agency tier first (makes $149 feel reasonable)
- Show daily cost: "$79/mo = less than $2.64/day"
- Name tiers with identity: Solo, Pro, Team, Agency
- "Most Popular" badge on Pro

---

## 2. Copywriting & Positioning

### Tagline
Current: "Set it. Wake up to leads." -- good but generic.

**Recommended: "County records. Today's date. Your inbox."**
- Specific, differentiating, passes the "could a competitor say this?" test (they can't)

### Positioning Statement
> For real estate wholesalers and flippers who are tired of chasing stale lists, BridgeLeads is an automated lead pipeline that scrapes county public records daily and delivers enriched seller leads every morning -- unlike PropStream and BatchLeads, which recycle bulk data that's 30-90 days old.

### Homepage Headlines (pick one)

**Option A (Competitive):** "Your competitors are working 90-day-old leads. You don't have to."

**Option B (Outcome):** "Stop buying recycled leads. Start getting them first."

**Option C (Mechanism):** "We scrape the courthouse so you don't have to."

### Messaging Pillars
1. **Freshness from the source** -- "Direct from county systems. Not resold. Not recycled. Scraped today."
2. **Zero manual work** -- "Set your counties and never think about it again."
3. **First-mover advantage** -- "The same record hits PropStream in 30-90 days. You had it this morning."
4. **Transparent pricing** -- "Goliath charges $1,499/mo for freshness. We don't."

### CTAs
- **Primary:** "Start getting leads tomorrow"
- **Secondary:** "See a sample lead list"
- **Avoid:** "Get started," "Sign up free," "Learn more"

### Objection-Handling Copy

| Objection | Response |
|-----------|----------|
| "I already use PropStream" | "PropStream aggregates bulk data. By the time you see a lead, 50 other investors have it. We pull from the source the day it's filed." |
| "Is the data accurate?" | "We scrape directly from county recorder systems -- the same source title companies use. 99% property match rate." |
| "Only Washington?" | "We launch state by state to guarantee quality. WA is live with 22 counties. Join the waitlist for your state." |
| "Is this legal?" | "100% public records. Same data you'd get at the county recorder's office -- we just do it daily." |

---

## 3. Launch Strategy

### Timeline

**Week 1 (Pre-launch hardening):**
- Add onboarding flow with county picker + record type selector
- Stripe billing enforced in production
- 2-minute Loom demo video
- Usage dashboard (records scraped, enrichment rate)

**Week 2 (Launch assets):**
- Landing page with pricing table + demo video
- Transactional emails (welcome, scrape complete, weekly digest)
- WA County Coverage page (14 counties listed)
- 3 cold outreach templates

**Week 3+ (Beta launch):**
- DM 50 WA wholesalers on BiggerPockets/Facebook/Instagram
- BiggerPockets forum post: "I automated my WA county record pulling"
- Attend Seattle or Tacoma REI meetup with live demo

### Launch Offer
**Founding Member: First 25 users get 40% off for life.** Pro at ~$47/mo, Business at $89/mo. Visible counter ("7 of 25 spots remaining"). Rewards early adopters who tolerate missing features.

### Channels

| Channel | Action |
|---------|--------|
| BiggerPockets Forums | Weekly value posts in Wholesaling forum |
| Facebook Groups | "Seattle REI", "WA Wholesalers" -- answer questions, soft promote |
| YouTube | 3-5 videos: county record walkthrough, BridgeLeads demo |
| Instagram/TikTok | 30-sec screen recordings pulling leads |
| REI meetups | Monthly attendance, in-person demos |
| Cold email | 100 WA investor-focused agents |

### Referral
"Give $15, get $15" via Stripe coupon + unique referral codes. Add "Share & Earn" tab in dashboard.

### 90-Day Success Metrics

| Metric | Target |
|--------|--------|
| Free signups | 200 |
| Paid conversions | 25 |
| MRR | $1,500 |
| Month-1 churn | <15% |
| Daily active scrapers | 10+ |

---

## 4. Conversion Optimization

### Aha Moment
**Viewing an enriched record with a mailing address.** That's when a wholesaler thinks "I can mail this person today." Track: `user_viewed_enriched_record` as north-star activation event.

### Top 3 Quick Wins

1. **Inline county + record type at signup** -- collapse 6 steps to 3. First scrape queues automatically when they hit the dashboard.
2. **Show enriched records inline** -- don't hide mailing addresses behind an export button. Let them SEE the value before downloading.
3. **Day-3 email with real data** -- "You found [N] leads in [County] this week. Pro finds them daily, automatically."

### Registration Flow Fix
- Kill email verification as a gate (verify to export, not to scrape)
- Move county/record-type into signup form
- Auto-queue first scrape on registration

### Upgrade Triggers (fire at these moments only)
- Record limit hit: "You've used 42/50 free records"
- Second county attempt: "Free plan covers 1 county"
- After first export: "Pro members get auto-delivery to inbox"
- Day 3 email with personalized record count

### Post-Signup Email Sequence

| Day | Email | Subject |
|-----|-------|---------|
| 0 | Welcome + first scrape CTA | "Your first 50 leads are waiting" |
| 1 | Walkthrough (if no scrape) | "Find motivated sellers in 90 seconds" |
| 3 | Results summary (real data) | "You found [N] leads in [County]" |
| 5 | Enrichment highlight | "We found mailing addresses for 89% of your leads" |
| 7 | Upgrade nudge | "Your free records reset soon" |
| 14 | Case study / ROI | "How [Name] closed 3 deals from county records" |

### Trust Signals to Add
- "All data sourced from public county records" below every results table
- "Last scraped: 2 hours ago" timestamp on results
- "We never sell your data" on signup page
- County seal/logo next to county names

### Churn Prevention
- Cancel flow: ask why + offer 50% off for 2 months
- Inactive 14+ days: "Your leads are piling up -- [N] new records since your last visit"

---

## 5. Content Strategy

### SEO Keywords (by intent)

**High-intent:** "probate leads [county/state]", "pre-foreclosure leads [state]", "motivated seller leads", "county public records for investors"

**Mid-funnel:** "how to find motivated sellers", "best lead generation for wholesalers", "skip tracing for real estate"

**Programmatic (long-tail):** "[county] probate records", "[county] tax delinquent list 2026", "pre-foreclosure leads [city]"

### First 10 Blog Articles
1. "The Complete Guide to Probate Real Estate Investing in 2026"
2. "How to Find Motivated Sellers Before Your Competition"
3. "Pre-Foreclosure Leads: How to Find and Close Deals"
4. "Tax Delinquent Properties: The Untapped Lead Source"
5. "Why Stale Data Is Costing You Deals (And How Daily Scraping Fixes It)"
6. "Wholesaling in Washington State: County-by-County Opportunity Map"
7. "6 Public Record Types Every Real Estate Investor Should Be Pulling"
8. "How to Build a Motivated Seller List Without Cold Calling"
9. "Direct-from-County vs. Aggregated Data: What Investors Get Wrong"
10. "From Lead to Close: How Top Wholesalers Automate Their Pipeline"

### Comparison Pages
- "BridgeLeads vs PropStream" -- daily freshness vs stale aggregated data
- "BridgeLeads vs BatchLeads" -- direct-from-county sourcing
- "BridgeLeads vs DealMachine" -- data-first vs driving-for-dollars
- "Best PropStream Alternative for Wholesalers 2026"
- "Goliath Data vs BridgeLeads: Which Has Fresher Leads?"

### Programmatic SEO (highest ROI)
Auto-generate pages for every county+record type:
- `/leads/[state]/[county]/probate` -- "King County WA Probate Records for Investors"
- Each page: record count, sample fields, freshness date, CTA
- **Scale:** 39 WA counties x 6 record types = **234 pages at launch**. Nationally: thousands.
- Populate with real metadata to avoid thin content penalties.

### Lead Magnets
- "Free County Lead Sample" -- 25 real leads, email-gated
- "2026 WA Motivated Seller Opportunity Report" (PDF)
- "The Wholesaler's Public Records Cheat Sheet"
- "Direct Mail Templates for Probate Leads" (Canva)

### YouTube Strategy
- Short (3-5 min) tactical videos -- this audience learns by watching
- "I Pulled 500 Probate Leads in 60 Seconds -- Here's How"
- Weekly "County Spotlight" with deal potential
- Repurpose clips to TikTok/Instagram Reels

### Social Cadence

| Platform | Cadence | Content |
|----------|---------|---------|
| YouTube | 1-2x/week | Tutorials, demos, county spotlights |
| Instagram/TikTok | 3-4x/week | Short clips, deal math, data screenshots |
| X (Twitter) | Daily | Data insights, industry takes |
| LinkedIn | 2x/week | Founder story, milestones |

### Community
- BiggerPockets forums (wholesaling, marketing)
- Reddit: r/realestateinvesting, r/WholesaleRealestate
- Facebook: "Wholesaling Houses Full Time", local WA groups
- Discord: REI-focused servers

### Content Funnel

| Stage | Content | Goal |
|-------|---------|------|
| Awareness | Blog, YouTube, Reddit, TikTok | Teach that daily county data exists |
| Consideration | Comparison pages, county spotlights, sample leads | Show fresher + cheaper than alternatives |
| Decision | Demo video, case study, free trial, ROI calculator | Convert to paid |

**Priority:** Programmatic county pages > lead magnet + email > 5 blog posts > comparison pages > YouTube

---

## Priority Action Items

### Do This Week
1. Update pricing page: Pro → $79/mo, add annual pricing (20% off)
2. Add 7-day data delay on free tier
3. Collapse county selection into signup form (auto-queue first scrape)
4. ~~Show enriched records inline on results page~~ DONE (expandable detail rows with heirs, legal desc, enrichment data)

### Do Next Week
1. Record Loom demo (2 min)
2. Set up post-signup email sequence (6 emails)
3. Create WA County Coverage page
4. Write BiggerPockets launch post

### Do Before Launch
1. Founding member offer (40% off, 25 spots)
2. DM 50 WA wholesalers
3. Attend 1 REI meetup
4. Add trust signals (freshness timestamp, "public records" badge)
