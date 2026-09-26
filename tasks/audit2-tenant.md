# Tenant Isolation / Authorization Re-Audit (audit 2)

- **Date:** 2026-09-25
- **Worktree:** `C:/Users/Windows/bl-wt-secaudit2`, HEAD `fc38e620` (origin/main). The brief named
  `25a04eaf`; origin/main has since advanced by PR #356 (migration 101, contact-lookup action
  schema). That delta is tenant-relevant, so this audit covers it.
- **Baseline:** prior audit `tasks/audit-tenant.md` @ `60f1b00` (2026-09-16).
- **Method:** static, read-only. No pytest, no alembic, no railway, no DB or network access. No
  source edited. Python was used only to AST-parse route decorators (no project imports).

## Headline

**No P0, no P1, no P2.** No route, response, count or cache lets tenant A read, write, or learn
anything about tenant B's jobs, leads, deliveries or contact lookups.

- The route surface is **unchanged in shape**: 69 routes in `src/api/routes/` plus `/health` and
  `/ready` in `main.py`. No route was added or removed since the baseline. The changed routes are
  `GET /jobs/{id}/results`, `/export-url` and `/download` (new `category` param), the SSE loop
  (keepalive), and the legacy Tracerfy webhook (kill switch).
- **Dedup / "already delivered" is TENANT-scoped** (per `(user_id, dedup_hash)`), and the v2
  skip-trace subject key puts `user_id` inside the hash. So **there is no cross-tenant reuse of a
  paid lookup at all**, and nothing to disclose. `skip_trace_source='reused'` only ever means "this
  account's own earlier answer".
- The four worker-side residuals from the last audit (F2 to F5) are **all still open**, and one
  (F4) has **grown**: a new unpinned `results` write was added to the NTS matcher.
- Migration 101 adds 3 tenant tables with RLS, composite tenant FKs and guard triggers. It is well
  built, with one latent gap: `pending_skip_trace_rows.action_id` has **no tenant-carrying FK**,
  although the migration header says every child carries one. It is not reachable today because no
  code writes `action_id` yet. It must be closed before Phase 1b-2 ships a writer.

| ID | Sev | Title | Status |
|---|---|---|---|
| T-1 | P3 | Worker loads `ScraperConfig` by id with no owner predicate and no composite FK (prior F2) | still open |
| T-2 | P3 | Skip-trace dispatcher `Result` updates by id only on a system session (prior F3) | still open |
| T-3 | P3 | NTS matcher writes `results` by id only, and a second such write was ADDED (prior F4, widened) | open, grew |
| T-4 | P3 | `dispatch_batch_run(run_id)` resolves any run with no owner assertion (prior F5) | still open |
| T-5 | P3 | `pending_skip_trace_rows.action_id` has no `(action_id, user_id)` composite FK (mig 101) | new, latent |
| T-6 | Info | `contact_lookup_actions.quote_id` is unique GLOBALLY, not per tenant | new, design note |
| T-7 | Info | Global `submission_collision_key` serializes same-address lookups ACROSS tenants (timing only) | new, accepted |
| T-8 | Info | Correction to prior report: 30 of 69 handlers do not use `get_rls_db`; none leaks | correction |
| T-9 | Info | 101's FORCE RLS and role-targeted policies depend on manually re-run scripts | ops verify |
| INFO-1/7/10 | Info | Prior informational items still present (public `include_all`, `run.user_id` uid, unjoined JobLog counts) | carry-over |
| INFO-4 | n/a | In-process rate-limit fallback flush | **FIXED** (`rate_limit.py`, expiry-only eviction) |

---

## 1. Route matrix (current code, all 71 routes)

Legend: `CU` = `CurrentUser` (`src/api/auth.py`, 401 on no/invalid credential). `rls` =
`Depends(get_rls_db)`, which sets `app.current_user_id` (transaction-scoped) and is re-applied per
transaction by the `after_begin` listener. `db` = plain `get_db` (no GUC). "Tenant predicate" is the
application-level `user_id` filter. "Gate" is any admin/plan/entitlement dependency. No route in
the repo takes `user_id`, `account_id`, `plan`, `records_limit`, `records_used` or `is_admin` from
a request body or query: those names appear only on response models
(`schemas.py:248-251`, `:694`, `:1163`).

### jobs.py (`/jobs`)

