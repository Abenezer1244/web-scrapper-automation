# Batch Overlaps-First Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cross-record-type overlaps the first-class output of batch scraping: a per-batch `delivery_mode` (`overlaps_only` default for new batches), property_key-only overlap identity, honest delivery counts, an always-downloadable export, and paginated `/leads` JSON endpoints for the in-app combined view.

**Architecture:** The combined query (`_COMBINED_SQL`) gets prefixed, type-scoped dedup buckets and a SQL-side mode filter + deterministic ORDER BY/LIMIT/OFFSET; a companion uncapped counts query produces honest `delivery_counts` stored on the run at finalize. Download readiness is re-keyed from "R2 object exists" to "run is done/partial" (the object was never served — downloads rebuild from the DB). New `/leads` endpoints reuse the same SQL on the async RLS session.

**Tech Stack:** FastAPI (async, RLS session), Celery worker (sync psycopg2 session), PostgreSQL (Supabase), Alembic, Pydantic v2, pytest against a real `TEST_DATABASE_URL` DB.

**Spec:** `docs/superpowers/specs/2026-07-01-batch-overlaps-delivery-design.md`
**Spec amendment (Task 7):** §7 said "always uploads + always sets combined_export_key". Ground truth: the R2 object is only a ready-marker/ops artifact (API has no R2 creds; downloads rebuild from DB), and existing tests run finalize without R2 creds. The fix for Bug B is therefore: readiness = `run.status in ("done","partial")`; upload still happens only when there are rows (unchanged). This IS Codex's "change the ready-marker path" P1.

## Global Constraints

- REAL production code — no mocks, stubs, dummy data, or placeholder logic, anywhere.
- Every DB query filters by `user_id` (RLS is belt, query filter is suspenders).
- Tests run against `TEST_DATABASE_URL` (guarded by `tests/_db_safety.py`) — never prod.
- All scraped output through existing sanitize/decrypt helpers; never log PII payloads.
- `delivery_mode` values: exactly `'overlaps_only' | 'overlaps_first' | 'everything'`.
- New-batch default `overlaps_only` lives in Pydantic ONLY; DB/model default stays `everything` (existing batches must not change behavior on deploy).
- Cross-type overlap identity = `property_key` ONLY: bucket `'pk:'||property_key`; `dedup_hash` buckets are type-scoped `'dh:'||record_type||':'||dedup_hash`; identity-less rows `'id:'||id`.
- Work in the `chore/xcheck-session` worktree (off `origin/main` @ 5bc4b74). Commit per task. Never touch other branches.
- After Tasks 1–6 pass: `codex review` the full diff. Any Critical/High = NO-GO.
- Ops note (deploy, not code): run migration 078 via `scripts/migrate.py` BEFORE deploying api+worker (the ORM maps the new columns; pre-migration code + post-migration DB is fine, the reverse is not).

---

### Task 1: Migration 078 + model columns

**Files:**
- Create: `alembic/versions/078_batch_delivery_mode.py`
- Modify: `src/db/models.py` (ScraperBatch `__table_args__`/columns ~line 380–399; BatchRun columns ~line 472)
- Test: `tests/test_batch_models.py` (append)

**Interfaces:**
- Produces: `ScraperBatch.delivery_mode: str` (NOT NULL, default `'everything'`), `BatchRun.delivery_counts: dict | None` (JSON). Later tasks read/write both.

- [ ] **Step 1: Write the failing test** — append to `tests/test_batch_models.py`:

```python
class TestDeliveryModeColumns:
    def test_scraper_batch_delivery_mode_default(self):
        from src.db.models import ScraperBatch

        col = ScraperBatch.__table__.c.delivery_mode
        assert col.nullable is False
        assert col.server_default is not None
        # Python-side default protects non-API writers (tests/scheduler).
        assert col.default.arg == "everything"

    def test_scraper_batch_delivery_mode_check_constraint(self):
        from sqlalchemy import CheckConstraint

        from src.db.models import ScraperBatch

        checks = [
            c for c in ScraperBatch.__table__.constraints
            if isinstance(c, CheckConstraint)
            and c.name == "ck_scraper_batches_delivery_mode"
        ]
        assert len(checks) == 1

    def test_batch_run_delivery_counts_column(self):
        from src.db.models import BatchRun

        col = BatchRun.__table__.c.delivery_counts
        assert col.nullable is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_batch_models.py::TestDeliveryModeColumns -v`
Expected: FAIL — `delivery_mode`/`delivery_counts` not in table columns.

- [ ] **Step 3: Add the model columns** — in `src/db/models.py`:

In `ScraperBatch.__table_args__` (currently only the UniqueConstraint), add a CheckConstraint (import `CheckConstraint` from sqlalchemy alongside the existing constraint imports):

```python
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_scraper_batches_id_user"),
        # Delivery-mode allowlist at the DB layer (Codex P2): scraper_batches is
        # also written by tests/scheduler paths, so bad data must fail early —
        # API validation alone doesn't cover non-API writers.
        CheckConstraint(
            "delivery_mode IN ('overlaps_only', 'overlaps_first', 'everything')",
            name="ck_scraper_batches_delivery_mode",
        ),
    )
```

After `deliver = Column(JSON, nullable=False, default=dict)` add:

```python
    # What the combined export/leads view contains. 'everything' = all deduped
    # leads (legacy behavior — existing batches are backfilled to this so a
    # recurring schedule never silently changes output on deploy). New batches
    # default to 'overlaps_only' at the API layer (BatchCreateRequest), NOT here:
    # the server_default exists for migration/back-compat safety only, and every
    # app writer must set the mode explicitly (Codex P1/P3).
    delivery_mode = Column(
        String(16), nullable=False, default="everything", server_default="everything"
    )
```

In `BatchRun`, after `delivery_started_at` add:

```python
    # Honest delivery accounting, worker-written at finalize (RLS cutover: app
    # role has no UPDATE on batch_runs; the system session writes this).
    # {"leads_total", "overlaps_delivered", "singletons_suppressed",
    #  "unmatchable_no_parcel"} — the as-delivered snapshot for email/history.
    # Live reads (UI/download) recompute with the batch's CURRENT mode instead of
    # trusting this blob (Codex P2).
    delivery_counts = Column(JSON, nullable=True)
```

- [ ] **Step 4: Create `alembic/versions/078_batch_delivery_mode.py`:**

```python
"""scraper_batches.delivery_mode + batch_runs.delivery_counts (078)

Batch overlaps-first delivery (spec 2026-07-01): a per-batch delivery mode for
the combined export/leads view, and honest per-run delivery counts.

delivery_mode: 'overlaps_only' | 'overlaps_first' | 'everything'. Existing rows
backfill to 'everything' (server_default) so recurring scheduled batches keep
their current output on deploy; NEW batches default to 'overlaps_only' at the
API layer. CHECK constraint because scraper_batches is also written outside the
API (tests/scheduler) and bad data must fail early.

delivery_counts: JSON snapshot written by the worker at finalize
({leads_total, overlaps_delivered, singletons_suppressed, unmatchable_no_parcel}).
Additive + nullable — no backfill required.
"""
import sqlalchemy as sa
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_batches",
        sa.Column(
            "delivery_mode", sa.String(length=16),
            nullable=False, server_default="everything",
        ),
    )
    op.create_check_constraint(
        "ck_scraper_batches_delivery_mode",
        "scraper_batches",
        "delivery_mode IN ('overlaps_only', 'overlaps_first', 'everything')",
    )
    op.add_column("batch_runs", sa.Column("delivery_counts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("batch_runs", "delivery_counts")
    op.drop_constraint(
        "ck_scraper_batches_delivery_mode", "scraper_batches", type_="check"
    )
    op.drop_column("scraper_batches", "delivery_mode")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_batch_models.py -v`
