# Handoff: duplicate scope, dedup-claim integrity, and same-run billing

**Date:** 2026-09-08
**Status:** all work MERGED and DEPLOYED. Nothing is in flight.
**Branch to continue on:** none exists. Cut a fresh branch from `origin/main`
(currently at `b511797`). The old worktree branches are merged and dead.

---

## 1. The goal, and what the goal turned out to be

The owner reported a suspected **cross-account data leak**. A Starter account's
results page said `0 new / 49 already delivered` and *"All 49 records from Mar 26
to Jun 24 were duplicates of leads you already received"*, on an account they
believed was new. The ask was to prove or disprove tenant contamination and fix
the root cause, not the wording.

**There is no cross-account leak.** Disproved three ways:

- The account was created **2026-06-23**, and a Pierce/probate run *that same
  day* delivered it **122 leads**. All 50 hashes on the 2026-07-02 re-run trace
  to that account's own job `fa573bfd`.
- Schema-wide: **0** `delivered_records` rows and **0** `results` rows owned by a
  different user than their job.
- A direct URL to another account's run returns `404 {"detail":"Job not found"}`
  from the API, verified live in production. Not a frontend hide.

**What was actually broken:** `previous_job_id` in `src/api/routes/jobs.py` chose
"newest DONE sibling job with visible leads" with **no bound requiring it to
precede the run being viewed**. On the reported page it linked to a run from
**two months later** that had delivered none of those leads. The banner made a
true claim and then handed the reader a link that appeared to refute it. That is
the whole incident.

Chasing it turned up four more real defects, described below.

---

## 2. Current state — everything shipped

| PR | Merge SHA | What |
|---|---|---|
| BE #258 | `97f41eb` | previous-run bound, migration 089 provenance, worker stamping, post-crash claim release, plan-cap sibling guard |
| FE #122 | `6bdc190` | banner copy, empty state, header split |
| BE #259 | `2dc0958` | todo closeout |
| BE #261 | `664c895` | orphaned-claim repair + release-script warning |
| BE #262 | `34fa2bd` | 13 ledger invariants as a runnable check |
| BE #263 | `890bbfe` | analytics job-status filter, delivery headline, 2 latent repair bugs |
| BE #265 | `cb387ed` | same-run sibling collapse (billing), segments job-status filter |

FE repo is `Abenezer1244/bridgeleads-web`, base branch `master`.
BE repo is `Abenezer1244/web-scrapper-automation`, base branch `main`.

### Production actions already applied (do not repeat)

- `alembic` migration **089** live; three `duplicate_*` columns on `results`.
- `ix_results_duplicate_source` built **CONCURRENTLY** over 108,745 rows,
  `indisvalid = true`, no lock. Used `DATABASE_URL_MIGRATE` (the `postgres`
  owner, session port 5432) with psycopg2 `autocommit=True`. **Not**
  `DATABASE_URL_SYNC` — that is the worker's non-owner role and a SQLAlchemy URL
  psql cannot parse.
- `scripts/backfill_duplicate_provenance.py --apply` → **2,021 rows** stamped
  across 25 jobs / 3 users. Converged exactly; re-run reports 0.
- `scripts/repair_orphaned_duplicate_flags.py --apply` → **16,761** claims
  written, 16,761 rows restored as delivered, 16,761 duplicates given a source.
  Orphans remaining: 0.
- `scripts/fix_repaired_job_headline.py --apply` → `record_count` 0 → 10,344 on
  job `68d83263`.

### Verified in production after deploy

Logged into the reported account: `previous_job_id` is now `fa573bfd` (June),
where it returned `437ecba1` (September) before. Banner reads *"were already
picked up by your run on Jun 22, 2026"*. Table reads *"No new leads in this
run."* Cross-account job still 404s.

All 13 invariants in `scripts/diag_verify_repair_invariants.py` hold
schema-wide. `records_used` for the repaired account unchanged at 1001/1000.

---

## 3. Active files

**Backend** (`Abenezer1244/web-scrapper-automation`)

