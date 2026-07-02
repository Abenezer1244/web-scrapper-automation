# Tier Enforcement — End-to-End Design (2026-06-22)

> Status: **APPROVED design** — ready for implementation planning.
> Goal: wire the 4 subscription tiers (Starter / Pro / Business / Agency) end-to-end so each
> tier's access and limits are actually enforced everywhere a scrape can run or data can leave
> the system — not just when a config is created.
> Reviewers: Claude (Opus 4.8) + Codex (codex-cli 0.139.0). Codex pressure-tested the plan and
> found 4 bypasses beyond the initial analysis (see §3).

---

## 1. Decisions locked (with the user)

1. **Tier names stay as in code:** `starter` (free, $0) / `pro` ($199) / `business` ($499) / `agency` ($1499). No renaming.
2. **Primary goal:** flip enforcement ON and prove every gate fires per tier, with a safe rollout that does not lock out existing accounts.
3. **Canonical matrix = Option A** (matches `docs/pricing-strategy-2026-06.md` §4 and `src/config/constants.py`):

| Tier | Counties (user picks any N) | Record types | Key feature gates |
|---|---|---|---|
| **Starter** (free) | 1 | `probate` only | no skip-trace, no webhook/API |
| **Pro** ($199) | 3 | `probate`, `pre_foreclosure`, `tax_delinquent` (core 3) | skip-trace add-on (250 incl.), batch |
| **Business** ($499) | 10 | all 6 (+ `divorce`, `code_violation`, `death_certificate`) + overlap | webhook + dialer + API, 1,000 skip-traces |
| **Agency** ($1499) | unlimited | all 6 + overlap | white-label, 2,500 skip-traces, priority queue |

   - **Counties are count-based and user-choice** (already the design): cap *how many* distinct counties, never *which*.
   - **Record types are a capability menu, not a count.** Premium lists (`divorce`, `code_violation`, `death_certificate`) and the distress-list *overlap/intersection* are gated at Business — the #1 monetization lever per the strategy doc ("Goliath charges $899 for exactly this").
   - The stale customer-facing copy in `src/api/routes/billing.py:253` and `:426` (Pro = "all record types" / 5 counties, Business = unlimited — leftover from the old $79 era) is **wrong** and must be corrected to match this matrix.

4. **Out of scope:** team/seat enforcement (no `teams` table exists — separate feature) and the `bridgeleads-web` frontend UI for 402/403 handling (user chose backend-only). Both noted as follow-ups in §8.

---

## 2. Current state (verified against code)

Most gates are **already enforced, always-on** and need no change:

| Gate | Enforcement site | Verdict |
|---|---|---|
| Batch access (Pro+) | `routes/batches.py:56` | OK |
| Batch combo caps | `routes/batches.py:63` | OK |
| Skip-trace add-on (Pro+) | `routes/scrapers.py:153`, `routes/batches.py:84`, `tasks_helpers/enrich.py:563` | OK |
| Webhook / business features (Business+) | `routes/scrapers.py:138-149`, `routes/batches.py:74-83` | OK at create-time only (see §3) |
| Priority queue routing | `routes/jobs.py:179` | OK |
| API-key creation (Business+) | `routes/auth.py:387` via `require_plan` | OK at create-time only (see §3) |
| Monthly records cap | `routes/jobs.py:140`, `routes/batches.py:90` | OK |
| AI job limits | `routes/jobs.py:100` | OK |
| Trial → Starter downgrade (hourly) | `workers/scheduler_helpers/billing.py:66` | OK |

The **only feature-flagged-OFF gate** is `enforce_entitlements()` (county cap + record-type gating) in
`src/api/entitlements.py`, gated by `settings.ENTITLEMENT_ENFORCEMENT` (default `False`, audit-only).
It is called from `routes/scrapers.py:109` and `routes/batches.py:129`.

---

## 3. Root cause + the bypasses Codex found

**Root cause:** county/record-type entitlements are validated **only at config-create time**, never at
execution time. So any path that runs an *existing* config after a plan change leaks.

Verified leak points (all confirmed by Codex against the code):

