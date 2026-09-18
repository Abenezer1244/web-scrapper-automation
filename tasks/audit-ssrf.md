# SSRF / Outbound Request Security / Ingestion Trust Boundary — Audit

**Worktree:** `C:/Users/Windows/bl-wt-secaudit` @ `60f1b00` (fresh tip of origin/main)
**Scope:** SSRF guard, outbound request security, public-record ingestion trust boundary (item 50)
**Method:** READ-ONLY. No files edited in `src/`. No pytest run. No requests sent to county portals or third-party systems.
**Date:** 2026-09-16

---

## VERDICT BLOCK

| # | Question | Verdict | Evidence |
|---|---|---|---|
| 1 | DNS rebinding / TOCTOU — resolved IP pinned for the connection? | **NO — resolves TWICE** | validate: `src/api/middleware/security.py:155`; connect: `src/workers/webhook_delivery.py:291` |
| 2 | Redirects followed? Every hop re-validated? | **Not followed by default; where followed, EVERY hop re-validated** | `src/utils/safe_http.py:113`, `:170`; `allow_redirects=False` on 100% of calls |
| 3 | `::1`, `fc00::/7`, `fe80::/10`, `::ffff:127.0.0.1`, `169.254.169.254` blocked? | **YES — all five** | `security.py:61`, `:62`, `:67`, `:107-109`, `:60`+`:88` |
| 4 | Customer-configured webhook URL passes the guard before the request? | **YES — at send time** | `webhook_delivery.py:253` → POST at `:291` |
| 5 | Webhook response body surfaced to the user? | **NO — BLIND SSRF** | zero `AsyncResult` refs in `src/`; redaction `webhook_delivery.py:242-245` |
| 6 | Playwright `page.goto()` guarded, or bypasses the Python HTTP layer? | **GUARDED — at the route layer, not the Python layer** | `src/scrapers/base_scraper.py:262`, `:329-375` |

---

## P1 FINDINGS

### F1 — SSRF guard resolves DNS twice and never pins the validated IP (DNS rebinding)

**CLASSIFICATION:** CONFIRMED VULNERABILITY
**SEVERITY:** P1

**EVIDENCE**

```python
# src/api/middleware/security.py:146-170
def _assert_resolved_ips_safe(hostname: str) -> None:
    infos = socket.getaddrinfo(hostname, None)          # :155  resolution #1
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if _ip_is_blocked(addr):
            raise ValueError("Target resolves to a blocked address")
    # returns None — the vetted address is DISCARDED, never reaches the socket
```

```python
# src/workers/webhook_delivery.py:253, 291
validate_outbound_webhook(webhook_url)                                  # resolution #1
resp = _SESSION.post(webhook_url, json=payload, ..., allow_redirects=False)  # resolution #2
```

Searched the entire tree for any pinning mechanism — `HTTPAdapter`, `mount(`, `create_connection`,
`PoolManager`, Host-header rewriting. **Zero hits.** The only `getaddrinfo` call site in all of
`src/` is `security.py:155`. No custom transport adapter exists anywhere in the codebase.

Every guarded call site has the same validate-then-re-resolve shape:

| Call site | Validate | Connect |
|---|---|---|
| `src/utils/safe_http.py` | `:72` | `:75` |
| `src/utils/safe_http.py` | `:113` | `:114` |
| `src/utils/safe_http.py` | `:170` | `:171` |
| `src/workers/webhook_delivery.py` | `:253` | `:291` |
| `src/workers/dialer_outbox.py` | `:244` | `:255` |
| `src/scrapers/enrichment/pacs.py` | `:128` | `:144` |
| `src/scrapers/enrichment/skip_trace.py` | `:396` | `:450` |
| `src/scrapers/enrichment/skip_trace.py` | `:515` | `:522` |

**ATTACK PATH**

1. A Business-plan customer saves `deliver.dialer_webhook_url = https://rebind.attacker.tld/x`.
2. Save-time validation is structural only and performs no DNS — `src/api/schemas.py:458-473`,
   which states this deliberately at `:459-463`.
3. At send time `deliver_job_webhook` calls `validate_outbound_webhook` →
   `getaddrinfo("rebind.attacker.tld")` returns the attacker's public A record (TTL 0) → passes.
