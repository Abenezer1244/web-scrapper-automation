"""Safe, selected-job skip-trace backfill for already-scraped results.

Use this when results were scraped with skip-trace OFF on the scraper config
(skip_trace_status='not_attempted', no phone/email) and you want to enrich them
without re-scraping.

DRY-RUN BY DEFAULT — nothing is written and no credits are spent until you pass
--commit. Even with --commit, this script only ENQUEUES rows into
pending_skip_trace_rows; the running dispatcher (every ~5 min) is what submits to
Tracerfy and spends credits. Cache hits are applied immediately and cost 0.

Mirrors the production enqueue path (src/workers/tasks.py::_enqueue_skip_trace_rows):
  - ORM objects so EncryptedString/EncryptedJSON columns auto-encrypt (no raw-SQL PII).
  - Per-subject cache check (lookup_subject_key: account + address + trace type +
    the exact names, migration 098) — free hits, and never another owner's answer.
  - build_pending_row_payload drops non-personal party names (code-violation
    descriptions) so they don't burn advanced-trace credits.
  - trace_type normal=1 credit, advanced=2 credits (cost estimate).

Safety beyond the production path (per Codex review):
  - Excludes result_ids already present in pending_skip_trace_rows
    (the table has no unique constraint on result_id → avoids double-enqueue).
  - Requires explicit --jobs or --hours scope; never blanket-runs.

Examples (prod env injects DATABASE_URL + TRACERFY_API_TOKEN):
    # Dry-run every job from the last 36h:
    railway run --service worker python scripts/backfill_skip_trace_jobs.py --hours 36
    # Dry-run specific jobs:
    railway run --service worker python scripts/backfill_skip_trace_jobs.py --jobs 9cf1448b,f6ee336a
    # Actually enqueue specific jobs:
    railway run --service worker python scripts/backfill_skip_trace_jobs.py --jobs f6ee336a --commit
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Credit cost per trace type (see tasks.py / build_pending_row_payload comments).
_CREDITS = {"normal": 1, "advanced": 2}


def _tracerfy_balance():
    """Best-effort read of the Tracerfy account credit balance. None on failure."""
    from src.config import settings

    if not settings.TRACERFY_API_TOKEN:
        return None
    try:
        import requests

        r = requests.get(
            "https://tracerfy.com/v1/api/analytics/",
            headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
            timeout=15,
        )
        return r.json().get("balance")
    except Exception as exc:  # noqa: BLE001 — diagnostic best-effort
        return f"<unavailable: {str(exc)[:60]}>"


def _resolve_job_ids(db, args):
    from sqlalchemy import select

    from src.db.models import Job

    if args.jobs:
        wanted = [j.strip() for j in args.jobs.split(",") if j.strip()]
        # Accept short (8-char) prefixes for convenience.
        all_recent = db.execute(
            select(Job.id).where(
                Job.created_at >= datetime.now(UTC) - timedelta(days=90)
            )
        ).scalars().all()
        resolved = []
        for w in wanted:
            matches = [jid for jid in all_recent if jid == w or jid.startswith(w)]
            if not matches:
                print(f"  WARNING: no job matches '{w}' (last 90d) — skipped")
            resolved.extend(matches)
        return resolved

    since = datetime.now(UTC) - timedelta(hours=args.hours)
    return db.execute(
        select(Job.id).where(Job.created_at >= since).order_by(Job.created_at.desc())
    ).scalars().all()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs", help="comma-separated job ids (full or 8-char prefix)")
    ap.add_argument("--hours", type=int, default=36,
                    help="if --jobs omitted, scope to jobs created in the last N hours")
    ap.add_argument("--commit", action="store_true",
                    help="actually enqueue (default is dry-run, no writes, no spend)")
    args = ap.parse_args()

    from sqlalchemy import and_, select

    from src.config import settings
    from src.db.models import (
        Job,
        PendingSkipTraceRow,
        Result,
        ScraperConfig,
        SkipTraceCache,
    )
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import (
        build_pending_row_payload,
        payload_subject_key,
    )
    from src.workers.skip_trace_claim import (
        claim_skip_trace_rows,
        lock_job_for_claim,
    )

    mode = "COMMIT (writes + enables Tracerfy spend via dispatcher)" if args.commit \
        else "DRY-RUN (no writes, no spend)"
    print(f"=== Skip-trace backfill — {mode} ===\n")

    if not settings.SKIP_TRACE_ENABLED:
        print("ABORT: settings.SKIP_TRACE_ENABLED is False.")
        return 1
    if not settings.TRACERFY_API_TOKEN:
        print("ABORT: TRACERFY_API_TOKEN not configured.")
        return 1

    bal = _tracerfy_balance()
    print(f"Tracerfy balance: {bal} credits\n")

    grand = {"eligible": 0, "cache_hit": 0, "enqueue_normal": 0,
             "enqueue_advanced": 0, "nonpersonal": 0, "already_pending": 0}

    with system_sync_session() as db:
        job_ids = _resolve_job_ids(db, args)
        if not job_ids:
            print("No matching jobs. Nothing to do.")
            return 0

        print(f"{len(job_ids)} job(s) in scope.\n")
        print(f"{'job':10} {'county/type':28} {'elig':>5} {'hit':>5} "
              f"{'norm':>5} {'adv':>5} {'skip':>5} {'pend':>5} {'~cred':>6}")
        print("-" * 92)

        for jid in job_ids:
            job = db.get(Job, jid)
            if not job:
                continue
            cfg = db.get(ScraperConfig, job.scraper_config_id)
            label = f"{getattr(cfg,'county','?')}/{getattr(cfg,'record_type','?')}"

            j = {"eligible": 0, "cache_hit": 0, "enqueue_normal": 0,
                 "enqueue_advanced": 0, "nonpersonal": 0, "already_pending": 0}
            to_claim: list[dict] = []

            # The same job-scoped advisory lock the scrape enqueue takes, so this
            # script and a live scrape of the same job cannot interleave their
            # read-decide-claim.
            #
            # Taken BEFORE the reads below, which is the whole point (Codex round
            # 15 diff review, round 6). Reading first and locking afterwards left
            # a window where a live scrape could claim a lead and rewrite its
            # owner in between: this script's ORM objects would then be stale
            # (the session runs expire_on_commit=False), and a stale cache hit
            # would write contacts and 'hit' straight over the scrape's queued
            # row, whose pending row could still be submitted and paid for.
            #
            # Transaction-scoped, released by this job's commit below, so only
            # ONE job lock is held at a time rather than one per job for the
            # length of the run.
            if args.commit:
                lock_job_for_claim(db, jid)

            # result_ids already queued (any status) — never double-enqueue.
            pending_ids = set(db.execute(
                select(PendingSkipTraceRow.result_id)
                .where(PendingSkipTraceRow.job_id == jid)
            ).scalars().all())

            # populate_existing: this session does not expire on commit, so a
            # Result already in the identity map from an earlier job would be
            # returned with its stale attributes. The rows are read under the
            # lock and must reflect what is committed right now.
            rows = db.execute(
                select(Result).where(and_(
                    Result.job_id == jid,
                    Result.skip_trace_status == "not_attempted",
                    Result.property_address.isnot(None),
                )).execution_options(populate_existing=True)
            ).scalars().all()

            for rec in rows:
                if rec.id in pending_ids:
                    j["already_pending"] += 1
                    continue
                payload = build_pending_row_payload(rec)
                if payload is None:
                    j["nonpersonal"] += 1
                    continue
                j["eligible"] += 1

                # v2 subject key (migration 098). The legacy address-only key
                # carries no owner name, so reading it here would copy whichever
                # owner was traced last at this address onto this lead — and this
                # script writes that PII straight onto the row.
                key = payload_subject_key(job.user_id, payload)
                cached = db.get(SkipTraceCache, key)
                cache_valid = bool(
                    cached and (datetime.now(UTC) - cached.fetched_at).days
                    < settings.SKIP_TRACE_CACHE_DAYS
                )

                if cache_valid:
                    j["cache_hit"] += 1
                    if args.commit:
                        rec.phone = cached.phone
                        rec.phone_type = cached.phone_type
                        rec.phone_dnc_flag = cached.phone_dnc_flag
                        rec.email = cached.email
                        rec.phones = cached.phones
                        rec.emails = cached.emails
                        rec.skip_trace_status = (
                            "hit" if (cached.phone or cached.email) else "miss"
                        )
                        rec.skip_trace_attempted_at = datetime.now(UTC)
                        rec.skip_trace_source = "reused"
                        rec.skip_trace_subject_hash = key
                else:
                    # Collected, not inserted here. This script used to build
                    # PendingSkipTraceRow itself, which made it a SECOND writer
                    # of the queue that knew nothing about the scrape enqueue:
                    # both could read one lead as 'not_attempted' and both insert,
                    # and with migration 099 unapplied both rows could be
                    # submitted and charged (Codex round 15 diff review, round 5).
                    # It also truncated `state` to 128 into a String(2) column.
                    # Routing through the shared claim fixes both, and the claim
                    # requires 099 to be enforced before it will write anything.
                    to_claim.append(payload)

            # Claim through the shared helper, which re-checks each lead exists,
            # belongs to this tenant and is still 'not_attempted' inside the one
            # statement that inserts, and withdraws any row whose lead it did not
            # win. Counts come from what it ACTUALLY claimed, not from what we
            # hoped to claim. In a dry run nothing is claimed, so the counts are
            # what WOULD be attempted.
            if to_claim:
                if args.commit:
                    claimed = set(claim_skip_trace_rows(db, to_claim))
                else:
                    claimed = {str(p["result_id"]) for p in to_claim}
                for payload in to_claim:
                    if str(payload["result_id"]) in claimed:
                        j[f"enqueue_{payload['trace_type']}"] += 1

            credits = (j["enqueue_normal"] * _CREDITS["normal"]
                       + j["enqueue_advanced"] * _CREDITS["advanced"])
            print(f"{str(jid)[:8]:10} {label[:28]:28} {j['eligible']:>5} "
                  f"{j['cache_hit']:>5} {j['enqueue_normal']:>5} "
                  f"{j['enqueue_advanced']:>5} {j['nonpersonal']:>5} "
                  f"{j['already_pending']:>5} {credits:>6}")

            for k in grand:
                grand[k] += j[k]

            # Commit PER JOB, which also releases this job's advisory lock
            # before the next one is taken. Committing once at the end would
            # hold every job's lock for the whole run, blocking live scrapes of
            # unrelated jobs, and would make one late failure discard work
            # already done for jobs that succeeded.
            if args.commit:
                db.commit()

        if not args.commit:
            db.rollback()

    total_credits = (grand["enqueue_normal"] * _CREDITS["normal"]
                     + grand["enqueue_advanced"] * _CREDITS["advanced"])
    print("-" * 92)
    print(f"{'TOTAL':10} {'':28} {grand['eligible']:>5} {grand['cache_hit']:>5} "
          f"{grand['enqueue_normal']:>5} {grand['enqueue_advanced']:>5} "
          f"{grand['nonpersonal']:>5} {grand['already_pending']:>5} {total_credits:>6}")
    print()
    print("Legend: elig=eligible persons | hit=free cache hits | norm/adv=paid traces "
          "to enqueue | skip=non-personal names dropped | pend=already queued")
    print(f"Estimated Tracerfy credits to spend on submit: ~{total_credits} "
          f"(normal=1, advanced=2 per lookup). Cache hits cost 0.")
    if not args.commit:
        print("\nDRY-RUN — nothing written. Re-run with --commit to enqueue.")
    else:
        print("\nCOMMITTED — dispatcher submits queued rows within ~5 min; "
              "watch results fill in. Cache hits already applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
