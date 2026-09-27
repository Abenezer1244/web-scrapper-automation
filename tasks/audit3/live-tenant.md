# Audit 3, leaf 1.4: live two-tenant tests (LT-)

Code under test: `git:786efcf0` (worktree `C:/Users/Windows/bl-wt-secaudit3`), the real `main.app` driven in-process
through `httpx.ASGITransport`. Nothing in the repo was edited except this report.

## Rig and method

- Database: isolated local `bl_audit3_idor_test`, created with psycopg2 against `template1`, migrated with
  `alembic upgrade head` from the worktree (head `102`). Redis db 12 only. No `.env` exists in the worktree; the env was
  set per the brief (`ENVIRONMENT=test`).
- Role: the harness connected as `bridgeleads`, which on this server is `rolsuper=true, rolbypassrls=true` and owns every
  table (`relforcerowsecurity=false`). RLS is therefore fully BYPASSED. Every result below proves the application-layer
  `user_id` predicate (the suspenders) on its own, which is the stricter test. RLS policies themselves were not exercised.
- Tokens: every account was created through the real `POST /auth/register` and signed in through the real
  `POST /auth/login`; refresh and logout went through the real `/auth/refresh` and `/auth/logout`. The admin (MFA enrolled,
  so login returns an MFA challenge) got its token from the real `create_token_pair`. Download tokens came from the real
  `mint_download_token` (the same function and 172800 s TTL the worker uses for emailed links).
- Network: the harness patched `socket.connect` and `getaddrinfo` to refuse every non-loopback destination before
  importing the app. It refused DNS for `api.stripe.com`, `services.arcgis.com`, `services2.arcgis.com` and
  `gismaps.kingcounty.gov` when the app and worker tried them, so no county, Stripe, Tracerfy or Resend traffic left the box.
- Harness (scratch, not in the repo): `.../scratchpad/lt/` `lt_common.py` (guards, seeding), `lt_check6.py`,
  `lt_stream_export.py`, `lt_matrix.py`, `lt_dedup.py`, `lt_dedup2.py`, `lt_extra.py`, `lt_rate.py`, `lt_sample.py`;
  raw results in `check6_out.json`, `stream_export_out.json`, `matrix_out.json`, `dedup_out.json`, `extra_out.json`.
  Audit #2's `idor_harness.py` was the starting point; it hand-listed routes, skipped every mutating owner control and
  never ran B -> A. All three gaps are closed here.
- Personas: A and B on `agency` (plan gates cannot mask authz), plus starter, pro, business, trial (what register
  creates: `plan=pro`, `trial_ends_at` in the future), expired (agency, `entitlement_ends_at` 2 days ago), cancelled
  (business, `subscription_status=canceled`, `entitlement_ends_at` passed), frozen (agency, `subscription_status=unpaid`)
  and admin (`is_admin`, `mfa_enabled`).
- Seeded per tenant (markers `SECRET OWNER <T>`, `SECRET CFG <T>`, `SECRET BATCH <T>`, `SECRET LOG <T>`,
  `SECRET NOTIF <T>`, a per-tenant phone and email): a standalone scraper config with webhook URL, webhook secret and
  dialer webhook; a disposable config; a batch with a child config, a done child job and a done batch run; a done job with
  `export_key`; two running jobs; results with phone, email and `skip_trace_status=hit`; job logs; a notification; a failed
  dialer delivery; an API key (minted through the real `POST /auth/api-key` with password re-auth).
- Owner controls were checked for CONTENT, not only status: the owner's results, download, logs, batch leads, batch
  download, scraper and notifications responses each contained the owner's own marker (`extra_out.json` `owner_content`),
  so a 404 for the foreign caller is not vacuous.

## Check 6: every id-bearing route, all methods

Routes were enumerated from `main.app.routes` at runtime (walking FastAPI 0.141 `_IncludedRouter.original_router`), not
from a hand list: 71 method/path pairs, 20 with a path parameter. 19 carry a tenant id. The 20th,
`POST /webhooks/tracerfy/{provided_secret}`, carries a shared secret and is tested in the matrix. The harness fails
closed if any id route has no resource mapping (`unspecified: []`).

