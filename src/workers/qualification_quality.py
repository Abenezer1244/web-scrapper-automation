"""Monitoring for the code-violation qualification gate (src/api/lead_qualification.py).

WHY THIS EXISTS
---------------
Run 1ad28ded (King, 2026-10-17) billed 491 code violations while King's owner source was
in cooldown, so the delivered file named nobody and nothing noticed. The gate (#542) now
holds an incomplete code violation back before billing. This module watches the gate
itself, so a regression in it, or a source that stops yielding complete leads, pages
someone instead of shipping quietly.

TWO CHECKS
----------
1. Gate invariants (check_gate_invariants), recurring and global: every sweep tick, over
   every done code-violation job that started after the gate deployed. A hit is a breach,
   never a quality dip, so there is no baseline:
     * unheld_unqualified: a subject row, not a duplicate, carrying no delivery exclusion,
       that fails the policy now. The hold step marks every failing row (no-address rows
       included), and a mark is never removed after the run, so this is a row the hold step
       missed or a delivered row that lost a value afterwards.
     * outbound_held: a row the run HELD that a dialer delivered or a paid skip trace was
       submitted for. The mark is written before anything can send, so a hit is not drift.
       Queued, cancelled and skipped work is not counted: refusing it is the gate working.
     * unstamped: a row of a code_violation job started after the gate deployed without
       the policy stamp. qualified_sql passes unstamped rows, so a lost stamp would switch
       the gate off for that row silently.
   One alert per kind (incident level), keyed on the kind, so twenty affected jobs page
   once; a condition that persists pages again after the cooldown, which is also the
   retry for a send that failed.

2. Qualification yield (qualification_yield / evaluate_yield), once per finished job from
   the data-quality sweep: the share of the run's subject, non-duplicate rows the gate let
   through, per county x state x record type x jurisdiction (enrichment_data.source),
   against the same cohort's other post-gate runs over 90 days. Both the hold mark and
   is_duplicate are frozen at the gate, so a later sweep filling a held row never moves
   the number. Over-quota rows passed the gate (the cap ranks qualified rows only).

WHAT IT NEVER DOES
------------------
It never writes results or jobs, never fails or re-runs a job, never prints PII (ids and
counts only). Alerts go through send_ops_alert, which writes the durable audit_events row.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.api.lead_actionability import delivery_excluded_sql, not_qualified_excluded_sql
from src.api.lead_qualification import (
    HOLD_KEY,
    OWNER_SOURCE_BY_SOURCE,
    POLICY_CODE_VIOLATION,
    POLICY_KEY,
    qualified_sql,
)
from src.utils.logger import setup_logger

_logger = setup_logger("workers.qualification_quality")

# The gate (#542) deployed 2026-10-27 01:09 UTC; the margin covers the worker restart.
# A job that STARTED earlier may have inserted rows on the old code and is never judged
# (started_at, not finished_at: a long or retried job can straddle the deploy).
GATE_DEPLOYED_AT = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)
LOOKBACK_DAYS = 90
# Same shape as the coverage check (src/workers/data_quality.py): a pass rate must have
# fallen by this many points, from a cohort baseline of at least this many rows, on a run
# cohort of at least this many rows, before it is a warning.
DROP_POINTS = 30.0
MIN_BASELINE_ROWS = 50
MIN_RUN_ROWS = 10
# Job ids listed per alert body; the count is always the full one.
_MAX_JOBS_LISTED = 10

# Spent or on its way to Tracerfy ('completed' and 'unmatched' are billed); 'queued' and
# 'cancelled' are not spend, and cancelling a withheld row is the dispatcher refusing it.
SKIP_TRACE_SENT = ("submitting", "submitted", "completed", "unmatched")

_SOURCES = frozenset(OWNER_SOURCE_BY_SOURCE)
_OTHER = "other"
UNRECORDED = "unrecorded"

_SUBJECT = f"(r.enrichment_data->>'{POLICY_KEY}' = '{POLICY_CODE_VIOLATION}')"
# Every query starts from the few post-gate code-violation jobs (jobs + scraper_configs) and
# reaches results through its job_id index; nothing scans results across record types.
_CV_JOBS = ("JOIN jobs j ON j.id = r.job_id "
            "JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.record_type = 'code_violation'")
_POST_GATE_DONE = ("j.status = 'done' AND j.started_at >= :boundary AND j.finished_at >= :since "
                   "AND r.user_id = j.user_id")

_UNHELD_UNQUALIFIED_SQL = f"""
SELECT r.job_id::text AS job_id, count(*) AS n
FROM results r {_CV_JOBS}
WHERE {_POST_GATE_DONE} AND r.is_duplicate = false AND {_SUBJECT}
  AND NOT ({delivery_excluded_sql('r')}) AND NOT {qualified_sql('r')}