| Method | Path | Auth | Session | Tenant predicate | Gate | Verdict |
|---|---|---|---|---|---|---|
| GET | `/jobs` | CU | rls | `Job.user_id` `jobs.py:109`; joined config also pinned `:120`, `:133` | none | OK |
| POST | `/jobs` | CU | rls | body `scraper_config_id` checked `ScraperConfig.user_id` `:316` then 404 | plan queue via `scrape_queue_for_plan` | OK |
| GET | `/jobs/{job_id}` | CU | rls | `Job.id` + `Job.user_id` `:335`, config `:343` | none | OK |
| DELETE | `/jobs/{job_id}` | CU | rls | single-statement CAS with `user_id` in predicate `:364`, probe `:373` | none | OK |
| GET | `/jobs/{job_id}/results` | CU | rls | parent `:419`; list `:463` + `category_condition` `:464`; every count/aggregate re-carries `Result.user_id` (`:530`, `:545`, `:640`, `:680`, `:712`, `:760`, `:773`, `:789`, `:824`, `:854`); provenance helper pins `user_id` on all 3 queries (`:880+`) | none | OK |
| GET | `/jobs/{job_id}/logs` (SSE) | CU | rls, then per-poll RLS sessions | open check `:966`; in-loop status and replay pinned (`_stream_job_status`, `_job_logs_select` `:1102-1111`) | per-user SSE lease | OK |
| GET | `/jobs/{job_id}/export-url` | CU | rls | `Job.user_id` `:1177`; token binds `(sub=user.id, job_id)` | none | OK |
| GET | `/jobs/{job_id}/download` | inline JWT (download or session token) | db + manual GUC `:1390` | token `job_id` must equal path `:1347` (403); `Job.user_id` `:1395`; rows `:1410` | none | OK |

### scrapers.py (`/scrapers`)

| Method | Path | Auth | Session | Tenant predicate | Gate | Verdict |
|---|---|---|---|---|---|---|
| GET | `/scrapers/sample` | public | db | none; reads pre-redacted `public_sample_cache` only | none | OK by design |
| GET | `/scrapers` | CU | rls | `:121` | none | OK |
| POST | `/scrapers` | CU | rls | insert `user_id=current_user.id` `:321` | `enforce_entitlements` | OK |
| GET | `/scrapers/connectors` | public | db | n/a, no tenant column | none | OK (INFO-1 carry-over) |
| GET | `/scrapers/{scraper_id}` | CU | rls | `:443` | none | OK |
| DELETE | `/scrapers/{scraper_id}` | CU | rls | `:462` | none | OK |
| PATCH | `/scrapers/{scraper_id}` | CU | rls | `:589` FOR UPDATE; in-flight job guard `:646` | entitlements | OK |
| PUT | `/scrapers/{scraper_id}/csv-layout` | CU | rls | `:855`; job guard `:895` | none | OK |
| POST | `/scrapers/connectors` | CU + `require_admin_mfa` | db | global admin table | admin, MFA step-up, server-side | OK |
| GET | `/scrapers/{config_id}/records` | CU | rls | config gate `:1158`; view-state SQL binds `:user_id` | none | OK (shared county catalog by design) |
| POST | `/scrapers/{config_id}/jobs/{job_id}/dialer-replay` | CU | rls | `:1325` (id + user + config), UPDATE re-asserts `:1338` | none | OK |

### batches.py (`/batches`), segments.py (`/segments`), analytics.py, notifications.py

| Method | Path | Auth | Session | Tenant predicate | Gate | Verdict |
|---|---|---|---|---|---|---|
| POST | `/batches` | CU | rls | writes `user_id=current_user.id` `:333`, `:360`, `:394` | entitlements | OK |
| GET | `/batches` | CU | rls | `:553`, children `:567`, `:581` | none | OK |
| GET | `/batches/{batch_id}` | CU | rls | `_owned_batch` `:509`; children `:625`, `:647`, `:663` | none | OK |
| GET | `/batches/{batch_id}/download` | CU | rls | `:728-729` | none | OK |
| GET | `/batches/{batch_id}/runs` | CU | rls | `:826`, `:830` | none | OK |
| GET | `/batches/{batch_id}/runs/{run_id}/download` | CU | rls | `:849-855` run + batch + tenant | none | OK |
| GET | `/batches/{batch_id}/leads` | CU | rls | `:1028-1029` | none | OK |
| GET | `/batches/{batch_id}/runs/{run_id}/leads` | CU | rls | `:1068-1074` | none | OK |
| POST | `/segments/intersection` | CU | rls | SQL binds `uid` (`:547`), pins `j`, `sc`, `r` | router `Depends(_require_overlap_plan)` `:98` | OK |
| POST | `/segments/intersection/export` | CU | rls | same SQL (`:621`) | overlap plan | OK |
| POST | `/segments/union` | CU | rls | `_UNION_SQL` pins all three (`:748`) | overlap plan | OK |
| POST | `/segments/union/export` | CU | rls | same | overlap plan | OK |
| GET | `/analytics/summary` | CU | rls | shared `Result.user_id` base; LEFT JOIN legs tenant-pinned in ON (`analytics.py:62-65`) | none | OK |
| GET | `/notifications` | CU | rls | `:33`, `:43` | none | OK |
| PATCH | `/notifications/{id}/read` | CU | rls | `:69`, 404 on miss | none | OK |
| POST | `/notifications/read-all` | CU | rls | `:92` | none | OK |

