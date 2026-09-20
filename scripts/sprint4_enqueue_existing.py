"""RETIRED: Sprint 4 one-off that enqueued skip-trace rows for existing
Thurston/Kitsap/Whatcom records.

It ran once, in Sprint 4, for three counties, and it is not coming back in this
form. The body was DELETED rather than guarded at the 098 cutover (Codex, second
security pass): leaving it behind a `return 1` meant the whole PII-copying
routine sat one deleted line away from running again, and a future reader
removing an unexplained early return is a much likelier accident than one
rewriting a retired migration on purpose.

Why it cannot run as written:

  * It read the LEGACY address-only cache key (`address_cache_key`), which
    carries no owner name, and wrote whatever it found straight onto the lead.
    After migration 098 that copies whichever owner happened to be traced last
    at an address onto an unrelated owner's record -- the exact leak 098 exists
    to close.
  * It was never a faithful copy of the enqueue path anyway: it built a stub
    Result and substituted the MAILING address for the property address, so even
    re-keying it to `lookup_subject_key` would key some rows to the wrong
    subject.

If existing records genuinely need skip-trace rows enqueued again, use
`scripts/backfill_skip_trace_jobs.py`, which mirrors the current enqueue path,
reads the v2 subject key, records `skip_trace_subject_hash`, and is dry-run by
default.

The original implementation is in git history if it is ever needed for
reference: `git log --follow -- scripts/sprint4_enqueue_existing.py`.
"""

import sys


def main() -> int:
    print(
        "RETIRED: this Sprint 4 one-off read the pre-098 address-only cache key "
        "and would copy another owner's contacts onto a lead. Its body was "
        "removed deliberately. Use scripts/backfill_skip_trace_jobs.py instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
