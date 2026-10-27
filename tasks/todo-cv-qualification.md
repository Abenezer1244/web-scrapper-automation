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

## Phase 2 plan (2026-10-27, Codex consult pending)
Goal: the user can SEE the held-back rows and why, and Already Delivered shows the delivered original's data
instead of the unenriched duplicate copy. Read-only everywhere: nothing bills, claims, quotas or skip-traces.

### 2a BE: `?category=not_qualified` list + reasons (<= 5 files)
- results_category.py: `not_qualified_list_condition()` = the breakdown CASE's not_qualified branch verbatim
  (address-actionable AND NOT duplicate AND (marked not_qualified OR NOT qualified)), ORM form built from the
  same helpers, so tab count == breakdown.not_qualified. ResultsListCategory gains "not_qualified"; the CSV /
  export URL / contact lookups keep ResultsCategory and still 422 it (held rows are not leads).
- jobs.py GET results: category=not_qualified skips actionable_condition (as no_address does), keeps job/user
  scope, delivered-run gate, tax cap, search, filters, sort, paging. Per page item: qualification_state +
  qualification_reasons from lead_qualification (Python, already twin-tested).
- schemas.py: ResultRow.qualification_state: str|None, qualification_reasons: list[str]|None (None off-tab).
- openapi.json regen. tests/test_results_not_qualified.py: count == breakdown, rows == held set, reasons,
  other-tenant isolation, CSV/export/lookup still 422, pre-delivery run lists nothing.

### 2b BE: Already Delivered shows the original (<= 4 files)
- _attach_delivery_provenance: one extra batched query: delivered_records(user_id, dedup_hash) ->
  first_result_id -> results row (same user_id, RLS + explicit filter). Attach `delivered_original`
  {party_name, parcel_id, property_address, mailing_address, mailing_city/state/zip, job_id} when the row still
  exists; null when purged (SET NULL) -> FE says "original no longer available".
- Never overwrite the duplicate's own columns (the CSV/export of already_delivered unchanged in this phase).
- schemas.py + openapi + tests (original attached, purged -> null, other tenant's hash never matched).

### 2c FE (bridgeleads-web): "Held back" tab with reason labels; Already Delivered rows render
`delivered_original` fields with an "as delivered" hint. Types regenerated from openapi. Deploy BE first
(additive fields), FE after.

Open questions for Codex: predicate parity with the CASE; whether reasons should be computed live (rows can
qualify later via the sweep -> state may say qualified while still marked not_qualified); first_result_id vs
hash-match on the original.
## Phase 2 plan r2: reconciliation of Codex consult r1 (GATE FAIL, 3 P1)
P1-1 live vs frozen reasons. ADOPTED (frozen): mark_not_qualified_rows already freezes the HOLD with the run
(a sweep never unholds; the next run takes the claim and bills). So the hold step also freezes WHY: after
_HOLD_SQL returns the held ids, the worker computes qualification_reasons() (the Python twin, already parity-tested
against qualified_sql) for those rows and writes enrichment_data.qualification_hold = {"reasons": [...],
"policy": "code_violation_v1"} in ONE batched UPDATE (jsonb_set, same statement scope, job+user filter).
_CLEAR_HOLDS_SQL removes the key on a watchdog re-run. API returns hold_reasons (frozen) AND
current_state = qualification_state(row) (live); FE copy: "Held: <frozen reasons>. Since then: complete, your
next run delivers it" when live state is qualified. Rows held before this ships (deployed 10-27 01:09Z, a few
runs at most) have no frozen key -> hold_reasons = null, FE shows live state only, labelled "reason not recorded".
P1-2 parity. ADOPTED: ORM = has_address_condition() AND is_duplicate IS FALSE AND (marked OR NOT
qualified_condition()); never actionable_condition(). Tax cap NOT applied on this tab: the breakdown applies none,
and a held row is always a code_violation subject row (tax rows are never subject and qualified_sql is TRUE for
non-subject rows), so the cap could only ever diverge, never help. Test: list total == computed breakdown
not_qualified over a fixture with every bucket (no-address precedence, same_run, prior_run, over_quota, marked,
currently-unqualified, qualified-but-marked).
P1-3 delivered original. ADOPTED: original = delivered_records(user_id, dedup_hash).first_result_id -> results row
with results.user_id = user AND results.job_id IN `available` (the existing done + same-user source-job check). No
hash fallback when first_result_id is NULL (purged) -> null, "original no longer available". delivered_original
is a FULL ResultRow (not a field subset) so the FE renders the original row whole, never a hybrid; the duplicate's
own provenance badge (source run, delivered date) stays. One batched query per page (query-count test).
Isolation tests: other tenant's same hash; forged first_result_id pointing at another tenant's row -> null.
Export contract: the CSV is built from download_rows_select / its own writer, not ResultRow serialization ->
assert exact CSV headers unchanged in a regression test anyway.
P2/P3: perf: list query uses existing job_id index; qualified_sql cost is per-row over one job (<= a few
thousand rows); EXPLAIN on a realistic local job before PR.
Phase split (each <= 5 files): 2a worker freeze (plan_cap.py + test). 2b API not_qualified tab (results_category,
jobs, schemas, openapi, test). 2c API delivered_original (jobs, schemas, openapi, test). 2d FE.
Codex consult r2: P1-2, P1-3 resolved. P1-1 residual (stale frozen reasons after a re-run unholds a row): already
planned (_CLEAR_HOLDS_SQL strips qualification_hold with the mark) + belt ADOPTED: the API returns hold_reasons
only while delivery_excluded_reason = 'not_qualified'. Test: re-run that qualifies a held row -> key gone, row
leaves the tab. Plan reconciled; awaiting owner approval.
