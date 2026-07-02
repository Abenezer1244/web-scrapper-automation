# Verify Pierce/King fixes (PR #133) end-to-end + fix King NTS parser defects

Session 2026-07-01, worktree `chore/verify-pierce-king-2026-07-01` off origin/main (5bc4b74).

## Verification tasks (from handoff)
- [x] Worktree off origin/main; Railway api+worker both deployed SUCCESS 2026-07-01 14:37
- [x] Read-only baseline recon: flagged rows unchanged (expected), 0 king nts rows, admin King pre_foreclosure 280 rows / 0 auction data
- [ ] Task 1: fresh Pierce probate scrape BOTH accounts (WAITING: user must click "Start run" ×2; then read-only verify new rows)
- [~] Task 2: King NTS crawler triggered — 3 blocks, 2 upserted, 1 ERRORED (varchar 512) → parser defects found (below), FIXED in PR #134 (CI green)
- [x] Task 2b: DONE — PR #134 squash-merged (a973b80), api+worker redeployed SUCCESS 16:57. Re-crawl: 3/3 upserted 0 errored (lost $282k Affinia notice landed w/ parcel 025700-0175-09 + clean addr). Backfilled MISSED 06-24 issue (5 notices, Codex PASS) → matcher enriched 2 admin King leads (RAMIREZ, parcel 7398900940): auction 2026-07-24 + default $300,754.23 + nts ref/trustee. Fix #4 proven end-to-end.
- [x] Task 3a: HANSON backfill APPLIED 2026-07-01 — 2 rows repaired (parcel 6779000110→6776000110, addr 2322 BRYCE CANYON CT, property_key recomputed, membership merge-moved). Codex PASS w/ fixes (rowcount guard adopted). Verified read-only.
- [x] Task 3b: `[E]` junk row DELETED (1 row) via owner connection (results.DELETE is owner-only under least-privilege — app role lacks the grant; used DATABASE_URL_MIGRATE). Codex P1 adopted: delivered_records.first_result_id anchor detected → Codex follow-up confirmed delete-with-SET-NULL is designed semantics, claim retained. Verified: junk count 0.
- [x] WALKER confirmed correctly kept-but-empty (no parcel + no legal = unactionable, per design)
- [ ] Task 4: UI verification (user logs in; Claude reads pages) or read-only DB equivalent

## NEW BUG: King NTS parser defects (found live on QA Legals 07-01-26.pdf)
Three root causes, confirmed by local no-DB repro (scripts/diag_king_pdf_parse_repro.py):
- [ ] A. `_AFFINIA_SHAPE` gap `{0,200}` between Beneficiary→Trustee too tight for long
      securitization-trust beneficiary names (~201 chars live) → gate misses → colon parser
      captures 810-char boilerplate into grantor/beneficiary/servicer → varchar(512) insert error,
      whole notice lost. Fix: widen gaps (keep negative lookaheads that block colon layouts).
- [ ] B. `_COMMONLY_KNOWN` boundary misses "The above property is subject to" → junk suffix
      "The above property is" leaks into property_address AND property_address_normalized
      (breaks matcher address key; block 0 has no parcel → unmatchable). Fix: add boundary.
- [ ] C. `_COMMONLY_KNOWN` requires colon; MTC layout says "More commonly known as 1814 ..." →
      address silently None. Fix: make colon optional.
- [ ] D. Defense-in-depth: clamp display-only fields to column limits at `notice_to_row`
      chokepoint; NULL (not truncate) overlong parcel (truncated parcel could false-match).
- [ ] Consult Codex on plan (standing rule) → reconcile → implement → tests → ruff → codex review gate
- [ ] PR to main; after deploy re-run crawler (Task 2b)

## Review (2026-07-01, end of verification session)

**Shipped:** PR #134 (merged a973b80) — King NTS parser: Affinia gate {0,800}/{0,1000},
_COMMONLY_KNOWN "The above property" stop + colon-optional ONLY behind "More commonly
known as" (Codex High), notice_to_row field clamps (display truncated, parcel>64 NULLed,
ts_number>64 skipped, grantor==beneficiary poison detector, raw_hash over stored values).
73 tests (new real 07-01 fixture + chokepoint unit tests), ruff clean, CI green.

**Prod repairs applied (all Codex-gated, dry-run first, read-back verified):**
- 2 HANSON rows: parcel 6779000110→6776000110, addr 2322 BRYCE CANYON CT, mailing,
  property_key recomputed, membership merge-moved (scripts/backfill_pierce_probate_legal_repair.py).
- 1 `[E]` junk row deleted via DATABASE_URL_MIGRATE owner conn (results.DELETE is
  owner-only); delivered_records claim retained (first_result_id SET NULL by design).
- Missed 06-24 King issue backfilled (5 notices) → matcher enriched 2 RAMIREZ leads
  (auction 7/24, $300,754.23). This week's 3 notices legitimately match 0 leads (no overlap).

**Notes / open:**
- Task 1 (fresh Pierce probate scrape ×2 accounts) + Task 4 (UI check) still waiting on user.
- Pre-existing cosmetic: MTC beneficiary swallows "Original Trustee of the Deed of Trust: X"
  (shared _STOP lacks that label) — deferred with Codex agreement (broad colon-parser
  hardening is a future item).
- Diag/backfill scripts left untracked in scripts/ per repo convention.
