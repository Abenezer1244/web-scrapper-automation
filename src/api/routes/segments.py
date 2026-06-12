"""Segment routes: Phase 3 combine / overlap lead targeting.

First slice — INTERSECTION ("on both lists"): the properties a user has on ALL
of 2+ record-type lists (e.g. probate AND pre_foreclosure) — the
highest-motivation sellers. STRONG-IDENTITY ONLY: overlap is keyed on the
post-enrichment parcel/address hash (property_key); weak name/date rows are
excluded by design and the API says so (identity_strength="strong").

Flow (per the Codex-reviewed design):
  1. users_overlap(user_id, record_types) -> property_keys on ALL selected
     lists (indexed Phase 1 membership lookup, not a results self-join).
  2. Join results -> jobs -> scraper_configs on those keys, constrained to the
     selected record_types (user_id+property_key alone could pull an unrelated
     newer row), optionally filtered by county.
  3. Pick ONE representative row per property via a window function
     (contactable first, then most recent job, then id), and aggregate
     matched_record_types + overlap_count per property.

Tenant-scoped via RLS (get_rls_db) AND an explicit user_id filter (belt +
suspenders). All exported fields pass sanitize_for_csv.

Union (inclusive, strong + weak) is a deliberately separate later slice.
"""
import io
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.middleware import rate_limit
from src.api.schemas import (
    SegmentIntersectionRequest,
    SegmentIntersectionResponse,
    SegmentLeadRow,
    SegmentUnionRequest,
    SegmentUnionResponse,
)
from src.utils.crypto import decrypt_field
from src.utils.lead_export import write_lead_csv_with_overlap
from src.utils.logger import setup_logger

_logger = setup_logger("api.segments")

router = APIRouter(prefix="/segments", tags=["segments"])

# Human-readable list labels for the CSV `lists` column (matches the frontend).
# Fallback title-cases an unknown slug so the export never shows a raw token.
_RECORD_TYPE_LABELS = {
    "probate": "Probate",
    "death_certificate": "Death Certificate",
    "pre_foreclosure": "Pre-Foreclosure",
    "tax_delinquent": "Tax Delinquent",
    "divorce": "Divorce",
    "code_violation": "Code Violation",
    "eviction": "Eviction",
}


def _label(slug: str) -> str:
    return _RECORD_TYPE_LABELS.get(slug, slug.replace("_", " ").title())


def _filing_sort_key(date_recorded: str | None) -> int:
    """-(ordinal) of a 'M/D/YYYY' filing date so the most recent sorts first;
    unparseable/blank -> 0 (sorts after real dates). Used for hottest-first."""
    if not date_recorded:
        return 0
    try:
        m, d, y = date_recorded.strip().split("/")
        return -date(int(y), int(m), int(d)).toordinal()
    except (ValueError, OverflowError):
        return 0


def _segment_csv_response(rows: list, filename_slug: str) -> Response:
    """Render decoded segment rows to the unified dialer-ready overlap CSV.

    Sort is hottest-first: most lists -> contactable (has phone/email) -> most
    recent filing date -> stable. Routes through the canonical lead_export builder
    (first/last + address split + 10-digit phone + sanitization) so the Lists CSV
    and the per-job CSV share one format. `counties` is the representative row's
    county (segments pick one row per property).
    """
    ordered = sorted(
        rows,
        key=lambda r: (
            -(r.overlap_count or 0),
            0 if (r.phone or r.email) else 1,
            _filing_sort_key(r.date_recorded),
        ),
    )
    pairs = [
        (
            r,
            {
                "lists_count": r.overlap_count,
                "lists": "; ".join(_label(t) for t in (r.matched_record_types or [])),
                "counties": r.county,
            },
        )
        for r in ordered
    ]
    output = io.StringIO()
    write_lead_csv_with_overlap(pairs, output)
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename_slug}.csv"',
            "Cache-Control": "private, no-store",
        },
    )

# Representative rows returned in the JSON preview. The CSV export returns the
# full set up to EXPORT_CAP (defensive bound — intersections are inherently
# small, but never stream an unbounded result into memory).
PREVIEW_CAP = 500
EXPORT_CAP = 50_000

