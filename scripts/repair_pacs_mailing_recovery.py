"""Repair the leads the PACS search-mode bug exhausted or wrongly settled (follow-up to #575).

Until #575 the PACS parcel search never sent `propertySearchOptions$searchType`, so
Clallam, Grant, Jefferson, Chelan and Island answered every lookup with "You must enter
search criteria." (unparsed, and each one charged a recovery attempt), and an Island id
with a letter was recorded `parcel_not_found` without any request. The standard requeue
(`requeue_gis_mailing_recovery.py`) deliberately skips every row that carries a
`mailing_recovery_outcome`, so it cannot reach these rows.

Two cohorts, both written by the GIS sweep `_apply` and (for B) by live jobs:

  A  a RETRY cohort: a PACS county whose lookups the bug broke (Clallam, Grant, Jefferson,
     Chelan, Island, Whatcom), outcome `error`, out of attempts, not deferred. `error`
     also covers genuine timeouts and ambiguous pages (the stored keys cannot tell them
     apart), so this is one more bounded recovery round, not a claim each row was hit by
     the bug. ACTION: re-defer, and drop the attempt count and outcome.
  B  Island, outcome `parcel_not_found` from `pacs_island`, a parcel id with a letter:
     a "not found" nobody asked the county for. ACTION: drop the false outcome, its
     source and the attempt count, and do NOT re-defer: #575 defers such ids without a
     request, so queueing them now would only rotate them until Island's id format is
     supported; with no outcome they become eligible for the standard requeue then.

Each repaired row records `pacs_search_mode_repair_v1` (cohort, when, and the previous
outcome, attempts, last-attempt time, source and deferral), built in SQL from the row's
values at write time. A row that carries it is never selected again, so a rerun (or a
row that fails five fresh times) is not reset twice and its evidence is never
overwritten. Every UPDATE re-checks the row: same tenant, job and parcel, still no
mailing address, and the outcome / attempts / source / deferral read for the report.
Any change in between makes it a no-op (`skipped_changed`).

    railway run --service worker python scripts/repair_pacs_mailing_recovery.py   # dry-run
    railway run --service worker python scripts/repair_pacs_mailing_recovery.py \\
        --apply --deployed-commit <sha the worker service runs, from `railway deployment list`>

`--apply` refuses unless that commit (looked up in this checkout; the SHA itself is the
operator's attestation of what is deployed) contains the #575 fix, and unless it can hold
the mailing-recovery lock as an owned lease (see Lease): no sweep tick (GIS or King) can
run, or overwrite a repaired row with a stale answer, while it writes, and if the lease is
ever lost or unprovable it stops before the next write (exit 4). Nothing is looked up; no
mailing address, billing, quota, job or skip-trace state is touched. Every candidate is
written to --report (JSON lines, no names or addresses) before any write.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MARKER = "pacs_search_mode_repair_v1"
REASON = "pacs_search_mode_fix_575"
# The PACS counties whose lookups the missing search mode broke (Benton's worked).
RETRY_COUNTIES = ("clallam", "grant", "jefferson", "chelan", "island", "whatcom")
ISLAND_SOURCE = "pacs_island"
_MAX_ATTEMPTS = 5          # mailing_recovery._MAX_ATTEMPTS (a test pins the two together)

_ED = ("(CASE WHEN jsonb_typeof(r.enrichment_data::jsonb) = 'object' "
       "THEN r.enrichment_data::jsonb ELSE '{}'::jsonb END)")
_ATTEMPTS = (f"(CASE WHEN {_ED}->>'mailing_recovery_attempts' ~ '^[0-9]{{1,6}}$' "
             f"THEN ({_ED}->>'mailing_recovery_attempts')::int ELSE 0 END)")

_CANDIDATES_SQL = f"""
    SELECT r.id, r.user_id, r.job_id, r.parcel_id, lower(sc.county) AS county,
           CASE WHEN {_ED}->>'mailing_recovery_outcome' = 'error' THEN 'A' ELSE 'B' END AS cohort,
           coalesce({_ED}->>'mailing_recovery_outcome', '') AS prev_outcome,
           coalesce({_ED}->>'mailing_recovery_attempts', '') AS prev_attempts,
           coalesce({_ED}->>'mailing_source', '') AS prev_source,
           coalesce({_ED}->>'mailing_lookup_deferred', '') AS prev_deferred
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE upper(sc.state) = 'WA'
      AND j.status = 'done'
      AND r.mailing_address IS NULL
      AND r.parcel_id IS NOT NULL
      AND length(btrim(r.parcel_id)) >= 6
      AND coalesce({_ED}->>'mailing_lookup_deferred', '') <> 'true'
      AND NOT ({_ED} ? '{MARKER}')
      AND (
            (lower(sc.county) = ANY(:counties)
             AND {_ED}->>'mailing_recovery_outcome' = 'error'
             AND {_ATTEMPTS} >= :max_attempts)
         OR (lower(sc.county) = 'island' AND 'island' = ANY(:counties)
             AND {_ED}->>'mailing_recovery_outcome' = 'parcel_not_found'
             AND {_ED}->>'mailing_source' = '{ISLAND_SOURCE}'
             AND btrim(r.parcel_id) ~ '[A-Za-z]')
      )
    ORDER BY cohort, lower(sc.county), r.job_id, r.id
