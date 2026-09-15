"""Which of a job's leads the plan quota delivers, and which it marks over quota.

Lives outside tasks.py so the tests run the statement the job runs, not a copy.
"""

from sqlalchemy import text as sa_text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    address_actionable_sql,
)
from src.scrapers.king_cv_sources import settled_sql

# Every record type without its own order below ranks by who and when, which also keeps
# an estate's records together.
_DEFAULT_RANK_ORDER = "party_name, date_recorded, id"

# Tax delinquent: the largest balance first (owner decision 2026-09-14), the older
# delinquency on a tie, then a stable key. The default order was wrong for tax
# twice over: a tax lead has no date, and a King tax lead's owner name comes from
# a slow per-parcel lookup that reaches a few hundred parcels, so the delivered set
# was "whichever rows happened to get a name" followed by random UUID order.
# Every column here is fixed at scrape time, so the ranking cannot move while
# enrichment runs or between a job and its watchdog re-run.
_TAX_RANK_ORDER = (
    "delinquent_amount DESC NULLS LAST, delinquent_bill_year ASC NULLS LAST, parcel_id, id"
)


# Code violation: open cases before cases the city already settled, the newest case
# first, then a stable key. King (Seattle SDCI) code violations carry no owner at
# scrape time and get one only for exactly located parcels inside a time budget, so
# ranking by party_name would let enrichment timing decide which leads are billed.
# Settled statuses (SDCI "Completed" / "Open Duplicate", King County Accela voided and
# no-violation cases; the same set auto skip trace skips) rank last, they are not
# removed. The bucket is scoped by source (src/scrapers/king_cv_sources SETTLED_STATUSES):
# Tacoma uses other status words. Every input is fixed at scrape time, so a watchdog
# re-run ranks the same way.
_CODE_VIOLATION_RANK_ORDER = (
    f"CASE WHEN {settled_sql('enrichment_data')} THEN 1 ELSE 0 END,"
    " date_recorded_parsed DESC NULLS LAST, id"
)

_RANK_ORDER_BY_RECORD_TYPE = {
    "tax_delinquent": _TAX_RANK_ORDER,
    "code_violation": _CODE_VIOLATION_RANK_ORDER,
}


def cap_rank_order_sql(record_type: str | None) -> str:
    """ORDER BY body the plan cap ranks a job's deliverable leads by."""
    return _RANK_ORDER_BY_RECORD_TYPE.get(record_type or "", _DEFAULT_RANK_ORDER)


def mark_over_quota_rows(db, *, job_id: str, user_id: str, remaining: int,
                         record_type: str | None) -> list[str]:
    """Mark every deliverable lead ranked past ``remaining`` over quota; return their ids.

    Ranks the job's non-duplicate rows that are deliverable ON ADDRESS
    (address_actionable_sql, deliberately not the full rule: a row this job already
    marked must still be ranked, or a re-run would renumber the survivors and mark
    a second batch). Rows with no address are never ranked, never marked and never
    billed. The caller clears this job's previous marks first and commits.
    """
    statement = (  # noqa: S608 -- splices only the module ORDER BY constants and address_actionable_sql; every value is bound
        "WITH ranked AS ("
        "  SELECT id, row_number() OVER (ORDER BY {order}) AS rn"
        "  FROM results"
        "  WHERE job_id = :jid AND user_id = CAST(:uid AS uuid)"
        "    AND is_duplicate = false"
        "    AND {addr_rule}"
        ") "
        "UPDATE results r SET enrichment_data ="
        "  (CASE WHEN jsonb_typeof(COALESCE(r.enrichment_data, '{{}}')::jsonb) = 'object'"
        "        THEN COALESCE(r.enrichment_data, '{{}}')::jsonb ELSE '{{}}'::jsonb END"
        "   || jsonb_build_object(:key, :reason))::json "
        "FROM ranked WHERE r.id = ranked.id AND ranked.rn > :remaining "
        "RETURNING r.id"
    ).format(order=cap_rank_order_sql(record_type), addr_rule=address_actionable_sql("results"))
    return [
        str(row[0]) for row in db.execute(
            sa_text(statement),
            {"jid": job_id, "uid": str(user_id), "key": DELIVERY_EXCLUDED_KEY,
             "reason": OVER_QUOTA, "remaining": remaining},
        ).fetchall()
    ]
