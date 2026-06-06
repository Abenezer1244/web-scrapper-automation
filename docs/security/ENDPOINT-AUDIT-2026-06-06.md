# Endpoint Security Audit — 2026-06-06

**Scope:** all 45 API endpoints across `src/api/routes/` (auth 13, billing 10, scrapers 9,
jobs 8, segments 4, webhooks 1).
**Method:** parallel per-file security agents (one per route file, security-analyst rigor)
against the 9 non-negotiables, then **Codex independent cross-check** of every file
(Claude × Codex doctrine, `docs/security/security-analyst-agent.md`).

## Headline
**No Critical. No missed High after cross-check.** The hard multi-tenant core is solid —
both reviewers independently confirmed:
- **No IDOR** — `job_id` / `config_id` / `scraper_id` path params are consistently scoped to
  `current_user.id` (incl. the new `POST /scrapers/{config_id}/jobs/{job_id}/dialer-replay`,
  which checks both job AND config owner).
- **No SQL injection** — `segments` binds `record_types` via `ANY(:types)`; the only `.format()`
  inserts fixed server-side county clauses, not user input.
- **Stripe webhook signature verification** is sound (`construct_event`, secret-configured check,
  rate-limit before HMAC).
- **No mass assignment** — no route accepts `plan` / `is_admin` / `user_id` in a request body;
  those fields appear only on response models (and secrets are write-only / presence-flagged).
- **No auth bypass** — public endpoints (login/register/reset/webhooks) are intentional;
  reset-password is gated by a signed single-use JWT.

## High findings (7) — disposition
| # | Endpoint(s) | Finding | Fix | Status |
|---|---|---|---|---|
| 1-3 | `billing` `/subscription`, `/checkout`, `/portal` | No rate limit; each makes an OUTBOUND Stripe call → stolen-JWT loop exhausts Stripe quota / spams Customer objects | Added a dedicated **`stripe` rate-limit zone** (10/min/user, **fail-closed** via `_FALLBACK_ZONES`) | ✅ Fixed |
| 4-6 | `billing` `/referral`, `/usage`, `/skip-trace-usage` | No rate limit on authed billing reads (Codex: Medium, not High) | Added `general`-zone rate limit (per-user) | ✅ Fixed |
| 7 | `webhooks` `POST /tracerfy/{secret}` | Shared secret in URL path → leaks into access logs / Referer | Added preferred header-based `POST /webhooks/tracerfy` (`X-Tracerfy-Webhook-Secret`); legacy path route kept + made **header-first** | ✅ Code done; ⏳ ops migration pending |

## Medium findings — disposition
- `billing /webhook` trusts `user_id` from Stripe session metadata (set server-side at checkout;
  defense-in-depth: also verify `customer_id`). **Accepted-low-risk** (metadata is server-set, not
  attacker input). Follow-up.
- `segments` export endpoints use the loose `general` zone though they materialize up to EXPORT_CAP
  rows. Follow-up: dedicated tighter export zone.
- `webhooks/tracerfy` no HTTP-layer replay/idempotency guard. **By design** — idempotency is owned by
  the worker (SELECT…FOR UPDATE + meter outbox); an edge dedup was deliberately removed (would let a
  forged first webhook suppress the genuine retry). No change.
- `webhooks` `download_url` from body. **Mitigated downstream** — the Tracerfy ingest worker validates
  the URL against expected hosts before fetching (Codex confirmed). No change.
- `jobs` returns internal S3 `export_key` in `JobResponse`. Follow-up: drop from the response.
- `auth/register` welcome-email in a bare `except: pass`. Follow-up: log the exception.

## Codex cross-check refinements (adopted)
1. **`general` was too loose for the Stripe endpoints** (60/min + fails *open*). → new fail-closed
   `stripe` zone for the 3 outbound-Stripe endpoints; `general` kept for the 3 reads.
2. **Webhook migration** = add header endpoint, keep legacy header-first, then (ops): point Tracerfy at
   `POST /webhooks/tracerfy` + the header, **rotate `TRACERFY_WEBHOOK_SECRET`**, then remove the path route.

## Remaining OPS actions (not code)
- [ ] Reconfigure the Tracerfy account webhook → `https://api.bridgeleads.io/webhooks/tracerfy`
      with header `X-Tracerfy-Webhook-Secret: <secret>`.
- [ ] Rotate `TRACERFY_WEBHOOK_SECRET` after the header delivery is confirmed.
- [ ] Remove the legacy `POST /webhooks/tracerfy/{provided_secret}` route once header deliveries are observed.
- [ ] (Optional) Follow-ups: segments export zone, jobs export_key, Stripe metadata customer_id check,
      register welcome-email logging.
