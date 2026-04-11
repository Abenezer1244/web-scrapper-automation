# WA Connector Health Audit — 2026-04-10

Phase 1 of Sprint 6.3 triage. Read-only. No DB writes or code changes
in this commit.

## Baseline numbers

| | Count |
|---|---|
| Active connectors in `county_connectors` | 37 |
| Health status = `healthy` | **9** |
| Health status = `degraded` | 13 |
| Health status = `down` | 15 |
| Stated PRD 6.3 goal ("10 counties active") | 10 |

Headline: the 37-county number is misleading. The canary has flagged
~75% of connectors as unhealthy, and the current plumbing has two
independent failure modes that make the real picture much worse than
even that suggests.

## Root cause #1 — the canary's 1-day probe window

`src/workers/scheduler.py` `canary_check()` runs scrapers against
`yesterday → today` only:

```python
today = datetime.now(UTC).date()
yesterday = today - timedelta(days=1)
records = asyncio.run(_canary_scrape(scraper_class, ...))
connector.health_status = "healthy" if records else "degraded"
```

A 1-day window is too narrow for rural counties, which routinely file
zero probates or pre-foreclosures on a given day (some file <1/week).
Those counties get flipped to `degraded` on canary runs that hit an
empty day, even though the scraper itself works fine.

### Verification

Ran four hand-coded scrapers (all non-healthy per DB) against a 7-day
window:

| County + type              | DB status  | 7-day probe result            | Verdict                 |
|----------------------------|------------|-------------------------------|-------------------------|
| Pierce probate             | `down`     | 11 records, 10/10 GIS enriched| **False positive**      |
| Pierce code_violation      | `degraded` | 3 records                     | **False positive**      |
| King code_violation        | `degraded` | 250 records                   | **False positive**      |
| Whatcom probate            | `down`     | 0 records over 7 days         | Inconclusive (sparse)   |

Three of four "down/degraded" hand-coded scrapers are working
correctly and were mis-classified by the canary. The fourth (Whatcom)
returned 0 records on a 7-day window, which could still be a genuine
regression or just sparse filing — needs a 30-day probe to
distinguish.

## Root cause #2 — AI-mode connectors are blocked by SSRF

`src/api/middleware/security.py` hardcodes the approved scraping
domains to seven values:

```python
_ALLOWED_SCRAPE_DOMAINS = frozenset([
    "armsweb.co.pierce.wa.us",
    "atip.piercecountywa.gov",
    "recordsearch.kingcounty.gov",
    "blue.kingcounty.com",
    "payment.kingcounty.gov",
    "e-docs.clark.wa.gov",
    "www.snoco.org",
])
```

Any additional domains must be registered at import time via
`add_scrape_domain()`. The template scrapers
(`templates/eagleweb.py`, `templates/landmarkweb.py`,
`templates/acclaimweb.py`, `templates/ava_fidlar.py`) call
`add_scrape_domain()` in their `__init__`, so template-matched AI
connectors get registered automatically.

**But AI connectors that don't match any template fall through to
`AIScraper`, which does NOT register the domain.** A comment at
`src/scrapers/ai_scraper.py:47` says *"Domain is validated at
connector creation time via validate_scraping_target()"* — but
connectors seeded via Alembic migration or `scripts/seed_connectors.py`
never pass through the API route that calls that function. So the
domain is never added, and the first scrape attempt throws
`ValueError: Scraping target not in approved domain list`, which the
canary interprets as `down`.

### Verification

Spot-probed three connectors the canary marked `down`:

| County   | URL                                                         | Error                                               |
|----------|-------------------------------------------------------------|-----------------------------------------------------|
| Cowlitz  | `www.co.cowlitz.wa.us/291/Auditor-Public-Record-Search`     | `Scraping target not in approved domain list`      |
| Skagit   | `www.skagitcounty.net/Search/Recording/`                    | `Scraping target not in approved domain list`      |
| Stevens  | `selfservice.stevenscountywa.gov/web` (Tyler selfservice)   | `Scraping target not in approved domain list`      |

All three URLs have additional problems — Cowlitz and Skagit point at
landing pages, not search systems; Stevens is Tyler selfservice which
is explicitly excluded from the EagleWeb template — but the SSRF
block is the first failure they hit, so the canary never gets far
enough to discover the deeper issues.

## Root cause #3 — Yakima scrapes "successfully" but returns wrong data

Yakima's connector is configured against
`https://ava.fidlar.com/WAYakima/AvaWeb/#/search`. A direct probe to
the portal's Breeze API endpoint
(`POST /WAYakima/ScrapRelay.WebService.Ava/breeze/DocumentTypes`)
shows the portal only indexes 10 document types, none of them
relevant:

