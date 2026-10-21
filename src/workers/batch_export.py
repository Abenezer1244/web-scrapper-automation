"""Piece 2 Phase 2A.3: batch combined export + delivery.

When all of a batch's child jobs are terminal (done/failed/cancelled), the
completion barrier (scheduler.batch_completion_sweep) claims the run and calls
finalize_batch_run: build ONE combined, deduped, overlap-flagged CSV over the
batch's job_ids (reusing Piece 1's write_lead_csv_with_overlap), upload to R2,
mark the run done/partial, then send one delivery email (best-effort).

Does NOT wait for async skip-trace — contacts (phone/email) fill in later; the
CSV is re-downloadable once they land. The CSV is built on property identity,
which is ready at child-job enrichment.
"""
import io
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import NamedTuple

from sqlalchemy import select, text, update

from src.api.lead_actionability import actionable_sql
from src.api.results_category import already_delivered_sql
from src.api.results_sort import auction_date_fallback_sql, filing_date_sql
from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year, tax_cap_sql

# Human-readable record-type labels. The private copy this replaces existed only
# to keep a worker from importing an API route module; this is a config module,
# not a route, so both call sites can share the one map.
from src.config.constants import record_type_label as _label
from src.db.models import BatchRun, Job, ScraperBatch
from src.utils.contact_decode import clean_phone_type, decode_array, decode_scalar
from src.utils.data_exporter import DataExporter
from src.utils.lead_export import (
    BATCH_LEAD_COLUMNS,
    PROBATE_SUBTYPE_AGG_SQL,
    write_lead_csv_with_overlap,
)
from src.utils.located_parcel import shown_tier_sql
from src.utils.logger import setup_logger

_logger = setup_logger("worker.batch_export")

