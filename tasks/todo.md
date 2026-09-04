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