Order per route: A on B's ids, anonymous on B's ids, B on A's ids, then the owner (A on A's ids) LAST, so a destructive
owner control ran only after both foreign attempts. Mutating owner controls ran with valid bodies and succeeded, so a
refusal is an authz result: PATCH used the row's real `updated_at`, PUT flipped the stored layout, DELETE /jobs cancelled a
job in `scraping`, dialer-replay reset a real failed delivery.

| Method | Route | Owner A | A -> B's id | B -> A's id | Anon | Leak in body |
|---|---|---|---|---|---|---|
| DELETE | /jobs/{job_id} (cancel) | 204 | 404 | 404 | 401 | none |
| DELETE | /scrapers/{scraper_id} | 204 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id} | 200 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id}/download | 200 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id}/leads | 200 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id}/runs | 200 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id}/runs/{run_id}/download | 200 | 404 | 404 | 401 | none |
| GET | /batches/{batch_id}/runs/{run_id}/leads | 200 | 404 | 404 | 401 | none |
| GET | /jobs/{job_id} | 200 | 404 | 404 | 401 | none |
| GET | /jobs/{job_id}/download | 200 | 404 | 404 | 401 | none |
| GET | /jobs/{job_id}/export-url | 200 | 404 | 404 | 401 | none |
| GET | /jobs/{job_id}/logs | 200 | 404 | 404 | 401 | none |
| GET | /jobs/{job_id}/results | 200 | 404 | 404 | 401 | none |
| GET | /scrapers/{config_id}/records | 200 | 404 | 404 | 401 | none |
| GET | /scrapers/{scraper_id} | 200 | 404 | 404 | 401 | none |
| PATCH | /notifications/{notification_id}/read | 200 | 404 | 404 | 401 | none |
| PATCH | /scrapers/{scraper_id} (valid body with real updated_at) | 200 | 404 | 404 | 401 | none |
| POST | /scrapers/{config_id}/jobs/{job_id}/dialer-replay | 200 | 404 | 404 | 401 | none |
| PUT | /scrapers/{scraper_id}/csv-layout (valid body) | 200 | 404 | 404 | 401 | none |

State after every foreign write, re-read from the DB: B's config name, csv_layout, `updated_at`, the disposable config's
`active`, the cancellable job's status (`scraping`), the notification's `read_at` and the dialer delivery's status
(`failed`) were byte-identical to the pre-test snapshot. A's rows changed only by A's own controls (name to `PWNED NAME`,
layout to `legacy_v1`, config soft-deleted, job cancelled, notification read, dialer row back to `pending`).

Same routes with an API key (A, agency) instead of a JWT: every GET id route 200 on A's ids and 404 on B's ids, no B marker.
`/jobs/{id}/download` returns 401 to an API key on both (that route has its own token parser, see LT-1).

Body and query tampering (as A):
- `POST /jobs {"scraper_config_id": <B config>}` 404; with B's batch-child config 404; with A's config plus
  `"user_id": <B>` 201 and the stored job's owner is A (the key is ignored, `JobCreate` has no `extra=forbid`).
- `POST /scrapers` with `"user_id": <B>, "batch_id": <B batch>`: 201, stored `user_id=A`, `batch_id=NULL` (ignored, LT-11).
- `PATCH /scrapers/{A}` plus `"user_id"`: 422 `extra_forbidden`. `PUT /auth/profile` plus `user_id`, `plan` or
  `records_limit`: 422 `extra_forbidden`.
- Mixed ids: dialer-replay with A's config and B's job 404, B's config and A's job 404; A's batch with B's run
  (download and leads) 404; B's batch with A's run 404.
- `?user_id=<B>` and `?owner_id=<B>&account_id=<B>` on `/jobs`, `/scrapers`, `/batches`, `/notifications`,
  `/jobs/{A}/results`, `/analytics/summary`, `/billing/usage`, `/auth/me`: 200, no B id or marker in any body.
