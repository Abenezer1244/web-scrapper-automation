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

## Phase 3b — Frontend login challenge  (≤4 files, repo: bridgeleads-web)

- [ ] `lib/auth.ts`: add token-adoption branch to `authorize()` (accessToken only; keep password branch).
- [ ] `lib/api.ts`: `loginStart(email,password)` + `loginVerify(mfaToken,code)` → typed `LoginResponse`.
- [ ] `app/(auth)/login/page.tsx`: 2-step state machine (password → code) using existing `InputOTP`;
      match register's RHF + error/loader patterns; "use a backup code" affordance.
- [ ] `types/next-auth.d.ts`: allow `accessToken` on the authorize input if needed.

## Phase 3c — Frontend MFA enrollment (Security settings tab)  (≤4 files, repo: bridgeleads-web)

- [ ] `lib/api.ts`: `getMfaStatus`, `mfaSetup`, `mfaEnable`, `mfaDisable`.
- [ ] `app/(dashboard)/settings/page.tsx`: add **Security** tab — status, QR (otpauth URI) + manual
      secret, code-to-enable, one-time backup-codes display, disable (password + code).
- [ ] QR rendering: use existing dep if present, else render secret + provisioning URI as copyable text
      (decide during impl — no new heavy dep without SBOM check).

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
