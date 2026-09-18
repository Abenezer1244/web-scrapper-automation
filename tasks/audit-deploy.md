# Deployment / Middleware / Rate-Limit / Logging / Dependency Audit

**Scope:** deployment posture, middleware, rate limiting, CORS, error handling, logging, dependencies.
**Worktree:** `C:/Users/Windows/bl-wt-secaudit` @ `60f1b00`
**Method:** read-only static audit. No files under audit were edited, no pytest was run, no dependency
was changed. Live-verified items are marked; they come from the team lead's own probes against
`api.bridgeleads.io`.

---

## 0. Severity summary

| ID | Finding | Class | Sev |
|---|---|---|---|
| F0 | All IP-keyed rate limiting + per-IP brute-force lockout dead in prod (Cloudflare) | CONFIRMED (live) | **P0** |
| F1 | 500s bypass both security-header and CORS middleware | CONFIRMED | P3 |
| F2 | HSTS never emitted by the origin | CONFIRMED (live) | P2 |
| F2b | `BaseHTTPMiddleware` wrapping a 30-minute SSE stream | PARTIAL-WEAK | P3 |
| F3a | XFF parsing logic correct; not spoofable given a trusted peer | **SECURE** | INFO |
| F3b | `fc00::/7` missing from trusted-proxy set (latent, *not* the live cause) | CONFIRMED | P3 |
| F4 | `/jobs/{id}/download`, `/export-url`, `POST /auth/api-key` unthrottled | CONFIRMED | P2 |
| F5 | Flat limits, no cost weighting | PARTIAL-WEAK | P3 |
| F6 | Redis fail-open; `_fallback_hits.clear()` flushes all buckets | PARTIAL-WEAK | P2 |
| F7 | Redaction filter unreachable from the riskiest loggers | PARTIAL-WEAK | P2 |
| F8 | Redaction misses non-HTTP DSN creds, `whsec_`, `re_` | PARTIAL-WEAK | P3 |
| F9 | Rate-limit trips / authz failures / webhook sig failures unaudited | PARTIAL-WEAK | P3 |
| F10 | CORS sound — explicit list, no wildcard/regex/reflection | **SECURE (live)** | INFO |
| F11 | CSP/`nosniff`/CORP earn their place; 4 headers cargo-cult for an API | **SECURE (live)** | INFO |
| F12 | Ref-id 500s, docs gated, no unauthenticated admin route | **SECURE (live)** | INFO |
| F12b | No `DEBUG` vs `ENVIRONMENT` cross-validation (would enable SQL echo) | PARTIAL-WEAK | P3 |
| F13 | No hardcoded fallback secret; >=32 validator + deny-list; DB/Redis TLS forced | **SECURE** | INFO |
| F13b | `TRUSTED_PROXY_HOPS` missing from `.env.example` | CONFIRMED | P3 |
| F14 | No app-layer request body size limit | CONFIRMED | P3 |
| F15 | `docker-compose.prod.yml` / nginx dormant, not the Railway path | NOT APPLICABLE | P3 |
| F16 | Dependency CVE status not dischargeable from knowledge — scan required | UNVERIFIED | P2 |
| F17 | Absolute URLs all built from config constants | **SECURE** | INFO |

**Highest-leverage actions, in order**

1. Cloudflare Rate Limiting Rules on `/auth/*` — today, no deploy. Closes the P0 while the code fix lands.
2. Enable HSTS at the Cloudflare edge — today, no deploy. Closes F2's practical exposure.
3. Lock the Railway origin behind Cloudflare, then switch `client_ip()` to `CF-Connecting-IP`.
4. Run `pip-audit` against `requirements.txt` — discharges F16.
5. Convert `SecurityHeadersMiddleware` to pure ASGI — closes F1 and F2b together.

---

## 1. Middleware order

Registration order — this is the complete set:

| # | Line | Middleware |
|---|---|---|
| 1 | `main.py:63` | `SecurityHeadersMiddleware` |
| 2 | `main.py:65` | `CORSMiddleware` |

No `TrustedHostMiddleware`, no `GZipMiddleware`, no body-size middleware, no rate-limit middleware.

`add_middleware` does `user_middleware.insert(0, ...)`, so **last registered is outermost**.

Effective request order (outer -> inner):

```
ServerErrorMiddleware              <- handler = main.py:102  [OUTSIDE everything]
  +- CORSMiddleware                   main.py:65
     +- SecurityHeadersMiddleware     main.py:63
        +- ExceptionMiddleware        HTTPException / RequestValidationError
           +- APIRouter -> handler
              +- await rate_limit(...)   [IN-BODY, not middleware]
```

Response order (inner -> outer): router -> ExceptionMiddleware -> SecurityHeaders (adds headers)
-> CORS (adds ACAO) -> ServerError.

**Verdict.** CORS outside SecurityHeaders is correct — CORS must be outermost to short-circuit
preflight. The rate limiter is not middleware at all; it is an explicit in-handler `await` (or a route
`dependencies=[...]`), so on most routes it runs *after* the auth dependency has resolved. The one
route that gets this right is `/billing/activation-funnel` (`billing.py:102-120`), which runs the
limiter as a dependency ahead of `require_admin`, deliberately, per the comment at `billing.py:103-111`.

- **4xx:** headers applied. `ExceptionMiddleware` is inside `SecurityHeadersMiddleware`, so
  401/403/404/422/429 all carry the full set.
