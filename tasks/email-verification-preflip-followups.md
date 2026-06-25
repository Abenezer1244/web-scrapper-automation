# Email-Verification — Pre-Flag-Flip Follow-ups (cross-check 2026-06-25)

Independent cross-check of the login-security build (BE #125 lockout, BE #126
register/verify, FE #59) by a second Claude session + an adversarial Codex pass
(`codex exec ... challenge`, model_reasoning_effort=high, 561k tokens).

**Verdict: the build is sound and safe to merge as-is.** `EMAIL_VERIFICATION_ENABLED`
defaults **false**, so #126 is inert in production until the flag is flipped. Everything
below is a **fix-before-you-flip-the-flag** item, NOT a fix-before-merge item. No code
was changed (user elected document-only).

What was independently verified clean: alembic chain 070→074 (no dup down_revision,
single linear head); token audiences segregated (`bridgeleads-api` / `-refresh` /
`-reset` / `-verify`, each decoder pins aud + checks `purpose`); password bounds 10–72
(bcrypt-safe) on both register and verify; `audit_log(..., None)` tolerated; `once_per`/
`release_once`/`system_sync_session` signatures match call sites; all changed files
`py_compile`-clean. PR #125 lockout logic (counter≠lock, monotonic Lua, `ttl>=0` lock
read, `clear()` deletes both keys) is correct.

---

## Reconciliation of Codex's 9 findings

### Codex "P1"s — downgraded after verification (NOT blockers)

1. **RLS enabled with no policy (`alembic/versions/074_pending_registrations.py:85`).**
   Codex: "breaks any non-BYPASSRLS deploy." **False for the current deployment.** Prod
   role is BYPASSRLS; migration 027 already does `ENABLE ROW LEVEL SECURITY`-no-policy on
   `users` and login works today; 074 documents the deferred app/system role policies for
   the future `RLS_ENFORCE` cutover. This is the same finding Codex *withdrew* last
   session after seeing the 027 evidence. Consistent-by-design.
   - Action at the deferred RLS cutover (same work as the other 027 tables): grant
     `bridgeleads_app` INSERT/SELECT/DELETE, `bridgeleads_system` DELETE; anon/authenticated
     stay default-denied. Already written up in the 074 docstring.

2. **Verification token in the URL query string (`registration.py` verify_link).**
   Codex: leaks via logs/Referer/history. **Matches the already-shipped reset-password
   flow** (`auth_helpers/password.py:118` builds `/reset-password?token=`), and the FE
   `VerifyEmailForm` scrubs the token from the address bar on mount
   (`history.replaceState`). Industry-standard, single-use, 24h TTL. Not a #126 regression.
   - Optional future hardening (would also apply to reset-password, do them together):
     move token to URL fragment + `Referrer-Policy: no-referrer` on the verify page, or
     exchange the link token for a short-lived server nonce on first GET.

### The one REAL pre-flip issue — verification-email reliability (Codex #2/#4/#5)

The verification email is the signup critical path ("no email = no account"), yet it can
silently fail to send three ways. All three are inert while the flag is off.

3. **`once_per` fails CLOSED on a Redis outage (`api/middleware/rate_limit.py:213`).**
   In `_register_user_verified`, the verification email is gated behind
   `once_per("verifyemail:<hmac>", 120)`. On a Redis outage `once_per` returns False, so
   the pending row is committed + a neutral 200 is returned **but no email is sent** — even
   the FIRST one. Root-cause fix when flipping the flag: don't gate the *first* send behind
   a fail-closed spam guard. Send first, gate only repeats; or fail-open on the gate-check
   for the initial send (rate_limit + bcrypt still throttle abuse).

4. **`_send` swallows Resend failures (`workers/onboarding_emails.py:39`).**
   `except Exception: _logger.error(...)` then returns — no raise. So
   `send_verification_email.delay()` reports task success and **never retries** on a Resend
   error or missing `RESEND_API_KEY`. Fix: make `send_verification_email` a bounded
   `autoretry_for=(Exception,)` task (or have `_send` raise for the verification path), and
   fail app startup if the flag is on without email creds.

5. **No `release_once` on enqueue failure (`registration.py` verified path).**
   The legacy `_notify_existing_account` calls `release_once` when the Celery enqueue
   raises; the verified path does not, so a transient enqueue failure leaves the user gated
   120s with no email. One-line fix: mirror the `_notify_existing_account` release pattern.

### Documented accepted residuals — leave as-is (product decisions already signed off)

6. **Statistical timing enumeration (`registration.py` existing-vs-new path).** Existing-email
   path skips the DB insert+commit the new path does, so repeated timing samples can still
   distinguish them despite the bcrypt burn. Documented + accepted in-code. Close only if a
   timing-padding requirement appears (would need a measured minimum response duration).

7. **Attacker-set display name (`registration.py` pending first/last name).** An
   attacker-initiated signup the victim verifies can set the victim's cosmetic display name.
   Documented + accepted (user-editable, grants no access). Full close = collect name at
   verify (also changes the FE verify form contract).

### Hardening follow-ups — low priority

8. **Email-bomb ceiling (Codex #7).** 120s resend window = up to 720 verification emails/day
   to one targeted address, and each attempt inserts an encrypted pending row. Bounded +
   purged hourly. Consider a daily per-address cap on top of the 120s window before flip.

9. **Single-transaction purge (`scheduler_helpers/registration.py:29`, Codex #9).** Hourly
   purge deletes all expired rows in one transaction; under a registration spray this can
   lock / burst WAL. Switch to batched deletes (`LIMIT` + repeated short transactions) if
   abandoned-signup volume ever grows.

---

## OPS sequence (unchanged) — flip is gated on items 3–5 above

1. Merge #125 → deploy api+worker (no migration).
2. Merge #126 + FE #59 → `gen:api-types` → deploy both.
3. Run migration 074 against prod.
4. **Before** flipping `EMAIL_VERIFICATION_ENABLED=true`: fix the email-reliability cluster
   (items 3–5) so a Redis/Resend blip can't silently strand new signups. Then flip on api
   AND worker and live-verify signup→verify→login.
