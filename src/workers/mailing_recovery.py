"""Recovery for King mailing lookups that a source outage deferred.

WHY THIS EXISTS
---------------
`enrich.py` marks a parcel `enrichment_data["mailing_lookup_deferred"] = True`
when the mailing pass could not reach it, with the comment "so a later sweep can
find them (never a silent gap)". That sweep was never written. The marker was
written in exactly one place and read in none, so "deferred" meant PERMANENTLY
SKIPPED unless a human re-ran the job.

Measured on 2026-09-07: 153 parcels on the reported job, 17,107 on the King tax
job hours earlier, and 16,983 + 16,984 on 2026-09-04. None of them was ever
coming back.

WHAT THIS IS NOT
----------------
This is a MAILING-ADDRESS BACKFILL and nothing else. It deliberately does not
reuse the job enrichment pipeline, because that pipeline also settles billing,
finalises jobs and enqueues paid skip traces. The boundary is the whole point:

  * It never creates a Job and never touches quota, `billed_count`,
    `billing_applied_at`, `reserved_count` or `records_used`.
  * It never calls `_enqueue_skip_trace_rows`, so it cannot buy a Tracerfy
    lookup. Skip trace keys off `property_address`, which this never changes.
  * It never touches `skip_trace_status`, `phone`, `email`, `party_name`,
    `parcel_id`, `is_duplicate` or `dedup_hash`.
  * It only ever fills a mailing address that is currently NULL. It cannot
    overwrite a value another path already found.
  * It only considers rows on TERMINAL jobs. Adding a mailing address to a row on
    a live job could change `is_actionable` underneath a job that is mid-count,
    mid-bill or mid-export (Codex).

TERMINAL POLICY
---------------
Retrying forever would let permanently-unanswerable parcels ("poison rows")
monopolise every tick and starve the rest of the backlog (Codex). So each row
carries its own attempt count and last outcome, and stops being eligible when:

  * the source answered and there is genuinely no mailing address (`none`), or
  * `_MAX_ATTEMPTS` attempts have been made.

Either way the row keeps a durable record of WHY it stopped, so a lead with no
mailing address can be told apart from one that was never looked at.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from sqlalchemy import text as sa_text

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.mailing_recovery")

# JSON keys on Result.enrichment_data. No migration: these live beside the
# `mailing_lookup_deferred` marker the enrichment pass already writes.
DEFERRED_KEY = "mailing_lookup_deferred"
ATTEMPTS_KEY = "mailing_recovery_attempts"
LAST_AT_KEY = "mailing_recovery_last_at"
OUTCOME_KEY = "mailing_recovery_outcome"

# Give up after this many attempts. Five spread over five ticks is far more
# patience than a transient outage needs, and it bounds the work a permanently
# unanswerable parcel can consume.
_MAX_ATTEMPTS = 5

# Distinct parcels per tick, sized against MEASURED latency rather than against
# the breaker window. 60 did not fit: phase 2 costs 5-10 s per parcel, so 60
# needs 378-678 s against a 480 s tick and the overflow was pure waste (Codex).
# 30 needs roughly 189-339 s and completes. The breaker is NOT relied on to fill
# inside one sweep tick: the sweep is gated by source health, which the canary
# maintains, and a batch inflated purely to fill a 50-request window would cost
# King more requests than it saves.
_BATCH_PARCELS = 30

# Hard wall-clock bound for one tick, inside the 10-minute schedule so ticks
# cannot pile up on each other.
#
# Sized so a tick FINISHES its batch. A batch that phase 1 covers but phase 2
# cannot reach is pure waste: this sweep persists only the mailing address, so a
# phase-1 fetch whose parcel never reaches phase 2 bought nothing and still cost
# King a request. Measured phase-1 latency is ~0.3 s and the code's own estimate
# for a phase-2 Playwright lookup is 5-10 s, so at `_BATCH_PARCELS` = 60 and
# `_SWEEP_PACE_S` = 0.5 one tick needs roughly 60*(0.3+0.5) + 60*(5+0.5) ~= 378 s.
# 480 s covers that with headroom and still leaves 2 minutes before the next tick.
_TICK_BUDGET_S = 480.0

# Pace between requests inside the sweep. Slower than a live job (0.1 s), because
# nothing is waiting on this and King has blocked us before, but not so slow that
# phase 1 eats the budget phase 2 needs.
_SWEEP_PACE_S = 0.5

# Single-flight lock TTL. Longer than the tick budget so a crashed tick's lock
# still expires on its own, and long enough that a slow-but-live tick never has
# its lease expire underneath it.
_LOCK_KEY = "bl:mailing_recovery:lock"
_LOCK_TTL_S = 1200


def _now() -> datetime:
    return datetime.now(UTC)


def _acquire_single_flight() -> object | None:
    """Redis SET NX lock so overlapping ticks cannot accumulate workers.

    Returns the client (to release with) or None when another tick holds it. A
    Redis failure returns the client-less sentinel `False`, meaning "run anyway":
    the tick is already bounded by `_TICK_BUDGET_S` and by the source-health
    gate, so losing the lock is not worth skipping recovery over.
    """
    try:
        import redis as sync_redis

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        if client.set(_LOCK_KEY, str(_now()), nx=True, ex=_LOCK_TTL_S):
            return client
        return None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Mailing recovery: lock unavailable (%s) — running unlocked",
                        str(exc)[:120])
        return False


def _release_single_flight(client) -> None:
    if not client:
        return
    try:
        client.delete(_LOCK_KEY)
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


# Candidate rows: King, terminal job, still no mailing address, still marked
# deferred, and not past the attempt ceiling.
#
# ORDER BY attempts, then oldest attempt first. Ordering by id alone would let
# the same low-id rows be re-selected every tick and starve everything behind
# them (Codex); rotating by attempt count means every row gets its turn before
# any row gets a second one.
# Distinct eligible PARCELS first. The row limit used to be applied before
# deduplication, so 120 rows that happened to be four copies of each parcel
# yielded only 30 lookups — and a phase-1 pass of 30 can never fill the 50-request
# breaker window that is supposed to stop a developing outage (Codex).
_CANDIDATE_PARCELS_SQL = """
    SELECT DISTINCT ON (btrim(r.parcel_id)) btrim(r.parcel_id) AS parcel_id,
           coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) AS attempts,
           coalesce(r.enrichment_data->>'mailing_recovery_last_at', '') AS last_at
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) >= 6
      AND coalesce(r.enrichment_data->>'mailing_lookup_deferred', '') = 'true'
      AND coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) < :max_attempts
      AND j.status = 'done'
      AND lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
    ORDER BY btrim(r.parcel_id),
             coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) ASC,
             coalesce(r.enrichment_data->>'mailing_recovery_last_at', '') ASC,
             r.id ASC
