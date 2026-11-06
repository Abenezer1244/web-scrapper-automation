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

from sqlalchemy import and_, func, not_, or_

from src.api.lead_qualification import is_qualified, qualified_condition, qualified_sql
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
# A subject row that failed qualification when its run delivered (src/api/lead_qualification.py).
# Frozen like over_quota: a sweep that names the owner later does not put the row in a
# finished run's file or bill; a later run takes the claim and delivers it (owner,
# 2026-10-25). Any other value in this key is not an exclusion.
NOT_QUALIFIED = "not_qualified"
EXCLUSION_REASONS = (OVER_QUOTA, NOT_QUALIFIED)


# Phase 6 privacy (migration 126): a row withheld because a person asked not to be in
# the product. A DEDICATED column, deliberately not a DELIVERY_EXCLUDED_KEY value: that
# key is a scalar, so privacy would overwrite quota/qualification state or be
# overwritten by it. Every rule below refuses a flagged row, so no list, count, file,
# bill, segment, dialer pick or paid lookup can include one. Nothing sets the flag
# until the enforcer sweep (P5) and activation (P7); until then every clause here is
# true for every row. Design: docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md.
def not_privacy_suppressed_sql(alias: str) -> str:
    """The row is not withheld by a privacy rule. ``alias`` is always a code literal."""
    return f"{alias}.privacy_suppressed_at IS NULL"


def not_privacy_suppressed_condition():
    """ORM twin of not_privacy_suppressed_sql."""
    return Result.privacy_suppressed_at.is_(None)


def is_privacy_suppressed(row: Any) -> bool:
    """Python twin. A pre-insert record (dict / ScrapedRecord) has no flag: not suppressed."""
    return _field(row, "privacy_suppressed_at") is not None


def already_delivered_condition():
    """Rows an earlier run of this account already holds the claim for (a prior-run copy).

    ``duplicate_reason`` is 'prior_run', or NULL on rows classified before migration 089,
    when losing a prior claim was the only way to be flagged. Lives here, not in
    results_category, because shown_condition below needs it."""
    return and_(
        Result.is_duplicate.is_(True),
        func.coalesce(Result.duplicate_reason, "prior_run") == "prior_run",
    )


def already_delivered_sql(alias: str) -> str:
    """``already_delivered_condition`` for raw SQL over ``results`` aliased ``alias``.
    ``alias`` is always a code literal, never input."""
    return (f"({alias}.is_duplicate IS TRUE "
            f"AND COALESCE({alias}.duplicate_reason, 'prior_run') = 'prior_run')")


def quota_excluded_sql(alias: str) -> str:
    """The row was marked past the plan cap. The run-count breakdown names these;
    everything else wants the negation inside actionable_sql."""
    return (
        f"COALESCE({alias}.enrichment_data->>'{DELIVERY_EXCLUDED_KEY}', '') = '{OVER_QUOTA}'"
    )


def not_qualified_excluded_sql(alias: str) -> str:
    """The row was marked not qualified at delivery. The run-count breakdown names these."""
    return (
        f"COALESCE({alias}.enrichment_data->>'{DELIVERY_EXCLUDED_KEY}', '') = '{NOT_QUALIFIED}'"
    )


def delivery_excluded_sql(alias: str) -> str:
    """Any delivery exclusion (over quota or not qualified)."""
    listed = ", ".join(f"'{r}'" for r in EXCLUSION_REASONS)
    return f"COALESCE({alias}.enrichment_data->>'{DELIVERY_EXCLUDED_KEY}', '') IN ({listed})"


def delivery_excluded_condition():
    """ORM twin of delivery_excluded_sql."""
    return func.coalesce(
        Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
    ).in_(EXCLUSION_REASONS)


def withheld_sql(alias: str) -> str:
    """Never deliver or buy contact data for this row: excluded, not qualified, or
    withheld by a privacy rule.

    What anything that SPENDS money on a row (paid skip trace) checks, so a stamped code
    violation is refused even in the window before the hold step has marked it."""
    return (f"({delivery_excluded_sql(alias)} OR NOT {qualified_sql(alias)} "
            f"OR NOT {not_privacy_suppressed_sql(alias)})")


def withheld_condition():
    """ORM twin of withheld_sql."""
    return or_(delivery_excluded_condition(), not_(qualified_condition()),
               not_(not_privacy_suppressed_condition()))


