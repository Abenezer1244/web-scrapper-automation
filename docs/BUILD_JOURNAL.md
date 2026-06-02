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

**Caught & fixed (the headline):** the Claude×Codex loop caught **12 bugs in the fixes themselves** across
5 Codex rounds — e.g. refresh-rotation TOCTOU (→ SET NX), XFF trusting spoofable Fly/CF headers on Railway,
password-change revoking *after* commit, a cosmetic lockout cap, a swallowed Stripe error defeating
autoretry, an enqueue-failure losing a meter event, a stale second reset link surviving a reset. Each was
re-fixed and re-verified. This is why two reviewers > one.

**Failed / Blocked:** full integration tests can't run locally (need CI Postgres+Redis; `conftest.py` wires
real infra) — verified statically (`py_compile` + `ruff` every phase) + pure-function CSV tests + the Codex
review gate. Local `pytest -k auth` ran against degraded infra (503s from Redis-unavailable revocation, DB
connection errors) — not logic regressions.

**Pending / Handoff:**
- **NOT merged to `main`** — branch awaits review/merge.
- **T2 ops:** confirm the deployed Supabase/Postgres runtime role is `NOBYPASSRLS NOSUPERUSER` (migration 025
  + startup guard only bite if the role can actually be constrained); run `alembic upgrade head` (025 + 026).
- **Migration collision heads-up:** the older `security/high-2-rls` branch also has a `025_*`; this branch's
  `025_rls_with_check_write_policies` + `026_add_skip_trace_meter_outbox` chain off `024`. Reconcile before
  merging either branch.
- Final Codex convergence review of the whole diff in progress at write time.

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
