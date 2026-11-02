# Inline enrichment: no transaction held across network calls (Phase 1: the three High findings)

Branch `fix/enrich-network-transactions` (off main `9b4ca49c`). Same class of bug as #579's canary:
a transaction left open across minutes of network work is killed by the pooler, and every write
in it is lost at commit (`SSL SYSCALL error: EOF detected`).

Source: Codex read-only audit of `src/workers/tasks_helpers/enrich.py` (2026-11-02), each finding
re-read and confirmed in the code before fixing.

## Phase 1 (this branch, <= 5 files)
1. [x] High, `pierce_address_recovery`: legal repair keeps every earlier repair UNCOMMITTED while it
   calls GIS for later targets (15 s + 20 s per target, no loop budget), and counts repairs a
   failed commit rolled back. Fix: materialise each target's parcel and legal text, commit the
   read before the first call, commit each repair before the next target's calls, count only
   committed repairs.
2. [x] High, `_resolve_king_account_parcels`: the King extract download (300 s timeout) runs inside
   the transaction the `all_results` SELECT opened. Fix: materialise the account numbers and
   commit before `resolve_account_pins`; the caller commits whether or not anything resolved.
3. [x] High, `_enqueue_skip_trace_rows` `ClaimUnenforcedError`: the ops alert (email + 5 s
   webhook) is sent while the job's advisory lock, jobs-row FOR UPDATE and config FOR SHARE are
   held, and its `_log(db=db)` commits mid-lock. Fix: record the alert and the log line, send and
   write them after the final commit, like the held-lookup line already is.
4. [x] Tests: each network boundary records `db.in_transaction()` at call time (real DB; only the
   network functions are substituted); the counts report only committed work.
5. [x] Codex review: NO-GO x2 (ATIP stub stats; expired row in a test; page on final-commit failure;
   King unavailable path) -> all fixed -> GO. [ ] CI (local DB forwarder was stopped for low memory, so the DB tests
   run in CI).

## Phase 2 (after owner approval): the five Medium recovery-path findings (rollback expires ORM
rows, and reading them before the next lookup reopens a transaction): GIS retry loop, PACS name
pass, King condo, King chunk, King not-carried confirmation.

## Phase 3 (after owner approval): Island owner-name lookup (6 of 211 Island leads have mailing):
the failure reasons collapsed into `no_match`, the six-digit row assumption, situs required before
the detail fetch, no retry of a missing mailing after a property hit. Lettered-id parcel lookup is
deferred: no Island lead carries a parcel id.

## Review (Phase 1)
- `pierce_address_recovery`: targets materialised as (row, parcel, legal); read committed before the
  first lookup; each repair committed before the next target's lookups and counted only once committed.
- `_resolve_king_account_parcels`: (account, row) pairs (two leads can share an account); read
  committed before `resolve_account_pins`; caller commits unconditionally.
- `_enqueue_skip_trace_rows`: refused claim recorded; `_alert_claim_unenforced` + the job-log line run
  after the final commit; on a failed final commit the alert is still sent after the rollback.
- Tests: tests/test_enrich_network_transactions.py (6). Not run locally (DB forwarder stopped for low
  memory); CI runs them.