1. **`POST /jobs`** (`routes/jobs.py:79`) — loads the active config, checks AI cap + records cap, enqueues. **No entitlement check.**
2. **Scheduler dispatch** (`workers/scheduler_helpers/dispatch.py:35` single, `:150` batch) — only checks schedule + records cap.
3. **Batch fan-out** (`workers/batch_tasks.py:119`) — child jobs only recheck records cap.
4. **Worker job-start** (`workers/tasks.py:251/285/324`) — `run_scrape_job` goes straight to scraping. Final backstop is missing.
5. **Generic webhook send** (`workers/tasks.py:1108`) — sends if `deliver.webhook_url` is set; comment says "gated at create time." Dialer push *correctly* rechecks current plan (`workers/scheduler_helpers/dialer.py:118`) — the generic webhook does not. A downgraded Business user keeps receiving webhooks.
6. **API-key use** (`src/api/auth.py:278`) — authenticating with an existing key checks hash + active user only, never current plan. A downgraded user keeps API access.

**TOCTOU race** (documented at `entitlements.py:78-81`): `projected_county_overage()` counts active
configs then decides, with no lock. Two concurrent creates can both pass and exceed the cap.

**Net:** the real invariant must be checked at **execution time**, not just at create time.

---

## 4. Design

### 4.1 One principle
Tier access is an **execution-time invariant**. Add one shared check; call it at every point a scrape
can start or data can leave the system. Create-time checks stay (fast feedback) but are no longer the
only line of defense.

### 4.2 Components

**A. Canonical matrix (single source of truth).**
`src/config/constants.py` already encodes Option A correctly (`COUNTY_LIMIT_BY_PLAN:149`,
`RECORD_TYPES_BY_PLAN:170`). Work: correct the stale plan-catalog/comparison copy in
`src/api/routes/billing.py:253` and `:426` so the `/billing/plans` API the frontend renders matches
the matrix. No DB change.

**B. Shared execution-time guard.**
Extend `src/api/entitlements.py` with a current-plan validator answering: *for this user's CURRENT
plan, is this config's `record_type` allowed, and is its `county` within the user's still-allowed
county set?* Provide:
- async `assert_config_runnable(db, user, config)` for API routes,
- a sync twin (or shared core) for Celery workers (they use `SyncSessionLocal`).

The **county** half at execution time is count-based, so "allowed set" is computed deterministically:
rank the user's distinct active counties by the **earliest `created_at`** among their configs; the
first N (= plan cap) are allowed; configs in counties ranked beyond N are not runnable. The
**record-type** half is a cheap per-config membership test against `RECORD_TYPES_BY_PLAN`.

Wire the guard into all six leak points from §3:
`routes/jobs.py` (POST /jobs), `scheduler_helpers/dispatch.py` (single + batch), `batch_tasks.py`
(fan-out), `tasks.py` (worker job-start backstop), `tasks.py:1108` (generic webhook send — mirror
`dialer.py:118`), and `auth.py:278` (API-key auth → current-plan check for Business+ API access).

Behavior is governed by the **existing `ENTITLEMENT_ENFORCEMENT` flag**: OFF → log "would block"
(audit), ON → raise 402 (or 403 for API-key/feature gates). This lets us measure before blocking.

**C. Downgrade reconciliation.**
When a plan drops — Stripe `customer.subscription.deleted`/`updated` (`routes/billing.py`) and the
trial-expiry beat task (`scheduler_helpers/billing.py:66`) — run a deterministic reconciliation:
- pause every config whose `record_type` is no longer allowed for the new plan;
- for counties, keep the oldest-N distinct counties (by earliest `created_at`), pause configs in
  counties beyond the cap;
- mark paused configs with a new `paused_reason='entitlement'` marker so the UI can show
  "Paused — upgrade to re-enable" and a later upgrade can revive exactly those.

Reconciliation prevents endless failed scheduled runs and noisy batch children. The execution guard
(B) remains the safety net for paths reconciliation can't pre-empt (API jobs, retries, watchdog
requeues, in-flight jobs). Child configs (those with `batch_id` set) are included; their parent batch
owns schedule/delivery, so pausing a child stops its scrapes without orphaning the parent.

**D. TOCTOU fix.**
In the two create paths (`routes/scrapers.py`, `routes/batches.py`), `SELECT ... FOR UPDATE` on the
user row (or a per-user advisory transaction lock) before counting counties, so concurrent creates
serialize and cannot both pass the cap. Use the same pattern in both paths.

