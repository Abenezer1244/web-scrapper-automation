# Retention purge — skip-traced PII (option c)

**Decision (owner, 2026-09-17):** keep the lead rows (county public-record data),
purge only the skip-traced contact PII. Policy §7 promises 365-day deletion of
"lead records"; this implements deletion of the personal data inside them.

**Status:** ALL FIVE PHASES IMPLEMENTED, shipped OFF
(`RETENTION_PURGE_ENABLED=false`, `RETENTION_PURGE_DRY_RUN=true`). See §F for what
changed versus this plan and what is still open. D1 (the clock) is still with
counsel; the code states its assumption explicitly and isolates it in one predicate.

Reviewed by Codex (read-only consult, gpt-5.6-luna, high effort) before any code
was written. Five BLOCKING findings, folded in below.

---

## A. Verified surface (read from the code, not assumed)

| Thing | Where |
|---|---|
| `results` PII: `phone`, `phone_type`, `phone_dnc_flag`, `email`, `phones`, `emails` | `src/db/models.py:810-818` |
| `results.skip_trace_status` String(16) NOT NULL, no CHECK constraint | `src/db/models.py:819` |
| `results.skip_trace_attempted_at` nullable tz | `src/db/models.py:820` |
| `skip_trace_cache` PII + `raw_response` (full Tracerfy payload) | `src/db/models.py:1155-1173` |
| Cache 90-day TTL is READ-TIME ONLY; rows are never deleted | `src/workers/tasks_helpers/enrich.py:2087-2092` |
| Batched-purge pattern to copy | `src/workers/scheduler_helpers/registration.py:238-276` |
| Beat schedule dict | `src/workers/scheduler.py:79+` |
| Cross-tenant session (no RLS GUC) | `src/db/session.py:266-284` |
| Alembic head | `095` |

**Already true, no work needed:** `bridgeleads_system` holds
`GRANT SELECT, INSERT, UPDATE ON ALL TABLES`, so NULLing `results` needs no new
grant. `bridgeleads_app` already has zero privileges on `skip_trace_cache`.

**Migration 068's index does NOT serve this sweep** — it is partial on
`is_duplicate = false` with `user_id` leading, built for dashboard analytics.

---

## B. Codex BLOCKING findings, folded into the plan

1. **`skip_trace_status` must not stay `"hit"` after purge.** Not an NPE, but two
   real correctness bugs: `src/api/routes/analytics.py:187` counts `hit` as
   `enriched` (would report enriched rows with no contact), and
   `src/workers/tasks_helpers/enrich.py:1995` only enqueues `not_attempted`, so a
   purged row would never retrace. Introduce `"purged"`. Safe at the DB level
   (String(16), no CHECK, `SkipTraceStatus` in `constants.py:419` is unused,
   OpenAPI types it as a plain string) but the frontend union and analytics
   semantics must move with it.
2. **Cache cutoff is 90 days, not 365.** Rows past `SKIP_TRACE_CACHE_DAYS` are
   already unusable as cache hits, and the cache feeds no billing, metering,
   delivery or customer-visible audit. Retaining raw vendor PII another 275 days
   buys nothing.
3. **R2 export objects are BLOCKING for the promise.** Delivered CSVs contain the
   phone/email. `src/utils/data_exporter.py` has upload and URL generation but no
   delete method, and there is no R2 lifecycle rule. Nulling the DB alone does not
   satisfy Policy §7. Already flagged outstanding in
   `docs/security/REVIEW-2026-06-01.md`.
4. **Idempotency.** Purging without changing the status means purged rows match the
   predicate forever, re-updating already-NULL columns every run and generating
   dead tuples indefinitely. Gate on PII presence and/or
   `skip_trace_status <> 'purged'`.
5. **Weekly leaves up to ~7 days of overshoot past the 365-day boundary.** Run
   daily, or weekly against an earlier cutoff (e.g. 358d). Daily preferred.

**New scope Codex found that neither the audit nor I had:**
`skip_trace_queues.download_url` is an encrypted bearer link to a Tracerfy CSV
containing phone/email. Not a copy of the PII, but a live access path to it. Needs
its own expiry after the ingest recovery window.

**Codex corrections to my own assumptions:** `delivered_records`,
`pending_skip_trace_rows` and `skip_trace_meter_events` hold NO returned contact
PII (I had them as suspects). Raw-SQL NULLing of the encrypted columns is safe and
needs no `FIELD_ENCRYPTION_KEY` — but never select full ORM `Result` rows in the
sweep, because that decrypts and can abort on a bad ciphertext
(see the `orm_read_decrypts_and_aborts_the_whole_audit` landmine).

---

## C. Phases (each independently testable, max 5 files)

### Phase 1 — retention settings + index migration
- [x] `src/config/settings.py`: `SKIP_TRACE_PII_RETENTION_DAYS` (365),
      `SKIP_TRACE_CACHE_RETENTION_DAYS` (90), `RETENTION_PURGE_BATCH` (1000)