- Arrays of ids: no request schema accepts a list of ids (`src/api/schemas.py`; the only `*_ids` field is the response
  field `BatchRunResponse.child_job_ids` at `schemas.py:964`). Batch children are resolved server side and the combined
  SQL pins every join to `:uid` (`src/workers/batch_export.py:89-93,219-220`).
- Every GET without a path id (20 routes, from the same enumeration) called as A: no B id, email, phone or marker.

Prior audit #2 items in this area, re-measured on 786efcf0:

| Prior item | Status now | Evidence |
|---|---|---|
| Audit #2 live IDOR: 20 routes, 0 failures | Holds, extended to all 4 caller directions and real owner writes | table above |
| G-1 no behavioural two-tenant segments test | Behaviour verified live here (A sees own 2 leads, no B data); a repo test still does not exist | `lt_check6.py` tamper, `lt_dedup.py` |
| N-03 undelivered runs deliver nothing | FIXED: export-url and download on a running job return 409 | `stream_export_out.json` |
| D-1 emailed link after deactivation | FIXED: 200 while active, 401 after `is_active=false` | `jobs.py:1405-1410` |
| A-1 logout revokes the session family | FIXED on normal routes, NOT on `/jobs/{id}/download` | LT-1 |
| F-09 download/export-url unthrottled | OPEN | LT-4 |
| C-1 `connectors?include_all` admin only | FIXED: non-admin 404, admin 200 | `scrapers.py:399-400` |

## Duplicate system (New / Already delivered / Combined)

Code path (read in full for the classification and every claim write):
- The claim table is `delivered_records`, unique on `(user_id, dedup_hash)` (`src/db/models.py:1064-1067`).
- The worker claims with `INSERT ... ON CONFLICT (user_id, dedup_hash) DO NOTHING RETURNING` (`src/workers/tasks.py:1195-1210`),
  re-reads its own claims by `first_job_id AND user_id` (`tasks.py:1223-1229`) and stamps `prior_run` plus
  `duplicate_source_job_id` through a `LEFT JOIN delivered_records dr ON dr.user_id = :uid` (`tasks.py:1262-1273`).
- Every other claim writer or reader is pinned to the same user: release paths (`tasks.py:168-172,332-336,1490-1494,1908-1912,2066-2070`,
  `tasks_helpers/status.py:555-560` joins `j.user_id = d.user_id`), transfer and election (`tasks_helpers/dedup.py:946-1123`),
  same-run collapse (`trustee_sale_finalize.py:168-197`), enrichment and contact reuse (`tasks_helpers/enrich.py:378-392` and the
  `later_sql` block, both `ro.user_id = :uid AND dr.user_id = :uid`).
- The shared caches: `skip_trace_cache` has no `user_id` column, but its primary key is a SHA-256 over
  `(version, user_id, address, city, state, trace_type, names)` (`src/scrapers/enrichment/skip_trace.py:230-252`), and the
  runtime reads only that key (`tasks_helpers/enrich.py:2625-2626`, `skip_trace_dispatcher.py:681-692`,
  `tracerfy_ingest.py:710-712`). `county_records` and `nts_notices` are shared public-record caches with no tenant
  enrichment written back: no worker path writes `county_records` except `daily_scrape.py:142`, and after the live runs
  below `county_records` held 0 rows and `skip_trace_cache` 0 rows.
- Read side: `category_condition` / `already_delivered_condition` (`src/api/results_category.py`) only filter the caller's
  own rows (`Result.user_id == current_user.id`, `jobs.py:482-486`). Row-level provenance is re-checked against
  `Job.user_id == user_id` and nulled when not owned (`jobs.py:924-973`).

Verdict: dedup is LEAD-scoped within ONE account (tenant-scoped, account-wide across all of that account's scrapers,
counties and record types). It is not global and not delivery-scoped.

