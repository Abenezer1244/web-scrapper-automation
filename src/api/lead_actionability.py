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


def actionable_condition():
    """ORM predicate: the row has a usable property address OR a mailing address.
    Whitespace-trimmed on the DB side so all three spellings agree (Codex)."""
    prop = func.btrim(Result.property_address)
    mail = func.btrim(Result.mailing_address)
    return or_(
        and_(Result.property_address.isnot(None), prop != "", prop != ADDRESS_PLACEHOLDER),
        and_(Result.mailing_address.isnot(None), mail != "", mail != ADDRESS_PLACEHOLDER),
    )


def actionable_sql(alias: str) -> str:
    """Raw-SQL twin of actionable_condition for hand-written queries. No binds:
    the only literal is the fixed placeholder constant, never user input."""
    return (
        f"(({alias}.property_address IS NOT NULL AND btrim({alias}.property_address) <> '' "
        f"AND btrim({alias}.property_address) <> '{ADDRESS_PLACEHOLDER}') "
        f"OR ({alias}.mailing_address IS NOT NULL AND btrim({alias}.mailing_address) <> '' "
        f"AND btrim({alias}.mailing_address) <> '{ADDRESS_PLACEHOLDER}'))"
    )


def is_actionable(row: Any) -> bool:
    """Python twin for ORM rows / ScrapedRecords / dicts (attribute or key access)."""
    prop = _field(row, "property_address")
    mail = _field(row, "mailing_address")
    if prop and prop.strip() and prop.strip() != ADDRESS_PLACEHOLDER:
        return True
    return bool(mail and mail.strip() and mail.strip() != ADDRESS_PLACEHOLDER)


def _field(row: Any, name: str) -> str | None:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
