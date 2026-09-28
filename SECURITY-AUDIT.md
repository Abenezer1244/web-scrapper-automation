# BridgeLeads Security Audit #3, Phase 1 (2026-09-27)

> Previous report (audit #2, 2026-09-25) is preserved at `docs/security/SECURITY-AUDIT-2026-09-25.md`.

Code audited: backend `origin/main` `786efcf0` (production at audit start), frontend `origin/master` `e42d5d0`/`6030491`.
Method: 8 parallel audit leaves (each report under `tasks/audit3/`), plus an independent Codex review run in an isolated
worktree without our findings (`tasks/audit3/codex.md`), then driver consolidation and re-verification.
Ledger: `.unlazy/secaudit3/` (status log has every dispatch, return, and remediation event).

This report does not claim BridgeLeads is safe. It states what was tested, what passed, what failed, what was fixed
today, and what remains unverified.

## Executive security summary

- **Tenant isolation held in every test.** 2 accounts x 19 tenant-id routes x all methods (GET, PATCH, PUT, DELETE,
  cancel, replay, download), in 4 directions plus anonymous, with the database connected as a role that bypasses RLS,
  so the app-layer `user_id` predicate was tested on its own: every foreign call 404, every anonymous call 401.
  Codex independently found no IDOR/BOLA.
- **The "previously delivered" records the owner saw are not a cross-tenant leak.** Dedup is scoped to one account.
  The likely cause is S3-19: the dedup key ignores county, so a King lead sharing a parcel number with a Pierce lead in
  the same account is labelled "Already delivered".
- **One P0, remediated today with the owner:** the Cloudflare API token leaked in git (audit #2 N-01) was also inside
  publicly pullable GHCR images. The token was verified ACTIVE, then deleted (now `401 Invalid API Token`); the GHCR
  package was made private (anonymous pull now 401/403). Cloudflare audit log since 2026-03-17 shows no suspicious writes.
- **Two P1 remain open:** trial accounts can spend unbillable Tracerfy lookups (S3-03; main now has a per-account credit cap from #364, merged after this audit started, but it defaults off and trials are still not gated), and IP rate limiting does
  nothing in production (S3-04), which cannot be fixed honestly until Cloudflare is the only ingress (S3-06).
- **No production code was changed in Phase 1.**

| Severity | Open | Fixed/remediated today |
|---|---|---|
| P0 | 0 | 1 (S3-01) |
| P1 | 2 | 1 (S3-02, closed by the rotation) |
| P2 | 15 | 1 (S3-05) |
| P3 | 34 (consolidated) | 0 |

## Architecture reviewed

Full per-layer table with evidence: `tasks/audit3/arch-secrets.md`. Summary:

| Layer | Implementation (verified) |
|---|---|
| Frontend | Next.js 16.3.5 + Auth.js 5 beta on Vercel (`bridgeleads.io`, `app.bridgeleads.io`); no DB access, only `NEXT_PUBLIC_API_URL` public |
| API | FastAPI on Railway behind Cloudflare (`api.bridgeleads.io`); origin also reachable directly via Railway edge (S3-06) |
| Auth | Home-grown HS256 JWT (1 h access, rotating refresh with session families), SHA-256-hashed API keys, bcrypt, TOTP MFA; Auth.js cookie holds backend tokens |
| Authorization | `get_current_user`, `require_plan`, entitlement gate, `require_admin` (404), `require_admin_mfa` |
| Database | PostgreSQL on Supabase; RLS GUC per transaction (belt) + `user_id` predicate (suspenders); roles `bridgeleads_app`, `bridgeleads_system`, owner via `DATABASE_URL_MIGRATE` |
| Workers / queue | Celery worker + beat on Railway; Redis is the Railway-internal `redis:8.2.9` service (not Upstash, as CLAUDE.md says; verified via `railway status`) |
| Scrapers | Playwright Chromium (`--no-sandbox`) + BeautifulSoup, admin-only connector registry, `validate_scraping_target()` |
| Storage / export | Cloudflare R2 (presigned), 60 s download tokens, 48 h emailed links, `sanitize_for_csv()` |
| Payments / providers | Stripe (signed webhooks, event ledger), Tracerfy (server token, shared-secret webhook), Resend |
| Push delivery | Customer job webhooks, generic dialer/Zapier webhook (HMAC), PhoneBurner (OAuth) |
| CI/CD | GitHub Actions: tests, pip-audit, build + push to GHCR (now private), `deploy-production` migration job; `main` push = Railway deploy |
| DNS/CDN | Cloudflare, Terraform-managed |

Trust-boundary flow (enforcement point per hop in `tasks/audit3/arch-secrets.md`): Browser -> Vercel FE (UX gate only)
-> Cloudflare/Railway API (CORS allowlist, headers, rate limit) -> Auth (JWT/API key, plan, admin) -> DB (RLS + user_id)
-> Celery (ids only, owner re-read) -> county sources (untrusted HTML in Chromium, SSRF allowlist) -> enrichment
(`safe_http`) -> Tracerfy (server token, webhook secret) -> results (Fernet PII) -> export/delivery (sanitized CSV,
tokens, pinned webhook egress).

## P0 findings

**S3-01: Cloudflare API token publicly downloadable from GHCR (REMEDIATED 2026-09-27).** CI pushed every `main` build
to `ghcr.io/abenezer1244/web-scrapper-automation`, which accepted anonymous pulls (anonymous tags/list 200, control 403).
358 of 363 tagged images predate the untracking fix `150421e5`; image `main-c23523e` layer 10 carries
`infra/terraform/terraform.tfvars`, hash-matching the leaked blob. Remediation performed with the owner:
- Identified the token via Cloudflare's read-only verify endpoint (value never printed): id `f832ce04...`, "Edit zone
  DNS", DNS Write on **all** zones in the account, issued 2026-03-18, status active, last used 2026-09-26 15:48 UTC (a read).
- Cloudflare audit log 2026-03-17..2026-09-27: the only writes attributable to it are the 3 Terraform records on
  2026-03-18 from `54.235.35.223`; no non-system change to bridgeleads.io after 2026-03-18; all other API DNS writes are
  on the owner's other zones from the owner's residential/mobile IPs on dates matching the owner's other tokens.
  Reads are not in the audit log, so read access by a third party cannot be excluded.
- Token deleted; verify now returns `401 Invalid API Token`. GHCR package set private after confirming Railway api,
  worker, and beat build from the GitHub repo (not GHCR); anonymous token/tags/manifest now 401/403; API health 200.
- Deletion of the 1,075 pre-fix image versions (tagged + their untagged children) was started; see Manual actions.

## P1 findings

- **S3-02 (FIXED by rotation):** the same token remains in git history and at the tip of 226/231 branches (AS-3).
  Harmless now that the token is dead; no history rewrite needed.
- **S3-03 (OPEN): trial accounts can spend unbillable Tracerfy lookups** (AZ-2, audit #2 F-12/B-3). Trials are
  `plan="pro"` (`registration.py:188`), the worker skip-trace gate blocks only Starter (`enrich.py:2284-2292`), trial
  usage is held unbillable (`skip_trace_usage.py:290-352`), and the only cap is global and soft (S3-12).
- **S3-04 (OPEN): IP rate limiting does nothing in production** (IE-1, audit #2 F-01). `rate_limit.py:54-60` does not
  trust `100.64.0.0/10` and `start.sh:97` runs uvicorn without proxy headers; reproduced in-process: 30 password-spray
  attempts from rotating CGNAT peers = 30x401, 0x429 (control from one peer = 25x429). Only the per-email lock works.
  Because the origin is bypassable (S3-06), no IP key is both honest and unforgeable until Cloudflare is sole ingress.

## P2 findings

See the Findings table (S3-05 .. S3-20). Highlights: logged-out tokens still download lead CSVs (S3-07), SSRF via a
URL-parser differential in `safe_http` (S3-08), no limiter on download/export/cancel/scraper writes (S3-09), production
secrets in unprotected GitHub Actions environments (S3-10), the owner DSN on every runtime service (S3-11), Chromium
`--no-sandbox` with all secrets in env (S3-13), a fail-open browser egress guard (S3-14), the Tracerfy webhook body
trusted for billing (S3-15), the legacy Tracerfy path-secret route on by default (S3-16), and the production admin
password in plaintext on OneDrive (S3-17).

## P3 findings

Consolidated in the Findings table (S3-21 .. S3-54). Per-leaf detail with full evidence is in each `tasks/audit3/*.md`.

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| S3-01 | P0 | Secret exposure | GHCR images carrying the Cloudflare token (AS-1) | live: anonymous GHCR tags/list 200; image main-c23523e layer holds terraform.tfvars; Cloudflare verify active then 401 after deletion | none (anonymous pull) | DNS write on all account zones; source code disclosure | Token deleted, package private, pre-fix versions deleted | tests/test_no_committed_credentials.py already guards tracked files; add a CI step asserting the GHCR package is private | FIXED-VERIFIED |
| S3-02 | P1 | Secret in VCS history | infra/terraform/terraform.tfvars in history and 226 branches (AS-3, N-01) | git:579dec50 | repo read access | superseded by S3-01 rotation | Token rotated (deleted); no rewrite needed | none (credential dead) | FIXED-VERIFIED |
| S3-03 | P1 | Billing / quota | Trial skip-trace spend (AZ-2, F-12, B-3); on main since 786efcf0 (#364) a per-account credit cap exists but defaults OFF and trials are still not gated | src/api/routes/auth_helpers/registration.py:188; src/workers/tasks_helpers/enrich.py:2284-2292 | a free trial account | provider cost with no billing; drains the global cap for paying tenants | No paid skip trace without an active paid subscription; per-account credit cap (lookup 1b-1b work) | trial account enqueues 0 Tracerfy rows; per-account cap test | CONFIRMED |
| S3-04 | P1 | Rate limiting | client_ip and every IP-keyed limiter (IE-1, F-01, F-01b) | src/api/middleware/rate_limit.py:54-60; start.sh:97; cmd:probe_spray.py | internet access | password spraying, signup/reset abuse unthrottled | Make Cloudflare sole ingress (S3-06), then trust CF-Connecting-IP only from Cloudflare; per-account limits meanwhile | forged XFF from a non-CF peer ignored; rotating-peer spray gets 429 | REPRODUCED |
| S3-05 | P2 | Source disclosure | Public GHCR package (AS-2) | live: anonymous pull of latest | none | full private backend source public | Package made private 2026-09-27 | CI check that the package is private | FIXED-VERIFIED |
| S3-06 | P2 | Edge bypass | Railway origin answers without Cloudflare (IO-1, F-28) | live: GET /health via Railway edge IP 69.46.46.123 = 200, no CF-RAY | know the Railway hostname | WAF and edge limits skippable; blocks S3-04 | Cloudflare Tunnel or Authenticated Origin Pulls; drop the public Railway domain | live probe returns non-200 via Railway edge | REPRODUCED |
| S3-07 | P2 | Session revocation | GET /jobs/{id}/download session-JWT branch (LT-1, AN-1, AZ-5, LT-2) | src/api/routes/jobs.py:1383-1391; src/api/auth.py:381-384 | a stolen or logged-out access token | signed-out session exports full lead CSVs for up to 1 h, also via ?token= | Route the bearer branch through get_auth_context incl. session-family check; refuse full session tokens in ?token= | logout then download = 401 (header and query) | REPRODUCED |
| S3-08 | P2 | SSRF | safe_http URL parser differential (IE-2, CX-3, IE-12) | src/api/middleware/security.py:234-242; cmd:probe_ssrf; http://127.0.0.1:PORT\@portal.test/ reached loopback | control of a scraped link, redirect Location, or Tracerfy download URL | internal service access; portal cookie sent to loopback | Route safe_http through pinned_session; reject backslash and userinfo; parse with the same library that connects | parser-differential URLs and rebinding refused with 0 listener hits | REPRODUCED |
| S3-09 | P2 | Rate limiting | download, export-url, finished-job logs, cancel, scraper create/edit/delete (LT-4, IE-3, F-09) | live:150/150 x 200 on download; src/api/routes/jobs.py | an account | CPU/PII-decrypt amplification, scraping abuse | Per-user limits on each route | 429 after the configured budget | REPRODUCED |
| S3-10 | P2 | CI secrets | GitHub Actions production env and repo secrets (IO-2, AS-4, S-3, F-14) | cmd:gh api environments (names only); .github/workflows/ci-cd.yml:311-337 | a workflow change merged or a compromised dependency at install | prod DSN and encryption keys exfiltrated | Protection rules + branch policy on production; move DSN to the environment; install deps in a step without secrets; delete unused RAILWAY_TOKEN_PRODUCTION | workflow lint asserting no secrets in install steps | CONFIRMED |
| S3-11 | P2 | DB least privilege | Owner DSN on api, worker, beat (IO-4, AS-7, S-2) | start.sh:55,84,96 | RCE in any service | DDL and RLS bypass | One-shot release migration job; remove DATABASE_URL_MIGRATE from runtime services | startup refuses to run with an owner DSN outside the migrate job | CONFIRMED |
| S3-12 | P2 | Spend control | Tracerfy global soft cap (BI-2, B-4); FIXED IN CODE on main by #364 (per-account + global credit caps read inside the claim lock) but both default OFF, so effective only if production sets SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP | src/workers/skip_trace_dispatcher.py:72-113,274 | any skip-trace customer | one tenant can take the whole daily cap; cap counts rows not credits | Per-account credit budget, cap checked per batch | two tenants, one saturating, the other still served | CONFIRMED |
| S3-13 | P2 | Sandbox | Chromium --no-sandbox in the worker (IO-5, CX-5, F-07) | src/scrapers/base_scraper.py:281 | a renderer exploit in county HTML | worker env holds every secret | Enable the sandbox (seccomp) or isolate rendering in a secretless container | launch args asserted without --no-sandbox | CONFIRMED |
| S3-14 | P2 | SSRF | Playwright egress guard fails open; WebSocket/service worker not intercepted (CX-4, IE-10, E-4) | src/scrapers/base_scraper.py:339,414-460 | hostile page content | browser reaches internal addresses | Fail closed on guard errors; block service workers; intercept WebSocket | guard exception aborts the request; ws:// to loopback blocked | CONFIRMED |
| S3-15 | P2 | Webhook trust | Tracerfy webhook body decides billing; download host check loose (BI-1, CX-1, B-5) | src/workers/tracerfy_ingest.py:436-455,831-841; src/api/billing/skip_trace_usage.py:611-624 | the Tracerfy webhook secret | billing manipulation, fetch from attacker bucket over http | Trust stored rows_uploaded; pin exact host and https | forged rows_uploaded ignored; http host refused | REPRODUCED |
| S3-16 | P2 | Secret handling | Legacy Tracerfy path-secret route on by default; secret reaches logs (CX-2, BI-7, AS-9, F-22, IO-13 part) | src/config/settings.py:345; main.py:108,123-130 | log access | webhook secret disclosed, enabling S3-15 | Default the legacy route off; scrub path in every logger | secret path absent from all log records | REPRODUCED |
| S3-17 | P2 | Credential on disk | Production admin password in plaintext on OneDrive and in a local stash and FE blob (AS-6, S-5) | git:568f67f3 (local stash); scripts/audit_out_ui log (untracked) | OneDrive or device access | admin account takeover | Rotate the admin password; delete the files, stash, and blob | none (owner action) | CONFIRMED |
| S3-18 | P2 | CI deploy integrity | CI runs bare alembic upgrade head against production racing the locked migrate (AS-5, IO-3, S-4) | .github/workflows/ci-cd.yml:337 | every push to main | concurrent migrations, partial DDL | Remove the CI migration or use scripts/migrate.py | none (config) | CONFIRMED |
| S3-19 | P3 | Dedup correctness | Billing dedup key has no county (LT-6); likely source of the owner's "previously delivered" report | cmd:lt_dedup.py same-account King vs Pierce collision | same account, two counties | new leads mislabelled Already delivered | Include county (fips) in the dedup key and fallback | cross-county same-parcel lead stays New | REPRODUCED |
| S3-20 | P3 | Cross-tenant fragments | Anonymous /scrapers/sample shows first name, last initial, filing date, city (LT-7) | cmd:lt harness; src/api/routes/scrapers.py sample cache | none | small PII fragments of tenants' newest leads | Build the sample from synthetic or county-cache data only | sample never contains tenant-owned rows | REPRODUCED |
| S3-21 | P3 | Plan entitlement | /scrapers/{id}/records reads the county cache without quota/payment/Starter-delay checks (AZ-1) | src/api/routes/scrapers.py:1147-1301 | Starter or past_due account | free reads of the shared cache (raise to P2 if the daily scrape is on) | Apply the same gates as POST /jobs | 402 for over-quota/past-due | REPRODUCED |
| S3-22 | P3 | Subscription state | Expired/cancelled/unpaid accounts can mint API keys, create webhook/skip-trace scrapers, use segments (LT-9) | cmd:lt matrix | an ended subscription | paid features after lapse (scraping itself is blocked) | Gate these routes on active subscription | ended account gets 402 | REPRODUCED |
| S3-23 | P3 | Admin step-up | require_admin accepts API keys and non-MFA sessions (AZ-3, AN-4, A-10) | src/api/auth.py:477-535 | an admin API key leak | admin reads without MFA | JWT-only admin reads with MFA session | admin API key gets 403 | REPRODUCED |
| S3-24 | P3 | Plan entitlement | Dialer replay has no plan re-check (AZ-4) | src/api/routes/scrapers.py:1304-1355; src/workers/dialer_outbox.py:64-160 | a downgraded account | replay of a paid channel | Re-check plan in route and outbox | downgraded replay refused | CONFIRMED |
| S3-25 | P3 | Quota race | AI-connector monthly job limit count-then-insert (AZ-6) | cmd:code trace | concurrent requests | a few jobs over limit | Lock or atomic counter | concurrent starts respect the limit | CONFIRMED |
| S3-26 | P3 | Session lifetime | No absolute session lifetime (AN-2, A-6) | cmd:harness s7 (90-day-old family refreshed) | a stolen refresh token | indefinite session | Absolute family max age | refresh after max age = 401 | REPRODUCED |
| S3-27 | P3 | Password oracle | change-password has no per-account throttle (AN-3, A-7) | cmd:harness s4 (15 x 400, no 429) | a stolen session | password guessing (while S3-04 open) | Per-account throttle | 429 after N failures | REPRODUCED |
| S3-28 | P3 | MFA brute force | /auth/mfa/enable lacks the failure lockout (AN-8) | src/api/routes/auth_helpers/mfa.py:53-120 | stolen session + abandoned setup | MFA secret guessing | Apply the A-3 lockout | 6th bad code = 429 | CONFIRMED |
| S3-29 | P3 | Live stream | Open SSE log stream continues after logout up to 30 min (LT-3) | cmd:lt harness | stolen session | log lines after revocation | Re-check session family per lease renewal | stream closes after logout | REPRODUCED |
| S3-30 | P3 | Download tokens | Emailed 48 h links and 60 s tokens reusable (LT-10, AN-11) | src/api/download_tokens.py:21-35 | a leaked link | repeat downloads | Single-use jti or shorter TTL; fix docstring | second use = 401 | CONFIRMED |
| S3-31 | P3 | Errors / headers | Unhandled 500 lacks CORS and security headers (AN-5, IO-12, F-16) | main.py:105-112 | any 500 | browser cannot read ref id; headers missing | Handle in middleware order | 500 carries headers | REPRODUCED |
| S3-32 | P3 | Transport | No HSTS on api.bridgeleads.io (AN-6, IO-11, F-15) | live: api response headers | network attacker | downgrade on first visit | Add HSTS at app or Cloudflare | header present | REPRODUCED |
| S3-33 | P3 | Verbose errors | 422 echoes input incl. passwords (IE-6, IO-14, CX-12, E-6) | main.py:100-111; live 422 | any caller | sensitive input reflected | Custom handler without input values | 422 body has no input | REPRODUCED |
| S3-34 | P3 | Input validation | page 2^62 returns 500; non-UUID ids 500 and logged raw (IE-4, IE-5, LT-8) | cmd:ie18 probes | any caller | log noise, error paths | Bound page; UUID path types | 422 not 500 | REPRODUCED |
| S3-35 | P3 | Input validation | No body-size cap (8 MiB JSON accepted); webhook parsers unbounded (IE-7, CX-6) | src/api/routes/webhooks.py:78; src/api/routes/billing.py:1753 | any caller | memory pressure | Body-size middleware; capped webhook reads | 413 over the cap | REPRODUCED |
| S3-36 | P3 | Input validation | Custom date range has no maximum span (IE-8, B-8) | src/api/schemas.py:429-457 | an account | long scrapes, load | Max span validator | 422 over max | CONFIRMED |
| S3-37 | P3 | Log redaction | Redaction absent on child loggers and worker; no traceback scrubbing; access_token/sig not matched (IO-13, F-23, F-27, CX-9, AS-14) | src/utils/logger.py:30-60; cmd:node regex test access_token= RAW | log access | secrets or PII in logs | Install filter on root and worker; scrub exc_info; add URL query redaction | secrets absent from every logger incl. worker | REPRODUCED |
| S3-38 | P3 | Log hygiene | Raw provider and webhook response bodies logged (IE-11, E-5) | src/workers/webhook_delivery.py:420-423; src/workers/skip_trace.py:649-657 | provider or customer endpoint | PII, log forging | Log status and length only | body not in logs | CONFIRMED |
| S3-39 | P3 | Provider double spend | Tracerfy 5xx or dropped connection re-sends the batch (BI-6) | src/workers/skip_trace.py:635-638; src/workers/skip_trace_dispatcher.py:891-913 | provider fault | operator charged twice | Idempotency key or treat as unknown and reconcile | 5xx after send does not resubmit | CONFIRMED |
| S3-40 | P3 | Webhook trust | Subscription event without id skips the Stripe re-read (BI-4) | src/api/routes/billing.py:2141-2173 | Stripe signing secret | forged body applied (Starter to Agency) | Refuse events without an id | id-less event ignored | REPRODUCED |
| S3-41 | P3 | Billing lifecycle | Disputes and refunds change nothing; livemode not checked; dup-sub alert misses usual order (BI-5, BI-8, BI-3, B-6, B-7) | src/api/routes/billing.py:2032-2052 | a chargeback | plan kept after chargeback | Handle dispute/refund; check livemode; alert on either order | dispute flags account | REPRODUCED |
| S3-42 | P3 | RLS defense in depth | users_app policy USING true with table-wide UPDATE; system policies USING true; FORCE script aborts (IO-6, IO-7, IO-8) | cmd:local role test set tenant B is_admin as app role | an app-layer predicate bug | cross-tenant write incl. is_admin | Row policy on users by GUC; column grants; fix apply_rls_force.sql | app role cannot update another user row | REPRODUCED |
| S3-43 | P3 | Supabase exposure | external_source_health without RLS or anon revoke (IO-9, S-11) | cmd:alembic upgrade head then pg_class relrowsecurity=false; alembic/versions/083_external_source_health.py | a Supabase anon key | operational data readable | Enable RLS, revoke anon | anon select denied | REPRODUCED |
| S3-44 | P3 | Transport | DB TLS require without cert verification; migrate path not forcing TLS (IO-10) | src/db/session.py:27-44; alembic/env.py:22-24 | network position | MITM of DB traffic | verify-full with CA | connect fails with wrong CA | CONFIRMED |
| S3-45 | P3 | Secrets at rest | PhoneBurner OAuth tokens and webhook HMAC secrets stored plain in deliver JSON (AS-8) | src/db/models.py:413,505 | DB read | third-party account access | Encrypt with the field key | stored value is ciphertext | CONFIRMED |
| S3-46 | P3 | Worker integrity | Worker loads and writes by id without owner checks (CX-10, CX-11, T-1..T-4) | src/workers/tasks.py:481-483; src/workers/batch_tasks.py:50-114 | a forged task message | cross-tenant worker write | Re-derive and assert owner in tasks | mismatched owner task refused | CONFIRMED |
| S3-47 | P3 | Frontend CSP | unsafe-inline, unsafe-eval, unused origins (FE-1, AN-10, CX-7, W-3) | next.config.ts:41,55-57,78,83 | an injection bug | weaker XSS containment | Nonce-based CSP, drop unused origins | header snapshot test | CONFIRMED |
| S3-48 | P3 | Token exposure | Backend access token readable by page JS (FE-2, CX-8, W-4) | lib/auth.ts:88-96,265 | an XSS | 1 h token theft | Proxy API calls server-side | session JSON has no accessToken | REPRODUCED |
| S3-49 | P3 | Header hygiene | Vercel ACAO * on HTML; localhost in serverActions and CORS prefix (FE-3, FE-4, AN-9, AN-13, W-5, W-6, A-11) | next.config.ts:4-14; main.py:68-82 | none today | future misuse | Remove localhost in prod; exact-match origins | config test | CONFIRMED |
| S3-50 | P3 | Latent DoS | Auth.js password path reachable (AN-7, A-9) | lib/auth.ts:206-225 | after S3-04 fix | shared-IP lockout | Remove unused path | none | CONFIRMED |
| S3-51 | P3 | CI supply chain | Actions not SHA-pinned, Railway CLI unpinned, no secret scan, deps unhashed; FE PAT reads BE history (AS-10, IO-16, IE-13, IO-18, S-2b) | .github/workflows/ci-cd.yml:198,303 | a compromised action or package | CI secret theft | Pin by SHA, hash-lock deps, scope the PAT, add secret scanning | none | CONFIRMED |
| S3-52 | P3 | Fail-closed config | Blind-index key not checked at boot (AS-12) | main.py:49-50; src/workers/__init__.py:175 | missing env | lazy failure | Validate at startup | boot fails without key | CONFIRMED |
| S3-53 | P3 | Artifact hygiene | .dockerignore denylist, compose ports, dormant prod compose publishes dashboards (AS-13, IO-19) | .dockerignore:46; docker-compose.prod.yml:113-164 | local use | accidental exposure | Allowlist-style dockerignore; delete dormant compose | none | CONFIRMED |
| S3-54 | P3 | Push channel CSV | Generic dialer webhook sends raw county text (IE-9, F-02r, accepted by design) | src/workers/dialer_connectors/generic_webhook.py:20-43 | a Sheets/Zapier consumer | formula evaluation in the customer's sheet | Opt-in spreadsheet-safe setting (product decision) | none | CONFIRMED |
| S3-55 | INFO | Docs | Stale BYPASSRLS comments; CLAUDE.md says Upstash (IO-17, AS-11) | src/db/session.py:221; src/config/settings.py:231 | none | misleads reviewers | Update comments | none | CONFIRMED |

## Codex findings

Codex (independent, no access to our findings, isolated worktree, read-only instructions honoured: only
`tasks/audit3/codex.md` written) reported P0 0, P1 0, P2 4, P3 7, INFO 1 (CX-1..CX-12).

| Codex | Ours | Outcome |
|---|---|---|
| CX-1 Tracerfy billing trust (P2) | BI-1 (P2) | Both: S3-15 at P2 |
| CX-2 legacy path-secret route (P2) | BI-7, AS-9 (P3) | Both: higher severity adopted, S3-16 at P2 |
| CX-3 safe_http rebinding (P2, suspected) | IE-2 reproduced a parser differential, IE-12 rebinding (P2/P3) | Both: S3-08 at P2, REPRODUCED |
| CX-4 browser guard fail-open (P2) | IE-10, E-4 (P3) | Both: higher adopted, S3-14 at P2 |
| CX-5 --no-sandbox (P3) | IO-5 (P2) | Both: S3-13 at P2 |
| CX-6 webhook parsers unbounded (P3) | IE-7 (P3) | S3-35 |
| CX-7, CX-8 CSP, token in JS (P3) | FE-1, FE-2 | S3-47, S3-48 |
| CX-9 URL secrets in logs (P3, suspected) | none | Codex-only: driver verified the redaction gap (access_token=, sig=, X-Amz-Signature= pass unredacted; cmd test), found no secret-bearing scraper URL today; adopted at P3 in S3-37 |
| CX-10, CX-11 worker by-id writes (P3) | audit #2 T-1..T-4 | S3-46 |
| CX-12 verbose 422 (INFO) | IE-6, IO-14 (P3) | S3-33 at P3 |

Codex missed: the public GHCR token exposure (not visible in code), the download-route revocation bypass (S3-07), the
CGNAT limiter reproduction (S3-04), and the origin bypass (S3-06). No Codex finding was rejected.

## Independent verification

- Every leaf report re-checked by the driver with `report-check.mjs` (REPORT_OK for all 9 including Codex); each
  report's own negative controls are listed in its sections.
- Driver re-measured the P0 end to end: anonymous GHCR access (tags/list 200 vs nonexistent-repo control 403) before,
  token status ACTIVE via Cloudflare verify (value never printed), then 401 after deletion; package 401/403 after
  making it private; Railway service sources read to rule out a GHCR dependency; API health 200 after each change.
- Driver verified the only Codex-only item (CX-9) in code and with a regex control.
- Cross-leaf agreement: S3-07 found independently by 3 leaves (live, authn, authz); S3-08 by leaf 1.8 and Codex;
  S3-13/S3-14 by leaf 1.5/1.8 and Codex.
- Audit #2 fixes re-verified as still in place: N-02, N-03, A-1 (except S3-07), A-2..A-5, C-1, D-1, E-1..E-3, F-03, F-08.

## Remaining risks

- S3-03 and S3-04 (P1) are open; S3-04 depends on S3-06 (infrastructure).
- Reads with the leaked Cloudflare token before 2026-09-27 cannot be ruled out (Cloudflare does not log reads); its
  scope was DNS only, and no DNS change by it after 2026-03-18 exists.
- Production values not readable here: `ENTITLEMENT_ENFORCEMENT`, `ENABLE_DAILY_SCRAPE` (sizes S3-21),
  `TRACERFY_LEGACY_PATH_ENABLED`, `SKIP_TRACE_DAILY_ROW_CAP`; production DB role attributes, grants, and FORCE RLS state.
- No authenticated production testing was done (by rule); cross-tenant results come from the real app on isolated DBs.

## Manual actions

1. Rotate the production admin password and delete the plaintext copies (S3-17).
2. Make Cloudflare the only ingress (Tunnel or Authenticated Origin Pulls) so S3-04 can be fixed (S3-06).
3. Add protection rules and branch policy to the GitHub `production` environment; delete `RAILWAY_TOKEN_PRODUCTION` (S3-10).
4. Decide F-02r (opt-in spreadsheet-safe webhook payloads) (S3-54).
5. Set `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP` (and `SKIP_TRACE_DAILY_CREDIT_CAP`) in Railway production; the #364 caps are off until set (S3-03, S3-12).
6. Optional: confirm `ENABLE_DAILY_SCRAPE` in production so S3-21 can be sized.

## Not verified

- Live authenticated production behaviour (no production credentials by rule).
- Production DB role attributes, grants, FORCE RLS state, Railway variable values, R2 bucket CORS/public access.
- Tracerfy's charging behaviour on 5xx (S3-39) and Stripe live portal/coupon configuration.
- Linux-only dependency `uvloop` (pip-audit ran on Windows).
- Whether anyone read data with the Cloudflare token before deletion.

---

# Audit #5 (2026-09-28): delta `ee601b55..29afc82e` + open-queue re-confirmation

Scope chosen by the owner: every source change since audit #4 (backend: #373 local-env, #375 run
eligibility, #376 dispatcher interval, #379 migration 105; frontend bridgeleads-web #165-#167), all
18 checks applied to that delta, plus re-confirmation of every open finding at `29afc82e`. Claude and
Codex reviewed independently (`tasks/audit5/delta-claude.md`, `tasks/audit5/delta-codex.md`). No
production data was read or changed; the only production access was a boolean-only read of one flag.

**Placement note:** this section is appended at the end so it merges cleanly with PR #374, which
inserts the Audit #4 section at the top of this file.

## Status of the unmerged audit #4 fixes

- **PR #374** (S3-03 / S4-01, trial skip-trace gate): OPEN, NOT merged, still merges into current main
  with no conflict. Until it merges, S3-03 (P1) is LIVE in production.
- **PR #378** (S4-03, batch/segment CSV exports in the `export` zone): OPEN, NOT merged, merges cleanly.

## New findings

| ID | Sev | Category | Location | Evidence | Prereq | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| D5-01 | P3 | Plan entitlement | src/workers/scheduler_helpers/dispatch.py:126,379; src/workers/batch_tasks.py:147; src/api/routes/batches.py:274 | `AI_JOB_LIMITS` read only in `config_eligibility.py`; scheduled and batch paths apply the account rule only | a Starter/Pro account with an ai-mode county | more "AI" runs than the plan lists; ai mode is template detection (no LLM spend), record quota still enforced | apply the AI monthly cap in the scheduler and batch dispatch through `config_run_eligibility` | scheduled/batch run over the AI cap is refused | CONFIRMED, pre-existing |
| D5-02 | P3 | Fail-open default | src/config/settings.py:194 | `ENTITLEMENT_ENFORCEMENT: bool = False`; production api AND worker read `true` today (boolean-only read, 2026-09-28) | a new service or env that misses the variable | county/record-type gates audit-only there | default `True` when `ENVIRONMENT=production`, or refuse to boot in production without it set | production settings without the var enforce | CONFIRMED (hardening); Codex CX5-01 rated this P1 from the code default alone, rejected as a live P1 because production sets it |
| D5-03 | P1 (Codex rating, adopted) | Browser egress containment | src/scrapers/base_scraper.py (whole browser) | 5a Codex diff review r1: after 5a, TCP channels no Playwright route sees remain (WebRTC TURN-over-TCP/TLS, speculative preconnect), and every browser request is check-then-use: Python resolves, Chromium resolves again (DNS rebinding) | hostile county page content, or a rebinding DNS name on a scraped link | a request from the worker, which holds every secret, to an internal address | route Chromium through a local validating egress proxy (`--proxy-server` to an in-worker CONNECT proxy that resolves once, checks the IP, and dials that IP), or an egress firewall on the worker; relates to S3-13 | a rebinding name and a TURN-over-TCP candidate to loopback both refused at the proxy | CONFIRMED residual, pre-existing, needs a design decision |

## Codex vs Claude

| Codex | Claude | Outcome |
|---|---|---|
| CX5-01 P1 entitlement enforcement off by default | not flagged | Re-verified: production api and worker have it ON. Downgraded to D5-02 P3 (fail-open default), reasoning recorded above. This is the known repo-only-review blind spot (flag defaults are not production values). |
| CX5-02 P2 dialer outbox + `safe_http` validate then fetch on an unpinned Requests session | S3-08 PRESENT | Agree. Read `src/workers/dialer_outbox.py:40,255` and `src/utils/safe_http.py:122-134`: validation resolves, then a separate `requests.Session` resolves again (DNS-rebinding TOCTOU). Generic webhook delivery now uses `pinned_session`. S3-08 narrowed to these two paths, P2. |
| CX5-03 P1 Playwright guard fails open | S3-14 PRESENT (P2 in audit #3) | Present, but the P1 premise does not hold: a DNS failure already fails CLOSED (`security.py:189-193` turns the resolver OSError into ValueError), so the generic `except` is reached only by the guard's own internal errors, which no page controls. The live parts of S3-14 are the channels `context.route` never sees: WebSockets and service workers, both REPRODUCED in real Chromium on main during 5a (`tests/test_scraper_egress_guard.py`). **S3-14 stays P2**, with this reasoning. The residual Codex raised during 5a is split out as D5-03. |
| CX5-04 P2 legacy Tracerfy path-secret route | S3-16 PRESENT, mitigated | Agree. `main.py:132` scrubs uvicorn access lines only; Railway's edge still logs the path. Removal needs Tracerfy to send the header (external). |
| CX5-05 P2 PII JSON views in `general` zone | S4-07 PRESENT | Present, **re-rated P3 by consensus** (Codex consult during 5c): every view returns only the caller's own rows (user_id filter + RLS); `general` is 60 req/min per user x at most 500 rows = ~30k decrypted rows/min, while the separate `export` zone already allows 20 x 50,000 = ~1M, so moving the views into `export` changes the per-user decrypt ceiling by under 3% and would 429 interactive browsing (the FE pages at 50). No code change. Optional hardening for the owner: a lower JSON `page_size` cap (a public-API contract change, `le=500` in OpenAPI) or a worker-wide concurrency cap on decrypt-heavy requests. |
| CX5-06 P2 reservation clock before lock | S4-06 PRESENT | Agree. |
| S4-02 FIXED | S4-02 PRESENT | Codex is wrong: `models.py:817-844` is unchanged since audit #4 (only an index was added). A fixed 300 s cooldown IS the S4-02 finding (a cancelled worker can outlive it). Stays P2. |

No confirmed IDOR/BOLA, admin-authz, SQL injection, migration-grant, secret, XSS, CSRF, or CORS issue in the delta (both reviewers).

## Open queue at 29afc82e (after reconciliation and the fix phases)

| ID | Sev | Status |
|---|---|---|
| S3-03 / S4-01 | P1 | FIXED on #374, NOT merged (live in prod) |
| S3-04 | P1 | PRESENT (needs Cloudflare sole ingress, owner) |
| D5-03 | P1 (Codex rating) | FIXED on branch `fix/security-audit5f-egress-proxy` (5f), stacked on 5a, behind `SCRAPER_EGRESS_PROXY_ENABLED` (default OFF): not effective until turned on |
| S3-14 | P2 | FIXED on `fix/security-audit5a-browser-egress` (5a) |
| S3-08 | P2 | FIXED on `fix/security-audit5b-pinned-egress` (5b); PACS / AcclaimWeb / Tracerfy-submit sessions (operator-configured or fixed hosts) remain unpinned, tracked as 5b-ii (P3) |
| S3-15 | P2 | FIXED on `fix/security-audit5d-tracerfy-webhook-trust` (5d) |
| S3-16 | P2 | PRESENT, mitigated; its impact is now bounded by 5d (the webhook body is never read, so a forged webhook can only trigger a provider lookup) |
| S4-02 | P2 | PRESENT (not in the approved phases) |
| S4-03 | P2 | FIXED on #378, NOT merged |
| S4-06 | P2 | PRESENT, DEFERRED: the reservation SQL is inline in `run_scrape_job` and the only tests exercise a copy of it, so a meaningful regression test needs the reservation extracted first (own refactor). Not attacker-reachable (needs a quota-window boundary during lock contention). |
| S4-07 | P3 (re-rated) | PRESENT; no code change, remedy is an owner choice |
| D5-01 | P3 | PRESENT, DEFERRED: the AI-cap evaluator is async/API-side, the scheduler and batch paths are sync; enforcing it there means duplicating the rule or porting the evaluator, for a mode that costs nothing (template detection). Product decision. |
| D5-02 | P3 | FIXED on `fix/security-audit5e-hardening` (5e) |

## Fix phases (Phase 2)

Each phase: its own branch and worktree, regression test first and proven to FAIL on
`origin/main`, Codex diff review until GATE: PASS (every round's findings recorded in the
commit messages), full local suite on its own `_test` database. Nothing is pushed or merged.

| Phase | Branch (head) | Finding | What changed | Regression proof | Codex |
|---|---|---|---|---|---|
| 5a | `fix/security-audit5a-browser-egress` (`8bb274b5`) | S3-14 | context `route_web_socket` guard; `service_workers="block"` on every context; `--disable-quic` + both WebRTC ip-handling switches (headed Chrome ignores the `force-` one); the guard fails closed | `tests/test_scraper_egress_guard.py`, real Chromium: 8/9 fail on main (the 9th is the listener's positive control); REPRODUCED on main: a page opened a WebSocket to a loopback listener and installed a service worker | r1 FAIL (its await P1 was wrong: `connect_to_server` is sync; test-honesty points fixed), r2 PASS |
| 5b | `fix/security-audit5b-pinned-egress` (`b02f2b07`) | S3-08 | `validate_scraping_target` refuses userinfo and a backslash in the authority (REPRODUCED: `http://evil.example\@portal/` validated as portal, dialled evil.example); `safe_http` and the dialer outbox use `pinned_session()` | `tests/test_egress_pinning_s3_08.py`: 8/11 fail on main | r1 PASS (P2s checked: redirect bodies are already closed; mixed-answer refusal is pre-existing policy) |
| 5c | none | S4-07 | no code: re-rated P3 by consensus (numbers above) | n/a | consult agreed P3 |
| 5d | `fix/security-audit5d-tracerfy-webhook-trust` (`7d780354`) | S3-15 | the webhook is a trigger only: download URL and counts come from Tracerfy's own queue record (`GET /v1/api/queues/`); not complete there or unreachable = bounded re-check (5 x 120 s, one chain per queue, Redis claim) then an ops alert, never 'errored'; download host HTTPS-only and pinned to `tracerfy.nyc3` (CDN and origin: 33/33 production URLs, read host-only, 2026-09-28); a malformed body can no longer fail the task (it used to mark the REAL queue errored) | `tests/test_tracerfy_webhook_trust_s3_15.py` 16/22 fail on main at r2; 104 ingest-suite tests pass | r1 FAIL, r2 FAIL, r3 FAIL, r4 FAIL, r5 PASS (+P2 fixed), r6 PASS (+P2 fixed), r7 PASS |
| 5e | `fix/security-audit5e-hardening` (`506f6f83`) | D5-02 | `ENTITLEMENT_ENFORCEMENT` unset + `ENVIRONMENT=production` = True; explicit false still wins | `tests/test_settings.py`: the production-unset test fails on main | r1 PASS (+P2 fixed) |
| 5f | `fix/security-audit5f-egress-proxy` (`d4893dcc`) | D5-03 | `src/scrapers/egress_proxy.py`: in-worker SOCKS5, CONNECT only, ports 80/443/8080/8443, resolves once, refuses if any answer is blocked (incl. mapped/NAT64/6to4 forms), dials the checked sockaddr on its own socket; Chromium launched with it and `<-loopback>` | `tests/test_scraper_egress_proxy.py`: the control proves that WITHOUT the proxy a TURN-over-TCP candidate dials a loopback listener directly (the residual, reproduced); with it, refused. Live 2026-09-28: `atip.piercecountywa.gov` 200 through the proxy in plain and default mode (4 page loads total) | design consult FAIL (HTTP proxy; switched to SOCKS5 and measured TURN routing), r1 FAIL (P1s disproved with evidence, P2s fixed), r2 PASS (+P2s fixed) |

## Commits that landed on main during this audit (`29afc82e..c0b09b7a`)

#380 (machine-readable code on the run-refusal 402s) and #382 (skip-trace pause state,
migration-free) merged while the audit ran. Reviewed: the 402 body carries only the
caller's own account code, message and resume time, through `RunRefusalResponse`; the
pause state is published to Redis and not yet read by any route. Forward note for its
Phase 1c reader: it must read only the caller's own `<user_id>` field (plus `global` /
`account_default`), never iterate the hash. No findings. All five fix branches still
merge cleanly into the new main (`git merge-tree`).

## Unverified in audit #5

- `.env.example` (2 changed lines): a permission rule blocks this session from reading it. It ships in the image (`.dockerignore:31`); the owner should confirm both lines are placeholders.
- D5-03's proxy has run live against one county portal only; every other template must
  be exercised with `SCRAPER_EGRESS_PROXY_ENABLED=true` (staging or a quiet window)
  before it is turned on in production. Portals on a port outside 80/443/8080/8443
  will be refused and logged.
- Everything listed as unverified in audit #3 (live authenticated prod behaviour, DB grants, R2 CORS) was not re-tested; the delta did not touch those surfaces.
