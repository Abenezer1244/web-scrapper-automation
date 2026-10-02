"""Per-run data-quality coverage, judged against the county x record-type baseline.

WHY THIS EXISTS
---------------
On 2026-10-02 the owner found by hand that every Clark and Benton probate lead had
no mailing address. Nothing had noticed: Clark job 62404bd0 delivered 1,335 rows
with 1,335 parcels, 1,292 property addresses and 0 mailing addresses, and its log
said "Enrichment complete: addresses added". A run that looks like that is
suspicious on its face, and it is also exactly what a county portal changing its
layout, a source cooling down for a day, or a parser regression looks like.

WHAT IT DOES
------------
For each finished job it measures, over the run's NEW (non-duplicate) rows, the
share carrying a parcel, property address, mailing address, phone, email,
auction date and principal owing, plus the share of mailing addresses that equal
the property address and the mailing-source mix. It then compares the shares with
the same county x record type over the trailing window (other done jobs), and
raises an ops alert (e-mail + durable audit_events row, src/workers/ops_alerts.py)
when a share has collapsed against that baseline, or when every mailing address
is an echo of the property address where the baseline was not.

WHAT IT NEVER DOES
------------------
It never fails, pauses or re-runs a job, never writes to results or jobs, never
reads PII (presence is a NULL check on the encrypted columns), and a low share
with no baseline is not an alert: some sources legitimately have no mailing data,
and the first runs of a new county are the baseline being born. A collapse is
measured, not assumed. The checked marker lives in Redis so a job is judged once.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger("workers.data_quality")

FIELDS = ("parcel_id", "property_address", "mailing_address", "phone", "email",
          "auction_date", "default_amount")
# A share must have fallen by this many points, from a baseline built on at least
# this many rows, on a run of at least this many rows, before it is a warning.
DROP_POINTS = 30.0
MIN_BASELINE_ROWS = 50
MIN_RUN_ROWS = 10
# A field the baseline itself rarely has (auction date on probate, principal owing
# outside pre-foreclosure) is not compared: its absence is the normal shape.
MIN_BASELINE_PCT = 5.0
# "Every mailing address is the property address" is the shape of a situs echo.
ECHO_RUN_PCT = 95.0
ECHO_BASELINE_PCT = 70.0
BASELINE_DAYS = 90
SWEEP_LOOKBACK_HOURS = 24
# Per tick. A judged job costs three indexed queries, so this is cheap; it only has
# to exceed the busiest day's finished-job count so nothing ages out unjudged.
SWEEP_MAX_JOBS = 500
_CHECKED_TTL_S = 14 * 24 * 3600

_COUNTS = """
SELECT count(*) AS n,
       count(r.parcel_id) FILTER (WHERE r.parcel_id <> '') AS parcel_id,
       count(r.property_address) FILTER (WHERE r.property_address <> '') AS property_address,
       count(r.mailing_address) FILTER (WHERE r.mailing_address <> '') AS mailing_address,
       count(r.phone) AS phone,
       count(r.email) AS email,
       count(r.auction_date) AS auction_date,
       count(r.default_amount) AS default_amount,
       count(*) FILTER (WHERE r.mailing_address IS NOT NULL AND r.property_address IS NOT NULL
                          AND upper(btrim(r.mailing_address)) = upper(btrim(r.property_address))) AS echo,
       count(*) FILTER (WHERE r.enrichment_data::text LIKE '%%mailing_lookup_deferred%%') AS deferred
FROM results r
"""

_RUN_SQL = _COUNTS + "WHERE r.job_id = :job_id AND NOT r.is_duplicate"

_BASELINE_SQL = _COUNTS + """
JOIN jobs j ON j.id = r.job_id
JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE NOT r.is_duplicate AND j.status = 'done' AND j.id <> :job_id
  AND j.finished_at >= :since
  AND lower(sc.county) = :county AND upper(sc.state) = :state AND sc.record_type = :record_type
"""

_SOURCES_SQL = """
SELECT coalesce(r.enrichment_data::jsonb ->> 'mailing_source', 'unstamped') AS source, count(*) AS n
FROM results r
WHERE r.job_id = :job_id AND NOT r.is_duplicate AND r.mailing_address IS NOT NULL
  AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
GROUP BY 1 ORDER BY 2 DESC
"""

_JOB_SQL = """
SELECT j.id, j.finished_at, lower(sc.county) AS county, upper(sc.state) AS state, sc.record_type,
       sc.skip_trace_enabled
FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE j.id = :job_id
"""

# Phone and e-mail only exist when the customer bought skip tracing for the config;
# judging them on a config that did not is a false alarm, not a collapse.
_SKIP_TRACE_FIELDS = frozenset({"phone", "email"})

_RECENT_DONE_SQL = """
SELECT j.id FROM jobs j
WHERE j.status = 'done' AND j.finished_at >= :since
ORDER BY j.finished_at DESC LIMIT :limit
"""


def _shares(row) -> dict:
    n = int(row["n"] or 0)
    out = {"rows": n, "pct": {}, "echo_pct": 0.0, "deferred": int(row["deferred"] or 0)}
    for f in FIELDS:
        out["pct"][f] = round(100.0 * int(row[f] or 0) / n, 1) if n else 0.0
    mailing = int(row["mailing_address"] or 0)
    out["echo_pct"] = round(100.0 * int(row["echo"] or 0) / mailing, 1) if mailing else 0.0
    return out


def run_coverage(db, job_id: str) -> dict:
    """Coverage shares over the run's NEW rows, plus its mailing-source mix."""
    row = db.execute(text(_RUN_SQL), {"job_id": job_id}).mappings().one()
    out = _shares(row)
    out["mailing_sources"] = {
        r["source"]: int(r["n"])
        for r in db.execute(text(_SOURCES_SQL), {"job_id": job_id}).mappings()
    }
    return out


def baseline_coverage(db, *, job_id: str, county: str, state: str, record_type: str,
                      days: int = BASELINE_DAYS) -> dict:
    """The same shares over this county x record type's OTHER done jobs in the window."""
    row = db.execute(text(_BASELINE_SQL), {
        "job_id": job_id, "county": county.lower(), "state": state.upper(),
        "record_type": record_type, "since": datetime.now(UTC) - timedelta(days=days),
    }).mappings().one()
    return _shares(row)


def evaluate(run: dict, baseline: dict, *, skip_trace: bool = True) -> list[dict]:
    """Warnings for this run. Empty when the run is within its baseline, when there
    is no baseline worth the name, or when the run is too small to judge. Phone and
    e-mail are judged only when the config has skip tracing on."""
    warnings: list[dict] = []
    if run["rows"] < MIN_RUN_ROWS or baseline["rows"] < MIN_BASELINE_ROWS:
        return warnings
    for f in FIELDS:
        base, now = baseline["pct"][f], run["pct"][f]
        if base < MIN_BASELINE_PCT or (f in _SKIP_TRACE_FIELDS and not skip_trace):
            continue
        if now + DROP_POINTS < base:
            warnings.append({"field": f, "run_pct": now, "baseline_pct": base,
                             "kind": "coverage_collapse"})
    if (run["pct"]["mailing_address"] > 0 and run["echo_pct"] >= ECHO_RUN_PCT
            and baseline["echo_pct"] < ECHO_BASELINE_PCT):
        warnings.append({"field": "mailing_address", "run_pct": run["echo_pct"],
                         "baseline_pct": baseline["echo_pct"], "kind": "mailing_echoes_property"})
    return warnings


def _format(job: dict, run: dict, baseline: dict, warnings: list[dict]) -> tuple[str, str]:
    where = f"{job['county']}/{job['state']} {job['record_type']}"
    subject = f"Data quality: {where} job {str(job['id'])[:8]} fell below its baseline"
    lines = [f"Job {job['id']} ({where}), {run['rows']} new rows; baseline {baseline['rows']} rows "
             f"over {BASELINE_DAYS} days.", ""]
    for w in warnings:
        if w["kind"] == "coverage_collapse":
            lines.append(f"- {w['field']}: {w['run_pct']}% this run vs {w['baseline_pct']}% baseline")
        else:
            lines.append(f"- mailing == property on {w['run_pct']}% of mailing addresses "
                         f"(baseline {w['baseline_pct']}%): possible situs echo")
    lines.append("")
    lines.append("Coverage this run: " + ", ".join(f"{f} {run['pct'][f]}%" for f in FIELDS))
    lines.append("Mailing sources: " + (", ".join(f"{k} {v}" for k, v in run["mailing_sources"].items()) or "none"))
    lines.append(f"Deferred mailing lookups: {run['deferred']}")
    return subject, "\n".join(lines)


