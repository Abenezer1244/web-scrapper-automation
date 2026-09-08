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

# Parcels per tick. Deliberately >= the phase-1 breaker window (50): the breaker
# counts within ONE call, so a sweep that asked for 20 at a time could fail every
# request forever and never fill the window that is supposed to stop it (Codex).
_BATCH_PARCELS = 60

# Hard wall-clock bound for one tick, comfortably inside the 10-minute schedule
# so ticks cannot pile up on each other.
_TICK_BUDGET_S = 240.0

# Single-flight lock TTL. Longer than the tick budget so a crashed tick's lock
# still expires on its own.
_LOCK_KEY = "bl:mailing_recovery:lock"
_LOCK_TTL_S = 600


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
_CANDIDATE_SQL = """
    SELECT r.id, r.user_id, r.parcel_id, r.enrichment_data
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
    ORDER BY coalesce((r.enrichment_data->>'mailing_recovery_attempts')::int, 0) ASC,
             coalesce(r.enrichment_data->>'mailing_recovery_last_at', '') ASC,
             r.id ASC
    LIMIT :limit
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


def _recover_impl(stats: dict) -> dict:
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import (
        KING_EREALPROPERTY,
        SourceUnavailableError,
        is_source_available,
    )

    deadline = time.monotonic() + _TICK_BUDGET_S

    with system_sync_session() as db:
        # Respect the same gate as every other caller. The canary, not this sweep,
        # is what decides King is back; a sweep that probed on its own would be a
        # second uncoordinated stream against a source we are backing off from.
        if not is_source_available(db, KING_EREALPROPERTY):
            stats["skipped"] = "king_erealproperty is in cooldown"
            return stats

        rows = db.execute(
            sa_text(_CANDIDATE_SQL),
            {"max_attempts": _MAX_ATTEMPTS, "limit": _BATCH_PARCELS * 2},
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
        parcels = list(by_parcel)[:_BATCH_PARCELS]
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
                        # Pace like a background job, not like a live one. Nothing
                        # is waiting on this, so it can afford to be gentle.
                        pace_s=1.0,
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

        # Parcels the time budget or a mid-run trip never reached must NOT be
        # charged an attempt: they were not tried, and burning the retry ceiling
        # on work that never happened is how a bounded sweep quietly abandons its
        # own backlog. They stay deferred with their counter untouched.
        never_tried = {p for p in king_stats.get("deferred", []) if p in by_parcel}
        attempted = [p for p in parcels if p not in never_tried]
        stats["parcels"] = len(attempted)
        stats["unreached"] = len(never_tried)
        _apply(db, by_parcel, attempted, enriched, stats)
    return stats


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
    try:
        result = db.execute(
            sa_text(
                "UPDATE results SET "
                "  mailing_address = COALESCE(mailing_address, :mailing), "
                # `results.enrichment_data` is JSON, not JSONB, and `||` is a
                # JSONB operator: merging without the casts raises
                # "COALESCE could not convert type jsonb to json". Cast in to
                # merge, cast back out to store.
                "  enrichment_data = (COALESCE(enrichment_data::jsonb, '{}'::jsonb) "
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


# ─── Celery task ─────────────────────────────────────────────────────────────

def _register() -> None:
    """Kept importable without a broker so tests can import this module freely."""


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.mailing_recovery.recover_deferred_mailing")
    def recover_deferred_mailing() -> dict:
        """Beat entry point: one bounded King mailing-recovery tick."""
        stats = recover_deferred_king_mailing()
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