- **5xx:** headers NOT applied — see F1.
- **Streaming:** headers applied (set on `http.response.start` before the body streams). See F2b.
- **Preflight:** `CORSMiddleware` short-circuits without calling the inner app, so preflight responses
  carry no security headers. Harmless; the lead's evil-origin preflight -> 400 confirms the
  short-circuit works.

---

## 2. Rate-limit coverage table

Backend is **Redis**, shared across replicas — `rate_limit.py:146-153` uses a pipelined
`ZREMRANGEBYSCORE` / `ZADD` / `ZCARD` / `EXPIRE` sliding window. It is **not** an in-process dict.
(`_fallback_hits`, `rate_limit.py:118`, is per-process but is only the Redis-outage path.)

Zones (`rate_limit.py:24-42`): `auth` 10/60s | `jobs` 5/60s | `general` 60/60s | `webhook` 120/60s |
`stripe` 10/60s.

Read the **Live?** column against F0: IP-keyed zones are non-functional in production today;
user-keyed zones work correctly.

| Route | Limited? | Zone / key | Live? |
|---|---|---|---|
| `POST /auth/register` | yes | `auth`/IP (`registration.py:221`) | **broken** |
| `POST /auth/verify-email` | yes | `auth`/IP (`registration.py:405`) | **broken** |
| `POST /auth/login` | yes | `auth`/IP (`login.py:47`) + `auth`/user MFA-issue (`:86`) | IP broken / user ok |
| `POST /auth/login/mfa` | yes | `auth`/IP (`login.py:106`) + `mfa-verify:{uid}` (`:132`) | IP broken / user ok |
| `POST /auth/login/break-glass` | yes | `auth`/IP (`login.py:211`) + per-user (`:235`) | IP broken / user ok |
| `POST /auth/refresh` | yes | `auth`/IP (`login.py:418`) | **broken** |
| `POST /auth/forgot-password` | yes | `auth`/IP (`password.py:106`) | **broken — lead's probe** |
| `POST /auth/reset-password` | yes | `auth`/IP (`password.py:141`) | **broken** |
| `POST /auth/change-password` | yes | `auth`/IP (`password.py:28`) | broken |
| `POST /auth/mfa/setup` | yes | `auth`/IP (`mfa.py:28`) | broken |
| `POST /auth/mfa/enable` | yes | `auth`/IP + per-user (`mfa.py:59,61`) | IP broken / user ok |
| `POST /auth/mfa/disable` | yes | `auth`/IP + per-user (`mfa.py:129,132`) | IP broken / user ok |
| **`POST /auth/api-key`** | **NO** | none — credential minting | — |
| `GET /auth/config`, `/me`, `/mfa/status`, `/onboarding` | NO | none (cheap reads) | — |
| `PUT /auth/profile`, `/notification-preferences` | NO | none (DB writes) | — |
| `POST /auth/logout`, `/logout-all` | NO | none | — |
| `POST /jobs` (create) | yes | `jobs`/user (`jobs.py:285`) | ok |
| `GET /jobs/{id}/results` | yes | `general`/user (`jobs.py:390`) | ok |
| **`GET /jobs/{id}/download`** | **NO** | none (`jobs.py:1028`) — builds + streams full lead CSV live from DB | — |
| **`GET /jobs/{id}/export-url`** | **NO** | none (`jobs.py:960`) — mints download tokens | — |
| `GET /jobs/{id}/logs` (SSE) | partial | no `rate_limit`; SSE leases cap at `SSE_MAX_STREAMS_PER_USER=5` (`jobs.py:818`) | ok (user-keyed) |
| `GET /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}` | NO | none | — |
| `POST /scrapers`, `PATCH`, `DELETE /scrapers/{id}` | NO | none | — |
| `POST /scrapers/connectors` (admin) | NO | none (admin + MFA gated) | — |
| `PUT /scrapers/{id}/csv-layout` | yes | `general`/user (`scrapers.py:846`) | ok |
| `GET /scrapers/{id}/records` | yes | `general`/user (`scrapers.py:1153`) | ok |
| `POST /scrapers/{cid}/jobs/{jid}/dialer-replay` | yes | `general`/user (`scrapers.py:1319`) | ok |
| `GET /scrapers`, `/sample`, `/connectors`, `/{id}` | NO | none | — |
| `POST /segments/{intersection,union}[/export]` | yes | `general`/user (`segments.py:589,637,735,763`) | ok |
| `POST /batches` + batch reads/downloads | yes | `general`/user (`batches.py:180,727,825,848,1027,1067`) | ok |
| `GET /batches`, `GET /batches/{id}` | NO | none | — |
| `GET /billing/activation-funnel` (admin) | yes | `general`/`admin-funnel:{IP}`, runs before `require_admin` (`billing.py:102-120`) | IP-keyed, broken |
| `GET /billing/{referral,skip-trace-usage,usage}` | yes | `general`/user | ok |
| `GET /billing/subscription`, `POST /checkout`, `/change-plan`, `/portal` | yes | `stripe`/user, fail-CLOSED fallback | ok |
| **`POST /billing/webhook` (Stripe)** | yes | `webhook`/IP — before signature verify (`billing.py:1710`) | **broken** |
| **`POST /webhooks/tracerfy`** | yes | `webhook`/IP — before secret verify (`webhooks.py:164`) | **broken** |
| `POST /webhooks/tracerfy/{secret}` (legacy) | yes | `webhook`/IP (`webhooks.py:180`) | broken |
| `GET /analytics/summary` | yes | `general`/user (`analytics.py:79`) | ok |
| `GET /notifications`, `PATCH /{id}/read`, `POST /read-all` | NO | none | — |
| `GET /health`, `GET /ready` | NO | none (`/ready` does a real DB round-trip) | — |

