"""Pipeline-phase helper bodies, extracted from src/workers/tasks.py.

The single Celery task REGISTRATION (`run_scrape_job`, its `@app.task(...)`
decorator, name= string, and signature) stays in tasks.py unchanged — moving
it would silently break the prod scrape queue. These modules hold the moved
helper functions VERBATIM (no logic changes); run_scrape_job and the other
helpers call them via imports, and tasks.py re-exports them so the historical
`from src.workers.tasks import <name>` import surface stays intact for the
scripts/tests/modules that depend on it.

Module map:
  status.py    — _now, _redis, _publish_log, _set_status, _fail_job,
                 _delivery_download_url, JobUpdateFields, _TERMINAL_STATUSES,
                 _DELIVERY_TOKEN_TTL
  dedup.py     — _extract_tax_fields, _upsert_property_membership,
                 _write_result_property_keys, _TRUSTED_TAX_SOURCES
  enrich.py    — _run_scraper, _reuse_enrichment_for_duplicates,
                 _run_inline_enrichment, _enqueue_skip_trace_rows
  dates.py     — _to_mmddyyyy, _resolve_date_range
"""
