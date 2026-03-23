# Cached Scraping + "New" Record Badges

**Date:** 2026-03-23
**Status:** Approved
**Problem:** Each user request triggers a 10-30 min scrape job. With 100 users, the queue backs up for hours.
**Solution:** Pre-scrape counties daily, serve cached results instantly. Track per-user "new" badges.

---

## Data Model

### New table: `county_records`

Shared source of truth. One row per unique record per county. No user_id — shared across all users.

```sql
CREATE TABLE county_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    county VARCHAR(64) NOT NULL,
    state VARCHAR(4) NOT NULL,
    doc_type VARCHAR(128),              -- raw doc type from scraper (e.g. "DEED OF TRUST")
    date_recorded VARCHAR(32),
    party_name VARCHAR(512),
    heirs TEXT,
    legal_description TEXT,
    parcel_id VARCHAR(64),
    property_address VARCHAR(512),
    mailing_address VARCHAR(512),
    enrichment_data JSONB,
    record_hash VARCHAR(32) NOT NULL UNIQUE,  -- MD5 of (county, state, party_name, date_recorded, legal_description)
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot query path: county + state + scraped_at (for "new" badge join)
CREATE INDEX idx_county_records_county_state_scraped ON county_records(county, state, scraped_at);
CREATE INDEX idx_county_records_batch_date ON county_records(batch_date);
CREATE INDEX idx_county_records_hash ON county_records(record_hash);
-- Search index for ?q= queries (trigram for ILIKE performance)
CREATE INDEX idx_county_records_party_trgm ON county_records USING gin (party_name gin_trgm_ops);
CREATE INDEX idx_county_records_address_trgm ON county_records USING gin (property_address gin_trgm_ops);
```

**`record_hash` definition:** MD5 of `f"{county}|{state}|{party_name}|{date_recorded}|{legal_description}"`. Excludes `scraped_at` and `batch_date` so re-scraping the same record deduplicates correctly via `INSERT ON CONFLICT DO NOTHING`.

**RLS policy:** `county_records` has a permissive SELECT policy for all authenticated users (shared read). INSERT/UPDATE/DELETE restricted to service role only (nightly worker uses service key).

```sql
ALTER TABLE county_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY county_records_read ON county_records FOR SELECT TO authenticated USING (true);
CREATE POLICY county_records_write ON county_records FOR ALL TO service_role USING (true);
```

### New table: `user_record_views`

Per-user tracking of when they last viewed results for a given scraper config.

```sql
CREATE TABLE user_record_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scraper_config_id UUID NOT NULL REFERENCES scraper_configs(id) ON DELETE CASCADE,
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, scraper_config_id)
);

ALTER TABLE user_record_views ENABLE ROW LEVEL SECURITY;
CREATE POLICY urv_user_only ON user_record_views FOR ALL TO authenticated
    USING (user_id = auth.uid());
```

### "New" badge logic — atomic CTE

Read old `last_viewed_at` and update to `NOW()` in a single atomic operation. This prevents race conditions from double-clicks or multiple tabs.

```sql
WITH old_view AS (
    INSERT INTO user_record_views (user_id, scraper_config_id, last_viewed_at)
    VALUES (:user_id, :config_id, NOW())
    ON CONFLICT (user_id, scraper_config_id)
    DO UPDATE SET last_viewed_at = NOW()
    RETURNING (
        SELECT last_viewed_at FROM user_record_views
        WHERE user_id = :user_id AND scraper_config_id = :config_id
    ) AS previous_viewed_at
)
SELECT cr.*,
    CASE WHEN cr.scraped_at > COALESCE(ov.previous_viewed_at, '1970-01-01') THEN true ELSE false END AS is_new
FROM county_records cr
CROSS JOIN old_view ov
WHERE cr.county = :county AND cr.state = :state
ORDER BY cr.scraped_at DESC
LIMIT :page_size OFFSET :offset;
```

### Existing tables: no schema changes

- `jobs` — still used for on-demand/custom scrapes
- `results` — still used for on-demand/custom scrapes
- `scraper_configs` — unchanged
- `county_connectors` — unchanged

---

## Nightly Scrape Flow

### New beat task: `scrape_county_daily`

**Schedule:** 2:00 AM UTC daily

**Logic:**

1. Query all active `county_connectors`
2. For each county:
   a. Check if already scraped today (`batch_date = today` in `county_records`) — skip if yes
   b. **Acquire advisory lock** (`pg_try_advisory_lock(hash(county||state))`) — prevents two workers from backfilling the same county simultaneously
   c. Check if county has zero records in `county_records` — if so, do 90-day backfill
   d. Otherwise, scrape only yesterday's date (1-day window)
3. Create a system job (no user_id, trigger="system_daily")
4. Scraper runs as normal (EagleWeb/AcclaimWeb/AI template)
5. Deduplicate via `record_hash` — `INSERT ON CONFLICT (record_hash) DO NOTHING`
6. Records go into `county_records` (not `results`)
7. Release advisory lock

**Concurrency:** 39 WA counties x ~2 min = ~20 min with 4 workers. Runs overnight, zero daytime impact.

**Error handling:** If a county fails, log it and continue. Watchdog will retry on next cycle. The `county_connectors.last_checked` field tracks health.