**Checked and found NOT to be gaps.** There is no email-resend endpoint — verification resend is
worker-driven (`workers/scheduler_helpers/registration.py:172`). There is no skip-trace start
endpoint — it is Celery-dispatcher driven; only `GET /billing/skip-trace-usage` is exposed, and it is
limited.

**Surviving control.** `once_per()` (`rate_limit.py:191-214`) keys on the **email**, not the IP, so the
duplicate-signup email-bomb guard still functions under F0. It also fails **closed** and documents why —
the deliberate opposite of `rate_limit()`'s fail-open posture. That asymmetry is correct.

### F0 — All IP-keyed rate limiting and per-IP brute-force lockout are non-functional (P0)

**Evidence**

- `rate_limit.py:99-109` — `client_ip()` returns `parts[-1]` (rightmost XFF) when the peer is private,
  else `direct_ip`.
- `rate_limit.py:90-97` — the code's own comment predicts this exact failure if a CDN is placed in front.
- `security.py:511` — HSTS gated on `request.url.scheme == "https"`. The live measurement shows no HSTS,
  therefore `scope["scheme"] == "http"`, therefore uvicorn did not trust the peer, therefore
  `scope["client"]` is the raw edge hop rather than a rewritten client address.
- `start.sh:97` — bare uvicorn, no proxy flags. `FORWARDED_ALLOW_IPS` absent from the entire repo.
- `login.py:49` — `ip = client_ip(request)` feeds `BruteForceProtection`.
- `security.py:578-580` — `audit_log()` records the same broken value as `audit_events.ip`.
- Live probe: 14x `/auth/forgot-password` -> 14x 200, with and without a fixed `X-Forwarded-For`.

**Attack path.** Unauthenticated, unlimited credential stuffing and password spraying against
`/auth/login`; unlimited account enumeration and reset-email flooding via `/auth/forgot-password`;
unlimited `/auth/register`; unlimited webhook signature spray burning HMAC CPU on both receivers.
Per-IP lockout never arms, so only the 15-minute-capped per-email counter slows an attacker — and
spraying *across* accounts defeats that entirely.

**Blast radius beyond the limiter.** Three controls key off the same broken function:

1. IP-keyed rate limiting (`auth`, `webhook`, admin-funnel). Every `identifier=current_user.id` call
   site is unaffected. The broken subset is therefore *exactly the unauthenticated surface*.
2. Per-IP brute-force lockout — the whole progressive 1/5/30-min/24h ladder
   (`auth_hardening.py:402-407`) never accumulates. The per-email counter still works
   (`auth_hardening.py:540`) but is capped at 15 min by design (`_EMAIL_LOCKOUT_CAP_SECONDS`).
3. **Every `audit_events.ip` row in production is a Cloudflare edge IP.** Incident response and
   forensics across the entire audit trail cannot attribute any event to a real source address.

**Unresolved sub-question (does not block the fix).** Two models both predict the observation:

- *Model A* — peer is a private Railway hop, `_is_trusted_proxy()` True, XFF branch entered,
  `parts[-1]` is Cloudflare's rotating egress IP.
- *Model B* — peer is public, `_is_trusted_proxy()` False, `direct_ip` returned, also rotating.

The lead's fixed-XFF test does not discriminate: both ignore client-supplied XFF. The HSTS argument
proves **uvicorn's** trust check failed (peer not in its `127.0.0.1` default); it says nothing about the
app's *separate* `_is_trusted_proxy()` allowlist (`rate_limit.py:54-60`: 127/8, ::1, 10/8, 172.16/12,
192.168/16). `audit_events.ip` does not discriminate either — it records `client_ip()`'s **output**,
which is the same rotating public IP under both models.

**Cheapest discriminator, zero deploy.** ASN-lookup the distinct output IPs:

```sql
SELECT DISTINCT ip FROM audit_events WHERE created_at > now() - interval '1 hour';
```

then `whois <ip> | grep -i origin`. **AS13335 (Cloudflare)** means the value came from XFF's rightmost
entry -> Model A. A **Railway/GCP ASN** means the value is the immediate peer -> Model B. (If the table
is empty for the window, re-probe `/auth/login` with a known test account and a wrong password —
`login_failure` fires unconditionally, and be aware it arms the per-email lockout for 15 minutes.)

**The fix is identical under both models**, so this is a forensics question, not a decision question.

**Fix**

- **Step 0 — immediate, zero deploy.** Cloudflare Rate Limiting Rules on `/auth/login`,
  `/auth/forgot-password`, `/auth/register`, `/auth/verify-email`, `/auth/refresh` (e.g. 10 req/min per
  IP). Works correctly today, never touches the origin. Also enable HSTS at the edge.
- **Step 1 — make Cloudflare the only ingress.** Cloudflare Tunnel (`cloudflared`; the origin gets no
  public address) is cleanest. Alternatives: Authenticated Origin Pulls (mTLS) with the origin rejecting
  anything lacking the client cert, or an enforced origin-secret header.
  **Until this holds, Step 2 is exploitable** — if `*.up.railway.app` stays reachable, an attacker skips
  Cloudflare and forges `CF-Connecting-IP` freely, which is strictly worse than today.
