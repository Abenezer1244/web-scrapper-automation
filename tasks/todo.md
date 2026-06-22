# Task: Quarantine the 1,968 mislabeled Clark `tax_delinquent` rows

> Decision (2026-06-21): Clark `tax_delinquent` re-activation is **ABANDONED** (BACKLOG §9).
> The 1,968 existing rows are DEED / DEED OF TRUST / MODIFICATION recorder docs mislabeled
> `tax_delinquent` (0% delinquent_amount/bill_year), scraped 2026-04-10 by an immature scraper.
> They DO carry real owner + address (PR #95 enriched legals). Codex consult: quarantine, don't
> silently relabel; preserve recoverability; check blast radius first.

## Verified ground truth (read-only, prod)
- 1,968 rows, all from 2 done jobs on 2026-04-10: `c7376f66-0a00-403f-b7db-2bcac14204f4` (1,161),
  `639aae57-9cbb-453f-a466-6c214d6736fa` (807).
- `enrichment_data.source = 'clark_county_recorder'` (100%); `doc_type` = DEED/DEED OF TRUST/MODIFICATION.
- parcel_id 100%, party_name 100%, mailing 96%, legal 97%; delinquent_amount/bill_year **0%**.
- All 3 Clark tax_delinquent `scraper_configs` already `active=False` (mig 066, 2026-06-19).
- Connector record_types = `['probate','pre_foreclosure']` (tax_delinquent already removed).

## Phase 0 — Blast-radius investigation (READ-ONLY, no writes) — DO FIRST
- [ ] Which tenant(s) own the 1,968 rows? (group by user_id)
- [ ] **Billing:** were these rows/jobs billed? Check `dedup_hash` / any per-lead or per-export billing
      events tied to these job_ids or result ids. (If billed → surface as a credit/support question, don't auto-refund.)
- [ ] **Lists:** are any of the 1,968 in `property_list_membership`? How many lists, which tenants?
- [ ] **property_key overlap:** do these parcels overlap real leads in other record types for the same tenant?
      (Don't globally delete shared owner/property identity — only remove the tax_delinquent membership.)
- [ ] **Exports:** any delivered exports/jobs that included these rows (can't claw back — just document).
- [ ] Decide the exact reversible mechanism from the findings (below is the default).

## Phase 1 — Reversible quarantine script (DRY-RUN first, --apply gated) — AFTER Phase 0 + approval
- [ ] New `scripts/quarantine_clark_tax_mislabeled.py`, modeled on the existing backfill scripts
      (job_id-scoped, TOCTOU-safe, idempotent, DRY-RUN default, `--apply`, `--limit`).
- [ ] Scope EXACTLY: results where county=clark/WA, record_type=tax_delinquent, source=clark_county_recorder
      (re-assert per row; never a blanket county delete).
- [ ] Mechanism (default, reversible):
      (a) stamp `enrichment_data.quarantined = {reason, decided:'2026-06-21', backlog:'§9', prev_membership:[...]}`;
      (b) remove their `property_list_membership` rows (store removed ids in the marker for restore);
      (c) do NOT delete the `results` rows; do NOT relabel record_type.
- [ ] Reversal path documented + a `--restore` mode (reads the marker, re-adds membership, clears flag).
- [ ] Worker can UPDATE these rows under RLS (system session) — verify, no owner DSN needed (precedent: PR #85 backfill).

## Phase 2 — Verify + record
- [ ] Re-run `scripts/diag_clark_tax_rows.py` + a list/export check to confirm they no longer surface.
- [ ] Codex review of the script diff (gate). Any P1 = fix before --apply.
- [ ] BUILD_JOURNAL entry; tick BACKLOG §9 quarantine item.

## Open question for the user (before Phase 1 --apply)
- Confirm the quarantine mechanism (remove from lists + audit marker, keep rows) vs. a harder delete.
- Confirm whether to also do the two §9 follow-ups now or later: the `tax_delinquent` requires-amount+year
  invariant, and the "100% parcel_id" enrichment-over-inference check (Codex point C).

## Review
_(to be filled in after execution)_