Expected: ALL PASS (new class + existing tests — the schema-driven test DB picks up model columns via create_all/migration fixture, matching how existing column tests run).

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/078_batch_delivery_mode.py src/db/models.py tests/test_batch_models.py
git commit -m "feat(batch): migration 078 — scraper_batches.delivery_mode + batch_runs.delivery_counts"
```

---

### Task 2: Combined SQL rework — buckets, overlap identity, mode filter, counts

**Files:**
- Modify: `src/workers/batch_export.py` (`_COMBINED_SQL` ~line 64–108; `_combined_pairs` ~line 123–168; `render_combined_csv` ~line 171)
- Test: `tests/test_batch_export.py` (modify `TestCombinedSql`, `TestRawSqlExecutesOnPostgres`; add `TestDeliveryCountsSql`)

**Interfaces:**
- Consumes: nothing new (Task 1 columns not needed yet).
- Produces:
  - `_COMBINED_SQL` binds: `:uid, :job_ids, :limit, :offset, :overlaps_only, TAX_CAP_BIND`
  - `_DELIVERY_COUNTS_SQL` binds: `:uid, :job_ids, TAX_CAP_BIND` → one row `(leads_total, overlaps_delivered, singletons_suppressed, unmatchable_no_parcel)`
  - `_combined_pairs(db, user_id, job_ids, delivery_mode="everything", limit=EXPORT_CAP, offset=0) -> list[tuple]`
  - `compute_delivery_counts(db, user_id, job_ids) -> dict[str, int]`
  - `render_combined_csv(user_id, job_ids, hidden_fields=None, delivery_mode="everything") -> bytes`

- [ ] **Step 1: Update the SQL-shape tests** — in `tests/test_batch_export.py`, replace `TestCombinedSql.test_dedup_and_overlap_and_counties` and add the new shape + counts tests:

```python
class TestCombinedSql:
    def test_scoped_to_batch_jobs_not_history(self):
        # unchanged — keep the existing assertions
        assert "r.job_id = ANY(CAST(:job_ids AS uuid[]))" in _COMBINED_SQL
        assert "r.user_id = CAST(:uid AS uuid)" in _COMBINED_SQL

    def test_buckets_are_prefixed_and_type_scoped(self):
        # Overlap identity is property_key ONLY. dedup_hash buckets carry the
        # record_type so a weak name+date hash can never merge two record types
        # (fake overlap + silently dropped row — Codex P1).
        assert "'pk:' || r.property_key" in _COMBINED_SQL
        assert "'dh:' || sc.record_type || ':' || r.dedup_hash" in _COMBINED_SQL
        assert "'id:' || r.id::text" in _COMBINED_SQL
        assert "COALESCE(r.property_key, r.dedup_hash" not in _COMBINED_SQL

    def test_overlap_count_is_property_key_only(self):
        assert (
            "CASE WHEN bucket LIKE 'pk:%' THEN count(DISTINCT record_type) ELSE 1 END"
            in _COMBINED_SQL
        )

    def test_mode_filter_and_deterministic_order(self):
        # overlaps_only filters in SQL BEFORE LIMIT (Codex P1: a Python filter
        # after the 50k cap could miss real overlaps + lie in counts).
        assert ":overlaps_only" in _COMBINED_SQL
        assert "ORDER BY a.overlap_count DESC" in _COMBINED_SQL
        assert "LIMIT :limit OFFSET :offset" in _COMBINED_SQL

    def test_counts_sql_shape(self):
        from src.workers.batch_export import _DELIVERY_COUNTS_SQL

        assert "count(*) AS leads_total" in _DELIVERY_COUNTS_SQL
        assert "overlaps_delivered" in _DELIVERY_COUNTS_SQL
        assert "singletons_suppressed" in _DELIVERY_COUNTS_SQL
        assert "unmatchable_no_parcel" in _DELIVERY_COUNTS_SQL
        assert "LIMIT" not in _DELIVERY_COUNTS_SQL  # counts are UNCAPPED
```

- [ ] **Step 2: Extend `TestRawSqlExecutesOnPostgres`** — the `_run` helper must bind the new params; both SQLs must execute on real Postgres via the sync session:

```python
    def _run(self, sql: str):
        from datetime import UTC, datetime

        from sqlalchemy import text

        from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year
        from src.db.session import system_sync_session

        with system_sync_session() as db:
            for job_ids in ([str(uuid.uuid4())], []):
                params = {"uid": str(uuid.uuid4()), "job_ids": job_ids}
                if "LIMIT :limit" in sql:
                    params["limit"] = 10
                    params["offset"] = 0
                if ":overlaps_only" in sql:
                    params["overlaps_only"] = True
                if f":{TAX_CAP_BIND}" in sql:
                    params[TAX_CAP_BIND] = tax_cap_min_year(datetime.now(UTC).date())
                db.execute(text(sql), params).fetchall()
            db.rollback()

    def test_combined_sql_executes(self):
        self._run(_COMBINED_SQL)

    def test_delivery_counts_sql_executes(self):
        from src.workers.batch_export import _DELIVERY_COUNTS_SQL

        self._run(_DELIVERY_COUNTS_SQL)

    def test_failed_children_sql_executes(self):
        self._run(_FAILED_CHILDREN_SQL)
```

- [ ] **Step 3: Run tests to verify the new ones fail**

Run: `python -m pytest tests/test_batch_export.py -v`
Expected: new shape tests FAIL (old SQL); `test_scoped_to_batch_jobs_not_history` and helper tests still PASS.

- [ ] **Step 4: Rewrite the SQL in `src/workers/batch_export.py`** — replace `_COMBINED_SQL` (keep the explanatory comment block above it, updating it) with a shared-CTE construction:

```python
# Combined set over the batch's jobs. Dedup bucket (prefixed — the prefixes make
# overlap classification unambiguous and kill cross-key collisions):
#   'pk:' || property_key                      — the ONLY cross-record-type identity
#   'dh:' || record_type || ':' || dedup_hash  — within-type dedup ONLY. dedup_hash's
#       weak branch is party_name+date_recorded (tasks.py), so an un-scoped hash
#       would merge two record types into one fake-overlap row and silently drop
#       one of them (Codex P1).
#   'id:' || id                                — no identity; never groups.
# overlap_count counts DISTINCT record types for pk: buckets only; everything
# else is 1 by construction. Tenant-scoped (every join carries :uid).
# Mode filter (:overlaps_only) and deterministic ORDER BY happen in SQL, BEFORE
# LIMIT/OFFSET — a Python filter after the cap could return zero overlaps even
# when overlaps exist past the 50k sample (Codex P1). Ordering: hottest first
# (overlap_count DESC), then contactable, then newest job, then id (stable).
_COMBINED_CTES = f"""
WITH candidates AS (
    SELECT r.id, r.date_recorded, r.party_name, r.parcel_id, r.property_address,
           r.mailing_address, r.phone, r.phone_type, r.email,
           r.property_key, r.is_duplicate,
           r.enrichment_data->>'lead_subtype' AS lead_subtype,
           sc.record_type, sc.county, j.created_at AS job_created_at,
           CASE
               WHEN r.property_key IS NOT NULL THEN 'pk:' || r.property_key
               WHEN r.dedup_hash IS NOT NULL
                   THEN 'dh:' || sc.record_type || ':' || r.dedup_hash
               ELSE 'id:' || r.id::text
           END AS bucket
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = CAST(:uid AS uuid)
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
    WHERE r.user_id = CAST(:uid AS uuid)
      AND r.job_id = ANY(CAST(:job_ids AS uuid[]))
      -- Hard 18-month tax-delinquent cap (self-scoping: NULL bill_year rows pass).
      AND {tax_cap_sql('r')}
),
agg AS (
    SELECT bucket,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           CASE WHEN bucket LIKE 'pk:%' THEN count(DISTINCT record_type) ELSE 1 END AS overlap_count,
           {PROBATE_SUBTYPE_AGG_SQL},
           array_agg(DISTINCT county ORDER BY county) AS source_counties
    FROM candidates
    GROUP BY bucket
)"""

