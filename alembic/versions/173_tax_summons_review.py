"""Operator review store for a county Treasurer's tax-foreclosure Notice and Summons.

Douglas WA publishes one scanned summons a year (~16 parcels) and re-uploads it as
parcels redeem, stamping those rows REDEEMED with a strike line that OCR cannot read.
So an operator types each version's rows and publishes it; only a published version's
eligible rows can ever become leads (Phase C, connector added later, inactive).

  tax_summons_versions  one row per distinct PDF digest, in the order seen (seen_order);
                        pending|published|superseded, at most one published per county.
                        The operator's manifest (rows per exhibit page, case number)
                        and the current load revision live here.
  tax_summons_rows      every load's rows, kept under that load's revision: a reload adds
                        a revision, never edits or deletes, and publication names the
                        revision it approves. Names and addresses: shared, system-only,
                        erasable via privacy_suppression_id (Phase C).
  tax_summons_events    append-only audit: codes, revisions and parcel numbers, never names.

Triggers seal what review decided (SEAL_SQL): a published or superseded version and its
rows cannot be changed, by any role, except a row's erasure. RLS on all three.
bridgeleads_system gets a FOR ALL policy on versions and only SELECT +
INSERT policies on rows and events, so those two stay append-only even under the
provisioner's blanket UPDATE grant (scripts/provision_rls_roles.sql). Grants and
policies sit inside a role-existence guard, exactly as 151 pacs_tax_status. No data.

Revision ID: 173
Revises: 172
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "173"
down_revision = "172"
branch_labels = None
depends_on = None

_ELIGIBILITY = "('eligible', 'redeemed', 'unreadable', 'excluded')"
_EXCLUDE = "('duplicate', 'not_a_parcel', 'privacy_request', 'entered_in_error')"
_REINSTATE = "('redemption_reversed', 'entered_in_error')"

# Per table: the grant, and the commands the system role's policies allow. Rows and
# events have no UPDATE/DELETE policy, so RLS refuses both whatever the grants say.
# Tests apply exactly this SQL (CI clusters have no provisioned roles).
POLICIES = {
    "tax_summons_versions": ("SELECT, INSERT, UPDATE", ("ALL",)),
    "tax_summons_rows": ("SELECT, INSERT", ("SELECT", "INSERT")),
    "tax_summons_events": ("SELECT, INSERT", ("SELECT", "INSERT")),
}


ROW_AMOUNT_CHECK = "amount_due IS NULL OR (amount_due > 0 AND amount_due <= 10000000)"
# Whole-number years 1990-2100; CASE keeps the array functions off non-arrays.
ROW_YEARS_CHECK = (
    "CASE WHEN tax_years IS NULL THEN true WHEN json_typeof(tax_years) <> 'array' THEN false "
    "ELSE json_array_length(tax_years) > 0 AND NOT jsonb_path_exists(tax_years::jsonb, "
    "'$[*] ? (@.type() != \"number\" || @ < 1990 || @ > 2100 || @ != @.floor())') END")
EVENT_REASON_CHECK = (
    f"reason IS NULL OR (action = 'entered' AND reason IN {_EXCLUDE}) "
    f"OR (action = 'reinstated' AND reason IN {_REINSTATE}) "
    f"OR (action = 'manifest' AND reason = 'new_case')")
EVENT_SHAPE_CHECK = (
    "(action <> 'reinstated' OR (parcel_id IS NOT NULL AND eligibility IS NOT DISTINCT FROM 'eligible' "
    "AND reason IS NOT NULL)) "
    "AND (action <> 'entered' OR eligibility IS NOT NULL)")

# The database seals what review has decided, whoever writes (the system role included):
#  - a row goes only into the CURRENT revision of a PENDING version; a published or
#    superseded version's rows never gain a row. A row's only change is erasure: a
#    personal field cleared to NULL, with privacy_suppression_id set (Phase C).
#  - a version is created pending and unreviewed; its identity never changes; its revision only grows; it is published only
#    from pending, at its current revision; once published its case, manifest, revision
#    and publication fields are frozen, and it can only become superseded. A
#    superseded version changes only last_checked_at.
SEAL_SQL = r"""
CREATE FUNCTION tax_summons_rows_seal() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_status text;
    v_revision integer;
    v_txid bigint;
    v_county text;
    v_state text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- FOR SHARE: a publish (FOR UPDATE) waits for this insert, or this insert waits
        -- for the publish and then sees the version sealed.
        SELECT status, revision, revision_txid, county, state
          INTO v_status, v_revision, v_txid, v_county, v_state
          FROM tax_summons_versions WHERE id = NEW.version_id FOR SHARE;
        -- Douglas parcels are 11 digits (tax_summons._PARCEL); a short one would never
        -- match the redemption history.
        IF v_county = 'douglas' AND v_state = 'WA' AND NEW.parcel_id !~ '^[0-9]{11}$' THEN
            RAISE EXCEPTION 'tax_summons_rows: a Douglas parcel is 11 digits' USING ERRCODE = 'check_violation';
        END IF;
        -- Only the transaction that opened the revision fills it: once committed, a
        -- revision's row set is what show displayed and what publish --revision approves.
        IF v_status IS DISTINCT FROM 'pending' OR NEW.revision IS DISTINCT FROM v_revision
           OR v_txid IS DISTINCT FROM txid_current() THEN
            RAISE EXCEPTION 'tax_summons_rows: revision % of this version is sealed', NEW.revision
                  USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.version_id, NEW.revision, NEW.page, NEW.row_no, NEW.parcel_id, NEW.amount_due,
        NEW.tax_years::text, NEW.eligibility, NEW.exclusion_reason, NEW.reinstate_reason,
        NEW.entered_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.version_id, OLD.revision, OLD.page, OLD.row_no, OLD.parcel_id, OLD.amount_due,
        OLD.tax_years::text, OLD.eligibility, OLD.exclusion_reason, OLD.reinstate_reason,
        OLD.entered_by, OLD.created_at)
       OR (NEW.vested_owner IS DISTINCT FROM OLD.vested_owner AND NEW.vested_owner IS NOT NULL)
       OR (NEW.taxpayer IS DISTINCT FROM OLD.taxpayer AND NEW.taxpayer IS NOT NULL)
       OR (NEW.property_address IS DISTINCT FROM OLD.property_address AND NEW.property_address IS NOT NULL)
       OR (NEW.mailing_address IS DISTINCT FROM OLD.mailing_address AND NEW.mailing_address IS NOT NULL)
       OR (OLD.privacy_suppression_id IS NOT NULL
           AND NEW.privacy_suppression_id IS DISTINCT FROM OLD.privacy_suppression_id)
       OR (NEW.privacy_suppression_id IS NULL AND (
           NEW.vested_owner, NEW.taxpayer, NEW.property_address, NEW.mailing_address)
           IS DISTINCT FROM (OLD.vested_owner, OLD.taxpayer, OLD.property_address, OLD.mailing_address))
    THEN
        RAISE EXCEPTION 'tax_summons_rows: a reviewed row only changes by erasure'
              USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER tax_summons_rows_seal BEFORE INSERT OR UPDATE ON tax_summons_rows
    FOR EACH ROW EXECUTE FUNCTION tax_summons_rows_seal();