EXPORT_CAP = 50_000



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
    -- Full column set the CSV builder (build_lead_export_row + derive_signals)
    -- consumes — an under-selected set silently blanks populated columns AND, by
    -- omitting delinquent_bill_year, ships a FABRICATED synthetic tax date (the
    -- per-job guard in build_lead_export_row only blanks it when bill_year is
    -- present). phones/emails are decrypted in Python (raw text() bypasses
    -- EncryptedJSON), mirroring segments._decrypt_pii_rows.
    SELECT r.id, r.date_recorded, r.date_recorded_parsed, r.party_name, r.heirs,
           r.parcel_id, r.property_address, r.mailing_address,
           r.property_city, r.property_state, r.property_zip,
           r.legal_description, r.doc_type,
           r.delinquent_amount, r.delinquent_bill_year,
           r.phone, r.phone_type, r.email, r.phones, r.emails,
           r.absentee_owner, r.out_of_state_owner, r.owner_state,
           r.auction_date, r.default_amount, r.enrichment_data,
           r.property_key, r.is_duplicate,
           -- The canonical "an EARLIER run delivered this" (results_category). Not
           -- is_duplicate alone: same-run siblings and superseded rows carry it too.
           {already_delivered_sql('r')} AS prior_delivered,
           -- Contact provenance (097/098): lookup = Tracerfy answered THIS row,
           -- reused = the account's own earlier answer copied free, NULL = none.
           r.skip_trace_status, r.skip_trace_source, r.skip_trace_attempted_at,
           r.enrichment_data->>'lead_subtype' AS lead_subtype,
           sc.record_type, sc.county, j.created_at AS job_created_at,
           CASE
               WHEN r.property_key IS NOT NULL THEN 'pk:' || r.property_key
               WHEN r.dedup_hash IS NOT NULL
                   THEN 'dh:' || sc.record_type || ':' || r.dedup_hash
               ELSE 'id:' || r.id::text
           END AS bucket
    FROM results r
    -- j.status = 'done': a batch child that FAILED or was cancelled at the
    -- deadline delivered nothing. Its rows were never charged and cannot be
    -- downloaded per-job, because export_key is written only inside the
    -- mark-done transaction. Without this filter finalize_batch_run handed
    -- EVERY child_job_id to the combined CSV, so a failed child's leads went
    -- out in the emailed partial-batch file while Lists — which filters on
    -- done — showed nothing. segments and analytics closed the same gap on
    -- 2026-09-08; this was the last path still open (Codex).
    JOIN jobs j ON j.id = r.job_id AND j.user_id = CAST(:uid AS uuid)
                AND j.status = 'done'
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
    WHERE r.user_id = CAST(:uid AS uuid)
      AND r.job_id = ANY(CAST(:job_ids AS uuid[]))
      -- Hard 18-month tax-delinquent cap (self-scoping: NULL bill_year rows pass).
      AND {tax_cap_sql('r')}
      -- Standing rule: no property AND no mailing address = not a lead (kept in
      -- results for dedup/health, never delivered or counted). lead_actionability.
      AND {actionable_sql('r')}
),
agg AS (
    SELECT bucket,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           CASE WHEN bucket LIKE 'pk:%' THEN count(DISTINCT record_type) ELSE 1 END AS overlap_count,
           {PROBATE_SUBTYPE_AGG_SQL},
           array_agg(DISTINCT county ORDER BY county) AS source_counties,
           -- New vs already delivered, per property. candidates spans EVERY row of
           -- the child jobs, including rows the account received in earlier runs
           -- (is_duplicate). A bucket is "new" when ANY of its rows is new; one made
           -- only of already-delivered rows is not a new lead, and every count the
           -- user sees must say which is which (2026-10-04 audit: a batch showed
           -- 1 + 384 combined leads beside 15 + 1 new ones).
           bool_or(NOT is_duplicate) AS has_new,
           bool_or(prior_delivered) AS has_prior
    FROM candidates
    GROUP BY bucket
)"""

# The representative row per bucket. Shared by _COMBINED_SQL and _QUALITY_SQL so
# the quality checks measure exactly the row each combined lead shows.
_RANKED_CTE = """,
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
)"""

# Record-type field groups (2026-10-11, owner decision D1). The representative row
# is picked for its CONTACT, so on a stacked property it is often the probate row,
# whose auction / tax / violation fields are NULL while a sibling row of that type
# holds them. Each group is therefore read WHOLE from ONE source row of its own
# record type in the bucket, never assembled field by field from several rows (the
# old independent max(amount) / max(bill_year) could pair one tax row's amount with
# another's year). Shared by _COMBINED_SQL and _QUALITY_SQL, so the table, the CSV
# and the quality checks read the same values. Rules (deterministic, id last):
#   tax       one tax_delinquent row with an amount or year: has amount, newest job
#   auction   one pre_foreclosure / trustee_sale row with auction data: has auction
#             date, has default amount, newest job
#   violation one code_violation row: newest job
# A bucket with no such row has NULL for the group (nothing to show, no fallback):
# apply_group_sources also strips that group's enrichment keys, so a representative
# row of ANOTHER type can never show its own "status" as a violation status.
_GROUP_SOURCE_CTES = """,
tax_src AS (
    SELECT DISTINCT ON (bucket) bucket, id AS tax_source_id,
           delinquent_amount AS src_delinquent_amount,
           delinquent_bill_year AS src_delinquent_bill_year,
           enrichment_data AS tax_enrichment
    FROM candidates
    WHERE record_type = 'tax_delinquent'
      AND (delinquent_amount IS NOT NULL OR delinquent_bill_year IS NOT NULL
           -- ->> reads a JSON null as SQL NULL, so a null key is not data.
           OR enrichment_data ->> 'billed_amount' IS NOT NULL
           OR enrichment_data ->> 'paid_amount' IS NOT NULL
           OR enrichment_data ->> 'account_status' IS NOT NULL)
    ORDER BY bucket, (delinquent_amount IS NULL), job_created_at DESC NULLS LAST, id DESC
),
auction_src AS (
    SELECT DISTINCT ON (bucket) bucket, id AS auction_source_id,
           record_type AS auction_source_record_type,
           auction_date AS src_auction_date, default_amount AS src_default_amount,
           -- Only an nts OBJECT is auction data (a JSON null or scalar is not).
           CASE WHEN json_typeof(enrichment_data -> 'nts') = 'object'
                THEN enrichment_data -> 'nts' END AS auction_nts
    FROM candidates
    WHERE record_type IN ('pre_foreclosure', 'trustee_sale')
      AND (auction_date IS NOT NULL OR default_amount IS NOT NULL
           OR json_typeof(enrichment_data -> 'nts') = 'object')
    ORDER BY bucket, (auction_date IS NULL), (default_amount IS NULL),
             job_created_at DESC NULLS LAST, id DESC
),
violation_src AS (
    SELECT DISTINCT ON (bucket) bucket, id AS violation_source_id,
           enrichment_data AS violation_enrichment
    FROM candidates
    WHERE record_type = 'code_violation'
    ORDER BY bucket, job_created_at DESC NULLS LAST, id DESC
)"""

_GROUP_SOURCE_JOINS = """
LEFT JOIN tax_src ts ON ts.bucket = rk.bucket
LEFT JOIN auction_src aus ON aus.bucket = rk.bucket
LEFT JOIN violation_src vs ON vs.bucket = rk.bucket
"""

# "located_parcel_id(rk.enrichment_data) is not None" in SQL: the shown tier/source pairs
# come from located_parcel's own constants (shown_tier_sql), so the two cannot drift.
_LOCATED_PIN_SHOWN_SQL = (
    "(jsonb_typeof(rk.enrichment_data::jsonb) = 'object'"
    " AND rk.enrichment_data::jsonb->>'kc_pin_status' = 'matched'"
    f" AND {shown_tier_sql('rk.enrichment_data::jsonb')}"
    " AND jsonb_typeof(rk.enrichment_data::jsonb->'kc_pin') = 'string'"
    " AND rk.enrichment_data::jsonb->>'kc_pin' ~ '^[0-9]{10}$')"
)

# Optional in-app view filters, shared by _COMBINED_SQL and _FILTERED_TOTAL_SQL so
# the page and its pager can never disagree. Each defaults to NULL (a NULL bind lets
# every row pass), so the CSV/delivery path binds NULL and behaves exactly as it did
# before the filters existed: one query, not two, so the filtered view can never
# drift from the exported file.
# Containment (= ANY), not equality: the combined set is DEDUPED, so one lead
# legitimately carries several record types / counties (that IS an overlap).
# f_delivery: 'new' = the property has at least one row new to the account;
# 'delivered' = no new row, and an EARLIER run delivered it (results_category).
# A property made only of same-run siblings / superseded rows is neither.
_VIEW_FILTERS = """
  AND (CAST(:f_record_type AS text) IS NULL
       OR CAST(:f_record_type AS text) = ANY(a.matched_record_types))
  AND (CAST(:f_county AS text) IS NULL
       OR CAST(:f_county AS text) = ANY(a.source_counties))
  AND (CAST(:f_delivery AS text) IS NULL
       OR (CAST(:f_delivery AS text) = 'new' AND a.has_new)
       OR (CAST(:f_delivery AS text) = 'delivered' AND NOT a.has_new AND a.has_prior))
