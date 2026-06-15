"""Body logic for the batch beat tasks: batch_completion_sweep +
batch_recovery_sweep (Piece 2 / Track A).

The Track A durability tunables (BATCH_LEASE_MINUTES, BATCH_FORCE_MINUTES,
BATCH_PENDING_REDISPATCH_MINUTES) live here and are re-exported from scheduler.py
so existing imports (and tests) keep resolving from src.workers.scheduler.
"""

from datetime import UTC, datetime, timedelta

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Track A durability tunables.
BATCH_LEASE_MINUTES = 30              # finalize claim lease TTL (> worst-case CSV+R2 build)
BATCH_FORCE_MINUTES = 90             # force-finalize a run stuck 'running' past this age
BATCH_PENDING_REDISPATCH_MINUTES = 3  # re-dispatch a 'pending' run not materialized in time


def _batch_completion_sweep_impl() -> None:
    """Finalize batch_runs whose child jobs are ALL terminal — build the one
    combined CSV + deliver. Does NOT wait on async skip-trace: the CSV is built on
    property identity (ready at child enrichment); contacts fill in later and the
    CSV is re-downloadable. No-op when no run is ready.

    Track A: the claim is a LEASE (claimed_at + claim_token) reclaimable after
    BATCH_LEASE_MINUTES, so a worker hard-killed mid-finalize can't strand a run
    'running' forever (Gap 2). A run still 'running' past BATCH_FORCE_MINUTES is
    FORCE-finalized even with a missing / stuck child, through this SAME claim +
    finalize path (Gap 3b) — one code path, eligibility differs.
    """
    import uuid as _uuid

    from sqlalchemy import func, or_, select, update

    from src.db.models import BatchRun, Job
    from src.db.session import system_sync_session
    from src.workers.batch_export import finalize_batch_run

    _BATCH = 20
    now = datetime.now(UTC)
    lease_cutoff = now - timedelta(minutes=BATCH_LEASE_MINUTES)
    force_cutoff = now - timedelta(minutes=BATCH_FORCE_MINUTES)
    _terminal = ("done", "failed", "cancelled")

    with system_sync_session() as db:
        # Eligible to (re)claim: 'running' AND the lease is free or expired.
        runs = db.execute(
            select(BatchRun)
            .where(
                BatchRun.status == "running",
                or_(BatchRun.claimed_at.is_(None), BatchRun.claimed_at < lease_cutoff),
            )
            .limit(_BATCH)
        ).scalars().all()

        for run in runs:
            distinct_ids = list({str(x) for x in (run.child_job_ids or [])})
            run_id, run_user = run.id, run.user_id
            # Measure stuck-time from when the run STARTED running, not when it was
            # created (Codex P1): a batch that sat 'pending' a long time then just
            # started must not be force-failed while its children are legitimately
            # active. running_at is NULL only for runs materialized before this
            # column existed -> fall back to created_at for those.
            stuck_since = run.running_at or run.created_at
            forced = stuck_since is not None and stuck_since < force_cutoff

            # Require EVERY child to exist for this tenant AND be terminal — not
            # merely "none active" (Codex P2): a missing / cross-tenant / deleted /
            # future-status child means the batch is not actually ready.
            all_terminal = False
            if distinct_ids:
                terminal = db.execute(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.id.in_(distinct_ids),
                        Job.user_id == run_user,
                        Job.status.in_(_terminal),
                    )
                ).scalar_one()
                all_terminal = terminal >= len(distinct_ids)

            # Finalize when all children settled OR the run is past the hard
            # deadline (force-finalize so a permanently-stuck child can't strand it).
            if not all_terminal and not forced:
                continue

            # Atomic LEASE claim before the heavy finalize: win if the lease is free
            # or expired AND the run is still 'running' (a concurrent cancel makes
            # rowcount 0). The claim_token identifies THIS lease owner so the
            # error path only releases a lease it still holds.
            token = str(_uuid.uuid4())
            claimed = db.execute(
                update(BatchRun)
                .where(
                    BatchRun.id == run_id,
                    BatchRun.status == "running",
                    or_(BatchRun.claimed_at.is_(None), BatchRun.claimed_at < lease_cutoff),
                )
                .values(claimed_at=now, claim_token=token)
            ).rowcount
            db.commit()
            if not claimed:
                continue  # another sweep holds the lease, or it was cancelled

            run = db.get(BatchRun, run_id)
            if run is None or run.status != "running":
                continue  # cancelled/deleted after claim — don't finalize

            try:
                finalize_batch_run(db, run, forced=forced, claim_token=token)
            except Exception as exc:  # noqa: BLE001
                # finalize commits status only at the END (after CSV+R2) and the
                # email is best-effort, so a propagating error means nothing was
                # committed. Release the lease (only if we still hold it — token
                # guard) so the next sweep RETRIES (R2 upload overwrites the same
                # key — idempotent), avoiding a permanently stuck 'running' run.
                _logger.error(
                    "batch_completion_sweep: finalize %s failed (will retry): %s",
                    run_id, str(exc)[:200],
                )
                try:
                    db.rollback()
                    db.execute(
                        update(BatchRun)
                        .where(BatchRun.id == run_id, BatchRun.claim_token == token)
                        .values(claimed_at=None, claim_token=None)
                    )
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()


