# HANDOFF — Scraper names, the King owner-name gap, and a live rate-block — 2026-07-30

Written for a **fresh session with zero context**. Everything below is verified against production
or the repo, not remembered. Nothing is mid-edit; all branches are pushed and all PRs are merged.

**One thing needs a human decision before any further work on King owner names: see §8 (legal).**

---

## 1. How this session started, and what it became

The user opened with *"I have a few questions, cross-check the build"* and asked **one** question:
the dashboard **Scrapers** table was vague because it had no name column.

That one question unwound into:

1. a naming root cause in the batch API (fixed, shipped, verified live),
2. a production cross-check that found **82% of all lead rows have no owner name**,
3. chasing that gap got our production IP **rate-blocked by King County**,
4. building a circuit breaker + durable source-health gate so it can't happen again,
5. a defect I shipped in (4) that made the whole gate **inert in production** until a follow-up fix.

**The user's other original questions were never asked.** That is still the largest open item and a
fresh session should probably start by asking for them.

---

## 2. Where we are RIGHT NOW

`origin/main` = **`d3bb02a`**. Both Railway services (`api`, `worker`) deployed at `d3bb02a`,
status SUCCESS. `/health` 200, `/ready` 200 `{"status":"ready"}`.

Everything this session produced is **merged and deployed**. No open PRs of mine. No uncommitted work.

| PR | What | State |
|---|---|---|
| BE #159 | batch child naming root cause | merged `c17591e` |
| FE #80, #81 | Scraper name column, name required | merged |
| FE #89 | created-at disambiguator + copy fix | merged `53c9589` |
| BE #161 | purge tooling + DELETE-privilege finding | merged `e681372` |
| BE #165 | circuit breaker + source-health gate (**mig 083**) | merged `931e6c4` |
| BE #169 | grants fix for mig 083 (**mig 084**) | merged `d3bb02a` |

**Prod alembic version: 084.** Full suite at time of merge: **1666 passed, 2 skipped, 0 failed.**

---

## 3. The five findings, and what was done about each

### 3.1 Scrapers table had no name column — FIXED, LIVE
Root cause was NOT the missing column. `src/api/routes/batches.py:256` named every batch child
`f"{county.title()} {rt} (batch)"`, **discarding `body.name`** (the batch name the user typed,
stored on the parent at line 228). Every batch over the same county+record_type minted identical
names — **12 of 29 prod configs (41%) collided**, all batch children. Zero carried the frontend
wizard's default, so the wizard was never the source.

Fixed by `derive_batch_child_name()` → `"{Batch} - {County} {Type}"`. Confirmed working on real
traffic: two configs created 5 minutes post-deploy came out as `Batch test - Snohomish Pre
Foreclosure` / `Batch test - Pierce Pre Foreclosure`.

🔑 `config.name` reaches the lead-delivery email **SUBJECT unescaped** (`src/workers/delivery.py:74`
— `html.escape()` guards only the HTML body). The helper sanitizes at the mint point for that reason.

### 3.2 12 legacy duplicate configs — RENAMED, then PURGED
Renamed via `scripts/backfill_batch_child_names.py` (12 renamed, 12 audit rows, collisions 12→0),
then the user chose to hard-delete them. Purged with `scripts/purge_test_batch_configs.py`:
**12 configs + 44,479 results + 322 job_logs + 156 pending_skip_trace_rows + 4 skip_trace_queues +
2 user_record_views + 12 notifications**, backup written first (45,088 rows / 11 tables, verified),
all 9 target tables verified 0 afterwards.

Backup: `scratchpad/purge-backup-final-2026-07-29.json` (80MB). **Ephemeral location** — move it if
the recovery path should survive. It holds Fernet **ciphertext**; restoring needs the *current*
`FIELD_ENCRYPTION_KEY`.

### 3.3 82% of lead rows have no owner name — ROOT-CAUSED, NOT FIXED
**15,954 of 19,451 `results` rows have no `party_name`. Every one is king/tax_delinquent.**
Every other county+type is 0% missing (snohomish/tax 0/2253, pierce/probate 0/255,
pierce/pre_foreclosure 0/803, king/pre_foreclosure 0/140, pierce/trustee_sale 0/34).
King tax has exactly ONE successful job ever (2026-06-23) and it produced **zero** names.
47% (7,542) also have no `property_address`.

