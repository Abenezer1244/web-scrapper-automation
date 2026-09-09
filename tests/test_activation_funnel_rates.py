"""Funnel rates must be bounded by the population they are a share of.

`download_to_paid` used to divide ALL paid users in the window by observed
downloaders. Those populations are not nested, so two paid users and one
downloader reported 200%. `job_to_download` and `scraper_to_job` had the same
shape. The fix is to divide by a real intersection (migration 091), and this
pins the arithmetic.
"""
import pytest

from src.api.routes.billing import step_conversions


def test_the_two_hundred_percent_case():
    """The exact reported shape: 2 paid, 1 downloader, and they overlap by 1."""
    rates = step_conversions(
        signups=10,
        first_scraper=5,
        first_job=4,
        first_download=1,
        scraper_and_job=4,
        job_and_download=1,
        downloaded_and_paid=1,
    )
    # The old maths was 100 * paid_upgrade(2) / first_download(1) = 200.0
    assert rates["download_to_paid"] == 100.0


def test_a_downloader_who_never_paid_drags_the_rate_down():
    rates = step_conversions(
        signups=10,
        first_scraper=5,
        first_job=4,
        first_download=4,
        scraper_and_job=4,
        job_and_download=4,
        downloaded_and_paid=1,
    )
    assert rates["download_to_paid"] == 25.0


def test_valid_counts_always_land_inside_zero_to_one_hundred():
    """The guarantee, stated honestly: it holds for counts SQL can produce.

    Every intersection here is <= both of its populations, which is what
    activation_funnel_v2 guarantees by computing them as COUNT(*) FILTER over one
    row per user.
    """
    rates = step_conversions(
        signups=100,
        first_scraper=60,
        first_job=45,
        first_download=30,
        scraper_and_job=45,
        job_and_download=30,
        downloaded_and_paid=12,
    )
    for key, value in rates.items():
        assert 0.0 <= value <= 100.0, f"{key} = {value}"


def test_an_impossible_intersection_is_reported_not_hidden():
    """It deliberately does not clamp.

    An intersection larger than its population means the SQL is broken. A min()
    here would turn that into a plausible-looking 100% and nobody would ever
    find it. This pins the choice so a future "tidy-up" cannot quietly add one.
    """
    rates = step_conversions(
        signups=10,
        first_scraper=5,
        first_job=4,
        first_download=1,
        scraper_and_job=4,
        job_and_download=1,
        downloaded_and_paid=2,  # impossible: 2 of 1 downloader
    )
    assert rates["download_to_paid"] == 200.0


@pytest.mark.parametrize(
    "zeroed",
    ["signups", "first_scraper", "first_job", "first_download"],
)
def test_a_zero_population_is_zero_not_a_crash(zeroed):
    """An empty window must not divide by zero."""
    counts = {
        "signups": 3,
        "first_scraper": 2,
        "first_job": 1,
        "first_download": 1,
        "scraper_and_job": 1,
        "job_and_download": 1,
        "downloaded_and_paid": 1,
    }
    counts[zeroed] = 0
    rates = step_conversions(**counts)
    assert all(0.0 <= v <= 100.0 for v in rates.values())


def test_an_empty_window_is_all_zeroes():
    rates = step_conversions(
        signups=0,
        first_scraper=0,
        first_job=0,
        first_download=0,
        scraper_and_job=0,
        job_and_download=0,
        downloaded_and_paid=0,
    )
    assert rates == {
        "signup_to_scraper": 0.0,
        "scraper_to_job": 0.0,
        "job_to_download": 0.0,
        "download_to_paid": 0.0,
    }


def test_each_rate_reads_its_own_intersection():
    """Distinct values, so a copy-paste between the three cannot pass unnoticed."""
    rates = step_conversions(
        signups=100,
        first_scraper=50,
        first_job=40,
        first_download=20,
        scraper_and_job=10,   # 20% of scrapers
        job_and_download=30,  # 75% of jobs
        downloaded_and_paid=1,  # 5% of downloaders
    )
    assert rates["signup_to_scraper"] == 50.0
    assert rates["scraper_to_job"] == 20.0
    assert rates["job_to_download"] == 75.0
    assert rates["download_to_paid"] == 5.0


# ─── The route itself, not just the helper ───────────────────────────────────

async def test_the_endpoint_returns_bounded_rates(client, db):
    """Codex's point: the helper being right proves nothing about the route.

    Reverting the handler to the old inline arithmetic left every test above
    green, because none of them went through the endpoint. This one does, with a
    population whose marginals differ from their intersections: 2 paid users, 1
    downloader, overlapping by 1. The old maths reported 200%.
    """
    import uuid
    from datetime import UTC, datetime

    from src.api.auth import create_secure_token, hash_password
    from src.db.models import User

    def _user(**over):
        u = User(
            id=str(uuid.uuid4()),
            email=f"funnel_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"),
            plan="starter",
            records_used=0,
            records_limit=50,
        )
        for k, v in over.items():
            setattr(u, k, v)
        return u

    # An admin who has enrolled MFA: require_admin 404s non-admins and 403s an
    # admin without enrollment. This route needs enrollment, not a fresh step-up.
    admin = _user(is_admin=True, mfa_enabled=True)
    paid_downloader = _user(
        plan="pro",
        stripe_customer_id=f"cus_{uuid.uuid4().hex[:16]}",
        first_leads_downloaded_at=datetime.now(UTC),
    )
    paid_only = _user(plan="pro", stripe_customer_id=f"cus_{uuid.uuid4().hex[:16]}")
    for u in (admin, paid_downloader, paid_only):
        db.add(u)
    await db.commit()

    resp = await client.get(
        "/billing/activation-funnel?days=30",
        headers={"Authorization": f"Bearer {create_secure_token(admin.id)}"},
    )
    assert resp.status_code == 200, resp.text
    rates = resp.json()["step_conversions"]

    for key, value in rates.items():
        assert 0.0 <= value <= 100.0, f"{key} = {value} from the live endpoint"
    # The two paid users and the single downloader are both in the window, so the
    # old marginal division would have produced at least 200 here.
    assert rates["download_to_paid"] <= 100.0