""" + """
  -- Search (Phase 4, D3): the REPRESENTATIVE row's shown fields, i.e. what the table
  -- row shows. strpos, never LIKE, so a typed % or _ is literal. :f_q is already
  -- trimmed + lowercased, :f_q_parcel alnum-only and NULL when nothing is left (an
  -- empty strpos needle matches everything: Codex P1). Mailing is searched only when
  -- the batch shows it (:f_q_mailing), so a hidden value cannot find or count a lead.
  -- Phones / emails are encrypted at rest: not searchable.
  AND (CAST(:f_q AS text) IS NULL
       OR strpos(lower(coalesce(rk.party_name, '')), CAST(:f_q AS text)) > 0
       OR strpos(lower(coalesce(rk.property_address, '')), CAST(:f_q AS text)) > 0
       OR (CAST(:f_q_mailing AS boolean)
           AND strpos(lower(coalesce(rk.mailing_address, '')), CAST(:f_q AS text)) > 0)
       OR (CAST(:f_q_parcel AS text) IS NOT NULL
           AND strpos(regexp_replace(lower(coalesce(rk.parcel_id, '')), '[^a-z0-9]', '', 'g'),
                      CAST(:f_q_parcel AS text)) > 0)
       -- The located King PIN the table shows in place of a missing parcel ID
       -- (located_parcel_id, its exact twin: tested to agree on every shape).
       OR (CAST(:f_q_parcel AS text) IS NOT NULL
           AND coalesce(rk.parcel_id, '') = ''
           AND """ + _LOCATED_PIN_SHOWN_SQL + """
           AND strpos(rk.enrichment_data::jsonb->>'kc_pin', CAST(:f_q_parcel AS text)) > 0))
"""

# Sort keys, ahead of the relevance order (which stays as the tie-break). Every key
# is a CASE on the :sort bind, so NULL / 'relevance' leaves them all NULL and the
# order is exactly today's: the combined CSV binds NULL. The date is the one the
# table SHOWS (FE leadDate): Results' filing-date parser, NULL for an auction-date
# stand-in and for the synthetic "01/01/{bill year}" when the row is its own tax
# source. Undated rows sort last in both directions.
_SHOWN_DATE_SQL = (
    f"CASE WHEN {auction_date_fallback_sql('rk')} THEN NULL"
    " WHEN ts.tax_source_id = rk.id"
    " AND rk.date_recorded = '01/01/' || ts.src_delinquent_bill_year::text THEN NULL"
    f" ELSE {filing_date_sql('rk')} END"
)
_SORT_KEYS = f"""
ORDER BY (CASE WHEN CAST(:sort AS text) = 'date_asc' THEN {_SHOWN_DATE_SQL} END) ASC NULLS LAST,
         (CASE WHEN CAST(:sort AS text) = 'date_desc' THEN {_SHOWN_DATE_SQL} END) DESC NULLS LAST,
         (CASE WHEN CAST(:sort AS text) = 'auction_asc' THEN aus.src_auction_date END) ASC NULLS LAST,
         (CASE WHEN CAST(:sort AS text) = 'auction_desc' THEN aus.src_auction_date END) DESC NULLS LAST,
"""


def view_filter_params(
    record_type: str | None = None,
    county: str | None = None,
    delivery: str | None = None,
    q: str | None = None,
    mailing_searchable: bool = False,
    sort: str | None = None,
) -> dict:
    """Every view-filter / sort bind _COMBINED_SQL and _FILTERED_TOTAL_SQL take, in
    one place, so no caller can forget one. All defaults = no filter, today's order:
    the combined CSV binds exactly this."""
    term = (q or "").strip().lower() or None
    return {
        "f_record_type": record_type,
        "f_county": county,
        "f_delivery": delivery,
        "f_q": term,
        # NULL, not "", when only punctuation is left: "" would match every parcel.
        "f_q_parcel": (re.sub(r"[^a-z0-9]", "", term) or None) if term else None,
        "f_q_mailing": mailing_searchable,
        "sort": sort,
    }

_COMBINED_SQL = _COMBINED_CTES + _RANKED_CTE + _GROUP_SOURCE_CTES + """
SELECT rk.id, rk.date_recorded, rk.date_recorded_parsed, rk.party_name, rk.heirs,
       rk.parcel_id, rk.property_address, rk.mailing_address,
       rk.property_city, rk.property_state, rk.property_zip,
       rk.legal_description, rk.doc_type,
       -- Record-type groups come from their own source rows (_GROUP_SOURCE_CTES).
       ts.src_delinquent_amount AS delinquent_amount,
       ts.src_delinquent_bill_year AS delinquent_bill_year,
       rk.phone, rk.phone_type, rk.email, rk.phones, rk.emails,
       rk.absentee_owner, rk.out_of_state_owner, rk.owner_state,
       aus.src_auction_date AS auction_date, aus.src_default_amount AS default_amount,
       rk.enrichment_data,
       -- Merged into a copy of enrichment_data by apply_group_sources().
       ts.tax_enrichment, aus.auction_nts, vs.violation_enrichment,
       ts.tax_source_id, aus.auction_source_id, aus.auction_source_record_type,
       vs.violation_source_id,
       -- The representative row's own type — lets the CSV builder tell a real date
       -- from the synthetic tax date after the tax bill_year is coalesced in (Codex).
       rk.record_type,
       a.matched_record_types, a.overlap_count, a.source_counties, a.lead_subtype,
       (NOT a.has_new AND a.has_prior) AS already_delivered,
       rk.skip_trace_status, rk.skip_trace_source, rk.skip_trace_attempted_at
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket""" + _GROUP_SOURCE_JOINS + """WHERE rk.rn = 1
  AND (NOT :overlaps_only OR (rk.bucket LIKE 'pk:%' AND a.overlap_count >= 2))
