# Audit 2: Egress, Export Injection, Logs, Verbose Errors

**Scope:** outbound requests (SSRF), CSV/Excel/JSON formula injection, user-visible job logs,
server log hygiene, verbose errors, scraped-content handling.
**Code:** worktree `C:/Users/Windows/bl-wt-secaudit2` at `origin/main 25a04eaf`.
**Method:** static read only. No pytest, no network calls to counties or prod, no source edits.
One offline check: the SSRF blocklist logic from `security.py:56-111` was copied into a scratch
script (not imported, so no `.env` was read) and run against encoded addresses (results in E-3).

**Verdict:** no P0 or P1. One P2 (E-1: a tenant-controlled webhook endpoint can exhaust a shared
worker). The rest are P3 hardening. Of the seven prior findings, one is FIXED (F-08 on the
`safe_http` path), four are PARTIAL and two are still OPEN.

---

## 1. Prior-finding status

| ID | Prior finding | Status | Evidence (current code) |
|---|---|---|---|
| F-02 | Push channels skip `sanitize_for_csv` | **PARTIAL (the remaining gap is a deliberate decision)** | PhoneBurner is FIXED: `dialer_connectors/phoneburner.py:81-101` runs every county-derived free-text field through `DialerConnector.spreadsheet_safe` (`dialer_connectors/base.py:73-104`), and `tests/test_phoneburner_connector.py` covers it. The generic dialer webhook and the job webhook are still unsanitized **on purpose**: `dialer_connectors/generic_webhook.py:20-43` and `webhook_delivery.py:171-184` send `party_name` and the addresses raw. Their argument is that JSON is not an injection context. That argument is weak for the product's main use: Zapier to Google Sheets "Create Row" writes values as USER_ENTERED, so a `=HYPERLINK(...)` owner name **runs as a formula** in the customer's sheet. Recommendation: add an opt-in per-destination `spreadsheet_safe` flag, and turn it on by default for Zapier/Make hosts. |
| F-03 | SSRF guard resolves DNS twice and never pins the IP | **OPEN (accepted in the code)** | Still resolves in `security.py:147-170` and then again when connecting (`webhook_delivery.py:319`, `safe_http.py:125-133`, `pacs.py:144`, `skip_trace.py:632/704`). There is now an explicit, reasoned risk-acceptance block at `webhook_delivery.py:253-279`, which depends on four invariants (blind, no redirects, re-checked right before the POST, full blocklist). Invariant 4 is weaker than the comment says (see E-3). Chromium does its own DNS lookup after the Python check in `base_scraper.py:436-456`, so the same time-of-check gap applies in the browser. |
| F-07 | Chromium runs with `--no-sandbox` | **OPEN** | `base_scraper.py:281`. Partly mitigated: the container runs as the non-root user `bridge` (`Dockerfile:49-50`). A renderer escape still runs with the worker's own UID, and that UID can read `/proc/<pid>/environ` of the Python worker, which holds DATABASE_URL, SECRET_KEY, Stripe, R2 and Tracerfy keys. |
| F-08 | No response-size cap on non-streaming fetches | **FIXED in `safe_http`; the gap remains at other call sites** | `safe_http.py:52-96` (`_read_capped`, 16 MiB, streamed; `iter_content` decompresses, so the cap counts the bytes after decompression) is used by `safe_get` / `safe_get_following` (`:125-134`, `:163-180`). Still unbounded: `webhook_delivery.py:319` (**tenant-controlled**, see E-1), `dialer_outbox.py:255` (fixed PhoneBurner host), `enrichment/pacs.py:144,159` and `templates/acclaimweb.py:1051,1071` (county PACS), `enrichment/skip_trace.py:632,704` (Tracerfy API). |
| F-22 | Tracerfy secret in the URL path reaches access logs | **PARTIAL** | A header route exists (`routes/webhooks.py:154-166`). The access-log line now scrubs the path (`main.py:131-158`). A kill switch exists, but `TRACERFY_LEGACY_PATH_ENABLED` defaults to **True** (`settings.py:345`), and Tracerfy still uses the path route. The secret is not rotated. **New leak path:** if `ingest_tracerfy_batch.delay` (`webhooks.py:144`) raises, for example because the broker is down, the global handler logs `request.url.path` at ERROR (`main.py:108-109`). The global redaction (`logger.py:31-58`) has no rule for `/webhooks/tracerfy/<secret>`, so the live secret is written to the log with a traceback. Railway's edge logs the URL whatever the app does. |
| F-23 | Log redaction misses several credential families | **OPEN** | `logger.py:31-58` has not changed. The basic-auth rule still matches only `https?://` (so `postgres://`, `redis://` and `rediss://` credentials pass through). There are no rules for `whsec_`, `re_`, `AKIA`, `ghp_`/`gho_`, `xox*`, or `X-Amz-Signature=`. Also, `_RedactionFilter` (`logger.py:81-94`) rewrites only `msg`/`args`. **Traceback text from `exc_info` is never scrubbed**, and every `_logger.exception` and every Celery task-failure traceback goes through that unscrubbed path. |
| F-27 | Access-log filter only matches `?token=` | **PARTIAL** | It now matches `token=` and the Tracerfy path segment (`main.py:118,131-135`). It is still attached only to `uvicorn.access` (`:158`) and scrubs only string `args`. There is no regression test: nothing in `tests/` references `_StripTokenFilter`, `_scrub_access_log_value` or `TRACERFY_LEGACY_PATH_ENABLED`. |

