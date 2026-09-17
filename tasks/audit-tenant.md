# Multi-Tenant Isolation Audit — BridgeLeads

**Worktree:** `C:/Users/Windows/bl-wt-secaudit` @ `60f1b00`
**Scope:** ACCOUNT A MUST NOT ACCESS ACCOUNT B'S ACCOUNT-SCOPED DATA
**Method:** static read-only. No DB touched, no pytest run.
**Coverage:** all 69 route decorators across the 10 files in `src/api/routes/`, plus worker
dedup/quota/enrichment paths, every `system_sync_session()` call site, and every Redis key.

---

## Headline

**P0: none. Zero routes are missing their application-level `user_id` filter.**

All 69 routes enforce ownership with **both** an explicit `user_id` predicate **and** an
RLS-bound session (`get_rls_db`). There are zero RLS-only routes and zero filter-only
routes. The four weaknesses found are all worker-side, none request-reachable.

Per the team lead (already established, not re-verified here): RLS is enforced in
production (cutover 2026-06-12, `docs/BUILD_JOURNAL.md:5428`, `RLS_ENFORCE=true`, 47
role-targeted policies, FORCE on 23 tables), and cross-tenant reads on `results` and
`delivered_records` were empirically blocked under a real NOBYPASSRLS role. **The comment
at `src/db/session.py:386-392` claiming "the current prod role HAS BYPASSRLS" is STALE and
should be deleted** — it misleads every future reader of that file into thinking the query
filter is the only boundary.

One thing worth confirming at the environment level: the code default is
`RLS_ENFORCE: bool = False` (`src/config/settings.py:237`), so enforcement depends on the
prod env var being set. The fail-closed guard (`src/db/session.py:374-410`) is what
downgrades findings F2–F5 below from "live gap" to "defense-in-depth".

---

## RLS plumbing (how a session gets its tenant context)

Correct and commit-durable:

- `src/api/deps.py:36-41` — `get_rls_db` sets `app.current_user_id` via parameterized
  `set_config(..., true)` (transaction-scoped) and stashes the uid on `session.info`.
- `src/db/session.py:234-239` — an `after_begin` listener re-applies the GUC at the start
  of **every** transaction, so it survives `run_scrape_job`'s mid-task commits. Gated on
  `session.info['rls_user_id']`, so `system_sync_session()` and plain `get_db` sessions are
  deliberately untouched.
- Unauthenticated callers never reach a handler: `get_rls_db` depends on `get_current_user`,
  which raises 401 first. There is no path that yields an RLS session with an unset GUC.
- RLS **fails CLOSED** on an unset GUC — policies use
  `NULLIF(current_setting('app.current_user_id', true), '')::uuid`
  (`alembic/versions/018_rls_sprint_tables.py:68`), which is the Supabase-safe form
  (Supabase returns `''`, not NULL, from `current_setting(..., true)`).

Two routes deliberately run on non-RLS sessions and compensate by hand:
- `src/api/routes/jobs.py:1033` — `/download`; GUC set manually at `:1191-1195` before the
  first tenant read, every subsequent query filters `user.id` explicitly.
- `src/api/routes/scrapers.py:941` — `create_connector`; touches no tenant table.

---

## Step 1 — Full route ownership map (69 routes)

`CU` = `CurrentUser` (`src/api/auth.py:484`). `rls` = `get_rls_db`. All verdicts **OK**
unless marked otherwise.

### jobs.py (8 routes)

| METHOD | PATH | id param | auth | session | ownership enforcement | verdict |
|---|---|---|---|---|---|---|
| GET | `/jobs` | — | CU | rls | `:85 Job.user_id==cu.id`; join guards `:96`, `:109` | OK |
| POST | `/jobs` | `scraper_config_id` (body) | CU | rls | `:289-295 ScraperConfig.id==… AND .user_id==cu.id` → 404 `:298` | OK |
| GET | `/jobs/{job_id}` | job_id | CU | rls | `:310-312 Job.id==…, Job.user_id==cu.id`; config `:319` | OK |
| DELETE | `/jobs/{job_id}` (cancel) | job_id | CU | rls | `:336-345` single-stmt CAS `update(Job).where(id, user_id, status.in_(CANCELLABLE))`; probe `:349` also filtered | OK |
| GET | `/jobs/{job_id}/results` | job_id | CU | rls | parent `:391-393` → 404 `:396`; **all 9 child queries re-carry `Result.user_id`** | OK |
| GET | `/jobs/{job_id}/logs` (SSE) | job_id | CU | rls | `:789-791` + in-stream re-checks `:921`, `:947` | OK |
| GET | `/jobs/{job_id}/export-url` | job_id | CU | rls | `:984-986`; token bound to `(user.id, job_id)` `:1005` | OK |
| GET | `/jobs/{job_id}/download` | job_id + token | inline JWT | get_db + manual GUC `:1191` | `:1150` job-binding 403; `:1197-1199`, `:1213`, `:1268`, `:1326` all `user.id` | OK |