4. Microseconds later `requests`/urllib3 resolves the same name independently. The attacker's
   authoritative server now answers `169.254.169.254`, or an internal Railway / Postgres / Redis
   address.
5. The POST — carrying lead PII and a valid `X-BridgeLeads-Signature` — is delivered to that
   internal endpoint.

Identical primitive via `deliver.webhook_url`, and via any admin-set `gis_endpoint` /
`assessor_url`. Read-back is blind (F4 / verdict 5), so this is an internal-service write/probe
primitive rather than response exfiltration.

**AGGRAVATING — the webhook path is denylist-only.** `validate_outbound_webhook` passes
`require_allowlisted=False` (`security.py:272`). The domain allowlist is enforced ONLY for browser
main-frame documents (`base_scraper.py:358`), `navigate()` (`:400`), `safe_goto()` (`:457`) and
`probe()` (`:500`). For the customer webhook there is nothing standing behind the resolved-IP
check — it is the entire control, and it is the one that is bypassable.

**FALSE ASSURANCE IN COMMENTS AND TESTS.** Four docstrings assert the defense exists:
`safe_http.py:7-8` ("so a host that resolves to a private/loopback/metadata IP is rejected
(DNS-rebinding aware)"), `county_gis.py:350`, `pacs.py:122-123`, and `tests/test_webhook_ssrf.py:4`.
The claim is true of *hostname-vs-resolved-IP* validation and false of *rebinding*.

The test file asserts the wrong thing on exactly this point:

```python
# tests/test_webhook_ssrf.py:73-77
def test_resolution_blocks_loopback_host():
    # `localhost` resolves to a loopback address; the resolved-IP check
    # must reject it (this is the DNS-rebinding defense in action).
    with pytest.raises(ValueError, match="blocked address"):
        _assert_resolved_ips_safe("localhost")
```

This proves hostname → resolved-IP validation. It does NOT test DNS rebinding, which by definition
requires two *differing* resolutions of the same name. No test anywhere asserts the connection uses
the validated address. **The suite is green and the vulnerability is present.**

**FIX**

1. Have `validate_scraping_target` **return** the vetted `ipaddress` object instead of `None`, so a
   caller cannot forget to use it.
2. Add a `requests.adapters.HTTPAdapter` subclass that connects to that literal IP while preserving
   the original hostname for SNI, `Host:`, and certificate verification.
3. Mount it on `safe_http._SESSION` (`:29`), `webhook_delivery._SESSION` (`:68`),
   `dialer_outbox._SESSION` (`:40`), and the ad-hoc sessions at `pacs.py:137`,
   `skip_trace.py:448,520`, `acclaimweb.py:1042,1067`.
4. Correct the four docstrings and the test comment in the same change.
5. Playwright cannot be pinned this way (Chromium resolves independently) — rely on the pre-flight
   route guard plus egress network policy.

---

## P2 FINDINGS

### F2 — No response-size cap on non-streaming fetches (decompression bomb / endless stream)

**CLASSIFICATION:** PARTIAL-WEAK CONTROL
**SEVERITY:** P2

**EVIDENCE**

The module's own docstring names the gap:

```
# src/utils/safe_http.py:141-144
Unlike ``safe_get`` / ``safe_get_following`` (which materialize the whole
body in a ``requests.Response`` in RAM), this writes chunks straight to disk
```

`safe_get()` (`safe_http.py:75-82`) and `safe_get_following()` (`:114`) are non-streaming with no
cap. Only `safe_download_to_file()` enforces one — `_stream_capped()` `:212-228` plus the declared
`Content-Length` early reject `:197-201`, against `Settings.MAX_DOWNLOAD_BYTES = 104857600`
(`src/config/settings.py:291`).

Uncapped readers: `download_tracerfy_csv` → `return resp.text` (`skip_trace.py:793`, 60s timeout),
`probe()` → `len(resp.text)` (`base_scraper.py:504`), every `county_gis` /
`king_county_assessor` fetch, `pacs.py:174`, `webhook_delivery.py:327`.

**ATTACK PATH**

A hostile county portal, a compromised ArcGIS `gis_endpoint`, or a customer webhook endpoint returns
`Content-Encoding: gzip` with a multi-GB expansion. `requests` transparently decompresses on
`.text`/`.content` with no ceiling. The Celery worker OOMs mid-job (Chromium is already capped at
`--max-old-space-size=512`, `base_scraper.py:206`). Variant: a server drip-feeding one byte just
under the read timeout holds the connection indefinitely — `timeout=` in `requests` is per-read, not
total elapsed.

**FIX**

Add `stream=True` plus a `max_bytes` accumulator to `safe_get` and `safe_get_following` (reuse
`_stream_capped` against a `BytesIO`), default a few MB with explicit override. Pass a cap from
`download_tracerfy_csv`. Add a total-elapsed deadline alongside the per-read timeout.

---

### F3 — Chromium runs `--no-sandbox` while rendering attacker-controlled HTML

**CLASSIFICATION:** PARTIAL-WEAK CONTROL
**SEVERITY:** P2

**EVIDENCE**

```python
# src/scrapers/base_scraper.py:193-207
self._browser = await self._playwright.chromium.launch(
    headless=use_headless,
    args=["--no-sandbox", ...],
```

**ATTACK PATH**

County portal HTML is attacker-controlled by this project's own threat model. With the OS sandbox
disabled, a Chromium renderer vulnerability escalates directly to code execution in the worker
process — which holds `DATABASE_URL`, `SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, S3 and Stripe
credentials in its environment. There is no second boundary.

**FIX**

Run the container as a non-root user with user-namespace / seccomp support and drop `--no-sandbox`.
If the Railway runtime forbids it, document the accepted risk explicitly and compensate with strict
egress network policy (which would also blunt F1).

---

## INGESTION TRUST BOUNDARY (item 50)

### Raw SQL built from scraped text — NONE FOUND

Every f-string / `.format()` SQL site interpolates structure or constants only:

| Site | Interpolated | Why safe |
|---|---|---|
| `src/api/routes/segments.py:473,565,573,690` | `county_clause`, `pk_clause` | Fixed literal strings chosen by a boolean (`:469-470`, `:559`, `:687`); values bind via `ANY(:counties)` |
| `src/workers/tasks_helpers/dedup.py:63-66,73` | `values_sql` | Generated **placeholder names** (`:uid_0, :rt_0, …`); all values bound in `params` `:67-72` |
| `src/workers/tasks.py:1064` | same VALUES-placeholder pattern | Params bound `:1055-1061` |
| `src/api/routes/billing.py:1745` | `_WEBHOOK_LOCK_TIMEOUT` | Module constant `= "15s"` (`:1811`) |
| `src/workers/cv_owner_recovery.py:431` | `_WRITE_STATEMENT_TIMEOUT_MS` | Module constant `= 10000` (`:78`) |
| `src/workers/daily_scrape.py:51,151` | `lock_key` | `int(md5(...)[:8], 16)` (`:50`) — an int by construction |

No `execute(f"...")` anywhere in `src/`.

### Dynamic ORDER BY / column names — ALLOWLISTED BY TYPE

The only dynamic sort is `src/api/routes/jobs.py:484` → `results_order_by(record_type, sort)`
(`src/api/results_sort.py:203-218`). `sort: ResultsSort = Literal["date_desc","date_asc"]`
(`results_sort.py:39`) — Pydantic rejects anything else at the boundary, and the function branches
to SQLAlchemy column objects, never to a string. No user-controlled column or direction name reaches
SQL.

`sanitize_search()` (`security.py:477-487`) additionally escapes `\`, `%`, `_` and caps at 100 chars
for ILIKE.

### Size / record caps — PARTIAL

**Bounded.** Varchar-backed fields are truncated to column width before insert via `_trunc`
(`tasks.py:846-847`):

```python
# src/workers/tasks.py:955-962
"date_recorded":    _trunc(rec.date_recorded, 32),
"party_name":       _trunc(rec.party_name, 512),
"doc_type":         _trunc(rec.doc_type, 128),
"parcel_id":        _trunc(rec.parcel_id, 64),
"property_address": _trunc(rec.property_address, 512),
"mailing_address":  _trunc(rec.mailing_address, 512),
```

**Unbounded.**

```python
# src/workers/tasks.py:957-963
"heirs":             rec.heirs,              # no cap — Text column, models.py:798
"legal_description": rec.legal_description,  # no cap — Text column, models.py:799
"enrichment_data":   _enrichment,            # raw scraper dict — JSON column, models.py:804
```

`ScrapedRecord` (`base_scraper.py:97-128`) is a bare dataclass — no validators, no type coercion. No
cap on `page.content()` in `get_soup_async` (`base_scraper.py:481`).

**Per-scraper page caps exist** — `clark_wa.py:286` (50), `king_wa_probate.py:829` (50),
`acclaimweb.py:598` (50), `king_cv_sources/base.py:42` (1000), `seattle_sdci.py:39` (1000),
`pierce_wa_code_violation.py:175` (1000), `nts_pdf.py:29` (40 PDF pages, explicitly "cap hostile
inputs"). Inserts batch at 1000 (`tasks.py:911`). **There is no global row-count ceiling.**

A hostile source emitting multi-megabyte legal descriptions across 1000 paginated rows is unbounded
in memory and disk.

**FIX:** apply `_trunc` to `heirs` and `legal_description`; add a serialized-size ceiling on
`enrichment_data` before insert.

### Sanitization is EXPORT-TIME ONLY — the webhook path skips the chokepoint

Confirmed independently, and it corroborates the peer agent's finding.

`clean_text()` (`security.py:458-472`) runs in exactly two places: inside `sanitize_for_csv`
(`:443`) and in `audit_log` (`:580-587`). **Nothing normalizes on ingest.** Control characters,
CR/LF and ANSI escapes persist in the database as scraped.

The CSV/Excel/JSON path is well defended *because* it is the only place normalization happens —
`sanitize_for_csv` (`security.py:424-450`) runs three checks (raw value `:446`, cleaned value `:447`,
and a de-quoted probe `:444,448` that catches `"=HYPERLINK(...)` which Excel unwraps into a live
formula), and `clean_text` also replaces embedded TAB (`:471`) to close the TSV-paste split. Coverage
across all emitted fields is complete (`src/utils/lead_export.py:572-681`), and all three formats
share the builder (`src/utils/data_exporter.py:200-208`).