### auth.py (`/auth`), billing.py (`/billing`), webhooks.py, main.py

| Method | Path | Auth | Session | Tenant predicate | Gate | Verdict |
|---|---|---|---|---|---|---|
| GET | `/auth/config` | public | none | n/a | none | OK |
| POST | `/auth/register` | public | db | n/a (creates own row) | rate limit | OK |
| POST | `/auth/verify-email` | token | db | token-bound | none | OK |
| POST | `/auth/login`, `/login/mfa`, `/login/break-glass`, `/refresh` | credential/token | db | credential-bound | rate limit | OK |
| GET | `/auth/me` | CU | none | `current_user` | none | OK |
| PUT | `/auth/notification-preferences` | CU | db | `User.id == current_user.id` `auth.py:225` | none | OK (T-8) |
| PUT | `/auth/profile` | CU | db | `User.id == current_user.id` `:256` | none | OK (T-8) |
| GET | `/auth/onboarding` | CU | rls | self | none | OK |
| POST | `/auth/logout` | CU | none | self | none | OK |
| POST | `/auth/logout-all` | CU | db | self (`:337`) | none | OK (T-8) |
| POST | `/auth/change-password` | CU | rls | self | none | OK |
| GET/POST | `/auth/mfa/status`, `/mfa/setup`, `/mfa/enable`, `/mfa/disable` | CU | none / rls | self | none | OK |
| POST | `/auth/forgot-password`, `/reset-password` | public / token | db | enumeration-safe | per-address `once_per` | OK |
| POST | `/auth/api-key` | CU + `require_plan('business','agency')` | db | self (`:456`) | plan, server-side | OK (T-8) |
| GET | `/billing/activation-funnel` | `require_admin` | db | cross-tenant AGGREGATE by design | admin (404 to non-admin, MFA enrolled), server-side | OK |
| GET | `/billing/referral` | CU | rls | self | none | OK |
| GET | `/billing/skip-trace-usage`, `/usage`, `/subscription` | CU | none | `current_user` | none | OK |
| GET | `/billing/plans`, `/pricing` | public | none | n/a | none | OK |
| POST | `/billing/checkout`, `/change-plan` | CU | rls | self; plan chosen by client but priced/validated server-side against the Stripe price map | none | OK |
| POST | `/billing/portal` | CU | none | self | none | OK |
| POST | `/billing/webhook` | Stripe signature | db | Stripe-resolved | signature | OK (billing audit's scope) |
| POST | `/webhooks/tracerfy` | shared secret header | none | tenant resolved worker-side from pending rows | secret | OK |
| POST | `/webhooks/tracerfy/{provided_secret}` | path secret | none | same | secret + new kill switch (410 when disabled) | OK |
| GET | `/health`, `/ready` | public | n/a | n/a | none | OK |

**Flags from the matrix: none.** No route is missing auth that needs it; no tenant-owned row is
read or written without a `user_id` predicate; no admin route is gated only by the frontend (both
admin routes use server-side dependencies); no route accepts identity, plan or quota from the client.

---

## 2. What changed since 2026-09-16 (diff-focused)

`git diff 60f1b00..HEAD --stat -- src/ alembic/ main.py` is 53 files, about 6.1k lines, plus 101.
Tenant-relevant changes and what I verified:

| Change | Files | Verified |
|---|---|---|
| Already-delivered view + provenance (#341) | `src/api/results_category.py`, `jobs.py:464`, `:524`, `:880+`, `:1430` | Category is an allowlisted `Literal`; list, counts, CSV all still AND `Result.user_id`. Provenance helper pins `Job.user_id`, `Result.user_id` on all three queries and **nulls `duplicate_source_job_id`** when that run is not this account's (defense against a cross-account id). |
| Skip trace for already-delivered leads, one answer bought once (#342) | `enrich.py` reuse passes, `results_category.skip_trace_eligible_*` | Both reuse UPDATEs pin `rn`, `ro`, `dr` to `:uid` (`enrich.py:373-395`, `:419-462`). |
| Record answer source, tenant-gate tests (#344) | mig 097, `skip_trace_source` | `reused` is written only by per-tenant reuse paths (enrich.py:386, :2642; dispatcher :767). |
| v2 lookup subject key cutover (#349, mig 098) | `skip_trace.py:208-265`, `enrich.py:2624`, `tracerfy_ingest.py:708,757`, dispatcher | `user_id` is the 2nd element of the hashed tuple (`skip_trace.py:243`). |
| One active claim per lead (#354, mig 100) | `skip_trace_claim.py`, dispatcher | Claim pins result, job and owner to one `uid` (see non-findings). Unique index is on `result_id`, which is tenant-owned. |
| Live Run progress (#348, mig 099) | 8 nullable columns on `jobs`; `JobResponse` stage fields; SSE keepalive | Columns live on an already RLS + FORCE table; exposed only via owned-job routes. Keepalive is a bare comment line (`jobs.py` stream loop), no data. |
| Contact-lookup action ledger (#356, mig 101) | 3 new tables, triggers, grants | See section 6 and T-5, T-6, T-9. |
| Retention purge | `scheduler_helpers/retention.py` | Global, age-based NULLing/DELETE; moves no data between tenants. |
| NTS matcher missing-reason stamp | `nts_matcher_task.py:336-348` | **New id-only write**, see T-3. |

---

## 3. Duplicate / "already delivered" scope, and cross-tenant disclosure

**Verdict: dedup is TENANT-scoped (per account, per property key). Nothing discloses another
tenant's delivery, enrichment or lookup.**

- The claim table is unique on `(user_id, dedup_hash)` (`src/db/models.py:1066`) and the claim is
  `ON CONFLICT (user_id, dedup_hash) DO NOTHING` (`src/workers/tasks.py:1205`). A tenant can only
  ever lose a claim to its own earlier run.
- The duplicate stamp joins `delivered_records dr ON dr.user_id = :uid` (`tasks.py:1258-1273`), so
  `duplicate_source_job_id` / `duplicate_source_at` can only name the caller's own run.
- `get_results` re-verifies source runs with `Job.user_id == current_user.id` and blanks the id
  otherwise (`jobs.py:880+`, provenance helper).
- `already_delivered_count` and `AlreadyDeliveredContacts` (found / none_found / looking / failed /
  not_looked_up / reused) are `COUNT ... FILTER` over the caller's own rows only
  (`jobs.py` counts block, `.where(Result.job_id == job_id, Result.user_id == current_user.id)`).

**The v2 subject key (prime suspect): NOT a cross-tenant cache.**

`lookup_subject_key` hashes `[version, user_id, address, city, state, trace_type, first, last]`
(`src/scrapers/enrichment/skip_trace.py:208-265`, `user_id` at `:243`). Every reader and writer
goes through it:

- enqueue cache read: `payload_subject_key(job.user_id, payload)` (`enrich.py:2624`)
- ingest cache write + result stamp: `pending_row_subject_key(p)` from the pending row's own
  `user_id` (`tracerfy_ingest.py:708`, `:757`)
- dispatcher settle-from-known-answers: `_answer_key(p)` = the same function (`skip_trace_dispatcher.py:639-649`), and `charged_unanswered` is keyed the same way (`:740-751`)
- duplicate reuse passes compare `ro.skip_trace_subject_hash = s.subject_hash` where the target hash
  is computed with this job's `uid` (`enrich.py:314-320`, `:337-347`), and every leg is also pinned
  `user_id = :uid` (`:389-395`, `:437-460`).

So tenant A's paid answer can never be copied to tenant B: B's key for the same person at the same
address is a different SHA-256. Because no cross-tenant reuse happens, there is nothing for the
`reused` counter, lookup status or provenance to disclose. This is a legitimate per-tenant internal
cache, not a disclosure. The one deliberately GLOBAL key, `submission_collision_key`
(`skip_trace.py:268-286`), decides only batch composition, never data flow (T-7).

**Ingest attribution across tenants in one Tracerfy batch:** a batch spans tenants by design. Rows
are matched by `(address, city, state)` but only among that batch's own pending rows
(`tracerfy_ingest.py:596-611`), each Result write is pinned `(id, user_id)` (`:735-745`), and
`_attribution_is_safe` refuses any key with more than one CSV answer, mixed trace types, or
differently named owners (`:279-333`). The dispatcher also keeps one address per batch across
tenants. A cross-tenant address collision fails closed (unmatched + alert), never stamps A's
contacts on B's lead. Billing splits per `user_id` (`src/api/billing/skip_trace_usage.py:543-593`).

---

## 4. Job ownership: start, view, stream, cancel, retry, results, download, export

| Action on another tenant's job id | Result | Evidence |
|---|---|---|
| start (POST /jobs with B's config id) | 404 | `jobs.py:316` |
| view (GET /jobs/{id}) | 404 | `:335` |
| stream (SSE) | 404 before lease or subscribe | `:966-970` |
| SSE reconnect | full re-auth; no Last-Event-ID/resume token | stream handler |
| SSE lease key | `sse_leases:{user_id}`, per caller | `src/api/sse_leases.py` (unchanged) |
| cancel | 204-less 404, CAS includes `user_id` | `:364`, `:373` |
| retry | no endpoint; worker-internal only | `tasks_helpers/status.py` |
| results / already-delivered view | 404 | `:419` |
| export-url | 404; token binds `(sub, job_id)` | `:1177` |
| download | 403 on token/job mismatch, 404 on foreign job | `:1347`, `:1395` |
| dialer-replay | 404 (id + user + config) | `scrapers.py:1325` |

The new `category` query param is carried through `export-url` into the download URL but is not
inside the signed token. That is fine: the token still binds user and job, and category only picks
between two subsets of the same tenant's own rows.

---

## 5. Worker side: tasks that take ids from a payload

| Task | Id source | Owner re-derived? | Evidence |
|---|---|---|---|
| `run_scrape_job(job_id)` | API | yes for job (bootstrap then `rls_sync_session(user)`); **config not owner-pinned** | `tasks.py:432-468` (T-1) |
| `dispatch_batch_run(run_id)` | API (`batches.py`) | no owner assertion in `_resolve_run` | `batch_tasks.py:50-80` (T-4) |
| `process_dialer_outbox(job_id)` | worker | yes, owner re-read | `dialer_outbox.py` (prior audit) |
| `ingest_tracerfy_batch(queue_id, url)` | webhook | yes, per pending row | `tracerfy_ingest.py:596-611`, `:735-745` |
| `report_skip_trace_meter_event(outbox_id)` | worker | yes, reads `user_id` from the row | `tracerfy_ingest.py:99` |
| `dispatch_pending_skip_trace()` | beat | mostly tuple-pinned; 2 id-only updates | T-2 |
| `match_nts_notices()` | beat | id-only writes | T-3 |
| `claim_skip_trace_rows(payloads)` | worker | yes: one uid, one job, joined on both | `skip_trace_claim.py:383-398`, `:471-477`, `:534-541` |

---

## 6. RLS on new tables (migrations 096 to 101)

- **096 to 100:** no new tables. 096 index, 097 `results.skip_trace_source`, 098
  `results.skip_trace_subject_hash`, 099 eight nullable columns on `jobs`, 100 a partial unique
  index on `pending_skip_trace_rows(result_id)`. All on tables already under ENABLE + FORCE RLS.
- **101: three new tables**, `contact_lookup_actions`, `contact_lookup_action_results`,
  `contact_lookup_action_events`:
  - `ENABLE ROW LEVEL SECURITY` + untargeted GUC policy inline (`101:660-666`), with the Supabase-safe
    `NULLIF(current_setting(..., true), '')::uuid` predicate (`101:126-128`).
  - Role-targeted per-verb policies (app SELECT/INSERT, app UPDATE on actions only, system FOR ALL
    except events which are SELECT/INSERT) in `scripts/apply_rls_cutover_policies.sql:322-394`.
  - FORCE in `scripts/apply_rls_force.sql:49-53`.
  - Grants: REVOKE from PUBLIC/anon/authenticated; app gets SELECT/INSERT and column-level
    `UPDATE (dispatched_at)` only (`101:675-723`, `scripts/provision_rls_roles.sql:141-194`).
  - Composite tenant FKs: action to `jobs(id, user_id)`, results to `actions(id, user_id)` and
    `results(id, user_id)`, events to `actions(id, user_id)` (`101:571-641`), backed by newly built
    `uq_jobs_id_user` / `uq_results_id_user`.
  - Guard triggers stop a user-scoped session from writing worker states, fabricating events, or
    passing as the worker without the `bridgeleads_system` role (`101:184-405`); triggers are
    `search_path`-pinned and the action lookup is schema-qualified.
  - Gaps: T-5 (no FK on `pending_skip_trace_rows.action_id`), T-6, T-9.

---

## 7. Findings

### T-1 (P3) Worker loads the job's ScraperConfig with no owner predicate and no composite FK

- **Evidence:** `src/workers/tasks.py:466-468`
  `select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)`. `jobs.scraper_config_id`
  is a plain FK (`models.py:664`), not `(scraper_config_id, user_id) -> scraper_configs(id, user_id)`.
  Siblings do pin it (`dialer.py:116-122`, `batch_tasks.py:148-151`, `dialer_outbox.py:87-92`).
- **Prerequisites:** a malformed `jobs` row pointing at another tenant's config (repair script,
  restore, future code path). Every current Job creator validates the config owner.
- **Impact:** the query runs inside `rls_sync_session(job.user_id)`, so with RLS enforced a foreign
  config is invisible and `scalar_one()` raises: a crash, not a leak. Without RLS, the worker would
  run with the other tenant's delivery block (dialer URL, emails) and push this tenant's leads there.
  **Rated P3, down from the prior P2**, because the boot gate is now explicitly fail-closed when
  `RLS_ENFORCE` is on and the role bypasses RLS (`main.py` lifespan comment, `session.py:388-449`),
  and production runs `RLS_ENFORCE=true` on api and worker.
- **Fix:** add `ScraperConfig.user_id == job.user_id` at `tasks.py:467`. Migration 101 just built
  `uq_jobs_id_user`; add the matching `uq_scraper_configs_id_user` (if absent) and a composite FK on
  `jobs(scraper_config_id, user_id)`, `NOT VALID` then `VALIDATE`.
- **Regression test:** insert a job whose `scraper_config_id` belongs to another user (owner role),
  run the task body, assert it aborts without touching delivery config or writing results.

### T-2 (P3) Skip-trace dispatcher updates Result by id only on a system session

- **Evidence:** `src/workers/skip_trace_dispatcher.py:1189-1196` (queued to submitted) and
  `:1290-1297` (to errored): `Result.id.in_([c.result_id for c in claimed])`, no `user_id`, while
  the neighbours use `tuple_(Result.id, Result.user_id)` (`:978`, `:1051`, `:1101`).
- **Prerequisites:** a corrupted `_Claim` list. Ids come from the dispatcher's own claim set.
- **Impact:** status-only write, no data disclosure; a wrong id would flip another tenant's lead
  status.
- **Fix:** use `tuple_(Result.id, Result.user_id).in_([(c.result_id, c.user_id) for c in claimed])`.
- **Regression test:** extend the existing owner-isolation tests: two tenants' rows with swapped ids
  in `claimed`, assert only the paired row changes.

### T-3 (P3) NTS matcher writes results with no tenant predicate, and a second such write was added

- **Evidence:** existing `src/workers/nts_matcher_task.py:400-415` (`WHERE id = :rid`) and **new**
  `_stamp_missing_reasons` `:336-348` (`WHERE id = ANY(:ids)`), both under `system_sync_session()`
  (`:104-108`). The candidate SELECT joins `results -> jobs -> scraper_configs` with no
  `j.user_id = r.user_id` / `sc.user_id = j.user_id`. Also `trustee_sale_finalize.py:150`.
- **Prerequisites:** an upstream id error; no request input reaches these ids.
- **Impact:** payload is public notice data or a reason code, so no disclosure; it is an unguarded
  cross-tenant write surface that just grew.
- **Fix:** carry `r.user_id` through `result_dicts`, add `AND user_id = :uid` (or tuple pairing) to
  both UPDATEs, pin both joins in the candidate SELECT.
- **Regression test:** two tenants with the same parcel in one county; run the matcher and the stamp;
  assert each row's writes and reason match its own candidate set only.

### T-4 (P3) dispatch_batch_run resolves any run id with no owner assertion

- **Evidence:** `src/workers/batch_tasks.py:50-80` `_resolve_run` selects `BatchRun.id == ref` /
  `db.get(ScraperBatch, ref)` on a system session; the argument is request-influenced
  (`batches.py` create path).
- **Prerequisites:** a code path that enqueues an unvalidated id; none exists (the route creates the
  run with `user_id=current_user.id`).
- **Impact:** would run the victim's batch against the victim's quota and delivery; nothing returns
  to the caller. Unauthorized-action shape, not disclosure.
- **Fix:** pass `user_id` into the task and assert `run.user_id == user_id` in `_resolve_run`.
- **Regression test:** call `dispatch_batch_run` with another user's run id and a mismatched owner;
  assert no children are created.

### T-5 (P3, latent) pending_skip_trace_rows.action_id has no tenant-carrying FK

- **Evidence:** `alembic/versions/101_contact_lookup_action_schema.py:516-519` adds
  `action_id UUID` with only an index `(action_id, user_id)` (`:655-656`). There is no
  `FOREIGN KEY (action_id, user_id) REFERENCES contact_lookup_actions(id, user_id)`. The header at
  `101:25-28` states "Every child row carries `user_id` and references its parent by
  `(parent_id, user_id)`, so a row can never point at another account's action"; that is true for
  the three new tables and **not** for this column. The header at `101:18-24` says 1b-2 ingest will
  "rebuild attribution from the pending rows" via this column, and Tracerfy batches span tenants.
- **Prerequisites:** a 1b-2 writer that sets `action_id` from a task argument or a join that is not
  owner-pinned. No writer exists at HEAD (schema only).
- **Impact:** tenant B's pending row tagged with tenant A's action id would be counted into A's
  action ledger (reused/billed counts, per-lead events) on the system session, which bypasses RLS.
  Accounting and billing-evidence corruption, and a disclosure vector if action results are ever
  shown per lead.
- **Fix:** before any writer ships, add the composite FK (`NOT VALID` + `VALIDATE`, the column is
  all NULL today so validation is instant), and correct the header comment.
- **Regression test:** as the system role, try to set `action_id` on a pending row whose `user_id`
  differs from the action's; assert a foreign-key violation.

### T-6 (Info) quote_id is unique across all tenants

- **Evidence:** `101:568` `UniqueConstraint("quote_id")`; plan `tasks/todo-lookup-contacts.md:384`
  re-fetches by `(quote_id, user_id, job_id)` after an IntegrityError.
- **Note:** safe while `quote_id` is server-minted and cryptographically random (the plan says so,
  `todo-lookup-contacts.md:561`). If it were ever client-influenced, a collision with another
  tenant's quote would produce IntegrityError followed by an empty RLS re-fetch: the 1b-2 handler
  must turn that into a 404/409, never a 500 or a "same action" answer. Consider
  `UNIQUE (user_id, quote_id)` to keep the uniqueness domain inside the tenant.
- **Test (1b-2):** confirm with a `quote_id` owned by another user returns 404/409 and creates nothing.

### T-7 (Info, accepted) Global submission collision key serializes lookups across tenants

- **Evidence:** `skip_trace.py:268-286` ("Deliberately GLOBAL"), used by the dispatcher
  (`skip_trace_dispatcher.py:660-670`).
- **Note:** if A and B both queue the same address, one waits a dispatcher tick. The only signal to B
  is a slightly longer "looking" state, mixed with ordinary queue noise. No data flows between them.
  This is the price of fail-closed attribution and is acceptable.

### T-8 (Info) Correction: not every tenant route is RLS-bound

The prior report said all 69 routes use `get_rls_db`. At HEAD, 30 of the 69 handlers use `get_db`, no session,
or a manual GUC: the pre-auth auth routes, `/billing/webhook`, `/billing/activation-funnel` (admin),
`/scrapers/sample`, `/scrapers/connectors` (GET and POST), `/jobs/{id}/download` (manual GUC at
`jobs.py:1390`), and four self-service routes that write the caller's own `users` row on `get_db`:
`PUT /auth/notification-preferences`, `PUT /auth/profile`, `POST /auth/logout-all`,
`POST /auth/api-key`. The `users` app policy is intentionally broad (auth reads users pre-GUC,
`apply_rls_cutover_policies.sql:134-139`), so RLS would not add protection there anyway; the
`User.id == current_user.id` filter is the boundary and it is present on all four. No leak.

### T-9 (Info, verify) 101's FORCE and role policies live in manually run scripts

- Migration 101 applies ENABLE + an untargeted GUC policy. FORCE and the per-role policies come from
  `apply_rls_force.sql` and `apply_rls_cutover_policies.sql`, which `start.sh` does not run. Merge is
  deploy, so 101 is live without them until someone runs the scripts.
- Tenant isolation for the app role holds either way (the inline policy is the tenant GUC). The
  consequences of not running them are: the table owner is not constrained (no FORCE), and the
  worker's `bridgeleads_system` session with no GUC sees zero rows, which is a functional failure
  and not a leak.
- **Action:** confirm read-only in prod that `relforcerowsecurity` is true for the three tables and
  that the `_app_*` / `_system*` policies exist, before 1b-2 ships.

### Carry-overs from the prior audit

- **INFO-1** public `GET /scrapers/connectors?include_all=true` still exposes `down`/`unknown`
  connectors (`scrapers.py:367-400`). Not tenant; route to sec-auth.
- **INFO-7** batch CSV/leads helpers still take `uid` from `run.user_id`
  (`batches.py:759`, `:908`, `:935`, `:944`, `:953`); correct today because every caller verified
  the run first.
- **INFO-10** `get_results` still counts `JobLog` without the Job join (`jobs.py:552-565`); job
  already proven owned.
- **INFO-4** fixed: the fallback limiter now evicts only expired buckets and fails closed
  (`src/api/middleware/rate_limit.py`).

---

## 8. Explicitly checked NON-findings

1. **No new routes since baseline;** changed routes keep their tenant predicates
   (`jobs.py:463-464`, `:1410`, `:1430`).
2. **`category` param** is a `Literal["new","already_delivered"]` (`results_category.py:105`);
   unknown values 422; `category_condition` raises on anything else (`:145-154`).
3. **Already-delivered provenance** never echoes another account's run id and never confirms it
   exists (provenance helper, `jobs.py:880+`).
4. **Dedup is tenant-scoped** (`models.py:1066`, `tasks.py:1205`, `:1258-1273`).
5. **v2 subject key includes `user_id`** (`skip_trace.py:243`); all five reuse paths use it.
6. **`skip_trace_cache` stays per tenant** (the key is the v2 subject hash; the model docstring was
   corrected, `models.py:1210-1233`); only the worker reads it.
7. **`reused` counter** (`AlreadyDeliveredContacts.reused`) counts only the caller's rows
   whose answer came from the caller's own earlier lookup.
8. **Claim primitive** pins result, job and owner to one uid and refuses mixed-tenant batches
   (`skip_trace_claim.py:383-388`, `:471-477`, `:534-541`); withdrawal deletes only its own
   generated pending ids (`:557-567`).
9. **Migration 100 unique index** is on tenant-owned `result_id`; a conflict cannot involve two
   tenants.
10. **`charged_unanswered`** is keyed on the per-tenant subject key (`skip_trace_dispatcher.py:740-751`).
11. **Tracerfy ingest** is queue-scoped, `(id, user_id)`-pinned and fails closed on any ambiguous
    address (`tracerfy_ingest.py:279-333`, `:596-611`, `:735-745`, `:806-807`); billing splits
    per user.
12. **SSE:** ownership before lease and subscribe (`jobs.py:966-970`), per-user lease, in-loop
    re-checks on RLS-bound short sessions (`:1102-1140`), keepalive carries no data.
13. **Download token** binds `job_id` (`jobs.py:1347`), honours blacklist and logout-all, sets the
    GUC before the first tenant read (`:1390`).
14. **Live Run progress** (mig 099) adds columns to `jobs` only; exposed via owned-job routes only.
15. **No request model** carries `user_id`/`plan`/`records_limit`/`is_admin`
    (grep of `schemas.py`); checkout plan is validated against the server's Stripe price map.
16. **Admin routes** are server-side gated: `activation-funnel` via `require_admin` (404 to non-admin,
    MFA enrollment required, `auth.py:429-446`); `create_connector` via `require_admin_mfa` (step-up,
    `:449+`).
17. **Retention purge** (`scheduler_helpers/retention.py:102-213`) is age-based and global; it
    deletes/NULLs and moves nothing between tenants.
18. **Migration 101** child tables use composite tenant FKs, per-verb policies, column-level grants
    and guard triggers that cannot be bypassed by a user-scoped session (section 6).

## 9. Not verified

- Production catalog state (FORCE flags, policies, grants for 101): code and scripts only. See T-9.
- `src/api/routes/billing.py` Stripe webhook tenant resolution: billing audit scope.
- The frontend repository.
- No test was run (hard rule: bare pytest reads the production `.env`).