### 4.3 Data model change
New nullable column on `scraper_configs` (Alembic migration, next number after 069):
- `paused_reason VARCHAR(32) NULL` — `NULL` = active/normally-paused by user; `'entitlement'` = paused
  by downgrade reconciliation. Distinguishes system-paused from user-paused so re-upgrade only revives
  what the system paused. (`active` stays the operational flag; `paused_reason` is the *why*.)

No backfill required (existing rows = `NULL`).

---

## 5. Rollout (Codex's order — measure before blocking anyone)

Every new check ships behind `ENTITLEMENT_ENFORCEMENT` in **audit mode first**.

1. **Freeze matrix** — fix `billing.py` copy; confirm `constants.py` canonical. (~2 files)
2. **Build guard + wire all 6 execution points in AUDIT mode** (+ webhook + API-key). (core)
3. **TOCTOU lock** in the two create paths.
4. **Downgrade reconciliation** — migration (`paused_reason`) + webhook/trial handlers; dry-run logged first.
5. **Read audit logs** → enumerate exactly who would be blocked → decide grandfathering from real data
   (strategy doc reports ~0 paying customers, 144 early-access, 4 with billing → likely a no-op; if any
   paid account is affected, add a time-bounded grandfather override keyed to renewal date).
6. **Flip `ENTITLEMENT_ENFORCEMENT=True`** (env on api + worker).
7. **Live-verify every gate per tier** (proof, as requested).

Each phase touches ≤5 files and is verified before the next (CLAUDE.md phased-execution rule). Codex
reviews every phase's diff (`codex review` / `codex challenge`); any Critical/High from either reviewer
is NO-GO until fixed.

---

## 6. Testing & verification

- Real DB + real settings, no mocks (per `.claude/rules/testing.md`).
- Unit tests for the guard: each tier × (allowed record type, blocked premium type, county within cap,
  county over cap, downgraded user with stale config).
- Reconciliation tests: downgrade Business→Starter with 3 counties + a premium type → correct configs
  paused with `paused_reason='entitlement'`, correct ones kept, re-upgrade revives only system-paused.
- TOCTOU test: two concurrent creates at the cap → exactly one succeeds when enforcement ON.
- Live per-tier verification before declaring done: Starter blocked from `pre_foreclosure`; Pro blocked
  from a 4th county and from `divorce`; trial-expiry → downgrade → next scheduled run blocked; API key
  rejected after downgrade; generic webhook suppressed after downgrade.

---

## 7. Risks

- **Locking out a paid account at flip time.** Mitigated by audit-first measurement (step 5) before the
  flip; grandfather overrides for any affected paid account.
- **County-keep ordering surprises a user** (we pause their newer counties). Mitigated by the
  `paused_reason` marker + clear "upgrade to re-enable" surfacing; deterministic oldest-N rule is
  explainable.
- **Worker/API plan staleness.** The guard reads the user's plan fresh at execution; reconciliation runs
  on the actual downgrade events, so no stale snapshot is trusted.
- **Double-pausing user-paused configs.** Avoided by only ever setting/clearing `paused_reason='entitlement'`;
  user-initiated `active=false` is never overwritten.

---

## 8. Follow-ups (explicitly deferred)

- **Frontend (`bridgeleads-web`):** render 402/403 as upgrade prompts, plan badges, disabled controls,
  "Paused — upgrade to re-enable" on entitlement-paused configs.
- **Team/seat enforcement:** no `teams`/`team_members` table exists; the 1/1/5/unlimited seat tiers in
  marketing are unimplemented. Separate spec.
- **Export-format gating:** currently CSV-only, nothing to gate; revisit only if Excel/JSON/API exports ship.

---

## 9. Open items for implementation planning

- Exact lock mechanism for §4.2 D (row `FOR UPDATE` vs `pg_advisory_xact_lock`) — decide in the plan;
  both acceptable, pick one and use it in both create paths.
- Whether the API-key path (§4.2 B, `auth.py:278`) should *reject on use* vs *revoke keys on downgrade* —
  reject-on-use is simpler and reversible; default to that unless planning surfaces a reason otherwise.
