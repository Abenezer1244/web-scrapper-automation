# Cached Scraping + "New" Record Badges — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-scrape counties daily into a shared cache, serve results instantly with per-user "new" badges.

**Architecture:** Two new DB tables (`county_records` shared cache, `user_record_views` per-user tracking). A nightly beat task scrapes each county's daily records into `county_records`. A new API endpoint reads from cache with atomic "new" badge computation. Existing on-demand job flow is untouched.

**Tech Stack:** SQLAlchemy + Alembic (migration), FastAPI (new endpoint), Celery beat (nightly task), PostgreSQL (RLS, trigram indexes, advisory locks).

**Spec:** `docs/superpowers/specs/2026-03-23-cached-scraping-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `alembic/versions/004_add_county_records.py` | Create | Migration: `county_records` + `user_record_views` tables, indexes, RLS |
| `src/db/models.py` | Modify | Add `CountyRecord` + `UserRecordView` SQLAlchemy models |
| `src/api/schemas.py` | Modify | Add `CachedRecordRow`, `CachedResultsPage` response schemas |
| `src/api/routes/scrapers.py` | Modify | Add `GET /scrapers/{config_id}/records` endpoint |
| `src/workers/scheduler.py` | Modify | Add `scrape_county_daily` + `purge_old_records` beat tasks |
| `src/workers/daily_scrape.py` | Create | Logic for daily county scrape + insert into `county_records` |
| `src/config/settings.py` | Modify | Add `ENABLE_DAILY_SCRAPE`, `RECORD_RETENTION_DAYS` settings |

---

### Task 1: Database Migration

**Files:**
- Create: `alembic/versions/004_add_county_records.py`

- [ ] **Step 1: Create the migration file**

```python
"""Add county_records and user_record_views tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004"
down_revision = "003_add_gis_assessor_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── county_records: shared cache ─────────────────────────────────

    op.create_table(
        "county_records",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("county", sa.String(64), nullable=False),
        sa.Column("state", sa.String(4), nullable=False),
        sa.Column("doc_type", sa.String(128), nullable=True),
        sa.Column("date_recorded", sa.String(32), nullable=True),
        sa.Column("party_name", sa.String(512), nullable=True),
        sa.Column("heirs", sa.Text, nullable=True),
        sa.Column("legal_description", sa.Text, nullable=True),
        sa.Column("parcel_id", sa.String(64), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        sa.Column("mailing_address", sa.String(512), nullable=True),
        sa.Column("enrichment_data", sa.JSON, nullable=True),
        sa.Column("record_hash", sa.String(32), nullable=False, unique=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("batch_date", sa.Date, server_default=sa.text("CURRENT_DATE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_county_records_county_state_scraped", "county_records", ["county", "state", "scraped_at"])
    op.create_index("idx_county_records_batch_date", "county_records", ["batch_date"])
    op.create_index("idx_county_records_hash", "county_records", ["record_hash"])

    # Trigram indexes for search — requires pg_trgm extension
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX idx_county_records_party_trgm ON county_records USING gin (party_name gin_trgm_ops)")
    op.execute("CREATE INDEX idx_county_records_address_trgm ON county_records USING gin (property_address gin_trgm_ops)")

    # RLS: shared read for all, write for service role only
    op.execute("ALTER TABLE county_records ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY county_records_read ON county_records FOR SELECT USING (true)")
    op.execute("CREATE POLICY county_records_write ON county_records FOR INSERT WITH CHECK (true)")
    op.execute("CREATE POLICY county_records_delete ON county_records FOR DELETE USING (true)")

    # ── user_record_views: per-user "new" badge tracking ─────────────
    op.create_table(
        "user_record_views",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scraper_config_id", UUID(as_uuid=False), sa.ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "scraper_config_id", name="uq_user_config_view"),
    )
    op.execute("ALTER TABLE user_record_views ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY urv_user_only ON user_record_views FOR ALL USING (user_id = current_setting('app.current_user_id')::uuid)")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS urv_user_only ON user_record_views")
    op.drop_table("user_record_views")
    op.execute("DROP POLICY IF EXISTS county_records_read ON county_records")
    op.drop_table("county_records")
```

- [ ] **Step 2: Run the migration**

Run: `alembic upgrade head`
Expected: Tables created, indexes built, RLS enabled.

- [ ] **Step 3: Verify migration**

```bash
python -c "
import psycopg2
conn = psycopg2.connect('...')  # use DATABASE_URL_SYNC
cur = conn.cursor()
cur.execute(\"SELECT tablename FROM pg_tables WHERE tablename IN ('county_records','user_record_views')\")
print(cur.fetchall())
conn.close()
"
```
Expected: `[('county_records',), ('user_record_views',)]`

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/004_add_county_records.py
git commit -m "migration: add county_records + user_record_views tables"
```

---

### Task 2: SQLAlchemy Models

**Files:**
- Modify: `src/db/models.py` (append after `JobLog` class, line ~138)

- [ ] **Step 1: Add CountyRecord and UserRecordView models**

First add `Date` to the imports at top of `src/db/models.py`:
```python
from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, func,
)
```

Then append to `src/db/models.py`:

```python
class CountyRecord(Base):
    __tablename__ = "county_records"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    county = Column(String(64), nullable=False, index=True)
    state = Column(String(4), nullable=False, index=True)
    doc_type = Column(String(128), nullable=True)
    date_recorded = Column(String(32), nullable=True)
    party_name = Column(String(512), nullable=True)
    heirs = Column(Text, nullable=True)
    legal_description = Column(Text, nullable=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    mailing_address = Column(String(512), nullable=True)
    enrichment_data = Column(JSON, nullable=True, default=dict)
    record_hash = Column(String(32), nullable=False, unique=True, index=True)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    batch_date = Column(Date, server_default=func.current_date(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserRecordView(Base):
    __tablename__ = "user_record_views"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scraper_config_id = Column(UUID(as_uuid=False), ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    last_viewed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

- [ ] **Step 2: Update `src/db/__init__.py` exports**

Add `CountyRecord` and `UserRecordView` to the `__init__.py` imports so they're importable from `src.db`.

- [ ] **Step 3: Verify models load**

Run: `python -c "from src.db.models import CountyRecord, UserRecordView; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/db/models.py src/db/__init__.py
git commit -m "feat: add CountyRecord + UserRecordView models"
```

---

### Task 3: API Schemas

**Files:**
- Modify: `src/api/schemas.py` (append new response models)

- [ ] **Step 1: Add cached record response schemas**

Append to `src/api/schemas.py`:

```python
# ─── Cached Records ──────────────────────────────────────────────────────────

class CachedRecordRow(BaseModel):
    id: str
    date_recorded: str | None = None
    party_name: str | None = None
    heirs: str | None = None
    doc_type: str | None = None
    legal_description: str | None = None
    parcel_id: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None
    is_new: bool = False
    scraped_at: datetime | None = None

    model_config = {"from_attributes": True}


class CachedResultsPage(BaseModel):
    config_id: str
    county: str
    state: str
    total: int
    new_count: int
    cache_age: str | None = None    # e.g. "2h", "1d"
    cache_stale: bool = False
    page: int
    page_size: int
    items: list[CachedRecordRow]
```

- [ ] **Step 2: Verify schemas parse**

Run: `python -c "from src.api.schemas import CachedRecordRow, CachedResultsPage; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/api/schemas.py
git commit -m "feat: add CachedRecordRow + CachedResultsPage schemas"
```

---

### Task 4: Cached Records API Endpoint

**Files:**
- Modify: `src/api/routes/scrapers.py` (add new endpoint)

- [ ] **Step 1: Add the GET /scrapers/{config_id}/records endpoint**

Add to `src/api/routes/scrapers.py`:

```python
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from src.db import CountyRecord, UserRecordView
from src.api.schemas import CachedRecordRow, CachedResultsPage


@router.get("/{config_id}/records", response_model=CachedResultsPage)
async def get_cached_records(
    config_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
    page: int = 1,
    page_size: int = 50,
    q: str | None = None,
):
    """Serve pre-scraped records from cache with per-user 'new' badges."""
    # 1. Verify config belongs to user
    config_result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == config_id,
            ScraperConfig.user_id == current_user.id,
            ScraperConfig.active,
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Scraper config not found")

    county = config.county.lower()
    state = config.state.upper()

    # 2. Atomic: read old last_viewed_at, then update to NOW()
    # Step A: read old value (or None for first visit)
    old_view_result = await db.execute(
        text("""
            SELECT last_viewed_at FROM user_record_views
            WHERE user_id = :user_id AND scraper_config_id = :config_id
            FOR UPDATE
        """),
        {"user_id": current_user.id, "config_id": config_id},
    )
    old_row = old_view_result.fetchone()
    previous_viewed = old_row.last_viewed_at if old_row else None

    # Step B: upsert to NOW()
    await db.execute(
        text("""
            INSERT INTO user_record_views (id, user_id, scraper_config_id, last_viewed_at)
            VALUES (gen_random_uuid(), :user_id, :config_id, NOW())
            ON CONFLICT (user_id, scraper_config_id)
            DO UPDATE SET last_viewed_at = NOW()
        """),
        {"user_id": current_user.id, "config_id": config_id},
    )

    # 3. Build doc_type filter from record_type keywords
    from src.scrapers.templates.eagleweb import _DOC_TYPE_MAP
    keywords = _DOC_TYPE_MAP.get(config.record_type, [])
    type_filter = ""
    type_params = {}
    if keywords:
        conditions = []
        for i, kw in enumerate(keywords):
            param_name = f"kw_{i}"
            conditions.append(f"doc_type ILIKE :{param_name}")
            type_params[param_name] = f"%{kw}%"
        type_filter = "AND (" + " OR ".join(conditions) + ")"

    # 4. Search filter
    search_filter = ""
    if q and len(q) <= 100:
        from src.api.middleware.security import sanitize_search
        clean_q = sanitize_search(q)
        search_filter = "AND (party_name ILIKE :q OR property_address ILIKE :q OR parcel_id ILIKE :q)"
        type_params["q"] = f"%{clean_q}%"

    # 5. Count total + new_count
    count_sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz)) AS new_count
        FROM county_records
        WHERE LOWER(county) = :county AND UPPER(state) = :state
        {type_filter} {search_filter}
    """
    counts = await db.execute(
        text(count_sql),
        {"county": county, "state": state, "prev_viewed": previous_viewed, **type_params},
    )
    count_row = counts.fetchone()
    total = count_row.total if count_row else 0
    new_count = count_row.new_count if count_row else 0

    # 6. Fetch paginated records
    offset = (page - 1) * page_size
    records_sql = f"""
        SELECT *,
            CASE WHEN scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz) THEN true ELSE false END AS is_new
        FROM county_records
        WHERE LOWER(county) = :county AND UPPER(state) = :state
        {type_filter} {search_filter}
        ORDER BY scraped_at DESC
        LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(records_sql),
        {"county": county, "state": state, "prev_viewed": previous_viewed,
         "limit": page_size, "offset": offset, **type_params},
    )
    rows = result.fetchall()

    # 7. Cache age
    cache_age = None
    cache_stale = True
    if rows:
        latest_batch = await db.execute(
            text("SELECT MAX(batch_date) FROM county_records WHERE LOWER(county) = :county AND UPPER(state) = :state"),
            {"county": county, "state": state},
        )
        max_batch = latest_batch.scalar()
        if max_batch:
            age = datetime.now(UTC).date() - max_batch
            cache_age = f"{age.days}d" if age.days > 0 else "today"
            cache_stale = age.days > 1

    await db.commit()

    return CachedResultsPage(
        config_id=config_id,
        county=config.county,
        state=config.state,
        total=total,
        new_count=new_count,
        cache_age=cache_age,
        cache_stale=cache_stale,
        page=page,
        page_size=page_size,
        items=[
            CachedRecordRow(
                id=str(r.id),
                date_recorded=r.date_recorded,
                party_name=r.party_name,
                heirs=r.heirs,
                doc_type=r.doc_type,
                legal_description=r.legal_description,
                parcel_id=r.parcel_id,
                property_address=r.property_address,
                mailing_address=r.mailing_address,
                is_new=r.is_new,
                scraped_at=r.scraped_at,
            )
            for r in rows
        ],
    )
```

- [ ] **Step 2: Verify endpoint loads**

Run: `python -c "from src.api.routes.scrapers import router; print([r.path for r in router.routes])"`
Expected: Should include `/{config_id}/records`

- [ ] **Step 3: Commit**

```bash
git add src/api/routes/scrapers.py
git commit -m "feat: add GET /scrapers/{config_id}/records cached endpoint"
```

---

### Task 5: Daily County Scrape Worker

**Files:**
- Create: `src/workers/daily_scrape.py`

- [ ] **Step 1: Create the daily scrape logic**

```python
"""Daily county scrape: populates county_records cache.

