# Lead Targeting & Delivery — Milestone Design

**Date:** 2026-06-04
**Status:** Phase 1 detailed; Phases 2–5 scoped (each gets its own spec when reached)
**Author:** Claude (brainstormed with user) + Codex review (session 019e917c)
**Reviewers:** Codex consult complete; Codex diff review required per phase before merge.

---

## 1. Problem / Motivation

A real-estate investor (primary user) requested four capabilities. Verbatim intent:

1. **Tax filters** — filter tax-delinquent leads by *time delinquent* (e.g. 6 vs 18 months) and by *amount owed*.
2. **Pre-foreclosure document-type control** — choose which foreclosure document drives the list (Notice of Default → Notice of Trustee Sale → Lis Pendens), and get the *earliest recorded* signal each county exposes.
3. **End-to-end automation** — scrape → skip-trace → push into a dialer (Enzo) with no manual steps.
4. **Combine + overlap lists** — merge multiple record types into one list (union), and surface properties appearing on **two** lists (intersection, e.g. probate ∩ pre-foreclosure = highest-motivation sellers).

Priority counties: **King, Pierce, Snohomish, Kitsap.**

### User decisions (locked 2026-06-04)
- Build order: **Codex's order** (foundation → #2 → #4 → #1 → #3).
- Enzo dialer: **has an API**; docs/credentials to be supplied at Phase 5.
- Tax filters: **all 4 counties** (accepting per-county treasurer-scraper research for Pierce/Snohomish/Kitsap).
- #4: **both** union and intersection matter.
- Scalability: explicitly a thousands-of-users SaaS; overlap must be O(indexed lookup), not O(scan).

---

## 2. Current-State Facts (verified by reading code)

- **`ScraperConfig`** = exactly one `county` + `state` + `record_type`. JSON cols: `fields`, `enrichment`, `schedule`, `deliver`, `skip_trace_enabled`.
- **`Job`** = one `ScraperConfig` run.
- **`Result`** (`src/db/models.py:158`) = one scraped sighting per job. Has `parcel_id`, `property_address`, `party_name`, `enrichment_data` (JSON), skip-trace fields, `dedup_hash` (indexed), `is_duplicate`. **No `doc_type` column. No amount column. No dedup-basis column.**
- **`DeliveredRecord`** (`src/db/models.py:202`) = append-only ledger, `UNIQUE(user_id, dedup_hash)`. Written at **scrape time** (`src/workers/tasks.py:447–494`) via `INSERT … ON CONFLICT DO NOTHING RETURNING`. Claimed hash = first delivery; unclaimed = `is_duplicate=true`.
- **Billing** (`src/workers/tasks.py:563–569`): `billable_count = len(records) - dup_count`, increments `User.records_used`. Happens at scrape time, *before* any user-facing filtering exists.
- **Duplicates are NOT deleted** — they are stored in `results` with `is_duplicate=true`, excluded from the results page (`record_count` = unique only) and from quota. The overlap signal exists in the DB but is never surfaced.
- **`dedup_hash`** (`src/workers/tasks.py:345`) = sha256 of normalized `parcel_id|property_address`; fallback `NAME:name|DATE:date`. Independent of `record_type`. The basis (parcel vs address vs name/date) is computed but **not recorded**.
- **`doc_type`** is captured in `ScrapedRecord.doc_type` and persisted to `CountyRecord.doc_type`, but never carried into `Result` and not user-filterable.
- **Tax-delinquent data**: only **King** has structured dollar amounts + tax year (King Socrata open-data API → `enrichment_data.delinquent_amount`, `billed_amount`, `bill_year`). Pierce/Snohomish/Kitsap currently derive "tax delinquent" from recorder keyword matches — **no dollar amount, no delinquency age**.
- **Pre-foreclosure per county**: King = Notice of Trustee Sale only; Pierce = NOD + Notice of Foreclosure + Lis Pendens + Trustee Sale; Kitsap = EagleWeb keyword; Snohomish = recorder login-required, **inactive**.
- Pipeline scales horizontally: Celery beat dispatch, per-config jobs, every query filters `user_id`, RLS forced, `purge_old_records` retention.

---

## 3. Milestone Decomposition (5 phases)

Each phase ships independently and is reviewed by Codex before merge. Detailed design for Phases 2–5 is written when that phase begins.