_COMBINED_SQL = _COMBINED_CTES + """,
ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.bucket
               ORDER BY (CASE WHEN c.phone IS NOT NULL OR c.email IS NOT NULL
                              THEN 0 ELSE 1 END),
                        c.is_duplicate ASC,
                        c.job_created_at DESC NULLS LAST,
                        c.id DESC
           ) AS rn
    FROM candidates c
)
SELECT rk.id, rk.date_recorded, rk.party_name, rk.parcel_id, rk.property_address,
       rk.mailing_address, rk.phone, rk.phone_type, rk.email,
       a.matched_record_types, a.overlap_count, a.source_counties, a.lead_subtype
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket
WHERE rk.rn = 1
  AND (NOT :overlaps_only OR (rk.bucket LIKE 'pk:%' AND a.overlap_count >= 2))
ORDER BY a.overlap_count DESC,
         (CASE WHEN rk.phone IS NOT NULL OR rk.email IS NOT NULL THEN 0 ELSE 1 END),
         rk.job_created_at DESC NULLS LAST,
         rk.id DESC
LIMIT :limit OFFSET :offset
"""

# Honest delivery accounting over the SAME dedup/aggregation — UNCAPPED (counts
# must be batch facts, not capped-sample facts — Codex P1). Mode-independent:
# these are dataset facts; delivery interprets them per mode.
_DELIVERY_COUNTS_SQL = _COMBINED_CTES + """
SELECT count(*) AS leads_total,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count >= 2) AS overlaps_delivered,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count < 2) AS singletons_suppressed,
       count(*) FILTER (WHERE bucket NOT LIKE 'pk:%') AS unmatchable_no_parcel
FROM agg
"""
```

- [ ] **Step 5: Update `_combined_pairs` + add `compute_delivery_counts` + thread mode through `render_combined_csv`:**

```python
_EMPTY_COUNTS = {
    "leads_total": 0,
    "overlaps_delivered": 0,
    "singletons_suppressed": 0,
    "unmatchable_no_parcel": 0,
}


def _combined_pairs(
    db,
    user_id: str,
    job_ids: list[str],
    delivery_mode: str = "everything",
    limit: int = EXPORT_CAP,
    offset: int = 0,
) -> list[tuple]:
    """Return (record_namespace, overlap_dict) pairs for the batch, hottest-first.

    Ordering + mode filtering are SQL-side (deterministic; pagination-safe).
    PII (phone/email) is decrypted here — the raw text() query bypasses the
    EncryptedString type. matched_record_types are humanized for the `lists` col.
    """
    if not job_ids:
        return []
    result = db.execute(
        text(_COMBINED_SQL),
        {
            "uid": user_id,
            "job_ids": job_ids,
            "limit": limit,
            "offset": offset,
            "overlaps_only": delivery_mode == "overlaps_only",
            TAX_CAP_BIND: tax_cap_min_year(datetime.now(UTC).date()),
        },
    )
    rows = []
    for r in result.fetchall():
        data = dict(r._mapping)
        if data.get("phone") is not None:
            data["phone"] = decrypt_field(data["phone"])
        if data.get("email") is not None:
            data["email"] = decrypt_field(data["email"])
        rows.append(SimpleNamespace(**data))
    return [
        (
            r,
            {
                "lists_count": r.overlap_count,
                "lists": "; ".join(_label(t) for t in (r.matched_record_types or [])),
                "counties": "; ".join(r.source_counties or []),
            },
        )
        for r in rows
    ]


def compute_delivery_counts(db, user_id: str, job_ids: list[str]) -> dict[str, int]:
    """Uncapped, mode-independent dataset facts for honest delivery messaging."""
    if not job_ids:
        return dict(_EMPTY_COUNTS)
    row = db.execute(
        text(_DELIVERY_COUNTS_SQL),
        {
            "uid": user_id,
            "job_ids": job_ids,
            TAX_CAP_BIND: tax_cap_min_year(datetime.now(UTC).date()),
        },
    ).one()
    return {
        "leads_total": int(row.leads_total),
        "overlaps_delivered": int(row.overlaps_delivered),
        "singletons_suppressed": int(row.singletons_suppressed),
        "unmatchable_no_parcel": int(row.unmatchable_no_parcel),
    }
```

Note: the old in-Python `rows.sort(...)` block and `_filing_sort_key` usage inside `_combined_pairs` are REMOVED (ordering is SQL-side now; `_filing_sort_key` itself stays — it has its own tests and no other callers, delete it ONLY if `grep -rn "_filing_sort_key" src tests` shows the tests as sole consumers, in which case delete the helper AND its test block in the same commit). Behavior note (deliberate): the tertiary sort changes from filing-date-recency to job-recency — filing-date parsing of `'M/D/YYYY'` strings in SQL would error on garbage rows and break the whole export.

`render_combined_csv` gains the mode param (same docstring guarantees):

```python
def render_combined_csv(
    user_id: str,
    job_ids: list[str],
    hidden_fields: set[str] | None = None,
    delivery_mode: str = "everything",
) -> bytes:
```

…and passes it through: `pairs = _combined_pairs(db, user_id, job_ids, delivery_mode=delivery_mode)`.

- [ ] **Step 6: Run the export tests**

Run: `python -m pytest tests/test_batch_export.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Run the neighboring batch suites (regression)**

Run: `python -m pytest tests/test_batch_recovery.py tests/test_batch_dispatch.py tests/test_batches_read.py tests/test_batch_2b_foundation.py -v`
Expected: ALL PASS (finalize call sites unchanged so far).

- [ ] **Step 8: Commit**

```bash
git add src/workers/batch_export.py tests/test_batch_export.py
git commit -m "fix(batch): property_key-only overlap identity + SQL-side mode filter/order + uncapped delivery counts"
```

---

### Task 3: Finalize — counts, mode-aware CSV, ready semantics, honest email

**Files:**
- Modify: `src/workers/batch_export.py` (`finalize_batch_run` ~line 196; `_deliver` ~line 343)
- Test: Create `tests/test_batch_delivery_mode.py`

**Interfaces:**
- Consumes: Task 1 columns; Task 2 `_combined_pairs(delivery_mode=...)`, `compute_delivery_counts`.
- Produces: `finalize_batch_run` stores `delivery_counts` on the run and finalizes zero-row runs as downloadable; `_delivery_summary(mode: str, counts: dict) -> str`; `_deliver(db, run, lead_count, object_key, new_status, summary)` (signature change — internal, no external callers).

- [ ] **Step 1: Write the failing DB-backed tests** — create `tests/test_batch_delivery_mode.py`. Follow the sync-session fixture pattern of `tests/test_batch_recovery.py` (module-local `_user`/`_batch`/`_config` helpers with `SyncSessionLocal`; copy those helpers, adding `delivery_mode` to `_batch` and a `_result` helper):