Every `Result` child query inside `get_results` carries the tenant predicate:
`:429`, `:494`, `:509`, `:580`, `:610`, `:642`, `:687`, `:701`/`:716`, `:751`.

Sole caveat (not a leak): `jobs.py:515-521` and `:525-531` count `JobLog` rows without
joining through `Job`. `JobLog` has **no `user_id` column** (`models.py:1085-1094`), and
`job_id` was already proven owned at `:391-396`, so only a `COUNT(*)` of a `LIKE` match is
reachable. The SSE path does this correctly via a join (`:918-923`); normalizing these two
to the same helper would close the inconsistency.

### scrapers.py (11 routes)

| METHOD | PATH | id param | auth | session | ownership enforcement | verdict |
|---|---|---|---|---|---|---|
| GET | `/scrapers/sample` | — | **public** | get_db | n/a — pre-redacted singleton `public_sample_cache` | OK by design |
| GET | `/scrapers` | — | CU | rls | `:121 ScraperConfig.user_id==cu.id` | OK |
| POST | `/scrapers` | — | CU | rls | `:322 user_id=current_user.id` on insert | OK |
| GET | `/scrapers/connectors` | — | **public** | get_db | n/a — `county_connectors` has no tenant column | OK (see INFO-1) |
| GET | `/scrapers/{scraper_id}` | scraper_id | CU | rls | `:442-443 id==… , user_id==cu.id` | OK |
| DELETE | `/scrapers/{scraper_id}` | scraper_id | CU | rls | `:461-462` same pair; soft-delete `active=False` `:468` | OK |
| PATCH | `/scrapers/{scraper_id}` | scraper_id | CU | rls | `:588-590` + `FOR UPDATE`; in-flight job guard `:645-647 Job.user_id==cu.id` | OK |
| PUT | `/scrapers/{id}/csv-layout` | scraper_id | CU | rls | `:855-857` + `FOR UPDATE`; job guard `:894-896` | OK |
| POST | `/scrapers/connectors` | — | `require_admin_mfa` | get_db | global admin table, no tenant column | OK |
| GET | `/scrapers/{config_id}/records` | config_id | CU | rls | config gate `:1157-1159`; view-state SQL binds `:user_id` `:1176`/`:1188` | OK (see INFO-2) |
| POST | `/scrapers/{cfg}/jobs/{job}/dialer-replay` | config_id + job_id | CU | rls | `:1324-1326` pins id + user + config; UPDATE re-asserts `DialerDelivery.user_id` `:1338` | OK |

### batches.py (8 routes)

Shared helpers: `_owned_batch` `:509-519` (`ScraperBatch.id==… , .user_id==uid`, 404),
`_run_for` `:529-541` (double-anchored on `BatchRun.user_id` **and** `ScraperBatch.user_id`).

| METHOD | PATH | id param | ownership enforcement | verdict |
|---|---|---|---|---|
| POST | `/batches` | — | writes only: `:333`, `:359`, `:393` all `user_id=current_user.id` | OK |
| GET | `/batches` | — | `:553`; children keyed off the already-filtered parent ids `:567`, `:581-583` | OK |
| GET | `/batches/{batch_id}` | batch_id | `_owned_batch`; children `:626`, `:647-648`, `:661-663` each re-carry `user_id` | OK |
| GET | `/batches/{id}/download` | batch_id | `:728`, `:729`; CSV built with `run.user_id` (provably == caller) | OK |
| GET | `/batches/{id}/runs` | batch_id | `:826` + `:830 BatchRun.user_id==cu.id` | OK |
| GET | `/batches/{id}/runs/{run_id}/download` | batch_id + run_id | `:852-856` triple-anchored: run id + parent batch + tenant → 404 `:859` | OK |
| GET | `/batches/{id}/leads` | batch_id | `:1028`, `:1029`; SQL re-anchors `batch_export.py:89-93` | OK |
| GET | `/batches/{id}/runs/{run}/leads` | batch_id + run_id | `:1071-1075` triple-anchored | OK |

All 4 route-level `Depends` are `get_rls_db`; `get_db` is never used in this file.

### segments.py (4 routes)

