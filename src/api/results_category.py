"""Which of a job's rows a Results view (and its CSV) is about.

A run's actionable rows fall into buckets the Results page names in its header:

  - ``new``: not a duplicate. Billed, emailed, exported by default. This is the only
    set the page used to list.
  - ``already_delivered``: flagged because an EARLIER run of the SAME account holds
    the dedup claim on the same property key (``delivered_records`` is unique on
    ``(user_id, dedup_hash)``, so another account's claim can never flag a row here).
    ``duplicate_reason`` is ``'prior_run'``, or NULL on rows classified before
    migration 089, when losing a prior claim was the only way to be flagged.
  - ``same_run`` ("combined") and ``superseded`` are deliberately in neither
    category. A combined row is another filing of a property that IS listed as new
    in this same run; a superseded row was never delivered and a later run took its
    claim. Neither is something this run already gave the user. Both are always
    written with ``is_duplicate = true`` (dedup.py sets the two together, and
    ``_promote_elected`` clears the reason whenever it un-flags a row), so the
    ``new`` predicate, unchanged from before this module existed, cannot reach them.

The predicate lives here once so the list, the header count and the CSV cannot
drift apart: the tab reading "227" must be the set the table pages through and the
file contains.
"""
from typing import Literal

from sqlalchemy import and_, func, or_

from src.db.models import Result

ResultsCategory = Literal["new", "already_delivered"]
DEFAULT_RESULTS_CATEGORY: ResultsCategory = "new"


def already_delivered_condition():
    """Rows an earlier run of this account already holds the claim for."""
    return and_(
        Result.is_duplicate.is_(True),
        func.coalesce(Result.duplicate_reason, "prior_run") == "prior_run",
    )


def already_delivered_sql(alias: str) -> str:
    """``already_delivered_condition`` for raw SQL over ``results`` aliased ``alias``.
    Kept beside the ORM form so the two spellings cannot drift. ``alias`` is always a
    code literal, never input."""
    return (f"({alias}.is_duplicate IS TRUE "
            f"AND COALESCE({alias}.duplicate_reason, 'prior_run') = 'prior_run')")


def skip_trace_eligible_condition():
    """Rows a run with skip trace on may look up: the ones it delivers now, and the ones
    an earlier run of this account already delivered.

    Delivered and traced are separate facts. Dedup stops a lead being delivered and
    billed twice; it does not stop the account getting that lead's phone and email when
    it turns skip trace on later (owner, 2026-09-18). Same-run siblings and superseded
    rows stay out: another row of this run is the same property, and it is the one
    traced. Whether a lookup is actually BOUGHT is decided after this, against reuse
    and the tenant cache."""
    return or_(Result.is_duplicate.is_(False), already_delivered_condition())


def skip_trace_eligible_sql(alias: str) -> str:
    """``skip_trace_eligible_condition`` for raw SQL (see ``already_delivered_sql``)."""
    return f"({alias}.is_duplicate IS NOT TRUE OR {already_delivered_sql(alias)})"


def category_condition(category: ResultsCategory):
    """The row predicate for one category. Callers still AND on job, user,
    actionability and the tax cap; this only chooses the bucket."""
    if category == "already_delivered":
        return already_delivered_condition()
    if category == "new":
        return Result.is_duplicate.is_(False)
    # The routes validate against ResultsCategory, so this is a programming error.
    # Falling back to "new" would quietly hand a caller the wrong set of rows.
    raise ValueError(f"unknown results category: {category!r}")