```python
"""Batch overlaps-first delivery (spec 2026-07-01).

DB-backed proof of the three fixed bugs + the delivery modes:
  Bug A — a weak dedup_hash (name+date) can no longer merge two record types
          (fake overlap + silently dropped row).
  Bug B — a zero-row overlaps_only run still finalizes 'done' with counts
          (readiness no longer keyed on the R2 object).
  Modes — overlaps_only filters to pk-bucket 2+-type leads; everything keeps all.
"""
import uuid
from datetime import UTC, datetime

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_export import (
    _combined_pairs,
    compute_delivery_counts,
    finalize_batch_run,
)


def _user(db) -> User:
    u = User(
        id=str(uuid.uuid4()),
        email=f"dm-{uuid.uuid4().hex[:10]}@test.local",
        hashed_password="x" * 60,
        plan="pro",
        records_used=0,
        records_limit=-1,
    )
    db.add(u)
    db.flush()
    return u


def _batch(db, user_id: str, delivery_mode: str = "everything") -> ScraperBatch:
    b = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="DM Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
        delivery_mode=delivery_mode,
    )
    db.add(b)
    db.flush()
    return b


def _config(db, user_id: str, batch_id: str, record_type: str) -> ScraperConfig:
    c = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        batch_id=batch_id,
        name=f"child {record_type}",
        county="pierce",
        state="WA",
        record_type=record_type,
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
    )
    db.add(c)
    db.flush()
    return c


def _done_job(db, user_id: str, config_id: str) -> Job:
    j = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        scraper_config_id=config_id,
        status="done",
        trigger="batch",
    )
    db.add(j)
    db.flush()
    return j


def _result(db, user_id: str, job_id: str, *, party="JANE DOE",
            property_key=None, dedup_hash=None) -> Result:
    r = Result(
        id=str(uuid.uuid4()),
        user_id=user_id,
        job_id=job_id,
        date_recorded="06/01/2026",
        party_name=party,
        property_key=property_key,
        dedup_hash=dedup_hash,
    )
    db.add(r)
    db.flush()
    return r


def _two_type_batch(db, user, delivery_mode: str):
    """A batch with probate + tax_delinquent children, both jobs done."""
    batch = _batch(db, user.id, delivery_mode)
    c1 = _config(db, user.id, batch.id, "probate")
    c2 = _config(db, user.id, batch.id, "tax_delinquent")
    j1 = _done_job(db, user.id, c1.id)
    j2 = _done_job(db, user.id, c2.id)
    return batch, j1, j2


class TestOverlapIdentity:
    def test_weak_hash_never_bridges_record_types(self):
        """Bug A: same name+date dedup_hash in two record types = TWO rows, no overlap."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            _result(db, user.id, j1.id, dedup_hash="weakhash1")
            _result(db, user.id, j2.id, dedup_hash="weakhash1")
            db.commit()

            pairs = _combined_pairs(db, user.id, [j1.id, j2.id])
            assert len(pairs) == 2  # was 1 (merged) before the fix
            assert all(rec.overlap_count == 1 for rec, _ in pairs)
            db.rollback()

    def test_property_key_bridges_record_types(self):
        """Same parcel in two record types = ONE row, overlap_count=2."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            _result(db, user.id, j1.id, property_key="WA|pierce|0011223344")
            _result(db, user.id, j2.id, property_key="WA|pierce|0011223344")
            db.commit()

            pairs = _combined_pairs(db, user.id, [j1.id, j2.id])
            assert len(pairs) == 1
            rec, overlap = pairs[0]
            assert rec.overlap_count == 2
            assert overlap["lists"] == "Probate; Tax Delinquent"
            db.rollback()


class TestDeliveryModes:
    def _seed(self, db):
        """1 overlap (parcel in both types) + 1 pk singleton + 1 no-identity row."""
        user = _user(db)
        batch, j1, j2 = _two_type_batch(db, user, "overlaps_only")
        _result(db, user.id, j1.id, party="OVERLAP", property_key="WA|pierce|0000000001")
        _result(db, user.id, j2.id, party="OVERLAP", property_key="WA|pierce|0000000001")
        _result(db, user.id, j1.id, party="SINGLETON", property_key="WA|pierce|0000000002")
        _result(db, user.id, j2.id, party="NOPARCEL")
        db.commit()
        return user, batch, [j1.id, j2.id]

    def test_overlaps_only_filters_to_real_overlaps(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            pairs = _combined_pairs(db, user.id, job_ids, delivery_mode="overlaps_only")
            assert [rec.party_name for rec, _ in pairs] == ["OVERLAP"]
            db.rollback()

    def test_everything_keeps_all_overlap_first(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            pairs = _combined_pairs(db, user.id, job_ids, delivery_mode="everything")
            assert len(pairs) == 3
            assert pairs[0][0].party_name == "OVERLAP"  # overlap ranks first
            db.rollback()

    def test_counts_are_honest(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            counts = compute_delivery_counts(db, user.id, job_ids)
            assert counts == {
                "leads_total": 3,
                "overlaps_delivered": 1,
                "singletons_suppressed": 1,
                "unmatchable_no_parcel": 1,
            }
            db.rollback()


class TestEmptyStateFinalize:
    def test_zero_overlap_run_finalizes_done_with_counts(self):
        """Bug B: overlaps_only + zero overlaps => run 'done', counts stored,
        no R2 object needed (readiness comes from status, Task 5)."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "overlaps_only")
            _result(db, user.id, j1.id, dedup_hash="w1")  # no parcels anywhere
            _result(db, user.id, j2.id, dedup_hash="w2")
            run = BatchRun(
                id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                status="running", child_job_ids=[j1.id, j2.id],
            )
            db.add(run)
            db.commit()
            run_id = run.id

            run = db.get(BatchRun, run_id)
            finalize_batch_run(db, run)

        with SyncSessionLocal() as db:
            run = db.get(BatchRun, run_id)
            assert run.status == "done"
            assert run.completed_at is not None
            assert run.combined_export_key is None  # nothing uploaded — nothing to store
            assert run.delivery_counts == {
                "leads_total": 2,
                "overlaps_delivered": 0,
                "singletons_suppressed": 0,
                "unmatchable_no_parcel": 2,
            }
```

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_batch_delivery_mode.py -v`
Expected: `TestOverlapIdentity`/`TestDeliveryModes` PASS already (Task 2 built them); `TestEmptyStateFinalize` FAILS on `delivery_counts` being None (finalize doesn't store counts yet).

- [ ] **Step 3: Update `finalize_batch_run`** in `src/workers/batch_export.py`. Concrete changes (the rest of the function body stays as-is):

(a) Move the `_batch` fetch + hidden-fields resolution ABOVE the `pairs = ...` line and add the mode; replace the current two lines `pairs = _combined_pairs(db, run.user_id, run.child_job_ids or [])` and the later `_batch`/`hidden_fields` block with:

```python
    # The parent batch owns output shape: delivery_mode (what the combined
    # export contains) + fields (hideable-column visibility).
    from src.utils.lead_export import resolve_hidden_output_fields
    _batch = db.get(ScraperBatch, run.batch_id)
    delivery_mode = (_batch.delivery_mode if _batch else None) or "everything"
    hidden_fields = resolve_hidden_output_fields(_batch.fields if _batch else None)

    pairs = _combined_pairs(
        db, run.user_id, run.child_job_ids or [], delivery_mode=delivery_mode
    )
    # Honest accounting (uncapped, mode-independent). Stored on the run as the
    # as-delivered snapshot; live reads recompute with the current mode.
    counts = compute_delivery_counts(db, run.user_id, run.child_job_ids or [])
```

(b) Pass `hidden_fields` to `write_lead_csv_with_overlap` exactly as today (unchanged); the upload block stays gated on `if pairs:` (the R2 object is an ops artifact + legacy marker, never served — readiness moves to run status in Task 5).

(c) Add `delivery_counts=counts` to the guarded terminal `update(BatchRun).values(...)`:

```python
        .values(
            combined_export_key=object_key,
            failed_children=failed or None,
            status=new_status,
            completed_at=datetime.now(UTC),
            delivery_counts=counts,
        )
```

(d) Replace the delivery call `_deliver(db, run, len(pairs), object_key)` with:

```python
    _deliver(
        db, run, len(pairs), object_key,
        new_status=new_status,
        summary=_delivery_summary(delivery_mode, counts),
    )
```

- [ ] **Step 4: Add `_delivery_summary` and update `_deliver`:**

```python
def _delivery_summary(mode: str, counts: dict) -> str:
    """One honest sentence for the delivery email. The empty overlaps_only case
    must read as 'no overlaps found' (with the why), never as 'broken'."""
    total = counts.get("leads_total", 0)
    overlaps = counts.get("overlaps_delivered", 0)
    no_parcel = counts.get("unmatchable_no_parcel", 0)
    if mode == "overlaps_only":
        if overlaps == 0:
            return (
                f"0 cross-list overlap leads found across {total:,} scraped leads. "
                f"{no_parcel:,} lead(s) had no parcel number and couldn't be "
                "cross-matched. Switch the batch to 'Everything' to receive all leads."
            )
        return (
            f"{overlaps:,} lead(s) found on 2 or more lists. "
            f"{total - overlaps:,} single-list lead(s) not included in this delivery."
        )
    return f"{overlaps:,} of {total:,} lead(s) appear on 2 or more lists."
