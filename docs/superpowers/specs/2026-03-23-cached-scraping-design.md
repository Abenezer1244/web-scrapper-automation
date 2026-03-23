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
    record_type VARCHAR(32),          -- NULL = unfiltered (all types)
    date_recorded VARCHAR(32),
    party_name VARCHAR(512),
    heirs TEXT,
    legal_description TEXT,
    parcel_id VARCHAR(64),
    property_address VARCHAR(512),
    mailing_address VARCHAR(512),
    enrichment_data JSONB,
    record_hash VARCHAR(32) NOT NULL UNIQUE,  -- dedup key
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_county_records_county_state ON county_records(county, state);
CREATE INDEX idx_county_records_batch_date ON county_records(batch_date);
CREATE INDEX idx_county_records_scraped_at ON county_records(scraped_at);
CREATE INDEX idx_county_records_hash ON county_records(record_hash);
```

### New table: `user_record_views`

Per-user tracking of when they last viewed results for a given scraper config.

```sql
CREATE TABLE user_record_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    scraper_config_id UUID NOT NULL REFERENCES scraper_configs(id),
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, scraper_config_id)
);
```

### "New" record logic

```sql
-- Records are "new" for a user when:
--   county_records.scraped_at > user_record_views.last_viewed_at
--
-- First visit (no user_record_views row): all records are "new"
-- After viewing: last_viewed_at is updated to NOW()
-- Next visit: only records scraped after last visit are "new"
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
   a. Check if already scraped today (`batch_date = today`) — skip if yes
   b. Check if county has zero records in `county_records` — if so, do 90-day backfill
   c. Otherwise, scrape only yesterday's date (1-day window)
3. Create a system job (no user_id, trigger="system_daily")
4. Scraper runs as normal (EagleWeb/AcclaimWeb/AI template)
5. Deduplicate via `record_hash` — INSERT ON CONFLICT DO NOTHING
6. Records go into `county_records` (not `results`)

**Concurrency:** 39 WA counties x ~2 min = ~20 min with 4 workers. Runs overnight, zero daytime impact.

**Error handling:** If a county fails, log it and continue. Watchdog will retry on next cycle. A county_connector `last_checked` field tracks health.

### Backfill

On first run (or when a new county connector is added), the system detects zero records for that county and runs a 90-day backfill instead of a 1-day scrape. This populates the cache so users get instant results from day one.

---

## User-Facing Flow

### Instant results (primary path)

When a user clicks "Run" or views results for their scraper config:

1. **Check cache freshness:** Query `county_records` for the matching county/state
   - If `batch_date >= today - 1` (scraped within last 24h): serve from cache
   - If stale: queue a background scrape AND serve existing cached data with a "updating..." indicator
2. **Query records:** Filter `county_records` by county, state, and record_type keywords (same doc type matching as template scrapers)
3. **Compute "new" flag:** LEFT JOIN `user_record_views` on (user_id, scraper_config_id)
   - If no row exists: all records are "new" (first visit)
   - If row exists: records where `scraped_at > last_viewed_at` are "new"
4. **Update tracking:** UPSERT `user_record_views` SET `last_viewed_at = NOW()`
5. **Return response** with `is_new` per record and `new_count` summary

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
    "cache_age": "2h",        // time since last scrape
    "page": 1,
    "page_size": 50,
    "items": [
        {
            "id": "rec-uuid",
            "date_recorded": "03/22/2026",
            "party_name": "SMITH, JOHN",
            "heirs": "SMITH, JANE",
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

### On-demand jobs (secondary path, kept as-is)

The existing `/jobs` POST endpoint continues to work for:
- Custom date ranges (user picks specific from/to)
- Manual force re-scrape
- Non-cached scenarios

These still create `jobs` + `results` rows per user. No change to this flow.

---

## Record Type Filtering

`county_records` stores ALL document types scraped from a county (we scrape "Search All Types"). When a user's config specifies `record_type: "probate"`, the query filters using the same keyword matching as the template scrapers:

```python
# Probate keywords (same as _DOC_TYPE_MAP in eagleweb.py)
PROBATE_KEYWORDS = ["PROBATE", "LETTERS TESTAMENTARY", ...]

# SQL: WHERE doc_type_matches(record, user_config.record_type)
```

This means one nightly scrape per county serves ALL record types. A probate user and a foreclosure user both read from the same `county_records` rows — just filtered differently.

---

## Export Changes

When a user exports (CSV/Excel/JSON), the exporter reads from `county_records` instead of `results` for cached data. The `is_new` flag is included as a column in the export.

---

## What Changes

| Component | Change | Files |
|---|---|---|
| DB migration | Add `county_records` + `user_record_views` tables | `alembic/versions/xxx_add_county_records.py` |
| SQLAlchemy models | Add `CountyRecord` + `UserRecordView` models | `src/db/models.py` |
| Beat scheduler | Add `scrape_county_daily` task | `src/workers/scheduler.py` |
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
