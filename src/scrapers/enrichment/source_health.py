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
  * Cooldown escalates 1h -> 6h -> 24h -> 48h -> 72h (capped) per consecutive
    failed probe, so a source that stays angry is asked less often, not more.
    The first rung is deliberately SHORT. It used to be 24h, and that turned a
    transient upstream blip into a full day with no King mailing enrichment for
    anybody: production sat `throttled` from 2026-09-04 11:15Z to 2026-09-08
    while the source itself answered 60/60 requests with HTTP 200. The long rungs
    still arrive, but only once a PROBE has actually confirmed the source is
    still refusing us -- which is what `sources_due_for_probe` + the canary in
    `src/workers/scheduler_helpers/health.py` now do. Escalation is driven by
    evidence rather than by assuming the worst on the first failure.
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

# 1h first, then 6h, 24h, 48h and 72h for every subsequent failed probe.
# Index 0 is the FIRST block, before any probe has run, so it is the rung that
# fires on a one-off blip -- keep it short. Index 1+ is only reached after the
# canary has probed and the source refused us again, so those may be long.
_COOLDOWN_LADDER_HOURS = (1, 6, 24, 48, 72)

_UNHEALTHY = ("throttled", "blocked")

# How long an expired cooldown may sit un-probed before we conclude that nothing
# is probing and let traffic through anyway. Long enough that a normal canary
# (which runs every few minutes) always wins the race; short enough that a
# scheduler outage costs hours, not days, of missing enrichment.
_CANARY_BACKSTOP = timedelta(hours=6)

# How long a probe claim counts as "a probe is in flight". `claim_probe` stamps
# `last_probe_at` BEFORE the request, so a worker that dies mid-probe leaves a
# stamp behind; without an expiry that single abandoned claim would hold traffic
# for good. Comfortably longer than a probe (3 requests, ~10 s) and than the
# canary's 10-minute claim floor, so a live canary always re-claims and
# re-stamps well inside this window. An abandoned claim therefore looks recent
# for up to 20 minutes and then stops counting, at which point the separate
# 6-hour backstop can apply.
_PROBE_INFLIGHT_GRACE = timedelta(minutes=20)


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


def _ladder_case_sql(counter: str) -> str:
    """SQL CASE mapping `counter + 1` to its cooldown interval, from the ladder.

    Generated from `_COOLDOWN_LADDER_HOURS` (a tuple of ints in this module), so
    the SQL and `cooldown_for()` can never drift apart and no value here comes
    from outside the process.
    """
    top = len(_COOLDOWN_LADDER_HOURS) - 1
    whens = [f"WHEN {counter} + 1 >= {top} THEN interval '{_COOLDOWN_LADDER_HOURS[top]} hours'"]
    whens += [
        f"WHEN {counter} + 1 = {i} THEN interval '{h} hours'"
        for i, h in reversed(list(enumerate(_COOLDOWN_LADDER_HOURS[:top])))
        if i > 0
    ]
    whens.append(f"ELSE interval '{_COOLDOWN_LADDER_HOURS[0]} hours'")
    return "CASE " + " ".join(whens) + " END"