| Phase | Goal | Risk | Depends on |
|---|---|---|---|
| **1. Foundation** | Record dedup basis; maintain incremental property-membership rollup so overlap is a fast indexed lookup. No user-visible change. | Low (touches dedup/billing-adjacent code) | — |
| **2. Doc-type control (#2)** | Carry `doc_type` into `Result`; per-county available types + confidence; user selects; default to earliest recorded. | Low–Med | 1 (uses membership for nothing; standalone, but built on stable Result schema) |
| **3. Combine + overlap (#4)** | Segment model referencing multiple configs/types; union + intersection export joined on strong identity only. | Med (mostly query/product logic) | **1** (rollup) |
| **4. Tax filters (#1)** | King structured columns + amount/age filters first; then treasurer scrapers for Pierce/Snohomish/Kitsap. Move billing claim to post-filter. | **High** (non-King data may not exist cleanly; billing-timing change) | 1 |
| **5. Dialer push (#3)** | Generic delivery-connector abstraction; Enzo API connector; push skip-traced + valid-phone + non-DNC rows. | Med–High (Enzo API reality, compliance) | 1, 4 (clean filtered rows) |

**Riskiest:** Phase 4 non-King tax (source acquisition, not code). **Second:** Phase 5 Enzo integration reality.

---

## 4. Phase 1 — Detailed Design

### 4.1 Goal
Lay the data foundation that makes Phase 3 overlap correct **and** scalable, with **zero user-visible behavior change**.

### 4.2 What ships (revised twice per Codex — sessions 019e91a1, 019e91a9)

**Two insights that shape the design:**
- A Job is always exactly ONE `record_type` (it runs one `ScraperConfig`). So no bitmask/JSON array of types is needed — each job writes rows for its one type into a **normalized** table; "has both" is a `GROUP BY … HAVING count(distinct record_type) ≥ 2`.
- **Overlap only works on a strong, post-enrichment property identity.** Probate records usually have no scrape-time parcel (keyed by `name_date`); pre-foreclosure records key by parcel/address. They only line up on the same property *after* enrichment resolves the probate owner to a parcel/address (PACS/GIS). So membership identity is computed **after** inline enrichment, and **only strong-identity rows are tracked**. Weak `name_date`-only rows are useless for cross-list overlap and are excluded.

1. **`property_list_membership`** — new normalized table. One row per `(user_id, record_type, property_key)`:
   - `user_id` (FK), `record_type` (String), `property_key` (String, sha256)
   - `property_key` = sha256 of normalized `parcel|address` computed **from post-enrichment `results` fields**, using the same normalization as `_compute_dedup_hash`'s strong branch. **Only rows that resolve to a valid parcel or address get a row** — there is no weak/`name_date` membership. (This replaces the earlier `strong_identity` boolean: the table holds strong rows only, so Phase 3 needs no strong filter.)
   - `parcel_id`, `property_address` (denormalized, for drill-down/debug)
   - `sighting_count` (int, **advisory only** — cumulative scrape observations, NOT idempotent across job re-runs, never used for billing/correctness), `first_seen_at`, `last_seen_at`
   - **`PRIMARY KEY (user_id, record_type, property_key)`**
   - Supporting index `(user_id, property_key)` for the overlap grouping.
   - **No** new column on `results` (Codex: not needed; avoids a 240M-row backfill).

2. **Maintenance — placed AFTER inline enrichment (`tasks.py:~603`, reusing the refreshed post-enrichment `results` SELECT already done at ~`608` for re-export), processing EVERY enriched row that resolves to a strong identity**, independent of `delivered_records` claim (Codex: the rows that *conflict* in `delivered_records` are the overlap signal). The billing path (`delivered_records` `ON CONFLICT DO NOTHING … RETURNING`, `is_duplicate`, `billable_count`) is **untouched**.

   `property_key` and strong/weak are classified **per row** (Codex: a job mixes strong and weak rows — not a single per-job flag). Upsert is **pre-aggregated by key first** (Codex: raw multi-row `ON CONFLICT DO UPDATE` throws `cannot affect row a second time` on a repeated key):
   ```sql
   WITH batch AS (
     SELECT user_id, :record_type AS record_type, <property_key_expr> AS property_key,
            MAX(parcel_id) AS parcel_id, MAX(property_address) AS property_address,
            COUNT(*)::int AS sightings,
            MIN(created_at) AS first_seen_at, MAX(created_at) AS last_seen_at
     FROM results
     WHERE job_id = :job_id AND user_id = :user_id
       AND <row resolves to a strong identity>      -- parcel OR address valid
     GROUP BY user_id, <property_key_expr>
   )
   INSERT INTO property_list_membership
     (id, user_id, record_type, property_key, parcel_id, property_address,
      sighting_count, first_seen_at, last_seen_at)
   SELECT gen_random_uuid(), user_id, record_type, property_key, parcel_id, property_address,
          sightings, first_seen_at, last_seen_at
   FROM batch
   ORDER BY user_id, property_key                    -- consistent lock order (deadlock guard)
   ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
     sighting_count = property_list_membership.sighting_count + EXCLUDED.sighting_count,
     first_seen_at  = LEAST(property_list_membership.first_seen_at,  EXCLUDED.first_seen_at),
     last_seen_at   = GREATEST(property_list_membership.last_seen_at, EXCLUDED.last_seen_at);
   ```
   (`property_key_expr` and the strong-identity predicate are generated in Python to mirror `_compute_dedup_hash` exactly, or done in Python then bulk-upserted — impl plan decides. Either way the classification logic is shared with `_compute_dedup_hash`, not duplicated.)
   Retry the statement on serialization/deadlock SQLSTATE `40001`/`40P01`.

3. **Forward write is DURABLE, not best-effort** (Codex D): the membership upsert runs before the job is marked `done`; on failure it retries; a job is not "done" until membership is written. Presence is idempotent via the PK on whole-job Celery retry (`sighting_count` may over-count, which is acceptable since it is advisory). Only the *historical* backfill (item 5) is best-effort.

4. **Schema-only migration (034).** Creates the table + indexes + RLS policy. **No backfill inside the migration** (Codex: `scripts/migrate.py` has a ~900s lock budget; a 240M-row backfill on boot would brick the deploy).

5. **Separate historical backfill script** (`scripts/backfill_property_membership.py`) — idempotent, batched, small commits, progress + retry, run manually *after* deploy, off the boot path. **Best-effort** (Codex): `record_type` was never snapshotted on `results`/`jobs`, so it joins `results → jobs → scraper_configs` using the config's *current* `record_type`; type-changed or deleted configs are approximate or skipped. Forward accrual is the source of truth; backfill is a convenience.

6. **Retention:** add membership cleanup to `purge_old_records` (Codex: it will NOT follow the `results` purge automatically). Prune membership rows whose `last_seen_at` is older than the retention window.

7. **Tests** — per-row strong/weak classification; only-strong rows tracked; same property under two `record_type`s → two rows → "has both" query returns it; a probate row enriched to a parcel overlaps a pre-foreclosure row on the same parcel; pre-aggregated upsert handles a repeated key with no double-affect error; `first_seen`/`last_seen` LEAST/GREATEST on re-run; whole-job retry keeps one row per key; retention prune.

### 4.3 What does NOT change
- No change to results page, exports, billing amounts, quota, or scrape output.
- `delivered_records` write path, `ON CONFLICT DO NOTHING … RETURNING`, `is_duplicate`, and `billable_count` are **untouched**. Membership is additive and isolated from billing.
- Billing-claim timing unchanged (deferred to Phase 4, where filters make it necessary).

### 4.4 Files touched
- `alembic/versions/034_*.py` (new) — create `property_list_membership` + indexes + RLS policy. Schema only.
- `src/db/models.py` — add `PropertyListMembership` model.
- `src/workers/tasks.py` — after inline enrichment, reuse the refreshed post-enrichment `results` SELECT to run the pre-aggregated, strong-only membership upsert (with retry), before marking the job `done`. Billing block unchanged.
- `src/workers/scheduler.py` — extend `purge_old_records` to prune stale membership rows.
- `scripts/backfill_property_membership.py` (new) — offline idempotent best-effort backfill.
- `tests/test_property_list_membership.py` (new).

### 4.5 Migration safety (per project landmines)
- Next sequential number **034** (after 033). **Schema-only** — no data backfill in the migration.
- Run via `scripts/migrate.py` advisory-lock runner, **not** bare `alembic upgrade head` (multi-replica boot race). Reject Supabase `:6543` transaction pooler.
- **Do NOT apply to production until merged to `main`** (branch-only migration on prod = api crash-loop; see incident memory). Keep `RLS_ENFORCE` as-is.
- New table gets an RLS policy consistent with existing per-tenant tables (`user_id` filter + forced RLS). Mirror the `delivered_records` policy (migrations 025/031).

---

## 5. Scalability Analysis

**Worst-case sizing:** 5,000 active users × ~2,000 new leads/month × 24 months ≈ **240M `results` rows.** Postgres handles this with existing indexes; `purge_old_records` bounds it.

**Phase 1 additions:**
- No new column on `results`.
- `property_list_membership`: one row per `(user_id, record_type, property_key)`, strong-identity rows only. Bounded by *unique resolved properties per user per type* — far smaller than `results`. Pruned by `purge_old_records`.

**Hot-path cost:**
- Membership upsert is one extra pre-aggregated `INSERT … ON CONFLICT` over the current job's enriched rows — added to background Celery scrape work, never on a user-facing request. It reuses the post-enrichment `results` SELECT already done for re-export, so no extra scan of history.
- Phase 3 "on both lists" = `SELECT property_key FROM property_list_membership WHERE user_id = :u AND record_type IN (:a,:b) GROUP BY property_key HAVING count(distinct record_type) >= 2`. Tenant-scoped, served by `PRIMARY KEY (user_id, record_type, property_key)` + `(user_id, property_key)`. Cost scales with *one user's* membership rows, not the global table.

**Anti-pattern avoided:** computing intersection live via self-join over a user's full `results` history. At scale that is a per-click scan of 10k–100k+ rows. The rollup converts it to a tenant-local indexed read.

**Cross-tenant isolation:** every query filters `user_id`; new table gets forced RLS. One user's volume never touches another's.

**Concurrency (Codex #9):** two jobs for the same user touching overlapping hashes serialize on row locks; consistent `ORDER BY (user_id, dedup_hash)` + bounded chunks avoid deadlock, with retry on `40001`/`40P01`. Because each job writes its own `record_type` partition of the PK, same-user different-type jobs rarely contend on the same row at all.

---

## 6. UI/UX Impact per Phase

Frontend lives in the **sibling repo `bridgeleads-web`** (Next.js 14). Each UI-bearing phase pairs a backend change with frontend work, reusing shipped conventions: four UI states, `getFriendlyError`/`toastError` (`lib/errors.ts`), route error boundaries, onBlur validation, monochrome icons + green accent `#72e3ad`. UI-heavy phases (3, 4) go through the design/UX review skills before build.

| Phase | UI change | What the user sees |
|---|---|---|
| **1. Foundation** | **None** | Invisible. No screen changes, no behavior change. |
| **2. Doc-type (#2)** | Yes (moderate) | Doc-type selector on pre-foreclosure configs (Notice of Default / Trustee Sale / Lis Pendens); per-county availability + confidence labels; new "Doc Type" column in results + export. |
| **3. Combine + overlap (#4)** | Yes (largest, net-new) | A "Lists/Segments" builder: pick multiple record types, choose **Combine** vs **On both lists**; results view flags overlap ("On 2 lists" badge) + `overlap_count`. New screens — design review first. |
| **4. Tax filters (#1)** | Yes | Amount-owed + months-delinquent filter inputs (min/max) on tax-delinquent configs; new amount/age columns in results. Filter availability gated per county (King first). |
| **5. Dialer (#3)** | Yes | "Push to dialer" delivery option in schedule/delivery settings (connect Enzo, pick auto-push list); push status indicators. |

Phase 1 ships with **no frontend work**. First UI lands in Phase 2.

---

## 7. Open Questions / Deferred

**Resolved (Phase 1):**
- *Overlap identity* = strong, post-enrichment `property_key` (parcel/address). Weak `name_date`-only rows are **excluded** from membership.
- **Known limitation:** a property that never resolves to a parcel/address (even after enrichment) cannot participate in overlap. For probate∩pre-foreclosure to work, the probate side must enrich to a parcel/address (PACS/GIS). Surfacing/improving that enrichment hit-rate is a Phase 3 concern, tracked there.
- `sighting_count` is advisory (not idempotent, not billing).

**Deferred:**
- **Phase 4:** move `delivered_records` claim + billing increment to post-filter/export finalization. Renaming consideration: `Result` (sighting) vs `BillableDelivery` (delivered/billed). Decide at Phase 4.
- **Phase 4:** confirm Pierce/Snohomish/Kitsap treasurer data sources actually expose amount + age (research spike before committing).
- **Phase 5:** Enzo API shape (auth, contact/list upload endpoint, rate limits, DNC handling).
- **Phase 2:** whether `name_date`-basis pre-foreclosure rows (no parcel/address) are acceptable for doc-type lists or need enrichment first.

---

## 8. Verification (Phase 1)
- Unit tests green: per-row strong/weak classification (only strong tracked); pre-aggregated upsert handles a repeated key with no double-affect error; `first_seen` LEAST / `last_seen` GREATEST on re-run; same property under two `record_type`s = two rows; probate-enriched-to-parcel overlaps pre-foreclosure on same parcel; "has both types" intersection query; whole-job retry keeps one row per key; concurrent-upsert retry path; retention prune.
- `pytest` full suite green.
- Manual: run a scrape locally for a config; confirm `property_list_membership` rows created (strong-identity only) with the job's `record_type`; re-run same config → `first_seen` stable, `last_seen` advances, no PK violation; run a *second* config of a different type that hits the same property (incl. a probate config whose owner enriches to a pre-foreclosure parcel) → second row appears and the "has both" query returns it; confirm results page / billing / exports unchanged.
- Codex diff review = PASS (no P1) before merge. Any Critical/High from Claude or Codex = NO-GO.
- **Deploy order:** merge to `main` → schema migration 034 runs via `scripts/migrate.py` → run `scripts/backfill_property_membership.py` manually off the boot path (optional, best-effort). Forward accrual works immediately regardless of backfill.
