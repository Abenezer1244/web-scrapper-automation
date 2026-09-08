"""Record WHY a result was flagged duplicate, at the moment it is flagged.

The results page tells a user "these were duplicates of leads you already
received" and then offers a link to go check. The link was computed by a rule
that has nothing to do with the dedup decision -- "newest DONE job on a sibling
config that still has visible leads" -- so it could, and in production did,
point at a run that happened MONTHS AFTER the run being viewed and that
delivered none of the leads in question. The page asserted a fact and then
handed the reader a link that appeared to refute it. That is how a correct
duplicate classification got reported as a cross-tenant data leak.

The obvious fix -- have the results API join ``delivered_records`` and name the
true source run -- is not available. ``bridgeleads_app`` has ALL privileges
REVOKED on that table and ``provision_rls_roles.sql`` hard-fails if the role
ever holds one, because the ledger is worker-only by design.

And the ledger could not answer the question reliably even from the worker:

  * it is a CLAIM ledger, not a delivery ledger. A row is inserted before the
    job finishes, so a claim alone does not prove the customer received a lead.
  * it is mutable. Claims are released by the plan cap, by upload failure, and
    by the stranded-claim script; a later job can then re-claim the same hash.
    A read-time join therefore returns whoever holds the claim NOW, not who
    held it when the row was classified.
  * 44,865 of 54,571 production claims (82%) already point at a deleted jobs
    row, with ``first_result_id`` NULL. For most history there is nothing left
    to join to.

So provenance is stamped onto the result row at classification time, where it
is immutable and where the app role can already read it (results: SELECT).

  * ``duplicate_source_job_id`` -- the run that held the claim when THIS row
    was classified. Nullable and deliberately without a foreign key, matching
    ``delivered_records.first_job_id``: the source job may later be purged, and
    losing the pointer must never cascade into the lead row.
  * ``duplicate_source_at``     -- when that claim was first made.
  * ``duplicate_reason``        -- 'prior_run' (a genuinely earlier delivery)
    or 'same_run' (the trustee_sale sibling collapse, which also sets
    is_duplicate=true for rows that were NEVER previously delivered and which
    the old copy therefore described incorrectly).

All three are NULL for every pre-existing row. NULL means "we do not know", and
the API reports those separately as unattributed rather than guessing -- the
whole point of this change is to stop the page claiming more than it can show.

Revision ID: 089
Revises: 088
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "089"
down_revision = "088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "results",
        sa.Column("duplicate_source_job_id", UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "results",
        sa.Column("duplicate_source_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "results",
        sa.Column("duplicate_reason", sa.String(length=16), nullable=True),
    )
    # NO INDEX HERE, deliberately (Codex [P1]). Adding three nullable columns
    # with no default is a metadata-only catalog change, so the ACCESS EXCLUSIVE
    # lock is held for microseconds. Building the supporting index in this same
    # transaction would hold that same lock for a full scan of `results` — the
    # largest table in the product — blocking every read and write on it for the
    # duration. Same treatment as uq_results_job_fingerprint: the index is
    # declared on the model so create_all gives the test database one, and
    # production builds it CONCURRENTLY out of band via
    # scripts/create_result_duplicate_source_index.sql. Until that runs, the
    # grouping query falls back to the existing job_id index, which is already
    # how every other per-job read on this table is served.


def downgrade() -> None:
    op.drop_column("results", "duplicate_reason")
    op.drop_column("results", "duplicate_source_at")
    op.drop_column("results", "duplicate_source_job_id")
