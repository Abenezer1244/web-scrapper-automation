# Handoff: duplicate scope, dedup-claim integrity, and same-run billing

**Date:** 2026-09-08 (§7A/§7B closed 2026-09-09)
**Status:** all work MERGED and DEPLOYED. Nothing is in flight.
**Branch to continue on:** none exists. Cut a fresh branch from `origin/main`.

> **2026-09-09 — §7A and §7B are now DONE.** PR **#271** (`8084f48`) shipped all
> three deferred P2s plus a fourth defect its own design review surfaced, and the
> `rev7` gate was re-run to **GATE PASS**. See §7 for what that changed and §9 for
> what is genuinely left. CI green; all 13 ledger invariants still 0 afterwards.

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
| BE #271 | `8084f48` | post-enrichment survivor reconciliation, claim-anchor coordination, source-field merge, batch_export job-status filter |

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

## 7. The `batch_export` filter invalidated an earlier Codex P1

Worth its own section, because it is the trap most likely to catch the next person.

The per-child `record_count` in `src/api/routes/batches.py` used to count a FAILED
child's rows, under an explicit earlier Codex **P1**: *"do NOT key the count on
status"*. Its stated reason was **the very gap #271 closed** — *"finalize_batch_run
builds the combined CSV from every child_job_id with no status filter ... so a
failed/cancelled child's rows can be in the delivered CSV. Those must stay
visible."*

Adding `j.status = 'done'` makes that premise false. Those rows now reach neither
the combined CSV nor Lists nor analytics, and a non-done job has no `export_key`
to download (0 non-done jobs hold one in production, against 48 of 48 done jobs —
it is written only inside the mark-done transaction). So a failed or cancelled
child now reports **0**, and an in-flight one still reports its counted rows.

**The ruling was encoded in two places, and grepping for the comment found only
one.** The other was four tests in `tests/test_batches_read.py`, which the
targeted run did not touch and only the FULL suite caught (4 failed / 2745
passed). One of them asserted the old rule and now asserts 0 with the reasoning
recorded; **the other three only used a failed child as a vehicle** for the
per-row counting rules (actionability, duplicates, the retry that resets
`record_count`) and were re-pointed at an in-flight child so no coverage was lost.

🔑 **When you invalidate a documented ruling, hunt for every place it is asserted —
comment, test and docstring — and for each ask whether the test is ABOUT the rule
or merely using it as scaffolding.**

## 8. Next steps — what is actually left

Nothing is broken or half-finished. These are the open items, in priority order.

### A. The three deferred Codex P2s — DONE (PR #271, `8084f48`)

All three shipped, and the design review for them surfaced a **fourth** defect in
already-shipped code that shipped in the same PR.

1. **Survivor selection ran before inline enrichment.** ✅ Fixed by
   `reconcile_same_run_survivors`, which re-elects each group's survivor after
   enrichment and before the refetch — the only point where the re-export, the
   property membership and the plan cap all still see the result.

   Two things it deliberately does NOT do, both because Codex was right about them:
   - it does **not** re-run `_collapse_groups`. Enrichment rewrites
     `property_address`, so a member stops satisfying that function's
     hash-equality admission test and vanishes from its output — and "not
     returned as a loser" is not "elected winner";
   - it does **not** touch a group without exactly one standing row. With *k* the
     duplicate count moves by *k−1* and the charge with it, so a malformed group
     is logged and skipped, never "repaired".