```

In `_deliver`, change the signature and the gate. Replace:

```python
def _deliver(db, run, lead_count: int, object_key: str | None) -> None:
```
```python
    if not object_key:
        return
```

with:

```python
def _deliver(
    db, run, lead_count: int, object_key: str | None,
    new_status: str = "done", summary: str | None = None,
) -> None:
```
```python
    # Email on every successful finalize — including a zero-row overlaps_only
    # run (the honest empty-state IS the delivery). Fully-failed runs don't
    # email (ops alerts cover failures); the old `if not object_key` gate would
    # have silently skipped exactly the empty-state case this feature exists for.
    if new_status not in ("done", "partial"):
        return
```

and extend the `deliver_job_email.delay(...)` call with the new kwargs (Task 4 adds them to the task):

```python
        deliver_job_email.delay(
            job_id=str(run.id),
            scraper_name=batch.name or "Batch scrape",
            record_count=lead_count,
            download_url=url,
            recipient_emails=emails,
            summary_message=summary,
            link_expires=False,  # in-app batch page — not a presigned URL (Codex P2)
        )
```

Keep the existing `delivery_started_at` CAS and docstring; update the docstring's first line to mention the summary message.

- [ ] **Step 5: Run the new tests + full batch regression**

Run: `python -m pytest tests/test_batch_delivery_mode.py tests/test_batch_export.py tests/test_batch_recovery.py tests/test_batch_2b_foundation.py -v`
Expected: ALL PASS. (`test_batch_recovery` finalize tests now also get `delivery_counts` populated — they don't assert on it, so no changes needed; verify no failures.)

- [ ] **Step 6: Commit**

```bash
git add src/workers/batch_export.py tests/test_batch_delivery_mode.py
git commit -m "feat(batch): mode-aware finalize with honest delivery_counts + empty-state email"
```

---

### Task 4: Delivery email — summary message + correct expiry copy

**Files:**
- Modify: `src/workers/delivery.py` (`_build_lead_delivery_email` ~line 61; `deliver_job_email` ~line 150)
- Test: `tests/test_delivery_download_url.py` (append — it already tests this module's pure builder; keep test module mapping)

**Interfaces:**
- Consumes: called by Task 3's `_deliver` with `summary_message`/`link_expires`.
- Produces: `_build_lead_delivery_email(scraper_name, record_count, download_url, fmt, summary_message=None, link_expires=True)`; `deliver_job_email(..., summary_message=None, link_expires=True)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_delivery_download_url.py`:

```python
class TestDeliverySummaryMessage:
    def test_summary_message_rendered_in_both_bodies(self):
        from src.workers.delivery import _build_lead_delivery_email

        subject, html_body, text_body = _build_lead_delivery_email(
            "My Batch", 0, "https://app.bridgeleads.io/batches/abc", "csv",
            summary_message="0 cross-list overlap leads found across 12 scraped leads.",
            link_expires=False,
        )
        assert "0 cross-list overlap leads" in html_body
        assert "0 cross-list overlap leads" in text_body

    def test_batch_link_has_no_expiry_copy(self):
        from src.workers.delivery import _build_lead_delivery_email

        _, html_body, text_body = _build_lead_delivery_email(
            "My Batch", 5, "https://app.bridgeleads.io/batches/abc", "csv",
            link_expires=False,
        )
        # The in-app batch page never expires — the 48h line is presign-only copy.
        assert "expires in 48 hours" not in html_body
        assert "expires in 48 hours" not in text_body

    def test_default_keeps_expiry_and_no_summary(self):
        from src.workers.delivery import _build_lead_delivery_email

        _, html_body, text_body = _build_lead_delivery_email(
            "Scraper", 5, "https://x/dl", "csv",
        )
        assert "expires in 48 hours" in html_body
        assert "expires in 48 hours" in text_body
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_delivery_download_url.py -v`
Expected: new class FAILS with `TypeError: unexpected keyword argument`.

- [ ] **Step 3: Implement** — in `src/workers/delivery.py`:

`_build_lead_delivery_email` signature:

```python
def _build_lead_delivery_email(
    scraper_name: str, record_count: int, download_url: str, fmt: str,
    summary_message: str | None = None, link_expires: bool = True,
) -> tuple[str, str, str]:
```

Inside, before the `html_body` f-string, build the two conditional fragments:

```python
    # Batch deliveries link to the in-app batch page (no expiry); per-job links
    # are 48h presigns. Wrong copy on a batch email erodes trust (Codex P2).
    expiry_html = (
        '<p class="expiry">This download link expires in 48 hours.</p>'
        if link_expires else ""
    )
    expiry_text = "This link expires in 48 hours.\n\n" if link_expires else ""
    summary_html = (
        f'<p class="meta" style="margin-top:-16px; margin-bottom:24px;">'
        f"{html.escape(summary_message)}</p>"
        if summary_message else ""
    )
    summary_text = f"{summary_message}\n\n" if summary_message else ""
```

In `html_body`: replace the literal `<p class="expiry">This download link expires in 48 hours.</p>` line with `{expiry_html}`, and insert `{summary_html}` directly after the `<div class="stat">...</div>` block. In `text_body`: replace `"This link expires in 48 hours.\n\n"` with `{expiry_text}` and insert `{summary_text}` after the records line:

```python
    text_body = (
        f"Your {scraper_name} leads are ready.\n\n"
        f"{record_count:,} records found.\n\n"
        f"{summary_text}"
        f"Download ({fmt.upper()}): {download_url}\n\n"
        f"{expiry_text}"
        f"{DNC_DISCLAIMER}\n\n"
        "Manage delivery settings at app.bridgeleads.io"
    )
```

`deliver_job_email` signature (backward-compatible — queued payloads without the kwargs still deserialize):

```python
def deliver_job_email(
    self,
    job_id: str,
    scraper_name: str,
    record_count: int,
    download_url: str,
    recipient_emails: list[str],
    fmt: str = "csv",
    summary_message: str | None = None,
    link_expires: bool = True,
) -> None:
```

…and pass both through to `_build_lead_delivery_email(scraper_name, record_count, download_url, fmt, summary_message=summary_message, link_expires=link_expires)` at its call site inside the task.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_delivery_download_url.py tests/test_batch_delivery_mode.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/workers/delivery.py tests/test_delivery_download_url.py
git commit -m "feat(delivery): optional summary message + expiry copy only for presigned links"
```

---

### Task 5: API — persist mode, status-based readiness, mode-aware download

**Files:**
- Modify: `src/api/schemas.py` (batch schemas ~line 835–927)
- Modify: `src/api/routes/batches.py` (`create_batch`, `_summary`, `_stream_run_csv`, `download_batch`, `download_batch_run`, `_run_response`, `get_batch`)
- Test: `tests/test_batches.py` + `tests/test_batches_read.py` (append)