**The dialer / outbound-webhook path bypasses it entirely:**

```python
# src/workers/webhook_delivery.py:171-184
enriched.append({
    "external_id": f"bridgeleads:result:{rid}",
    "id": rid,
    "party_name": ld.get("party_name"),            # verbatim from DB
    "phone": ld.get("phone"),
    "phone_type": ld.get("phone_type"),
    "dnc_status": dnc_status,
    "email": ld.get("email"),
    "property_address": ld.get("property_address"), # verbatim from DB
    "mailing_address": ld.get("mailing_address"),   # verbatim from DB
    ...
})
```

Lead dict assembled at `src/workers/dialer_outbox.py:170-179` and
`src/workers/scheduler_helpers/dialer.py:210-222` — no `sanitize_for_csv` on either path.

**Net effect:** hostile county HTML → DB (no ingest normalization) → customer's CRM verbatim, in one
continuous unsanitized channel. If the receiving CRM renders or exports those fields, the formula
injection and control-character payloads that `sanitize_for_csv` exists to stop are delivered intact.

### Log injection — same root cause

Because control characters survive in the DB, `%s`-formatted log lines are forgeable:

```python
# src/scrapers/enrichment/county_gis.py:312   (also :319, :415)
_logger.info("GIS name-based fallback succeeded for %s", owner_name)
# src/scrapers/enrichment/parcel.py:39
_logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)
```