2. **Collapse discarded source-only fields.** ✅ Fixed with a narrow ALLOWLIST:
   `heirs` (union, probate only, dropping the survivor's own `party_name`),
   `legal_description` (fill-only), `lead_subtype` (elected by the same priority
   order the combined export aggregates with, now one shared constant). Every
   other `enrichment_data` key is deliberately left alone — copying keys
   individually across two filings manufactures an object no source produced, and
   the blob carries per-row state such as the plan-cap exclusion key.

3. **`batch_export` had no job-status filter.** ✅ Fixed. See §7 — this one had a
   consequence nobody had written down.

### A2. The fourth defect: the dedup claim did not follow the survivor

`delivered_records.first_result_id` is set from whichever row PostgreSQL reached
first inside the batched `INSERT ... ON CONFLICT DO NOTHING`; the collapse elects
its survivor by actionability. **Nothing coordinated the two**, so the claim could
name a row the collapse had just flagged `is_duplicate`.

That is invariant #5 — and it matters beyond the invariant, because
`_reuse_enrichment_for_duplicates` joins `results ro ON ro.id = dr.first_result_id`
and copies address plus settled skip-trace PII **from** the anchored row. An
anchor left on a collapsed loser makes the reuse source a row the run suppressed.

**It was LATENT, never active:** production held 0 `same_run` rows, so the collapse
shipped in #265 had never fired on real data. Proven with
`scripts/diag_same_run_anchor_drift.py` (added by #271). The `trustee_sale` path
had the identical gap and was fixed too.

### B. The unfinished gate — DONE

`rev7` was re-run on 2026-09-09 and returned **GATE PASS**. All four questions were
answered independently this time: the hash-equality guard fully closes the
weak-hash hole, it admits nothing new, under-collapsing is safe, and the check
itself breaks nothing (normalization matches the frozen insert-time formula; the
SQL excludes NULL hashes).

### B2. What the diff review caught — worth reading before touching this code

Codex's review of #271 returned GATE PASS with two [P2]s. **Both were real bugs**,
and both are the same class of mistake:

- **The reconciliation re-ranked `trustee_sale` by actionability.** Auction Leads
  elects the *soonest-auction* row — a product decision from 2026-07-03. The
  reconciliation runs for every record type, so it was silently overriding that
  rule after the fact. Fixed with `sort_key_for(record_type)`; the auction key now
  lives in `dedup.py` beside `survivor_sort_key` so both paths read one definition.
- **An empty heir union fell through to the fill-only path.** `_merge_heirs`
  returned `None` both for "does not apply" and for "applies but came out empty",
  so when the exclusion removed the survivor's own party as the only name, the
  fallback copied that exact name straight back — making them their own heir, the
  precise corruption the exclusion existed to prevent.

### C. Historical charges not refunded

The 50 extra charges across 8 jobs stand — the owner chose forward-only. If that
changes, the work is: flag the extra rows duplicate and credit `records_used`,
with its own dry-run and verification pass.

### D. Latent, never exercised in production

Post-crash claim release, the plan-cap sibling guard, and now the whole same-run
collapse family — the collapse itself, the post-enrichment reconciliation and the
claim-anchor repoint — have never fired against real data (0 stranded claims, and
still **0 `same_run` rows** in production as of 2026-09-09). They are covered by
tests only.

Watch for them after the next failed run, the next capped run, and the next run
that scrapes one property twice. `scripts/diag_same_run_anchor_drift.py` answers
"has any of this fired yet, and did it leave a claim naming a duplicate row".

### E. Unrelated but flagged

- **Do not merge dependabot #251** (stripe 11.4.0 → 15.6.1). `StripeObject` is
  not a dict in v15; ~17 `.get()` call sites raise. A method-existence probe
  misses this.
- 1 of 10 all-duplicate pages still cannot name a source run (its source job was
  purged). This is the honest fallback working; nothing to do.

---

## 9. How to verify anything in here

```bash
# ledger integrity, schema-wide (13 checks, all must be 0)
railway run --service worker python scripts/diag_verify_repair_invariants.py

# the reported account's page, end to end
railway run --service worker python scripts/diag_dup_scope_audit.py <email>

# is the backfill still converged? (expect 0 recoverable)
railway run --service worker python scripts/backfill_duplicate_provenance.py --dry-run
```

```bash
# has the collapse fired yet, and does any claim name a duplicate row?
railway run --service worker python scripts/diag_same_run_anchor_drift.py
```

Local full suite (never bare pytest):

```bash
bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <path-to-worktree>
```

Two rig notes from the 2026-09-09 session:

- **A targeted run is not enough.** The four `test_batches_read` regressions lived
  in a file none of the new tests touched. Run the full suite before believing a
  behavior change is contained.
- **If another session is using the shared rig, build your own database** rather
  than resetting `bridgeleads_test` — see the isolated-DB recipe in memory. And a
  background test task reported "killed for low memory" kills only the WRAPPER:
  the `pytest` process keeps running and will compete with your relaunch. Check
  for the orphan first.