CREATE FUNCTION tax_summons_versions_seal() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_established text;
    v_entries integer;
    v_pages integer;
    v_rows integer;
    v_valid boolean;
BEGIN
    -- The checks below read other versions; a snapshot older than the county lock would
    -- miss a copy committed while this transaction waited for it.
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'tax_summons_versions: write under READ COMMITTED'
              USING ERRCODE = 'feature_not_supported';
    END IF;
    -- The review module's per-county lock (tax_summons._lock_county): a new copy and a
    -- publication never interleave, whoever writes them.
    PERFORM pg_advisory_xact_lock(7173, hashtext(NEW.county || '/' || NEW.state));
    IF TG_OP = 'INSERT' THEN
        -- The order copies were seen is the database's to assign, never the writer's.
        SELECT coalesce(max(o.seen_order), 0) + 1 INTO NEW.seen_order FROM tax_summons_versions o
         WHERE o.county = NEW.county AND o.state = NEW.state;
        IF NEW.status <> 'pending' OR NEW.revision <> 0 OR NEW.revision_txid IS NOT NULL
           OR NEW.case_number IS NOT NULL
           OR NEW.manifest IS NOT NULL OR NEW.published_at IS NOT NULL OR NEW.published_by IS NOT NULL
           OR NEW.published_revision IS NOT NULL THEN
            RAISE EXCEPTION 'tax_summons_versions: a new copy starts pending and unreviewed'
                  USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.county, NEW.state, NEW.sha256, NEW.seen_order, NEW.source_url, NEW.first_seen_at)
       IS DISTINCT FROM
       (OLD.id, OLD.county, OLD.state, OLD.sha256, OLD.seen_order, OLD.source_url, OLD.first_seen_at)
       OR NEW.revision < OLD.revision
       OR (OLD.status, NEW.status) NOT IN (
           ('pending', 'pending'), ('pending', 'published'), ('pending', 'superseded'),
           ('published', 'published'), ('published', 'superseded'), ('superseded', 'superseded'))
       -- publication approves the revision, case and manifest as they stand
       OR (OLD.status = 'pending' AND NEW.status = 'published'
           AND (NEW.published_revision IS DISTINCT FROM OLD.revision OR NEW.revision <> OLD.revision
                OR NEW.case_number IS DISTINCT FROM OLD.case_number
                OR NEW.manifest::text IS DISTINCT FROM OLD.manifest::text))
       -- a pending case or manifest edit starts a new revision (loaded rows stop counting)
       OR (OLD.status = 'pending'
           AND (NEW.case_number IS DISTINCT FROM OLD.case_number
                OR NEW.manifest::text IS DISTINCT FROM OLD.manifest::text)
           AND NEW.revision <= OLD.revision)
       OR (OLD.status <> 'pending'
           AND (NEW.case_number, NEW.manifest::text, NEW.revision, NEW.published_at, NEW.published_by,
                NEW.published_revision)
               IS DISTINCT FROM
               (OLD.case_number, OLD.manifest::text, OLD.revision, OLD.published_at, OLD.published_by,
                OLD.published_revision))
       OR (OLD.status = 'pending' AND NEW.status <> 'published'
           AND (NEW.published_at, NEW.published_by, NEW.published_revision)
               IS DISTINCT FROM (OLD.published_at, OLD.published_by, OLD.published_revision))
    THEN
        RAISE EXCEPTION 'tax_summons_versions: a reviewed version is sealed'
              USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.revision > OLD.revision THEN
        NEW.revision_txid := txid_current();
    ELSIF NEW.revision_txid IS DISTINCT FROM OLD.revision_txid THEN
        RAISE EXCEPTION 'tax_summons_versions: a reviewed version is sealed' USING ERRCODE = 'check_violation';
    END IF;
    -- A manifest is {"<page>": <rows>}: pages 1-9999 written plainly, each once; 1-50 rows
    -- a page, at most 500 in all (as tax_summons.set_manifest checks).
    IF NEW.manifest IS NOT NULL AND NEW.manifest::text IS DISTINCT FROM OLD.manifest::text THEN
        IF json_typeof(NEW.manifest) <> 'object' THEN
            RAISE EXCEPTION 'tax_summons_versions: a manifest maps pages to row counts'
                  USING ERRCODE = 'check_violation';
        END IF;
        SELECT count(*), count(DISTINCT e.key),
               coalesce(sum(CASE WHEN json_typeof(e.value) = 'number' AND e.value::text ~ '^[0-9]{1,2}$'
                                 THEN e.value::text::int END), 0),
               coalesce(bool_and(e.key ~ '^[1-9][0-9]{0,3}$' AND json_typeof(e.value) = 'number'
                                 AND e.value::text ~ '^[0-9]{1,2}$'
                                 AND e.value::text::int BETWEEN 1 AND 50), false)
          INTO v_entries, v_pages, v_rows, v_valid
          FROM json_each(NEW.manifest) e;
        IF NOT v_valid OR v_entries = 0 OR v_entries <> v_pages OR v_rows > 500 THEN
            RAISE EXCEPTION 'tax_summons_versions: a manifest maps pages to row counts'
                  USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    -- Only the newest copy can be published, so only it takes a case or manifest.
    IF (NEW.case_number IS DISTINCT FROM OLD.case_number OR NEW.manifest::text IS DISTINCT FROM OLD.manifest::text)
       AND EXISTS (SELECT 1 FROM tax_summons_versions o WHERE o.county = NEW.county AND o.state = NEW.state
                      AND o.seen_order > NEW.seen_order) THEN
        RAISE EXCEPTION 'tax_summons_versions: a newer copy has been seen' USING ERRCODE = 'check_violation';
    END IF;
    -- A re-uploaded summons keeps the case of the county's latest published copy; only a
    -- later year's case may start a new redemption history.
    IF NEW.case_number IS DISTINCT FROM OLD.case_number AND NEW.case_number IS NOT NULL THEN
        -- Established = the case of the latest PUBLISHED copy: a pending copy's case
        -- (possibly a mistake) never becomes the rule for the next one.
        SELECT o.case_number INTO v_established FROM tax_summons_versions o
         WHERE o.county = NEW.county AND o.state = NEW.state AND o.id <> NEW.id
           AND o.published_at IS NOT NULL
         ORDER BY o.seen_order DESC LIMIT 1;
        IF v_established IS NOT NULL AND NEW.case_number <> v_established
           AND left(NEW.case_number, 2)::int <= left(v_established, 2)::int THEN
            RAISE EXCEPTION 'tax_summons_versions: the summons is case %', v_established
                  USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    -- Publication, whoever writes it, must pass what review checks (tax_summons.publish):
    -- the revision's rows are exactly the manifest's, no newer copy has been seen, and an
    -- eligible row carries a reinstatement reason exactly when its parcel's latest
    -- published decision in the case (by the order copies were seen) is 'redeemed'.
    IF OLD.status = 'pending' AND NEW.status = 'published' THEN
        NEW.published_at := now();  -- the clock is the database's, never the writer's
        IF NEW.manifest IS NULL OR NOT EXISTS (
               SELECT 1 FROM tax_summons_rows r WHERE r.version_id = NEW.id AND r.revision = NEW.revision)
           OR EXISTS (
               (SELECT m.key::int, gs.n
                  FROM json_each_text(NEW.manifest) m, generate_series(1, m.value::int) gs(n)
                EXCEPT
                SELECT r.page, r.row_no FROM tax_summons_rows r
                 WHERE r.version_id = NEW.id AND r.revision = NEW.revision)
               UNION ALL
               (SELECT r.page, r.row_no FROM tax_summons_rows r
                 WHERE r.version_id = NEW.id AND r.revision = NEW.revision
                EXCEPT
                SELECT m.key::int, gs.n
                  FROM json_each_text(NEW.manifest) m, generate_series(1, m.value::int) gs(n)))
        THEN
            RAISE EXCEPTION 'tax_summons_versions: the published rows must be exactly the manifest''s'
                  USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM tax_summons_versions o
                    WHERE o.county = NEW.county AND o.state = NEW.state AND o.seen_order > NEW.seen_order) THEN
            RAISE EXCEPTION 'tax_summons_versions: a newer copy has been seen'
                  USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM tax_summons_rows r
             WHERE r.version_id = NEW.id AND r.revision = NEW.revision AND r.eligibility = 'eligible'
               AND (r.reinstate_reason IS NOT NULL) IS DISTINCT FROM (
                   (SELECT p.eligibility FROM tax_summons_rows p
                      JOIN tax_summons_versions pv
                        ON pv.id = p.version_id AND pv.published_revision = p.revision
                     WHERE pv.county = NEW.county AND pv.state = NEW.state
                       AND pv.case_number = NEW.case_number AND pv.published_at IS NOT NULL
                       AND pv.id <> NEW.id AND p.parcel_id = r.parcel_id
                       AND p.eligibility IN ('redeemed', 'eligible')
                     ORDER BY pv.seen_order DESC LIMIT 1) IS NOT DISTINCT FROM 'redeemed'))
        THEN
            RAISE EXCEPTION 'tax_summons_versions: an eligible row''s reinstatement does not match the '
                  'parcel''s last published decision' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER tax_summons_versions_seal BEFORE INSERT OR UPDATE ON tax_summons_versions
    FOR EACH ROW EXECUTE FUNCTION tax_summons_versions_seal();

