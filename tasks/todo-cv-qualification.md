# Code Violation lead qualification (2026-10-25)

Branch `fix/code-violation-lead-qualification`, worktree `C:/Users/Windows/bl-wt/cv-qual`, base origin/main c939bb84.

## Forensics (prod, read-only, run 1ad28ded, account b6d2095d, config `yet`)
- Found 5,455 = 491 new (billed) + 4,915 prior_run duplicates + 38 same-run + 11 no-address. UI labels match the DB.
- In-job owner lookup: 1,182 parcels asked, 0 named. `king_erealproperty` was in breaker cooldown
  (recovered alert 10-18 01:59 UTC). Job still finished done, billed 491, CSV had 0 owner names.
- Beat sweep named 368 of the 491 on 10-18 09:20-10:18 UTC. 123 billed rows still unnamed:
  Seattle PIN tier not shown (address_mismatch 96, condo_complex 16, no_coordinates 10, no_parcel 6, multiple 2),
  Bellevue 1 not_on_record.
- 4,915 duplicate copies: party_name NULL on 100% (hidden duplicates skip enrichment, #521). The Already Delivered
  tab lists those copies, so it shows N/A. 1,593 point at a purged source job.
- Unincorporated KC (Accela) returned 0 rows (source failure, ops alert sent, fix #332 never merged). Run still `done`.
- Population (every done code_violation run, one account): Seattle 3,860 delivered / 2,966 owner / 2,938 strict;
  Bellevue 280/273/273; Burien 21/21/21; Tacoma 51/51/51. 4,212 billed, 929 fail strict, mailing alone costs 28.
- No complaint text or label in any party_name; owner_pin == shown parcel on every named row.

## Root causes
1. No qualification gate: billing/export/display = "has an address" (src/api/lead_actionability.py).
2. Owner naming is best-effort inside a shared, breaker-protected King source; a blocked source finishes the run silently.
3. Already Delivered lists unenriched duplicate copies instead of the delivered original's data.
4. Accela date-box bug (#332) still ships 0 unincorporated rows.

## Design (Codex consult r1 GATE FAIL folded in)
- Subject rows: `enrichment_data.qualification_policy = 'code_violation_v1'`, stamped at the single insert site
  (tasks.py pg_insert). Legacy rows keep today's rule (no history disappears, re-bills or loses its claim).
- Qualified = verified owner (party_name + owner_source allowlist + owner_pin == shown parcel) + parcel + property
  address + mailing address + case reference + record date. Phone/email never required.
- Frozen before billing: after enrichment, non-duplicate subject rows that fail are marked
  `delivery_excluded_reason = 'not_qualified'` (same mechanism as over_quota), cleared/re-marked on a re-run until
  billing applies. The plan cap ranks only qualified rows. Actionable rule also checks qualification directly (belt).
- Run breakdown bucket `not_qualified` (migration: jobs.breakdown_not_qualified).
- Owner source blocked during a run: explicit warning + degraded marker, rows held, not billed.

## Owner decisions (2026-10-25)
- Mailing address mandatory (costs 28 rows across all delivered history).
- A held record stays held; a later run takes the claim, delivers and bills it.
- Phase 1 approved.

## Phases (each <= 5 files, approval between phases)
- [x] 1a src/api/lead_qualification.py (Python + SQL + ORM twins, state/reasons) wired into
      lead_actionability (actionable rule, withheld_*, deliverable_before_cap_sql)
- [x] 1b worker: stamp at insert (tasks.py), hold step before the cap for every plan
      (plan_cap.mark_not_qualified_rows), cap ranks qualified only, skip-trace dispatcher uses
      withheld_*, blocked-owner-source run warning (enrich.py), breakdown bucket not_qualified
      + migration 121 (legacy snapshots read NULL as 0)
- Codex: consult r1 FAIL (folded in); review r1 FAIL (3 adopted, 1 false positive rejected,
  1 kept by owner decision); r2-r4 FAIL on Python/SQL twin divergence (trim set, JSON types,
  NBSP, scalar case data), all adopted; r5 GATE PASS.
- 🛑 DEPLOY ORDER: FE must handle `not_qualified` BEFORE or WITH the BE deploy. Live FE
  lib/run-summary.ts computes found = dropped + (buckets it knows), so a held run would
  show a "found" figure short by the held count.
- [ ] 2 API: `not_qualified` results category with reasons, Already Delivered shows the original's fields; FE labels
- [ ] 3 Monitoring: invariant + pass-rate collapse alert; historical dry-run report script (no writes)
- [ ] Accela #332 follow-up (separate, needs a live portal run)

## Historical records (measured read-only 2026-10-25, NOT changed)
- One account holds every code_violation lead. 5 done runs, 4,212 billed rows.
- Strict policy today would pass 3,283 of 4,212 billed rows (Seattle 2,938/3,860, Bellevue 273/280,
  Burien 21/21, Tacoma 51/51); 929 billed rows fail it, 894 of them Seattle without a verified owner.
- Run 1ad28ded: 491 billed; 368 now named (by the sweep, 12 h after delivery), 123 never qualifiable
  under the current parcel rules. The 4,915 "already delivered" copies are unenriched duplicates;
  their originals: 3,171 found (2,519 named), 1,593 point at a purged run.
- No party_name in any code_violation row is a complaint label (all assessor provenance).
- Proposed remediation (owner approval required for each step, dry-run first, bounded, resumable):
  1. Report only: per run, billed rows failing the strict rule, by reason. Writes nothing.
  2. Already Delivered display: show the delivered original's owner/parcel/mailing (Phase 2).
  3. Optional: re-run owner recovery for the 123 unnamed billed rows of 1ad28ded (King pace limits).
  4. Quota: the owner decides whether the 929 rows billed before the gate are credited. Nothing
     automatic; billing rows and delivery records are never rewritten.

## Review (Phase 1 + FE compat, 2026-10-26)
- BE full suite on the final code, isolated DB, 18 reset-per-chunk slices: 6,837 passed, 1 failed
  (test_account_export_worker DST, fails identically on origin/main). Ruff clean repo-wide.
  New tests: 68 pure + 19 DB. Negative control: disabling the gate fails 26 tests.
- openapi.json regenerated (only RunBreakdown.not_qualified + docstring), `--check` OK.
- FE: 47/47 unit tests, tsc clean, eslint clean, `next build` OK, bundle carries the new copy;
  Playwright Chromium at 1440 and 390 on a stub API: "Held back 124", "5,455 found", no overflow, no em dash.
- Codex: BE r7 GATE PASS, FE GATE PASS.
- Not done: commit/push/PR (awaiting owner), Phase 2, Phase 3.