A `party_name` of `"SMITH\n2026-09-16 AUDIT event=login_success user_id=<victim> ip=..."` forges an
audit line.

**Honest reachability caveat:** I could not find a live caller. `enrich_parcel()` (`parcel.py:24`) is
unreferenced in `src/`, and the `owner_name=` path reaches `enrich_parcel_gis` only through it. Most
live worker lines log counts (`tasks_helpers/enrich.py:345,460,473,526`) and scrapers use `%r`, which
repr-escapes `\n` (`king_wa_probate.py:926,1097`; `pacs.py:176`). Latent pattern, P3 — not a
demonstrated live injection.

**FIX ONCE, AT THE SOURCE:** apply `clean_text` at `ScrapedRecord` construction or in the row build
at `tasks.py:951-981`. That closes the DB, webhook and log paths together, rather than patching three
call sites.

### Frontend (HTML/JS) — UNVERIFIED, separate repo

The API emits no HTML — zero hits for `HTMLResponse` / `PlainTextResponse` / `text/html` across
`src/`. `SecurityHeadersMiddleware` (`security.py:492-513`) sets
`CSP: default-src 'none'; frame-ancestors 'none'`, `nosniff`, `X-Frame-Options: DENY`, COOP/CORP, and
HSTS on HTTPS.