Live, with the REAL worker (`run_scrape_job.apply`) on the DB-backed `trustee_sale` connector (it reads the shared
`nts_notices` cache, so the scrape itself needs no county traffic). The only substitution was `DataExporter.upload_to_r2`
writing to a local folder instead of Cloudflare; dedup, claim, classification, finalize and results code ran unmodified.
Two parcels, same parcel and address strings for both tenants:

| Step | Rows | is_duplicate | reason | source job |
|---|---|---|---|---|
| B1: B scrapes first (done), then B's rows get a phone, email and mailing address | 2 | false | none | none |
| A1: A scrapes the SAME parcels (done) | 2 | false | none | none |
| A2: A again | 2 | true | prior_run | A1 (A's own) |
| B2: B again | 2 | true | prior_run | B1 (B's own) |

`delivered_records` after B1 held only B's two claims. Through the real API as A: `/jobs/{A1}/results` new total 2,
already_delivered 0, `duplicate_sources` empty, no B phone, email, mailing address, user id or job id; `/jobs/{A2}/results`
already_delivered 2 with `duplicate_sources` naming only A1; both CSV variants, `/segments/union`, `/segments/union/export`,
`/jobs`, `/notifications`, `/analytics/summary`, `/scrapers/{A cfg}/records`, `/billing/usage` and A1's log replay: no B
marker (`dedup_out.json`).

The owner's report ("an account shown previously delivered records it should not own") did NOT reproduce across tenants.
It did reproduce inside ONE account, from a different cause: the billing dedup key is `sha256(parcel|address)` with no
county or state (`src/workers/property_identity.py:61-73`), computed at insert time before enrichment
(`tasks.py:1103`), with a `NAME|DATE` fallback that is also unscoped (`tasks.py:1001-1008`). Live (`lt_dedup2.py`): one
account ran Pierce, then King trustee sales; each county had one different property sharing the parcel number
`7705305645` and no situs address at scrape time. The King lead `KING OWNER TWO`, never delivered, was stamped
`prior_run` citing the Pierce run; after its address was filled (as enrichment does in production) the King results page
listed it under Already delivered with `duplicate_sources` pointing at the Pierce run, and its New view was 0. The
overlap key (`property_key`) differed for the two rows, so only the billing key collides. See LT-6.

Defense-in-depth gap found while testing this: the grouped `duplicate_sources` summary echoes the stamped source job id
even when that job is not the caller's (`jobs.py:745-753`), while the per-row field is nulled (`jobs.py:972-973`). With a
row seeded to name B's job, A's already_delivered page returned B's job id with `job_available=false`. No current code
path stamps a foreign id (all stamping writes above are user-pinned), so this is LT-5, P3.

## Scraper job

- Start: `POST /jobs` with B's config (or B's batch child) as A: 404 (`jobs.py:334-343`, requires `user_id` and `active`).
  Owner control 201. A cannot start a job on B's scraper config.
- View, results, cancel, download, export-url, logs with B's job id: 404 for A, 401 anonymous (Check 6). B's cancellable
  job stayed `scraping` after A's cancel; the cancel is one `UPDATE ... WHERE id AND user_id AND status IN (...)` (`jobs.py:381-390`).
- Retry/replay: there is no retry route in the enumerated 71. The only replay, dialer-replay, requires job id, `user_id`
  and config id to match (`scrapers.py:1327-1335`); both mixed-owner combinations 404.
- Worker side: the Celery task resolves the owner from the job row and then runs in `rls_sync_session(owner)`, but loads
  the job's `ScraperConfig` by id alone (`tasks.py:466-468`, prior T-1, still OPEN, code read). With RLS forced for the
  worker role that read is bounded by RLS; on this rig (bypass) only the API-side ownership check at `jobs.py:334-343`
  prevents a cross-tenant config id from reaching the queue, and it held.

## Live stream

`GET /jobs/{id}/logs` (`jobs.py:976-1123`), tested live with Redis pub/sub on db 12:
- Ownership at connect: `select(Job).where(id, user_id)` before any lease or subscription (`jobs.py:991-997`). A on B's
  RUNNING job: 404. B reconnecting with A's job id: 404. Anonymous: 401.
- Isolation: while A streamed its own running job, a line was published on B's channel `job_logs:{B job}`. A's stream
  contained A's stored line, A's live line and the terminal event, and did not contain B's line (channel is the job id,
  reachable only after the ownership check).
