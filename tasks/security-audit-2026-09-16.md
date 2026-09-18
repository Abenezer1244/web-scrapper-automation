# BridgeLeads Pre-Launch Security Audit — Phase 1 (AUDIT ONLY)

**Date:** 2026-09-16
**Worktree:** `C:/Users/Windows/bl-wt-secaudit` — branch `chore/security-audit-2026-09-16` @ `60f1b00` (tip of `origin/main`)
**Scope:** Audit only. No production changes. No credential rotation. No destructive testing.

## Phase 0 — Workspace safety (DONE)

- [x] `git worktree list` — **~90 worktrees** exist; many concurrent sessions (quota-1001, CRM-CSV,
      results-sort, king-cv, mailing, skip-trace, journal/design-audit). None touched.
- [x] Primary checkout was on **detached HEAD `ba29576`, 100 commits behind `origin/main`**.
      Auditing it would have produced findings against stale code. Isolated worktree cut instead.
- [x] No writes to the primary tree (its `tasks/todo.md` is modified by another session).

## Phase 1 — Architecture / trust boundaries (DONE)

| Layer | Actual |
|---|---|
| Marketing + app | Next.js on **Vercel** — `bridgeleads.io`, `app.bridgeleads.io` (same deployment, identical ETag) |
| API | FastAPI on **Railway**, fronted by **Cloudflare** — `api.bridgeleads.io` |
| Auth (API) | JWT bearer + API key (`src/api/auth.py`) |
| Auth (marketing) | **Auth.js/NextAuth cookies present on the Vercel host** (`__Host-authjs.csrf-token`) — second auth system, needs reconciliation |
| DB | Postgres/Supabase, RLS + app-layer `user_id` filter; boot-time `check_rls_role_status()` is **advisory only** |
| Queue | Celery + Redis (Upstash) |
| Object storage | Cloudflare R2; `.env.example` references `exports.bridgeleads.io` — **NXDOMAIN** (see F-06) |
| Billing | Stripe | Enrichment | Tracerfy | Email | Resend |

## Confirmed so far (live, non-destructive production evidence)

### CONFIRMED SECURE CONTROLS
- **C-01 CORS origin validation.** Attacker / `null` / suffix-trick origins receive **no**
  `Access-Control-Allow-Origin`; preflight from evil origin → 400. `main.py:65-79`.
- **C-02 API docs disabled in prod.** `/docs`, `/redoc`, `/openapi.json` → 404 (`main.py:55-57`, DEBUG off).
- **C-03 Unauthenticated access rejected.** `/jobs /scrapers /batches /analytics /notifications /billing` → 401.
- **C-04 Account brute-force lockout works.** 5 failed logins → 429 `Retry-After: 28`, and a
  **spoofed `X-Forwarded-For` does NOT reset it** (account-keyed, not IP-keyed).
- **C-05 Server-side input validation.** Malformed email → 422 before any auth work.
- **C-06 Enumeration-safe reset.** `/auth/forgot-password` → 200 for unknown addresses.
- **C-07 API security headers.** CSP `default-src 'none'; frame-ancestors 'none'`, COOP, CORP,
  nosniff, XFO DENY, Referrer-Policy. Appropriate for a JSON API.
- **C-08 HTTPS.** Both hosts 308-redirect HTTP→HTTPS.
- **C-09 Generic error envelope.** Global handler returns `{detail, ref}` — no stack trace (`main.py:102-108`).

### CONFIRMED VULNERABILITIES

