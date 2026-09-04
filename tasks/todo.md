# "Test 5" data-quality audit + Records-count inconsistency (2026-09-03)

Worktree `C:/Users/Windows/bridgeleads-worktrees/test5-dq`, branch `fix/test5-data-quality` (off origin/main).
Subject: scraper config "Test 5" (snohomish/WA/**pre_foreclosure**), job `425d49ce`, 4 rows.
Frontend worktree `C:/Users/Windows/bridgeleads-worktrees/fe-test5`, branch `fix/test5-records-count` (off origin/master).

## Verified findings (evidence gathered before any code change)

1. **Parcel IDs are legitimate source values — BridgeLeads is NOT altering them.**
   The Snohomish County Tribune legals PDF prints all four verbatim under
   `Parcel Number(s):` — `00876100600800`, `01133800000900`, `008337-000-009-00`,
   `010347-00-0086-00`. Quality Loan Service Corp prints Snohomish PINs bare;
   North Star Trustee hyphenates (and not even consistently: 6-3-3-2 for one
   property, 6-2-4-2 for another). Every value is 14 digits with leading zeros
   intact. `property_identity.normalize_parcel()` strips hyphens before computing
   `dedup_hash`/`property_key`, so the variation cannot break dedup or billing.
   **No code change — the variation is the source's, and it must be preserved.**

2. **`Records = 0` vs 4 leads is a PRESENTATION defect, not a counting bug.**
   `jobs.record_count` = billable (non-duplicate + actionable) = 0, and that is
   load-bearing: `workers/tasks.py` commits it in the same transaction as billing so
   the headline, email, webhook and bill can never disagree.
   `/jobs/{id}/results.total` deliberately INCLUDES duplicates = 4.
   The detail page rendered `total` while the list rendered `record_count`, so two
   surfaces labelled two different rules identically, with nothing explaining the gap.
   All 4 rows are duplicates of **"Test 4"** (snohomish/trustee_sale, run 4 minutes
   earlier) — both record types are sourced from the SAME NTS legal notices, and
   `delivered_records` is keyed `(user_id, dedup_hash)` with no record_type scope.

3. **The "all N were duplicates" banner was unreachable.** Gated on
   `resultsPage.total === 0`, but `total` includes duplicates, so an all-duplicate
   job always has `total > 0`. It could never fire for the one case it explains.

4. **2 of the 4 leads carried a stale, wrong top-level `enrichment_data.ts_number`**
   (CASEY CATE `25-10595`, SHAWN WEINTRAUB `26-78299`). Reproduced exactly by
   re-running the pre-PR#195 header-only split over the real source PDF. The correct
   values live in `enrichment_data["nts"]` / `nts_notices`. PR #195 fixed the scraper
   AND repaired the nested copy, but its repair never modelled the top-level key that
   `snohomish_wa_pre_foreclosure.py` writes from its own parse. 6 rows / 3 jobs.

5. **`SHAWN M WEINTRAUB` was falsely flagged `absentee_owner = True`** while living in
   the house. The NTS situs line has no comma before the city ("1207 118TH PL SW
   EVERETT, WASHINGTON 98204-4813"), so the comma-splitting parser glues the city into
   the STREET. That shape only ever survived by accident — via the whole-string
   equality shortcut — which breaks the moment one side carries ZIP+4 and the other
   ZIP5. Measured in prod: 1 row of 2,432 absentee rows.

## Tasks

- [x] BE: add `new_count` to `ResultsPage` (the same predicate `record_count` bills on)
- [x] BE: regression tests — 0 / 1 / many / all-duplicate / unactionable / view-filtered
- [x] BE: fix the glued-city false absentee in `address_intel._addresses_differ`
- [x] BE: absentee regression tests pinned to the real Test 5 rows
- [x] BE: export test — a stale top-level ts_number never overrides the nested one
- [x] BE: parcel test — the two Snohomish house formats are dedup-equivalent
- [x] Repair: extend `repair_nts_ts_number.py --results` to the top-level key; APPLIED (6 rows, converged to 0)
- [x] FE: detail headline shows the list's rule (`new_count`) + duplicate context
- [x] FE: fix the unreachable all-duplicate banner guard
- [x] FE: duplicate rows say "Duplicate", not "Old"
- [x] Codex review of the diff
- [x] E2E re-verify against production

## Reported, deliberately NOT changed (owner decision, not mine)

- **CSV export includes duplicate rows.** `/jobs/{id}/download` and the scheduled R2
  export filter on actionable + tax cap but NOT `is_duplicate`, so Test 5's CSV has
  4 rows while the email said "0 records". Changing it alters delivered files for
  every existing customer and every scheduled delivery; adding an `is_duplicate`
  column changes the dialer-ready CSV contract customers have mapped. Codex agrees
  this is ambiguous rather than clearly wrong, and wants it pinned by an explicit
  product decision either way.
- **Cross-record-type dedup.** Snohomish pre_foreclosure and trustee_sale draw from
  the same NTS notices and collide on `(user_id, dedup_hash)`. Codex and I both read
  this as intended ("never bill the same property twice"); scoping the key by
  record_type would double-bill and weaken skip-trace reuse.
- **The Lee/Sang Ki commercial notice is dropped** — `auction_date` unparsed from
  "the 17th day of September, 2026", so `is_valid_nts` rejects it. A pre-existing
  parser coverage gap, present in both PDFs, unrelated to Test 5's four leads.

---

# (earlier session, kept for reference)

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
# Plan — backfill convergence + real property_state (post-#188 cross-check)

Branch `fix/backfill-convergence-and-situs-state` off `origin/main` @ `1b964d9`.
Found by cross-checking the #188 build against its own production evidence, not by a
new user report. Codex consulted on the design before any code was written.

## What the cross-check found (all MEASURED against prod, not assumed)

1. **The backfill does not converge.** 6 `--apply` runs of rule K wrote 180 rows but touched
   only **39 distinct ids**; 23 ids appear in all 6 runs. When the assessor's real mailing IS
   the property (owner-occupied), the write leaves `mailing LIKE property || '%'` still true,
   so the row stays a candidate and the `ORDER BY created_at,id` + `[:30]` head never advances.
   The handoff's "repeat until candidates: 0" can never terminate.
2. **The handoff's remaining-work figure is wrong.** Not "217 candidates, 180 done, ~37 left".
   Measured now: **King 847 candidates, only 30 stamped → ~817 actually remain.**
   Pierce 1218 candidates / 1175 stamped → 43 remain. Snohomish 0.
3. **The backfill NULLs `property_state` on every row it touches.** It calls
   `compute_owner_flags(r.property_address, ...)` with no structured situs, so the state is
   parsed from the FROZEN street-only line and always comes back NULL — which also forces
   `out_of_state_owner` NULL. Blast radius: **1,286 prod rows** (1,229 pierce_county_gis,
   39 king_assessor_tax_bill, 18 none_no_source) all `property_state IS NULL`,
   `out_of_state_owner IS NULL`. The query is `sc.state='WA'`-scoped, so the state is knowable
   with certainty. This defeats the stated goal of audit item 4.
4. Codex P2 confirmed in code: `enrichment_data` is `Column(JSON)` (models.py:659), not JSONB —
   the `||` merge needs an object guard and an explicit cast back to `json`.
5. Codex P3 (`LIKE` treats `%`/`_` in property_address as wildcards) is real but has
   **zero** live impact: 0 of 23,284 property_address values contain either character.

## Codex findings ADOPTED
- [P1] Terminal/retry stamp so skips cannot pin the LIMIT-30 head (`mailing_backfill_status`).
- [P1] Keep `mailing_source` = provenance only; workflow state gets its own key.
- [P2] `jsonb_typeof` object guard + explicit `::json` cast back on the merge.
- [P2] Stronger write guard + require `rowcount == 1`, count conflicts separately.
- [P2] `property_state` from `sc.state`, never parsed from the street-only line.
- [P3] Replace `LIKE` with a metacharacter-free prefix comparison.

## Codex finding REJECTED, with evidence
- Codex's *stricter* predicate (require `=` or a `,` delimiter after the street) **drops 9 real
  candidates** in prod — including `'20508 ISLAND PKWY'` vs `'20508 ISLAND PKWY E, LAKE TAPPS…'`,
  which is precisely the truncated-situs case Test 1 defect #3 was about.
  Measured: LIKE 20,277 / STRICT 20,268. Adopting only the equivalent form:
  `left(upper(mailing), length(property)) = upper(property)` → 20,277, **0 lost / 0 gained**.

## Todo
- [x] 1. `_CANDIDATES`: metacharacter-free prefix predicate (proven equivalent) + `btrim <> ''`
      + exclude rows already carrying a terminal `mailing_backfill_status`.
- [x] 2. `decide()`/main: every selected row leaves a durable state — resolved / confirmed_same /
      not_found / retry_later; run-level ABORT stays for source-health (never stamp 30 retries
      when King is globally blocked).
- [x] 3. `_UPDATE`: object-guarded jsonb merge cast back to `json`; guard on
      `property_address` too; require `rowcount == 1`, report conflicts.
- [x] 4. Pass the real `property_state` (`sc.state`) + structured situs parts into
      `compute_owner_flags` so item 4's whole point actually lands.
- [x] 5. Repair script/mode for the 1,286 already-stamped rows whose `property_state` was nulled.
- [x] 6. Unit tests for convergence (a confirmed_same row must NOT be re-selected), the
      predicate equivalence, the json merge guard, and the state fix.
- [x] 7. Codex adversarial review of the diff; resolve to consensus; ruff + full related suite.
- [ ] 8. 👤 Decide the King scope (817 rows ≈ 28 runs ≈ 2h of a source that has IP-blocked us).

## Review

Shipped in one commit. `scripts/backfill_assumed_mailing.py` + its tests only — no runtime
code, so nothing in the request path changes.

**Codex round 2 (adversarial review of the diff) — 5 findings, 4 adopted, 1 rejected:**
- [High] retry_later rows still pin the ordered head — **ADOPTED** (found independently at the
  same time): bounded `--max-attempts` (default 3), retries sort after untried rows, exhausted
  rows go `failed_terminal`.
- [High] the K global-abort was a result-SHAPE heuristic, so an all-absent batch could stall for
  ever — **ADOPTED**: 'found'/'none' now count as real answers, 'not_attempted' takes a bounded
  retry stamp, and only an all-transport-failure batch aborts — which now exits **2** so it can
  never stall silently.
- [Medium] jsonb `||` merges but does not delete, so a re-decided row kept a `mailing_source` it
  no longer had a claim to — **ADOPTED**: the merge drops the old provenance/error keys first.
  Verified on prod: after re-deciding to not_found, `mailing_source` is NULL.
- [Medium] --repair-flags compared only 2 of the 4 flags before skipping — **ADOPTED**: compares
  all four.
- [Critical] "compute_owner_flags does not accept the structured-situs kwargs" — **REJECTED,
  false.** It does (src/utils/address_intel.py, added by #188); Codex saw only the script diff.
  The call also executed successfully against prod. Verified before rejecting.

**Verification.** 67 tests pass, ruff clean. The unit tests assert on SQL *strings*, which
cannot catch a runtime SQL error, so every new statement was additionally executed against the
LIVE production DB inside a transaction that always rolls back: `_CANDIDATES` (1,218 Pierce
candidates), `_UPDATE`/`_STAMP` rowcount 1, the stale-address guard rowcount 0, `enrichment_data`
still `json_typeof=object`, stale provenance dropped, `_REPAIR` in scope — then ROLLBACK, with
0 stamps left behind (re-checked in a fresh session).

**Proof the core bug is fixed:** on a real prod row the flags went
`property_state NULL -> 'WA'` and `out_of_state_owner NULL -> False`.

**Not done / handed back:** the King run itself (847 undecided rows ≈ 29 paced runs) is a
resource decision for the user — see todo 8. Pierce (1,218) and --repair-flags (1,286) are cheap
and ready to run.


---

# Test 4 — auction-date label + lead data-quality audit

Job: `90e5eb41-07ff-46ae-8d06-63bca40f67cc` (config `Test 4`, snohomish/trustee_sale, 6 leads)
Source: Snohomish Tribune legals PDF `Legals - 8-5-26.pdf`

## Findings (all verified against the real source PDF + prod DB)

- [x] "2D" = `days_to_auction` (int) + a literal "d", CSS-uppercased. Means **2 days until the auction**.
- [x] **P1 TS-number off-by-one / cross-record contamination** — `split_notice_blocks` orphans a
      pre-header `TS No <x>` into the PREVIOUS block. 2 of 6 Test 4 leads carry the next notice's
      TS number; the last notice in every such PDF is DROPPED (`is_valid_nts` needs a ts_number).
- [x] **P2 past auctions clamp to 0** — indistinguishable from "auction is today".
- [x] **P2 UTC clock for a Washington-local event** — Pacific-evening off-by-one.
- [x] property_city/property_zip NULL = STALE pre-fix data (fixed by 1b964d9, landed 21h AFTER the run).
- [x] mailing_address NULL = Case B — Snohomish publishes no mailing source.

## Tasks
- [x] Fix `split_notice_blocks` to bind a notice's pre-header identity preamble to its OWN block
- [x] Make `days_to_auction` signed; add a county-local auction clock (keep UTC for tax parity)
- [x] Frontend: replace `{n}d` with plain language, keep the date visible
- [x] Regression tests: tomorrow / in 2 days / today / yesterday / several days ago + splitter
- [~] Codex review — design review DONE (it rejected my first splitter draft, correctly).
      Post-implementation DIFF review NOT done: Codex hit its usage limit mid-run.

## Review

**Changed:** `nts_pdf.py` (pre-header identity binding, linear peeling), `lead_signals.py`
(signed `days_to_auction` + `auction_reference_date`), `lead_export.py` / `data_exporter.py` /
`schemas.py` (two frozen clocks through every export surface), FE `AuctionCountdown` +
`lib/utils.ts`, tests + a second real-PDF fixture.

**Verified:** all 8 notices in the source PDF now parse with their own TS number (was 3 wrong +
1 dropped); backend 1884 passed / 2 skipped; FE tsc + eslint + build clean; the label rendered in
real Chromium (light + dark) across in-5/in-2/tomorrow/today/yesterday/5-days-ago/45-days, no
console or network errors; the live API path returns `days_to_auction=1` for all 6 Test 4 rows.

**Deliberately NOT changed:** party names, parcel IDs, property addresses, auction dates and
default-owed values — all 6 verified correct against the source. Blank mailing addresses are a
real Snohomish source limitation.

**Left open:** historic prod rows not repaired (forward-only fix); `trustee_sale.scrape()` /
`nts_crawler` expiry still gate on a UTC date for a WA-local event; neither branch pushed.
