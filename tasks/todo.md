# Test 7 data-quality audit (King WA probate, job f19f9cc5)

Branch: `fix/test7-data-quality`  ·  Worktree: `C:/Users/Windows/bridgeleads-worktrees/test7-dq`

## Verified root causes (live source + prod DB, 2026-09-03)

1. **`PUBLIC` in Party Name** — King LandmarkWeb indexes a death certificate's
   counterparty as the literal placeholder `PUBLIC` / `THE PUBLIC` / `PUBLIC THE`
   (101 of 204 raw rows in the Test 7 window have it in the GRANTEE slot). In 8 rows
   the recorder indexed the parties REVERSED, so the placeholder/agency sits in the
   GRANTOR slot and the DECEDENT in the grantee slot. `orient_probate_party` has no
   rule for `PUBLIC`, and its agency regex only matches the `DEPT OF HEALTH` word
   order — so `WASHINGTON STATE HEALTH DEPARTMENT`, `DEPARTMENT WASHINGTON STATE
   HEALTH` and `WASHINGTON STATE-GOVT` all reach `party_name`. Category C
   (semantic mapping defect), NOT a row/column shift: all 121 stored rows match the
   source on instrument, date, parcel, legal and doc_type.
2. **Missing Property Address** (result `45472c60`, parcel 3751604519) — King's own
   Site Address cell is empty; GIS reports `vacant_no_situs` (vacant single-family,
   ZIP 98001). Category B: source genuinely lacks it. Keep NULL.
3. **July 15 Health Department missing Mailing Address** (result `4eae622c`) — the
   recorder's legal text carries an 11-digit PID `64116000027` (King PINs are 10).
   eRealProperty SILENTLY TRUNCATES to the first 10 digits and serves a DIFFERENT
   parcel (641160-0002, owner SNYDER JACOB). So the lead got the WRONG property
   address and a wrong `assessor_current_owner`, and the mailing lookup found no
   tax account. Category A: application defect (trusting a truncating lookup).

## Plan

- [ ] Phase 1 — `src/scrapers/probate.py`: placeholder-party rule, wider agency
      word orders, `<STATE> STATE-GOVT`, and heirs sanitation. Tests.
- [ ] Phase 2 — `src/scrapers/enrichment/king_county_assessor.py`: verify the
      "Parcel Number" the assessor page echoes matches the PID we requested; gate
      BOTH `_fetch_king_owner` and `batch_enrich_king_county`. Tests.
- [ ] Phase 3 — King probate scraper: never ship a probate lead whose party_name
      resolved to nothing. Tests.
- [ ] Phase 4 — reusable repair script + apply to prod (party/heirs re-orientation,
      wrong-parcel address/owner clearing, cancel the 2 queued skip-trace rows).
- [ ] Phase 5 — Codex diff review, full test suite, E2E verification in the app.

## Codex consult (design, pre-implementation) — verified independently

- [P1] Echo verification must also cover `_fetch_king_owner` / `batch_extract_king_owners`
  (owner-only path + 2 backfill scripts). **CONFIRMED** by reading the code — same URL,
  no echo check. Adopted.
- [P1] Agency/placeholder values in the GRANTEE slot are left in `heirs` untouched when
  the grantor is person-like. **CONFIRMED** (`party, heirs = (g or None), (e or None)`).
  205 prod rows carry `heirs='PUBLIC'`. Adopted.
- [P1] Repair scope too narrow — skip-trace residue. **CONFIRMED, and worse than stated:**
  2 `pending_skip_trace_rows` are sitting in `status=queued` with
  `property_address='11524 MERIDIAN AVE N 98133'` — a stranger's house. If the dispatcher
  drains them, BridgeLeads pays Tracerfy for the wrong property and attaches that
  stranger's phone/email to the REINKE lead. Adopted; repair must cancel them.
- [P2] Reject non-10-digit King PIDs at extraction. **PARTIALLY adopted — documented
  disagreement.** Dropping `parcel_id` drops the whole lead (`if parcel_id:` gate), which
  would destroy a verified-real death-certificate record over a county typo. The brief for
  this task says missing source data stays null/empty, not that the record is discarded.
  Phase 2's echo check already removes the actual harm (wrong address, and therefore the
  skip-trace enqueue, which requires a non-null address). Keeping the row + provenance.
- Codex note adopted: the repair must pass the stored `doc_type` into
  `orient_probate_party` or it bypasses the Transfer-on-Death guard.
- Codex note adopted: parse the `Parcel Number` cell label-specifically, never "first
  10-digit number on the page".

## Progress

- [x] Phase 1 — `src/scrapers/probate.py` placeholder + agency word orders + heirs rule (`6c906c2`)
- [x] Phase 2 — King assessor parcel-echo verification, both call sites (`5d07415`)
- [x] Phase 3 — never ship a party-less probate lead (`6c60c3c`, extended to 4 more scrapers in `023daf0`)
- [x] Phase 4 — repair script + APPLIED to prod (`2898cd6`, `023daf0`, `cc4c843`, `0007fea`, `ecfd2ac`)
- [x] Phase 5 — Codex diff review (3 rounds), full suite, E2E in the live app