-- Nothing reviewed is ever deleted, by any role: a removed row would change what an
-- operator approved, and a removed redemption would drop out of the case's history.
-- (Erasure clears fields; it never deletes.)
CREATE FUNCTION tax_summons_no_delete() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '%: review records are never deleted', TG_TABLE_NAME USING ERRCODE = 'check_violation';
END
$fn$;

CREATE TRIGGER tax_summons_versions_no_delete BEFORE DELETE ON tax_summons_versions
    FOR EACH ROW EXECUTE FUNCTION tax_summons_no_delete();
CREATE TRIGGER tax_summons_versions_no_truncate BEFORE TRUNCATE ON tax_summons_versions
    FOR EACH STATEMENT EXECUTE FUNCTION tax_summons_no_delete();
CREATE TRIGGER tax_summons_rows_no_delete BEFORE DELETE ON tax_summons_rows
    FOR EACH ROW EXECUTE FUNCTION tax_summons_no_delete();
CREATE TRIGGER tax_summons_rows_no_truncate BEFORE TRUNCATE ON tax_summons_rows
    FOR EACH STATEMENT EXECUTE FUNCTION tax_summons_no_delete();
CREATE TRIGGER tax_summons_events_no_delete BEFORE DELETE ON tax_summons_events
    FOR EACH ROW EXECUTE FUNCTION tax_summons_no_delete();