# Final SELECT is assembled with a fixed optional county clause (no user data is
# interpolated — values are bound params). array_agg(DISTINCT ...) cannot run as
# a WINDOW aggregate in Postgres, so the per-property aggregate and the
# representative-row ranking live in two separate CTEs.
#
# Overlap is sourced from property_list_membership (the indexed Phase 1 rollup —
# the design's overlap source) as an inline subquery, NOT materialized into
# Python: pushing it into SQL keeps the LIMIT bound effective and avoids shipping
# a full property_key array back into the query (Codex P2). The membership
# subquery decides overlap county-agnostically; the agg HAVING then re-checks
# that the intersection STILL holds within the county-filtered candidate scope,
# so a county filter can't return a property that is only on one list there
# (Codex P2). :n is the count of distinct selected record types.
_INTERSECTION_SQL = """
WITH candidates AS (
    SELECT r.id, r.date_recorded, r.party_name, r.parcel_id, r.property_address,
           r.mailing_address, r.phone, r.phone_type, r.email,
           r.phones, r.emails, r.property_key,
           sc.record_type, sc.county, sc.state, j.created_at AS job_created_at
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = :uid
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = :uid
    WHERE r.user_id = :uid
      AND r.property_key IS NOT NULL
      AND sc.record_type = ANY(:types)
      AND r.property_key IN (
          SELECT property_key
          FROM property_list_membership
          WHERE user_id = :uid AND record_type = ANY(:types)
          GROUP BY property_key
          HAVING count(DISTINCT record_type) = :n
      )
      {county_clause}
),
agg AS (
    SELECT property_key,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           count(DISTINCT record_type) AS overlap_count
    FROM candidates
    GROUP BY property_key
    HAVING count(DISTINCT record_type) = :n
),
ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.property_key
               ORDER BY (CASE WHEN c.phone IS NOT NULL OR c.email IS NOT NULL
                              THEN 0 ELSE 1 END),
                        c.job_created_at DESC NULLS LAST,
                        c.id DESC
           ) AS rn
    FROM candidates c
)
SELECT rk.id, rk.date_recorded, rk.party_name, rk.parcel_id, rk.property_address,
       rk.mailing_address, rk.county, rk.state, rk.phone, rk.phone_type, rk.email,
       rk.phones, rk.emails,
       a.matched_record_types, a.overlap_count
FROM ranked rk
JOIN agg a ON a.property_key = rk.property_key
WHERE rk.rn = 1
ORDER BY a.overlap_count DESC, rk.property_address NULLS LAST, rk.id
LIMIT :limit
"""