Verified chain:
1. `src/scrapers/king_wa_tax_delinquent.py:336` sets `party_name = None` **deliberately** — King's
   Socrata feed has no owner column and King redacts owner from bulk downloads. Correct as designed;
   the name is meant to arrive via enrichment.
2. `src/workers/tasks_helpers/enrich.py` owner repair required `res.mailing_address`, capped at 500,
   and ran **after** a Playwright mailing lookup wrapped in `wait_for(..., 240s)`. A timeout there
   discards everything — which is why the job produced **0, not 500**.
3. `scripts/backfill_king_tax_owner_names.py` matched only `party_name LIKE 'Tax Delinquent%'` — the
   *legacy* placeholder. The scraper writes NULL now, so the repair tool matched **zero rows, ran
   clean, and reported success while fixing nothing.**

**877 of 15,954 now have real names.** The rest are blocked (§3.4).

🔑 `jobs.record_count` is NOT a proxy for `results` row count — it counts only NEW non-duplicate
records. Reading it as "how much data is here" understated these configs by **~500x** (83 vs 44,479)
and nearly justified a delete on false numbers.

### 3.4 Production IP rate-blocked by King County — ACTIVE, UNRESOLVED
Backfilling at `--delay 0.3 --batch 500` (~3.3 req/s) got us blocked. Failures per 500-parcel batch:

| batch | failed after 3 retries |
|---|---|
| 1 | 2 |
| 2 | 145 |
| 3 | **500 (total block)** |

A later 5-parcel probe at `delay=1.0` returned **0/5**. The block **outlives the run**.

Two things made it worse than bad pacing: **a throttled lookup is indistinguishable from "parcel not
found"** at the call site, so the backfill cached real owners as `None` and ground forward through a
total block; and `_fetch_king_owner` retried **3x on transient errors**, multiplying load exactly
when the server was pushing back.

🔑 An 8-parcel pre-flight returned 8/8 in 4.6s and looked perfectly healthy. **A small sample tells
you nothing about sustained-volume behaviour.** The server was fine until roughly request ~1,000.

### 3.5 Source-health gate shipped INERT — FOUND AND FIXED
Migration 083 created `external_source_health` but **granted nothing**. Prod threw
`permission denied for table external_source_health`; the table had **zero grantees** while every
comparable system-written table has `SELECT/INSERT/UPDATE` for `bridgeleads_system` + a
`<table>_system` RLS policy.

It failed **safe but silent**: `check_source_or_raise()` deliberately falls through to "allowed" when
it cannot READ health (health infra must never block real work), so the feature shipped doing nothing
and green CI + a SUCCESS deploy said nothing. Migration 084 fixes it.

🔑 A GRANT alone is insufficient — RLS is enabled on the table, so without a policy the role is
permitted but every row is filtered away. The regression test asserts a real
insert→select→update round trip under `SET LOCAL ROLE`, not `has_table_privilege`.

---

## 4. Active files — what to read first

**The gate (new this session)**
- `src/scrapers/enrichment/source_health.py` — get/assert/mark/probe/recover +
  `sources_due_for_probe()`. **No row == healthy**, so the happy path is one indexed read, never a
  write. Cooldown ladder 24h→48h→72h capped.
