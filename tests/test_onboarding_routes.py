"""GET /auth/onboarding must hand the dashboard a route the frontend serves.

The onboarding card renders ``next_action.route`` directly as a next/link href,
so a route the app router has no page for is a 404 in the user's face. Every
signed-in page lives inside the frontend's ``app/(dashboard)/`` route GROUP,
which contributes nothing to the URL, so a ``/dashboard/...`` prefix on anything
other than the dashboard itself is the exact bug these tests exist to catch.
"""
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import frontend_routes
from src.db.models import Job, ScraperConfig, User

# The pages the frontend actually serves, transcribed from the app router's
# page.tsx files. Deliberately a LITERAL list and not frontend_routes.ALL_PATHS:
# checking the module against itself would pass on a constant renamed to a page
# that does not exist. A next_action route is one of these, or a job detail page
# under /results.
#
# app/(auth)/ and app/(dashboard)/ are route GROUPS and contribute nothing to the
# URL, which is the whole reason this list looks the way it does.
FRONTEND_PAGES = frozenset(
    {
        "/",
        "/pricing",
        "/coverage",
        "/privacy",
        "/terms",
        "/login",
        "/register",
        "/forgot-password",
        "/reset-password",
        "/verify-email",
        "/dashboard",
        "/scrapers",
        "/scrapers/new",
        "/results",
        "/segments",
        "/deliver",
        "/settings",
        "/admin/connectors",
        "/admin/funnel",
    }
)


def _page_of(path: str) -> str:
    """Strip a query string, so "/settings?tab=billing" checks as "/settings"."""
    return path.split("?", 1)[0]


def assert_servable(route: str) -> None:
    """The route must be a page the frontend has, and must not fake a segment.

    ``/dashboard`` itself is a real page. ``/dashboard/anything`` is not: that
    prefix only ever came from mistaking a route group for a URL segment.
    """
    assert route.startswith("/"), f"{route!r} is not an app-relative path"
    assert not route.startswith("/dashboard/"), (
        f"{route!r} uses the (dashboard) route GROUP as a URL segment. "
        "The group contributes nothing to the URL, so this path 404s."
    )
    if _page_of(route) in FRONTEND_PAGES:
        return
    # The only dynamic page a next_action can point at is /results/<job id>.
    # Exactly one more segment: /results/<id>/anything is not a page either.
    prefix = frontend_routes.RESULTS + "/"
    assert route.startswith(prefix), f"{route!r} is not a page the frontend serves"
    tail = route[len(prefix):]
    assert tail and "/" not in tail, (
        f"{route!r} has extra path segments under /results; that is not a page"
    )