- **Step 2 — read `CF-Connecting-IP`.** Settings-gated edge mode (`TRUSTED_EDGE=cloudflare`). A single
  value, not a list, so there is no hop arithmetic to get wrong. Validate it parses as a public IP. Keep
  the XFF-rightmost path as the non-CF default. Update the now-inverted comment at `rate_limit.py:90-97`.
- **Step 3 — set `FORWARDED_ALLOW_IPS`.** Restores `scope["scheme"] = "https"` and therefore HSTS from
  the origin. **This alone does not fix the limiter** — it makes `request.client.host` the rightmost XFF
  entry, which is still Cloudflare's edge. Steps 1+2 are what fix the limiter.
- **Step 4 — close the `fc00::/7` gap** in `_TRUSTED_PROXY_NETWORKS` for parity with `security.py:62`.
  Latent trap, not the live cause.
- **Step 5 — add an assertion.** Log the resolved `client_ip()` for the first N requests after boot, or
  WARN when the configured edge mode disagrees with the observed peer. This bug was invisible for as long
  as it was because nothing ever asserts the limiter's key is the value it thinks it is.

### F3a — XFF parsing logic is correct (SECURE, INFO)

`rate_limit.py:104-107` takes `parts[-hops]` — the Nth-from-**right** entry, i.e. the hop the trusted
proxy itself appended. That is the correct algorithm and is **not client-spoofable given a trusted peer**.
Header trust is gated on the peer being private, and vendor headers are deliberately untrusted with sound
reasoning. The classic leftmost-XFF bypass is explicitly closed. F0 is a **precondition** failure, not a
logic failure: the algorithm is right, the peer assumption went stale when Cloudflare was added.

### F3b — `fc00::/7` missing from the trusted-proxy set (P3, latent)

`_TRUSTED_PROXY_NETWORKS` (`rate_limit.py:54-60`) omits IPv6 ULA `fc00::/7` while the SSRF blocklist at
`security.py:62` includes it — the asymmetry indicates an oversight rather than a decision.
**An earlier hypothesis that this collapses all callers into one shared bucket is RETRACTED:** a shared
bucket would have produced a hard 429 on request 11, and the live probe returned 200s. The key rotates,
it does not collapse.

### F4 — The most expensive endpoint is unthrottled (P2)

`jobs.py:1028` has no `rate_limit` call; the only occurrences in the file are `:19`, `:285`, `:390`.
Its docstring at `jobs.py:1047` — "Build and stream the lead CSV LIVE from the DB". Also unthrottled:
`GET /jobs/{id}/export-url` (`jobs.py:960`), `POST /auth/api-key`, all scraper CRUD writes,
`PUT /auth/profile`.

**Attack path.** An authenticated user, or anyone holding a 60-second download token that `/export-url`
mints without limit, loops `/download` and forces unbounded concurrent full-corpus CSV builds against a
**`NullPool`** async engine (`src/db/session.py:54`) — every request takes a fresh Postgres connection.
Connection exhaustion takes down the whole API, not just the abuser.

**Fix.** A dedicated tight `export` zone keyed on `current_user.id` (unaffected by F0) on `/download`,
`/export-url`, and the segment/batch exports; `auth`-zone limiting on `POST /auth/api-key`.

### F5 — Flat limits, no cost weighting (P3)