# UNION ("combine"): every lead on ANY selected record type, merged + deduped,
# NEVER dropping weak leads (contrast intersection). No membership — union is a
# direct per-user results scan filtered by record_type. Dedup bucket:
#   COALESCE(property_key, dedup_hash, 'id:'||id) — strong rows dedupe by
#   property_key; weak rows fall back to dedup_hash (name|date); rows with
#   neither stand alone by id. NO prefix on the hash branches so a strong row
#   whose property_key was not yet backfilled (it still carries the strong hash
#   in dedup_hash) coalesces with a backfilled row for the same property by hash
#   VALUE (Codex). identity_strength is per-bucket via bool_or(property_key IS
#   NOT NULL): a bucket is strong iff some row proves strong identity.
#
# CAVEAT (documented, not hidden — Codex): dedup_hash is PRE-enrichment and
# property_key is POST-enrichment, so for strong rows where enrichment changed
# the parcel/address, an un-backfilled historical row (property_key NULL) can
# still split from / mislabel vs a backfilled row. Run
# scripts/backfill_result_property_key.py for fully accurate union; forward rows
# are always correct. Weak dedup is name|date (not property identity), so weak
# buckets can merge same-name/date leads across types — intended, do not
# overclaim overlap_count for weak rows.
_UNION_SQL = """
WITH candidates AS (
    SELECT r.id, r.date_recorded, r.party_name, r.parcel_id, r.property_address,
           r.mailing_address, r.phone, r.phone_type, r.email,
           r.phones, r.emails,
           r.property_key, r.is_duplicate,
           sc.record_type, sc.county, sc.state, j.created_at AS job_created_at,
           COALESCE(r.property_key, r.dedup_hash, 'id:' || r.id::text) AS bucket
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = :uid
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = :uid
    WHERE r.user_id = :uid
      AND sc.record_type = ANY(:types)
      -- Optional filing-date window (migration 049 date_recorded_parsed). When no
      -- window is requested :require_date is FALSE and from/to are NULL, so all
      -- three predicates pass and behavior is identical to the all-time query.
      -- When a window IS active, NULL filing dates are excluded (can't be placed
      -- in time) and reported separately via excluded_no_date_count.
      AND (CAST(:filing_from AS date) IS NULL OR r.date_recorded_parsed >= CAST(:filing_from AS date))
      AND (CAST(:filing_to AS date) IS NULL OR r.date_recorded_parsed <= CAST(:filing_to AS date))
      AND (CAST(:require_date AS boolean) = FALSE OR r.date_recorded_parsed IS NOT NULL)
      {county_clause}
),
agg AS (
    SELECT bucket,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           count(DISTINCT record_type) AS overlap_count,
           CASE WHEN bool_or(property_key IS NOT NULL) THEN 'strong' ELSE 'weak' END
               AS identity_strength
    FROM candidates
    GROUP BY bucket
),
ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.bucket
               -- Contactable FIRST so we never drop an available phone/email by
               -- preferring an older non-duplicate row that was never skip-traced
               -- over a newer duplicate that has contact data (Codex); matches
               -- the intersection ranking. is_duplicate only breaks ties among
               -- equally-contactable rows (prefer the original lead).
               ORDER BY (CASE WHEN c.phone IS NOT NULL OR c.email IS NOT NULL
                              THEN 0 ELSE 1 END),
                        c.is_duplicate ASC,
                        c.job_created_at DESC NULLS LAST,
                        c.id DESC
           ) AS rn
    FROM candidates c
)
SELECT rk.id, rk.date_recorded, rk.party_name, rk.parcel_id, rk.property_address,
       rk.mailing_address, rk.county, rk.state, rk.phone, rk.phone_type, rk.email,
       rk.phones, rk.emails,
       a.matched_record_types, a.overlap_count, a.identity_strength
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket
WHERE rk.rn = 1
ORDER BY a.overlap_count DESC, a.identity_strength, rk.property_address NULLS LAST, rk.id
LIMIT :limit
"""

# INTERSECTION, DATE-WINDOWED variant. The all-time intersection above uses the
# property_list_membership rollup, which carries OUR scrape timestamps — NOT the
# county filing date — so it CANNOT honor a filing-date window (Codex P1). When a
# window is requested we compute overlap from `results` directly: candidates are
# date-filtered first, then a property qualifies only if it appears on all :n
# selected record types WITHIN that window. Strong-identity only (property_key).
_INTERSECTION_DATED_SQL = """
WITH candidates AS (
    SELECT r.id, r.date_recorded, r.party_name, r.parcel_id, r.property_address,
           r.mailing_address, r.phone, r.phone_type, r.email,
           r.phones, r.emails, r.property_key,
           sc.record_type, sc.county, sc.state, j.created_at AS job_created_at
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = :uid
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = :uid
    WHERE r.user_id = :uid
      AND r.property_key IS NOT NULL
      AND r.date_recorded_parsed IS NOT NULL
      AND sc.record_type = ANY(:types)
      AND (CAST(:filing_from AS date) IS NULL OR r.date_recorded_parsed >= CAST(:filing_from AS date))
      AND (CAST(:filing_to AS date) IS NULL OR r.date_recorded_parsed <= CAST(:filing_to AS date))
      {county_clause}
),
agg AS (
    SELECT property_key,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           count(DISTINCT record_type) AS overlap_count
    FROM candidates
    GROUP BY property_key
    HAVING count(DISTINCT record_type) = :n
),
ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.property_key
               ORDER BY (CASE WHEN c.phone IS NOT NULL OR c.email IS NOT NULL
                              THEN 0 ELSE 1 END),
                        c.job_created_at DESC NULLS LAST,
                        c.id DESC
           ) AS rn
    FROM candidates c
)
SELECT rk.id, rk.date_recorded, rk.party_name, rk.parcel_id, rk.property_address,
       rk.mailing_address, rk.county, rk.state, rk.phone, rk.phone_type, rk.email,
       rk.phones, rk.emails,
       a.matched_record_types, a.overlap_count
FROM ranked rk
JOIN agg a ON a.property_key = rk.property_key
WHERE rk.rn = 1
ORDER BY a.overlap_count DESC, rk.property_address NULLS LAST, rk.id
LIMIT :limit
"""