| METHOD | PATH | id param | ownership enforcement | verdict |
|---|---|---|---|---|
| POST | `/segments/intersection` | — | `:593` binds `uid=current_user.id`; SQL pins `j.user_id`, `sc.user_id`, `r.user_id` (`:206-216`) | OK |
| POST | `/segments/intersection/export` | — | `:640`, same SQL; CSV returned inline, no URL/key | OK |
| POST | `/segments/union` | — | `:738`; `_UNION_SQL` pins all three (`:285-287`) | OK |
| POST | `/segments/union/export` | — | `:766`, same SQL | OK |

Router-level `Depends(_require_overlap_plan)` `:97` is a **plan** gate, not a tenant gate;
tenant scoping is per-query as above.

### analytics.py (1 route)

| METHOD | PATH | ownership enforcement | verdict |
|---|---|---|---|
| GET | `/analytics/summary` | `:93 Result.user_id == uid` in the shared `base` tuple, spread into **all 4** aggregates (`:120`, `:138`, `:154`, `:186`); the `Job.id` subquery `:107` is scoped; both LEFT JOIN legs are tenant-scoped **in the ON clause** (`:62`, `:65-66`) | OK |

The ON-clause scoping is the correct construction — a foreign job/config cannot be joined
in to leak its `record_type`/`county`; it degrades to NULL → `'unknown'`. **No aggregate
over all tenants' rows exists in this file.**

### notifications.py (3 routes)

| METHOD | PATH | id param | ownership enforcement | verdict |
|---|---|---|---|---|
| GET | `/notifications` | — | `:34` + unread count `:44` | OK |
| PATCH | `/notifications/{id}/read` | notification_id | `:66-69` then 404 `:73-77`; UUID-parse guard `:60-63` returns 404 not 422, so no format oracle | OK |
| POST | `/notifications/read-all` | — | `:91-94 update(...).where(user_id==cu.id, read_at IS NULL)` | OK |

### auth.py (21 routes)

**No endpoint in this file accepts a key id or a user id from the client**, so there is no
IDOR surface. Every authenticated route self-scopes via `User.id == current_user.id`
(`:225`, `:256`, `:337`, `:456`) or reads `current_user` directly. Public/pre-auth routes
(`/config`, `/register`, `/login`, `/forgot-password`, `/reset-password`, `/verify-email`)
are credential-gated by token, not by ownership.

API keys: one key per user in `users.api_key_hash` (`models.py:105`). Create
`POST /auth/api-key` `:447-462` (plan-gated, writes own row). **No list endpoint** — the
raw key is unrecoverable by design. **No dedicated revoke endpoint** — revocation is a side
effect of `POST /auth/logout-all` `:315-342`, which clears the caller's own hash.

### webhooks.py (2 routes) — **UNVERIFIED at this layer**

| METHOD | PATH | auth | verdict |
|---|---|---|---|
| POST | `/webhooks/tracerfy` | shared secret, `hmac.compare_digest` `:165` | UNVERIFIED |
| POST | `/webhooks/tracerfy/{provided_secret}` | same, header-first `:181-182` | UNVERIFIED |

No DB session at all; `queue_id` → tenant resolution is deferred to
`src/workers/tracerfy_ingest.py`. Its `Result` writes **are** correctly tuple-pinned
(`:714-722`, `:774-786`), but the full queue→tenant path was outside my file scope.
Route onward to **sec-auth**. See INFO-8.

### billing.py (11 routes)

**No route in this file takes a path or body resource identifier** — IDOR surface is nil.
All are self-scoped via `current_user` (`/referral` `:218`, `/skip-trace-usage` `:296`,
`/usage` `:652`, `/subscription` `:706`, `/checkout` `:1005`, `/change-plan` `:1384`,
`/portal` `:1646`), public (`/plans` `:512`, `/pricing` `:554`), admin-gated
(`activation_funnel` `:118` via `require_admin`), or Stripe-signature-verified
(`/webhook` `:1676`).

---

## Step 2 — RLS-only vs filter-only

- **Routes enforced by RLS only, with no query filter: ZERO.**
- **Routes enforced by query filter only, with no RLS: TWO, both deliberate and
  compensated** — `jobs.py:1033` (`/download`, manual GUC at `:1191`) and
  `scrapers.py:941` (`create_connector`, touches no tenant table).

The project's "RLS is belt, query filter is suspenders" rule is met on every route.

---

## Step 3 — Tenant-table queries with no `user_id` predicate

**None in `src/api/`.** Four in `src/workers/` — F2–F5 below.

---

## Step 5 — Sub-resources reached through a parent

**No instance of the parent-checked-then-raw-id anti-pattern was found.**

Every child query is constrained twice: by the verified parent's own field **and** by an
independent `user_id` predicate.

