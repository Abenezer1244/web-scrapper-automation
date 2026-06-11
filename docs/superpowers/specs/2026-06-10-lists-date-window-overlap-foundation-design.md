# Lists Date-Window + Overlap/Combine Foundation — Design

**Date:** 2026-06-10
**Status:** Spec — approved for planning (Piece 1 of 2; see companion `2026-06-10-batch-scrape-design.md`)
**Author:** Claude (brainstormed with user) + Codex consults (sessions 019eb307, 019eb327, 019eb33b, 019eb357)
**Reviewers:** Codex consult complete (×4); Codex diff review required before merge.

---

## 1. Problem / Motivation

The user wants to find **homes that appear on more than one record-type list** (e.g. a property on both **probate** and **pre-foreclosure** — the highest-motivation sellers), and to **combine** lists into one, scoped to a **time frame** (since last run / 3 / 6 / 12 months / custom). Verbatim intent across the brainstorm:

> "select multiple different kinds of lists like pre-foreclosure's and probates into one list grouped together" … "a list of properties that are in both probate and pre-foreclosure" … "newly scrapped and also already scrapped based on the date frame … our system will see if there is an overlap on the scrapped data and also combine based on the given date frame."

The overlap/combine engine **already exists and is live** on the `/segments` ("Lists") page — but it scans **all history with no date filter**. This spec adds the **date-frame dial** (plus a county filter) and the **shared, correct, indexed filing-date foundation** that the batch-scrape feature (Piece 2) will reuse.

### Why this is "Piece 1" (build first)
1. It is small (one generated DB column + query/endpoint/UI additions), standalone, and low-risk.
2. It builds the **shared engine** — a parsed filing-date column + a results-based, date-scoped overlap/combine query — that **Batch scrape (Piece 2) reuses** for its combined CSV. Foundation first, not just "small first."

### User decisions (locked 2026-06-10)
- **Scope dial = the county *filing* date**, not when we scraped it. ("based on the date frame.")
- **Decoupled model:** scraping and overlap-lookback are *separate* dials. The Lists page overlaps **data already in the account** (free, no re-scrape). (User: "i lean to two settings.")
- **Keep the Lists page** — it is the free historical-intelligence layer; it is NOT redundant with batch scrape (which spends quota to acquire new data). (Codex + Claude agree, session 019eb357.)
- **Lists owns the lookback UX**; batch may later link into it.

---

## 2. Current-State Facts (verified by reading code, 2026-06-10)

- **`/segments` is live.** `src/api/routes/segments.py`: `POST /segments/intersection` (+`/export`) and `POST /segments/union` (+`/export`). Both accept `record_types` + optional `counties`; **neither has a date filter.**
- **Intersection** is computed via the `property_list_membership` rollup (`src/api/routes/segments.py:69`). **Union** is computed directly from `results` (`segments.py:137`), deduped by `COALESCE(property_key, dedup_hash, 'id:'||id)`.
- **`property_list_membership`** (`src/db/models.py:379`) PK `(user_id, record_type, property_key)`, has `first_seen_at`/`last_seen_at` = **OUR scrape timestamps**, NOT the county filing date. → **It cannot serve a filing-date window.**
- **`Result.date_recorded`** (`src/db/models.py:258`) = `String(32)`, the county filing date as free-form text.
- **`Result.property_key`** (`models.py:292`) = post-enrichment `sha256(parcel|address)`, nullable (NULL for weak-identity rows). Strong-identity overlap key.
- **`Result.created_at`** = timestamptz, when WE scraped (the wrong date for the business window).
- **Skip-trace (phone/email) is async** — lands after the job via a sweep; `property_key` is written inline at enrichment, before job done (`src/workers/tasks.py:866`). → Overlap *detection* needs only `property_key` (ready at enrichment); *contact info* fills later.
- **Frontend Lists page:** `bridgeleads-web/app/(dashboard)/segments/page.tsx` — Method toggle (On both lists / Combine) + record-type pills + Build/Export. **No county or date control today.**

