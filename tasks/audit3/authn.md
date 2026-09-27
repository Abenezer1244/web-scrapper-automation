# Audit 3, leaf-1.2 (AN): authentication, sessions, CSRF, CORS, security headers

Backend: `C:/Users/Windows/bl-wt-secaudit3` at `786efcf0` (origin/main, what production runs).
Frontend: `bridgeleads-web` origin/master `e42d5d0`, read from a separate worktree `C:/Users/Windows/bl-web-audit3-leaf12`.
Method: I read the code first, then reproduced locally against the real `main.app` in-process (httpx ASGITransport), using the isolated DB `bl_audit3_authn_test` and Redis db 14. The worktree has no `.env`, so no production config was loaded. After that I sent 26 unauthenticated GET/OPTIONS requests to production, at least 1.2 s apart.
Harness scripts and their raw output are in `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/753293f5-47cc-4259-bfe7-d8c5df0966b7/scratchpad/an/` (called `scratch/an/` below): `authn_harness.py` / `harness.out`, `download_after_logout.py`, `err500.py`, `probe1.py` / `probe1.out`, `probe2.py` / `probe2.out`.

## Check 4: Authentication

### Session lifecycle as built
| Aspect | Implementation (786efcf0 / e42d5d0) | Tested how | Result |
|---|---|---|---|
| Creation | `/auth/login` returns a bcrypt(12) check with a dummy hash for unknown emails, then `create_token_pair` (`src/api/auth.py:203-220`). MFA accounts get only a 5-minute challenge token (`auth_helpers/login.py:85-96`). | harness s8 | Password login on an MFA account returned `mfa_required` and no access token. PASS. |
| Storage | Backend: stateless HS256 JWTs. FE: Auth.js v5 beta.32 JWT-strategy cookie that seals the access and refresh tokens (`lib/auth.ts:232-263`). The access token is exposed to page JS through `/api/auth/session` (`lib/auth.ts:265`). The refresh token is not. | code | As described. The JS-readable access token is prior W-4, an accepted design. |
| Access expiry | 1 h (`src/api/auth.py:75`) | harness s6, expired token | 401. PASS. |
| Refresh expiry | 7 days, and every rotation mints a fresh 7-day `exp` (`src/api/auth.py:76,196`) | harness s7 | See AN-2. |
| Absolute lifetime | None. `auth_time` is carried forward (`auth_helpers/login.py:537-544`) but never compared to a cap. FE `maxAge` is a rolling 7 days (`lib/auth.ts:234`). | harness s7 | A refresh token whose session authenticated 90 days ago rotated with 200 and got a new 7-day `exp`. FAIL, AN-2. |
| Rotation and reuse detection | Single-use via SET NX (`auth_hardening.py:67-87`). A 30 s grace window returns the same pair (`:146`, `login.py:492-501`). After the grace window, a replay revokes the whole session family (`login.py:508-516`). | harness s1 | Replay inside 30 s gave the same pair. Replay after 32 s gave 401, and R2 and A2 then both returned 401. PASS. |
| Logout, server side | `/auth/logout` blacklists the jti of each presented token and revokes its family (`routes/auth.py:303-354`). An API-key-only caller gets 401. | harness s2, s11 | After logout(A2,R2), refresh R2 = 401, A2 = 401, and A1 (the same family, not presented) = 401 on `/auth/me`. EXCEPTION: `/jobs/{id}/download`, see AN-1. |
| Logout, FE | The Auth.js `events.signOut` hook calls `/auth/logout` server-side with bearer + refresh token (`lib/auth.ts:121-145`, commit `0790629`, merged in #161). `@auth/core` awaits `events.signOut` (`node_modules/@auth/core/lib/actions/signout.js:18`). | code | Fixed in the code on master. Live deployment not verified (server code). |
| Logout-all | Revokes by user timestamp (durable in the DB, fail-closed cache) and clears the API key (`routes/auth.py:357-384`). No FE caller exists (grep of app/components/lib), so users have no "sign out everywhere" control. | code | AN-12 (INFO). |
| Password change | Requires a session (API key gets 403). Revokes all tokens before commit and clears the API key (`auth_helpers/password.py:35-104`). | harness s3 | Old access = 401, old refresh = 401, API key = 401. PASS. There is no per-account throttle: AN-3. |
| Password reset | 30-minute JWT, reset audience (`auth_helpers/tokens.py:29-79`). Single use via consume_once. Any sibling link minted before a revoke dies (`password.py:278-283`). Link is in the URL fragment (`:161`). Response is identical whether or not the email exists, and `once_per` runs unconditionally (`:151`). Reset revokes all sessions and clears the API key (`:298-309`) and does not touch MFA. | harness s5 | t1 = 200. t1 reuse = 400. Sibling t2 = 400. Access token used as a reset token = 400. forgot-password for existing and missing emails gave the same status and body. PASS. |
| Email verification | Production flag is ON (live `GET /auth/config` returned `"email_verification_enabled":true`). The verify token is a JWT with the verify audience and `exp` pinned to the pending row. It is single use by construction: pending rows are deleted and `users.email_hmac` is UNIQUE. The password is set at verify time, which blocks pre-hijacking (`auth_helpers/registration.py:396-510`). | code + live | Not exercised end to end (needs the Celery mail path). Enumeration-safe neutral 200 confirmed in code only (`registration.py:315-395`). |
| MFA | Enrolment: setup needs the password plus a session (`routes/auth.py:419-433`). Enable revokes every session and the API key (`mfa.py:101-118`). Login factor lockout: 5 failures give a 15-minute lock (`auth_hardening.py:721-798`, `login.py:174-183`). TOTP replay is blocked by a monotonic counter (`tokens.py:260-276`). Break-glass: 128-bit codes, atomic consume, degraded `amr` (`login.py:213-389`). | harness s8 | setup with a wrong password = 400. enable = 200, and the pre-MFA session then returned 401. 5 wrong TOTPs, then the CORRECT code = 429. A challenge token used as a bearer = 401. PASS. `/mfa/enable` is not covered by the lockout: AN-8. |
| API keys | `bl_` + `token_urlsafe(40)`, stored as SHA-256 and looked up by exact hash. Plan is re-checked on every use (`src/api/auth.py:61-69,304-343`). Minting needs the password plus a session (`routes/auth.py:497-520`). There is no scope model: a key acts as the full user on CurrentUser routes. | harness s3 | The key works on `/auth/me` and `/auth/onboarding` (by design). It gets 403 on change-password, api-key, mfa/setup, mfa/enable and mfa/disable. It dies after a password change. The admin gate accepts it: AN-4. |
| JWT algorithm pinning | Every decode passes `algorithms=["HS256"]` with a pinned `aud` and `iss` (`src/api/auth.py:228-251`, `tokens.py:68-75,149-156,209-216`, `jobs.py:1307-1327`). PyJWT 2.13.0. There are no asymmetric keys. | harness s6 | alg=none = 401. HS256 with a wrong key = 401. HS512 with the REAL key = 401. RS256 header with an HMAC signature = 401. Missing aud = 401. Refresh token as bearer = 401. Access token at /refresh = 401. Deactivated user (access and refresh) = 401. Non-existent user = 401. Control (valid token) = 200. PASS. |
| Cookie flags (live) | Unauthenticated GET of `/api/auth/csrf` on app. and the apex returned `__Host-authjs.csrf-token; Path=/; HttpOnly; Secure; SameSite=Lax` and `__Secure-authjs.callback-url; Path=/; HttpOnly; Secure; SameSite=Lax`. No Domain attribute, so host-only. `lib/auth.ts` has no `cookies` override, so the session cookie is the Auth.js default `__Secure-authjs.session-token` (HttpOnly, Secure, SameSite=Lax). | live: `scratch/an/probe2.out` [4]-[6] | Flags as expected. The session-token cookie itself was not observed live (no login). |
| Login brute force | Email lock at 5, 10 and 20 failures, capped at 15 minutes (`auth_hardening.py:445-527`). | harness s10, rotating XFF | 5 x 401, then 429. The email lock arms. The IP half depends on F-01 (leaf 1.8). |

### Prior audit-2 items, re-measured on 786efcf0
| Prior | Status now | Evidence |
|---|---|---|
| A-1 logout does not revoke | FIXED (backend reproduced; FE fixed in code, deploy not verified) | harness s2; `lib/auth.ts:121-145` |
| A-2 API key mintable from a stolen session | FIXED | `routes/auth.py:497-511`; harness s3 (403 by API key; wrong password gives 429 after 10) |
| A-3 no TOTP lockout | FIXED for `/login/mfa` and `/mfa/disable`. Residual on `/mfa/enable` = AN-8 | harness s8; `mfa.py:150` |
| A-4 hostile MFA enrolment | FIXED | `routes/auth.py:419-433`; harness (setup wrong password = 400, API key = 403) |
| A-5 no refresh reuse detection | FIXED | harness s1 |
| A-6 no absolute lifetime | OPEN, AN-2 | harness s7 |
| A-7 change-password oracle | CHANGED: API-key callers now 403. Still no per-account throttle, AN-3 | harness s4 |
| A-8 download links | CHANGED: `is_active` added (`jobs.py:1407`). Access JWT still accepted in `?token=`, and the 48 h link is not single use. New family-revocation gap, AN-1, AN-11 | harness + `download_after_logout.py` |
| A-9 Auth.js password path | OPEN, AN-7 | `lib/auth.ts:206-225` |
| A-10 admin gate accepts API keys | OPEN, AN-4 | harness s9 (200) |
| A-11 CORS hardening | OPEN, AN-9 | `main.py:68-82`, `settings.py:475-490` |
| F-15 no API HSTS | OPEN, AN-6 | live `scratch/an/probe1.out` [1]-[4] |
| F-16 500s lack headers and CORS | OPEN, AN-5 | `err500.py` |
| F-01 dead IP limiter | OPEN in code (`rate_limit.py:54-60` still has no 100.64.0.0/10; `start.sh:97` has no proxy headers). Owned by leaf 1.8. Listed here because AN-3 depends on it | code |
| F-01b email lock never fires | CHANGED: 5 rapid failures lock (harness s10). Slow spraying below 5 per 15 minutes stays under the lock by design | harness s10 |
| W-3 FE CSP, W-5 Vercel ACAO *, W-6 serverActions localhost | OPEN (AN-10, AN-13) | live `scratch/an/probe2.out`, `next.config.ts:4-14,59-85` |

## CSRF

- Backend: there is no cookie authentication anywhere. A grep for `request.cookies`, `Cookie(`, `set_cookie`, `APIKeyCookie` in `src/api` and `main.py` returns 0 hits. Every protected route resolves the caller from the `Authorization` header (`src/api/auth.py:286-402`). The one query-string credential is `?token=` on `GET /jobs/{id}/download` (`jobs.py:1254`), and it is not ambient. Backend CSRF therefore has no attack path.
  - Negative control: an anonymous request to `/auth/me` returned 401 (live and harness).
- GET routes that change state (all need a bearer or a download token, so none is CSRF-reachable):
  - `GET /scrapers/{id}/records` upserts `user_record_views.last_viewed_at` (`scrapers.py:1175-1192`).
  - `GET /jobs/{id}/download` and the batch and segment downloads call `mark_leads_downloaded` (`jobs.py:1585`).
  - Side note (AN-11): a mail-scanner prefetch of the emailed 48 h link would record a download.
- Auth.js:
  - Sign-in, sign-out and callback POSTs use the Auth.js double-submit CSRF token. The live `__Host-authjs.csrf-token` has the `__Host-` prefix, is HttpOnly, and is SameSite=Lax.
  - `POST /api/session/refresh` (`app/api/session/refresh/route.ts`) has no CSRF token. SameSite=Lax keeps the cookie off cross-site POSTs. The worst same-site forgery is an extra rotation, and it returns no tokens in the body.
- Next server actions: none exist (grep for `"use server"` found 0 files). `experimental.serverActions.allowedOrigins` still lists `localhost:3000` and `localhost:3005` (`next.config.ts:4-14`). The config is dead but misleading (AN-13).

## CORS

Code: `main.py:68-82`.
- Explicit list: `https://bridgeleads.io`, `https://app.bridgeleads.io`, `https://bridgeleads-web.vercel.app`, plus `settings.get_allowed_origins()`.
- `get_allowed_origins()` accepts any `https://` origin, and any origin that starts with `http://localhost` or `http://127.0.0.1` (`settings.py:475-490`), from the `ALLOWED_ORIGINS` env.
- `allow_credentials=True`, even though no cookie auth exists.

Live probes against api.bridgeleads.io (`scratch/an/probe1.out` [7]-[16]):

| Origin | OPTIONS preflight `/auth/me` | GET `/health`: ACAO | ACAC |
|---|---|---|---|
| https://evil.example | 400 `Disallowed CORS origin`, no ACAO | none | `true` (emitted without ACAO, has no effect) |
| null | 400, no ACAO | none | `true` (no effect) |
| http://localhost:3000 | 400, no ACAO | none | `true` (no effect) |
| https://bridgeleads.io.evil.example | 400, no ACAO | none | `true` (no effect) |
| https://app.bridgeleads.io (positive control) | 200, ACAO `https://app.bridgeleads.io` | `https://app.bridgeleads.io` | `true` |

- No origin is reflected. `null` and suffix or prefix look-alikes are rejected.
- `https://bridgeleads-web.vercel.app/login` is served live by the same Vercel project (same CSP; `probe2.out` [9]), so that allowlisted origin is not dangling.
- Remaining hardening is in AN-9.
- Vercel sends `Access-Control-Allow-Origin: *` on the FE's public HTML and JS, with no ACAC (AN-13).

## Security headers

Measured live on 2026-09-26 (`scratch/an/probe1.out`, `probe2.out`).

| Header | api. (200, 401, 404, preflight) | api. 500 (local repro) | bridgeleads.io / app. HTML | app. JS asset | Code |
|---|---|---|---|---|---|
| CSP | `default-src 'none'; frame-ancestors 'none'` | MISSING | see the evaluation below | same CSP as the HTML | `security.py:534`; `next.config.ts:59-85` |
| HSTS | MISSING (the http URL 301s at Cloudflare) | MISSING | `max-age=31536000; includeSubDomains` (no preload) | same | `security.py:544` sets it only when `scheme=="https"`, which is never true behind the proxy; `next.config.ts:55` |
| X-Content-Type-Options | nosniff | MISSING | nosniff | nosniff | `security.py:530` |
| XFO / frame-ancestors | DENY / 'none' | MISSING | DENY / 'none' | DENY | |
| Referrer-Policy | strict-origin-when-cross-origin | MISSING | same | same | |
| Permissions-Policy | geolocation, microphone, camera=() | MISSING | camera, microphone, geolocation=() | same | |
| COOP / CORP | same-origin / same-site | MISSING | absent | absent | `security.py:541-542` |
| ACAO | per allowlist | MISSING, even for the allowed origin | `*` (Vercel edge) | `*` | |

The 404 (`{"detail":"Not Found"}`) and 401 responses on the API carried the full header set live. The 500 case comes from `scratch/an/err500.py`: a throwaway in-process route raised RuntimeError. The response was `{"detail":"Internal error","ref":...}` with none of the headers above. Controls `/health` and `/nope` had all of them (AN-5).

FE CSP evaluation (live, identical to `next.config.ts:59-85`):
- `script-src 'self' 'unsafe-inline' 'unsafe-eval'`: gives no script-injection protection, so XSS defence rests on React escaping alone.
- `connect-src 'self' https://*.bridgeleads.io https://*.stripe.com`: a wildcard that allows exfiltration to any Stripe subdomain. The browser does not call Stripe APIs directly.
- `frame-src https://my.spline.design https://js.stripe.com`.
- `img-src data: blob:`.
- No `report-to` or `report-uri`.
- Good: `default-src 'self'`, `base-uri 'self'`, `object-src 'none'`, `form-action 'self'`, `frame-ancestors 'none'`.

The apex HSTS with `includeSubDomains` covers `api.bridgeleads.io` for any browser that has already visited `bridgeleads.io`. That lowers the weight of the missing API HSTS (AN-6).

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| AN-1 | P3 | Session revocation bypass | GET /jobs/{id}/download | src/api/routes/jobs.py:1384-1391 checks jti blacklist and logout-all but never TokenBlacklist.is_family_revoked, and jobs.py:1254 accepts the access JWT from ?token=. cmd:scratch/an/download_after_logout.py: after logout(A2,R2), pre-rotation A1 gets 401 on /auth/me and /jobs/{id}/results but 200 on /download via header AND via ?token=, body contains the lead row (control before logout 200; A2 itself 401) | Attacker holds any access token of the session (1 h life), e.g. copied before the user rotated or signed out | A signed-out or reuse-burned session keeps exporting the full lead CSV (owner PII) for the rest of that token's hour. This defeats the A-1/A-5 family revocation on the most data-rich route. A session JWT in the URL also lands in edge logs. | In the non-download branch call is_family_revoked(payload.get("fam")) (better: reuse get_auth_context), and accept session JWTs only from the Authorization header, never ?token= | Login, refresh, logout(A2,R2); GET /jobs/{id}/download with A1 by header and by ?token= returns 401 | REPRODUCED |
| AN-2 | P3 | Session lifetime | /auth/refresh | src/api/routes/auth_helpers/login.py:537-544 copies auth_time and never checks its age; src/api/auth.py:196 new 7-day exp each rotation; lib/auth.ts:234 rolling maxAge. cmd:scratch/an/authn_harness.py s7: refresh token with auth_time 90 days old gives 200 and a fresh 7-day refresh | Stolen refresh token or Auth.js cookie | A session that refreshes at least weekly never re-authenticates, so a stolen cookie is usable indefinitely until logout-all or a password change | Reject refresh when now - auth_time exceeds a cap (e.g. 30 days) with 401; the FE then routes to login | Refresh token with auth_time older than the cap returns 401 | REPRODUCED |
| AN-3 | P3 | Password oracle / throttling | POST /auth/change-password | src/api/routes/auth_helpers/password.py:35 only IP-keyed rate_limit, :40 distinct 400 "Current password is incorrect", no BruteForceProtection or per-account key (contrast routes/auth.py:98 reauth per account). cmd:scratch/an/authn_harness.py s4: 15 wrong guesses with rotating X-Forwarded-For all 400, no 429; control /auth/api-key reauth 429 after 10; control single IP 429 after 10 | Stolen session (access token or Auth.js cookie); production IP key rotating per F-01 (rate_limit.py:54-60) | Online guessing of the durable password at bcrypt speed, turning a temporary session into the password (reusable elsewhere and surviving logout-all) | Call _reauthenticate (per-account reauth:{user.id} bucket) in change-password before verify_password, and record failures | 11 wrong current_password submissions from rotating IPs return 429 | REPRODUCED |
| AN-4 | P3 | Authz step-up | require_admin / GET /billing/activation-funnel | src/api/auth.py:477-494 checks is_admin and mfa_enabled but not ctx.auth_method or amr; src/api/routes/billing.py:121. cmd:scratch/an/authn_harness.py s9: admin API key gets 200 on /billing/activation-funnel; control POST /scrapers/connectors (require_admin_mfa) gets 403 | Admin on a Business or Agency plan with an API key, and the key leaks | Admin analytics readable with a static key and no MFA session. Read-only aggregate data | Reject auth_method == "api_key" in require_admin (or use require_admin_mfa for all admin routes) | Admin API key on /billing/activation-funnel returns 403 | REPRODUCED |
| AN-5 | P3 | Security headers / CORS on errors | main.py exception handler | main.py:105-111 registers on Exception, so Starlette runs it in ServerErrorMiddleware outside SecurityHeadersMiddleware and CORSMiddleware. cmd:scratch/an/err500.py: 500 response has no CSP, XCTO, XFO, HSTS, CORP or ACAO; /health and 404 controls have all | Any unhandled exception | The browser cannot read the error ref (no ACAO), so users see a generic network error, and error bodies go out without nosniff or CSP. Low | Wrap handlers in a pure ASGI outermost middleware that adds headers, or catch in a middleware inside CORS and return JSONResponse there | A route raising RuntimeError returns 500 with ACAO for the allowed origin and nosniff | REPRODUCED |
| AN-6 | P3 | Transport | api.bridgeleads.io HSTS | live:GET https://api.bridgeleads.io/health, /auth/me, 404 have no Strict-Transport-Security; src/api/middleware/security.py:544 only when scheme https; start.sh:97 no --proxy-headers | Network attacker and a client that never visited the apex (the apex HSTS includeSubDomains covers api. otherwise) | First plain-http request to the API can be downgraded. Cloudflare 301s http, but without HSTS the redirect itself is strippable | Emit HSTS unconditionally in production (or trust X-Forwarded-Proto from the proxy), or enable HSTS at Cloudflare | Response on api. carries max-age >= 31536000 | REPRODUCED |
| AN-7 | P3 | Latent lockout DoS | Auth.js authorize() password path | lib/auth.ts:206-225 still proxies email and password to /auth/login from Vercel; production uses email verification (live /auth/config email_verification_enabled true), so the only UI caller (register legacy flow, app/(auth)/register/page.tsx:260) is dead in prod but the path is reachable via POST /api/auth/callback/credentials with a self-fetched CSRF token | F-01 fixed later (IP keys become real) | All attempts through this path share Vercel egress IPs, so an attacker can drive the uncapped IP lockout ladder against them. No impact today | Remove the password branch and keep token adoption only | authorize({email,password}) returns null without a network call | CONFIRMED |
| AN-8 | P3 | MFA brute force | POST /auth/mfa/enable | src/api/routes/auth_helpers/mfa.py:53-120: only rate_limit mfa-user 10/min (:61), no MfaFailureGuard (contrast :150 on disable and login.py:174) | Stolen session and a victim with a pending (setup but not enabled) secret | About 14,400 TOTP guesses a day against the pending secret (3 valid codes, about 4% a day); success returns the backup codes to the attacker and revokes the owner's sessions. The attacker still needs the password to log in | Apply MfaFailureGuard.ensure_not_locked, record_failure and clear in mfa_enable_for_user | 5 wrong enable codes then a correct one returns 429 | CONFIRMED |
| AN-9 | P3 | CORS hardening | CORSMiddleware config | main.py:73 bridgeleads-web.vercel.app allowlisted, main.py:76 allow_credentials=True with no cookie auth; src/config/settings.py:486-488 startswith("http://localhost") also admits http://localhost.attacker.tld from env. live: no reflection for 4 hostile origins (probe1.out [7]-[15]) | Operator adds a bad ALLOWED_ORIGINS value | None with current prod config. A config footgun: a credentialed trusted origin would be widened | allow_credentials=False; parse with urlsplit and require hostname in {localhost,127.0.0.1} for http; drop the vercel.app origin if unused | Unit test: get_allowed_origins drops http://localhost.evil.tld | CONFIRMED |
| AN-10 | P3 | CSP | FE next.config.ts headers | live:GET https://app.bridgeleads.io/login CSP script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src https://*.stripe.com; frame-src my.spline.design; no report-to. next.config.ts:78,41,83 | An XSS bug elsewhere (none found by this leaf) | CSP adds no script-injection containment, and the access token is JS-readable via /api/auth/session | Nonce-based CSP via proxy.ts; drop unsafe-eval; narrow connect-src to https://api.bridgeleads.io; frame-src 'none' unless used | CI fetch of the preview CSP asserts no unsafe-eval and no *.stripe.com | CONFIRMED |
| AN-11 | INFO | Download link lifetime | Emailed download links | src/workers/tasks_helpers/status.py:50-57 48 h token in the query string; jobs.py:1200 docstring says "single-use" but no consume_once; jobs.py:1585 GET marks leads downloaded | Mail forwarding, edge logs or link scanners | A link stays valid for 48 h and replayable; a scanner prefetch records a download | Correct the docstring; consider 24 h; do not mark downloaded on HEAD or scanner user agents | n/a | CONFIRMED |
| AN-12 | INFO | Session management UX | FE has no logout-all caller | cmd:grep -rn "logout-all" app components lib in bridgeleads-web e42d5d0 returns only generated types | A user who suspects a stolen session | The only way to kill other sessions is a password change or reset | Add "Sign out of all devices" calling /auth/logout-all | n/a | CONFIRMED |
| AN-13 | INFO | Header hygiene | Vercel edge and next.config | live:ACAO * on https://bridgeleads.io/ HTML and the app JS asset, no ACAC; next.config.ts:4-14 serverActions allowedOrigins lists localhost:3000 and 3005 with zero server actions | none | Public content only; misleading dead config | Remove the dead allowedOrigins entries | n/a | CONFIRMED |

Counts: P0 0, P1 0, P2 0, P3 10, INFO 3.

## Not verified

- **FE sign-out deployment.** Whether the deployed Vercel build includes the `events.signOut` backend revocation (commit `0790629`). It runs on the server and cannot be seen from an unauthenticated probe. I have no production credentials to sign in and out.
- **Live session-token cookie flags.** The `__Secure-authjs.session-token` cookie was not observed live, because that needs a login. Its flags are taken from Auth.js defaults and the absence of a `cookies` override in `lib/auth.ts`.
- **AN-3 in production.** Production exploitability depends on the IP limiter being dead (F-01). That is documented in code comments (`password.py:416-422`) and was measured live by audit 2. I did not re-measure it, because it requires POST requests to production, which this leaf's probe rules forbid. Leaf 1.8 owns F-01.
- **Email verification end to end.** Not exercised: it needs the Celery mail path. Register enumeration parity in verified mode is confirmed from code only.
- **A real 500 from production.** Not triggered, to avoid destructive probing. AN-5 was reproduced locally with an in-process route. No file was edited.
- **Direct access to the Railway origin (F-28).** Not probed. The origin hostname is not in the code, and this belongs to another leaf.
- **Break-glass redemption and backup-code login.** Not exercised live in the harness. Read in code only (`login.py:213-389`, `tokens.py:278-293`).
