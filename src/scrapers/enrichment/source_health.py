"""Shared health state for EXTERNAL enrichment sources.

A per-run circuit breaker only stops the run that noticed. This is the durable,
cross-process flag: once a source is marked unhealthy, every worker, scheduled
job and backfill skips it until a canary clears it.

Born from a real incident — a bulk owner backfill got our production IP
rate-blocked by King County's eRealProperty, and nothing stopped the next process
from starting again on the same blocked source.

Design notes:
  * A source with NO row is healthy. The happy path does one indexed PK read and
    never writes, so this stays off the hot path.
  * `cooldown_until` is the single enforcement field. Callers do not reason about
    status strings; they call `assert_source_available()` and get an exception.
  * Cooldown escalates 24h -> 48h -> 72h (capped) per consecutive failed probe,
    so a source that stays angry is asked less often, not more.
  * Every write is best-effort and never raises into the caller: failing to
    RECORD a block must not also break the job that discovered it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger("enrichment.source_health")

# Stable slugs. Add a constant here rather than passing raw strings around, so a
# typo can't silently create a second health row for the same source.
KING_EREALPROPERTY = "king_erealproperty"

# 24h first, then 48h, then 72h for every subsequent failed probe.
_COOLDOWN_LADDER_HOURS = (24, 48, 72)

_UNHEALTHY = ("throttled", "blocked")


class SourceUnavailableError(RuntimeError):
    """Raised when a source is in cooldown. Callers should degrade, not retry."""

    def __init__(self, source_key: str, status: str, until: datetime | None, reason: str | None):
        self.source_key = source_key
        self.status = status
        self.cooldown_until = until
        super().__init__(
            f"{source_key} is {status} until {until.isoformat() if until else 'cleared'}"
            f"{f' — {reason}' if reason else ''}"
        )


def cooldown_for(consecutive_failures: int) -> timedelta:
    """Escalating cooldown; index 0 is the first block."""
    idx = min(max(consecutive_failures, 0), len(_COOLDOWN_LADDER_HOURS) - 1)
    return timedelta(hours=_COOLDOWN_LADDER_HOURS[idx])


def _row(db, source_key: str):
    return db.execute(
        text(
            "SELECT source_key, status, reason, first_seen_at, cooldown_until, "
            "last_probe_at, last_success_at, consecutive_probe_failures "
            "FROM external_source_health WHERE source_key = :k"
        ),
        {"k": source_key},
    ).first()


def get_source_state(db, source_key: str) -> dict | None:
    """Current state, or None when the source has never had a problem."""
    r = _row(db, source_key)
    return dict(r._mapping) if r else None


def is_source_available(db, source_key: str) -> bool:
    """True when the source may be called right now.

    Unhealthy but PAST its cooldown counts as available — that is exactly the
    window in which the canary is meant to probe it.
    """
    r = _row(db, source_key)
    if r is None or r.status not in _UNHEALTHY:
        return True
    if r.cooldown_until is None:
        return False
    return datetime.now(UTC) >= r.cooldown_until


def assert_source_available(db, source_key: str) -> None:
    """Raise SourceUnavailableError if the source is in cooldown."""
    if is_source_available(db, source_key):
        return
    r = _row(db, source_key)
    raise SourceUnavailableError(source_key, r.status, r.cooldown_until, r.reason)


def mark_source_unhealthy(db, source_key: str, reason: str, status: str = "throttled") -> None:
    """Record that a source is refusing us, and start/extend its cooldown.

    `first_seen_at` is preserved across repeated marks so the true length of an
    outage stays visible; `consecutive_probe_failures` drives the escalation.
    Best-effort: never raises into the caller.
    """
    try:
        now = datetime.now(UTC)
        existing = _row(db, source_key)
        failures = (existing.consecutive_probe_failures if existing else 0) or 0
        # Only escalate when it was ALREADY unhealthy — a fresh block starts at
        # the bottom of the ladder rather than inheriting an old streak.
        if existing is None or existing.status not in _UNHEALTHY:
            failures = 0
        until = now + cooldown_for(failures)
        db.execute(
            text(
                "INSERT INTO external_source_health "
                "  (source_key, status, reason, first_seen_at, cooldown_until, "
                "   consecutive_probe_failures, updated_at) "
                "VALUES (:k, :s, :r, :now, :until, :f, :now) "
                "ON CONFLICT (source_key) DO UPDATE SET "
                "  status = EXCLUDED.status, "
                "  reason = EXCLUDED.reason, "
                "  cooldown_until = EXCLUDED.cooldown_until, "
                "  consecutive_probe_failures = EXCLUDED.consecutive_probe_failures, "
                "  updated_at = EXCLUDED.updated_at, "
                # Keep the ORIGINAL first_seen_at unless the source was healthy.
                "  first_seen_at = CASE WHEN external_source_health.status IN "
                "      ('throttled','blocked') THEN external_source_health.first_seen_at "
                "      ELSE EXCLUDED.first_seen_at END"
            ),
            {"k": source_key, "s": status, "r": reason[:512], "now": now,
             "until": until, "f": failures},
        )
        db.commit()
        _logger.warning(
            "Source %s marked %s until %s — %s", source_key, status, until.isoformat(), reason[:180]
        )
    except Exception as exc:  # noqa: BLE001 — recording a block must not break the caller
        _logger.error("Could not record %s health: %s", source_key, str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass


def mark_probe_failed(db, source_key: str, reason: str) -> None:
    """A canary probe failed — extend the cooldown one rung up the ladder."""
    try:
        now = datetime.now(UTC)
        existing = _row(db, source_key)
        failures = ((existing.consecutive_probe_failures if existing else 0) or 0) + 1
        until = now + cooldown_for(failures)
        db.execute(
            text(
                "UPDATE external_source_health SET "
                "  consecutive_probe_failures = :f, cooldown_until = :until, "
                "  last_probe_at = :now, reason = :r, updated_at = :now "
                "WHERE source_key = :k"
            ),
            {"k": source_key, "f": failures, "until": until, "now": now, "r": reason[:512]},
        )
        db.commit()
        _logger.warning(
            "Source %s still unavailable (probe #%d) — next attempt after %s",
            source_key, failures, until.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.error("Could not record %s probe failure: %s", source_key, str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass


def mark_source_healthy(db, source_key: str) -> bool:
    """Clear a source. Returns True if this was a RECOVERY (was unhealthy)."""
    try:
        now = datetime.now(UTC)
        existing = _row(db, source_key)
        was_unhealthy = bool(existing and existing.status in _UNHEALTHY)
        db.execute(
            text(
                "INSERT INTO external_source_health "
                "  (source_key, status, reason, cooldown_until, last_probe_at, "
                "   last_success_at, consecutive_probe_failures, updated_at) "
                "VALUES (:k, 'healthy', NULL, NULL, :now, :now, 0, :now) "
                "ON CONFLICT (source_key) DO UPDATE SET "
                "  status = 'healthy', reason = NULL, cooldown_until = NULL, "
                "  first_seen_at = NULL, last_probe_at = :now, last_success_at = :now, "
                "  consecutive_probe_failures = 0, updated_at = :now"
            ),
            {"k": source_key, "now": now},
        )
        db.commit()
        if was_unhealthy:
            _logger.info("Source %s RECOVERED", source_key)
        return was_unhealthy
    except Exception as exc:  # noqa: BLE001
        _logger.error("Could not clear %s health: %s", source_key, str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
        return False


def sources_due_for_probe(db) -> list[str]:
    """Unhealthy sources whose cooldown has expired — the canary's work list."""
    rows = db.execute(
        text(
            "SELECT source_key FROM external_source_health "
            "WHERE status IN ('throttled','blocked') "
            "AND (cooldown_until IS NULL OR cooldown_until <= :now) "
            "ORDER BY cooldown_until NULLS FIRST"
        ),
        {"now": datetime.now(UTC)},
    ).all()
    return [r.source_key for r in rows]