""" + _VIEW_FILTERS + _SORT_KEYS + """         a.overlap_count DESC,
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


# Mode-aware total UNDER the active view filters. Deliberately NOT a second
# counting rule: same _COMBINED_CTES, same dedup buckets, same mode predicate as
# _COMBINED_SQL / _DELIVERY_COUNTS_SQL — only narrowed. Counts the rn=1
# representative rows joined to agg, exactly the FROM / WHERE of _COMBINED_SQL, so
# the search (which reads the representative row) counts what the page renders.
# One rn=1 row per bucket, so without a search this equals counting agg.
_FILTERED_TOTAL_SQL = _COMBINED_CTES + _RANKED_CTE + """
SELECT count(*) AS total
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket
WHERE rk.rn = 1
  AND (NOT :overlaps_only OR (rk.bucket LIKE 'pk:%' AND a.overlap_count >= 2))
""" + _VIEW_FILTERS

# Batch data-quality facts, one statement over the representative rows (the row
# each combined lead SHOWS), so a check can never pass on a sibling row the user
# never sees. Not mode-filtered and not view-filtered: these describe the whole
# combined set. Categories are exclusive only where named so:
#   new + already_delivered = leads; stacked + single_list + no_identity = leads.
# Record-type checks count only the leads they apply to (auction fields only exist
# on pre-foreclosure / trustee-sale rows, tax fields only on tax-delinquent ones).
# Record-type fields are read off the same group source rows the combined lead
# delivers (_GROUP_SOURCE_CTES), so a check passes exactly when the file has the value.
_QUALITY_SQL = _COMBINED_CTES + _RANKED_CTE + _GROUP_SOURCE_CTES + """,
q AS (
    SELECT rk.*, a.has_new, a.has_prior, a.overlap_count, a.matched_record_types,
           ts.src_delinquent_amount, ts.src_delinquent_bill_year,
           aus.src_auction_date, aus.src_default_amount,
           ('pre_foreclosure' = ANY(a.matched_record_types)
            OR 'trustee_sale' = ANY(a.matched_record_types)) AS auction_applies,
           'tax_delinquent' = ANY(a.matched_record_types) AS tax_applies
    FROM ranked rk JOIN agg a ON a.bucket = rk.bucket""" + _GROUP_SOURCE_JOINS + """    WHERE rk.rn = 1
)
SELECT count(*) AS leads,
       count(*) FILTER (WHERE has_new) AS new_leads,
       count(*) FILTER (WHERE NOT has_new AND has_prior) AS already_delivered,
       -- Neither: only same-run siblings / superseded rows (never handed over).
       count(*) FILTER (WHERE NOT has_new AND NOT has_prior) AS not_new_not_delivered,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count >= 2) AS stacked,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count >= 2 AND has_new) AS stacked_new,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count < 2) AS single_list,
       count(*) FILTER (WHERE bucket NOT LIKE 'pk:%') AS no_identity,
       count(*) FILTER (WHERE nullif(btrim(parcel_id), '') IS NOT NULL) AS with_parcel,
       count(*) FILTER (WHERE nullif(btrim(property_address), '') IS NOT NULL) AS with_property_address,
       count(*) FILTER (WHERE nullif(btrim(mailing_address), '') IS NOT NULL) AS with_mailing_address,
       count(*) FILTER (WHERE phone IS NOT NULL) AS with_phone,
       count(*) FILTER (WHERE email IS NOT NULL) AS with_email,
       count(*) FILTER (WHERE skip_trace_source = 'lookup') AS contacts_looked_up,
       count(*) FILTER (WHERE skip_trace_source = 'reused') AS contacts_reused,
       count(*) FILTER (WHERE auction_applies) AS auction_applicable,
       count(*) FILTER (WHERE auction_applies AND src_auction_date IS NOT NULL) AS with_auction_date,
       count(*) FILTER (WHERE auction_applies AND src_default_amount IS NOT NULL) AS with_default_amount,
       count(*) FILTER (WHERE tax_applies) AS tax_applicable,
       count(*) FILTER (WHERE tax_applies AND src_delinquent_amount IS NOT NULL) AS with_tax_balance,
       count(*) FILTER (WHERE tax_applies AND src_delinquent_bill_year IS NOT NULL) AS with_tax_year
FROM q
"""

