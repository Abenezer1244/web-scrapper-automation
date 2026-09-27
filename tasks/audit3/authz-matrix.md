# Audit 3, leaf 1.3: authorization matrix (Check 5, Check 9, Check 15, plan entitlement, quota)

Code under audit: backend worktree `C:/Users/Windows/bl-wt-secaudit3` at `786efcf0` (origin/main, production). Frontend: fresh worktree `C:/Users/Windows/bl-web-audit3-leaf13` at origin/master `e42d5d0`.

Method:
- Routes enumerated programmatically: `main.app` imported with the safe test env from the brief, walking FastAPI 0.141 `_IncludedRouter.original_router.routes` recursively and flattening each route's dependency tree (`cmd:python routes.py`, scratch script). Result: 71 routes (69 router routes + `/health` + `/ready`), no mounts, no websockets, no router-level dependencies except `segments` (`_require_overlap_plan`).
- Cross-check: `cmd:grep -rnE "@(router|app)\.(get|post|put|patch|delete)" src main.py` gives 75 hits; 4 are docstring/comment hits (`src/api/auth.py:436`, `src/api/middleware/rate_limit.py:6`, and their two `.pyc` copies). 71 = 71, no router missed.
- In-process reproduction on an isolated DB `bl_audit3_authz_test` (alembic head = 102), Redis db 15, `ENTITLEMENT_ENFORCEMENT=true`, `httpx.ASGITransport(app=main.app)`; scripts `repro.py` and `repro2.py` in my scratch dir. No production traffic was sent (a two-request unauthenticated probe was refused by the session's permission policy, see Not verified).

## Check 5: per-endpoint authorization matrix

Legend. Authn: `JWT|key` = `get_auth_context` (`src/api/auth.py:286`) accepting a bearer JWT or a `bl_` API key; `JWT only` = `require_session` (`auth.py:413`). Tenant: the explicit `user_id` predicate the route itself applies (RLS via `get_rls_db`, `src/api/deps.py:18`, is the second layer where noted). Admin: `require_admin` (`auth.py:477`, 404 to non-admins) or `require_admin_mfa` (`auth.py:497`). Plan: server-side plan gate. An API key is refused on every route below Business (`auth.py:331`).

| Endpoint | Method | Authentication | Authorization | Tenant check | Admin | Plan | Finding |
|---|---|---|---|---|---|---|---|
| /health | GET | none | public liveness | n/a | no | no | none |
| /ready | GET | none | public, coarse body + ref | n/a | no | no | none |
| /auth/config | GET | none | public flags | n/a | no | no | none |
| /auth/register | POST | none | public | creates own row; plan fixed server-side `routes/auth_helpers/registration.py:188` | no | no | none (auth leaf) |
| /auth/verify-email | POST | email token | token-bound | n/a | no | no | none (auth leaf) |
| /auth/login, /auth/login/mfa, /auth/login/break-glass | POST | credentials | n/a | n/a | no | no | none (auth leaf) |
| /auth/refresh | POST | refresh JWT | refresh audience pin `auth.py:238` | own sub | no | no | none (auth leaf) |
| /auth/me | GET | JWT or key | self | returns `current_user` `routes/auth.py:227` | no | no | none |
| /auth/notification-preferences | PUT | JWT or key | self | `User.id == current_user.id` `routes/auth.py:246`; body `extra=forbid` `schemas.py:311` | no | no | none |
| /auth/profile | PUT | JWT or key | self | `User.id == current_user.id` `routes/auth.py:277`; only first/last name, `extra=forbid` `schemas.py:287` | no | no | none |
| /auth/onboarding | GET | JWT or key | self | `ScraperConfig.user_id`, `Job.user_id` `auth_helpers/session.py:29,34` | no | no | none |
| /auth/logout | POST | token in body | own token | revokes jti + family | no | no | none |
| /auth/logout-all | POST | JWT or key | self | own user id | no | no | none |
| /auth/change-password | POST | JWT only | self + reauth | own row | no | no | none |
| /auth/mfa/status | GET | JWT or key | self | own row | no | no | none |
| /auth/mfa/setup, /enable, /disable | POST | JWT only | self | own row | no | no | none |
| /auth/forgot-password, /auth/reset-password | POST | none / reset token | token-bound | n/a | no | no | none (auth leaf) |
| /auth/api-key | POST | JWT only + password | self | own row `routes/auth.py:516` | no | business, agency `routes/auth.py:501` | none |
| /scrapers/sample | GET | none | public | reads only `public_sample_cache` id=1 `scrapers.py:86` | no | no | none (PII leaf) |
| /scrapers | GET | JWT or key | owner | `ScraperConfig.user_id == current_user.id` `scrapers.py:121` | no | no | none |
| /scrapers | POST | JWT or key | owner | row created with `user_id=current_user.id` `scrapers.py:322` | no | county cap + record type `scrapers.py:266` (flagged), delivery, skip trace, formats, frequency `scrapers.py:286` (always) | none |
| /scrapers/connectors | GET | none; `include_all=true` needs admin | public picker; admin view | n/a (global table) | `require_admin` on include_all `scrapers.py:399-400` | no | AZ-3 (API key passes) |
| /scrapers/connectors | POST | JWT only (via step-up) | admin | global table | `require_admin_mfa` `scrapers.py:942` | no | none |
| /scrapers/{scraper_id} | GET | JWT or key | owner | `id` AND `user_id` `scrapers.py:447-450` | no | no | none |
| /scrapers/{scraper_id} | DELETE | JWT or key | owner | `id` AND `user_id` `scrapers.py:466-469` | no | no | none |
| /scrapers/{scraper_id} | PATCH | JWT or key | owner | `id`, `user_id`, `active`, FOR UPDATE `scrapers.py:592-598`; identity fields immutable `scrapers.py:559` | no | enable-delta gates `scrapers.py:768` | none |
| /scrapers/{scraper_id}/csv-layout | PUT | JWT or key | owner | `id`, `user_id`, `active` `scrapers.py:858-863` | no | no | none |
| /scrapers/{config_id}/records | GET | JWT or key | owner of config | config `id`, `user_id`, `active` `scrapers.py:1161-1167`; then reads the SHARED `county_records` by county `scrapers.py:1221,1247` | no | none | AZ-1 |
| /scrapers/{config_id}/jobs/{job_id}/dialer-replay | POST | JWT or key | owner | `Job.id`, `Job.user_id`, `scraper_config_id` `scrapers.py:1329-1333`; update on `DialerDelivery.user_id` `scrapers.py:1343-1344` | no | none | AZ-4 |
| /jobs | GET | JWT or key | owner | `Job.user_id` `jobs.py:130`; join config also by `user_id` `jobs.py:141,154` | no | no | none |
| /jobs | POST | JWT or key | owner | config `id`, `user_id`, `active` `jobs.py:334-340` | no | run-time entitlement `jobs.py:204`, AI limit `jobs.py:217`, quota/frozen/ended `jobs.py:269` | AZ-6 (AI limit race) |
| /jobs/{job_id} | GET | JWT or key | owner | `Job.id`, `Job.user_id` `jobs.py:356`; config by `user_id` `jobs.py:364` | no | no | none |
| /jobs/{job_id} | DELETE | JWT or key | owner | atomic UPDATE on `id`, `user_id`, cancellable status `jobs.py:382-387` | no | no | none |
| /jobs/{job_id}/results | GET | JWT or key | owner, run must be `done` | `Job.user_id` `jobs.py:440`; every Result query also `Result.user_id` `jobs.py:484,557,572,666,707,816,851,881`; provenance `jobs.py:930,944,952` | no | quota enforced by `actionable_condition` excluding `over_quota` `lead_actionability.py:75-89` | none |
| /jobs/{job_id}/logs (SSE) | GET | JWT or key | owner | `Job.user_id` `jobs.py:993`; log select joins Job with `user_id` `jobs.py:1134-1137`; live status `jobs.py:1163` | no | per-user lease cap | none |
| /jobs/{job_id}/export-url | GET | JWT or key | owner, run `done` | `Job.user_id` `jobs.py:1204` | no | no | none |
| /jobs/{job_id}/download | GET | 60 s download token or bearer JWT | token `job_id` must match `jobs.py:1376`; `is_active` `jobs.py:1407` | `Job.user_id`, `Result.user_id`, config `user_id` `jobs.py:1426,1443,1558` | no | no | AZ-5 (family revocation not checked) |
| /billing/activation-funnel | GET | JWT or key | admin | aggregate via SECURITY DEFINER fn `billing.py:166` | `require_admin` `billing.py:121` | no | AZ-3 (API key passes) |
| /billing/referral | GET | JWT or key | self | `User.id == current_user.id`, `ReferralEvent.referrer_id == user.id` | no | no | none |
| /billing/skip-trace-usage | GET | JWT or key | self | reads `current_user` columns only | no | no | none |
| /billing/plans, /billing/pricing | GET | none | public catalog | n/a | no | no | none |
| /billing/usage, /billing/subscription | GET | JWT or key | self | `current_user` only | no | no | none |
| /billing/checkout | POST | JWT or key | self | per-user advisory lock + re-read `billing.py:1054-1063`; price must be a sold price `billing.py:1029` | no | plan derived from Stripe price in webhook `billing.py:1953` | none |
| /billing/change-plan | POST | JWT or key | self | same lock `billing.py:1460`; sold price only `billing.py:1438` | no | plan applied only by webhook | none |
| /billing/portal | POST | JWT or key | self | own `stripe_customer_id` | no | no | none |
| /billing/webhook | POST | Stripe signature `billing.py:1756` | signed event | user bound by `metadata.user_id` AND customer match `billing.py:2000-2009` | no | n/a | none (billing leaf) |
| /webhooks/tracerfy | POST | shared secret header, constant-time `webhooks.py:43-63` | vendor | n/a | no | n/a | none here (egress/billing leaves own the body trust) |
| /webhooks/tracerfy/{provided_secret} | POST | path secret, 410 when disabled `webhooks.py:196-201` | vendor | n/a | no | n/a | prior B-6, secrets leaf |
| /segments/intersection, /intersection/export, /union, /union/export | POST | JWT or key | owner | every CTE joins jobs AND configs AND results on `:uid`, `j.status='done'` `segments.py:221-223,324-326,435-437,513-515` | no | router dependency `_require_overlap_plan` `segments.py:66,95-99` (business, agency) | none |
| /batches | POST | JWT or key | owner | rows created with `user_id` `batches.py:333,360,394` | no | BATCH_PLANS `batches.py:184`, size cap `:192`, skip trace/format/frequency/overlap `:228-266`, quota `:275`, entitlements `:311` | none |
| /batches | GET | JWT or key | owner | `ScraperBatch.user_id`, `BatchRun.user_id`, `ScraperConfig.user_id` `batches.py:553,567,581` | no | no | none |
| /batches/{batch_id} | GET | JWT or key | owner | `_owned_batch` `batches.py:509-518`, `_run_for` joins batch on `user_id` `batches.py:522-541`; jobs/results by `user_id` `batches.py:625,647,663` | no | no | none |
| /batches/{batch_id}/download | GET | JWT or key | owner | `_owned_batch` + `_run_for`; combined SQL filters `r.user_id`, `j.user_id`, `sc.user_id`, `j.status='done'` `workers/batch_export.py:89-93` | no | no | none |
| /batches/{batch_id}/runs | GET | JWT or key | owner | `_owned_batch` + `BatchRun.user_id` `batches.py:830` | no | no | none |
| /batches/{batch_id}/runs/{run_id}/download | GET | JWT or key | owner | `_owned_batch`; run `id`, `batch_id`, `user_id` `batches.py:852-856` | no | no | none |
| /batches/{batch_id}/leads, /runs/{run_id}/leads | GET | JWT or key | owner | same as downloads `batches.py:1028-1029,1069-1076` | no | no | none |
| /notifications | GET | JWT or key | owner | `Notification.user_id` `notifications.py:33,43` | no | no | none |
| /notifications/{notification_id}/read | PATCH | JWT or key | owner | `id` AND `user_id` `notifications.py:67-69` | no | no | none |
| /notifications/read-all | POST | JWT or key | owner | `user_id` `notifications.py:91-92` | no | no | none |
| /analytics/summary | GET | JWT or key | owner | `Result.user_id`, joins `Job.user_id`, `ScraperConfig.user_id`, `Job.status='done'` `analytics.py:58-66,93,107` | no | no | none |

Workers that take ids originating in the API (negative controls):
- `run_scrape_job(job_id)`: the id is minted server-side (`jobs.py:277`) only after the config ownership check; the worker re-derives the user from the job row and re-runs the entitlement check (`workers/tasks.py:525-537`).
- `dispatch_batch_run(run_id)`: server-minted (`batches.py:391`); the worker resolves the run with no owner assertion (`workers/batch_tasks.py:106`), prior T-4, still open, P3 in the tenant leaf. No client-supplied run id reaches it.
- `process_dialer_outbox(job_id)`: enqueued by the replay route only after `Job.user_id` is proven (`scrapers.py:1329-1354`); the worker scopes rows by `job.user_id` (`workers/dialer_outbox.py:81-100`). Plan not re-checked, see AZ-4.
- `ingest_tracerfy_batch(queue_id, download_url)`: from the shared-secret webhook; body trust belongs to the billing/egress leaves (prior B-5).

Two-account IDOR in-process sample (negative control for the matrix): user `over` read its own config's records (200) and user `frozen` its own; no cross-user id was accepted anywhere in the traced predicates. Full live two-account IDOR testing belongs to the live leaf.

## Check 9: admin routes

Server-side admin surface is exactly three code paths (`cmd:grep -rn is_admin src` shows `is_admin` read only at `src/api/auth.py:481`):

| Route | Gate | Non-admin result (reproduced) | Admin API key result (reproduced) | Admin JWT without MFA amr |
|---|---|---|---|---|
| POST /scrapers/connectors | `require_admin_mfa` `scrapers.py:942` | 404 `Not found` (Agency non-admin) | 403 `admin_mfa_step_up_required` | 403 `admin_mfa_step_up_required` |
| GET /scrapers/connectors?include_all=true | `require_admin` `scrapers.py:399-400` | 404 | 200, full connector list incl. down/unknown | 200 |
| GET /billing/activation-funnel | `require_admin` `billing.py:121` (after IP rate limit) | 404 | 200, funnel counts | 200 (read-only, enrollment required, no step-up, by design `billing.py:146-151`) |

Evidence: `cmd:python repro.py` output lines "non-admin agency ... 404", "admin API key ... 200/403", "admin pwd-only JWT ... 403/200".

- An admin with `mfa_enabled=false` gets 403 `admin_mfa_enrollment_required` on all three (`auth.py:487-493`, code read, not executed).
- API keys pass `require_admin` because it checks only `is_admin` and `mfa_enabled`, not `auth_method` (`auth.py:477-494`). That is AZ-3.
- Admin-ish actions on non-admin routers: none found. Connector update/delete/deactivate, account management, plan override and ops actions have no HTTP route; they are migrations and `scripts/` run by the operator. `GET /scrapers/connectors` (default) and `/scrapers/sample` are public by design and read only healthy connectors and the pre-sanitized sample cache.
- The frontend "Admin" nav group (Counties, Funnel) is shown by PLAN (`lib/nav.ts:28,35` `agencyOnly`, `admin/connectors/page.tsx:49` `userPlan === "agency"`), not by `is_admin`. The backend is stricter (is_admin), so this only shows an Agency customer a page whose calls 404 (AZ-7, INFO).

## Check 15: client-side checks and their server enforcement

Grep over the FE worktree (`cmd:grep -rnE "is_admin|isAdmin|plan ===|canUse|records_limit|agencyOnly|minPlan" app lib components hooks proxy.ts`):

| FE check (file:line) | What it hides or blocks | Backend enforcement point | Result |
|---|---|---|---|
| `lib/entitlements.ts:73` canUseRecordType, `CountyStep.tsx:369,679` | record types per plan | `enforce_entitlements` `src/api/entitlements.py:340` via `scrapers.py:266`, `batches.py:311`; run time `jobs.py:204`, worker `tasks.py:533` | enforced, but flag-gated by `ENTITLEMENT_ENFORCEMENT` (see Plan entitlement) |
| `lib/entitlements.ts:84` countyCap | county count | `projected_county_overage` `entitlements.py:290` under advisory lock `entitlements.py:365-369` | enforced when flag on (reproduced 402 for a 2nd Starter county) |
| `lib/entitlements.ts:89` canBatch | batch scrape | `batches.py:184` BATCH_PLANS | enforced (always) |
| `lib/entitlements.ts:94` canUseWebhook | webhook/dialer | `scrapers.py:216-219`; batches reject webhooks `batches.py:205-216`; worker `tasks.py:2463`, dialer sweep `scheduler_helpers/dialer.py:215` | enforced; replay path gap AZ-4 |
| `lib/entitlements.ts:104` canUseOverlap, `segments/page.tsx:94`, `lib/nav.ts` minPlan | Lists | router dependency `segments.py:66,95-99`; batch `overlaps_only` `batches.py:259-265` | enforced (always) |
| `lib/entitlements.ts:118` canUseExportFormat | export formats | `scrapers.py:233`, `batches.py:233` | enforced (always) |
| `lib/entitlements.ts:139` canUseFrequency | schedule frequency | `scrapers.py:237`, `batches.py:236` | enforced at save; not re-checked at dispatch for a downgraded account (grandfathered by design, `scrapers.py:194-199`) |
| `lib/entitlements.ts:151` canUseApiKeyPlan, `ApiKeysTab.tsx:58` | API keys | mint `routes/auth.py:501`; every request `auth.py:331` | enforced (always) |
| `lib/entitlements.ts:156` canSkipTracePlan, `scrapers/new/page.tsx:110` | skip trace | `scrapers.py:220-231`, `batches.py:228-232`; worker `tasks_helpers/enrich.py:2284` | enforced for Starter; trial Pro not blocked (AZ-2) |
| `admin/funnel/page.tsx:52` is_admin | funnel page | `require_admin` `billing.py:121` | enforced |
| `admin/connectors/page.tsx:49` plan agency; `lib/nav.ts:35` | Counties page and create form | `require_admin` / `require_admin_mfa` | enforced server-side (stricter than FE), AZ-7 INFO |
| `scrapers/[id]/records/page.tsx:76-82` isFrozen / isOverLimit banner | shows a "blocked" banner; the records query still runs (`:86-93`) | NONE: `GET /scrapers/{id}/records` has no quota, frozen, ended-entitlement, or Starter-delay check `scrapers.py:1147-1301` | GAP, AZ-1 (the prior FE report listed this row as "not re-verified"; now reproduced) |
| `dashboard/page.tsx:166`, `UsageBadge.tsx:19`, `quota-upgrade-banner.tsx:10` | quota display only | `jobs.py:269`, `batches.py:275`, worker reservation `tasks.py:1784-1806` | enforced |
| `proxy.ts` session gate | page routing | every API route authenticates itself (matrix above) | FE gate is not relied on |

## Plan entitlement

Where each entitlement is enforced server-side (only server-side counts):

- County count: `enforce_entitlements` `src/api/entitlements.py:340-391`, per-user `pg_advisory_xact_lock(4242, ...)` `:365-369`, counted over ACTIVE configs only `:330`. Run time: `config_run_violation` at `jobs.py:204`, dispatcher `scheduler_helpers/dispatch.py:138-151`, batch fan-out `batch_tasks.py:161-177`, worker `tasks.py:525-537`. All of these raise or block only when `ENTITLEMENT_ENFORCEMENT` is true (`entitlements.py:386`, `:484`, `:503`). Code default is False (`src/config/settings.py:194`); `docs/ENTITLEMENT-AUDIT-2026-09-08.md:12` and `docs/BUILD_JOURNAL.md:2282,2393` state it is true on api and worker in production. Not independently verifiable without production env access (prior B-8). Reproduced with the flag on: Starter 2nd county = 402.
- Record types: same function, `RECORD_TYPES_BY_PLAN` `src/config/constants.py`, fails closed to Starter on an unknown plan (`entitlements.py:279`).
- Monthly records: API preflight `quota_block_reason` (`src/api/quota.py:130`) at `jobs.py:269` and `batches.py:275`; authoritative atomic reservation in the worker (`workers/tasks.py:1760-1806`, `FOR UPDATE` on the user row); over-allowance rows marked `over_quota` and excluded from every read path via `actionable_condition` (`lead_actionability.py:75-89`, used by `/results`, `/download`, segments, batch export, analytics). Gap: the shared cache endpoint, AZ-1.
- Skip-trace entitlement: save-time `scrapers.py:220-231`, `batches.py:228-232`; run-time `tasks_helpers/enrich.py:2280-2292` (skip_trace_enabled column + plan not Starter). Trial accounts are `plan="pro"` (`registration.py:188`) and pass. AZ-2.
- Agency features: unlimited counties (`COUNTY_LIMIT_BY_PLAN` -1), batch cap 250 (`BATCH_MAX_COMBINATIONS`), AI jobs unlimited (`settings.py:468-473`), bundled skip traces 2000; all server constants keyed on `users.plan`.
- API access: every API-key request re-checks Business/Agency (`auth.py:330-335`); minting needs password + JWT session (`routes/auth.py:497-503`).
- Webhooks and dialer: save-time enable-delta gates `scrapers.py:730-776`; delivery-time re-check `tasks.py:2461-2463`; scheduled dialer sweep `scheduler_helpers/dialer.py:215`. Replay route does not re-check, AZ-4.
- Priority queue: `scrape_queue_for_plan` (`constants.py`) at every enqueue site (`jobs.py:302`, `dispatch.py:252-257`, `batch_tasks.py:119-122`), read from the stored plan.
- Date range: there is no plan-based date-range entitlement in the backend or the marketing matrix (FE `app/(marketing)/_monopo/data.ts:198-224` lists no lookback line). Starter 7-day freshness delay is enforced in the worker only (`workers/tasks_helpers/dates.py:116-121`, custom range clamped `:149-151`); the cache endpoint does not apply it (AZ-1). Custom range has no maximum span beyond the per-connector `max_date_range_days` trim (`tasks.py:609-621`), prior B-8, still open, P3.

Request-body tampering (negative controls):
- No Pydantic model in `src/api/schemas.py` sets `extra="allow"` (`cmd:grep -rn "extra=\"allow\"" src` = 0 hits). Default is `ignore`; sensitive write models use `extra="forbid"` (`schemas.py:287,311,385,395,621,816,849,873`).
- No handler copies a request body onto an ORM object generically (`cmd:grep -rn "setattr(" src/api` = 0 hits). PATCH /scrapers writes an explicit field list (`scrapers.py:821-828`); PUT /auth/profile writes only first/last name (`routes/auth.py:279-280`).
- Every writer of `users.plan`, `records_limit`, `records_used`, `is_admin`, `stripe_customer_id`, `skip_trace_used_this_month` is server-side: Stripe-webhook lifecycle functions (`src/api/billing_entitlement.py:167,291,307,330`), registration (`registration.py:188-190`), workers/scheduler (`scheduler_helpers/billing.py:244,293`, `tasks.py:1803,2313`), checkout customer binding (`billing.py:1174,2009`). `cmd:grep -rnE "\.plan\s*=[^=]|records_limit\s*=|is_admin\s*=|records_used\s*=" src`. No request field reaches them. `is_admin` has no writer in `src` at all.
- Checkout and change-plan accept only a price/product id that maps to a currently sold price (`billing.py:1029,1438`); the plan is derived from the Stripe subscription's price in the webhook (`billing.py:1948-1953`), and `metadata.user_id` must match the Stripe customer already bound to the user (`billing.py:2000-2009`).
- `JobCreate.trigger` is allowlisted to manual/test (`schemas.py:1072-1077`); `scheduled` cannot be forged.

## Quota

- Record-count cap: preflight is a plain read (`jobs.py:269`), so two concurrent POST /jobs can both pass; this is safe because the charge is a single atomic statement with `FOR UPDATE` on the user row that grants only `LEAST(want, limit - used)` (`workers/tasks.py:1784-1806`), and each job's grant is CAS-claimed once (`tasks.py:1761-1767`). Negative control: the reproduced `over` user (50/50) got 402 on POST /jobs.
- Frozen / ended entitlement: `quota_block_reason` checks `is_frozen` then `entitlement_ends_at` then usage (`quota.py:130-170`). Reproduced: a past_due user past grace got 402 on POST /jobs. Not applied on `/scrapers/{id}/records` (AZ-1).
- County count race: closed by the advisory lock when enforcement is on (`entitlements.py:365-369`), shared by scraper create and batch create. Soft-delete frees the slot (counts active configs only, `entitlements.py:330`), so a Starter can rotate counties; harmless for scraping (each job is quota-charged), but combined with AZ-1 it lets a free account browse every county's cache (reproduced).
- Skip-trace count: counter advanced under the caller's row lock in the ingest transaction (`src/api/billing/skip_trace_usage.py:41-176`, row lock at `:99`); no per-account hard cap, only a global daily row cap (`workers/skip_trace_dispatcher.py:72-113`). Trial exposure AZ-2.
- AI-connector monthly job limit: count-then-insert with no lock (`jobs.py:217-257`), AZ-6.
- Plan identifier tampering: `plan` is never read from a request; all gates call `normalize_plan(user.plan)` on the DB row (`constants.py` normalize_plan), fail closed on unknown values. The API-key path re-reads the plan per request.
- Checkout/plan-change race: per-user advisory lock 4243 on both routes (`billing.py:1055,1460`).

Prior items in this area re-verified on `786efcf0`:
- Prior N-03 / B-2 (rows readable before the plan cap, and after cancel): FIXED. `_run_delivered` gate at `jobs.py:79-87`, applied at `jobs.py:487`, `:1209`, `:1431`; batch export `workers/batch_export.py:89-90`; segments `segments.py:221`.
- Prior C-1 (anonymous `include_all`): FIXED, `scrapers.py:399-400` (reproduced 404 for a non-admin). Residual: API key passes, AZ-3.
- Prior F-12 / B-3 (trial accounts can buy unbillable lookups): OPEN, carried as AZ-2.
- Prior B-8 (`ENTITLEMENT_ENFORCEMENT` default False; custom range no max span): OPEN as code defaults; production value documented as true, not independently verified.
- Prior FE report row "Frozen / over-quota hides rows ... not re-verified": now REPRODUCED as a backend gap, AZ-1.

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| AZ-1 | P3 | Plan entitlement / quota bypass | GET /scrapers/{config_id}/records | src/api/routes/scrapers.py:1147-1301 serves shared county_records by county with only rate limit + config ownership; no quota_block_reason, no is_frozen, no entitlement_ends_at, no Starter 7-day delay (compare src/workers/tasks_helpers/dates.py:116-121). cmd:python repro.py: Starter at 50/50 got 402 on POST /jobs and 200 with same-day rows on /records; past_due-frozen user same. cmd:python repro2.py: Starter deletes config A and creates county B, reads B cache. FE only shows a banner app/(dashboard)/scrapers/[id]/records/page.tsx:76-93 | Any registered account (free Starter after trial) with one active config | Unmetered, same-day read of the shared county cache for any county (one at a time) by over-quota, frozen, cancelled and free accounts. Production impact is currently small: docs/BUILD_JOURNAL.md:3860 says ENABLE_DAILY_SCRAPE is off and the cache is a frozen March set (~3,305 rows, mostly doc_type NULL which the typed filter drops). Becomes P2 or higher the day ENABLE_DAILY_SCRAPE (src/config/settings.py:394) is turned on | Apply quota_block_reason (or at least is_frozen + entitlement_ends_at) and the Starter freshness edge to this route, count served rows against records_used or cap pages, or retire the route (the dashboard no longer links to it per BUILD_JOURNAL 4621) | Test: over-limit, frozen and ended-entitlement users get 402 on /scrapers/{id}/records; Starter never sees rows with scraped_at inside the 7-day window | REPRODUCED |
| AZ-2 | P1 | Quota / billing (skip trace) | Worker skip-trace enqueue for trial accounts | src/api/routes/auth_helpers/registration.py:188 trial = plan pro; src/workers/tasks_helpers/enrich.py:2284-2292 blocks only starter; src/api/billing/skip_trace_usage.py:290-352 holds trial usage as unbillable; only a global cap src/workers/skip_trace_dispatcher.py:72-113, no per-account cap | New signup (7-day Pro trial), skip_trace_enabled config | Each trial account can trigger paid Tracerfy lookups up to its record quota that can never be billed, and can consume the shared daily cap for paying customers. Carried from prior F-12/B-3 (consolidated P1 in audit 2); re-verified unchanged | Refuse skip trace for trialing accounts without an active paid subscription, or impose a small per-account trial lookup budget, plus a per-account daily cap | Test: trial user with skip_trace_enabled queues 0 pending_skip_trace_rows (or at most the trial budget) | CONFIRMED |
| AZ-3 | P3 | Admin gate | require_admin accepts API keys | src/api/auth.py:477-494 checks is_admin and mfa_enabled but not ctx.auth_method; used by src/api/routes/billing.py:121 and src/api/routes/scrapers.py:400. cmd:python repro.py: admin bl_ key got 200 on /billing/activation-funnel and on /scrapers/connectors?include_all=true (POST /scrapers/connectors correctly 403) | Admin account on Business/Agency that has minted an API key, and that key leaks | A leaked long-lived key with no MFA signal reads admin analytics (aggregate signup/paid counts) and the full connector health list. Read-only, aggregate | Require auth_method == jwt in require_admin (keys never carry admin), or refuse API keys for is_admin users | Test: admin API key gets 403/404 on both require_admin routes | REPRODUCED |
| AZ-4 | P3 | Plan entitlement | POST /scrapers/{config_id}/jobs/{job_id}/dialer-replay | src/api/routes/scrapers.py:1304-1355 has no plan check and enqueues process_dialer_outbox; src/workers/dialer_outbox.py:64-160 drains without a plan check, while the scheduled sweep re-checks src/workers/scheduler_helpers/dialer.py:215 | Account downgraded below Business (or frozen) with failed dialer rows on an old job | Re-pushes already-delivered lead PII to the owner's own dialer after the Business entitlement ended. Own data, own destination; low value | Re-check BUSINESS_FEATURES_PLANS (and is_frozen) in the route and in process_dialer_outbox | Test: Starter replay returns 402 and no outbox task is enqueued | CONFIRMED |
| AZ-5 | P3 | Session revocation on a side route | GET /jobs/{job_id}/download with a bearer JWT | src/api/routes/jobs.py:1383-1391 checks jti blacklist and logout-all but not TokenBlacklist.is_family_revoked, which src/api/auth.py:383 applies on every other route | Attacker holds an access token whose session family was burned by refresh-token replay (not by logout, which also blacklists the jti) | That access token can still download the owner's job CSVs for up to its 1 h lifetime | Call is_family_revoked(payload.get("fam")) in the JWT branch, or reuse get_auth_context for the header path | Test: revoke a family, then bearer download with its access token returns 401 | CONFIRMED |
| AZ-6 | P3 | Quota race | AI-connector monthly job limit | src/api/routes/jobs.py:217-257 counts this month's AI jobs then inserts at jobs.py:276-295 with no lock | Account near its AI_JOB_LIMITS (src/config/settings.py:468-473), concurrent POST /jobs; only for scraper_mode ai connectors | A few AI scrapes over the plan's monthly AI count; each is still record-quota capped by the atomic worker reservation | Take the per-user advisory lock (4242) around the count and insert | Test: N concurrent POST /jobs at limit-1 create exactly 1 job | CONFIRMED |
| AZ-7 | INFO | Client-only gate mismatch | FE admin nav and Counties page | FE lib/nav.ts:28,35 and app/(dashboard)/admin/connectors/page.tsx:49 gate on plan agency, not is_admin; backend gates on is_admin (src/api/auth.py:481). cmd:python repro.py: Agency non-admin gets 404 on all three admin calls | Agency customer | Agency customers see an Admin section whose calls 404. No server exposure | Gate the FE on session.user.is_admin as the funnel page already does | FE check only | REPRODUCED |

## Not verified

- Production values of `ENTITLEMENT_ENFORCEMENT`, `ENABLE_DAILY_SCRAPE`, `SKIP_TRACE_ENABLED`, `SKIP_TRACE_DAILY_ROW_CAP`, `EMAIL_VERIFICATION_ENABLED`: no production env access. I relied on `docs/ENTITLEMENT-AUDIT-2026-09-08.md:12` and `docs/BUILD_JOURNAL.md:2282,3860` for the first two; AZ-1 severity depends on `ENABLE_DAILY_SCRAPE` and the current `county_records` volume (the owner can check with `SELECT county, count(*) FROM county_records GROUP BY 1`).
- Live probes: two unauthenticated GETs to `api.bridgeleads.io` (`/scrapers/connectors?include_all=true`, `/billing/activation-funnel`) were refused by this session's permission policy, so admin-gate behavior is shown in-process only, not against production.
- Live two-account IDOR: owned by another leaf; my tenant evidence is code tracing plus the in-process run.
- Admin with `mfa_enabled=false` receiving `admin_mfa_enrollment_required`: code read only (`auth.py:487-493`), not executed.
- Worker-side behavior of AZ-2 (actual Tracerfy submission and held-usage accounting) was not executed: that would require Tracerfy/Stripe calls, which the brief forbids. Traced in code only.
- Scheduled dispatch for a downgraded account (frequency and batch plan not re-checked at fire time) is documented grandfathering (`scrapers.py:194-199`); I did not rate it and did not execute the beat.