- **F-01 [P1] Every IP-keyed rate limit is non-functional in production.**
  `/auth/login` calls `rate_limit(zone="auth")` = 10/min per IP (`login.py:47`), yet **30+ auth
  requests produced zero zone-limiter 429s**. Discriminating test on `/auth/forgot-password`:
  14 requests without XFF → all 200; 14 with a *fixed* XFF → all 200 (so XFF is **not** trusted,
  i.e. `_is_trusted_proxy()` is false and `client_ip()` falls back to `request.client.host`).
  `rate_limit.py:90-97` predicted exactly this: *"If a CDN like Cloudflare is added in front
  later, validate the immediate peer or strip/normalize the header at the edge."* Cloudflare
  **has** since been added.
  Affects the IP-keyed calls only: login, register, forgot/reset-password, MFA issue,
  **and webhook signature-spray throttling** (`webhooks.py:164,180`; `billing.py:1710`).
  **Not** affected: every call passing `identifier=current_user.id` (jobs/stripe/general zones)
  and MFA verify/break-glass (keyed `mfa-verify:{user_id}`) — those key on the user, not the IP.
  Root cause is CONFIRMED-as-broken but the precise mechanism is **UNVERIFIED** among:
  (a) rotating Cloudflare/Railway edge IP as the key, (b) Redis fail-open
  (`rate_limit.py:154-172`), (c) fallback limiter spread across replicas.
  *Diagnostic:* grep prod logs for `rate_limit fail-open` from logger `security.rate_limit`.

- **F-02 [P2] Password spraying is unthrottled.** 16 distinct accounts × 1 attempt from one IP →
  all 401, no 429. The per-account lockout (C-04) is by design no defense against spraying.
  Consequence of F-01.

- **F-03 [P2] Unauthenticated registration flooding.** 8 rapid `/auth/register` → all 200, no
  rate limit, no CAPTCHA. Consequence of F-01. (`once_per` is a fail-closed email-bomb guard, so
  per-address email flooding is partly mitigated — but account-row creation is not.)

- **F-04 [P3] HSTS missing on the API host.** `api.bridgeleads.io` sends no
  `Strict-Transport-Security` (the Vercel hosts do). Partly mitigated because
  `bridgeleads.io`'s HSTS carries `includeSubDomains`, which covers the API *for browsers that
  visited the apex first* — but not API-key/mobile/server clients that only ever hit the API.

- **F-05 [P3] Unhandled-500 responses bypass CORS and security headers.** `add_middleware`
  ordering puts Starlette's `ServerErrorMiddleware` **outside** both `CORSMiddleware` and
  `SecurityHeadersMiddleware` (`main.py:63-79`), so the `{detail, ref}` 500 body is emitted above
  them. The browser cannot read the `ref`, and the 500 carries no security headers.

- **F-06 [P3] `exports.bridgeleads.io` does not resolve (NXDOMAIN)** but is referenced at
  `.env.example:29`. Needs confirmation of the *actual* prod export URL base.

- **F-07 [P3] Stale security comments assert the opposite of production reality.**
  `src/db/session.py:386-392` states *"the current prod role HAS BYPASSRLS"* and `main.py:36-39`
  calls the check *"Advisory"*. Both are **false since 2026-06-12**: `docs/BUILD_JOURNAL.md:5428-5444`
  documents the completed cutover (47 role-targeted policies, Railway repointed api=app /
  worker=system / migrate=postgres, `RLS_ENFORCE=true` with *"all fail-closed boot gates passed"*,
  `FORCE ROW LEVEL SECURITY` on 23 tables, 10/10 prod isolation checks).
  Separately, the `check_rls_role_status` docstring (`session.py:321-331`) claims enforcement is
  gated on `ENVIRONMENT == "production"`, but the code gates on `settings.RLS_ENFORCE`
  (`session.py:398`) — the docstring and the implementation disagree.
  *Risk:* a future engineer reading these concludes RLS is inert and drops a `WHERE user_id`
  filter as pointless. This cost me real time during this audit — I nearly filed it as a P1.

- **F-08 [P2] No DMARC record.** `_dmarc.bridgeleads.io` → **NXDOMAIN** (verified via Cloudflare,
  the authoritative NS). Nothing instructs receivers to reject unauthenticated mail claiming to be
  from the domain. BridgeLeads sends **password-reset links** by email, so a spoofed
  "BridgeLeads password reset" phish passes receiver policy checks, and there is no RUA reporting
  to detect a campaign. *Mitigating:* DKIM (`resend._domainkey`) and SPF
  (`send.bridgeleads.io` → `v=spf1 include:amazonses.com ~all`) are correctly configured, so
  legitimate mail authenticates — the gap is policy + enforcement, not signing.

