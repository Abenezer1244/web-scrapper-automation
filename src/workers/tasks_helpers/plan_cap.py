"""The plan quota cap, applied to the ACTIONABLE set after enrichment.

The cap cannot be applied to the raw scrape: a row's actionability is unknowable
until inline enrichment has run, because the counties whose addresses arrive
there (King probate, the generic GIS sweep) look addressless before it. Slicing
the raw list to the quota therefore saved a possibly-quarantined prefix, billed
close to nothing, and silently discarded real leads the user still had quota for
— measured at roughly half the rows on king/tax_delinquent.

So every scraped row is persisted and enriched, and the rows past the quota are
marked here. `lead_actionability` carries that marker in all three of its
spellings, so display, export, counting and billing hide the same set and cannot
disagree.

Extracted from run_scrape_job so it can be tested directly: the three defects a
review found in the original inline version (stale ORM identities, an overbroad
claim release, and a marker silently swallowed by non-object enrichment_data)
were all invisible to a green suite precisely because nothing could call this
code without running a whole scrape.
"""
from sqlalchemy import text as sa_text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    address_actionable_sql,
)

# `enrichment_data` is the generic JSON type, so every jsonb operator needs an
# explicit cast. The CASE guard is not defensive noise: `jsonb || jsonb_build_object`
# on an ARRAY or SCALAR does not produce a top-level object, so the key would be
# unreadable by `->>` afterwards and the row would be reported as capped while
# staying actionable, exportable and billable. Same exposure for `- :key`.
_AS_OBJECT = (
    "CASE WHEN jsonb_typeof(COALESCE({col}, '{{}}')::jsonb) = 'object' "
    "THEN COALESCE({col}, '{{}}')::jsonb ELSE '{{}}'::jsonb END"
)

_CLEAR_SQL = (
    "UPDATE results SET enrichment_data = (" + _AS_OBJECT.format(col="enrichment_data") +
    " - :key)::json "
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
    "AND enrichment_data->>:key = :reason"
)

_MARK_SQL_TMPL = (
    "WITH ranked AS ("
    "  SELECT id, row_number() OVER ("
    "    ORDER BY party_name, date_recorded, id"
    "  ) AS rn"
    "  FROM results"
    "  WHERE job_id = :jid AND user_id = CAST(:uid AS uuid)"
    "    AND is_duplicate = false"
    "    AND __ADDR_RULE__"
    ") "
    "UPDATE results r SET enrichment_data = ("
    + _AS_OBJECT.format(col="r.enrichment_data") +
    " || jsonb_build_object(:key, :reason))::json "
    "FROM ranked "
    "WHERE r.id = ranked.id AND ranked.rn > :remaining "
    "RETURNING r.id"
)

# Lock this job's queued skip-trace rows BEFORE marking anything.
#
# The dispatcher's claim query reads `results` through an un-locked EXISTS, so
# under READ COMMITTED it can still see the PRE-cap version of a row while this
# transaction is uncommitted, claim the pending row and POST it — money gone,
# for a lead that will never be delivered. Taking the lock first means the
# dispatcher's own FOR UPDATE SKIP LOCKED skips these rows for the duration,
# closing the window instead of narrowing it (Codex, 2026-09-03).
#
# Scoped to this job and to 'queued' only: a 'submitting' row is already POSTed
# and must never be touched.
_LOCK_PENDING_SQL = (
    "SELECT id FROM pending_skip_trace_rows "
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) AND status = 'queued' "
    "FOR UPDATE SKIP LOCKED"
)

# A capped row is never delivered, so paying Tracerfy for it is pure waste — and
# `_enqueue_skip_trace_rows` runs INSIDE inline enrichment, i.e. BEFORE this cap
# exists, so by the time we mark a row its skip trace may already be queued.
#
# Only 'queued' rows are withdrawn. A 'submitting' row has already been POSTed
# and may have been charged; the dispatcher's standing rule is that such a row is
# NEVER auto-resolved, because releasing it is how you pay twice. Ops reconciles
# those against Tracerfy's queue list.
_CANCEL_PENDING_SQL = (
    "DELETE FROM pending_skip_trace_rows "
    "WHERE status = 'queued' "
    "  AND user_id = CAST(:uid AS uuid) "
    "  AND result_id = ANY(CAST(:ids AS uuid[])) "
    "RETURNING result_id"
)