### Spike result (read-only, full prod table, 2026-06-10) — the key de-risking finding
`scripts/spike_date_recorded_coverage.py` over **293,451 leads**:
- `date_recorded` null/empty: **116 (0.0%)**.
- Parse coverage on 60,000 newest values: **100.0% parseable, 0 unparseable**.
- Format diversity: **one family** — US `M/D/YYYY`, slash-separated, sometimes single-digit month/day (`9/9/2024`). No alpha-month, no ISO, no garbage.
- Per record_type parse rate (5k samples each): probate 100%, code_violation 100%, tax_delinquent 100%, pre_foreclosure 100% (2.1% null), death_certificate 100%, divorce 100%.

**Conclusion:** the [P1] "messy date text" risk is **retired**. The field is clean enough to parse in a guarded DB expression — no per-scraper parsing layer, no app backfill script needed.

---

## 3. Design

### 3.1 Filing-date column (one migration, generated + stored)
Add to `results`:

```sql
ALTER TABLE results ADD COLUMN date_recorded_parsed DATE
  GENERATED ALWAYS AS (
    CASE
      WHEN date_recorded ~ '^\s*\d{1,2}/\d{1,2}/\d{4}\s*$'
      THEN to_date(trim(date_recorded), 'FMMM/FMDD/YYYY')
      ELSE NULL
    END
  ) STORED;
```

- **Generated/stored** (Codex [P1]): Postgres materializes it from existing text automatically → existing 293k rows AND all future scraped rows are handled with **zero app code and zero backfill script**.
- **Regex guard** rejects anything that is not `M/D/YYYY` → unparseable/future-malformed rows become `NULL` (auto-excluded), so the clean-prod finding stays true even if a scraper later emits a weird value.
- `FMMM/FMDD/YYYY` handles single- and double-digit month/day. **The migration must include a verification step** asserting `to_date` parses `9/9/2024`, `9/99/2024`, `99/9/2024`, `99/99/2024` correctly (Codex [P1] — verify before relying on it).
- The generated expression must be IMMUTABLE: `to_date` + regex are immutable. No `now()`/locale dependence.

