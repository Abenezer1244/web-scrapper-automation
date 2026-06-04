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

### 4.2 What ships
1. **`results.dedup_basis`** — new nullable `VARCHAR(16)` column. Values: `parcel` | `address` | `name_date`. Recorded at insert from the same logic that computes `dedup_hash`. Lets Phase 3 trust only `parcel`/`address` matches for intersection and ignore weak `name_date` coincidences.
2. **`property_membership`** — new rollup table, one row per `(user_id, dedup_hash)` (same cardinality order as `delivered_records`):
   - `user_id` (FK, indexed), `dedup_hash`
   - `dedup_basis` (strongest basis seen for this property)
   - `record_types` (JSON array of distinct record_types this property has appeared on, for this user)
   - `job_ids` (JSON array, capped/most-recent N) — for drill-down
   - `sighting_count` (int), `first_seen_at`, `last_seen_at`
   - `UNIQUE(user_id, dedup_hash)`
   - Index: `(user_id)` and a GIN/expression index strategy for record_types membership (decide at impl; may store a small int bitmask `record_type_mask` instead of/alongside JSON for fast "has both" filtering — **Codex to weigh in**).
3. **Incremental maintenance** — inside the existing scrape-time loop that upserts `delivered_records` (`tasks.py:447–519`), upsert `property_membership`: add this job's `record_type` to the array/mask, bump `sighting_count`, update `last_seen_at`, set/keep strongest `dedup_basis`. No new pass over the data; piggybacks the loop already running.
4. **Backfill migration (one-time, batched)** — populate `dedup_basis` on existing `results`; build `property_membership` from existing `results` + `scraper_configs.record_type`. Runs in batches off the request path.
5. **Tests** — basis classification, membership upsert idempotency, "has both types" query correctness, backfill correctness.

### 4.3 What does NOT change
- No change to results page, exports, billing amounts, quota, or scrape output.
- `is_duplicate` semantics unchanged for now (still "not newly billable"); Phase 3 will *read around* it via the membership table rather than redefining it.
- Billing-claim timing unchanged (deferred to Phase 4, where filters make it necessary).

### 4.4 Files touched (≤5)
- `alembic/versions/034_*.py` (new) — add column + table + indexes + batched backfill.
- `src/db/models.py` — mirror the new column + `PropertyMembership` model.
- `src/workers/tasks.py` — record `dedup_basis` at insert; upsert `property_membership` in the dedup loop.
- `tests/test_property_membership.py` (new).
- (Possibly) `src/scrapers/base_scraper.py` only if basis needs surfacing on `ScrapedRecord` — likely not; basis is derivable server-side.

### 4.5 Migration safety (per project landmines)
- Next sequential number **034** (after 033).
- Run via `scripts/migrate.py` advisory-lock runner, **not** bare `alembic upgrade head` (multi-replica boot race). Reject Supabase `:6543` transaction pooler.
- **Do NOT apply to production until merged to `main`** (branch-only migration on prod = api crash-loop; see incident memory). Keep `RLS_ENFORCE` as-is.
- New table needs an RLS policy consistent with existing per-tenant tables (`user_id` filter + forced RLS). Mirror `delivered_records` policy.

---

## 5. Scalability Analysis

**Worst-case sizing:** 5,000 active users × ~2,000 new leads/month × 24 months ≈ **240M `results` rows.** Postgres handles this with existing indexes; `purge_old_records` bounds it.

**Phase 1 additions:**
- `results.dedup_basis`: ~10 bytes/row. Negligible.
- `property_membership`: one row per unique property per user — strictly smaller than `delivered_records`, far smaller than `results`. Same retention treatment.

**Hot-path cost:**
- Membership upsert runs inside the loop that already upserts `delivered_records` → **no new scan**, amortized into existing scrape work (background Celery, not user-facing).
- Phase 3 "on both lists" = indexed lookup on `property_membership` filtered by `user_id` + record-type membership (mask or GIN). **Constant-time regardless of user history size.** This is the entire reason the rollup exists.

**Anti-pattern avoided:** computing intersection live via self-join over a user's full `results` history. At scale that is a per-click scan of 10k–100k+ rows. The rollup converts it to an indexed read.

**Cross-tenant isolation:** every new query filters `user_id`; new table gets forced RLS. One user's volume never touches another's.

**Codex to review for scale specifically:** mask-vs-JSON for record-type membership, index choice, backfill batch size, lock behavior of the membership upsert under concurrent jobs for the same user.

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
- Unit tests green (basis classification, membership idempotency, has-both query, backfill).
- `pytest` full suite green.
- Manual: run a scrape locally for a config; confirm `property_membership` rows created with correct `record_types`; re-run same config → `sighting_count` increments, no duplicate membership rows, no user-visible change.
- Codex diff review = PASS (no P1) before merge. Any Critical/High from Claude or Codex = NO-GO.
- Migration applied only after merge to `main`, via `scripts/migrate.py`.
