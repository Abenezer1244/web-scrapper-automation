"""Which of a job's leads the plan quota delivers, and which it marks over quota.

Lives outside tasks.py so the tests run the statement the job runs, not a copy.
"""

import json

from sqlalchemy import text as sa_text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NOT_QUALIFIED,
    OVER_QUOTA,
    address_actionable_sql,
    deliverable_before_cap_sql,
)
from src.api.lead_qualification import (
    HOLD_KEY,
    POLICY_CODE_VIOLATION,
    POLICY_KEY,
    hold_record,
    qualified_sql,
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

    Ranks the job's non-duplicate rows that are deliverable ON ADDRESS and qualified
    (deliverable_before_cap_sql, deliberately not the full rule: a row this job already
    marked over quota must still be ranked, or a re-run would renumber the survivors and
    mark a second batch). Rows with no address, and code violations held by
    mark_not_qualified_rows, are never ranked, never marked over quota and never billed,
    so the quota is spent only on leads that can be delivered. The caller clears this
    job's previous marks first and commits.
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
    ).format(order=cap_rank_order_sql(record_type), addr_rule=deliverable_before_cap_sql("results"))
    return [
        str(row[0]) for row in db.execute(
            sa_text(statement),
            {"jid": job_id, "uid": str(user_id), "key": DELIVERY_EXCLUDED_KEY,
             "reason": OVER_QUOTA, "remaining": remaining},
        ).fetchall()
    ]



_ED_OBJECT = ("(CASE WHEN jsonb_typeof(COALESCE(enrichment_data, '{}')::jsonb) = 'object' "
              "THEN COALESCE(enrichment_data, '{}')::jsonb ELSE '{}'::jsonb END)")
_CLEAR_HOLDS_SQL = (  # splices only module constants; every value is bound
    # The frozen reasons go with the mark, so a re-run that qualifies the row leaves neither.
    f"UPDATE results SET enrichment_data = ({_ED_OBJECT} - :key - '{HOLD_KEY}')::json "  # noqa: S608
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
    "  AND enrichment_data->>:key = :reason"
)
_HOLD_SQL = (  # splices only module constants; every value is bound
    f"UPDATE results SET enrichment_data = ({_ED_OBJECT} || jsonb_build_object(CAST(:key AS text), CAST(:reason AS text)))::json "  # noqa: S608
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
    "  AND (is_duplicate = false OR duplicate_reason = 'same_run') "
    f"  AND NOT {qualified_sql('results')} "
    f"RETURNING id, dedup_hash, is_duplicate, {address_actionable_sql('results')} AS has_address, "
    # The columns qualification_reasons reads, so the reasons are frozen from this row as held.
    "  party_name, parcel_id, property_address, mailing_address, date_recorded, enrichment_data"
)
_FREEZE_REASONS_SQL = (  # splices only module constants; every value is bound
    f"UPDATE results SET enrichment_data = ({_ED_OBJECT} || jsonb_build_object('{HOLD_KEY}', v.hold))::json "  # noqa: S608
    "FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS v(id uuid, hold jsonb) "
    "WHERE results.id = v.id AND results.job_id = :jid AND results.user_id = CAST(:uid AS uuid)"
)
_HOLD_SIBLINGS_SQL = (  # splices only module constants; every value is bound
    f"UPDATE results SET enrichment_data = ({_ED_OBJECT} || jsonb_build_object(CAST(:key AS text), CAST(:reason AS text)))::json "  # noqa: S608
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
    "  AND duplicate_reason = 'same_run' "
    "  AND dedup_hash = ANY(CAST(:hashes AS text[])) "
    f"  AND enrichment_data->>'{POLICY_KEY}' = '{POLICY_CODE_VIOLATION}' "
    "  AND COALESCE(enrichment_data->>:key, '') <> :reason"
)


def mark_not_qualified_rows(db, *, job_id: str, user_id: str) -> int:
    """Hold this job's code violations that are not qualified leads; return how many.

    Runs after enrichment and the claim transfer, before the quota reservation and the
    cap, for EVERY plan (an unlimited account is never capped but must still not receive
    an unqualified lead). A held row carries delivery_excluded_reason = 'not_qualified',
    which lead_actionability treats like over_quota: not listed, exported, counted,
    billed, skip traced or pushed. The decision is frozen with the run: a sweep naming
    the owner later does not unhold it; the next run takes the claim and delivers it.

    Idempotent for a watchdog re-run: this job's previous holds are cleared first, so a
    row the re-run enriched is decided again. A row with no address is held too (Codex
    final pass): a sweep that later finds its address and owner must not put it in this
    finished run either. It still reports in the no_address bucket, which the run
    breakdown checks first, and is not counted in the return value (the run log names
    no-address records on its own line). A held survivor's same-run siblings are held
    with it, because lists and the batch export keep duplicates and would otherwise
    deliver the property through a sibling. Does not commit.
    """
    params = {"jid": job_id, "uid": str(user_id), "key": DELIVERY_EXCLUDED_KEY,
              "reason": NOT_QUALIFIED}
    db.execute(sa_text(_CLEAR_HOLDS_SQL), params)
    held = db.execute(sa_text(_HOLD_SQL), params).fetchall()
    # Freeze WHY with the hold: a sweep may complete the row later, and the Results page
    # must still say why this run held it. One statement for the run; same-run siblings
    # are never listed on their own, so they carry only the mark.
    frozen = [{"id": str(row.id), "hold": hold_record(row)} for row in held if not row.is_duplicate]
    if frozen:
        db.execute(sa_text(_FREEZE_REASONS_SQL), {**params, "payload": json.dumps(frozen)})
    survivor_hashes = [row.dedup_hash for row in held if row.dedup_hash and not row.is_duplicate]
    if survivor_hashes:
        db.execute(sa_text(_HOLD_SIBLINGS_SQL), {**params, "hashes": survivor_hashes})
    return sum(1 for row in held if not row.is_duplicate and row.has_address)
