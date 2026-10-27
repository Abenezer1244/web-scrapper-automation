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
from datetime import date
from typing import Literal

from sqlalchemy import and_, func, not_, or_, select

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NOT_QUALIFIED,
    already_delivered_condition,
    already_delivered_sql,
    has_address_condition,
    shown_condition,
)
from src.api.tax_filters import tax_cap_condition
from src.db.models import Result

ResultsCategory = Literal["new", "already_delivered"]
DEFAULT_RESULTS_CATEGORY: ResultsCategory = "new"

# What the Results LIST may page through: the two lead categories, plus the rows
# the run-count breakdown names "no_address" (no usable property AND no mailing
# address) and "not_qualified" (code violations held back by src/api/lead_qualification.py).
# Those are not leads, so they exist only here, to be looked at: the CSV, the export URL
# and contact lookups keep validating ResultsCategory and refuse them.
ResultsListCategory = Literal["new", "already_delivered", "no_address", "not_qualified"]


def no_address_condition():
    """Exactly the run breakdown's first branch (``NOT address_actionable_sql``):
    every row of the job with neither address, whatever else is true of it. No
    duplicate or superseded exclusion, because the breakdown applies none either."""
    return not_(has_address_condition())


# already_delivered_condition / already_delivered_sql live in lead_actionability
# (shown_condition needs them); routes and workers keep importing them from here.


def not_qualified_condition():
    """The rows this run held back as not qualified when it delivered.

    Keyed on the hold MARK, not on is_duplicate: the mark is written once, before
    billing, and nothing clears it, so the list keeps naming what the run held back.
    A later run that takes the claim and delivers the lead turns the held row into a
    'superseded' duplicate (dedup.transfer_undelivered_claims keeps the mark); it stays
    listed here and its duplicate_source_job_id names that later run. Same-run siblings
    carry the mark too but are another filing of a listed row, so they stay out, as in
    the run breakdown. A held row with no address is in the no_address list instead.
    Only meaningful once the run delivered (the route gates every list on that)."""
    return and_(
        has_address_condition(),
        func.coalesce(Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), "") == NOT_QUALIFIED,
        or_(Result.is_duplicate.is_(False), Result.duplicate_reason == "superseded"),
    )


def list_condition(category: ResultsListCategory):
    """The row predicate for one Results LIST category: the two lead categories plus the
    two look-only ones. Their tab counts use this same predicate."""
    if category == "no_address":
        return no_address_condition()
    if category == "not_qualified":
        return not_qualified_condition()
    return category_condition(category)


def skip_trace_eligible_condition():
    """Rows a run with skip trace on may look up: the ones it delivers now, and the ones
    an earlier run of this account already delivered.

    Delivered and traced are separate facts. Dedup stops a lead being delivered and
    billed twice; it does not stop the account getting that lead's phone and email when
    it turns skip trace on later (owner, 2026-09-18). Same-run siblings and superseded
    rows stay out: another row of this run is the same property, and it is the one
    traced. Whether a lookup is actually BOUGHT is decided after this, against reuse
    and the tenant cache."""
    # IS NOT TRUE, the same spelling as skip_trace_eligible_sql (the column is NOT NULL,
    # but the two forms must not be able to disagree).
    return or_(Result.is_duplicate.is_not(True), already_delivered_condition())


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


def download_rows_select(job_id: str, user_id: str, today: date,
                         category: ResultsCategory = DEFAULT_RESULTS_CATEGORY):
    """The rows of one job's lead CSV before any view filter, in file order: the
    product's standing rules (shown, the tax-delinquency cap, the category), never
    a user's choice. shown_condition is actionable_condition for new leads; it only lets
    the already-delivered file keep the code violations its tab lists. Shared by GET /jobs/{id}/download and the account data export, so
    "every lead CSV you can download" is the same file in both."""
    return (
        select(Result)
        .where(
            Result.job_id == job_id, Result.user_id == user_id,
            tax_cap_condition(today), shown_condition(), category_condition(category),
        )
        # Groups an estate's records together; the same order the R2 export uses.
        .order_by(Result.party_name, Result.date_recorded, Result.id)
    )
