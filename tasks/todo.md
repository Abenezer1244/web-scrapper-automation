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
