"""Codex round-7 [P2] verification: can one Result carry MORE THAN ONE
pending_skip_trace_rows row?

There is no unique constraint on pending_skip_trace_rows.result_id (models.py:1038
declares a plain FK; the only Index is the dispatch index on
status/trace_type/enqueued_at), and the enqueue in
src/workers/tasks_helpers/enrich.py:1076 inserts unconditionally. So it is
STRUCTURALLY possible. This asks production whether it actually happens, and in
particular whether any result has two rows sharing the same (first_name,
last_name, trace_type) tuple — that is the exact shape under which
_refresh_pending_name()'s per-result UPDATE would touch both rows on the first
loop iteration and then journal a phantom second write.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_TOTALS = text(
    """
    SELECT count(*) AS pending_rows,
           count(DISTINCT result_id) AS distinct_results
    FROM pending_skip_trace_rows
    """
)

_MULTI = text(
    """
    SELECT result_id, count(*) AS n,
           count(DISTINCT (first_name, last_name, trace_type)) AS distinct_tuples,
           array_agg(status) AS statuses
    FROM pending_skip_trace_rows
    GROUP BY result_id
    HAVING count(*) > 1
    ORDER BY count(*) DESC
    LIMIT 50
    """
)

# The refresh only ever touches this shape, so this is the blast radius that matters.
_MULTI_QUEUED = text(
    """
    SELECT result_id, count(*) AS n
    FROM pending_skip_trace_rows
    WHERE status = 'queued'
      AND tracerfy_queue_id IS NULL
      AND submitted_at IS NULL
    GROUP BY result_id
    HAVING count(*) > 1
    ORDER BY count(*) DESC
    LIMIT 50
    """
)


def main() -> None:
    with system_sync_session() as db:
        t = db.execute(_TOTALS).mappings().one()
        print(f"pending rows={t['pending_rows']} distinct results={t['distinct_results']}")

        multi = db.execute(_MULTI).mappings().all()
        print(f"\nresults with >1 pending row (any status): {len(multi)}")
        for row in multi:
            print(f"  result={row['result_id']} n={row['n']} "
                  f"distinct_name_tuples={row['distinct_tuples']} statuses={row['statuses']}")

        mq = db.execute(_MULTI_QUEUED).mappings().all()
        print(f"\nresults with >1 REFRESHABLE (queued/unsubmitted) pending row: {len(mq)}")
        for row in mq:
            print(f"  result={row['result_id']} n={row['n']}")


if __name__ == "__main__":
    main()
