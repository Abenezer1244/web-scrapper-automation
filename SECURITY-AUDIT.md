# BridgeLeads Security Audit, Phase 1 (audit only)

**Date:** 2026-09-25
**Commit audited:** `fc38e620` (tip of `origin/main`, includes PR #356 / migration 101). Frontend: `origin/master` `8332673`.
**Worktree:** `C:/Users/Windows/bl-wt-secaudit2`, branch `chore/security-audit-2026-09-25` (no commits yet).
**Baseline:** the 2026-09-16 audit (`tasks/SECURITY-AUDIT-REPORT-2026-09-16.md`, 0 P0 / 4 P1 / 11 P2 / 13 P3). Every prior
finding was re-checked on current code; 93 backend commits landed since.
**Method:** 6 parallel domain audits (detail files `tasks/audit2-{tenant,billing,egress,auth,frontend,secrets}.md`),
a live two-account IDOR harness on an isolated database (`tasks/audit2-idor-live.md`), low-volume read-only production
probes, a production frontend build + bundle scan, `pip-audit` + `npm audit`, a full-history secret scan of both repos,
and an **independent** Codex review that was not shown any of our findings.
**No production code changed. No credential rotated or printed. No production data written.**

Every secret below is redacted to first-4 + last-3 characters.

---

## 1. Executive security summary

Nothing found in this pass is a P0. No cross-tenant read or write, no authentication bypass, no admin escalation,
no payment-signature bypass.

**Tenant isolation holds, and this time it was tested, not just read.** Two real accounts on the real app: 20 id-bearing
routes x (foreign token, no token) produced 0 cross-tenant reads, 0 cross-tenant writes, 0 unauthenticated accesses, with
positive owner controls. "Already delivered" and skip-trace reuse are tenant-keyed at the schema level.

**Six P1s**, three of them new:

| ID | P1 | Status |
|---|---|---|
| **N-01** | **A live-scoped Cloudflare API token is committed** in `infra/terraform/terraform.tfvars` (since 2026-03-17), and ships inside every Docker image. Missed by the prior audit. | NEW. Code side FIXED (`150421e5`); **ROTATION PENDING (you)** |
| **N-02** | **Plan-change arbitrage:** upgrade is granted immediately but charged on the next invoice; downgrade is credited by Stripe immediately but deferred in the app. Pro -> Agency -> Pro yields Agency at about the Pro price, repeatable monthly. | NEW. **FIXED** (`98473b46`) |
| **N-03** | **Results readable before the plan cap applies:** rows are committed before enrichment and marked OVER_QUOTA only after it, and `GET /jobs/{id}/results` has no status gate. | NEW. Reproduced, **FIXED** (`67382e30`) |
| F-01/F-01b | IP rate limiting is still dead in production (re-proven today: 14/14 forgot-password in <1 min, 10/min zone, 0 x 429). The escalating lockout still never fires. | OPEN |
| F-03 | Webhook SSRF DNS-rebinding TOCTOU (resolve twice, never pin). Now risk-accepted in a code comment. | OPEN |
| F-12 | No per-account Tracerfy spend ceiling. Worse than recorded: **trial accounts are `plan="pro"` and can buy ~1,000 unbillable lookups each**, draining the global cap for paying customers. | OPEN, widened (raised P2 -> P1: Codex and Claude both flag) |

| Severity | Count |
|---|---|
| P0 | **0** |
| P1 | **6** |
| P2 | **16** (B-3 is counted inside the F-12 P1) |
| P3 | **41** |

**Fixed since 2026-09-16:** F-05 (reset email-bomb), F-10 (fallback limiter flush), F-11 (stale Stripe event),
F-13 (BLIND_INDEX_KEY fail-closed, lazily), F-26 (Stripe return URLs), F-08 (inside `safe_http` only), F-02 (PhoneBurner only).

**This report does not declare BridgeLeads secure.** Sections 33-35 list exactly what was not verified.

---

## 2. Architecture reviewed

| Layer | Actual (verified) |
|---|---|
| Frontend | Next.js 16.3.5 + Auth.js (next-auth 5.0.0-beta.32) on **Vercel**; `bridgeleads.io` and `app.bridgeleads.io` are one deployment |
| API | FastAPI on **Railway**, behind **Cloudflare** (`api.bridgeleads.io`); origin also directly reachable (F-28) |
| Auth | Backend JWT bearer (1 h access, rotating refresh) + hashed API keys; Auth.js cookie (7 d) holds the backend tokens server-side |
| Authorization | `get_current_user` + `require_plan` + entitlement gate in `auth.py`; admin = `require_admin` (404) / `require_admin_mfa` |
| Database | PostgreSQL (Supabase-hosted). RLS (belt) + mandatory `user_id` predicate (suspenders). Runtime roles `bridgeleads_app` / `bridgeleads_system`, neither superuser nor BYPASSRLS; migrations as owner |
| Workers / queue | Celery + Redis (Upstash); beat for dispatch, watchdog, canary, skip-trace dispatcher |
| Scrapers | Playwright headless Chromium (`--no-sandbox`) + BeautifulSoup; admin-only connector registration; SSRF route guard |
| Object storage | Cloudflare R2; delivery via revocable download-token links (`API_BASE_URL` set in prod) |
| Payments | Stripe (Checkout, subscription modify, portal, signed webhooks + mig-095 event ledger) |
| Skip trace | Tracerfy (server-side key; webhook `/webhooks/tracerfy[/{secret}]`) |
| Email | Resend |
| Push delivery | Customer webhooks, generic dialer webhook, PhoneBurner |
| CI/CD | GitHub Actions (private repo); push to `main` = Railway deploy + `alembic upgrade head` on boot; `master` = Vercel |
| DNS/CDN | Cloudflare (Terraform-managed, see N-01) |

**Data flow and trust boundaries**

```
Browser --(Auth.js cookie)--> Vercel/Next --(Bearer JWT)--> Cloudflare --> Railway API
   [B1: untrusted client]                               [B2: origin reachable around CF, F-28]
API --(RLS GUC + user_id filter)--> Postgres            [B3: tenant boundary]
API --(Redis broker, ids only)--> Celery worker         [B4: worker re-derives owner from DB]
Worker --(Playwright, SSRF route guard)--> county portals   [B5: UNTRUSTED input: HTML/text]
Worker --> enrichment (county GIS / assessor via safe_http) [B5]
Worker --> Tracerfy (paid)  <-- Tracerfy webhook (shared secret) [B6: spend + replay boundary]
Worker --> results --> CSV/XLSX/JSON export (sanitized) --> R2 --> signed download link
Worker --> customer webhook / dialer (SSRF-checked, egress) [B7: user-chosen destination]
Stripe --(HMAC)--> /billing/webhook                          [B8]
```

---

## 3. P0 findings

**None.** Explicitly checked and not found: cross-tenant read/write, auth bypass, admin escalation, Stripe signature
bypass, client-trusted price/plan/quota, secret in the client bundle, browser-held DB credential.

---

## 4. P1 findings

### N-01 [P1] Cloudflare API token committed to the repository and baked into every image
- **Category:** secret exposure. **Component:** `infra/terraform/terraform.tfvars` line 1.
- **Evidence:** `cloudflare_api_token = "AwLQ...zyR"` (40 chars), added in `579dec50` (2026-03-17, "fix: resolve all ruff
  lint errors"), still tracked at `fc38e620`, present on `origin/main` and many remote branches. `.gitignore:60` lists
  the file but was added after it was tracked, so it never applied. `.dockerignore:44` excludes only
  `infra/terraform/.terraform`, and `Dockerfile:62` is `COPY . .`, so the token is inside every Railway/GHCR image.
  A UTF-8 BOM before the key (`\357\273\277cloudflare_api_token`) is why anchored regex scans, including the prior
  audit's, missed it. `main.tf` scopes it to DNS + R2 + WAF.
- **Prerequisites:** read access to the repo, any clone/worktree (incl. the college-managed OneDrive folder), any
  built image, or any CI token with repo read (the frontend's `BACKEND_SCHEMA_TOKEN` PAT).
- **Impact:** DNS takeover of `bridgeleads.io` (phishing/password-reset interception, MX injection), WAF disable,
  read/write of R2 export buckets holding every tenant's lead CSVs.
- **Mitigating:** repo is private, one collaborator. Validity was **not tested** (doing so would use the credential).
- **Remediation:** (1) YOU roll the token in Cloudflare now; (2) review Cloudflare audit log back to 2026-03-17;
  (3) `git rm --cached infra/terraform/terraform.tfvars`, add `infra/` to `.dockerignore`, supply the token via
  `TF_VAR_cloudflare_api_token`; (4) history rewrite only with your approval (rotation makes it unnecessary);
  (5) add a BOM-tolerant, case-insensitive secret scan (gitleaks) to CI.
- **Regression test:** CI secret-scan step; test that `infra/terraform/terraform.tfvars` is not tracked.

### N-02 [P1] Plan-change arbitrage: Agency entitlement at about the Pro price, repeatable
- **Category:** billing / entitlement. **Component:** `src/api/routes/billing.py:1510-1516`, `src/api/billing_entitlement.py:288-303`.
- **Evidence:** `change-plan` modifies the subscription with `proration_behavior="create_prorations"`: the upgrade
  charge waits for the next invoice. `apply_subscription_state` grants an upgrade immediately (`user.plan = plan`)
  but parks a downgrade in `pending_plan` until the quota boundary. Stripe credits the unused Agency time the moment
  the downgrade is applied.
- **Exploit:** Pro subscriber -> `POST /billing/change-plan` Agency -> wait for the webhook -> change back to Pro.
  Stripe nets roughly the Pro price; the app keeps `plan=agency` (unlimited-tier records, Agency features) until the
  period boundary. Repeat every period. Annual variant defers the upgrade charge up to a year.
- **Remediation:** charge upgrades before granting (`proration_behavior="always_invoice"` + grant only on
  `invoice.paid`), and apply downgrades in Stripe at period end (subscription schedule) so both systems agree.
- **Regression test:** entitlement-state test: upgrade then downgrade in the same period ends on the paid tier, and
  an upgrade with an unpaid proration invoice does not grant.
- Needs Stripe test-mode confirmation of the annual `cancel_at_period_end` sub-variant (noted in `audit2-billing.md`).

### N-03 [P1, CONFIRMED, FIXED in `67382e30`] Results readable before the plan cap marks the excess
- **Category:** quota bypass. **Component:** `src/workers/tasks.py:976-1132` (insert + commit),
  `:1542-1555` (inline enrichment, commits repeatedly), `:1753-1880` (reservation + OVER_QUOTA marking);
  `src/api/routes/jobs.py:414-497` (`GET /jobs/{id}/results`, no job-status gate).
- **Evidence:** rows commit at `tasks.py:1132`; enrichment then runs for minutes and commits; only afterwards does the
  single-statement reservation mark rows beyond the plan's remaining quota `OVER_QUOTA`. The results route hides
  `OVER_QUOTA` rows (`lead_actionability.py:86-89`) and requires an address, but does not gate on status, so during
  `enriching` every addressed row is readable. Cancelling then refunds the reservation (`tasks.py:2384-2396`).
- **Exploit:** Starter (50/period) runs a large tax-delinquent or pre-foreclosure scrape (addresses come from the
  source), pages `/jobs/{id}/results` during `enriching`, then cancels.
- **Mitigating:** record types whose address only arrives through enrichment expose fewer rows mid-run.
- **Reproduced in Phase 2** on the isolated rig: `enriching`, `scraping`, `failed` and `cancelled` runs all listed
  and downloaded their unbilled rows before the fix; none do after. `/download` and `/export-url` had the same hole.
- **Remediation:** reserve before the rows become visible (move reservation ahead of the first commit), or gate
  `/results` (and `/records`, segments, batch leads) to `status == done` or to rows inside the reserved grant.
- **Regression test:** a job mid-`enriching` with rows beyond quota returns only the granted rows.

### F-01 / F-01b [P1] IP rate limiting dead in production; escalating lockout never fires (OPEN)
- Re-proven live today: 14 forgot-password requests in under a minute (zone `auth` = 10/min) -> 14 x 200, 0 x 429.
- Code unchanged: `rate_limit.py:54-60` lacks `100.64.0.0/10`; `start.sh:97` has no `--proxy-headers`;
  `auth_hardening.py:484,491` email counter TTL capped at 15 min so 4 guesses / 15 min never lock.
- Affects: login, refresh, register, verify-email, reset, change-password, MFA setup, admin funnel, both webhooks.
- **Hard ordering (unchanged from 09-16):** close the Cloudflare bypass (F-28) first, then trust `CF-Connecting-IP`.
  Never `--forwarded-allow-ips=*`. Independently flagged by Codex (P1).

### F-03 [P1] Webhook SSRF DNS-rebinding TOCTOU (OPEN, risk-accepted in code)
- `security.py:146-170` validates resolution; `webhook_delivery.py:319` resolves again at connect. The code comment at
  `webhook_delivery.py:253-279` accepts the risk on four invariants; invariant 4 is weaker than stated (E-3 below).
- Blind SSRF (response body never shown), redirects disabled. Codex independently P1.
- Fix: resolve once, validate every A/AAAA, connect to the pinned IP with SNI/Host set.

### F-12 [P1] No per-account Tracerfy spend ceiling; trials can spend (OPEN, widened)
- Only a global rolling-24h row cap (`skip_trace_dispatcher.py:72-113`), set to 1000 in prod but defaulting to 0
  (off) in code (`settings.py:365`), checked once per tick while a tick may submit up to 2 x 5,000 rows; counts rows
  not credits (advanced lookup = 2 credits); one tenant can consume all of it (B-4).
- **B-3:** trials are created `plan="pro"` (`registration.py:188`); skip-trace gates exclude only Starter, and
  billing requires an active subscription, so a trial can generate about 1,000 lookups that are never billed, and a
  handful of throwaway trials can exhaust the global cap for paying customers.
- Codex independently P1. The per-account cap is in flight (Phase 1b-1b, owned by another session); Phase 2 must
  coordinate rather than collide.

---

## 5. P2 findings

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| A-1 | Sign-out revokes nothing server-side; FE never calls `/auth/logout`, backend logout only blacklists the access token | FE `lib/api.ts:284-316`; `routes/auth.py` logout | Call `/auth/logout` from `signOutSafely`, revoke the refresh family |
| A-2 | A 1 h session can mint a permanent API key with no re-auth and no owner email; access token is readable by page JS | `routes/auth.py:447`; FE `lib/auth.ts:233` | Require password / fresh MFA; email the owner; show key age |
| A-3 | MFA verify: no failure lockout, only 10/min per user with 3 valid codes | `auth_hardening`, MFA routes | Escalating per-user MFA lockout |
| A-4 | MFA enrollment needs no password and is reachable by API key (hostile enrollment locks owner out) | MFA setup/enable routes | Require password + JWT session |
| A-5 | No refresh-token reuse detection (family revocation) | `login.py` refresh | Revoke the family on reuse |
| B-3 | Trial accounts can buy unbillable Tracerfy lookups (folded into F-12 P1 above) | `registration.py:188` | Gate skip-trace on `first_paid_at` |
| B-4 | Global cap: once per tick, rows not credits, one tenant can take all | `skip_trace_dispatcher.py:72-113` | Per-account credit budget |
| B-5 | Tracerfy webhook `rows_uploaded` overwrites stored value and decides billing; download host check allows any DO region + `http` | `tracerfy_ingest.py:437-452,832-840`; `skip_trace_usage.py:611-622` | Trust stored value; pin host + https (needs webhook secret to exploit) |
| E-1 | Customer webhook response read unbounded on the shared queue: gzip bomb OOM-kills the worker (other tenants' scrapes die), slow-drip holds a slot 60 min x 4 attempts | `webhook_delivery.py:319` | `_read_capped`, task time limit, dedicated queue |
| F-02r | Generic dialer webhook + job webhook still push raw county text (Zapier -> Google Sheets evaluates formulas) | `generic_webhook.py:20-43`, `webhook_delivery.py` payload | Sanitize text fields at the payload builder |
| F-07 | Chromium `--no-sandbox` on attacker-controlled county HTML; worker env holds owner DSN (S-2) | `base_scraper.py:281` | Enable sandbox (seccomp profile) or isolate renderer |
| F-28 | Cloudflare bypassable: Railway origin answers directly (per team comment `password.py:123-129`; not re-probed today) | prior live probe | CF Tunnel / Authenticated Origin Pulls |
| S-2 | Owner DSN (`DATABASE_URL_MIGRATE`, bypasses RLS, DDL) present on api, worker, beat because they migrate on boot | Railway service vars (names) | Run migrations in a one-shot release job only |
| S-3 / F-14 | Repo-level `DATABASE_URL_SYNC` is a working prod DSN readable by PR test jobs; `RAILWAY_TOKEN_PRODUCTION` unused; no environment protection rules | `gh api` names only | Move to protected environment; delete unused token |
| S-4 | CI runs bare `alembic upgrade head` without the advisory lock, racing Railway's locked `migrate.py` (dormant while Actions is blocked) | workflow file | Use `scripts/migrate.py` or remove |
| S-5 | Production credentials on disk in the college-managed OneDrive folder (`.rls-cutover-secrets`; an admin login URL+password in `scripts/audit_out_ui/run3_thurston_whatcom.log`) | files not read beyond names/pattern | Move out of OneDrive, rotate the admin password |
| S-2b | Frontend PAT `BACKEND_SCHEMA_TOKEN` has repo read, so FE PR jobs can read N-01 | secrets names | Scope to a read-only artifact |

---

## 6. P3 findings

- **T-1..T-4** worker-side writes by id only: config load without owner (`tasks.py:466-468`), dispatcher result
  updates (`skip_trace_dispatcher.py:1189-1196,1290-1297`), NTS matcher (`nts_matcher_task.py:336-348,400-415`),
  `dispatch_batch_run` owner check (`batch_tasks.py:50-80`). Not request-reachable.
- **T-5** mig 101: `pending_skip_trace_rows.action_id` lacks a `(action_id, user_id)` composite FK (`101:516-519`).
  **Must be fixed before any Phase 1b-2 writer ships.** T-6 `quote_id` globally unique (info).
- **C-1** `GET /scrapers/connectors?include_all=true` is anonymous and returns the 6 `down` connectors plus public
  county URLs, GIS endpoints, health (live: 24 default, 30 with the flag; no internal hosts). Codex P2 / frontend P3;
  held at **P3** because only public county URLs and operational state are exposed.
- **D-1** Emailed download token (48 h, `status.py:50`) path loads the user without `is_active`
  (`jobs.py:1376`); logout-all revocation IS honoured; no code path sets `is_active=False`, so only an
  out-of-band deactivation leaves links live. Also `?token=` accepts a full access token (A-8).
- **E-2** webhook network errors log `str(exc)` incl. the full URL/query (`webhook_delivery.py:326-330`); URL is also a Celery arg in Redis.
- **E-3** SSRF blocklist allows IPv4-embedding IPv6 forms. **Verified against the real `_ip_is_blocked`:** allowed
  `64:ff9b::a9fe:a9fe` (NAT64 metadata), `2002:a9fe:a9fe::1` (6to4), `::a9fe:a9fe`, Teredo, `fec0::1`, `192.88.99.1`.
  Exploitable only where the network translates them.
- **E-4** browser SSRF guard: WebSockets and service workers not intercepted; guard fails open on unexpected errors (`base_scraper.py:458-460`).
- **E-5** raw provider / customer-webhook error bodies logged (PII, log forging). **E-6** default 422 echoes input (incl. password > 72 chars).
- **F-15** no HSTS on `api.bridgeleads.io` (live, today). **F-16** unhandled 500 bypasses CORS + security headers (`main.py:105`).
- **F-22** Tracerfy path-secret route still enabled by default (`settings.py:345`); B-6/new path: global exception handler logs `request.url.path` with the secret (`main.py:103-110`).
- **F-23 / F-27** redaction gaps: no traceback (`exc_info`) scrubbing (`logger.py:31-58`); access-log filter only on `uvicorn.access`, untested.
- **F-25** `fc00::/7` not in trusted proxies (latent).
- **A-6..A-11** no absolute session lifetime; change-password usable as a password oracle; download links not
  single-use; unused password branch in Auth.js `authorize()` (lockout DoS on shared Vercel IPs once F-01 is fixed);
  admin gate accepts API keys for non-MFA admin reads; CORS localhost prefix match.
- **B-6..B-8** no dispute/refund webhook handling (tier kept after chargeback); `livemode` never checked; empty
  `STRIPE_PRODUCT_*` collapses the product map; custom date range has no maximum span.
- **W-1..W-6** FE admin nav gated on `plan === "agency"` not `is_admin` (backend enforces); route ids not
  `encodeURIComponent`-ed; CSP `unsafe-inline`/`unsafe-eval` + unused origins; access token in page JS (see A-2);
  Vercel `ACAO: *` on HTML (not exploitable); localhost `serverActions.allowedOrigins`.
- **S-6..S-12** stale RLS comments (`settings.py:229-235`, `session.py:218-223`); blind-index check lazy not at boot;
  Actions pinned by tag not SHA; ignore-file gaps; compose ports; `external_source_health` has no RLS in migrations;
  pinning notes.
- **G-1** no behavioural two-tenant test for `/segments/*` (only SQL-structure tests).

### Status of every 2026-09-16 finding on `fc38e620`

| ID | 09-16 sev | Now | Evidence |
|---|---|---|---|
| F-01 | P1 | **OPEN** | live today: 14/14 x 200, 0 x 429; `rate_limit.py:54-60` |
| F-01b | P1 | **OPEN** | `auth_hardening.py:484,491` |
| F-02 | P1 | **PARTIAL** (PhoneBurner fixed; generic/job webhook raw -> F-02r P2) | `phoneburner.py:81-101`; `generic_webhook.py:20-43` |
| F-03 | P1 | **OPEN** (risk-accepted in code) | `webhook_delivery.py:253-279,319` |
| F-04 | P2 | **OPEN** (same root as F-01) | `login.py:47` |
| F-05 | P2 | **FIXED** (per-address once_per 5 min) | `password.py:150-152` |
| F-06 | P2 | **OPEN**: `_dmarc.bridgeleads.io` NXDOMAIN (live DoH, today) | Cloudflare DNS |
| F-07 | P2 | **OPEN** | `base_scraper.py:281` |
| F-08 | P2 | **PARTIAL** (fixed in `safe_http`; 6 call sites still uncapped, incl. E-1) | `safe_http.py:52-96` |
| F-09 | P2 | **OPEN**: `/jobs/{id}/download`, `/export-url`, batch downloads have no limiter | `audit2-auth.md` rate-limit matrix |
| F-10 | P2 | **FIXED** | `rate_limit.py:124-172` |
| F-11 | P2 | **FIXED** (re-raise instead of stale body) | `billing.py:2102-2132` |
| F-12 | P2 | **OPEN, raised to P1** (B-3/B-4) | `skip_trace_dispatcher.py:72-113` |
| F-13 | P2 | **FIXED** (fail-closed on first use, not at boot: S-7 P3) | `crypto.py` |
| F-14 | P2 | **OPEN** (S-3) | `gh api` names |
| F-15 | P3 | **OPEN** (live: no HSTS on api) | section 28 |
| F-16 | P3 | **OPEN** | `main.py:105` |
| F-17 | P3 | **OPEN**: `exports.bridgeleads.io` NXDOMAIN | live DoH |
| F-18 | P3 | **PARTIAL** (stale text at `settings.py:229-235`, `session.py:218-223`) | S-6 |
| F-19 | P3 | **OPEN**: no MX on `bridgeleads.io` | live DoH |
| F-20 | P3 | **OPEN**: no apex TXT/SPF | live DoH |
| F-21 | P3 | **OPEN**: grants still only inside the create-role branch | `test_rls_isolation.py:56-77` |
| F-22 | P3 | **PARTIAL** (header route + access-log scrub added; path route on by default; new exception-log path) | `settings.py:345`, `main.py:103-110` |
| F-23 | P3 | **OPEN** | `logger.py:31-58` |
| F-24 | P3 | **PARTIAL** (`.env.check` untracked + expired; S-5 new) | S-5 |
| F-25 | P3 | **OPEN** | `rate_limit.py:54-60` |
| F-26 | P3 | **FIXED** (`redirectToStripe` host allowlist) | FE `lib/api.ts` |
| F-27 | P2 | **PARTIAL** (also matches Tracerfy path now; only on `uvicorn.access`; untested) | `main.py:114-129` |
| F-28 | P2 | **OPEN** per team comment `password.py:123-129`; not re-probed | section 34 |

---

## 7. Exposed credential findings

| Item | Location | Exposure | Status |
|---|---|---|---|
| Cloudflare API token `AwLQ...zyR` | `infra/terraform/terraform.tfvars:1` | repo (private), all branches, every Docker image, every clone | **LIVE-SCOPED, rotate** (N-01) |
| Cloudflare zone/account ids, Railway IP | same file lines 2-4 | identifiers, not credentials | informational |
| Prod DSN `DATABASE_URL_SYNC` | GitHub repo-level secret | PR test jobs can read | S-3 |
| Owner DSN `DATABASE_URL_MIGRATE` | api/worker/beat env | any RCE in those services | S-2 |
| Admin login URL + password | `scripts/audit_out_ui/run3_thurston_whatcom.log` (untracked, OneDrive; also local-only stash `568f67f3`) | local disk / OneDrive sync | S-5, rotate admin password |
| Role passwords | `.rls-cutover-secrets` (untracked, not read) | OneDrive sync | S-5 |
| `.env.check` Vercel OIDC | untracked, never committed, expired | none | closed |

**Client bundle:** production build of `origin/master` + 20 live chunks: no `sk_`/`rk_`/`whsec_`/DSN/JWT/Resend/
Tracerfy/`AUTH_SECRET`/Railway hosts; the only `NEXT_PUBLIC_*` is `NEXT_PUBLIC_API_URL` (intended public); no source
maps served (20 x 404). **Database credentials are server-only.**

**Public env files:** `.env`, `.env.local`, `.env.production`, `.env.development`, `.env.backup`, `.env.old`,
`.env.bak`, `.env.example`, `.git/config`, `.git/HEAD` on all three hosts: api -> 404; Vercel hosts -> 307 to
`/login` (auth gate), never the file. Vercel builds only `.next`; `.env*` are gitignored in the FE repo.

---

## 8. Git secret-history findings

- **Scope:** all refs + reflogs; backend 2,233 commits / 6,553 blobs, frontend 757 commits / 1,984 blobs; case-
  insensitive, BOM-tolerant regexes (gitleaks/trufflehog not installed). Values redacted in all output.
- **Found:** N-01 only (type Cloudflare API token, commit `579dec50`, path `infra/terraform/terraform.tfvars`,
  validity not tested, remediation: rotate, then untrack). **Frontend history: clean.**
- **The prior audit's "history clean" was wrong** for this token (upper-case / vendor-prefix patterns + BOM). No other
  historical credential found. No history rewrite performed.

---

## 9. Authentication findings

JWT HS256 with pinned algorithm, `aud`/`iss` checked, 1 h access + single-use rotating refresh; bcrypt(12) direct;
password-reset tokens distinct audience, single-use, revoke all sessions; logout-all revokes refresh tokens and the API
key (the 7-day Auth.js cookie cannot re-mint after it). Auth.js cookies: `__Host-`/`__Secure-`, HttpOnly, Secure,
SameSite=Lax. Weaknesses: A-1..A-5 (P2), A-6..A-9 (P3), F-01/F-01b (P1). Detail: `tasks/audit2-auth.md`.

## 10. Authorization findings

75 routes enumerated from the live app object (69 business + `/health`, `/ready` + 4 docs routes, the docs routes
404 in prod). Full matrix: `tasks/audit2-tenant.md`. Every tenant route filters on `user_id`; 39 also use the RLS
session, 30 do not (correction to the prior report's "69/69 RLS"), and none of the 30 leaks (self-service `users` rows
filtered on `User.id == current_user.id`). No request schema accepts `user_id`, `plan`, `records_used` or quota
(`extra="forbid"`). Plan gates are server-side in `auth.py` for JWT and API-key callers alike.

## 11. Cross-account isolation results

Live two-account harness (real app, isolated `bridgeleads_secaudit2_test`, **RLS bypassed** so the app-layer predicate
is tested alone): 20 id-bearing routes incl. job view/results/logs/download/export-url/cancel, scraper
get/patch/csv-layout/records/dialer-replay/delete, batch get/download/leads/runs/run-download/run-leads, notification
read. **A -> B: all 404. Anon: all 401. Owner controls: 200.** B's rows unchanged after A's foreign PATCH/PUT/DELETE.
9 list endpoints as A contained no B ids or data. Detail: `tasks/audit2-idor-live.md`.

**Duplicate system:** dedup is **tenant-scoped** (`UniqueConstraint("user_id","dedup_hash")`, `models.py:1066`;
`tasks.py:1205,1258-1273`); the results page names only runs the caller owns. Skip-trace reuse uses the v2 subject
key with `user_id` inside the hash (`skip_trace.py:243`); all five reuse paths are pinned to the caller; mixed-tenant
Tracerfy batches match answers only to the batch's own pending rows with `(id, user_id)` writes and refuse ambiguity.
**No disclosure of another tenant's delivery, enrichment or lookup state was found.** One timing-only side channel
(T-7, accepted): a global collision key can delay one tenant's lookup of the same address by a tick.

## 12. Admin-route results

`POST /scrapers/connectors` -> `require_admin_mfa` (admin + enrolled MFA + fresh MFA JWT, not API key); live
non-admin: **404**. `GET /billing/activation-funnel` -> `require_admin`; live non-admin: **404**. No other admin
route. Frontend admin gating is UI-only (W-1) and is not relied on. `GET /scrapers/connectors` is public by design (C-1).

## 13. Database-security results

PostgreSQL. Runtime roles `bridgeleads_app` (api) and `bridgeleads_app`/`bridgeleads_system` (worker): not superuser,
not BYPASSRLS, `app` not a member of `system` (recorded prod read, 2026-09-25). `DATABASE_URL` role has no DELETE.
RLS FORCE + role-targeted policies come from manually-run scripts, not migrations. Migration 101 reviewed: API may
update only `dispatched_at`; event log append-only; `anon`/`authenticated` revoked; triggers not SECURITY DEFINER,
schema-qualified, empty-GUC accepted only for `bridgeleads_system`/bypass roles. Open: S-2 (owner DSN on runtime
services), T-5 (missing composite FK), S-11 (`external_source_health` no RLS in migrations), **and whether FORCE +
policies are actually live on the three mig-101 tables in prod (not verified, see section 35).**

## 14. Cloud/deployment findings

N-01 (token in images), F-28 (origin bypass), S-2/S-3/S-4 (DSN placement, CI), F-15 (no API HSTS), R2 served only via
signed/tokened links (`API_BASE_URL` set). Redis/queue/Flower/metrics: not exposed (`/flower`, `/metrics`, `/debug`
-> 404 on api). Container runs as non-root (`Dockerfile:49-50`). Provider billing caps, backup restore: unverified.

## 15. Production debug/error findings

Live: `/docs`, `/redoc`, `/openapi.json`, `/debug`, `/_debug`, `/metrics`, `/flower`, `/admin` -> 404 on api;
`/health` -> 200 with `{"status","service"}` only. Unhandled errors return `{detail, ref}` with no trace (but without
CORS/security headers, F-16). No Stripe/Tracerfy error text reaches clients. Default 422 echoes input (E-6).

## 16. Logging findings

Live Run messages are fixed, author-written strings with an allowlisted stage set; no exception text, SQL, paths or
hosts reach users (Codex concurs). Server-side gaps: E-2 (webhook URL in logs), E-5 (raw provider/webhook bodies),
F-22/B-6 (Tracerfy path secret via exception handler), F-23 (tracebacks unscrubbed). No auth headers, cookies, DSNs
or reset tokens logged.

## 17. Input-validation findings

Pydantic `extra="forbid"` on request models, bounded lists/strings, enum record types, 422 before auth work on
malformed email (live). Gaps: custom date range has no max span (B-8), 422 echoes input (E-6).

## 18. SQL/NoSQL injection findings

All raw `text()` SQL uses bound parameters; ORDER BY is allowlisted (`results_sort.py:160`, `scrapers.py:1210`); scraped
text never reaches SQL construction. NoSQL: none in use; Redis keys are passed as values, not command fragments
(`rate_limit.py:184`, `sse_leases.py:50`). **No injection finding.** No destructive injection test was run.

## 19. XSS/CSRF/CORS findings

**XSS:** only raw-HTML use is shadcn chart styles from hardcoded config; scraped fields render as text; email templates
escape (`email_layout.py:203`). **CSRF:** backend has no cookie auth path (bearer only; SSE uses the header);
Auth.js has its own CSRF token; no server actions. **CORS (live):** hostile origin gets no ACAO, hostile preflight
400; allowed origin `https://app.bridgeleads.io` with credentials. A-11 (localhost prefix) P3.

## 20. Stripe findings

Sound: server-side price allowlist, one-subscription guard (per-user lock + Stripe re-check + session expiry), promo
/ 100%-off handling via `first_paid_at`, customer id alone grants nothing, HMAC + 300 s tolerance + mig-095 ledger read
before dispatch, out-of-order events, portal return URL, dunning/freeze, **F-11 fixed** (re-raises instead of
applying a stale body, `billing.py:2102-2132`). Open: **N-02 (P1)**, B-7/B-8 (P3). Duplicate webhook delivery is
idempotent by ledger (code + existing tests; not replayed against prod).

## 21. Tracerfy findings

Key server-side only; no user-controlled provider call; claim idempotency (PRs #349/#354: advisory lock, one active
claim per lead, fail-closed money invariant) verified by reading; provider fields CSV-sanitized; errors not surfaced.
Open: **F-12/B-3 (P1)**, B-4/B-5 (P2), F-22 (P3).

## 22. Scraper/job security findings

Start/view/stream/cancel/results/download/export tenant-bound (live, section 11). No retry endpoint exists. Workers
re-derive the owner from the job row except T-1..T-4. Connector registration admin+MFA only; customers cannot set a
scrape URL. F-07 renderer sandbox, E-4 browser guard gaps.

## 23. Live stream security findings

`/jobs/{id}/logs` (Live Run): owner check before admission and on reads, bearer header (no token in URL), leases keyed
by user id (`sse_leases.py:50-82`), reconnect re-authorizes. Live: foreign 404, anon 401. Codex concurs.

## 24. Export/CSV security findings

Job/batch/run downloads bind object id + `user_id` (live: foreign 404). Emailed links are 48 h signed download tokens
(revocable via logout-all; D-1 `is_active` gap). CSV/XLSX/JSON: every text column through `sanitize_for_csv`
(leading `= + - @ \t \r` and whitespace variants); phone numbers normalised to 10 digits, so `+1 206...` is neither
corrupted nor a formula; numeric columns untouched. Push channels: PhoneBurner fixed, generic/job webhook raw (F-02r).

## 25. Webhook/SSRF findings

HTTPS-only, redirects disabled or re-validated per hop, `trust_env=False`, blocks loopback/RFC1918/100.64/10/
169.254/ULA/link-local/IPv4-mapped. Open: F-03 rebinding (P1), E-3 IPv4-embedding IPv6 forms (P3, verified), E-1
unbounded response (P2), E-2 URL in logs (P3). Delivery destinations still have no proof-of-control (product decision
from 09-16, unchanged).

## 26. Rate-limit findings

Full per-endpoint matrix: `tasks/audit2-auth.md`. IP-keyed (dead in prod, F-01): login, refresh, register,
verify-email, reset, change-password, MFA setup, admin funnel, both webhooks. User-keyed (working): MFA verify /
break-glass, jobs, results, batches, segments, analytics, Stripe calls. **No limiter at all:** scraper
create/edit/delete, job cancel, download, export-url, api-key endpoints. Forgot-password per-address guard now works (F-05 fixed).

## 27. Dependency findings

`pip-audit -r requirements.txt`: **0 known vulnerabilities in 94 packages** (Windows run; Linux-only `uvloop` not
audited). `npm audit` (prod and full): **0 advisories**. Next 16.3.5 is past the CVE-2025-29927 middleware-bypass
class; 26 live bypass attempts against a local `next start` all rejected. Deliberate pins `stripe==11.4.0`,
`redis==5.2.1` must not be bumped blindly. `next-auth` is a beta carrying production auth.

## 28. Security headers

| Header | api.bridgeleads.io (live) | bridgeleads.io (live) |
|---|---|---|
| CSP | `default-src 'none'; frame-ancestors 'none'` | `default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; ...` (W-3) |
| HSTS | **missing** (F-15) | `max-age=31536000; includeSubDomains` |
| X-Content-Type-Options | nosniff | nosniff |
| X-Frame-Options | DENY | DENY |
| Referrer-Policy | strict-origin-when-cross-origin | strict-origin-when-cross-origin |
| Permissions-Policy | geolocation/microphone/camera=() | camera/microphone/geolocation=() |
| COOP / CORP | same-origin / same-site | n/a |

## 29. Fixes implemented

Phase 2 was approved on 2026-09-25 ("proceed with the rest", manual items excepted). Every fix below has a
regression test that was run FAILING on the pre-fix commit and PASSING after, and a Codex review (NO-GO rounds
listed). Nothing is merged: merging to `main` deploys.

| Step | Finding | Sev | Fix | Commit | Codex |
|---|---|---|---|---|---|
| S1 | N-01 | P1 | untrack `terraform.tfvars`, `infra/` out of the Docker context, `.tfvars.example`, BOM-tolerant tracked-file credential scan | `150421e5`, `c06a4a85` | P1 = rotation (yours); P2 fixed |
| S2 | N-02 | P1 | change-plan: upgrade `always_invoice` + `error_if_incomplete` (paid before applied), downgrade `proration_behavior=none`; card decline -> 402 | `98473b46` | PASS |
| S3 | N-03 | P1 | `/results`, `/download`, `/export-url` deliver rows only when `status == done`; **reproduced before the fix** (enriching/scraping/failed/cancelled runs all leaked rows) | `67382e30` | PASS |
| S4 | E-1, E-2, E-3 | P2/P3 | webhook body read raw (never decoded) and capped at 2 KB, 60/90s task limit; URL never logged or in Celery trace/result, echoed URL secrets scrubbed; SSRF unwraps NAT64/SIIT/6to4/Teredo/IPv4-compatible, blocks `64:ff9b:1::/48`, `fec0::/10`, `192.88.99.0/24` | `adf443f6` | 2 NO-GO, then PASS |
| S6 | A-2, A-4 | P2 | `/auth/api-key` and `/auth/mfa/setup` need `current_password` + a session; API keys refused on api-key, mfa setup/enable/disable, change-password; per-account reauth throttle | BE `66ec1b03`, FE `91256fb` + `9eed5cb` | 1 NO-GO, then PASS |
| S7 | A-1, A-5 | P2 | session families: logout (access and/or refresh token) revokes the family; refresh reuse after the grace window burns the family; FE sign-out calls backend logout server-side | BE `60918c78`, FE `0790629` | design + review PASS |
| S8 | A-3 | P2 | 5 wrong MFA codes lock second-factor verification 15 min (login MFA and mfa/disable), atomic Lua counter | `9ea64a09` | 1 NO-GO, then PASS |
| S10 | C-1, D-1 | P3 | `include_all` connectors admin-only; download token path requires `is_active` | `a96aaf15` | PASS |

**Not fixed in this phase, deliberately:**
- **S5 / F-02r** (generic + job webhook push raw county text): a documented, Codex-reviewed product decision
  (JSON is not an injection context; apostrophe-prefixing corrupts every consumer's data). Reclassified
  **accepted by design (P3)**; an opt-in "spreadsheet-safe" delivery setting is a product decision for you.
- **S9 / B-3** (trial accounts' unbillable overage lookups): the fix ("no overage without an active paid
  subscription") belongs inside the per-account spend cap the lookup session is building right now
  (`feat/lookup-1b1b-ledger`); implementing it here would collide. Carried as a requirement for that work.
- **F-01/F-01b** (needs Cloudflare sole ingress first), **F-03** pinning, **F-07** sandbox, **S-2** owner DSN on
  runtime services: infrastructure-dependent, unchanged.

## 30. Regression tests

Added (each proven failing on the pre-fix commit, in a separate throwaway worktree, never via the shared stash):

| File | Covers | Fails before fix |
|---|---|---|
| `tests/test_no_committed_credentials.py` | N-01: tracked-file credential scan (BOM, case, prefix-only placeholders), tfvars untracked | 2 of 5 |
| `tests/test_plan_change_billing.py` | N-02: proration by direction; declined upgrade = 402 and plan unchanged | 5 of 5 |
| `tests/test_undelivered_run_rows.py` | N-03: non-done runs list/download nothing; done run control | 4 of 5 |
| `tests/test_webhook_egress_hardening.py` | E-1/E-2/E-3: gzip bomb, 32 MB body, URL secret in logs (query, path, echoed, truncated, percent-encoded), 9 IPv6 embedding forms + public controls | 11 of 14 (first version) |
| `tests/test_reauth_sensitive_actions.py` | A-2/A-4: password + session for api-key and MFA setup; API keys refused on credential changes | 3 of 3 (first version) |
| `tests/test_session_family_revocation.py` | A-1/A-5: logout by refresh or access token, reuse burns the family only, crash-safe, grace race kept | 3 of 7 |
| `tests/test_mfa_failure_lockout.py` | A-3: lock after 5 (login MFA and mfa/disable), success clears | 2 of 3 |
| `tests/test_audit_p3_access_gates.py` | C-1, D-1 | 3 of 5 |

Updated to the new contract (with the reason in each diff): `test_results_new_count.py` (2 tests pinned a
2026-09-03 behaviour superseded by the 2026-09-08 "not done delivers nothing" rule), `test_auth.py`,
`test_break_glass_login.py`, `test_plan_entitlement_audit.py` (send `current_password`; the 6th bad MFA code is now 429).

Still required, not written: F-01 proxy-trust with a forged XFF (after the ingress fix); F-03 pinned connect under a
rebinding resolver; F-12/B-3 per-account and trial spend gate (lookup session); G-1 behavioural segments isolation.

Browser verification (Playwright against the real local API, isolated DB): API-key and MFA-setup password prompts
(7/7 checks), UI sign-out issues `POST /auth/logout` 204 and revokes a session family, browser lands on `/login`.

## 31. Test results

- Live IDOR harness: 20 routes, **0 failures** (section 11).
- `pip-audit`: 0 vulns. `npm audit`: 0. Production frontend build: success (placeholder env only).
- **Full backend suite, Phase 2 branch rebased onto `origin/main` `2275c2de`:** CI's target (`-m "not integration"`),
  fresh DB `bridgeleads_secaudit3_test` at head 101, Redis db 10, 8 foreground batches:
  **4,681 passed, 0 failed, 2 skipped.** `ruff check .` clean; `export_openapi.py --check` up to date.
  Not run locally: the 145+ `integration` tests (need provisioned RLS roles; CI runs its own).
- Frontend: `tsc --noEmit` and `eslint` clean on the changed files; production `next build` succeeds.

## 32. Codex findings

Codex ran independently (no findings shared), read-only, on `fc38e620`. It reported 3 P1 + 4 P2:

| Codex claim | Our verification | Verdict |
|---|---|---|
| P1 rate limiting: 100.64/10 untrusted | Re-proven live today | **CONFIRMED P1** (F-01) |
| P1 webhook DNS-rebinding TOCTOU | Code unchanged; risk-accepted comment | **CONFIRMED P1** (F-03) |
| P1 no per-account Tracerfy ceiling | Confirmed + widened by B-3 (trials) | **CONFIRMED P1** (F-12; raised from P2 per cross-check doctrine) |
| P2 `/scrapers/connectors?include_all=true` anonymous | Live: 6 down connectors + public county URLs, no internal hosts | **CONFIRMED, held at P3** (C-1): the doctrine says take the higher severity, but the measured impact is public county URLs only; your brief says not to inflate. Disagreement recorded. |
| P2 Tracerfy secret in URL path | Confirmed, plus a new logging path (B-6) | **CONFIRMED**, P3 (F-22) |
| P2 webhook `str(exc)` logs URL/secret | Confirmed at `webhook_delivery.py:326-330` | **CONFIRMED**, P3 (E-2): worker logs only, secret only if the customer put one in the URL |
| P2 download token skips `is_active` | Confirmed; window is **48 h**, not the 60 s Codex assumed; logout-all revocation is honoured | **CONFIRMED**, P3 (D-1) |
| Non-findings: IDOR, SSE, admin, Stripe core, Tracerfy idempotency, CSV, SQL, Redis keys, XSS, CSRF, CORS, errors | Match ours (and our live harness) | **CONCUR** |

**What Codex missed and we found:** N-01 (committed Cloudflare token), N-02 (plan-change arbitrage), N-03 (results
before cap), B-3 (trial spend), A-1..A-5 (session/API-key/MFA), E-1 (webhook DoS), S-2..S-5. Codex found nothing we
did not also find except C-1 (found independently by the frontend agent too).

## 33. Independent verification

Verified first-hand by me, not taken from an agent or Codex: F-01 (live), F-15 HSTS (live), CORS (live), env/debug
paths (live), C-1 (live), all IDOR results (live harness), N-01 (file, commit, BOM, Dockerfile, repo visibility,
redacted), N-02 (code), N-03 (code path + commit ordering), E-3 (real `_ip_is_blocked`), D-1 (code + TTL),
A-1/A-2 (FE + BE code), E-2 (code). Agent findings not individually re-verified by me are marked by their agent ID
and detailed with file:line in the `tasks/audit2-*.md` files; Phase 2 re-verifies each before fixing.

## 34. Remaining risks

- **Not verified:** whether FORCE RLS + policies are live on the three mig-101 tables in prod (my read-only catalog
  query was blocked by the permission classifier); N-03 end-to-end reproduction; N-02 annual variant in Stripe test
  mode; F-28 re-probe; N-01 token validity; provider billing caps; backup restore (never tested); `uvloop` CVEs.
- Security depends on three production flags whose code defaults are `False` (`RLS_ENFORCE`,
  `ENTITLEMENT_ENFORCEMENT`, `EMAIL_VERIFICATION_ENABLED`), plus `SKIP_TRACE_DAILY_ROW_CAP`. Last confirmed 2026-09-17.
- The per-account Tracerfy cap is being built by another session; until it ships, F-12 stays P1.
- Chromium renderer runs unsandboxed next to an owner DSN (F-07 + S-2): a renderer exploit is a full-DB compromise.

## 35. Manual actions required from you

1. **Roll the Cloudflare API token now** (N-01) and review the Cloudflare audit log back to 2026-03-17. Then approve
   untracking the file.
2. **Rotate the admin account password** found in `scripts/audit_out_ui/run3_thurston_whatcom.log`; move
   `.rls-cutover-secrets` and that log out of OneDrive (S-5).
3. Run the read-only RLS catalog check I prepared (it prints no DSN):
   `! railway run -s api -- C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/AppData/Local/Temp/claude/secaudit2/rls_catalog.py`
4. Cloudflare: make it the sole ingress (Tunnel / Authenticated Origin Pulls). This unblocks F-01.
5. GitHub: move `DATABASE_URL_SYNC` to a protected environment, delete `RAILWAY_TOKEN_PRODUCTION`, narrow `BACKEND_SCHEMA_TOKEN`.
6. Decide: disable the legacy Tracerfy path-secret route and rotate that secret (F-22).
7. Decide the Tracerfy per-account and trial spend policy (F-12/B-3).
8. Still open from 09-16: DMARC, apex SPF, MX for `security@`, backup restore test, provider billing caps.

**Proposed Phase 2 (needs your approval; max 5 files per step, each with a regression test, Codex review per build):**
1. N-01 code side: untrack tfvars, `.dockerignore` `infra/`, CI secret scan (after you rotate).
2. N-02 plan-change: `always_invoice` for upgrades, period-end downgrades.
3. N-03 reproduce, then gate results to the granted rows.
4. A-1 + A-5 (logout revokes the refresh family), then A-2/A-4 re-auth for API key and MFA enrollment, then A-3.
5. E-1 webhook body cap + time limit; F-02r sanitize generic/job webhook; E-2/E-3 small fixes.
6. B-3 trial skip-trace gate (coordinate with the 1b-1b session, do not touch its files).
7. F-01 only after item 4 of the manual list. F-03 pinning. Remaining P3s.

## 36. Files changed

**Backend** (branch `chore/security-audit-2026-09-25`): `.dockerignore`, `infra/terraform/terraform.tfvars`
(untracked), `infra/terraform/terraform.tfvars.example`, `src/api/auth.py`, `src/api/schemas.py`,
`src/api/middleware/__init__.py`, `src/api/middleware/auth_hardening.py`, `src/api/middleware/security.py`,
`src/api/routes/auth.py`, `src/api/routes/auth_helpers/{login,mfa,registration}.py`, `src/api/routes/billing.py`,
`src/api/routes/jobs.py`, `src/api/routes/scrapers.py`, `src/workers/webhook_delivery.py`, `schema/openapi.json`;
8 new test files (section 30) and 4 updated ones. Docs: this report, `tasks/audit2-*.md`,
`tasks/todo-security-2026-09-25.md`, `docs/BUILD_JOURNAL.md`.
**Frontend** (branch `fix/reauth-sensitive-actions`): `lib/api.ts`, `lib/auth.ts`,
`components/settings/ApiKeysTab.tsx`, `components/settings/security-tab.tsx`.

## 37. Commits

Backend, on top of `origin/main` `2275c2de`: `150421e5` N-01, `c06a4a85` N-01 test, `98473b46` N-02, `67382e30` N-03,
`adf443f6` E-1..3, `66ec1b03` A-2/A-4, `60918c78` A-1/A-5, `9ea64a09` A-3, `a96aaf15` C-1/D-1, `c1caf29e` OpenAPI,
plus the docs commit. Frontend, on top of `origin/master` `8332673`: `91256fb`, `9eed5cb`, `0790629`.
**Merge order: frontend first, then backend.** Nothing is merged (merging to `main` deploys).

## 38. Git status

Worktrees `C:/Users/Windows/bl-wt-secaudit2` (backend) and `C:/Users/Windows/bl-web-secaudit2` (frontend), each on its
own branch, clean apart from the untracked `.unlazy/` ledger. A throwaway worktree
`C:/Users/Windows/bl-wt-secaudit2-pre` was used only to prove tests fail on pre-fix commits. No other session's
worktree or branch, and not the primary checkout, was modified. Production side effects:
14 forgot-password requests for non-existent `@example.com` addresses (no account, no email, no row), 12 invalid ones
(422), and about 70 read-only GETs/HEADs/OPTIONS. Isolated rig artifacts: DB `bridgeleads_secaudit2_test`, Redis db 11,
one 6543 proxy process, and a frontend worktree `C:/Users/Windows/bl-web-secaudit2` (detached, clean).