---

## 2. New findings

### E-1 [P2] Tenant webhook endpoint can exhaust or pin a shared worker (unbounded body, per-read timeout)

**Evidence.** `webhook_delivery.py:319-326` calls `_SESSION.post(...)` without `stream=True` and
with `timeout=15` (`:62`). `requests` reads the whole response into memory, and the timeout applies
to each socket read, not to the whole request. The body is then read again through `resp.text` at
`:355`, `:375`, `:382` and `:396`. The same task carries the generic dialer push, so both tenant
URLs (`webhook_url`, `dialer_webhook_url`) go through it. The task is routed to the shared
`celery` queue (`workers/__init__.py:110`), which also runs the beat-driven scheduler tasks
(dispatch, watchdog). It has no per-task time limit, so it inherits the 60-minute global limit
(`__init__.py:118-119`), and it retries 3 times.

**Prerequisites.** An authenticated tenant on a plan with webhook or dialer push enabled points the
URL at a public HTTPS host they control. The SSRF guard allows this by design.

**Impact.**
- **Memory exhaustion.** The endpoint replies 200 with a multi-GB body, gzip-compressed or chunked. The prefork child grows until the kernel or Railway OOM-kills it. If the container limit is hit, every in-flight job on that worker dies, including other tenants' scrapes (see the "concurrent deploys kill long jobs" landmine).
- **Slot pinning.** The endpoint sends one byte every 14 seconds. That holds a `celery` queue slot for up to 60 minutes per attempt, 4 attempts per job. With the default concurrency of 2, a few jobs can starve dispatch and watchdog for every tenant.

No data is exposed. This is availability only, which is why it is P2 and not P1.

**Fix.**
1. Use `stream=True` and read at most a small budget, 64 KiB or less. The simplest way is to reuse `safe_http._read_capped(resp, 64*1024)`, or to read `resp.raw.read(4096, decode_content=True)` for the excerpt and then close the response.
2. Add a total deadline: `soft_time_limit=60, time_limit=90` on `deliver_job_webhook`.
3. Route `webhook_delivery.*` to its own queue so it cannot starve the scheduler.
4. Apply the same cap to `dialer_outbox.py:255`.

**Regression test.** A test in `tests/test_webhook_ssrf.py` builds a real `requests.Response` whose
`raw` is a `BytesIO` of 20 MiB. It passes that response through the new bounded-read helper and
asserts that a `ValueError` is raised or the body is truncated at the cap, and that the task
result's `response_excerpt` is 500 characters or fewer. A second test asserts that the task
decorator has `soft_time_limit <= 120`.

### E-2 [P3] Webhook catch-hook URL (a capability secret) leaks into worker logs through exception text

