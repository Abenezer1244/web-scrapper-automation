# Lists Date-Window + Overlap Foundation — Implementation Plan (Piece 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a parsed county-filing-date column and a date-windowed, results-based overlap/combine query, and surface a date-lookback + county filter (and a unified, dialer-ready CSV) on the `/segments` Lists page.

**Architecture:** One generated/stored `date_recorded_parsed DATE` column on `results` (regex-guarded `to_date`, auto-fills existing + future rows, no backfill). The `/segments` intersection/union queries gain an optional filing-date window; with a window present the intersection computes from `results` directly (the membership rollup has no filing date). Segment CSV exports route through the canonical `lead_export.py` builder, extended with overlap columns.

**Tech Stack:** FastAPI, SQLAlchemy (async), Alembic, Postgres, pytest; Next.js 14 frontend (`bridgeleads-web`).

**Spec:** `docs/superpowers/specs/2026-06-10-lists-date-window-overlap-foundation-design.md`

**Phasing for Codex gates (per `.claude/rules/codex-collaboration.md`):** Phase A = Tasks 1–2 (DB + schemas). Phase B = Tasks 3–4 (queries). Phase C = Task 5 (CSV). Phase D = Task 6 (frontend). Codex review + `tsc`/`ruff`/`pytest` at each phase boundary; STOP for user approval between phases (`CLAUDE.md` phased execution).

---

## File Structure

- `alembic/versions/049_add_result_date_recorded_parsed.py` — new migration (Task 1)
- `src/api/schemas.py` — request/response fields (Task 2)
- `src/api/routes/segments.py` — date window in queries + null count + CSV via lead_export (Tasks 3,4,5)
- `src/utils/lead_export.py` — overlap-columns export variant (Task 5)
- `tests/test_date_recorded_parsed.py` — migration/parse coverage (Task 1)
- `tests/test_segments_date_window.py` — windowed query behavior (Tasks 3,4)
- `tests/test_lead_export_overlap.py` — combined CSV format (Task 5)
- `bridgeleads-web/app/(dashboard)/segments/page.tsx`, `lib/api.ts`, `lib/types.ts` — UI (Task 6)

---

## Task 1: Migration — generated `date_recorded_parsed` column + index

**Files:**
- Create: `alembic/versions/049_add_result_date_recorded_parsed.py`
- Create: `tests/test_date_recorded_parsed.py`

- [ ] **Step 1: Confirm the current head revision**

Run: `python -m alembic heads`
Expected: a single head = the `048_county_connector_max_date_range_days` revision id. Use that id as `down_revision`. If multiple heads, STOP and resolve before continuing.

- [ ] **Step 2: Write the migration**

```python
"""add results.date_recorded_parsed generated column

Revision ID: 049_add_result_date_recorded_parsed
Down revision: <REPLACE with 048 head revision id from Step 1>
"""
from alembic import op

revision = "049_add_result_date_recorded_parsed"
down_revision = "<REPLACE with 048 head revision id>"
branch_labels = None
depends_on = None

# Single-family US M/D/YYYY (single- or double-digit M/D). Regex guard => any
# non-matching/garbage/empty value yields NULL (excluded from windows), keeping
# the clean-prod finding true for future rows too. to_date is IMMUTABLE.
_EXPR = (
    "CASE WHEN date_recorded ~ '^\\s*\\d{1,2}/\\d{1,2}/\\d{4}\\s*$' "
    "THEN to_date(trim(date_recorded), 'FMMM/FMDD/YYYY') ELSE NULL END"
)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE results ADD COLUMN date_recorded_parsed DATE "
        f"GENERATED ALWAYS AS ({_EXPR}) STORED"
    )
    op.execute(
        "CREATE INDEX ix_results_user_filingdate ON results "
        "(user_id, date_recorded_parsed) WHERE property_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_results_user_filingdate")
    op.execute("ALTER TABLE results DROP COLUMN IF EXISTS date_recorded_parsed")
```

- [ ] **Step 3: Write the parse-coverage test (asserts the SQL expression is correct)**