def _batch_recovery_sweep_impl() -> None:
    """Pre-finalize crash recovery for the batch dispatch windows (Track A):

      Gap 1  — a 'pending' run whose dispatch .delay() was lost: the batch sits
               with no jobs. Re-dispatch it (idempotent) every sweep. If it still
               hasn't materialized after BATCH_FORCE_MINUTES, give up and mark it
               'failed' so it can't sit 'pending' forever.
      Gap 3a — a 'running' run with children still 'pending' (a lost child .delay):
               re-enqueue them every sweep (the atomic claim dedupes in-flight).

    All enqueues happen AFTER commit (commit-before-delay). dispatch_batch_run is
    idempotent (FOR UPDATE + UNIQUE(batch_id)); run_scrape_job's atomic claim makes
    a re-enqueue of an in-flight child a no-op.
    """
    from sqlalchemy import select, update

    from src.db.models import BatchRun, Job, ScraperConfig
    from src.db.session import system_sync_session
    from src.workers.batch_tasks import dispatch_batch_run
    from src.workers.tasks import run_scrape_job

    _BATCH = 50
    now = datetime.now(UTC)
    pending_cutoff = now - timedelta(minutes=BATCH_PENDING_REDISPATCH_MINUTES)
    force_cutoff = now - timedelta(minutes=BATCH_FORCE_MINUTES)

    redispatch_run_ids: list[str] = []
    reenqueue_job_ids: list[str] = []
    # M6: give-up alert payloads, dispatched only AFTER the commit below.
    give_up_alerts: list[tuple[str, str, str, str]] = []
    with system_sync_session() as db:
        # Gap 1: 'pending' runs not materialized in time.
        pending_runs = db.execute(
            select(BatchRun)
            .where(BatchRun.status == "pending", BatchRun.created_at < pending_cutoff)
            .limit(_BATCH)
        ).scalars().all()
        give_up_ids: list[str] = []
        for run in pending_runs:
            # FAILURE is purely TIME-based (Codex P2): a run that never materialized
            # within BATCH_FORCE_MINUTES is given up. dispatch_attempts must NOT be a
            # failure cutoff — transient worker backlog / a broker hiccup would then
            # turn a perfectly valid run terminal, and capping re-dispatch would also
            # strand a run if the broker recovered after the cap. So we keep
            # re-dispatching (dispatch_batch_run is idempotent) every sweep until the
            # time cutoff; dispatch_attempts is an OBSERVABILITY counter only.
            if run.created_at is not None and run.created_at < force_cutoff:
                give_up_ids.append(run.id)
            else:
                run.dispatch_attempts = (run.dispatch_attempts or 0) + 1
                # 2B: dispatch is RUN-scoped (runs are plural per batch; the run id
                # is the unambiguous durable intent).
                redispatch_run_ids.append(run.id)

        # Gap 3a: batch child jobs stuck 'pending' (a lost child .delay) under a
        # 'running' run. Drive off the JOBS (bounded by _BATCH) via
        # job -> scraper_config.batch_id -> batch_run, so a large number of healthy
        # running runs can't starve the few with a lost child — the old
        # "load 50 running runs then filter" could page past the run that needed
        # recovery (Codex P2). The partial unique (one ACTIVE run per batch) keeps
        # the status='running' join 1:1; the child_job_ids MEMBERSHIP check below
        # is what makes this correct under 2B plural runs (Codex P2) — a stale
        # pending job from an old terminal run of the same batch must NOT be
        # re-enqueued against the current run. Re-enqueue every sweep (the atomic
        # claim dedupes if one is actually in flight); the 90min force-finalize is
        # the terminal backstop. Skip force-eligible runs: their children are about
        # to be cancelled, so re-enqueueing would let them scrape after
        # terminalization (wasted work, excluded results).
        from sqlalchemy import text as _sql_text
        stuck_children = db.execute(
            select(
                Job.id, BatchRun.id, BatchRun.running_at, BatchRun.created_at,
                BatchRun.child_job_ids,
            )
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .join(BatchRun, BatchRun.batch_id == ScraperConfig.batch_id)
            .where(
                Job.status == "pending",
                Job.trigger == "batch",
                BatchRun.status == "running",
                # Membership in SQL (Codex P2): the LIMIT below must page over
                # rows that ALREADY satisfy membership — stale pending jobs from
                # older terminal runs of the same batch would otherwise fill the
                # page and starve the rows that actually need recovery.
                _sql_text(
                    "batch_runs.child_job_ids::jsonb @> to_jsonb(jobs.id::text)"
                ),
            )
            .limit(_BATCH)
        ).all()
        bumped_run_ids: set[str] = set()
        for job_id, run_id, running_at, created_at, child_job_ids in stuck_children:
            if str(job_id) not in (child_job_ids or []):
                continue  # job belongs to a different (older) run of this batch
            stuck_since = running_at or created_at
            if stuck_since is not None and stuck_since < force_cutoff:
                continue  # force-finalize will cancel this child — don't re-enqueue
            reenqueue_job_ids.append(str(job_id))
            bumped_run_ids.add(run_id)
        if bumped_run_ids:
            # One dispatch_attempts bump per affected run (observability counter).
            db.execute(
                update(BatchRun)
                .where(BatchRun.id.in_(bumped_run_ids))
                .values(dispatch_attempts=BatchRun.dispatch_attempts + 1)
            )

        # STATUS-GUARDED terminalization (Codex P1): only fail runs STILL 'pending'.
        # A delayed dispatch_batch_run can materialize one to 'running' (with active
        # child jobs) concurrently with this give-up; an ORM update-by-PK would
        # clobber that 'running' and orphan the children. WHERE status='pending'
        # makes the loser a no-op.
        for rid in give_up_ids:
            failed = db.execute(
                update(BatchRun)
                .where(BatchRun.id == rid, BatchRun.status == "pending")
                .values(
                    status="failed",
                    failed_children=[{"reason": "dispatch never materialized"}],
                    # Terminal-state consistency: finalize sets completed_at on
                    # every terminal write; this give-up path must too.
                    completed_at=now,
                )
            ).rowcount
            if failed:
                _logger.error(
                    "batch_recovery_sweep: gave up on pending run %s (age > %dm)",
                    rid, BATCH_FORCE_MINUTES,
                )
                # M6: a batch run that never materialized despite repeated
                # re-dispatch usually means broker/worker trouble — queue an
                # ops page, sent post-commit (Codex P2).
                give_up_alerts.append((
                    "batch",
                    str(rid),
                    f"Batch run gave up — dispatch never materialized ({rid})",
                    f"run_id={rid}\nage > {BATCH_FORCE_MINUTES} min in 'pending' "
                    "despite recovery re-dispatch (broker/worker issue likely)",
                ))
        db.commit()

    # Enqueue AFTER commit so a worker can't pick up an uncommitted row. Per-item
    # try/except: a broker failure on one shouldn't abort the rest, and anything
    # that fails stays committed and is retried by the next sweep.
    for rid in redispatch_run_ids:
        try:
            dispatch_batch_run.delay(rid)
        except Exception as exc:  # noqa: BLE001 — recovered next sweep
            _logger.warning("batch_recovery_sweep: re-dispatch of %s failed: %s", rid, str(exc)[:200])
    for jid in reenqueue_job_ids:
        try:
            run_scrape_job.delay(jid)
        except Exception as exc:  # noqa: BLE001 — recovered next sweep
            _logger.warning("batch_recovery_sweep: re-enqueue of %s failed: %s", jid, str(exc)[:200])
    if redispatch_run_ids or reenqueue_job_ids:
        _logger.info(
            "batch_recovery_sweep: re-dispatched %d run(s), re-enqueued %d child job(s)",
            len(redispatch_run_ids), len(reenqueue_job_ids),
        )
    # M6: alerts only after the give-up terminalizations durably committed.
    from src.workers.ops_alerts import send_ops_alert
    for kind, key, subject, body in give_up_alerts:
        send_ops_alert(kind, key, subject, body)