But raw scraped text does cross the boundary: `ResultResponse.enrichment_data: dict[str, Any] | None`
(`src/api/schemas.py:1259`) ships the unvalidated scraper blob to the client alongside `party_name`,
`heirs`, `legal_description` and addresses. **Recommend** a targeted grep in `bridgeleads-web` for
`dangerouslySetInnerHTML` and for any raw render of `enrichment_data`.

---

## FULL ENUMERATION — every user-influenceable URL

| # | URL | Who sets it | Guard before the request | Verdict |
|---|---|---|---|---|
| 1 | `deliver.webhook_url` (job completion) | **End user** (Business+) | `validate_outbound_webhook()` `webhook_delivery.py:253` → POST `:291` | GUARDED (TOCTOU per F1) |
| 2 | `deliver.dialer_webhook_url` (carries PII) | **End user** | `scheduler_helpers/dialer.py:249` → same task → `:253` | GUARDED (TOCTOU per F1) |
| 3 | PhoneBurner endpoint | Connector code | Hardcoded host pin `dialer_outbox.py:233` **then** `validate_outbound_webhook` `:244` | SECURE |
| 4 | `CountyConnector.base_url` | Admin (`POST /scrapers/connectors`) | `validate_scraping_target` `routes/scrapers.py:988`, then `add_scrape_domain` `:997` | GUARDED |
| 5 | `CountyConnector.gis_endpoint` | Admin | `routes/scrapers.py:1009` (`resolve=True`); fetch-time `safe_get` `county_gis.py:351,407,1099` | SECURE |
| 6 | `CountyConnector.assessor_url` | Admin | `routes/scrapers.py:1024`; fetch-time `pacs.py:128` | SECURE |
| 7 | Migration/script-seeded connector hosts | Operator | `register_connector_domains_from_db()` validates BEFORE allowlisting `security.py:373-390` | SECURE |
| 8 | Tracerfy `download_url` | Vendor webhook (shared-secret gated `routes/webhooks.py:51-71`) | `safe_get_following(require_https=True)` `skip_trace.py:737-743` — per-hop | GUARDED (no size cap, F2) |
| 9 | Scraped detail hrefs (EagleWeb etc.) | **County portal HTML** | `safe_get(same_origin_as=...)` origin pin `safe_http.py:73-74` | SECURE |
| 10 | Avatar / logo / `redirect_uri` / `next=` | — | None exist. Stripe `success_url`/`cancel_url`/`return_url` built from `settings.FRONTEND_URL` (`billing.py:1130,1131,1669`) | NOT APPLICABLE |

---

## ALLOWLIST vs DENYLIST — and post-boot connector reload

### Is the guard an allowlist? Both — and for the webhook path it is denylist-only

Mode is per-call via `require_allowlisted` (`security.py:174`, default `True`).

**Allowlist ENFORCED (strong control):**
- `navigate()` `base_scraper.py:400`, `safe_goto()` `:457` — default `True`
- `probe()` `base_scraper.py:500` — explicit `True`
- Browser main-frame documents — `base_scraper.py:358`:
  `require_allowlisted = request.resource_type == "document" and not is_subframe`

**Allowlist BYPASSED (denylist-only: blocked IP ranges + blocked hostnames + scheme):**
- **`validate_outbound_webhook` → `security.py:272`: `require_allowlisted=False`** ← customer webhooks
- `safe_http.safe_get` default `False` (`safe_http.py:55`) — all Python enrichment fetches:
  `county_gis.py:351,407,445,1099,1215`, `king_county_assessor.py:284,586,817,898`,
  `king_address_points.py:157`, `king_parcel_locate.py:112`, `king_rpacct.py:71`, `national.py:63`,
  `pierce_atip.py:279`, `pierce_legal_repair.py:243,278`