"""

_CANDIDATE_SQL = """
    SELECT r.id, r.user_id, r.parcel_id, r.enrichment_data,
           r.property_address, r.property_city, r.property_state, r.property_zip
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) >= 6
      AND coalesce(r.enrichment_data->>'mailing_lookup_deferred', '') = 'true'
      AND coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) < :max_attempts
      AND j.status = 'done'
      AND lower(sc.county) = 'king'
      AND upper(sc.state) = 'WA'
      AND btrim(r.parcel_id) = ANY(:parcels)
    ORDER BY r.id ASC
"""


def recover_deferred_king_mailing() -> dict:
    """One bounded recovery tick. Returns a stats dict (also used by tests)."""
    stats = {"candidates": 0, "parcels": 0, "unreached": 0, "found": 0, "none": 0,
             "unverified": 0, "errors": 0, "skipped": ""}

    lock = _acquire_single_flight()
    if lock is None:
        stats["skipped"] = "another tick is running"
        return stats
    try:
        return _recover_impl(stats)
    finally:
        _release_single_flight(lock)


def _recover_impl(stats: dict, deadline: float | None = None) -> dict:
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import (
        KING_EREALPROPERTY,
        SourceUnavailableError,
        is_source_available,
    )

    if deadline is None:
        deadline = time.monotonic() + _TICK_BUDGET_S
    if deadline - time.monotonic() < 60:
        # Not enough of the shared tick left for even one paced King batch. The next
        # tick picks it up; starting now would only overrun the schedule.
        stats["skipped"] = "tick budget spent before the King sweep"
        return stats

    with system_sync_session() as db:
        # Respect the same gate as every other caller. The canary, not this sweep,
        # is what decides King is back; a sweep that probed on its own would be a
        # second uncoordinated stream against a source we are backing off from.
        if not is_source_available(db, KING_EREALPROPERTY):
            stats["skipped"] = "king_erealproperty is in cooldown"
            return stats

        # Pick the PARCELS first (fewest attempts, oldest attempt), then fetch
        # every eligible row naming them.
        parcel_rows = db.execute(
            sa_text(_CANDIDATE_PARCELS_SQL), {"max_attempts": _MAX_ATTEMPTS}
        ).all()
        parcels = [r.parcel_id for r in
                   sorted(parcel_rows, key=lambda r: (r.attempts, r.last_at, r.parcel_id))
                   ][:_BATCH_PARCELS]
        if not parcels:
            db.rollback()
            return stats
        rows = db.execute(
            sa_text(_CANDIDATE_SQL),
            {"max_attempts": _MAX_ATTEMPTS, "parcels": parcels},
        ).all()
        db.rollback()  # release the read snapshot before any network I/O
        if not rows:
            return stats
        stats["candidates"] = len(rows)

        # One lookup per PARCEL, applied to every row naming it. Two leads on one
        # parcel share one mailing address, so fetching per row would pay twice.
        by_parcel: dict[str, list] = {}
        for row in rows:
            by_parcel.setdefault(row.parcel_id.strip(), []).append(row)
        stats["parcels"] = len(parcels)

        _logger.info(
            "Mailing recovery: %d deferred row(s) across %d parcel(s) this tick",
            len(rows), len(parcels),
        )

        from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

        enriched: dict[str, dict] = {}
        king_stats: dict = {}
        try:
            enriched = asyncio.run(
                asyncio.wait_for(
                    batch_enrich_king_county(
                        parcels,
                        time_budget_s=max(10.0, deadline - time.monotonic() - 30),
                        stats=king_stats,
                        # Gentler than a live job (0.1 s): nothing is waiting on
                        # this and King has blocked us before. See _SWEEP_PACE_S
                        # for why it is not slower still.
                        pace_s=_SWEEP_PACE_S,
                    ),
                    timeout=max(30.0, deadline - time.monotonic()),
                )
            )
        except SourceUnavailableError as exc:
            # The breaker tripped mid-tick and persisted a fresh cooldown. Stop.
            stats["skipped"] = f"source became unavailable: {str(exc)[:120]}"
            _logger.warning("Mailing recovery: %s", stats["skipped"])
        except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
            stats["skipped"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            _logger.warning("Mailing recovery: lookup failed: %s", stats["skipped"])

        # Charge an attempt ONLY on positive evidence that a request was issued.
        #
        # Deriving it as "everything not in `unreached`" was wrong on every
        # exceptional exit (Codex): if the batch raises after seeding `unreached`
        # empty — a mid-run health block, a browser that fails to start, a
        # cancellation — then nothing is in `unreached` and EVERY selected parcel
        # looks attempted. Five such ticks would exhaust the ceiling and clear the
        # marker on parcels that were never once looked up, which is precisely the
        # silent permanent loss this whole change exists to end.
        #
        # `attempted` is appended the moment a request is issued, into the
        # caller-owned stats dict, so it survives a cancelled coroutine.
        # MAILING attempts only (Codex). Charging off all-phase `attempted` meant a
        # parcel whose phase 1 succeeded but which phase 2 never reached spent a
        # mailing retry with no mailing request ever made; five such ticks
        # terminalised it unlooked-at. A phase-1 failure leaves the parcel
        # deferred and uncharged, which is safe: if phase 1 keeps failing, the
        # breaker and the health gate stop the sweep entirely rather than letting
        # it spin.
        attempted = [p for p in dict.fromkeys(king_stats.get("mailing_attempted_pids", []))
                     if p in by_parcel]
        stats["parcels"] = len(attempted)
        stats["unreached"] = len(parcels) - len(attempted)
        _apply(db, by_parcel, attempted, enriched, stats)

        # Parcels we REQUESTED but could not mailing-attempt (phase 1 failed, or
        # the budget ran out before phase 2 reached them) keep their attempt count
        # — they have not spent a mailing retry — but they must not keep their
        # place at the head of the queue. Ordering is (attempts, last_at), so
        # without touching last_at the same thirty unresolvable parcels are the
        # first thirty candidates on every single tick and starve the entire
        # backlog behind them (Codex). Touching the timestamp rotates them.
        _touch = [p for p in parcels if p not in set(attempted)]
        if _touch:
            _rotate(db, by_parcel, _touch, stats)
    return stats


def _rotate(db, by_parcel: dict, parcels: list[str], stats: dict) -> None:
    """Move un-attempted parcels to the back of the queue without charging them."""
    now_iso = _now().isoformat()
    payload = _json({LAST_AT_KEY: now_iso})
    moved = 0
    for pid in parcels:
        for row in by_parcel.get(pid, []):
            try:
                db.execute(
                    sa_text(
                        "UPDATE results SET enrichment_data = "
                        "  ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object' THEN enrichment_data::jsonb ELSE '{}'::jsonb END) "
                        "   || CAST(:payload AS jsonb))::json "
                        "WHERE id = :rid AND user_id = :uid AND mailing_address IS NULL"
                    ),
                    {"rid": row.id, "uid": row.user_id, "payload": payload},
                )
                db.commit()
                moved += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                _logger.warning(
                    "Mailing recovery: could not rotate row %s: %s",
                    str(row.id)[:8], str(exc)[:120],
                )
    if moved:
        _logger.info(
            "Mailing recovery: rotated %d row(s) across %d un-attempted parcel(s) "
            "to the back of the queue", moved, len(parcels),
        )


def _apply(db, by_parcel: dict, parcels: list[str], enriched: dict, stats: dict) -> None:
    """Write outcomes back, one row at a time, each under its own guard."""
    now_iso = _now().isoformat()
    for pid in parcels:
        data = enriched.get(pid) or {}
        mailing = (data.get("mailing_address") or "").strip() or None
        lookup = data.get("mailing_lookup") or "error"

        if mailing:
            outcome = "found"
        elif lookup == "none":
            outcome = "none"                 # source answered: no mailing address
        elif lookup == "identity_unverified":
            outcome = "identity_unverified"  # page never named this parcel
        else:
            outcome = "error"                # unknown; try again next tick

        for row in by_parcel[pid]:
            attempts = 0
            if isinstance(row.enrichment_data, dict):
                try:
                    attempts = int(row.enrichment_data.get(ATTEMPTS_KEY) or 0)
                except (TypeError, ValueError):
                    attempts = 0
            attempts += 1
            # Terminal when the source gave a real answer of "no mailing address",
            # or when we have asked enough times. Anything else stays eligible.
            terminal = outcome in ("found", "none") or attempts >= _MAX_ATTEMPTS
            _write_row(db, row, mailing, outcome, attempts, terminal, now_iso, stats)

        stats[{"found": "found", "none": "none",
               "identity_unverified": "unverified"}.get(outcome, "errors")] += 1


def _write_row(db, row, mailing, outcome, attempts, terminal, now_iso, stats) -> None:
    """Conditional single-row write. Never overwrites, never widens its blast radius.

    The guard is the WHERE clause, not a read-then-write: this tick's lookup began
    before the write, and another path (a job re-run, a manual repair) may have
    filled the mailing address or repaired the parcel in between. Committing on a
    stale read would let a lookup for the OLD parcel land on the new one.

    The merge base is the existing value only when it is a JSON OBJECT. A row written
    with `enrichment_data=None` through the ORM stores JSON `null`, which COALESCE does
    not catch, and `'null'::jsonb || '{...}'` builds an ARRAY: the marker would land
    inside `[null, {...}]` where `->>` cannot see it and the row would never recover.

    `jsonb_strip_nulls` is not used and the whole column is not replaced: the
    bookkeeping keys are merged with `||` so unrelated enrichment metadata
    (situs parts, parcel provenance, assessor owner, delivery exclusion) survives.
    """
    payload = {
        ATTEMPTS_KEY: attempts,
        LAST_AT_KEY: now_iso,
        OUTCOME_KEY: outcome,
        DEFERRED_KEY: not terminal,
    }
    # The owner-location flags are derived from the mailing address, and the job's
    # own recompute (tasks.py) ran while it was still NULL. Without this a recovered
    # absentee owner never reaches the absentee / out-of-state filters. Computed only
    # from the value this UPDATE writes: the `mailing_address IS NULL` guard below
    # makes the write a no-op if anything else filled it first, so the flags can never
    # describe a different address than the stored one.
    flags: dict = {}
    if mailing:
        from src.utils.address_intel import compute_owner_flags

        flags = compute_owner_flags(
            getattr(row, "property_address", None), mailing,
            property_city=getattr(row, "property_city", None),
            property_state=getattr(row, "property_state", None),
            property_zip=getattr(row, "property_zip", None),
        )
    try:
        result = db.execute(
            sa_text(
                "UPDATE results SET "
                # Flags move only when this write supplies a mailing address.
                "  property_state = CASE WHEN CAST(:mailing AS text) IS NOT NULL "
                "    THEN CAST(:f_property_state AS varchar) ELSE property_state END, "
                "  owner_state = CASE WHEN CAST(:mailing AS text) IS NOT NULL "
                "    THEN CAST(:f_owner_state AS varchar) ELSE owner_state END, "
                "  absentee_owner = CASE WHEN CAST(:mailing AS text) IS NOT NULL "
                "    THEN CAST(:f_absentee AS boolean) ELSE absentee_owner END, "
                "  out_of_state_owner = CASE WHEN CAST(:mailing AS text) IS NOT NULL "
                "    THEN CAST(:f_out_of_state AS boolean) ELSE out_of_state_owner END, "
                "  mailing_address = COALESCE(mailing_address, :mailing), "
                # `results.enrichment_data` is JSON, not JSONB, and `||` is a
                # JSONB operator: merging without the casts raises
                # "COALESCE could not convert type jsonb to json". Cast in to
                # merge, cast back out to store.
                "  enrichment_data = ((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object' THEN enrichment_data::jsonb ELSE '{}'::jsonb END) "
                "                     || CAST(:payload AS jsonb))::json "
                "WHERE id = :rid "
                "  AND user_id = :uid "          # tenant filter, always
                "  AND parcel_id = :pid "        # the parcel we actually looked up
                "  AND mailing_address IS NULL"  # never overwrite a real value
            ),
            {
                "rid": row.id,
                "uid": row.user_id,
                "pid": row.parcel_id,
                "mailing": mailing,
                "payload": _json(payload),
                "f_property_state": flags.get("property_state"),
                "f_owner_state": flags.get("owner_state"),
                "f_absentee": flags.get("absentee_owner"),
                "f_out_of_state": flags.get("out_of_state_owner"),
            },
        )
        db.commit()
        if not result.rowcount:
            _logger.info(
                "Mailing recovery: row %s changed under us — skipped", str(row.id)[:8]
            )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        stats["errors"] += 1
        _logger.warning(
            "Mailing recovery: write failed for row %s: %s", str(row.id)[:8], str(exc)[:160]
        )


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload)


# ─── County GIS mailing recovery ─────────────────────────────────────────────
#
# The same backfill contract as King above, for counties whose own ArcGIS parcel
# layer publishes the owner's mailing address (Snohomish, Cowlitz, Pierce). Rows get
# here two ways: a live job whose county request failed marks them deferred
# (enrich.py), and the historical repair script marks rows from before those
# counties had a mailing source at all.
#
# No source-health gate: these are public bulk ArcGIS layers answering 50 parcels per
# request, not a per-parcel page scrape that has blocked us. The bound is the batch
# size and the attempt ceiling, and one tick is a handful of requests per county.
_GIS_BATCH_PARCELS = 200

_GIS_CANDIDATE_PARCELS_SQL = """
    SELECT DISTINCT ON (lower(sc.county), btrim(r.parcel_id))
           lower(sc.county) AS county, btrim(r.parcel_id) AS parcel_id,
           coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) AS attempts,
           coalesce(r.enrichment_data->>'mailing_recovery_last_at', '') AS last_at
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) >= 6
      AND coalesce(r.enrichment_data->>'mailing_lookup_deferred', '') = 'true'
      AND coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) < :max_attempts
      AND j.status = 'done'
      AND lower(sc.county) = ANY(:counties)
      AND upper(sc.state) = 'WA'
    ORDER BY lower(sc.county), btrim(r.parcel_id),
             coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) ASC,
             r.id ASC
