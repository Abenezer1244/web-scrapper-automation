"""Database latency canary: an ops alert before the customers notice.

On 2026-10-01 the database was degraded for about six hours (simple queries
taking 12-43 s) before it stopped answering at all, and the outage was found by
a person who could not log in. The same had happened on 2026-07-28. Nothing in
the system was watching how long the database took to answer.

Every 2 minutes this opens ONE fresh connection on ``canary_engine`` (the API's
path, including the pooler login that failed) and runs ``SELECT 1``. A probe is
bad when it fails or takes longer than ``_SLOW_SECONDS``. After
``_ALERT_AFTER`` bad probes in a row it sends an ops alert; send_ops_alert's
cooldown keeps a long outage to one e-mail per cooldown window. One good probe
resets the streak, so a single slow blip never alerts.

The streak lives in Redis. When Redis is unavailable the canary logs and does
NOT alert: it cannot tell a blip from an outage, and the alert cooldown fails
open, so alerting then would mean an e-mail every 2 minutes.

This is the second line of detection. The first is an external uptime monitor
on GET /ready, which does not share Railway, Redis or this worker.
"""

from __future__ import annotations

import time

from sqlalchemy import text

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.db_canary")

# A healthy SELECT 1 on a fresh connection takes well under a second. During the
# 2026-10-01 degradation a 15-row scan took 32 s.
_SLOW_SECONDS = 3.0
_ALERT_AFTER = 3          # consecutive bad probes, i.e. ~6 minutes at the 2-minute beat
_STREAK_KEY = "bl:db_canary:bad_streak"
_STREAK_TTL_S = 3600      # a streak older than this is stale, start over


def _probe() -> tuple[bool, float, str | None]:
    """(answered, seconds, error class name). Never raises."""
    import src.db.session as db_session

    started = time.monotonic()
    try:
        with db_session.canary_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — a canary reports, it never raises
        return False, time.monotonic() - started, type(exc).__name__
    return True, time.monotonic() - started, None


def _redis_client():
    """Short socket timeouts: a hung Redis must not keep the task running into
    the next tick."""
    import redis as sync_redis

    return sync_redis.from_url(
        settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2,
        **settings.redis_kwargs(),
    )


def _update_streak(bad: bool) -> tuple[int, int] | None:
    """(streak after this probe, streak before it), or None when Redis is unavailable.

    One MULTI transaction each way, so a crash cannot leave a streak with no
    expiry."""
    try:
        client = _redis_client()
        try:
            pipe = client.pipeline(transaction=True)
            if bad:
                pipe.incr(_STREAK_KEY)
                pipe.expire(_STREAK_KEY, _STREAK_TTL_S)
                streak = int(pipe.execute()[0])
                return streak, streak - 1
            pipe.get(_STREAK_KEY)
            pipe.delete(_STREAK_KEY)
            previous = pipe.execute()[0]
            return 0, int(previous or 0)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("DB canary: streak unavailable (%s), not alerting", type(exc).__name__)
        return None


def _announce_recovery(previous: int) -> bool:
    """The database answers again after an alerted outage: say so, and clear the
    outage alert's cooldown. Without the clear, an outage that came back inside
    the cooldown window would reach the alert threshold and send nothing."""
    from src.workers.ops_alerts import _COOLDOWN_PREFIX, send_ops_alert

    try:
        client = _redis_client()
        try:
            client.delete(f"{_COOLDOWN_PREFIX}db_latency:primary")
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — the notice still goes out
        _logger.warning("DB canary: could not clear the alert cooldown (%s)", type(exc).__name__)
    return send_ops_alert(
        "db_latency",
        "recovered",
        "Database answering again",
        f"SELECT 1 answered within {_SLOW_SECONDS:.0f}s after {previous} bad probes in a row.\n"
        "Confirm https://api.bridgeleads.io/ready returns 200.",
    )


def run_db_latency_canary() -> dict:
    answered, seconds, error = _probe()
    bad = not answered or seconds > _SLOW_SECONDS
    counted = _update_streak(bad)
    streak = counted[0] if counted else None
    stats = {
        "answered": answered,
        "seconds": round(seconds, 3),
        "error": error,
        "streak": streak,
        "alerted": False,
    }
    if not bad:
        if counted and counted[1] >= _ALERT_AFTER:
            stats["alerted"] = _announce_recovery(counted[1])
        return stats

    _logger.warning(
        "DB canary: %s after %.1fs (streak %s)",
        f"failed: {error}" if error else "slow", seconds, streak,
    )
    if streak is not None and streak >= _ALERT_AFTER:
        from src.workers.ops_alerts import send_ops_alert

        what = f"is not answering ({error})" if error else f"is slow ({seconds:.1f}s for SELECT 1)"
        stats["alerted"] = send_ops_alert(
            "db_latency",
            "primary",
            f"Database {what}",
            (
                f"{streak} probes in a row, 2 minutes apart, were bad (> {_SLOW_SECONDS:.0f}s "
                f"or failed).\n"
                f"Last probe: answered={answered} seconds={seconds:.2f} error={error}\n\n"
                "Check now:\n"
                "  * https://api.bridgeleads.io/ready (503 = the API cannot reach the DB)\n"
                "  * Supabase dashboard: Reports > Database (CPU, memory, Disk IO, connections)\n"
                "  * Railway logs for 'Database unavailable' on the api service\n"
                "On 2026-10-01 this pattern ended in a full login outage about 3 hours later."
            ),
        )
    return stats


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.db_canary.db_latency_canary")
    def db_latency_canary() -> dict:
        """Beat entry point: see run_db_latency_canary."""
        return run_db_latency_canary()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("DB latency canary task NOT registered: %s", str(exc)[:120])