def _not_quota_excluded_sql(alias: str) -> str:
    return f"NOT ({delivery_excluded_sql(alias)})"


def deliverable_before_cap_sql(alias: str) -> str:
    """What the plan cap ranks: address AND qualified AND not marked not qualified AND
    not withheld by a privacy rule (a withheld row never takes or holds quota).

    Leaves the over_quota mark out on purpose (see address_actionable_sql): a re-run must
    rank the rows it already marked, or it would renumber and mark a second batch. The
    not_qualified mark is written before the cap ranks, from the same rule, so including
    it changes nothing on a first pass and keeps a re-run from ranking a held row.
    """
    return (f"({address_actionable_sql(alias)} AND {qualified_sql(alias)} "
            f"AND NOT ({not_qualified_excluded_sql(alias)}) "
            f"AND {not_privacy_suppressed_sql(alias)})")


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
    ).notin_(EXCLUSION_REASONS)
    return and_(has_address, not_excluded, qualified_condition(),
                not_privacy_suppressed_condition())


def shown_condition():
    """What a run's Results view and its already-delivered counts may show: the
    actionable rule, except that a prior-run copy is not judged by qualification.

    A copy repeats a lead an earlier run delivered, and duplicate copies are never
    enriched, so a code-violation copy has no owner of its own. Judged by the gate,
    every already-delivered code violation vanished from that list and its count while
    the run breakdown still said "N already delivered" (found 2026-10-27, before any
    production run hit it). DISPLAY ONLY: claim transfer, billing, segments, dialers
    and paid lookups keep actionable_condition, so an unqualified copy can never take a
    claim, be delivered or be traced. For non-duplicates this IS actionable_condition."""
    not_excluded = func.coalesce(
        Result.enrichment_data.op("->>")(DELIVERY_EXCLUDED_KEY), ""
    ).notin_(EXCLUSION_REASONS)
    return and_(has_address_condition(), not_excluded,
                or_(qualified_condition(), already_delivered_condition()),
                not_privacy_suppressed_condition())


def has_address_condition():
    """ORM predicate for the ADDRESS half of `actionable_condition` alone.

    `actionable_condition` is address + "not excluded from delivery", and the
    exclusion reasons (over-quota today, duplicates at the query layer) are why a row
    is not DELIVERABLE — not evidence that the job scraped nothing. The download
    endpoint needs that distinction: a job whose rows all hit the plan cap, or are all
    duplicates, still produced rows and its completion email still links to the
    download, so it must get a header-only CSV rather than a 404 (Codex).
    """
    prop = func.btrim(Result.property_address)
    mail = func.btrim(Result.mailing_address)
    return or_(
        and_(Result.property_address.isnot(None), prop != "", prop != ADDRESS_PLACEHOLDER),
        and_(Result.mailing_address.isnot(None), mail != "", mail != ADDRESS_PLACEHOLDER),
    )


def actionable_sql(alias: str) -> str:
    """Raw-SQL twin of actionable_condition for hand-written queries. No binds:
    the only literal is the fixed placeholder constant, never user input."""
    return (f"({address_actionable_sql(alias)} AND {_not_quota_excluded_sql(alias)} "
            f"AND {qualified_sql(alias)} AND {not_privacy_suppressed_sql(alias)})")


def is_actionable(row: Any) -> bool:
    """Python twin for ORM rows / ScrapedRecords / dicts (attribute or key access)."""
    if is_privacy_suppressed(row):
        return False
    enr = _field(row, "enrichment_data")
    if isinstance(enr, dict) and enr.get(DELIVERY_EXCLUDED_KEY) in EXCLUSION_REASONS:
        return False
    return has_address(row) and is_qualified(row)


def has_address(row: Any) -> bool:
    """The ADDRESS half of ``is_actionable`` alone (Python twin of
    ``address_actionable_sql``): a usable property OR mailing address."""
    prop = _field(row, "property_address")
    mail = _field(row, "mailing_address")
    if prop and prop.strip() and prop.strip() != ADDRESS_PLACEHOLDER:
        return True
    return bool(mail and mail.strip() and mail.strip() != ADDRESS_PLACEHOLDER)


def _field(row: Any, name: str) -> str | None:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