"""

_GIS_CANDIDATE_SQL = """
    SELECT r.id, r.user_id, r.parcel_id, r.enrichment_data,
           r.property_address, r.property_city, r.property_state, r.property_zip
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND coalesce(r.enrichment_data->>'mailing_lookup_deferred', '') = 'true'
      AND coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) < :max_attempts
      AND j.status = 'done'
      AND lower(sc.county) = :county
      AND upper(sc.state) = 'WA'
      AND btrim(r.parcel_id) = ANY(:parcels)
    ORDER BY r.id ASC
"""


def recover_deferred_gis_mailing() -> dict:
    """One bounded recovery tick for county-GIS mailing sources."""
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.county_gis import (
        batch_enrich_parcels_gis,
        gis_mailing_source_counties,
    )

    stats = {"candidates": 0, "parcels": 0, "found": 0, "none": 0, "unverified": 0,
             "errors": 0}
    counties = gis_mailing_source_counties("WA")
    if not counties:
        return stats

    with system_sync_session() as db:
        parcel_rows = db.execute(
            sa_text(_GIS_CANDIDATE_PARCELS_SQL),
            {"max_attempts": _MAX_ATTEMPTS, "counties": counties},
        ).all()
        picked = sorted(parcel_rows,
                        key=lambda r: (r.attempts, r.last_at, r.county, r.parcel_id))
        picked = picked[:_GIS_BATCH_PARCELS]
        by_county: dict[str, list[str]] = {}
        for row in picked:
            by_county.setdefault(row.county, []).append(row.parcel_id)
        rows_by_county: dict[str, list] = {}
        for county, parcels in by_county.items():
            rows_by_county[county] = db.execute(
                sa_text(_GIS_CANDIDATE_SQL),
                {"max_attempts": _MAX_ATTEMPTS, "county": county, "parcels": parcels},
            ).all()
        db.rollback()  # release the read snapshot before any network I/O

        for county, parcels in by_county.items():
            rows = rows_by_county.get(county) or []
            if not rows:
                continue
            stats["candidates"] += len(rows)
            by_parcel: dict[str, list] = {}
            for row in rows:
                by_parcel.setdefault(row.parcel_id.strip(), []).append(row)

            gis_stats: dict = {}
            try:
                found = batch_enrich_parcels_gis(parcels, county, "WA", stats=gis_stats)
            except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
                _logger.warning("GIS mailing recovery: %s lookup failed: %s",
                                county, str(exc)[:120])
                continue

            # A parcel whose county request failed was not looked up: it keeps its
            # attempt count and only rotates to the back of the queue, exactly like an
            # un-attempted King parcel.
            unreached = {p for p in gis_stats.get("county_unreached", []) if p in by_parcel}
            attempted = [p for p in parcels if p in by_parcel and p not in unreached]
            enriched = {
                pid: {
                    "mailing_address": (found.get(pid) or {}).get("mailing_address"),
                    # The county answered this request. No mailing address in that
                    # answer, matched or not, is a real "none" from the source.
                    "mailing_lookup": "none",
                }
                for pid in attempted
            }
            stats["parcels"] += len(attempted)
            _apply(db, by_parcel, attempted, enriched, stats)
            if unreached:
                _rotate(db, by_parcel, sorted(unreached), stats)
    return stats


def run_mailing_recovery_tick() -> dict:
    """One beat tick: the county-GIS sweep, then the King sweep, under ONE lock.

    GIS first because it is a few bulk requests and must not wait behind King's
    up-to-480 s page-scrape tick, nor be skipped while King is in cooldown. Both sit
    inside the single-flight lock: a tick that overran the 10-minute interval must not
    let the next one re-select and re-request the same deferred GIS rows (Codex P2).
    """
    king = {"candidates": 0, "parcels": 0, "unreached": 0, "found": 0, "none": 0,
            "unverified": 0, "errors": 0, "skipped": ""}
    lock = _acquire_single_flight()
    if lock is None:
        king["skipped"] = "another tick is running"
        return {"gis": {}, "king": king}
    try:
        # ONE budget for the whole tick (Codex P2): the King sweep gets what the GIS
        # sweep left, not a fresh 480 s, so a slow GIS pass cannot push the tick past
        # the 10-minute schedule and make the next beat skip on the lock.
        deadline = time.monotonic() + _TICK_BUDGET_S
        gis: dict = {}
        try:
            gis = recover_deferred_gis_mailing()
        except Exception as exc:  # noqa: BLE001 -- must not block the King tick
            _logger.warning("GIS mailing recovery tick failed: %s", str(exc)[:160])
        return {"gis": gis, "king": _recover_impl(king, deadline=deadline)}
    finally:
        _release_single_flight(lock)


# ─── Celery task ─────────────────────────────────────────────────────────────

def _register() -> None:
    """Kept importable without a broker so tests can import this module freely."""


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.mailing_recovery.recover_deferred_mailing")
    def recover_deferred_mailing() -> dict:
        """Beat entry point: see run_mailing_recovery_tick."""
        tick = run_mailing_recovery_tick()
        gis_stats = tick["gis"]
        if gis_stats.get("parcels"):
            _logger.info(
                "GIS mailing recovery: %d parcel(s) looked up, %d found, %d none, "
                "%d error", gis_stats["parcels"], gis_stats["found"],
                gis_stats["none"], gis_stats["errors"],
            )
        stats = tick["king"]
        if stats.get("skipped"):
            _logger.info("Mailing recovery skipped: %s", stats["skipped"])
        elif stats.get("parcels"):
            _logger.info(
                "Mailing recovery: %d parcel(s) attempted — %d found, %d none, "
                "%d identity-unverified, %d error, %d unreached",
                stats["parcels"], stats["found"], stats["none"],
                stats["unverified"], stats["errors"], stats["unreached"],
            )
        return stats
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.debug("Mailing recovery task not registered: %s", str(exc)[:120])
