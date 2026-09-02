# Test 2 data-quality audit — Pierce WA pre_foreclosure (2026-09-02)

Branch `fix/test2-data-quality`, worktree `C:/Users/Windows/bridgeleads-worktrees/test2-dq` (off origin/main 5106fe0).
Subject: scraper config "Test 2" (fde53328, pierce/WA/pre_foreclosure), job e72bd6bf, 217 results.

## Findings (all verified against prod DB + live sources)

- [x] Auction Date / Default Owed blank on every row. Source facts: the ARMS recorder grid never carries
      them (they are inside the document image); the product attaches them from the Tacoma Daily Index
      newspaper cache (`nts_notices`) by parcel/address. RCW 61.24.040 puts publication 28–35 days before a
      sale that is itself >= 90/120 days after recording, so publication is 55–150 days AFTER recording.
      None of Test 2's trustee sales (recorded 6/3–9/1) had been published as of 9/2 -> genuinely null today.
- [x] **BUG (app loses data):** the daily re-match beat only considers Results created in the last 45 days
      (`nts_matcher_task._RECENT_DAYS`). 21 Pierce pre_foreclosure Results (created 6/23–7/1) have an EXACT
      parcel match to an ACTIVE notice fetched 9/2 and were never matched — they aged out. Test 2 rows
      recorded after ~mid-July would go permanently blank the same way.
- [x] 12 rows parcel-but-no-address: all are Pierce personal-property (mobile-home) accounts (counterparty
      = MHP/HOA; ATIP shows "Account Type: Mobile Home"). The county GIS Tax_Parcels layer + WA statewide
      layer return 0 features for them (verified live). ATIP has the site/mailing address but its API is
      reCAPTCHA-gated and the portal cites RCW 42.56.070(8). -> product/legal decision, no code.
- [x] 3 rows name-only: verified on ARMS detail pages that the Legal Description tab has NO Parcel Id
      (2 TRUSTEE SALE, 1 LIS PENDENS). GIS legal lookup ambiguous. Real source records; keep, no fabrication.
- [x] 2 recorder-typo parcels (9066600050 vs real 9066000050; 718500090 vs real 7185000190): existing
      probate-only legal-repair guards would reject both classes. Leave; report.
- [x] Scraper discards the real ARMS document type (grid carries NOTICE OF DEFAULT / NOTICE OF FORECLOSURE /
      LIS PENDENS / TRUSTEE SALE) and stamps "PRE-FORECLOSURE" on every row -> users cannot tell which rows
      can ever carry an auction date.
- [x] Placeholder / dummy / fake scan: none. 217 real instruments, no company-token party names, no
      repeated instrument, 4 parcels with 2 filings each (legit distinct documents).

## Plan

- [x] Consult Codex on fixes (GATE: PASS; ship A with behavior test + re-notice caveat; B only after
      consumer + hash check — done: no consumer keys on the value; hash caveat documented).
- [x] Fix A: widen `_RECENT_DAYS` to the statutory horizon (180d) with rationale; behavior test on a real
      test DB (lead created 120d ago + active exact-parcel notice -> enriched) + tripwire test.
- [x] Fix B: Pierce `_map_row` stores the real ARMS document type (closed-set exact match against the
      configured checkbox labels; pre_foreclosure only; fallback unchanged). Fixture-based unit tests.
- [x] Run related tests on the local rig (49 passed); ruff clean.
- [x] Codex diff review (GATE: PASS, no P1); P2 already covered by the DB test; §14: no new inputs,
      endpoints or SQL shapes — doc_type is a closed-set match, the window is a bound parameter.
- [x] Browser/E2E: logged in as the user, opened Test 2 — headers include AUCTION DATE / DEFAULT OWED,
      API `has_auction_data=true`, 0/50 items with auction data, 7 null addresses + 3 null parcels on
      page 1, no console errors. Patched parser validated on 2 LIVE ARMS grid pages (48 rows, 0 mismatches).
- [x] Journal entry + review section below.

## Review

- Root causes: (1) NTS re-match window shorter than the statutory publication lag (app lost data);
  (2) Pierce scraper discarded the per-row document type the source prints (app lost data);
  (3) mobile-home accounts absent from every parcel GIS layer (source-layer gap; assessor portal is
  captcha-gated + RCW 42.56.070(8) — escalated); (4) name-only rows: recorder index has no parcel
  (source gap; kept); (5) two recorder typos outside the repair guards (source gap; left).
- Changed: `src/workers/nts_matcher_task.py` (window 45→180 + rationale), `src/scrapers/pierce_wa_probate.py`
  (`ARMS_DOC_TYPE_LABELS`, `_grid_doc_type`, stale comment removed), tests (+9), journal, this file.
- Historical Test 2 rows: no manual patching. The beat will enrich them as notices publish; the 12
  mobile-home rows and 3 name-only rows stay as they are (honest nulls). Existing rows keep the
  legacy `PRE-FORECLOSURE` doc_type — only new scrapes carry the real label.
- Unverified: prod effect of the window fix (needs deploy + beat run + newspaper publication).
