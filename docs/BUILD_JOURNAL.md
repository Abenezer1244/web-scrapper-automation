# BridgeLeads — Build Journal

**Purpose:** The running, append-only record of what was **built**, **tried**, **failed**, and
**succeeded** — plus the decisions behind them. Newest entry on top. This is the place to look
to understand *why* the code is the way it is and *what's been attempted before*.

> **How to add an entry** (do this at the end of any substantial session):
> ```
> ## YYYY-MM-DD — <short title>
> **Built / Shipped:** what actually landed (with commits/paths).
> **Tried / Decided:** approaches considered, chosen, or rejected — and why.
> **Failed / Blocked:** what didn't work, dead ends, external blockers.
> **Caught & fixed:** bugs found in review before shipping.
> **Pending / Handoff:** what's left, who owns it.
> **Facts learned:** durable truths about the system worth remembering.
> ```
> Keep it honest — record failures and dead ends, not just wins. See also
> `docs/security/REVIEW-2026-06-01.md` (security tracker) and `CLAUDE.md`.

---

## 2026-06-02 — SQL-injection audit (Claude × Codex): NO SQLi found; pivoted to DB role least-privilege cutover plan

**Built / Shipped:**
- Full SQLi audit of the FastAPI/SQLAlchemy/Supabase app on a user request ("search bar wiped the users table").
  Traced every user-input→DB path. **Verdict: no SQL injection exists.** Everything is parameterized:
  `text()` with `:named` binds throughout; search uses ORM `ilike(pattern, escape="\\")` (`jobs.py:241`) and
  static clause-strings with `:q`/`:kw_n` binds (`scrapers.py:477-532`, plus `sanitize_search()`); the f-string
  INSERT in `tasks.py:463` interpolates only placeholder *tokens* (data → `params`); alembic/scripts f-strings
  use hardcoded constants only; advisory-lock f-string is a guaranteed `int(md5,16)`. No psycopg/asyncpg raw
  cursor, no `from_statement`/`literal_column` w/ user data, no dynamic `order_by`/column injection.
- **Codex independently CONFIRMED** "no SQLi" (read the files itself; also noted `billing.py:58` — `days` bound,
  int 1-365, safe). Codex then sharpened the real fix (below). Cross-confirmation per codex-collaboration rule.
- **Real risk = over-privileged role,** not injection: prod connects as a `BYPASSRLS` role (matches today's
  earlier journal entry + the RLS_ENFORCE landmine). Authored a staged least-privilege cutover:
  `tasks/rls-cutover-todo.md` + `docs/security/RLS-CUTOVER-RUNBOOK.md` + Phase-0 `scripts/provision_rls_roles.sql`.

**Tried / Decided:** Three-role model (Codex's refinement of my single-restricted-role idea): `bridgeleads_owner`
(DDL/alembic), `bridgeleads_app` (API: SELECT/INSERT/UPDATE, **no DELETE** — user deletes are soft:
`jobs.status='cancelled'`, `scraper_configs.active=false`), `bridgeleads_system` (workers: + DELETE on
`county_records` only, the lone physical delete at `scheduler.py:521`). Rejected blanket-DELETE app role.

**Caught & fixed:** Self-review of the Phase-0 SQL (Codex was rate-limited) caught a missing grant: the API
**writes** `county_connectors` via `POST /connectors` (`scrapers.py:313`). SELECT-only would have permission-
denied at Phase 4 — changed to **SELECT + INSERT** on that table.

**Failed / Blocked:** Codex CLI hit its usage limit mid-session (resets ~3:25 PM local) → the Phase-0 Codex
review gate is DEFERRED, must run before Phase 3 repoints connections. Did NOT fabricate any SQLi "fix" — there
was nothing to fix; reported that honestly instead.

**Pending / Handoff:** Phases 1-4 await user approval (per phased-execution rule). Open Qs answered: custom roles
OK; API uses `DATABASE_URL` / workers+alembic share `DATABASE_URL_SYNC` (`alembic/env.py:15`) → Phase 3 adds
`DATABASE_URL_MIGRATE` for the owner role. Phase 1 is the load-bearing code change (per-transaction GUC reapply
so `app.current_user_id` survives the mid-task commit; else NOBYPASSRLS breaks `run_scrape_job`).

**Facts learned:** This codebase is genuinely hardened against SQLi (8 prior red-team rounds show). The tenancy
boundary today is the app-layer `WHERE user_id` filter, NOT RLS — because the role bypasses RLS. The cutover is
what makes RLS actually load-bearing. `FORCE ROW LEVEL SECURITY` must be the very last step.

---

## 2026-06-02 — CRITICAL: Supabase `rls_disabled_in_public` (county_records PII) — live-fixed + Codex-verified

**Built / Shipped:**
- Supabase advisor flagged CRITICAL "Table publicly accessible — RLS not enabled." Different surface from the
  red-team (which audited the FastAPI app): this is Supabase's auto-exposed PostgREST API (anon key in the
  frontend) where **RLS is the only guard**. A `public` table without RLS is readable/writable by anyone with
  the project URL + anon key, bypassing the app.
