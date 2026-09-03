"""Lead actionability — the standing rule for what counts as a deliverable lead.

Product decision (owner, 2026-09-02): a result row with NO property address and
NO mailing address cannot be mailed, called or visited, so it is not a lead. It
is not displayed, not exported, not counted in the job headline and not billed
against the monthly quota. The row itself is KEPT in `results`:

- cross-job dedup (delivered_records) still sees it, so a later filing on the
  same estate is not double-counted;
- it remains a scraper-health signal (a jump in unactionable rows is how a
  county layout change is noticed);
- a later backfill that fills an address makes the row appear on its own.

Same shape as src/api/tax_filters.tax_cap_condition: an ORM predicate that is
safe to AND onto ANY Result query, a raw-SQL twin for the hand-written
batch/segment queries, and a Python twin for in-memory row lists — one rule,
three spellings, so the table, the CSV, the batch export and the bill can never
disagree about which rows are leads.
"""
from typing import Any

from sqlalchemy import and_, func, or_

from src.db.models import Result

# Older enrichment writers stored this literal instead of NULL when a parcel
# lookup came back empty. It is a placeholder, never an address.
#
# It lands in BOTH columns, not just property_address: enrichment/parcel.py's
# failure return sets property_address AND mailing_address to it. Excluding it
# on only one side let a fully-failed row pass the rule through the other
# (Codex, 2026-09-03) — the row was listed, exported, counted and BILLED with
# no address anywhere. Every branch below rejects it on BOTH columns.
ADDRESS_PLACEHOLDER = "(enrichment unavailable)"

# Rows the plan cap excluded from delivery. The cap CANNOT be applied to the raw
# scrape: a county whose addresses arrive during inline enrichment (King probate,
# the generic GIS sweep) is unscoreable until then, so slicing raw rows to the
# quota could save a fully-quarantined prefix, bill ~0, and silently discard real
# leads the user still had quota for. Instead every row is persisted and enriched,
# and the ones past the quota are marked here — after which this rule hides them
# from display, export, counting and billing alike, so those four can never
# disagree (Codex, 2026-09-03).
#
# Stored in `results.enrichment_data` (plain JSON, NOT EncryptedJSON, so raw SQL
# can read it) rather than a new column: a branch-only migration crash-loops this
# project's prod on deploy. A dedicated boolean is the cleaner long-term model.
DELIVERY_EXCLUDED_KEY = "delivery_excluded_reason"
OVER_QUOTA = "over_quota"


def _not_quota_excluded_sql(alias: str) -> str:
    return (
        f"COALESCE({alias}.enrichment_data->>'{DELIVERY_EXCLUDED_KEY}', '') <> '{OVER_QUOTA}'"
    )


def address_actionable_sql(alias: str) -> str:
    """The ADDRESS half of the rule only — no quota-exclusion clause.

    The plan cap needs this: it ranks the rows that are deliverable ON ADDRESS and
    marks everything past the quota. Ranking with the full rule instead would
    exclude rows this job already marked, so a re-run would renumber the survivors
    and mark a second batch, shrinking the delivered set on every pass.
    Only the cap should use this; every consumer wants actionable_sql.
    """
    return (
        f"(({alias}.property_address IS NOT NULL AND btrim({alias}.property_address) <> '' "
        f"AND btrim({alias}.property_address) <> '{ADDRESS_PLACEHOLDER}') "
        f"OR ({alias}.mailing_address IS NOT NULL AND btrim({alias}.mailing_address) <> '' "
        f"AND btrim({alias}.mailing_address) <> '{ADDRESS_PLACEHOLDER}'))"
    )


def actionable_condition():
    """ORM predicate: the row has a usable property address OR a mailing address.
    Whitespace-trimmed on the DB side so all three spellings agree (Codex)."""
    prop = func.btrim(Result.property_address)
    mail = func.btrim(Result.mailing_address)
    has_address = or_(
        and_(Result.property_address.isnot(None), prop != "", prop != ADDRESS_PLACEHOLDER),
        and_(Result.mailing_address.isnot(None), mail != "", mail != ADDRESS_PLACEHOLDER),
    )
    # `.op("->>")` rather than `.astext`: the column is the generic JSON type,
    # whose comparator has no astext. On PostgreSQL this emits the same operator.
    not_excluded = func.coalesce(
        Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
    ) != OVER_QUOTA
    return and_(has_address, not_excluded)


def actionable_sql(alias: str) -> str:
    """Raw-SQL twin of actionable_condition for hand-written queries. No binds:
    the only literal is the fixed placeholder constant, never user input."""
    return f"({address_actionable_sql(alias)} AND {_not_quota_excluded_sql(alias)})"


def has_deliverable_address(row: Any) -> bool:
    """Python twin of address_actionable_sql: the ADDRESS half of the rule only.

    Use this when the question is genuinely "could we mail/call/visit this?" —
    e.g. the post-enrichment health log, which counts rows enrichment could not
    rescue. Using the full rule there would later fold in quota-excluded rows,
    which DO have addresses and are not an enrichment failure.
    """
    prop = _field(row, "property_address")
    mail = _field(row, "mailing_address")
    if prop and prop.strip() and prop.strip() != ADDRESS_PLACEHOLDER:
        return True
    return bool(mail and mail.strip() and mail.strip() != ADDRESS_PLACEHOLDER)


def is_actionable(row: Any) -> bool:
    """Python twin for ORM rows / ScrapedRecords / dicts (attribute or key access)."""
    enr = _field(row, "enrichment_data")
    if isinstance(enr, dict) and enr.get(DELIVERY_EXCLUDED_KEY) == OVER_QUOTA:
        return False
    return has_deliverable_address(row)


def _field(row: Any, name: str) -> str | None:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