GROUP BY r.job_id ORDER BY n DESC
"""  # noqa: S608 -- splices only module constants; every value is bound

_OUTBOUND_HELD_SQL = f"""
SELECT job_id, sum(n)::bigint AS n FROM (
  SELECT r.job_id::text AS job_id, count(*) AS n
  FROM dialer_deliveries d
  JOIN results r ON r.id = d.result_id AND r.user_id = d.user_id
  {_CV_JOBS}
  WHERE d.status = 'delivered' AND {_POST_GATE_DONE} AND {_SUBJECT} AND {not_qualified_excluded_sql('r')}
  GROUP BY r.job_id
  UNION ALL
  SELECT r.job_id::text AS job_id, count(*) AS n
  FROM pending_skip_trace_rows p
  JOIN results r ON r.id = p.result_id AND r.user_id = p.user_id
  {_CV_JOBS}
  WHERE p.status = ANY(:sent) AND {_POST_GATE_DONE} AND {_SUBJECT} AND {not_qualified_excluded_sql('r')}
  GROUP BY r.job_id
) t GROUP BY job_id ORDER BY n DESC
"""  # noqa: S608 -- splices only module constants; every value is bound

_UNSTAMPED_SQL = f"""
SELECT r.job_id::text AS job_id, count(*) AS n
FROM results r {_CV_JOBS}
WHERE {_POST_GATE_DONE}
  AND COALESCE(r.enrichment_data->>'{POLICY_KEY}', '') <> '{POLICY_CODE_VIOLATION}'