- **Live audit** (`scripts/check_rls_roles.py`, read-only): both app roles (`DATABASE_URL` async +
  `DATABASE_URL_SYNC` sync) = `postgres`, `bypassrls=true`. Live, exactly ONE public table had RLS disabled:
  **`county_records`** (3305 rows of scraped homeowner PII) — RLS was *explicitly* disabled in migration 023
  (which relied on a write-trigger that does nothing against anon *reads*).
- **Live hotfix applied** (`scripts/apply_rls_hotfix.py`): `ENABLE ROW LEVEL SECURITY` + a shared-read SELECT
  policy on `county_records`. **Verified live by role impersonation:** `postgres`(BYPASSRLS)=3305 rows,
  `anon`=0, `authenticated`=0 → exposure closed, app unaffected.
- **Permanent migrations:** `027` (ENABLE RLS on the 5 anon-exposed app tables — idempotent, covers the new
  `skip_trace_meter_events` once 026 deploys) + `028` (the county_records shared-read policy).
- **Codex** consulted on the plan (consensus: enable RLS, no policy/FORCE = default-deny for anon, safe under
  BYPASSRLS), then reviewed the build → caught a real **deadlock** (concurrent webhooks locking overlapping
  users in opposite order in the meter outbox) → fixed with `ORDER BY user_id` deterministic locking.

**Tried / Decided:** enable-RLS-no-policy (not FORCE) for the emergency lockout — default-deny stops anon while
the BYPASSRLS app is untouched; FORCE/the WITH-CHECK enforcement belongs in the deferred HIGH-2 cutover. The
county_records SELECT policy denies anon (never sets `app.current_user_id`) yet allows authenticated app
sessions — forward-compat for the non-bypass cutover, inert today.

**Facts learned:** the app connects as `postgres` (BYPASSRLS, not superuser) on both URLs. Supabase exposes a
public PostgREST API guarded ONLY by RLS — every `public` table needs RLS even though the app never uses that
API. `county_records` write-trigger ≠ read protection.

**Pending:** `alembic upgrade head` (applies 025–028) on the next deploy; the live hotfix already covers the
exposed table so prod is safe meanwhile. county_records *writes* under a future non-bypass role still need the
HIGH-2 system-role handling.

---

## 2026-06-01 — Claude × Codex adversarial red-team + remediation (branch `security/redteam-remediation-2026-06-01`)

**Built / Shipped** (14 atomic commits on the branch; full register `docs/security/REDTEAM-2026-06-01.md`):
- **Round 1 — Claude red team:** 6 parallel security-auditor subagents across auth, SSRF, multi-tenancy,
  exports, billing, infra. ~26 findings, each with a proven exploit.
