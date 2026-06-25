# Email-Verification — Cross-check + Hardening Status (2026-06-25)

Independent cross-check of the login-security build (BE #125 lockout, BE #126
register/verify, FE #59) by a second Claude session + three adversarial Codex
passes. The build was sound; this session then **fixed** the real reliability +
hardening gaps from the Codex passes. `EMAIL_VERIFICATION_ENABLED` still defaults
**false** (inert in prod until flipped).

Verified clean up front: alembic chain single linear head; 4 segregated token
audiences (each decoder pins aud + purpose); password 10–72 (bcrypt-safe); #125
lockout logic correct.

---

## FIXED this session (commits on feat/register-email-verification)

**Durable verification-email OUTBOX** (`9a41ebd`) — root-cause fix for the email
that could silently never send (Codex: `once_per` fails closed on a Redis outage,
and the Celery broker IS Redis so the `.delay` enqueue fails too; `_send` swallowed
Resend errors; no `release_once` on enqueue failure). The pending_registrations row
is now the outbox:
  * register just commits the row — NO broker / `once_per` in the request path
    (migration **075** adds `email_dispatch_state` / `verification_email_sent_at` /
    `email_attempts` / `next_email_attempt_at` + a partial dispatch index).
  * a 60s beat `dispatch_pending_verification_emails` sends each due row and records
    the outcome, so a signup made while Redis is down is drained + sent on recovery.
    Per-row `FOR UPDATE SKIP LOCKED` + non-blocking per-address advisory lock; bomb
    guard over REAL ('sent') sends only — **120s window + 10/day cap**; classified
    retry/backoff (beat is the sole retry owner) → 'failed' + ops-alert on permanent.
  * verification email RAISES on failure; token `exp == row.expires_at` (math.ceil).
  * batched purge (`SKIP LOCKED`).
  * 6 tests: send / suppress / daily-noop / transient-retry / permanent-fail.

**Timing oracle** (`2c86840`) — the existing-account notice (`_notify_existing_account`)
was still awaited inline on the verified path, so a Redis outage made existing-email
registrations hang while new-email returned fast. Now scheduled as a post-response
`BackgroundTask`: neither path blocks on Redis inline. Legacy path unchanged (it
raises 400 and is already status-code-enumerable).

**Daily-cap retention** (`a4d0b6c`) — a successful send bumps `expires_at = now+24h`
(and mints the token against it), so a delayed/recovered send gives a full fresh
window AND the row is retained a real 24h for the rolling bomb-guard count, without
de-indexing the purge.

Each fix was Codex-reviewed; its sub-findings (per-row `next_email_attempt_at`
recheck, token ceil, purge `SKIP LOCKED`, cap retention) were applied.

---

## Accepted residuals (by design / inherent) — NOT bugs

* **At-least-once delivery.** Send is committed to Resend before 'sent' is recorded;
  a crash between can re-send. Resend 2.7.0 exposes no idempotency key; a duplicate
  verification link is benign (same row, first redeemed wins, second 400s). Same bar
  as `deliver_job_email`.
* **>24h full-Redis-outage edge.** A signup whose row passes its original 24h expiry
  before any send (the entire async stack down for >24h) is dropped and purged; the
  user re-registers after recovery. A 24h Redis outage is itself a sev0.
* **~2ms timing delta.** The new-email path does one extra Postgres insert; a stable
  sub-bcrypt constant. Fully flattening needs measured constant-time padding.
* **RLS policies deferred-by-design.** Mig 075/074 keep `ENABLE RLS` no-policy
  (mirrors 027); the `bridgeleads_app`/`bridgeleads_system` roles don't exist
  pre-cutover, so `CREATE POLICY ... TO` them would fail today. Policies land with
  the same deferred `RLS_ENFORCE` cutover as the other 027 tables (documented in 074).

---

## OPEN — cross-repo / cross-cutting decisions (need product call)

* **Attacker-set display NAME (Codex #8).** An attacker-initiated signup the victim
  verifies can set the victim's cosmetic display name (user-editable, grants no
  access). Closing it = collect first/last name at **verify** instead of register —
  a coordinated change to the backend (`VerifyEmailRequest` + `verify_user_email` +
  drop name from the pending row) AND the frontend (`bridgeleads-web`: register form
  stops collecting name, verify form starts). Cannot ship backend-only without
  breaking the current FE.

* **Token in the URL query string (Codex #2).** `/verify-email?token=` matches the
  already-shipped `/reset-password?token=` pattern, and the FE scrubs it from the
  address bar on mount. Hardening (fragment + `Referrer-Policy: no-referrer`, or a
  one-time server nonce) would also have to change reset-password to stay consistent.

---

## OPS sequence (unchanged) — flip is no longer gated on reliability

1. Merge #125 → deploy api+worker.
2. Merge #126 + FE #59 → `gen:api-types` → deploy both. **Run migration 074 AND 075.**
3. Flip `EMAIL_VERIFICATION_ENABLED=true` on api AND worker; the dispatcher beat must
   run on the worker. Live-verify signup → (≤60s) email → verify → login.