```python
"""Verify the date_recorded_parsed generated-column expression parses the real
formats and rejects garbage. Runs against the configured DB (real, no mocks)."""
import datetime

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session

# (raw date_recorded, expected parsed date or None)
CASES = [
    ("9/9/2024", datetime.date(2024, 9, 9)),
    ("9/19/2024", datetime.date(2024, 9, 19)),
    ("12/9/2024", datetime.date(2024, 12, 9)),
    ("12/19/2024", datetime.date(2024, 12, 19)),
    ("03/14/2026", datetime.date(2026, 3, 14)),
    (" 1/2/2025 ", datetime.date(2025, 1, 2)),
    ("", None),
    ("N/A", None),
    ("2024-01-15", None),       # ISO not in the single-family scope -> NULL
    ("Jan 5, 2024", None),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_filing_date_expression(raw, expected):
    expr = (
        "CASE WHEN :v ~ '^\\s*\\d{1,2}/\\d{1,2}/\\d{4}\\s*$' "
        "THEN to_date(trim(:v), 'FMMM/FMDD/YYYY') ELSE NULL END"
    )
    with system_sync_session() as db:
        got = db.execute(text(f"SELECT {expr}"), {"v": raw}).scalar_one()
    assert got == expected
```

- [ ] **Step 4: Run the test (expect FAIL before migration applied if column-dependent, PASS for the expression test)**

Run: `pytest tests/test_date_recorded_parsed.py -v`
Expected: PASS (this test checks the SQL expression directly, independent of the column). If it FAILS on a width variant, fix `_EXPR` and the migration together before proceeding — this is the [P1] `to_date` verification.

- [ ] **Step 5: Apply migration on a test/dev DB**

Run: `python scripts/migrate.py`  (advisory-lock runner; NOT bare `alembic upgrade head` — see `project_migration_advisory_lock`)
Expected: 049 applies; `\d results` shows `date_recorded_parsed` + `ix_results_user_filingdate`.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/049_add_result_date_recorded_parsed.py tests/test_date_recorded_parsed.py
git commit -m "feat(results): add generated date_recorded_parsed filing-date column (migration 049)"
```

---

## Task 2: Request/response schema fields

**Files:**
- Modify: `src/api/schemas.py` (the `SegmentIntersectionRequest`, `SegmentUnionRequest`, `SegmentIntersectionResponse`, `SegmentUnionResponse` classes)

- [ ] **Step 1: Read the current segment schemas**

Run: read `src/api/schemas.py` around the `Segment*` classes to get exact current fields.

- [ ] **Step 2: Add optional window + county fields to both request models**

Add to `SegmentIntersectionRequest` and `SegmentUnionRequest` (keep existing fields):

```python
    # Filing-date window (optional, back-compatible). lookback_days is the preset
    # path (server computes filing_from = today - days); filing_from/to is the
    # custom path. If both given, explicit filing_from/to wins.
    lookback_days: int | None = Field(default=None, ge=1, le=3660)  # <=~10y
    filing_from: date | None = None
    filing_to: date | None = None
    counties: list[str] | None = Field(default=None, max_length=64)
```

Ensure `from datetime import date` and `Field` are imported.

- [ ] **Step 3: Add the excluded-count field to both response models**

```python
    excluded_no_date_count: int = 0  # rows skipped because filing date unparseable/NULL (windowed only)
