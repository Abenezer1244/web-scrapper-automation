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

- [ ] A1. `scripts/provision_rls_roles.sql`: app grants — mfa_backup_codes S/I/U/D,
      mfa_break_glass_codes S/U, scraper_batches S/I, batch_runs S/I, audit_events I,
      dialer_deliveries S/U (replay refactor); system DELETE on both MFA tables
      (reset_user_mfa.py); REVOKE/verify blocks updated with explicit allowlist.
- [ ] A2. Migration 056: ENABLE RLS + tenant GUC user_isolation policies on
      `scraper_batches` + `batch_runs` (mirror 043); ENABLE RLS on `audit_events`
      + PUBLIC-shaped INSERT policy (role-independent, alembic-safe).
- [ ] A3. `scripts/apply_rls_cutover_policies.sql`: role-targeted policies — batches
      (app FOR SELECT + FOR INSERT), audit_events (app FOR INSERT WITH CHECK true),
      dialer_deliveries (app SELECT/UPDATE tenant-scoped), MFA tables (app + system),
      convert 043/045/041 GUC policies to _app/_system; `scripts/apply_rls_force.sql`:
      extend FORCE list + convergence check (incl. dialer_deliveries).
- [ ] A4. Refactor dialer-replay route (`scrapers.py:556`) → async app session
      (get_rls_db) instead of system_sync_session.
- [ ] A5. Update `scripts/_cutover_step2_grants_policies.py` (and any other Python
      cutover mirror) in lockstep with A1/A3.
- [ ] A6. Tests: async AsyncSession GUC-reapply test (old Codex note D); extend
      `tests/test_rls_role_policies.py` for MFA/batches/audit/dialer tables.
- [ ] A7. `.env.example`: `RLS_ENFORCE` + `DATABASE_URL_MIGRATE` documented; runbook
      drift-table addendum.
- [ ] A8. Codex review of full diff (review + challenge). Fix all P1/P2. CI green. PR.

## Phase B — Execution (ops, with user, runbook order is LAW)

- [ ] B1. Rehearse on scratch DB (per decision 5): `alembic upgrade head` →
      `provision_rls_roles.sql` → `apply_rls_cutover_policies.sql` → boot API+worker
      against it with repointed URLs, `RLS_ENFORCE=False` → smoke: register/login/MFA/
      create job → `RLS_ENFORCE=True` → integration tests → `apply_rls_force.sql`.
- [ ] B2. Prod: merge PR (auto-deploys; mig 056 must be inert-safe) → run
      `provision_rls_roles.sql` + `apply_rls_cutover_policies.sql` against prod.
- [ ] B3. Prod repoint (Railway api+worker): `DATABASE_URL`→bridgeleads_app,
      `DATABASE_URL_SYNC`→bridgeleads_system, `DATABASE_URL_MIGRATE`→postgres,
      keep `RLS_ENFORCE=False`. Verify boot logs show `bypassrls=False`, full live
      cycle: login, register, MFA, scrape job, batch, delivery email, Stripe webhook.
- [ ] B4. Flip `RLS_ENFORCE=True` (api+worker). Verify boot + live cycle again.
- [ ] B5. `apply_rls_force.sql` (LAST). Verify SECURITY DEFINER fns still work
      (referral grant, activation funnel, /scrapers/sample).
- [ ] B6. Run the 9 `@pytest.mark.integration` RLS tests against prod DB.
- [ ] B7. Master Security Review (§14) twice-clean + Codex final gate.
- [ ] B8. Update BACKLOG.md (H1 done), BUILD_JOURNAL.md entry, memory.

**Rollback at any point:** repoint the three URLs back to the `postgres` role +
`RLS_ENFORCE=False`. FORCE is the only step that changes table state — `apply_rls_force.sql`
is reversible via `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`.

## Review
- _pending_