- **Round 2 — Codex independent verification:** Codex re-derived every finding from code — **refuted 6**
  Claude over-claimed (incl. 2 fake "Criticals" → both real but HIGH), and **found 3 Claude missed**
  (PACS assessor SSRF `N1`, unauth `/scrapers/sample` real-PII leak `N2`, dead connector validation `N3`).
- **Round 3 — remediation:** Phases 1–5 by Claude directly; Phases 6/7/8 + the A3 reset flow by 4 parallel
  coder subagents on disjoint files. Fixes (all committed):
  - Auth: refresh rotation (atomic `consume_once`), change-pw revokes sessions, **new password-reset flow**,
    register timing parity, lockout-DoS cap (real TTL decay), narrowed `/refresh` except.
  - SSRF: in-page fetch/XHR egress closed (`base_scraper` route guard validates ALL resource types),
    model-emitted `evaluate` JS removed, PACS `assessor_url` validated, raw `requests`→`safe_http`,
    `validate_scraping_target` resolve-by-default + IDNA fail-closed + loopback aliases.
  - CSV: leading-quote + embedded-tab formula-injection bypasses closed (proven vs 11 payloads) + tests.
  - Billing: Tracerfy webhook replay/SSRF guard, counter↔meter consistency, **transactional meter outbox**
    (`SkipTraceMeterEvent`, migration 026, retrying task + 180s beat sweep), coupon caching.
  - Tenancy: migration 025 adds `WITH CHECK` to RLS write policies + startup hard-fails on a BYPASSRLS
    role; download-token audience hardened; PII log demoted to `Result.id`.
  - Infra: XFF rightmost-hop (kill spoof bypass), fail-closed auth rate-limit fallback, CORS origin validation.

**Tried / Decided:**
- Two independent reviewers with different blind spots is the whole point — kept Claude and Codex passes
  fully independent in Round 1/2 (Codex never saw Claude's findings before re-deriving them).
- Billing durability: rejected fire-and-forget meter reporting; chose a **transactional outbox** (intent
  persisted in the same txn as the counter advance, swept by a beat task) — the only design that survives
  Stripe-down AND broker-down without double-billing (stable MeterEvent id).
- Removed the Tracerfy webhook edge dedup entirely — the worker `FOR UPDATE` + status guard is the
  authoritative idempotency; the edge claim was net-negative (could drop a legit retry).
- Phased, ≤5 files/phase, atomic commit per phase; subagents on disjoint files to parallelize safely.

**Caught & fixed (the headline):** the Claude×Codex loop caught **~17 bugs in the fixes themselves** across
**8 Codex review rounds** — refresh-rotation TOCTOU (→ SET NX), XFF trusting spoofable Fly/CF headers on
Railway, password-change revoking *after* commit, a cosmetic lockout cap, a swallowed Stripe error defeating
autoretry, an enqueue-failure losing a meter event, a stale second reset link surviving a reset, password
recovery leaving the API key valid, a fail-open revoke cache, an RLS guard swallowed by the worker
bootstrap, and — biggest — a **production-outage-class** T2 bug: the hard-fail-on-BYPASSRLS would have made
the API + workers refuse to boot on the *current* prod role, and a downgraded role would block scrapes/ingest
(mid-task commit clears the `SET LOCAL` GUC; `system_sync_session` has no tenant context). Findings shrank
and deepened each round (3→2→3→2→2→1→2) — convergence. Each was re-fixed + re-verified. Two reviewers > one.

**Failed / Blocked:** full integration tests can't run locally (need CI Postgres+Redis; `conftest.py` wires
real infra) — verified statically (`py_compile` + `ruff` every phase) + pure-function CSV tests + the Codex
review gate. Local `pytest -k auth` ran against degraded infra (503s from Redis-unavailable revocation, DB
connection errors) — not logic regressions.

