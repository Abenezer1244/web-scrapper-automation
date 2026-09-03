# "Test 1" lead data-quality investigation (2026-09-02)

Worktree `C:/Users/Windows/bridgeleads-worktrees/test1-data-quality`, branch `fix/test1-lead-data-quality` (off origin/main).
Subject: scraper config "Test 1" (pierce/WA/probate), job `1e358ca8`, 110 rows.

## Baseline (verified against prod API, prod DB read-only, and the live sources)
- party_name: 0 blank, no placeholders. Recorder grantor strings ("… EST OF") + heirs — correct for probate.
- parcel_id: 4 blank — ALL 4 verified parcel-less on the ARMS Legal Description tab (source has none). Correct nulls.
- property/mailing address: 5 blank = the 4 parcel-less + BAKKE (parcel 0121228036 absent from Pierce GIS AND WA statewide). Correct nulls.
- phone/email: 22 hits from the 25 "normal" Tracerfy rows; 79 "advanced" rows queued since 04:23 and NEVER submitted —
  every dispatcher tick since 04:25 fails 402 "Insufficient credits" on a 344-row batch and returns without trying anything smaller.
  Prod backlog: 565 rows / 7 jobs. UI says "Processing 10-15 min" forever. No ops alert.
- SAARENAS AVELINO G skipped (not_attempted) — `looks_like_non_personal_party_name` substring-matches " ave"/" way" →
  AVELINO/WAYNE/AVERY people excluded. Prod 90d: 43 rows / 12 jobs.
- absentee_owner=True false positives (2 in Test 1, 5 prod/90d): Pierce GIS Site_Address omits suffix/directional
  ("20508 ISLAND PKWY" vs "20508 ISLAND PKWY E").
- "View" on Scrapers/dashboard → `/scrapers/{id}/records` = SHARED `county_records` cache: 3,305 rows from March,
  doc_type NULL everywhere (passes every record_type filter), 302/647 Pierce rows with NULL party, 308 rows with the
  literal "(enrichment unavailable)", column-shifted rows. ENABLE_DAILY_SCRAPE=False → cache dead since March.
- Latent: WA statewide GIS fallback fabricates mailing_address from the situs address (→ absentee=False claims).
- Latent (Codex): skip-trace payload never fills mail_* fields (comment says "populated below" — nothing does).

## Plan
### Phase 1 — backend data-quality (pure functions + tests)
- [x] skip_trace.py: `looks_like_non_personal_party_name` → code-violation shapes only (category prefix, "? <digits>" separators, bare address as name); whole-word suffix match; entities keep advanced trace
- [x] skip_trace.py: `build_pending_row_payload` populates mail_* from mailing_address
- [x] address_intel.py: `_addresses_differ` tolerates a trailing suffix/directional omitted on one side (→ discriminators → None, not True)
- [x] county_gis.py: statewide fallback returns property_address WITH locality and mailing_address=None; generic parse else-branch → None
- [x] tests: new tests/test_skip_trace_eligibility.py, extend tests/test_address_intel.py, new tests/test_county_gis_parse.py
### Phase 2 — ops visibility + cached records
- [x] skip_trace_dispatcher.py: on 402 → `send_ops_alert` (cooldown) + submit the affordable FIFO slice (parsed from the 402 body; no-op if unparseable); rows claimed `FOR UPDATE SKIP LOCKED` AND moved to a committed `submitting` state before the POST (Codex High ×2); failure classification releases / errors / leaves-for-reconciliation; stale-claim ops alert; dialer sweep treats `submitting` as unsettled
- [x] scrapers.py `get_cached_records`: no `doc_type IS NULL` escape hatch; SQL mirrors the scraper matcher (word-boundary short codes + excludes, Codex Medium); map "(enrichment unavailable)" → null
- [x] scripts/backfill_owner_flags.py: `--recompute-suffixless` targeted mode for the known false positives
### Phase 3 — frontend (separate FE worktree/branch)
- [x] Scrapers page + dashboard row already prefer `/results/{latest done job}` on master; command palette fixed to match (FE `e7c6352`)
### Phase 4 — quarantine unactionable leads (owner decision, same session)
- [x] `src/api/lead_actionability.py`: one rule, three spellings (ORM / raw SQL / Python) + tests
- [x] standing filter in jobs.py results + download + total_scraped/duplicate_count, batch_export, segments ×4, analytics, dialer sweep + outbox
- [x] tasks.py: both exports filtered; billing block moved after enrichment; billable_count = non-dup actionable; display_count = billable_count; webhook count fixed
- [x] fixtures that built address-less "leads" given an address; Codex consult + adversarial review
### Verification
- [x] ruff clean; 91 focused tests + 263 across every module touching the changed code; full rig run (see Review)
- [x] Codex consult + round-1 review (FAIL → 3 adopted, 1 rejected with prod evidence) + round-2 review
- [x] Headless Chromium: real login → /results/1e358ca8 renders 110 rows with correct field mapping, 0 console errors, 0 API errors
- [x] prod: `--recompute-suffixless` dry-run 5 → `--commit` 5; Tracerfy credits = ops blocker (👤)