| File | What changed |
|---|---|
| `src/api/routes/jobs.py` | `Job.created_at < job.created_at` bound; `previous_job_run_at`; `duplicate_sources` aggregation; all counts from ONE snapshot |
| `src/api/schemas.py` | `DuplicateSource` model; `previous_job_run_at`, `duplicate_sources`, `unattributed_duplicate_count`, `same_run_duplicate_count` |
| `src/db/models.py` | `duplicate_source_job_id` / `_at` / `duplicate_reason` on `Result`; `ix_results_duplicate_source`; corrected stale `SkipTraceCache` docstring |
| `src/workers/tasks.py` | provenance stamping at classification; post-crash claim release; plan-cap sibling guard; cap exclusion propagated to same-run siblings; `else` branch calling the general collapse |
| `src/workers/tasks_helpers/dedup.py` | `release_capped_dedup_claims`, `_collapse_loser_ids`, `collapse_same_run_siblings` |
| `src/workers/trustee_sale_finalize.py` | same-run collapse now stamps `duplicate_reason='same_run'` |
| `src/api/routes/analytics.py` | job-status filter (done only) |
| `src/api/routes/segments.py` | job-status filter on all 4 queries |
| `alembic/versions/089_result_duplicate_provenance.py` | 3 nullable columns, **no index** (built out of band) |
| `alembic/env.py` | `include_object` excludes the concurrently-built index from autogenerate |
| `tests/test_duplicate_provenance.py` | 17 tests |
| `tests/test_same_run_sibling_collapse.py` | 8 tests |
| `scripts/` | `backfill_duplicate_provenance.py`, `repair_orphaned_duplicate_flags.py`, `create_result_duplicate_source_index.sql`, `diag_verify_repair_invariants.py`, `fix_repaired_job_headline.py` |

**Frontend** (`Abenezer1244/bridgeleads-web`)

- `app/(dashboard)/results/[id]/page.tsx` — banner branches, header split
  ("N already delivered" vs "N combined"), `dupNamedRunAt` guards
- `app/(dashboard)/results/[id]/_components/ResultsTable.tsx` — empty state
- `lib/api-types.generated.ts` — regenerated

---

## 4. The five defects found and fixed

1. **`previous_job_id` pointed forward in time.** The reported bug.
2. **Post-crash cleanup released quota but kept dedup claims.** A run that died
   before delivering suppressed those leads from every future run, permanently.
3. **Plan-cap release dropped a claim a shipped, billed sibling needed.** Next
   run delivered and billed the same property again.
4. **A released claim left other jobs still asserting delivery.** 33,522 rows.
   See §5.
5. **Same-run siblings both billed.** `dedup_hash` is the billing key but
   billing counts ROWS; only `trustee_sale` collapsed. **8 jobs, 50 extra
   charges** across probate and pre_foreclosure.

---

## 5. The 33,522-row incident (context for anyone reading the repair scripts)

On 2026-09-04 job `60a0e80c` (King tax_delinquent) claimed 16,761 hashes and
failed on the plan cap. It tried to release, but **the worker role was missing
`DELETE` on `delivered_records`**, so every release path raised
`InsufficientPrivilege` and the claims stranded. `_alert_dedup_release_failed`
in `tasks.py` documents this incident and names the number. Two later runs
(`68d83263`, `035501e3`) saw the stranded claims and each reported *"0 new,
17,157 duplicates"*. The grant was later fixed and the claims released — but
nothing repaired the flags, so 33,522 rows kept asserting a delivery that never
happened.

Repaired: `68d83263` now owns the leads (16,761 claims), `035501e3`'s rows point
at it as their source, and the failed job's rows were left alone because a failed
run delivered nothing.