```

- [ ] **Step 4: Run schema import sanity**

Run: `python -c "import src.api.schemas"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas.py
git commit -m "feat(segments): add filing-date window + counties request fields and excluded_no_date_count"
```

---

## Task 3: Date-windowed UNION query + null exclusion

**Files:**
- Modify: `src/api/routes/segments.py` (`_UNION_SQL`, `_fetch_union`, `union_preview`, `union_export`)
- Create: `tests/test_segments_date_window.py`

- [ ] **Step 1: Write failing test for windowed union**

```python
"""Date-windowed union: only rows whose filing date is in-window; NULL filing
dates excluded and counted. Real DB; seeds a user + jobs + results."""
# (Use the existing test fixtures/patterns in tests/test_segments_union.py for
# seeding a user, scraper_config, job, and results with date_recorded values.)
# Assert:
#  - union with lookback_days=90 returns only results with date_recorded within 90d
#  - a result with date_recorded='' (NULL parsed) is excluded
#  - response.excluded_no_date_count counts those excluded-in-scope rows
```

Write a concrete test mirroring `tests/test_segments_union.py` seeding, with at least: one in-window row, one out-of-window row, one null-date row; assert counts.

- [ ] **Step 2: Run test, expect FAIL**

Run: `pytest tests/test_segments_date_window.py -v`
Expected: FAIL (window not implemented).

- [ ] **Step 3: Add the optional filing-date predicate to `_UNION_SQL`**

In the `candidates` CTE WHERE clause add:
```sql
      AND (:filing_from IS NULL OR r.date_recorded_parsed >= :filing_from)
      AND (:filing_to   IS NULL OR r.date_recorded_parsed <= :filing_to)
      AND (:require_date = FALSE OR r.date_recorded_parsed IS NOT NULL)
```
(`:require_date` = TRUE when any window is supplied, so null-date rows drop only when a window is active.)

- [ ] **Step 4: Compute the window + excluded count in `_fetch_union`**

```python
# resolve window: explicit filing_from/to wins; else lookback_days from today.
# today must be passed in (no Date.now in pure code paths is N/A here — this is a
# request handler; use datetime.now(UTC).date()).
# require_date = bool(filing_from or filing_to or lookback_days)
# bind :filing_from, :filing_to, :require_date.
# After fetching rows, if require_date: run ONE count query of in-scope rows with
# date_recorded_parsed IS NULL (same candidates filters minus the date predicate)
# -> excluded_no_date_count.
```
Implement the resolution + the second count query; thread `excluded_no_date_count` back through `union_preview`/`union_export` into the response.

- [ ] **Step 5: Run test, expect PASS**

Run: `pytest tests/test_segments_date_window.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/segments.py tests/test_segments_date_window.py
git commit -m "feat(segments): date-windowed union with null-date exclusion + count"
```

---

## Task 4: Date-windowed INTERSECTION (results-based path)

**Files:**
- Modify: `src/api/routes/segments.py` (`_fetch_intersection`; add a results-based dated SQL)
- Modify: `tests/test_segments_date_window.py` (add intersection cases)

- [ ] **Step 1: Add failing intersection-window test**

Seed a property on 2 record_types both in-window (should appear), and the same property where one type is out-of-window (should NOT appear with a tight window). Assert.

- [ ] **Step 2: Run, expect FAIL**

Run: `pytest tests/test_segments_date_window.py -k intersection -v`
Expected: FAIL.

- [ ] **Step 3: Add a results-based dated intersection SQL**

Add `_INTERSECTION_DATED_SQL` that mirrors `_INTERSECTION_SQL` but the `candidates` come from `results` with the filing-date predicate, and overlap is `HAVING count(DISTINCT record_type) = :n` over the dated candidates (NOT the membership subquery). Keep `property_key IS NOT NULL`, tenant scoping, ranking.

- [ ] **Step 4: Branch in `_fetch_intersection`**

```python
# if a window is supplied -> use _INTERSECTION_DATED_SQL (results-based);
# else -> existing membership-backed _INTERSECTION_SQL (unchanged, fast path).
# Compute excluded_no_date_count the same way as union when windowed.
```

- [ ] **Step 5: Run, expect PASS**

Run: `pytest tests/test_segments_date_window.py -v`
Expected: PASS (union + intersection).

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/segments.py tests/test_segments_date_window.py
git commit -m "feat(segments): results-based date-windowed intersection path"
```

---

## Task 5: CSV unification — overlap columns via `lead_export.py`

**Files:**
- Modify: `src/utils/lead_export.py` (overlap-columns variant)
- Modify: `src/api/routes/segments.py` (`intersection_export`, `union_export` to use it)
- Create: `tests/test_lead_export_overlap.py`

