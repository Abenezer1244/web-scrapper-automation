# Login Screen Security audit — "Vibe-Coding Guide" 5-item checklist

Cross-checked every item against the REAL build (FastAPI backend + Next.js/NextAuth frontend),
verified with Codex as an independent second reviewer.

## Verdict per item
- [x] **1. Server-side input validation** — DONE. `EmailStr` + shared `_validate_password_rules`
      (min 10 / max 72), every field `max_length`-bounded, `extra="forbid"` on updates. Both agree.
- [x] **2. Rate limiting + account lockout** — FIXED (see Fix A). Sliding-window rate limit was
      already strong; the *lockout duration* was broken.
- [x] **3. Password hashing** — DONE. pyca/bcrypt cost 12, 72-byte handling, constant-time
      `checkpw`, never logged, no plaintext. Exceeds the guide. Both agree.
- [~] **4. Generic error messages** — login/forgot/reset enumeration-safe (with timing protection).
      Register leaks via STATUS CODE (201+tokens vs 400). → Fix B (user chose full fix).
- [x] **5. Custom vs provider auth** — custom auth is sound & deeply hardened (encrypted email at
      rest, MFA, break-glass, single-use refresh rotation, password history, RLS). Keep custom;
      its only real hole was the item-2 lockout bug (now fixed).

## Fix A — brute-force lockout duration (DONE)
Branch `feat/login-lockout-fix` (worktree). Commits fe8c3b0 + 31bb2e8.
- Root cause: lockout derived from the failure COUNTER, which never decays below a threshold →
  5 fails locked an IP for the counter's ~24h TTL, not the documented 1 min; progressive ladder
  was fiction; email notify (10) unreachable from a single IP (check() raises before record_failure).
- Fix: separate COUNTER from a short-lived, MONOTONIC LOCK key, computed atomically in one Lua
  script. check() reads only the lock; clear() wipes both; IP escalates fully, email capped 15min.
- Codex design consult + adversarial diff review: VERDICT PASS (1 P3 — sub-second TTL — fixed).
- Verified in-memory against the exact Lua via fakeredis+lupa: 11/11. Real-Redis regression tests
  committed (run in CI; cannot run here — .env points at PROD Upstash/Supabase).
- NEXT: push + open PR (additive only — do NOT delete/move shared branches per OneDrive hazard).

## Fix B — registration enumeration (PLANNED, user approved full fix)
Codex caught an account-squatting hole in the naive "nullable email_verified_at on users" design.
Correct architecture = `pending_registrations` table; real `users` row only created on verify.
Backend phases (own worktree/PR):
  1. Migration: `pending_registrations` (email_hmac unique, password_hash, names, ref, expires_at).
  2. register(): existing user -> dup-account email; new -> upsert pending + send verify token;
     BOTH return neutral 200, no tokens. Keep equal-bcrypt burn for timing parity.
  3. New POST /auth/verify-email: consume token (audience-separated, single-use via consume_once,
     fail-closed) -> create real user -> (auto-login? see decision) ; rate-limited.
  4. Tests + Codex review.
Frontend (bridgeleads-web worktree): register -> "check your email"; new verify page; login unchanged.
Open decisions (asked): auto-login after verify?; verify link TTL/resend; frontend now or after BE.

## Constraints honored
- Isolated worktree off origin/main; additive only (other terminals active).
- Codex consulted on design BEFORE code and reviewed the diff AFTER (both fixes).
- No mocks; real-Redis tests; PROD .env never dialed (synthetic env + fakeredis for local verify).