Index for the windowed query:
```sql
CREATE INDEX ix_results_user_type_filingdate
  ON results (user_id, date_recorded_parsed)
  WHERE property_key IS NOT NULL;
```
(County/record_type live on `scraper_configs` via the `jobs` join, so the index is keyed on the `results`-local dimensions; the join filters the rest. Revisit a composite if EXPLAIN shows it's needed at scale.)

Migration number: **next free** (last applied was 041; CI-branch reserved 048/049 per `incident_migration_branch_mismatch` — confirm the next free number at implementation time and renumber if rebasing).

### 3.2 Date-windowed overlap/combine query (computed from `results`, not membership)
**[P1] Compute from `results` directly** — the membership rollup has scrape timestamps, not filing dates, and would silently return the wrong answer (Codex). Extend the existing `_INTERSECTION_SQL` / `_UNION_SQL` in `segments.py` with an **optional** filing-date predicate:

```
AND (:filing_from IS NULL OR r.date_recorded_parsed >= :filing_from)
AND (:filing_to   IS NULL OR r.date_recorded_parsed <= :filing_to)
```

Critically, the **intersection** path must move OFF the membership rollup **when a date window is supplied** (membership can't honor it). Approach:
- **No date window** → keep today's membership-backed intersection (fast, unchanged).
- **Date window present** → compute intersection from `results` (group by `property_key`, `HAVING count(DISTINCT record_type) = :n`, with the filing-date predicate). Union already reads from `results`, so it just gains the predicate.

This yields **two intersection code paths** short-term (membership all-history vs results-based dated). Codex [P2]: acceptable now; converge later to one canonical results-based query with membership as a pure cache. Documented as tech debt, not built now.

### 3.3 Null-date handling
Rows with `date_recorded_parsed IS NULL` are **excluded** from any *windowed* query (a NULL can't be placed in time) and the response reports an **`excluded_no_date_count`** so the user knows N leads were skipped (Codex [P2] — never silently drop). With **no** window selected (or "All time"), nulls are included exactly as today.

### 3.4 API changes (`src/api/routes/segments.py` + `schemas.py`)
Add to `SegmentIntersectionRequest` / `SegmentUnionRequest` (all optional, back-compatible):
- `filing_from: date | None`, `filing_to: date | None` — explicit window; OR
- `lookback_days: int | None` (bounded, e.g. `le=3660` ≈ 10y) — server computes `filing_from = today - lookback_days`. Presets map to this.
- `counties: list[str] | None` already supported by the query; expose it in the request model if not already.

Add to responses: `excluded_no_date_count: int`. Keep existing rate-limit, `sanitize_for_csv`, tenant scoping, caps (`PREVIEW_CAP`, `EXPORT_CAP`).

**CSV unification (do here):** the current `/segments` CSV export hand-rolls a thin column set (`segments.py:309`, `:435`) and **bypasses the canonical `src/utils/lead_export.py` builder** that every other export uses. Route the segment exports through the canonical builder + the overlap columns instead, in the same **caller-first column order**, **hottest-first sort**, and **`overlap` flag ("Overlap" or blank) + `lists_count`/`lists`/`counties`** columns defined in the batch spec (`2026-06-10-batch-scrape-design.md` §4.4). This (a) gives the organized, dialer-ready format the user asked for, (b) keeps the Lists export and the batch combined CSV **byte-consistent** (one builder), (c) fixes the existing divergence. Extend `lead_export.py` with the optional overlap-columns variant once; both surfaces consume it.

**Boundary rule (Codex [P2]):** windows are **inclusive** on both ends, interpreted in the user's date terms (pure `DATE`, no timezone — Codex confirmed no tz issue).

### 3.5 UI changes (`bridgeleads-web/.../segments/page.tsx`)
Add to the builder, matching existing pill/toggle styling (`TOGGLE_ON`/`TOGGLE_OFF`):
- **Counties** row — pills incl. "All" (maps to `counties` param).
- **Look back** row — preset buttons + custom date picker. **Presets include an "All time"** option (= no window; preserves today's behavior so nobody loses the all-history view). Final preset set is a UI detail to confirm in the plan; default set: All time / Last week / 3 / 6 / 12 months / Custom.
- **Result:** add a **Filed** (date) column; show `excluded_no_date_count` as "· N skipped (no filing date)" next to the count.
- Changing counties/lookback invalidates the built result (same `resetResult()` pattern already there).

### 3.6 Skip-trace / contacts
Unchanged. Phone/email show if skip-trace has run, fill in later otherwise — exactly like today. The list is "ready" on property identity, not contacts.

---

## 4. Testing
- Migration verification: `to_date` parses all four digit-width variants; regex rejects `''`, `'N/A'`, `'2024-01-15'` (ISO not in scope), `'Jan 5 2024'` → NULL.
- Query: dated intersection returns only properties with ≥2 record_types whose filing date is in-window; null-date rows excluded + counted; no-window path equals today's result.
- Tenant isolation: window query stays `user_id`-scoped (belt + RLS suspenders).
- Boundary: inclusive endpoints; `lookback_days` math.
- No-mock, real-DB per `.claude/rules/testing.md`; pure-logic units where DB not required.

## 5. Risks
- **[P1 — retired]** Date-text messiness → spike proved 100% clean; generated column + regex guard keep it safe forward.
- **[P1]** `to_date` digit-width handling → verified in migration before reliance.
- **[P2]** Two intersection code paths → documented tech debt; converge later.
- **[P2]** Generated-column add on a 293k-row table = one-time table rewrite/lock on deploy → run via `scripts/migrate.py` advisory lock (`project_migration_advisory_lock`); acceptable at this size; confirm lock window. Merge to main before applying to prod (`incident_migration_branch_mismatch`).

## 6. Out of scope (this spec)
- Batch scrape (Piece 2 — separate spec).
- Converging the two intersection paths into one canonical query.
- Any change to billing, dedup, or the scrape pipeline.
- Saved segments / scheduled segment delivery.