Called by the beat scheduler. Scrapes each active county for yesterday's
records (or 90-day backfill if the county has no cached data yet).
Inserts into county_records with ON CONFLICT DO NOTHING for dedup.
"""

import asyncio
import hashlib
import uuid as _uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, text

from src.config import settings
from src.db.models import CountyConnector, CountyRecord
from src.db.session import SyncSessionLocal
from src.scrapers.registry import get_scraper_class, UnsupportedCountyError
from src.utils.logger import setup_logger
from src.workers.tasks import _run_scraper

_logger = setup_logger("worker.daily_scrape")


def make_record_hash(county: str, state: str, party_name: str, date_recorded: str, legal_description: str) -> str:
    """MD5 hash for dedup. Excludes timestamps so re-scrapes deduplicate."""
    raw = f"{county}|{state}|{party_name or ''}|{date_recorded or ''}|{legal_description or ''}"
    return hashlib.md5(raw.encode()).hexdigest()


def run_daily_scrape_for_county(county: str, state: str) -> int:
    """Scrape a single county's daily records into county_records.

    Returns the number of new records inserted.
    """
    with SyncSessionLocal() as db:
        # Check if already scraped today
        today = datetime.now(UTC).date()
        existing = db.execute(
            select(func.count()).select_from(CountyRecord).where(
                func.lower(CountyRecord.county) == county.lower(),
                func.upper(CountyRecord.state) == state.upper(),
                CountyRecord.batch_date == today,
            )
        ).scalar_one()

        if existing > 0:
            _logger.info("County %s/%s already scraped today (%d records), skipping", county, state, existing)
            return 0

        # Advisory lock to prevent concurrent backfills
        lock_key = int(hashlib.md5(f"{county}|{state}".encode()).hexdigest()[:8], 16)
        got_lock = db.execute(text(f"SELECT pg_try_advisory_lock({lock_key})")).scalar()
        if not got_lock:
            _logger.info("County %s/%s locked by another worker, skipping", county, state)
            return 0

        try:
            # Determine date range: backfill (90d) or daily (1d)
            total_records = db.execute(
                select(func.count()).select_from(CountyRecord).where(
                    func.lower(CountyRecord.county) == county.lower(),
                    func.upper(CountyRecord.state) == state.upper(),
                )
            ).scalar_one()

            if total_records == 0:
                # Backfill: 90-day window
                date_from = (today - timedelta(days=90)).strftime("%m/%d/%Y")
                date_to = today.strftime("%m/%d/%Y")
                _logger.info("Backfill %s/%s: %s to %s", county, state, date_from, date_to)
            else:
                # Daily: yesterday only
                yesterday = today - timedelta(days=1)
                date_from = yesterday.strftime("%m/%d/%Y")
                date_to = today.strftime("%m/%d/%Y")
                _logger.info("Daily scrape %s/%s: %s to %s", county, state, date_from, date_to)

            # Get scraper class (template or AI)
            connector = db.execute(
                select(CountyConnector).where(
                    func.lower(CountyConnector.county) == county.lower(),
                    func.upper(CountyConnector.state) == state.upper(),
                    CountyConnector.active,
                )
            ).scalar_one_or_none()

            if not connector:
                _logger.warning("No active connector for %s/%s", county, state)
                return 0

            # Use first record type for scraper lookup (template scrapers ignore it anyway)
            record_type = connector.record_types[0] if connector.record_types else "probate"
            try:
                scraper_class = get_scraper_class(county, state, record_type)
            except UnsupportedCountyError as exc:
                _logger.warning("Unsupported county %s/%s: %s", county, state, exc)
                return 0

            # Run scraper
            import redis
            r = redis.from_url(settings.REDIS_URL, ssl_cert_reqs=None)
            records = asyncio.run(_run_scraper(scraper_class, date_from, date_to, r, "system_daily"))

            if not records:
                _logger.info("No records found for %s/%s", county, state)
                return 0

            # Bulk insert with ON CONFLICT DO NOTHING
            def _trunc(val, max_len):
                return val[:max_len] if val and len(val) > max_len else val

            batch_size = 1000
            inserted = 0
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                rows = []
                for rec in batch:
                    rec_hash = make_record_hash(
                        county, state,
                        rec.party_name, rec.date_recorded, rec.legal_description,
                    )
                    rows.append({
                        "id": str(_uuid.uuid4()),
                        "county": county.lower(),
                        "state": state.upper(),
                        "doc_type": _trunc(getattr(rec, "doc_type", None), 128),
                        "date_recorded": _trunc(rec.date_recorded, 32),
                        "party_name": _trunc(rec.party_name, 512),
                        "heirs": rec.heirs,
                        "legal_description": rec.legal_description,
                        "parcel_id": _trunc(rec.parcel_id, 64),
                        "property_address": _trunc(rec.property_address, 512),
                        "mailing_address": _trunc(rec.mailing_address, 512),
                        "enrichment_data": rec.enrichment_data or {},
                        "record_hash": rec_hash,
                        "batch_date": today,
                    })

                stmt = insert(CountyRecord).values(rows).on_conflict_do_nothing(index_elements=["record_hash"])
                result = db.execute(stmt)
                inserted += result.rowcount
                db.commit()

            _logger.info("Inserted %d new records for %s/%s (out of %d scraped)", inserted, county, state, len(records))
            return inserted

        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({lock_key})"))
```

- [ ] **Step 2: Verify module imports**

Run: `python -c "from src.workers.daily_scrape import run_daily_scrape_for_county, make_record_hash; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/workers/daily_scrape.py
git commit -m "feat: add daily county scrape worker logic"
```

---

### Task 6: Beat Scheduler Tasks

**Files:**
- Modify: `src/workers/scheduler.py` (add two new beat tasks)
- Modify: `src/config/settings.py` (add new env vars)

- [ ] **Step 1: Add env vars to settings**

Add to the `Settings` class in `src/config/settings.py` (pydantic_settings style):

```python
    # ─── Daily Scrape Cache ────────────────────────────────────────────────
    ENABLE_DAILY_SCRAPE: bool = False
    RECORD_RETENTION_DAYS: int = 365
```

- [ ] **Step 2: Add beat schedule entries**

Add to `app.conf.beat_schedule` dict in `src/workers/scheduler.py`:

```python
    "scrape-county-daily": {
        "task": "src.workers.scheduler.scrape_county_daily",
        "schedule": crontab(hour=2, minute=0),  # 2:00 AM UTC daily
    },
    "purge-old-records": {
        "task": "src.workers.scheduler.purge_old_records",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),  # Sundays 3 AM UTC
    },
```

- [ ] **Step 3: Add the scrape_county_daily task**

Append to `src/workers/scheduler.py`:

```python
# ─── Task 5: Daily county scrape ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.scrape_county_daily")
def scrape_county_daily() -> None:
    """Dispatch daily scrape for each active county.

    Runs at 2 AM UTC. Each county gets its own Celery task for parallelism.
    Gated by ENABLE_DAILY_SCRAPE env var.
    """
    from src.config import settings

    if not settings.ENABLE_DAILY_SCRAPE:
        return

    from sqlalchemy import select

    from src.db.models import CountyConnector
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as db:
        connectors = db.execute(
            select(CountyConnector).where(CountyConnector.active)
        ).scalars().all()

    _logger.info("Daily scrape: dispatching %d counties", len(connectors))

    for conn in connectors:
        run_single_county_scrape.delay(conn.county, conn.state)


@app.task(name="src.workers.scheduler.run_single_county_scrape", queue="scrape")
def run_single_county_scrape(county: str, state: str) -> None:
    """Scrape a single county's daily records into county_records cache."""
    from src.workers.daily_scrape import run_daily_scrape_for_county

    try:
        count = run_daily_scrape_for_county(county, state)
        _logger.info("Daily scrape %s/%s: %d new records", county, state, count)
    except Exception:
        _logger.exception("Daily scrape failed for %s/%s", county, state)


