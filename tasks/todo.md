# H1 — RLS Enforcement Cutover (2026-06-12)

**Goal:** flip Postgres RLS from decorative (BYPASSRLS `postgres` role, `RLS_ENFORCE=False`)
to enforced (two NOBYPASSRLS roles + role-targeted policies + `RLS_ENFORCE=True` + FORCE).
The last code item on `tasks/BACKLOG.md`.

**Foundation already on main (Codex-reviewed 2026-06-02, `tasks/rls-cutover-todo.md`):**
Phase 0–4 code complete — `provision_rls_roles.sql`, `apply_rls_cutover_policies.sql`,
`apply_rls_force.sql`, GUC-reapply listener (`session.py:107`), `get_rls_db`, SECURITY
DEFINER helpers (mig 029), no-op migs 030/031, 4 integration test files, runbook.
`users` login problem already solved: broad `users_app` policy (auth-bootstrap inherent).

**What drifted since 2026-06-02 (this session's code work):**
| Table | RLS state today | App-role need (route-audit verified) |
|---|---|---|
| `mfa_backup_codes` (043) | RLS ✅ + GUC policy | SELECT/INSERT/UPDATE/**DELETE** (auth.py:701,1209,1293) |
| `mfa_break_glass_codes` (045) | RLS ✅ + GUC policy | SELECT/UPDATE (atomic consume + revoke) |
| `scraper_batches` (050) | **NO RLS** | SELECT/INSERT (batches.py) |
| `batch_runs` (050/052) | **NO RLS** | SELECT/INSERT (durable run intent) |
| `audit_events` (055) | **NO RLS** | INSERT (audit_log background task, AsyncSessionLocal) |
| `dialer_deliveries` (041) | RLS ✅ + GUC policy | none direct — replay route uses `system_sync_session` (⚠️ flag to Codex) |

Worker-only tables (skip_trace_*, delivered_records, county_records): app needs ZERO access — verified.
All PKs are UUIDs (no sequence drama). No staging env exists (Railway prod only) — rehearsal strategy is an open decision.

---

## Design decisions — RESOLVED (Codex consult 2026-06-12, session 019ebbc2)

1. **MFA DELETE** ✅ Codex: scoped DELETE grant on `mfa_backup_codes` only; explicit
   verify-block allowlist; do NOT generalize app DELETE. (SECURITY DEFINER = overkill.)
2. **audit_events** ✅ Codex: ENABLE RLS (don't leave disabled); app INSERT-only
   `WITH CHECK (true)` (audit session has no GUC, user_id nullable); system FOR ALL;
   no app SELECT.
3. **batches policies** ✅ Codex: explicit `FOR SELECT` + `FOR INSERT` policies,
   NOT `FOR ALL` ("workable but sloppy and brittle").
4. **dialer-replay** ❌ REJECTED as-is — `_cutover_step4_repoint.py:70` deliberately
   gives the API the **app** role on `DATABASE_URL_SYNC` (no system creds in the
   internet-facing process), so the replay route would BREAK at cutover. Fix:
   refactor route to async app session + narrow app UPDATE grant/policy on
   `dialer_deliveries`.
5. **Rehearsal** ✅ Codex: scratch Supabase project is the minimum responsible
   substitute for staging. Do it.
6. **Supavisor** ✅ pooler-safe (`set_config(...,true)` + after_begin reapply);
   `role.project-ref` username already in `_cutover_step1_roles.py`; verify both
   poolers during rehearsal. Session advisory locks unsafe on :6543 — migrate.py
   already rejects it.

**Codex-found additional blockers (adopted):**
7. `dialer_deliveries` missing from `apply_rls_cutover_policies.sql` + `apply_rls_force.sql`.
8. `scripts/reset_user_mfa.py:92` physically DELETEs MFA rows as system → system role
   needs DELETE on `mfa_backup_codes` + `mfa_break_glass_codes`.
9. Python cutover scripts mirror the stale SQL (`_cutover_step2_grants_policies.py`) —
   update in lockstep or the operator path runs stale grants.
10. Per-service env split is explicit: API `DATABASE_URL_SYNC`→app, worker→system.
11. Integration tests must cover the drift tables (MFA, batches, audit no-GUC insert,
    dialer_deliveries, operator DELETE case).

## Phase A — Code (this session, ≤5 files per step, Codex gate each)

- [x] A1–A8 ALL DONE — PR #33 merged (5 commits `7ad1e56`..`b586e16` + `af0856e`-equivalent
      test fix). Codex: consult (5 blockers adopted) + review PASS + challenge
      (0 P1/3 P2/2 P3, all fixed) + re-gate CLEAN. CI green.
      ⚠️ `.env.example` was session-write-protected → 👤 manual `RLS_ENFORCE=false` line.

## Phase B — Execution — ✅ ALL DONE 2026-06-12, RLS ENFORCED IN PROD

- [x] B1. Scratch Supabase rehearsal (project `fsakmdkiwvhiiekhvblw`, deleted after):
      migrated head, prod artifacts applied, Supavisor custom-role auth PROVEN on both
      poolers, API booted as app role with RLS_ENFORCE=true (register+login worked),
      FORCE applied, suite green pre+post FORCE. Caught: test_rls_isolation false-fail
      (inverse skip guard shipped), CRLF-in-password footgun.
- [x] B2. Prod: roles (step1, pooler auth SUCCESS) + grants/policies (step2,
      verify=0, 47 policies; one transient lock_timeout → clean retry) + read-only
      rehearsal on prod data (step3: 10/10 PASS, 310k rows).
- [x] B3. Repoint: per-service vars verified EXACTLY (api=app/app, worker+beat=
      app/system, migrate=postgres), staged --skip-deploys, redeployed beat→worker→api.
      pg_stat_activity confirmed live traffic on the new roles. Login/authed reads 200.
- [x] B4. RLS_ENFORCE=true all three services — every fail-closed boot gate passed.
      Live E2E ran BEFORE the flip (Codex order): island/WA probate job DONE,
      147 records, results readable via app session.
- [x] B5. FORCE on 23 tables. SECURITY DEFINER paths verified (/scrapers/sample,
      /billing/referral 200). Worker permission errors: 0.
- [x] B6. Prod integration suite (owner DSN): 13 passed / 2 skipped (legacy module
      correctly superseded).
- [x] B7. Codex final gate: **SIGN-OFF** ("H1 is complete... No remaining cutover
      blocker"). §14 master-review sweep = optional follow-up formality.
- [x] B8. BACKLOG/BUILD_JOURNAL/memory updated.

**Rollback (emergency-only now):** repoint the three URLs back (captured in
`.rls-cutover-secrets`) + `RLS_ENFORCE=false` + `NO FORCE` block in apply_rls_force.sql.

## Review

**H1 closed end-to-end in one session: code (PR #33) → scratch rehearsal → prod cutover →
enforcement → FORCE → verification → Codex SIGN-OFF.** The DB now enforces tenant isolation
independently of the app layer: a future missed `WHERE user_id` filter returns 0 rows instead
of leaking cross-tenant. Key catches along the way: the dialer-replay route would have broken
at cutover (Codex consult), test_rls_isolation would have false-failed on prod (scratch
rehearsal), CRLF in a Windows-written secrets file broke pooler auth, and prod Upstash
throttling had to clear before the repoint. Residual user actions live in BACKLOG §4
(secrets file → password manager; .env.example line).
