"""Background recovery of King property addresses a job could not look up.

WHY THIS EXISTS
---------------
King's GIS layer has no condominium UNIT features, so a unit's property address comes
from the Assessor condo extract (enrich._fill_king_condo_unit_situs) or, failing that,
from the per-parcel eRealProperty page. That page runs under a shared lease, a breaker
and a budget; when it does not run, the lead used to keep a blank property address
forever: King pre-foreclosure fell from 98.7% property addresses (2026-09-02) to 69.4%
(2026-09-13) with nothing retrying them. Since the property-recovery marker, a job marks
each such lead `property_lookup_deferred = true`, and this sweep reads that marker.

WHAT THIS IS NOT
----------------
A PROPERTY-ADDRESS FILL and nothing else, on the same boundary as the mailing and owner
sweeps:

  * It never creates a Job and never touches quota, billing, delivery, dedup_hash or
    parcel_id.
  * It never enqueues a skip trace and never touches skip_trace_status, phone or email.
    (A lead that gains an address here is not traced retroactively; that stays a
    deliberate customer action.)
  * It only fills a property address that is still empty, on a row that is still marked
    and still delivered, on a terminal King job. Each condition is re-checked in the
    UPDATE itself.
  * It never copies the mailing address into the property address.

ORDER OF SOURCES
----------------
1. The Assessor condo extract with the complex's corroborated locality: no request to
   King's rate-limited pages at all.
2. The eRealProperty page, for what the extract cannot answer, through
   batch_enrich_king_county: the shared SourceAdmission lease (one King pass at a time
   across jobs and sweeps), the source-health gate and the breaker. Property only
   (do_mailing=False).

WHICH LEADS
-----------
Only DELIVERED leads (non-duplicate, not excluded by the plan cap), and only leads a job
marked: historical leads are repaired by scripts/repair_king_property_situs.py, a
deliberate, reviewed run, never by this sweep.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime

from sqlalchemy import text as sa_text

from src.config import settings
from src.utils.logger import setup_logger
from src.workers.tasks_helpers.enrich import (
    KING_ACCOUNT_RESOLVER,
    PROPERTY_DEFERRED_KEY,
    PROPERTY_OUTCOME_KEY,
)

_logger = setup_logger("worker.property_recovery")

ATTEMPTS_KEY = "property_recovery_attempts"
LAST_AT_KEY = "property_recovery_last_at"

_MAX_ATTEMPTS = 5
_BATCH_PARCELS = 120
_TICK_BUDGET_S = 300.0
_PACE_S = 1.0

_LOCK_KEY = "bl:property_recovery:lock"
_LOCK_TTL_S = 1200

_TRAILING_ZIP_RE = re.compile(r"\s(\d{5})(?:-\d{4})?$")

# The PIN King sources are asked about: an exact account-number resolution, else the
# parcel as printed. Mirrors enrich._king_lookup_pin.
_LOOKUP_PIN = f"""(CASE WHEN r.enrichment_data->>'resolved_by' = '{KING_ACCOUNT_RESOLVER}'
                   THEN r.enrichment_data->>'resolved_parcel_id' ELSE btrim(r.parcel_id) END)"""

_ELIGIBLE_ROW = f"""
      r.is_duplicate = false
  AND r.enrichment_data->>'delivery_excluded_reason' IS NULL
  AND coalesce(r.enrichment_data->>'{PROPERTY_DEFERRED_KEY}', '') = 'true'
  AND coalesce(btrim(r.property_address), '') IN ('', '(enrichment unavailable)')
  AND {_LOOKUP_PIN} ~ '^[0-9]{{10}}$'
  AND (CASE WHEN r.enrichment_data->>'{ATTEMPTS_KEY}' ~ '^[0-9]{{1,6}}$'
            THEN (r.enrichment_data->>'{ATTEMPTS_KEY}')::int ELSE 0 END) < :max_attempts
"""

_KING_DONE_JOB = """
      j.status = 'done'
  AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