- `get_batch` — jobs `:626`, results `:647-648`, configs `:661-663`.
- `_leads_page` `:866-998` and `_stream_run_csv` `:733-786` pass `run.child_job_ids`
  (parent-derived) with `uid=run.user_id` (verified by `_run_for`), and the SQL re-anchors:
  `batch_export.py:89-93` joins `j.user_id = CAST(:uid AS uuid)` and
  `sc.user_id = CAST(:uid AS uuid)` and filters `r.user_id = CAST(:uid AS uuid)`. Even a
  corrupt `child_job_ids` array holding a foreign job id is dropped by the join.
- `download_batch_run` / `list_batch_run_leads` do not trust `run_id` alone:
  `BatchRun.batch_id == batch_id` is asserted alongside `BatchRun.user_id` (`:854`, `:1073`).
  A valid `run_id` from tenant B under tenant A's `batch_id` yields 404.

**No endpoint in either file accepts an ARRAY of resource ids** (`result_ids[]`,
`lead_ids[]`, etc.), so there is no partial-ownership-check surface. The arrays that are
accepted (`counties`, `record_types`) are non-id value lists, regex-validated
(`schemas.py:1577`, `:1604-1606`).

---

## Step 6a — DEDUP / "already delivered" verdict

**PER-TENANT. Not a cross-tenant data leak. Intended behavior + a display bug already fixed.**

Delivery history is tenant-keyed at the schema level:

```python
# src/db/models.py:988-989
__table_args__ = (
    UniqueConstraint("user_id", "dedup_hash", name="uq_delivered_records_user_hash"),
)
```

and the claim primitive arbitrates on that composite key, so tenant A's claim can never
conflict with tenant B's:

```sql
-- src/workers/tasks.py:1065-1071
INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, …)
VALUES …
ON CONFLICT (user_id, dedup_hash) DO NOTHING
RETURNING dedup_hash
```

Every surrounding read/write is bound to the job's own user:
- fresh-row SELECT `tasks.py:1015`
- owned-claims re-read `tasks.py:1091`
- duplicate-flag UPDATE `tasks.py:1133-1136` — note the `LEFT JOIN delivered_records dr` is
  itself scoped `ON dr.user_id = CAST(:uid AS uuid)`
- claim release `dedup.py:301`, `:305`, `:310`
- anchor repoint `dedup.py:574-576`
- same-run collapse `dedup.py:679`, `:697`
- enrichment reuse `enrich.py:270-293` — pins `rn.user_id`, `ro.user_id` **and**
  `dr.user_id` to the same `:uid`

**Why a NEW Starter account saw "already delivered":** not inherited claims — its
`delivered_records` rows do not exist yet. What it saw was a **mis-attributed link**. Before
migration 089 the results page had no stored answer for "where was this delivered before",
and its fallback rule picked a run by an unrelated ordering:

```python
# src/workers/tasks.py:1113-1120
# Migration 089: stamp WHICH run holds the claim, NOW, while the answer is
# still knowable. … the link it offered instead was chosen by an unrelated
# rule that pointed at a run two months LATER (see get_results).
```

This matches the prior investigation recorded as `project_duplicate_scope_2026_09_08`
("49 already delivered" was NOT a leak; the page linked FORWARD in time). No tenant learns
anything about another tenant: `duplicate_source_job_id` is populated only from a claim row
already filtered to the caller's `user_id`, and `get_results` re-filters the linkable source
jobs by `Job.user_id == current_user.id` (`jobs.py:642`).

**Classification: CONFIRMED SECURE CONTROL.**

---

## Step 6b — QUOTA "1,001/50" verdict

**No. The count does not aggregate other tenants' rows. Within-tenant accounting artifact.**

`records_used` is a **stored column** (`models.py:107-108`), not a read-time aggregate.
Both `COUNT(*)`s that feed it carry **`user_id` AND `job_id`**:
- reservation `tasks.py:1605-1612`
- settlement `tasks.py:2024-2031`

Every write is single-user:
- `tasks.py:1647-1670` — `WHERE u.id = CAST(:uid AS uuid)`
- `tasks.py:2120-2154` — `JOIN jobs j ON j.user_id = u.id WHERE j.id = :jid`
- `status.py:297-303` — refund, decrement only, uid from the job's own `RETURNING user_id`
- `billing_entitlement.py:92` — single ORM object

The one RLS-bypassing quota write, `scheduler_helpers/billing.py:276-299`, has no bound
`:uid` — but it correlates `FROM w WHERE u.id = w.id` and its projected `base` is 0 for
every reachable row (`quota_window.py:297`). **It can only ever write zero.** No cross-row
value transfer is possible.