**Pending / Handoff:**
- **NOT merged to `main`** — branch awaits review/merge.
- **T2 / `RLS_ENFORCE` — DO NOT enable yet:** default is OFF (advisory log; the API/workers boot normally on
  today's BYPASSRLS role). The `025` WITH CHECK policies are inert until the role is downgraded. Flip
  `RLS_ENFORCE=True` ONLY after the deferred HIGH-2 cutover lands (non-BYPASSRLS role + per-transaction GUC
  reapply in `rls_sync_session` + a system RLS policy for `system_sync_session`) — else scrapes + ingest break.
  Add `RLS_ENFORCE` to `.env.example` (access-restricted in this session). Run `alembic upgrade head` (025 + 026).
- **Migration collision:** the older `security/high-2-rls` branch also has a `025_*`; this branch's
  `025_rls_with_check_write_policies` + `026_add_skip_trace_meter_outbox` chain off `024`. Reconcile before merge.
- **✅ CONVERGED (2026-06-02):** final Codex review (round 9) over the whole 19-commit diff returned CLEAN —
  "no discrete, actionable regressions ... that would break existing behavior or undermine the intended
  fixes." Both reviewers agree. Branch is review-ready (pending the RLS_ENFORCE/`.env.example` handoff above).

**Facts learned:**
- The codebase was already well-hardened from the prior 2026-06-01 review — remaining bugs were subtle
  (races, TOCTOU, fail-open ordering, durability), exactly where a second independent model pays off.
- New `settings` added: `TRUSTED_PROXY_HOPS` (default 1). New tables: `skip_trace_meter_events`.

---

## 2026-06-01 — Security pack adoption + full review remediation

**Built / Shipped** (all on `main` unless noted; every fix Codex-reviewed):
- **Standing security + Codex workflow:** copied the security pack to `docs/security/`; added
  `.claude/rules/security.md` + `.claude/rules/codex-collaboration.md` (auto-loaded) and a
  SessionStart hook (`.claude/helpers/security-codex-reminder.cjs`) so every session/build runs
  the security baseline and brainstorms-with-Codex-then-Codex-reviews.
- **Full security review** (5 parallel reviewers + Codex cross-check) → `docs/security/REVIEW-2026-06-01.md`
  (Critical 1, High 9, Medium 9, Low 8).
- **CRITICAL-1** webhook SSRF closed (`validate_outbound_webhook`, fail-closed worker gate). `a8b358a`
- **HIGH-1** DNS rebinding + **per-hop context route guard** (`base_scraper._ssrf_route_guard`),
  live-validated with Chromium. `3ace79c`,`f0a2dc4`
- **HIGH-3/4/5** SSRF cluster — `src/utils/safe_http.py` (`safe_get`, `safe_get_following`),
  eagleweb cookie-fetch origin-pin, county_gis endpoint validation. `ea4b4b8`
- **HIGH-6** CSV injection, **HIGH-7** `.e2e` gitignore, **HIGH-8** API-key revocation on logout-all,
  **HIGH-9** Stripe error leak. `51b7b45`,`056a1b6`
- **All 9 Mediums** (`f37180c`,`9d52aea`,`c144b01`,`edb0eb2`,`84e02e7`,`ae5235b`,`a9987c8`) — input
  bounds, Tracerfy SSRF, rate-limits, `R2_PUBLIC_URL` gate, RLS-ordering, **revocable download-token
  delivery links** (`src/api/download_tokens.py`, gated on `API_BASE_URL`).
- **All 8 Lows** (`4e3aa4d`,`a8a3315`,`4a70c3d`,`be2fe59`) — global exception handler, admin 403→404,
  scoped reads, PII-log demotion, central `_RedactionFilter`.
- **HIGH-2 (RLS role downgrade): code-ready on branch `security/high-2-rls`** (`d0a89dd`, NOT on main):
  migration `025` (policy `bridgeleads_system` escape + WITH CHECK), `session.py` system engine +
  `after_begin` GUC reapply, cutover runbook. Pending staged cutover.
- **Cleanup:** deleted stray junk + committed-junk (`.terraform` cache w/ a `.exe`, scratch PNGs);
  hardened `.gitignore` (incl. root scratch). `1f5e7ca`,`5f284f3`
- **Ops:** set `API_BASE_URL=https://api.bridgeleads.io` on Railway via CLI; deleted live-credential
  `.e2e_*` files from disk.

**Tried / Decided:**
- Pack adaptation: **kept the Abro pack + a stack-translation rules layer** rather than a full
  native rewrite (Codex argued for native; we deferred it — translation table lives in
  `.claude/rules/security.md`).
- HIGH-2: **deferred to a branch** rather than applying to `main` — a `FORCE RLS` migration
  auto-runs on deploy and would break prod before roles/code exist. Role-based policy escape
  chosen over a GUC bypass (a GUC any SQL path could flip is a weak boundary).
- Delivery links: chose revocable app download-tokens over raw 48h presigned R2 URLs;
  gated on `API_BASE_URL` so it's a safe no-op until configured.

**Failed / Blocked:**
- SSH `git push` denied (no authorized key) → switched remote to HTTPS via `gh` (Abenezer1244).
- Railway `variables --set` first failed ("trial expired"); later succeeded — `API_BASE_URL` set.
- `.env.example` is **sandbox-hard-blocked** for the agent → user must add `API_BASE_URL`,
  `R2_ALLOW_PUBLIC_URLS=false`, `DATABASE_URL_SYSTEM` manually.

**Caught & fixed** (Codex review caught real bugs before shipping):
- CSV sanitizer leading-whitespace bypass (` \t=cmd`); Tracerfy redirect-revalidation gap +
  https→http scheme downgrade; progressively-more unbounded Pydantic fields (4 rounds);
  `safe_get`/`probe` ambient-proxy (`trust_env`) gaps; King page-route taking precedence over
  the context SSRF guard; a `scheduler.py` `F821` NameError (would crash dispatch on deploy);
  2 cutover-runbook P1s (missing `DATABASE_URL` switch; broken `DO/:'var'` role create);
  `.gitignore` missing root scratch paths.

**Pending / Handoff (user/ops):**
- The leaked credential was an **auto-generated E2E demo test-user login**
  (`king_e2e_*@bridgeleads.io`, created by the E2E test tooling — not a real
  customer/admin or external-portal password). Low severity. Optional: rotate or
  disable that throwaway test account. (Local `.e2e_*` files already deleted.)
- HIGH-2 cutover: provision `bridgeleads_app`/`bridgeleads_system` non-owner `NOBYPASSRLS` roles,
  switch all 3 DB URLs, extend `tests/test_rls_isolation.py` to 10 tables, `FORCE` last on staging
  → promote. Runbook: `docs/security/high-2-cutover-runbook.sql`.
- Add a Cloudflare R2 lifecycle expiry rule on the `exports/` prefix (30–90d).
- Decide on `design-system/bridgeleads/MASTER.md` (deleted on disk, deletion not committed).

**Facts learned (durable):**
- Prod DB role has `BYPASSRLS=true` → RLS policies are decorative in prod today; the `WHERE
  user_id` filter is the only tenant boundary until the HIGH-2 cutover.
- No table uses `FORCE ROW LEVEL SECURITY`; the app role owns the tables → a role swap alone is a
  no-op without `FORCE`.
- `SET LOCAL` / `set_config(...,true)` is transaction-local and **dies on commit** — worker
  sessions that commit mid-block lose RLS context (fixed via an `after_begin` listener).
- Download route path is `/jobs/{id}/download` (no `/api/v1` prefix); API domain is
  `https://api.bridgeleads.io` (Railway service `api`, project `bridgeleads-production`).
- Codex CLI works here (`codex exec resume <session> -`); SSH push doesn't (use `gh`/HTTPS).