CREATE TRIGGER tax_summons_events_no_truncate BEFORE TRUNCATE ON tax_summons_events
    FOR EACH STATEMENT EXECUTE FUNCTION tax_summons_no_delete();

-- The audit is append-only for every role, not just the system role's policies.
CREATE FUNCTION tax_summons_events_no_update() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'tax_summons_events: the audit is append-only' USING ERRCODE = 'check_violation';
END
$fn$;

CREATE TRIGGER tax_summons_events_no_update BEFORE UPDATE ON tax_summons_events
    FOR EACH ROW EXECUTE FUNCTION tax_summons_events_no_update();

-- A copy is superseded only by a newer published one, checked at commit so publish()
-- can supersede the old copies and publish the new one in a single transaction.
CREATE FUNCTION tax_summons_versions_superseded() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tax_summons_versions o
                    WHERE o.county = NEW.county AND o.state = NEW.state AND o.seen_order > NEW.seen_order
                      AND o.status = 'published') THEN
        RAISE EXCEPTION 'tax_summons_versions: a copy is superseded only by a newer published one'
              USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$fn$;

CREATE CONSTRAINT TRIGGER tax_summons_versions_superseded AFTER UPDATE ON tax_summons_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status = 'superseded' AND OLD.status <> 'superseded')
    EXECUTE FUNCTION tax_summons_versions_superseded();