"""  # noqa: S608 -- splices only module constants; every value is bound

# One row, everything it was read with re-checked. The evidence object is built from the
# row's values at THIS moment, before the keys it records are removed.
_REPAIR_SQL = f"""
    UPDATE results r SET enrichment_data = ((
        -- B's source "answered" nothing, so it goes too; A keeps every other key.
        (CASE WHEN CAST(:cohort AS text) = 'B'
              THEN {_ED} - 'mailing_recovery_outcome' - 'mailing_recovery_attempts' - 'mailing_source'
              ELSE {_ED} - 'mailing_recovery_outcome' - 'mailing_recovery_attempts' END)
        || jsonb_build_object('{MARKER}', jsonb_build_object(
               'cohort', CAST(:cohort AS text), 'reason', CAST(:reason AS text),
               'at', CAST(:at AS text),
               'previous_outcome', {_ED}->'mailing_recovery_outcome',
               'previous_attempts', {_ED}->'mailing_recovery_attempts',
               'previous_last_at', {_ED}->'mailing_recovery_last_at',
               'previous_source', {_ED}->'mailing_source',
               'previous_deferred', {_ED}->'mailing_lookup_deferred'))
        || CAST(:patch AS jsonb)))::json
    WHERE r.id = :rid AND r.user_id = :uid AND r.job_id = :jid AND r.parcel_id = :pid
      AND r.mailing_address IS NULL
      AND (r.enrichment_data IS NULL OR jsonb_typeof(r.enrichment_data::jsonb) IN ('object', 'null'))
      AND NOT ({_ED} ? '{MARKER}')
      AND coalesce({_ED}->>'mailing_recovery_outcome', '') = :prev_outcome
      AND coalesce({_ED}->>'mailing_recovery_attempts', '') = :prev_attempts
      AND coalesce({_ED}->>'mailing_source', '') = :prev_source
      AND coalesce({_ED}->>'mailing_lookup_deferred', '') = :prev_deferred