**Evidence.** The code says the URL itself is secret: see `generic_webhook.py:28-30` ("a
user-provided catch-hook secret") and the "never log the full URL" rule at `webhook_delivery.py:249`.
On a network error, however, `webhook_delivery.py:326-330` logs `str(exc)[:200]`. For
`requests.ConnectionError` and `ConnectTimeout`, that string is
`HTTPSConnectionPool(host='hooks.zapier.com', port=443): Max retries exceeded with url: /hooks/catch/<acct>/<hook>/?...`.
The path and the query both fit inside 200 characters. The exception is then re-raised into
`autoretry_for`, and Celery's retry log line (`Retry in Ns: ConnectionError(...)`) prints it again
in full. The URL is also a plain Celery task argument (`deliver_job_webhook(job_id, webhook_url, payload)`),
so it sits in the Redis broker, while the dialer path deliberately re-reads its secrets from the DB.

**Prerequisites.** Read access to Railway logs, which means ops staff or a log-pipeline compromise.

**Impact.** Anyone who reads the logs can POST forged leads into a customer's Zap or CRM.

**Fix.** Follow `dialer_outbox.py:263-266`: log `type(exc).__name__` and the host only. Retry with
a sanitized exception, `raise self.retry(exc=Exception(type(exc).__name__))`, instead of re-raising
the original. Optionally, look the URL up from the DB by `scraper_config_id` at send time.

**Regression test.** Point the task at an unresolvable `.invalid` host with a distinctive path
token. Capture the logs with `caplog` and assert that the token appears in no record.

### E-3 [P3] SSRF blocklist misses IPv6 transition prefixes that embed IPv4

**Evidence.** `security.py:56-83` plus `_ip_is_blocked` (`:98-108`) unwrap only `::ffff:0:0/96`.
An offline run of the same logic gives:

| Address | Result |
|---|---|
| `::ffff:169.254.169.254` | blocked |
| `64:ff9b::a9fe:a9fe` (NAT64 form of 169.254.169.254) | **allowed** |
| `2002:a9fe:a9fe::1` (6to4) | **allowed** |
| `::a9fe:a9fe` (IPv4-compatible) | **allowed** |
| `2001:0:...` (Teredo) | **allowed** |
| `fec0::1` (site-local) | **allowed** |
| `192.88.99.1` (6to4 relay) | **allowed** |

This contradicts invariant 4 of the F-03 risk acceptance ("including IPv4-mapped forms").

**Prerequisites.** The worker's egress network routes NAT64 or 6to4, and an attacker controls DNS
for a webhook host (an AAAA record) or for a public host that a scraper fetches. Railway's egress
behaviour here is unverified.

**Impact.** A blind POST or GET to metadata or internal IPv4 services by way of IPv6 translation.

**Fix.** Block `not addr.is_global`, and first extract any embedded IPv4 and re-check it:
`ipv4_mapped`, `sixtofour`, `teredo[1]`, the last 32 bits for `64:ff9b::/96` and
`64:ff9b:1::/48`, and `::/96`. Also add `fec0::/10`, `2001::/32` and `192.88.99.0/24` explicitly.

**Regression test.** Parametrize `tests/test_ssrf*` over the six addresses above and assert that
`_ip_is_blocked` returns True for each.

Non-issue, verified: decimal, octal and hex hosts (`2130706433`, `0177.0.0.1`, `0x7f000001`,
`127.1`) are not parsed as IP literals by `ipaddress`. They fall through to `getaddrinfo`, which
resolves them to 127.0.0.1, and the `resolve=True` check at `:162-170` then blocks them. Every
egress caller passes `resolve=True`.

### E-4 [P3] Browser egress guard gaps: WebSocket, service workers, fail-open

**Evidence.**
- `base_scraper.py:436-438` returns True for any scheme other than http(s). `context.route("**/*")` (`:347`) does not intercept WebSocket handshakes at all, and `route_web_socket` (available in the pinned Playwright 1.62) is not used.
- The context is created without `service_workers="block"` (`:337-341`).
- The guard **fails open** on any unexpected exception (`:458-460`).
- Chromium resolves DNS on its own after the Python check (F-03 in the browser).

**Prerequisites.** An attacker controls HTML or JS served by an allowlisted county portal, or a
third-party script that portal loads. The same precondition applies to F-07.

**Impact.** Blind requests from inside the worker network to internal or metadata addresses.
Combined with F-07, a real escalation path.

**Fix.**
- `new_context(service_workers="block")`.
- Add a `context.route_web_socket("**/*", ...)` handler that applies the same validation, or blocks outright (no county flow needs WebSockets).
- Fail closed (`return False`) on unexpected exceptions and log at ERROR.
- Fix F-07.

**Regression test.** Assert that the launched context was created with `service_workers == "block"`.
Unit-test `_ssrf_nav_allowed` with a request stub that raises from `.url` and assert False (use a
minimal real object, not a mock library).

### E-5 [P3] Vendor and tenant response bodies written to logs and the result backend unsanitized

**Evidence.**
- `skip_trace.py:651` builds `TracerfyError(f"... {resp.text[:500]}")`. The dispatcher logs it (`skip_trace_dispatcher.py:364`) and appends it to the task's `errors` return, which is stored in the Celery result backend. Tracerfy error bodies can echo the submitted name and address rows.
- For non-dialer webhooks, `webhook_delivery.py:355,375,396` logs and returns 500 characters of a **tenant-controlled** body without `clean_text`. That allows log forging (embedded newlines, ANSI codes) in Railway logs.

**Fix.** Pass these through `clean_text(...)[:200]`. For Tracerfy, keep the 402 "need N more credits" parse, which needs the body, but log only the status code and the parsed number.

**Regression test.** Feed a body of `"x\nFAKE ERROR root login"` through the excerpt helper and
assert that no newline survives.

### E-6 [P3] Default 422 handler echoes submitted input, including passwords

**Evidence.** There is no custom `RequestValidationError` handler (none in `main.py` or `src/`).
Pydantic v2's default error includes `"input"`, so a password longer than the 72-character
`max_length` (`schemas.py:152,156,212`) comes back to the client in plaintext JSON. Only the
submitter sees it, but browser error reporters and proxies record it.

**Fix.** Register a handler that strips `input` and `ctx` from each error.

**Regression test.** POST `/auth/register` with an 80-character password and assert that the
password string is not in the response body.

---

## 3. Explicit non-findings (checked, clean)

**Job and Live Run logs.**
- Every user-visible failure `reason` is fixed copy: `tasks.py:780-910,1320-1520,1915-2350`, the post-crash reason at `:387`, and the watchdog message at `scheduler_helpers/health.py:343`.
- The only `str(exc)` that reaches a user is `UnsupportedCountyError` (`tasks.py:572`), whose message is authored by us.
- Transient retries use `transient_retry_notice` (`tasks.py:873-881`); the exception class goes only to the engineering log.
- `report_stage` values are restricted to the `JOB_STAGES` allowlist (`base_scraper.py:213-240`).
- Email-enqueue failure shows only "Email delivery unavailable" (`tasks.py:2447`).
- Log replay is tenant-scoped through a join on `Job.user_id` (`jobs.py:1099-1112`).
- `Job.error_message` in `JobResponse` carries only those fixed strings.

**Verbose errors.**
- The global handler returns `{detail, ref}` only (`main.py:105-111`).
- No route builds `detail` from an exception, except the three admin-only (`require_admin_mfa`) connector routes (`scrapers.py:992,1013,1028`), which echo the SSRF validator's own fixed messages.
- Stripe and Tracerfy errors are never passed to clients.
- Batch download 503s are generic (`batches.py:760-765`).

**CSV, Excel and JSON injection.**
- `sanitize_for_csv` (`security.py:424-450`) handles `= + - @`, TAB, CR, LF, leading whitespace (including NBSP through `str.strip`), and wrapping quotes (`"=`, `'=`, `` `= ``). `clean_text` turns embedded TAB, CR and LF into spaces.
- Every free-text column in `build_lead_export_row` is sanitized (`lead_export.py:548-655`).
- Columns left unsanitized are numeric or date typed from the DB (`delinquent_amount` is `Numeric`, `models.py:936`), derived integers, fixed labels, or phones cut down to 10 digits (`lead_formatting.py:136-152`). So `+1 206...` becomes `2065551234`: nothing is corrupted and no formula prefix is left.
- Excel goes through the same rows (`data_exporter.py:54-86`). openpyxl would bind a string starting with `=` as a formula, but no emitted string can start with `=` any more. Header labels are fixed constants (`CRM_V1_LABELS`).
- JSON uses the same builder.
- The Lists, batch and segment CSVs go through `build_overlap_export_row`, with `lists` and `counties` also sanitized (`lead_export.py:814-815`).
- Download filenames are built from a slug regex or UUID prefixes (`schemas.py:1912`, `segments.py:721`).
- Fullwidth `＝` and a zero-width-space prefix are not formula triggers in Excel, Sheets or LibreOffice.
- Accepted trade-off: the `'` prefix appears literally in JSON and in non-spreadsheet CRMs.

**SSRF, other egress.**
- Every egress uses `trust_env=False` (`safe_http.py:29-30`, `webhook_delivery.py:68-69`, `dialer_outbox.py:40`, `pacs.py:143`, `acclaimweb.py:1044,1069`, `skip_trace.py:631,703`) and `allow_redirects=False`, or re-validates each hop (`safe_http.py:146-180,183-260`).
- Schemes are https only for webhooks (`security.py:255-272`, `schemas.py:458-473`) and http(s) elsewhere, so no `file:` or `gopher:`.
- Tracerfy result download: host pinned to the Tracerfy domain or Tracerfy's DO Spaces bucket (`tracerfy_ingest.py:418-458`), HTTPS required, every hop validated, the URL redacted out of errors (`skip_trace.py:918-960`).
- PhoneBurner: fixed host allowlist plus `validate_outbound_webhook` (`dialer_outbox.py:233-247`).
- Tenant scraper configs carry no URL. `base_url`, `gis_endpoint` and `assessor_url` are admin-only and validated (`scrapers.py:936-1030`).
- Parser differential (`https://a\@127.0.0.1`): `urlparse` and urllib3 agree on the host. Chromium canonicalizes differently, but the route guard validates Chromium's canonical `request.url`.
- A delivery-config "test webhook" endpoint does not exist.
- Non-standard ports are allowed on webhook URLs. That only lets a tenant make blind connections to public hosts, which is informational.

**Scraped content into HTML or SQL.**
- Email templates `html.escape` every interpolated value (`email_layout.py:203-437`, `ops_alerts.py:199-201`), and subjects go through `header_text`.
- The API returns JSON only, with CSP `default-src 'none'`.
- Every f-string SQL builds only placeholder lists or constants, with values bound as parameters (`tasks.py:1199`, `dedup.py:73,145`, `retention.py:109,213`, `daily_scrape.py:51` uses an integer hash).

**Server logs.**
- No code logs Authorization headers, cookies, DSNs, reset or verification tokens, presigned URLs (`data_exporter.py:383` logs only the object key), or PACS/Tracerfy tokens.
- Emails logged at INFO (`delivery.py:278,329,385`, `onboarding_emails.py:44,233`) are masked by the redaction rule to `j***@domain`.
- Delivery errors log only the type and code (`delivery.py:72-76`).

---

## 4. Suggested fix order

1. **E-1:** cap the webhook body, add a task deadline, and give webhooks their own queue.
2. **F-22:** move Tracerfy to the header route, set `TRACERFY_LEGACY_PATH_ENABLED=false`, rotate the secret, and add a global-redaction rule for `/webhooks/tracerfy/`.
3. **E-2 + F-23:** sanitize exception text in the webhook path. Extend `_SECRET_PATTERNS` (any-scheme userinfo, `whsec_`, `re_`, `AKIA`, `X-Amz-Signature`), and scrub `exc_text` in the filter by formatting the exception and then redacting it.
4. **F-07 + E-4:** re-enable the Chromium sandbox (or use seccomp), block service workers and WebSockets, and make the guard fail closed.
5. **E-3:** make the IP check `is_global`-based, with embedded-IPv4 extraction.
6. **F-02 residual:** an opt-in `spreadsheet_safe` flag for generic webhooks. E-5 and E-6 are hygiene.