## Review
See `docs/BUILD_JOURNAL.md` 2026-09-02 entry. Source-unavailable fields (4 parcels, 5 addresses)
verified at ARMS / Pierce GIS / WA statewide and left null. Application defects fixed: name gate
false positives, unfilled mail_* payload, suffix-less absentee false positives, statewide
mailing fabrication, dispatcher out-of-credits stall (alert + partial drain + row claim), dead
county cache leaking into typed configs, palette routing. Not fixable in code: Tracerfy credits,
county_records purge (elevated role).
# Real owner-location data (audit items 3 + 4) — 2026-09-02

Branch: `feat/real-owner-location` · worktree `bridgeleads-worktrees/real-owner-location` off `origin/main` (`0bb74bc`)

User decision: "it should not assume the owner lives [at the property]… real data everywhere."
Option B on both items (Codex-recommended).

## Phase 1 — item 3: stop writing assumed mailing addresses (≤5 files)
- [ ] `county_gis.py`: statewide (single + batch) returns `mailing_address=None`; drop `_statewide_mailing`; generic-config `_parse_gis_response` fallback → None
- [ ] `ai_assessor.py`, `national.py`, `parcel.py` (ATIP): no situs-as-mailing fallback
- [ ] tests updated/added; Codex review
## Phase 2 — item 3 backfill (prod)
- [ ] NULL provably-assumed mailing rows (no real mailing source for that county/record_type) + recompute flags; evidence file; Codex review; run
## Phase 3 — item 4 schema
- [ ] migration 085: `results.property_city`, `results.property_zip`; model; `compute_owner_flags` accepts structured situs parts
## Phase 4 — item 4 fill at scrape/enrich time
- [ ] capture the scraper's full situs (notice "commonly known as") before GIS overwrites the street; statewide/Pierce/King situs city+zip where the SOURCE has them; insert + end-of-job flags use the parts
## Phase 5 — item 4 backfill (prod)
- [ ] fill city/zip for existing leads from real sources; recompute flags; Codex review; run

## Review
(pending)

Root causes were three independent defects, none in the UI: a parser regex that required
"principal" wording, a label-stop firing inside a parenthetical, and a batch-GIS dict keyed by
the server's parcel spelling while the worker keys by the lead's. Nothing was hard-coded to
Test 3; every fix is pinned by a real-source fixture or a real ArcGIS feature.

---

# Snohomish tax_delinquent — source layout change (2026-07-30)

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

## Round 2 — captcha passer + typo parcels (user follow-up)

- [x] Verified ATIP API + 2Captcha Enterprise token live (9/12 mobile-home parcels resolved, 1 solve).
- [x] `pierce_atip.py` + `captcha.solve_recaptcha(enterprise=)` + cache key `(sitekey, url, enterprise)`.
- [x] Legal repair: trailing BLK, edit-distance-1 guard gated on single survivor + plat adjacency; enabled for pre_foreclosure.
- [x] `enrich.pierce_address_recovery()` extracted; `scripts/rerun_pierce_address_recovery.py` (dry-run verified on Test 2).
- [x] Codex design consult (PASS) + two diff reviews; all code P2/P3 adopted.
- [x] Deleted two exploratory scripts with a hardcoded 2Captcha key. 👤 REVOKE that key (NO-GO until done).
- [x] Recovery run on Test 2 (user authorised): 11/12 filled (2 legal repair + 9 ATIP); `9009002080` not on
      file anywhere. Results page re-verified: only the 3 name-only rows blank; API enriched_count 202 → 213.
- [x] Push + PR; deploy worker + api (round 3).

## Round 3 — the 12-item list

- [x] 1 leaked key: already dead at 2Captcha (verified). 2 PR/merge/deploy: this session.
- [x] 5 King mailing time budget + deferred marker (Codex PASS). 6 parser layouts + 40-page ingest (Codex PASS).
- [x] 9 ARMS diff (0 parser losses). 11 other Pierce jobs recovered (26/28).
- [ ] 3/4 legal stances (user), 7/10/12 deferred with reasoning (journal).