`rate_limit.py:24-42` — five `(count, seconds)` buckets, no cost notion. `general` = 60/min applies
equally to `GET /billing/usage` (one indexed row) and `POST /segments/union/export` (`segments.py:763`,
a multi-table intersection over a tenant's whole corpus). The `stripe` zone (`rate_limit.py:35-41`) shows
the right instinct — it exists because those calls spend external quota — but the reasoning was never
applied to DB cost.

### F6 — Fail-open on Redis loss; the fallback can be flushed wholesale (P2)

On `RedisError` the limiter fails **open** (`rate_limit.py:154-179`), deliberately, with the incident
rationale documented inline. Security-critical zones (`auth`, `webhook`, `stripe`) degrade to a
per-process limiter rather than fully open (`_FALLBACK_ZONES`, `:117`). `BruteForceProtection.check()`
also fails open (`auth_hardening.py:526-532`). During a Redis outage the combined auth protection is
per-worker 10/min/IP with no lockout ladder, multiplied by (replicas x uvicorn workers).

Concrete bug:

```python
# rate_limit.py:126-127
if len(_fallback_hits) > 10_000:  # crude memory bound for the fallback path
    _fallback_hits.clear()
```

`.clear()` wipes **every** bucket including the attacker's. During an outage, traffic from more than 10k
distinct keys resets all counters and the last remaining auth protection vanishes.

**Fix.** Evict expired buckets or use a bounded LRU; never clear wholesale.

---

## 3. Error handling and information exposure

### F1 — 500 responses bypass both middlewares (P3)

Starlette's `build_middleware_stack` pulls the `Exception` handler *out* of the exception-handler map and
hands it to `ServerErrorMiddleware`, which it places **outermost**:

```python
for key, value in self.exception_handlers.items():
    if key in (500, Exception):
        error_handler = value          # <- main.py:103 lands HERE
    else:
        exception_handlers[key] = value

middleware = ([Middleware(ServerErrorMiddleware, handler=error_handler, ...)]
              + self.user_middleware
              + [Middleware(ExceptionMiddleware, handlers=exception_handlers, ...)])
```

`ServerErrorMiddleware.__call__` then writes the response via the **raw transport `send`**, never
re-entering the inner app. So the response never traverses `CORSMiddleware`'s send-wrapper or
`SecurityHeadersMiddleware.dispatch`. The exception itself propagates *up* through both (BaseHTTPMiddleware
re-raises; CORS's simple-response path does not catch), so neither middleware ever sees a response object.

**Impact — the control is defeated for its stated purpose.** `main.py:104` mints
`ref = uuid.uuid4().hex[:12]` so support can correlate a user report to the server-side traceback. With no
`Access-Control-Allow-Origin` on that response the browser blocks it and the frontend surfaces an opaque
CORS/network error — the user never sees the `ref`. The missing `nosniff`/CSP on a short JSON body is
negligible by comparison.

**Test that will mislead you.** `/ready`'s 503 (`main.py:191-193`) is a *returned* `JSONResponse`, not a
raised exception. It travels the normal inner path and **does** get full headers. Use a genuinely raised
unhandled exception.

**Fix.** Convert `SecurityHeadersMiddleware` to pure ASGI and register it outermost (also closes F2b);
or set the headers inside `_unhandled_exception_handler`.

### F12 — Error handling, DEBUG, information exposure (SECURE, live-verified)

- Global handler returns `{"detail": "Internal error", "ref": "<12 hex>"}` (`main.py:102-108`) — no stack
  trace, no SQL, no file path. A reference id **is** returned and correlated in the log line
  (`main.py:105-107`).
- Docs gated: `main.py:55-57` sets `docs_url` / `redoc_url` / `openapi_url` to `None` unless `DEBUG`.
  `DEBUG: bool = False` (`settings.py:228`), `.env.example:64` `DEBUG=false`, `docker-compose.prod.yml`
  `DEBUG: "false"`. Live: all three 404.
- No `--reload` in production (`start.sh:97`). The `--reload` at `docker-compose.yml:42` is the dev compose.
- `ENVIRONMENT` defaults to `"production"` (`settings.py:229`) — fail-safe — and is consumed for real at
  `src/db/session.py:37`.
- `/health` returns `{"status":"ok","service":"bridgeleads-api"}` (`main.py:151`) — no version, no DB, no
  queue. `/ready` returns only `{"status":"degraded","ref":...}`, with the reasoning deliberately
  documented at `main.py:181-187`.
- **No unauthenticated admin or debug route exists.** All 69 route decorators were enumerated and each
  signature checked. The only unauthenticated routes are the pre-auth `/auth/*` set, `GET /auth/config`
  (password-policy rules only), `GET /scrapers/connectors`, and the two webhook receivers
  (signature/secret verified in-body). The admin analytics route is properly gated — `billing.py:118-121`,
  `dependencies=[Depends(_rate_limit_activation_funnel), Depends(require_admin)]`, plus admin MFA
  enrollment per `billing.py:148-152`.

### F12b — No `DEBUG` vs `ENVIRONMENT` cross-validation (P3)

Nothing cross-validates the two. A single `DEBUG=true` would simultaneously expose `/openapi.json` **and**
flip `echo=settings.DEBUG` on the async engine (`src/db/session.py:55`), logging every SQL statement with
bound parameters — seller PII and email blind-index values — into the log pipeline, which per F7 is also
the unredacted path. Add a `model_validator` raising when `DEBUG and ENVIRONMENT == "production"`.

### F14 — No application-layer request body size limit (P3)

Grepped `src/` and `main.py` for `content-length` / `max_body` / `body_size` — zero hits. Starlette and
FastAPI impose no default cap. `client_max_body_size 1m` at `infra/nginx/api.bridgeleads.io.conf:45` is
not in the request path (F15). `python-multipart==0.0.31` is pinned for form-parse DoS CVEs
(`requirements.txt:6`) — the risk is understood, but the pin bounds the parser, not the payload.
Cloudflare's edge now imposes a ceiling, so this is bounded in practice, but the application has no say in
it and no per-route limit. Also: no `Cache-Control: no-store` on the CSV/lead endpoints, so an intermediary
is not told to avoid retaining PII responses.

---

## 4. CORS and security headers

### F10 — CORS (SECURE, live-verified)

`main.py:65-79` — explicit finite `allow_origins` list; **no wildcard, no `allow_origin_regex`, no
reflection**. The specific `allow_origins=["*"]` + `allow_credentials=True` misconfiguration is **absent**.
Methods and headers are explicit allowlists, not `["*"]`.

The env-driven portion is defensively parsed at `settings.py:420-435`: `get_allowed_origins()` drops a
literal `"*"` and drops any non-`https://` origin except `http://localhost` / `http://127.0.0.1` — so a
stray `ALLOWED_ORIGINS=*` in Railway cannot widen the policy. Production default if unset
(`settings.py:244`) is the same three origins.

Live results corroborate: attacker origin, `null` origin, and the `app.bridgeleads.io.evil.com` suffix
trick all received no ACAO (Starlette does exact set membership, not suffix matching), and an evil-origin
preflight returned 400.

*Nit (INFO):* the three hardcoded origins at `main.py:68-70` duplicate the default, and being hardcoded
means an origin can never be removed by config — retiring `bridgeleads-web.vercel.app` needs a code change.

### F11 — Which headers actually matter for a JSON API origin (SECURE, live-verified)

All values from `security.py:497-512`. Live capture matches exactly, HSTS excepted.

| Header | Literal value | Verdict for this origin |
|---|---|---|
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` | **Meaningful.** Right CSP for an API; `frame-ancestors` is the modern clickjacking control. |
| `X-Content-Type-Options` | `nosniff` | **Meaningful.** Blocks MIME-sniffing a JSON body into script — the classic JSON-XSS vector. |
| `X-Frame-Options` | `DENY` | **Redundant** with `frame-ancestors` in every modern browser. Harmless; keep for legacy. |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | Correct value. **Never sent — F2.** |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | **Cargo-cult here.** Governs referrers on navigations from a document; API JSON responses are not documents. Belongs on the Vercel frontend. |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` | **Cargo-cult here.** Needs a browsing context; a JSON response has none. |
| `Cross-Origin-Opener-Policy` | `same-origin` | **Cargo-cult here.** COOP isolates a browsing context group; an API response creates none. |
| `Cross-Origin-Resource-Policy` | `same-site` | **Meaningful.** Genuinely blocks another origin pulling these JSON responses in as a subresource. |

The four that earn their place for an API origin: **CSP, `nosniff`, HSTS, CORP**. The other four are inert
but harmless. Frontend headers are a separate repo's concern and were not conflated here.

### F2 — HSTS never emitted by the origin (P2, live-verified)

```python
# src/api/middleware/security.py:511-512
if request.url.scheme == "https":
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
```

`request.url.scheme` is `scope["scheme"]`, which stays `"http"` unless uvicorn's `ProxyHeadersMiddleware`
rewrites it from `X-Forwarded-Proto` — and that happens only when the immediate peer is in
`--forwarded-allow-ips`, default `127.0.0.1`. `start.sh:97` is a bare uvicorn invocation with no proxy
flags, and `FORWARDED_ALLOW_IPS` appears nowhere in the repo. Live: no HSTS header on `api.bridgeleads.io`.

The `add_header Strict-Transport-Security` at `infra/nginx/api.bridgeleads.io.conf:41` does **not** cover
this — that config is not in the request path (F15).

**Fix.** Enable HSTS at the Cloudflare edge immediately (zero deploy). Then set `FORWARDED_ALLOW_IPS` to
restore it at the origin, or emit it unconditionally — correct for an HTTPS-only API.

### F2b — `BaseHTTPMiddleware` wrapping a 30-minute SSE stream (P3)

`security.py:492` — `class SecurityHeadersMiddleware(BaseHTTPMiddleware)`. `jobs.py:836` opens a generator
holding a Redis Pub/Sub connection and an SSE lease for up to 30 minutes, released in `finally`. Starlette
documents `BaseHTTPMiddleware` as problematic for long-lived streams: it interposes an anyio task group
that changes disconnect and cancellation semantics. Same failure class as this project's recorded history
of leaked SSE lease counters producing spurious "max 5 streams". The pure-ASGI conversion in F1 removes it.

---

## 5. Absolute URL generation

### F17 — All absolute URLs come from config constants (SECURE, INFO)

If `scope["scheme"] == "http"` (F2), anything building an absolute URL from the request would emit
`http://` links — and would also be host-header poisonable. **This does not materialise.** A grep of all of
`src/` plus `main.py` for `url_for`, `request.base_url`, `request.url`, and Host / `X-Forwarded-Host` reads
found **zero request-derived URL construction**. The only `request.url` use is `security.py:511`, the HSTS
scheme check itself; every other hit is scraper `base_url` (outbound targets from DB config), unrelated.

| Link | Source | file:line |
|---|---|---|
| Password-reset | `settings.FRONTEND_URL` | `auth_helpers/password.py:123` |
| Email-verification | `settings.FRONTEND_URL` | `workers/scheduler_helpers/registration.py:172` |
| Stripe success / cancel | `settings.FRONTEND_URL` | `billing.py:1130-1131` |
| Stripe portal return | `settings.FRONTEND_URL` | `billing.py:1669` |
| Central link helper | `settings.FRONTEND_URL` | `config/frontend_routes.py:72-74` |
| Batch / delivery emails | `settings.FRONTEND_URL` | `workers/batch_export.py:610`, `workers/delivery.py:247` |
| Emailed download-token link | `settings.API_BASE_URL` | `settings.py:239-243`, `workers/tasks.py:2259-2262` |
| `/export-url` response | **relative path, no host at all** | `jobs.py:1025` |

`FRONTEND_URL` defaults to `https://bridgeleads.io` (`settings.py:238`); `.env.example:66` is
`https://app.bridgeleads.io`. Both HTTPS.

**Verdict.** No `http://` downgrade in any user-facing link and **no host-header poisoning vector** — which
is also why the absence of `TrustedHostMiddleware` is genuinely NOT APPLICABLE here rather than an accepted
risk. The F2 `scheme == "http"` consequence is contained entirely to the HSTS branch. Worth preserving: any
future migration to `request.url_for()` would silently reintroduce both problems.

---

## 6. Logging and redaction

### F7 — The redaction filter is very likely unreachable from the riskiest loggers (P2)

**Correction to the audit brief.** `tests/test_config_secret_redaction.py` is **not** a log-redaction test.
Its docstring reads "webhook HMAC secrets must be WRITE-ONLY in responses" — it asserts
`ScraperConfigResponse` strips `webhook_secret` / `dialer_webhook_secret` / `phoneburner_access_token` from
API **responses**. The actual log test is `tests/test_log_redaction.py`, and it calls
`_redaction_filter.filter(record)` **directly** on a hand-built `LogRecord` (`test_log_redaction.py:12-16`).
That proves the **patterns** work. It never exercises the **wiring**.

The wiring is doubtful. `logger.py:66-73`:

- `lg.addFilter(...)` is a **no-op for propagated records**. Python applies a logger's own `.filters` only
  in `Logger.handle()` for records emitted *through that logger*; `callHandlers` walks ancestors invoking
  **handlers** only, so ancestor logger-level filters are never re-applied.
- `handler.addFilter(...)` only helps if the logger has handlers at that moment. `"security"` has none —
  `security.py:14` creates `security.audit`, not `security`. **Root has none either**: `start.sh:97` passes
  no `--log-config`, so uvicorn's default `LOGGING_CONFIG` configures only `uvicorn`, `uvicorn.error` and
  `uvicorn.access`, and adds no root handler.

The two bare loggers in scope are exactly the risky ones:

- `main.py:99` — `logging.getLogger("api.unhandled")`, whose `.exception(...)` at `main.py:105` emits
  **full tracebacks**, where a DSN, SQL text or bound parameter would surface.
- `rate_limit.py:21` — `logging.getLogger("security.rate_limit")`, which logs the raw Redis exception at
  `rate_limit.py:164-167`.

Neither has handlers; both propagate to a handler-less root and land on `logging.lastResort` (a bare
`StreamHandler`, level `WARNING`, **no filters**). Redaction never runs on them.

*Useful corollary.* Because `lastResort` is WARNING-level, the fail-open warning at `rate_limit.py:164`
**does** reach Railway logs (unformatted and unredacted). Its **absence** there is evidence against the
"Redis is down" hypothesis for F0.

**Fix.** Attach the filter to the handlers that actually emit — uvicorn's configured handlers, or install a
root handler at startup and filter that. Then add a test that logs through
`logging.getLogger("api.unhandled")` and asserts on captured **output**, not on the filter function.

### F8 — Redaction misses non-HTTP DSN credentials, `whsec_`, `re_` (P3)

```python
# src/utils/logger.py:39
(re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s:@]+@"), r"\1[REDACTED]@"),  # basic-auth creds in URL
```

Anchored on `https?://` only. Not covered: `postgresql://`, `postgres://`, `rediss://`, `redis://`,
`amqp://` — precisely the shapes of `DATABASE_URL`, `DATABASE_URL_SYNC` and `REDIS_URL`, and precisely what
appears inside the SQLAlchemy and redis-py exception strings that F7's unredacted paths emit. The suite's
only URL case is `"fetching https://admin:s3cretpass@internal.host/path"` (`test_log_redaction.py:30`), so
the gap is invisible to the tests.

Separately, the only value-shaped key pattern is `\bsk[_-][A-Za-z0-9_\-]{16,}` (`logger.py:37`). It catches
`sk_live_...` but **not** `whsec_...` (`STRIPE_WEBHOOK_SECRET`) or `re_...` (`RESEND_API_KEY`). Those are
caught only if they happen to sit next to an `api_key=`-shaped label.

**Fix.** Widen the DSN pattern to `(?i)([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@`; add `whsec_`, `re_`
and `rk_live_` to the key-prefix alternation.

### F9 — Security events are logged, but rate-limit trips and authz failures are not (P3)

`audit_log()` (`security.py:567`) is well implemented — a structured line **plus** a durable
`audit_events` row, `clean_text()` applied to every field (log-injection safe), and a strong task ref so the
GC cannot drop the write mid-flight (`security.py:602`). 28 call sites cover the auth surface:
`login_success`, `login_failure`, `register`, `register_pending`, `register_verified`, `logout`,
`logout_all`, `password_changed`, `password_reset`, `password_reset_requested`, `mfa_enabled`,
`mfa_disabled`, `mfa_setup`, `mfa_failure`, `mfa_challenge`, `mfa_breakglass_used`,
`mfa_breakglass_failure`, `api_key_created`, `job_created`, `scraper_updated`, `profile_updated`,
`notification_prefs_updated`.

**Not audited:**

- **Rate-limit trips.** `rate_limit.py:183-188` raises the 429 with no log and no `audit_log`. There is no
  signal at all that throttling fired — you cannot distinguish "quiet" from "under attack".
  *This is why F0 went unnoticed: a completely dead limiter is indistinguishable from a quiet one.*
- **Authorization failures** — cross-tenant access attempts 404 silently.
- **Webhook signature failures** — despite the `webhook` zone existing specifically to blunt signature
  spray (`rate_limit.py:28-34`).
- **Admin actions** beyond `scraper_updated`, e.g. connector creation.

---

## 7. Settings and secret loading

### F13 — No hardcoded fallback secret; validators and transport security (SECURE, INFO)

- **No hardcoded secret fallback exists anywhere.** The exact dangerous pattern
  (`os.getenv("SECRET_KEY", "dev-secret")` and relatives) was grepped for: zero hits. `SECRET_KEY: str`
  (`settings.py:43`) has **no default**, so pydantic-settings raises at import if the env var is absent.
  Same for `DATABASE_URL`, `DATABASE_URL_SYNC`, `REDIS_URL`.
- The documented >=32-char validator exists and is **stronger than documented** (`settings.py:92-101`): it
  rejects `len(v) < 32` **and** an explicit deny-list containing the literal placeholder shipped at
  `.env.example:22`. Copying `.env.example` to `.env` unchanged therefore **fails startup** rather than
  booting on a known key.
- Two further validators: `FIELD_ENCRYPTION_KEY` is Fernet-parsed at config load (`settings.py:53-72`), and
  `TRACERFY_WEBHOOK_SECRET` must be >=24 chars if set (`settings.py:351-361`).
- **DB TLS is enforced** (`src/db/session.py:27-44`). For any non-local host with no `sslmode`,
  `{"ssl": "require"}` / `{"sslmode": "require"}` is injected; a DSN that *pins* `disable` / `allow` /
  `prefer` raises `RuntimeError` at boot when `ENVIRONMENT == "production"`. Applied to all three engines
  (`:58`, `:108`, `:162`). This closes libpq's silent-plaintext `prefer` default.
  **Known gap, documented in-code at `session.py:19-21`: Alembic builds its own engine and is not covered.**
- **Redis TLS is verified** (`settings.py:437-469`): `ssl_cert_reqs` defaults to `"required"` with a
  `certifi` CA bundle, and the value is a **string**, not the `ssl.CERT_NONE` integer that caused a prior
  production outage.

### F13b — `TRUSTED_PROXY_HOPS` missing from `.env.example` (P3)

`settings.py:248` declares it; `.env.example` does not document it, violating
`.claude/rules/settings.md` ("New settings must be added to both `settings.py` and `.env.example`").
Given F0, this is the one proxy-related knob an operator would need to find and cannot.

---

## 8. Deployment artifacts

### F15 — `docker-compose.prod.yml` and `infra/nginx/` are not the production deployment (NOT APPLICABLE, P3)

Production is Railway: `railway.toml` sets `builder = "DOCKERFILE"` and
`startCommand = "/app/start.sh"`, and `start.sh:97` execs a bare uvicorn. There is no nginx and no
docker-compose in that path.

This matters because those files *look* like they provide controls that are in fact absent in production:

- nginx `limit_req_zone auth 10r/m` (`api.bridgeleads.io.conf:5-7`) — not active; the only rate limiting is
  the in-app Redis limiter, which is itself broken (F0).
- nginx `add_header Strict-Transport-Security` (`:41`) — not active; see F2.
- nginx `client_max_body_size 1m` (`:45`) — not active; see F14.

Anyone reading them while diagnosing F0 or F2 would be misled. If that compose stack is ever actually
deployed it carries its own problems: Prometheus runs with `--web.enable-lifecycle` on a **published**
`9090:9090` with no auth (unauthenticated `POST /-/reload`), and Grafana `3001`, Loki `3100` and Flower
`5555` are all published to the host. Flower at least has `--basic_auth`.

**Recommendation.** Delete these files, or add a one-line header to each stating they are not the
production topology.

---

## 9. Dependencies

### F16 — Dependency CVE status cannot be discharged from knowledge (UNVERIFIED, P2 as a process gap)

**This is a knowledge-based assessment, not a scan — and for this particular file it is not a reliable
one.** A majority of these pins sit at or beyond the assistant's knowledge cutoff: `fastapi==0.141.1`,
`cryptography==50.0.0`, `pypdf==6.16.1`, `playwright==1.62.0`, `anthropic==1.3.0`, `requests==2.34.2`,
`lxml==6.1.0`, `pytest==9.0.3`, `uvicorn==0.52.0`. The inline comments cite 2026-dated CVE and PYSEC
identifiers that cannot be confirmed or refuted from knowledge. **No CVE verdict is invented here for
versions that cannot be verified.**

**The finding is the process gap:** knowledge-based CVE matching cannot discharge this requirement for this
dependency set. A `pip-audit` or `osv-scanner` run against `requirements.txt` is required, and should be
wired into CI so the answer stays current.

**Load-bearing pins — do NOT bump without the code change first. Both are correctly documented in-file and
nothing is flagged against them:**

- **`stripe==11.4.0`** (`requirements.txt:77-95`). v15 changed `StripeObject` to no longer inherit from
  `dict`; `.get()` raises. Roughly 17 call sites including the webhook handler, the subscription sync in
  `workers/scheduler_helpers/billing.py`, and the skip-trace usage meter. The comment correctly notes that a
  method-existence probe does **not** catch this, because it is the *returned object* that changed. Bumping
  alone silently breaks billing, webhooks and usage metering.
- **`redis==5.2.1`** (`requirements.txt:29-34`). `kombu` 5.6.2 declares `redis<6.5`; redis 8.x breaks the
  Celery broker and result backend. A matching ignore rule exists in `.github/dependabot.yml`.

**Supportable notes:**

- `beautifulsoup4==4.12.3` and `sqlalchemy==2.0.36` are several minor versions behind current. Drift worth a
  scheduled bump, not a CVE claim.
- `2captcha-python==1.5.0` and `playwright-recaptcha==0.5.1` are small, low-activity third-party packages
  shipped in a production image — a supply-chain surface disproportionate to their role. Worth hash-pinning
  or vendoring.
- **Checked and cleared: test dependencies ship in the production image.** `Dockerfile:31` runs
  `pip install --no-cache-dir -r requirements.txt`, and `requirements.txt:100-104` includes `pytest`,
  `pytest-cov`, `pytest-asyncio` and `httpx`. Given this project's history of pytest wiping production
  twice, this warranted a hard look — **and it holds**: `.dockerignore:15` excludes `tests` and
  `.dockerignore:29-30` excludes `.env` and `.env.*`, so the image contains the runner but neither the test
  files nor a production `.env` to point it at. Downgraded to image bloat and attack surface only. Splitting
  a `requirements-dev.txt` would still be correct.
- **P3:** `scripts/` is **not** in `.dockerignore`, so every `diag_*`, `backfill_*` and `admin_*` script
  ships in the production image. Post-exploitation convenience only — an attacker with code execution
  already holds the env vars — but there is no reason for them to be there.