def _resolve_filing_window(
    lookback_days: int | None,
    filing_from: date | None,
    filing_to: date | None,
) -> tuple[date | None, date | None, bool]:
    """Resolve the optional filing-date window to (from, to, require_date).

    Precedence (Codex P2): an explicit filing_from/filing_to wins over
    lookback_days. lookback_days is the preset path: filing_from = today - days.
    require_date is True when ANY window is active (then NULL filing dates are
    excluded). All-None => (None, None, False) = today's all-time behavior.
    """
    if filing_from is not None or filing_to is not None:
        return filing_from, filing_to, True
    if lookback_days is not None:
        return datetime.now(UTC).date() - timedelta(days=lookback_days), None, True
    return None, None, False


_EXCLUDED_NO_DATE_SQL = """
SELECT count(*)
FROM results r
JOIN jobs j ON j.id = r.job_id AND j.user_id = :uid
JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = :uid
WHERE r.user_id = :uid
  AND sc.record_type = ANY(:types)
  AND r.date_recorded_parsed IS NULL
  {pk_clause}
  {county_clause}
"""


async def _count_excluded_no_date(
    db: AsyncSession,
    user_id: str,
    record_types: list[str],
    counties: list[str] | None,
    require_property_key: bool,
) -> int:
    """Count in-scope leads skipped because their filing date is NULL/unparseable.

    Only called when a window is active. Row-level count over the same base
    filters (user + record_types + counties [+ property_key for intersection]);
    surfaces a "N skipped (no filing date)" heads-up. In practice ~0 (the spike
    found date_recorded ~100% parseable)."""
    county_clause = "AND sc.county = ANY(:counties)" if counties else ""
    pk_clause = "AND r.property_key IS NOT NULL" if require_property_key else ""
    # Only fixed clause fragments are interpolated (no user data — values are bound
    # params), matching the .format pattern used for the other queries in this file.
    sql = text(_EXCLUDED_NO_DATE_SQL.format(pk_clause=pk_clause, county_clause=county_clause))
    params: dict = {"uid": user_id, "types": record_types}
    if counties:
        params["counties"] = counties
    return int((await db.execute(sql, params)).scalar_one())


