# Retention purge — skip-traced PII (option c)

**Decision (owner, 2026-09-17):** keep the lead rows (county public-record data),
purge only the skip-traced contact PII. Policy §7 promises 365-day deletion of
"lead records"; this implements deletion of the personal data inside them.

**Status:** PLAN ONLY. Nothing implemented. Awaiting owner sign-off on the three
open decisions in §D.

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
- [ ] `src/config/settings.py`: `SKIP_TRACE_PII_RETENTION_DAYS` (365),
      `SKIP_TRACE_CACHE_RETENTION_DAYS` (90), `RETENTION_PURGE_BATCH` (1000)
- [ ] `.env.example`: document all three. **deny-ruled in this environment**, so
      this one lands as an owner step, not a commit from me
- [ ] `alembic/versions/096_results_skip_trace_attempted_idx.py`:
      `CREATE INDEX CONCURRENTLY` on `results (skip_trace_attempted_at, id)`,
      NOT partial, following 068's autocommit + invalid-index-preflight pattern
- [ ] `down_revision = "095"`

### Phase 2 — the purge task
- [ ] `src/workers/scheduler_helpers/retention.py` (new): batched, deterministic
      `ORDER BY skip_trace_attempted_at, id LIMIT :batch FOR UPDATE SKIP LOCKED`,
      raw `UPDATE` of the six columns, `system_sync_session()`, per-batch commit
- [ ] Idempotency predicate so purged rows stop matching
- [ ] Cache delete loop at 90 days
- [ ] Metrics: rows nulled, cache rows deleted, rows skipped on locks, duration,
      oldest remaining eligible row (a silent failure here is a compliance gap)
- [ ] `src/workers/scheduler.py`: register daily in `beat_schedule`.
      See the `beat_intervals_reset_on_every_deploy` landmine

### Phase 3 — grants (drift-guarded; all three files or tests fail)
- [ ] `scripts/provision_rls_roles.sql`: `GRANT DELETE ON skip_trace_cache`
      + verify IN-list
- [ ] `scripts/_cutover_step2_grants_policies.py`: `_GRANTS` + `_SYSTEM_DELETE_TABLES`
- [ ] `scripts/verify_worker_delete_grants.py`: `REQUIRED_DELETE_TABLES`
- [ ] `tests/test_worker_delete_grants.py` hard-fails on drift between these three

### Phase 4 — the `purged` status contract
- [ ] `src/config/constants.py`: add to `SkipTraceStatus`
- [ ] `src/api/routes/analytics.py:187`: stop counting purged as `enriched`
- [ ] `src/workers/tasks_helpers/enrich.py:1995`: decide retrace behaviour. Do NOT
      blindly add `purged` to the enqueue predicate: it would issue a new PAID
      Tracerfy lookup on a maintenance rerun
- [ ] Regenerate `schema/openapi.json` in the pinned env; frontend TS union follows
      in a separate FE PR

### Phase 5 — R2 exports (owner decision, see D3)
- [ ] Either a Cloudflare R2 lifecycle rule (owner, no code) or a deletion sweep
      (code: `data_exporter.py` has no delete method today)
- [ ] Check R2 object versioning: deleting the current object may not delete
      prior versions
- [ ] Export race (Codex P1): a job can read pre-purge PII and upload it after the
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

_To be filled in after implementation._