- `pacs.py:128`, `skip_trace.py:396,515`, `acclaimweb.py:1036`, `routes/scrapers.py:988,1009,1024`
- Browser sub-frames and sub-resources (XHR/JS/CSS/images) — deliberate, so reCAPTCHA iframes and
  CDN assets keep working

### Is a connector added after boot rejected until restart? NO — three refresh points

1. `main.py:34-35` — FastAPI `lifespan` (API boot)
2. `src/workers/__init__.py:146-148` — Celery `@worker_ready.connect` (worker boot)
3. **`src/workers/tasks.py:418` — refresh at the start of EVERY scrape job.** Its comment
   (`:414-417`) names this exact scenario.
4. `routes/scrapers.py:997` calls `add_scrape_domain(new_host)` inline, so the serving API process
   has it immediately.

The dynamic design is deliberate and documented at `security.py:277-290`, which explicitly warns
against a startup lock: *"The SSRF guarantee does not come from immutability."* That reasoning is
sound.

### F9 (P3) — but the refresh only ADDS; deactivation never revokes

```python
# src/api/middleware/security.py:292-298
if domain in _ALLOWED_SCRAPE_DOMAINS:
    return
_ALLOWED_SCRAPE_DOMAINS = _ALLOWED_SCRAPE_DOMAINS | {domain}   # union only
```

`register_connector_domains_from_db()` filters to `CountyConnector.active` (`:349`) but has no
removal pass; same for `_HTTP_ALLOWED_DOMAINS` via `add_http_allowed_host` (`:313-319`).

**Attack path:** a county portal is retired, taken over, or found compromised; an admin deactivates
the connector. Every running API and worker process keeps the host allowlisted — including on the
narrow HTTP-plaintext opt-in — until that process restarts. Partially bounded for workers by
`worker_max_tasks_per_child=25` (`workers/__init__.py:134`); the API process has no such bound.

**FIX:** rebuild the dynamic portion of the set from the DB (preserving the hardcoded seed at
`security.py:20-30`) instead of unioning into it, so deactivation propagates at the next refresh —
which for workers is every scrape job.

---

## P3 FINDINGS (summary)

| ID | Finding | Evidence |
|---|---|---|
| F4 | 500-byte response excerpt of a possibly-internal endpoint persisted to Redis + worker logs (operator-visible only; keeps SSRF blind rather than full-read) | `webhook_delivery.py:325-328, 347, 368`; redaction only for dialer events `:242-245` |
| F5 | `heirs` / `legal_description` / `enrichment_data` written unbounded from scraped input | `tasks.py:957-963`; `models.py:798-799, 804` |
| F6 | Browser route guard allows non-HTTP(S) schemes incl. `file://` — `return True` on non-http scheme. No reachable read found (Chromium scheme policy + no `--allow-file-access-from-files`), but the guard is not what stops it | `base_scraper.py:351-353` |
| F7 | `add_scrape_domain()` called with NO prior validation — contradicts its own contract at `security.py:288-290`. Not currently exploitable (sole caller `parcel.py:78` never passes `assessor_url`; `enrich_parcel()` is unreferenced), but a latent allowlist-widening primitive | `ai_assessor.py:92-97` |
| F8 | No ingest-time `clean_text`; `%s`-formatted scraped values in log lines (no live caller found) | `county_gis.py:312,319,415`; `parcel.py:39` |
| F9 | Allowlist refresh never revokes a deactivated connector's host | `security.py:292-298, 349` |
| F10 | SSRF route guard fails OPEN on non-`ValueError` exceptions | `base_scraper.py:373-375` |

---

## CONFIRMED SECURE CONTROLS