## Review

**Test 7 passes the data-quality audit.** All 121 leads re-verified against King County's own
systems after the repair.

| Field | Result |
|---|---|
| Source fidelity (instrument, date, doc_type, parcel, legal) | **121/121 exact**, 0 mismatches |
| Party name | 0 null, 0 placeholder/agency, **121/121 trace to a real source party** |
| Heirs | 0 placeholders; every non-null value traces to the source grantee |
| Parcel ID | 120 well-formed 10-digit King PINs; 1 preserved verbatim as the county printed it |
| Property address | 103 agree with King GIS, 16 agree with eRealProperty (condo units absent from the GIS layer), 1 legitimately NULL (vacant parcel), 1 correctly NULL (malformed county parcel). **0 disagreements** |
| Mailing address | 1 NULL (the malformed-parcel row); 103 owner-occupied, 16 absentee |
| Auction date / Default owed | 0 — correct: probate records carry neither at the source |
| Phone / Email | 0 — skip trace still queued (119 queued, 2 not_attempted) |

**Root causes, all three confirmed against the live source:**

1. `PUBLIC` = the King recorder's PLACEHOLDER counterparty on a death certificate (101/204 raw
   rows carry it in the grantee slot). In 8 rows the parties were indexed REVERSED, so it reached
   `party_name`. **Category C** — semantic mapping defect, not a row/column shift. Confirmed NOT a
   shift: all 121 rows match the source exactly on every other field.
2. Missing Property Address (result `45472c60`, parcel 3751604519) — King's Site Address cell is
   empty and GIS reports `vacant_no_situs`. **Category B** — the source genuinely lacks it.
3. July 15 Health Department (result `4eae622c`) — the county's legal carries an 11-digit PID;
   eRealProperty silently truncated it and served a DIFFERENT parcel. **Category A** — application
   defect. The lead had a WRONG property address, not merely a missing mailing one.

**Notable:** the wrong address had already enqueued 2 skip-trace rows against a stranger's house.

## Codex review — 3 rounds, 9 findings, all independently verified

Round 1 (design) 4 findings · Round 2 (diff) GATE FAIL, 5 findings · Round 3 GATE **PASS**,
2 findings · Round 4 GATE **PASS**, **0 findings** — confirmed both round-3 fixes and that the
repair script is safe against live customer data.

Adopted: 8 of 9. One [P2] declined with reasoning — rejecting non-10-digit PIDs at extraction
would DROP a verified-real death-certificate lead over a county typo, and the echo check already
removes the harm. 🔑 My own fix for finding #6 introduced finding #7 (the residue guard over-fired
on retained entity parties) — tightening one guard opened another.

## Not done / limitations

- The correct parcel for the July 15 lead is almost certainly `6411600027` (assessor owner
  REINKE NORMAN L, 11547 CORLISS AVE N — matching the decedent), but choosing which digit to
  delete from `64116000027` is a guess and `parcel_id` feeds the FROZEN `dedup_hash`. Left as the
  county printed it. **Recovering it is a human decision.**
