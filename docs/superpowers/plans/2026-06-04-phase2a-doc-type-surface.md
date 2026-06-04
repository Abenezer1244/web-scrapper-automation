# Phase 2a — Surface Pre-Foreclosure Doc Type: Implementation Plan

> **For agentic workers:** subagent-driven, TDD, one task per commit. Checkbox steps.

**Goal:** Carry the document type (Notice of Trustee Sale / Notice of Default / Lis Pendens / etc.) all the way from scrape → `Result` → API + every export, so users can see *which* pre-foreclosure document produced each lead. No selection UI yet (that's 2b).

**Design (Claude + Codex, session 584178):** Real `Result.doc_type` column (not JSON). Carry at worker insert + BOTH worker exports + `_COLUMN_ORDER` + API `ResultRow`. The `/download` endpoint streams the stored worker export, so it's covered automatically. Capture `doc_type` in EagleWeb (currently dropped). Old rows stay NULL (no backfill — `CountyRecord.doc_type` isn't safely keyed to `results`). **Pierce per-record capture is DEFERRED** — its scraper selects doc-type checkboxes but `_map_row` can't reliably identify the per-row doc type without live ARMS validation; faking it is worse than null.

**Branch:** `feature/phase2-doc-type`. **Migration:** 035 (head is 034). **Constraint:** no test DB / Playwright / prod here — verify via no-DB unit tests + offline migration render + Codex oracle; DB roundtrip runs in CI.

---

## Task 1: Migration 035 + `Result.doc_type` column

**Files:** `alembic/versions/035_add_result_doc_type.py` (new), `src/db/models.py`

- [ ] **Step 1** — Add the column to the `Result` model in `src/db/models.py`, immediately after the `legal_description` line (`legal_description = Column(Text, nullable=True)`):
```python
    doc_type = Column(String(128), nullable=True)  # Phase 2a: which document produced this lead
```

- [ ] **Step 2** — Create `alembic/versions/035_add_result_doc_type.py`:
```python
"""Add results.doc_type (035) — Phase 2a surface pre-foreclosure document type.

Schema only, additive, nullable. Old rows stay NULL (CountyRecord.doc_type is a
separate fuzzy-keyed path — a bad backfill is worse than null). Forward-only:
the worker populates it going forward.

Revision ID: 035
Revises: 034
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("doc_type", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("results", "doc_type")
```

- [ ] **Step 3** — Verify (NO DB):
  - `python -m py_compile alembic/versions/035_add_result_doc_type.py src/db/models.py`
  - `python -c "from src.db.models import Result; print('doc_type' in [c.name for c in Result.__table__.columns])"` → `True`
  - Offline render: set `DATABASE_URL_SYNC=postgresql+psycopg2://u:p@127.0.0.1:1/none` (+ DATABASE_URL, DATABASE_URL_MIGRATE same), then `python -m alembic upgrade 034:035 --sql` → shows `ALTER TABLE results ADD COLUMN doc_type VARCHAR(128)`.

- [ ] **Step 4** — Commit:
```
git add alembic/versions/035_add_result_doc_type.py src/db/models.py
git commit -m "feat(doc-type): add results.doc_type column (migration 035, Phase 2a)

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 2: Carry doc_type at insert + both exports + API ResultRow

**Files:** `src/workers/tasks.py`, `src/utils/data_exporter.py`, `src/api/schemas.py`

- [ ] **Step 1** — `src/workers/tasks.py`, in the bulk-insert row dict (after the `"legal_description": rec.legal_description,` line, ~458), add:
```python
                    "doc_type": _trunc(rec.doc_type, 128),
```

- [ ] **Step 2** — `src/workers/tasks.py`, in the re-export `record_dicts` column list (~683), add `"doc_type"` to the list (after `"legal_description",`):
```python
                    {c: getattr(res, c) for c in [
                        "date_recorded", "party_name", "heirs", "parcel_id",
                        "property_address", "mailing_address", "legal_description",
                        "doc_type",
                        # Sprint 4: skip trace fields (may be null on first export
                        # if dispatcher hasn't submitted or webhook hasn't fired)
                        "phone", "phone_type", "email", "skip_trace_status",
                    ]}
```

- [ ] **Step 3** — `src/utils/data_exporter.py`, add `"doc_type"` to `_COLUMN_ORDER` after `"legal_description",`:
```python
    "legal_description",
    "doc_type",
    "parcel_id",
```

- [ ] **Step 4** — `src/api/schemas.py`, add to `ResultRow` (after `legal_description: str | None`, ~440):
```python
    doc_type: str | None = None
```

- [ ] **Step 5** — Verify (NO DB):
  - `python -m py_compile src/workers/tasks.py src/utils/data_exporter.py src/api/schemas.py`
  - Pure export test — append to `tests/test_data_exporter.py` (or create `tests/test_doc_type_export.py`):
```python
def test_doc_type_in_export_column_order():
    from src.utils.data_exporter import _COLUMN_ORDER
    assert "doc_type" in _COLUMN_ORDER
    # ordered right after legal_description
    assert _COLUMN_ORDER.index("doc_type") == _COLUMN_ORDER.index("legal_description") + 1