- Token handling: header only. `?token=<A JWT>` and `?access_token=<A JWT>` without a header: 401. The frontend uses the
  header (`bridgeleads-web lib/api.ts:1300-1303`).
- Connection limit: `SSE_MAX_STREAMS_PER_USER=5`. Five concurrent streams for A: 200; the sixth: 429 with `Retry-After: 59`.
  B, meanwhile, opened both a finished-job replay and a live stream: 200 and 200 (the cap is per user, `sse_leases.py`).
  A finished job replays without taking a lease (`jobs.py:1001-1013`) and is unthrottled (LT-4).
- Reconnect after logout: a new connection with the logged-out token: 401. But an ALREADY-OPEN stream kept delivering:
  a line published after `POST /auth/logout` of that session arrived on the stream (LT-3).

## Export

- Formats: the API has one download route, `/jobs/{id}/download`, which always builds CSV live from the DB
  (`jobs.py:1251-1613`); `?format=xlsx` still returns `text/csv`. XLSX/JSON exist only as the worker's R2 deliverable,
  which is never served directly. Batch CSVs are rebuilt from the DB (`batches.py:708-786`).
- 60 s in-app token (`/export-url`): claims `aud=bridgeleads-download`, `purpose=download`, bound `job_id`, TTL 60.
  Used on B's job path: 403 (`jobs.py:1376-1377`). Reused twice within 60 s: 200 both times (the docstring at
  `jobs.py:1200` says single-use; it is not, LT-10).
- Emailed 48 h token (real `mint_download_token`, TTL 172800, the worker's `_DELIVERY_TOKEN_TTL` at
  `tasks_helpers/status.py:50-57`): B's token presented by A (with A's own Authorization header) downloads B's CSV. It is
  a bearer link by design; the token wins over the header. B's token on A's job path: 403. Expired token: 401. Wrong-key
  token: 401. `alg=none` token: 401. Refresh token: 401. After B's `/auth/logout-all`: 401. After deactivation: 401.
- Revoked sessions: logout-all is honoured; single-session logout is NOT (LT-1). A session JWT is also accepted in the
  query string (LT-2).
- R2 object keys are `exports/{user_id}/{job_id}/leads.{ext}` (`tasks.py:1457`), guessable if the ids are known. They are
  not reachable: the API has no R2 credentials and streams from the DB, delivery links are app download tokens when
  `API_BASE_URL` is set (`status.py:53-70`), and a public bucket URL is refused unless `R2_ALLOW_PUBLIC_URLS` is true
  (`data_exporter.py:349-357`). The production bucket's public-access setting was not verified (see Not verified).
- Throttling: 150 sequential calls each: `/download` 150 x 200, `/export-url` 150 x 200, finished-job `/logs` 150 x 200,
  while `/results` and batch `/download` gave 60 x 200 then 429 (LT-4).

## Security test matrix

Expected vs actual, all live on the rig (`matrix_out.json`, `check6_out.json`, `stream_export_out.json`).

