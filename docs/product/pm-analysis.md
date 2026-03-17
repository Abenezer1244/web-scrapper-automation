# BridgeLeads — Senior PM Analysis

**Date:** 2026-03-16
**Analyst:** Claude (Senior PM perspective)

---

## What's Strong

- **Customer zero is real.** Mike Girma is named, his pain is specific, and charging him $99/mo from day one is the right instinct.
- **Positioning is clean.** "Set it, wake up to leads" is concrete and testable in a single sentence.
- **North star metric is outcome-based.** Closed deals, not records delivered. Most data products get this wrong.
- **Phase 1 discipline is correct.** Probate only. One county. One customer.
- **GTM is community-led, not paid.** Right channel for this buyer. CAC <$200 is achievable.
- **Unit economics are sound.** One closed deal at $5K–$50K profit makes any subscription tier a rounding error.

---

## Critical Gaps

### 1. Legal and Compliance Risk — Completely Absent

This is the #1 business risk and the document doesn't mention it once.

- County websites have Terms of Service. Some explicitly prohibit automated access. Pierce County ARMS needs a ToS review before building anything.
- Aggregating names, heirs, and mailing addresses of individuals triggers state privacy law exposure (Washington My Health MY Data Act, future CCPA equivalents).
- Skip tracing in Phase 3 is FCRA territory. Selling skip-traced data without understanding FCRA Consumer Reporting Agency rules is an existential legal event.

**Action: legal review before Phase 2 billing goes live.**

---

### 2. Scraper Reliability Is the Product — But There's No Reliability Plan

County websites change without notice. One site redesign breaks a customer's daily lead digest.

Missing entirely:
- Scraper health monitoring (silent 0-result runs vs. truly 0 new records)
- User-facing run status ("Your last run: ✓ 47 records — 6:02am today")
- Failure alerting ("We couldn't reach Pierce County today — retrying at noon")

Without this, churn happens silently. The customer stops getting leads and cancels.

---

### 3. North Star Metric Is Unmeasurable As Written

"Records that lead to closed deals per customer per month" — there's no mechanism to track this.

**Minimum viable tracking:** add one link to every CSV email — "Did you close a deal this week?" — routing to a 1-question form. Even 5% response rate gives signal.

---

### 4. Churn Target of <5%/mo Is Set Wrong

<5%/mo = 46% annual retention. You're replacing the entire customer base every 20 months. Best-in-class B2B SaaS targets <1.5%/mo.

The retention mechanic is missing. Daily CSV delivery alone doesn't create habit. Needed:
- Lead status tracking (contacted / offer sent / closed)
- Saved searches / custom alerts
- Run history and comparison

These are lightweight but create switching cost.

---

### 5. Free Tier Conversion Path Is Weak

50 records/mo is enough for a small-volume investor to get real value and never upgrade. The scheduling trigger doesn't apply to investors doing 1–2 deals/month.

**Recommendation:** make free tier manual runs only, no scheduling, no email delivery (must visit dashboard to download). Friction of manual retrieval is the real upgrade trigger.

---

### 6. Phase 3 Timeline Is Unrealistic

"Multi-state rollout to top 10 investor states" in months 6–9:
- Texas: 254 counties, each with a different records system
- Florida: 67 counties
- California: 58 counties

Each county is a bespoke engineering project. This is years of work compressed into one phase.

Either reframe Phase 3 as "top 5 counties in 3 states" or commit to AI-powered extraction earlier (Phase 2) so county expansion doesn't require per-county engineering.

---

### 7. Agency Tier Is a Different Product

White label + multi-client dashboard + SLA is a platform play, not a pricing tier. Getting there from Pro requires a completely different auth model, billing structure, and support capability.

**Recommendation:** remove Agency from the 12-month roadmap. Flag it as a future product line.

---

## Strategic Recommendations

| Priority | Recommendation |
|----------|---------------|
| **P0** | Get Mike's credit card before writing the scraper. If he won't commit $99 now, the price point is wrong. |
| **P0** | ToS review on both ARMS and ATIP before any scraping infrastructure investment. |
| **P1** | Build run status + failure alerting into Phase 1. It's what makes the product feel reliable. |
| **P1** | Add a dead-simple lead status tracker (contacted / closed) to the dashboard in Phase 2. |
| **P2** | Revise free tier mechanics — manual-only, no email delivery — to make the scheduling upgrade trigger real. |
| **P2** | Compress Phase 3 scope significantly. Multi-state is a 2026+ story, not Month 9. |
| **P3** | Remove agency tier from the 12-month roadmap. Distraction before $1M ARR. |

---

## What's Missing From the Document

- **Risk register** — legal, technical (scraper fragility), competitive (PropStream copies daily pulls)
- **Customer interview data** — Mike is one data point. 5 investor interviews would validate price and CSV format assumption
- **Definition of done for Phase 1** — what does "airtight probate" mean? How many records manually validated?
- **Team/resourcing** — who's building this? One engineer? Two?
- **Success metrics per phase** — Phase 1 ends when... what?

---

## Bottom Line

The vision is sound. The market sizing is credible. The GTM is the right approach. Phase 1 scope is correctly narrow.

The document reads like a founder who knows the market well but hasn't yet stress-tested the assumptions.

**Three things that can kill this before it starts:**
1. Legal exposure on data aggregation
2. Scraper fragility with no monitoring
3. Mike not paying before the build starts

Fix those three. Everything else is refinable after launch.