GROUP BY r.job_id ORDER BY n DESC
"""  # noqa: S608 -- splices only module constants; every value is bound

INVARIANTS = {
    "unheld_unqualified": (
        _UNHELD_UNQUALIFIED_SQL,
        "code violations that fail the qualification policy are not held",
        "The hold step marks every failing code violation before billing, and a mark is never "
        "removed after the run. These rows carry no mark and fail the policy now: either the "
        "hold step missed them, or a delivered lead lost its owner, parcel or address afterwards "
        "and dropped out of its run's list.",
    ),
    "outbound_held": (
        _OUTBOUND_HELD_SQL,
        "held code violations were sent to a dialer or a paid skip trace",
        "These rows were held back by the qualification gate, yet a dialer delivered them or a "
        "Tracerfy lookup was submitted for them. The mark is written before anything can send, "
        "so the delivery or spend path bypassed the gate.",
    ),
    "unstamped": (
        _UNSTAMPED_SQL,
        "code violations from after the gate deployed carry no qualification stamp",
        "Rows of code_violation jobs that started after the gate deployed have no "
        "qualification_policy stamp. The policy passes unstamped rows, so the gate is off for "
        "them: check the insert in src/workers/tasks.py and any writer that replaces "
        "enrichment_data.",
    ),
}


def _params(*, days: int = LOOKBACK_DAYS) -> dict:
    return {"boundary": GATE_DEPLOYED_AT, "since": datetime.now(UTC) - timedelta(days=days),
            "sent": list(SKIP_TRACE_SENT)}


def gate_invariants(db, *, days: int = LOOKBACK_DAYS) -> dict[str, list[tuple[str, int]]]:
    """Every breach of the gate across post-gate done jobs: {kind: [(job_id, rows), ...]}."""
    params = _params(days=days)
    return {
        kind: [(r["job_id"], int(r["n"])) for r in db.execute(text(sql), params).mappings()]
        for kind, (sql, _, _) in INVARIANTS.items()
    }


def check_gate_invariants(db, *, alert: bool = True, days: int = LOOKBACK_DAYS) -> dict:
    """Measure the invariants and page once per breached kind. Returns the measurement."""
    found = gate_invariants(db, days=days)
    if alert:
        for kind, jobs in found.items():
            if jobs:
                _alert_invariant(kind, jobs)
    return found


def _alert_invariant(kind: str, jobs: list[tuple[str, int]]) -> None:
    from src.workers.ops_alerts import send_ops_alert

    _, headline, explanation = INVARIANTS[kind]
    total = sum(n for _, n in jobs)
    listed = ", ".join(f"{job_id[:8]} ({n})" for job_id, n in jobs[:_MAX_JOBS_LISTED])
    more = f" and {len(jobs) - _MAX_JOBS_LISTED} more" if len(jobs) > _MAX_JOBS_LISTED else ""
    subject = f"CRITICAL lead quality gate: {total} {headline}"
    body = (f"{explanation}\n\nRows: {total} across {len(jobs)} job(s): {listed}{more}.\n"
            f"Scope: done jobs started since {GATE_DEPLOYED_AT:%Y-%m-%d %H:%M} UTC and finished "
            f"in the last {LOOKBACK_DAYS} days.")
    _logger.error("Lead quality gate: %s rows %s (%d jobs)", total, kind, len(jobs))
    send_ops_alert("qualification_gate", kind, subject, body)


# ── yield ────────────────────────────────────────────────────────────────────

_SOURCE_EXPR = "coalesce(r.enrichment_data->>'source', '')"

_RUN_YIELD_SQL = f"""
SELECT {_SOURCE_EXPR} AS source, count(*) AS n,
       count(*) FILTER (WHERE {not_qualified_excluded_sql('r')}) AS held
FROM results r
WHERE r.job_id = :job_id AND r.is_duplicate = false AND {_SUBJECT}
GROUP BY 1
"""  # noqa: S608 -- splices only module constants; every value is bound

_BASELINE_YIELD_SQL = f"""
SELECT {_SOURCE_EXPR} AS source, count(*) AS n,
       count(*) FILTER (WHERE {not_qualified_excluded_sql('r')}) AS held
FROM results r {_CV_JOBS}
WHERE {_POST_GATE_DONE} AND j.id <> :job_id AND r.is_duplicate = false AND {_SUBJECT}
  AND lower(sc.county) = :county AND upper(sc.state) = :state AND sc.record_type = :record_type
GROUP BY 1
"""  # noqa: S608 -- splices only module constants; every value is bound

_HELD_REASONS_SQL = f"""
SELECT {_SOURCE_EXPR} AS source, r.enrichment_data->'{HOLD_KEY}' AS hold
FROM results r
WHERE r.job_id = :job_id AND r.is_duplicate = false AND {_SUBJECT}
  AND {not_qualified_excluded_sql('r')}
