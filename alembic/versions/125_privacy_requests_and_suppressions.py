"""Privacy requests and suppression rules for people named in lead data (125).

Phase 6, P2 of docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md
(section 8), after a Codex consult (tasks/todo-compliance-audit-2026-11.md). SCHEMA
ONLY: no role may read or write these tables yet, and no rule can become active.

  privacy_requests         the immutable envelope of a request: what kind, how it
                           came in, when, and its statutory deadline. No PII: the
                           requester's email appears only as its keyed blind index.
  privacy_request_payloads the requester's contact and details, Fernet-encrypted
                           (fe1:), in their OWN table so a retention or erasure
                           decision (D5) can delete them without touching the
                           envelope or the audit trail.
  privacy_request_events   the append-only audit trail. Order is `seq`, unique per
                           request: a writer takes max(seq) + 1, and two concurrent
                           writers collide on UNIQUE (request_id, seq) instead of
                           both landing. The request's status is its latest event;
                           there is no status column to drift. No free-text notes.
  privacy_suppressions     suppression rules, append-only: a state change is a new
                           row with the same rule_id. A rule is an anchor HMAC plus
                           a REQUIRED subject HMAC (src/privacy/keys.py), with the
                           key and normalisation versions it was made under; during
                           a rotation one rule has a row per version.

THE ACTIVATION GUARD (design section 8): ck_privacy_suppressions_activation_guard
allows state = 'pending' only. No rule can be active until a later migration (P7,
when every enforcement layer has shipped) replaces that CHECK; the application
guard (src/privacy/activation.py, PRIVACY_ENFORCEMENT_VERSION) is the second lock.

What the database does NOT prove: that an HMAC came from a valid subject, or that
an fe1: value decrypts. Those are the P1 key library's and crypto's guarantees;
here they are only shape checks.

Immutability: a trigger refuses UPDATE on every table (BLD24). DELETE is not
blocked by a trigger, deliberately: retention and erasure (D5) are undecided, and
whoever is given that authority must be able to delete. Today no runtime role holds
DELETE (or anything else). A request whose suppression rule exists cannot be
deleted (FK RESTRICT): deleting the paperwork must never lift a suppression.

Not tenant data (no user_id), so no account-deletion fence. actor_user_id is a
plain UUID, NOT a foreign key to users: the 113 purge must never be blocked by, or
cascade into, this audit trail.

UUID primary keys only: no sequences, so no blanket GRANT ... ON ALL SEQUENCES can
leak a privilege here. RLS is on with NO policies; every privilege is revoked from
PUBLIC, the Supabase API roles and the runtime roles (guarded: CI has no runtime
roles), and scripts/provision_rls_roles.sql and _cutover_step2_grants_policies.py
revoke again after their blanket grants. Writers get column grants (never the
database-stamped timestamps) and policies when they ship (P7/P8).

Revision ID: 125
Revises: 124
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "125"
down_revision = "124"
branch_labels = None
depends_on = None

_TABLES = ("privacy_requests", "privacy_request_payloads", "privacy_request_events",
           "privacy_suppressions")
_HEX64 = "~ '^[0-9a-f]{64}$'"
_CODE = "~ '^[a-z0-9_]+$'"


def _id() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=False), primary_key=True,
                     server_default=sa.text("gen_random_uuid()"))


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(),
                     nullable=False)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))

    op.create_table(
        "privacy_requests",
        _id(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        _now("received_at"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requester_email_hmac", sa.String(64), nullable=True),
        sa.CheckConstraint("kind IN ('opt_out', 'delete', 'access', 'correct')",
                           name="ck_privacy_requests_kind"),
        sa.CheckConstraint("channel IN ('web', 'email', 'postal', 'drop')",
                           name="ck_privacy_requests_channel"),
        sa.CheckConstraint("deadline_at > received_at", name="ck_privacy_requests_deadline"),
        sa.CheckConstraint(f"requester_email_hmac {_HEX64}",
                           name="ck_privacy_requests_email_hmac"),
    )
    op.create_table(
        "privacy_request_payloads",
        sa.Column("request_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_requests.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("contact_encrypted", sa.Text(), nullable=False),
        sa.Column("details_encrypted", sa.Text(), nullable=True),
        sa.CheckConstraint("contact_encrypted LIKE 'fe1:%'",
                           name="ck_privacy_request_payloads_contact"),
        sa.CheckConstraint("details_encrypted LIKE 'fe1:%'",
                           name="ck_privacy_request_payloads_details"),
    )
    op.create_table(
        "privacy_request_events",
        _id(),
        sa.Column("request_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        # Decision-neutral: the levels and codes themselves are owner decisions (D1).
        sa.Column("verification_level", sa.String(32), nullable=True),
        sa.Column("decision_code", sa.String(32), nullable=True),
        # The new deadline an 'extended' event sets.
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=False), nullable=True),
        _now("occurred_at"),
        sa.UniqueConstraint("request_id", "seq", name="uq_privacy_request_events_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_privacy_request_events_seq"),
        sa.CheckConstraint(
            "event_type IN ('received', 'verification_sent', 'verified', 'extended', "
            "'in_review', 'decided', 'closed', 'withdrawn')",
            name="ck_privacy_request_events_type"),
        sa.CheckConstraint(f"verification_level {_CODE}",
                           name="ck_privacy_request_events_verification"),
        sa.CheckConstraint(f"decision_code {_CODE}",
                           name="ck_privacy_request_events_decision_code"),
        # A decision, and only a decision, carries its (owner-defined) code.
        sa.CheckConstraint("(event_type = 'decided') = (decision_code IS NOT NULL)",
                           name="ck_privacy_request_events_decision"),
        sa.CheckConstraint("(event_type = 'extended') = (deadline_at IS NOT NULL)",
                           name="ck_privacy_request_events_deadline"),
        sa.CheckConstraint("actor IN ('system', 'operator')",
                           name="ck_privacy_request_events_actor"),
        sa.CheckConstraint("(actor = 'operator') = (actor_user_id IS NOT NULL)",
                           name="ck_privacy_request_events_actor_user"),
    )
    op.create_table(
        "privacy_suppressions",
        _id(),
        sa.Column("rule_id", UUID(as_uuid=False), nullable=False),
        sa.Column("request_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_requests.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("anchor_kind", sa.String(16), nullable=False),
        sa.Column("anchor_hmac", sa.String(64), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column("subject_hmac", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("key_version", sa.String(8), nullable=False),
        sa.Column("normalization_version", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        _now("created_at"),
        sa.UniqueConstraint("rule_id", "key_version", "normalization_version", "state",
                            name="uq_privacy_suppressions_rule_version_state"),
        sa.CheckConstraint("anchor_kind IN ('property', 'mailing', 'phone', 'email')",
                           name="ck_privacy_suppressions_anchor_kind"),
        sa.CheckConstraint(f"anchor_hmac {_HEX64}", name="ck_privacy_suppressions_anchor"),
        sa.CheckConstraint("subject_kind IN ('person', 'entity')",
                           name="ck_privacy_suppressions_subject_kind"),
        sa.CheckConstraint(f"subject_hmac {_HEX64}", name="ck_privacy_suppressions_subject"),
        # 'property_all_persons' is an owner decision (design section 5, [D]).
        sa.CheckConstraint("scope = 'subject'", name="ck_privacy_suppressions_scope"),
        sa.CheckConstraint("key_version ~ '^v[1-9][0-9]{0,3}$'",
                           name="ck_privacy_suppressions_key_version"),
        sa.CheckConstraint("normalization_version ~ '^n[1-9][0-9]{0,3}$'",
                           name="ck_privacy_suppressions_normalization_version"),
        sa.CheckConstraint("state = 'pending'",
                           name="ck_privacy_suppressions_activation_guard"),
    )

    op.execute(
        """
        CREATE FUNCTION public.privacy_rows_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
        BEGIN
            RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'BLD24';
        END
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.privacy_rows_immutable() FROM PUBLIC")
    for table in _TABLES:
        op.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON public.{table} "
                   "FOR EACH ROW EXECUTE FUNCTION public.privacy_rows_immutable()")
        op.execute(f"ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER {table}_no_update")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
    revokes = "\n".join(
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
        f"REVOKE ALL ON public.{table} FROM {role}; END IF;"
        for table in _TABLES
        for role in ("anon", "authenticated", "service_role", "bridgeleads_app",
                     "bridgeleads_system", "bridgeleads_purge")
    )
    op.execute(f"DO $privacy_revokes$\nBEGIN\n{revokes}\nEND\n$privacy_revokes$;")


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # Children first: suppressions and events reference the request envelope.
    op.drop_table("privacy_suppressions")
    op.drop_table("privacy_request_events")
    op.drop_table("privacy_request_payloads")
    op.drop_table("privacy_requests")
    op.execute("DROP FUNCTION IF EXISTS public.privacy_rows_immutable()")
