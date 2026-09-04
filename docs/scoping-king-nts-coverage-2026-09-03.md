# Scoping: King County NTS coverage — is the Seattle DJC worth buying?

**Date:** 2026-09-03 · **Status:** scoping only, nothing purchased · **Asked for by:** user

## TL;DR

**Do not buy the DJC subscription for scraping.** Two independent blockers, both verified:

1. **DJC's own `robots.txt` disallows `/notices/` for all crawlers** — the exact path we would need.
   Paying $199/yr does not change that; the subscription buys *reading* access, not crawl permission.
2. The free statewide aggregator that carries the same DJC notices (**wapublicnotices.com**, run by the
   WA Newspaper Publishers' Association) **explicitly forbids scraping in its Terms of Use**, with
   liquidated damages of "up to $10,000 per incident or $100 per notice, whichever is greater."

So neither the paid nor the free web route to DJC's notices is clean. This is a **policy** blocker, not a
cost one — and it sits directly against `.claude/rules/scraping.md` and this project's standing posture.

The internal note that prompted this ("DJC, $350/yr, deferred" in `src/workers/nts_crawler.py`) is
**stale on price**: the relevant tier is **DJC Basic at $199/yr**. It was also silent on the robots.txt
issue, which is the part that actually decides it.

## What was verified vs. inferred

| Claim | Status |
|---|---|
| `djc.com/robots.txt` has `User-agent: *` → `Disallow: /notices/` (+ `crawl-delay: 5`) | **Verified directly** (HTTP 200, full file read) |
| DJC Basic is $199/yr and includes "all stories and Public Notices"; no notices-only tier, no API, no data feed | Verified on `djc.com/rg/subscribe.html` |
| wapublicnotices.com is free, covers King, lists DJC among King publications | Verified |
| wapublicnotices.com ToU bans screen/database scraping and spidering, with liquidated damages | Verified on its Terms-of-Use page |
| RCW 61.24.040 mandates publication; RCW 65.16 sets who qualifies as a "legal newspaper"; King County Superior Court approves the list by order | Verified (statute text) |
| The **full current list** of King-County-approved legal newspapers | **NOT verified** — King County's legal-publications page links a PDF / points to the Clerk rather than listing inline |
| Whether DJC *individual notice pages* (vs. the listing) are paywalled | **NOT verified** — would need credentials |
| DJC's own Terms of Service language on automated access | **NOT verified** — only robots.txt was retrievable |

## The tempting wrong answer, and why it is wrong

Research surfaced that we **already have a free, authoritative King NTS source**: the King County Recorder
(Hyland LandmarkWeb) `pre_foreclosure` connector, which a prior proof run showed returning **364 "NOTICE OF
TRUSTEE SALE" records in a 180-day window** — roughly an order of magnitude more than the Queen Anne &
Magnolia News's ~31 per quarter. The obvious conclusion is "repurpose the recorder feed for auction leads
and skip the newspaper entirely."

**That does not work, and it is worth writing down why.** I checked
`src/scrapers/templates/landmarkweb.py`: it scrapes the **search-results grid only** — the column headers it
parses are record date, document type, grantor/grantee and parcel (`landmarkweb.py:465-474`). It never opens
the recorded document's image or body, and there is no PDF/image path in the module at all. So the recorder
feed yields the **lead identity** but structurally **cannot yield the auction-day fields** the `trustee_sale`
product is built on — sale date, time, location, principal owing.

That is precisely why the architecture is what it is: recorder → `pre_foreclosure` leads, newspaper →
`nts_notices`, then `nts_matcher_task` attaches the auction data onto the lead. The newspaper source is not
redundant with the recorder; it carries the one thing the recorder index does not.

There is also a second, subtler reason the recorder cannot replace publication: **a postponed sale is
re-published, not re-recorded.** This session found a live King example — TS `WA05000073-24-2`, whose notice
prints `"on June 26, 2026, 09:00 AM***THE SALE WAS POSTPONED TO 09/18/2026 @ 9:00AM***"`. Only the published
notice carries that update.

## What actually closes the King gap

Ranked by value per unit of effort, given the above:

1. **Already shipped this session (no new source needed).** The Affinia parser gap plus the Thursday-only
   crawl were together costing far more King coverage than the choice of newspaper was. Re-parsing every
   published back issue yields **31 distinct notices where the cache held 14**, and **9 live auctions where
   the product showed 1**. Daily crawls + `scripts/backfill_nts_pdf_archive.py` recover that at zero
   subscription cost. Measure King coverage *after* this deploys before spending anything.
2. **Enumerate the other King legal newspapers.** The blocker is DJC-specific, not universal — Pacific
   Publishing already works for us and is not the only option. wapublicnotices.com's publication filter lists
   King County titles including the Seattle Times, the Reporter/Herald network (Bellevue, Kent, Renton,
   Federal Way, Kirkland, Redmond, Issaquah, Mercer Island, Bothell-Kenmore, Highline, Ballard) and Seattle
   Weekly. Several are likely to publish trustee sales in a scrapable form under permissive robots.txt. **The
   cheapest next step is to check each candidate's robots.txt and legals section** — same weekly-PDF or
   HTML-listing shape we already handle, so a new source is a config + parser variant, not new architecture.
   Get the authoritative approved-newspaper list from the King County Clerk first.
3. **DJC by a non-crawling route only, if ever.** A human reading notices behind the DJC login and entering
   them is permitted; an automated crawler against `/notices/` is not. Only worth it if (2) fails to close
   the gap, and it does not scale.

## Recommendation

**Skip the DJC.** Ship what is already built, measure King coverage after the daily crawl + backfill land,
then spend the next effort on step (2) — auditing the other approved King legal newspapers for a
robots-clean legals section. That is engineering effort against a free source rather than $199/yr against a
source we are not permitted to crawl.

## Sources

- RCW 61.24.040 — https://app.leg.wa.gov/rcw/default.aspx?cite=61.24.040
- Chapter 65.16 RCW (legal newspaper qualification) — https://app.leg.wa.gov/rcw/default.aspx?cite=65.16&full=true
- King County Legal Publications — https://kingcounty.gov/en/dept/dja/courts-jails-legal-system/court-services-resources/legal-publications
- DJC subscribe / pricing — https://www.djc.com/rg/subscribe.html
- DJC public notices listing — https://www.djc.com/notices/
- DJC robots.txt — https://www.djc.com/robots.txt
- WA Public Notices (WNPA) — https://www.wapublicnotices.com/
- WA Public Notices Terms of Use — https://www.wapublicnotices.com/Terms-of-Use.aspx
- King County Recorder LandmarkWeb — https://recordsearch.kingcounty.gov/LandmarkWeb
