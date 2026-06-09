# ✅ H3 — PII-at-rest encryption — BUILT (2026-06-09), SPLIT INTO TWO BRANCHES

**Spec:** `docs/superpowers/specs/2026-06-08-h3-pii-encryption-design.md` (Codex-consulted; banner at top
explains the split). Both branches off `main`, UNMERGED, NOT deployed.

**Why two branches:** the `User.email` cutover (NOT NULL on `email_hmac`) can't ride the same rolling
deploy as the column-add — old replicas would 500 on `/auth/register`. So:

- **`security/h3-pii-encryption` (STAGE 1 — deploy first):** contact-PII encryption (P1–P3) + additive
  `User.email` blind index (P4: `email_hmac` nullable + `@validates` dual-write; email stays plaintext).
  Migrations 046 + 047. **Every phase Codex-gated CLEAN** (P1 R2, P2, P3, P4 R2). 32 pure tests pass.
  Safe single deploy. **THIS is the high-value work — closes the audit's owner-contact-PII target.**
- **`security/h3-email-cutover` (STAGE 2 — deploy after Stage 1 + backfill):** email→`EncryptedString`,
  `email_hmac` NOT NULL + UNIQUE (migration 048), login/register/reset → `email_hmac`, operator-script +
  test updates, verify + email-encrypt backfill scripts, full deploy runbook (spec §11). **Codex P5 gate:
  6 rounds, all findings fixed; final round (R7) confirms clean — see that branch.**

**Pre-merge:** Stage 1 → Codex-gate the split composition (re-run `codex review --base main` on this
branch) → merge. Then deploy Stage 1, run `backfill_pii_encryption.py` + `backfill_user_email_hmac.py`
(0 NULL/0 collisions), THEN merge + deploy Stage 2 (spec §11 runbook). Provision `FIELD_ENCRYPTION_KEY`
+ `BLIND_INDEX_KEY` in Railway before Stage 1.

---

# H2 Phase 5 — Admin MFA enforcement + step-up + break-glass — PLAN (implement in a fresh session)

**Decisions (owner):** FORCE-ENROLL + STEP-UP; BOTH break-glass mechanisms (operator script + in-app
break-glass codes); **implement in a fresh `/clear` session from this plan.**

**Codex consult done (2026-06-08, high effort).** Recommended design below. **Codex flagged a CRITICAL
pre-existing bug that is Step 0.**

## ✅ STEP 0 — DONE (committed `434d440`, standalone). Details below kept for context.
## ⚠️ STEP 0 — CRITICAL pre-req (fix BEFORE amr; arguably ship standalone first)
**Refresh tokens are valid access tokens.** `create_refresh_token` (src/api/auth.py:68) sets
`aud="bridgeleads-api"` (same as access) and `purpose="refresh"`, but `decode_secure_token` only pins
aud and `get_current_user` never checks `purpose`. So a 7-day refresh token authenticates ANY request.
Today that's a latent priv/longevity bug; with P5 it becomes a 7-day **MFA-backed** bearer.
- **Fix:** give refresh tokens a DISTINCT audience (`bridgeleads-refresh`) OR make `get_current_user`
  reject `payload.get("purpose") == "refresh"`. Add `purpose="access"` to access tokens. `/auth/refresh`
  must still accept only refresh-purpose tokens (it decodes via `decode_secure_token` today — verify it
  pins refresh purpose after the change). Add a test: a refresh token is rejected by `/auth/me`.

## ✅ STEP A — DONE (Codex round-2 CLEAN; UNCOMMITTED). py_compile + ruff clean; 31 pure tests pass.
**Shipped:** `create_secure_token`/`create_refresh_token` gained `amr`+`auth_time` params (access also
already `purpose="access"`); `_sanitize_amr` (subset of {pwd,mfa,break_glass}, legacy→["pwd"]);
`_coerce_auth_time` (STRICT — rejects bool/float/str). `AuthContext` dataclass + `get_auth_context`
(decode-once: API-key→amr=[]/auth_time=None; jwt→sanitized amr/coerced auth_time); `get_current_user`
now a thin wrapper (FastAPI dep-cache = single decode). Routes: register/login→["pwd"], login_mfa→
["pwd","mfa"], `/auth/refresh` copies amr+auth_time UNCHANGED (never adds/drops mfa).
`tests/test_token_amr.py` (31 pure tests, no DB). `CurrentAuth` type alias added.
**Codex gate:** R1 [P1] (refresh minted FRESH "now" for an mfa token w/ missing/garbage auth_time →
silent step-up-passing session) + [P3] (bool⊂int). FIXED: refresh substitutes 0 (stale epoch) not None;
`_coerce_auth_time` rejects bool. **R2 CLEAN.**

