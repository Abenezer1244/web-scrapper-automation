# Red-Team Remediation — Claude × Codex — 2026-06-01

Full register + reconciled verdicts: `docs/security/REDTEAM-2026-06-01.md`.
Scope (user-approved): fix **everything** — HIGH + MEDIUM + LOW. RLS: add WITH CHECK + fail-closed startup. N2: kill PII leak.
Rule: ≤5 files/phase · py_compile after each · atomic commit per phase · Codex re-verifies the full diff at the end.

## Phase 1 — Auth (HIGH A1, A2; MED A4; LOW A5, A6)  · routes/auth.py, auth_hardening.py
- [ ] A1 — rotate refresh tokens (blacklist consumed jti on /refresh)
- [ ] A2 — change_password revokes all sessions (revoke_all_for_user)
- [ ] A4 — /register constant-time: burn bcrypt on duplicate-email branch
- [ ] A5 — narrow /refresh except to InvalidTokenError only
- [ ] A6 — cap email-driven lockout (anti targeted-DoS); IP keeps full escalation

## Phase 2 — Password reset flow (MED A3)  · routes/auth.py, schemas.py, delivery.py
- [ ] A3 — /auth/forgot-password (enumeration-safe 200) + /auth/reset-password (single-use token, revoke_all_for_user)

## Phase 3 — Rate-limit / CORS (HIGH I1; MED I2; LOW I4)  · rate_limit.py, main.py, settings.py
- [ ] I1 — trust rightmost XFF hop; prefer Fly/CF single-value headers
- [ ] I2 — fail-closed in-process fallback bucket for `auth` zone on RedisError
- [ ] I4 — reject `*` / non-https origins in get_allowed_origins()

## Phase 4 — Exports / CSV injection (HIGH E1; MED E3; LOW E4)  · security.py, data_exporter.py, tests
- [ ] E1 — de-quote/de-space before formula-prefix test in sanitize_for_csv
- [ ] E3 — strip TAB in clean_text
- [ ] E4 — to_json: sanitize unconditionally (incl. non-str)
- [ ] tests — leading-quote + embedded-tab + non-str payloads

## Phase 5 — Browser / AI SSRF (HIGH S1, S2)  · base_scraper.py, navigator.py, paginator.py
- [ ] S1 — route guard validates ALL resource types (fetch/xhr), not just document
- [ ] S2 — drop/strictly-gate model-emitted `evaluate` JS

## Phase 6 — Outbound-fetch SSRF (MED N1; LOW S4, S5, N3)  · pacs.py, security.py, county_gis.py, scrapers/*
- [ ] N1 — assessor_url via validate_scraping_target(resolve=True) + safe_http
- [ ] S4 — remaining raw requests.* through safe_http
- [ ] S5 — resolve=True default + IDNA fail-closed + loopback aliases
- [ ] N3 — persist validated gis_endpoint/assessor_url or drop from API

## Phase 7 — Billing / webhook idempotency (HIGH T3, B1; MED B2; LOW B3, B4)  · webhooks.py, tracerfy_ingest.py, skip_trace_usage.py, billing.py, delivery.py
- [ ] B1/T3 — queue_id idempotency + edge SET NX; don't trust body download_url
- [ ] B2 — tie counter advance to UNIQUE(queue_id,user_id) meter record
- [ ] B3 — clamp attempt_count
- [ ] B4 — cache coupon lookup; narrow except

## Phase 8 — Tenancy / RLS / PII (HIGH T2; MED I3, N2; LOW T1)  · new migration, session.py, scrapers.py, jobs.py, tasks.py
- [ ] T2 — migration WITH CHECK + fail-closed startup on BYPASSRLS role
- [ ] N2 — /scrapers/sample static fixtures / redact addresses
- [ ] I3 — log result id, not party_name
- [ ] T1 — decode download token with explicit audience

All 8 phases ✅ implemented + committed (12 commits on `security/redteam-remediation-2026-06-01`).
Phases 1–5 by Claude directly; Phases 6/7/8/2 by 4 parallel subagents (disjoint files).

## Round 3 verify
- [x] py_compile + ruff clean every phase; CSV sanitizer proven vs 11 payloads
- [x] Codex review × multiple rounds — caught 4 bugs in Claude's fixes (I1,A1,A2,A6) + 3 in subagent code (webhook dedup, reset timing, reset reuse-policy); all fixed
- [x] Codex `codex review` over full diff; reconciled; convergence re-review running
- [ ] Update REDTEAM register statuses to ✅; BUILD_JOURNAL entry (after convergence verdict)

## Review
Cross-verification (Claude×Codex) caught **7 total bugs in the fixes themselves** across 3 Codex
rounds — the core value of two independent reviewers. Integration tests need CI Postgres+Redis.
T2 needs the Supabase runtime role confirmed `NOBYPASSRLS` for the RLS belt to engage.
