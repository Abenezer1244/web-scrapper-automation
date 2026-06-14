# Thread 3b — King/Snohomish Pacific Publishing PDF NTS crawlers

(Prior Tier-0 / Tier-1 build history captured in memory `project_record_type_lead_quality_2026_06_12`.)

Scope decision (user, 2026-06-13): **Snoho first, then King.** Defer DJC/buy.
Pattern to extend: the existing Pierce/Tacoma pipeline (parser `src/scrapers/sources/nts_tacoma_index.py`,
cache `nts_notices` mig 058, crawler beat `src/workers/nts_crawler.py`, matcher `nts_matcher.py` + task).

## Discovery (DONE 2026-06-13 — verified against a real downloaded PDF)

- **Source (Snoho):** Snohomish County Tribune weekly Legals PDF.
  URL seen: `.../static-4/snoho/images/Legals%20-%2012-17-25.pdf` (pacificpublishingcompany.media.clients.ellingtoncms.com)
  - Text-based PDF (NOT scanned), 6 pages, 271 KB. `pypdf` extracts clean text. ✅
  - ⚠️ CDN domain has flipped before; filename pattern inconsistent → **discover the current PDF URL by scraping the paper site (snoho.com) legals link, do NOT hardcode CDN paths.**
- **Format:** SAME Quality Loan / North Star statutory layout the Tacoma parser already handles. One weekly PDF = MANY notices (7 NTS blocks in sample), all Snohomish County (sale @ Everett courthouse).
- **Reuse test (real PDF):** after normalize+split, existing `parse_nts_notice` parsed TS#/parcel/address/principal on all 7 blocks. ✅
- **Gaps that need real work:**
  1. **PDF text artifacts:** column-wrap hyphenation (`Par-\ncel`, `SER-\nVICE`) breaks regexes → de-hyphenate (`([A-Za-z])-\n([a-z])`→`\1\2`), then `\n`→space, collapse. Curly apostrophe arrives as `�` (parser already maps it). ⚠️ RISK: never join on a digit (a wrapped `WA-25-\n1012820` must keep its hyphen) — needs a regression test.
  2. **Multi-notice splitting:** split on `(?=NOTICE OF TRUSTEE'?S SALE)`; `is_valid_nts` (TS#+auction) is the backstop.
  3. **Auction location-preposition variance (SHARED-MODULE change):** Snoho uses `at <time> On the Steps in Front of <loc>` AND `at <time> at <loc>`. Current `_AUCTION` requires `at <loc>` → misses "On the Steps" variant (5/7 blocks failed auction_date in the test). Broaden WITHOUT regressing Tacoma (keep Tacoma fixture green).

## Plan (phased, each Codex-gated; max ~5 files/phase)

- [x] **Phase 0 — Codex consult DONE.** Confirmed: (a) de-hyphenation needs to handle UPPERCASE wraps but NOT corrupt identifiers; (b) split notices FIRST so the lazy `_AUCTION` can't drift + add a drift guard + robust header regex; (c) FORK ingestion (nts_pdf), reuse field extraction; (d) `(source,ts_number)` with source-specific value, never key on PDF URL; (e) %PDF- magic + reject encrypted + page cap on top of safe_download_to_file.
- [x] **Phase 1 — PDF infra + parser, fixture-tested. DONE + Codex-gated (branch `feature/nts-pdf-snoho-king`).**
  - `pypdf==6.13.2` added (SBOM: 0 OSV vulns, pure-python).
  - `src/scrapers/sources/nts_pdf.py`: extract (magic/encrypted/page-cap) + normalize (2-rule de-hyphenation: soft letter-wrap joins, identifier wrap keeps hyphen — Codex caught TS# truncation) + split (ALL-CAPS possessive header only, no boilerplate over-split).
  - REAL PDF fixture `tests/fixtures/nts_snoho_tribune_2025-12-17.pdf`; 5/7 notices parse clean (2 misses = commercial + MTC formats, safely skipped). 69 tests pass.
  - `_AUCTION` broadened (location-preposition flexible + drift guard); Tacoma green.
  - `notice_to_row` parameterized `source`/`county` (Codex P2: avoid mislabeling Snoho as Pierce).
- [x] **Phase 2 — Snoho crawler beat. DONE + Codex-gated.** `crawl_nts_snoho_tribune` + shared `_crawl_pacific_publishing_pdf` (discover via snoho.com [browser UA, follows 302, soft-404 tolerant, "legal"-in-filename filter] → `safe_download_to_file` [SSRF, https, 25 MB cap] → extract→normalize→split→parse→`notice_to_row(source='snohomish_tribune', county='snohomish')` → per-row SAVEPOINT upsert → source-scoped expiry). Weekly beat (Thu). **Live prod: 2 Snoho notices upserted, Pierce untouched.** Codex [P1]=matcher-not-wired → resolved by Phase 3.
- [x] **Phase 3 — county-generic matcher. DONE + Codex-gated.** `NTS_MATCH_COUNTIES`, beat loops counties, `_match_and_write(county=)` + `match_job_inline` derives job county, tasks.py inline gate widened. **County-ALIGNED** (a notice only matches a same-county lead). Live prod: runs pierce+snohomish, 1061 candidates, 0 errors. 0 matched = correct (0 Snohomish pre_foreclosure LEADS exist — see follow-up).
- [x] **Phase 4 — King (Queen Anne). DONE + Codex-gated.** `crawl_nts_king_queenanne` (constants only), king in NTS_MATCH_COUNTIES, weekly beat. **Live prod: discovered QA Legals PDF, 0 errors (0 upserted this week — partial coverage, those trustees use unsupported formats).**

## Review (2026-06-13)
- **Branch `feature/nts-pdf-snoho-king` (5 commits, pushed `59c9a32`, NOT merged). 71 NTS tests pass, ruff clean. Codex GATE PASS (no P1); all P2s fixed** (TS#-wrap truncation, county/source param, lazy page cap).
- All 3 crawlers (Tacoma/Snoho/King) + the county-generic matcher proven live in prod via `railway run`.
- **King build-vs-buy:** free Queen Anne PDF shipped (partial); DJC ($350/yr, complete) deferred to user.
- **⚠️ Product follow-ups (not bugs):** (1) Snohomish/King NTS data only enriches leads once there ARE Snohomish/King **pre_foreclosure scrapers** producing leads — currently 0 Snohomish pre_foreclosure leads. (2) Parser covers Quality Loan + North Star formats; add MTC/Trustee-Corps + commercial-loan + Affinia/Aztec (King) formats to lift coverage (safely skipped today). (3) `nts_notices.source` is varchar(32) — sources kept short.
- **DEPLOY decision (user):** merging adds `pypdf` dep + 2 weekly beats + the matcher refactor. No migration. Recommend merge.


---

# Thread 3c — Finish Snohomish pre_foreclosure SCRAPER (the missing LEAD source)

(2026-06-14. The crawler caches Snoho NTS *auction data*; the matcher is snohomish-aware;
but there are 0 Snohomish pre_foreclosure LEADS for it to enrich. This scraper produces them.)

Draft: `src/scrapers/snohomish_wa_pre_foreclosure.py` (pure-HTTP BaseScraper, reuses the TESTED
`nts_pdf` + `parse_nts_notice`; discovery mirrors the proven `_discover_latest_legals_pdf`). Parses + ruff-clean.

## Plan (each Codex-gated; branch `feature/nts-snoho-preforeclosure-scraper`)

- [x] **Step 0 — Codex consult** DONE. 5 findings, all reconciled: (High) migration idempotency must key
      on scraper_class — mig 040 already does, mirrored. (Med) no date_recorded→today fallback (draft already
      avoids; is_valid_nts guarantees auction_date so `nod_date or auction_date` is never None). (Med) no
      double-count; dedup on stable parcel/addr. (Med) matcher needs cache populated → run crawler first in
      Step 4. (Low) wired settings.DEFAULT_TIMEOUT, dropped `_ = settings` placeholder.
- [x] **Step 1 — Live-test** DONE (`railway run --service worker python scripts/test_snoho_preforeclosure.py`).
      Current PDF = `Legals - 6-10-26.pdf`. **2 real Snohomish NTS leads, both FUTURE-dated (auction 7/10/2026)**,
      clean Everett/Marysville addresses, TS#/default/grantor populated. Re-verified after the settings edits.
      🔎 Cosmetic de-hyphen artifacts in grantor/trustee (`LUD -WIG`, `Mort -gage`) — pre-existing SHARED
      `nts_pdf` behavior (space-before-hyphen wrap), hits the crawler cache identically, match keys (parcel/addr)
      parse clean → matching unaffected. Logged as a follow-up, NOT folded in (shared code, needs own gate).
- [x] **Step 2 — Register** DONE. (a) `src.scrapers.snohomish_wa_pre_foreclosure` added to registry
      `_ALLOWED_SCRAPER_MODULES`. (b) Migration `060_add_snohomish_pre_foreclosure.py` (mirrors 040, INSERT-only,
      idempotent WHERE NOT EXISTS keyed on scraper_class, down_revision=059). ruff-clean (src); linear chain.
- [ ] **Step 3 — Deploy migration** (merge → Railway runs `scripts/migrate.py`). NO concurrent backfill
      during the deploy ([[incident_backfill_blocks_migration]]) — this migration is INSERT-only, no ALTER,
      but keep the rule.
- [ ] **Step 4 — Run E2E in prod**: ensure the Snoho crawler has run (fresh future-dated notices in
      nts_notices), run a Snohomish pre_foreclosure scrape, verify `match_job_inline` attaches
      `Result.auction_date`. NOTE: a match only writes auction_date if the current PDF carries a
      FUTURE-dated auction — surface honestly if this week is all past-dated.
- [ ] **Step 5 — Codex review** the full diff (gate). Critical/High in either reviewer = NO-GO.

## Then (low-urgency) — drop the derived encryption key
- [ ] Set `FIELD_ENCRYPTION_KEY` = PRIMARY key ONLY (first of the comma pair) on api + worker via
      `--set-from-stdin`, `railway redeploy` both, re-verify reads (`reencrypt_derived_key_pii.py --verify`
      = 0 derived already PASSED). [[incident_field_encryption_key_drift_2026_06_13]]