- [ ] **Step 1: Write failing test for the overlap CSV builder**

```python
from io import StringIO
from src.utils.lead_export import write_lead_csv_with_overlap, OVERLAP_LEAD_COLUMNS

def test_overlap_csv_columns_and_flag():
    rec = {
        "party_name": "DOE, JOHN", "property_address": "123 MAIN ST, KENT WA 98031",
        "phone": "(206) 555-1234", "date_recorded": "03/14/2026",
    }
    overlap = {"lists_count": 2, "lists": "Probate; Pre-Foreclosure", "counties": "King"}
    buf = StringIO()
    write_lead_csv_with_overlap([(rec, overlap)], buf)
    out = buf.getvalue().splitlines()
    assert out[0].split(",")[:4] == ["overlap", "lists_count", "lists", "counties"]
    row = out[1]
    assert row.startswith("Overlap,2,")          # lists_count>=2 -> "Overlap"
    assert "JOHN" in row and "DOE" in row and "2065551234" in row

def test_overlap_blank_for_single_list():
    rec = {"party_name": "LEE, ROBERT", "property_address": "9 PINE ST, EVERETT WA 98201"}
    overlap = {"lists_count": 1, "lists": "Probate", "counties": "Snohomish"}
    buf = StringIO()
    write_lead_csv_with_overlap([(rec, overlap)], buf)
    assert buf.getvalue().splitlines()[1].startswith(",1,")  # blank overlap flag
```

- [ ] **Step 2: Run, expect FAIL**

Run: `pytest tests/test_lead_export_overlap.py -v`
Expected: FAIL (symbols not defined).

- [ ] **Step 3: Implement the overlap variant in `lead_export.py`**

```python
# Caller-first order: overlap signal, then the existing dialer-ready columns.
OVERLAP_LEAD_COLUMNS: list[str] = [
    "overlap", "lists_count", "lists", "counties",
    "first_name", "last_name",
    "phone", "phone_type", "email", "phone_2", "phone_3", "email_2", "email_3",
    "property_street", "property_city", "property_state", "property_zip",
    "filed_date", "doc_type", "delinquent_amount", "delinquent_bill_year",
    "party_name", "mailing_address", "parcel_id", "heirs", "legal_description",
    "property_address",
]

def build_overlap_export_row(record: Any, overlap: dict) -> dict[str, str]:
    base = build_lead_export_row(record)  # reuse the canonical dialer-ready fields
    count = int(overlap.get("lists_count") or 0)
    return {
        "overlap": "Overlap" if count >= 2 else "",
        "lists_count": str(count) if count else "",
        "lists": sanitize_for_csv(overlap.get("lists")),
        "counties": sanitize_for_csv(overlap.get("counties")),
        "first_name": base["first_name"], "last_name": base["last_name"],
        "phone": base["phone"], "phone_type": base["phone_type"], "email": base["email"],
        "phone_2": base["phone_2"], "phone_3": base["phone_3"],
        "email_2": base["email_2"], "email_3": base["email_3"],
        "property_street": base["property_street"], "property_city": base["property_city"],
        "property_state": base["property_state"], "property_zip": base["property_zip"],
        "filed_date": base["date_recorded"], "doc_type": base["doc_type"],
        "delinquent_amount": base["delinquent_amount"],
        "delinquent_bill_year": base["delinquent_bill_year"],
        "party_name": base["party_name"], "mailing_address": base["mailing_address"],
        "parcel_id": base["parcel_id"], "heirs": base["heirs"],
        "legal_description": base["legal_description"],
        "property_address": base["property_address"],
    }

def write_lead_csv_with_overlap(rows: list, filelike) -> None:
    """rows = iterable of (record, overlap_dict)."""
    import csv as _csv
    w = _csv.DictWriter(filelike, fieldnames=OVERLAP_LEAD_COLUMNS)
    w.writeheader()
    for record, overlap in rows:
        w.writerow(build_overlap_export_row(record, overlap))
```