async def _onboarding(client: AsyncClient, token: str) -> dict:
    resp = await client.get(
        "/auth/onboarding", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    return resp.json()


# ─── Fresh account: the state that shipped the 404 ────────────────────────────

async def test_new_account_cta_opens_the_create_scraper_page(
    client: AsyncClient, starter_token: str
):
    """A brand-new account is told to create a scraper, at the page that exists."""
    data = await _onboarding(client, starter_token)
    action = data["next_action"]

    assert action["action"] == "create_scraper"
    assert action["cta"] == "New Scraper"
    assert action["route"] == "/scrapers/new"
    assert_servable(action["route"])


async def test_new_account_route_is_not_the_dead_dashboard_prefix(
    client: AsyncClient, starter_token: str
):
    """Regression: the CTA must never point back at /dashboard/scrapers/new."""
    data = await _onboarding(client, starter_token)
    assert data["next_action"]["route"] != "/dashboard/scrapers/new"


async def test_trial_user_on_pro_gets_the_same_creation_route(
    client: AsyncClient, db: AsyncSession
):
    """A Pro-trial account (how every new signup starts) reaches creation too.

    The reported 404 was hit on a Pro trial, so plan must not change the route.
    """
    from datetime import UTC, datetime, timedelta

    from src.api.auth import create_secure_token, hash_password

    user = User(
        id=str(uuid.uuid4()),
        email=f"trial_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=0,
        records_limit=1000,
        # A real signup carries a trial end date. Without it a route decision
        # gated on trial_ends_at would slip past this fixture untested.
        trial_ends_at=datetime.now(UTC) + timedelta(days=6),
    )
    db.add(user)
    await db.commit()

    data = await _onboarding(client, create_secure_token(user.id))
    assert data["next_action"]["route"] == "/scrapers/new"
    assert data["steps"]["scraper_configured"] is False
    # timedelta.days floors, and the handler reads the clock after the fixture
    # set the end date, so 6 days out always reports 5 whole days remaining.
    assert data["trial_days_remaining"] == 5


# ─── The later onboarding states ──────────────────────────────────────────────

async def test_configured_scraper_points_at_the_page_with_the_run_button(
    client: AsyncClient, starter_token: str, scraper_config: ScraperConfig
):
    """"Run Now" lives on a row of the scrapers list, so that is where we send them.

    There is no per-scraper detail page in the app, which is why the old
    /dashboard/scrapers/<id> route could not be fixed by dropping the prefix.
    """
    data = await _onboarding(client, starter_token)
    action = data["next_action"]

    assert action["action"] == "run_scrape"
    assert action["route"] == "/scrapers"
    assert_servable(action["route"])


async def test_running_job_points_at_the_dashboard(
    client: AsyncClient, starter_token: str, pending_job: Job
):
    """Waiting on a scrape keeps the user on /dashboard, which is a real page."""
    data = await _onboarding(client, starter_token)
    action = data["next_action"]

    assert action["action"] == "wait_for_scrape"
    assert action["route"] == "/dashboard"
    assert_servable(action["route"])


async def test_finished_job_points_at_its_results_page(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """The CSV download lives on /results/<job id>; there is no /jobs page."""
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
        record_count=12,
    )
    db.add(job)
    await db.commit()

    data = await _onboarding(client, starter_token)
    action = data["next_action"]

    assert action["action"] == "download_export"
    assert action["route"] == f"/results/{job.id}"
    assert_servable(action["route"])


async def test_completed_onboarding_points_at_the_create_scraper_page(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """The "Add Another County" CTA is the same creation page, not a dead route."""
    from datetime import UTC, datetime

    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
        record_count=12,
        export_key="exports/whatever.csv",
    )
    db.add(job)
    # The export existing is NOT the download. Only an observed one completes
    # the checklist (migration 089).
    starter_user.first_leads_downloaded_at = datetime.now(UTC)
    await db.commit()

    data = await _onboarding(client, starter_token)
    action = data["next_action"]

    assert action["action"] == "complete"
    assert action["route"] == "/scrapers/new"
    assert_servable(action["route"])
    assert data["progress_pct"] == 100


# ─── Auth ─────────────────────────────────────────────────────────────────────

async def test_onboarding_requires_authentication(client: AsyncClient):
    """No token means 401, never a route a caller could act on."""
    resp = await client.get("/auth/onboarding")
    assert resp.status_code in (401, 403)
    assert "next_action" not in resp.text


# ─── The shared route table ───────────────────────────────────────────────────

def test_every_declared_path_is_a_page_the_frontend_serves():
    """The route table must not drift onto a page that does not exist."""
    for path in frontend_routes.ALL_PATHS:
        assert _page_of(path) in FRONTEND_PAGES, path


def test_no_declared_path_uses_the_route_group_as_a_segment():
    """Every path the backend can hand out must survive the group-prefix rule."""
    for path in frontend_routes.ALL_PATHS:
        assert not path.startswith("/dashboard/"), path
    assert not frontend_routes.job_detail("abc").startswith("/dashboard/")


def test_referral_link_targets_the_signup_page_that_exists():
    """/signup is not a page and is not public; /register is both."""
    link = frontend_routes.referral_signup("ABC123")
    assert link == "/register?ref=ABC123"
    assert _page_of(link) in FRONTEND_PAGES
    assert not link.startswith("/signup")


def test_referral_link_escapes_the_code():
    assert frontend_routes.referral_signup("a b&c") == "/register?ref=a%20b%26c"


def test_job_detail_builds_the_results_page_path():
    assert frontend_routes.job_detail("job-123") == "/results/job-123"


def test_absolute_joins_frontend_url_without_doubling_the_slash(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.test/")
    assert (
        frontend_routes.absolute(frontend_routes.SCRAPERS_NEW)
        == "https://app.example.test/scrapers/new"
    )


# ─── The referral share link (same defect class, different endpoint) ──────────

async def test_referral_endpoint_shares_the_register_page_not_signup(
    client: AsyncClient, starter_token: str
):
    """Reverting this endpoint to /signup must fail here, not just in the helper.

    /signup is not a page and is not in the frontend middleware's public list, so
    a prospect following a shared link was bounced to /login and the ref code was
    dropped on the way.
    """
    resp = await client.get(
        "/billing/referral", headers={"Authorization": f"Bearer {starter_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()

    code = data["code"]
    assert code, "the endpoint must backfill a referral code"
    assert "/signup" not in data["share_url"]
    assert data["share_url"].endswith(f"/register?ref={code}")

    path = data["share_url"].split("bridgeleads.io", 1)[-1]
    assert_servable(path)


async def test_referral_share_url_is_absolute(
    client: AsyncClient, starter_token: str
):
    """It is pasted into messages and emails, so it must carry a host."""
    resp = await client.get(
        "/billing/referral", headers={"Authorization": f"Bearer {starter_token}"}
    )
    assert resp.json()["share_url"].startswith("https://")