"""  # noqa: S608 -- splices only module constants; every value is bound


# What the deployed pacs_parcel.py must carry before Clallam rows are re-queued: the
# site's land suffix, the resolver handing it to the grid, and the detail check naming
# the row the grid matched. Each is structure, not a token a comment could satisfy alone.
_CLALLAM_FIX_SHAPES = (
    re.compile(r'"clallam":\s*PacsSite\([^)]*land_suffix="0000"[^)]*short_id_len=12'),
    re.compile(r"land_id=land_ids\[key\]"),
    re.compile(r"parse_detail\(resp\.text,\s*matched_key\)"),
)


def commit_has_fix(sha: str, counties: tuple[str, ...] = RETRY_COUNTIES) -> bool:
    """True when commit `sha` (as known to this checkout) carries #575's fix, and, when
    ``counties`` includes Clallam, the 12-digit land-id fix too.

    Only a hex commit id is accepted, so the argument can never reach git as an option.
    """
    git = shutil.which("git")
    if not git or not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        return False
    try:
        src = subprocess.run(  # noqa: S603 -- absolute git, fixed argv, validated sha, no shell
            [git, "-C", str(REPO), "show", f"{sha}:src/scrapers/enrichment/pacs_parcel.py"],
            capture_output=True, text=True, check=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    if "search_type_field" not in src:
        return False
    return "clallam" not in counties or all(p.search(src) for p in _CLALLAM_FIX_SHAPES)


def select_candidates(db, counties: tuple[str, ...] = RETRY_COUNTIES) -> list:
    """Both cohorts in ``counties``, as read now. Every later write re-checks its own row."""
    rows = db.execute(text(_CANDIDATES_SQL),
                      {"counties": list(counties), "max_attempts": _MAX_ATTEMPTS}).all()
    db.rollback()   # release the read snapshot
    return rows


class LeaseLostError(RuntimeError):
    """The mailing-recovery lock is no longer ours: stop before the next write."""


# Compare-and-act on the lock: only the holder of OUR token may extend or delete it, so a
# repair whose lease ran out can never extend, or release, a sweep tick's lock.
_RENEW_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
              "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end")
_OWNED_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then return 1 else return 0 end")
_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end")


class Lease:
    """The sweeps' single-flight lock (mailing_recovery._LOCK_KEY), held with an owner token.

    The sweep ticks (GIS and King) take this key with SET NX, so while the repair holds it
    no tick runs, and none can overwrite a repaired row with a stale answer. `check()` runs
    before every database step and ALWAYS proves the key still holds our token (Redis can
    lose a key, and a tick can then take it, at any moment); only the TTL renewal is
    throttled, to once per `_RENEW_EVERY_S`. With every statement capped at
    `_STATEMENT_TIMEOUT_S`, the longest gap between renewals stays far inside the TTL. Any
    Redis error or lost ownership raises LeaseLostError: the repair fails closed.
    """

    def __init__(self, client, key: str, ttl_s: int):
        self.client, self.key, self.ttl_s = client, key, ttl_s
        self.token = uuid.uuid4().hex
        self._renewed = 0.0

    def acquire(self) -> bool:
        if not self.client.set(self.key, self.token, nx=True, ex=self.ttl_s):
            return False
        self._renewed = time.monotonic()
        return True

    def check(self) -> None:
        renew = time.monotonic() - self._renewed >= _RENEW_EVERY_S
        try:
            if renew:
                ok = self.client.eval(_RENEW_LUA, 1, self.key, self.token, self.ttl_s)
            else:
                ok = self.client.eval(_OWNED_LUA, 1, self.key, self.token)
        except Exception as exc:  # noqa: BLE001 -- any Redis failure means we cannot prove the lease
            raise LeaseLostError(f"lease check failed: {type(exc).__name__}") from exc
        if not ok:
            raise LeaseLostError("the lock expired or another holder took it")
        if renew:
            self._renewed = time.monotonic()

    def release(self) -> None:
        try:
            self.client.eval(_RELEASE_LUA, 1, self.key, self.token)
        except Exception:  # noqa: BLE001, S110 -- the TTL releases it; never touches another holder
            pass


_RENEW_EVERY_S = 60.0
_STATEMENT_TIMEOUT_S = 120   # each select/update; 60 + 120 s worst gap << the 1200 s TTL


def apply_rows(db, rows: list, stats: dict, lease: Lease | None = None) -> None:
    """One guarded UPDATE + commit per row; a row changed since it was read is a no-op.
    With a lease, its ownership is checked (and renewed when due) before every write."""
    at = datetime.now(UTC).isoformat()
    for r in rows:
        if lease is not None:
            lease.check()
        cohort_a = r.cohort == "A"
        result = db.execute(text(_REPAIR_SQL), {
            "rid": r.id, "uid": r.user_id, "jid": r.job_id, "pid": r.parcel_id,
            "prev_outcome": r.prev_outcome, "prev_attempts": r.prev_attempts,
            "prev_source": r.prev_source, "prev_deferred": r.prev_deferred,
            "cohort": r.cohort, "reason": REASON, "at": at,
            "patch": json.dumps({"mailing_lookup_deferred": True} if cohort_a else {}),
        })
        db.commit()
        stats["repaired" if result.rowcount else "skipped_changed"] += 1


def repair(db, *, apply: bool, report: Path, lease: Lease | None = None,
           counties: tuple[str, ...] = RETRY_COUNTIES) -> dict:
    """Select, report, then (only with ``apply``) repair. Returns counts."""
    if lease is not None:
        lease.check()
    rows = select_candidates(db, counties)
    stats: dict = {
        "candidates": len(rows),
        "by_cohort": dict(Counter(r.cohort for r in rows)),
        "by_county": dict(Counter(f"{r.cohort}:{r.county}" for r in rows)),
        "jobs": len({r.job_id for r in rows}),
        "repaired": 0,
        "skipped_changed": 0,
    }
    # The evidence file is complete on disk before the first write; a failure here
    # (bad path, full disk) raises before anything changes.
    with report.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({
                "result_id": str(r.id), "job_id": str(r.job_id), "county": r.county,
                "cohort": r.cohort, "parcel_id": r.parcel_id,
                "previous_outcome": r.prev_outcome, "previous_attempts": r.prev_attempts,
                "action": "repair" if apply else "dry-run",
            }) + "\n")
    if apply:
        apply_rows(db, rows, stats, lease=lease)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--deployed-commit", default="",
                    help="the commit the worker service runs; required with --apply")
    ap.add_argument("--report", type=Path,
                    default=Path(f"repair_pacs_mailing_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"),
                    help="JSON-lines evidence file (one line per candidate row)")
    # Clallam's leads carry 12-digit ids the portal only prints extended to 16: repairing
    # them on code without the land-id fix would spend their fresh attempts on
    # "ambiguous" and mark them so they are never repaired again (commit_has_fix).
    ap.add_argument("--county", action="append", choices=RETRY_COUNTIES, default=None,
                    help="repair only this county (repeatable; default: every PACS county)")
    args = ap.parse_args(argv)
    counties = tuple(dict.fromkeys(args.county)) if args.county else RETRY_COUNTIES

    from src.db.session import system_sync_session

    if not args.apply:
        with system_sync_session() as db:
            stats = repair(db, apply=False, report=args.report, counties=counties)
        print(json.dumps(stats, indent=2))
        print(f"evidence -> {args.report}")
        return 0

    if not args.deployed_commit or not commit_has_fix(args.deployed_commit, counties):
        print("refused: --deployed-commit must name the worker's running commit, and it must "
              "contain the #575 PACS fix, plus the Clallam land-id fix when Clallam is "
              "included (fetch main first if this checkout lacks it). Re-queued rows would "
              "otherwise burn their attempts on the old code.")
        return 2
    import redis as sync_redis

    from src.config import settings
    from src.workers import mailing_recovery as mr

    try:
        lease = Lease(sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()),
                      mr._LOCK_KEY, mr._LOCK_TTL_S)
        held = lease.acquire()
    except Exception as exc:  # noqa: BLE001 -- unreachable Redis: refuse, never run unlocked
        print(f"refused: Redis is unreachable ({type(exc).__name__}); the repair never runs unlocked.")
        return 3
    if not held:
        print("refused: the mailing-recovery lock is held (a sweep tick is running). "
              "Retry in a few minutes.")
        return 3
    try:
        with system_sync_session() as db:
            db.execute(text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT_S}s'"))
            stats = repair(db, apply=True, report=args.report, lease=lease,
                           counties=counties)
    except LeaseLostError as exc:
        print(f"stopped: {exc}. Rows written before this point are listed as repaired in the "
              "database; rerun later to finish (repaired rows are never selected again).")
        return 4
    finally:
        lease.release()
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