# The facet values actually present in the mode-filtered combined set, so the UI
# offers only filters that can return rows (and can hide the County control
# entirely for a single-county batch). Deliberately NOT narrowed by the active filters — the option
# list must not collapse as soon as the user picks one.
_FACETS_SQL = _COMBINED_CTES + """
SELECT
  (SELECT coalesce(array_agg(DISTINCT rt ORDER BY rt), '{}')
     FROM agg a2, unnest(a2.matched_record_types) AS rt
    WHERE (NOT :overlaps_only OR (a2.bucket LIKE 'pk:%' AND a2.overlap_count >= 2))
  ) AS record_types,
  (SELECT coalesce(array_agg(DISTINCT ct ORDER BY ct), '{}')
     FROM agg a3, unnest(a3.source_counties) AS ct
    WHERE (NOT :overlaps_only OR (a3.bucket LIKE 'pk:%' AND a3.overlap_count >= 2))
  ) AS counties
"""


# Per-child terminal status for the failed_children summary. Same uuid-param
# casting as _COMBINED_SQL (raw text() binds str/list[str] as text/text[]; the
# columns are native uuid — psycopg2 has no uuid=text operator without the cast).
_FAILED_CHILDREN_SQL = """
SELECT j.id::text AS job_id, j.status AS status,
       sc.county AS county, sc.record_type AS record_type
FROM jobs j
JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
WHERE j.user_id = CAST(:uid AS uuid) AND j.id = ANY(CAST(:job_ids AS uuid[]))
"""


_EMPTY_COUNTS = {
    "leads_total": 0,
    "overlaps_delivered": 0,
    "singletons_suppressed": 0,
    "unmatchable_no_parcel": 0,
}


# The enrichment_data keys each record-type group owns (the keys build_lead_export_row
# and the Results UI read for that group). apply_group_sources moves exactly these
# from the group's source row onto the lead, so a group is never half one row's.
_TAX_ENRICHMENT_KEYS = ("billed_amount", "paid_amount", "account_status")
_VIOLATION_ENRICHMENT_KEYS = (
    "violation_category", "record_type", "case_type", "status", "description",
    "last_inspection", "record_number", "case_number",
)


def _json_dict(value) -> dict | None:
    """A json column as a dict. Raw text() SQL returns it parsed (psycopg2) or as its
    text (asyncpg); anything that is not an object reads as None."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def apply_group_sources(data: dict) -> dict:
    """Fold a _COMBINED_SQL row's record-type group sources into the lead, in place.

    Pops the helper columns (tax_enrichment, auction_nts, violation_enrichment) and,
    for EVERY group, drops that group's keys from a COPY of the representative row's
    enrichment_data (the fetched value is never mutated), then sets them from the
    group's source row when it has one. A group without a source shows nothing: the
    representative row belongs to another record type, so its keys under those names
    mean something else (Codex P1). The source ids become strings. One function for the CSV and the API, so the two can
    never fold differently.
    """
    tax = _json_dict(data.pop("tax_enrichment", None))
    nts = data.pop("auction_nts", None)
    if isinstance(nts, str):
        try:
            nts = json.loads(nts)
        except ValueError:
            nts = None
    violation = _json_dict(data.pop("violation_enrichment", None))
    for key in ("tax_source_id", "auction_source_id", "violation_source_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])

    original = _json_dict(data.get("enrichment_data"))
    enr = dict(original or {})
    groups = (
        (data.get("tax_source_id"), _TAX_ENRICHMENT_KEYS, tax or {}),
        (data.get("violation_source_id"), _VIOLATION_ENRICHMENT_KEYS, violation or {}),
        (data.get("auction_source_id"), ("nts",), {"nts": nts} if nts is not None else {}),
    )
    for source_id, keys, source in groups:
        for key in keys:
            enr.pop(key, None)
            if source_id is not None and source.get(key) not in (None, ""):
                enr[key] = source[key]
    data["enrichment_data"] = enr if (enr or original is not None) else None
    return data


def _combined_pairs(
    db,
    user_id: str,
    job_ids: list[str],
    delivery_mode: str = "everything",
    limit: int | None = EXPORT_CAP,
    offset: int = 0,
) -> list[tuple]:
    """Return (record_namespace, overlap_dict) pairs for the batch, hottest-first.

    Ordering + mode filtering are SQL-side (deterministic; pagination-safe).
    `limit=None` binds SQL `LIMIT NULL` (unbounded — the whole set in one snapshot).
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
            # Export is never view-filtered or re-sorted: the all-NULL binds make every
            # filter and sort key a no-op, so the delivered file is exactly as before.
            **view_filter_params(),
            TAX_CAP_BIND: tax_cap_min_year(datetime.now(UTC).date()),
        },
    )
    rows = []
    for r in result.fetchall():
        data = dict(r._mapping)
        # Raw text() SQL: no column type runs, so every contact goes through the one
        # decoder (UX 3.8s2), exactly like segments._decrypt_pii_rows. An unreadable
        # value is None + a WARNING naming the lead and field: a blank cell, never
        # ciphertext in the file and never a failed render. The arrays feed
        # phone_2/3 + email_2/3. The bucket was ranked over ciphertext, so a
        # malformed representative reads blank; no sibling is substituted (accepted).
        lead = str(data.get("id"))
        apply_group_sources(data)
        data["phone"], _ = decode_scalar(data.get("phone"), field="phone", lead_id=lead)
        data["email"], _ = decode_scalar(data.get("email"), field="email", lead_id=lead)
        data["phone_type"], _ = clean_phone_type(data.get("phone_type"), lead_id=lead)
        for kind in ("phones", "emails"):
            data[kind], _ = decode_array(data.get(kind), kind=kind, lead_id=lead)
        # The winning row's own enrichment_data carries a lead_subtype, but the
        # bucket-AGGREGATED subtype (a.lead_subtype via PROBATE_SUBTYPE_AGG_SQL) is
        # authoritative — a bucket's representative row may be non-probate. Drop the
        # per-row subtype from a COPY (never mutate the fetched row) so
        # build_lead_export_row falls back to the aggregated scalar (Codex P2).
        enr = data.get("enrichment_data")
        if isinstance(enr, dict) and "lead_subtype" in enr:
            data["enrichment_data"] = {k: v for k, v in enr.items() if k != "lead_subtype"}
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


