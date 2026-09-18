"""Preflight for the §7 retention sweep. Read-only. Runbook step 4/5/6 in one command.

    railway run --service worker python scripts/verify_retention_ready.py

Exists because enabling this sweep depends on four things that a deploy does NOT
do for you, each of which fails SILENTLY:

  * migration 096 builds its index CONCURRENTLY, which can fail partway and leave
    an INVALID index behind — the sweep still runs, just without the index;
  * the DELETE grant on skip_trace_cache is applied by the cutover script, and
    `alembic upgrade head` is the only thing deploy runs. Losing exactly this kind
    of grant is how this repo stranded 16,761 dedup claims on delivered_records,
    and it presented as nothing at all until someone went looking;
  * the R2 lifecycle rule is set by hand, and it is the only half that catches an
    export uploaded moments after a purge commits;
  * the beat entry has to actually be registered in the deployed worker.

Checks only. It changes nothing, and it prints no secret — bucket and endpoint are
shown because you need to confirm WHICH bucket, credentials never are.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from src.config import settings  # noqa: E402

_INDEX = "ix_results_skip_trace_attempted"
_RULE_ID = "bridgeleads-export-retention"

_PASS, _FAIL, _WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str, str]] = []


def _record(state: str, name: str, detail: str) -> None:
    _results.append((state, name, detail))


def _check_db() -> None:
    # No "is it set?" guard: DATABASE_URL_SYNC is a REQUIRED Settings field, so if
    # this module imported at all, it is set. A guard here would be dead code.
    engine = create_engine(settings.DATABASE_URL_SYNC)
    with engine.connect() as conn:
        # 1. migration 096's index, and whether CONCURRENTLY actually finished.
        row = conn.execute(
            text(
                "SELECT i.indisvalid FROM pg_class c "
                "JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name"
            ),
            {"name": _INDEX},
        ).first()
        if row is None:
            _record(_FAIL, "migration 096 index", f"{_INDEX} does not exist - has the deploy run?")
        elif not row[0]:
            _record(
                _FAIL, "migration 096 index",
                f"{_INDEX} exists but is INVALID: CREATE INDEX CONCURRENTLY failed "
                "partway. DROP INDEX CONCURRENTLY and re-run the migration.",
            )
        else:
            _record(_PASS, "migration 096 index", f"{_INDEX} present and valid")

        # 2. The grant the sweep's cache DELETE depends on.
        role = conn.execute(text("SELECT current_user")).scalar()
        can_delete = conn.execute(
            text("SELECT has_table_privilege(current_user, 'skip_trace_cache', 'DELETE')")
        ).scalar()
        if can_delete:
            _record(_PASS, "cache DELETE grant", f"{role} may DELETE skip_trace_cache")
        else:
            _record(
                _FAIL, "cache DELETE grant",
                f"{role} CANNOT DELETE skip_trace_cache. Re-run: "
                "PYTHONPATH=. python scripts/_cutover_step2_grants_policies.py",
            )

        # 3. The blanket UPDATE the results purge relies on.
        can_update = conn.execute(
            text("SELECT has_table_privilege(current_user, 'results', 'UPDATE')")
        ).scalar()
        _record(
            _PASS if can_update else _FAIL, "results UPDATE grant",
            f"{role} {'may' if can_update else 'CANNOT'} UPDATE results",
        )

        # 4. How much is actually waiting. Not a gate — context for the dry run.
        pending = conn.execute(
            text(
                "SELECT count(*) FROM results WHERE skip_trace_attempted_at "
                "< now() - make_interval(days => :d) AND skip_trace_status "
                "NOT IN ('purged','queued','submitted') AND (phone IS NOT NULL OR "
                "email IS NOT NULL OR phones IS NOT NULL OR emails IS NOT NULL)"
            ),
            {"d": settings.SKIP_TRACE_PII_RETENTION_DAYS},
        ).scalar()
        _record(_PASS, "rows awaiting purge", f"{pending} results rows are past retention")


def _check_beat() -> None:
    try:
        from src.workers.scheduler import app
    except Exception as exc:  # noqa: BLE001 — a preflight reports, never raises
        _record(_FAIL, "beat registration", f"scheduler import failed: {str(exc)[:120]}")
        return
    entry = app.conf.beat_schedule.get("purge-skip-trace-pii")
    if entry:
        _record(_PASS, "beat registration", f"purge-skip-trace-pii @ {entry['schedule']}")
    else:
        _record(_FAIL, "beat registration", "purge-skip-trace-pii is NOT in beat_schedule")


def _check_r2() -> None:
    creds = all(
        getattr(settings, n, "")
        for n in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    )
    if not creds:
        _record(_WARN, "R2 lifecycle", "R2 credentials absent - cannot check the rule")
        return
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3", endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY, region_name="auto",
    )
    bucket = settings.R2_BUCKET_NAME
    try:
        rules = client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchLifecycleConfiguration", "404", "NoSuchConfiguration"):
            rules = []
        else:
            _record(_FAIL, "R2 lifecycle", f"{bucket}: {code}")
            return
    ours = [r for r in rules if r.get("ID") == _RULE_ID]
    if not ours:
        _record(
            _FAIL, "R2 lifecycle",
            f"{bucket} has no '{_RULE_ID}' rule. Delivered exports still hold the "
            "same phone numbers. Run scripts/set_r2_lifecycle.py --apply",
        )
    elif ours[0].get("Status") != "Enabled":
        _record(_FAIL, "R2 lifecycle", f"'{_RULE_ID}' exists but is {ours[0].get('Status')}")
    else:
        _record(
            _PASS, "R2 lifecycle",
            f"{bucket}: expire after {ours[0].get('Expiration', {}).get('Days')}d "
            f"({len(rules)} rule(s) total)",
        )


def _check_settings() -> None:
    mode = (
        "ENFORCING" if settings.RETENTION_PURGE_ENABLED and not settings.RETENTION_PURGE_DRY_RUN
        else "dry run" if settings.RETENTION_PURGE_ENABLED
        else "OFF"
    )
    _record(
        _PASS, "mode",
        f"{mode}  (pii={settings.SKIP_TRACE_PII_RETENTION_DAYS}d "
        f"cache={settings.SKIP_TRACE_CACHE_RETENTION_DAYS}d "
        f"link={settings.SKIP_TRACE_LINK_RETENTION_DAYS}d "
        f"export={settings.EXPORT_RETENTION_DAYS}d batch={settings.RETENTION_PURGE_BATCH})",
    )


def main() -> None:
    print("Retention preflight - read-only, changes nothing\n")
    _check_settings()
    _check_beat()
    for label, fn in (("database", _check_db), ("R2 lifecycle", _check_r2)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a preflight reports, never raises
            # Driver errors are multi-line and carry a doc URL; collapse them so
            # one broken check cannot bury the rest of the report.
            _record(_FAIL, label, " ".join(str(exc).split())[:160])

    width = max(len(n) for _, n, _ in _results)
    for state, name, detail in _results:
        print(f"  [{state}] {name.ljust(width)}  {detail}")

    failed = [n for s, n, _ in _results if s == _FAIL]
    print()
    if failed:
        print(f"NOT READY - {len(failed)} check(s) failed: {', '.join(failed)}")
        sys.exit(1)
    print("All checks passed. Safe to proceed to the dry run (runbook 5g step 7).")


if __name__ == "__main__":
    main()