**Interfaces:**
- Consumes: Task 1 columns, Task 2 `render_combined_csv(delivery_mode=...)`.
- Produces:
  - `BatchCreateRequest.delivery_mode: Literal["overlaps_only","overlaps_first","everything"] = "overlaps_only"`
  - `BatchDeliveryCounts` schema; `delivery_mode` on `BatchSummaryResponse`; `delivery_counts` on `BatchDetailResponse` + `BatchRunResponse`
  - Readiness: `combined_export_ready = run.status in ("done", "partial")`; download gate likewise (Task 6 reuses `_DOWNLOADABLE_STATUSES`).

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_batches.py` (which holds create-endpoint tests — follow its existing client/auth fixture pattern; read the file's fixtures first and reuse them exactly):

```python
class TestDeliveryModeCreate:
    async def test_default_is_overlaps_only(self, client, pro_user_token, db):
        # Reuse this file's existing minimal valid create payload — copy the
        # payload dict from the nearest passing create test and do NOT set
        # delivery_mode.
        resp = await client.post("/batches", json=payload, headers=_auth(pro_user_token))
        assert resp.status_code == 201
        from sqlalchemy import select

        from src.db.models import ScraperBatch

        batch = (await db.execute(
            select(ScraperBatch).where(ScraperBatch.id == resp.json()["batch_id"])
        )).scalar_one()
        assert batch.delivery_mode == "overlaps_only"

    async def test_explicit_mode_persisted(self, client, pro_user_token, db):
        payload_with_mode = {**payload, "delivery_mode": "everything"}
        resp = await client.post(
            "/batches", json=payload_with_mode, headers=_auth(pro_user_token)
        )
        assert resp.status_code == 201
        from sqlalchemy import select

        from src.db.models import ScraperBatch

        batch = (await db.execute(
            select(ScraperBatch).where(ScraperBatch.id == resp.json()["batch_id"])
        )).scalar_one()
        assert batch.delivery_mode == "everything"

    async def test_invalid_mode_422(self, client, pro_user_token):
        resp = await client.post(
            "/batches", json={**payload, "delivery_mode": "bogus"},
            headers=_auth(pro_user_token),
        )
        assert resp.status_code == 422