# ─── Self-contained wrappers ──────────────────────────────────────────────────
# The enrichment modules are async and hold no DB session, so these open their
# own short-lived system session. Kept separate from the db-taking functions
# above so tests can drive the logic directly without a session factory.
#
# The reads are a single indexed PK lookup on a table with one row per source, and
# they run once per BATCH (not per parcel), so the cost is irrelevant next to the
# HTTP call they are guarding.


def check_source_or_raise(source_key: str) -> None:
    """Raise SourceUnavailableError if `source_key` is in cooldown.

    Call at the top of every public entry point that talks to the source — that
    is what makes it impossible for a new call site to forget the check.
    A failure to READ health must never block real work, so an infrastructure
    error here falls through to "allowed" rather than failing closed.
    """
    from src.db.session import system_sync_session

    try:
        with system_sync_session() as db:
            assert_source_available(db, source_key)
    except SourceUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        _logger.error("Source health check failed for %s: %s", source_key, str(exc)[:200])


def record_source_blocked(source_key: str, reason: str, status: str = "throttled") -> None:
    """Mark a source unhealthy from a context with no session (best-effort)."""
    from src.db.session import system_sync_session

    try:
        with system_sync_session() as db:
            mark_source_unhealthy(db, source_key, reason, status)
    except Exception as exc:  # noqa: BLE001
        _logger.error("Could not record %s as blocked: %s", source_key, str(exc)[:200])
