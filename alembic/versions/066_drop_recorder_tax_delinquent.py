"""Drop tax_delinquent from recorder connectors (Clark, Skagit, Chelan).

WHY: Verified by live scrape (2026-06-18) that these three connectors cannot
produce a single usable tax_delinquent lead:

  - Clark  (LandmarkWeb): record_type=tax_delinquent searches ONLY the
    "Federal Tax Lien" document type (checkbox value 97). A live scrape over a
    120-day window found 159 federal tax liens and kept 0 — every one dropped
    for no parcel_id. Federal (IRS) tax liens are filed against a PERSON, not a
    parcel, so they carry no parcel id and the scraper (correctly) requires one.
  - Skagit (skagit_recording template): tax_delinquent dropdown is ONLY
    "Federal Tax Lien". 25 found, 25/25 dropped for no parcel_id → 0 leads.
  - Chelan (acclaimweb template): keyword search returned 0 across the sampled
    window; it also mixes federal tax liens with real Certificate-of-Delinquency
    terms, so even when non-empty it is not a trusted dollars-owed + tax-year
    source.

A FEDERAL TAX LIEN is unpaid federal INCOME tax against a person — NOT county
property-tax delinquency. Selling it under the tax_delinquent label is a
mislabel, and structurally these connectors yield zero leads anyway. Only King
and Snohomish publish structured property-tax delinquency (dollars owed + tax
year) and are the sole members of _TRUSTED_TAX_SOURCES
(src/workers/tasks_helpers/dedup.py). They are intentionally untouched here.

This only edits the record_types array; the connectors stay active for their
other record types (Clark/Chelan: probate, pre_foreclosure; Skagit: + divorce).

Idempotent: the WHERE guard skips rows that no longer carry tax_delinquent, so
re-running is a no-op.

MERGE NOTE (alembic single-head invariant): this revision chains off 064, which
is origin/main's head at authoring time. The unrelated 065_notifications lives
on another branch and was also a child of 064. 065 (notifications) merged to main
FIRST (PR #66), so this migration was rebased onto it: down_revision = "065",
giving a single linear head 064 -> 065 -> 066. This is required — `alembic upgrade
head` fails with multiple heads and would crash-loop the Railway boot migration.

Revision ID: 066
Revises: 065
Create Date: 2026-06-18
"""

import sqlalchemy as sa
from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FAIL CLOSED on DISPATCHABLE batch children (Codex P2). Batch fan-out
    # (dispatch_batch_run) selects child configs by batch_id and IGNORES
    # ScraperConfig.active, so the deactivation below cannot stop a batch from
    # re-dispatching a tax_delinquent child. We must only block when a batch can
    # ACTUALLY dispatch again: its parent scraper_batches.status = 'active', OR it
    # has a non-terminal (pending/running) batch_run that could still re-enqueue.
    # Child configs are retained after a batch archives/completes, so counting bare
    # batch_id IS NOT NULL would abort on harmless historical rows AND make the
    # "archive the parent" remediation a no-op (the child keeps its batch_id). The
    # status/run filter fixes both. Verified live: 0 such children for these counties.
    # Whole migration is one txn, so this rolls back cleanly with nothing applied.
    orphan_batch_children = op.get_bind().execute(
        sa.text(
            """
            SELECT count(*)
            FROM scraper_configs sc
            JOIN scraper_batches sb ON sb.id = sc.batch_id
            WHERE sc.record_type = 'tax_delinquent'
              AND lower(sc.county) IN ('clark', 'skagit', 'chelan')
              AND lower(sc.state) = 'wa'
              AND (
                    sb.status = 'active'
                 OR EXISTS (
                        SELECT 1 FROM batch_runs br
                        WHERE br.batch_id = sb.id
                          AND br.status IN ('pending', 'running')
                    )
              )
            """
        )
    ).scalar()
    if orphan_batch_children:
        raise RuntimeError(
            f"{orphan_batch_children} tax_delinquent child config(s) under a dispatchable batch "
            "exist for clark/skagit/chelan — dispatch_batch_run ignores ScraperConfig.active, "
            "so this migration cannot safely stop them. Archive the parent batch(es) "
            "(scraper_batches.status='archived') AND terminalize any pending/running batch_runs, "
            "then re-run."
        )

    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (
            SELECT COALESCE(jsonb_agg(elem ORDER BY ord), '[]'::jsonb)::json
            FROM jsonb_array_elements_text(record_types::jsonb)
                 WITH ORDINALITY AS t(elem, ord)
            WHERE elem <> 'tax_delinquent'
        )
        WHERE lower(county) IN ('clark', 'skagit', 'chelan')
          AND lower(state) = 'wa'
          AND record_types::jsonb ? 'tax_delinquent'
        """
    )

    # Deactivate orphaned user scraper_configs (Codex review). Removing the type
    # from county_connectors makes get_scraper_class() raise UnsupportedCountyError
    # (caught at tasks.py — the job fails gracefully, no crash) for any still-active
    # clark/skagit/chelan tax_delinquent config, turning every SCHEDULED run into a
    # recurring failure. These configs were producing 0 leads anyway (federal tax
    # liens have no parcel id), so deactivating loses nothing real and stops the
    # re-enqueues. Idempotent (active = true guard). Batch children are handled by
    # the fail-closed guard above (this UPDATE only safely covers standalone configs).
    op.execute(
        """
        UPDATE scraper_configs
        SET active = false, updated_at = now()
        WHERE record_type = 'tax_delinquent'
          AND lower(county) IN ('clark', 'skagit', 'chelan')
          AND lower(state) = 'wa'
          AND active = true
        """
    )


def downgrade() -> None:
    # Best-effort restore (Codex P3). upgrade() does not record which rows it
    # changed, so this re-adds tax_delinquent to EVERY clark/skagit/chelan/wa
    # connector that currently lacks it — correct for the actual prod state (one
    # connector per county, all of which had tax_delinquent pre-upgrade), but in a
    # non-standard DB with a recorder connector that never carried tax_delinquent it
    # would over-restore by one record type. Accepted: exact reversal needs per-row
    # state capture, and an extra advertised type on rollback is benign (it would
    # just route to the recorder scraper that yields 0). It is APPENDED, not
    # reinserted at its original index (Skagit had it before "divorce") — order is
    # non-semantic: matching is membership-based and the scheduler uses
    # record_types[0] (always "probate" here).
    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (record_types::jsonb || '["tax_delinquent"]'::jsonb)::json
        WHERE lower(county) IN ('clark', 'skagit', 'chelan')
          AND lower(state) = 'wa'
          AND NOT (record_types::jsonb ? 'tax_delinquent')
        """
    )

    # NOTE: scraper_configs are intentionally NOT reactivated here (Codex P3).
    # upgrade() does not record WHICH configs it disabled, so a blanket reactivate
    # would also flip on configs the user had disabled themselves, against their
    # intent. Leaving them inactive is the safe asymmetry — the connector is
    # restored above, so a user can re-enable any config manually if they want it.