# Withdrawing the work row without resetting the lead's own status stranded it:
# `results.skip_trace_status` stayed 'queued' with nothing left to process it, so
# if the cap later cleared (upgrade, new month, re-run) the lead came back visible
# and permanently "Processing…". Put it back to not_attempted so the next run can
# legitimately enqueue it again (Codex, 2026-09-03).
_RESET_STATUS_SQL = (
    "UPDATE results SET skip_trace_status = 'not_attempted' "
    "WHERE user_id = CAST(:uid AS uuid) "
    "  AND id = ANY(CAST(:ids AS uuid[])) "
    "  AND skip_trace_status = 'queued'"
)

# Scoped to THIS job's claims. Matching on (user_id, dedup_hash) alone had no
# ownership guard, so a stale is_duplicate, a manual repair or a hash anomaly
# could delete a DIFFERENT job's durable claim for the same user — letting an
# already-delivered lead be delivered and billed a second time.
_RELEASE_SQL = (
    "DELETE FROM delivered_records dr USING results r "
    "WHERE dr.user_id = CAST(:uid AS uuid) "
    "  AND dr.first_job_id = :jid "
    "  AND dr.dedup_hash = r.dedup_hash "
    "  AND r.id = ANY(CAST(:ids AS uuid[])) "
    "  AND r.user_id = CAST(:uid AS uuid) "
    "  AND r.dedup_hash IS NOT NULL"
)


def apply_plan_cap(db, job_id: str, user_id: str, remaining: int) -> list[str]:
    """Mark this job's actionable rows past `remaining` as over-quota.

    Returns the ids that were excluded. Commits on success; the caller is
    responsible for rolling back and failing the job if this raises, because a
    finite-quota user whose cap did not apply must never be billed or delivered.

    The caller MUST re-read any `Result` objects it already holds afterwards
    (`populate_existing=True`): the sessions are built with
    `expire_on_commit=False`, so instances loaded before this call keep their
    stale `enrichment_data` and would not see the marker.
    """
    params = {
        "jid": job_id,
        "uid": str(user_id),
        "key": DELIVERY_EXCLUDED_KEY,
        "reason": OVER_QUOTA,
    }

    # Take the skip-trace lock before any marking (see _LOCK_PENDING_SQL).
    db.execute(sa_text(_LOCK_PENDING_SQL), {"jid": job_id, "uid": str(user_id)})

    # Clear this job's previous marks FIRST. On a watchdog re-run the ranking has
    # to start from the full actionable set; ranking over the already-capped set
    # would renumber the survivors and mark a second batch, shrinking what is
    # delivered on every pass.
    db.execute(sa_text(_CLEAR_SQL), params)

    capped_ids = [
        str(row[0])
        for row in db.execute(
            # .replace, not .format: _AS_OBJECT has already resolved its
            # doubled braces to a literal '{}' JSON default, which a second
            # .format() pass would read as a positional placeholder.
            sa_text(_MARK_SQL_TMPL.replace(
                "__ADDR_RULE__", address_actionable_sql("results")
            )),
            {**params, "remaining": remaining},
        ).fetchall()
    ]

    if capped_ids:
        # Release the dedup claims of rows we are NOT delivering, so a later run
        # (or next month's quota) can still deliver them. Keeping the claim would
        # make the lead permanently unreachable rather than merely deferred.
        db.execute(_release_stmt(), {"uid": str(user_id), "jid": job_id, "ids": capped_ids})
        # Withdraw any skip trace queued for them before this cap existed, and
        # put the lead's own status back so it is not stranded as "Processing…".
        withdrawn = [
            str(row[0])
            for row in db.execute(
                sa_text(_CANCEL_PENDING_SQL),
                {"uid": str(user_id), "ids": capped_ids},
            ).fetchall()
        ]
        if withdrawn:
            db.execute(
                sa_text(_RESET_STATUS_SQL),
                {"uid": str(user_id), "ids": withdrawn},
            )

    db.commit()
    return capped_ids


def _release_stmt():
    return sa_text(_RELEASE_SQL)
