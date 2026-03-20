# BridgeLeads — Product Vision

> "Set it, wake up to leads."

Real estate investors — wholesalers, flippers, and agents — live and die by motivated seller leads. The earlier they find a distressed property owner, the less competition, the better the deal. **BridgeLeads** automates that entire discovery process: pulling directly from county public records daily, enriching with property and mailing addresses, and delivering clean, actionable lead lists on a schedule.

---

## The Problem

Every county in the US publishes public records — probate filings, pre-foreclosures, tax delinquent lists, divorce decrees. These are the earliest signals of motivated sellers. But accessing them is painful:

- **3,100+ counties**, each with a different website
- Manual search, form-filling, pagination, data entry
- No property addresses (just parcel IDs or legal descriptions)
- No mailing addresses for direct mail campaigns
- Hours of work per county, per week

Most investors either pay $200-500/month for stale list providers, or spend 10+ hours/week doing it manually.

---

## The Solution

BridgeLeads scrapes county records automatically using **AI-powered browser automation**. Add any county's public records URL, and Claude (our AI agent) figures out the forms, fields, and extraction — zero code needed.

For every record found, BridgeLeads enriches it with **property and mailing addresses** via Regrid's national parcel API, covering every county in every US state.

The result: fresh leads delivered to your inbox before you wake up.

---

## How It Works

1. **Pick your counties** — select from our directory or add any county URL
2. **Choose record types** — probate, pre-foreclosure, tax delinquent, divorce
3. **Set your schedule** — daily, weekly, or monthly
4. **Wake up to leads** — CSV in your email with names, addresses, and property details

---

## Target Users

| Tier | User | Need | Plan |
|------|------|------|------|
| Starter | New wholesaler | Try one county free | Free (50 records) |
| Pro | Active investor | 2-5 counties, weekly scrapes | $49/mo |
| Business | Wholesaling team | 10-20 counties, daily scrapes, API | $149/mo |
| Agency | List provider | Unlimited counties, white-label | $499/mo |

---

## Technical Edge

- **AI scraper** — Claude analyzes any county website via screenshots, no per-county code
- **National enrichment** — Regrid API covers all 3,100+ US counties in one integration
- **Action caching** — first AI scrape costs ~$0.15, subsequent runs replay cached actions (free)
- **CAPTCHA solving** — 2Captcha integration for sites that require it ($0.003/solve)
- **Real-time streaming** — watch your scrape run live with SSE log streaming

---

## Market Size

- 2M+ active real estate investors in the US
- $4.5B proptech market for lead generation
- Competitor pricing: $49-499/month
- TAM: $500M+ annually for automated lead generation tools

---

## North Star Metric

**Records that lead to closed deals per customer per month.**

Everything we build optimizes for this: fresher data, more counties, better enrichment, faster delivery.

---

## Competitive Advantage

| Feature | BridgeLeads | PropStream | BatchLeads |
|---------|-------------|-----------|------------|
| Automated scraping | AI-powered, any county | Manual search | Partial |
| County coverage | 3,100+ (national) | National (data reseller) | Limited |
| Fresh data | Daily scrape, direct from source | Monthly updates | Weekly |
| Price | $49-499/mo | $99/mo | $79/mo |
| Customization | Any record type, any county | Fixed data sources | Fixed |
| API access | Business+ plan | No | Enterprise |

---

## Vision Timeline

- **Month 1-2**: WA state (39 counties), probate records
- **Month 3-6**: Top 10 investor states, pre-foreclosure
- **Month 6-12**: National coverage, all record types
- **Month 12+**: Enterprise features, CRM integrations, lead scoring, Series A
