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
- [ ] Research: identify most-feasible next county + its crawlable NTS source (agent running).
- [ ] Consult Codex on the concrete build plan (source + crawler shape) BEFORE code.
- [ ] Build crawler + parser adaptation (if needed) with real fixture.
- [ ] Wire beat + NTS_MATCH_COUNTIES + subclass + connector migration.
- [ ] Tests green (pytest); type/lint clean.
- [ ] Codex reviews the diff (NO-GO on any Critical/High).
- [ ] Prove: run crawler against live source, show real notices parsed.
- [ ] Dead-code sweep.

## Review
_(to fill in at end)_