- 3 non-probate rows (2 King parcels) keep an address obtained through the truncating lookup.
  Correct today (the assessor owner corroborates the lead's party) but unverifiable in principle.
- The 2Captcha key is dead (`ERROR_KEY_DOES_NOT_EXIST`). The King search still succeeded without
  a solved token, but the captcha path is unprotected.
- **No dead code was removed** — nothing in the touched paths was verified unused.

# Test 6 — King County WA `trustee_sale` data-quality audit (2026-09-03)

Job `3dca2765-fea5-4db6-bc91-367eccc2d047` · config `24647671` "test 6" · window 06/04–09/02/2026 · **1 lead**

## Verdict on the user's question

The one delivered lead has **all six fields real and correct** (verified against the source PDF
and the King assessor). The defect is not field quality — it is that **only 1 lead came back**.

## Findings

- [x] **F1 (P0) — `_AUCTION` regex cannot match the Affinia notice layout → notices silently DROPPED.**
  `nts_tacoma_index.py:163` requires a location clause BETWEEN the time and the literal
  "sell at public auction". Affinia Default Services prints the location AFTER it
  ("…will on 08/14/2026, at 10:00 AM sell at public auction located at the 4th Avenue Entrance…").
  All three variants (`_AUCTION`, `_AUCTION_KING`, `_AUCTION_WORDED`) return False.
  No auction_date → `is_valid_nts()` False → the whole notice is discarded, counted only as `skipped`.
  **Measured on live PDFs with production code:** 08-05-26 issue 3 of 5 dropped; **current 09-02-26
  issue 2 of 2 dropped (100%)**. Affinia is a high-volume WA trustee.
- [x] **F2 (P1) — stale cross-bound TS numbers in prod `nts_notices` (King).** Pre-`ec5a3d6` crawls
  bound each notice to the NEXT notice's TS#. Prod: `WA07000020-26-1` carries GUILER's data
  (it is really MEKMORAKOTH's); GUILER's real `WA05000073-24-2` is absent; MEKMORAKOTH is
  stored under the surrogate `REF-20231006000715`. The `ec5a3d6` parser fix IS correct for King
  (re-ran it on the same PDF — both TS#s now bind correctly), but `scripts/repair_nts_ts_number.py:57`
  hardcodes `SOURCE = "snohomish_tribune"`, so King rows were never repaired and cannot self-heal
  (`_upsert_notice` keys on (source, ts_number) → a corrected TS# INSERTS a new row).
  Effect on Test 6: `WA07000020-26-1` inherited GUILER's past auction date → `is_active=False`
  → a live King lead was suppressed.
- [x] **F3 (P2) — silent drops are unobservable.** A notice discarded for a missing auction date
  increments only `summary["skipped"]`; no log, no alert, no metric. F1 ran undetected.
- [x] **F4 (P2) — no catch-up / no re-sweep for PDF sources.** `_discover_latest_legals_pdf` takes
  only the newest issue and the legals page exposes no archive; the King task runs Thursdays only
  (`scheduler.py:182`). A missed/failed week is lost permanently. `_resweep_null_amount_notices`
  is wired for Tacoma + Clark but never for the PDF sources.
- [x] **F5 (business, not a bug) — King coverage is structurally thin.** Queen Anne & Magnolia News
  is a neighborhood paper; King's dominant foreclosure venue is the DJC (paid, deferred —
  `nts_crawler.py:64`). Even with F1–F4 fixed, King will under-cover.
- [x] **F6 (design, worth surfacing) — the job's date window is discarded** for `trustee_sale`
  (`trustee_sale.py:158` `del date_from, date_to`) and `date_recorded` falls through to the auction
  date, so delivered rows legitimately sit OUTSIDE the requested window (the 9/4/2026 row in a
  06/04–09/02 job). Intentional, but nothing tells the user.

## Status

**P0 SHIPPED — PR #200 open, Codex gate CLEAN after 3 rounds, full suite 2091 passed / 0 failed.**
Both live King issues now yield 7 of 7 notices kept, 0 dropped, all active.
A third defect surfaced during the Codex gate and is fixed in the same PR: **inline sale
postponements were ignored**, so TS `WA05000073-24-2` (sale genuinely 09/18/2026,
$155,361.99 owing) was stored with its stale 06/26/2026 date, marked inactive, and
suppressed — another reason Test 6 came back nearly empty.

## Plan

- [x] **P0-1** Add `_AUCTION_LOC_AFTER` as a 4th fallback in `nts_tacoma_index.py`, tried only after
      the existing three miss (byte-identical behavior for every layout that parses today).
      Per Codex: anchor on `Trustee\s+will`, capture location as bounded `[\s\S]{0,300}`.
- [x] **P0-2** Tests: 3 Affinia positives; unchanged-output regression for the existing 3 regexes;
      a 45k-char adversarial block asserting no catastrophic backtracking (hard runtime bound).
- [x] **P1-1a** `--source` added to `scripts/repair_nts_ts_number.py` (King paired with
      `parse_king_notice`; snohomish default unchanged). Committed.
- [ ] **P1-1b** 🔒 BLOCKED ON USER — running the repair (even the dry run) and linking the
      worktree to Railway were both denied by the auto-mode classifier. The repair must be
      run by the user AFTER PR #200 deploys:
      `railway run --service worker python scripts/repair_nts_ts_number.py --results --notices --source queen_anne_news`
      (dry-run first; add `--apply --i-confirm-fixed-parser-is-deployed` to write).
      Known King state needing repair:
        * `WA07000020-26-1` holds GUILER's data — truth says MEKMORAKOTH / parcel 259900081003
        * `REF-20231006000715` is MEKMORAKOTH under a surrogate key; the Test 6 lead hangs off it
        * `WA07000014-24-4` GUILER 06/26 — superseded twin
        * `WA05000073-24-2` (GUILER, 09/18/2026, $155,361.99) is MISSING entirely.
          Its published run includes 09/09/2026, so the post-deploy crawl should re-ingest it.
- [x] **P2-1** Log + `_alert_if_crawl_barren`-style signal when notices are dropped for a missing
      auction date (F3) — the silent drop is the real production failure.
- [ ] **P2-2** Decide: re-sweep / catch-up for PDF sources (F4).
- [ ] **F5/F6** → user decision, no code.

## Verification
- [ ] Re-run the production splitter+parser over both King issues: expect 5/5 and 2/2 kept.
- [ ] Full pytest via the isolated rig; then Codex `review` gate on the diff.
- [ ] After deploy, confirm the Thursday King crawl upserts > 0 and re-run "test 6".
