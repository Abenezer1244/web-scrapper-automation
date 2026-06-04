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

### 4.2 What ships (revised per Codex review — session 019e91a1)

**Key insight that simplifies everything:** a Job is always exactly ONE `record_type` (it runs one `ScraperConfig`). So we never need a bitmask or JSON array of types on a single row — each job contributes rows for its one type. A **normalized** membership table keyed `(user_id, record_type, dedup_hash)` is the clean, fast, race-light shape.

1. **`property_list_membership`** — new normalized table. One row per `(user_id, record_type, dedup_hash)`:
   - `user_id` (FK, indexed), `record_type` (String), `dedup_hash` (String)
   - `strong_identity` (bool) — `true` when the hash came from parcel/address, `false` for the `name_date` fallback. Phase 3 intersection trusts only `strong_identity = true`.
   - `sighting_count` (int), `first_seen_at`, `last_seen_at`
   - **`PRIMARY KEY (user_id, record_type, dedup_hash)`** (Codex: best shape for "has both types" intersection)
   - Supporting index `(user_id, dedup_hash)` for the overlap grouping.
   - **No** `dedup_basis` column on `results` (Codex #6: not needed for overlap, misleading values, and not worth backfilling 240M rows). The strong/weak distinction lives only here as `strong_identity`.

2. **Incremental maintenance — placed AFTER the fresh-row SELECT (`tasks.py:~426`), processing EVERY fresh row with a `dedup_hash`, independent of `delivered_records` claim** (Codex #2: the rows that *conflict* in `delivered_records` are the overlap signal; gating on `claimed_hashes` would drop exactly what we want). The billing path (`delivered_records` `ON CONFLICT DO NOTHING … RETURNING`, `is_duplicate`, quota) is **left completely untouched** — membership is a separate, additive write so the revenue-critical code is not entangled.

   Upsert SQL is **pre-aggregated by key first** (Codex #3 — a raw multi-row `ON CONFLICT DO UPDATE` errors with `cannot affect row a second time` when one job sees a property twice):
   ```sql
   WITH batch AS (
     SELECT user_id, dedup_hash,
            COUNT(*)::int AS sightings,
            MIN(created_at) AS first_seen_at,
            MAX(created_at) AS last_seen_at
     FROM results
     WHERE job_id = :job_id AND user_id = :user_id AND dedup_hash IS NOT NULL
     GROUP BY user_id, dedup_hash
   )
   INSERT INTO property_list_membership
     (id, user_id, record_type, dedup_hash, strong_identity,
      sighting_count, first_seen_at, last_seen_at)
   SELECT gen_random_uuid(), user_id, :record_type, dedup_hash, :strong_identity,
          sightings, first_seen_at, last_seen_at
   FROM batch
   ORDER BY user_id, dedup_hash          -- consistent lock order (Codex #9)
   ON CONFLICT (user_id, record_type, dedup_hash) DO UPDATE SET
     sighting_count = property_list_membership.sighting_count + EXCLUDED.sighting_count,
     last_seen_at   = GREATEST(property_list_membership.last_seen_at, EXCLUDED.last_seen_at);
   ```
   `record_type` and `strong_identity` are known at scrape time from the loaded `config` + the hash basis — no join needed for live maintenance. Retry the statement on serialization/deadlock SQLSTATE `40001`/`40P01`.

3. **Schema-only migration (034).** Creates the table + indexes + RLS policy. **No backfill inside the migration** (Codex #8: `scripts/migrate.py` has a ~900s advisory-lock budget; a 240M-row backfill on API boot would brick the deploy).

4. **Separate, optional backfill script** (`scripts/backfill_property_membership.py`) — idempotent, batched, small commits, progress + retry, run manually *after* deploy, off the boot path. **Best-effort by design** (Codex #1): `record_type` was never snapshotted on `results`/`jobs`, so it joins `results → jobs → scraper_configs` using the config's *current* `record_type`; properties whose config changed type, or whose config was deleted, are approximate or skipped. Documented as a known limitation. Forward-only accrual (from launch onward) is the source of truth; backfill is a convenience.

5. **Tests** — `strong_identity` classification; pre-aggregated upsert idempotency (same job re-run bumps count, no PK violation, no double-affect error); same property under two different `record_type`s yields two rows; "has both types" intersection query correctness; concurrent-upsert retry path.

### 4.3 What does NOT change
- No change to results page, exports, billing amounts, quota, or scrape output.
- `delivered_records` write path, `ON CONFLICT DO NOTHING … RETURNING`, `is_duplicate`, and `billable_count` are **untouched**. Membership is additive and isolated from billing.
- Billing-claim timing unchanged (deferred to Phase 4, where filters make it necessary).

### 4.4 Files touched (≤5)
- `alembic/versions/034_*.py` (new) — create `property_list_membership` + indexes + RLS policy. Schema only.
- `src/db/models.py` — add `PropertyListMembership` model.
- `src/workers/tasks.py` — after the fresh-row SELECT, run the pre-aggregated membership upsert for all fresh rows (with retry). Billing block unchanged.
- `scripts/backfill_property_membership.py` (new) — offline idempotent backfill.
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
- No new column on `results` (basis lives on the rollup as `strong_identity`).
- `property_list_membership`: one row per `(user_id, record_type, dedup_hash)`. Bounded by *unique properties per user per type* — far smaller than `results`. Same retention treatment.

**Hot-path cost:**
- Membership upsert is one extra pre-aggregated `INSERT … ON CONFLICT` over the current job's fresh rows — added to background Celery scrape work, never on a user-facing request. It reuses the fresh-row set already SELECTed for dedup, so no extra scan of history.
- Phase 3 "on both lists" = `SELECT dedup_hash FROM property_list_membership WHERE user_id = :u AND record_type IN (:a,:b) AND strong_identity GROUP BY dedup_hash HAVING count(*) >= 2`. Tenant-scoped, served by `PRIMARY KEY (user_id, record_type, dedup_hash)` + `(user_id, dedup_hash)`. Cost scales with *one user's* membership rows, not the global table.

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
- **Phase 4:** move `delivered_records` claim + billing increment to post-filter/export finalization. Renaming consideration: `Result` (sighting) vs `BillableDelivery` (delivered/billed). Decide at Phase 4.
- **Phase 4:** confirm Pierce/Snohomish/Kitsap treasurer data sources actually expose amount + age (research spike before committing).
- **Phase 5:** Enzo API shape (auth, contact/list upload endpoint, rate limits, DNC handling).
- **Phase 2:** whether `name_date`-basis pre-foreclosure rows (no parcel/address) are acceptable for doc-type lists or need enrichment first.

---

## 8. Verification (Phase 1)
- Unit tests green: `strong_identity` classification; pre-aggregated upsert idempotency (re-run bumps count, no double-affect error); same property under two `record_type`s = two rows; "has both types" intersection query; concurrent-upsert retry path.
- `pytest` full suite green.
- Manual: run a scrape locally for a config; confirm `property_list_membership` rows created with the job's `record_type`; re-run same config → `sighting_count` increments, no PK violation; run a *second* config of a different type that hits the same property → second row appears and the "has both" query returns it; confirm results page / billing / exports unchanged.
- Codex diff review = PASS (no P1) before merge. Any Critical/High from Claude or Codex = NO-GO.
- **Deploy order:** merge to `main` → schema migration 034 runs via `scripts/migrate.py` → run `scripts/backfill_property_membership.py` manually off the boot path (optional, best-effort). Forward accrual works immediately regardless of backfill.
