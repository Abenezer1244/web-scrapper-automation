# Trustee Sale ("Auction Leads") — County Expansion

Branch: `feat/trustee-sale-county-expansion` (worktree off origin/main).

## Goal
Extend `trustee_sale` coverage beyond the current 3 counties (Pierce, Snohomish,
King-partial). Build ONE new county end-to-end first, prove it, then repeat.

## Architecture (verified against origin/main, not assumed)
`trustee_sale` is a DB reader over the shared `nts_notices` cache. Coverage is
bounded by which counties have an NTS crawler feeding that cache. Per new county:

1. **Crawler** → `nts_notices` (source-specific; the hard part).
   - HTML source: listing crawl → `extract_notice_urls` → `extract_article_text`
     → `parse_nts_notice`/surrogate → `notice_to_row(source=…, county=…)`.
   - PDF source: `safe_download_to_file` → `nts_pdf.extract/normalize/split`
     → `parse_nts_notice` → `notice_to_row`.
   - Parser `parse_nts_notice` is SHARED + already multi-layout — reuse, don't fork.
2. **Beat schedule** entry in `src/workers/scheduler.py`.
3. **`NTS_MATCH_COUNTIES`** (`nts_matcher_task.py`) += county — so the county's
   `pre_foreclosure` leads also get auction enrichment.
4. **`{County}WATrusteeSaleScraper`** subclass in `src/scrapers/trustee_sale.py`.
5. **`county_connectors`** seed migration (mirror 081) — one `trustee_sale` row.
6. **Tests** — parser fixture (real saved notice, no mocks) + wiring.

## Steps
- [x] Research: most-feasible next county = **Clark** (The Columbian classifieds — free
      HTML, robots 404, verified real full-text NTS). Whatcom (Lynden Tribune) is 2nd.
- [x] Consult Codex on the plan BEFORE code — changed 2 decisions (fetch-all not
      pre-filter; fix the SHARED parser not a Clark override).
- [x] Build ingestion adapter (nts_columbian.py) + shared-parser fix, real fixtures.
- [x] Wire crawler + beat + NTS_MATCH_COUNTIES + subclass + migration 082.
- [x] Tests green (108 parser + 32 wiring locally; full suite 1618 pass); ruff clean.
- [x] Codex reviewed the diff: no P1/Critical. 1 P2 + 1 P3 fixed (barren-alert on total
      fetch failure; log cap truncation).
- [x] Prove: live crawl found 32 ads → 1 real Clark trustee sale parsed (TS
      WA07000393-24-1, MTC acting trustee), 31 non-NTS skipped, 0 errors.
- [x] Dead-code sweep: no stale `_TRUSTEE`; ruff F-rules clean.

## Review

Built Clark County ("Auction Leads" / `trustee_sale`) end to end, the 4th NTS county
after Pierce/Snohomish/King. The record type is downstream of the shared `nts_notices`
cache, so the work was: a new **ingestion adapter** for The Columbian classifieds +
wiring, reusing the shared field parser.

**Changes (7 commits, isolated worktree off origin/main):**
1. `fix(nts)` — shared parser: prefer Current over Original trustee, stop beneficiary
   before "Original Trustee" (dual-label MTC layout; latently fixes King too). Proven
   byte-identical on all 5 Pierce fixtures.
2. `feat(nts)` — `nts_columbian.py` ingestion adapter (listing → /ad-details permalinks
   → `p.ad-content-container` body via BeautifulSoup), real HTML fixtures + tests.
3. `feat(nts)` — crawler task, daily beat, `NTS_MATCH_COUNTIES += clark`,
   `ClarkWATrusteeSaleScraper`, migration 082 (coexists w/ Clark recorder connector).
4. `fix(nts)` — Codex P2/P3: alert on total fetch failure; log cap truncation.

**Codex:** consulted before code (2 decisions changed) + reviewed the diff (no
Critical/High; P2/P3 fixed). Live-proven. No migration risk (data INSERT only, idempotent).

**Follow-ups:** Whatcom (Lynden Tribune) is the ready 2nd county on the same pattern.
Spokane/Thurston need a headed-browser recheck (403'd). wapublicnotices.com statewide
aggregator is a possible one-crawler-many-counties spike (ASP.NET postback).
