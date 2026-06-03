"""Phase 2b RLS cutover: SECURITY DEFINER helper functions (migration 029).

Proves public.grant_referral_credit() and public.activation_funnel() behave
correctly — the bounded cross-tenant primitives that replace direct app-role
writes/reads for the Stripe webhook and the admin funnel.

These functions are SECURITY DEFINER (owned by the migration role), so the
tests call them directly via the sync engine. Real DB, no mocks; everything is
seeded inside a transaction and rolled back, so no fixtures persist. Requires
migration 029 applied to the test database.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from src.api.auth import hash_password
from src.db.session import sync_engine


def _seed_user(conn, *, referred_by: str | None = None) -> str:
    """Insert a minimal active user and return its id."""
    uid = str(uuid.uuid4())
    conn.execute(
        text("""
            INSERT INTO users (
                id, email, password_hash, plan, records_used, records_limit,
                is_active, is_admin, referral_credit_cents, referred_by_user_id
            ) VALUES (
                :id, :email, :pw, 'starter', 0, 50, true, false, 0, :ref
            )
        """),
        {
            "id": uid,
            "email": f"helper_{uid[:8]}@bl.test",
            "pw": hash_password("testpassword123"),
            "ref": referred_by,
        },
    )
    return uid


def test_grant_referral_credit_idempotent() -> None:
    """One grant per referee, even if Stripe replays the webhook."""
    with sync_engine.begin() as conn:
        referrer = _seed_user(conn)
        referee = _seed_user(conn, referred_by=referrer)

        # First grant: exactly one audit row + a single $20 credit.
        conn.execute(text("SELECT public.grant_referral_credit(:r)"), {"r": referee})
        n = conn.execute(
            text("SELECT COUNT(*) FROM referral_events WHERE referee_id = :r"),
            {"r": referee},
        ).scalar()
        credit = conn.execute(
            text("SELECT referral_credit_cents FROM users WHERE id = :id"),
            {"id": referrer},
        ).scalar()
        assert n == 1, f"expected 1 referral_events row, got {n}"
        assert credit == 2000, f"expected 2000 cents credited, got {credit}"

        # Replay (Stripe retry): unique(referee_id) → no second row, and the
        # balance must NOT be double-incremented.
        conn.execute(text("SELECT public.grant_referral_credit(:r)"), {"r": referee})
        n2 = conn.execute(
            text("SELECT COUNT(*) FROM referral_events WHERE referee_id = :r"),
            {"r": referee},
        ).scalar()
        credit2 = conn.execute(
            text("SELECT referral_credit_cents FROM users WHERE id = :id"),
            {"id": referrer},
        ).scalar()
        assert n2 == 1, f"replay created a duplicate row: {n2}"
        assert credit2 == 2000, f"replay double-incremented credit: {credit2}"

        conn.rollback()


def test_grant_referral_credit_noop_without_referrer() -> None:
    """A user with no referrer triggers no grant and no error."""
    with sync_engine.begin() as conn:
        loner = _seed_user(conn)  # referred_by is NULL
        conn.execute(text("SELECT public.grant_referral_credit(:r)"), {"r": loner})
        n = conn.execute(
            text("SELECT COUNT(*) FROM referral_events WHERE referee_id = :r"),
            {"r": loner},
        ).scalar()
        assert n == 0, f"expected no grant for a referrer-less user, got {n}"
        conn.rollback()


def test_activation_funnel_counts_seeded_user() -> None:
    """The funnel aggregate counts a freshly-seeded signup through download."""
    with sync_engine.begin() as conn:
        base = conn.execute(text("SELECT * FROM public.activation_funnel(30)")).fetchone()

        user_id = _seed_user(conn)
        sc_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO scraper_configs (
                    id, user_id, name, county, state, record_type,
                    fields, enrichment, schedule, deliver,
                    skip_trace_enabled, active
                ) VALUES (
                    :sc, :u, 'cfg', 'pierce', 'WA', 'probate',
                    '[]'::json, '[]'::json, '{}'::json, '{}'::json, false, true
                )
            """),
            {"sc": sc_id, "u": user_id},
        )
        job_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO jobs (
                    id, user_id, scraper_config_id, status, trigger,
                    page_current, page_total, record_count, retry_count, export_key
                ) VALUES (
                    :j, :u, :sc, 'done', 'manual', 0, 0, 1, 0, 'exports/x.csv'
                )
            """),
            {"j": job_id, "u": user_id, "sc": sc_id},
        )

        after = conn.execute(text("SELECT * FROM public.activation_funnel(30)")).fetchone()
        # The seeded user advances signup → first_scraper → first_job →
        # first_download (export_key set). Assert deltas, not absolutes, so the
        # test is robust against whatever else is in the window.
        assert after.signups == base.signups + 1
        assert after.first_scraper == base.first_scraper + 1
        assert after.first_job == base.first_job + 1
        assert after.first_download == base.first_download + 1
        # starter plan + no stripe_customer_id → not a paid upgrade.
        assert after.paid_upgrade == base.paid_upgrade

        conn.rollback()
