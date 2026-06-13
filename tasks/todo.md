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
- [ ] **Phase 2 — Snoho crawler beat** in `nts_crawler.py`: discover PDF URL from snoho.com (safe_get host-pin) → `safe_download_to_file` → extract→normalize→split→parse→`notice_to_row` (county='snohomish') → upsert. Parametrize county/source (currently hardcoded 'pierce'). Register weekly beat.
- [ ] **Phase 3 — Snoho matcher wiring**: match Snohomish pre_foreclosure Results (same scoring; county gate). Verify in prod via railway run.
- [ ] **Phase 4 — King (Queen Anne & Magnolia News)**: reuse Phase 1 infra; `crawl_nts_king_queenanne()` (county='king'), URL discovery from queenannenews.com. PARTIAL coverage (document gap). King matcher wiring.

## Notes
- King build-vs-buy: building free Queen Anne PDF (partial); DJC ($350/yr, complete) deferred to user.
- `nts_notices` already FORCE'd + system policy; Snoho/King crawlers reuse the verified system_sync_session write path.

## Review
(to be filled at end)
