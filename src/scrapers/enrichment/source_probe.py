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

from src.scrapers.enrichment.source_health import (
    CLARK_PIC,
    KING_CV_PARCEL_LOCATE,
    KING_EREALPROPERTY,
)
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
        # MUST be filtered to King. `results` holds Pierce, Snohomish and Clark
        # parcels too, and several counties also use 10 digits: verified in
        # production, the unfiltered version of this query returned
        # 9900000021, a PIERCE parcel, in its top 5. Feeding that to
        # eRealProperty yields a page that is not about it, `parcel_page_is_for`
        # fails, and the probe reports King as still refusing us. King would then
        # stay blocked forever WITH a canary running, which is worse than the
        # outage this whole change exists to fix (Codex).
        rows = db.execute(
            text(
                "SELECT DISTINCT r.parcel_id FROM results r "
                "JOIN jobs j ON j.id = r.job_id "
                "JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
                "WHERE r.parcel_id IS NOT NULL AND length(btrim(r.parcel_id)) = 10 "
                "AND btrim(r.parcel_id) ~ '^[0-9]+$' "
                "AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA' "
                "ORDER BY r.parcel_id DESC LIMIT :n"
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


# Parcels Clark County itself owns ("CLARK COUNTY INTERNAL SERVICES", verified
# 2026-10-03 on the Fact Sheet): 1300 and 1200 Franklin St, Vancouver, the county's
# own government buildings. Using the county's parcels rather than our scraped rows
# keeps every tenant's lead data out of a system probe (Codex), and a seat of county
# government is not retired the way a residential parcel can be.
_CLARK_PROBE_PARCELS = ("55735000", "50490000")


def probe_clark_pic(db) -> tuple[bool, str]:
    """Is Clark's Property Information Center Fact Sheet serving readable records?

    Healthy requires a 200 whose page names the parcel asked for AND yields a mailing
    address: the same `parse_page` the enrichment path uses, held to `found` only, so
    a challenge, maintenance or re-laid-out page can never clear the source. `db` is
    unused; the targets are fixed county parcels.
    """
    from src.scrapers.enrichment.clark_pic import (
        _PACE_S,
        _TIMEOUT_S,
        _URL,
        FOUND,
        parse_page,
    )
    from src.utils.safe_http import safe_get

    outcomes: list[str] = []
    for i, pid in enumerate(_CLARK_PROBE_PARCELS):
        if i:
            time.sleep(_PACE_S)  # Clark answers 429 at 1.5 s spacing; ~7 s is safe
        try:
            r = safe_get(_URL, params={"account": pid},
                         headers={"User-Agent": "Mozilla/5.0 (compatible; BridgeLeads)"},
                         timeout=_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"{pid}:{type(exc).__name__}")
            continue
        if r.status_code != 200:
            outcomes.append(f"{pid}:HTTP{r.status_code}{_redirect_hint(r)}")
            continue
        outcome = parse_page(r.text, pid).outcome
        if outcome != FOUND:
            outcomes.append(f"{pid}:{outcome}")  # an outcome code, never page content
            continue
        return True, f"200 + readable Fact Sheet for county parcel {pid}"
    return False, "; ".join(outcomes)


# A fixed public King parcel the strict locate rule matches (PIN 0904000025; the same
# point and address the king_code_violation tests use, from a real layer answer).
_KING_CV_PROBE_POINT = ("47.66817947", "-122.40862917", "5412 39TH AVE W, SEATTLE WA 98199")


def probe_king_cv_parcel_locate(db) -> tuple[bool, str]:
    """Can the King code-violation locate step run: extract usable AND layer matching?

    The step (cv_mailing_recovery) stands down when either half fails, so both must be
    proven before it is cleared: the Assessor extract must load (cached_extract, the
    same call the step makes) and the parcel layer must give a strict `matched` for a
    fixed public point (one request). `db` is unused.
    """
    from src.scrapers.enrichment.king_parcel_locate import locate
    from src.scrapers.enrichment.king_rpacct import cached_extract

    try:
        if cached_extract() is None:
            return False, "Assessor extract unavailable"
        status = locate(*_KING_CV_PROBE_POINT).status
    except Exception as exc:  # noqa: BLE001 -- a probe reports, it never raises
        return False, type(exc).__name__
    return status == "matched", f"extract ok, layer {status}"


# source_key -> probe. A source with no entry is left alone by the canary rather
# than guessed at: clearing a source we cannot actually verify would be worse
# than leaving it in cooldown.
PROBES: dict[str, Callable[[object], tuple[bool, str]]] = {
    KING_EREALPROPERTY: probe_king_erealproperty,
    CLARK_PIC: probe_clark_pic,
    KING_CV_PARCEL_LOCATE: probe_king_cv_parcel_locate,
}