def _decrypt_pii_rows(rows: list) -> list:
    """Decrypt the encrypted phone/email columns of a raw-SQL result.

    The segment queries are raw ``text()`` so the ``EncryptedString`` /
    ``EncryptedJSON`` types do NOT run — ``phone``/``email`` come back as
    ``fe1:`` ciphertext and ``phones``/``emails`` as encrypted JSON text.
    Decrypt them here, centrally, so every consumer (preview + export,
    intersection + union) receives plaintext and none can forget. Returns
    ``SimpleNamespace`` rows so existing attribute access (``r.phone``,
    ``r.party_name``, ...) is unchanged.
    ``party_name``/addresses are out of H3 scope (plaintext) and pass through.
    The contactability ranking + ``ORDER BY`` already ran in SQL over ciphertext,
    which is correct (ciphertext is non-NULL; blanks were normalized to NULL).
    """
    out = []
    for r in rows:
        data = dict(r._mapping)
        if data.get("phone") is not None:
            data["phone"] = decrypt_field(data["phone"])
        if data.get("email") is not None:
            data["email"] = decrypt_field(data["email"])
        # Multi-contact arrays (EncryptedJSON): decrypt + parse; a legacy
        # plaintext-JSON row passes through decrypt_field unchanged (non-strict)
        # and still parses. Anything unparseable degrades to None — the CSV
        # builder then just emits blank phone_2/3 + email_2/3 (never a 500).
        for key in ("phones", "emails"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                data[key] = json.loads(decrypt_field(raw))
            except (ValueError, TypeError):
                data[key] = None
        out.append(SimpleNamespace(**data))
    return out


async def _fetch_intersection(
    db: AsyncSession,
    user_id: str,
    record_types: list[str],
    counties: list[str] | None,
    limit: int,
    lookback_days: int | None = None,
    filing_from: date | None = None,
    filing_to: date | None = None,
) -> tuple[list, int]:
    """Return (representative lead rows for properties on ALL `record_types`,
    excluded_no_date_count).

    No window => the fast membership-backed all-time query (unchanged). A window
    => the results-based `_INTERSECTION_DATED_SQL` (the membership rollup has no
    filing date — Codex P1) and a count of NULL-filing-date in-scope leads.
    `db` is RLS-bound to the user. `record_types` is pre-validated to 2+ distinct,
    so len() == the distinct-type count.
    """
    ff, ft, require_date = _resolve_filing_window(lookback_days, filing_from, filing_to)
    county_clause = ""
    params: dict = {
        "uid": user_id,
        "types": record_types,
        # Distinct count so a duplicate type can't make HAVING = :n unsatisfiable
        # (the schema already dedupes; this is defense-in-depth — Codex P2).
        "n": len(set(record_types)),
        "limit": limit,
    }
    if counties:
        county_clause = "AND sc.county = ANY(:counties)"
        params["counties"] = counties

    if require_date:
        params["filing_from"] = ff
        params["filing_to"] = ft
        sql = text(_INTERSECTION_DATED_SQL.format(county_clause=county_clause))
        result = await db.execute(sql, params)
        rows = _decrypt_pii_rows(result.fetchall())
        excluded = await _count_excluded_no_date(
            db, user_id, record_types, counties, require_property_key=True
        )
        return rows, excluded

    sql = text(_INTERSECTION_SQL.format(county_clause=county_clause))
    result = await db.execute(sql, params)
    return _decrypt_pii_rows(result.fetchall()), 0


@router.post("/intersection", response_model=SegmentIntersectionResponse)
async def intersection_preview(
    body: SegmentIntersectionRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> SegmentIntersectionResponse:
    """JSON preview of the intersection ('on both lists'), capped at PREVIEW_CAP.

    Strong-identity only — the response says so via identity_strength.
    """
    await rate_limit(request, zone="general", identifier=current_user.id)

    # Fetch one extra row to detect truncation without a second count query.
    rows, excluded_no_date = await _fetch_intersection(
        db, str(current_user.id), body.record_types, body.counties, PREVIEW_CAP + 1,
        body.lookback_days, body.filing_from, body.filing_to,
    )
    truncated = len(rows) > PREVIEW_CAP
    rows = rows[:PREVIEW_CAP]

    return SegmentIntersectionResponse(
        record_types=body.record_types,
        counties=body.counties,
        property_count=len(rows),
        truncated=truncated,
        excluded_no_date_count=excluded_no_date,
        rows=[
            SegmentLeadRow(
                id=str(r.id),
                date_recorded=r.date_recorded,
                party_name=r.party_name,
                parcel_id=r.parcel_id,
                property_address=r.property_address,
                mailing_address=r.mailing_address,
                county=r.county,
                state=r.state,
                phone=r.phone,
                phone_type=r.phone_type,
                email=r.email,
                matched_record_types=list(r.matched_record_types or []),
                overlap_count=r.overlap_count,
            )
            for r in rows
        ],
    )


@router.post("/intersection/export")
async def intersection_export(
    body: SegmentIntersectionRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> Response:
    """CSV export of the intersection. One representative lead per property,
    with matched_record_types + overlap_count columns. CSV-injection sanitized.
    """
    await rate_limit(request, zone="general", identifier=current_user.id)

    rows, _excluded = await _fetch_intersection(
        db, str(current_user.id), body.record_types, body.counties, EXPORT_CAP,
        body.lookback_days, body.filing_from, body.filing_to,
    )
    if len(rows) >= EXPORT_CAP:
        _logger.warning(
            "Intersection export hit EXPORT_CAP=%d for user %s (types=%s) — truncated",
            EXPORT_CAP, current_user.id, body.record_types,
        )

    types_slug = "_".join(body.record_types)[:60]
    return _segment_csv_response(rows, f"bridgeleads_overlap_{types_slug}")


async def _fetch_union(
    db: AsyncSession,
    user_id: str,
    record_types: list[str],
    counties: list[str] | None,
    limit: int,
    lookback_days: int | None = None,
    filing_from: date | None = None,
    filing_to: date | None = None,
) -> tuple[list, int]:
    """Return (one representative deduped lead per bucket across `record_types`,
    excluded_no_date_count).

    Inclusive: strong rows deduped by property_key, weak by dedup_hash, neither
    kept as singletons — no lead dropped. Optional filing-date window via the
    same `_UNION_SQL` (predicates no-op when no window). `db` is RLS-bound.
    """
    ff, ft, require_date = _resolve_filing_window(lookback_days, filing_from, filing_to)
    params: dict = {
        "uid": user_id,
        "types": record_types,
        "limit": limit,
        "filing_from": ff,
        "filing_to": ft,
        "require_date": require_date,
    }
    county_clause = ""
    if counties:
        county_clause = "AND sc.county = ANY(:counties)"
        params["counties"] = counties

    sql = text(_UNION_SQL.format(county_clause=county_clause))
    result = await db.execute(sql, params)
    rows = _decrypt_pii_rows(result.fetchall())
    excluded = (
        await _count_excluded_no_date(
            db, user_id, record_types, counties, require_property_key=False
        )
        if require_date
        else 0
    )
    return rows, excluded


def _union_rows(rows: list) -> list[SegmentLeadRow]:
    return [
        SegmentLeadRow(
            id=str(r.id),
            date_recorded=r.date_recorded,
            party_name=r.party_name,
            parcel_id=r.parcel_id,
            property_address=r.property_address,
            mailing_address=r.mailing_address,
            county=r.county,
            state=r.state,
            phone=r.phone,
            phone_type=r.phone_type,
            email=r.email,
            matched_record_types=list(r.matched_record_types or []),
            overlap_count=r.overlap_count,
            identity_strength=r.identity_strength,
        )
        for r in rows
    ]


@router.post("/union", response_model=SegmentUnionResponse)
async def union_preview(
    body: SegmentUnionRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> SegmentUnionResponse:
    """JSON preview of the combined ('union') deduped lead set, capped at
    PREVIEW_CAP. Inclusive — each row carries its identity_strength."""
    await rate_limit(request, zone="general", identifier=current_user.id)

    rows, excluded_no_date = await _fetch_union(
        db, str(current_user.id), body.record_types, body.counties, PREVIEW_CAP + 1,
        body.lookback_days, body.filing_from, body.filing_to,
    )
    truncated = len(rows) > PREVIEW_CAP
    rows = rows[:PREVIEW_CAP]

    return SegmentUnionResponse(
        record_types=body.record_types,
        counties=body.counties,
        lead_count=len(rows),
        truncated=truncated,
        excluded_no_date_count=excluded_no_date,
        rows=_union_rows(rows),
    )


@router.post("/union/export")
async def union_export(
    body: SegmentUnionRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> Response:
    """CSV export of the combined ('union') deduped lead set. Adds an
    identity_strength column (strong|weak). CSV-injection sanitized."""
    await rate_limit(request, zone="general", identifier=current_user.id)

    rows, _excluded = await _fetch_union(
        db, str(current_user.id), body.record_types, body.counties, EXPORT_CAP,
        body.lookback_days, body.filing_from, body.filing_to,
    )
    if len(rows) >= EXPORT_CAP:
        _logger.warning(
            "Union export hit EXPORT_CAP=%d for user %s (types=%s) — truncated",
            EXPORT_CAP, current_user.id, body.record_types,
        )

    types_slug = "_".join(body.record_types)[:60]
    return _segment_csv_response(rows, f"bridgeleads_combined_{types_slug}")