"""  # noqa: S608 -- splices only module constants; every value is bound


def _cohort(source: str | None) -> str:
    return source if source in _SOURCES else _OTHER


def _fold(rows) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        c = out.setdefault(_cohort(r["source"]), {"n": 0, "held": 0})
        c["n"] += int(r["n"])
        c["held"] += int(r["held"])
    return out


def _reason_counts(rows) -> dict[str, dict[str, int]]:
    """Frozen reasons of the run's held rows per cohort; a row without a usable record
    counts once as 'unrecorded' (held before #547, or a malformed record)."""
    from src.api.schemas import QualificationReason

    known = frozenset(QualificationReason.__args__)
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        counts = out.setdefault(_cohort(r["source"]), {})
        hold = r["hold"]
        reasons = hold.get("reasons") if isinstance(hold, dict) and hold.get("policy") == POLICY_CODE_VIOLATION else None
        usable = [x for x in reasons if isinstance(x, str) and x in known] if isinstance(reasons, list) else []
        for reason in usable or [UNRECORDED]:
            counts[reason] = counts.get(reason, 0) + 1
    return out


def qualification_yield(db, *, job_id: str, county: str, state: str, record_type: str,
                        days: int = LOOKBACK_DAYS) -> dict:
    """The run's per-cohort pass counts, its cohort baselines and its held reasons."""
    params = {**_params(days=days), "job_id": job_id, "county": county.lower(),
              "state": state.upper(), "record_type": record_type}
    return {
        "run": _fold(db.execute(text(_RUN_YIELD_SQL), params).mappings()),
        "baseline": _fold(db.execute(text(_BASELINE_YIELD_SQL), params).mappings()),
        "reasons": _reason_counts(db.execute(text(_HELD_REASONS_SQL), params).mappings()),
    }


def _pass_rate(c: dict[str, int]) -> float:
    return 100.0 * (c["n"] - c["held"]) / c["n"] if c["n"] else 0.0


def _pass_pct(c: dict[str, int]) -> float:
    """For display only; the threshold compares _pass_rate."""
    return round(_pass_rate(c), 1)


def evaluate_yield(measured: dict) -> tuple[list[dict], list[str]]:
    """(warnings, cohorts too small to judge). A cohort warns when its pass rate fell at
    least DROP_POINTS below its own baseline; zero qualified on a healthy baseline is that."""
    warnings: list[dict] = []
    unjudged: list[str] = []
    for cohort, run in sorted(measured["run"].items()):
        base = measured["baseline"].get(cohort, {"n": 0, "held": 0})
        if run["n"] < MIN_RUN_ROWS or base["n"] < MIN_BASELINE_ROWS:
            unjudged.append(cohort)
            continue
        # Unrounded and inclusive: a drop of exactly DROP_POINTS warns (Codex P2).
        if _pass_rate(base) - _pass_rate(run) >= DROP_POINTS:
            now, before = _pass_pct(run), _pass_pct(base)
            warnings.append({"kind": "qualification_collapse", "field": cohort, "run_pct": now,
                             "baseline_pct": before, "run_rows": run["n"], "held": run["held"],
                             "baseline_rows": base["n"],
                             "reasons": measured["reasons"].get(cohort, {})})
    return warnings, unjudged


def alert_yield(job: dict, warnings: list[dict]) -> None:
    """One page per collapsed cohort (county/state/record type/jurisdiction), not per job."""
    from src.workers.ops_alerts import send_ops_alert

    where = f"{job['county']}/{job['state']} {job['record_type']}"
    for w in warnings:
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(w["reasons"].items(), key=lambda kv: -kv[1]))
        subject = (f"Lead quality: {where} {w['field']} job {str(job['id'])[:8]} passed "
                   f"{w['run_pct']}% vs {w['baseline_pct']}% baseline")
        body = (f"Job {job['id']} ({where}, jurisdiction {w['field']}): {w['run_rows']} code violations, "
                f"{w['held']} held back by the qualification gate, so {w['run_pct']}% passed. The same "
                f"jurisdiction's other runs since the gate passed {w['baseline_pct']}% of "
                f"{w['baseline_rows']} rows over {LOOKBACK_DAYS} days.\n"
                f"Why held (frozen with the hold): {reasons or 'none recorded'}.\n"
                "Nothing was billed for the held rows. A collapse usually means the owner, parcel or "
                "mailing source for this jurisdiction stopped answering during the run.")
        _logger.warning("%s", subject)
        send_ops_alert("qualification_yield",
                       f"{job['county']}/{job['state']}/{job['record_type']}/{w['field']}", subject, body)