- [ ] `.env.example`: document all three. **deny-ruled in this environment**, so
      this one lands as an owner step, not a commit from me
- [x] `alembic/versions/096_results_skip_trace_attempted_idx.py`:
      `CREATE INDEX CONCURRENTLY` on `results (skip_trace_attempted_at, id)`,
      NOT partial, following 068's autocommit + invalid-index-preflight pattern
- [x] `down_revision = "095"`

### Phase 2 — the purge task
- [x] `src/workers/scheduler_helpers/retention.py` (new): batched, deterministic
      `ORDER BY skip_trace_attempted_at, id LIMIT :batch FOR UPDATE SKIP LOCKED`,
      raw `UPDATE` of the six columns, `system_sync_session()`, per-batch commit
- [x] Idempotency predicate so purged rows stop matching
- [x] Cache delete loop at 90 days
- [x] Metrics: rows nulled, cache rows deleted, rows skipped on locks, duration,
      oldest remaining eligible row (a silent failure here is a compliance gap)
- [x] `src/workers/scheduler.py`: register daily in `beat_schedule`.
      See the `beat_intervals_reset_on_every_deploy` landmine

### Phase 3 — grants (drift-guarded; all three files or tests fail)
- [x] `scripts/provision_rls_roles.sql`: `GRANT DELETE ON skip_trace_cache`
      + verify IN-list
- [x] `scripts/_cutover_step2_grants_policies.py`: `_GRANTS` + `_SYSTEM_DELETE_TABLES`
- [x] `scripts/verify_worker_delete_grants.py`: `REQUIRED_DELETE_TABLES`
- [x] `tests/test_worker_delete_grants.py` hard-fails on drift between these three

### Phase 4 — the `purged` status contract
- [x] `src/config/constants.py`: add to `SkipTraceStatus`
- [x] `src/api/routes/analytics.py:187`: stop counting purged as `enriched`
- [x] `src/workers/tasks_helpers/enrich.py:1995`: decide retrace behaviour. Do NOT
      blindly add `purged` to the enqueue predicate: it would issue a new PAID
      Tracerfy lookup on a maintenance rerun
- [x] Regenerate `schema/openapi.json` in the pinned env; frontend TS union follows
      in a separate FE PR

### Phase 5 — R2 exports (owner decision, see D3)
- [x] Either a Cloudflare R2 lifecycle rule (owner, no code) or a deletion sweep
      (code: `data_exporter.py` has no delete method today)
- [x] Check R2 object versioning: deleting the current object may not delete
      prior versions
- [x] Export race (Codex P1): a job can read pre-purge PII and upload it after the
      purge commits, resurrecting it in R2

---

## D. Open decisions — OWNER

**D1. What does the 365-day clock run from?** `skip_trace_attempted_at` is reset by
a successful retrace AND by a cache hit, so a refreshed row can stay populated
indefinitely. That is correct if the rule is "retain each newly obtained copy for
365 days"; it is wrong if the rule is "delete 365 days after the lead was created".
This is a policy reading, not an engineering call. **Counsel question.**

**D2. Retrace after purge.** Should a purged row be retraceable, and is that a new
billable vendor lookup? (Codex: legitimately new and billable, but it must be
explicit, never an accident of a maintenance rerun.)