- **F-09 [P2] No MX on `bridgeleads.io`** — inbound mail to `security@`, `support@`, `privacy@`,
  `abuse@` **bounces**. Consequences: no channel to receive a vulnerability report; DSAR/GDPR/CCPA
  legal mail vanishes; provider account-recovery to a domain address fails.
  *Note:* extends the known compliance finding about `bridgeleads.com` — the **production `.io`
  domain has the same defect**, which that earlier finding did not cover.

- **F-10 [P3] No SPF on the apex.** No TXT records at all on `bridgeleads.io`. Mail is sent from
  the `send.` subdomain, so this is not breaking delivery — but a `v=spf1 -all` on the apex would
  explicitly declare that the apex never sends mail, closing apex-From spoofing.

### CORRECTED — was nearly a false P1
- **C-10 RLS is ENFORCED in production.** Because the boot gate *refuses to start* when the role
  bypasses RLS (`session.py:398-406`), the API serving traffic is itself evidence the gate passed.
  Combined with the journal record above: policies are active, not decorative.
  **Residual:** proof-of-liveness only holds while `RLS_ENFORCE=true`. One Railway env check
  (`RLS_ENFORCE`) or one boot-log line from logger `security.rls` closes this to fully CONFIRMED.
  Until then: **PARTIAL / NEEDS INFRASTRUCTURE ACCESS.**

### Side effects of this audit (disclosed)
- ~8 pending registrations created on production with `@example.com` addresses
  (`reg-probe-<ts>-N@example.com`) and ~28 `@example.com` forgot-password requests.
  All unverified/no-op rows against nonexistent accounts. **Recommend cleanup.** Probing stopped
  on discovery.

## Empirical test evidence (isolated rig — no interference with concurrent sessions)

Ran on a **dedicated** DB `bridgeleads_secaudit_test` + Redis db 12 (shared `bridgeleads_test`
and db 0 untouched; shared rig was idle, pg.log 18h stale). venv reused: `bl-testenv/venv-taxcap`.
Migrations applied through **095** (`stripe_webhook_events` — the webhook idempotency ledger).

**Result: 209 passed · 10 skipped · 0 genuine failures** across the security suite
(RLS isolation + role policies + GUC reapply, collection scope, quota reservation/accounting,
skip-trace over-quota, entitlement matrix, jobs entitlement guard, API-key plan guard,
CSV injection + sanitize, webhook SSRF, brute-force lockout, break-glass ×2, log redaction,
config secret redaction, token amr).

- **C-11 Cross-tenant RLS isolation empirically verified.**
  `test_rls_blocks_cross_tenant_read_on_results` and `..._on_delivered_records` PASS against a
  real `NOBYPASSRLS` role — tenant A cannot read tenant B's rows.

- **F-11 [P3] RLS isolation tests can fail (or pass) for the wrong reason.**
  These 2 tests initially failed with `permission denied for table delivered_records`. Cause:
  `tests/test_rls_isolation.py:56-77` creates role `bridgeleads_rls_test` and grants on
  *all tables as of creation time*. The role is **cluster-wide and persists across runs**, so it
  silently drifts as migrations add tables. After re-granting, both passed in 1.01s — proving the
  failure was environmental, **not** a product defect.
  *Why it matters:* "permission denied" and "RLS filtered it out" are not the same thing. A
  grant-drifted role can make an isolation assertion pass for the wrong reason. The grant should
  be re-applied every run, not only at role creation.

## Remaining — in flight (8 parallel agents)
- [ ] Auth/session/password deep audit · [ ] Tenant isolation + IDOR matrix · [ ] Stripe/quota/Tracerfy
- [ ] Exports/CSV injection · [ ] SSRF + ingestion trust · [ ] Deploy/headers/deps · [ ] Secrets + git history · [ ] Frontend
- [ ] Local two-tenant cross-tenant test matrix · [ ] Codex independent review · [ ] Final report