"""


def system_only_sql(table: str) -> str:
    grants, commands = POLICIES[table]
    policies = []
    for cmd in commands:
        name = f"{table}_system_{cmd.lower()}"
        clause = "WITH CHECK (true)" if cmd == "INSERT" else (
            "USING (true)" if cmd == "SELECT" else "USING (true) WITH CHECK (true)")
        policies.append(f"DROP POLICY IF EXISTS {name} ON {table}; "
                        f"CREATE POLICY {name} ON {table} FOR {cmd} TO bridgeleads_system {clause};")
    revoke = "" if "ALL" in commands else f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM bridgeleads_system;"
    return f"""
        DO $tsr_rls$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT {grants} ON {table} TO bridgeleads_system;
                {revoke}
                {' '.join(policies)}
            END IF;
        END
        $tsr_rls$;
        """


def upgrade() -> None:
    op.create_table(
        "tax_summons_versions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("county", sa.String(64), nullable=False),
        sa.Column("state", sa.String(4), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("seen_order", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("case_number", sa.String(32), nullable=True),
        sa.Column("manifest", sa.JSON, nullable=True),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        # The transaction that opened the current revision (set by the seal trigger):
        # only it may add that revision's rows.
        sa.Column("revision_txid", sa.BigInteger, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_revision", sa.Integer, nullable=True),
        sa.UniqueConstraint("county", "state", "sha256", name="uq_tax_summons_versions_digest"),
        sa.UniqueConstraint("county", "state", "seen_order", name="uq_tax_summons_versions_seen"),
        sa.CheckConstraint("status IN ('pending', 'published', 'superseded')",
                           name="ck_tax_summons_versions_status"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_tax_summons_versions_sha256"),
        sa.CheckConstraint("county ~ '^[a-z]+( [a-z]+)*$' AND state ~ '^[A-Z]{2}$'",
                           name="ck_tax_summons_versions_place"),
        sa.CheckConstraint("seen_order > 0 AND revision >= 0", name="ck_tax_summons_versions_counters"),
        sa.CheckConstraint("case_number IS NULL OR case_number ~ '^[0-9]{2}-[0-9]-[0-9]{5}-[0-9]{2}$'",
                           name="ck_tax_summons_versions_case"),
        # Published (now or once) means: who, when, which revision, under which case.
        sa.CheckConstraint(
            "(published_at IS NULL) = (published_by IS NULL) "
            "AND (published_at IS NULL) = (published_revision IS NULL) "
            "AND (published_at IS NULL OR (case_number IS NOT NULL AND published_revision > 0)) "
            "AND (status <> 'published' OR published_at IS NOT NULL)",
            name="ck_tax_summons_versions_published"),
    )
    op.create_index("uq_tax_summons_versions_one_published", "tax_summons_versions",
                    ["county", "state"], unique=True,
                    postgresql_where=sa.text("status = 'published'"))

    op.create_table(
        "tax_summons_rows",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("version_id", UUID(as_uuid=False),
                  sa.ForeignKey("tax_summons_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("page", sa.Integer, nullable=False),
        sa.Column("row_no", sa.Integer, nullable=False),
        sa.Column("parcel_id", sa.String(32), nullable=True),
        sa.Column("vested_owner", sa.String(512), nullable=True),
        sa.Column("taxpayer", sa.String(512), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        sa.Column("mailing_address", sa.String(512), nullable=True),
        sa.Column("amount_due", sa.Numeric(12, 2), nullable=True),
        sa.Column("tax_years", sa.JSON, nullable=True),
        sa.Column("eligibility", sa.String(16), nullable=False),
        sa.Column("exclusion_reason", sa.String(32), nullable=True),
        sa.Column("reinstate_reason", sa.String(32), nullable=True),
        sa.Column("entered_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("privacy_suppression_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_suppressions.id", ondelete="RESTRICT"), nullable=True),
        sa.UniqueConstraint("version_id", "revision", "page", "row_no", name="uq_tax_summons_rows_locator"),
        sa.CheckConstraint(f"eligibility IN {_ELIGIBILITY}", name="ck_tax_summons_rows_eligibility"),
        sa.CheckConstraint("revision > 0 AND page > 0 AND row_no > 0", name="ck_tax_summons_rows_locator"),
        sa.CheckConstraint("parcel_id IS NULL OR parcel_id ~ '^[0-9]{4,32}$'", name="ck_tax_summons_rows_parcel"),
        # numeric 'NaN' sorts above every number, so the upper bound also refuses it.
        sa.CheckConstraint(ROW_AMOUNT_CHECK, name="ck_tax_summons_rows_amount"),
        sa.CheckConstraint(ROW_YEARS_CHECK, name="ck_tax_summons_rows_tax_years"),
        # An eligible row is a whole lead: parcel, a positive amount, its tax years. (The
        # owner is required by the review code, not here: erasure must be able to clear
        # names on any row.)
        sa.CheckConstraint(
            "eligibility <> 'eligible' OR (parcel_id IS NOT NULL AND amount_due IS NOT NULL "
            "AND tax_years IS NOT NULL)",
            name="ck_tax_summons_rows_eligible_complete"),
        # A redemption binds later versions by parcel, so it must name one.
        sa.CheckConstraint("eligibility <> 'redeemed' OR parcel_id IS NOT NULL",
                           name="ck_tax_summons_rows_redeemed_parcel"),
        sa.CheckConstraint(
            f"(eligibility = 'excluded') = (exclusion_reason IS NOT NULL) "
            f"AND (exclusion_reason IS NULL OR exclusion_reason IN {_EXCLUDE})",
            name="ck_tax_summons_rows_exclusion_reason"),
        sa.CheckConstraint(
            f"reinstate_reason IS NULL OR (eligibility = 'eligible' AND reinstate_reason IN {_REINSTATE})",
            name="ck_tax_summons_rows_reinstate_reason"),
    )
    op.create_index("uq_tax_summons_rows_parcel", "tax_summons_rows", ["version_id", "revision", "parcel_id"],
                    unique=True, postgresql_where=sa.text("parcel_id IS NOT NULL"))

    op.create_table(
        "tax_summons_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("version_id", UUID(as_uuid=False),
                  sa.ForeignKey("tax_summons_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("parcel_id", sa.String(32), nullable=True),
        sa.Column("eligibility", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('manifest', 'loaded', 'entered', 'reinstated', 'published')",
                           name="ck_tax_summons_events_action"),
        sa.CheckConstraint(f"eligibility IS NULL OR eligibility IN {_ELIGIBILITY}",
                           name="ck_tax_summons_events_eligibility"),
        sa.CheckConstraint(EVENT_REASON_CHECK, name="ck_tax_summons_events_reason"),
        sa.CheckConstraint(EVENT_SHAPE_CHECK, name="ck_tax_summons_events_shape"),
        sa.CheckConstraint("parcel_id IS NULL OR parcel_id ~ '^[0-9]{4,32}$'",
                           name="ck_tax_summons_events_parcel"),
        sa.CheckConstraint("revision > 0", name="ck_tax_summons_events_revision"),
    )
    op.create_index("ix_tax_summons_events_version", "tax_summons_events", ["version_id", "at"])

    for table in POLICIES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(system_only_sql(table))
    op.execute(SEAL_SQL)


def downgrade() -> None:
    for table in ("tax_summons_events", "tax_summons_rows", "tax_summons_versions"):
        op.drop_table(table)  # drops its indexes, policies and triggers with it
    op.execute("DROP FUNCTION IF EXISTS tax_summons_versions_seal()")
    op.execute("DROP FUNCTION IF EXISTS tax_summons_versions_superseded()")
    op.execute("DROP FUNCTION IF EXISTS tax_summons_no_delete()")
    op.execute("DROP FUNCTION IF EXISTS tax_summons_events_no_update()")
    op.execute("DROP FUNCTION IF EXISTS tax_summons_rows_seal()")