"""

_CANDIDATE_PARCELS_SQL = f"""
    SELECT {_LOOKUP_PIN} AS pin,
           min(CASE WHEN r.enrichment_data->>'{ATTEMPTS_KEY}' ~ '^[0-9]{{1,6}}$'
                    THEN (r.enrichment_data->>'{ATTEMPTS_KEY}')::int ELSE 0 END) AS attempts,
           min(coalesce(r.enrichment_data->>'{LAST_AT_KEY}', '')) AS last_at,
           max(r.created_at) AS newest
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_DONE_JOB} AND {_ELIGIBLE_ROW}
    GROUP BY {_LOOKUP_PIN}
    ORDER BY attempts ASC, last_at ASC, newest DESC, pin ASC
    LIMIT :batch
"""  # noqa: S608 -- splices only module constants; every value is bound

_CANDIDATE_ROWS_SQL = f"""
    SELECT r.id, r.user_id, r.parcel_id, {_LOOKUP_PIN} AS pin, r.mailing_address,
           r.property_city, r.property_state, r.property_zip, r.enrichment_data
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_DONE_JOB} AND {_ELIGIBLE_ROW}
      AND {_LOOKUP_PIN} = ANY(:pins)
    ORDER BY r.id
"""  # noqa: S608 -- splices only module constants; every value is bound

# Re-applies eligibility against the row and its job as they are NOW. The address
# columns move only when :address is set; the flags only then too.
_WRITE_SQL = f"""
    UPDATE results r SET
      property_address = COALESCE(CAST(:address AS varchar), r.property_address),
      property_city = CASE WHEN CAST(:address AS text) IS NULL THEN r.property_city
                           ELSE COALESCE(r.property_city, CAST(:city AS varchar)) END,
      property_state = CASE WHEN CAST(:address AS text) IS NULL THEN r.property_state
                            ELSE COALESCE(r.property_state, CAST(:state AS varchar)) END,
      property_zip = CASE WHEN CAST(:address AS text) IS NULL THEN r.property_zip
                          ELSE COALESCE(r.property_zip, CAST(:zip AS varchar)) END,
      owner_state = CASE WHEN CAST(:address AS text) IS NULL THEN r.owner_state
                         ELSE CAST(:f_owner_state AS varchar) END,
      absentee_owner = CASE WHEN CAST(:address AS text) IS NULL THEN r.absentee_owner
                            ELSE CAST(:f_absentee AS boolean) END,
      out_of_state_owner = CASE WHEN CAST(:address AS text) IS NULL THEN r.out_of_state_owner
                                ELSE CAST(:f_out_of_state AS boolean) END,
      enrichment_data = ((CASE WHEN jsonb_typeof(r.enrichment_data::jsonb) = 'object'
                               THEN r.enrichment_data::jsonb ELSE '{{}}'::jsonb END)
                         || CAST(:payload AS jsonb))::json
    WHERE r.id = :rid AND r.user_id = :uid AND r.parcel_id = :raw_pid
      AND r.mailing_address IS NOT DISTINCT FROM :old_mail
      AND {_ELIGIBLE_ROW}
      AND EXISTS (SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                  WHERE j.id = r.job_id AND {_KING_DONE_JOB})