## Step A — amr/auth_time + AuthContext  (original plan below)
- `create_secure_token(user_id, amr=["pwd"], auth_time=now)` + `purpose="access"`;
  `create_refresh_token(user_id, amr=["pwd"], auth_time=now)`. login/register → `["pwd"]`; login_mfa →
  `["pwd","mfa"]`; auth_time=now.
- `/auth/refresh`: sanitize amr (intersect `{pwd,mfa,break_glass}`, default `["pwd"]` for old tokens),
  copy amr + auth_time UNCHANGED into both new tokens. **Never add `mfa` on refresh** (no escalation);
  never drop it (no silent downgrade).
- **AuthContext (Codex's pick over request.state / ORM attrs / re-decode):** new `get_auth_context()`
  decodes once → `{user, auth_method: "jwt"|"api_key", amr, auth_time, jti, payload}`. `get_current_user`
  becomes a thin wrapper returning `ctx.user` (keeps all existing `CurrentUser` deps working).

## ✅ STEP B — DONE (Codex round-2 CLEAN; UNCOMMITTED). py_compile + ruff clean; 44 pure tests pass.
**Shipped:** `require_admin` (non-admin→404 hidden; admin+mfa_enabled=False→403 `admin_mfa_enrollment_required`)
and `require_admin_mfa` (layers on require_admin: auth_method=="jwt" AND "mfa" in amr AND auth_time fresh
[15min window, both-sided: stale>900s OR future<-60s skew → fail]; API-key always fails). `RequireAdmin`/
`RequireAdminMfa` aliases. Applied: `billing.py /activation-funnel` → `require_admin` (read-only, enroll-
only) with new IP-keyed `_rate_limit_activation_funnel` dep FIRST (before gate); `scrapers.py POST
/connectors` → `require_admin_mfa` (state-changing, registers SSRF target → step-up). Inline is_admin
checks removed (central dep, Codex HIGH: won't drift). `tests/test_admin_mfa_deps.py` (13 tests).
**Codex gate:** R1 0 P1, [P2] funnel probes un-rate-limited after gate moved to dep (no global limiter) +
[P3] future auth_time stayed fresh. FIXED: pre-gate IP limiter + both-sided freshness. **R2 CLEAN.**
**Decision (Codex-endorsed):** funnel=enroll-only (read), connector=step-up (write). Dropped now-unused
current_user/request params from funnel body.

## Step B — admin enforcement dependencies  (original plan below)
- `require_admin(ctx)`: non-admin → **404** (endpoint hiding, matches current behavior); admin with
  `mfa_enabled=False` → **403 `admin_mfa_enrollment_required`**.
- `require_admin_mfa(ctx)` (step-up): `require_admin` AND `auth_method=="jwt"` AND `"mfa" in amr` AND
  **auth_time fresh (< 15 min)** → else **403 `admin_mfa_step_up_required`**. **API-key sessions always
  fail step-up** (no amr/auth_time).
- Apply to the 2 existing admin endpoints: `billing.py:~23` (activation-funnel) + `scrapers.py:~255`
  (connector creation). Replace inline `is_admin` checks with the dependency (Codex HIGH: inline checks
  will drift). Frontend: do NOT hide Settings behind an admin-gated 403 (no-MFA admin must reach enroll).

## ⏳ STEP C — IN PROGRESS. Decisions: FULL break-glass; RECOVERY-ONLY (break-glass session amr=
## ["pwd","break_glass"], NO "mfa" → can never pass require_admin_mfa). RLS-enforce stays OFF (user
## confirmed continue; H1 cutover deferred — grant gap tracked in provision_rls_roles.sql + 045).

### ✅ C1 — DONE (Codex round-3 CLEAN; UNCOMMITTED). compile+ruff clean; 50 pure tests pass.
**Shipped:** migration 045 `mfa_break_glass_codes` (id,user_id,code_hash,batch_id,created_by,
created_reason,expires_at,used_at/used_ip/used_user_agent,revoked_at; RLS mirrors 043). Model
`MfaBreakGlassCode`. `generate_break_glass_codes` (128-bit, `bg-` format, same keyed-HMAC as backup
codes). Operator scripts (railway run): `reset_user_mfa.py` (revoke-FIRST fail-safe → clear MFA + delete
backup+break-glass codes; any revoke failure = exit 3, nothing cleared) + `generate_break_glass.py`
(revokes prior unused by default, prints once to stdout only, FOR UPDATE). `tests/test_break_glass.py` (6).
**Codex gate:** R1 [P1] reset swallowed ALL revoke exceptions + [P2] cutover grant gap. R2: P1 partial
(swallowing RedisError still defeats fail-closed) + P2 accepted-deferred. R3 CLEAN (revoke-first, any
failure=exit 3). **H1-CUTOVER TODO recorded:** bridgeleads_app needs grants on mfa_backup_codes (043,
pre-existing gap) + mfa_break_glass_codes — blocked on reconciling app-DELETE vs the script's no-DELETE
invariant. Harmless today (RLS_ENFORCE=False/BYPASSRLS).

### ✅ C2 — DONE (Codex round-5; no Crit/High; 1 documented-accepted P2). compile+ruff clean; 51 pure tests.
**Shipped:** `POST /auth/login/break-glass` (reuses the 5-min challenge token). Flow: IP limit → decode →
per-user `mfa-breakglass:{id}` limit → RLS bind → revocation gate (503 fail-closed) → load user
(active+mfa_enabled) → ATOMIC consume (UPDATE...RETURNING, unused/unrevoked/unexpired) → burn jti →
revoke_all_for_user → recovery txn (clear MFA + delete backup + revoke sibling break-glass + clear API key)
→ commit → WAIT for clock to pass revoke second → mint DEGRADED session `amr=["pwd","break_glass"]` (NO
"mfa"). `BreakGlassLoginRequest` schema (code max_length=64). `revoke_all_for_user` now RETURNS the
revoke datetime (single API clock w/ JWT iat). `tests/test_break_glass_login.py` (7 CI-only integration tests).
**Codex gate (5 rounds):** R1 [P1] schema cap 32<35 + [P1] now-1 missed same-second tokens. R2 [P1] stale
early `now` capture. R3 [P1] Python pre-write capture window + [P3] wait-loop fell through. R4 [P2]
clock_timestamp introduced cross-clock skew. R5 [P2 ACCEPTED] row-lock-wait extends capture window —
NOT fixed via SELECT FOR UPDATE because mfa_enable/disable call revoke_all_for_user while holding a users
FOR UPDATE lock → would deadlock; inherent timestamp-precision limit, robust fix = token-versioning (separate).
**Same-second-revoke solved:** revoke at now (catches same-second sessions), WAIT until clock>revoke_ts, mint
iat=now (no future iat — PyJWT rejects future iat). fail-closed 503 if clock never advances.
**⚠️ NOTE for user:** pre-existing concern observed — mfa_enable/mfa_disable call revoke_all_for_user while
holding a `with_for_update()` lock on the users row; the separate-txn UPDATE inside contends with that lock.
Apparently works in prod but worth verifying (NOT introduced by P5). **Frontend break-glass affordance =
follow-up (not in P5 backend scope).**

## ✅ H2 PHASE 5 COMPLETE (Step 0 + A + B + C1 + C2). See per-step blocks above.

## Step C — break-glass (BOTH)  (original plan below)
- **New table `mfa_break_glass_codes`** (migration 045 — NOT reuse MfaBackupCode): `user_id, code_hash,
  batch_id, created_by, created_reason, expires_at, used_at, used_ip, used_user_agent, revoked_at`.
- `scripts/reset_user_mfa.py` (railway run): clear MFA (enabled/secret/counter) + delete backup codes +
  revoke sessions + audit. Operator-authenticated by Railway DB/env access.
- `scripts/generate_break_glass.py` (railway run): generate N high-entropy codes, store hashes, print
  once. Uses the `scripts/_creds.py` env pattern.
- **Redemption:** through the existing challenge flow (password already verified) — single-use atomic
  consume, rate-limited, revoke sessions+API key on success, LOUD audit. Resulting token
  `amr=["pwd","mfa","break_glass"]`; block destructive admin ops until normal TOTP re-enrolled OR allow
  only a 5-min emergency window.

## Minimum safe slice (if cutting scope)
Step 0 + Step A + AuthContext + `require_admin`/`require_admin_mfa` + apply to the 2 endpoints + reject
API keys for step-up + the operator reset script. **Defer in-app break-glass codes.** Do NOT ship admin
enforcement without Step 0.

## Severity flags from Codex
- **CRITICAL:** refresh-token-as-access (Step 0). **HIGH:** sparse inline admin checks → central dep.
  **HIGH:** API-key has no amr → must reject for step-up.

## Tests to write
refresh token rejected by /auth/me; amr=["pwd"] vs ["pwd","mfa"]; refresh preserves amr+auth_time and
never adds mfa; require_admin 403 for no-MFA admin; require_admin_mfa 403 for API-key + stale auth_time +
pwd-only session, 200 for fresh mfa session; break-glass single-use + audit; operator reset clears MFA.

---

# H2 Phase 4 — Session hardening (TOTP replay) — SCOPED: TOTP-replay only (amr→P5)

**Goal:** Close the TOTP-replay window deferred from P3, and stamp the auth method
(`amr`) into issued tokens so P5 can enforce MFA on sensitive actions.

## Proposed design (pre-Codex)
- **Migration 044:** `users.mfa_last_totp_counter BIGINT NULL` (additive; safe).
- **`src/utils/mfa.py`:** add `verify_totp_counter(secret, code) -> int | None` — returns the unique
  30s timestep counter whose code matches (scan ±1 window, constant-time compare). Each code maps to
  one counter, so returning it (not "max of window") avoids the advance-too-far lockout.
- **`_consume_second_factor` (login_mfa):** TOTP branch → `verify_totp_counter`; if matched, **atomic
  replay-guarded advance**: `UPDATE users SET mfa_last_totp_counter=:c WHERE id=:id AND
  (mfa_last_totp_counter IS NULL OR mfa_last_totp_counter < :c) RETURNING id`. 0 rows = replay or a
  concurrent loser → reject (do NOT fall through to backup codes). Keep enable/disable on plain
  `verify_totp` (authenticated, first-use / password-gated).
- **amr claims:** `create_secure_token`/`create_refresh_token` gain `amr` param; login + register emit
  `["pwd"]`, login_mfa emits `["pwd","mfa"]`; `/auth/refresh` propagates `amr` from the refresh token.
- **Tests:** replay rejected (same code twice → 2nd 401); newer code accepted after advance; amr =
  `["pwd","mfa"]` after MFA login vs `["pwd"]` non-MFA; refresh preserves amr.

## Risks to pressure-test with Codex
- Atomic counter advance under concurrency (two logins, same code → exactly one wins?).
- Lockout: does advancing last_counter ever reject a legit *next* code? (Single-counter return should avoid it.)
- Should enable/disable also be replay-tracked, or is leaving them on verify_totp acceptable?
- amr threading through refresh — any token-family / aud pitfalls.

## STATUS: ✅ DONE (Codex 3 rounds; py_compile/ruff/app-build clean) — UNCOMMITTED
**Shipped:** migration 044 (`users.mfa_last_totp_counter BIGINT NULL`) + model column; `verify_totp_counter`
(`src/utils/mfa.py`); `_consume_second_factor` TOTP branch → atomic guarded advance
(`UPDATE users SET mfa_last_totp_counter=:c WHERE id AND mfa_enabled AND secret NOT NULL AND
(col IS NULL OR col<:c) RETURNING id`); `mfa_enable` seeds the counter from the enrollment code;
`mfa_disable` is now replay-aware (counter>last, FOR-UPDATE-locked) + clears the counter. 3 new tests
(replay rejected, concurrent single-use, enrollment-code-can't-login) + fixed the existing
TOTP-completes test to use a counter+1 code.
**Codex gate:** R1 no P1, 2×P2 (seeding broke a test → fixed; disable not replay-guarded → fixed) + 1×P3.
R2 found a NEW P2 (concurrent disable-vs-login could mint a session post-disable) → fixed with the
`mfa_enabled`/secret WHERE guards. **R3 CLEAN.**
**Trade-off (documented):** seeding means a login within the same 30s window as enrollment is rejected
("wait for next code") — accepted; enable revokes sessions so re-login is usually a fresh code anyway.
**⚠️ migration 044 is branch-only — not on prod; applies at deploy (alembic-on-boot).**

## Codex consult: DONE — design sound, 0 Crit/High. Reconciled:
- Atomic UPDATE race-safe; `WHERE` MUST use the DB column (not Python-read counter). 0 rows → reject,
  no backup fallback.
- Return the matched counter (not max-of-window) → no lockout; on rare collision prefer HIGHEST match.
- **Initialize `mfa_last_totp_counter` at `/auth/mfa/enable`** (from the verified enrollment code) so it
  can't be replayed into the first login. (enable already revokes sessions → re-login uses a fresh code.)
- RLS GUC already bound in `login_mfa` before `_consume_second_factor` → put the UPDATE in that txn.
- amr: signed JWT can't be forged, but sanitize on refresh (subset of {pwd,mfa}, default `["pwd"]` for
  old tokens). amr = session-strength, NOT freshness; step-up needs `auth_time` max-age (P5 concern).
- **Codex rec: do TOTP-replay now; defer amr to P5** (where it's consumed) unless done fully w/ tests.

---

# H2 Phase 3 — MFA Login Challenge + Frontend

**Goal:** Make the TOTP MFA built in Phases 1–2 actually gate login, and give users a UI to
(a) pass the MFA challenge at sign-in and (b) enroll/disable MFA from settings.

Backend enrollment endpoints already exist (`/auth/mfa/setup|enable|disable|status`); the secret is
Fernet-encrypted; backup codes are 80-bit HMAC-hashed. What's missing: `/auth/login` ignores
`mfa_enabled`, and there is no frontend for either the challenge or enrollment.

---

## Design decision — RECONCILED with Codex (challenge-token model)

next-auth Credentials `authorize()` is one-shot. A password→code two-step flow doesn't fit one
call, and v5's custom-error channel is version-fragile. **Codex review (medium, consult) upgraded the
design**: model the MFA challenge **explicitly** with a short-lived challenge token + a dedicated
verify endpoint, rather than overloading `/auth/login` with an optional `mfa_code` + resending the
password. Cleaner home for rate-limit/expire/audit; password never resent. Doctrine: docs silent → Codex wins.

**Backend (two endpoints):**
1. `POST /auth/login {email,password}` — password check unchanged. If `user.mfa_enabled` →
   per-user rate-limit (`mfa-user:{id}`), issue a **short-lived signed MFA challenge token**
   (`purpose="mfa_challenge"`, `sub=user_id`, distinct `aud`, ~5 min exp, NO access privilege),
   return `LoginResponse(mfa_required=True, mfa_token=...)`. Do NOT clear brute-force, do NOT issue
   access/refresh. Audit `mfa_challenge`. If not enabled → issue tokens as today.
2. `POST /auth/login/mfa {mfa_token, code}` — decode+validate challenge token (purpose/aud/exp/sub);
   per-user rate-limit (`mfa-user:{sub}`); verify 2nd factor (TOTP, else **atomic** backup-code
   consume); on fail → audit `mfa_failure` + 401 (NO password-bucket `record_failure`); on success →
   `BruteForceProtection.clear(ip, user.email)`, issue access+refresh, audit `login_success`.

**Frontend ("token adoption" — next-auth only materializes the session):**
1. Login page POSTs `/auth/login` **directly** via `lib/api` (not `signIn`).
   - `401` → bad creds. `{mfa_required, mfa_token}` → store token, show OTP step.
   - `{access_token}` → `signIn("credentials",{accessToken, redirect:false})` → dashboard.
2. OTP step POSTs `/auth/login/mfa {mfa_token, code}` → `{access_token}` → `signIn` → dashboard.
   (Password is NOT resent — only the challenge token is.)
3. `authorize()` gains a **token-adoption branch**: `credentials.accessToken` present → validate via
   `GET /auth/me` → build session user. **Only ever accept accessToken, never refresh.** Keep the
   existing password branch (register auto-login uses it).

**Codex-driven invariants (security):**
- Bad MFA code does NOT feed the password `(ip,email)` brute-force/lockout bucket (avoids
  password-knowing attacker DoS-locking the real user + avoids conflating password vs MFA compromise).
  Per-user `mfa-user:{id}` rate limiter caps TOTP guessing across rotating IPs + `mfa_required` farming.
- Backup-code single-use is **atomic**: `UPDATE mfa_backup_codes SET used_at=now() WHERE user_id=:u
  AND code_hash=:h AND used_at IS NULL RETURNING id` — valid iff exactly one row updated (fixes both
  the async race Codex flagged AND the pre-existing "never marked used" gap).
- accessToken: never logged, never in URLs/errors; Auth.js CSRF stays on; refresh token never adopted.
- TOTP replay (±1/90s window) NOT prevented in P3 → deferred to P4 (Codex agreed: acceptable under
  TLS + per-user rate-limit, do not claim replay-resistant). Documented inline.

---

## Phase 3a — Backend login challenge  ✅ DONE (Codex round-2 CLEAN)

- [x] MFA challenge-token helpers — put in `src/api/routes/auth.py` (co-located with the existing
      reset-token family, not `src/api/auth.py`): `_mint_mfa_challenge_token` /
      `_decode_mfa_challenge_token` (`aud="bridgeleads-mfa"`, `purpose="mfa_challenge"`, 300s exp).
- [x] `src/api/schemas.py`: `LoginResponse` (optional tokens + `mfa_required` + `mfa_token`) +
      `MfaLoginRequest`.
- [x] `src/api/routes/auth.py`: `/auth/login` challenge branch (`response_model=LoginResponse`);
      `POST /auth/login/mfa`; `_consume_second_factor` (TOTP, else atomic conditional-UPDATE consume).
- [x] `tests/test_auth.py`: 10 real-flow tests incl. concurrent race + >threshold no-lockout.

**Codex gate:** round 1 found 1×P1 + 3×P2 + 2×P3 → ALL FIXED → round 2 CLEAN.
- P1 revocation: `/auth/login/mfa` rejects challenges minted ≤ `revoked_at` (logout-all / pwd change),
  fail-closed 503 on Redis down.
- P2 bucket split: `mfa-issue:{id}` (login) vs `mfa-verify:{id}` (verify) — challenge farming can't
  exhaust the user's verify budget.
- P2 replay: challenge `jti` burned via `consume_once` on success (wrong code never burns it).
- P2 RLS: bind `app.current_user_id` to the proven challenge subject before SELECT/UPDATE → correct
  under a future RLS-enforce cutover.
- P3: schema docstring corrected; added concurrent `asyncio.gather` race test + 6-attempt no-lockout.

**Verification:** `py_compile` OK; `ruff` clean (also fixed 2 pre-existing I001 in the file);
app builds + both routes registered; token audience-separation proven via direct exec
(access↔challenge↔reset cross-rejection). ⚠️ Integration tests NOT run locally — only configured
`DATABASE_URL` is **production** and the `db` fixture does unconditional table-wipes; tests must run
in CI (dedicated test DB) or against a local throwaway Postgres+Redis.

## Phase 3b — Frontend login challenge  ✅ DONE (Codex 3 rounds; tsc clean)

- [x] `lib/auth.ts`: token-adoption branch in `authorize()` (accessToken-only via shared `buildUser`,
      validated by `/auth/me`; password branch kept + returns null when `mfa_required`).
- [x] `lib/api.ts`: `LoginResponse` type, `LoginError`, `loginStart` / `loginVerify` (raw fetch, NOT
      apiFetch — no signOut-on-401).
- [x] `app/(auth)/login/page.tsx`: 2-step (password → code) with `InputOTP` (TOTP) + backup-code text
      mode; matches register RHF/error/loader patterns.
- [x] `types/next-auth.d.ts`: no change needed — `accessToken` declared on the provider `credentials`.

**Codex gate:** R1 found 3×P2 + 3×P3 (no MFA bypass) → fixed → R2: 5/6 resolved, 1×P2 partial
(unmount race) → fixed (mounted ref) → R3: residual sub-second window where signIn completes after
unmount. **Accepted with documented reasoning** (not a defect: runs only after valid password + 2nd
factor, so establishing the session is the correct auth outcome; signIn has no AbortSignal; UI effects
are guarded). Fixes: sync in-flight ref (no double-redeem), gen-guard + mounted-ref (no stale
adopt/navigate), fixed safe 401 copy (no backend-text leak / no regex), 6-digit TOTP gate,
`!result.ok` check.

**Verification:** `npx tsc --noEmit` clean ✅. ESLint NOT configured in bridgeleads-web (no config/dep/
script) → type safety via tsc only. ⚠️ user to confirm acceptance of the documented R3 residual.

## Phase 3c — Frontend MFA enrollment (Security settings tab)  ✅ DONE (Codex 4 rounds; tsc clean)

- [x] `lib/api.ts`: `getMfaStatus` / `mfaSetup` / `mfaEnable` / `mfaDisable` + types; `apiFetch` thrown
      errors now carry `status`; `setSuppressSignOutOn401` escape-hatch.
- [x] `components/settings/security-tab.tsx` (NEW, extracted to keep the 1330-line page small): enable
      flow (setup → QRCodeSVG + copyable secret → 6-digit verify → one-time backup codes) + disable
      flow (password + TOTP/backup code). Wired into `settings/page.tsx` (Security tab, 3-line change).
- [x] QR: `qrcode.react@^4.2.0` — SBOM clean (zero runtime deps, React-19 peer, 115KB, maintained).

**Codex gate:** R1 found **1×P1** (backup codes destroyed by a background-query 401→signOut after
enable revokes the session) → fixed → R2 found TOCTOU residual → fixed → R3 found an enable-own-401
P3 → fixed → **R4 CLEAN**. Final mechanism: enable revokes the session, so `apiFetch`'s signOut-on-401
is suppressed (armed in `onMutate`, reset on unmount/error), backup codes render before any
query-driven branch, `mfa-status` is disabled while codes show, cache is set `{enabled:true}`, and a
real 401 during enable redirects to /login. Both enable AND disable end in an intentional `signOut`
(backend revokes sessions on both).

**Verification:** `npx tsc --noEmit` clean ✅. (No ESLint in bridgeleads-web.)

---

## Review (H2 Phase 3 complete)

**Shipped:** MFA now gates login end-to-end. Backend challenge-token flow (3a, committed `3539d2e`),
frontend 2-step login challenge (3b, committed `49d37a7`), and the Security settings enrollment tab
(3c). Every phase passed a Codex review gate (NO-GO on any Crit/High) — 3a: 1P1+3P2 fixed; 3b:
3P2+3P3 fixed (+1 documented-accept); 3c: 1P1 fixed across 4 rounds. tsc/ruff/py_compile all clean.

**Deferred (later phases, per checklist):** P4 session hardening (TOTP replay last-counter — documented
inline); P5 admin MFA enforcement + break-glass; H1 `users` RLS self-row policy (the login-SELECT
landmine — keep `RLS_ENFORCE=False`).

**⚠️ Ops note for deploy:** migration 043 (MFA columns) is on this branch, NOT on main/prod yet — the
backend won't have the columns until 043 is applied at deploy (alembic-on-boot). The frontend Security
tab + login challenge are inert until the backend is live with MFA. Don't push frontend master ahead
of the backend deploy or enrolled users could be half-broken (there are none yet).

---

## Out of scope (later phases, per checklist)
- P4 session hardening (TOTP replay/last-counter, session pinning).
- P5 admin MFA enforcement + break-glass.
- H1 `users` RLS self-row policy (login `SELECT` landmine) — tracked separately; do NOT enable
  `RLS_ENFORCE` here.

## Risks / open questions
- TOTP replay within the ±1 window is **not** prevented in P3 (deferred to P4) — document inline.
- `/auth/login` response shape change (`LoginResponse`) must keep the no-MFA client contract intact
  (`access_token` still present) — register + any other caller unaffected.

## Codex consult (pre-build, per .claude/rules/codex-collaboration.md)
Consult run 2026-06-08 (medium, 24.4k tok). Verdict: design viable; **2 items pulled into P3**:
(1) backup-code row race → atomic conditional UPDATE; (2) separate MFA attempt limiter (no
password-bucket conflation). **Architecture upgraded** to explicit challenge-token + dedicated
`/auth/login/mfa` (was: overloaded `/auth/login` + optional `mfa_code`). TOTP replay tracking
confirmed deferrable to P4 under TLS + rate-limit. All folded into the design above.

## Review
_(to be filled at end)_