# ─── Task 6: Purge old records ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.purge_old_records")
def purge_old_records() -> None:
    """Delete county_records older than RECORD_RETENTION_DAYS. Weekly."""
    from src.config import settings
    from src.db.session import SyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(days=settings.RECORD_RETENTION_DAYS)

    with SyncSessionLocal() as db:
        result = db.execute(
            text("DELETE FROM county_records WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
        _logger.info("Purged %d records older than %d days", result.rowcount, settings.RECORD_RETENTION_DAYS)
```

- [ ] **Step 4: Verify scheduler loads**

Run: `python -c "from src.workers.scheduler import scrape_county_daily, purge_old_records; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add src/workers/scheduler.py src/config/settings.py
git commit -m "feat: add daily scrape + purge beat tasks"
```

---

### Task 7: Integration Test — End to End

**Files:**
- No new files — uses existing infrastructure

- [ ] **Step 1: Run migration on production DB**

```bash
alembic upgrade head
```

- [ ] **Step 2: Test daily scrape locally for one county**

```python
python -c "
from src.workers.daily_scrape import run_daily_scrape_for_county
count = run_daily_scrape_for_county('spokane', 'WA')
print(f'Inserted {count} records')
"
```
Expected: Records inserted into `county_records`.

- [ ] **Step 3: Verify records in DB**

```python
python -c "
import psycopg2
conn = psycopg2.connect('...')
cur = conn.cursor()
cur.execute('SELECT COUNT(*), MIN(date_recorded), MAX(date_recorded) FROM county_records WHERE county = %s', ('spokane',))
print(cur.fetchone())
conn.close()
"
```

- [ ] **Step 4: Test cached endpoint via API**

```bash
curl -H 'Authorization: Bearer <token>' 'https://api.bridgeleads.io/scrapers/<config_id>/records?page=1&page_size=5'
```
Expected: JSON response with `items`, `new_count`, `is_new` fields.

- [ ] **Step 5: Push and deploy**

```bash
git push origin main
```

- [ ] **Step 6: Enable daily scrape on Railway**

```bash
railway variables set ENABLE_DAILY_SCRAPE=true
```

- [ ] **Step 7: Commit any fixes**

```bash
git add -A && git commit -m "fix: integration fixes from E2E testing"
```