"""  # noqa: S608 -- splices only module constants; every value is bound

_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def _acquire_lock() -> tuple | str:
    """Single-flight lock, failing CLOSED: a skipped tick costs nothing but a tick."""
    import uuid

    try:
        import redis as sync_redis

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        token = uuid.uuid4().hex
        if client.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_S):
            return client, token
        return "another tick is running"
    except Exception as exc:  # noqa: BLE001
        return f"lock unavailable: {type(exc).__name__}"


def _release_lock(lock: tuple) -> None:
    client, token = lock
    try:
        client.eval(_RELEASE_IF_OWNER, 1, _LOCK_KEY, token)
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


def recover_deferred_king_property() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"parcels": 0, "rows": 0, "found_extract": 0, "found_page": 0, "no_site_address": 0,
             "parcel_mismatch": 0, "transient": 0, "gave_up": 0, "unreached": 0, "stale": 0,
             "errors": 0, "skipped": ""}
    if not settings.PROPERTY_RECOVERY_ENABLED:
        stats["skipped"] = "PROPERTY_RECOVERY_ENABLED is off"
        return stats
    lock = _acquire_lock()
    if isinstance(lock, str):
        stats["skipped"] = lock
        return stats
    try:
        return _tick(stats)
    finally:
        _release_lock(lock)


def _tick(stats: dict) -> dict:
    from src.db.session import system_sync_session
    from src.scrapers.enrichment import county_gis, king_county_assessor
    from src.scrapers.enrichment.king_condo_units import (
        UnitSitus,
        complex_pin,
        compose_fill,
        resolve_units,
    )
    from src.scrapers.enrichment.source_health import (
        KING_EREALPROPERTY,
        SourceUnavailableError,
        is_source_available,
    )

    deadline = time.monotonic() + _TICK_BUDGET_S
    with system_sync_session() as db:
        params = {"max_attempts": _MAX_ATTEMPTS}
        pins = [r.pin for r in db.execute(sa_text(_CANDIDATE_PARCELS_SQL),
                                          {**params, "batch": _BATCH_PARCELS}).all()]
        if not pins:
            db.rollback()
            return stats
        rows = db.execute(sa_text(_CANDIDATE_ROWS_SQL), {**params, "pins": pins}).all()
        db.rollback()  # release the read snapshot before any file scan or network I/O
        by_pin: dict[str, list] = {}
        for row in rows:
            by_pin.setdefault(row.pin, []).append(row)
        pins = [p for p in pins if p in by_pin]
        stats["parcels"], stats["rows"] = len(pins), len(rows)
        if not pins:
            return stats

        # 1) The condo extract: no request to King's pages.
        units = {}
        resolved = resolve_units(set(pins))
        snapshot = None
        if resolved is not None:
            answers, snapshot = resolved
            units = {p: s for p, s in answers.items() if s.status == "found" and s.zip}
        complex_gis = {}
        if units:
            try:
                complex_gis = county_gis.batch_enrich_parcels_gis(
                    sorted({complex_pin(p) for p in units}), "king", "WA")
            except Exception as exc:  # noqa: BLE001 -- no locality means no fill
                if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                    raise
                _logger.warning("Property recovery: complex locality lookup failed: %s", str(exc)[:120])
        remaining = []
        for pin in pins:
            fill = compose_fill(units[pin], complex_gis.get(complex_pin(pin))) if pin in units else None
            if fill is None:
                remaining.append(pin)
                continue
            for row in by_pin[pin]:
                stats[_write(db, row, "found_extract", address=fill.property_address, city=fill.city,
                             state=fill.state, zip_=fill.zip, source="king_condo_unit",
                             extra={"property_source_snapshot": snapshot, "condo_unit_status": "found",
                                    "property_locality_source": f"king_gis_complex:{complex_pin(pin)}"})] += 1
        if not remaining:
            return stats

        # 2) The page, under the shared lease, property only.
        if not is_source_available(db, KING_EREALPROPERTY):
            stats["skipped"] = "king_erealproperty is in cooldown"
            stats["unreached"] += sum(len(by_pin[p]) for p in remaining)
            _rotate(db, [row for p in remaining for row in by_pin[p]])
            return stats
        enriched: dict = {}
        k_stats: dict = {}
        try:
            enriched = asyncio.run(asyncio.wait_for(
                king_county_assessor.batch_enrich_king_county(
                    remaining, time_budget_s=max(10.0, deadline - time.monotonic() - 30),
                    stats=k_stats, do_mailing=False, pace_s=_PACE_S),
                timeout=max(30.0, deadline - time.monotonic()),
            ))
        except SourceUnavailableError as exc:
            stats["skipped"] = f"stopped: {str(exc)[:120]}"
            _logger.warning("Property recovery: %s", stats["skipped"])
        except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            stats["skipped"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            _logger.warning("Property recovery: lookup failed: %s", stats["skipped"])

        requested = set(k_stats.get("requested_pids") or [])
        for pin in remaining:
            data = enriched.get(pin) or {}
            prop = (data.get("property_address") or "").strip() or None
            lookup = data.get("parcel_lookup")
            for row in by_pin[pin]:
                if prop and lookup == "verified":
                    # A unit's page line has no city; take it only with the same corroboration.
                    tail = _TRAILING_ZIP_RE.search(prop)
                    street = prop[: tail.start()] if tail else prop
                    fill = compose_fill(UnitSitus("found", street=street, zip=tail.group(1) if tail else None),
                                        complex_gis.get(complex_pin(pin)))
                    stats[_write(db, row, "found_page",
                                 address=fill.property_address if fill else prop,
                                 city=fill.city if fill else None, state=fill.state if fill else None,
                                 zip_=fill.zip if fill else (tail.group(1) if tail else None),
                                 source="king_erealproperty")] += 1
                elif lookup == "verified":
                    stats[_write(db, row, "no_site_address")] += 1
                elif lookup == "mismatch":
                    stats[_write(db, row, "parcel_mismatch")] += 1
                elif pin in requested:
                    stats[_write(db, row, "transient")] += 1
                else:
                    stats["unreached"] += 1
                    _rotate(db, [row])

    _logger.info("Property recovery: %s", {k: v for k, v in stats.items() if v})
    return stats


def _rotate(db, rows: list) -> None:
    """Move rows no request reached to the back of the queue without charging them."""
    now = datetime.now(UTC).isoformat()
    for row in rows:
        try:
            db.execute(sa_text(
                "UPDATE results SET enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object' "
                "THEN enrichment_data::jsonb ELSE '{}'::jsonb END) || CAST(:payload AS jsonb))::json "
                "WHERE id = :rid AND user_id = :uid"),
                {"rid": row.id, "uid": row.user_id, "payload": json.dumps({LAST_AT_KEY: now})})
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _logger.warning("Property recovery: could not rotate row %s: %s", str(row.id)[:8], str(exc)[:120])


def _write(db, row, outcome: str, *, address: str | None = None, city: str | None = None,
           state: str | None = None, zip_: str | None = None, source: str | None = None,
           extra: dict | None = None) -> str:
    """One guarded UPDATE per row. Returns the stats key: the outcome, gave_up, stale or errors."""
    ed = row.enrichment_data if isinstance(row.enrichment_data, dict) else {}
    try:
        attempts = int(ed.get(ATTEMPTS_KEY) or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    label = outcome
    payload: dict = {ATTEMPTS_KEY: attempts, LAST_AT_KEY: datetime.now(UTC).isoformat()}
    if outcome in ("found_extract", "found_page"):
        payload.update({PROPERTY_DEFERRED_KEY: False, PROPERTY_OUTCOME_KEY: "found",
                        "property_source": source, **(extra or {})})
    elif outcome in ("no_site_address", "parcel_mismatch"):
        payload.update({PROPERTY_DEFERRED_KEY: False, PROPERTY_OUTCOME_KEY: outcome})
    else:
        gave_up = attempts >= _MAX_ATTEMPTS
        payload.update({PROPERTY_DEFERRED_KEY: not gave_up,
                        PROPERTY_OUTCOME_KEY: "gave_up" if gave_up else "transient_failure"})
        label = "gave_up" if gave_up else "transient"
    payload = {k: v for k, v in payload.items() if v is not None}
    flags: dict = {}
    if address:
        from src.utils.address_intel import compute_owner_flags

        flags = compute_owner_flags(address, row.mailing_address,
                                    property_city=row.property_city or city,
                                    property_state=row.property_state or state,
                                    property_zip=row.property_zip or zip_)
    try:
        result = db.execute(sa_text(_WRITE_SQL), {
            "rid": row.id, "uid": row.user_id, "raw_pid": row.parcel_id, "old_mail": row.mailing_address,
            "address": address, "city": city, "state": state, "zip": zip_,
            "f_owner_state": flags.get("owner_state"), "f_absentee": flags.get("absentee_owner"),
            "f_out_of_state": flags.get("out_of_state_owner"),
            "payload": json.dumps(payload), "max_attempts": _MAX_ATTEMPTS,
        })
        db.commit()
        return label if result.rowcount else "stale"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _logger.warning("Property recovery: write failed for row %s: %s", str(row.id)[:8], str(exc)[:160])
        return "errors"


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.property_recovery.recover_deferred_property")
    def recover_deferred_property() -> dict:
        """Beat entry point: see recover_deferred_king_property."""
        return recover_deferred_king_property()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("Property recovery task NOT registered: %s", str(exc)[:120])