**Migration 088 does not touch `records_used` at all** (explicit at `088:30-33`); its three
backfills are per-row column-to-column copies. One correction to the standing project note:
`BACKFILL_FIRST_PAID` (`088:229-256`) does **not** ignore `trial_ends_at` — the legacy-payer
arm explicitly requires `trial_ends_at IS NULL`, and the comment at `088:244-249` says
legacy payers still carrying one are deliberately left to `expire_trials`.

**Where 1001 actually comes from:** trials are granted the **Pro** limit of 1000
(`routes/auth.py:98-104`). Every downgrade path sets `records_limit=50` while deliberately
preserving `records_used`:

```
# src/api/billing_entitlement.py:319-323
"records_used, the window and the anchor are deliberately UNTOUCHED…
 refunding the counter would be a small free grant on every cancellation."
```

A consumed trial plus a one-record settlement delta (`tasks.py:2147`) reads exactly
`1001/50` until the next entitlement anniversary rolls it to 0. All that tenant's own rows.

**Separate real bug found in passing (not a leak, route to billing owner):**
`/billing/usage` displays the **raw** `current_user.records_limit` (`billing.py:677`) while
enforcement uses `pending_records_limit` when the window has rolled (`quota.py:109-113`).
Display and enforcement can disagree on the *limit*, widening a mismatch like this
cosmetically.

**Classification: CONFIRMED SECURE CONTROL.**

---

## Step 7 — Job control verdict

**Tenant A cannot cancel, retry, or read the status/logs of tenant B's job.**

- **Cancel** is a single-statement compare-and-set with the tenant inside the predicate
  (`jobs.py:336-345`), so there is no TOCTOU window between check and mutate. The
  "why not cancellable" probe is filtered too (`:349`).
- **There is no retry endpoint.** `_retry_scrape_job` (`tasks_helpers/status.py:574`) is
  worker-internal, called only from `tasks.py:720` on failure. Not request-reachable.
- **Status / logs** are covered by the route table above.

**Classification: CONFIRMED SECURE CONTROL.**

---

## Step 8 — Live streams verdict

`GET /jobs/{job_id}/logs` (SSE), `jobs.py:773-907`. **CONFIRMED SECURE CONTROL.**

- **Authorized before anything streams** — `jobs.py:788-794`,
  `select(Job).where(Job.id == job_id, Job.user_id == current_user.id)`, 404 on mismatch.
- **Stream id is not guessable** — it is the job's own UUIDv4 (`models.py:662`,
  `default=_uuid`; minted `jobs.py:232`). No counter, no sequence.
- **Reconnect fully re-authorizes** — there is no Last-Event-ID or resume-token path, so a
  reconnect is a brand-new request through `CurrentUser` + the `:789` ownership query +
  fresh lease admission.
- **Re-checks inside the live loop, not just at open** — status poll `:943-949`
  (`select(Job.status).where(Job.id==job_id, Job.user_id==user_id)`), log replay `:952-955`
  through the join-filtered `_job_logs_select` (`:918-923`), both on short-lived RLS-bound
  sessions (`_stream_session` `:930-940`).
- Lease release is `asyncio.shield`ed in `finally` (`:899-901`), so a client disconnect
  cannot leak the SSE counter.

Pub/sub channel is `f"job_logs:{job_id}"` (`jobs.py:846`) — no tenant discriminator in the
channel name, but subscription is reachable only after the `:789` ownership check and the
id is an unguessable UUIDv4. Worth noting for infrastructure: Redis Pub/Sub has no
per-channel ACL, so anyone holding `REDIS_URL` credentials can `PSUBSCRIBE job_logs:*`.
That is a credential boundary, not an app bug.

---

## Step 9 — Cache isolation verdict

**CONFIRMED SECURE CONTROL.**

- **There is no Redis-backed API response cache**, and **zero memoizing decorators in
  `src/`** — `grep -rn "lru_cache|TTLCache|aiocache|fastapi_cache|cachetools" src/` returns
  no matches. The worst case (a response cache keyed without the user id) does not exist.
- Every key holding tenant data is discriminated by `user_id` or an unguessable UUID4:

| key | file:line | discriminator |
|---|---|---|
| `rl:{zone}:{key_id}` | `middleware/rate_limit.py:141` | `user_id` when `identifier=current_user.id` is passed; client IP on pre-auth zones |
| `sse_leases:{user_id}` | `sse_leases.py:51` | user_id |
| `bl:user_revoke:{user_id}` | `middleware/auth_hardening.py:52` | user_id |
| `bl:jti:{jti}` | `middleware/auth_hardening.py:51` | UUID4 |
| `bl:refresh_replay:{jti}` | `middleware/auth_hardening.py:102` | UUID4, 30s TTL |
| `job_logs:{job_id}` | `routes/jobs.py:846` | UUID4 + ownership gate |
| `founding_offer:FOUNDING25` | `routes/billing.py:446` | global, tenant-independent by design |