def _combined_pairs_all(
    db, user_id: str, job_ids: list[str], delivery_mode: str = "everything"
) -> list[tuple]:
    """ALL combined pairs for the delivered CSV, in ONE query (limit=None -> SQL
    `LIMIT NULL` = unbounded). Two properties we need together:
      - no SILENT truncation: the old default limit=EXPORT_CAP dropped rows past 50k
        while the email/UI counts reported the true, larger total (a broken paid
        export).
      - a CONSISTENT snapshot: a single statement reads one MVCC snapshot, so a
        skip-trace contact fill landing mid-read can't reorder rows across pages and
        duplicate/drop them the way OFFSET paging could (Codex P2 — the ORDER BY
        keys on contactable status, which async enrichment mutates).
    EXPORT_CAP stays the API page size for the interactive /leads view; this
    delivered-export path is intentionally uncapped."""
    return _combined_pairs(db, user_id, job_ids, delivery_mode=delivery_mode, limit=None)


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


class RenderedCsv(NamedTuple):
    """A rendered CSV and how many lead rows went into it.

    The row count is returned rather than re-derived because the caller needs to
    know whether the download carried any leads (it is the activation signal),
    and the only other way to find out is to parse the bytes back. That parse was
    a real bug: csv.reader refuses a field over 131,072 characters and
    legal_description and heirs are uncapped, so an outsized lead turned a good
    export into a 500 (#264). The renderer already has the number.
    """

    data: bytes
    row_count: int