- `src/db/models.py` → `ExternalSourceHealth` (bottom of file)
- `alembic/versions/083_external_source_health.py` — table
- `alembic/versions/084_external_source_health_grants.py` — grants + `_system` policy, wrapped in
  `DO`-blocks keyed on `pg_roles` so role-less local/CI DBs no-op (migration 029's convention)

**The source it guards**
- `src/scrapers/enrichment/king_county_assessor.py` — `KingOwnerLookupBlockedError`, rolling-window
  breaker (>10% transient or >50% unresolved over 50), `check_source_or_raise()` at the top of BOTH
  entry points, retries default to **1 attempt**
- `src/workers/tasks_helpers/enrich.py` (~line 519-560) — inline repair, now capped **25** parcels at
  1s, degrades to a user-visible warning

**Ops scripts (all dry-run by default)**
- `scripts/backfill_king_tax_owner_names.py` — the 877/15,954 backfill. **Do not run: blocked.**
- `scripts/backfill_king_probate_current_owner.py` — shares the source, same breaker settings
- `scripts/purge_test_batch_configs.py` — needs `DATABASE_URL_MIGRATE` (see §6)
- `scripts/diag_build_health_sweep.py` — the whole-build production sweep
- `scripts/diag_xcheck_followup.py`, `scripts/diag_xcheck_king_and_canary.py`

**Tests**
- `tests/test_source_health.py` (14, real DB, no mocks)
- `tests/test_king_assessor_owner.py` (16, incl. 3 breaker) — has an autouse fixture resetting the
  shared health row; without it the tripping tests block every later test
- `tests/test_rls_role_policies.py` — the grants regression test is at the bottom

---

## 5. Failed attempts / dead ends — do not repeat

1. **Raising `_MAX_KING_OWNER_PARCELS` from 500 to 10,000.** The user asked why not. Measured
   0.57s/parcel, so 10k inline blows the `wait_for(..., 240s)` at ~420 parcels — and a timeout there
   discards *everything*. Raising the cap makes it strictly worse. The answer is no cap, moved out of
   the job's critical path. Cap is now **25**.
2. **Backfilling at `--delay 0.3`.** Got us blocked (§3.4). Anything resembling sustained multi-req/s
   against a county assessor is wrong.
3. **Re-probing the block aggressively.** A `delay=1.0` probe still returned 0/5 and probing can
   *extend* the block. Correct recovery: **24-48h of no requests**, then ONE parcel already known-good
   from the 877 (an unknown blank returning nothing is ambiguous), then 3 parcels 60s apart.
4. **Trusting a green deploy.** #165 merged green and deployed SUCCESS while being completely inert
   (§3.5). Verify the feature in prod, not the pipeline.
5. **Reading `ruff --statistics` through `tail`.** Cut the top line and hid 94 dead f-strings; I
   reported "no dead code" wrongly. Read statistics from the head.
6. **Guessing table/column names.** Cost three failed prod script runs (`leads` doesn't exist — it's
   `results`; `last_checked_at` → `last_checked`; no `last_error` column on `county_connectors`).
   Read `src/db/models.py` first.
7. **Recreating the local test DB to fix moving test failures.** Helped (11→5) but was not the cause.
   See §7.

---

## 6. Environment landmines (verified this session)

- **Two DB roles.** `DATABASE_URL` → `bridgeleads_app`/`bridgeleads_system`: SELECT/INSERT/UPDATE but
  **`DELETE=False` on EVERY table**, not superuser, no bypassrls. `DATABASE_URL_MIGRATE` → `postgres`,
  bypassrls, full DELETE — the sanctioned elevated path, already in the Railway env.
- **The product never hard-deletes.** `DELETE /scrapers/{id}` (`src/api/routes/scrapers.py:399`) sets
  `active = False`, "preserves job history". The role permissions enforce that.
- **Soft refs that dangle on delete** (no FK): `notifications.job_id`, `delivered_records.first_job_id`,
  `batch_runs.child_job_ids` / `failed_children`. `delivered_records.first_result_id` is SET NULL.
- **Railway auto-deploys `main`** to both `api` and `worker` within seconds of merge. There is no
  manual redeploy step. The api runs `alembic upgrade head` **on boot** — a branch-only migration
  crash-loops prod.
- `railway run` executes **LOCAL** code with prod env. Use a script FILE, not `-c`.
- Verify deployed code with `railway ssh -e production -s <svc>` + `printenv RAILWAY_GIT_COMMIT_SHA`.

---

## 7. The local test rig is NOT isolated

`bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <worktree>` shares **one** `bridgeleads_test` DB
**and one Redis (6379)** with every other process on the box, and conftest `FLUSHDB`s Redis at setup.
Any foreign client corrupts a run — not just a second pytest. Observed **11 connected Redis clients**
doing `lpush`/`exec` while my pytest ran.

**Symptom:** 5-11 failures whose *membership moves between runs*, concentrated in
`test_break_glass_login`, `test_register_email_verification`, `*_tax_cap`, classically
`assert 401 == 400` (the auth rate-limiter tripping on foreign state).

**Diagnose:** `redis-cli -p 6379 info clients` (>1 while nothing of yours runs = contaminated), and
`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` — **two `proxy6543.py` processes = two
rigs running**. Then run the same `-k` subset against a detached `origin/main` worktree: on
2026-07-29 `main` failed 7F+7E where the branch failed 2 — proof it was environmental.

🔑 **GitHub Actions CI is the authoritative gate.** When local and CI disagree, believe CI.

---

## 8. ⚠️ BLOCKING: the legal question

Codex surfaced this and it outranks every technical item.

- King's assessment data download carries a **commercial-use restriction under RCW 42.56.070(9)**
  (Washington's prohibition on using public-record lists of individuals for commercial purposes).
- The King GIS layer that actually exposes `TAXPAYERNAME` (`rpacct_extr`) is marked **"Not Public"**
  with restrictive terms.
- The layer PR #153 uses (`KingCo_PropertyInfo/2`) exposes `PIN`/`ADDR_FULL`, **not** owner name.
- King redacts owner from bulk downloads, so **per-parcel eRealProperty is currently the only owner
  source** — there is no compliant bulk fallback already wired.

Codex: *"do not treat `safe_get`/SSRF safety as permission."*

**Nothing built this session answers whether we should be bulk-fetching King owner names at all.**
The gate only makes us stop politely when told to. Resolve this **before** building the canary —
a canary exists to resume something that may not be resumable.

Options: (a) formal bulk-data request to King (`docs/` has precedents for other counties, e.g.
`docs/snohomish_code_enforcement_data_request.md`, `docs/spokane_data_access_request.md`);
(b) accept blank owner for King tax and rely on address + skip-trace (billable, and skip-trace
currently updates phone/email/status, **not** `party_name`); (c) drop King tax owner names.

---

## 9. Next steps, in priority order

1. **Ask the user for their other questions.** They said "a few" and asked one. Everything above came
   from that single question.
2. **Resolve §8.** Blocks items 3 and 4 entirely.
3. **Beat canary + ops alerting** (Codex's items 6-7, designed but NOT built). The state and
   `sources_due_for_probe()` exist; nothing probes or alerts, so **recovery from a block is manual**
   (clear the row by hand). Design agreed: a new `external-source-canary-check` Beat task beside the
   existing hourly `canary-check` in `src/workers/scheduler.py:78`, probing only after
   `cooldown_until`, one known-good parcel, alert via `src/workers/ops_alerts.py` on **transitions
   only** (healthy→blocked, blocked→healthy), never per skipped job.
4. **Resume the King backfill** — only after §8 and §3.4's cooldown. It is resumable and idempotent.
5. **Three cross-check findings, surfaced but unfixed:**
   - `snohomish` tax_delinquent connector is **`down` with 3 active user-facing scrapers** (the only
     down connector that is user-facing; the other 5 have empty `scraper_class` and 0 configs).
   - `county_connectors.state` case split: **37 `'WA'` vs 14 `'wa'`**. Exact-match joins on state will
     silently miss. Not yet traced to a specific break.
   - **PR #97 appears obsolete** — clark/tax_delinquent has 0 rows and no Clark connector offers that
     record type. Probably close rather than merge.
6. **Per-source rate budgeting** (Codex flagged as the next layer, deliberately not built): one
   tenant's backfill can block the source for all tenants. Health state does not prevent the *next*
   block.

---

## 10. Product-quality note worth carrying forward

Of the 19,451 result rows now in prod, **King tax_delinquent is 15,954 of them and has no owner
name**; 47% also lack a property address. A "motivated seller lead" with neither cannot be mailed or
skip-traced. Whatever the resolution to §8, the product currently presents these as leads. Consider
whether exports should expose an `owner_unavailable` / `owner_source` signal rather than a silent
blank — Codex's recommendation, and not built.

Do **not** reintroduce a synthetic placeholder name. Blank is honest; a fake lead name is worse.
The old `tax_placeholder_party` helpers are retained ONLY so the backfill can recognise historical
rows — they are still referenced in `enrich.py` and are not dead code.