```

Append to `tests/test_batches_read.py`:

```python
class TestStatusBasedReadiness:
    async def test_done_run_without_key_is_ready_and_downloadable(
        self, client, starter_token, db, starter_user
    ):
        """Bug B end-to-end: a 'done' run with NO combined_export_key (zero-row
        overlaps_only) must read ready and stream a headers-only CSV."""
        import uuid as _uuid

        from src.db.models import BatchRun, ScraperBatch

        batch = ScraperBatch(
            id=str(_uuid.uuid4()), user_id=starter_user.id, name="Empty",
            state="WA", fields=[], enrichment=[], schedule={}, deliver={},
            status="active", delivery_mode="overlaps_only",
        )
        db.add(batch)
        await db.flush()
        run = BatchRun(
            id=str(_uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
            status="done", child_job_ids=[], combined_export_key=None,
            delivery_counts={"leads_total": 0, "overlaps_delivered": 0,
                             "singletons_suppressed": 0, "unmatchable_no_parcel": 0},
        )
        db.add(run)
        await db.commit()

        detail = await client.get(f"/batches/{batch.id}", headers=_auth(starter_token))
        assert detail.status_code == 200
        body = detail.json()
        assert body["combined_export_ready"] is True
        assert body["delivery_mode"] == "overlaps_only"
        assert body["delivery_counts"]["leads_total"] == 0

        dl = await client.get(
            f"/batches/{batch.id}/download", headers=_auth(starter_token)
        )
        assert dl.status_code == 200
        lines = [ln for ln in dl.text.splitlines() if ln.strip()]
        assert len(lines) == 1  # header only

    async def test_running_run_not_ready(self, client, starter_token, db, starter_user):
        import uuid as _uuid

        from src.db.models import BatchRun, ScraperBatch

        batch = ScraperBatch(
            id=str(_uuid.uuid4()), user_id=starter_user.id, name="Running",
            state="WA", fields=[], enrichment=[], schedule={}, deliver={},
            status="active",
        )
        db.add(batch)
        await db.flush()
        db.add(BatchRun(
            id=str(_uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
            status="running", child_job_ids=[],
        ))
        await db.commit()

        dl = await client.get(
            f"/batches/{batch.id}/download", headers=_auth(starter_token)
        )
        assert dl.status_code == 404
```

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_batches.py -k DeliveryMode tests/test_batches_read.py -k Readiness -v`
Expected: FAIL (`delivery_mode` unknown field / readiness False / download 404).

- [ ] **Step 3: Schemas** — in `src/api/schemas.py` (add `Literal` to the existing typing import if absent):

In `BatchCreateRequest` after `skip_trace_enabled`:

```python
    # What the combined export contains. Default overlaps_only: the product
    # point of a batch is leads on 2+ lists — singletons are obtainable from
    # single scrapes. Existing batches (created before this field) stay
    # 'everything' via the DB default; the route persists this value explicitly.
    delivery_mode: Literal["overlaps_only", "overlaps_first", "everything"] = (
        "overlaps_only"
    )
```

Before `BatchRunResponse` add:

```python
class BatchDeliveryCounts(BaseModel):
    """Honest delivery accounting (uncapped dataset facts, mode-interpreted)."""

    leads_total: int = 0
    overlaps_delivered: int = 0
    singletons_suppressed: int = 0
    unmatchable_no_parcel: int = 0
```

`BatchRunResponse` gains: `delivery_counts: BatchDeliveryCounts | None = None`
`BatchSummaryResponse` gains: `delivery_mode: str = "everything"`
`BatchDetailResponse` gains: `delivery_counts: BatchDeliveryCounts | None = None`

- [ ] **Step 4: Routes** — in `src/api/routes/batches.py`:

(a) `create_batch`: add `delivery_mode=body.delivery_mode,` to the `ScraperBatch(...)` constructor (Codex P1 — without this the DB default silently wins and every new batch is `everything`).

(b) Module-level, near `_RUN_HISTORY_CAP`:

```python
# A run's combined output is downloadable once it TERMINALIZES with data intact
# — including a zero-row overlaps_only run (Bug B: the old combined_export_key
# gate 404'd exactly the honest-empty case; the R2 object is an ops artifact,
# never served — downloads rebuild from the DB). 'failed' = ALL children failed:
# nothing was produced, so it stays not-ready (matches the UI's copy).
_DOWNLOADABLE_STATUSES = ("done", "partial")
```

(c) `_summary`: replace `combined_export_ready=bool(run and run.combined_export_key)` with `combined_export_ready=bool(run and run.status in _DOWNLOADABLE_STATUSES)` and add `delivery_mode=batch.delivery_mode or "everything",`.

(d) `_run_response`: same readiness swap for its `combined_export_ready=bool(run.combined_export_key)` line → `combined_export_ready=run.status in _DOWNLOADABLE_STATUSES`, and add `delivery_counts=run.delivery_counts,`.

(e) `get_batch`: pass the latest run's counts into the detail response — in the final `BatchDetailResponse(**_summary(...).model_dump(), ...)` add `delivery_counts=run.delivery_counts if run else None,`.

(f) `_stream_run_csv`: change signature to accept the mode and use the status gate:

```python
async def _stream_run_csv(
    batch_id: str, run: BatchRun | None, batch_fields: object = None,
    delivery_mode: str = "everything",
) -> StreamingResponse:
```

Replace the `if run is None or not run.combined_export_key:` gate with:

```python
    if run is None or run.status not in _DOWNLOADABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The combined CSV is not ready yet.",
        )
```

and thread the mode into the rebuild:

```python
        data = await run_in_threadpool(
            render_combined_csv, run.user_id, run.child_job_ids or [],
            hidden_fields, delivery_mode,
        )
```

(g) `download_batch` and `download_batch_run`: pass the mode —
`return await _stream_run_csv(batch_id, run, batch.fields, batch.delivery_mode or "everything")`.

- [ ] **Step 5: Run the batch API suites**

Run: `python -m pytest tests/test_batches.py tests/test_batches_read.py tests/test_batch_2b_scheduled.py -v`
Expected: ALL PASS — including pre-existing readiness tests (fixtures that set `combined_export_key` also set run status `done`, so readiness stays True for them; if any fixture has a non-terminal run + key, update THAT test's expectation to the new honest semantics and say so in the commit message).

- [ ] **Step 6: Commit**

```bash
git add src/api/schemas.py src/api/routes/batches.py tests/test_batches.py tests/test_batches_read.py
git commit -m "feat(batch): persist delivery_mode + status-based download readiness + mode-aware rebuild"
```

---

### Task 6: Paginated combined-leads endpoints

**Files:**
- Modify: `src/api/routes/batches.py` (new endpoints + shared helper)
- Modify: `src/api/schemas.py` (leads page schemas)
- Test: Create `tests/test_batch_leads_endpoint.py`

**Interfaces:**
- Consumes: `_COMBINED_SQL` / `_DELIVERY_COUNTS_SQL` binds (Task 2), `_owned_batch`, `_run_for`, `_DOWNLOADABLE_STATUSES` (Task 5).
- Produces: `GET /batches/{batch_id}/leads` and `GET /batches/{batch_id}/runs/{run_id}/leads` returning `BatchLeadsPage`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_batch_leads_endpoint.py`. Reuse `tests/test_batches_read.py`'s fixture pattern (client + `db: AsyncSession` + `starter_user`/`starter_token`; also its second-user fixture for tenant isolation — read that file and mirror its exact fixture names):

```python
"""GET /batches/{id}/leads + /runs/{run_id}/leads — the in-app combined view.

DB-backed: the endpoints run the same combined SQL as the CSV on the async RLS
session, so tenant isolation, the ready-gate, pagination determinism, mode
filtering, and hidden-field blanking are all proven against real Postgres.
"""
import uuid
from datetime import UTC, datetime

import pytest_asyncio

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def overlap_batch(db, starter_user):
    """overlaps_only batch, done run, 1 overlap + 1 pk singleton + 1 no-parcel."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Leads",
        state="WA", fields=[], enrichment=[], schedule={}, deliver={},
        status="active", delivery_mode="overlaps_only",
    )
    db.add(batch)
    await db.flush()
    jobs = []
    for rt in ("probate", "tax_delinquent"):
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=starter_user.id, batch_id=batch.id,
            name=f"c-{rt}", county="pierce", state="WA", record_type=rt,
            fields=[], enrichment=[], schedule={}, deliver={},
        )
        db.add(cfg)
        await db.flush()
        job = Job(id=str(uuid.uuid4()), user_id=starter_user.id,
                  scraper_config_id=cfg.id, status="done", trigger="batch")
        db.add(job)
        await db.flush()
        jobs.append(job)
    for job, party, pk in (
        (jobs[0], "OVERLAP", "WA|pierce|0000000001"),
        (jobs[1], "OVERLAP", "WA|pierce|0000000001"),
        (jobs[0], "SINGLETON", "WA|pierce|0000000002"),
        (jobs[1], "NOPARCEL", None),
    ):
        db.add(Result(
            id=str(uuid.uuid4()), user_id=starter_user.id, job_id=job.id,
            date_recorded="06/01/2026", party_name=party, property_key=pk,
        ))
    run = BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
        status="done", child_job_ids=[j.id for j in jobs],
    )
    db.add(run)
    await db.commit()
    return batch, run


class TestBatchLeads:
    async def test_overlaps_only_page(self, client, starter_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["delivery_mode"] == "overlaps_only"
        assert [l["party_name"] for l in body["leads"]] == ["OVERLAP"]
        assert body["leads"][0]["overlap_count"] == 2
        assert set(body["leads"][0]["matched_record_types"]) == {
            "probate", "tax_delinquent",
        }
        assert body["counts"] == {
            "leads_total": 3, "overlaps_delivered": 1,
            "singletons_suppressed": 1, "unmatchable_no_parcel": 1,
        }
        assert body["total"] == 1  # overlaps_only => total = overlaps

    async def test_run_scoped_variant(self, client, starter_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/runs/{run.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_pagination_deterministic(self, client, starter_token, db,
                                            starter_user, overlap_batch):
        batch, run = overlap_batch
        # Flip mode to everything so 3 rows paginate.
        batch_row = await db.get(ScraperBatch, batch.id)
        batch_row.delivery_mode = "everything"
        await db.commit()
        p1 = await client.get(
            f"/batches/{batch.id}/leads?page=1&page_size=2",
            headers=_auth(starter_token),
        )
        p2 = await client.get(
            f"/batches/{batch.id}/leads?page=2&page_size=2",
            headers=_auth(starter_token),
        )
        names = [l["party_name"] for l in p1.json()["leads"]] + [
            l["party_name"] for l in p2.json()["leads"]
        ]
        assert len(names) == 3
        assert names[0] == "OVERLAP"  # overlap-first ordering
        assert len(set(names)) == 3  # no dup/missing rows across pages
        assert p1.json()["total"] == 3

    async def test_not_ready_while_running_404(self, client, starter_token, db,
                                               starter_user):
        batch = ScraperBatch(
            id=str(uuid.uuid4()), user_id=starter_user.id, name="R",
            state="WA", fields=[], enrichment=[], schedule={}, deliver={},
            status="active",
        )
        db.add(batch)
        await db.flush()
        db.add(BatchRun(
            id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
            status="running", child_job_ids=[],
        ))
        await db.commit()
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 404

    async def test_tenant_isolation(self, client, other_user_token, overlap_batch):
        batch, _ = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(other_user_token)
        )
        assert resp.status_code == 404
```

(If `tests/test_batches_read.py` names its second-user token fixture differently, use that exact name.)

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_batch_leads_endpoint.py -v`
Expected: FAIL 404/405 — routes don't exist.

- [ ] **Step 3: Schemas** — append to `src/api/schemas.py` after `BatchDetailResponse`:

```python
class BatchLeadRow(BaseModel):
    """One combined-view lead — mirrors the combined CSV's columns."""

    id: str
    date_recorded: str | None = None
    party_name: str | None = None
    parcel_id: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None
    phone: str | None = None
    phone_type: str | None = None
    email: str | None = None
    matched_record_types: list[str] = Field(default_factory=list)
    overlap_count: int = 1
    source_counties: list[str] = Field(default_factory=list)
    lead_subtype: str | None = None


class BatchLeadsPage(BaseModel):
    """A page of the combined batch view + live honest counts."""

    leads: list[BatchLeadRow]
    counts: BatchDeliveryCounts
    delivery_mode: str
    page: int
    page_size: int
    total: int  # rows in the CURRENT mode (overlaps_only => overlaps_delivered)
```

- [ ] **Step 4: Endpoints** — append to `src/api/routes/batches.py` (after the run-download endpoint). Imports to add at the top of the file: `from fastapi import Query, Response`, `from sqlalchemy import text`, `from datetime import UTC, datetime`, `from src.api.schemas import BatchDeliveryCounts, BatchLeadRow, BatchLeadsPage`, `from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year`, `from src.utils.crypto import decrypt_field`, `from src.utils.lead_export import resolve_hidden_output_fields`, `from src.workers.batch_export import _COMBINED_SQL, _DELIVERY_COUNTS_SQL` (worker module import here is read-only SQL constants — it does not pull the Celery app; `batch_export` imports `src.workers` lazily only inside functions. Verify with `python -c "from src.workers.batch_export import _COMBINED_SQL"` — if that import drags Celery in, move the two SQL constants + `EXPORT_CAP` to a new leaf module `src/workers/batch_sql.py` and import from there in BOTH places):

```python
# ─── Combined leads view (in-app "one list") ─────────────────────────────────

async def _leads_page(
    db: AsyncSession,
    batch: ScraperBatch,
    run: BatchRun | None,
    page: int,
    page_size: int,
    response: Response,
) -> BatchLeadsPage:
    """Shared body for the latest-run and run-scoped leads endpoints. Caller has
    verified batch ownership and (for the run-scoped variant) run membership.

    Same SQL, mode, and hidden-field semantics as the CSV rebuild, on the async
    RLS session (precedent: per-job results + segments return decrypted contacts
    through get_rls_db). Counts are LIVE (current mode) — never the stored
    snapshot, which reflects the mode at finalize time (Codex P2).
    """
    if run is None or run.status not in _DOWNLOADABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The combined lead list is not ready yet.",
        )
    response.headers["Cache-Control"] = "no-store"  # decrypted PII — never cache
    delivery_mode = batch.delivery_mode or "everything"
    job_ids = [str(j) for j in (run.child_job_ids or [])]
    tax_bind = tax_cap_min_year(datetime.now(UTC).date())

    counts_row = (await db.execute(
        text(_DELIVERY_COUNTS_SQL),
        {"uid": run.user_id, "job_ids": job_ids, TAX_CAP_BIND: tax_bind},
    )).one() if job_ids else None
    counts = BatchDeliveryCounts(
        leads_total=int(counts_row.leads_total) if counts_row else 0,
        overlaps_delivered=int(counts_row.overlaps_delivered) if counts_row else 0,
        singletons_suppressed=int(counts_row.singletons_suppressed) if counts_row else 0,
        unmatchable_no_parcel=int(counts_row.unmatchable_no_parcel) if counts_row else 0,
    )
    total = (
        counts.overlaps_delivered
        if delivery_mode == "overlaps_only"
        else counts.leads_total
    )

    rows = []
    if job_ids:
        result = await db.execute(
            text(_COMBINED_SQL),
            {
                "uid": run.user_id,
                "job_ids": job_ids,
                "limit": page_size,
                "offset": (page - 1) * page_size,
                "overlaps_only": delivery_mode == "overlaps_only",
                TAX_CAP_BIND: tax_bind,
            },
        )
        rows = result.fetchall()

    hidden = resolve_hidden_output_fields(batch.fields)
    leads = []
    for r in rows:
        data = dict(r._mapping)
        data["id"] = str(data["id"])
        data["phone"] = decrypt_field(data["phone"]) if data.get("phone") else None
        data["email"] = decrypt_field(data["email"]) if data.get("email") else None
        # Honor the batch's output-field visibility exactly like the CSV
        # (of the hideable set, only mailing_address is a combined-view column).
        if "mailing_address" in hidden:
            data["mailing_address"] = None
        data["matched_record_types"] = list(data.get("matched_record_types") or [])
        data["source_counties"] = list(data.get("source_counties") or [])
        leads.append(BatchLeadRow(**data))

    return BatchLeadsPage(
        leads=leads,
        counts=counts,
        delivery_mode=delivery_mode,
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/{batch_id}/leads", response_model=BatchLeadsPage)
async def list_batch_leads(
    batch_id: str,
    request: Request,
    response: Response,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_rls_db),
) -> BatchLeadsPage:
    """The combined (deduped, overlap-first, mode-filtered) lead list of the
    LATEST run — the in-app equivalent of the combined CSV."""
    await rate_limit(request, zone="general", identifier=current_user.id)
    batch = await _owned_batch(db, batch_id, current_user.id)
    run = await _run_for(db, batch_id, current_user.id)
    return await _leads_page(db, batch, run, page, page_size, response)


@router.get("/{batch_id}/runs/{run_id}/leads", response_model=BatchLeadsPage)
async def list_batch_run_leads(
    batch_id: str,
    run_id: str,
    request: Request,
    response: Response,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_rls_db),
) -> BatchLeadsPage:
    """Run-scoped combined lead list (2B history parity with the CSV download)."""
    await rate_limit(request, zone="general", identifier=current_user.id)
    batch = await _owned_batch(db, batch_id, current_user.id)
    run = (
        await db.execute(
            select(BatchRun).where(
                BatchRun.id == run_id,
                BatchRun.batch_id == batch_id,  # run must belong to THIS batch
                BatchRun.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return await _leads_page(db, batch, run, page, page_size, response)
```

- [ ] **Step 5: Run the endpoint tests**

Run: `python -m pytest tests/test_batch_leads_endpoint.py -v`
Expected: ALL PASS. If asyncpg rejects the `:overlaps_only` boolean or `:job_ids` array binds, the failure appears here — fix by casting in SQL (`CAST(:overlaps_only AS boolean)`), never by switching the endpoint to the system session.

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/batches.py src/api/schemas.py tests/test_batch_leads_endpoint.py
git commit -m "feat(batch): paginated combined-leads endpoints for the in-app one-list view"
```

---

### Task 7: Full verification, spec amendment, journal, Codex gate

**Files:**
- Modify: `docs/superpowers/specs/2026-07-01-batch-overlaps-delivery-design.md` (§7 ready-marker amendment)
- Modify: `tasks/todo.md` (plan checkboxes + review section, per repo working instructions)
- Modify: `docs/BUILD_JOURNAL.md` (append session entry)

- [ ] **Step 1: Amend the spec** — in §7, replace the "always builds + uploads … always sets combined_export_key" bullet with the implemented truth:

```markdown
- `finalize_batch_run`: always finalizes with honest `delivery_counts`; uploads to
  R2 only when there are rows (the object is an ops artifact — downloads rebuild
  from the DB). **Readiness is status-based** (`done`/`partial`), not
  key-based — this is the ready-marker-path fix for Bug B: a zero-row
  overlaps_only run is downloadable (headers-only CSV) and emails its honest
  empty-state summary.
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -x -q`
Expected: ALL PASS (full suite against `TEST_DATABASE_URL`). Fix any regression before proceeding — never skip/xfail to get green.

- [ ] **Step 3: Lint/type gate** — this repo has no tsc/eslint; run what exists:

Run: `python -m ruff check src tests 2>/dev/null || echo "ruff not configured"`
If ruff isn't configured, state that explicitly in the review section instead of claiming a lint pass.

- [ ] **Step 4: Security self-review checklist** (Master Review §14 scope for this diff — record answers in `tasks/todo.md` review section):
- Every new query carries `user_id` (`_COMBINED_SQL`/`_DELIVERY_COUNTS_SQL` binds `:uid`; `/leads` uses `_owned_batch` + run membership + RLS session).
- No PII in logs (counts are integers; summary messages contain no names/contacts).
- `/leads` is rate-limited, `no-store`, and 404s for non-owners and non-ready runs.
- No new secrets; no user-supplied URLs; CSV output unchanged through `sanitize_for_csv` path (`build_overlap_export_row`).

- [ ] **Step 5: Update `tasks/todo.md`** — append the completed checkbox list + a Review section summarizing: the three bugs fixed, the mode semantics, the readiness change, files touched, test counts.

- [ ] **Step 6: Append `docs/BUILD_JOURNAL.md` entry** — built/tried/failed/succeeded honestly, including the Codex quota outage and the spec §7 amendment.

- [ ] **Step 7: Commit docs**

```bash
git add docs/superpowers/specs/2026-07-01-batch-overlaps-delivery-design.md tasks/todo.md docs/BUILD_JOURNAL.md
git commit -m "docs(batch): spec §7 ready-marker amendment + todo review + build journal"
```

- [ ] **Step 8: Codex review gate (MANDATORY — NO-GO on Critical/High)**

```bash
codex review "Review this diff: batch overlaps-first delivery. Focus: (1) the reworked _COMBINED_SQL bucket/overlap semantics and its binds, (2) finalize_batch_run status/lease guards still correct with delivery_counts added, (3) the readiness semantics change from combined_export_key to run status — any consumer missed?, (4) /leads endpoints: tenant isolation, PII, pagination correctness, (5) migration 078 deploy safety." --base main -c 'model_reasoning_effort="high"'
```

Reconcile per `.claude/rules/codex-collaboration.md`: consensus = higher severity; Codex-only findings adopted unless docs contradict; Critical/High from either reviewer = NO-GO until fixed. Re-run after fixes until clean.

- [ ] **Step 9: Push the branch** (additive only — never touch other branches):

```bash
git push -u origin chore/xcheck-session
```

Then report to the user with proof (test output + the empty-state download demo) and propose the PR.