- The one cache holding purchased PII, `SkipTraceCache`, hashes `user_id` into its primary
  key: `sha256(user_id | addr | city | state)` (`enrichment/skip_trace.py:116-139`).
- Advisory locks are namespaced by classid and keyed on `hashtext(:uid)`
  (`entitlements.py:367`, `billing.py:1054`); a 32-bit collision costs serialization only,
  never a cross-tenant read.

---

## Findings

### F1 — Stale BYPASSRLS comment misrepresents the security model
**CLASSIFICATION:** INFO (documentation) · **SEVERITY: INFO**

`src/db/session.py:386-392` states *"the current prod role HAS BYPASSRLS and worker paths
depend on it"*. Per the team lead this is **stale** — the cutover landed 2026-06-12
(`docs/BUILD_JOURNAL.md:5428`). The same stale claim is echoed in
`alembic/versions/028_county_records_shared_read_policy.py:6` ("verified live"),
`src/api/download_tracking.py:56`, and `src/workers/batch_export.py:374`.

**Why it matters:** this is the single most load-bearing sentence in the file for anyone
reasoning about tenant isolation. A reader who believes it concludes the query filter is
the only boundary, which inverts the risk assessment for F2–F5.

**FIX:** delete or correct all four comments; confirm `RLS_ENFORCE` is actually `true` in
the prod environment, since the code default is `False` (`src/config/settings.py:237`).

### F2 — `jobs → scraper_configs` tenant pairing unenforced in code AND schema
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P2** (highest-consequence of the four)

```python
# src/workers/tasks.py:464-466
config = db.execute(
    select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)
).scalar_one()
```

No `ScraperConfig.user_id == job.user_id`. There is also **no composite FK backing it**:
`Job.__table_args__` (`models.py:753-766`) carries only two indexes, while `ScraperConfig`
(`models.py:459-464`) *does* pin `(batch_id, user_id) → scraper_batches(id, user_id)` with
the comment *"Tenant-scoped composite FK (Codex P1)"*. So the Job→Config link is the one
parent relationship in this schema with **neither** a DB constraint **nor** a query
predicate.

Every sibling path closes this hole and says exactly why:
```python
# src/workers/scheduler_helpers/dialer.py:116-122
# the DB doesn't enforce job.user_id == config.user_id, and this sweep runs in
# a system session that bypasses RLS — without this, a malformed job (user A)
# pointing at user B's config could push A's lead PII to B's dialer_webhook_url
```
Same owner re-read at `batch_tasks.py:148-151` and `dialer_outbox.py:87-92`.

**ATTACK PATH:** not reachable today. `scraper_config_id` is the one request-supplied
selector feeding this, and every Job-creation site validates it —
`routes/jobs.py:289-295`, `batch_tasks.py:148`, `dispatch.py:396` (copies `config.user_id`)
— and PATCH locks config identity fields (`scrapers.py:553-562`). The exposure is that a
single malformed `jobs` row, from a future code path / repair script / partial restore,
makes the worker scrape with a foreign tenant's `deliver` block (`dialer_webhook_url`,
delivery `emails`, dialer credentials) while attributing results and billing to
`job.user_id`. RLS does not catch it because both rows are read inside
`rls_sync_session(job.user_id)` — the foreign config would simply be invisible and
`scalar_one()` would raise, which is a crash rather than a leak, but only while RLS is on.

**FIX:** add `ScraperConfig.user_id == job.user_id` at `tasks.py:465` (one line, matching
the three siblings), **and** add the composite FK
`(scraper_config_id, user_id) → scraper_configs(id, user_id)` on `jobs` so the invariant is
enforced where it cannot be forgotten.

### F3 — Skip-trace dispatcher drops the `(id, user_id)` tuple pairing on two updates
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3**

`src/workers/skip_trace_dispatcher.py:773-779` (`→ "submitted"`) and `:872-881`
(`→ "errored"`) do `update(Result).where(Result.id.in_([c.result_id for c in claimed]), …)`
under `system_sync_session()` — a session that deliberately sets no RLS context, draining a
queue that spans tenants by design (`:60-63`, `:99`).