**Correction to an earlier claim in this session:** I said that account "received
nothing." That was overstated — `segments` had no job-status filter either, so
those leads were reachable via Lists all along. That path is now closed too
(#265).

---

## 6. Failed attempts and dead ends — read this before redoing any of it

- **Reading `delivered_records` from the API is impossible.** `bridgeleads_app`
  has ALL privileges REVOKED on it and `scripts/provision_rls_roles.sql`
  **hard-fails** if the role ever holds one. This killed the original design.
- **The claim ledger cannot answer "who delivered this" after the fact.** It is
  a CLAIM ledger written before a job finishes, three paths delete from it, and
  **44,865 of 71,332 rows** point at a purged job. That is why provenance is
  stamped on the `results` row at classification time instead.
- **Building the index inside the migration** would have held `ACCESS EXCLUSIVE`
  on `results` through a full scan. Caught by Codex as a [P1]. It is now built
  out of band; `alembic/env.py` excludes it from autogenerate so a future
  migration cannot propose a blocking `op.create_index`.
- **Tests that copy production SQL assert nothing.** Five cap-release tests held
  their own copy of the statement; deleting the guard in `tasks.py` left them all
  green. Extracted to `release_capped_dedup_claims` and proven by deleting the
  guard and watching the test fail.
- **Tests that invent a `dedup_hash` cannot catch identity bugs.** The collapse
  tests used `uuid4().hex`, so they could never have caught the weak-hash hole.
  They now derive real signatures via `legacy_strong_signature`.
- **Checking that a row HAS a strong identity is not the same as checking its
  hash IS one.** The hash is computed at INSERT time; enrichment mutates
  `property_address` after. A retry would have collapsed weak `NAME|DATE` rows.
  Fixed by requiring `legacy_strong_signature(parcel, address) == dedup_hash`.
- **CRLF makes `python str.replace` silently no-op.** `tasks/todo.md` was
  committed **twice** unchanged because the search strings used `\n`. Always
  `assert old in s` before writing, or use the Edit tool.
- **The local pytest rig is flaky under load.** Failing sets moved between runs.
  Proven environmental: with Redis flushed on an identical rig, this branch ran
  the suspect files at 3 failed / 57 passed while clean `origin/main` ran the
  same files at **23 failed / 37 passed**. CI is the authority. Restart Postgres
  and the 6543 proxy via PowerShell `Start-Process` when it degrades.
- **Do not run bare `pytest`.** It reads the production `.env` and the db-fixture
  teardown DELETEs rows. Use `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <worktree>`.

---

## 7. Next steps — what is actually left

Nothing is broken or half-finished. These are the open items, in priority order.

### A. Codex P2s from #265, recorded and deliberately not taken

1. **Survivor selection runs before inline enrichment.** Two addressless rows
   sharing a strong parcel hash pick a survivor before a Pierce legal-description
   recovery could make only the *collapsed* sibling actionable. That sibling is
   then excluded from per-job delivery and skip-tracing, and retries never
   reconsider it because the SELECT excludes duplicates.
   *Fix shape:* rank after address recovery, and allow reconsidering existing
   `same_run` siblings.
2. **Collapse discards source-only fields.** `heirs`, legal description,
   `lead_subtype` and other `enrichment_data` keys on a loser are neither ranked
   nor merged. A later filing carrying the only heir vanishes from the per-job
   export.
   *Fix shape:* merge those fields onto the survivor before marking the loser.
3. **`src/workers/batch_export.py:79` still has no job-status filter**, where
   `segments` and `analytics` now do. A failed child's actionable rows can appear
   in an emailed partial-batch CSV but not in Lists. This is the same question
   already answered "yes" twice; closing it is a consistency fix.

### B. The unfinished gate

The final narrow Codex round on #265 (`rev7`) confirmed the hash-equality guard
closes the demonstrated hole and that skipping is safe, then **hit its usage
limit mid-answer**. Two of its four questions were answered by me, not
independently. Re-run that check if you want the gate formally closed:
`scratchpad/rev7_prompt.txt` has the prompt.

### C. Historical charges not refunded

The 50 extra charges across 8 jobs stand — the owner chose forward-only. If that
changes, the work is: flag the extra rows duplicate and credit `records_used`,
with its own dry-run and verification pass.

### D. Latent, never exercised in production

Post-crash claim release and the plan-cap sibling guard have never fired against
real data (0 stranded claims, 0 same-run rows before #265 shipped). They are
covered by tests only. Watch for them after the next failed run and the next
capped run.

### E. Unrelated but flagged

- **Do not merge dependabot #251** (stripe 11.4.0 → 15.6.1). `StripeObject` is
  not a dict in v15; ~17 `.get()` call sites raise. A method-existence probe
  misses this.
- 1 of 10 all-duplicate pages still cannot name a source run (its source job was
  purged). This is the honest fallback working; nothing to do.

---

## 8. How to verify anything in here

```bash
# ledger integrity, schema-wide (13 checks, all must be 0)
railway run --service worker python scripts/diag_verify_repair_invariants.py

# the reported account's page, end to end
railway run --service worker python scripts/diag_dup_scope_audit.py <email>

# is the backfill still converged? (expect 0 recoverable)
railway run --service worker python scripts/backfill_duplicate_provenance.py --dry-run
```

Local full suite (never bare pytest):

```bash
bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <path-to-worktree>
```