### Backfill

On first run (or when a new county connector is added), the system detects zero records for that county and runs a 90-day backfill instead of a 1-day scrape. This populates the cache so users get instant results from day one.

**Enrichment during backfill:** Same as current template scrapers — enrich up to 200 records per job with GIS parcel data. Daily scrapes (small record counts) enrich all records.

### Data retention

Records older than **365 days** are purged weekly by a new beat task `purge_old_records`. This keeps `county_records` bounded. The retention period is configurable via env var `RECORD_RETENTION_DAYS` (default: 365).

---

## User-Facing Flow

### Instant results (primary path)

When a user clicks "Run" or views results for their scraper config:

1. **Check cache freshness:** Query `county_records` for the matching county/state
   - If `batch_date >= today - 1` (scraped within last 24h): serve from cache
   - If stale: queue a background scrape AND serve existing cached data with a `"cache_stale": true` indicator
2. **Query records:** Filter `county_records` by county, state, and doc_type keywords (same `_DOC_TYPE_MAP` matching as template scrapers)
3. **Atomic read+update:** Execute the CTE above to get records with `is_new` flags AND update `last_viewed_at` in one statement
4. **Return response** with `is_new` per record and `new_count` summary

### New API endpoint: `GET /scrapers/{config_id}/records`

Serves cached records instantly. Separate from the existing `/jobs/{job_id}/results` endpoint.

```
GET /scrapers/{config_id}/records?page=1&page_size=50&q=smith

Response:
{
    "config_id": "abc-123",
    "county": "spokane",
    "state": "WA",
    "total": 5200,
    "new_count": 47,
    "cache_age": "2h",
    "cache_stale": false,
    "page": 1,
    "page_size": 50,
    "items": [
        {
            "id": "rec-uuid",
            "date_recorded": "03/22/2026",
            "party_name": "SMITH, JOHN",
            "heirs": "SMITH, JANE",
            "doc_type": "PROBATE",
            "legal_description": "LOT 5 BLK 2",
            "parcel_id": "12345678",
            "property_address": "123 Main St, Spokane WA",
            "mailing_address": "456 Oak Ave, Spokane WA",
            "is_new": true,
            "scraped_at": "2026-03-22T02:15:00Z"
        }
    ]
}
```

**Search (`?q=`):** Uses PostgreSQL trigram `ILIKE` via `gin_trgm_ops` indexes on `party_name` and `property_address`. Also matches `parcel_id` via exact prefix match. Max query length: 100 chars, sanitized via existing `sanitize_search()`.

### On-demand jobs (secondary path, kept as-is)

The existing `/jobs` POST endpoint continues to work for:
- Custom date ranges (user picks specific from/to)
- Manual force re-scrape
- Non-cached scenarios

These still create `jobs` + `results` rows per user. No change to this flow.

---

## Record Type Filtering

`county_records` stores ALL document types with the raw `doc_type` from the scraper. When a user's config specifies `record_type: "probate"`, the query filters using the same keyword matching as the template scrapers:

```python
_DOC_TYPE_MAP = {
    "probate": ["PROBATE", "LETTERS TESTAMENTARY", ...],
    "pre_foreclosure": ["LIS PENDENS", "NOTICE OF TRUSTEE", ...],
    ...
}

# SQL WHERE clause built from keywords:
# WHERE doc_type ILIKE ANY(ARRAY['%PROBATE%', '%LETTERS TESTAMENTARY%', ...])
```

One nightly scrape per county serves ALL record types. A probate user and a foreclosure user both read from the same `county_records` rows — just filtered differently.

---

## Export Changes

When a user exports (CSV/Excel/JSON) from the cached endpoint, the exporter reads from `county_records` instead of `results`. The `is_new` flag is included as a column in the export.

---

## Deployment Order

1. **Run migration** — create `county_records` + `user_record_views` tables, indexes, RLS policies
2. **Deploy app code** — new models, API endpoint, worker task
3. **Enable beat task** — set `ENABLE_DAILY_SCRAPE=true` env var to activate `scrape_county_daily`

This avoids the worker writing to tables that don't exist yet during rolling deploys.

---

## What Changes

| Component | Change | Files |
|---|---|---|
| DB migration | Add `county_records` + `user_record_views` tables + RLS | `alembic/versions/xxx_add_county_records.py` |
| SQLAlchemy models | Add `CountyRecord` + `UserRecordView` models | `src/db/models.py` |
| Beat scheduler | Add `scrape_county_daily` + `purge_old_records` tasks | `src/workers/scheduler.py` |
| Worker tasks | Add `run_daily_county_scrape` task | `src/workers/tasks.py` |
| New API route | `GET /scrapers/{config_id}/records` | `src/api/routes/scrapers.py` |
| API schemas | Add `CachedRecordRow`, `CachedResultsPage` | `src/api/schemas.py` |
| Existing job flow | No change | — |
| Template scrapers | No change | — |
| AI scraper | No change | — |

## What Doesn't Change

- Template scrapers (EagleWeb, AcclaimWeb)
- AI scraper
- Existing `/jobs` endpoints
- Existing `results` table
- Stripe billing
- Email delivery
- Frontend (separate repo — needs `is_new` badge, but that's frontend work)