The statements immediately around them use the correct form —
`tuple_(Result.id, Result.user_id).in_([(r.result_id, r.user_id) …])` at `:561-570` and
`:683-690` — and `tracerfy_ingest.py:774-786` spells out the rule: *"Tenant filter is
MANDATORY here, not decorative … pinned with a (id, user_id) tuple that preserves the
pairing instead of matching ids from any tenant."* `_Claim` already carries `user_id`
(`:1200`), so these two were simply missed.

**ATTACK PATH:** none directly — ids come from the dispatcher's own claim set, so each write
lands on the row it was chosen for, and the write is status-only.
**FIX:** one-line `tuple_` pairing on both, matching `:561-570`.

### F4 — NTS matcher writes `results` with no tenant column in the predicate at all
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3**

```sql
-- src/workers/nts_matcher_task.py:307-319
UPDATE results SET
    auction_date = :auction_date, default_amount = :default_amount,
    nts_match_confidence = :confidence, nts_notice_id = :notice_id,
    enrichment_data = COALESCE(enrichment_data, '{}'::json)::jsonb
                      || jsonb_build_object('nts', CAST(:nts AS jsonb))
WHERE id = :rid AND (auction_date IS NULL OR …)
```

The only `results` write in the repo with **zero** tenant column in the predicate. On the
beat path (`match_nts_notices` `:68-101`) the `:rid`s come from a cross-tenant SELECT
(`:79-95`) whose `jobs → scraper_configs` join is itself **not tenant-pinned** — no
`j.user_id = r.user_id`, no `sc.user_id = j.user_id`. Contrast
`skip_trace_dispatcher.py:100-113`, which pins both.

**ATTACK PATH:** no disclosure — the payload is public county NTS notice data and each row
keeps its own owner. The risk is a cross-tenant **write** with no guard: any upstream id
error silently rewrites another tenant's lead.
**FIX:** carry `r.user_id` through `_match_and_write` and add `AND user_id = :uid`; pin both
joins in the candidate SELECT. The same one-line consistency fix applies to
`trustee_sale_finalize.py:141-156`, whose every *other* statement is user-pinned
(`:169-176`, `:190-201`, `:234-240`, `:252-258`, `:262-268`).

### F5 — `dispatch_batch_run(run_id)` resolves any run/batch id with no owner check
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3**

`src/workers/batch_tasks.py:56-92` (`_resolve_run`) does
`select(BatchRun).where(BatchRun.id == ref).with_for_update()` and `db.get(ScraperBatch, ref)`
under `system_sync_session()`, and its argument is request-influenced
(`routes/batches.py:414 dispatch_batch_run.delay(run.id)`).

**ATTACK PATH:** **not a read leak.** The route creates that run with
`user_id=current_user.id` (`batches.py:391-397`), and everything downstream re-pins to the
row's own owner — quota `db.get(User, batch.user_id)` (`:132`), configs
`ScraperConfig.user_id == batch.user_id` (`:150`), children `user_id=c.user_id` (`:187`).
A foreign run id would fan out **that tenant's** batch against **their** quota and **their**
delivery emails; nothing returns to the caller. It is a denial-of-quota / unauthorized-action
shape, not disclosure, and reaching it requires a code path that passes an unvalidated id —
none exists today.
**FIX:** have `_resolve_run` take and assert an owner.

---

## Informational (not tenant-isolation; route onward)

**INFO-1 — `GET /scrapers/connectors?include_all=true` is unauthenticated.**
`scrapers.py:369`, `:396-399`. Reveals `down`/`unknown` connectors — which county scrapers
are currently broken — plus `base_url`, `gis_endpoint`, `assessor_url` (the exact hosts the
SSRF allowlist was extended to at `:997-999`) to anonymous callers. The docstring calls it
"admin tooling and support investigation"; there is no admin gate. → **sec-auth / sec-ssrf**