def check_job(db, job_id: str, *, alert: bool = True) -> dict:
    """Measure one finished job, alert on a collapse (unless ``alert`` is off, as the
    read-only report script does). Returns the measurement."""
    job = db.execute(text(_JOB_SQL), {"job_id": job_id}).mappings().one_or_none()
    if job is None:
        return {"job_id": job_id, "skipped": "no such job"}
    run = run_coverage(db, job_id)
    baseline = baseline_coverage(db, job_id=job_id, county=job["county"], state=job["state"],
                                 record_type=job["record_type"])
    warnings = evaluate(run, baseline, skip_trace=bool(job["skip_trace_enabled"]))
    report = {"job_id": job_id, "county": job["county"], "state": job["state"],
              "record_type": job["record_type"], "run": run, "baseline": baseline,
              "warnings": warnings}
    if warnings and alert:
        from src.workers.ops_alerts import send_ops_alert

        subject, body = _format(job, run, baseline, warnings)
        _logger.warning("%s: %s", subject, "; ".join(
            f"{w['field']} {w['run_pct']}% vs {w['baseline_pct']}%" for w in warnings))
        send_ops_alert("data_quality", f"{job['county']}/{job['record_type']}", subject, body)
    elif not warnings:
        _logger.info("Data quality: job %s (%s/%s) within baseline; mailing %.1f%% of %d rows",
                     job_id[:8], job["county"], job["record_type"],
                     run["pct"]["mailing_address"], run["rows"])
    return report


def _checked_key(job_id: str) -> str:
    return f"bl:dq:checked:{job_id}"


def run_data_quality_sweep(*, lookback_hours: int = SWEEP_LOOKBACK_HOURS,
                           limit: int = SWEEP_MAX_JOBS) -> dict:
    """Judge every job that finished in the lookback window once. Bounded; never raises
    for one job's failure; the Redis marker is best-effort (a lost marker re-judges a
    job, which only repeats an alert already in its cooldown)."""
    from src.db.session import system_sync_session

    stats = {"checked": 0, "warned": 0, "skipped": 0}
    try:
        import redis as _redis

        from src.config import settings

        client = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2)
    except Exception:  # noqa: BLE001 -- marker is best-effort
        client = None
    with system_sync_session() as db:
        since = datetime.now(UTC) - timedelta(hours=lookback_hours)
        job_ids = [r["id"] for r in db.execute(text(_RECENT_DONE_SQL), {"since": since, "limit": limit}).mappings()]
        for job_id in job_ids:
            key = _checked_key(str(job_id))
            token = uuid.uuid4().hex
            # Claim first with a short TTL so two ticks never judge the same job at
            # once; the 14-day "judged" mark is written only AFTER a successful check.
            # A check that fails releases ITS OWN claim (compare-and-delete on the
            # token, so an expired claim never deletes a newer worker's), and a
            # transient DB error never silences a job for two weeks (Codex P1/P2).
            try:
                if client is not None and not client.set(key, token, nx=True, ex=_CLAIM_TTL_S):
                    stats["skipped"] += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                report = check_job(db, str(job_id))
            except Exception as exc:  # noqa: BLE001 -- one bad job never stops the sweep
                _logger.error("Data quality check failed for job %s: %s", str(job_id)[:8], str(exc)[:160])
                db.rollback()
                _release(client, key, token)
                continue
            _mark_judged(client, key)
            stats["checked"] += 1
            if report.get("warnings"):
                stats["warned"] += 1
    return stats


_CLAIM_TTL_S = 600
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


def _release(client, key: str, token: str) -> None:
    try:
        if client is not None:
            client.eval(_RELEASE_LUA, 1, key, token)
    except Exception:  # noqa: BLE001
        pass


def _mark_judged(client, key: str) -> None:
    try:
        if client is not None:
            client.set(key, "1", ex=_CHECKED_TTL_S)
    except Exception:  # noqa: BLE001
        pass


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.data_quality.data_quality_sweep")
    def data_quality_sweep() -> dict:
        """Beat entry point: see run_data_quality_sweep."""
        stats = run_data_quality_sweep()
        _logger.info("Data quality sweep: %d checked, %d warned, %d already judged",
                     stats["checked"], stats["warned"], stats["skipped"])
        return stats
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.debug("Data quality task not registered: %s", str(exc)[:120])
