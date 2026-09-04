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

---

# Addendum 2026-09-04: the recorded-document route, and the rest of the newspaper list

**Prompted by:** the "Test 8" audit (King, `pre_foreclosure`, 155 leads, job `5178ce6c`).
**Status:** scoping only, nothing built. **Verdict: the recorded-document route is NOT feasible.**

## What the audit measured

Re-parsing **every** Queen Anne & Magnolia News issue from 2026-05-06 to 2026-09-02
(18/18 fetched, HTTP 200) yields **36 distinct King notices**. Test 8 recorded **155**
King Notices of Trustee Sale over an overlapping window. Exact-parcel overlap: **1**.

So the ceiling on King auction-data coverage from the currently wired source is
**~1%**, and 154 of Test 8's 155 leads correctly carry a NULL Auction Date and
Default Owed. That is a coverage limit, not a bug, and must never be papered over by
inferring a date from `date_recorded` or any other field.

## Why not read the recorded document itself?

Every recorded NTS *does* contain both values — RCW 61.24.040(1)(f) requires the notice
to state the sale date/time/place and the amount in arrears — and we already store each
document's instrument number. The blocker is not technical and not price:

> "You agree not to use high-volume, automated, electronic processes to access or query
> the database contained on the website of the King County Recorder's Office."
>
> "You agree not to engage in Data Mining (mass downloading) of images and index
> information... Any users detected mining information via this website will be denied
> access immediately."
>
> — King County LandmarkWeb terms, surfaced on `recordsearch.kingcounty.gov/LandmarkWeb/search/index`

That describes the proposed activity almost literally. Copies are not the obstacle
(unofficial watermarked images are free; only *certified* copies cost $3 + $1/page) —
permission is. `recordsearch.kingcounty.gov/robots.txt` is a 404, so there is no
technical exclusion to rely on either way; the in-page terms govern.

Compounding factors, each independently sufficient to defer:

- King County has **already IP-rate-blocked this project** once, on eRealProperty — a
  system with *less* explicit restrictions than this one.
- The 2Captcha key the King scraper depends on is currently dead, so the index scrape
  itself is fragile before adding a per-document fetch on top.
- **Unverified:** whether LandmarkWeb serves text-layer PDFs or image-only scans. If
  scanned, extracting free-form "amount in arrears" language needs a whole OCR layer —
  and this project's parsing history on the *already text-bearing* legals PDFs
  (header-splitting, missed "SALE POSTPONED TO", a >120s regex backtrack) is not an
  argument for optimism.

No King County **recorder** bulk-data or API product was found. RANS (Recording Activity
Notification System) is a free name-watch email alert, not a data feed.

## The other approved legal newspapers

King County Superior Court's current list (22 papers, per the Serve-By-Publication
packet, "as of 02/23/2026") was obtained. Status of the ones checked:

| Outlet | Status |
|---|---|
| Seattle DJC | **Blocked** — `robots.txt` disallows `/notices/` (verified 2026-09-03) |
| wapublicnotices.com (WNPA aggregator) | **Blocked** — ToU bans scraping, liquidated damages to $10k/incident |
| **Seattle Times** classifieds | **Blocked by ToS**, despite a permissive robots.txt: "you agree not to use any robot, spider, scraper or other automated means to access the Sites." Notice pages also expire quickly. |
| Queen Anne & Magnolia News | Wired today; ~1% coverage |
| Puget Sound Business Journal | **Unverified** — robots.txt/ToS not reachable this pass; publishes legals as scanned classifieds PDFs (same OCR burden) |
| Sound Publishing "Reporter" chain (Auburn, Bellevue, Bothell/Kenmore, Covington/Maple Valley, Kent, Redmond, Renton) + 7 smaller papers | **Unverified** — not individually checked |

`wa.mypublicnotices.com` did not resolve from the research environment — unverified,
not "gone".

## Recommendation

The cheap next step is a **verification-only** pass over the unverified outlets above —
each one's own robots.txt *and* its Terms of Service, since Seattle Times proves the two
can disagree and the ToS wins. Do not build anything against an outlet until both are
clean, and measure its actual coverage against the LandmarkWeb NTS index before
investing in a parser. Until then, King `pre_foreclosure` Auction Date / Default Owed
stays sparse by design, and the product should say so rather than imply missing data.
