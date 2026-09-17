# Auth / Session / Password / Account-Lifecycle Audit

**Worktree:** `C:/Users/Windows/bl-wt-secaudit` @ `60f1b00` (tip of `origin/main`)
**Method:** static read of the full auth surface + mechanical route scan. No code executed, no pytest, no prod probing by me. Production facts below are from the team lead's probes.
**Scope:** `src/api/auth.py`, `src/api/routes/auth.py`, `src/api/routes/auth_helpers/*`, `src/api/middleware/auth_hardening.py`, `src/api/middleware/rate_limit.py`, `src/utils/mfa.py`, `src/utils/crypto.py`, `main.py`.

---

## 0. PRODUCTION FACTS RECONCILED

1. **Cloudflare now fronts `api.bridgeleads.io`** → every IP-keyed rate limit is non-functional (team lead's F-01).
2. **`EMAIL_VERIFICATION_ENABLED` is ON in prod** (register returns 200, not the legacy 201).

### 0.1 Mechanism of F-01: (b) and (c) ELIMINATED, (a) vs (b') UNRESOLVED

**Eliminated — Redis is healthy.** `BruteForceProtection.check()` returned a real `Retry-After: 28`. That value can only come from a successful Redis TTL read at `auth_hardening.py:544`; the method **fails open and returns silently** on any `RedisError` (`auth_hardening.py:556-561`). Both limiters share `settings.REDIS_URL` (`rate_limit.py:47-51`, `auth_hardening.py:19-23`). So `rate_limit`'s ZSET pipeline (`rate_limit.py:146-153`) is executing normally and `zcard` is simply returning <=10. This rules out candidate (b) *Redis fail-open* and candidate (c) *per-process fallback spread*, since the fallback at `rate_limit.py:171-178` only engages inside the `RedisError` handler.

**Therefore: the key is dispersing.** `redis_key = f"rl:{zone}:{key_id}"` (`rate_limit.py:141`) takes a near-fresh value per request.

**UNRESOLVED — two models fit the data equally.** I originally concluded the fixed-XFF test proved `_is_trusted_proxy(direct_ip)` is False. **That was wrong; I concede it.**

| | Model A (mine) | Model B (sec-deploy's) |
|---|---|---|
| Peer | public (CF colo) | private Railway hop |
| XFF branch (`rate_limit.py:101`) | skipped | entered |
| Key | `direct_ip` = rotating CF colo | `parts[-1]` = rotating CF edge Railway appended |
| Fixed XFF effect | ignored (never read) | ignored (sits at `parts[0]`, left of `parts[-1]`) |

Both produce identical all-200s. **The test does not discriminate.**

**Independent evidence favours Model B (i.e. against my original call):**
- `rate_limit.py:74` — `_TRUSTED_PROXY_HOPS = 1` with the comment *"Railway/Fly = 1"*. Only meaningful if the XFF branch is actually entered on Railway.
- Stronger: **pre-Cloudflare, per-IP limiting evidently worked.** Under Model A the key would have been the stable Railway edge IP — one shared bucket, constant false 429s for every user. That did not happen. Under Model B, pre-CF `parts[-1]` was the real client. Only Model B explains the working prior state.

**The fix differs by model — this is why it matters:**
- **Model B** → minimal fix is `TRUSTED_PROXY_HOPS=2` (`settings.py:248`): CF appends the client, Railway appends CF's edge, so `parts[-2]` is the client.
- **Model A** → hops is inert; must add Cloudflare's published ranges to `_TRUSTED_PROXY_NETWORKS` (`rate_limit.py:54-60`), then trust `CF-Connecting-IP`.

**CRITICAL CAVEAT on `hops=2`:** it reintroduces the I1 spoofing bypass for anyone reaching the origin *directly*. Bypassing CF, an attacker sends `XFF: evil`, Railway appends its peer, and `parts[-2]` = `evil` — attacker-chosen rate-limit key. **Origin lockdown (Cloudflare Authenticated Origin Pulls / Railway allowlist) becomes mandatory, not optional, under that fix.** Whether the Railway origin is directly reachable is the single most important thing to check alongside this; I could not test it.

**One diagnostic resolves the model:** log `request.client.host` once on any auth route and compare against Cloudflare's ranges.

### 0.2 Which paths lose protection, which are saved

**LOST — `zone="auth"`, IP-keyed, no `identifier=` (12 auth call sites):**

| Endpoint | Line |
|---|---|
| `POST /auth/login` | `auth_helpers/login.py:47` |
| `POST /auth/login/mfa` (outer) | `auth_helpers/login.py:106` |
| `POST /auth/login/break-glass` (outer) | `auth_helpers/login.py:211` |
| `POST /auth/refresh` | `auth_helpers/login.py:418` |
| `POST /auth/change-password` | `auth_helpers/password.py:28` |
| `POST /auth/forgot-password` | `auth_helpers/password.py:106` |
| `POST /auth/reset-password` | `auth_helpers/password.py:141` |
| `POST /auth/mfa/setup` | `auth_helpers/mfa.py:28` |
| `POST /auth/mfa/enable` (outer) | `auth_helpers/mfa.py:59` |
| `POST /auth/mfa/disable` (outer) | `auth_helpers/mfa.py:129` |
| `POST /auth/register` | `auth_helpers/registration.py:221` |
| `POST /auth/verify-email` | `auth_helpers/registration.py:405` |

Also (outside my scope, confirms F-01): `webhooks.py:164,180`, `billing.py:1710`.

**SAVED — explicit `identifier=`, account-keyed, IP-independent:**

| Guard | Line | Protects |
|---|---|---|
| `mfa-issue:{user.id}` | `login.py:86` | challenge-token farming |
| `mfa-verify:{user_id}` | `login.py:132` | **TOTP / backup-code guessing** |
| `mfa-breakglass:{user_id}` | `login.py:235` | **break-glass code guessing** |
| `mfa-user:{current_user.id}` | `mfa.py:61`, `mfa.py:132` | enable/disable 2nd-factor guessing |
| `identifier=current_user.id` | ~25 sites across jobs/batches/billing/segments/scrapers/analytics | all authenticated zones |

**Headline: every second-factor guessing surface survived.** The per-user buckets were added in the H2-P3 Codex round precisely because per-IP limiting was judged insufficient against rotating IPs. That decision is the only reason MFA verification is still throttled today.

**ALSO SAVED — the email half of `BruteForceProtection`**: `bf:email:{blind_index(email)}` / `bf:lock:email:{...}` (`auth_hardening.py:540,581,600-605,663-669`). No IP component — which is exactly why the spoofed XFF could not reset the lockout. The lead's C-04 is correct as written.

**LOST — the IP half**: `bf:ip:{ip}` with the full uncapped ladder incl. the 24h tier (`auth_hardening.py:586-594`). See F-01b.

---

## 1. FINDINGS

### F-01b [P1] — The 15-minute email-lockout cap was explicitly premised on the IP ladder, which is now dead

**CLASSIFICATION:** CONFIRMED VULNERABILITY (distinct consequence of F-01)

**EVIDENCE**
- `auth_hardening.py:402-407` — ladder: 5→1min, 10→5min, 20→30min, 50→24h.
- `auth_hardening.py:484` — `_EMAIL_LOCKOUT_CAP_SECONDS = 15 * 60`; `:491` — `_EMAIL_COUNTER_TTL = _EMAIL_LOCKOUT_CAP_SECONDS` (15-min **sliding** memory).
- `auth_hardening.py:475-483` states the compensating-control argument verbatim:
  > *"Cap email-driven lockout at a short ceiling… **The per-IP counter keeps the FULL escalation (incl. 24h) against the actual source of an attack**, where a long lockout is the desired outcome."*
- `auth_hardening.py:586-594` — the IP arm, uncapped (cap arg `0`), 24h memory, keyed on the broken `client_ip()`.

**ATTACK PATH** — The email cap is safe *only because* the IP ladder was supposed to catch the source. With the IP key dispersing, that half never accumulates and the design's own stated compensating control is gone. Against one targeted account:
- The email counter's memory is a **15-min sliding window**. **4 guesses per 15 minutes never crosses the 5-failure threshold at all** — zero lockouts, indefinitely, ~384 guesses/day/account.
- Even pacing aggressively, past 50 failures every lock is still capped at 15 min → ~480/day/account, forever, no escalation.

Against a 10-char minimum (`schemas.py:28-29`) this is not brute force. Against **credential stuffing** (one or two known-reused passwords per account) it is entirely sufficient, and invisible: neither counter escalates and the notification fires only at exactly 10 email failures (`auth_hardening.py:618`), which a paced attacker never reaches. Combined with unthrottled spraying (lead's F-02): unlimited low-rate guesses against every account simultaneously, no limit, no escalation, no alert.

**FIX** — Fixing `client_ip()` restores it (same root cause). Independently and immediately: **decouple `_EMAIL_COUNTER_TTL` from the lock cap** (`auth_hardening.py:491`). A long counter *memory* with a short *lock* still defeats the account-lockout DoS the cap exists to prevent, while closing the "4 per 15 min forever" hole. One-constant change.

---

### F-08 [P2] — `/auth/forgot-password` is an unthrottled email-bomb primitive

**CLASSIFICATION:** CONFIRMED VULNERABILITY

**EVIDENCE**
- `auth_helpers/password.py:100-133` — the whole handler. Only throttle is `rate_limit(request, zone="auth")` at `:106` — IP-keyed, therefore dead.
- `password.py:116-129` — every request for an existing address mints a token and queues a real Resend send. **No per-address guard of any kind.**
- `src/workers/delivery.py:334-379` — `send_password_reset_email` checks only `RESEND_API_KEY` then sends. No throttle, no dedup.
- **Both sibling flows have one.** Duplicate-signup notice: `once_per(key, 86400)` (`registration.py:41,102`). Verification email: Postgres advisory lock per address + 120s window + daily cap (`workers/scheduler_helpers/registration.py:129-157`). Password reset is the only member of the family with neither.

**ATTACK PATH** — Unauthenticated attacker loops `/auth/forgot-password` on any known customer address. Every request delivers a real, correctly-signed "Reset your BridgeLeads password" email. No cap, no dedup, no CAPTCHA, no working rate limit. Consequences: (1) inbox flood of authentic security emails — the classic setup for a phishing follow-up made credible by the genuine ones around it; (2) **Resend sending-domain reputation damage**, degrading deliverability for every transactional email the product sends, including job delivery and billing — this outlasts the attack; (3) many live 30-min reset tokens in flight (individually harmless: single-use, revocation-gated, fragment-delivered).

This was always latent — the IP limiter never adequately defended it. The Cloudflare collapse removed the last obstacle.

**FIX** — Add an address-keyed guard, matching the pattern used twice elsewhere: `await rate_limit(request, zone="auth", identifier=f"pwreset:{blind_index(body.email)}")` after `:106`, or `once_per(f"pwreset:{fp}", 300)` gating the `background_tasks.add_task` at `:129`. Key on `blind_index` to keep plaintext out of Redis (consistent with `auth_hardening.py:540`). **The guard must gate only the send — never the response.** The uniform 200 at `:108,133` must be preserved byte-for-byte.

---

### F-09 [P2] — `POST /auth/logout` leaves the 7-day refresh token fully valid and renewable

**CLASSIFICATION:** CONFIRMED VULNERABILITY

**EVIDENCE** — `src/api/routes/auth.py:282-312`:
```python
token = auth_header.removeprefix("Bearer ").strip()
payload = decode_secure_token(token)   # ACCESS audience only (auth.py:202-209)
jti = payload.get("jti", "")
await TokenBlacklist.add(jti, ttl)     # blacklists the ACCESS jti, and nothing else
```
The handler accepts no request body (`auth.py:283-286` — only `request`, `current_user`), so the client cannot surrender its refresh token, and nothing stamps `users.revoked_at`.

**ATTACK PATH** — User logs out on a shared machine or after suspecting compromise. The 1-hour access token dies; the **7-day refresh token is untouched**. Anyone holding it (that machine's storage, an XSS capture, an exfiltrated backup) POSTs it to `/auth/refresh` and gets a fresh privileged pair, renewable indefinitely. `/auth/refresh` checks only `is_revoked_by_user_logout_all` (`login.py:465`), which logout never sets. **Effective logout lifetime: 7 days, renewable — not "now".**

Real asymmetry inside this codebase, not a generic note: `/auth/logout-all` (`auth.py:329`), change-password (`password.py:83`), reset-password (`password.py:252`), mfa/enable (`mfa.py:112-115`), mfa/disable (`mfa.py:190-193`) and break-glass (`login.py:324`) **all** revoke. `/auth/logout` is the sole credential-ending action that does not.

**FIX** — Accept the refresh token in the logout body and `TokenBlacklist.consume_once(refresh_jti, ttl)` alongside the access jti. If the frontend cannot ship in lockstep, interim: have `/auth/logout` call `revoke_all_for_user(current_user.id)` — costs other-device sessions but makes logout mean logout.

---

### F-10 [P2] — MFA enrollment needs only a live session; a hijacked session permanently locks the owner out

**CLASSIFICATION:** CONFIRMED VULNERABILITY

**EVIDENCE** — the asymmetry is visible in one file:
- `auth.py:375-395` — `/auth/mfa/setup` and `/auth/mfa/enable` take `current_user: CurrentUser` and nothing else. `MfaEnableRequest` is `code` only (`schemas.py:199-201`). Neither helper re-verifies the password: `mfa.py:23-50`, `mfa.py:53-120` contain no `verify_password` call.
- `auth.py:398-407` — `/auth/mfa/disable` requires **password AND a second factor**: `MfaDisableRequest` is `{password, code}` (`schemas.py:209-214`), enforced at `mfa.py:145-171`.

**Turning MFA on is cheaper than turning it off.**

**ATTACK PATH** — Attacker obtains an access token (frontend XSS, shared machine, or the F-09 logout gap). They call `/auth/mfa/setup` → `/auth/mfa/enable` with a code from *their own* authenticator. `mfa.py:101-102` sets `mfa_enabled = True`; `mfa.py:111-118` stamps `revoked_at` and clears `api_key_hash` (logging everyone out, attacker included); `mfa.py:120` returns the ten backup codes **to the attacker**. The owner then logs in with the correct password and `login.py:80-91` hands them an MFA challenge they cannot answer. No self-recovery: `/auth/mfa/disable` demands a second factor they lack, and the backup codes went to the attacker. Only route back is the operator break-glass script — a manual, out-of-band human process.

The attacker gains **no read access** (logged themselves out, still lacks the password). Impact is **account denial-of-service plus a durable hostile foothold**, worse for an admin (force-enrolled anyway, `auth.py:439-445`).

**FIX** — Require the password on `/auth/mfa/enable`, exactly as `/auth/mfa/disable` already does: add `password: str = Field(max_length=72)` to `MfaEnableRequest` and a `verify_password` check before `user.mfa_enabled = True` (`mfa.py:101`). ~4 lines; makes enrollment and un-enrollment symmetric.

---

### F-11 [P2] — No refresh-token reuse detection; the 30s grace window silences the only theft signal

**CLASSIFICATION:** PARTIAL-WEAK CONTROL

**EVIDENCE**
- `auth_hardening.py:102-119` — `REPLAY_GRACE_SECONDS = 30`; `remember_rotation` caches the exchanged pair keyed by the **old** jti.
- `login.py:467-488` — on a lost `consume_once`, polls up to 1.5s (`login.py:388,392-410`) and, if the cache is populated, returns **the same live pair**; else a bare 401.

**Is the grace window exploitable? No — and that is not the bug.** The in-code rationale (`auth_hardening.py:86-101`) is sound: a replayer inside the window "gains nothing it could not have had by racing the original request." The browser-parallel-request problem it solves is real and was verified against production.

**The bug is what is missing around it: there is no reuse detection anywhere.** A post-grace replay returns a bare 401 — no family revocation, no `revoke_all_for_user`, no audit event, no alert. So:
- Attacker uses a stolen refresh token *before* the victim rotates → attacker wins `consume_once`; victim's next refresh 401s. The victim sees a spurious logout — **indistinguishable from the benign browser race this window exists to hide.**
- Attacker replays *within* 30s of the victim's rotation → both get the identical pair and **neither side sees anything wrong**. The window absorbs the one symptom that would have surfaced the theft.

Either way the attacker holds a valid renewable 7-day chain that nothing revokes.

Secondary: the grace cache is keyed on `jti` alone with no IP/UA binding (`auth_hardening.py:106-113`); the cached value is **two live bearer tokens as plaintext JSON in Redis** for 30s (`login.py:524-527`), so a Redis-read compromise in that window yields directly usable sessions.

**FIX** — Keep the window. Add detection on the post-grace branch: when `consume_once` fails **and** `recall_rotation` returns nothing, `revoke_all_for_user(user_id)` + `audit_log(request, "refresh_reuse_detected", user_id)` before the 401. RFC 9700 posture; restores the signal the window swallows.

---

### F-03a [P2] — Registration flooding, sharpened by the verification flag being ON

**CLASSIFICATION:** CONFIRMED VULNERABILITY (refines the lead's F-03)

**EVIDENCE** — with `EMAIL_VERIFICATION_ENABLED` on, the live path is `_register_user_verified`:
- `registration.py:348` **and** `:372` — **both** branches call `hash_password(...)`, a deliberate bcrypt cost-12 burn for timing parity. ~250ms CPU **guaranteed on every unauthenticated request, by design**.
- `registration.py:373-391` — the new-email branch inserts a `pending_registrations` row per attempt; `models.py:294-298` makes `email_hmac` **deliberately non-unique** (correct for anti-hijacking, unbounded for flooding).
- `registration.py:221` — only throttle is the dead IP limiter.

**ATTACK PATH** — Two amplifiers: (1) **CPU exhaustion** — unauthenticated requests each costing ~250ms of bcrypt, unthrottled, is a cheap asymmetric DoS that degrades *authenticated* traffic; the same primitive exists on `/auth/login` (`login.py:61` always verifies, even for unknown users). (2) **Unbounded row growth** — `once_per` caps the *emails*, not the *rows*; retention keys off `expires_at` (24h), so the table grows to whatever an attacker inserts in a day.

**FIX** — Add `identifier=f"register:{blind_index(body.email)}"` alongside the IP limiter; consider a cheap pre-bcrypt gate (CAPTCHA / proof-of-work). **The bcrypt burn must stay** — removing it reopens the timing oracle the verified flow was built to close — so the throttle must sit *in front of* it.

---

### F-12 [P3] — Opt-in auth model, no default-deny (currently zero accidental gaps — verified)

**CLASSIFICATION:** PARTIAL-WEAK CONTROL

`main.py:63-91` installs **no** authentication middleware (only `SecurityHeadersMiddleware` + CORS); no router carries `dependencies=`. Auth is per-route via `CurrentUser` / `CurrentAuth` / `RequireAdmin` / `RequireAdminMfa` (`auth.py:484-487`). A new route that omits the dependency is simply public.

I scanned every `@router.<verb>` decorator and signature across `src/api/routes/**`. Exactly 17 routes carry no auth dependency and **all 17 are intentional**: the 9 pre-auth `/auth/*` endpoints; `billing.py:512,554` (public price list); `billing.py:1676` + `webhooks.py:154,169` (HMAC-verified); `scrapers.py:71,367` (public catalogue); `jobs.py:1028`, gated by a 60-second signed download JWT correctly re-decoded under a strictly-pinned audience after reading unverified claims only to select it (`jobs.py:1062-1084`).

**FIX** — CI guard that parses route decorators and fails on any lacking an auth dependency unless the path is on an explicit allowlist.

---

### F-13 [P3] — Break-glass codes: no default expiry, plaintext to platform logs

`scripts/generate_break_glass.py:119-122` — `--expires-days` defaults to `None`; `run()` maps that to `expires_at = None` (`:49`), which the redeem query treats as never-expiring (`login.py:280-283`). A code issued for a one-off scenario stays a live password-plus-one credential indefinitely. Partly mitigated by default-revoke-on-reissue (`:67-79`).

`scripts/generate_break_glass.py:96-108` prints plaintext to stdout; the docstring flags the consequence itself (`:15-19`). Correctly kept out of the app logger, but relying on an operator remembering to scrub a credential that may never expire is weak.

**FIX** — Default `--expires-days` to 30–90 with an explicit `--no-expiry` opt-out; write to a `0600` file rather than stdout.

---

### F-14 [P3] — No `Cache-Control: no-store` on token-bearing responses

`src/api/middleware/security.py:495-513` sets no cache header, yet `/auth/login`, `/auth/login/mfa`, `/auth/refresh`, `/auth/verify-email` all return live bearer tokens in the JSON body. Low impact over HTTPS with no shared forward proxy; one line on the auth router.

---

### F-15 [P3, UNVERIFIED] — NUL byte in a password likely 500s

`hash_password` (`auth.py:41`) has no NUL guard. If pyca/bcrypt 5.0 (`requirements.txt:23`) rejects NUL with `ValueError`, a registration containing one surfaces as a generic 500 + ref (`main.py:102-108`) — a robustness bug, not a bypass (`verify_password` already catches `ValueError` → `False`). If it instead truncates at the NUL, two passwords sharing a pre-NUL prefix collide. **Could not settle empirically** — the repo's interpreter is gone (`No Python at '"C:\Users\Windows\anaconda3\python.exe'`) and pytest was off-limits. One-line fix: `if "\x00" in v: raise ValueError` in `_validate_password_rules` (`schemas.py:21-32`).

---

## 2. DIRECT ANSWERS

### 2a. BREAK-GLASS LOGIN

**Reachable in production: YES.** `auth.py:171-188` — registered unconditionally. No feature flag, no environment gate, no DEBUG guard.

**What it bypasses:** the TOTP / backup-code second factor, and **only** that.

**What it does NOT bypass — two independent factors:**
1. **The password.** It consumes the same challenge token as `/auth/login/mfa` (`login.py:223` → `_decode_mfa_challenge_token`), and that token is minted *only* after a verified password (`login.py:80-91`). No password → no challenge → no break-glass. Audience-pinned `bridgeleads-mfa` with a `purpose` re-check (`tokens.py:200-219`), 5-min TTL (`tokens.py:180`).
2. **Possession of an operator-issued code.** **No API endpoint anywhere issues break-glass codes.** Sole issuer is the offline CLI `scripts/generate_break_glass.py`, run by a human against prod. Codes are **128-bit** (`utils/mfa.py:107-129` — 16 random bytes, `bg-` prefix), hashed with the same keyed HMAC as backup codes. Guessing is not a threat model, and the redeem path is additionally throttled per-user at `login.py:235` (`mfa-breakglass:{user_id}`) — **one of the buckets that survived the IP collapse.**

**Cannot be used to escalate — the session is deliberately degraded:**
- `login.py:379-380` — `amr=["pwd","break_glass"]`, **no `"mfa"`**, so `require_admin_mfa` (`auth.py:459`) rejects it outright. A break-glass session can never perform a sensitive admin operation.
- `login.py:332-352` — tears MFA down to un-enrolled, burns **every** remaining break-glass and backup code, clears `api_key_hash`. With `mfa_enabled = False`, `require_admin` (`auth.py:439-445`) routes an admin to re-enrollment.

**Ordering is correct — the part that would be dangerous if wrong.** The atomic single-use consume is the **gate**: `login.py:273-294` burns exactly one unused/unrevoked/unexpired code via `UPDATE … RETURNING`, and *nothing destructive happens* until a code is proven valid. A password-knowing attacker submitting a bogus code triggers **no** teardown and **no** revocation — this endpoint is not a griefing primitive. Failures return a uniform `"Invalid recovery code."` that never distinguishes invalid / used / expired / revoked (`login.py:288-294`). Sessions are revoked **before** the recovery commits (`:323-326`), and the new session is minted only once the wall clock ticks strictly past the revoke instant — **failing closed** if it does not (`:360-374`).

**VERDICT: not a backdoor.** Requires the password *plus* a 128-bit operator-issued secret, and yields a strictly *less* privileged session than a normal login. Residual risk is operational (F-13), not architectural.

### 2b. PASSWORD-RESET TOKEN

- **Entropy / unforgeability:** not a random string — an HS256 JWT signed with `settings.SECRET_KEY` (`tokens.py:36-55`), `jti = str(uuid.uuid4())`. `SECRET_KEY` validated >=32 chars + default-value denylist (`settings.py:92-101`).
- **Expiry:** 30 minutes — `_RESET_TOKEN_EXPIRE_SECONDS = 30 * 60` (`tokens.py:33`), plus an independent TTL floor at `password.py:160-165`.
- **Single-use:** YES — atomic `TokenBlacklist.consume_once(jti, ttl)` (`password.py:237`, Redis `SET NX` at `auth_hardening.py:83`), fail-closed 503 on `RedisError` (`password.py:242-243`).
- **Audience isolation:** `aud="bridgeleads-reset"` (`tokens.py:30`); `_decode_reset_token` pins audience + issuer and re-checks `purpose == "reset"` (`tokens.py:58-79`). A session token cannot reset a password; a reset token cannot authenticate (`auth.py:206`).
- **Sibling-link invalidation:** a token issued at/before `users.revoked_at` is rejected (`password.py:232-236`), so completing one reset kills every other outstanding 30-min link.
- **Reuse policy not bypassable via reset:** same "not current, not last 5" rule as change-password (`password.py:203-219`), enforced *before* the token is burned (`password.py:172-177`), so a policy rejection does not consume the link.
- **Fragment delivery:** token in the URL fragment, never the query (`password.py:118-123`) — never sent to a server, so no access-log or `Referer` leak.

### 2c. RESET LINK HOST — **config constant. Lead's grep confirmed; I concur.**

- `password.py:123` — `reset_link = f"{settings.FRONTEND_URL}/reset-password#token={token}"`
- `settings.py:238` — `FRONTEND_URL: str = "https://bridgeleads.io"` (env var)
- `src/config/frontend_routes.py:72-74` — `settings.FRONTEND_URL.rstrip('/') + path`, read at call time.
- Verification link identical: `workers/scheduler_helpers/registration.py:172`.
- I grepped all of `src/` + `main.py` for `headers.get("host")`, `headers["host"]`, `X-Forwarded-Host`: **zero hits.** No email link anywhere derives from request headers.

**HOST-HEADER POISONING: NOT APPLICABLE — verified, not assumed.**

### 2d. BCRYPT 72-BYTE TRUNCATION — **collides: YES. Handled safely.**

`auth.py:32-38` — `return password.encode("utf-8")[:72]`. A >72-byte password authenticates against its own 72-byte prefix. That is bcrypt's own semantics, not a defect introduced here.

**Safe because:**
1. Hash (`auth.py:43`) and verify (`auth.py:54`) apply the **identical** truncation — no verify-side mismatch, the classic way this goes wrong.
2. Truncation is on **bytes**, the correct unit — bcrypt 5.0 raises on >72 bytes, so a character-only cap would have 500'd on multibyte input (`auth.py:33-37` names exactly this).
3. Policy caps at 72 **characters** (`schemas.py:21-32`; `UserLogin.password` is `Field(max_length=72)` at `schemas.py:152`), so the worst reachable case is 72 multibyte chars → effectively ~18. Entropy floor is still 72 bytes. **No practical exposure.**
4. Legacy passlib-produced `$2b$12$…` hashes verify unchanged (`auth.py:23-27`).

Parameters: `BCRYPT_ROUNDS = 12` (`auth.py:29`), `bcrypt.gensalt(rounds=12)` (`auth.py:44`), `bcrypt==5.0.0` (`requirements.txt:23`).

### 2e. ENUMERATION VERDICT — secure on all three

- **Login — CONFIRMED SECURE.** `login.py:53-73`: static `_DUMMY_HASH` so a real cost-12 bcrypt runs on the user-not-found path; uniform 401 `"Invalid credentials"`; audit logs an HMAC-keyed `email_fingerprint`, never the address (`:65-69`). (`mfa_required=True` at `:80-91` discloses MFA status only to someone already holding valid credentials — not an enumeration vector.)
- **Forgot-password — CONFIRMED SECURE.** Matches the lead's prod 200-for-unknown. `password.py:108,133` uniform body and status; the slow Resend call is a **post-response background task** (`:128-129`) so latency cannot distinguish the branches.
- **Register — CONFIRMED SECURE (my earlier P3 RETRACTED).** With the flag ON, `_register_user_verified` returns a neutral **200** on both branches (`registration.py:342`), no tokens either way, bcrypt burned on **both** (`:348`, `:372`), notice deferred to a background task so a Redis outage cannot reintroduce a timing split (`:330-338`).

**Retraction note:** I originally reported register 201-vs-400 as a live P3. I inferred the prod flag value from `settings.py:225` (`= False`) plus `tasks/email-verification-preflip-followups.md:6,90`, which still lists the flip as an open ops item. Both are stale artifacts. A code default and a handoff checklist are not evidence of a deployed env var; I should have marked it UNVERIFIED.

---

## 3. CONFIRMED SECURE CONTROLS

| Control | Evidence |
|---|---|
| Dual-audience JWTs; refresh cannot authenticate | `auth.py:84-85`, `:202-209`, belt check `:328-329` |
| Revocation fails **closed** (503) at every call site | `auth.py:348-355`; rationale `auth_hardening.py:122-141` |
| Durable `users.revoked_at` + correctly-failing Redis cache | `auth_hardening.py:217-254` (DEL-on-SETEX-fail, **re-raise if DEL also fails** `:238-252`); no negative caching `:301-377` |
| Non-strict `issued_at <= revoke_time` closes same-second bypass | `auth_hardening.py:380-397` |
| Atomic single-use rotation closes check-then-add TOCTOU | `auth_hardening.py:66-84`; `login.py:467` |
| Fail-closed `auth_time` on refresh — no silent step-up escalation | `login.py:509-511` |
| Reset/verify tokens in URL **fragment**; host from trusted config | `password.py:118-123`; `workers/…/registration.py:168-172` |
| Progressive lockout: separated counter/lock keys, atomic Lua, monotonic lock | `auth_hardening.py:410-454`, `:493-517` |
| Brute-force keys use full-length keyed HMAC, never plaintext email | `auth_hardening.py:540,581,663` |
| TOTP single-use via conditional counter advance; replay does not fall through to backup codes | `tokens.py:260-276` |
| TOTP secret Fernet-encrypted; **production refuses** the SECRET_KEY-derived fallback | `crypto.py:78-83`; boot gate `main.py:46-47` |
| Admin force-enroll (404 to non-admins) + two-sided-bounded step-up freshness | `auth.py:429-481` |
| API-key sessions can never satisfy step-up | `auth.py:310-317`, `:459` |
| `pending_registrations` closes squatting, attacker-chosen names, referral abuse | `models.py:283-318`; `registration.py:373-391`, `:445-458` |
| XFF parsed Nth-from-last — spoofing confirmed ineffective by the lead's probe | `rate_limit.py:78-109` |
| Per-user MFA buckets survive the IP collapse | `login.py:86,132,235`; `mfa.py:61,132` |
| Token redaction in access logs + global PII redaction | `main.py:115-136` |

---

## 4. PRIORITY

1. **F-01 / F-01b** — fix `client_ip()` for Cloudflare (resolve the model first with the `request.client.host` log line), **and check whether the Railway origin is directly reachable**; independently decouple `_EMAIL_COUNTER_TTL` from the lock cap.
2. **F-08** — address-keyed guard on forgot-password (~2 lines; closes a live email-bomb primitive).
3. **F-09, F-10** — two small self-contained auth changes; recommended next commit.
4. **F-11** — reuse detection on the post-grace branch.

**No P0.** Nothing found is a direct authentication bypass. The P1 is a *control failure* — real, live and unauthenticated, but it degrades resistance rather than granting access.