- [ ] **Step 4: Run, expect PASS**

Run: `pytest tests/test_lead_export_overlap.py -v`
Expected: PASS.

- [ ] **Step 5: Route segment exports through it + hottest-first sort**

In `intersection_export`/`union_export`: build `(SimpleNamespace(decrypted row), {lists_count, lists (human labels via the record-type label map), counties})` tuples, sort hottest-first (lists_count desc → contactable → date_recorded_parsed desc → id), then `write_lead_csv_with_overlap`. Keep `sanitize_for_csv` (the builder already applies it), R2/response handling. Remove the hand-rolled `fieldnames`/`DictWriter` blocks.

- [ ] **Step 6: Run full segments tests**

Run: `pytest tests/test_segments_intersection.py tests/test_segments_union.py tests/test_segments_date_window.py tests/test_lead_export_overlap.py -v`
Expected: PASS (update any export-column assertions in the existing tests to the new columns).

- [ ] **Step 7: Commit**

```bash
git add src/utils/lead_export.py src/api/routes/segments.py tests/test_lead_export_overlap.py tests/test_segments_*.py
git commit -m "feat(segments): unified dialer-ready CSV with overlap columns, hottest-first"
```

---

## Task 6: Frontend — Lists page county filter + lookback + Filed column

**Files:**
- Modify: `bridgeleads-web/lib/types.ts` (request/response types)
- Modify: `bridgeleads-web/lib/api.ts` (pass new params)
- Modify: `bridgeleads-web/app/(dashboard)/segments/page.tsx`

- [ ] **Step 1: Extend types**

Add `lookback_days?`, `filing_from?`, `filing_to?`, `counties?` to `SegmentRequest`; add `excluded_no_date_count` to the responses; add `date_recorded?` to `SegmentLeadRow` if missing.

- [ ] **Step 2: Pass params in `api.ts`**

Ensure `getSegmentIntersection`/`getSegmentUnion`/`exportSegment` forward the new body fields verbatim.

- [ ] **Step 3: Add a "Look back" preset row to the builder**

Buttons: All time (default, sends no window) / Last week (7) / 3 months (90) / 6 months (180) / 12 months (365) / Custom (date pickers → filing_from/filing_to). Reuse `TOGGLE_ON`/`TOGGLE_OFF`. Selecting one calls `resetResult()`.

- [ ] **Step 4: Add a "Counties" pill row**

Pills incl. "All" (sends `counties: undefined`); the county list from the user's connectors (`/connectors` already fetched elsewhere — reuse the same source). Multi-select → `counties` array. `resetResult()` on change.

- [ ] **Step 5: Add a Filed column + excluded count**

Add a `Filed` header + `{r.date_recorded ?? "—"}` cell. In the result summary line append `· {excluded_no_date_count} skipped (no filing date)` when `> 0`.

- [ ] **Step 6: Verify build + types**

Run (in `bridgeleads-web`): `npx tsc --noEmit && npx next build` (or `npm run build`)
Expected: clean.

- [ ] **Step 7: Commit (frontend repo)**

```bash
git add lib/types.ts lib/api.ts "app/(dashboard)/segments/page.tsx"
git commit -m "feat(segments): look-back window + county filter + Filed column on Lists"
```

---

## Self-Review notes
- Spec coverage: migration+index (T1), API fields (T2), windowed union (T3), windowed intersection results-path (T4), CSV unification + overlap cols + sort (T5), UI window/county/Filed/excluded (T6). All §3 spec items covered.
- The membership all-history intersection path stays unchanged when no window is sent (back-compat).
- `excluded_no_date_count` only counts when a window is active (null-date rows are included in the all-time view, as today).
- Verify `to_date` width handling in Task 1 Step 4 BEFORE relying on it (spec [P1]).

## After Piece 1 lands + verifies
Write the Piece 2 plan from `2026-06-10-batch-scrape-design.md` (it reuses Task 1's column + Task 5's `write_lead_csv_with_overlap`). Retire the completion-semantics risk first.
