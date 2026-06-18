# Fix: drop tax_delinquent from recorder connectors (Clark / Skagit / Chelan)

Branch: `fix/drop-recorder-tax-delinquent` (off origin/main, head 064)

## Problem (verified 2026-06-18)
`tax_delinquent` was offered on 5 active connectors, but only King + Snohomish
publish real property-tax delinquency (dollars owed + tax year) and are the only
members of `_TRUSTED_TAX_SOURCES`. The other three are recorder portals wired to
the wrong document type:

| County | Searches | Live scrape (120d) | Usable leads |
|---|---|---|---|
| Clark | Federal Tax Lien only (checkbox 97) | 159 FTLs | 0 (all dropped, no parcel_id) |
| Skagit | Federal Tax Lien only | 25 FTLs | 0 (25/25 dropped, no parcel_id) |
| Chelan | keyword (mixes FTL + Cert of Delinquency) | 0 rows | 0 |

A federal (IRS) tax lien is unpaid INCOME tax against a person — not county
property-tax delinquency — and has no parcel id, so the scraper drops 100% of
them. These connectors structurally cannot produce a tax_delinquent lead.

Codex consult (2026-06-18) agreed: disable Clark+Skagit immediately (mislabel is
config-evident), quarantine Chelan. Live scrape confirmed all three yield 0.

## Tasks
- [x] Verify live DB connector state (read-only query) — 5 carry tax_delinquent
- [x] Confirm scraper doc-type wiring (Clark/Skagit = Federal Tax Lien only)
- [x] Live non-persisting scrape of Clark/Skagit/Chelan — all 0 usable leads
- [x] Codex consult on remove-vs-relabel
- [x] Migration 066: remove tax_delinquent from clark/skagit/chelan record_types
      (preserve their other record types; idempotent)
- [x] Codex review of the diff (codex-collaboration gate) — no SQL bug; downgrade-order nit documented
- [x] Update docs/BUILD_JOURNAL.md
- [x] PR #67 opened
- [x] CI multi-head failure (065 merged to main first) → rebased onto 065;
      down_revision 064 → 065; single head 064→065→066
- [x] Codex re-review r2: P2 orphaned scraper_configs (9 active: 3 each) →
      migration deactivates them (idempotent)
- [x] Codex re-review r3: P2 batch dispatch ignores active → verified 0 batch
      children for these counties; P3 downgrade no longer reactivates configs
- [x] Codex re-review r4-5: fail-closed guard for DISPATCHABLE batch children
      (active batch OR pending/running batch_run); P3 downgrade over-restore documented
- [x] Codex loop STOPPED at r7 (judgment): remaining ask = terminalize in-flight
      jobs in the migration → HELD (would race the worker; wrong layer). Filed as
      worker-layer capability guard in backlog. All remaining items P2, not NO-GO.

## Out of scope (backlog — Codex point #4)
- API should not OFFER any record type a connector can't fulfill (structural
  guard: only `_TRUSTED_TAX_SOURCES` counties may carry tax_delinquent).
- Worker-layer capability guard: `run_scrape_job` + `dispatch_batch_run` should
  check the connector still supports (county, record_type) and skip/cancel
  gracefully instead of failing with UnsupportedCountyError. This is the right
  home for the in-flight-job/batch-dispatch robustness Codex raised (rounds 6-7),
  not a one-shot migration.
- Connector `health_status` has no record-type-level canary (tax_delinquent
  showed healthy while producing 0).
- Scheduler uses `record_types[0]` only — advertised-but-never-auto-scraped
  inventory for multi-type connectors.
- Investigate whether acclaimweb keyword-mode grid-read is broken for Chelan
  (0 rows/day across the window is suspicious) BEFORE ever re-enabling.

## Notes
- Scraper doc-type maps (clark_wa / skagit_recording / acclaimweb) still list
  tax_delinquent. Left in place intentionally: the migration stops routing, and
  re-enabling must go through a record-type-level canary first (backlog).
- MERGE: 066 and notifications 065 are both children of 064. Whichever merges
  second must rebase down_revision to avoid two alembic heads (Railway boot
  runs `alembic upgrade head`, which fails on multiple heads).

## Review
- Migration 066 compiles (py_compile OK); branch DAG is single-head (066 → 064),
  no shared-parent multi-head within the branch.
- Codex review: no correctness bug in the SQL — correlated subquery unambiguous,
  upgrade preserves order, `?` membership correct, NULL/`[]` safely skipped,
  `::json` cast correct, scope excludes king/snohomish. Only flag: downgrade
  appends rather than restoring original index → accepted Low + documented
  (order is functionally irrelevant; matching is membership-based, scheduler
  uses record_types[0]="probate").
- NOT applied to any DB. Pure data migration; runs on merge via Railway boot.
- Files changed: alembic/versions/066_drop_recorder_tax_delinquent.py (new),
  tasks/todo.md, docs/BUILD_JOURNAL.md.