```
BINDING SITE PLAN, BOUNDARY LINE ADJUSTMENT, CEMETERY, CONDOMINIUM,
DEED, EASEMENT, MOBILE HOME COURT, PLAT, SHORT PLAT MAP, SURVEY
```

Nevertheless, a 7-day probe via the AI scraper returned
"1 record". Because the portal has no probate or pre-foreclosure
documents, this record is necessarily a misclassified deed/easement,
plat, or survey — garbage data wearing a `probate` label.

Yakima's full probate + pre-foreclosure document set is only
available behind Fidlar's paid Tapestry or Laredo products (see
`docs/compliance/wa-tax-delinquent.md` for the general WA portal
landscape). Yakima must be deactivated regardless of what we do about
the canary/SSRF issues.

## Breakdown of the 28 non-healthy connectors

Categories after probing:

### A. False positives — scraper works, canary is wrong (high confidence)

Verified via 7-day probe:

- pierce/WA probate, pre_foreclosure, divorce (manual, armsweb)
- pierce/WA code_violation (manual, arcgis)
- king/wa code_violation (manual, data.seattle.gov)

Likely false positives (template-matched AI-mode, small counties
where 1-day window routinely sees 0 records — same pattern as the
healthy AI-mode set):

- benton, chelan, clallam, grant, lewis, mason, okanogan,
  pacific, pend oreille, whitman — all Acclaim/EagleWeb template
  matches. Need 7-day re-probe to confirm individually.

### B. SSRF-blocked — fixable by extending allowlist + seeding script

These all hit the "Scraping target not in approved domain list"
error. Each would also need a functioning scraper beyond that:

- cowlitz (landing page — needs URL change + AI scraper)
- skagit (landing page — needs URL change + AI scraper)
- stevens (Tyler selfservice — needs new template + URL fix)
- lincoln (Tyler selfservice — same as stevens)
- okanogan (Tyler selfservice — same)
- klickitat, ferry, columbia, garfield, grays harbor, walla walla,
  san juan, skamania — all non-template AI connectors, need
  individual investigation after SSRF is unblocked

### C. Terminal blockers — deactivate

Will never work regardless of how much effort we pour in:

- **yakima/WA** — portal indexes only deeds/easements, probate not
  available without paid Tapestry/Laredo subscription. Currently
  returns misclassified data (root cause #3).
- **snohomish/WA** — ToS blocker established in Sprint 2 memory.
  Despite being on the hardcoded SSRF allowlist, commercial use is
  prohibited by the portal's terms.
- **whatcom/WA** — needs further investigation. Previous Sprint 2 run
  succeeded; 7-day probe today returned 0. Either a genuine
  regression or genuinely sparse county (Whatcom pop ~230K, so
  plausible). Needs a 30-day probe before deciding.

### D. Needs human decision

- **king/wa code_violation** — currently fetches from
  `data.seattle.gov` (Seattle city, not full King County). Records
  are there (250 in 7 days) but geographic scope is ambiguous. Is
  this by design, or should it cover all of King County?

## Recommended phases

**Phase 2 — Fix canary (small, high leverage)**

Widen the canary probe window from 1 day to 7 days. This single
change will flip most of category A back to `healthy` on the next
canary run, without requiring any scraper code changes.

**Phase 3 — Fix SSRF seeding bug (small, high leverage)**

Add an import-time registration path so that every connector row in
the DB has its base_url's domain added to `_ALLOWED_SCRAPE_DOMAINS`
at app startup. This unblocks category B for further investigation.
Option: load all active connectors at app startup and call
`add_scrape_domain()` for each hostname.

**Phase 4 — Deactivate category C**

Set `active=false` for yakima, snohomish, and any confirmed-terminal
connectors. Document in this file.

**Phase 5 — Surface real status in UI**

Update the frontend onboarding / county picker to filter on
`health_status = 'healthy'` instead of `active = true`, so users only
see counties that are actually producing data. Optionally show
`active AND NOT healthy` in a separate "experimental" tab.

**Phase 6 — Individual category B investigations**

Only after phases 2-5 are in place. Each SSRF-unblocked connector
needs to be driven through the AI scraper and either succeed, get a
template, or join category C.

## What NOT to do

- Don't "fix Yakima" by building a new scraper — the data isn't there
  in a form we can legally or technically reach.
- Don't expand the hardcoded SSRF allowlist manually for every
  county — that path does not scale past ~10 counties and creates
  drift between the DB and the code. Fix the seeding path instead.
- Don't restore any connector to `healthy` manually without a probe
  that proves it — the canary is the single source of truth; don't
  fight it, fix it.