def render_combined_csv(
    user_id: str,
    job_ids: list[str],
    hidden_fields: set[str] | None = None,
    delivery_mode: str = "everything",
) -> RenderedCsv:
    """Build the combined, deduped, overlap-flagged CSV ON DEMAND from the DB
    (NOT the stored R2 snapshot). Used by the download endpoint so:
      - a re-download reflects later async skip-trace fills (fresh contacts), and
      - the API never needs R2 access (R2_ACCOUNT_ID/keys live on the worker, not
        the api — the stored object is only a 'barrier finished' marker).
    Opens its own SYNC psycopg2 session like the worker. Tenant isolation is the
    explicit user_id filter baked into _COMBINED_SQL — callers MUST pass the
    verified owner's id + that batch_run's own child_job_ids.

    `hidden_fields` (from the batch's shared `fields`) blanks the user-deselected
    hideable columns so the combined download matches the per-job exports.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        pairs = _combined_pairs_all(db, user_id, job_ids, delivery_mode=delivery_mode)
        buf = io.StringIO()
        write_lead_csv_with_overlap(
            pairs, buf, hidden_fields=hidden_fields, columns=BATCH_LEAD_COLUMNS)
        db.rollback()  # read-only
        # One CSV row per pair, so len(pairs) IS the lead count in the file.
        return RenderedCsv(buf.getvalue().encode("utf-8"), len(pairs))


def finalize_batch_run(db, run, forced: bool = False, claim_token: str | None = None) -> None:
    """Build + upload the combined CSV and deliver it. Called AFTER the run is
    claimed (lease held) and either all child jobs are terminal OR (forced=True)
    the run is past the hard deadline. Commits the run status before emailing so a
    delivery failure can't undo the export.

    forced=True (Track A backstop): a run stuck 'running' past the deadline
    finalizes anyway — missing / still-non-terminal children count as failed and
    the combined CSV is built from whatever children DID produce results, so a
    permanently-stuck child can't strand the run forever.

    claim_token (Track A): the lease owner token. If a finalize runs longer than
    the lease TTL, another sweep can re-claim the run and run a concurrent
    finalize. Guarding the terminal write on claim_token ensures only the CURRENT
    lease owner commits the status + triggers delivery — a stale worker's write
    no-ops (Codex P2). The R2 overwrite is idempotent (same key, deterministic
    content), so a double upload is harmless."""
    # Per-child status -> failed_children summary.
    child_rows = db.execute(
        text(_FAILED_CHILDREN_SQL),
        {"uid": run.user_id, "job_ids": run.child_job_ids or []},
    ).fetchall()
    if forced:
        # Any child not 'done' (failed/cancelled OR still non-terminal) is a
        # failure; children missing from the table entirely are 'missing'.
        present = {row.job_id for row in child_rows}
        failed = [
            {
                "job_id": row.job_id,
                "county": row.county,
                "record_type": row.record_type,
                "reason": row.status if row.status in ("failed", "cancelled") else "timed out",
            }
            for row in child_rows
            if row.status != "done"
        ]
        failed += [
            {"job_id": jid, "county": None, "record_type": None, "reason": "missing"}
            for jid in (run.child_job_ids or [])
            if jid not in present
        ]
        # Terminalize still-active children (Codex P2): force-finalize only RECORDS
        # them as timed out, but the underlying jobs row stays pending/scraping. A
        # lost child .delay() would leave a permanent active job, and a delayed
        # broker message could claim + scrape + bill it AFTER the batch is terminal.
        # Cancel them in this same txn so run_scrape_job's boot check / atomic claim
        # short-circuits any late delivery (idempotent: only non-terminal rows flip).
        db.execute(
            update(Job)
            .where(
                Job.id.in_(run.child_job_ids or []),
                Job.user_id == run.user_id,
                Job.status.not_in(("done", "failed", "cancelled")),
            )
            .values(status="cancelled", finished_at=datetime.now(UTC))
        )
    else:
        failed = [
            {"job_id": row.job_id, "county": row.county, "record_type": row.record_type, "reason": row.status}
            for row in child_rows
            if row.status in ("failed", "cancelled")
        ]

    # The parent batch owns output shape: delivery_mode (what the combined
    # export contains) + fields (hideable-column visibility).
    from src.utils.lead_export import resolve_hidden_output_fields
    _batch = db.get(ScraperBatch, run.batch_id)
    delivery_mode = (_batch.delivery_mode if _batch else None) or "everything"
    hidden_fields = resolve_hidden_output_fields(_batch.fields if _batch else None)

    pairs = _combined_pairs_all(
        db, run.user_id, run.child_job_ids or [], delivery_mode=delivery_mode
    )
    # Honest accounting (uncapped, mode-independent). Stored on the run as the
    # as-delivered snapshot; live reads recompute with the current mode.
    counts = compute_delivery_counts(db, run.user_id, run.child_job_ids or [])

    object_key = None
    if pairs:
        exporter = DataExporter()
        # UNIQUE local temp name per finalize (Codex P2): if a finalize outruns its
        # lease and another reclaims, two finalizers can run at once. A shared name
        # would let one overwrite/unlink the other's file mid-upload. A per-call
        # suffix (claim_token if present, else a uuid) isolates them; the R2 object
        # key stays stable (idempotent overwrite of equivalent content).
        suffix = (claim_token or uuid.uuid4().hex)[:8]
        local_path = exporter.export_dir / f"batch_{run.id[:8]}_{suffix}.csv"
        exporter.export_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(local_path, "w", newline="", encoding="utf-8") as fh:
                write_lead_csv_with_overlap(
                    pairs, fh, hidden_fields=hidden_fields, columns=BATCH_LEAD_COLUMNS)
            object_key = exporter.upload_to_r2(
                local_path, f"exports/{run.user_id}/batch/{run.id}/combined.csv"
            )
        finally:
            local_path.unlink(missing_ok=True)

    # STATUS-GUARDED final write (Codex P1): only flip running->done/partial if the
    # run is STILL 'running'. A cancel that committed during the CSV/R2 build (after
    # the claim) makes rowcount 0 -> we must NOT overwrite 'cancelled' or deliver.
    # done = all succeeded; failed = ALL children failed; partial = a mix
    # (Codex: 100%-failure must not read as 'partial').

    # Configs blocked by tier enforcement were recorded on the run at fan-out time;
    # they never became child jobs, so child_rows can't see them. Read fresh (avoid
    # identity-map staleness) and merge so the run reads partial/failed correctly and
    # blocked configs stay visible in failed_children. Blocked entries have no job_id,
    # so there's no overlap with the per-child `failed` list.
    prior_blocked = db.execute(
        select(BatchRun.failed_children).where(BatchRun.id == run.id)
    ).scalar() or []
    failed = prior_blocked + failed

    total_children = len(run.child_job_ids or []) + len(prior_blocked)
    if not failed:
        new_status = "done"
    elif len(failed) >= total_children and total_children > 0:
        new_status = "failed"
    else:
        new_status = "partial"
    # LEASE-OWNER guard (Codex P2): if this finalize outran its lease and another
    # sweep re-claimed the run, claim_token has changed -> rowcount 0 -> we don't
    # commit a terminal state or deliver. Only the current owner finalizes.
    guard = [BatchRun.id == run.id, BatchRun.status == "running"]
    if claim_token is not None:
        guard.append(BatchRun.claim_token == claim_token)
    updated = db.execute(
        update(BatchRun)
        .where(*guard)
        .values(
            combined_export_key=object_key,
            failed_children=failed or None,
            status=new_status,
            completed_at=datetime.now(UTC),
            delivery_counts=counts,
        )
    ).rowcount
    db.commit()
    if not updated:
        _logger.info(
            "finalize_batch_run %s: not finalized (cancelled, or lease re-claimed "
            "by another worker) — no delivery",
            run.id,
        )
        return

    _deliver(
        db, run, len(pairs), object_key,
        new_status=new_status,
        summary=_delivery_summary(delivery_mode, counts),
    )
    _logger.info(
        "finalize_batch_run %s: %d leads, status=%s, failed_children=%d",
        run.id, len(pairs), new_status, len(failed),
    )


def _delivery_summary(mode: str, counts: dict) -> str:
    """One honest sentence for the delivery email. The empty overlaps_only case
    must read as 'no overlaps found' (with the why), never as 'broken'."""
    total = counts.get("leads_total", 0)
    overlaps = counts.get("overlaps_delivered", 0)
    singletons = counts.get("singletons_suppressed", 0)
    no_parcel = counts.get("unmatchable_no_parcel", 0)
    if mode == "overlaps_only":
        if overlaps == 0:
            return (
                f"0 cross-list overlap leads found across {total:,} scraped leads. "
                f"{no_parcel:,} lead(s) had no parcel number and couldn't be "
                "cross-matched. Switch the batch to 'Everything' to receive all leads."
            )
        # Report singletons and unmatchable-no-parcel SEPARATELY — lumping the
        # no-parcel rows into "single-list" (total - overlaps) mislabels them.
        parts = [f"{overlaps:,} lead(s) found on 2 or more lists."]
        if singletons:
            parts.append(f"{singletons:,} single-list lead(s) not included in this delivery.")
        if no_parcel:
            parts.append(
                f"{no_parcel:,} lead(s) had no parcel number and couldn't be cross-matched."
            )
        return " ".join(parts)
    return f"{overlaps:,} of {total:,} lead(s) appear on 2 or more lists."


def _deliver(
    db, run, lead_count: int, object_key: str | None,
    new_status: str = "done", summary: str | None = None,
) -> None:
    """Send the one combined-CSV delivery email (best-effort, non-fatal),
    including the honest delivery summary message.

    At-most-once: a re-finalize (lease steal / retry after a crash) must not
    re-send. The status-guarded final write protects DB state, not this
    post-commit email, so we CAS delivery_started_at NULL->now and only the winner
    sends (Codex: duplicate delivery is not tolerable; a missed email is
    recoverable because the CSV is always downloadable in-app)."""
    # Email on every successful finalize — including a zero-row overlaps_only
    # run (the honest empty-state IS the delivery). Fully-failed runs don't
    # email (ops alerts cover failures); the old `if not object_key` gate would
    # have silently skipped exactly the empty-state case this feature exists for.
    if new_status not in ("done", "partial"):
        return
    claimed = db.execute(
        update(BatchRun)
        .where(BatchRun.id == run.id, BatchRun.delivery_started_at.is_(None))
        .values(delivery_started_at=datetime.now(UTC))
    ).rowcount
    db.commit()
    if not claimed:
        _logger.info(
            "batch %s: delivery already started/sent — skipping duplicate email", run.id
        )
        return
    batch = db.get(ScraperBatch, run.batch_id)
    if batch is None:
        return
    deliver = batch.deliver or {}
    emails = deliver.get("emails") or []
    if not emails:
        return
    try:
        from src.config import settings
        from src.workers.delivery import deliver_job_email
        # Link to the in-app batch page (authed streaming download), NOT an R2
        # presigned URL — that S3-presign path 401s in this R2 config (Codex).
        url = f"{settings.FRONTEND_URL.rstrip('/')}/batches/{run.batch_id}"
        # Enqueue on Celery so a transient Resend failure is retried instead of
        # dropped. The delivery_started_at CAS above already guarantees this runs
        # at most once per batch, so enqueue-once + retry-on-failure is safe.
        from src.utils.crypto import seal_task_args

        # P0-04b: recipients travel sealed; only the run id is readable in the broker.
        deliver_job_email.delay(job_id=str(run.id), sealed=seal_task_args({
            "scraper_name": batch.name or "Batch scrape",
            "record_count": lead_count,
            "download_url": url,
            "recipient_emails": emails,
            "summary_message": summary,
            "link_expires": False,  # in-app batch page — not a presigned URL (Codex P2)
        }))
    except Exception as exc:  # enqueue is best-effort — the CSV is in R2 either way
        # The delivery_started_at CAS above is already consumed, so no future
        # finalizer will retry this batch email — surface the miss to ops (the
        # per-job path alerts on the same failure mode).
        _logger.warning("batch %s delivery email enqueue failed: %s", run.id, str(exc)[:200])
        try:
            from src.workers.ops_alerts import send_ops_alert
            send_ops_alert(
                "batch_email_enqueue", str(run.id),
                "Batch lead email could not be queued",
                f"Could not queue the delivery email for batch run {run.id}: "
                f"{str(exc)[:200]}. The combined export is available in-app.",
            )
        except Exception:  # ops alert is best-effort — never mask the original miss
            pass
