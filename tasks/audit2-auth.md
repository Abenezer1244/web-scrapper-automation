# Auth / Session / Rate-Limit Audit (round 2)

**Backend:** `C:/Users/Windows/bl-wt-secaudit2` @ `25a04eaf` (origin/main)
**Frontend:** `bridgeleads-web` @ `origin/master` `8332673` (read via `git show`, working tree not used)
**Method:** static read only. No pytest, no prod calls, no source edits. Line numbers are against the commits above.
**Prior reports re-checked:** `tasks/SECURITY-AUDIT-REPORT-2026-09-16.md`, `tasks/audit-auth.md`.

Severity scale: P0 = exploitable now, severe; P1 = high; P2 = medium; P3 = low / hardening. Not inflated: every finding below states its prerequisite.

---

## 1. Prior-finding status

| ID | Prior claim | Status on 25a04eaf | Evidence |
|---|---|---|---|
| F-01 | IP rate limiting dead: `100.64.0.0/10` not trusted | **OPEN** | `rate_limit.py:54-60` still only 127/8, ::1, 10/8, 172.16/12, 192.168/16. `client_ip()` `rate_limit.py:99-109` returns the rotating peer. `start.sh:97` is `uvicorn main:app --host 0.0.0.0 --port ...` with no `--proxy-headers` / `--forwarded-allow-ips`. `TRUSTED_PROXY_HOPS` default 1 (`settings.py:248`). The team's own comment at `auth_helpers/password.py:123-129` confirms the limiter is not load-bearing and that the origin is still directly reachable. |
| F-28 | Cloudflare bypassable (origin reachable), precondition for F-01 fix | **OPEN (per code comment; infra not verifiable from code)** | `password.py:127-129`: "the Railway origin is currently reachable directly, so X-Forwarded-For is forgeable". |
| F-01b | Escalating lockout never fires (email counter memory = 15 min cap) | **OPEN** | `auth_hardening.py:484` cap 15 min; `:491` `_EMAIL_COUNTER_TTL = _EMAIL_LOCKOUT_CAP_SECONDS`. 4 guesses / 15 min per account never reaches threshold 5 (`:403`). The IP half (`:586-594`) keys on the dead `client_ip()`. |
| F-04 | Password spraying unthrottled | **OPEN** | Same root cause: `login.py:47` IP-keyed; per-account lockout cannot see 1-attempt-per-account spraying. |
| F-05 | forgot-password email bomb, no per-address guard | **FIXED** | `password.py:21-26` `_RESET_EMAIL_MIN_INTERVAL = 300`; `:150-152` `once_per(f"pwreset:{blind_index(email)}")` called unconditionally (no timing oracle), gates the send only, response unchanged. Residual: 12 mails/hour/address, acceptable. |
| F-10 | `_fallback_hits.clear()` flush | **FIXED** | `rate_limit.py:124-172`: expiry-only eviction, fails closed when full of live buckets. Test: `tests/test_rate_limit_fallback.py`. |
| F-15 | No HSTS on API | **OPEN** | `security.py:511-512` emits HSTS only when `request.url.scheme == "https"`; without uvicorn proxy headers (`start.sh:97`) scheme stays `http`. |
| F-16 | Unhandled 500s bypass CORS + security headers | **OPEN** | `main.py:105-111` still registers the handler on the `Exception` key (becomes `ServerErrorMiddleware`'s handler, outermost). |
| F-25 | `fc00::/7` missing from trusted-proxy set | **OPEN** | `rate_limit.py:54-60`. |
| audit-auth F-09 | `/auth/logout` leaves refresh token valid | **OPEN, and worse than reported** | See A-1: the frontend never calls `/auth/logout` at all. |
| audit-auth F-10 | MFA enrollment needs only a session | **OPEN** | See A-4. |
| audit-auth F-11 | No refresh-token reuse detection | **OPEN** | See A-5. |
| audit-auth F-03a | Registration flood (bcrypt burn + pending rows), IP-only throttle | **OPEN** | `registration.py:221` only `rate_limit(zone="auth")`; `:348`, `:372` bcrypt on every request. |

---

## 2. New / re-scoped findings

### A-1 [P2] Sign-out revokes nothing server-side; a copied session keeps minting tokens

**Evidence**
- Frontend: `lib/api.ts:284-316` `signOutSafely()` is "the ONLY way this app should sign a user out". It calls Auth.js `signOut({redirect:false})` and navigates. It never calls backend `/auth/logout`. `git grep "/auth/logout"` on origin/master hits only `lib/api-types.generated.ts:184,201` (generated types), no caller.
- Backend: `routes/auth.py:282-312` `/auth/logout` takes no body, blacklists only the ACCESS jti, and swallows every non-Redis exception (`:309-310`), so an API-key caller gets 204 with nothing revoked.
- `/auth/refresh` checks only `is_revoked_by_user_logout_all` + `consume_once` (`login.py:464-488`); neither is touched by sign-out.
- Auth.js session is a stateless encrypted JWT cookie, `maxAge` 7 days (`lib/auth.ts:200-203`), carrying the backend refresh token (`lib/auth.ts:210-211`).

**Prerequisites** Attacker holds any of: a copy of the Auth.js session cookie (malware / shared machine profile / backup), the backend refresh token, or an access token read by XSS (A-2 notes the bearer is JS-readable).

**Impact** After the user clicks "Sign out": the access token lives up to 1 h; the refresh token (or the cookie that wraps it, replayed to `POST /api/session/refresh`) keeps rotating into fresh 7-day refresh tokens indefinitely (see A-6). Only logout-all, password change/reset, or an MFA toggle ends it. Users reasonably believe sign-out ended the session.

**Fix**
1. Backend: accept `{refresh_token}` on `/auth/logout`; `decode_refresh_token` it, verify `sub == current_user.id`, `TokenBlacklist.consume_once(refresh_jti, ttl)` plus the existing access-jti blacklist. Return 204 only if something was revoked, else 400 for an API-key caller.
2. Frontend: add a POST route handler (e.g. `app/api/session/logout/route.ts`) that reads the server-side session (`auth()`), POSTs `/auth/logout` with bearer + refresh token, then lets `signOutSafely()` clear the cookie. The refresh token never touches client JS.

**Regression test** Login, capture refresh token R, call `/auth/logout` with R, then `/auth/refresh` with R returns 401 (and outside the 30 s replay window, not a cached pair).

---

### A-2 [P2] A stolen 1-hour session converts into a non-expiring, sign-out-surviving API key

**Evidence**
- `routes/auth.py:447-462` `POST /auth/api-key`: guarded only by `require_plan("business","agency")`. No password re-entry, no `auth_time` freshness, no notification email (only `audit_log`), no expiry. It silently replaces the owner's existing key.
- `auth.py:277-317`: an API key authenticates as the full user on every `CurrentUser` route, including `/auth/api-key` itself (self-rotation), `/auth/mfa/setup`, `/auth/mfa/enable`, `/auth/change-password`, `/auth/logout-all`.
- The access token is readable by browser JS: `lib/auth.ts:233` puts `accessToken` on the session object served by `/api/auth/session`; `lib/api.ts:47-48` attaches it from `getSession()`. The FE CSP keeps `script-src 'self' 'unsafe-inline' 'unsafe-eval'` (`next.config.ts:78`), so any XSS reads it.

**Prerequisites** Victim on Business/Agency plan; attacker obtains one access token (XSS, shared machine, A-1 leftover).

**Impact** Persistence far beyond the 1 h token: the minted key survives sign-out (A-1), token expiry, and refresh-chain death. The owner's own integration key is silently rotated away. Revoked only by logout-all / password change / reset / MFA toggle, which the owner has no signal to perform.

**Fix** Require `current_password` in the `POST /auth/api-key` body (verify with `verify_password`, feed failures to the per-user limiter), or require `auth_time` within 5 min. Reject `auth_method == "api_key"` on `/auth/api-key`, `/auth/mfa/*`, `/auth/change-password` (use `CurrentAuth` and check). Email the owner on key creation. Store `api_key_created_at` and show it in Settings.

**Regression test** `POST /auth/api-key` without / with wrong password returns 400/401 and leaves `api_key_hash` unchanged; the same call authenticated by an API key returns 403.

---

### A-3 [P2] TOTP second factor has no failure lockout: flat 10 guesses/min/user forever

**Evidence**
- `login.py:132` `rate_limit(zone="auth", identifier=f"mfa-verify:{user_id}")` = 10/min (`rate_limit.py:25`). That is the only brake.
- A wrong code does not burn the challenge (`login.py:176-180`) and is not fed to any failure counter (`login.py:127-131` says so deliberately); only `audit_log("mfa_failure")` (`:170`). No escalation, no notification.
- `mfa.py:26` `_TOTP_VALID_WINDOW = 1`: 3 codes are valid at any instant (`verify_totp_counter` scans counter-1..counter+1).
- New challenges are cheap: `mfa-issue:{user.id}` also 10/min (`login.py:86`), challenge lives 5 min (`tokens.py:180`).

**Prerequisites** Attacker knows the password (credential stuffing, which is exactly the threat MFA exists for).

**Impact** p = 3e-6 per guess, 14,400 guesses/day: about 4.2% success per day, about 50% within ~16 days, fully automated and silent to the user. NIST 800-63B requires capping consecutive failures (<= 100). Side effect: the attacker also exhausts the real user's `mfa-verify` bucket.

**Fix** Per-user consecutive-MFA-failure counter in Redis (reuse `_RECORD_FAILURE_LUA` with key `bf:mfa:{user_id}`, long memory e.g. 24 h, escalating lock), cleared on success; notify the user at a threshold (reuse `send_lockout_notification`); optionally count toward the email lock. Apply the same counter on `/auth/login/break-glass` and `/auth/mfa/disable`.

**Regression test** N (e.g. 10) wrong codes for one user across fresh challenges produce 429 on the next attempt even with a correct code, and the counter survives beyond 60 s.

---

### A-4 [P2] MFA enrollment needs only a session (or an API key): hostile enrollment locks the owner out (prior audit-auth F-10, still open, wider)

**Evidence** `schemas.py:199-201` `MfaEnableRequest` is `code` only; `mfa.py:53-120` no `verify_password`. Disable requires password + factor (`schemas.py:209-214`, `mfa.py:145-171`). New since the prior report: API keys also reach these routes (`auth.py:277-317`, `routes/auth.py:375-395` use `CurrentUser`).

**Prerequisites** One access token or one API key.

**Impact** Attacker enrolls their own authenticator, receives the backup codes (`mfa.py:120`), and every session plus the API key is revoked (`mfa.py:111-118`). The owner, knowing the password, is then challenged for a factor they never had; `/auth/mfa/disable` needs that factor. Recovery only via operator break-glass. Account DoS; for an admin, admin surface lockout.

**Fix** Add `password: str = Field(max_length=72)` to `MfaEnableRequest` and `verify_password` before `user.mfa_enabled = True` (`mfa.py:101`); reject API-key callers on `/auth/mfa/*`.

**Regression test** `/auth/mfa/enable` with a valid TOTP but wrong/missing password returns 400 and `mfa_enabled` stays False; API-key caller returns 403.

---

### A-5 [P2] No refresh-token reuse detection (prior audit-auth F-11, still open)

**Evidence** `login.py:467-488`: a lost `consume_once` outside the 30 s grace window (`auth_hardening.py:103`) returns a bare 401 ("Refresh token already used"), with no `revoke_all_for_user`, no audit event, no alert. The replay cache holds two live bearer tokens in plaintext for 30 s (`login.py:523-527`).

**Prerequisites** Stolen refresh token (A-1 path, or Auth.js cookie copy).

**Impact** If the attacker rotates first, the victim sees an ordinary "session expired" and re-logs in; the attacker's chain continues indefinitely. The one reliable theft signal is discarded.

**Fix** Keep the grace window. On the post-grace branch (consume failed and `_await_rotation_result` returned None) call `TokenBlacklist.revoke_all_for_user(user_id)` and `audit_log(request, "refresh_reuse_detected", user_id)` before the 401 (RFC 9700 family revocation).

**Regression test** Rotate R to R2, wait > 30 s (or clear the replay key), replay R: 401 and a subsequent call with R2 or its access token also 401.

---

### A-6 [P3] No absolute session lifetime

**Evidence** Each rotation mints a fresh 7-day `exp` (`auth.py:192`); `auth_time` is carried forward (`login.py:509-517`) but never checked on `/auth/refresh`. Auth.js JWT cookie is rolling (`lib/auth.ts:200-203`, default `updateAge`).

**Impact** A session that refreshes at least weekly never re-authenticates. Amplifies A-1/A-5.

**Fix** In `refresh_tokens`, reject when `now - auth_time > 30 days` (auth_time is already signed and propagated). Keep the admin step-up window as is.

**Regression test** Refresh token with `auth_time` older than the cap returns 401.

---

### A-7 [P3] `/auth/change-password` is an unthrottled password oracle for any session or API-key holder

**Evidence** `password.py:35` only IP-keyed `rate_limit(zone="auth")` (dead per F-01); `:40-44` distinct 400 "Current password is incorrect."; no `BruteForceProtection` involvement. Reachable by API key (`auth.py:277-317`).

**Prerequisites** Access token or API key.

**Impact** Online guessing of the current password at bcrypt speed, turning a temporary credential into the durable password (then reusable elsewhere, and survives logout-all).

**Fix** Add `rate_limit(request, zone="auth", identifier=f"pwchange:{current_user.id}")` and record failures via `BruteForceProtection.record_failure(ip, user.email)`; reject API-key callers.

**Regression test** 11 wrong `current_password` submissions within 60 s return 429 regardless of source IP.

---

### A-8 [P3] Emailed download links: 48 h bearer in the query string, not single-use, no `is_active` check; the endpoint also accepts a full access JWT as `?token=`

**Evidence**
- `workers/tasks_helpers/status.py:50-56` mints a 48 h download JWT into `.../download?token=...`.
- `jobs.py:1173` docstring says "single-use", but `jobs.py:1310-1325` only checks blacklist + logout-all; no `consume_once`.
- `jobs.py:1284-1285`: a non-download token in `?token=` is validated as an ACCESS token, so a session JWT is accepted from the URL.
- `jobs.py:1376` `select(User).where(User.id == user_id)` has no `User.is_active` filter (every other auth path has it: `auth.py:358`, `login.py:435`).
- Only uvicorn access logs are scrubbed (`main.py:118`, `:134-137`); Cloudflare / Railway edge logs and mail link-scanners see the token.

**Prerequisites** Access to edge logs, a forwarded email, or a mail-scanner log.

**Impact** Read access to one job's lead CSV for up to 48 h; a deactivated account's links keep working. Revocable only via logout-all.

**Fix** Add `User.is_active` to `jobs.py:1376`; accept access JWTs only from the `Authorization` header, never `?token=`; either implement true single-use for the 60 s in-app token (`consume_once`) or correct the docstring; consider 24 h for delivery links.

**Regression test** Deactivated user's valid download token returns 401; access JWT in `?token=` returns 401.

---

### A-9 [P3] Latent F-01-fix hazard: Auth.js password path proxies logins from Vercel's IPs

**Evidence** `lib/auth.ts:174-193`: `authorize()` still has a password path that calls backend `/auth/login` server-side from Vercel. It is reachable by anyone via `POST /api/auth/callback/credentials` (Auth.js CSRF token is self-fetchable). The login page no longer uses it (`app/(auth)/login/page.tsx` adopts tokens obtained directly from the browser), and with `EMAIL_VERIFICATION_ENABLED` on, register does not auto-login.

**Impact today** None beyond F-01 (IP key already dead). **After F-01 is fixed**, all attempts through this path share Vercel egress IPs: an attacker can drive the uncapped IP ladder (`auth_hardening.py:586-594`, 24 h tier) against those shared IPs, and any legitimate traffic still on that path is locked with them.

**Fix** Delete the password branch from `authorize()` (keep token adoption only) before or together with the F-01 fix.

**Regression test** FE: `authorize({email, password})` returns null without a network call.

---

### A-10 [P3] Admin gate accepts API keys; admin funnel limiter is IP-keyed

**Evidence** `auth.py:429-446` `require_admin` does not check `auth_method` (only `require_admin_mfa` does, `:459`). `/billing/activation-funnel` uses `require_admin` (`billing.py:118-121`) and its limiter key is `admin-funnel:{client_ip}` (`billing.py:113-115`), dead per F-01.

**Impact** An admin's API key (if the admin is on a Business plan) reads the funnel without MFA freshness; funnel is read-only aggregate data. Low.

**Fix** Reject `auth_method == "api_key"` in `require_admin`; key the funnel limiter on user id after the gate.

---

### A-11 [P3] CORS hardening

**Evidence** `main.py:68-82` `allow_credentials=True` although the backend has no cookie auth (section 4). `settings.py:486-488` accepts any origin starting with `http://localhost` or `http://127.0.0.1`, which also matches `http://localhost.attacker.tld` if an operator ever puts such a value in `ALLOWED_ORIGINS`.

**Impact** None with the current defaults (`settings.py:244` is three explicit https origins). Config footgun only.

**Fix** `allow_credentials=False`; parse origins with `urlsplit` and require `hostname in {"localhost","127.0.0.1"}` for http.

---

## 3. Rate-limit matrix (25a04eaf)

"IP (dead)" = keyed on `client_ip()`, non-functional in production per F-01. User-keyed buckets are unaffected.

| Endpoint | Zone / limit | Key | Status in prod | Other brake |
|---|---|---|---|---|
| POST /auth/login | auth 10/min | IP | **dead** | BruteForce email lock (capped 15 min, F-01b) + IP lock (dead) |
| POST /auth/login (MFA account, challenge issue) | auth 10/min | `mfa-issue:{user}` `login.py:86` | works | |
| POST /auth/login/mfa | auth 10/min | IP `:106` + `mfa-verify:{user}` `:132` | user half works | no failure lockout (A-3) |
| POST /auth/login/break-glass | auth 10/min | IP `:211` + `mfa-breakglass:{user}` `:235` | user half works | 80-bit codes |
| POST /auth/refresh | auth 10/min | IP `login.py:418` | **dead** | single-use rotation |
| POST /auth/register | auth 10/min | IP `registration.py:221` | **dead** | dup-notice `once_per` 24 h `:102`; verify-mail advisory lock + daily cap (worker) |
| POST /auth/verify-email | auth 10/min | IP `registration.py:405` | **dead** | 24 h signed token |
| Verification resend | no endpoint; re-register restages; beat dispatch has 120 s window + daily cap | n/a | | |
| POST /auth/forgot-password | auth 10/min | IP `password.py:113` | **dead** | `once_per pwreset:{blind_index}` 300 s `:151` (works) |
| POST /auth/reset-password | auth 10/min | IP `password.py:187` | **dead** | signed single-use token |
| POST /auth/change-password | auth 10/min | IP `password.py:35` | **dead** | none (A-7) |
| POST /auth/mfa/setup | auth 10/min | IP `mfa.py:28` | **dead** | session required |
| POST /auth/mfa/enable, /disable | auth 10/min | IP + `mfa-user:{user}` `mfa.py:61,132` | user half works | |
| POST /auth/logout, /logout-all, /api-key, GET /auth/me, PUT profile/prefs | none | | | session required |
| POST /jobs (run now) | jobs 5/min | user `jobs.py:309` | works | quota |
| DELETE /jobs/{id} (cancel) | none | | | ownership |
| Retry | no user-facing retry endpoint on this commit | | | watchdog-driven |
| GET /jobs/{id}/results (search/filter) | general 60/min | user `jobs.py:417` | works | |
| GET /jobs/{id}/logs (SSE) | none | | | per-user SSE lease cap (`sse_leases.py`) |
| GET /jobs/{id}/export-url | none | | | 60 s token |
| GET /jobs/{id}/download | none | | | token; live CSV build from DB |
| POST/PATCH/DELETE /scrapers | none | | | plan + config limits |
| PUT /scrapers/{id}/csv-layout | general | user `scrapers.py:846` | works | |
| GET /scrapers/{id}/records | general | user `scrapers.py:1153` | works | |
| POST /scrapers/{id}/jobs/{job}/dialer-replay | general | user `scrapers.py:1319` | works | |
| POST /scrapers/connectors (admin) | none | | | `require_admin_mfa` step-up |
| Skip-trace lookup | no HTTP endpoint on this commit; driven by `skip_trace_enabled` on the scraper config and the job pipeline (gated by POST /jobs 5/min) | | | billing claim invariants |
| POST /batches; batch downloads / leads / runs | general | user `batches.py:180,727,825,848,1027,1067` | works | |
| GET /batches, /batches/{id} | none | | | |
| POST /segments/* (4) | general | user `segments.py:661,709,807,835` | works | |
| GET /analytics/summary | general | user `analytics.py:79` | works | |
| GET /billing/referral, /skip-trace-usage, /usage | general | user | works | |
| GET /billing/subscription, POST /checkout, /change-plan, /portal | stripe 10/min, fail-closed fallback | user | works | |
| GET /billing/activation-funnel (admin) | general | `admin-funnel:{IP}` `billing.py:113` | **dead** | require_admin |
| POST /billing/webhook | webhook 120/min | IP `billing.py:1710` | **dead** | Stripe signature |
| POST /webhooks/tracerfy[/{secret}] | webhook 120/min | IP `webhooks.py:164,195` | **dead** | shared secret |
| API-key traffic | no per-key limiter; shares the per-user buckets above | | | plan gate at use (`auth.py:304-309`) |

---

## 4. Explicit non-findings (checked, sound)

- **JWT algorithm pinning.** Every decode passes `algorithms=["HS256"]` (`auth.py:205,221`, `tokens.py:71,152,212`, `jobs.py:1280-1296`); PyJWT 2.13.0 rejects `none` and there are no asymmetric keys, so no HS/RS confusion. `iss` and a per-purpose `aud` are pinned everywhere.
- **Token-type separation.** Access (`bridgeleads-api`), refresh (`-refresh`), reset (`-reset`), verify (`-verify`), MFA challenge (`-mfa`), download (`-download`) audiences are distinct; `purpose` double-checked (`auth.py:226-227,328-329`). A refresh token cannot authenticate a request; an access token cannot refresh.
- **Refresh rotation.** Atomic single-use via `SET NX` (`auth_hardening.py:66-84`); 30 s grace window returns the same pair only (`login.py:467-488`); fails closed on Redis error (503).
- **logout-all kills the refresh chain.** `/auth/refresh` rejects any refresh token with `iat <= users.revoked_at` (`login.py:465`), so a 7-day Auth.js cookie cannot re-mint after logout-all; logout-all also clears the API key (`routes/auth.py:337-341`). Revocation is durable in Postgres with fail-closed cache handling (`auth_hardening.py:155-254`).
- **Credential-change revocation.** change-password (`password.py:89-103`), reset (`:297-309`), MFA enable/disable (`mfa.py:111-118,189-196`) and break-glass (`login.py:323-353`) all revoke every JWT and clear the API key, with fail-safe ordering.
- **Password reset.** 30 min token (`tokens.py:33`), single-use `consume_once` (`password.py:283`), all outstanding links die on any revoke (`:278`), delivered in the URL fragment (`:161`), enumeration-safe body and timing (`:145-152,167`), reuse policy enforced before burning the link.
- **Email verification.** Token in fragment (`scheduler_helpers/registration.py:172`), exp pinned to the pending row, structurally single-use (pending rows deleted, `users.email_hmac` unique), enumeration-safe register (bcrypt parity at `registration.py:348,372`).
- **MFA.** Secret Fernet-encrypted; TOTP replay blocked by monotonic counter (`tokens.py:260-276`); backup and break-glass codes stored as peppered HMAC; challenge token single-use and revocation-gated (`login.py:150-190`); break-glass mints a degraded `amr` that can never pass step-up.
- **Admin MFA freshness.** `require_admin_mfa` requires JWT + `amr` containing `mfa` + `auth_time` within 15 min, bounded against future skew, API keys always fail (`auth.py:449-481`); refresh never adds `mfa` and does not reset `auth_time` (`login.py:501-517`).
- **API key storage.** `bl_` + 320-bit random, stored as SHA-256 (`auth.py:61-69`), exact-hash indexed lookup; plan re-checked on every use so a downgrade disables it (`auth.py:304-309`).
- **No cookie-based auth on the backend.** No `request.cookies` / `Cookie()` use anywhere in `src/api` or `main.py`; SSE (`jobs.py:949`) uses the bearer header via `CurrentUser`; the only query-string credential is the download token (A-8). Backend CSRF therefore N/A.
- **FE session cookie.** No custom `cookies` config in `lib/auth.ts`, so Auth.js v5 defaults apply: `__Secure-` prefix, HttpOnly, Secure, SameSite=Lax. `next-auth` 5.0.0-beta.32.
- **Refresh token not exposed to client JS through the session.** `session` callback (`lib/auth.ts:232-243`) returns only `accessToken` and `error`. (It does transit the login page JS once, on the way from `/auth/login` into `signIn`; acceptable, the same page holds the password.)
- **Proxy fail-open fix (#153).** `proxy.ts:53` requires `req.auth?.user`, so an Auth.js error object fails closed; public routes matched on segment boundaries (`:44-46`).
- **callbackUrl open redirect.** `proxy.ts:55` sets `callbackUrl` but the login page ignores it and always `router.push("/dashboard")`; `signOutSafely` callers pass constant paths; Auth.js's own redirect callback defaults to same-origin. No open redirect.
- **FE CSRF.** Only two route handlers exist (`app/api/auth/[...nextauth]`, Auth.js built-in CSRF; `app/api/session/refresh`, SameSite=Lax and the worst forged outcome is an extra rotation). No server actions (`"use server"` absent).
- **CORS.** Explicit https allowlist, no wildcard, no regex origins (`main.py:70-75`, `settings.py:475-490`); see A-11 for hardening only.

---

## 5. Suggested fix order

1. A-1 + A-5 together (logout that revokes the refresh token, plus family revocation on reuse).
2. A-2 + A-4 + A-7 together: re-auth (password) on API-key minting and MFA enable; reject API-key callers on `/auth/api-key`, `/auth/mfa/*`, `/auth/change-password`.
3. A-3 MFA failure counter.
4. F-01 program (origin lockdown, then CF-aware client IP) with A-9 removed first, then F-01b counter-memory decoupling, F-15, F-25.
5. A-6, A-8, A-10, A-11, F-16.
