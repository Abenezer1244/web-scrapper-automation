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

import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.db.session import sync_engine
from src.utils.crypto import blind_index

# Needs provisioned RLS cutover roles + RLS_ENFORCE — excluded from the unit CI job.
pytestmark = pytest.mark.integration


def _seed_user(conn, *, referred_by: str | None = None) -> str:
    """Insert a minimal active user and return its id."""
    uid = str(uuid.uuid4())
    email = f"helper_{uid[:8]}@bl.test"
    conn.execute(
        text("""
            INSERT INTO users (
                id, email, email_hmac, password_hash, plan, records_used, records_limit,
                is_active, is_admin, referral_credit_cents, referred_by_user_id
            ) VALUES (
                :id, :email, :email_hmac, :pw, 'starter', 0, 50, true, false, 0, :ref
            )
        """),
        {
            "id": uid,
            "email": email,
            "email_hmac": blind_index(email),
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
        # The seeded user advances signup → first_scraper → first_job. It does
        # NOT advance first_download: the job carries an export_key, which is
        # what the worker writes when it marks a job done, and that is exactly
        # the thing this funnel used to miscount as a download (migration 090).
        # Assert deltas, not absolutes, so the test is robust against whatever
        # else is in the window.
        assert after.signups == base.signups + 1
        assert after.first_scraper == base.first_scraper + 1
        assert after.first_job == base.first_job + 1
        assert after.first_download == base.first_download
        # starter plan + no stripe_customer_id → not a paid upgrade.
        assert after.paid_upgrade == base.paid_upgrade

        # Only an OBSERVED download advances the step.
        conn.execute(
            text(
                "UPDATE users SET first_leads_downloaded_at = NOW() WHERE id = :u"
            ),
            {"u": user_id},
        )
        downloaded = conn.execute(
            text("SELECT * FROM public.activation_funnel(30)")
        ).fetchone()
        assert downloaded.first_download == base.first_download + 1

        # The onboarding grandfather flag is presentation state; the funnel must
        # not read it, or it would report a download nobody watched happen.
        conn.execute(
            text(
                "UPDATE users SET first_leads_downloaded_at = NULL, "
                "onboarding_download_grandfathered = true WHERE id = :u"
            ),
            {"u": user_id},
        )
        grandfathered = conn.execute(
            text("SELECT * FROM public.activation_funnel(30)")
        ).fetchone()
        assert grandfathered.first_download == base.first_download

        conn.rollback()


# ─── activation_funnel_v2: rates that cannot exceed 100% (migration 091) ─────

def _funnel_v2(conn, days: int = 30):
    return conn.execute(
        text("SELECT * FROM public.activation_funnel_v2(:d)"), {"d": days}
    ).fetchone()


def _mark_paid(conn, user_id: str) -> None:
    # stripe_customer_id is UNIQUE, so it has to differ per user.
    conn.execute(
        text(
            "UPDATE users SET plan='pro', stripe_customer_id = :cus WHERE id = :u"
        ),
        {"u": user_id, "cus": f"cus_{uuid.uuid4().hex[:16]}"},
    )


def _mark_downloaded(conn, user_id: str) -> None:
    conn.execute(
        text("UPDATE users SET first_leads_downloaded_at = NOW() WHERE id = :u"),
        {"u": user_id},
    )


def test_v2_returns_the_same_stage_counts_as_v1() -> None:
    """The bars must not move. Only the ratios were wrong."""
    with sync_engine.begin() as conn:
        v1 = conn.execute(
            text("SELECT * FROM public.activation_funnel(30)")
        ).fetchone()
        v2 = _funnel_v2(conn)

        assert v2.signups == v1.signups
        assert v2.first_scraper == v1.first_scraper
        assert v2.first_job == v1.first_job
        assert v2.first_download == v1.first_download
        assert v2.paid_upgrade == v1.paid_upgrade
        conn.rollback()


def test_paid_and_downloaded_are_counted_separately_from_their_overlap() -> None:
    """The 200% case, exactly.

    Two paid users and one downloader. Dividing the paid COUNT by the downloader
    count gives 200%; the intersection gives the real answer, which is that the
    one observed downloader is also paid.
    """
    with sync_engine.begin() as conn:
        base = _funnel_v2(conn)

        paid_downloader = _seed_user(conn)
        _mark_paid(conn, paid_downloader)
        _mark_downloaded(conn, paid_downloader)

        paid_only = _seed_user(conn)
        _mark_paid(conn, paid_only)

        after = _funnel_v2(conn)

        assert after.paid_upgrade == base.paid_upgrade + 2
        assert after.first_download == base.first_download + 1
        # The naive rate divided these two marginals: +2 paid over +1 downloader.
        assert (after.paid_upgrade - base.paid_upgrade) > (
            after.first_download - base.first_download
        )
        # The intersection only counts the user who did both.
        assert after.downloaded_and_paid == base.downloaded_and_paid + 1
        assert after.downloaded_and_paid <= after.first_download
        conn.rollback()


def test_a_downloader_who_never_paid_is_not_in_the_intersection() -> None:
    with sync_engine.begin() as conn:
        base = _funnel_v2(conn)

        u = _seed_user(conn)
        _mark_downloaded(conn, u)

        after = _funnel_v2(conn)
        assert after.first_download == base.first_download + 1
        assert after.downloaded_and_paid == base.downloaded_and_paid
        conn.rollback()


def test_a_user_with_many_jobs_is_counted_once() -> None:
    """EXISTS, not a JOIN: joining users to jobs would multiply the user."""
    with sync_engine.begin() as conn:
        base = _funnel_v2(conn)

        uid = _seed_user(conn)
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
            {"sc": sc_id, "u": uid},
        )
        for _ in range(3):
            conn.execute(
                text("""
                    INSERT INTO jobs (
                        id, user_id, scraper_config_id, status, trigger,
                        page_current, page_total, record_count, retry_count
                    ) VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 1, 0)
                """),
                {"j": str(uuid.uuid4()), "u": uid, "sc": sc_id},
            )
        _mark_downloaded(conn, uid)

        after = _funnel_v2(conn)
        assert after.first_job == base.first_job + 1
        assert after.scraper_and_job == base.scraper_and_job + 1
        assert after.job_and_download == base.job_and_download + 1
        conn.rollback()


def test_every_intersection_is_bounded_by_both_of_its_populations() -> None:
    """The invariant the whole change exists to guarantee."""
    with sync_engine.begin() as conn:
        # Seed a mix so the assertion is not vacuous on an empty database.
        both = _seed_user(conn)
        _mark_paid(conn, both)
        _mark_downloaded(conn, both)
        _mark_paid(conn, _seed_user(conn))
        _mark_downloaded(conn, _seed_user(conn))

        r = _funnel_v2(conn)
        assert r.scraper_and_job <= min(r.first_scraper, r.first_job)
        assert r.job_and_download <= min(r.first_job, r.first_download)
        assert r.downloaded_and_paid <= min(r.first_download, r.paid_upgrade)
        for n in (r.first_scraper, r.first_job, r.first_download, r.paid_upgrade):
            assert n <= r.signups
        conn.rollback()
