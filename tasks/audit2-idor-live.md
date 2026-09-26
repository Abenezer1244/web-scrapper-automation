# Two-account cross-tenant probe (live, isolated) - 2026-09-25

Real app (`main.app`, in-process via httpx ASGITransport), real Postgres 16 DB `bridgeleads_secaudit2_test`
(migrated to head 101), Redis db 11. Connected as the DB OWNER role, i.e. RLS is BYPASSED, so this
proves the application-layer `user_id` predicate (the "suspenders") on its own. RLS itself is covered by
tests/test_rls_isolation.py. Two accounts, agency plan (so plan gates cannot mask an authz result),
each seeded with batch, config, job, batch run, result (party_name "SECRET OWNER A/B"), job log, notification.
Harness: scratchpad `idor_harness.py`, `idor_harness2.py`.

| Method | Route | Owner (control) | A -> B's id | Anon |
|---|---|---|---|---|
| GET | /jobs/{id} | 200 | 404 | 401 |
| GET | /jobs/{id}/results | 200 | 404 | 401 |
| GET | /jobs/{id}/logs (Live Run log) | 200 | 404 | 401 |
| GET | /jobs/{id}/download | 404 (no real R2 object; control n/a) | 404 | 401 |
| GET | /jobs/{id}/export-url | 200 | 404 | 401 |
| DELETE | /jobs/{id} (cancel) | not run | 404 | 401 |
| GET | /scrapers/{id} | 200 | 404 | 401 |
| PATCH | /scrapers/{id} (valid body incl. updated_at) | not run | 404 | 401 |
| PUT | /scrapers/{id}/csv-layout (valid body) | not run | 404 | 401 |
| GET | /scrapers/{id}/records | 200 | 404 | 401 |
| POST | /scrapers/{cfg}/jobs/{job}/dialer-replay | 200 | 404 | 401 |
| DELETE | /scrapers/{id} | not run | 404 | 401 |
| GET | /batches/{id} | 200 | 404 | 401 |
| GET | /batches/{id}/download | 200 | 404 | 401 |
| GET | /batches/{id}/leads | 200 | 404 | 401 |
| GET | /batches/{id}/runs | 200 | 404 | 401 |
| GET | /batches/{id}/runs/{run}/download | 200 | 404 | 401 |
| GET | /batches/{id}/runs/{run}/leads | 200 | 404 | 401 |
| PATCH | /notifications/{id}/read | not run | 404 | 401 |

After every foreign PATCH/PUT/DELETE, B's config and job were re-read from the DB: **unchanged** (name "cfg B", no csv_layout, job present).

List endpoints as A (`/jobs`, `/scrapers`, `/batches`, `/notifications`, `/analytics/summary`, `/billing/usage`,
`/billing/skip-trace-usage`, `/billing/subscription`, `/auth/me`): all 200, **none contained any B id or B data**.

Normal user -> admin: `POST /scrapers/connectors` 404, `GET /billing/activation-funnel` 404.
`GET /scrapers/connectors` is 200 for any user by design (county catalog) and returns connector internals
(`base_url`, `gis_endpoint`, `assessor_url`, `scraper_mode`, `render_mode`, `health_status`, doc-type method/confidence).
These are public county URLs, not tenant data; see finding list for the P3 data-minimisation note.

Segments (`/segments/union`, `/intersection`, `/union/export`): take `record_types`, not ids. 200 with 0 rows for
A and no B data, but the positive control was also empty (union reads `property_list_membership`, not seeded), so
this is **static evidence only**: SQL pins `r.user_id`, `j.user_id`, `sc.user_id` = :uid (segments.py:221-231),
plus structural tests test_segments_union.py:78 / test_segments_intersection.py:85. A behavioural two-tenant
segments test does not exist (coverage gap).

**Result: 0 cross-tenant reads, 0 cross-tenant writes, 0 unauthenticated accesses across 20 id-bearing routes.**