**INFO-2 — `county_records` is a shared catalog by design, with unbounded pagination.**
Any authenticated user with one active config in a county reads that county's **entire**
catalog including `party_name`, `heirs`, `property_address`, `mailing_address`, `parcel_id`,
`legal_description`. Migration 028 states this explicitly (*"the documented freemium
model"*). `CountyRecord` (`models.py:1097-1115`) has **no tenant columns at all**, so "who
scraped it" and delivery state cannot leak. Not cross-tenant. But `page_size` is capped at
500 (`scrapers.py:1148`) while `page` is **unbounded** (`:1147`), with no per-record
metering on this path (`enforce_entitlements` runs only at config-create, `:265-272`) — one
config enumerates a whole county in 500-row pages. Also note `GET /{config_id}/records`
**writes on a GET** (`INSERT … ON CONFLICT` on `user_record_views`, `:1181-1189`) — correctly
tenant-scoped, but relevant to any prefetch or CDN caching.

**INFO-3 — public sample cache publishes the 5 newest matching `results` rows globally.**
`scheduler_helpers/public_cache.py:43-94`. PII is redacted at write time
(`_generalize_address` `:27-33`, name anonymization `:60-66`) and the public endpoint never
live-queries tenant tables. Safe as built; a regression in that redaction is a public leak,
so it deserves a pinned test.

**INFO-4 — in-process rate-limit fallback is globally flushable.**
`middleware/rate_limit.py:126-127` does `_fallback_hits.clear()` past 10,000 entries. During
a Redis outage the auth/webhook/stripe zones fall back to this limiter (`:171`); an attacker
driving 10,000 distinct identifiers wipes every in-flight counter, restoring an unthrottled
window. LOW, outage-conditional. **FIX:** evict expired buckets instead of `clear()`.
→ **sec-auth**

**INFO-5 — `dupsignup:` uses the 48-bit fingerprint the codebase marks "for LOGS only".**
`routes/auth_helpers/registration.py:98-99` keys on `email_fingerprint(email)` =
`hmac_sha256(...)[:12]`, while `middleware/auth_hardening.py:536-539` explicitly requires
the full-length `blind_index` for anything cross-account. Consequence is a suppressed
courtesy email, not a data leak. LOW.

**INFO-6 — no environment namespace on any Redis key.** `settings.py:34-40` exposes only a
bare `REDIS_URL`. If staging and prod ever share a URL *and* DB index, `bl:jti:*`,
`bl:user_revoke:*`, `bf:*`, `rl:*` collide — a staging logout would revoke production
sessions. Deploy hygiene.

**INFO-7 — `_stream_run_csv` and `_leads_page` take `:uid` from `run.user_id`.**
`batches.py:733-743` and `:866-883`, under a **docstring-only** ownership contract
(`:738-740`, `:876-877`). All four current callers verified the run first, so this is
correct today; a fifth caller that fetched `run` by id alone would silently cross tenants.
**FIX:** pass `current_user.id` instead. Related: `render_combined_csv` runs on
`system_sync_session()` (`batch_export.py:374`), so the predicates at `:89-93` are the sole
boundary for the batch-download path even after the cutover — worth a comment marking those
five lines as a single point of failure.

**INFO-8 — `/webhooks/tracerfy` tenant resolution is worker-side and unaudited here.**
The legacy path-secret variant (`webhooks.py:169`, flagged in-file at `:45-47` as a log
leak), the IP-only rate-limit bucket (`:164`, `:180` pass no `identifier`, so a distributed
brute-force of the secret is not throttled as one bucket), and the attacker-supplied
`download_url` (`:98` → `:145`) are the live items. → **sec-auth / sec-ssrf**

**INFO-9 — `ai_nav_cache:{base_url}:{record_type}` has no tenant key (latent, dead code).**
`scrapers/ai/cache.py:19,27-29` caches navigation actions that are later **executed**.
`grep` finds only the definitions — no production callers — and `base_url` comes from the
operator-managed `SourceConnector` table. If `base_url` ever becomes tenant-supplied, tenant
A could poison the shared key and have tenant B replay A's actions. **FIX:** delete the
module, or add the connector id to `_cache_key`.

**INFO-10 — `JobLog` counts in `get_results` skip the parent join.**
`jobs.py:515-521` and `:525-531`. `JobLog` has no `user_id` column
(`models.py:1085-1094`), `job_id` was already proven owned at `:391-396`, and only a
`COUNT(*)` of a `LIKE` match is reachable. Consistency gap against the file's own stated
rule (`:93-95`); the SSE path does it correctly at `:918-923`.

---

## What I did not verify

- **No database was touched.** No pytest, no queries — per the standing prohibition (bare
  pytest reads the production `.env` and has wiped production twice). Every claim above is
  from source.
- **RLS enforcement in prod** is taken from the team lead's empirical verification, not
  observed by me. My own reading of `src/config/settings.py:237` shows the code default is
  `False`, so the prod env var is what carries it — worth a one-command confirmation using
  the query already implemented at `src/db/session.py:295-300`.
- **`src/workers/tracerfy_ingest.py`** was spot-checked, not fully audited. Its `Result`
  writes are correctly tuple-pinned (`:714-722`, `:774-786`), but the `queue_id → tenant`
  resolution path behind `/webhooks/tracerfy` was outside my file scope.
- **`src/api/routes/billing.py`** (119 KB) was read only for quota and route-signature
  purposes, not line-by-line. It has no id-taking routes, so its IDOR surface is nil, but
  its Stripe webhook tenant resolution belongs to whoever owns billing.
- **Frontend** (`bridgeleads-web`) was entirely out of scope.