| ID | Control | Evidence |
|---|---|---|
| F11 | Blocked-range coverage: RFC1918, loopback, link-local v4+v6, CGNAT, metadata, TEST-NETs, multicast, reserved; IPv4-mapped IPv6 renormalized; IDNA-uncanonicalizable hosts fail CLOSED; IPv6 zone-ids stripped | `security.py:55-82, 84-98, 101-110, 113-132, 135-143, 218` |
| F12 | Resolved-IP validation, rejecting if ANY returned A/AAAA is blocked; fails closed on NXDOMAIN/timeout/empty/unparseable. `resolve=True` is the DEFAULT | `security.py:146-170, 174` |
| F13 | **Per-hop redirect re-validation.** `allow_redirects=False` on 100% of outbound calls (zero `=True` in tree). Relative `Location` resolved via `urljoin`, hop count capped, scheme-downgrade blocked. Webhook treats 3xx as permanent non-retryable failure and logs only the Location host | `safe_http.py:107-122, 167-189`; `webhook_delivery.py:296, 308-321` |
| F14 | Playwright route-layer guard on every context (incl. `reset_context`) validates ALL http(s) requests — documents, sub-frames, XHR, scripts — and aborts pre-flight. Model-emitted JS refused outright | `base_scraper.py:262, 293, 329-375, 377-383`; `ai/navigator.py:361-370` |
| F15 | Cookie origin pin on scraped hrefs — exact scheme+host+port, explicitly NOT subdomain-aware | `safe_http.py:37-48, 73-74` |
| F16 | Ambient-proxy bypass closed (`trust_env=False` on every session) — without it a `HTTPS_PROXY` would move resolution off-box and void the resolved-IP check | `safe_http.py:29-30`; `webhook_delivery.py:68-69`; `dialer_outbox.py:40-41`; `pacs.py:143`; `skip_trace.py:449,521` |
| F17 | PhoneBurner destination double-gated (hardcoded host pin + SSRF guard); credentials re-read from DB at send time, never serialized into a Celery arg | `dialer_outbox.py:8-21, 187-191, 233, 244` |
| F18 | CSV/Excel/JSON formula injection — three-check sanitizer incl. de-quoted probe and TAB replacement; complete field coverage; correct no-double-sanitize note | `security.py:424-450, 458-472`; `lead_export.py:405-439, 572-681`; `data_exporter.py:186-189, 200-208` |
| F19 | No SQL injection from scraped or user text; dynamic ORDER BY allowlisted by Literal type | see Ingestion section |
| F20 | No open redirect / avatar / logo / `redirect_uri` surface | `billing.py:1130,1131,1669` |

---

## TEST-FILE VERIFICATION — `tests/test_webhook_ssrf.py`

Verified the implementation against the tests, not the tests against themselves.

**The file asserts the wrong thing on the single point that matters most.** `test_resolution_blocks_loopback_host`
(`:73-77`) and the module docstring (`:3-6`) both label a hostname→resolved-IP check as "the
DNS-rebinding defense." It is not. Rebinding requires two differing resolutions; no test does that,
and no test asserts the connection uses the validated address. This is the source of the false
assurance — correct the comment at `:75` and the docstring at `:4` even before the code.

**Secondary coverage gaps** (implementation correct in all three — untested, not broken):
- `fc00::/7` and `fe80::/10` are implemented (`security.py:62, 67`) but absent from the parametrize
  tables at `:34-44` and `:56-67`.
- `::ffff:127.0.0.1` specifically untested; `::ffff:169.254.169.254` (`:43`) and `::ffff:10.0.0.1`
  (`:64`) are covered.
- No test asserts `allow_redirects=False` or per-hop re-validation in `safe_get_following`.

**Good test:** `test_worker_blocks_ssrf_webhook_without_posting` (`:86-96`) runs the real Celery task
eagerly and asserts both `status == "blocked"` and `result.successful()`, correctly pinning the
no-retry behaviour.

---

## ROLL-UP

| Sev | Findings |
|---|---|
| **P1** | F1 — DNS rebinding: resolved IP validated then discarded; connection re-resolves. Affects every outbound call including customer webhooks carrying lead PII. Blind. |
| **P2** | F2 — no response-size cap on `safe_get` / `safe_get_following` / `download_tracerfy_csv`; F3 — Chromium `--no-sandbox` rendering attacker-controlled county HTML |
| P3 | F4, F5, F6, F7, F8, F9, F10 |
| SECURE | F11–F20 |
| UNVERIFIED | Frontend XSS rendering of `enrichment_data` (separate repo) |

**GATE: NO-GO** on anything touching the outbound webhook path until F1 is fixed. One P1, two P2.

**The asymmetry worth carrying forward:** redirect re-validation — the harder of the two classic SSRF
failures — is implemented correctly and consistently at every call site. DNS rebinding is not
implemented at all, yet four docstrings and one test comment assert that it is. The gap is not an
oversight in a neglected corner; it is a confident, documented, tested-looking claim with nothing
behind it.
