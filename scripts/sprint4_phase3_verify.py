"""RETIRED: Sprint 4 experiment -- Phase 3 round trip: a live batch of 10 Pierce probate records, polled
for its webhook to measure the phone/email hit rate.

It spent real Tracerfy credits by calling `submit_batch()` DIRECTLY, and that is
why its body is gone (Phase 1b-1b-ii-0, Codex pre-code consult R1). The
dispatcher is the one place allowed to spend: it claims rows under
`pg_try_advisory_xact_lock`, records them as pending rows, and (from 1b-1b-ii)
reserves each account's and the global daily credit allowance inside that lock.
A script calling the provider directly takes none of that, so while any such
script existed the per-account and global caps could not honestly be called
hard: one run would spend outside both, unrecorded, with nothing to reconcile.

The body was deleted rather than guarded, for the reason recorded on
`scripts/sprint4_enqueue_existing.py`: a guard is one deleted line away from
running again, and removing an unexplained early return is a likelier accident
than rewriting a retired script on purpose.

The experiment it ran is finished; its results are in the Sprint 4 notes. To
measure hit rates today, read them from settled production rows
(`pending_skip_trace_rows` completed/unmatched, `results.phone`/`email`) rather
than buying new lookups. If new lookups are ever genuinely needed, enqueue them
so the dispatcher sends them (`scripts/backfill_skip_trace_jobs.py`, dry-run by
default), inside the cap.

The original implementation is in git history:
`git log --follow -- scripts/sprint4_phase3_verify.py`.
"""

import sys


def main() -> int:
    print(
        "RETIRED: this Sprint 4 experiment called Tracerfy's submit_batch() directly, "
        "outside the dispatcher's lock and daily credit caps. Its body was removed "
        "deliberately. Enqueue lookups for the dispatcher instead "
        "(scripts/backfill_skip_trace_jobs.py)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