| Case | Probe | Expected | Actual | Result |
|---|---|---|---|---|
| anonymous -> protected | 7 list routes, 19 id routes | 401 | 401 on all | pass |
| A -> A | 19 id routes, owner with valid bodies, content checked | 2xx with own data | 200/204, own marker present | pass |
| A -> B | 19 id routes plus 7 mixed-id and body-id probes | 404, no B data, B unchanged | 404 on all, no marker, B snapshot identical | pass |
| B -> A | 19 id routes | 404 | 404 on all | pass |
| normal -> admin endpoint | activation-funnel, connectors?include_all, POST connectors (agency and starter) | 404 | 404 x 6; admin control 200, 200, 403 step-up | pass |
| starter -> pro-only | POST /batches; scraper with skip_trace_enabled | 402 | 402, 402 | pass |
| starter -> agency/business-only | segments; api-key; webhook scraper | 402/403 | 402, 403, 402 | pass |
| pro / trial -> business-only | segments; api-key; webhook scraper | 402/403 | 402, 403, 402 (trial: batch 201, skip-trace flag 201, as Pro) | pass |
| expired subscription -> paid feature | run job; batch | 402 | 402, 402 | pass |
| expired subscription -> paid feature | api-key mint; webhook scraper; skip-trace scraper; segments | refused | 201, 201, 201, 200 | FAIL (LT-9) |
| cancelled -> paid feature | run job; batch | 402 | 402, 402 | pass |
| cancelled -> paid feature | api-key; webhook scraper; skip-trace scraper; segments | refused | 201, 201, 201, 200 | FAIL (LT-9) |
| frozen (unpaid) -> paid feature | run job 402, batch 402; api-key 201, webhook scraper 201, segments 200 | refused | as listed | partial (LT-9) |
| any state -> own past exports | GET own download | 200 by design ("past exports are untouched") | 200 for all 8 personas | pass |
| tampered resource id | foreign ids on all id routes | 404 | 404 | pass |
| tampered resource id (malformed) | `1 OR 1=1`, `%00`, 300 x `x` on /jobs and /batches | 404/422 | 500 with `{detail, ref}` | FAIL (LT-8) |
| tampered account id | `user_id`/`owner_id`/`account_id` in query and body | ignored or 422 | ignored (no effect) or 422 | pass |
| tampered account id (JWT) | wrong-key and `alg=none` tokens | 401 | 401 | pass |
| tampered plan | `plan` in PUT /auth/profile, notification-preferences, register | 422 or ignored | 422, 422, register ignored (stored `pro` trial, `is_admin=false`) | pass |
| tampered quota | `records_limit`/`records_used` in PUT /auth/profile | 422 | 422 `extra_forbidden` | pass |
| tampered price | checkout and change-plan with `price_attacker_1cent`, `prod_fake`, empty | 400 before Stripe | 400 `Invalid plan` x 6 | pass |
| tampered webhook (Stripe) | bad signature, wrong key, no header | 400/422 | 400, 400, 422 | pass |
| tampered webhook (Tracerfy) | wrong secret header, no header, wrong legacy path secret | 401 | 401 x 3 | pass |
| tampered job id | B's job id on start/view/cancel/logs/results | 404 | 404 | pass |
| tampered export id | A's download token on B's job path; B's token on A's path | 403 | 403, 403 | pass |
| revoked session -> export | logged-out session token on /download | 401 | 200 with lead data | FAIL (LT-1) |

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| LT-1 | P2 | Session revocation bypass | GET /jobs/{id}/download session-JWT branch | src/api/routes/jobs.py:1383-1391 checks only jti blacklist and logout-all; get_auth_context also checks is_family_revoked (src/api/auth.py:381-384). live: login T1, refresh to T2, POST /auth/logout with T2: T1 on GET /jobs 401, T1 on /download 200 with the lead CSV (header and ?token=); logout with only the refresh token: T3 on /jobs 401, on /download 200 | A stolen or leaked access token, and a victim who signs out (single-session logout, the A-1 remedy) | For up to 1 h after issue the attacker keeps downloading any of the victim's job CSVs (names, addresses, decrypted phones and emails) after the victim logged out | Route the session-JWT branch through get_auth_context (or add is_family_revoked plus the purpose=refresh rejection); accept only purpose=download tokens in the query | After /auth/logout, the sibling rotated access token gets 401 on /jobs/{id}/download via header and via ?token= | REPRODUCED |
| LT-2 | P3 | Token handling | /jobs/{id}/download | src/api/routes/jobs.py:1289-1293 takes ?token= first and accepts any bridgeleads-api JWT there. live: ?token=<session JWT> 200. Frontend never does this (bridgeleads-web lib/api.ts:1109 uses bearerFetch) | A session JWT placed in a URL by any client | 1 h session credential lands in access logs, proxy logs and browser history | Only purpose=download tokens in the query; session JWT only in the Authorization header | ?token=<session JWT> on /download returns 401 | REPRODUCED |
| LT-3 | P3 | Session revocation | GET /jobs/{id}/logs live stream | src/api/routes/jobs.py:1039-1105 re-checks lease and job status but never the credential; _SSE_MAX_DURATION_SECONDS=1800 at jobs.py:54. live: stream opened, POST /auth/logout 204, line published afterwards delivered on the open stream; reconnect 401 | An open stream at the moment of logout | Own-tenant log lines keep flowing to a revoked session for up to 30 min | Re-check blacklist and family revocation at each lease renewal and end the stream on revocation | Logout during an open stream ends it within one heartbeat | REPRODUCED |
| LT-4 | P2 | Missing rate limit (prior F-09, still OPEN) | /jobs/{id}/download, /jobs/{id}/export-url, finished-job /jobs/{id}/logs | src/api/routes/jobs.py:1176,1251,976 have no rate_limit call; results (jobs.py:438) and batch download (batches.py:727) do. live: 150 sequential calls each: download 150x200, export-url 150x200, logs replay 150x200; results 60x200 then 429 | Any authenticated account | Each download rebuilds the full CSV from the DB; one account can drive unbounded DB and CPU load for all tenants | Add rate_limit(zone general, identifier user) to download, export-url and the replay branch of logs | 61st download in a minute returns 429 | REPRODUCED |
| LT-5 | P3 | Defense in depth, cross-tenant id echo | GET /jobs/{id}/results duplicate_sources | src/api/routes/jobs.py:745-753 returns job_id for every stamped source, linkable or not; the per-row field is nulled at jobs.py:972-973. live: A's row seeded with B's job as duplicate_source_job_id: A's page returned B's job id with job_available=false | A separate bug that stamps a foreign source id (none found: all stamping writes are user-pinned, tasks.py:1262-1273) | Discloses another tenant's job UUID, run time and duplicate count | Emit job_id only when linkable, as the row path does | A result stamped with a foreign job id never returns that id anywhere in the body | REPRODUCED |
| LT-6 | P3 | Dedup correctness (same tenant), likely source of the owner's report | Billing dedup key | src/workers/property_identity.py:61-73 (parcel plus address, no county); src/workers/tasks.py:1001-1008 NAME plus DATE fallback, also unscoped; key computed pre-enrichment at tasks.py:1103. live: one account, Pierce then King trustee sale, different properties sharing parcel 7705305645 with no situs address: King lead stamped prior_run citing the Pierce run, listed under Already delivered, New view 0 | One account working two counties (or two record types) whose rows collide on parcel-only or name-plus-date | A lead the account never received is shown as already delivered and never exported or billed; the page names an unrelated run as its source | Version the claim key with county and state (new namespace, keep old claims readable), or require property_key agreement before stamping prior_run | Two counties, same parcel number, no address: second run's row is new | REPRODUCED |
| LT-7 | P3 | Cross-tenant data to anonymous | GET /scrapers/sample and refresh_public_sample_cache | src/workers/scheduler_helpers/public_cache.py:43-76 takes the 5 newest done results of ANY tenant and publishes first name plus last initial, exact filing date, county, and property and mailing city/state/ZIP. live (rig): anonymous GET returned tenant-seeded rows as SECRET O., 09/01/2026, Pierce, TACOMA WA 98402 | None (public endpoint) | Reveals which leads paying customers pulled most recently, in a form re-identifiable against public court and recorder indexes | Build samples from the shared public caches (nts_notices or county_records) or from synthetic data, and drop the exact date | Sample payload contains no value derived from the results table | REPRODUCED |
| LT-8 | P3 | Input validation, error handling | Path ids on /jobs/* and /batches/* | Path params typed str and bound to UUID columns (for example src/api/routes/jobs.py:350-357, batches.py:509-516); notifications validate (notifications.py mark_read uuid.UUID check). live: /jobs/1 OR 1=1, /jobs/%00, 300-char id: 500 {detail, ref}; same on /results, /download, /batches/{id}/download | Any caller (anonymous gets 401 first; authenticated gets 500) | Error-rate noise and alert fatigue; no data or trace in the body | Type path ids as uuid.UUID so FastAPI returns 422 before the DB | Malformed id returns 404 or 422 on every id route | REPRODUCED |
| LT-9 | P3 | Subscription state not enforced on Business features | require_plan, overlap gate, scraper feature gates | src/api/auth.py:432-450 and src/api/routes/segments.py:66-80 check plan only; quota_block_reason (src/api/quota.py:130-170) is consulted only when starting work. live: expired (agency, entitlement ended), cancelled (business, canceled, ended) and frozen (agency, unpaid): api-key 201, webhook scraper 201, skip-trace scraper 201, segments 200; run job and batch 402 | An account whose payment failed or whose term ended but whose plan column has not been downgraded yet (frozen unpaid accounts stay that way indefinitely) | Paid-tier features (API keys, overlap lists, webhook destinations) remain usable without an active paid entitlement; scraping and billing stay blocked | Apply quota_block_reason (or is_frozen plus entitlement_ends_at) inside require_plan and the overlap gate for write and mint actions | Frozen and ended accounts get 402 on api-key mint, segments and webhook scraper create | REPRODUCED |
| LT-10 | INFO | Download link semantics | Emailed and in-app download tokens | src/api/download_tokens.py:21-37 (no single-use state); jobs.py:1200 docstring says single-use. live: 60 s token reused 200 twice; B's 48 h link opened by A 200; logout-all and deactivation revoke it (401) | Possession of the link | Any holder of an emailed link (forwarded mail, shared inbox) can download that one job's CSV for 48 h | Accept as bearer by design, or record jti on first use; fix the docstring | Second use of a 60 s token returns 401 if single-use is intended | CONFIRMED |
| LT-11 | INFO | Input validation consistency | ScraperConfigCreate, JobCreate, segment requests | src/api/schemas.py:674 and 1068 lack extra=forbid while ScraperConfigUpdate has it (schemas.py:873). live: POST /scrapers with user_id and batch_id 201, stored user A, batch_id NULL | None | No effect found (keys are dropped); a future field of the same name would silently become client-settable | Add extra=forbid to create models | POST /scrapers with an unknown key returns 422 | REPRODUCED |

## Not verified

- RLS policies (belt): the rig role is superuser with BYPASSRLS, so every result proves the app predicate only. The
  worker-side id-only reads (T-1 at `tasks.py:466-468`, T-4) are bounded in production only by RLS on the worker role,
  which was not exercised here.
- Production content of `GET /scrapers/sample`: a single unauthenticated read-only GET to check whether the production
  payload carries real tenant-derived fragments was denied by the session's permission classifier, so LT-7 is shown on the
  rig only. The code path is the same one production runs.
- The production R2 bucket's public-access setting and whether any `R2_PUBLIC_URL` is set in Railway (no production
  credentials; code only).
- Skip-trace reuse across tenants was verified by code (key composition, `skip_trace.py:230-252`) and by the rig showing
  no cache rows written; a live Tracerfy round trip was not run (external API forbidden).
- The real `refresh_public_sample_cache` beat schedule and the hourly reconciliation that closes the LT-9 window for
  ended terms were not run on a clock; LT-9 for `unpaid` does not depend on it.
- MFA step-up success path for `POST /scrapers/connectors` (needs a TOTP secret); only the refusal (403 step-up) and the
  non-admin 404 were tested.
- Two production lead sets were not compared for parcel-number collisions, so LT-6's real-world frequency is unknown;
  it is reproduced mechanically.