**D4. The error path resets the retention clock. NEW, found post-implementation.**
Raised by Codex ("the guard can leave old PII permanently protected when a
provider claim never settles"), and on investigation it is worse than raised.
`skip_trace_attempted_at` means "last ATTEMPT", not "when we obtained this data",
and THREE sites stamp it to now() while acquiring nothing --
`skip_trace_dispatcher.py:569`, `:880` and `tracerfy_ingest.py:782`, all on the
'errored' transition. A row holding 400-day-old contact data that is re-traced and
errors therefore gets its clock reset to today, and that old PII gets a fresh full
365-day window. Re-queueing alone does NOT stamp the column
(`enrich.py:2146`), which is what makes the in-flight guard safe; the error path
is the problem.

Mitigated, not fixed: the sweep now counts in-flight past-retention rows every run
and logs a warning, so a permanently-exempt row is loud instead of silent.

The real fix is a dedicated "PII obtained at" column that only the paths actually
storing contact data set, with the purge aging off it. That is a migration plus
edits to the PAID ingest path. Deliberately not done blind, with no test able to
run. It also overlaps D1, so both should be answered together.

**D3. R2 exports.** Lifecycle rule (owner, minutes in Cloudflare) or code sweep?
Until one exists, the policy promise is not met no matter what the DB does.

---

## E. Verification constraints (read before trusting any "done")

- **NEVER run pytest locally.** It has twice wiped production.
- **CI cannot verify anything right now**: GitHub Actions is billing-blocked, every
  job fails in ~1s without starting. This plan cannot be proven green until the
  owner clears billing.
- Schema regen only in the pinned env (python 3.12 + `requirements.txt`); see the
  handoff §6 Step 1 for the rebuild command.

---

## F. Review

All five phases implemented. Commits on `fix/openapi-drift-from-docstring`:
`32e8407` (ph1-2), `84d4422` (ph3), `d318f36` (ph4), `5bdc18b` (ph5).

### What the implementation changed versus the plan

**A real bug the Phase 4 audit found, which the design review had not.** Auditing
every reader of `skip_trace_status` (not just the two Codex named) turned up an
in-flight race: a row traced long ago and since RE-QUEUED carries old,
past-retention PII while sitting in `queued`/`submitted`. Purging it would flip
its status, and `tracerfy_ingest.py:780` only accepts a provider result for a row
still `IN ('queued','submitted')` -- so the callback would match nothing and a
lookup we PAID FOR would be silently discarded. `_ELIGIBLE` now excludes
in-flight rows.

**Phase 4 was smaller than planned, and the plan was wrong about why.**
`skip_trace_status` is typed `str`, not an enum, and the permitted-value list
lives in a Python comment that never reaches OpenAPI. Regenerating
`schema/openapi.json` produces a byte-identical file, so there is NO API contract
change and NO frontend type PR. The FE may want to render 'purged' distinctly;
nothing breaks if it does not.

**Both analytics and the retrace path needed no code change.** `analytics.py:187`
counts `== 'hit'`, so purged rows drop out of `enriched` by themselves -- which is
the behaviour we want, since `enriched` then agrees with the phone/email
percentages beside it. `enrich.py:1995` enqueues only `'not_attempted'`, so
decision D2's safe default (no accidental paid retrace) came for free.

**A latent bug fixed incidentally.** `scripts/purge_skip_trace_cache.py` issues
`DELETE FROM skip_trace_cache` as `bridgeleads_system`, a role that held no DELETE
on that table until Phase 3. It would have failed with `InsufficientPrivilege` any
time after the RLS cutover.

### Verification, and its limits

Done: `ruff check src/ tests/ scripts/` clean; scheduler imports and the beat
entry registers; alembic graph resolves to a single head `096`;
`export_openapi.py --check` OK in the pinned env; the disabled path returns
without opening a DB session; the R2 traversal guard rejects `../etc/passwd`; all
five grant-drift assertions from `tests/test_worker_delete_grants.py` replicated
standalone (pure regex, no DB) and passing, with all four table lists agreeing.

NOT done, and it matters: **no test has run against this.** pytest is banned
locally (it has twice wiped production) and GitHub Actions is billing-blocked, so
CI cannot run either. Nothing here has executed against a real database. The SQL
is reviewed, not proven.

### Added after the review passes

**Phase 6 - provider download links (`d051acc`).** Closes the scope Codex found
that neither the audit nor I had. `skip_trace_queues.download_url` is not a copy
of the PII, it is a live ACCESS PATH: the Tracerfy CDN needs no auth, so the URL
alone fetches a CSV of traced phone numbers. Encrypted at rest, retained forever
(migration 054 cleaned it once in 2026; nothing since). Two windows, because the
link is kept for a reason - a paid-but-unapplied batch is recovered by hand from
it: `completed` queues drop theirs at 30 days (already ingested, nothing to
recover), `pending`/`errored` keep theirs until the PII window, past which there
is nothing left to recover into. Deliberately generous to errored, because those
raise an ops alert and `OPS_ALERT_EMAIL` has been empty in production before,
making that alert a silent no-op. No new grant needed.

**Tests (`3de0547`).** `tests/test_retention.py`, real DB, no mocks. The negative
cases carry the weight: in-flight rows (parametrized queued/submitted) keep status
AND PII; a 'miss' is never relabelled 'purged'; never-traced and in-window rows
untouched; the lead itself survives; a second pass is a no-op; cache inside the
reuse window survives. Plus two that need no network - the export-key conditional
clear asserted at SQL level (the Codex High), and a `has_table_privilege` check on
the cache DELETE grant. `EXPORT_RETENTION_DAYS` is pinned high in enforce-mode
tests so the R2 leg cannot send a shared test DB out to the internet.

**They have not been run.** pytest is banned locally; CI is billing-blocked. They
are verified only as far as ruff, compilation, and every imported symbol and model
attribute resolving.

### Still open

- D1 (the clock) with counsel. One-line predicate change if the answer differs.
- D3's belt: the R2 lifecycle rule is an owner step and is NOT done. The code
  sweep alone does not catch the export race.
- §7 still promises deletion of "lead records" while we delete the data inside
  them. Wording gap, counsel item.
- `.env.example` entries (deny-ruled here) -- owner step 5f in the runbook.
