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

## Plan (awaiting approval before any code)

- [ ] **P0-1** Add `_AUCTION_LOC_AFTER` as a 4th fallback in `nts_tacoma_index.py`, tried only after
      the existing three miss (byte-identical behavior for every layout that parses today).
      Per Codex: anchor on `Trustee\s+will`, capture location as bounded `[\s\S]{0,300}`.
- [ ] **P0-2** Tests: 3 Affinia positives; unchanged-output regression for the existing 3 regexes;
      a 45k-char adversarial block asserting no catastrophic backtracking (hard runtime bound).
- [ ] **P1-1** Add `--source` to `scripts/repair_nts_ts_number.py`; dry-run then apply for
      `queen_anne_news`. Retire the mis-bound twin, re-key the surrogate row.
- [ ] **P2-1** Log + `_alert_if_crawl_barren`-style signal when notices are dropped for a missing
      auction date (F3) — the silent drop is the real production failure.
- [ ] **P2-2** Decide: re-sweep / catch-up for PDF sources (F4).
- [ ] **F5/F6** → user decision, no code.

## Verification
- [ ] Re-run the production splitter+parser over both King issues: expect 5/5 and 2/2 kept.
- [ ] Full pytest via the isolated rig; then Codex `review` gate on the diff.
- [ ] After deploy, confirm the Thursday King crawl upserts > 0 and re-run "test 6".