# Built once, from `_COOLDOWN_LADDER_HOURS` (a tuple of ints defined in this
# module). No value in it comes from outside the process, so the S608 string-built
# SQL warning does not apply: there is no user input on this path at all.
_PROBE_FAILED_SQL = (
    "UPDATE external_source_health SET "  # noqa: S608 -- only int literals interpolate
    "  status = CASE WHEN status IN ('throttled','blocked') "
    "                THEN status ELSE 'throttled' END, "
    "  consecutive_probe_failures = consecutive_probe_failures + 1, "
    # Escalation computed DATABASE-SIDE from the row's own counter, so two probes
    # cannot both read 0 and both write 1, losing a rung of the ladder.
    "  cooldown_until = :now + "
    + _ladder_case_sql("external_source_health.consecutive_probe_failures")
    + ", reason = :reason, updated_at = :now "
    "WHERE source_key = :k AND updated_at = :token"
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

    An expired cooldown used to mean "available", which released ORDINARY TRAFFIC
    rather than a probe: the next real job walked straight into the still-refusing
    source, spent a full circuit-breaker window (50 requests) rediscovering the
    block, and re-armed the cooldown. That is what happened on 2026-09-07 at
    03:02 UTC, and it is why the outage renewed itself instead of ending.

    So an expired cooldown now means "eligible for a CLAIMED PROBE", and the gate
    stays shut for everyone else until that probe succeeds (`claim_probe` +
    `resolve_probe`, driven by the canary in scheduler_helpers/health.py).

    BACKSTOP: making recovery depend on the canary would let a canary that is not
    running block a source forever, which is a worse failure than the one being
    fixed. So once the anchor (`cooldown_until`, or `first_seen_at` when there is
    no cooldown) is more than `_CANARY_BACKSTOP` old with no RECENT probe, we
    conclude nothing is probing and allow traffic rather than blocking forever.
    """
    r = _row(db, source_key)
    if r is None or r.status not in _UNHEALTHY:
        return True
    now = datetime.now(UTC)
    # A NULL cooldown used to return False before ever reaching the backstop, so
    # an unhealthy row without one stayed blocked forever if no canary was running
    # (Codex). Anchor on when the outage was first seen instead; with no anchor at
    # all there is nothing to time out against, so it stays blocked.
    anchor = r.cooldown_until or r.first_seen_at
    if anchor is None:
        return False
    if now < anchor:
        return False
    # Cooldown expired. Normally the canary probes and clears; hold traffic while
    # a probe could plausibly be in flight.
    #
    # "In flight" must EXPIRE (Codex). `claim_probe` writes `last_probe_at` BEFORE
    # making the request, so a worker killed between the claim and the verdict
    # leaves a timestamp that satisfies `last_probe_at >= cooldown_until` forever.
    # Treating that as proof a canary is running held traffic permanently on the
    # strength of one abandoned claim, which is the same shape of silent
    # indefinite block this whole change exists to remove. A claim is evidence of
    # a live canary only while it is RECENT.
    probe_is_recent = (
        r.last_probe_at is not None and now - r.last_probe_at < _PROBE_INFLIGHT_GRACE
    )
    if probe_is_recent:
        return False
    if now >= anchor + _CANARY_BACKSTOP:
        _logger.warning(
            "Source %s: blocked %s with no probe since — assuming no canary is "
            "running and allowing traffic",
            source_key, now - anchor,
        )
        return True
    return False


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


def claim_probe(db, source_key: str, min_interval: timedelta) -> datetime | None:
    """Atomically claim the right to probe `source_key`. Returns a token, or None.

    `sources_due_for_probe` reads, the probe then writes -- so two overlapping
    canary ticks would both probe the same source and both call
    `mark_probe_failed`, escalating a single outage TWO rungs up the ladder (1h
    straight to 24h) off one real refusal. Beat normally runs one scheduler, but
    a redeploy overlap or a manual run is enough, and the cost of the race is
    paid in hours of unnecessary blocking.

    The claim is the `last_probe_at` write itself, done as a conditional UPDATE
    so the database decides the winner. Only the caller whose UPDATE touched a
    row may probe.

    The returned token is the `updated_at` this claim wrote. Every write in this
    module bumps `updated_at`, so passing the token back to `resolve_probe` makes
    the transition conditional on NOTHING having touched the row since the claim.
    That is what stops the dangerous sequence: our probe succeeds, another worker
    records a FRESH outage while it was in flight, and we then clear the source
    healthy, deleting a block that is newer than our evidence (Codex).
    """
    try:
        now = datetime.now(UTC)
        result = db.execute(
            text(
                "UPDATE external_source_health SET last_probe_at = :now, updated_at = :now "
                "WHERE source_key = :k "
                "AND status IN ('throttled','blocked') "
                "AND (cooldown_until IS NULL OR cooldown_until <= :now) "
                "AND (last_probe_at IS NULL OR last_probe_at <= :cutoff)"
            ),
            {"k": source_key, "now": now, "cutoff": now - min_interval},
        )
        db.commit()
        return now if result.rowcount else None
    except Exception as exc:  # noqa: BLE001 -- losing a claim must never raise
        _logger.error("Could not claim probe for %s: %s", source_key, str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
        return None


def resolve_probe(db, source_key: str, token: datetime, *, healthy: bool,
                  reason: str) -> bool:
    """Apply a probe's verdict, but only if nothing touched the row since `token`.

    Returns True when the verdict was applied. False means another writer got
    there first (a fresh block, or a competing probe) and OUR result is stale, so
    it is dropped rather than allowed to overwrite newer evidence.

    On failure this also re-asserts `status`, because the old `mark_probe_failed`
    wrote a future `cooldown_until` WITHOUT touching status: a late failing probe
    landing after a recovery left the row `healthy` with a future cooldown, and
    `is_source_available` checks status first, so that incoherent row read as
    available anyway (Codex).
    """
    try:
        now = datetime.now(UTC)
        if healthy:
            result = db.execute(
                text(
                    "UPDATE external_source_health SET "
                    "  status = 'healthy', cooldown_until = NULL, first_seen_at = NULL, "
                    "  consecutive_probe_failures = 0, last_success_at = :now, "
                    # Keep the diagnosis. Nulling `reason` on recovery threw away the
                    # only durable record of what the outage looked like, and worker
                    # log retention does not reach back far enough to replace it.
                    "  reason = :reason, updated_at = :now "
                    "WHERE source_key = :k AND updated_at = :token"
                ),
                {"k": source_key, "now": now, "token": token,
                 "reason": f"recovered: {reason}"[:512]},
            )
        else:
            result = db.execute(
                text(_PROBE_FAILED_SQL),
                {"k": source_key, "now": now, "token": token, "reason": reason[:512]},
            )
        db.commit()
        if not result.rowcount:
            _logger.warning(
                "Source %s: probe verdict (healthy=%s) DISCARDED — the row changed "
                "while the probe was in flight", source_key, healthy,
            )
            return False
        _logger.info(
            "Source %s probe verdict applied: %s", source_key,
            "RECOVERED" if healthy else "still unavailable",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _logger.error("Could not resolve %s probe: %s", source_key, str(exc)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
        return False


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
