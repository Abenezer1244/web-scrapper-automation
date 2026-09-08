"""Liveness probes for the external enrichment sources in `source_health`.

`source_health` was written around a canary: "once a source is marked unhealthy,
every worker skips it UNTIL A CANARY CLEARS IT". That canary was never built.
`sources_due_for_probe`, `mark_probe_failed` and `mark_source_healthy` had no
production caller at all, only tests, so a source that went into cooldown could
only leave it by passive expiry of `cooldown_until` -- and the next job to run
then paid a full circuit-breaker window (50 requests) to rediscover the block and
re-armed another cooldown on top.

Production proof, read 2026-09-07: `king_erealproperty` had been `throttled`
since 2026-09-04 11:15:39Z with `last_probe_at = NULL` and
`consecutive_probe_failures = 0`. Three days, zero probes. Meanwhile the source
itself answered 60/60 requests with HTTP 200 from the production worker's own
container. Every King mailing lookup in that window was skipped for nothing.

This module is the missing half: one CHEAP, BOUNDED liveness check per source.

Design notes:
  * A probe answers exactly one question -- "is this source serving us right
    now?" -- and answers it the same way the real enrichment path would, so a
    source that returns 200 but no longer parses is NOT called healthy. A probe
    that only checked the status code would clear the source straight back into a
    run that then recorded every parcel as "has no data".
  * A probe MUST NOT go through the health gate it exists to lift. These call
    `safe_get` directly, never `check_source_or_raise`.
  * Bounded by construction: at most `_MAX_PROBE_PARCELS` requests, spaced, and
    only for a source whose cooldown has already expired.
  * Probe targets come from our own scraped rows, so the probe cannot rot the way
    a hardcoded parcel would when the county retires it. A literal fallback
    exists only for an empty database.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import text

from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from src.utils.logger import setup_logger

_logger = setup_logger("enrichment.source_probe")

# At most this many parcels per probe, and we stop at the FIRST success. A block
# costs 3 requests to confirm; a recovery usually costs 1.
_MAX_PROBE_PARCELS = 3
_PROBE_SPACING_S = 1.0

# Only used when the database has no King parcel to probe with (a fresh install).
# A real, long-lived King PIN; if the county ever retires it the DB-driven path
# above is what keeps the probe working.
_KING_FALLBACK_PARCEL = "3879900805"


def _king_probe_parcels(db) -> list[str]:
    """Recently scraped King parcel ids to probe with, newest first."""
    try:
        rows = db.execute(
            text(
                "SELECT DISTINCT parcel_id FROM results "
                "WHERE parcel_id IS NOT NULL AND length(btrim(parcel_id)) = 10 "
                "AND btrim(parcel_id) ~ '^[0-9]+$' "
                "ORDER BY parcel_id DESC LIMIT :n"
            ),
            {"n": _MAX_PROBE_PARCELS},
        ).all()
        pids = [r.parcel_id.strip() for r in rows if r.parcel_id]
    except Exception as exc:  # noqa: BLE001 -- a probe must not depend on a query
        _logger.warning("King probe: could not read probe parcels: %s", str(exc)[:160])
        pids = []
    finally:
        # End the read transaction BEFORE any network I/O. A SELECT leaves an open
        # transaction holding a snapshot, and the HTTP probe below can take tens of
        # seconds; never hold a database transaction across a network call.
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
    return pids or [_KING_FALLBACK_PARCEL]


def probe_king_erealproperty(db) -> tuple[bool, str]:
    """Is eRealProperty serving parseable parcel pages right now?

    Returns (healthy, detail). Healthy requires a 200 whose page BOTH echoes the
    parcel we asked for and yields real content -- the same two conditions the
    enrichment path applies -- so "200 but the layout changed" or "200 but the
    county resolved a different parcel" both read as still-unhealthy rather than
    clearing the source into a run that would record every parcel as empty.
    """
    from src.scrapers.enrichment.king_county_assessor import (
        _ERP_URL,
        _HEADERS,
        _read_parcel_page,
        parcel_page_is_for,
    )
    from src.utils.safe_http import safe_get

    outcomes: list[str] = []
    for i, pid in enumerate(_king_probe_parcels(db)):
        if i:
            time.sleep(_PROBE_SPACING_S)
        try:
            r = safe_get(f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=10)
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"{pid}:{type(exc).__name__}")
            continue
        if r.status_code != 200:
            # Where a redirect points is the single most useful fact about it, and
            # it was exactly what the last incident could not tell us. Host + path
            # only: the query string can carry the parcel and any session token.
            outcomes.append(f"{pid}:HTTP{r.status_code}{_redirect_hint(r)}")
            continue
        if not parcel_page_is_for(r.text, pid):
            outcomes.append(f"{pid}:parcel_mismatch")
            continue
        prop, tax_url, owner = _read_parcel_page(r.text)
        if not (prop or tax_url or owner):
            outcomes.append(f"{pid}:200_but_unparseable")
            continue
        return True, f"200 + parseable parcel page for {pid}"
    return False, "; ".join(outcomes) or "no probe target"


def _redirect_hint(response) -> str:
    """` -> host/path` for a redirect, or "". Never includes the query string."""
    location = response.headers.get("Location") if response is not None else None
    if not location:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(location)
    target = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    return f" -> {target[:120]}"


# source_key -> probe. A source with no entry is left alone by the canary rather
# than guessed at: clearing a source we cannot actually verify would be worse
# than leaving it in cooldown.
PROBES: dict[str, Callable[[object], tuple[bool, str]]] = {
    KING_EREALPROPERTY: probe_king_erealproperty,
}
