"""Readiness probe — can this instance actually serve DB-backed traffic?

Deliberately split from ``/health``:

* ``/health`` is LIVENESS. The process is up and the event loop responds. It
  touches nothing downstream, so a platform health gate wired to it can never be
  held down by a dependency outage — you can still ship a fix during an incident.
* ``/ready`` is READINESS. It answers the question a liveness check cannot:
  "would a real request work right now?" It touches the database.

The gap between those two is not academic. On 2026-07-28 the Supabase project
went away (``(ENOTFOUND) tenant/user ... not found``); every DB-backed route
returned 500 and every Celery beat task failed, while ``/health`` reported 200
the whole time. The outage surfaced only when a human tried to log in.

Why the result is cached AND single-flighted
--------------------------------------------
The async engine uses ``NullPool`` (see ``src/db/session.py``), so every probe
opens a fresh TCP + TLS + auth connection to Supabase, which has a hard
connection cap. An unauthenticated, uncached ``/ready`` would therefore be a
connection-exhaustion amplifier: any caller could force a new database
connection per request.

``_TTL_SECONDS`` bounds probe traffic to at most one DB connection per window no
matter how hard the endpoint is hit. The lock closes the remaining hole — on
expiry a herd of concurrent callers would otherwise all miss the cache at once
and stampede the database together. Callers that arrive during an in-flight
probe wait for it and reuse its result.

Why Redis is NOT checked here
-----------------------------
``rate_limit()`` fails open to a per-process limiter when Redis is unavailable
(``src/api/middleware/rate_limit.py``), so a Redis outage does not stop users
signing in or using the API. Failing readiness on Redis would be a false red on
the main customer path — and a false red trains people to ignore the alert.
Redis health belongs in a separate ops/dependency view, not in the primary gate.
"""

import asyncio
import logging
import time
import uuid

from sqlalchemy import text

# Imported as a MODULE, not `from ... import async_engine`, on purpose: the test
# suite swaps `src.db.session.async_engine` for a test engine at session setup
# (tests/conftest.py). Binding the object at import time would pin whichever
# engine existed first and silently probe the wrong database.
import src.db.session as _db_session

_logger = logging.getLogger("api.readiness")

# How long one probe result is reused. Also caps how stale an outage/recovery
# signal can be, so keep it well under a monitor's alert threshold.
_TTL_SECONDS = 10.0

# Hard ceiling on the probe itself. Without it, a hung TCP connect to a dead
# database host would pin the request until the client gave up — turning a
# readiness check into its own source of latency.
_PROBE_TIMEOUT_SECONDS = 2.0

_lock = asyncio.Lock()

# (monotonic_timestamp, is_ready, ref) — ref is None while ready. time.monotonic
# is used rather than wall clock so an NTP step can't extend or expire the TTL.
_cached: tuple[float, bool, str | None] | None = None


async def _probe() -> tuple[bool, str | None]:
    """Run a single real database round-trip. Never raises.

    Returns ``(True, None)`` when the database answered, else ``(False, ref)``
    where ``ref`` correlates the client's 503 body with the logged traceback.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with _db_session.async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 — a probe must report, never propagate
        # Full traceback server-side, opaque ref to the caller. Same contract as
        # the global handler in main.py: clients never see DB internals.
        ref = uuid.uuid4().hex[:12]
        _logger.error(
            "readiness probe failed ref=%s: %s", ref, exc, exc_info=True
        )
        return False, ref


async def database_ready() -> tuple[bool, str | None]:
    """Return ``(is_ready, ref)``, reusing a recent probe when one exists.

    ``ref`` is None when ready, otherwise the id of the logged failure.
    """
    global _cached

    cached = _cached
    if cached is not None and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1], cached[2]

    async with _lock:
        # Re-check inside the lock: while we waited, another caller may have
        # already refreshed. Without this, every member of a stampede would run
        # its own probe one after another instead of sharing the first result.
        cached = _cached
        if cached is not None and time.monotonic() - cached[0] < _TTL_SECONDS:
            return cached[1], cached[2]

        ok, ref = await _probe()
        # Stamp AFTER the probe so a slow probe shortens the reuse window rather
        # than serving a result that is already older than the TTL.
        _cached = (time.monotonic(), ok, ref)
        return ok, ref