def test_export_includes_doc_type_column(tmp_path):
    from src.utils.data_exporter import DataExporter
    import csv
    rows = [{"party_name": "DOE, JOHN", "doc_type": "NOTICE OF TRUSTEE SALE", "parcel_id": "123"}]
    exporter = DataExporter()
    # export() writes to Settings.EXPORTS_DIR; just assert the column appears in the dataframe path
    import pandas as pd
    from src.utils.data_exporter import _order_columns  # if a helper exists; else build df inline
```
  (If `data_exporter` has no importable column-ordering helper, keep ONLY `test_doc_type_in_export_column_order` — it's pure and sufficient. Do not write a test that needs the DB or filesystem export if it's flaky.)
  - Run: `python -m pytest tests/test_doc_type_export.py -q` (or the file you appended to) → pass.

- [ ] **Step 6** — Commit:
```
git add src/workers/tasks.py src/utils/data_exporter.py src/api/schemas.py tests/
git commit -m "feat(doc-type): carry doc_type into Result insert, exports, and API row

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 3: Capture doc_type in EagleWeb (currently dropped)

**Files:** `src/scrapers/templates/eagleweb.py`, `tests/test_eagleweb_doc_type.py` (new)

EagleWeb filters by `desc` (the document description) but never stores it. Set `record.doc_type` from `desc` right before appending.

- [ ] **Step 1** — Write the failing test `tests/test_eagleweb_doc_type.py`:
```python
"""EagleWeb must capture the matched document description as doc_type (Phase 2a)."""
from src.scrapers.base_scraper import ScrapedRecord


def test_scraped_record_carries_doc_type_through_to_dict():
    r = ScrapedRecord(party_name="DOE, JOHN", doc_type="NOTICE OF TRUSTEE SALE")
    assert r.to_dict()["doc_type"] == "NOTICE OF TRUSTEE SALE"
```
(This pins the data-shape contract. The live capture itself is validated by reading the code + Codex oracle, since EagleWeb needs a live county site to run end-to-end.)

- [ ] **Step 2** — Run `python -m pytest tests/test_eagleweb_doc_type.py -q` → passes (ScrapedRecord already supports doc_type).

- [ ] **Step 3** — In `src/scrapers/templates/eagleweb.py`, in `_extract_page`, set `record.doc_type` from `desc` just before `records.append(record)` (inside the `if record.party_name or record.date_recorded:` block). The `desc` variable already holds the document description string used for filtering:
```python
                    # Phase 2a: capture the document type so it reaches Result/export.
                    if desc:
                        record.doc_type = desc.strip()[:128]
                    records.append(record)
```
Place this AFTER the doc-type filter `continue`s (so only matched records get here) and BEFORE `records.append(record)`. Re-read the surrounding block first to match indentation exactly.

- [ ] **Step 4** — Verify (NO DB): `python -m py_compile src/scrapers/templates/eagleweb.py` ; `python -m pytest tests/test_eagleweb_doc_type.py -q`.

- [ ] **Step 5** — Commit:
```
git add src/scrapers/templates/eagleweb.py tests/test_eagleweb_doc_type.py
git commit -m "feat(doc-type): EagleWeb captures matched document description as doc_type

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 4: Verification gate (Codex)

- [ ] **Step 1** — Full no-DB suite: `python -m pytest tests/test_property_identity.py tests/test_doc_type_export.py tests/test_eagleweb_doc_type.py -q` → green. `python -m py_compile` all changed files. `ruff check` changed files.
- [ ] **Step 2** — Offline render migration 035 (no DB) → valid `ALTER TABLE`.
- [ ] **Step 3** — Codex full-diff review (`codex exec` over `git diff main...HEAD`) + Codex oracle: would the insert→Result→export→API roundtrip carry doc_type correctly? Confirm King (already populates) + EagleWeb (now populates) flow through; confirm Pierce stays NULL (deferred) without error. Fix any P1.
- [ ] **Step 4** — Update `docs/BUILD_JOURNAL.md` + memory. Commit.

---

## Deferred (explicitly NOT in 2a)
- **Pierce per-record doc_type capture** — needs live ARMS fixture validation; its `_map_row` can't currently identify the doc-type column. Tracked for a follow-up with a real run.
- **2b**: user doc-type selection, capability registry, per-county availability + confidence, scraper plumbing (`ScrapeOptions`), King-NOD-hidden, defaults, UI.

## Spec coverage
| Design requirement | Task |
|---|---|
| Real `Result.doc_type` column, migration 035 | 1 |
| Carry at worker insert | 2 |
| Both worker exports + `_COLUMN_ORDER` (covers `/download`) | 2 |
| API `ResultRow.doc_type` (UI table) | 2 |
| EagleWeb captures doc_type | 3 |
| Old rows NULL, no backfill | 1 (nullable, no backfill) |
| Pierce deferred, not faked | Deferred section |
| Codex-verified, no prod contact | 4 |
